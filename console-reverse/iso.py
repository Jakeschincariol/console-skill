#!/usr/bin/env python3
"""Read an ISO 9660 disc image you dumped from your own PS2 disc.

Subcommands:
  info     volume name, layout, size, and whether the dump looks complete
  ls       list a directory on the disc (or the whole tree with -r)
  extract  copy the whole disc, or selected files and folders, into a folder
  plain    convert a raw 2352-byte .bin into a plain 2048-byte-sector .iso (for the runtime's cdImage)
  boot     read SYSTEM.CNF (from an image or an extracted folder): boot ELF, serial, region

Pure Python standard library. Streams the image: it never reads a whole disc into memory.
Handles plain 2048-byte sector images (.iso) and raw 2352-byte sector images (.bin,
Mode 1 or Mode 2 Form 1). Joliet and UDF are not read: PS2 discs carry ISO 9660 names.
"""

import argparse
import json
import os
import re
import struct
import sys

BLOCK = 2048
COPY_BLOCKS = 512          # 1 MiB per read when copying
MAX_DEPTH = 64
MAX_ENTRIES = 2000000
SYNC = b"\x00" + b"\xff" * 10 + b"\x00"

REGIONS = {
    "SLUS": "USA", "SCUS": "USA",
    "SLES": "Europe", "SCES": "Europe",
    "SLPS": "Japan", "SLPM": "Japan", "SCPS": "Japan",
    "SLKA": "Korea", "SCKA": "Korea",
    "SCAJ": "Asia",
}


class InputError(Exception):
    """Bad input: exit 2 with a one-line message."""


# ---------------------------------------------------------------- image access

