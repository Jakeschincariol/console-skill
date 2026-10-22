#!/usr/bin/env python3
"""memcard.py: PS2 memory card saves for /console-saves.

  info     a card image's superblock: size, ECC, format version, free space
  ls       the saves on a card image, a PCSX2 folder card or the port's mc0 folder
  export   copy saves from a card into the port's folder memory card (mc0/<SAVE>/...)
  psu      export / import / ls single saves as .psu files (uLaunchELF, PCSX2, mymc)
  backup   timestamped zip of mc0 (keeps the newest N)
  restore  put a backup zip back (backs up what is there first)
  sync     two-way sync of mc0 with a cloud folder; never silently overwrites

Card images are READ only (writing them needs ECC and is out of scope).
The PS2Recomp runtime keeps memory card slot 1 as plain folders in mc0 next to
the game's ELF (slot 2 is the sibling mc1): see Kernel/Stubs/MemoryCard.cpp,
getMcRootPath() and guestMcPathToHostPath(). A save the game writes to
mc0:/BASLUS-20312GAME/DATA lands in <elf dir>/mc0/BASLUS-20312GAME/DATA.

Python 3.8+ standard library only. Exit codes: 0 ok, 1 needs attention,
2 bad input.
"""

import argparse
import calendar
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import sys
import time
import unicodedata
import zipfile

MAGIC = b"Sony PS2 Memory Card Format "
DIRENT = struct.Struct("<HHL8sLL8sL28x448s")
TOD = struct.Struct("<xBBBBBH")
DIRENT_SIZE = 512
PSU_CLUSTER = 1024
JST = 9 * 3600  # the PS2 clock and card timestamps are UTC+9

DF_READ = 0x0001
DF_WRITE = 0x0002
DF_EXECUTE = 0x0004
DF_PROTECTED = 0x0008
DF_FILE = 0x0010
DF_DIR = 0x0020
DF_CLOSED = 0x0080
DF_0400 = 0x0400
DF_POCKETSTN = 0x0800
DF_PSX = 0x1000
DF_HIDDEN = 0x2000
DF_EXISTS = 0x8000
MODE_DIR = DF_EXISTS | DF_0400 | DF_DIR | DF_READ | DF_WRITE | DF_EXECUTE                 # 0x8427
MODE_FILE = DF_EXISTS | DF_0400 | DF_CLOSED | DF_FILE | DF_READ | DF_WRITE | DF_EXECUTE   # 0x8497

FAT_ALLOCATED = 0x80000000
FAT_NEXT_MASK = 0x7FFFFFFF  # low 31 bits all set = end of chain

SKIP_NAMES = (".DS_Store", "Thumbs.db", "desktop.ini")
REGIONS = {"A": "USA", "E": "Europe", "I": "Japan/Asia"}
SERIAL_RE = re.compile(r"^B([AEI])(S[CL][A-Z]{2})[-_]?(\d{3})\.?(\d{2})")


class ToolError(Exception):
    """Bad input: exit 2."""


class Attention(Exception):
    """A check failed: exit 1."""


# ---------------------------------------------------------------- time

def tod_unpack(raw):
    sec, minute, hour, day, month, year = TOD.unpack(raw)
    return (year, month, day, hour, minute, sec)


def tod_to_epoch(t):
    year, month, day, hour, minute, sec = t
    try:
        return calendar.timegm((year, month or 1, day or 1, hour, minute, sec, 0, 0, 0)) - JST
    except (ValueError, OverflowError):
        return None


def epoch_to_tod(when):
    tm = time.gmtime(max(0, int(when)) + JST)
    return TOD.pack(tm.tm_sec, tm.tm_min, tm.tm_hour, tm.tm_mday, tm.tm_mon, tm.tm_year)


def tod_text(t):
    if not t or not t[0]:
        return "-"
    return "%04d-%02d-%02d %02d:%02d" % t[:5]


# ---------------------------------------------------------------- names

def host_name(raw):
    """A card name as a safe host file name, or raise ToolError."""
    if not raw or raw in (b".", b".."):
        raise ToolError("empty or dot name")
    if any(c < 0x20 or c in b'/\\:*?"<>|' for c in raw):
        raise ToolError("name %r has characters a host folder cannot hold" % raw)
    if len(raw) > 31:
        raise ToolError("name %r is longer than 31 bytes" % raw)
    return os.fsdecode(raw)


def serial_of(name):
    m = SERIAL_RE.match(name)
    if not m:
        return None
    return "%s-%s%s" % (m.group(2), m.group(3), m.group(4)), REGIONS.get(m.group(1), "?")


def icon_title(blob):
    """The two-line save title from an icon.sys (Shift-JIS at 0xC0, line break offset at 0x06)."""
    if not blob or len(blob) < 0x104 or blob[:4] != b"PS2D":
        return None
    brk = struct.unpack_from("<H", blob, 6)[0]
    raw = blob[0xC0:0x104].split(b"\0")[0]
    brk = min(brk, len(raw))

    def conv(b):
        return unicodedata.normalize("NFKC", b.decode("cp932", "replace")).strip()
    parts = [p for p in (conv(raw[:brk]), conv(raw[brk:])) if p]
    return " / ".join(parts) if parts else None


# ---------------------------------------------------------------- card image

