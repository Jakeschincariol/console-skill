#!/usr/bin/env python3
"""Read a PS2 boot ELF (ELF32, little-endian, MIPS) and start a PS2Recomp config.

Subcommands:
  info    type, machine, entry, PT_LOAD segments, sections, symbols, both CRCs,
          serial (from the file name) and the IRX modules / disc paths it references
  crc     just the two CRCs and the pnach file name PCSX2 looks for
  toml    write a starter PS2Recomp config with only keys the recompiler reads
  serial  convert SLUS_203.12 <-> SLUS-20312 and name the region

Pure Python standard library. Exit codes: 0 ok, 1 something needs attention,
2 bad input (not an ELF, wrong machine, truncated) with a one-line message.
"""

import argparse
import json
import os
import re
import struct
import sys
import zlib
from array import array

MAX_ELF_BYTES = 256 * 1024 * 1024
EM_MIPS = 8
ET_NAMES = {0: "NONE", 1: "REL (relocatable)", 2: "EXEC (executable)", 3: "DYN (shared object)",
            4: "CORE", 0xFF80: "SCE IOP relocatable (IRX module)"}
PT_LOAD = 1
SHT_SYMTAB = 2
SHT_NOBITS = 8

REGIONS = {
    "SLUS": "USA", "SCUS": "USA",
    "SLES": "Europe", "SCES": "Europe",
    "SLPS": "Japan", "SLPM": "Japan", "SCPS": "Japan",
    "SLKA": "Korea", "SCKA": "Korea",
    "SCAJ": "Asia",
}

TOOL_TAG = "console-reverse elf.py"


class InputError(Exception):
    """Bad input: exit 2 with a one-line message."""


# ---------------------------------------------------------------- parsing

def read_file(path):
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise InputError("cannot open %s: %s" % (path, e.strerror or e))
    if os.path.isdir(path):
        raise InputError("%s is a folder, not an ELF file" % path)
    if size > MAX_ELF_BYTES:
        raise InputError("%s is %d bytes: too big to be a PS2 program" % (path, size))
    with open(path, "rb") as f:
        return f.read()