class IsoImage(object):
    def __init__(self, path):
        self.path = path
        try:
            self.f = open(path, "rb")
        except OSError as e:
            raise InputError("cannot open %s: %s" % (path, e.strerror or e))
        try:
            self.f.seek(0, os.SEEK_END)
            self.image_size = self.f.tell()
            self.sector_size, self.data_offset, self.layout = self._detect_layout()
            self.pvd = self._find_pvd()
            self._parse_pvd()
        except BaseException:
            self.f.close()
            raise

    def close(self):
        try:
            self.f.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _raw(self, offset, size):
        if offset < 0 or offset + size > self.image_size:
            return None
        self.f.seek(offset)
        data = self.f.read(size)
        if len(data) != size:
            return None
        return data

    def _detect_layout(self):
        candidates = [
            (2048, 0, "2048-byte sectors (plain ISO)"),
            (2352, 16, "2352-byte raw sectors, Mode 1"),
            (2352, 24, "2352-byte raw sectors, Mode 2 Form 1"),
        ]
        for sector_size, data_offset, name in candidates:
            head = self._raw(16 * sector_size + data_offset, 8)
            if head is not None and head[1:6] == b"CD001":
                if sector_size == 2352:
                    sync = self._raw(16 * sector_size, 12)
                    if sync != SYNC:
                        continue
                return sector_size, data_offset, name
        if self.image_size < 17 * BLOCK:
            raise InputError("%s is too small to be a disc image (%d bytes)" % (self.path, self.image_size))
        raise InputError("%s is not an ISO 9660 image (no CD001 volume descriptor at sector 16)" % self.path)

    def block_offset(self, lba):
        return lba * self.sector_size + self.data_offset

    def read_block(self, lba):
        data = self._raw(self.block_offset(lba), BLOCK)
        if data is None:
            raise InputError("sector %d is past the end of the image (truncated dump?)" % lba)
        return data

    def extent_fits(self, lba, size):
        if size == 0:
            return True
        blocks = (size + BLOCK - 1) // BLOCK
        last = lba + blocks - 1
        return self.block_offset(last) + min(BLOCK, size - (blocks - 1) * BLOCK) <= self.image_size

    def iter_extent(self, lba, size):
        """Yield the bytes of an extent in chunks, without loading it whole."""
        remaining = size
        cur = lba
        while remaining > 0:
            if self.sector_size == BLOCK:
                chunk = min(remaining, COPY_BLOCKS * BLOCK)
                data = self._raw(self.block_offset(cur), chunk)
                if data is None:
                    raise InputError("data at sector %d is past the end of the image (truncated dump?)" % cur)
                cur += (chunk + BLOCK - 1) // BLOCK
                remaining -= chunk
                yield data
            else:
                parts = []
                count = 0
                while remaining > 0 and count < COPY_BLOCKS:
                    take = min(BLOCK, remaining)
                    parts.append(self.read_block(cur)[:take])
                    cur += 1
                    remaining -= take
                    count += 1
                yield b"".join(parts)

    def _find_pvd(self):
        for i in range(16, 16 + 64):
            try:
                block = self.read_block(i)
            except InputError:
                break
            if block[1:6] != b"CD001":
                break
            vd_type = block[0]
            if vd_type == 1:
                return block
            if vd_type == 255:
                break
        raise InputError("%s has no ISO 9660 primary volume descriptor" % self.path)

    def _parse_pvd(self):
        p = self.pvd
        self.volume_id = p[40:72].decode("latin-1").strip()
        self.system_id = p[8:40].decode("latin-1").strip()
        self.volume_blocks = struct.unpack_from("<I", p, 80)[0]
        self.logical_block_size = struct.unpack_from("<H", p, 128)[0]
        if self.logical_block_size != BLOCK:
            raise InputError("unsupported logical block size %d (expected 2048)" % self.logical_block_size)
        root = parse_record(p, 156)
        if root is None or not root["is_dir"]:
            raise InputError("the root directory record is damaged")
        self.root = {"name": "", "path": "", "is_dir": True,
                     "extents": [(root["lba"], root["size"])], "size": root["size"]}

    def expected_size(self):
        return self.volume_blocks * self.sector_size

    # -------------------------------------------------------------- directories

    def list_dir(self, entry):
        """Return the children of a directory entry (files and folders)."""
        children = []
        lba, size = entry["extents"][0]
        if not self.extent_fits(lba, size):
            raise InputError("directory /%s points past the end of the image (truncated dump?)" % entry["path"])
        blocks = (size + BLOCK - 1) // BLOCK
        pending = None
        for b in range(blocks):
            block = self.read_block(lba + b)
            pos = 0
            while pos < BLOCK:
                rec_len = block[pos]
                if rec_len == 0:
                    break
                rec = parse_record(block, pos)
                if rec is None:
                    raise InputError("damaged directory record in /%s (sector %d)" % (entry["path"], lba + b))
                pos += rec_len
                if rec["raw_name"] in (b"\x00", b"\x01"):
                    continue
                name = clean_name(rec["raw_name"])
                if pending is not None and pending["name"] == name and not pending["is_dir"]:
                    pending["extents"].append((rec["lba"], rec["size"]))
                    pending["size"] += rec["size"]
                    if not rec["multi_extent"]:
                        pending = None
                    continue
                child = {
                    "name": name,
                    "raw_name": rec["raw_name"],
                    "path": (entry["path"] + "/" + name) if entry["path"] else name,
                    "is_dir": rec["is_dir"],
                    "extents": [(rec["lba"], rec["size"])],
                    "size": rec["size"],
                }
                children.append(child)
                pending = child if rec["multi_extent"] else None
                if len(children) > MAX_ENTRIES:
                    raise InputError("directory /%s has too many entries" % entry["path"])
        return children

    def walk(self, entry=None, depth=0, seen=None):
        """Yield every entry under `entry` (depth first), guarding against loops."""
        if entry is None:
            entry = self.root
        if seen is None:
            seen = set()
        if depth > MAX_DEPTH:
            raise InputError("directory tree deeper than %d levels (damaged image?)" % MAX_DEPTH)
        key = entry["extents"][0][0]
        if key in seen:
            return
        seen.add(key)
        for child in self.list_dir(entry):
            yield child
            if child["is_dir"]:
                for sub in self.walk(child, depth + 1, seen):
                    yield sub

    def find(self, path):
        """Find an entry by disc path. Accepts cdrom0:\\X;1, /X, X, case-insensitive."""
        parts = split_disc_path(path)
        cur = self.root
        for part in parts:
            if not cur["is_dir"]:
                return None
            kids = self.list_dir(cur)
            match = None
            for k in kids:
                if k["name"] == part:
                    match = k
                    break
            if match is None:
                low = part.lower()
                for k in kids:
                    if k["name"].lower() == low:
                        match = k
                        break
            if match is None:
                return None
            cur = match
        return cur


def parse_record(buf, pos):
    """Parse one ISO 9660 directory record at buf[pos]. None if malformed."""
    if pos + 33 > len(buf):
        return None
    rec_len = buf[pos]
    if rec_len < 34 or pos + rec_len > len(buf):
        return None
    name_len = buf[pos + 32]
    if name_len == 0 or 33 + name_len > rec_len:
        return None
    lba = struct.unpack_from("<I", buf, pos + 2)[0]
    size = struct.unpack_from("<I", buf, pos + 10)[0]
    flags = buf[pos + 25]
    return {
        "lba": lba,
        "size": size,
        "is_dir": bool(flags & 0x02),
        "multi_extent": bool(flags & 0x80),
        "raw_name": bytes(buf[pos + 33:pos + 33 + name_len]),
    }


