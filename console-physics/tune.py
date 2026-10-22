#!/usr/bin/env python3
"""tune.py: find and change the numbers that decide how a PS2 game moves.

  scan   find a float32 value in the ELF: (a) stored in data (aligned words, with the
         instructions that read it), (b) built in code by lui (+ ori/addiu) then mtc1
  fps    list frame-timing candidates (1/60, 1/30, 60.0, 30.0, PAL 50/25 ...)
  patch  print the pnach line(s) to test a new value in PCSX2, and the PS2Recomp fix:
         TOML instruction patches for a lui/ori pair, or the data write for /console-mod

The EE's FPU is single precision, so game physics are float32. Python 3.8+, stdlib only.
"""

import argparse
import json
import math
import os
import struct
import sys

TOOL = "tune.py"
WINDOW = 8          # instructions after a lui that may finish building the value


class UsageError(Exception):
    """Bad input the user can fix: printed as one line, exit code 2."""


def fail(msg):
    raise UsageError(msg)


def hex32(v):
    return "0x%08X" % (v & 0xFFFFFFFF)


def f2u(f):
    return struct.unpack("<I", struct.pack("<f", f))[0]


def u2f(u):
    return struct.unpack("<f", struct.pack("<I", u & 0xFFFFFFFF))[0]


def fmt(f):
    if f != f or f in (float("inf"), float("-inf")):
        return str(f)
    return "%.9g" % f


def sext16(v):
    return v - 0x10000 if v & 0x8000 else v


# --------------------------------------------------------------------------
# PS2 address space and ELF32 little-endian MIPS reader (self-contained)
# --------------------------------------------------------------------------

RAM_SIZE = 32 * 1024 * 1024


def ram_offset(addr):
    addr &= 0xFFFFFFFF
    if addr < RAM_SIZE:
        return addr
    if 0x20000000 <= addr < 0x40000000:
        return addr & (RAM_SIZE - 1)
    if 0x80000000 <= addr < 0xC0000000:
        p = addr & 0x1FFFFFFF
        return p if p < RAM_SIZE else None
    return None


