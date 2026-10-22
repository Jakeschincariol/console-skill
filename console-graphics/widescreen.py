#!/usr/bin/env python3
"""widescreen.py: find the 4:3 numbers a PS2 game's camera uses, and the 16:9 ones to try.

  scan    aspect-ratio and FOV candidates in an ELF (4:3 = 1.3333334, 0.75, 640/448,
          512/448 ... as data floats and as lui/ori/mtc1 constants), each with the pnach
          line(s) that set its 16:9 value. It cannot know which one is the camera: try
          them one at a time in PCSX2 (--out writes one [section] per candidate).
  values  the 4:3 -> 16:9 table, and the Hor+ FOV conversion (--fov DEG)

Python 3.8+, standard library only.
"""

import argparse
import json
import math
import os
import re
import struct
import sys

TOOL = "widescreen.py"
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
# Widescreen
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


WIDE = (16.0 / 9.0) / (4.0 / 3.0)      # 4/3: how much wider 16:9 is than 4:3

# (label, value, factor): width/height ratios widen by 4/3, height/width ratios shrink by 3/4
ASPECTS = [
    ("4:3 width/height", 4.0 / 3.0, WIDE),
    ("3:4 height/width", 3.0 / 4.0, 1.0 / WIDE),
    ("640/448 frame", 640.0 / 448.0, WIDE),
    ("448/640 frame", 448.0 / 640.0, 1.0 / WIDE),
    ("512/448 frame", 512.0 / 448.0, WIDE),
    ("448/512 frame", 448.0 / 512.0, 1.0 / WIDE),
    ("640/512 PAL frame", 640.0 / 512.0, WIDE),
    ("512/640 PAL frame", 512.0 / 640.0, 1.0 / WIDE),
]
REL_TOL = 5e-4

HONEST = [
    "These are candidates. The tool cannot know which number is the camera's: many games use 0.75 or",
    "1.3333334 for other things too. Try ONE at a time in PCSX2 (each --out section is one try): the right",
    "one widens the view (more on the sides) instead of stretching or squashing it. Some games need two",
    "values changed together (projection and culling), and 2D menus and HUDs may need their own fix.",
]


def serial_from_name(name):
    m = re.match(r"^([A-Za-z]{4})[_-](\d{3})\.?(\d{2})", name)
    return "%s-%s%s" % (m.group(1).upper(), m.group(2), m.group(3)) if m else None


def pcsx2_crc(data):
    crc = 0
    n = len(data) // 4
    for (w,) in struct.iter_unpack("<I", data[:n * 4]):
        crc ^= w
    return crc


def new_words(elf, h, bits):
    """Instruction words that make a code constant equal `bits`. Returns (words, note)."""
    lui_w = elf.word(h["lui"])
    if h["lo"] is not None:
        lo_w = elf.word(h["lo"])
        hi, lo = split_hi_lo(bits, h["lo_op"])
        return [(h["lui"], (lui_w & 0xFFFF0000) | hi), (h["lo"], (lo_w & 0xFFFF0000) | lo)], None
    note = None
    if bits & 0xFFFF:
        near = (bits + 0x8000) & 0xFFFF0000
        note = "a lone lui cannot hold %s exactly: uses %s" % (fmt(u2f(bits)), fmt(u2f(near)))
        bits = near
    return [(h["lui"], (lui_w & 0xFFFF0000) | (bits >> 16))], note


def candidates(elf, limit):
    targets = [(label, v, v * REL_TOL) for label, v, _ in ASPECTS]
    factor = dict((label, f) for label, _, f in ASPECTS)
    out = []
    for h in search(elf, targets, limit):
        wide_bits = f2u(h["value"] * factor[h["target"]])
        c = dict(h, wide_value=u2f(wide_bits), wide_bits=wide_bits, notes=[])
        if h["kind"] == "data":
            c["words"] = [(h["addr"], wide_bits)]
        else:
            ws, note = new_words(elf, h, wide_bits)
            c["words"] = [(x, w) for x, w in ws if elf.word(x) != w]
            if note:
                c["notes"].append(note)
                c["wide_value"] = u2f(((ws[0][1] & 0xFFFF) << 16))
        c["pnach"] = [pnach_line(1, a, w) for a, w in c["words"]]
        out.append(c)
    return out


def pnach_text(elf, cands):
    lines = ["gametitle=%s widescreen candidates (console-graphics widescreen.py)" % elf.name,
             "comment=Enable ONE section at a time. Each changes one 4:3 value to its 16:9 value.", ""]
    for i, c in enumerate(cands, 1):
        how = "data, %s" % c["where"] if c["kind"] == "data" else "code, %s" % (
            "lui" if c["lo"] is None else "lui+" + c["lo_op"])
        lines.append("[Widescreen try %02d: %s at %s (%s)]" % (i, fmt(c["value"]), hex32(c["addr"]), how))
        lines.append("author=console-graphics widescreen.py")
        lines.append("description=%s %s -> %s. Right if the view widens without stretching." % (
            c["target"], fmt(c["value"]), fmt(c["wide_value"])))
        lines.append("gsaspectratio=16:9")
        lines.extend(c["pnach"])
        lines.append("")
    return "\n".join(lines)