def clean_name(raw):
    name = raw.decode("latin-1")
    semi = name.rfind(";")
    if semi != -1 and name[semi + 1:].isdigit():
        name = name[:semi]
    if name.endswith(".") and len(name) > 1:
        name = name[:-1]
    return name


def split_disc_path(path):
    p = path.strip()
    low = p.lower()
    for prefix in ("cdrom0:", "cdrom:"):
        if low.startswith(prefix):
            p = p[len(prefix):]
            break
    p = p.replace("\\", "/")
    parts = []
    for part in p.split("/"):
        if not part:
            continue
        part = clean_name(part.encode("latin-1", "replace"))
        parts.append(part)
    return parts


UNSAFE_CHARS = set('/\\:\x00')


def safe_component(name):
    if name in ("", ".", ".."):
        return False
    if any(c in UNSAFE_CHARS for c in name):
        return False
    if any(ord(c) < 0x20 for c in name):
        return False
    return True


# ---------------------------------------------------------------- SYSTEM.CNF

def parse_system_cnf(text):
    fields = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key.strip().upper()] = value.strip()
    return fields


def serial_from_elf_name(name):
    m = re.match(r"^([A-Za-z]{4})_(\d{3})\.(\d{2})$", name)
    if not m:
        return None
    return "%s-%s%s" % (m.group(1).upper(), m.group(2), m.group(3))


def boot_info_from_cnf(text):
    fields = parse_system_cnf(text)
    info = {
        "boot2": fields.get("BOOT2"),
        "ver": fields.get("VER"),
        "vmode": fields.get("VMODE"),
        "elf": None, "serial": None, "prefix": None, "region": None,
    }
    if not info["boot2"]:
        if fields.get("BOOT"):
            raise InputError("this is a PlayStation 1 disc (SYSTEM.CNF has BOOT, not BOOT2); "
                             "PS2Recomp ports PS2 games only")
        raise InputError("SYSTEM.CNF has no BOOT2 line")
    boot2 = info["boot2"]
    leaf = re.split(r"[\\/:]", boot2)[-1]
    elf = clean_name(leaf.encode("latin-1", "replace"))
    info["elf"] = elf
    serial = serial_from_elf_name(elf)
    info["serial"] = serial
    if serial:
        info["prefix"] = serial[:4]
        info["region"] = REGIONS.get(serial[:4], "unknown region prefix")
    return info


def find_in_dir_ci(directory, name):
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    if name in names:
        return os.path.join(directory, name)
    low = name.lower()
    for n in names:
        if n.lower() == low:
            return os.path.join(directory, n)
    return None


def boot_from_dir(directory):
    cnf = find_in_dir_ci(directory, "SYSTEM.CNF")
    if cnf is None or not os.path.isfile(cnf):
        raise InputError("no SYSTEM.CNF in %s: is this the extracted disc folder?" % directory)
    with open(cnf, "rb") as f:
        text = f.read(65536).decode("latin-1")
    info = boot_info_from_cnf(text)
    parts = split_disc_path(info["boot2"])
    cur = directory
    for part in parts:
        nxt = find_in_dir_ci(cur, part) if cur else None
        cur = nxt
    info["source"] = os.path.abspath(directory)
    if cur and os.path.isfile(cur):
        info["elf_found"] = True
        info["elf_path"] = os.path.abspath(cur)
        info["elf_size"] = os.path.getsize(cur)
    else:
        info["elf_found"] = False
        info["elf_path"] = None
        info["elf_size"] = None
    return info


def boot_from_iso(img):
    entry = img.find("SYSTEM.CNF")
    if entry is None or entry["is_dir"]:
        raise InputError("no SYSTEM.CNF on this disc: not a PS2 game disc, or not its data track")
    data = b"".join(img.iter_extent(entry["extents"][0][0], min(entry["size"], 65536)))
    info = boot_info_from_cnf(data.decode("latin-1"))
    elf = img.find(info["boot2"])
    info["source"] = os.path.abspath(img.path)
    if elf is not None and not elf["is_dir"]:
        info["elf_found"] = True
        info["elf_path"] = "/" + elf["path"]
        info["elf_size"] = elf["size"]
        info["elf_lba"] = elf["extents"][0][0]
    else:
        info["elf_found"] = False
        info["elf_path"] = None
        info["elf_size"] = None
    return info


# ---------------------------------------------------------------- commands