def parse_elf(data, path="the file"):
    if len(data) < 52:
        raise InputError("%s is too short to be an ELF (%d bytes)" % (path, len(data)))
    if data[:4] != b"\x7fELF":
        raise InputError("%s is not an ELF file (no 0x7F 'ELF' magic)" % path)
    if data[4] != 1:
        raise InputError("%s is a 64-bit ELF; PS2 programs are ELF32" % path)
    if data[5] != 1:
        raise InputError("%s is big-endian; PS2 programs are little-endian" % path)
    (e_type, e_machine, e_version, e_entry, e_phoff, e_shoff, e_flags, e_ehsize,
     e_phentsize, e_phnum, e_shentsize, e_shnum, e_shstrndx) = struct.unpack_from("<HHIIIIIHHHHHH", data, 16)
    if e_machine != EM_MIPS:
        raise InputError("%s is not a MIPS program (machine %d, expected 8)" % (path, e_machine))
    elf = {
        "type": e_type, "type_name": ET_NAMES.get(e_type, "0x%04X" % e_type),
        "machine": e_machine, "entry": e_entry, "flags": e_flags,
        "segments": [], "sections": [], "symbols": 0, "has_symtab": False,
        "has_dwarf": False, "is_irx": e_type == 0xFF80, "warnings": [],
    }
    # program headers
    if e_phnum:
        if e_phentsize < 32:
            raise InputError("%s has a bad program header size (%d)" % (path, e_phentsize))
        end = e_phoff + e_phnum * e_phentsize
        if e_phoff == 0 or end > len(data):
            raise InputError("%s is truncated: program headers run past the end of the file" % path)
        for i in range(e_phnum):
            off = e_phoff + i * e_phentsize
            p_type, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_flags, p_align = \
                struct.unpack_from("<IIIIIIII", data, off)
            seg = {"index": i, "type": p_type, "offset": p_offset, "vaddr": p_vaddr, "paddr": p_paddr,
                   "filesz": p_filesz, "memsz": p_memsz, "flags": p_flags, "align": p_align}
            if p_type == PT_LOAD:
                if p_offset + p_filesz > len(data):
                    raise InputError("%s is truncated: segment %d needs bytes up to 0x%X, the file has 0x%X"
                                     % (path, i, p_offset + p_filesz, len(data)))
                if p_memsz < p_filesz:
                    elf["warnings"].append("segment %d has memsz < filesz" % i)
            elf["segments"].append(seg)
    # section headers (optional: many retail ELFs keep them, some do not)
    if e_shoff and e_shnum:
        if e_shentsize < 40:
            elf["warnings"].append("bad section header size %d: sections ignored" % e_shentsize)
        elif e_shoff + e_shnum * e_shentsize > len(data):
            elf["warnings"].append("section headers run past the end of the file: sections ignored")
        else:
            raw = []
            for i in range(e_shnum):
                off = e_shoff + i * e_shentsize
                vals = struct.unpack_from("<IIIIIIIIII", data, off)
                raw.append(vals)
            names_blob = b""
            if e_shstrndx < len(raw):
                s = raw[e_shstrndx]
                if s[4] + s[5] <= len(data):
                    names_blob = data[s[4]:s[4] + s[5]]
            for i, (sh_name, sh_type, sh_flags, sh_addr, sh_offset, sh_size,
                    sh_link, sh_info, sh_addralign, sh_entsize) in enumerate(raw):
                name = ""
                if names_blob and sh_name < len(names_blob):
                    end = names_blob.find(b"\x00", sh_name)
                    name = names_blob[sh_name:end if end != -1 else len(names_blob)].decode("latin-1")
                sec = {"index": i, "name": name, "type": sh_type, "flags": sh_flags, "addr": sh_addr,
                       "offset": sh_offset, "size": sh_size, "entsize": sh_entsize}
                elf["sections"].append(sec)
                if sh_type == SHT_SYMTAB:
                    elf["has_symtab"] = True
                    ent = sh_entsize or 16
                    elf["symbols"] += max(0, sh_size // ent - 1)
                if name.startswith(".debug") or name == ".mdebug":
                    elf["has_dwarf"] = True
                if name == ".iopmod":
                    elf["is_irx"] = True
    return elf


def seg_flags(f):
    return ("R" if f & 4 else "-") + ("W" if f & 2 else "-") + ("X" if f & 1 else "-")


def crc32_std(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def crc_pnach(data):
    """PCSX2's game CRC: XOR of every little-endian 32-bit word (trailing bytes ignored)."""
    usable = len(data) - (len(data) % 4)
    code = "I" if array("I").itemsize == 4 else "L"
    words = array(code)
    words.frombytes(data[:usable])
    if sys.byteorder == "big":
        words.byteswap()
    acc = 0
    for w in words:
        acc ^= w
    return acc & 0xFFFFFFFF


SERIAL_FILE_RE = re.compile(r"^([A-Za-z]{4})_(\d{3})\.(\d{2})(?:;\d+)?$")
SERIAL_ID_RE = re.compile(r"^([A-Za-z]{4})[-_ ]?(\d{5})$")


def parse_serial(text):
    """Return (serial 'SLUS-20312', file name 'SLUS_203.12', prefix) or None."""
    base = re.split(r"[\\/:]", text.strip())[-1]
    m = SERIAL_FILE_RE.match(base)
    if m:
        pre = m.group(1).upper()
        return "%s-%s%s" % (pre, m.group(2), m.group(3)), "%s_%s.%s" % (pre, m.group(2), m.group(3)), pre
    m = SERIAL_ID_RE.match(base)
    if m:
        pre = m.group(1).upper()
        d = m.group(2)
        return "%s-%s" % (pre, d), "%s_%s.%s" % (pre, d[:3], d[3:]), pre
    return None


DEVICE_PATH_RE = re.compile(rb"(?:cdrom0?|rom0|host0?|mc0):[\\/]?[A-Za-z0-9_\\/.\-;]{1,120}", re.I)
BARE_IRX_RE = re.compile(rb"(?<![A-Za-z0-9_\\/.\-:])[A-Za-z0-9_\-]{1,40}\.IRX(?:;\d)?", re.I)


def find_references(data):
    """IRX modules and disc paths named in the program's strings."""
    irx, other = [], []
    seen = set()
    for m in DEVICE_PATH_RE.finditer(data):
        s = m.group(0).decode("latin-1").rstrip(".")
        if s in seen:
            continue
        seen.add(s)
        low = s.lower()
        if ".irx" in low or low.startswith("rom0:"):
            irx.append(s)
        else:
            other.append(s)
    for m in BARE_IRX_RE.finditer(data):
        s = m.group(0).decode("latin-1")
        if s in seen or any(s.lower() in x.lower() for x in irx):
            continue
        seen.add(s)
        irx.append(s)
    return irx, other


def describe(path, data, elf):
    info = {
        "file": os.path.abspath(path),
        "name": os.path.basename(path),
        "bytes": len(data),
        "type": elf["type_name"],
        "machine": "MIPS (%d)" % elf["machine"],
        "entry": "0x%08X" % elf["entry"],
        "flags": "0x%08X" % elf["flags"],
        "is_irx": elf["is_irx"],
        "segments": [],
        "sections": [s["name"] for s in elf["sections"] if s["name"]],
        "section_count": len(elf["sections"]),
        "symbols": elf["symbols"],
        "stripped": not elf["has_symtab"],
        "debug_info": elf["has_dwarf"],
        "crc32": "0x%08X" % crc32_std(data),
        "pnach_crc": "0x%08X" % crc_pnach(data),
        "serial": None, "region": None, "pnach_file": None,
        "warnings": list(elf["warnings"]),
    }
    for s in elf["segments"]:
        if s["type"] != PT_LOAD:
            continue
        info["segments"].append({
            "vaddr": "0x%08X" % s["vaddr"], "end": "0x%08X" % (s["vaddr"] + s["memsz"]),
            "filesz": s["filesz"], "memsz": s["memsz"], "bss": max(0, s["memsz"] - s["filesz"]),
            "offset": "0x%X" % s["offset"], "flags": seg_flags(s["flags"]),
        })
        if (s["vaddr"] & 0x1FFFFFFF) + s["memsz"] > 0x02000000:
            info["warnings"].append("segment at 0x%08X runs past the 32 MB of RDRAM" % s["vaddr"])
    ser = parse_serial(os.path.basename(path))
    pn = info["pnach_crc"][2:]
    if ser:
        info["serial"] = ser[0]
        info["region"] = REGIONS.get(ser[2], "unknown region prefix")
        info["pnach_file"] = "%s_%s.pnach" % (ser[0], pn)
    else:
        info["pnach_file"] = "<SERIAL>_%s.pnach" % pn
    irx, other = find_references(data)
    info["irx_modules"] = irx
    info["disc_paths"] = other
    return info


# ---------------------------------------------------------------- commands

def load(path):
    data = read_file(path)
    return data, parse_elf(data, os.path.basename(path))


def cmd_info(args):
    data, elf = load(args.elf)
    info = describe(args.elf, data, elf)
    if args.json:
        print(json.dumps(info, indent=2))
        return 0
    print("File         %s (%d bytes)" % (info["name"], info["bytes"]))
    if info["serial"]:
        print("Serial       %s (%s)" % (info["serial"], info["region"]))
    print("Type         %s, %s, ELF32 little-endian" % (info["type"], info["machine"]))
    if info["is_irx"]:
        print("             This is an IOP module (IRX), not the game's main program. ps2xIOP runs it.")
    print("Entry        %s" % info["entry"])
    print("Segments     %d PT_LOAD" % len(info["segments"]))
    for s in info["segments"]:
        extra = ("  (+0x%X zero-filled bss)" % s["bss"]) if s["bss"] else ""
        print("  %s  %s-%s  file 0x%X bytes at %s%s"
              % (s["flags"], s["vaddr"], s["end"], s["filesz"], s["offset"], extra))
    if info["section_count"]:
        names = " ".join(info["sections"][:16])
        more = " ..." if len(info["sections"]) > 16 else ""
        print("Sections     %d: %s%s" % (info["section_count"], names, more))
    else:
        print("Sections     none (no section headers)")
    if info["stripped"]:
        print("Symbols      none: stripped. Use the Ghidra export (ExportPS2Functions.java) for function bounds")
    else:
        print("Symbols      %d in .symtab" % info["symbols"])
    if info["debug_info"]:
        print("Debug info   yes: ps2_analyzer can read function names and bounds from it")
    print("CRC-32       %s  (PS2_REGISTER_GAME_OVERRIDE key; standard CRC-32 of the whole file)" % info["crc32"])
    print("PCSX2 CRC    %s  (pnach file: %s)" % (info["pnach_crc"], info["pnach_file"]))
    if info["irx_modules"]:
        print("IRX modules  %d referenced:" % len(info["irx_modules"]))
        for m in info["irx_modules"][:40]:
            print("  %s" % m)
    else:
        print("IRX modules  none named in the strings")
    if info["disc_paths"]:
        print("Disc paths   %d other device paths (first 20):" % len(info["disc_paths"]))
        for m in info["disc_paths"][:20]:
            print("  %s" % m)
    for w in info["warnings"]:
        print("Warning      %s" % w)
    return 0


def cmd_crc(args):
    data, elf = load(args.elf)
    info = describe(args.elf, data, elf)
    out = {"crc32": info["crc32"], "pnach_crc": info["pnach_crc"], "pnach_file": info["pnach_file"],
           "entry": info["entry"], "elf_name": info["name"]}
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print("CRC-32     %s  (runtime override key)" % out["crc32"])
        print("PCSX2 CRC  %s  (%s)" % (out["pnach_crc"], out["pnach_file"]))
        print("Entry      %s   ELF name %s" % (out["entry"], out["elf_name"]))
    return 0


def toml_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_toml(elf_path, info, ghidra_csv, output_dir, csv_rows):
    serial = info["serial"] or info["name"]
    lines = [
        "# PS2Recomp starter config for %s, written by %s." % (serial, TOOL_TAG),
        "# Every key below is one ps2xRecomp/src/lib/config_manager.cpp reads (PS2Recomp, Oct 2026).",
        "# Prefer the TOML that ps2xRecomp/tools/ghidra/ExportPS2Functions.java writes from Ghidra:",
        "# it fills `stubs` with the runtime handlers it recognises. Merge your own lines into it.",
        "# Run: ps2_recomp <this file>   (the executable prints its usage as `ps2recomp`)",
        "# ELF: entry %s, CRC-32 %s, %d PT_LOAD segments, %s."
        % (info["entry"], info["crc32"], len(info["segments"]),
           "stripped" if info["stripped"] else "%d symbols" % info["symbols"]),
        "",
        "[general]",
        "input = %s" % toml_str(os.path.abspath(elf_path)),
        "# Generated C++ lands here. Copy the .cpp files into ps2xRuntime/src/runner/ and the two",
        "# generated headers (ps2_recompiled_functions.h, ps2_recompiled_stubs.h) into ps2xRuntime/include/.",
        "# It is derived from the game's code: never publish it.",
        "output = %s" % toml_str(os.path.abspath(output_dir)),
    ]
    if ghidra_csv:
        lines.append("# Ghidra function map (Name,Start,End,Size): %d functions. Its bounds win over heuristics."
                     % csv_rows)
        lines.append("ghidra_output = %s" % toml_str(os.path.abspath(ghidra_csv)))
    else:
        lines.append("# Recommended for stripped retail ELFs: the CSV ExportPS2Functions.java writes.")
        lines.append('# ghidra_output = "/path/to/functions.csv"')
    lines += [
        "single_file_output = false   # one .cpp per function keeps rebuilds incremental",
        "low_memory_mode = false",
        "output_worker_threads = 0    # 0 = one less than your CPU threads",
        "patch_syscalls = false       # leave false: SYSCALL patches only apply when this is true",
        "patch_cop0 = true            # recompiler default; COP0 patches apply only when true",
        "patch_cache = true           # recompiler default; CACHE patches apply only when true",
        "",
        "# Bind a function start to a runtime handler by name: \"sceCdRead@0x00123456\".",
        "# The name must exist in PS2_SYSCALL_LIST or PS2_STUB_LIST (ps2xRuntime/include/ps2_call_list.h).",
        "# Temporary triage handlers: \"ret0@0x...\", \"ret1@0x...\", \"reta0@0x...\" (record each in STATE.md).",
        "stubs = [",
        "]",
        "",
        "# Extra function starts the recompiler should emit, for example \"0x00123456\" after the runtime",
        "# logs `Error: No exact recompiled function for guest PC 0x00123456 ... codeRegion=yes`.",
        "entry_points = [",
        "]",
        "",
        "# Legacy. A skipped function is NOT stepped over: calling it logs",
        "# `Unimplemented PS2 stub called` and stops the run. Use ret0@/ret1@ stubs instead.",
        "skip = []",
        "",
        "# Instruction patches: the raw 32-bit replacement word at an address, applied before translation.",
        "# Plain patches always apply; SYSCALL/COP0/CACHE patches only when patch_syscalls/patch_cop0/patch_cache",
        "# are true. Example (a MIPS nop):",
        "# [[patches.instructions]]",
        '# address = "0x00100230"',
        '# value = "0x00000000"',
        "",
    ]
    return "\n".join(lines)


def count_csv(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            header = f.readline().strip()
            rows = sum(1 for line in f if line.strip())
    except OSError as e:
        raise InputError("cannot read the Ghidra CSV %s: %s" % (path, e.strerror or e))
    if header.replace(" ", "").lower() != "name,start,end,size":
        raise InputError("%s does not look like an ExportPS2Functions.java map (header %r, expected "
                         "'Name,Start,End,Size')" % (path, header[:40]))
    return rows


def cmd_toml(args):
    data, elf = load(args.elf)
    if elf["is_irx"]:
        raise InputError("%s is an IRX module; recompile the EE boot ELF instead" % args.elf)
    if elf["type"] != 2:
        raise InputError("%s is not an executable ELF (type %s)" % (args.elf, elf["type_name"]))
    info = describe(args.elf, data, elf)
    out = args.out
    if out is None:
        stem = info["serial"] or os.path.basename(args.elf).replace(".", "_")
        out = os.path.join("console", "%s.toml" % stem)
    if os.path.exists(out) and not args.force:
        sys.stderr.write("elf.py: %s already exists; not overwriting your edits (pass --force to replace it)\n" % out)
        return 1
    rows = 0
    if args.ghidra_csv:
        if not os.path.isfile(args.ghidra_csv):
            raise InputError("no such Ghidra CSV: %s" % args.ghidra_csv)
        rows = count_csv(args.ghidra_csv)
    output_dir = args.output_dir or os.path.join(os.path.dirname(os.path.abspath(out)), "output")
    text = render_toml(args.elf, info, args.ghidra_csv, output_dir, rows)
    parent = os.path.dirname(os.path.abspath(out))
    try:
        os.makedirs(parent, exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except OSError as e:
        raise InputError("cannot write %s: %s" % (out, e.strerror or e))
    if args.json:
        print(json.dumps({"toml": os.path.abspath(out), "output": os.path.abspath(output_dir),
                          "ghidra_rows": rows, "entry": info["entry"], "crc32": info["crc32"]}, indent=2))
    else:
        print("Wrote %s" % out)
        print("Generated C++ will go to %s" % os.path.abspath(output_dir))
        if not args.ghidra_csv and info["stripped"]:
            print("Stripped ELF: export functions from Ghidra (ExportPS2Functions.java) and pass --ghidra-csv,")
            print("or use the TOML the export writes. ps2_analyzer is the quick fallback.")
        print("Next: ps2_recomp %s" % out)
    return 0


def cmd_serial(args):
    ser = parse_serial(args.name)
    if ser is None:
        raise InputError("%r is not a PS2 serial (expected SLUS-20312, SLUS_203.12 or SLUS20312)" % args.name)
    region = REGIONS.get(ser[2], "unknown region prefix")
    if args.json:
        print(json.dumps({"serial": ser[0], "elf_name": ser[1], "prefix": ser[2], "region": region}, indent=2))
    else:
        print("Serial     %s" % ser[0])
        print("ELF name   %s" % ser[1])
        print("Region     %s" % region)
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="elf.py",
        description="Read a PS2 boot ELF (ELF32 LE MIPS): segments, CRCs, IRX modules; write a starter PS2Recomp TOML.")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")
    sub.required = True

    s = sub.add_parser("info", help="segments, sections, symbols, CRC-32 and PCSX2 CRC, IRX modules")
    s.add_argument("elf", help="the boot ELF, e.g. console/disc/SLUS_203.12")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("crc", help="CRC-32 (runtime override key) and PCSX2 pnach CRC")
    s.add_argument("elf", help="the boot ELF")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_crc)

    s = sub.add_parser("toml", help="write a starter PS2Recomp config (refuses to overwrite)")
    s.add_argument("elf", help="the boot ELF")
    s.add_argument("--out", help="config path (default console/<serial>.toml)")
    s.add_argument("--ghidra-csv", metavar="PATH", help="function map from ExportPS2Functions.java")
    s.add_argument("--output-dir", metavar="DIR",
                   help="where ps2_recomp writes C++ (default: an output/ folder next to the TOML)")
    s.add_argument("--force", action="store_true", help="overwrite an existing config")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_toml)

    s = sub.add_parser("serial", help="convert SLUS_203.12 <-> SLUS-20312 and name the region")
    s.add_argument("name", help="a serial or boot ELF name (a path is fine)")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_serial)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except InputError as e:
        sys.stderr.write("elf.py: %s\n" % e)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