class Entry(object):
    __slots__ = ("name", "raw_name", "mode", "length", "created", "modified", "cluster", "attr", "children")

    def __init__(self, name, raw_name, mode, length, created, modified, cluster, attr):
        self.name, self.raw_name, self.mode, self.length = name, raw_name, mode, length
        self.created, self.modified, self.cluster, self.attr = created, modified, cluster, attr
        self.children = []

    @property
    def is_dir(self):
        return bool(self.mode & DF_DIR)


def unpack_dirent(raw):
    mode, _unused, length, created, cluster, parent, modified, attr, name = DIRENT.unpack(raw)
    return {"mode": mode, "length": length, "created": tod_unpack(created), "cluster": cluster,
            "parent": parent, "modified": tod_unpack(modified), "attr": attr,
            "raw_name": name[:32].split(b"\0")[0]}


def pack_dirent(mode, length, created, modified, raw_name, cluster=0, parent=0, attr=0):
    return DIRENT.pack(mode, 0, length, created, cluster, parent, modified, attr, raw_name[:32])


class Card(object):
    """Read-only PS2 memory card image (.ps2 / .mcd / .bin), with or without ECC."""

    def __init__(self, path):
        self.path = path
        try:
            self.size = os.path.getsize(path)
            self.fh = open(path, "rb")
        except OSError as e:
            raise ToolError("cannot read %s: %s" % (path, e.strerror or e))
        sb = self.fh.read(0x154)
        if len(sb) < 0x154 or sb[:28] != MAGIC:
            self.fh.close()
            raise ToolError("%s is not a PS2 memory card image (no 'Sony PS2 Memory Card Format' superblock)" % path)
        self.version = sb[0x1C:0x28].split(b"\0")[0].decode("ascii", "replace")
        self.page_len, self.ppc, self.ppb = struct.unpack_from("<HHH", sb, 0x28)
        (self.clusters, self.alloc_offset, self.alloc_end, self.root_cluster,
         self.backup1, self.backup2) = struct.unpack_from("<6I", sb, 0x30)
        self.ifc_list = struct.unpack_from("<32I", sb, 0x50)
        self.card_type, self.card_flags = sb[0x150], sb[0x151]
        if self.page_len < 128 or self.page_len % 128 or self.page_len > 8192 or not 1 <= self.ppc <= 16:
            raise ToolError("%s: implausible page size %d / pages per cluster %d" % (path, self.page_len, self.ppc))
        if not 16 <= self.clusters <= (1 << 22):
            raise ToolError("%s: implausible cluster count %d" % (path, self.clusters))
        if self.alloc_offset + self.alloc_end > self.clusters or self.root_cluster >= max(self.alloc_end, 1):
            raise ToolError("%s: allocation area (%d + %d clusters) does not fit the card" %
                            (path, self.alloc_offset, self.alloc_end))
        self.cluster_size = self.page_len * self.ppc
        pages = self.clusters * self.ppc
        self.spare = -(-self.page_len // 128) * 4
        plain, ecc = pages * self.page_len, pages * (self.page_len + self.spare)
        if self.size == ecc:
            self.ecc = True
        elif self.size == plain:
            self.ecc = False
        else:
            raise ToolError("%s is %d bytes; a card with this superblock is %d bytes (no ECC) or %d (with ECC):"
                            " truncated or not a raw card dump" % (path, self.size, plain, ecc))
        self.raw_page = self.page_len + (self.spare if self.ecc else 0)
        self._cache = {}

    def close(self):
        self.fh.close()

    def read_cluster(self, n):
        if n >= self.clusters:
            raise ToolError("cluster %d is past the end of the card: the card is damaged" % n)
        c = self._cache.get(n)
        if c is not None:
            return c
        if not self.ecc:
            self.fh.seek(n * self.cluster_size)
            c = self.fh.read(self.cluster_size)
        else:
            parts = []
            for p in range(n * self.ppc, n * self.ppc + self.ppc):
                self.fh.seek(p * self.raw_page)
                parts.append(self.fh.read(self.page_len))
            c = b"".join(parts)
        if len(c) != self.cluster_size:
            raise ToolError("short read at cluster %d" % n)
        if len(self._cache) < 4096:
            self._cache[n] = c
        return c

    def fat(self, n):
        epc = self.cluster_size // 4
        fat_idx, ent = divmod(n, epc)
        ind_idx, ind_ent = divmod(fat_idx, epc)
        if ind_idx >= 32 or not self.ifc_list[ind_idx]:
            raise ToolError("FAT entry %d is outside the indirect FAT list: the card is damaged" % n)
        ind = self.read_cluster(self.ifc_list[ind_idx])
        fat_cluster = struct.unpack_from("<I", ind, ind_ent * 4)[0]
        if not fat_cluster or fat_cluster >= self.clusters:
            raise ToolError("indirect FAT points at cluster %d: the card is damaged" % fat_cluster)
        return struct.unpack_from("<I", self.read_cluster(fat_cluster), ent * 4)[0]

    def chain(self, start, need):
        """The first `need` clusters of a chain (relative to the allocation area)."""
        out = []
        seen = set()
        n = start
        while len(out) < need:
            if n >= self.alloc_end:
                raise ToolError("a cluster chain points past the allocation area (cluster %d): the card is damaged" % n)
            if n in seen:
                raise ToolError("a cluster chain loops back on itself at cluster %d: the card is damaged" % n)
            seen.add(n)
            out.append(n)
            if len(out) == need:
                break
            v = self.fat(n)
            if not v & FAT_ALLOCATED:
                raise ToolError("a cluster chain runs into a free cluster after cluster %d: the card is damaged" % n)
            nxt = v & FAT_NEXT_MASK
            if nxt == FAT_NEXT_MASK:
                raise ToolError("a chain ends after %d clusters but its entry needs %d: the card is damaged"
                                % (len(out), need))
            n = nxt
        return out

    def read_chain(self, start, length):
        if length == 0:
            return b""
        need = -(-length // self.cluster_size)
        data = b"".join(self.read_cluster(self.alloc_offset + c) for c in self.chain(start, need))
        return data[:length]

    def dirents(self, cluster, count):
        if not 1 <= count <= 16384:
            raise ToolError("a directory claims %d entries: the card is damaged" % count)
        blob = self.read_chain(cluster, count * DIRENT_SIZE)
        return [unpack_dirent(blob[i * DIRENT_SIZE:(i + 1) * DIRENT_SIZE]) for i in range(count)]

    def root(self):
        first = self.dirents(self.root_cluster, 1)[0]
        if not first["mode"] & DF_DIR:
            raise ToolError("the root directory's first entry is not a directory: not formatted, or damaged")
        if first["length"] < 2:
            raise ToolError("the root directory claims %d entries (at least . and .. are needed): the card is"
                            " damaged" % first["length"])
        return self.dirents(self.root_cluster, first["length"])

    def tree(self):
        """Top-level entries (saves) with their children, deleted entries skipped."""
        return self._children(self.root(), 0)

    def _children(self, ents, depth):
        out = []
        for i, d in enumerate(ents):
            if i < 2 or not d["mode"] & DF_EXISTS or d["raw_name"] in (b".", b".."):
                continue
            name = os.fsdecode(d["raw_name"])
            e = Entry(name, d["raw_name"], d["mode"], d["length"], d["created"], d["modified"], d["cluster"], d["attr"])
            if e.is_dir and depth < 8:
                e.children = self._children(self.dirents(d["cluster"], d["length"]), depth + 1)
            out.append(e)
        return out

    def read_file(self, entry):
        return self.read_chain(entry.cluster, entry.length)

    def free_clusters(self):
        return sum(1 for n in range(self.alloc_end) if not self.fat(n) & FAT_ALLOCATED)


# ---------------------------------------------------------------- folder sources (mc0, PCSX2 folder cards)

def _skip_host(name):
    return name.startswith("_pcsx2_") or name.startswith(".console-") or name in SKIP_NAMES or name.startswith("._")


class FolderCard(object):
    """A folder holding save folders: the runtime's mc0, or a PCSX2 folder memory card."""

    def __init__(self, path):
        self.path = path
        self.pcsx2 = os.path.exists(os.path.join(path, "_pcsx2_superblock"))

    def tree(self):
        out = []
        for name in sorted(os.listdir(self.path), key=lambda s: s.lower()):
            full = os.path.join(self.path, name)
            if _skip_host(name) or os.path.islink(full):
                continue
            out.append(self._entry(full, name, 0))
        return out

    def _entry(self, full, name, depth):
        st = os.stat(full)
        t = tod_unpack(epoch_to_tod(st.st_mtime))
        if os.path.isdir(full):
            e = Entry(name, os.fsencode(name), MODE_DIR, 0, t, t, 0, 0)
            if depth < 8:
                for n in sorted(os.listdir(full), key=lambda s: s.lower()):
                    p = os.path.join(full, n)
                    if not _skip_host(n) and not os.path.islink(p):
                        e.children.append(self._entry(p, n, depth + 1))
            e.length = len(e.children) + 2
        else:
            e = Entry(name, os.fsencode(name), MODE_FILE, st.st_size, t, t, 0, 0)
        e.cluster = full  # host path, read by read_file
        return e

    def read_file(self, entry):
        with open(entry.cluster, "rb") as fh:
            return fh.read()

    def close(self):
        pass


def open_source(path):
    if os.path.isdir(path):
        return FolderCard(path)
    if not os.path.exists(path):
        raise ToolError("%s does not exist" % path)
    return Card(path)


def save_summary(src, e):
    files = [c for c in e.children if not c.is_dir]
    total = sum(c.length for c in files)
    title = None
    for c in files:
        if c.name.lower() == "icon.sys":
            try:
                title = icon_title(src.read_file(c))
            except ToolError:
                title = None
    ser = serial_of(e.name)
    return {"name": e.name, "dir": e.is_dir, "files": len(files), "bytes": total if e.is_dir else e.length,
            "modified": tod_text(e.modified), "title": title,
            "serial": ser[0] if ser else None, "region": ser[1] if ser else None,
            "subfolders": sum(1 for c in e.children if c.is_dir),
            "file_list": [{"name": c.name, "bytes": c.length, "mode": "0x%04x" % c.mode} for c in e.children]}


# ---------------------------------------------------------------- commands: info, ls

def cmd_info(args):
    card = Card(args.card)
    try:
        free = card.free_clusters()
        res = {"card": args.card, "bytes": card.size, "ecc": card.ecc, "version": card.version,
               "page_len": card.page_len, "pages_per_cluster": card.ppc, "pages_per_block": card.ppb,
               "clusters_per_card": card.clusters, "alloc_offset": card.alloc_offset, "alloc_end": card.alloc_end,
               "rootdir_cluster": card.root_cluster, "backup_block1": card.backup1, "backup_block2": card.backup2,
               "card_type": card.card_type, "card_flags": "0x%02x" % card.card_flags,
               "free_kb": free * card.cluster_size // 1024,
               "usable_kb": card.alloc_end * card.cluster_size // 1024}
    finally:
        card.close()
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print("%s: PS2 memory card, format %s, %s, %s" % (args.card, res["version"],
                                                         "%d MB" % (card.clusters * card.cluster_size // (1 << 20)),
                                                         "with ECC (528-byte pages)" if card.ecc else "no ECC"))
        print("  free %d KB of %d KB; %d-byte pages, %d per cluster, allocation from cluster %d"
              % (res["free_kb"], res["usable_kb"], card.page_len, card.ppc, card.alloc_offset))
    return 0


def cmd_ls(args):
    src = open_source(args.card)
    try:
        rows = [save_summary(src, e) for e in src.tree()]
    finally:
        src.close()
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("no saves on %s" % args.card)
        return 0
    print("%-22s %5s %9s  %-16s %-11s %s" % ("save folder", "files", "size", "modified", "serial", "title"))
    for r in rows:
        print("%-22s %5d %8dK  %-16s %-11s %s" % (r["name"], r["files"], -(-r["bytes"] // 1024), r["modified"],
                                                   r["serial"] or "-", r["title"] or ""))
        if args.files:
            for f in r["file_list"]:
                print("    %-30s %8d bytes  mode %s" % (f["name"], f["bytes"], f["mode"]))
    print("%d save(s). Card times are the PS2 clock (Japan time, UTC+9)." % len(rows))
    return 0


# ---------------------------------------------------------------- export

def _write_tree(src, e, dest, warnings):
    os.makedirs(dest, exist_ok=True)
    count = 0
    for c in e.children:
        try:
            nm = host_name(c.raw_name)
        except ToolError as err:
            warnings.append("skipped %s/%r: %s" % (e.name, c.raw_name, err))
            continue
        target = os.path.join(dest, nm)
        if c.is_dir:
            count += _write_tree(src, c, target, warnings)
        else:
            data = src.read_file(c)
            with open(target, "wb") as fh:
                fh.write(data)
            _stamp(target, c.modified)
            count += 1
    _stamp(dest, e.modified)
    return count


def _stamp(path, tod):
    t = tod_to_epoch(tod) if isinstance(tod, tuple) else None
    if t is not None and t > 0:
        try:
            os.utime(path, (t, t))
        except OSError:
            pass


def cmd_export(args):
    if os.path.abspath(args.card) == os.path.abspath(args.to):
        raise ToolError("the source and --to are the same folder")
    src = open_source(args.card)
    warnings = []
    done = []
    try:
        saves = [e for e in src.tree() if e.is_dir]
        if args.only:
            want = set(args.only)
            missing = want - set(e.name for e in saves)
            if missing:
                raise ToolError("not on the card: %s" % ", ".join(sorted(missing)))
            saves = [e for e in saves if e.name in want]
        os.makedirs(args.to, exist_ok=True)
        clash = [e.name for e in saves if os.path.exists(os.path.join(args.to, e.name))]
        if clash and not args.force:
            raise Attention("already in %s: %s. Nothing written. Pass --force to replace them (mc0 is backed up"
                            " first)." % (args.to, ", ".join(clash)))
        if clash:
            z = make_backup(args.to, args.backups, keep=args.keep)
            print("backed up %s to %s before replacing %d save(s)" % (args.to, z, len(clash)))
        for e in saves:
            try:
                nm = host_name(e.raw_name)
            except ToolError as err:
                warnings.append("skipped %r: %s" % (e.raw_name, err))
                continue
            dest = os.path.join(args.to, nm)
            tmp = os.path.join(args.to, ".console-tmp-" + nm)
            if os.path.exists(tmp):
                shutil.rmtree(tmp)
            try:
                n = _write_tree(src, e, tmp, warnings)
            except ToolError as err:
                shutil.rmtree(tmp, ignore_errors=True)
                warnings.append("skipped %s, nothing written for it: %s" % (e.name, err))
                continue
            except BaseException:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            if os.path.isdir(dest) and not os.path.islink(dest):
                shutil.rmtree(dest)
            elif os.path.lexists(dest):
                os.remove(dest)
            os.replace(tmp, dest)
            done.append((e.name, n))
    finally:
        src.close()
    for name, n in done:
        print("exported %s (%d file%s) -> %s" % (name, n, "" if n == 1 else "s", os.path.join(args.to, name)))
    for w in warnings:
        print("WARNING: %s" % w)
    print("%d save(s) in %s. The port reads them as mc0:/<folder>/... (slot 2 is the sibling mc1 folder)."
          % (len(done), args.to))
    return 1 if warnings else 0


# ---------------------------------------------------------------- psu

def psu_build(src, e):
    """Bytes of a .psu for one save folder entry (files only)."""
    if any(c.is_dir for c in e.children):
        raise ToolError("%s has a subfolder; the .psu format holds one folder of files" % e.name)
    files = [c for c in e.children]
    created = TOD.pack(*_tod_fields(e.created))
    modified = TOD.pack(*_tod_fields(e.modified))
    out = bytearray(pack_dirent(MODE_DIR, len(files) + 2, created, modified, e.raw_name))
    out += pack_dirent(MODE_DIR, 0, created, created, b".")
    out += pack_dirent(MODE_DIR, 0, created, created, b"..")
    for c in files:
        if len(c.raw_name) > 31:
            raise ToolError("file name %r is longer than 31 bytes" % c.raw_name)
        data = src.read_file(c)
        mode = c.mode if c.mode & DF_FILE else MODE_FILE
        out += pack_dirent(mode, len(data), TOD.pack(*_tod_fields(c.created)), TOD.pack(*_tod_fields(c.modified)),
                           c.raw_name)
        out += data + bytes((-len(data)) % PSU_CLUSTER)
    return bytes(out)


def _tod_fields(t):
    year, month, day, hour, minute, sec = t
    return (sec, minute, hour, day, month, year)


def psu_parse(blob, label="psu"):
    if len(blob) < 3 * DIRENT_SIZE:
        raise ToolError("%s is too short to be a .psu" % label)
    heads = [unpack_dirent(blob[i * DIRENT_SIZE:(i + 1) * DIRENT_SIZE]) for i in range(3)]
    if not all(h["mode"] & DF_DIR for h in heads) or heads[0]["length"] < 2:
        raise ToolError("%s is not a .psu save (the first three headers must be folders)" % label)
    folder = heads[0]
    count = folder["length"] - 2
    if count > 4096:
        raise ToolError("%s claims %d files" % (label, count))
    pos = 3 * DIRENT_SIZE
    files = []
    for i in range(count):
        if pos + DIRENT_SIZE > len(blob):
            raise ToolError("%s is truncated: header %d of %d is missing" % (label, i + 1, count))
        h = unpack_dirent(blob[pos:pos + DIRENT_SIZE])
        pos += DIRENT_SIZE
        if h["mode"] & DF_DIR or not h["mode"] & DF_FILE:
            raise ToolError("%s holds a subfolder (%r); not supported" % (label, h["raw_name"]))
        n = h["length"]
        if pos + n > len(blob):
            raise ToolError("%s is truncated inside %r (%d bytes expected, %d present)"
                            % (label, h["raw_name"], n, len(blob) - pos))
        h["data"] = blob[pos:pos + n]
        pos += n + (-n) % PSU_CLUSTER
        files.append(h)
    return folder, files


def cmd_psu_export(args):
    if os.path.isdir(args.source):
        src = FolderCard(os.path.dirname(os.path.abspath(args.source)) or ".")
        name = os.path.basename(os.path.normpath(args.source))
        entry = src._entry(os.path.abspath(args.source), name, 0)
    else:
        src = Card(args.source)
        if not args.save:
            src.close()
            raise ToolError("exporting from a card image needs --save <folder name> (see memcard.py ls)")
        matches = [e for e in src.tree() if e.is_dir and e.name == args.save]
        if not matches:
            src.close()
            raise ToolError("%s is not on %s" % (args.save, args.source))
        entry = matches[0]
    try:
        host_name(entry.raw_name)
        blob = psu_build(src, entry)
    finally:
        src.close()
    out = args.out or os.path.join("console", "saves", entry.name + ".psu")
    if os.path.exists(out) and not args.force:
        raise ToolError("%s exists (pass --force to overwrite)" % out)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "wb") as fh:
        fh.write(blob)
    print("wrote %s: %s, %d file(s), %d bytes" % (out, entry.name, len(entry.children), len(blob)))
    return 0


def cmd_psu_import(args):
    try:
        with open(args.psu, "rb") as fh:
            blob = fh.read()
    except OSError as e:
        raise ToolError("cannot read %s: %s" % (args.psu, e.strerror or e))
    folder, files = psu_parse(blob, args.psu)
    name = host_name(folder["raw_name"])
    names = []
    for f in files:
        names.append(host_name(f["raw_name"]))
    if len(set(n.lower() for n in names)) != len(names):
        raise ToolError("%s has two files whose names differ only in case" % args.psu)
    dest = os.path.join(args.to, name)
    if os.path.exists(dest):
        if not args.force:
            raise Attention("%s already exists. Nothing written. Pass --force to replace it (mc0 is backed up"
                            " first)." % dest)
        z = make_backup(args.to, args.backups, keep=args.keep)
        print("backed up %s to %s" % (args.to, z))
        shutil.rmtree(dest)
    os.makedirs(dest)
    for f, n in zip(files, names):
        p = os.path.join(dest, n)
        with open(p, "wb") as fh:
            fh.write(f["data"])
        _stamp(p, f["modified"])
    _stamp(dest, folder["modified"])
    print("imported %s (%d file(s)) -> %s" % (name, len(files), dest))
    return 0


def cmd_psu_ls(args):
    try:
        with open(args.psu, "rb") as fh:
            blob = fh.read()
    except OSError as e:
        raise ToolError("cannot read %s: %s" % (args.psu, e.strerror or e))
    folder, files = psu_parse(blob, args.psu)
    title = None
    for f in files:
        if f["raw_name"].lower() == b"icon.sys":
            title = icon_title(f["data"])
    res = {"folder": os.fsdecode(folder["raw_name"]), "modified": tod_text(folder["modified"]), "title": title,
           "files": [{"name": os.fsdecode(f["raw_name"]), "bytes": f["length"], "mode": "0x%04x" % f["mode"]}
                     for f in files]}
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print("%s  (%s)%s" % (res["folder"], res["modified"], ("  " + title) if title else ""))
        for f in res["files"]:
            print("    %-30s %8d bytes" % (f["name"], f["bytes"]))
    return 0


# ---------------------------------------------------------------- backup / restore

def _label(mc0):
    full = os.path.abspath(mc0)
    parent = os.path.basename(os.path.dirname(full)) or "card"
    lab = "%s-%s" % (parent, os.path.basename(full))
    return re.sub(r"[^A-Za-z0-9_.-]", "_", lab)


def _host_files(root):
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(base, d)))
        for f in sorted(files):
            p = os.path.join(base, f)
            if not os.path.islink(p):
                out.append(p)
    return out


def make_backup(mc0, to, keep=20, label=None):
    if not os.path.isdir(mc0):
        raise ToolError("%s is not a folder" % mc0)
    to = to or os.path.join("console", "saves", "backups")
    full_to, full_mc = os.path.abspath(to), os.path.abspath(mc0)
    if full_to == full_mc or full_to.startswith(full_mc + os.sep):
        raise ToolError("put backups outside the card folder (the game would see them in %s)" % mc0)
    os.makedirs(to, exist_ok=True)
    label = label or _label(mc0)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    # same second as an earlier backup: number it after the highest one so it sorts newest
    pat = re.compile(r"^%s-%s(?:-(\d+))?\.zip$" % (re.escape(label), stamp))
    taken = [int(m.group(1) or 1) for m in (pat.match(f) for f in os.listdir(to)) if m]
    k = max(taken) + 1 if taken else 1
    path = os.path.join(to, "%s-%s%s.zip" % (label, stamp, "" if k == 1 else "-%d" % k))
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as z:
        for p in _host_files(mc0):
            z.write(p, os.path.relpath(p, mc0).replace(os.sep, "/"))
    os.replace(tmp, path)
    if keep and keep > 0:
        prune_backups(to, label, keep, protect=os.path.basename(path))
    return path


def prune_backups(folder, label, keep, protect=None):
    """Delete this label's oldest backups beyond `keep`. Other labels are never touched."""
    pat = re.compile(r"^%s-(\d{8}T\d{6}Z)(?:-(\d+))?\.zip$" % re.escape(label))
    mine = []
    for f in os.listdir(folder):
        m = pat.match(f)
        if m:
            mine.append((m.group(1), int(m.group(2) or 1), f))
    mine.sort()
    removed = []
    if len(mine) > keep:
        for _s, _k, f in mine[:len(mine) - keep]:
            if f == protect:
                continue
            os.remove(os.path.join(folder, f))
            removed.append(f)
    return removed


def cmd_backup(args):
    if not os.path.isdir(args.mc0):
        raise ToolError("%s is not a folder (point at the port's mc0 folder)" % args.mc0)
    path = make_backup(args.mc0, args.to, args.keep, args.label)
    with zipfile.ZipFile(path) as z:
        n = len(z.namelist())
    print("backed up %s (%d file%s) to %s; keeping the newest %d" % (args.mc0, n, "" if n == 1 else "s", path, args.keep))
    return 0


def _safe_member(name):
    n = name.replace("\\", "/")
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n) or ".." in n.split("/") or "\0" in n:
        raise ToolError("backup contains an unsafe path %r; refusing to restore" % name)
    return n


def cmd_restore(args):
    try:
        z = zipfile.ZipFile(args.zip)
    except (OSError, zipfile.BadZipFile) as e:
        raise ToolError("%s is not a readable zip: %s" % (args.zip, e))
    with z:
        names = [_safe_member(m) for m in z.namelist()]
        if os.path.isdir(args.to) and os.listdir(args.to):
            if not args.force:
                raise Attention("%s is not empty. Nothing written. Pass --force to replace its contents (it is"
                                " backed up first)." % args.to)
            b = make_backup(args.to, args.backups, keep=0)
            print("backed up the current %s to %s" % (args.to, b))
            for entry in os.listdir(args.to):
                p = os.path.join(args.to, entry)
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p)
                else:
                    os.remove(p)
        os.makedirs(args.to, exist_ok=True)
        for info, n in zip(z.infolist(), names):
            target = os.path.join(args.to, *n.split("/"))
            if n.endswith("/"):
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            try:
                t = time.mktime(info.date_time + (0, 0, -1))
                os.utime(target, (t, t))
            except (OSError, OverflowError, ValueError):
                pass
    print("restored %d file(s) from %s into %s" % (len([n for n in names if not n.endswith("/")]), args.zip, args.to))
    return 0