def cmd_scan(a):
    elf = Elf(a.elf)
    cands = candidates(elf, a.limit)
    serial = serial_from_name(elf.name)
    target = ("%s_%08X.pnach" % (serial, pcsx2_crc(elf.data))) if serial else "<SERIAL>_%08X.pnach" % pcsx2_crc(elf.data)
    if a.out:
        if os.path.exists(a.out) and not a.force:
            fail("%s exists: pass --force to overwrite it" % a.out)
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", newline="\n") as f:
            f.write(pnach_text(elf, cands))
    if a.json:
        rows = []
        for c in cands:
            d = hit_json(c)
            d.update(wide_value=fmt(c["wide_value"]), wide_bits=hex32(c["wide_bits"]), pnach=c["pnach"],
                     words=[{"addr": hex32(x), "new": hex32(w)} for x, w in c["words"]], notes=c["notes"])
            rows.append(d)
        print(json.dumps({"elf": elf.name, "pcsx2_pnach": target, "candidates": rows, "honest": " ".join(HONEST),
                          "out": a.out}, indent=2))
        return 0 if cands else 1
    print("Widescreen candidates in %s: %d" % (elf.name, len(cands)))
    for i, c in enumerate(cands, 1):
        if c["kind"] == "data":
            head = "DATA %s  %s" % (hex32(c["addr"]), c["where"])
        else:
            head = "CODE %s  %s -> mtc1 $f%d" % (hex32(c["addr"]), "lui" if c["lo"] is None else "lui+" + c["lo_op"], c["freg"])
        print("\ntry %02d  %s   %s (%s) -> %s" % (i, head, fmt(c["value"]), c["target"], fmt(c["wide_value"])))
        if c["kind"] == "data" and c["read_by"]:
            print("        read by: " + ", ".join("%s at %s" % (x["insn"], hex32(x["at"])) for x in c["read_by"][:4]))
        for line in c["pnach"]:
            print("        " + line)
        for n in c["notes"]:
            print("        note: " + n)
    print("")
    for line in HONEST:
        print(line)
    if a.out:
        print("\nwrote %s (one [section] per try). Copy the sections into PCSX2's %s" % (a.out, target))
    else:
        print("\nWrite them as a pnach with --out console/patches/widescreen-try.pnach (PCSX2 wants %s)." % target)
    print("Found it? Bake it in with /console-mod (pnach.py toml / override), then stretch the port's output "
          "to 16:9 (see reference/widescreen.md).")
    return 0 if cands else 1


def cmd_values(a):
    rows = []
    for label, v, f in ASPECTS:
        rows.append({"label": label, "value": fmt(u2f(f2u(v))), "bits": hex32(f2u(v)),
                     "wide_value": fmt(u2f(f2u(v * f))), "wide_bits": hex32(f2u(v * f)),
                     "rule": "x4/3" if f > 1 else "x3/4"})
    fov = None
    if a.fov is not None:
        if not 0 < a.fov < 180:
            fail("--fov must be an angle between 0 and 180 degrees")
        wide = math.degrees(2 * math.atan(math.tan(math.radians(a.fov) / 2) * WIDE))
        fov = {"horizontal_4_3": a.fov, "horizontal_16_9": round(wide, 4),
               "radians_4_3": round(math.radians(a.fov), 7), "radians_16_9": round(math.radians(wide), 7)}
    if a.json:
        print(json.dumps({"aspects": rows, "fov": fov}, indent=2))
        return 0
    line = "%-18s %-13s %-10s  ->  %-12s %-10s  %s"
    print(line % ("4:3 value", "", "bits", "16:9 value", "bits", "rule"))
    for r in rows:
        print(line % (r["label"], r["value"], r["bits"], r["wide_value"], r["wide_bits"], r["rule"]))
    print("\nWidth/height ratios get x4/3, height/width ratios get x3/4. A horizontal projection scale (focal")
    print("length in x) gets x0.75; a vertical one stays. A horizontal FOV angle a becomes 2*atan(tan(a/2)*4/3).")
    if fov:
        print("\nHorizontal FOV %s deg at 4:3 -> %s deg at 16:9 (%s -> %s radians)" % (
            fmt(a.fov), fov["horizontal_16_9"], fov["radians_4_3"], fov["radians_16_9"]))
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="widescreen.py",
        description="Find a PS2 game's 4:3 aspect and FOV constants and print 16:9 pnach lines to try in PCSX2.",
        epilog="Exit codes: 0 ok, 1 no candidates, 2 bad input.")
    sub = p.add_subparsers(dest="cmd")
    sp = sub.add_parser("scan", help="list aspect/FOV candidates with 16:9 pnach lines")
    sp.add_argument("elf", help="the game's main ELF")
    sp.add_argument("--out", help="write a .pnach with one [section] per candidate")
    sp.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    sp.add_argument("--limit", type=int, default=60, help="most candidates (default 60)")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_scan)
    sp = sub.add_parser("values", help="the 4:3 -> 16:9 values and the FOV rule")
    sp.add_argument("--fov", type=float, help="a horizontal FOV in degrees to convert to 16:9")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_values)
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
