#!/usr/bin/env python3
"""datascan.py: map a PS2 disc's data files for /console-levels.

  scan    inventory every file: size, type by magic, entropy, a table-of-contents guess
  unpack  split an AFS archive or a detected table-of-contents archive into files + index.json
  repack  rebuild the archive from that folder, same order, original alignment
  diff    which byte ranges changed between two files (or two folders)
  strings printable text with offsets (ASCII, Shift-JIS, UTF-16LE)
  files   the disc files the port looked up, read from a PS2Recomp runtime log

Every game's format is different. This gives the map and the knife; finding
what the bytes mean is per game (diff two levels, read the loader in Ghidra).

Python 3.8+ standard library only. Exit codes: 0 ok, 1 needs attention (or,
for diff, the files differ), 2 bad input.
"""

import argparse
import hashlib
import json
import math
import os
import re
import struct
import sys
import zlib

INDEX_NAME = "index.json"
HEAD_BYTES = 1 << 20
CHUNK = 4 << 20
MAX_ENTRIES = 65536
TOC_THRESHOLD = 0.6


class ToolError(Exception):
    """Bad input: exit 2 with a one-line message."""


class Refused(Exception):
    """A check failed: exit 1 with a one-line message."""


# ---------------------------------------------------------------- small helpers

def u32(buf, off):
    return struct.unpack_from("<I", buf, off)[0]


def align_up(v, a):
    return v if a <= 1 else (v + a - 1) // a * a


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def read_head(path, n=HEAD_BYTES):
    try:
        with open(path, "rb") as fh:
            return fh.read(n)
    except IsADirectoryError:
        raise ToolError("%s is a folder, not a file" % path)
    except OSError as e:
        raise ToolError("cannot read %s: %s" % (path, e.strerror or e))


def need_file(path):
    if not os.path.exists(path):
        raise ToolError("%s does not exist" % path)
    if not os.path.isfile(path):
        raise ToolError("%s is not a file" % path)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def entropy(data):
    if not data:
        return 0.0
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    n = float(len(data))
    e = 0.0
    for c in counts:
        if c:
            p = c / n
            e -= p * math.log(p, 2)
    return e