# ---------------------------------------------------------------- sync

def _sync_files(root):
    out = {}
    placeholders = []
    for p in _host_files(root):
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        parts = rel.split("/")
        base = parts[-1]
        if parts[0] in (".console-conflicts",) or base.startswith(".console-"):
            continue
        if base.endswith(".icloud") and base.startswith("."):
            placeholders.append(rel)
            continue
        if base in SKIP_NAMES or base.startswith("._") or base.startswith("_pcsx2_"):
            continue
        st = os.stat(p)
        out[rel] = {"path": p, "size": st.st_size, "mtime": st.st_mtime, "sha": None}
    return out, placeholders


def _sha(info):
    if info["sha"] is None:
        h = hashlib.sha256()
        with open(info["path"], "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        info["sha"] = h.hexdigest()
    return info["sha"]


def _copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = os.path.join(os.path.dirname(dst), ".console-tmp-" + os.path.basename(dst))
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def _device():
    return re.sub(r"[^A-Za-z0-9_-]", "_", socket.gethostname().split(".")[0]) or "device"


def plan_sync(local, cloud, base, direction, prefer):
    """Decide per file. Returns a list of (rel, action, reason)."""
    plan = []
    for rel in sorted(set(local) | set(cloud)):
        L, C = local.get(rel), cloud.get(rel)
        B = base.get(rel)
        if L and C and L["size"] == C["size"] and _sha(L) == _sha(C):
            plan.append((rel, "same", "identical"))
            continue
        if L and not C:
            act, why = "push", ("new here" if not B else "missing in the cloud (sync never deletes)")
        elif C and not L:
            act, why = "pull", ("new in the cloud" if not B else "missing here (sync never deletes)")
        elif B and _sha(L) == B["sha"]:
            act, why = "pull", "changed in the cloud since the last sync"
        elif B and _sha(C) == B["sha"]:
            act, why = "push", "changed here since the last sync"
        else:
            why = "changed on both sides since the last sync" if B else "different on both sides and never synced"
            if prefer == "local":
                act = "push-keep"
            elif prefer == "cloud":
                act = "pull-keep"
            elif prefer == "newest" and L["mtime"] != C["mtime"]:
                act = "push-keep" if L["mtime"] > C["mtime"] else "pull-keep"
            else:
                act = "conflict"
            if act != "conflict":
                why += "; --prefer %s" % prefer
        if direction == "push" and act.startswith("pull"):
            act, why = "skip", why + " (run without --push to bring it here)"
        elif direction == "pull" and act.startswith("push"):
            act, why = "skip", why + " (run without --pull to send it up)"
        plan.append((rel, act, why))
    return plan


def _manifest_path(mc0, override):
    if override:
        return override
    full = os.path.abspath(mc0)
    return os.path.join(os.path.dirname(full), ".console-sync.json")


def _manifest_key(mc0, cloud):
    return "%s -> %s" % (os.path.abspath(mc0), os.path.abspath(cloud))


def cmd_sync(args):
    mc0, cloud = args.mc0, args.cloud
    if not os.path.isdir(mc0):
        raise ToolError("%s is not a folder (point at the port's mc0 folder)" % mc0)
    if os.path.abspath(cloud).startswith(os.path.abspath(mc0) + os.sep) or \
            os.path.abspath(mc0).startswith(os.path.abspath(cloud) + os.sep) or \
            os.path.abspath(mc0) == os.path.abspath(cloud):
        raise ToolError("the cloud folder and mc0 must not contain each other")
    if os.path.exists(cloud) and not os.path.isdir(cloud):
        raise ToolError("%s is a file, not a folder" % cloud)
    if not os.path.isdir(cloud):
        parent = os.path.dirname(os.path.abspath(cloud))
        if not os.path.isdir(parent):
            raise ToolError("%s does not exist (and neither does its parent): check the cloud path" % cloud)
        if not args.dry_run:
            os.makedirs(cloud)
    direction = "push" if args.push else ("pull" if args.pull else "both")
    mpath = _manifest_path(mc0, args.manifest)
    manifest = {}
    if os.path.exists(mpath):
        try:
            with open(mpath) as fh:
                manifest = json.load(fh)
        except ValueError:
            raise ToolError("%s is not valid JSON; move it aside to start fresh" % mpath)
    key = _manifest_key(mc0, cloud)
    base = manifest.get(key, {}).get("files", {})
    local, lph = _sync_files(mc0)
    remote, rph = _sync_files(cloud) if os.path.isdir(cloud) else ({}, [])
    if lph or rph:
        print("refused: %d file(s) are cloud placeholders not downloaded yet (%s). Download the folder first"
              " (Finder: Download Now, or keep it downloaded), then sync." % (len(lph + rph), ", ".join((lph + rph)[:3])),
              file=sys.stderr)
        return 1
    plan = plan_sync(local, remote, base, direction, args.prefer)
    counts = {}
    for _rel, act, _why in plan:
        counts[act] = counts.get(act, 0) + 1
    if args.json and args.dry_run:
        print(json.dumps([{"path": r, "action": a, "reason": w} for r, a, w in plan], indent=2))
        return 1 if counts.get("conflict") else 0
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    cdir = os.path.join(cloud, ".console-conflicts", "%s-%s" % (stamp, _device()))
    writes_here = any(a in ("pull", "pull-keep") for _r, a, _w in plan)
    backup = None
    if writes_here and not args.dry_run and _host_files(mc0):
        backup = make_backup(mc0, args.backups, keep=args.keep)
    newbase = dict(base)
    for rel, act, why in plan:
        L, C = local.get(rel), remote.get(rel)
        lp = os.path.join(mc0, *rel.split("/"))
        cp = os.path.join(cloud, *rel.split("/"))
        if not args.dry_run:
            if act in ("conflict", "push-keep", "pull-keep"):
                _copy(L["path"], os.path.join(cdir, "here", *rel.split("/")))
                _copy(C["path"], os.path.join(cdir, "cloud", *rel.split("/")))
            if act in ("push", "push-keep"):
                _copy(L["path"], cp)
                newbase[rel] = {"sha": _sha(L), "size": L["size"]}
            elif act in ("pull", "pull-keep"):
                _copy(C["path"], lp)
                newbase[rel] = {"sha": _sha(C), "size": C["size"]}
            elif act == "same":
                newbase[rel] = {"sha": _sha(L), "size": L["size"]}
        if not args.json and (act != "same" or args.verbose):
            print("%-10s %s  (%s)" % (act, rel, why))
    for rel in list(newbase):
        if rel not in local and rel not in remote:
            del newbase[rel]
    if not args.dry_run:
        manifest[key] = {"files": newbase, "last_sync": stamp, "device": _device()}
        tmp = mpath + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(manifest, fh, indent=1, sort_keys=True)
        os.replace(tmp, mpath)
    summary = {"pushed": counts.get("push", 0) + counts.get("push-keep", 0),
               "pulled": counts.get("pull", 0) + counts.get("pull-keep", 0),
               "same": counts.get("same", 0), "conflicts": counts.get("conflict", 0),
               "skipped": counts.get("skip", 0), "backup": backup, "dry_run": args.dry_run,
               "conflict_copies": cdir if (counts.get("conflict") or counts.get("push-keep") or counts.get("pull-keep"))
               and not args.dry_run else None}
    if args.json:
        summary["plan"] = [{"path": r, "action": a, "reason": w} for r, a, w in plan]
        print(json.dumps(summary, indent=2))
    else:
        print("%s%d pushed, %d pulled, %d already the same, %d conflict(s), %d skipped"
              % ("dry run: " if args.dry_run else "", summary["pushed"], summary["pulled"], summary["same"],
                 summary["conflicts"], summary["skipped"]))
        if backup:
            print("mc0 was backed up first: %s" % backup)
        if summary["conflict_copies"]:
            print("both versions of every conflict are in %s (here/ and cloud/). Nothing was overwritten for a"
                  " conflict: pick one and rerun with --prefer local|cloud|newest." % summary["conflict_copies"])
    return 1 if summary["conflicts"] else 0


# ---------------------------------------------------------------- CLI

def build_parser():
    p = argparse.ArgumentParser(
        prog="memcard.py",
        description="PS2 memory card saves: read card images, move saves into the port's mc0 folder, .psu files,"
                    " backups and cloud sync.",
        epilog="Card images are read only. Close the game before export, restore or sync.")
    sub = p.add_subparsers(dest="cmd", metavar="command")
    sub.required = True

    s = sub.add_parser("info", help="superblock of a card image (.ps2/.mcd/.bin): ECC, version, free space")
    s.add_argument("card")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("ls", help="saves on a card image, a PCSX2 folder card or an mc0 folder")
    s.add_argument("card")
    s.add_argument("--files", action="store_true", help="list the files in each save")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("export", help="copy saves from a card into the port's folder memory card (mc0)")
    s.add_argument("card", help="card image, PCSX2 folder card, or another mc0 folder")
    s.add_argument("--to", required=True, help="the mc0 folder next to the game's ELF")
    s.add_argument("--only", nargs="+", metavar="SAVE", help="only these save folders")
    s.add_argument("--force", action="store_true", help="replace saves already in mc0 (backs mc0 up first)")
    s.add_argument("--backups", default=None, help="backup folder (default console/saves/backups)")
    s.add_argument("--keep", type=int, default=20)
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("psu", help=".psu single-save files: export, import, ls")
    psub = s.add_subparsers(dest="psu_cmd", metavar="action")
    psub.required = True
    q = psub.add_parser("export", help="one save folder (mc0/SAVE, or a card image with --save) to a .psu")
    q.add_argument("source", help="mc0/<SAVE> folder, or a card image")
    q.add_argument("--save", help="the save folder name when the source is a card image")
    q.add_argument("--out", help="the .psu path (default console/saves/<SAVE>.psu)")
    q.add_argument("--force", action="store_true")
    q.set_defaults(func=cmd_psu_export)
    q = psub.add_parser("import", help="a .psu into mc0/<SAVE>/")
    q.add_argument("psu")
    q.add_argument("--to", required=True, help="the mc0 folder")
    q.add_argument("--force", action="store_true", help="replace an existing save (backs mc0 up first)")
    q.add_argument("--backups", default=None)
    q.add_argument("--keep", type=int, default=20)
    q.set_defaults(func=cmd_psu_import)
    q = psub.add_parser("ls", help="what a .psu holds")
    q.add_argument("psu")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_psu_ls)

    s = sub.add_parser("backup", help="timestamped zip of mc0, keeping the newest N")
    s.add_argument("mc0")
    s.add_argument("--to", default=os.path.join("console", "saves", "backups"))
    s.add_argument("--keep", type=int, default=20, help="backups to keep for this card (default 20, 0 = all)")
    s.add_argument("--label", default=None, help="name prefix (default <parent folder>-<mc0 folder>)")
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser("restore", help="put a backup zip back into mc0 (backs up the current contents first)")
    s.add_argument("zip")
    s.add_argument("--to", required=True, help="the mc0 folder")
    s.add_argument("--force", action="store_true", help="replace a non-empty mc0")
    s.add_argument("--backups", default=None)
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("sync", help="sync mc0 with a cloud folder: changed side wins, conflicts keep both")
    s.add_argument("mc0")
    s.add_argument("--cloud", required=True, help="a folder your cloud app syncs (iCloud Drive, Dropbox, ...)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--push", action="store_true", help="only send changes up")
    g.add_argument("--pull", action="store_true", help="only bring changes down")
    s.add_argument("--prefer", choices=("local", "cloud", "newest"), default=None,
                   help="settle conflicts this way (the losing version is still kept in .console-conflicts)")
    s.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    s.add_argument("--manifest", default=None, help="sync state file (default .console-sync.json next to mc0)")
    s.add_argument("--backups", default=None, help="backup folder (default console/saves/backups)")
    s.add_argument("--keep", type=int, default=20)
    s.add_argument("--verbose", action="store_true", help="also list files that are already the same")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sync)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ToolError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    except Attention as e:
        print("refused: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