def entry_json(e):
    return {
        "path": "/" + e["path"],
        "name": e["name"],
        "type": "dir" if e["is_dir"] else "file",
        "size": None if e["is_dir"] else e["size"],
        "lba": e["extents"][0][0],
    }


def cmd_info(args):
    with IsoImage(args.image) as img:
        complete = img.image_size >= img.expected_size()
        info = {
            "image": os.path.abspath(img.path),
            "layout": img.layout,
            "volume_id": img.volume_id,
            "system_id": img.system_id,
            "volume_blocks": img.volume_blocks,
            "image_bytes": img.image_size,
            "expected_bytes": img.expected_size(),
            "complete": complete,
        }
        if args.json:
            print(json.dumps(info, indent=2))
        else:
            print("Image        %s" % info["image"])
            print("Layout       %s" % info["layout"])
            print("Volume       %s (system %s)" % (img.volume_id or "(no name)", img.system_id or "?"))
            print("Size         %d bytes, volume says %d blocks (%d bytes)"
                  % (img.image_size, img.volume_blocks, img.expected_size()))
            if complete:
                print("Dump         complete (the image is at least as big as the volume)")
            else:
                print("Dump         SHORT by %d bytes: re-dump the disc, some files will be missing"
                      % (img.expected_size() - img.image_size))
        return 0 if complete else 1


def cmd_ls(args):
    with IsoImage(args.image) as img:
        target = img.root if not args.path else img.find(args.path)
        if target is None:
            raise InputError("not found on the disc: %s" % args.path)
        if not target["is_dir"]:
            entries = [target]
        elif args.recursive:
            entries = list(img.walk(target))
        else:
            entries = img.list_dir(target)
        if args.json:
            print(json.dumps([entry_json(e) for e in entries], indent=2))
            return 0
        print("%s  (%s)" % (img.volume_id or "(no volume name)", img.layout))
        for e in entries:
            if e["is_dir"]:
                print("  dir  %12s  lba %-8d /%s/" % ("-", e["extents"][0][0], e["path"]))
            else:
                print("  file %12d  lba %-8d /%s" % (e["size"], e["extents"][0][0], e["path"]))
        files = [e for e in entries if not e["is_dir"]]
        print("%d entries, %d files, %d bytes" % (len(entries), len(files), sum(e["size"] for e in files)))
        return 0


def collect_targets(img, only):
    if not only:
        return list(img.walk())
    out = []
    seen = set()
    for p in only:
        e = img.find(p)
        if e is None:
            raise InputError("not found on the disc: %s" % p)
        group = [e] + (list(img.walk(e)) if e["is_dir"] else [])
        for g in group:
            if g["path"] not in seen:
                seen.add(g["path"])
                out.append(g)
        # include parent folders so the tree is preserved
        parts = e["path"].split("/")[:-1]
        for i in range(1, len(parts) + 1):
            parent = "/".join(parts[:i])
            if parent not in seen:
                seen.add(parent)
                out.append({"name": parts[i - 1], "path": parent, "is_dir": True,
                            "extents": [(0, 0)], "size": 0})
    return out