class Elf(object):
    def __init__(self, path):
        if not os.path.isfile(path):
            fail("ELF not found: %s" % path)
        with open(path, "rb") as f:
            data = f.read()
        self.data = data
        self.name = os.path.basename(path)
        if len(data) < 52 or data[:4] != b"\x7fELF":
            fail("%s is not an ELF file" % self.name)
        if data[4] != 1 or data[5] != 1:
            fail("%s is not a 32-bit little-endian ELF (PS2 EE ELFs are)" % self.name)
        (_t, machine, _v, self.entry, phoff, shoff, _f, _eh, phentsize, phnum, shentsize, shnum,
         shstrndx) = struct.unpack_from("<HHIIIIIHHHHHH", data, 16)
        if machine != 8:
            fail("%s is not a MIPS ELF (machine %d, the PS2 EE is 8)" % (self.name, machine))
        if phnum and (phentsize < 32 or phoff + phnum * phentsize > len(data)):
            fail("%s is truncated or has a bad program header table" % self.name)
        self.segments = []
        for i in range(phnum):
            p_type, off, vaddr, _pa, filesz, memsz, flags, _al = struct.unpack_from("<IIIIIIII", data, phoff + i * phentsize)
            if p_type != 1 or memsz == 0:
                continue
            if filesz > memsz or off + filesz > len(data):
                fail("%s is truncated: segment %d runs past the end of the file" % (self.name, i))
            self.segments.append({"index": i, "offset": off, "vaddr": vaddr, "filesz": filesz, "memsz": memsz,
                                  "flags": flags, "ram": ram_offset(vaddr)})
        if not self.segments:
            fail("%s has no loadable (PT_LOAD) segments" % self.name)
        self.sections = []
        if shnum and shoff and shentsize >= 40 and shoff + shnum * shentsize <= len(data):
            raw = [struct.unpack_from("<IIIIIIIIII", data, shoff + i * shentsize) for i in range(shnum)]
            strtab = b""
            if shstrndx < len(raw) and raw[shstrndx][4] + raw[shstrndx][5] <= len(data):
                strtab = data[raw[shstrndx][4]:raw[shstrndx][4] + raw[shstrndx][5]]
            for (nm, typ, flg, addr, off, size, _l, _i, _a, _e) in raw:
                if not (flg & 2) or size == 0:
                    continue
                end = strtab.find(b"\0", nm) if nm < len(strtab) else -1
                name = strtab[nm:end].decode("ascii", "replace") if end >= 0 else ""
                self.sections.append({"name": name, "type": typ, "flags": flg, "addr": addr, "size": size,
                                      "offset": off, "ram": ram_offset(addr)})
        self._analysis = None

    def locate(self, ram):
        for s in self.segments:
            if s["ram"] is None:
                continue
            if s["ram"] <= ram < s["ram"] + s["filesz"]:
                return s, True
            if s["ram"] + s["filesz"] <= ram < s["ram"] + s["memsz"]:
                return s, False
        return None, False

    def section_at(self, ram):
        for sec in self.sections:
            if sec["ram"] is not None and sec["ram"] <= ram < sec["ram"] + sec["size"]:
                return sec
        return None

    def word(self, vaddr):
        ram = ram_offset(vaddr)
        if ram is None:
            return None
        seg, backed = self.locate(ram)
        if seg is None or not backed or ram + 4 > seg["ram"] + seg["filesz"]:
            return None
        return struct.unpack_from("<I", self.data, seg["offset"] + ram - seg["ram"])[0]

    def where(self, vaddr):
        ram = ram_offset(vaddr)
        sec = self.section_at(ram) if ram is not None else None
        if sec is not None:
            return sec["name"] or "section"
        seg, backed = self.locate(ram) if ram is not None else (None, False)
        if seg is None:
            return "outside the ELF"
        perm = "".join(c for c, bit in (("R", 4), ("W", 2), ("X", 1)) if seg["flags"] & bit)
        return "segment %d (%s)%s" % (seg["index"], perm, "" if backed else " zero-filled")

    def regions(self):
        """(code, data): lists of (vaddr, file offset, size) to scan."""
        code, data = [], []
        if self.sections:
            for sec in self.sections:
                if sec["type"] == 8 or sec["ram"] is None:
                    continue
                seg, backed = self.locate(sec["ram"])
                if seg is None or not backed:
                    continue
                size = min(sec["size"], seg["ram"] + seg["filesz"] - sec["ram"])
                off = seg["offset"] + sec["ram"] - seg["ram"]
                (code if sec["flags"] & 4 else data).append((sec["addr"], off, size))
        if not code and not data:
            for s in self.segments:
                if not s["filesz"]:
                    continue
                if s["flags"] & 1:
                    code.append((s["vaddr"], s["offset"], s["filesz"]))
                if not (s["flags"] & 1) or (s["flags"] & 2):
                    data.append((s["vaddr"], s["offset"], s["filesz"]))   # RWX: code and data mixed
        return code, data

    def is_code(self, vaddr):
        code, _ = self.regions()
        return any(a <= vaddr < a + n for a, _, n in code)


# --------------------------------------------------------------------------
# Minimal MIPS decoding: only what float constants need
# --------------------------------------------------------------------------

