#!/usr/bin/env python3
"""pnach.py: port PCSX2 patches (.pnach) to a PS2Recomp port.

Reads a PCSX2 patch file, sorts every write by where it lands in YOUR ELF and
turns each kind into the right PS2Recomp fix:

  CODE     inside the game's code: an instruction patch ([[patches.instructions]]
           in the ps2_recomp TOML), then re-run ps2_recomp
  DATA     inside data the ELF loads: written into RDRAM by a game override at load
  BSS/HEAP zero-filled or beyond the ELF: the game rewrites it after boot, so the
           override re-applies it from a per-frame hook (--frame-hook)
  OUTSIDE  not main RAM (scratchpad, hardware registers) or EE kernel RAM: manual port
  CAVE     new code written into memory (jumps into patched areas): manual port

Subcommands: parse, classify, toml, override, crc. Python 3.8+, standard library only.
"""

import argparse
import datetime
import importlib.util
import json
import os
import re
import struct
import sys
import zlib

TOOL = "pnach.py"

# --------------------------------------------------------------------------
# Errors and small helpers
# --------------------------------------------------------------------------


class UsageError(Exception):
    """Bad input the user can fix: printed as one line, exit code 2."""


def fail(msg):
    raise UsageError(msg)


def hex32(v):
    return "0x%08X" % (v & 0xFFFFFFFF)


def parse_hex(text, what):
    t = text.strip()
    if t.lower().startswith("0x"):
        t = t[2:]
    if not t or not re.fullmatch(r"[0-9A-Fa-f]+", t):
        fail("%s is not a hex number: %r" % (what, text))
    return int(t, 16)


def parse_addr_arg(text, what="address"):
    try:
        v = int(text, 0) if text.lower().startswith("0x") else int(text, 16)
    except ValueError:
        fail("%s is not a hex number: %r" % (what, text))
    if not 0 <= v <= 0xFFFFFFFF:
        fail("%s is out of range: %r" % (what, text))
    return v


def read_file(path, what="file"):
    if not os.path.isfile(path):
        fail("%s not found: %s" % (what, path))
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        fail("cannot read %s %s: %s" % (what, path, e.strerror))


# --------------------------------------------------------------------------
# PS2 address space
# --------------------------------------------------------------------------

RAM_SIZE = 32 * 1024 * 1024
RAM_MASK = RAM_SIZE - 1
KERNEL_END = 0x00080000      # the EE kernel's RAM on a real PS2; user programs load above it


def ram_offset(addr):
    """Main RAM offset for an EE address (same rules as PS2Memory::translateAddress), or None."""
    addr &= 0xFFFFFFFF
    if addr < RAM_SIZE:
        return addr
    if 0x20000000 <= addr < 0x40000000:      # uncached and accelerated mirrors
        return addr & RAM_MASK
    if 0x80000000 <= addr < 0xC0000000:      # KSEG0 / KSEG1
        p = addr & 0x1FFFFFFF
        return p if p < RAM_SIZE else None
    return None


def outside_reason(addr):
    if 0x70000000 <= addr < 0x70004000:
        return "scratchpad (16 KB at 0x70000000)"
    p = addr & 0x1FFFFFFF if addr >= 0x80000000 else addr
    if 0x10000000 <= p < 0x10010000:
        return "EE hardware registers"
    if 0x11000000 <= p < 0x11010000:
        return "VU0/VU1 memory"
    if 0x12000000 <= p < 0x12002000:
        return "GS privileged registers"
    if 0x1FC00000 <= p < 0x20000000:
        return "BIOS ROM"
    return "not main RAM"


# --------------------------------------------------------------------------
# ELF32 little-endian MIPS reader (self-contained on purpose)
# --------------------------------------------------------------------------

PT_LOAD = 1
PF_X, PF_W, PF_R = 1, 2, 4
SHF_WRITE, SHF_ALLOC, SHF_EXECINSTR = 1, 2, 4
SHT_NOBITS = 8


class Elf(object):
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        data = read_file(path, "ELF")
        self.data = data
        if len(data) < 52:
            fail("%s is too short to be an ELF (%d bytes)" % (self.name, len(data)))
        if data[:4] != b"\x7fELF":
            fail("%s is not an ELF file (bad magic)" % self.name)
        if data[4] != 1:
            fail("%s is not a 32-bit ELF (PS2 game ELFs are ELF32)" % self.name)
        if data[5] != 1:
            fail("%s is not little-endian (PS2 EE ELFs are little-endian)" % self.name)
        (self.e_type, self.machine, _ver, self.entry, phoff, shoff, _flags, _ehsize,
         phentsize, phnum, shentsize, shnum, shstrndx) = struct.unpack_from("<HHIIIIIHHHHHH", data, 16)
        if self.machine != 8:
            fail("%s is not a MIPS ELF (machine %d, the PS2 EE is 8)" % (self.name, self.machine))
        if phnum and phentsize < 32:
            fail("%s has a bad program header size (%d)" % (self.name, phentsize))
        if phoff + phnum * phentsize > len(data):
            fail("%s is truncated: program headers run past the end of the file" % self.name)
        self.segments = []
        for i in range(phnum):
            off = phoff + i * phentsize
            p_type, p_off, p_vaddr, _pa, p_filesz, p_memsz, p_flags, _al = struct.unpack_from("<IIIIIIII", data, off)
            if p_type != PT_LOAD or p_memsz == 0:
                continue
            if p_filesz > p_memsz:
                fail("%s segment %d has filesz > memsz" % (self.name, i))
            if p_off + p_filesz > len(data):
                fail("%s is truncated: segment %d runs past the end of the file" % (self.name, i))
            self.segments.append({"index": i, "offset": p_off, "vaddr": p_vaddr, "filesz": p_filesz,
                                  "memsz": p_memsz, "flags": p_flags, "ram": ram_offset(p_vaddr)})
        if not self.segments:
            fail("%s has no loadable (PT_LOAD) segments" % self.name)
        self.sections = []
        if shnum and shoff and shentsize >= 40 and shoff + shnum * shentsize <= len(data):
            raw = []
            for i in range(shnum):
                raw.append(struct.unpack_from("<IIIIIIIIII", data, shoff + i * shentsize))
            strtab = b""
            if shstrndx < len(raw):
                s = raw[shstrndx]
                if s[4] + s[5] <= len(data):
                    strtab = data[s[4]:s[4] + s[5]]
            for (sh_name, sh_type, sh_flags, sh_addr, sh_off, sh_size, _l, _i, _a, _e) in raw:
                if not (sh_flags & SHF_ALLOC) or sh_size == 0:
                    continue
                name = ""
                if sh_name < len(strtab):
                    end = strtab.find(b"\0", sh_name)
                    name = strtab[sh_name:end if end >= 0 else len(strtab)].decode("ascii", "replace")
                self.sections.append({"name": name, "type": sh_type, "flags": sh_flags, "addr": sh_addr,
                                      "size": sh_size, "ram": ram_offset(sh_addr)})

    def pcsx2_crc(self):
        """PCSX2's game CRC: XOR of every little-endian 32-bit word of the ELF (trailing bytes ignored)."""
        d = self.data
        n = len(d) // 4
        crc = 0
        for (w,) in struct.iter_unpack("<I", d[:n * 4]):
            crc ^= w
        return crc

    def crc32(self):
        """Standard CRC-32 of the whole file: the key PS2Recomp game overrides match on."""
        return zlib.crc32(self.data) & 0xFFFFFFFF

    def locate(self, ram):
        """(segment, file_backed) for a RAM offset, or (None, False)."""
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

    def read_ram(self, ram, size):
        seg, backed = self.locate(ram)
        if seg is None or not backed or ram + size > seg["ram"] + seg["filesz"]:
            return None
        off = seg["offset"] + (ram - seg["ram"])
        return self.data[off:off + size]

    def vaddr_of(self, ram):
        """The address as the ELF (and ps2_recomp) spell it, for a RAM offset."""
        seg, _ = self.locate(ram)
        if seg is None:
            return ram
        return (seg["vaddr"] + (ram - seg["ram"])) & 0xFFFFFFFF

    def is_code(self, ram, funcs=None):
        """CODE test. Returns (is_code, confidence, why)."""
        seg, backed = self.locate(ram)
        if seg is None or not backed:
            return False, "high", ""
        sec = self.section_at(ram)
        if sec is not None:
            if sec["flags"] & SHF_EXECINSTR:
                return True, "high", "section %s is executable" % (sec["name"] or "?")
            return False, "high", "section %s is data" % (sec["name"] or "?")
        if funcs is not None:
            if funcs.covers(self.vaddr_of(ram)):
                return True, "high", "inside a function in the Ghidra map"
            return False, "medium", "no function in the Ghidra map covers it"
        if seg["flags"] & PF_X:
            if seg["flags"] & PF_W:
                return True, "low", "segment %d is read-write-execute (code and data mixed, no sections)" % seg["index"]
            return True, "medium", "segment %d is executable" % seg["index"]
        return False, "high", ""