def cmd_extract(args):
    out_root = os.path.abspath(args.out)
    with IsoImage(args.image) as img:
        targets = collect_targets(img, args.only)
        # 1. validate every name and every extent before writing anything
        for e in targets:
            for comp in e["path"].split("/"):
                if not safe_component(comp):
                    raise InputError("refusing unsafe path on the disc: %r (nothing written)" % e["path"])
            if not e["is_dir"]:
                for lba, size in e["extents"]:
                    if not img.extent_fits(lba, size):
                        raise InputError("/%s points past the end of the image: the dump is truncated "
                                         "(nothing written; re-dump the disc)" % e["path"])
        try:
            os.makedirs(out_root, exist_ok=True)
        except OSError as e:
            raise InputError("cannot create %s: %s" % (out_root, e.strerror or e))
        real_root = os.path.realpath(out_root)
        written = kept = 0
        total = 0
        for e in sorted(targets, key=lambda t: (t["path"].count("/"), t["path"])):
            dest = os.path.join(out_root, *e["path"].split("/"))
            real_parent = os.path.realpath(os.path.dirname(dest))
            if real_parent != real_root and not real_parent.startswith(real_root + os.sep):
                raise InputError("refusing to write outside %s: %s" % (out_root, e["path"]))
            if e["is_dir"]:
                os.makedirs(dest, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.lexists(dest) and not args.force:
                kept += 1
                continue
            tmp = dest + ".part"
            with open(tmp, "wb") as f:
                for lba, size in e["extents"]:
                    for chunk in img.iter_extent(lba, size):
                        f.write(chunk)
            os.replace(tmp, dest)
            written += 1
            total += e["size"]
        boot = None
        try:
            boot = boot_from_dir(out_root)
        except InputError:
            boot = None
        result = {"out": out_root, "written": written, "kept_existing": kept, "bytes": total,
                  "boot_elf": boot["elf_path"] if boot else None,
                  "serial": boot["serial"] if boot else None}
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("Extracted %d files (%d bytes) into %s" % (written, total, out_root))
            if kept:
                print("Kept %d files that already existed (your edits stay; pass --force to overwrite them)" % kept)
            if boot and boot["elf_found"]:
                print("Boot ELF     %s  (%s)" % (boot["elf_path"], boot["serial"] or "no serial in the name"))
                print("Run the port with that path as argv[1]: the runtime reads cdrom0: from this folder.")
        return 0


def cmd_plain(args):
    with IsoImage(args.image) as img:
        if img.sector_size == BLOCK:
            print("%s already has 2048-byte sectors: use it as it is." % img.path)
            return 0
        out = os.path.abspath(args.out)
        if os.path.exists(out) and not args.force:
            sys.stderr.write("iso.py: %s already exists (pass --force to replace it)\n" % args.out)
            return 1
        if os.path.abspath(img.path) == out:
            raise InputError("--out must be a new file, not the source image")
        total = img.image_size // img.sector_size
        tmp = out + ".part"
        try:
            with open(tmp, "wb") as f:
                lba = 0
                while lba < total:
                    n = min(COPY_BLOCKS, total - lba)
                    f.write(b"".join(img.read_block(lba + i) for i in range(n)))
                    lba += n
            os.replace(tmp, out)
        except OSError as e:
            raise InputError("cannot write %s: %s" % (args.out, e.strerror or e))
        print("Wrote %s: %d sectors of 2048 bytes (%d bytes)" % (args.out, total, total * BLOCK))
        print("It is a copy of your own disc: keep it on this machine.")
        return 0


def cmd_boot(args):
    src = args.source
    if os.path.isdir(src):
        info = boot_from_dir(src)
    elif os.path.isfile(src):
        with IsoImage(src) as img:
            info = boot_from_iso(img)
    else:
        raise InputError("no such file or folder: %s" % src)
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        print("BOOT2        %s" % info["boot2"])
        print("Boot ELF     %s" % info["elf"])
        print("Serial       %s" % (info["serial"] or "(the ELF name is not a serial)"))
        print("Region       %s" % (info["region"] or "unknown"))
        print("VER          %s" % (info["ver"] or "-"))
        print("VMODE        %s" % (info["vmode"] or "-"))
        if info["elf_found"]:
            print("Found        %s (%d bytes)" % (info["elf_path"], info["elf_size"]))
        else:
            print("Found        NO: the boot ELF named in SYSTEM.CNF is not there")
    return 0 if info["elf_found"] else 1


def build_parser():
    p = argparse.ArgumentParser(
        prog="iso.py",
        description="Read a PS2 disc image you dumped yourself (ISO 9660). Streams; never loads the whole disc.")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")
    sub.required = True

    s = sub.add_parser("info", help="volume name, sector layout, and whether the dump is complete")
    s.add_argument("image", help="disc image (.iso, or raw 2352-byte .bin)")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("ls", help="list a directory on the disc")
    s.add_argument("image", help="disc image")
    s.add_argument("path", nargs="?", default="", help="folder or file on the disc (default: the root)")
    s.add_argument("-r", "--recursive", action="store_true", help="list everything below the folder")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("extract", help="copy the disc (or parts of it) into a folder, keeping the tree")
    s.add_argument("image", help="disc image")
    s.add_argument("--out", required=True, help="destination folder (convention: console/disc)")
    s.add_argument("--only", nargs="+", metavar="PATH", help="only these files or folders (disc paths)")
    s.add_argument("--force", action="store_true",
                   help="overwrite files that already exist (default keeps them, so your edits survive)")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_extract)

    s = sub.add_parser("plain", help="convert a raw 2352-byte .bin into a plain 2048-byte-sector .iso")
    s.add_argument("image", help="raw disc image (.bin, Mode 1 or Mode 2 Form 1)")
    s.add_argument("--out", required=True, help="the new .iso (refuses to overwrite)")
    s.add_argument("--force", action="store_true", help="replace an existing --out file")
    s.set_defaults(func=cmd_plain)

    s = sub.add_parser("boot", help="read SYSTEM.CNF: boot ELF, serial, region, VER, VMODE")
    s.add_argument("source", help="disc image, or the extracted disc folder")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_boot)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except InputError as e:
        sys.stderr.write("iso.py: %s\n" % e)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