def sample_entropy(path, size, window=1 << 16):
    """Entropy of up to three 64 KB windows (start, middle, end)."""
    if size <= 0:
        return 0.0
    spots = [0] if size <= 3 * window else [0, size // 2 - window // 2, size - window]
    data = b""
    with open(path, "rb") as fh:
        for s in spots:
            fh.seek(max(0, s))
            data += fh.read(window)
    return entropy(data)


def entropy_label(e):
    if e < 1.0:
        return "mostly empty"
    if e < 6.0:
        return "structured"
    if e < 7.5:
        return "mixed"
    return "compressed/encrypted/ADPCM"


# ---------------------------------------------------------------- magic sniffing

def _elf_kind(path, head):
    if len(head) < 52:
        return "ELF", "truncated header"
    if head[4] != 1 or head[5] != 1:
        return "ELF", "not 32-bit little-endian"
    e_type, e_machine = struct.unpack_from("<HH", head, 16)
    if e_machine != 8:
        return "ELF", "machine %d, not MIPS" % e_machine
    if e_type == 0xFF80:
        return "IRX", "IOP module (ELF type 0xFF80)"
    names = _elf_section_names(path, head)
    if ".iopmod" in names:
        return "IRX", "IOP module (.iopmod section)"
    if e_type == 2:
        return "ELF", "MIPS executable (EE)"
    if e_type == 1:
        return "ELF", "MIPS relocatable (an overlay or library?)"
    return "ELF", "MIPS, type 0x%x" % e_type


def _elf_section_names(path, head):
    try:
        shoff = u32(head, 0x20)
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", head, 0x2E)
        size = os.path.getsize(path)
        if not shoff or shentsize < 40 or not shnum or shnum > 4096 or shstrndx >= shnum:
            return set()
        if shoff + shnum * shentsize > size:
            return set()
        with open(path, "rb") as fh:
            fh.seek(shoff)
            table = fh.read(shnum * shentsize)
            so = shstrndx * shentsize
            str_off, str_size = struct.unpack_from("<II", table, so + 16)
            if str_off + str_size > size or str_size > 1 << 20:
                return set()
            fh.seek(str_off)
            strtab = fh.read(str_size)
        names = set()
        for i in range(shnum):
            nm = u32(table, i * shentsize)
            if nm < len(strtab):
                names.add(strtab[nm:strtab.find(b"\0", nm) if strtab.find(b"\0", nm) >= 0 else len(strtab)]
                          .decode("latin-1"))
        return names
    except (struct.error, OSError):
        return set()


def _looks_text(head):
    if not head:
        return False
    sample = head[:4096]
    bad = sum(1 for b in sample if b < 9 or (13 < b < 32) or b == 127)
    return bad == 0


def sniff(path, head, size):
    """(kind, detail) for a file from its first bytes."""
    h4 = head[:4]
    if size == 0:
        return "empty", ""
    if h4 == b"TIM2":
        return "TIM2", "texture (/console-graphics)"
    if h4 in (b"VAGp", b"VAGi"):
        return "VAG", "ADPCM audio (/console-sound)"
    if head[:8] == b"IECSsreV":
        return "IECS", "Sony sound bank header (.hd/.sq); the samples live in the matching .bd"
    if h4 == b"IECS":
        return "IECS", "Sony sound data (IECS chunks)"
    if h4 == b"\x00\x00\x01\xba":
        return "MPEG-PS", "PSS/MPEG program stream (a movie)"
    if h4 == b"\x00\x00\x01\xb3":
        return "MPEG", "MPEG video elementary stream"
    if h4 == b"ipum":
        return "IPU", "IPU video (MPEG-2 intra frames)"
    if h4 == b"\x7fELF":
        return _elf_kind(path, head)
    if head[:3] == b"\x1f\x8b\x08":
        return "gzip", "compressed"
    if head[:4] == b"PK\x03\x04":
        return "ZIP", "archive"
    if h4 == b"RIFF":
        return "RIFF", head[8:12].decode("latin-1", "replace").strip() or "RIFF"
    if h4 == b"AFS\0":
        return "AFS", "CRI archive (unpack works)"
    if h4 == b"CPK ":
        return "CPK", "CRI CPK archive (not unpacked by this tool)"
    if h4 == b"PS2D":
        return "icon.sys", "memory card save icon info"
    if head[:28] == b"Sony PS2 Memory Card Format ":
        return "memcard", "PS2 memory card image (/console-saves)"
    if len(head) >= 0x8006 and head[0x8001:0x8006] == b"CD001":
        return "ISO9660", "disc image (/console-reverse)"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG", "image"
    if h4 == b"OggS":
        return "Ogg", "audio"
    if h4 == b"MThd":
        return "MIDI", "music sequence"
    if len(head) >= 2 and head[0] == 0x78 and head[1] in (0x01, 0x5E, 0x9C, 0xDA):
        try:
            zlib.decompressobj().decompress(head[:4096])
            return "zlib", "compressed stream"
        except zlib.error:
            pass
    if _looks_text(head):
        return "text", head[:40].split(b"\n")[0].decode("latin-1", "replace").strip()
    return "data", ""


# ---------------------------------------------------------------- table-of-contents detection

def _alignment(offsets):
    a = 2048
    while a > 1:
        if all(o % a == 0 for o in offsets):
            return a
        a //= 2
    return 1


def _score(entries, table_end, size, prior):
    """Score a candidate table. entries: list of (offset, size) in bytes."""
    real = [(o, s) for o, s in entries if not (o == 0 and s == 0)]
    if len(real) < 2 or size <= table_end:
        return None
    prev_off = -1
    prev_end = table_end
    for o, s in real:
        if o < table_end or o + s > size or o < prev_off or o < prev_end:
            return None
        prev_off = o
        prev_end = o + s
    zero = sum(1 for o, s in real if s == 0)
    if zero * 2 > len(real):
        return None
    offs = [o for o, _ in real]
    a = _alignment(offs)
    span = size - table_end
    coverage = min(1.0, sum(s for _, s in real) / float(span))
    pad = max(a, 16)
    gaps = [real[i + 1][0] - (real[i][0] + real[i][1]) for i in range(len(real) - 1)]
    gap_reg = (sum(1 for g in gaps if g < pad) / float(len(gaps))) if gaps else 1.0
    align = 1.0 if a >= 16 else (0.6 if a >= 4 else 0.3)
    front = real[0][0] - table_end
    front_fit = 1.0 if front < max(pad, 2048) else (0.5 if front < 65536 else 0.0)
    tail = size - (real[-1][0] + real[-1][1])
    tail_fit = 1.0 if tail < max(pad, 2048) else (0.5 if tail < 65536 else 0.0)
    n = len(real)
    count_factor = 0.75 if n == 2 else (0.9 if n == 3 else 1.0)
    score = (0.3 * coverage + 0.2 * gap_reg + 0.2 * align + 0.15 * front_fit + 0.15 * tail_fit)
    return round(score * count_factor * prior, 3), a, coverage


def _read_pairs(head, t0, n, osc, ssc, table_end, size):
    """(offset, size) pairs in bytes, or None at the first entry that cannot be right."""
    ents = []
    prev = table_end
    for i in range(n):
        o = u32(head, t0 + i * 8) * osc
        s = u32(head, t0 + i * 8 + 4) * ssc
        if o or s:
            if o < prev or o + s > size:
                return None
            prev = o + s
        ents.append((o, s))
    return ents


def _read_offsets(head, t0, n, osc, table_end, size):
    offs = []
    prev = table_end
    for i in range(n):
        o = u32(head, t0 + i * 4) * osc
        if o < prev or o > size:
            return None
        prev = o
        offs.append(o)
    return offs


def detect_toc(head, size, limit=5):
    """Try common PS2 table layouts at the start of a file. Best first."""
    cands = []
    hl = len(head)
    for h in (0, 4, 8, 12, 16):
        if h + 4 > hl:
            break
        n = u32(head, h)
        for kind in ("count+pairs", "count+offsets"):
            if not 1 <= n <= MAX_ENTRIES:
                continue
            stride = 8 if kind == "count+pairs" else 4
            t0 = h + 4
            t1 = t0 + n * stride
            if t1 > hl:
                continue
            for osc in (1, 2048):
                if kind == "count+pairs":
                    for ssc in (1, 2048):
                        ents = _read_pairs(head, t0, n, osc, ssc, t1, size)
                        r = _score(ents, t1, size, 1.0) if ents else None
                        if r:
                            cands.append({"kind": kind, "header": h, "count_at": h, "table_at": t0, "n": n,
                                          "off_scale": osc, "size_scale": ssc, "sentinel": False,
                                          "table_end": t1, "score": r[0], "alignment": r[1],
                                          "coverage": round(r[2], 3), "entries": ents})
                else:
                    offs = _read_offsets(head, t0, n, osc, t1, size)
                    if offs is None:
                        continue
                    for sentinel in (False, True):
                        if sentinel:
                            if t1 + 4 > hl:
                                continue
                            end = u32(head, t1) * osc
                            if end > size or not offs or end < offs[-1]:
                                continue
                            tend = t1 + 4
                        else:
                            end = size
                            tend = t1
                        ents = [(offs[i], (offs[i + 1] if i + 1 < n else end) - offs[i]) for i in range(n)]
                        if any(s < 0 for _, s in ents):
                            continue
                        r = _score(ents, tend, size, 0.85)
                        if r:
                            cands.append({"kind": kind, "header": h, "count_at": h, "table_at": t0, "n": n,
                                          "off_scale": osc, "size_scale": 1, "sentinel": sentinel,
                                          "table_end": tend, "score": r[0], "alignment": r[1],
                                          "coverage": round(r[2], 3), "entries": ents})
        # tables without a count: the first offset says where the table ends
        if h + 8 > hl:
            continue
        for osc in (1, 2048):
            first = u32(head, h) * osc
            if first <= h or first > size:
                continue
            for kind, stride in (("pairs", 8), ("offsets", 4)):
                n = (first - h) // stride
                if not 2 <= n <= MAX_ENTRIES or h + n * stride > hl:
                    continue
                # trailing zero entries are padding before the data
                if kind == "pairs":
                    m = n
                    while m and u32(head, h + (m - 1) * 8) == 0 and u32(head, h + (m - 1) * 8 + 4) == 0:
                        m -= 1
                    ents = _read_pairs(head, h, m, osc, 1, h + n * stride, size)
                    r = _score(ents, h + n * stride, size, 0.9) if ents and len(ents) >= 2 else None
                    if r:
                        cands.append({"kind": kind, "header": h, "count_at": None, "table_at": h, "n": len(ents),
                                      "off_scale": osc, "size_scale": 1, "sentinel": False,
                                      "table_end": h + n * stride, "score": r[0], "alignment": r[1],
                                      "coverage": round(r[2], 3), "entries": ents})
                else:
                    m = n
                    while m and u32(head, h + (m - 1) * 4) == 0:
                        m -= 1
                    offs = _read_offsets(head, h, m, osc, h + n * stride, size)
                    if not offs or len(offs) < 2:
                        continue
                    ents = [(offs[i], (offs[i + 1] if i + 1 < len(offs) else size) - offs[i]) for i in range(len(offs))]
                    if any(s < 0 for _, s in ents):
                        continue
                    r = _score(ents, h + n * stride, size, 0.8)
                    if r:
                        cands.append({"kind": kind, "header": h, "count_at": None, "table_at": h, "n": len(ents),
                                      "off_scale": osc, "size_scale": 1, "sentinel": False,
                                      "table_end": h + n * stride, "score": r[0], "alignment": r[1],
                                      "coverage": round(r[2], 3), "entries": ents})
    cands.sort(key=lambda c: (-c["score"], c["header"], c["kind"]))
    return cands[:limit]


# ---------------------------------------------------------------- AFS

def parse_afs(path):
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(8)
        if len(head) < 8 or head[:4] != b"AFS\0":
            raise ToolError("%s is not an AFS archive (no 'AFS\\0' magic)" % path)
        n = u32(head, 4)
        if not 1 <= n <= MAX_ENTRIES:
            raise ToolError("AFS entry count %d is implausible" % n)
        toc = fh.read(8 * n + 8)
        if len(toc) < 8 * n:
            raise ToolError("AFS table of contents is truncated")
        ents = [struct.unpack_from("<II", toc, 8 * i) for i in range(n)]
        for i, (o, s) in enumerate(ents):
            if (o or s) and (o < 8 + 8 * n or o + s > size):
                raise ToolError("AFS entry %d (offset 0x%x, size %d) points outside the file (%d bytes): truncated?"
                                % (i, o, s, size))
        real = sorted(o for o, s in ents if o or s)
        first = real[0] if real else align_up(8 + 8 * n, 2048)
        meta = None
        where = None
        cands = []
        if 8 + 8 * n + 8 <= first and len(toc) >= 8 * n + 8:
            cands.append(("after_toc", 8 + 8 * n, struct.unpack_from("<II", toc, 8 * n)))
        if first - 8 >= 8 + 8 * n and first - 8 != 8 + 8 * n:
            fh.seek(first - 8)
            pair = fh.read(8)
            if len(pair) == 8:
                cands.append(("before_data", first - 8, struct.unpack_from("<II", pair, 0)))
        for w, at, (mo, ml) in cands:
            if mo >= first and ml >= 48 * n and mo + 48 * n <= size:
                meta = (mo, ml, at)
                where = w
                break
        names = [None] * n
        dates = [None] * n
        rule = None
        raw_tail = None
        if meta:
            fh.seek(meta[0])
            mt = fh.read(48 * n)
            fh.seek(0)
            hdr_words = fh.read(4 + 4 * n + 4)
            tails = []
            for i in range(n):
                rec = mt[48 * i:48 * i + 48]
                names[i] = rec[:32].split(b"\0")[0].decode("latin-1")
                dates[i] = list(struct.unpack_from("<6H", rec, 32))
                tails.append(u32(rec, 44))
            if all(tails[i] == ents[i][1] for i in range(n)):
                rule = "size"
            elif all(tails[i] == ents[i][0] for i in range(n)):
                rule = "offset"
            elif len(hdr_words) >= 4 + 4 * n + 4 and all(tails[i] == u32(hdr_words, 4 + 4 * i) for i in range(n)):
                rule = "header_seq"
            else:
                rule = "raw"
                raw_tail = tails
    return {"n": n, "entries": ents, "first": first, "meta": meta, "meta_where": where,
            "names": names, "dates": dates, "name_rule": rule, "raw_tail": raw_tail, "size": size}


# ---------------------------------------------------------------- scan

def scan_file(path, deep=False, toc=True):
    size = os.path.getsize(path)
    head = read_head(path)
    kind, detail = sniff(path, head, size)
    row = {"path": path, "size": size, "kind": kind, "detail": detail,
           "entropy": round(sample_entropy(path, size), 2)}
    row["entropy_label"] = entropy_label(row["entropy"])
    if kind == "AFS":
        try:
            afs = parse_afs(path)
            row["toc"] = {"kind": "AFS", "n": afs["n"], "score": 1.0,
                          "names": afs["meta"] is not None}
        except ToolError as e:
            row["toc"] = {"kind": "AFS", "error": str(e)}
    elif toc and kind in ("data", "IECS") and size >= 64:
        c = detect_toc(head, size, limit=1)
        if c and c[0]["score"] >= TOC_THRESHOLD:
            best = dict(c[0])
            best.pop("entries", None)
            row["toc"] = best
    if deep:
        row["embedded"] = embedded_signatures(path)
    return row


EMBED = [(b"TIM2", "TIM2"), (b"VAGp", "VAG"), (b"IECSsreV", "IECS"), (b"AFS\0", "AFS"),
         (b"\x7fELF", "ELF"), (b"\x00\x00\x01\xba", "MPEG-PS"), (b"PS2D", "icon.sys")]


def embedded_signatures(path, cap=20):
    pat = re.compile(b"|".join(re.escape(m) for m, _ in EMBED))
    names = dict(EMBED)
    found = {}
    with open(path, "rb") as fh:
        base = 0
        carry = b""
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            data = carry + block
            for m in pat.finditer(data):
                if m.end() <= len(carry):
                    continue  # already counted in the previous chunk
                k = names[m.group(0)]
                rec = found.setdefault(k, {"count": 0, "first_offsets": []})
                rec["count"] += 1
                if len(rec["first_offsets"]) < 5:
                    rec["first_offsets"].append(base - len(carry) + m.start())
            carry = data[-7:]
            base += len(block)
    return found


def walk(target):
    if os.path.isfile(target):
        yield target
        return
    for base, dirs, files in os.walk(target):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(base, f)
            if os.path.isfile(p) and not os.path.islink(p):
                yield p


def cmd_scan(args):
    if not os.path.exists(args.target):
        raise ToolError("%s does not exist" % args.target)
    rows = []
    for p in walk(args.target):
        if os.path.getsize(p) < args.min_size:
            continue
        r = scan_file(p, deep=args.embedded)
        r["path"] = os.path.relpath(p, args.target) if os.path.isdir(args.target) else p
        rows.append(r)
    if args.sort == "size":
        rows.sort(key=lambda r: -r["size"])
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no files")
        return 0
    print("%-10s %-9s %-5s %-24s %s" % ("size", "kind", "ent", "table of contents", "path"))
    for r in rows:
        t = r.get("toc")
        if t is None:
            ts = ""
        elif t.get("kind") == "AFS":
            ts = ("AFS, %d entries%s" % (t["n"], ", names" if t.get("names") else "")) if "n" in t else "AFS (broken)"
        else:
            ts = "%s n=%d (%.2f)" % (t["kind"], t["n"], t["score"])
        print("%-10s %-9s %-5.2f %-24s %s%s" % (human(r["size"]), r["kind"], r["entropy"], ts, r["path"],
                                                ("  [%s]" % r["detail"]) if r["detail"] and r["kind"] not in ("data",) else ""))
        if args.embedded and r.get("embedded"):
            print("           inside: " + ", ".join("%s x%d (first 0x%x)" % (k, v["count"], v["first_offsets"][0])
                                                     for k, v in sorted(r["embedded"].items())))
    kinds = {}
    for r in rows:
        k = kinds.setdefault(r["kind"], [0, 0])
        k[0] += 1
        k[1] += r["size"]
    print("\n%d files, %s. By kind: %s" % (len(rows), human(sum(r["size"] for r in rows)),
                                           ", ".join("%s %d (%s)" % (k, v[0], human(v[1]))
                                                     for k, v in sorted(kinds.items(), key=lambda kv: -kv[1][1]))))
    big = sorted(rows, key=lambda r: -r["size"])[:5]
    print("Biggest: " + ", ".join("%s (%s)" % (r["path"], human(r["size"])) for r in big))
    return 0


# ---------------------------------------------------------------- unpack

def _safe_name(name, fallback):
    name = name.replace("\\", "/").split("/")[-1]
    name = "".join(c if (c.isalnum() or c in "-_.") and ord(c) < 128 else "_" for c in name).strip(".")
    return name[:64] or fallback


def _guess_ext(blob):
    k, _ = sniff("", blob[:64], len(blob)) if blob else ("empty", "")
    return {"TIM2": ".tm2", "VAG": ".vag", "AFS": ".afs", "ELF": ".elf", "IRX": ".irx", "MPEG-PS": ".pss",
            "IECS": ".hd", "gzip": ".gz", "ZIP": ".zip", "RIFF": ".riff", "text": ".txt", "PNG": ".png",
            "icon.sys": ".sys"}.get(k, ".bin")


def _pad_byte(fh, ranges):
    counts = {}
    for a, b in ranges:
        if b - a <= 0:
            continue
        fh.seek(a)
        chunk = fh.read(min(b - a, 4096))
        for x in chunk:
            counts[x] = counts.get(x, 0) + 1
    if not counts:
        return 0
    return max(counts.items(), key=lambda kv: (kv[1], kv[0] == 0))[0]


def _is_pad(fh, a, b, pad):
    fh.seek(a)
    left = b - a
    pb = bytes([pad]) * 65536
    while left > 0:
        chunk = fh.read(min(left, 65536))
        if not chunk:
            return True
        if chunk != pb[:len(chunk)]:
            return False
        left -= len(chunk)
    return True


def cmd_unpack(args):
    need_file(args.file)
    size = os.path.getsize(args.file)
    out = args.out
    if os.path.exists(out) and os.listdir(out) and not args.force:
        raise ToolError("%s is not empty (pass --force to write into it)" % out)
    head = read_head(args.file)
    fmt = args.format
    if fmt == "auto":
        fmt = "afs" if head[:4] == b"AFS\0" else "toc"
    index = {"tool": "datascan.py", "version": 1, "format": fmt, "source": os.path.basename(args.file),
             "source_size": size, "source_sha256": sha256_file(args.file)}
    regen = []  # byte ranges rebuilt by repack (not extras)
    if fmt == "afs":
        afs = parse_afs(args.file)
        ents = afs["entries"]
        names = afs["names"]
        layout = {"kind": "afs", "n": afs["n"], "table_at": 8, "count_at": 4, "off_scale": 1, "size_scale": 1,
                  "sentinel": False}
        first = afs["first"]
        if afs["meta"]:
            mo, ml, at = afs["meta"]
            index["afs"] = {"meta_where": afs["meta_where"], "meta_ptr_at": at, "meta_offset": mo,
                            "meta_length": ml, "name_rule": afs["name_rule"], "raw_tail": afs["raw_tail"]}
            regen.append((mo, mo + 48 * afs["n"]))
        else:
            index["afs"] = {"meta_where": None}
    else:
        cands = detect_toc(head, size)
        if args.header is not None or args.layout:
            cands = [c for c in cands if (args.header is None or c["header"] == args.header)
                     and (not args.layout or c["kind"] == args.layout)]
        if not cands or cands[0]["score"] < (0.0 if args.force_toc else TOC_THRESHOLD):
            best = cands[0]["score"] if cands else 0
            raise Refused("no table of contents found in %s (best guess scored %.2f, need %.2f). This format needs"
                          " per-game work: find the loader in Ghidra, or try --layout/--header with --force-toc."
                          % (args.file, best, TOC_THRESHOLD))
        c = cands[0]
        ents = c["entries"]
        names = [None] * len(ents)
        layout = {k: c[k] for k in ("kind", "n", "header", "count_at", "table_at", "off_scale", "size_scale",
                                    "sentinel", "table_end", "score")}
        real = [o for o, s in ents if o or s]
        first = min(real) if real else c["table_end"]
    real_sorted = sorted((o, s) for o, s in ents if o or s)
    align = max(_alignment([o for o, _ in real_sorted]) if real_sorted else 1, layout["off_scale"])
    index["layout"] = layout
    index["alignment"] = align
    files_dir = os.path.join(out, "files")
    os.makedirs(files_dir, exist_ok=True)
    entries = []
    used = set()
    with open(args.file, "rb") as fh:
        # header bytes (table included, patched on repack)
        fh.seek(0)
        hdr = fh.read(first)
        with open(os.path.join(out, "_header.bin"), "wb") as hf:
            hf.write(hdr)
        index["header_file"] = "_header.bin"
        # slots: room up to the next thing in the file
        bounds = sorted(set([o for o, _ in real_sorted] + [a for a, _ in regen] + [size]))
        for i, (o, s) in enumerate(ents):
            e = {"index": i, "offset": o, "size": s}
            if fmt == "afs":
                e["name"] = names[i]
                e["date"] = afs["dates"][i]
            if not (o or s):
                e.update({"file": None, "slot": 0, "empty": True})
                entries.append(e)
                continue
            nxt = [b for b in bounds if b > o]
            e["slot"] = (nxt[0] if nxt else size) - o if s else 0
            fh.seek(o)
            blob = fh.read(s)
            base = _safe_name(names[i], "") if names[i] else ""
            fname = "%04d_%s" % (i, base) if base else "%04d%s" % (i, _guess_ext(blob))
            while fname in used:
                fname = "_" + fname
            used.add(fname)
            with open(os.path.join(files_dir, fname), "wb") as ef:
                ef.write(blob)
            e["file"] = "files/" + fname
            e["sha256"] = hashlib.sha256(blob).hexdigest()
            entries.append(e)
        # what lies between and after the entries: padding, or extra bytes to keep
        covered = sorted([(o, o + s) for o, s in real_sorted] + regen)
        gaps = []
        pos = first
        for a, b in covered:
            if a > pos:
                gaps.append((pos, a))
            pos = max(pos, b)
        if pos < size:
            gaps.append((pos, size))
        pad = _pad_byte(fh, gaps)
        last_end = max([b for _a, b in covered] + [first])
        extras = []
        for a, b in gaps:
            if not _is_pad(fh, a, b, pad):
                fh.seek(a)
                name = "_extra_%08x.bin" % a
                with open(os.path.join(out, name), "wb") as xf:
                    xf.write(fh.read(b - a))
                extras.append({"offset": a, "length": b - a, "file": name, "after_last": a >= last_end})
    index["pad_byte"] = pad
    index["extras"] = extras
    index["entries"] = entries
    with open(os.path.join(out, INDEX_NAME), "w") as fh:
        json.dump(index, fh, indent=1)
    n_files = sum(1 for e in entries if e["file"])
    if args.json:
        print(json.dumps({"out": out, "format": fmt, "entries": n_files, "alignment": align,
                          "extras": len(extras)}, indent=2))
    else:
        desc = "AFS" if fmt == "afs" else "%s table at 0x%x (confidence %.2f)" % (layout["kind"], layout["table_at"],
                                                                                 layout["score"])
        print("unpacked %d entries from %s (%s, %d-byte alignment) into %s"
              % (n_files, args.file, desc, align, files_dir))
        if extras:
            print("kept %d byte range(s) outside the entries as _extra_*.bin (repack puts them back)" % len(extras))
        print("edit the files (same names), then: datascan.py repack %s --out <new archive>" % out)
    return 0


# ---------------------------------------------------------------- repack

def _load_index(folder):
    p = os.path.join(folder, INDEX_NAME)
    if not os.path.isfile(p):
        raise ToolError("%s has no %s (run unpack first)" % (folder, INDEX_NAME))
    try:
        with open(p) as fh:
            idx = json.load(fh)
    except ValueError:
        raise ToolError("%s is not valid JSON" % p)
    if idx.get("tool") != "datascan.py" or "entries" not in idx or "layout" not in idx:
        raise ToolError("%s was not written by datascan.py unpack" % p)
    return idx


def _readall(path):
    with open(path, "rb") as fh:
        return fh.read()


def _inside(folder, rel):
    if rel is None:
        return None
    if os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
        raise ToolError("index.json names a path outside the folder: %r" % rel)
    p = os.path.join(folder, rel)
    if not os.path.isfile(p):
        raise ToolError("missing %s (named in index.json)" % p)
    return p


def build_archive(folder, mode):
    """Rebuild from an unpacked folder. Returns (bytes, report)."""
    idx = _load_index(folder)
    lay = idx["layout"]
    fmt = idx["format"]
    align = int(idx.get("alignment", 1)) or 1
    pad = int(idx.get("pad_byte", 0))
    header = bytearray(_readall(_inside(folder, idx["header_file"])))
    first = len(header)
    size0 = int(idx["source_size"])
    ents = idx["entries"]
    data = []
    for e in ents:
        p = _inside(folder, e.get("file"))
        data.append(_readall(p) if p else None)
    changed = [e["index"] for e, d in zip(ents, data)
               if d is not None and hashlib.sha256(d).hexdigest() != e.get("sha256")]
    fits = all(d is None or len(d) <= e["slot"] for e, d in zip(ents, data))
    if mode == "keep" and not fits:
        bad = [(e["index"], len(d), e["slot"]) for e, d in zip(ents, data) if d is not None and len(d) > e["slot"]]
        raise Refused("entry %d is now %d bytes but its slot holds %d; --layout keep cannot move anything"
                      " (shrink it, or use --layout pack if the game reads offsets from this table)" % bad[0])
    use_keep = mode == "keep" or (mode == "auto" and fits)
    afs = idx.get("afs") or {}
    meta_len = 48 * len(ents) if afs.get("meta_where") else 0
    meta_room = max(meta_len, int(afs.get("meta_length") or 0)) if meta_len else 0
    placed = {}
    if use_keep:
        for e, d in zip(ents, data):
            if d is not None:
                placed[e["index"]] = e["offset"]
        end = size0
        meta_at = afs.get("meta_offset")
    else:
        pos = first
        for e, d in zip(ents, data):
            if d is None:
                continue
            pos = align_up(pos, align)
            placed[e["index"]] = pos
            pos += len(d)
        meta_at = None
        if meta_len:
            pos = align_up(pos, align)
            meta_at = pos
            pos += meta_room
        tail_extras = [x for x in idx.get("extras", []) if x.get("after_last")]
        for x in tail_extras:
            pos = align_up(pos, align)
            x["_new"] = pos
            pos += x["length"]
        end = align_up(pos, align)
    out = bytearray([pad]) * end
    out[0:first] = header
    # entries
    sizes = {}
    for e, d in zip(ents, data):
        if d is None:
            continue
        o = placed[e["index"]]
        if lay["size_scale"] > 1:
            d = d + bytes([pad]) * (align_up(len(d), lay["size_scale"]) - len(d))
        out[o:o + len(d)] = d
        sizes[e["index"]] = len(d)
    # extras
    dropped = 0
    for x in idx.get("extras", []):
        blob = _readall(_inside(folder, x["file"]))
        if use_keep:
            out[x["offset"]:x["offset"] + len(blob)] = blob
        elif "_new" in x:
            out[x["_new"]:x["_new"] + len(blob)] = blob
        else:
            dropped += len(blob)
    # table fields
    n = len(ents)
    osc, ssc = lay["off_scale"], lay["size_scale"]
    kind = lay["kind"]

    def put(at, v):
        if at + 4 > first:
            raise ToolError("table field at 0x%x is outside the saved header" % at)
        struct.pack_into("<I", out, at, v)

    offs_bytes = []
    for e in ents:
        if e["index"] in placed:
            offs_bytes.append((placed[e["index"]], sizes[e["index"]]))
        else:
            offs_bytes.append((0, 0))
    for i, (o, s) in enumerate(offs_bytes):
        if o % osc:
            raise ToolError("entry %d offset 0x%x is not a multiple of the table's unit %d" % (i, o, osc))
        if kind in ("afs", "count+pairs", "pairs"):
            put(lay["table_at"] + 8 * i, o // osc)
            put(lay["table_at"] + 8 * i + 4, -(-s // ssc))
        else:
            if (o, s) == (0, 0):
                o = end if i == n - 1 else 0
            put(lay["table_at"] + 4 * i, o // osc)
    if kind == "count+offsets" and lay.get("sentinel"):
        last_end = max(o + s for o, s in offs_bytes) if offs_bytes else first
        put(lay["table_at"] + 4 * n, align_up(last_end, osc) // osc)
    # AFS name table
    if meta_len:
        ptr_at = afs["meta_ptr_at"]
        if ptr_at + 8 > len(out):
            raise ToolError("AFS name-table pointer position is outside the archive")
        struct.pack_into("<II", out, ptr_at, meta_at, int(afs.get("meta_length") or meta_len))
        rule = afs.get("name_rule")
        raw_tail = afs.get("raw_tail") or []
        for i, e in enumerate(ents):
            rec = bytearray(48)
            nm = (e.get("name") or "").encode("latin-1", "replace")[:32]
            rec[0:len(nm)] = nm
            struct.pack_into("<6H", rec, 32, *[int(v) & 0xFFFF for v in (e.get("date") or [0] * 6)])
            o, s = offs_bytes[i]
            if rule == "size":
                t = s
            elif rule == "offset":
                t = o
            elif rule == "header_seq":
                t = u32(out, 4 + 4 * i)
            else:
                t = raw_tail[i] if i < len(raw_tail) else 0
            struct.pack_into("<I", rec, 44, t)
            out[meta_at + 48 * i:meta_at + 48 * i + 48] = rec
    report = {"layout": "keep" if use_keep else "pack", "changed": changed, "size": len(out),
              "original_size": size0, "dropped_extra_bytes": dropped,
              "moved": sum(1 for e in ents if e["index"] in placed and placed[e["index"]] != e["offset"])}
    return bytes(out), report


def cmd_repack(args):
    if not os.path.isdir(args.folder):
        raise ToolError("%s is not a folder" % args.folder)
    if os.path.exists(args.out) and not args.force:
        raise ToolError("%s exists (pass --force to overwrite; never overwrite your only copy of the original)" % args.out)
    blob, rep = build_archive(args.folder, args.layout)
    tmp = args.out + ".tmp"
    d = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(d, exist_ok=True)
    with open(tmp, "wb") as fh:
        fh.write(blob)
    os.replace(tmp, args.out)
    # verify: every entry reads back
    idx = _load_index(args.folder)
    ok = _verify(args.out, idx, args.folder)
    if args.json:
        rep["verified"] = ok
        rep["out"] = args.out
        print(json.dumps(rep, indent=2))
    else:
        print("wrote %s: %s (%s before), layout %s, %d entr%s changed%s"
              % (args.out, human(rep["size"]), human(rep["original_size"]), rep["layout"], len(rep["changed"]),
                 "y" if len(rep["changed"]) == 1 else "ies",
                 (", %d moved to new offsets" % rep["moved"]) if rep["moved"] else ""))
        if rep["layout"] == "pack" and rep["moved"]:
            print("note: offsets changed. Fine when the game reads this table; if it hard-codes offsets (in the ELF),"
                  " shrink your edit and use --layout keep.")
        if rep["dropped_extra_bytes"]:
            print("WARNING: %d bytes found between entries were dropped by the pack layout" % rep["dropped_extra_bytes"])
        print("verified: every entry reads back" if ok else "WARNING: read-back check failed")
    return 0 if ok and not rep["dropped_extra_bytes"] else 1


def _verify(path, idx, folder):
    size = os.path.getsize(path)
    lay = idx["layout"]
    with open(path, "rb") as fh:
        head = fh.read(max(lay.get("table_at", 8) + 8 * len(idx["entries"]) + 8, 64))
        for i, e in enumerate(idx["entries"]):
            p = _inside(folder, e.get("file"))
            if not p:
                continue
            want = _readall(p)
            if lay["kind"] in ("afs", "count+pairs", "pairs"):
                o = u32(head, lay["table_at"] + 8 * i) * lay["off_scale"]
            else:
                o = u32(head, lay["table_at"] + 4 * i) * lay["off_scale"]
            if o + len(want) > size:
                return False
            fh.seek(o)
            if fh.read(len(want)) != want:
                return False
    return True


# ---------------------------------------------------------------- diff

def diff_ranges(pa, pb, gap=8, keep=200):
    """Differing byte ranges between two files: (ranges, differing_bytes, size_a, size_b)."""
    sa, sb = os.path.getsize(pa), os.path.getsize(pb)
    ranges = []
    count = 0
    cur = None
    extra = 0
    with open(pa, "rb") as fa, open(pb, "rb") as fb:
        off = 0
        while True:
            a = fa.read(CHUNK)
            b = fb.read(CHUNK)
            n = min(len(a), len(b))
            if n == 0:
                break
            if a[:n] != b[:n]:
                i = 0
                while i < n:
                    j = min(i + 256, n)
                    if a[i:j] != b[i:j]:
                        for k in range(i, j):
                            if a[k] != b[k]:
                                p = off + k
                                count += 1
                                if cur is not None and p - cur[1] <= gap:
                                    cur[1] = p + 1
                                else:
                                    if cur is not None:
                                        if len(ranges) < keep:
                                            ranges.append(tuple(cur))
                                        else:
                                            extra += 1
                                    cur = [p, p + 1]
                    i = j
            off += n
            if len(a) != len(b):
                break
    if cur is not None:
        if len(ranges) < keep:
            ranges.append(tuple(cur))
        else:
            extra += 1
    return ranges, count, sa, sb, extra


def _values(path, a, b, as_type):
    w = {"u8": 1, "u16": 2, "u32": 4, "f32": 4}[as_type]
    fmt = {"u8": "<B", "u16": "<H", "u32": "<I", "f32": "<f"}[as_type]
    start = a - a % w
    with open(path, "rb") as fh:
        fh.seek(start)
        blob = fh.read(min(align_up(b, w) - start, 4 * w))
    vals = []
    for i in range(0, len(blob) - w + 1, w):
        v = struct.unpack_from(fmt, blob, i)[0]
        vals.append(("%g" % v) if as_type == "f32" else str(v))
    return start, vals


def _preview(path, a, n=16):
    with open(path, "rb") as fh:
        fh.seek(a)
        return fh.read(n).hex()


def cmd_diff(args):
    a, b = args.a, args.b
    for p in (a, b):
        if not os.path.exists(p):
            raise ToolError("%s does not exist" % p)
    if os.path.isdir(a) != os.path.isdir(b):
        raise ToolError("compare two files or two folders, not one of each")
    if os.path.isdir(a):
        return _diff_dirs(args)
    ranges, count, sa, sb, extra = diff_ranges(a, b, args.gap, args.max)
    res = {"a": a, "b": b, "size_a": sa, "size_b": sb, "differing_bytes": count,
           "ranges": [], "more_ranges": extra}
    for r0, r1 in ranges:
        row = {"start": r0, "end": r1, "length": r1 - r0, "a": _preview(a, r0), "b": _preview(b, r0)}
        if args.as_type:
            st, va = _values(a, r0, r1, args.as_type)
            _, vb = _values(b, r0, r1, args.as_type)
            row["values_at"] = st
            row["a_values"], row["b_values"] = va, vb
        res["ranges"].append(row)
    same = count == 0 and sa == sb
    if args.json:
        print(json.dumps(res, indent=2))
        return 0 if same else 1
    if same:
        print("identical (%d bytes)" % sa)
        return 0
    if sa != sb:
        print("sizes differ: %s is %d bytes, %s is %d bytes (the last %d bytes exist only in the %s file)"
              % (a, sa, b, sb, abs(sa - sb), "second" if sb > sa else "first"))
    print("%d differing bytes in %d range(s)%s (ranges closer than %d bytes merged)"
          % (count, len(ranges) + extra, (", first %d shown" % len(ranges)) if extra else "", args.gap))
    for row in res["ranges"]:
        print("  0x%08x-0x%08x  %6d bytes  a=%s  b=%s" % (row["start"], row["end"] - 1, row["length"], row["a"], row["b"]))
        if args.as_type:
            print("      as %s from 0x%x: a=[%s] b=[%s]" % (args.as_type, row["values_at"], ", ".join(row["a_values"]),
                                                          ", ".join(row["b_values"])))
    return 1


def _rel_files(root):
    out = {}
    for p in walk(root):
        out[os.path.relpath(p, root).replace(os.sep, "/")] = p
    return out


def _diff_dirs(args):
    fa, fb = _rel_files(args.a), _rel_files(args.b)
    added = sorted(set(fb) - set(fa))
    removed = sorted(set(fa) - set(fb))
    changed = []
    for rel in sorted(set(fa) & set(fb)):
        pa, pb = fa[rel], fb[rel]
        if os.path.getsize(pa) == os.path.getsize(pb) and sha256_file(pa) == sha256_file(pb):
            continue
        ranges, count, sa, sb, extra = diff_ranges(pa, pb, args.gap, 1000)
        changed.append({"path": rel, "size_a": sa, "size_b": sb, "differing_bytes": count,
                        "ranges": len(ranges) + extra})
    res = {"added": added, "removed": removed, "changed": changed}
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        if not (added or removed or changed):
            print("identical folders (%d files)" % len(fa))
        for r in added:
            print("  added    %s" % r)
        for r in removed:
            print("  removed  %s" % r)
        for c in changed:
            print("  changed  %s: %d bytes in %d range(s)%s" % (c["path"], c["differing_bytes"], c["ranges"],
                                                             "" if c["size_a"] == c["size_b"] else
                                                             ", size %d -> %d" % (c["size_a"], c["size_b"])))
    return 0 if not (added or removed or changed) else 1


# ---------------------------------------------------------------- strings

SJIS_RUN = re.compile(rb"(?:[\x20-\x7e]|[\x81-\x9f\xe0-\xef][\x40-\x7e\x80-\xfc]|[\xa1-\xdf])+")


def _patterns(enc, n):
    pats = []
    if enc in ("ascii", "all"):
        pats.append(("ascii", re.compile(rb"[\x20-\x7e]{%d,}" % n)))
    if enc in ("utf16le", "all"):
        pats.append(("utf16le", re.compile(rb"(?:[\x20-\x7e]\x00){%d,}" % n)))
    if enc in ("sjis", "all"):
        pats.append(("sjis", SJIS_RUN))
    return pats


_PERMISSIVE = {
    "ascii": frozenset(range(0x20, 0x7F)),
    "utf16le": frozenset(list(range(0x20, 0x7F)) + [0]),
    "sjis": frozenset(list(range(0x20, 0x7F)) + list(range(0x81, 0xFD))),
}


def _trailing_run(data, allowed, cap=1 << 20):
    i = len(data)
    lo = max(0, len(data) - cap)
    while i > lo and data[i - 1] in allowed:
        i -= 1
    return i


def iter_strings(path, n=4, enc="ascii"):
    """Yield (offset, encoding, text). Runs that cross a read chunk are kept whole."""
    pats = _patterns(enc, n)
    with open(path, "rb") as fh:
        for name, pat in pats:
            allowed = _PERMISSIVE[name]
            fh.seek(0)
            base = 0
            carry = b""
            while True:
                block = fh.read(CHUNK)
                data = carry + block
                if not data:
                    break
                cut = _trailing_run(data, allowed) if block else len(data)
                for m in pat.finditer(data):
                    if block and m.start() >= cut:
                        break
                    text = _decode(name, m.group(0), n)
                    if text is not None:
                        yield base + m.start(), name, text
                if not block:
                    break
                carry = data[cut:]
                base += cut


def _decode(kind, raw, n):
    if kind == "ascii":
        return raw.decode("ascii")
    if kind == "utf16le":
        return raw.decode("utf-16-le")
    # Shift-JIS (heuristic): keep runs that decode cleanly, are mostly double-byte, use only
    # kana / kanji / full-width forms, and are at least half kana or full-width (random byte
    # pairs decode mostly to kanji). Short or mostly-ASCII Japanese strings can be missed.
    try:
        text = raw.decode("cp932")
    except UnicodeDecodeError:
        return None
    wide = [c for c in text if ord(c) > 0x7F]
    if len(wide) < 2 or len(text) < n or len(wide) * 2 < len(text):
        return None
    for c in wide:
        o = ord(c)
        if not (0x3000 <= o <= 0x30FF or 0x4E00 <= o <= 0x9FFF or 0xFF01 <= o <= 0xFF5E):
            return None
    kana = sum(1 for c in wide if 0x3000 <= ord(c) <= 0x30FF or 0xFF01 <= ord(c) <= 0xFF5E)
    if kana * 2 < len(wide):
        return None
    return text


def cmd_strings(args):
    need_file(args.file)
    rows = []
    grep = args.grep.lower() if args.grep else None
    for off, kind, text in iter_strings(args.file, args.min, args.encoding):
        if grep and grep not in text.lower():
            continue
        rows.append({"offset": off, "encoding": kind, "text": text})
    rows.sort(key=lambda r: r["offset"])
    shown = rows[:args.max] if args.max else rows
    if args.json:
        print(json.dumps(shown, indent=2, ensure_ascii=False))
    else:
        for r in shown:
            tag = "" if args.encoding == "ascii" else "%-7s " % r["encoding"]
            print("0x%08x  %s%s" % (r["offset"], tag, r["text"]))
        if len(rows) > len(shown):
            print("... %d more (raise --max)" % (len(rows) - len(shown)))
    return 0


# ---------------------------------------------------------------- files (runtime log)

LOG_OK = re.compile(r'\[sceCdSearchFile:ok\] path="(.*?)" lsn=0x([0-9a-fA-F]+) size=0x([0-9a-fA-F]+)')
LOG_FAIL = re.compile(r"sceCdSearchFile failed: (.*?) \((?:normalized path is empty, )?root: ")


def cmd_files(args):
    need_file(args.log)
    seen = {}
    order = []
    with open(args.log, "r", errors="replace") as fh:
        for ln, line in enumerate(fh, 1):
            m = LOG_OK.search(line)
            if m:
                path, lsn, size = m.group(1), int(m.group(2), 16), int(m.group(3), 16)
                st = "found"
            else:
                m = LOG_FAIL.search(line)
                if not m:
                    continue
                path, lsn, size = m.group(1), None, None
                st = "missing"
            key = path.upper()
            if key not in seen:
                seen[key] = {"path": path, "first_line": ln, "lookups": 0, "status": st, "size": size, "lsn": lsn}
                order.append(key)
            r = seen[key]
            r["lookups"] += 1
            if st == "found":
                r["status"] = "found"
                r["size"], r["lsn"] = size, lsn
    rows = [seen[k] for k in order]
    missing = [r for r in rows if r["status"] == "missing"]
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        if not rows:
            print("no sceCdSearchFile lines in %s (runtime logs need PS2X_ENABLE_RUNTIME_LOGS, on by default)" % args.log)
        for r in rows:
            print("line %-7d %-8s %-10s x%-4d %s" % (r["first_line"], r["status"],
                                                    human(r["size"]) if r["size"] is not None else "-",
                                                    r["lookups"], r["path"]))
        if rows:
            print("%d file(s) looked up, %d missing. The runtime traces the first 128 lookups, then every 512th."
                  % (len(rows), len(missing)))
    return 1 if missing else 0


# ---------------------------------------------------------------- CLI

def _int(text):
    try:
        return int(text, 0)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("not a number: %r" % text)


def build_parser():
    p = argparse.ArgumentParser(
        prog="datascan.py",
        description="Map a PS2 game's data files: inventory, archive unpack/repack, byte diffs, strings.",
        epilog="Only for discs you own. Unpacked game files stay on your machine; never publish them.")
    sub = p.add_subparsers(dest="cmd", metavar="command")
    sub.required = True

    s = sub.add_parser("scan", help="inventory a folder or file: size, type, entropy, table-of-contents guess")
    s.add_argument("target")
    s.add_argument("--embedded", action="store_true", help="also count known signatures inside each file")
    s.add_argument("--sort", choices=("path", "size"), default="path")
    s.add_argument("--min-size", type=_int, default=0)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("unpack", help="split an AFS or detected table-of-contents archive into a folder + index.json")
    s.add_argument("file")
    s.add_argument("--out", required=True, help="folder to write (must be empty)")
    s.add_argument("--format", choices=("auto", "afs", "toc"), default="auto")
    s.add_argument("--layout", choices=("count+pairs", "count+offsets", "pairs", "offsets"),
                   help="only consider this table layout")
    s.add_argument("--header", type=_int, default=None, help="table starts after this many header bytes (0, 4, 8, 12, 16)")
    s.add_argument("--force-toc", action="store_true", help="use the best guess even below the confidence threshold")
    s.add_argument("--force", action="store_true", help="write into a non-empty folder")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_unpack)

    s = sub.add_parser("repack", help="rebuild an archive from an unpacked folder (same order, original alignment)")
    s.add_argument("folder")
    s.add_argument("--out", required=True, help="new archive path (never your only copy of the original)")
    s.add_argument("--layout", choices=("auto", "keep", "pack"), default="auto",
                   help="keep: every entry stays at its offset (refuse if one outgrew its slot); pack: recompute"
                        " offsets; auto (default): keep when everything fits, else pack")
    s.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_repack)

    s = sub.add_parser("diff", help="changed byte ranges between two files (or two folders)")
    s.add_argument("a")
    s.add_argument("b")
    s.add_argument("--gap", type=_int, default=8, help="merge ranges closer than this many bytes (default 8)")
    s.add_argument("--max", type=_int, default=200, help="ranges to list (default 200)")
    s.add_argument("--as", dest="as_type", choices=("u8", "u16", "u32", "f32"), help="also show the values")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("strings", help="printable strings with offsets")
    s.add_argument("file")
    s.add_argument("-n", "--min", type=_int, default=4, help="minimum length (default 4)")
    s.add_argument("--encoding", choices=("ascii", "sjis", "utf16le", "all"), default="ascii")
    s.add_argument("--grep", help="only strings containing this text (case-insensitive)")
    s.add_argument("--max", type=_int, default=0, help="stop after this many")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_strings)

    s = sub.add_parser("files", help="disc files the port looked up, from a PS2Recomp runtime log")
    s.add_argument("log")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_files)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ToolError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    except Refused as e:
        print("refused: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