# --------------------------------------------------------------------------
# Ghidra CSV map (ExportPS2Functions.java: Name,Start,End,Size)
# --------------------------------------------------------------------------


class FuncMap(object):
    def __init__(self, path):
        text = read_file(path, "Ghidra map").decode("utf-8", "replace")
        self.ranges = []
        for n, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line or line.lower().startswith("name,"):
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                fail("Ghidra map line %d is not Name,Start,End,Size: %r" % (n, line))
            try:
                start, end = int(parts[1], 16), int(parts[2], 16)
            except ValueError:
                fail("Ghidra map line %d has a bad address: %r" % (n, line))
            if end > start:
                self.ranges.append((start, end, parts[0]))
        self.ranges.sort()
        self.starts = set(r[0] for r in self.ranges)

    def covers(self, vaddr):
        for start, end, _ in self.ranges:
            if start <= vaddr < end:
                return True
            if start > vaddr:
                break
        return False

    def name_at(self, vaddr):
        for start, end, name in self.ranges:
            if start <= vaddr < end:
                return name
        return None


# --------------------------------------------------------------------------
# A tiny MIPS describer, only for readable comments
# --------------------------------------------------------------------------

REG = ["zero", "at", "v0", "v1", "a0", "a1", "a2", "a3", "t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7",
       "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "t8", "t9", "k0", "k1", "gp", "sp", "fp", "ra"]
OPS = {0x02: "j", 0x03: "jal", 0x04: "beq", 0x05: "bne", 0x06: "blez", 0x07: "bgtz", 0x08: "addi",
       0x09: "addiu", 0x0A: "slti", 0x0B: "sltiu", 0x0C: "andi", 0x0D: "ori", 0x0E: "xori", 0x0F: "lui",
       0x14: "beql", 0x15: "bnel", 0x16: "blezl", 0x17: "bgtzl", 0x18: "daddi", 0x19: "daddiu",
       0x1A: "ldl", 0x1B: "ldr", 0x1C: "mmi", 0x1E: "lq", 0x1F: "sq", 0x20: "lb", 0x21: "lh", 0x22: "lwl",
       0x23: "lw", 0x24: "lbu", 0x25: "lhu", 0x26: "lwr", 0x27: "lwu", 0x28: "sb", 0x29: "sh", 0x2A: "swl",
       0x2B: "sw", 0x2C: "sdl", 0x2D: "sdr", 0x2E: "swr", 0x2F: "cache", 0x31: "lwc1", 0x33: "pref",
       0x36: "lqc2", 0x37: "ld", 0x39: "swc1", 0x3E: "sqc2", 0x3F: "sd", 0x01: "regimm", 0x10: "cop0",
       0x11: "cop1", 0x12: "cop2"}
SPECIAL = {0x00: "sll", 0x02: "srl", 0x03: "sra", 0x04: "sllv", 0x06: "srlv", 0x07: "srav", 0x08: "jr",
           0x09: "jalr", 0x0A: "movz", 0x0B: "movn", 0x0C: "syscall", 0x0D: "break", 0x0F: "sync",
           0x10: "mfhi", 0x11: "mthi", 0x12: "mflo", 0x13: "mtlo", 0x18: "mult", 0x19: "multu", 0x1A: "div",
           0x1B: "divu", 0x20: "add", 0x21: "addu", 0x22: "sub", 0x23: "subu", 0x24: "and", 0x25: "or",
           0x26: "xor", 0x27: "nor", 0x2A: "slt", 0x2B: "sltu", 0x2D: "daddu", 0x2F: "dsubu", 0x38: "dsll",
           0x3A: "dsrl", 0x3C: "dsll32", 0x3E: "dsrl32", 0x3F: "dsra32"}


def describe(word, vaddr=None):
    op = word >> 26
    rs, rt, rd = (word >> 21) & 31, (word >> 16) & 31, (word >> 11) & 31
    imm = word & 0xFFFF
    if word == 0:
        return "nop"
    if op == 0:
        fn = word & 0x3F
        name = SPECIAL.get(fn, "special")
        if fn == 0x08:
            return "jr $%s" % REG[rs]
        if fn == 0x0C:
            return "syscall"
        if fn in (0x21, 0x25, 0x2D) and rt == 0:
            return "move $%s, $%s" % (REG[rd], REG[rs])
        return name
    if op in (0x02, 0x03):
        target = (word & 0x03FFFFFF) << 2
        if vaddr is not None:
            target |= (vaddr + 4) & 0xF0000000
        return "%s 0x%08X" % (OPS[op], target)
    if op in (0x04, 0x05, 0x14, 0x15, 0x06, 0x07, 0x16, 0x17):
        regs = ("$%s, $%s" % (REG[rs], REG[rt])) if op in (0x04, 0x05, 0x14, 0x15) else "$" + REG[rs]
        off = (imm - 0x10000 if imm & 0x8000 else imm) << 2
        where = ("0x%08X" % ((vaddr + 4 + off) & 0xFFFFFFFF)) if vaddr is not None else ("%+d" % (off + 4))
        return "%s %s, %s" % (OPS[op], regs, where)
    if op == 0x0F:
        return "lui $%s, 0x%04X" % (REG[rt], imm)
    if op in (0x09, 0x0D, 0x08, 0x0C, 0x0E):
        return "%s $%s, $%s, 0x%04X" % (OPS[op], REG[rt], REG[rs], imm)
    if op == 0x11:
        if rs == 0x04:
            return "mtc1 $%s, $f%d" % (REG[rt], rd)
        if rs == 0x00:
            return "mfc1 $%s, $f%d" % (REG[rt], rd)
        return "cop1"
    if op in (0x20, 0x21, 0x23, 0x24, 0x25, 0x27, 0x28, 0x29, 0x2B, 0x31, 0x37, 0x39, 0x3F, 0x1E, 0x1F):
        reg = ("$f%d" % rt) if op in (0x31, 0x39) else "$" + REG[rt]
        off = imm - 0x10000 if imm & 0x8000 else imm
        return "%s %s, %s($%s)" % (OPS[op], reg, ("-0x%X" % -off) if off < 0 else ("0x%X" % off), REG[rs])
    return OPS.get(op, "op%02X" % op)


def jump_target(word, vaddr):
    op = word >> 26
    if op in (0x02, 0x03):
        return ((vaddr + 4) & 0xF0000000) | ((word & 0x03FFFFFF) << 2)
    if op in (0x04, 0x05, 0x06, 0x07, 0x14, 0x15, 0x16, 0x17) or op == 0x01:
        imm = word & 0xFFFF
        off = imm - 0x10000 if imm & 0x8000 else imm
        return (vaddr + 4 + (off << 2)) & 0xFFFFFFFF
    return None


def call_sites(elf, target):
    """Direct references to `target` in the ELF's code: {"jal": [...], "j": [...]} (instruction addresses).

    PS2Recomp emits a jal as a dispatched call (runtime->dispatchGuestBranch, through the function
    table), but a j to a known function as a direct C++ call that skips the table, so a replaced or
    wrapped function only sees its jal (and jalr) callers."""
    out = {"jal": [], "j": []}
    if target & 3:
        return out
    regions = []
    for sec in elf.sections:
        if sec["flags"] & SHF_EXECINSTR and sec["type"] != SHT_NOBITS and sec["ram"] is not None:
            seg, backed = elf.locate(sec["ram"])
            if seg is not None and backed:
                size = min(sec["size"], seg["ram"] + seg["filesz"] - sec["ram"])
                regions.append((sec["addr"], seg["offset"] + sec["ram"] - seg["ram"], size))
    if not regions:
        regions = [(s["vaddr"], s["offset"], s["filesz"]) for s in elf.segments if s["flags"] & PF_X]
    index = (target >> 2) & 0x03FFFFFF
    for base, off, size in regions:
        if (base + 4) & 0xF0000000 != target & 0xF0000000:
            continue
        for kind, op in (("jal", 3), ("j", 2)):
            needle = struct.pack("<I", (op << 26) | index)
            i = elf.data.find(needle, off, off + size)
            while i >= 0:
                if (i - off) % 4 == 0:
                    out[kind].append(base + i - off)
                i = elf.data.find(needle, i + 1, off + size)
    return out


def hook_advice(elf, addr):
    """Warnings for a function about to be replaced or wrapped. Returns (lines, serious)."""
    sites = call_sites(elf, addr)
    nj, nt = len(sites["jal"]), len(sites["j"])
    lines = ["%s is reached by %d jal and %d j instruction(s) in the ELF." % (hex32(addr), nj, nt)]
    serious = False
    if nt:
        lines.append("%d j tail jump(s) to it (%s) compile to a direct C++ call that skips the function table: "
                     "the hook does not run on those paths." % (nt, ", ".join(hex32(x) for x in sites["j"][:4])))
    if nt and not nj:
        serious = True
        lines.append("Nothing reaches it with jal: pick the caller one level up (a function called with jal) "
                     "or check the callers in Ghidra.")
    elif not nj and not nt:
        lines.append("No direct jal or j: fine if it is called through a pointer (jalr goes through the table); "
                     "check its references in Ghidra.")
    return lines, serious


def patch_class_of(original):
    """How ps2_recomp classifies a patched word: by the ORIGINAL instruction (ps2_recompiler.cpp)."""
    op = original >> 26
    if op == 0 and (original & 0x3F) == 0x0C:
        return "syscall"
    if op == 0x10:
        return "cop0"
    if op == 0x2F:
        return "cache"
    return "generic"


# --------------------------------------------------------------------------
# .pnach parser
# --------------------------------------------------------------------------

PLACES = {0: "once on load", 1: "continuously (every vsync)", 2: "on load and continuously",
          3: "on load or when enabled"}
SIZES = {"byte": 1, "short": 2, "word": 4, "double": 8, "beshort": 2, "beword": 4, "bedouble": 8}
META_KEYS = ("gametitle", "comment", "author", "description", "gsaspectratio", "gsinterlacemode")
EXT_NAMES = {0x0: "8-bit write", 0x1: "16-bit write", 0x2: "32-bit write", 0x3: "increment/decrement",
             0x4: "serial (repeated) write", 0x5: "copy bytes", 0x6: "pointer write",
             0x7: "bitwise operation", 0xC: "32-bit conditional (gates every following code)",
             0xD: "16-bit conditional (gates the next code)", 0xE: "multi-line conditional"}


class _ExtState(object):
    def __init__(self):
        self.cont = 0
        self.cont_owner = 0
        self.cont_name = ""
        self.ptr_second = False
        self.cond_lines = 0
        self.cond_all = False
        self.cond_owner = 0


def parse_pnach_text(text):
    meta = {}
    sections = []
    writes = []
    manual = []
    errors = []
    current = ""
    ext = _ExtState()
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and "]" in line:
            current = line[1:line.index("]")].strip()
            if current not in sections:
                sections.append(current)
            ext = _ExtState()
            continue
        if "=" not in line:
            errors.append({"line": n, "text": raw.strip(), "error": "not key=value"})
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key in META_KEYS:
            meta.setdefault(key, [])
            meta[key].append({"section": current, "value": value})
            continue
        if key == "dpatch":
            manual.append({"line": n, "section": current, "source": raw.strip(),
                           "reason": "dpatch (pattern-matched dynamic patch): port by hand once you know the address"})
            continue
        if key != "patch":
            meta.setdefault(key, [])
            meta[key].append({"section": current, "value": value})
            continue
        body = value.split("//", 1)[0].strip()
        parts = [p.strip() for p in body.split(",")]
        if len(parts) != 5:
            errors.append({"line": n, "text": raw.strip(), "error": "patch= needs 5 fields: place,cpu,address,type,value"})
            continue
        place_s, cpu, addr_s, ptype, val_s = parts
        ptype = ptype.lower()
        cpu = cpu.upper()
        try:
            place = int(place_s)
        except ValueError:
            errors.append({"line": n, "text": raw.strip(), "error": "place must be 0, 1, 2 or 3"})
            continue
        if place not in PLACES:
            errors.append({"line": n, "text": raw.strip(), "error": "place must be 0, 1, 2 or 3"})
            continue
        if cpu not in ("EE", "IOP"):
            errors.append({"line": n, "text": raw.strip(), "error": "cpu must be EE or IOP"})
            continue
        try:
            addr = parse_hex(addr_s, "address")
            if ptype != "bytes":
                val = parse_hex(val_s, "value")
        except UsageError as e:
            errors.append({"line": n, "text": raw.strip(), "error": str(e)})
            continue
        if addr > 0xFFFFFFFF:
            errors.append({"line": n, "text": raw.strip(), "error": "address wider than 32 bits"})
            continue
        base = {"line": n, "section": current, "place": place, "cpu": cpu, "type": ptype, "source": raw.strip(),
                "value": val_s}
        if cpu == "IOP":
            manual.append(dict(base, reason="IOP patch: the port runs IRX modules in ps2xIOP; port by hand"))
            continue
        if ptype == "bytes":
            hx = val_s[2:] if val_s.lower().startswith("0x") else val_s
            if not hx or len(hx) % 2 or not re.fullmatch(r"[0-9A-Fa-f]+", hx):
                errors.append({"line": n, "text": raw.strip(), "error": "bytes value must be an even-length hex string"})
                continue
            writes.append(dict(base, addr=addr, data=bytes.fromhex(hx)))
            continue
        if ptype in SIZES:
            size = SIZES[ptype]
            notes = []
            if val >= (1 << (8 * size)):
                notes.append("value 0x%X is wider than a %s; PCSX2 keeps the low %d bytes" % (val, ptype, size))
                val &= (1 << (8 * size)) - 1
            data = val.to_bytes(size, "big" if ptype.startswith("be") else "little")
            w = dict(base, addr=addr, data=data)
            if notes:
                w["notes"] = notes
            writes.append(w)
            continue
        if ptype != "extended":
            errors.append({"line": n, "text": raw.strip(), "error": "unknown type %r" % ptype})
            continue
        # CodeBreaker-style extended code: the address's top nibble is the command.
        if ext.cont > 0:
            ext.cont -= 1
            if ext.ptr_second:
                ext.ptr_second = False
                levels = max(addr & 0xFFFF, 1)
                ext.cont += levels // 2           # ceil((levels - 1) / 2) extra offset lines
            if ext.cond_lines > 0:
                ext.cond_lines -= 1
            manual.append(dict(base, reason="second line of the %s code on line %d" % (ext.cont_name, ext.cont_owner)))
            continue
        cmd = addr >> 28
        gated = ext.cond_all or ext.cond_lines > 0
        gate_owner = ext.cond_owner
        if ext.cond_lines > 0:
            ext.cond_lines -= 1
        name = EXT_NAMES.get(cmd, "unsupported CodeBreaker command %X" % cmd)
        if cmd in (0, 1, 2):
            if gated:
                manual.append(dict(base, reason="%s that only runs when the conditional on line %d is true" % (name, gate_owner)))
                continue
            size = (1, 2, 4)[cmd]
            v = val & ((1 << (8 * size)) - 1)
            writes.append(dict(base, addr=addr & 0x0FFFFFFF, data=v.to_bytes(size, "little"),
                               extended="%s (code %08X %08X)" % (name, addr, val)))
            continue
        reason = "extended %s: needs a manual port" % name
        if gated:
            reason += " (and it is gated by the conditional on line %d)" % gate_owner
        if cmd == 3:
            if ((addr >> 20) & 0xFF) in (4, 5):
                ext.cont, ext.cont_owner, ext.cont_name = 1, n, name
        elif cmd in (4, 5):
            ext.cont, ext.cont_owner, ext.cont_name = 1, n, name
        elif cmd == 6:
            ext.cont, ext.cont_owner, ext.cont_name = 1, n, name
            ext.ptr_second = True
        elif cmd == 0xC:
            ext.cond_all, ext.cond_owner = True, n
        elif cmd == 0xD:
            ext.cond_lines, ext.cond_owner = ext.cond_lines + 1, n
        elif cmd == 0xE:
            ext.cond_lines, ext.cond_owner = ext.cond_lines + ((addr >> 16) & 0xFF), n
        manual.append(dict(base, reason=reason))
    return {"meta": meta, "sections": sections, "writes": writes, "manual": manual, "errors": errors}


def load_pnach(path, only_sections=None):
    raw = read_file(path, "pnach")
    if b"\0" in raw[:4096]:
        fail("%s looks binary, not a .pnach text file" % os.path.basename(path))
    text = raw.decode("utf-8", "replace")
    res = parse_pnach_text(text)
    if only_sections:
        wanted = set(only_sections)
        missing = [s for s in only_sections if s not in res["sections"] and s != ""]
        if missing:
            fail("section(s) not in %s: %s (have: %s)" % (os.path.basename(path), ", ".join(missing),
                                                          ", ".join(res["sections"]) or "none"))
        for k in ("writes", "manual"):
            res[k] = [w for w in res[k] if w["section"] in wanted]
    res["path"] = path
    res["name"] = os.path.basename(path)
    return res


def crc_from_pnach_name(name):
    m = re.match(r"^(?:[A-Za-z]{4}[-_]\d{5}_)?([0-9A-Fa-f]{8})\.pnach$", name, re.IGNORECASE)
    return int(m.group(1), 16) if m else None


def serial_from_name(name):
    m = re.match(r"^([A-Za-z]{4})[_-](\d{3})\.?(\d{2})", name)
    if not m:
        return None
    return "%s-%s%s" % (m.group(1).upper(), m.group(2), m.group(3))


def check_crc(pn, elf, ignore):
    want = crc_from_pnach_name(pn["name"])
    if want is None:
        return None
    have = elf.pcsx2_crc()
    if want != have and not ignore:
        fail("%s is for the build with PCSX2 CRC %08X but %s has %08X: the addresses will not line up "
             "(use --ignore-crc only if you are sure)" % (pn["name"], want, elf.name, have))
    return want == have


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def _merge_ranges(ranges):
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _code_words(items, elf, funcs):
    """Merge CODE byte/short/word writes into the original words read from the ELF (last write wins)."""
    words = {}
    for it in items:
        if it["class"] != "CODE":
            continue
        for i, b in enumerate(it["data"]):
            r = it["ram"] + i
            wr = r & ~3
            cw = words.get(wr)
            if cw is None:
                orig = elf.read_ram(wr, 4) or bytes(4)
                cw = words[wr] = {"orig": bytes(orig), "new": bytearray(orig), "lines": [], "sources": [],
                                  "sections": [], "confidence": it["confidence"], "why": it["why"]}
            cw["new"][r - wr] = b
            w = it["write"]
            if w["line"] not in cw["lines"]:
                cw["lines"].append(w["line"])
                cw["sources"].append(w["source"])
            if w["section"] not in cw["sections"]:
                cw["sections"].append(w["section"])
    code = []
    for wr in sorted(words):
        cw = words[wr]
        o = struct.unpack("<I", cw["orig"])[0]
        nw = struct.unpack("<I", bytes(cw["new"]))[0]
        vaddr = elf.vaddr_of(wr)
        entry = {"ram": wr, "vaddr": vaddr, "original": o, "new": nw, "lines": cw["lines"], "sources": cw["sources"],
                 "sections": cw["sections"], "confidence": cw["confidence"], "why": cw["why"], "notes": []}
        if o == nw:
            entry["notes"].append("no change: the ELF already holds this value")
        pc = patch_class_of(o)
        if pc != "generic":
            flag = {"syscall": "patch_syscalls", "cop0": "patch_cop0", "cache": "patch_cache"}[pc]
            entry["notes"].append("the original is a %s instruction: ps2_recomp only applies this patch when "
                                  "%s = true in [general]" % (pc.upper(), flag))
        code.append(entry)
    return code


def classify(pn, elf, funcs=None):
    items = []
    manual = list(pn["manual"])
    for w in pn["writes"]:
        ram = ram_offset(w["addr"])
        if ram is None or ram + len(w["data"]) > RAM_SIZE:
            manual.append(dict(w, reason="OUTSIDE: %s at %s: no RDRAM write can reach it, port by hand"
                                         % (outside_reason(w["addr"]), hex32(w["addr"]))))
            continue
        if ram < KERNEL_END:
            manual.append(dict(w, reason="OUTSIDE: EE kernel memory at %s (below 0x%08X): the port's kernel is "
                                         "high-level emulation, so this write does not do what it did on a PS2"
                                         % (hex32(w["addr"]), KERNEL_END)))
            continue
        # classify byte by byte, then group runs of the same class
        runs = []
        for i in range(len(w["data"])):
            r = ram + i
            seg, backed = elf.locate(r)
            if seg is None:
                cls, conf, why = "HEAP", "high", "RAM outside every ELF segment (heap, stack or loaded files)"
            elif not backed:
                cls, conf, why = "BSS", "high", "zero-filled part of segment %d" % seg["index"]
            else:
                sec = elf.section_at(r)
                if sec is not None and sec["type"] == SHT_NOBITS:
                    cls, conf, why = "BSS", "high", "section %s (zero-filled)" % sec["name"]
                else:
                    code, conf, why = elf.is_code(r, funcs)
                    cls = "CODE" if code else "DATA"
                    if not code and not why:
                        why = "file-backed data in segment %d" % seg["index"]
            if runs and runs[-1]["class"] == cls and runs[-1]["end"] == i:
                runs[-1]["end"] = i + 1
            else:
                runs.append({"class": cls, "confidence": conf, "why": why, "start": i, "end": i + 1})
        for run in runs:
            items.append({"write": w, "ram": ram + run["start"], "data": w["data"][run["start"]:run["end"]],
                          "class": run["class"], "confidence": run["confidence"], "why": run["why"]})

    # Code caves: new code written into memory, or jumps and branches into patched areas.
    written = _merge_ranges([(it["ram"], it["ram"] + len(it["data"])) for it in items])

    def block_of(r):
        for a, b in written:
            if a <= r < b:
                return (a, b)
        return None

    cave_blocks = set()
    cave_lines = {}
    code = _code_words(items, elf, funcs)
    for cw in code:
        if cw["new"] == cw["original"]:
            continue
        if funcs is not None and not funcs.covers(cw["vaddr"]):
            for ln in cw["lines"]:
                cave_lines.setdefault(ln, "code written at %s, where no function in the Ghidra map is "
                                          "(ps2_recomp never decodes it)" % hex32(cw["vaddr"]))
            continue
        tgt = jump_target(cw["new"], cw["vaddr"])
        if tgt is None:
            continue
        tr = ram_offset(tgt)
        if tr is None:
            continue
        kind = describe(cw["new"], cw["vaddr"]).split()[0]
        why = None
        blk = block_of(tr)
        if blk is not None:
            cave_blocks.add(blk)
            why = "%s to %s lands in memory this pnach writes" % (kind, hex32(tgt))
        elif not elf.is_code(tr, funcs)[0]:
            why = "%s to %s lands outside the game's code" % (kind, hex32(tgt))
        elif funcs is not None and (cw["new"] >> 26) in (0x02, 0x03) and not funcs.covers(tgt):
            why = "%s to %s lands where no function in the Ghidra map is" % (kind, hex32(tgt))
        if why:
            for ln in cw["lines"]:
                cave_lines.setdefault(ln, why)
    for a, b in written:
        if (a, b) in cave_blocks or b - a < 8:
            continue
        inside = [it for it in items if a <= it["ram"] < b]
        if not any(it["class"] in ("DATA", "BSS", "HEAP") for it in inside):
            continue
        buf = bytearray(b - a)
        for it in inside:
            buf[it["ram"] - a:it["ram"] - a + len(it["data"])] = it["data"]
        for off in range((4 - a % 4) % 4, len(buf) - 3, 4):
            if struct.unpack_from("<I", buf, off)[0] == 0x03E00008:   # jr $ra: a function body
                cave_blocks.add((a, b))
                break
    for it in items:
        if any(a <= it["ram"] < b for a, b in cave_blocks):
            cave_lines.setdefault(it["write"]["line"], "new code written at %s" % hex32(it["ram"]))
    if cave_lines:
        for it in items:
            if it["write"]["line"] in cave_lines:
                it["class"] = "CAVE"
        code = _code_words(items, elf, funcs)
        seen = set()
        for it in items:
            ln = it["write"]["line"]
            if it["class"] == "CAVE" and ln not in seen:
                seen.add(ln)
                manual.append(dict(it["write"], reason="CAVE: %s; rewrite its logic as an override function "
                                                       "(override.py new --replace or --wrap)" % cave_lines[ln]))
    manual.sort(key=lambda m: m["line"])
    return {"items": items, "code": code, "manual": manual}


# --------------------------------------------------------------------------
# Output builders
# --------------------------------------------------------------------------


def value_text(data):
    """A write's value as the number the pnach line meant (little-endian), or its bytes."""
    if len(data) in (1, 2, 4, 8):
        return "0x%0*X" % (2 * len(data), int.from_bytes(data, "little"))
    return data.hex().upper()


def write_bucket(it):
    """Which override function writes this item: load / first / every, or None (not in the override)."""
    cls, place = it["class"], it["write"]["place"]
    if cls == "DATA":
        return ["load"] + (["every"] if place in (1, 2) else [])
    if cls in ("BSS", "HEAP"):
        return ["every"] if place in (1, 2) else ["first"]
    if cls == "CODE" and it["confidence"] == "low":
        return ["load"] + (["every"] if place in (1, 2) else [])
    return []


def toml_text(pn, elf, res):
    out = []
    out.append("# Console skill: instruction patches from %s for %s" % (pn["name"], elf.name))
    out.append("# Paste at the end of the game's ps2_recomp TOML (as top-level [[patches.instructions]] blocks),")
    out.append("# then re-run ps2_recomp and rebuild. ps2_recomp only patches words inside functions it recompiles.")
    gates = set()
    for cw in res["code"]:
        if cw["original"] == cw["new"]:
            continue
        out.append("")
        sec = ", ".join("[%s]" % s for s in cw["sections"] if s)
        for ln, src in zip(cw["lines"], cw["sources"]):
            out.append("# %sline %d: %s" % (sec + " " if sec else "", ln, src))
        out.append("# original %s (%s) -> new %s (%s)" % (hex32(cw["original"]), describe(cw["original"], cw["vaddr"]),
                                                         hex32(cw["new"]), describe(cw["new"], cw["vaddr"])))
        for note in cw["notes"]:
            out.append("# NOTE: " + note)
            if "patch_" in note:
                gates.add(note.split("patch_")[1].split()[0])
        if cw["confidence"] == "low":
            out.append("# NOTE: " + cw["why"] + "; the override also writes it to RDRAM in case it is data")
        out.append("[[patches.instructions]]")
        out.append('address = "%s"' % hex32(cw["vaddr"]))
        out.append('value = "%s"' % hex32(cw["new"]))
    n = sum(1 for cw in res["code"] if cw["original"] != cw["new"])
    if not n:
        out.append("")
        out.append("# No code patches: nothing in this pnach lands in the game's code.")
    others = [it for it in res["items"] if it["class"] in ("DATA", "BSS", "HEAP")]
    if others:
        out.append("")
        out.append("# %d data/BSS/heap write(s) are not instruction patches: use  pnach.py override  for them." % len(others))
    if res["manual"]:
        out.append("# %d line(s) need a manual port: see  pnach.py classify." % len(res["manual"]))
    if gates:
        out.append("# Some originals are gated: set %s = true in [general] or those patches are skipped."
                   % " and ".join("patch_" + g for g in sorted(gates)))
    return "\n".join(out) + "\n"


def c_string(s):
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")


def slugify(text):
    s = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    if not s:
        s = "mod"
    if s[0].isdigit():
        s = "m_" + s
    return s


def emit_write(lines, it, indent="    "):
    w = it["write"]
    sec = ("[%s] " % w["section"]) if w["section"] else ""
    lines.append("%s// %sline %d (%s): %s" % (indent, sec, w["line"], PLACES[w["place"]], w["source"].replace("*/", "* /")))
    if it["class"] == "CODE":
        lines.append("%s// also an instruction patch in the TOML; written here too because %s" % (indent, it["why"]))
    data, ram = it["data"], it["ram"]
    if len(data) == 1:
        lines.append("%sPs2FastWrite8(rdram, 0x%08Xu, 0x%02Xu);" % (indent, ram, data[0]))
    elif len(data) == 2:
        lines.append("%sPs2FastWrite16(rdram, 0x%08Xu, 0x%04Xu);" % (indent, ram, int.from_bytes(data, "little")))
    elif len(data) == 4:
        lines.append("%sPs2FastWrite32(rdram, 0x%08Xu, 0x%08Xu);" % (indent, ram, int.from_bytes(data, "little")))
    elif len(data) == 8:
        lines.append("%sPs2FastWrite64(rdram, 0x%08Xu, 0x%016XULL);" % (indent, ram, int.from_bytes(data, "little")))
    else:
        arr = ", ".join("0x%02X" % b for b in data)
        lines.append("%s{" % indent)
        lines.append("%s    static const uint8_t kBytes[] = {%s};" % (indent, arr))
        lines.append("%s    for (uint32_t i = 0; i < sizeof(kBytes); ++i)" % indent)
        lines.append("%s        Ps2FastWrite8(rdram, 0x%08Xu + i, kBytes[i]);" % (indent, ram))
        lines.append("%s}" % indent)


def override_parts(pn, elf, res, frame_hook, mod_name):
    buckets = {"load": [], "first": [], "every": []}
    for it in res["items"]:
        for b in write_bucket(it):
            buckets[b].append(it)
    fn = []

    def func(name, comment, items, unused):
        for c in comment:
            fn.append("// " + c)
        fn.append("%svoid %s(uint8_t *rdram)" % ("[[maybe_unused]] " if unused else "", name))
        fn.append("{")
        fn.append("    (void)rdram;")
        for it in items:
            emit_write(fn, it)
        fn.append("}")
        fn.append("")

    hook = frame_hook is not None
    func("applyLoadWrites", ["Data patches: written into RDRAM once, when the ELF is loaded (before the game's",
                             "first instruction)."], buckets["load"], False)
    func("applyFirstFrameWrites", ["Zero-filled (BSS) or heap memory the game clears during boot: written once,",
                                   "on the first call of the frame hook, after boot cleared it."]
         + ([] if hook else ["NOT WIRED UP: no --frame-hook was given (see the note at the top)."]),
         buckets["first"], not hook)
    func("applyEveryFrameWrites", ["Patches PCSX2 re-applies every vsync (place 1 and 2) outside the code:",
                                   "written on every call of the frame hook, before the game's frame function runs."]
         + ([] if hook else ["NOT WIRED UP: no --frame-hook was given (see the note at the top)."]),
         buckets["every"], not hook)
    apply = ["    applyLoadWrites(rdram);"]
    if hook:
        fn.append("constexpr uint32_t kFrameHookAddress = 0x%08Xu;" % frame_hook)
        fn.append("PS2Runtime::RecompiledFunction g_originalFrameFunction = nullptr;")
        fn.append("bool g_firstFrameDone = false;")
        fn.append("")
        fn.append("// Runs instead of the game's once-per-frame function, then calls the real one.")
        fn.append("void frameHook(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)")
        fn.append("{")
        fn.append("    if (!g_firstFrameDone)")
        fn.append("    {")
        fn.append("        g_firstFrameDone = true;")
        fn.append("        applyFirstFrameWrites(rdram);")
        fn.append("    }")
        fn.append("    applyEveryFrameWrites(rdram);")
        fn.append("    g_originalFrameFunction(rdram, ctx, runtime);")
        fn.append("}")
        apply += [
            "",
            "    if (!runtime.hasFunction(kFrameHookAddress))",
            "    {",
            '        std::cerr << "[console-mod] %s: no recompiled function at frame hook 0x%08X, "' % (c_string(mod_name), frame_hook),
            '                     "per-frame patches are off" << std::endl;',
            "        return;",
            "    }",
            "    PS2Runtime::RecompiledFunction current = runtime.lookupFunction(kFrameHookAddress);",
            "    if (current != &frameHook)",
            "    {",
            "        g_originalFrameFunction = current; // keep the game's function (safe if loadELF runs twice)",
            "    }",
            "    g_firstFrameDone = false;",
            "    runtime.replaceFunction(kFrameHookAddress, &frameHook);",
        ]
    counts = dict((k, len(v)) for k, v in buckets.items())
    return fn, apply, counts


def load_override_module():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "override.py")
    if not os.path.isfile(path):
        fail("override.py is missing next to pnach.py (reinstall the console-mod skill)")
    spec = importlib.util.spec_from_file_location("console_mod_override_helper", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.UsageError = UsageError     # its errors surface as ours: one line, exit code 2
    mod.fail = fail
    return mod


def override_text(pn, elf, res, frame_hook, mod_name, slug):
    ov = load_override_module()
    fn, apply, counts = override_parts(pn, elf, res, frame_hook, mod_name)
    code_n = sum(1 for cw in res["code"] if cw["original"] != cw["new"])
    notes = [
        "Source: %s%s" % (pn["name"], (" (sections: %s)" % ", ".join(pn["sections"])) if pn["sections"] else ""),
        "Generated by console-mod pnach.py override on %s." % datetime.date.today().isoformat(),
        "Writes at load: %d, on the first frame: %d, every frame: %d." % (counts["load"], counts["first"], counts["every"]),
        "Code patches are NOT here: %d instruction patch(es) go in the game's TOML (pnach.py toml)." % code_n,
    ]
    if frame_hook is None and (counts["first"] or counts["every"]):
        notes += ["WARNING: %d write(s) need a frame hook and are NOT wired up. Find a function the game" % (counts["first"] + counts["every"]),
                  "calls once per frame (Ghidra: the main loop's per-frame call, or whatever waits for vsync)",
                  "and re-run: pnach.py override ... --frame-hook 0xADDR"]
    elif frame_hook is not None:
        notes.append("Frame hook: the game's function at 0x%08X is wrapped. Only calls that go through the" % frame_hook)
        notes.append("runtime's function table see the wrapper (jal, jalr); a j tail jump to it is a direct C++ call.")
        notes += hook_advice(elf, frame_hook)[0]
    if res["manual"]:
        notes.append("Not ported (%d, do these by hand):" % len(res["manual"]))
        for m in res["manual"][:40]:
            notes.append("  line %d: %s" % (m["line"], m["reason"]))
        if len(res["manual"]) > 40:
            notes.append("  ... and %d more (pnach.py classify lists them all)" % (len(res["manual"]) - 40))
    return ov.render_module(mod_name=mod_name, elf_name=elf.name, entry=elf.entry, crc32=elf.crc32(),
                            slug=slug, functions="\n".join(fn), apply_body="\n".join(apply), notes=notes)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _w_json(w):
    d = {"line": w["line"], "section": w["section"], "place": w["place"], "type": w["type"], "cpu": w["cpu"],
         "address": hex32(w["addr"]), "bytes": w["data"].hex().upper(), "source": w["source"]}
    if w.get("extended"):
        d["extended"] = w["extended"]
    if w.get("notes"):
        d["notes"] = w["notes"]
    return d


def cmd_parse(a):
    pn = load_pnach(a.pnach, a.section)
    if a.json:
        print(json.dumps({"file": pn["name"], "meta": pn["meta"], "sections": pn["sections"],
                          "writes": [_w_json(w) for w in pn["writes"]],
                          "manual": [{"line": m["line"], "section": m["section"], "reason": m["reason"],
                                      "source": m["source"]} for m in pn["manual"]],
                          "errors": pn["errors"]}, indent=2))
    else:
        print("%s: %d write(s), %d needing a manual port, %d bad line(s)" % (
            pn["name"], len(pn["writes"]), len(pn["manual"]), len(pn["errors"])))
        for k, vals in pn["meta"].items():
            for v in vals:
                print("  %-15s %s%s" % (k, v["value"], ("   [%s]" % v["section"]) if v["section"] else ""))
        if pn["sections"]:
            print("  sections        " + ", ".join("[%s]" % s for s in pn["sections"]))
        last = None
        for w in pn["writes"]:
            if w["section"] != last:
                print("\n[%s]" % w["section"] if w["section"] else "\n(no section)")
                last = w["section"]
            extra = ("  <- " + w["extended"]) if w.get("extended") else ""
            print("  line %-4d %-26s %s %-8s %s%s" % (w["line"], PLACES[w["place"]], hex32(w["addr"]), w["type"],
                                                    w["value"].upper(), extra))
            for note in w.get("notes", []):
                print("            note: " + note)
        if pn["manual"]:
            print("\nNeeds a manual port:")
            for m in pn["manual"]:
                print("  line %-4d %s" % (m["line"], m["reason"]))
        if pn["errors"]:
            print("\nBad lines:")
            for e in pn["errors"]:
                print("  line %-4d %s: %s" % (e["line"], e["error"], e["text"]))
    return 1 if (pn["errors"] or pn["manual"]) else 0


def _common(a):
    pn = load_pnach(a.pnach, a.section)
    elf = Elf(a.elf)
    funcs = FuncMap(a.map) if getattr(a, "map", None) else None
    crc_ok = check_crc(pn, elf, a.ignore_crc)
    res = classify(pn, elf, funcs)
    return pn, elf, funcs, res, crc_ok


def cmd_classify(a):
    pn, elf, funcs, res, crc_ok = _common(a)
    counts = {}
    for it in res["items"]:
        counts[it["class"]] = counts.get(it["class"], 0) + 1
    if a.json:
        print(json.dumps({
            "pnach": pn["name"], "elf": elf.name, "pcsx2_crc": "%08X" % elf.pcsx2_crc(), "crc_match": crc_ok,
            "writes": [{"line": it["write"]["line"], "section": it["write"]["section"], "place": it["write"]["place"],
                        "class": it["class"], "confidence": it["confidence"], "why": it["why"],
                        "ram": hex32(it["ram"]), "bytes": it["data"].hex().upper(),
                        "override": write_bucket(it)} for it in res["items"]],
            "code_patches": [{"address": hex32(cw["vaddr"]), "original": hex32(cw["original"]), "new": hex32(cw["new"]),
                              "original_asm": describe(cw["original"], cw["vaddr"]), "new_asm": describe(cw["new"], cw["vaddr"]),
                              "lines": cw["lines"], "confidence": cw["confidence"], "notes": cw["notes"]} for cw in res["code"]],
            "manual": [{"line": m["line"], "section": m["section"], "reason": m["reason"], "source": m["source"]}
                       for m in res["manual"]],
            "counts": counts}, indent=2))
        return 1 if res["manual"] else 0
    print("%s on %s (PCSX2 CRC %08X%s)" % (pn["name"], elf.name, elf.pcsx2_crc(),
                                          "" if crc_ok is None else (", matches the file name" if crc_ok else ", DOES NOT match the file name")))
    print("  " + ", ".join("%s %d" % (k, counts[k]) for k in ("CODE", "DATA", "BSS", "HEAP", "CAVE") if k in counts)
          + (", manual %d" % len(res["manual"]) if res["manual"] else ""))
    where = {"CODE": "TOML instruction patch", "DATA": "override, at load", "BSS": "override, frame hook",
             "HEAP": "override, frame hook", "CAVE": "manual"}
    print("")
    for it in res["items"]:
        w = it["write"]
        dest = where[it["class"]]
        if it["class"] in ("DATA", "BSS", "HEAP"):
            dest = "override: " + " + ".join({"load": "at load", "first": "first frame", "every": "every frame"}[b]
                                              for b in write_bucket(it))
        conf = "" if it["confidence"] == "high" else " (%s confidence: %s)" % (it["confidence"], it["why"])
        print("  line %-4d %-5s %s %-18s -> %s%s" % (w["line"], it["class"], hex32(it["ram"]),
                                                     value_text(it["data"]), dest, conf))
    if res["code"]:
        print("\nCode words after merging (original from the ELF -> new):")
        for cw in res["code"]:
            print("  %s  %s %-24s -> %s %s" % (hex32(cw["vaddr"]), hex32(cw["original"]), describe(cw["original"], cw["vaddr"]),
                                              hex32(cw["new"]), describe(cw["new"], cw["vaddr"])))
            for note in cw["notes"]:
                print("      note: " + note)
    if res["manual"]:
        print("\nNeeds a manual port:")
        for m in res["manual"]:
            print("  line %-4d %s" % (m["line"], m["reason"]))
    return 1 if res["manual"] else 0


def cmd_toml(a):
    pn, elf, funcs, res, _ = _common(a)
    text = toml_text(pn, elf, res)
    if a.out:
        _write_out(a.out, text, a.force)
        print("wrote %s" % a.out, file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 1 if res["manual"] else 0


def _guard_out(path):
    norm = os.path.abspath(path).replace("\\", "/")
    if "/ps2xRuntime/src/runner/" in norm + "/":
        fail("refusing to write into ps2xRuntime/src/runner/ (generated code): use console/patches/")
    if norm.endswith(".h") or norm.endswith(".hpp"):
        fail("refusing to write a header: header edits force a full rebuild")


def _write_out(path, text, force):
    _guard_out(path)
    if os.path.exists(path) and not force:
        fail("%s exists: pass --force to overwrite it (it may hold your own edits)" % path)
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with open(path, "w", newline="\n") as f:
        f.write(text)


def cmd_override(a):
    pn, elf, funcs, res, _ = _common(a)
    hook = None
    if a.frame_hook:
        hook = parse_addr_arg(a.frame_hook, "--frame-hook")
        if hook & 3:
            fail("--frame-hook %s is not 4-byte aligned" % hex32(hook))
        r = ram_offset(hook)
        if r is None or not elf.is_code(r, funcs)[0]:
            fail("--frame-hook %s is not in the ELF's code" % hex32(hook))
        if funcs is not None and hook not in funcs.starts:
            print("warning: %s is not a function start in the Ghidra map; the hook must be a function's first "
                  "instruction" % hex32(hook), file=sys.stderr)
        advice, serious = hook_advice(elf, hook)
    base = os.path.splitext(pn["name"])[0]
    mod_name = a.name or ("pnach " + base)
    slug = slugify(a.name or base)
    out = a.out or os.path.join("console", "patches", slug + ".cpp")
    text = override_text(pn, elf, res, hook, mod_name, slug)
    _write_out(out, text, a.force)
    ov = load_override_module()
    cm = ov.ensure_cmake_include(os.path.dirname(os.path.abspath(out)))
    _, _, counts = override_parts(pn, elf, res, hook, mod_name)
    print("wrote %s  (keys: %s, entry %s, CRC-32 %s)" % (out, elf.name, hex32(elf.entry), hex32(elf.crc32())))
    print("  at load %d, first frame %d, every frame %d" % (counts["load"], counts["first"], counts["every"]))
    if hook is not None:
        for line in advice:
            print("  frame hook: " + line)
    if hook is None and (counts["first"] or counts["every"]):
        print("  WARNING: %d write(s) need --frame-hook 0xADDR (a function the game calls once per frame)"
              % (counts["first"] + counts["every"]))
    ncode = sum(1 for cw in res["code"] if cw["original"] != cw["new"])
    if ncode:
        print("  %d code patch(es) are not in this file: run  pnach.py toml  and add them to the game's TOML" % ncode)
    if res["manual"]:
        print("  %d line(s) need a manual port (listed at the top of the file)" % len(res["manual"]))
    print("  install: %s" % (cm if cm else "console_mods.cmake already next to it"))
    print("  (the include adds every .cpp in that folder to the ps2EntryRunner executable itself: in the static")
    print("  ps2_runtime library the linker would drop the registration)")
    unwired = hook is None and (counts["first"] or counts["every"])
    return 1 if (res["manual"] or unwired or (hook is not None and serious)) else 0


def cmd_crc(a):
    elf = Elf(a.elf)
    serial = a.serial or serial_from_name(elf.name)
    pc = elf.pcsx2_crc()
    rc = elf.crc32()
    info = {"elf": elf.name, "size": len(elf.data), "serial": serial, "crc32": "%08X" % rc,
            "pcsx2_crc": "%08X" % pc, "entry": hex32(elf.entry),
            "pnach": ("%s_%08X.pnach" % (serial, pc)) if serial else None, "pnach_legacy": "%08X.pnach" % pc}
    if a.json:
        print(json.dumps(info, indent=2))
        return 0
    print("ELF          %s  (%d bytes, entry %s)" % (elf.name, len(elf.data), hex32(elf.entry)))
    print("Serial       %s" % (serial or "unknown (name the ELF like SLUS_203.12 or pass --serial SLUS-20312)"))
    print("CRC-32       %08X  standard CRC-32 of the whole file: the PS2Recomp game override key" % rc)
    print("PCSX2 CRC    %08X  XOR of every little-endian 32-bit word: the pnach key" % pc)
    if serial:
        print("Pnach file   %s  (PCSX2 2.x: the patches/ or cheats/ folder)" % info["pnach"])
    print("Old name     %s  (PCSX2 1.6 and older)" % info["pnach_legacy"])
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="pnach.py",
        description="Port PCSX2 .pnach patches to a PS2Recomp port: code writes become TOML instruction "
                    "patches, data writes become a game override that writes RDRAM at load or every frame.",
        epilog="Exit codes: 0 ok, 1 something needs a manual port or a bad line, 2 bad input.")
    sub = p.add_subparsers(dest="cmd")

    def common(sp, elf=True):
        sp.add_argument("pnach", help="the .pnach file")
        if elf:
            sp.add_argument("--elf", required=True, help="the game's main ELF (e.g. SLUS_203.12) the pnach was written for")
            sp.add_argument("--map", help="Ghidra CSV (Name,Start,End,Size) from ExportPS2Functions.java, to tell code from data")
            sp.add_argument("--ignore-crc", action="store_true", help="port even if the pnach file name's CRC does not match the ELF")
        sp.add_argument("--section", action="append", metavar="NAME",
                        help="only this [section] (repeat for more); default every line")

    sp = sub.add_parser("parse", help="read a .pnach and list every write, section and line kind")
    common(sp, elf=False)
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_parse)

    sp = sub.add_parser("classify", help="sort every write into CODE, DATA, BSS, HEAP, OUTSIDE or CAVE for your ELF")
    common(sp)
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_classify)

    sp = sub.add_parser("toml", help="print [[patches.instructions]] blocks for the code writes")
    common(sp)
    sp.add_argument("--out", help="write to this file instead of stdout")
    sp.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    sp.set_defaults(func=cmd_toml)

    sp = sub.add_parser("override", help="write a PS2_REGISTER_GAME_OVERRIDE module for the data writes")
    common(sp)
    sp.add_argument("--frame-hook", metavar="0xADDR",
                    help="a function the game calls once per frame (from Ghidra): wraps it to re-apply place 1/2 and BSS/heap writes")
    sp.add_argument("--name", help="the mod's name (default: from the pnach file name)")
    sp.add_argument("--out", help="output .cpp (default console/patches/<name>.cpp)")
    sp.add_argument("--force", action="store_true", help="overwrite the output if it exists")
    sp.set_defaults(func=cmd_override)

    sp = sub.add_parser("crc", help="print the ELF's CRC-32 (override key), PCSX2 CRC and pnach file name")
    sp.add_argument("elf", help="the game's main ELF")
    sp.add_argument("--serial", help="disc serial like SLUS-20312 when the ELF is not named after it")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_crc)
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