REG = ["zero", "at", "v0", "v1", "a0", "a1", "a2", "a3", "t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7",
       "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "t8", "t9", "k0", "k1", "gp", "sp", "fp", "ra"]
OP_LUI, OP_ORI, OP_ADDIU, OP_COP1, OP_LWC1, OP_LW = 0x0F, 0x0D, 0x09, 0x11, 0x31, 0x23
WRITES_RT = {0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x18, 0x19, 0x1A, 0x1B, 0x1E,
             0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x37}
NAMES = {0x02: "j", 0x03: "jal", 0x04: "beq", 0x05: "bne", 0x06: "blez", 0x07: "bgtz", 0x14: "beql",
         0x15: "bnel", 0x23: "lw", 0x2B: "sw", 0x31: "lwc1", 0x39: "swc1", 0x0C: "andi", 0x0A: "slti",
         0x1C: "mmi", 0x1E: "lq", 0x1F: "sq", 0x37: "ld", 0x3F: "sd", 0x24: "lbu", 0x28: "sb", 0x21: "lh",
         0x25: "lhu", 0x29: "sh", 0x20: "lb", 0x10: "cop0", 0x12: "cop2", 0x01: "regimm"}


def describe(w, vaddr=None):
    op, rs, rt, rd, imm = w >> 26, (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31, w & 0xFFFF
    if w == 0:
        return "nop"
    if op == OP_LUI:
        return "lui $%s, 0x%04X" % (REG[rt], imm)
    if op in (OP_ORI, OP_ADDIU):
        return "%s $%s, $%s, 0x%04X" % ("ori" if op == OP_ORI else "addiu", REG[rt], REG[rs], imm)
    if op == OP_COP1 and rs == 4:
        return "mtc1 $%s, $f%d" % (REG[rt], rd)
    if op == OP_COP1 and rs == 0:
        return "mfc1 $%s, $f%d" % (REG[rt], rd)
    if op in (OP_LWC1, 0x39):
        return "%s $f%d, %d($%s)" % (NAMES[op], rt, sext16(imm), REG[rs])
    if op in (OP_LW, 0x2B):
        return "%s $%s, %d($%s)" % (NAMES[op], REG[rt], sext16(imm), REG[rs])
    if op in (0x02, 0x03) and vaddr is not None:
        return "%s 0x%08X" % (NAMES[op], ((vaddr + 4) & 0xF0000000) | ((w & 0x03FFFFFF) << 2))
    if op == 0:
        fn = w & 0x3F
        if fn == 0x08:
            return "jr $%s" % REG[rs]
        return {0x09: "jalr", 0x0C: "syscall", 0x21: "addu", 0x25: "or", 0x2D: "daddu", 0x00: "sll"}.get(fn, "special")
    if op == OP_COP1:
        return "cop1"
    return NAMES.get(op, "op%02X" % op)


def dest_reg(w):
    """The GPR an instruction writes (approximate: enough to stop tracking), or None."""
    op = w >> 26
    if op == 0:
        fn = w & 0x3F
        if fn in (0x08, 0x0C, 0x0D, 0x0F, 0x11, 0x13, 0x18, 0x19, 0x1A, 0x1B):
            return None
        return (w >> 11) & 31
    if op in WRITES_RT:
        return (w >> 16) & 31
    if op == 0x03:
        return 31
    if op == OP_COP1 and ((w >> 21) & 31) in (0, 2):
        return (w >> 16) & 31
    if op == 0x1C:
        return (w >> 11) & 31
    return None


def is_transfer(w):
    op = w >> 26
    if op in (0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x14, 0x15, 0x16, 0x17, 0x01):
        return True
    return op == 0 and (w & 0x3F) in (0x08, 0x09)


def analyze(elf):
    """One pass over the code: float constants built in registers, data loads, and $gp."""
    if elf._analysis is not None:
        return elf._analysis
    code, _ = elf.regions()
    gp = None
    consts = []
    xrefs = {}
    seen_mtc1 = set()
    for base, off, size in code:
        n = size // 4
        ws = struct.unpack_from("<%dI" % n, elf.data, off) if n else ()
        # $gp: crt0 sets it near the entry with lui $gp + addiu/ori $gp
        if gp is None:
            start = max(0, (elf.entry - base) // 4) if base <= elf.entry < base + size else 0
            for i in list(range(start, min(n, start + 256))) + list(range(0, n)):
                w = ws[i]
                if w >> 26 == OP_LUI and (w >> 16) & 31 == 28:
                    for j in range(i + 1, min(n, i + 5)):
                        v = ws[j]
                        if v >> 26 in (OP_ADDIU, OP_ORI) and (v >> 21) & 31 == 28 and (v >> 16) & 31 == 28:
                            hi = (w & 0xFFFF) << 16
                            gp = (hi | (v & 0xFFFF)) if v >> 26 == OP_ORI else (hi + sext16(v & 0xFFFF)) & 0xFFFFFFFF
                            break
                    if gp is not None:
                        break
        for i in range(n):
            w = ws[i]
            if w >> 26 != OP_LUI:
                continue
            rt = (w >> 16) & 31
            if rt in (0, 28):       # $zero, and $gp (its loads are found by the $gp pass below)
                continue
            known = {rt: {"bits": (w & 0xFFFF) << 16, "lo": None, "lo_op": None}}
            stop_at = None
            for j in range(i + 1, min(n, i + 1 + WINDOW)):
                v = ws[j]
                op, rs, rt2 = v >> 26, (v >> 21) & 31, (v >> 16) & 31
                if op in (OP_ORI, OP_ADDIU) and rs in known and known[rs]["lo"] is None and rt2 != 0:
                    k = known[rs]
                    bits = (k["bits"] | (v & 0xFFFF)) if op == OP_ORI else (k["bits"] + sext16(v & 0xFFFF)) & 0xFFFFFFFF
                    known[rt2] = {"bits": bits, "lo": base + 4 * j, "lo_op": "ori" if op == OP_ORI else "addiu"}
                elif op == OP_COP1 and rs == 4 and rt2 in known:
                    k = known[rt2]
                    at = base + 4 * j
                    if at not in seen_mtc1:
                        seen_mtc1.add(at)
                        consts.append({"lui": base + 4 * i, "lo": k["lo"], "lo_op": k["lo_op"], "mtc1": at,
                                       "freg": (v >> 11) & 31, "reg": rt2, "bits": k["bits"]})
                elif op in (OP_LWC1, OP_LW) and rs in known:
                    addr = (known[rs]["bits"] + sext16(v & 0xFFFF)) & 0xFFFFFFFF
                    xrefs.setdefault(addr, []).append({"at": base + 4 * j, "insn": describe(v),
                                                       "via": "lui" if known[rs]["lo"] is None else "lui+" + known[rs]["lo_op"]})
                    if op == OP_LW:
                        known.pop(rt2, None)
                else:
                    d = dest_reg(v)
                    if d is not None:
                        known.pop(d, None)
                if not known:
                    break
                if stop_at is not None and j >= stop_at:
                    break
                if is_transfer(v):
                    stop_at = j + 1          # follow the delay slot, then stop
        if gp is not None:
            for i in range(n):
                w = ws[i]
                if w >> 26 in (OP_LWC1, OP_LW) and (w >> 21) & 31 == 28:
                    addr = (gp + sext16(w & 0xFFFF)) & 0xFFFFFFFF
                    lst = xrefs.setdefault(addr, [])
                    if not any(x["at"] == base + 4 * i for x in lst):
                        lst.append({"at": base + 4 * i, "insn": describe(w), "via": "$gp"})
    elf._analysis = {"gp": gp, "consts": consts, "xrefs": xrefs}
    return elf._analysis


# --------------------------------------------------------------------------
# Searching for values
# --------------------------------------------------------------------------


def _bits_ceil(x):
    b = f2u(x)
    if u2f(b) < x:
        b += 1
    return b


def _bits_floor(x):
    b = f2u(x)
    if u2f(b) > x:
        b -= 1
    return b


def pattern_ranges(value, tol):
    """Inclusive float32 bit-pattern intervals whose values lie in [value - tol, value + tol]."""
    big = 3.4028234663852886e38
    lo, hi = max(value - tol, -big), min(value + tol, big)
    out = []
    if hi >= 0:
        out.append((_bits_ceil(max(lo, 0.0)), min(_bits_floor(hi), 0x7F7FFFFF)))
    if lo < 0:
        out.append((0x80000000 | _bits_ceil(max(-hi, 0.0)), 0x80000000 | min(_bits_floor(-lo), 0x7F7FFFFF)))
    return [(a, b) for a, b in out if a <= b]


def default_tol(value):
    return max(abs(value) * 1e-3, 1e-6)


def check_value(value):
    if value != value or value in (float("inf"), float("-inf")):
        fail("the value must be a finite number")
    if value == 0:
        fail("0.0 is in every zero word: search for a non-zero value (the one the game shows, or 1.0)")
    if abs(value) > 3.4e38:
        fail("%s does not fit in a float32" % value)


def search(elf, targets, limit):
    """targets: list of (label, value, tol). Returns hits sorted by address."""
    code, data = elf.regions()
    ana = analyze(elf)
    ranges = []
    for label, value, tol in targets:
        for a, b in pattern_ranges(value, tol):
            ranges.append((a, b, label, value))
    hits = []
    if not ranges:
        return hits
    lo_all = min(r[0] for r in ranges)
    hi_all = max(r[1] for r in ranges)
    for base, off, size in data:
        n = size // 4
        start_pad = (-base) % 4
        n = (size - start_pad) // 4
        if n <= 0:
            continue
        ws = struct.unpack_from("<%dI" % n, elf.data, off + start_pad)
        for i, w in enumerate(ws):
            if w < lo_all or w > hi_all:
                continue
            for a, b, label, value in ranges:
                if a <= w <= b:
                    addr = base + start_pad + 4 * i
                    hits.append({"kind": "data", "addr": addr, "bits": w, "value": u2f(w), "target": label,
                                 "where": elf.where(addr), "read_by": ana["xrefs"].get(addr, [])})
                    break
    for c in ana["consts"]:
        w = c["bits"]
        for a, b, label, value in ranges:
            if a <= w <= b:
                h = {"kind": "code", "addr": c["lui"], "bits": w, "value": u2f(w), "target": label,
                     "where": elf.where(c["lui"]), "lui": c["lui"], "lo": c["lo"], "lo_op": c["lo_op"],
                     "mtc1": c["mtc1"], "freg": c["freg"], "reg": c["reg"]}
                hits.append(h)
                break
    hits.sort(key=lambda h: (h["addr"], h["kind"]))
    return hits[:limit] if limit else hits


def context(elf, h, before=2, after=2):
    first = h["lui"] - 4 * before
    last = max(h["mtc1"], h["lo"] or 0) + 4 * after
    out = []
    for a in range(first, last + 4, 4):
        w = elf.word(a)
        if w is None:
            continue
        mark = ">" if a in (h["lui"], h["lo"], h["mtc1"]) else " "
        out.append("%s %s  %08X  %s" % (mark, hex32(a), w, describe(w, a)))
    return out


def print_hits(elf, hits, title, show_target=False):
    nd = sum(1 for h in hits if h["kind"] == "data")
    nc = len(hits) - nd
    print("%s: %d in data, %d built in code" % (title, nd, nc))
    for h in hits:
        tgt = ("  [%s]" % h["target"]) if show_target else ""
        if h["kind"] == "data":
            print("\nDATA  %s  %-22s %s (%s)%s" % (hex32(h["addr"]), h["where"], fmt(h["value"]), hex32(h["bits"]), tgt))
            if h["read_by"]:
                rb = ", ".join("%s at %s (%s)" % (x["insn"], hex32(x["at"]), x["via"]) for x in h["read_by"][:6])
                more = " and %d more" % (len(h["read_by"]) - 6) if len(h["read_by"]) > 6 else ""
                print("      read by: %s%s" % (rb, more))
            else:
                print("      read by: nothing found by lui or $gp addressing (check xrefs in Ghidra)")
        else:
            how = "lui" if h["lo"] is None else "lui + " + h["lo_op"]
            print("\nCODE  %s  %-22s %s (%s) built by %s, then mtc1 into $f%d%s" % (
                hex32(h["lui"]), h["where"], fmt(h["value"]), hex32(h["bits"]), how, h["freg"], tgt))
            for line in context(elf, h):
                print("      " + line)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def hit_json(h):
    d = dict(h)
    for k in ("addr", "bits", "lui", "lo", "mtc1"):
        if d.get(k) is not None:
            d[k] = hex32(d[k])
    d["value"] = fmt(h["value"])
    if "read_by" in d:
        d["read_by"] = [dict(x, at=hex32(x["at"])) for x in d["read_by"]]
    return d


def cmd_scan(a):
    check_value(a.value)
    tol = a.tol if a.tol is not None else default_tol(a.value)
    if tol < 0:
        fail("--tol must be zero or more")
    elf = Elf(a.elf)
    hits = search(elf, [(fmt(a.value), a.value, tol)], a.limit)
    if a.json:
        print(json.dumps({"elf": elf.name, "value": a.value, "tol": tol, "gp": hex32(analyze(elf)["gp"]) if analyze(elf)["gp"] is not None else None,
                          "hits": [hit_json(h) for h in hits]}, indent=2))
        return 0 if hits else 1
    print_hits(elf, hits, "%s (+/- %s) in %s" % (fmt(a.value), fmt(tol), elf.name))
    if not hits:
        print("\nNothing. Try a wider --tol, the value in other units (9.8 m/s2 may be 980 cm/s2, 0.98 per"
              " frame, or 9.8/60), or find it while playing with PCSX2's memory search.")
        return 1
    if len(hits) >= a.limit:
        print("\n(first %d shown: --limit to see more)" % a.limit)
    print("\nNext: try one in PCSX2:  tune.py patch %s --addr <address> --float <new value>" % elf.name)
    return 0


FPS_TARGETS = [
    ("1/60 s, one NTSC frame", 1.0 / 60.0), ("1/30 s, a 30 fps frame", 1.0 / 30.0),
    ("60 per second", 60.0), ("30 per second", 30.0), ("1/50 s, one PAL frame", 1.0 / 50.0),
    ("1/25 s, a 25 fps PAL frame", 1.0 / 25.0), ("50 per second (PAL)", 50.0), ("25 per second (PAL)", 25.0),
]

FPS_CAVEAT = [
    "These are candidates, not answers. Many games pace frames with a vsync count (wait 2 vsyncs = 30 fps),",
    "an integer the scan cannot see: look in Ghidra for the loop that waits on vsync (sceGsSyncV, a vsync",
    "semaphore) and the number it waits for.",
    "A 60 fps unlock also needs the game's logic to scale by the frame time: if movement, physics or",
    "animation are counted per frame, doubling the frame rate doubles the game speed. Change one",
    "candidate at a time and test in PCSX2 first (tune.py patch), then bake it in with /console-mod.",
]


def cmd_fps(a):
    elf = Elf(a.elf)
    targets = [(label, v, abs(v) * 0.005) for label, v in FPS_TARGETS]
    hits = search(elf, targets, a.limit)
    if a.json:
        print(json.dumps({"elf": elf.name, "hits": [hit_json(h) for h in hits], "caveat": " ".join(FPS_CAVEAT)}, indent=2))
        return 0 if hits else 1
    print_hits(elf, hits, "Frame-timing candidates in %s" % elf.name, show_target=True)
    print("")
    for line in FPS_CAVEAT:
        print(line)
    return 0 if hits else 1


def find_pair(elf, addr):
    """For a code address: the lui and its low-half instruction. Returns dict or None."""
    w = elf.word(addr)
    if w is None:
        return None
    for c in analyze(elf)["consts"]:
        if addr in (c["lui"], c["lo"], c["mtc1"]):
            return {"lui": c["lui"], "lo": c["lo"], "lo_op": c["lo_op"], "mtc1": c["mtc1"], "freg": c["freg"]}
    op = w >> 26
    if op == OP_LUI:
        rt = (w >> 16) & 31
        for j in range(1, WINDOW + 1):
            v = elf.word(addr + 4 * j)
            if v is None:
                break
            if v >> 26 in (OP_ORI, OP_ADDIU) and (v >> 21) & 31 == rt:
                return {"lui": addr, "lo": addr + 4 * j, "lo_op": "ori" if v >> 26 == OP_ORI else "addiu",
                        "mtc1": None, "freg": None}
            if dest_reg(v) == rt:
                break
        return {"lui": addr, "lo": None, "lo_op": None, "mtc1": None, "freg": None}
    if op in (OP_ORI, OP_ADDIU):
        rs = (w >> 21) & 31
        for j in range(1, WINDOW + 1):
            v = elf.word(addr - 4 * j)
            if v is None:
                break
            if v >> 26 == OP_LUI and (v >> 16) & 31 == rs:
                return {"lui": addr - 4 * j, "lo": addr, "lo_op": "ori" if op == OP_ORI else "addiu",
                        "mtc1": None, "freg": None}
    return None


def split_hi_lo(bits, lo_op):
    """New (hi, lo) immediates so the pair builds exactly `bits`."""
    if lo_op == "addiu":
        hi = ((bits + 0x8000) >> 16) & 0xFFFF
        lo = bits & 0xFFFF
        assert ((hi << 16) + sext16(lo)) & 0xFFFFFFFF == bits
        return hi, lo
    return bits >> 16, bits & 0xFFFF


def pnach_line(place, addr, value):
    return "patch=%d,EE,%08X,word,%08X" % (place, addr, value)


def cmd_patch(a):
    elf = Elf(a.elf)
    try:
        addr = int(a.addr, 16)
    except ValueError:
        fail("--addr is not a hex address: %r" % a.addr)
    if addr & 3:
        fail("--addr %s is not 4-byte aligned" % hex32(addr))
    new = a.float
    if new != new or abs(new) > 3.4e38:
        fail("--float must be a finite float32 value")
    bits = f2u(new)
    ram = ram_offset(addr)
    if ram is None:
        fail("%s is not main RAM" % hex32(addr))
    seg, backed = elf.locate(ram)
    out = {"elf": elf.name, "addr": hex32(addr), "new_value": fmt(u2f(bits)), "new_bits": hex32(bits)}
    status = 0
    if seg is None or not backed:
        place = 1 if a.place is None else a.place
        out.update(kind="runtime", pnach=[pnach_line(place, addr, bits)], toml=[],
                   note="%s is %s: the game sets this value while it runs, so nothing in the ELF to change. "
                        "Write it every frame: put the pnach line in a .pnach and run /console-mod "
                        "pnach.py override --frame-hook." % (hex32(addr), "zero-filled (BSS)" if seg else "outside the ELF"))
    elif elf.is_code(addr):
        pair = find_pair(elf, addr)
        if pair is None:
            fail("%s holds '%s', not part of a float constant: give a data float or the lui/ori/addiu "
                 "of a constant (tune.py scan lists them)" % (hex32(addr), describe(elf.word(addr), addr)))
        lui_w = elf.word(pair["lui"])
        place = 1 if a.place is None else a.place
        old_bits = (lui_w & 0xFFFF) << 16
        words_ = []
        if pair["lo"] is not None:
            lo_w = elf.word(pair["lo"])
            old_bits = (old_bits | (lo_w & 0xFFFF)) if pair["lo_op"] == "ori" else (old_bits + sext16(lo_w & 0xFFFF)) & 0xFFFFFFFF
            hi, lo = split_hi_lo(bits, pair["lo_op"])
            words_ = [(pair["lui"], lui_w, (lui_w & 0xFFFF0000) | hi), (pair["lo"], lo_w, (lo_w & 0xFFFF0000) | lo)]
            out["kind"] = "code lui+" + pair["lo_op"]
        else:
            out["kind"] = "code lui"
            if bits & 0xFFFF:
                near = (bits + 0x8000) & 0xFFFF0000
                if a.exact:
                    fail("%s needs a low half (%s) but this constant is a lone lui with no room for one; "
                         "the nearest lui-only value is %s" % (fmt(new), hex32(bits), fmt(u2f(near))))
                out["note"] = ("%s needs a low half but this constant is a lone lui: using the nearest value it can "
                               "hold, %s (off by %s). For the exact value, write the C++ by hand (/console-mod)."
                               % (fmt(new), fmt(u2f(near)), fmt(abs(u2f(near) - new))))
                bits = near
                out["new_value"], out["new_bits"] = fmt(u2f(bits)), hex32(bits)
                status = 1
            words_ = [(pair["lui"], lui_w, (lui_w & 0xFFFF0000) | (bits >> 16))]
        out["old_value"] = fmt(u2f(old_bits))
        out["words"] = [{"addr": hex32(x), "original": hex32(o), "new": hex32(nw), "original_asm": describe(o),
                         "new_asm": describe(nw)} for x, o, nw in words_]
        changed = [(x, o, nw) for x, o, nw in words_ if o != nw]
        if not changed:
            out["note"] = "the constant already holds this value: nothing to patch"
        out["pnach"] = [pnach_line(place, x, nw) for x, o, nw in changed]
        out["toml"] = ['[[patches.instructions]]\naddress = "%s"\nvalue = "%s"' % (hex32(x), hex32(nw)) for x, o, nw in changed]
        uses = "mtc1 $f%d at %s" % (pair["freg"], hex32(pair["mtc1"])) if pair.get("mtc1") else "no mtc1 seen nearby: check it really is a float"
        out["used_by"] = uses
    else:
        place = 0 if a.place is None else a.place
        old = elf.word(addr)
        reads = analyze(elf)["xrefs"].get(addr, [])
        out.update(kind="data", old_value=fmt(u2f(old)), old_bits=hex32(old), pnach=[pnach_line(place, addr, bits)],
                   toml=[], where=elf.where(addr),
                   read_by=[dict(x, at=hex32(x["at"])) for x in reads],
                   override="Ps2FastWrite32(rdram, 0x%08Xu, 0x%08Xu); // %sf" % (ram, bits, fmt(u2f(bits))))
        if len(reads) > 1:
            out["note"] = ("%d instructions read this constant: changing it changes every one of them. To change "
                           "only one, patch that instruction's code instead (Ghidra)." % len(reads))
    if a.json:
        print(json.dumps(out, indent=2))
        return status
    print("%s  %s in %s" % (out["addr"], out["kind"], elf.name))
    if "old_value" in out:
        print("  value       %s -> %s (%s)" % (out["old_value"], out["new_value"], out["new_bits"]))
    for w in out.get("words", []):
        print("  %s  %s  %-24s -> %s  %s" % (w["addr"], w["original"], w["original_asm"], w["new"], w["new_asm"]))
    if out.get("used_by"):
        print("  used by     " + out["used_by"])
    if out.get("read_by"):
        print("  read by     " + ", ".join("%s at %s" % (x["insn"], x["at"]) for x in out["read_by"][:8]))
    if out.get("note"):
        print("  NOTE: " + out["note"])
    print("\nTest in PCSX2 first (add to %s, see /console-mod pnach.py crc):" % "the game's .pnach")
    for line in out["pnach"]:
        print("  " + line)
    if out["toml"]:
        print("\nThen the port: paste into the game's ps2_recomp TOML, re-run ps2_recomp, rebuild:")
        for block in out["toml"]:
            print("  " + block.replace("\n", "\n  "))
    elif out["kind"] == "data":
        print("\nThen the port: it is data, so a game override writes it at load. Put the pnach line in a .pnach and")
        print("run /console-mod: pnach.py override <file.pnach> --elf %s, or by hand in an override:" % elf.name)
        print("  " + out["override"])
    return status


def build_parser():
    p = argparse.ArgumentParser(
        prog="tune.py",
        description="Find the float32 numbers that decide how a PS2 game moves (speed, gravity, grip, jump "
                    "height, frame rate) and print the patches to change them.",
        epilog="Exit codes: 0 ok, 1 nothing found or an approximation was used, 2 bad input.")
    sub = p.add_subparsers(dest="cmd")
    sp = sub.add_parser("scan", help="find a float value in data and in lui/ori/mtc1 code")
    sp.add_argument("elf", help="the game's main ELF")
    sp.add_argument("--value", type=float, required=True, help="the number to look for, e.g. 9.8")
    sp.add_argument("--tol", type=float, help="absolute tolerance (default 0.1%% of the value)")
    sp.add_argument("--limit", type=int, default=50, help="most hits to show (default 50)")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_scan)
    sp = sub.add_parser("fps", help="list frame-timing candidates (1/60, 1/30, 60, 30, PAL 50/25)")
    sp.add_argument("elf", help="the game's main ELF")
    sp.add_argument("--limit", type=int, default=100, help="most hits to show (default 100)")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_fps)
    sp = sub.add_parser("patch", help="print pnach + TOML/override lines that set a new float value")
    sp.add_argument("elf", help="the game's main ELF")
    sp.add_argument("--addr", required=True, help="a data float, or the lui / ori / addiu of a constant (hex)")
    sp.add_argument("--float", type=float, required=True, help="the new value, e.g. 12.5")
    sp.add_argument("--place", type=int, choices=(0, 1, 2), help="pnach place (default 0 for data, 1 for code)")
    sp.add_argument("--exact", action="store_true", help="refuse instead of rounding when a lone lui cannot hold the value")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_patch)
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    if not getattr(a, "func", None):
        p.print_help()
        return 2
    try:
        return a.func(a)
    except UsageError as e:
        print("%s: error: %s" % (TOOL, e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
