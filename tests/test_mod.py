"""Tests for console-mod: pnach.py and override.py. Fully offline, synthetic fixtures only."""

import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout

from _load import load, ROOT

pnach = load("console-mod", "pnach")
override = load("console-mod", "override")

SKILL = os.path.join(ROOT, "console-mod")

# --------------------------------------------------------------------------
# Synthetic ELF builder (no game data: a few hand-assembled MIPS words)
# --------------------------------------------------------------------------

PF_X, PF_W, PF_R = 1, 2, 4


def words(*ws):
    return b"".join(struct.pack("<I", w) for w in ws)


TEXT = words(
    0x3C02411C,  # 0x00100000 lui  $v0, 0x411C
    0x3442CCCD,  # 0x00100004 ori  $v0, $v0, 0xCCCD
    0x44826000,  # 0x00100008 mtc1 $v0, $f12
    0x03E00008,  # 0x0010000C jr   $ra
    0x00000000,  # 0x00100010 nop
    0x0000000C,  # 0x00100014 syscall
    0x8C820010,  # 0x00100018 lw   $v0, 0x10($a0)
    0x10400003,  # 0x0010001C beq  $v0, $zero, +3
    0x00000000,  # 0x00100020 nop
    0x03E00008,  # 0x00100024 jr   $ra
    0x00000000,  # 0x00100028 nop
    0x42000038,  # 0x0010002C cop0 (ei)
    0x0C040006,  # 0x00100030 jal  0x00100018   (a dispatched call)
    0x00000000,  # 0x00100034 nop
    0x08040009,  # 0x00100038 j    0x00100024   (a tail jump: a direct C++ call in the port)
    0x00000000,  # 0x0010003C nop
)
DATA = words(0x00000064, 0x3F800000, 0x11111111, 0x22222222) + bytes(0x100 - 16)


def make_elf(segments, entry=0x00100000, sections=True, extra=b""):
    """segments: list of (vaddr, data, memsz, flags, name). Returns ELF bytes."""
    phnum = len(segments)
    off = 52 + 32 * phnum
    body = bytearray()
    placed = []
    for vaddr, data, memsz, flags, name in segments:
        pad = (-(off + len(body))) % 16
        body += bytes(pad)
        placed.append(off + len(body))
        body += data
    body += extra
    shdrs = b""
    shoff = 0
    shnum = 0
    shstrndx = 0
    if sections:
        names = b"\0"
        sh = [(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)]
        for (vaddr, data, memsz, flags, name), foff in zip(segments, placed):
            nidx = len(names)
            names += name.encode() + b"\0"
            shf = 2 | (4 if flags & PF_X else 0) | (1 if flags & PF_W else 0)
            sh.append((nidx, 1, shf, vaddr, foff, len(data), 0, 0, 16, 0))
            if memsz > len(data):
                nidx = len(names)
                names += b".bss\0"
                sh.append((nidx, 8, 3, vaddr + len(data), foff + len(data), memsz - len(data), 0, 0, 16, 0))
        nidx = len(names)
        names += b".shstrtab\0"
        stroff = off + len(body)
        body += names
        sh.append((nidx, 3, 0, 0, stroff, len(names), 0, 0, 1, 0))
        pad = (-(off + len(body))) % 4
        body += bytes(pad)
        shoff = off + len(body)
        shdrs = b"".join(struct.pack("<IIIIIIIIII", *s) for s in sh)
        shnum = len(sh)
        shstrndx = len(sh) - 1
    hdr = bytearray(b"\x7fELF" + bytes([1, 1, 1, 0]) + bytes(8))
    hdr += struct.pack("<HHIIIIIHHHHHH", 2, 8, 1, entry, 52, shoff, 0, 52, 32, phnum, 40, shnum, shstrndx)
    ph = b""
    for (vaddr, data, memsz, flags, name), foff in zip(segments, placed):
        ph += struct.pack("<IIIIIIII", 1, foff, vaddr, vaddr, len(data), memsz, flags, 16)
    return bytes(hdr) + ph + bytes(body) + shdrs


def standard_elf(sections=True):
    return make_elf([(0x00100000, TEXT, len(TEXT), PF_R | PF_X, ".text"),
                     (0x00200000, DATA, 0x1000, PF_R | PF_W, ".data")], sections=sections)


def rwx_elf():
    """One read-write-execute segment, no sections: the ambiguous retail layout."""
    blob = TEXT + bytes((0x100 - len(TEXT))) + DATA
    return make_elf([(0x00100000, blob, len(blob) + 0x100, PF_R | PF_W | PF_X, ".x")], sections=False)


PNACH = """gametitle=Synthetic Test Game [SLUS-99999] (U)
comment=Hand-written test patches // not stripped in metadata
author=console-skill tests
gsaspectratio=16:9
gsinterlacemode=1
// a comment line
patch=1,EE,00100018,word,00000000
patch=1,EE,0010001E,byte,00
patch=0,EE,00100004,short,0000

[Data]
description=data writes
patch=0,EE,00200000,word,0000270F
patch=1,EE,00200004,word,3F800000
patch=1,EE,20200008,word,12345678  // uncached mirror of 0x00200008
patch=1,EE,0020000C,beword,AABBCCDD
patch=0,EE,00200010,double,0102030405060708
patch=0,EE,00200018,bytes,DEADBEEF01

[Boot clears]
patch=1,EE,00200100,word,00000063
patch=0,EE,00200200,byte,01
patch=1,EE,01000000,word,00000001

[Unportable]
patch=1,EE,70000010,word,00000001
patch=1,IOP,00012340,word,00000000
"""


def tmpdir(test):
    d = tempfile.mkdtemp(prefix="console_mod_")
    test.addCleanup(shutil.rmtree, d, True)
    return d


def write(path, data):
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as f:
        f.write(data)
    return path


def run_main(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def cli(script, *args, cwd=None):
    return subprocess.run([sys.executable, os.path.join(SKILL, script)] + list(args), cwd=cwd,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)


# --------------------------------------------------------------------------
# Mock PS2Recomp headers for compile tests (the real API's shape, no SSE)
# --------------------------------------------------------------------------

MOCK_GAME_OVERRIDES = r"""
#pragma once
#include <cstdint>
#include <string>
#include <string_view>
class PS2Runtime;
namespace ps2_game_overrides {
    using ApplyFn = void (*)(PS2Runtime &runtime);
    struct Descriptor { const char *name = ""; const char *elfName = ""; uint32_t entry = 0; uint32_t crc32 = 0; ApplyFn apply = nullptr; };
    class AutoRegister { public: explicit AutoRegister(const Descriptor &descriptor); };
    void registerDescriptor(const Descriptor &descriptor);
    bool bindAddressHandler(PS2Runtime &runtime, uint32_t address, std::string_view handlerName);
}
#define PS2_GAME_OVERRIDE_JOIN2(a, b) a##b
#define PS2_GAME_OVERRIDE_JOIN(a, b) PS2_GAME_OVERRIDE_JOIN2(a, b)
#define PS2_REGISTER_GAME_OVERRIDE(NAME_LITERAL, ELF_NAME_LITERAL, ENTRY_U32, CRC32_U32, APPLY_FN) \
    namespace { \
        static const ps2_game_overrides::Descriptor PS2_GAME_OVERRIDE_JOIN(kPs2OverrideDesc_, __LINE__) \
        { NAME_LITERAL, ELF_NAME_LITERAL, static_cast<uint32_t>(ENTRY_U32), static_cast<uint32_t>(CRC32_U32), APPLY_FN }; \
        static const ps2_game_overrides::AutoRegister PS2_GAME_OVERRIDE_JOIN(kPs2OverrideReg_, __LINE__) \
            (PS2_GAME_OVERRIDE_JOIN(kPs2OverrideDesc_, __LINE__)); \
    }
"""

MOCK_RUNTIME = r"""
#pragma once
#include <cstdint>
#include <cstring>
constexpr uint32_t PS2_RAM_SIZE = 32u * 1024u * 1024u;
constexpr uint32_t PS2_RAM_MASK = PS2_RAM_SIZE - 1u;
struct alignas(16) R5900Context { uint32_t r[32][4]; uint32_t pc; float f[32]; };
inline uint32_t getRegU32(const R5900Context *ctx, int reg) { return (reg <= 0 || reg > 31) ? 0u : ctx->r[reg][0]; }
inline void setReturnU32(R5900Context *ctx, uint32_t value) { ctx->r[2][0] = value; }
inline void setReturnS32(R5900Context *ctx, int32_t value) { ctx->r[2][0] = static_cast<uint32_t>(value); }
class PS2Memory { public: uint8_t *getRDRAM() { return m_rdram; } private: uint8_t *m_rdram = nullptr; };
class PS2Runtime {
public:
    using RecompiledFunction = void (*)(uint8_t *, R5900Context *, PS2Runtime *);
    bool replaceFunction(uint32_t address, RecompiledFunction func);
    bool registerFunction(uint32_t address, RecompiledFunction func);
    RecompiledFunction lookupFunction(uint32_t address);
    bool hasFunction(uint32_t address) const;
    inline PS2Memory &memory() { return m_memory; }
private:
    PS2Memory m_memory;
};
"""

MOCK_MACROS = r"""
#pragma once
#include <cstdint>
#include <cstring>
#include "ps2_runtime.h"
static inline uint8_t Ps2FastRead8(const uint8_t *rdram, uint32_t addr) { return rdram[addr & PS2_RAM_MASK]; }
static inline uint16_t Ps2FastRead16(const uint8_t *rdram, uint32_t addr) { uint16_t v; std::memcpy(&v, rdram + (addr & PS2_RAM_MASK), 2); return v; }
static inline uint32_t Ps2FastRead32(const uint8_t *rdram, uint32_t addr) { uint32_t v; std::memcpy(&v, rdram + (addr & PS2_RAM_MASK), 4); return v; }
static inline uint64_t Ps2FastRead64(const uint8_t *rdram, uint32_t addr) { uint64_t v; std::memcpy(&v, rdram + (addr & PS2_RAM_MASK), 8); return v; }
static inline void Ps2FastWrite8(uint8_t *rdram, uint32_t addr, uint8_t value) { rdram[addr & PS2_RAM_MASK] = value; }
static inline void Ps2FastWrite16(uint8_t *rdram, uint32_t addr, uint16_t value) { std::memcpy(rdram + (addr & PS2_RAM_MASK), &value, 2); }
static inline void Ps2FastWrite32(uint8_t *rdram, uint32_t addr, uint32_t value) { std::memcpy(rdram + (addr & PS2_RAM_MASK), &value, 4); }
static inline void Ps2FastWrite64(uint8_t *rdram, uint32_t addr, uint64_t value) { std::memcpy(rdram + (addr & PS2_RAM_MASK), &value, 8); }
"""

CXX = shutil.which("c++") or shutil.which("clang++") or shutil.which("g++")


def compile_ok(test, cpp_text):
    """Compile with -std=c++20 -fsyntax-only against the mock headers. Returns (ok, stderr)."""
    d = tmpdir(test)
    write(os.path.join(d, "game_overrides.h"), MOCK_GAME_OVERRIDES)
    write(os.path.join(d, "ps2_runtime.h"), MOCK_RUNTIME)
    write(os.path.join(d, "ps2_runtime_macros.h"), MOCK_MACROS)
    src = write(os.path.join(d, "mod.cpp"), cpp_text)
    r = subprocess.run([CXX, "-std=c++20", "-fsyntax-only", "-Wall", "-Wextra", "-Werror", "-I", d, src],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
    return r.returncode == 0, r.stderr


class Fixture(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir(self)
        self.elf_bytes = standard_elf()
        self.elf = write(os.path.join(self.d, "SLUS_999.99"), self.elf_bytes)
        self.pcrc = pnach.Elf(self.elf).pcsx2_crc()
        self.pn = write(os.path.join(self.d, "SLUS-99999_%08X.pnach" % self.pcrc), PNACH)


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


class TestParse(unittest.TestCase):
    def parse(self, text):
        return pnach.parse_pnach_text(text)

    def test_metadata_sections_and_comments(self):
        r = self.parse(PNACH)
        self.assertEqual(r["sections"], ["Data", "Boot clears", "Unportable"])
        self.assertEqual(r["meta"]["gametitle"][0]["value"], "Synthetic Test Game [SLUS-99999] (U)")
        self.assertIn("// not stripped", r["meta"]["comment"][0]["value"])
        self.assertEqual(r["meta"]["gsaspectratio"][0]["value"], "16:9")
        self.assertEqual(r["meta"]["gsinterlacemode"][0]["value"], "1")
        self.assertEqual(r["meta"]["description"][0]["section"], "Data")
        self.assertEqual(r["errors"], [])

    def test_all_types_to_bytes(self):
        r = self.parse(PNACH)
        by_line = dict((w["line"], w) for w in r["writes"])
        self.assertEqual(by_line[7]["data"], bytes(4))                       # word
        self.assertEqual(by_line[8]["data"], b"\x00")                        # byte
        self.assertEqual(by_line[9]["data"], b"\x00\x00")                    # short
        self.assertEqual(by_line[13]["data"], bytes.fromhex("0F270000"))     # word LE
        self.assertEqual(by_line[16]["data"], bytes.fromhex("AABBCCDD"))     # beword: memory order
        self.assertEqual(by_line[17]["data"], bytes.fromhex("0807060504030201"))  # double LE
        self.assertEqual(by_line[18]["data"], bytes.fromhex("DEADBEEF01"))   # bytes
        self.assertEqual(by_line[15]["addr"], 0x20200008)
        self.assertEqual(by_line[13]["place"], 0)
        self.assertEqual(by_line[14]["place"], 1)

    def test_be_variants(self):
        r = self.parse("patch=1,EE,00200000,beshort,1234\npatch=1,EE,00200008,bedouble,0102030405060708\n")
        self.assertEqual(r["writes"][0]["data"], b"\x12\x34")
        self.assertEqual(r["writes"][1]["data"], bytes(range(1, 9)))

    def test_spaces_case_and_place3(self):
        r = self.parse("PATCH = 3 , ee , 0x00200000 , WORD , 0x1\n")
        self.assertEqual(len(r["writes"]), 1)
        self.assertEqual(r["writes"][0]["place"], 3)
        self.assertEqual(r["writes"][0]["data"], b"\x01\0\0\0")

    def test_value_too_wide_is_truncated_with_note(self):
        r = self.parse("patch=1,EE,00200000,byte,1FF\n")
        self.assertEqual(r["writes"][0]["data"], b"\xff")
        self.assertIn("wider", r["writes"][0]["notes"][0])

    def test_bad_lines_are_reported_not_fatal(self):
        r = self.parse("patch=1,EE,00200000,word\npatch=9,EE,0,word,0\npatch=1,GS,0,word,0\n"
                       "patch=1,EE,zz,word,0\npatch=1,EE,0,quad,0\npatch=1,EE,0,bytes,ABC\njunk line\n"
                       "patch=1,EE,00200000,word,1\n")
        self.assertEqual(len(r["errors"]), 7)
        self.assertEqual(len(r["writes"]), 1)
        self.assertEqual([e["line"] for e in r["errors"]], [1, 2, 3, 4, 5, 6, 7])

    def test_iop_and_dpatch_are_manual(self):
        r = self.parse("patch=1,IOP,00012340,word,0\ndpatch=0,1,1,0,0,word,0,0\n")
        self.assertEqual(r["writes"], [])
        self.assertIn("IOP", r["manual"][0]["reason"])
        self.assertIn("dpatch", r["manual"][1]["reason"])

    def test_extended_plain_writes(self):
        r = self.parse("patch=1,EE,20200010,extended,0000ABCD\npatch=1,EE,10200014,extended,00001234\n"
                       "patch=1,EE,00200016,extended,000000FF\n")
        self.assertEqual([(w["addr"], w["data"]) for w in r["writes"]],
                         [(0x00200010, b"\xcd\xab\0\0"), (0x00200014, b"\x34\x12"), (0x00200016, b"\xff")])
        self.assertIn("32-bit write", r["writes"][0]["extended"])

    def test_extended_two_line_codes_consume_their_second_line(self):
        # 4 = serial write: its second line starts with 0 and must NOT become an 8-bit write.
        r = self.parse("patch=1,EE,40200020,extended,00040001\npatch=1,EE,00000005,extended,00000001\n"
                       "patch=1,EE,50200000,extended,00000010\npatch=1,EE,00300000,extended,00000000\n"
                       "patch=1,EE,30400000,extended,00200000\npatch=1,EE,00000001,extended,00000000\n"
                       "patch=1,EE,20200040,extended,00000001\n")
        self.assertEqual(len(r["writes"]), 1)
        self.assertEqual(r["writes"][0]["addr"], 0x00200040)
        reasons = [m["reason"] for m in r["manual"]]
        self.assertIn("serial", reasons[0])
        self.assertIn("second line", reasons[1])
        self.assertIn("copy", reasons[2])
        self.assertIn("second line", reasons[3])
        self.assertIn("increment", reasons[4])
        self.assertIn("second line", reasons[5])

    def test_extended_pointer_code_multi_line(self):
        # 6-code with 3 pointer levels: line 2 holds the first offset, one more line for offsets 2 and 3.
        r = self.parse("patch=1,EE,60200030,extended,000000FF\npatch=1,EE,00020003,extended,00000010\n"
                       "patch=1,EE,00000020,extended,00000030\npatch=1,EE,20200044,extended,00000002\n")
        self.assertEqual([w["addr"] for w in r["writes"]], [0x00200044])
        self.assertEqual(len(r["manual"]), 3)
        self.assertIn("pointer", r["manual"][0]["reason"])

    def test_extended_single_line_inc_dec_and_bitwise(self):
        r = self.parse("patch=1,EE,30000001,extended,00200000\npatch=1,EE,70200000,extended,00100001\n"
                       "patch=1,EE,20200048,extended,00000003\n")
        self.assertEqual([w["addr"] for w in r["writes"]], [0x00200048])
        self.assertEqual(len(r["manual"]), 2)

    def test_conditionals_gate_following_writes(self):
        text = ("[C]\npatch=1,EE,C0200050,extended,00000001\npatch=1,EE,20200054,extended,00000002\n"
                "patch=1,EE,20200058,extended,00000002\n"
                "[D]\npatch=1,EE,D0200060,extended,00000001\npatch=1,EE,20200064,extended,00000002\n"
                "patch=1,EE,20200068,extended,00000003\n"
                "[E]\npatch=1,EE,E0020000,extended,00200070\npatch=1,EE,20200074,extended,00000001\n"
                "patch=1,EE,20200078,extended,00000001\npatch=1,EE,2020007C,extended,00000001\n")
        r = self.parse(text)
        self.assertEqual(sorted(w["addr"] for w in r["writes"]), [0x00200068, 0x0020007C])
        gated = [m for m in r["manual"] if "only runs when" in m["reason"]]
        self.assertEqual(len(gated), 5)

    def test_unknown_extended_command(self):
        r = self.parse("patch=1,EE,F0100000,extended,00000000\n")
        self.assertIn("unsupported", r["manual"][0]["reason"])

    def test_section_filter(self):
        d = tmpdir(self)
        p = write(os.path.join(d, "x.pnach"), PNACH)
        res = pnach.load_pnach(p, ["Data"])
        self.assertTrue(all(w["section"] == "Data" for w in res["writes"]))
        self.assertEqual(len(res["writes"]), 6)
        with self.assertRaises(pnach.UsageError):
            pnach.load_pnach(p, ["Nope"])

    def test_binary_file_refused(self):
        d = tmpdir(self)
        p = write(os.path.join(d, "x.pnach"), b"\x7fELF\0\0\0binary")
        with self.assertRaises(pnach.UsageError):
            pnach.load_pnach(p)


# --------------------------------------------------------------------------
# ELF reader and CRCs
# --------------------------------------------------------------------------


class TestElfAndCrc(Fixture):
    def test_reads_segments_and_sections(self):
        e = pnach.Elf(self.elf)
        self.assertEqual(e.entry, 0x00100000)
        self.assertEqual(len(e.segments), 2)
        self.assertEqual([s["name"] for s in e.sections], [".text", ".data", ".bss"])
        self.assertEqual(e.read_ram(0x00100018, 4), struct.pack("<I", 0x8C820010))
        self.assertIsNone(e.read_ram(0x00200100, 4))  # BSS has no file bytes

    def test_two_crcs_differ_and_are_right(self):
        e = pnach.Elf(self.elf)
        xor = 0
        for i in range(0, len(self.elf_bytes) // 4 * 4, 4):
            xor ^= struct.unpack_from("<I", self.elf_bytes, i)[0]
        self.assertEqual(e.pcsx2_crc(), xor)
        self.assertEqual(e.crc32(), zlib.crc32(self.elf_bytes) & 0xFFFFFFFF)
        self.assertNotEqual(e.pcsx2_crc(), e.crc32())

    def test_pcsx2_crc_ignores_trailing_bytes(self):
        d = tmpdir(self)
        a = write(os.path.join(d, "a"), self.elf_bytes)
        b = write(os.path.join(d, "b"), self.elf_bytes + b"\x01\x02\x03")
        self.assertEqual(pnach.Elf(a).pcsx2_crc(), pnach.Elf(b).pcsx2_crc())
        self.assertNotEqual(pnach.Elf(a).crc32(), pnach.Elf(b).crc32())

    def test_crc_command(self):
        code, out, _ = run_main(pnach, ["crc", self.elf, "--json"])
        self.assertEqual(code, 0)
        info = json.loads(out)
        self.assertEqual(info["serial"], "SLUS-99999")
        self.assertEqual(info["pnach"], "SLUS-99999_%08X.pnach" % self.pcrc)
        self.assertEqual(info["crc32"], "%08X" % (zlib.crc32(self.elf_bytes) & 0xFFFFFFFF))
        code, out, _ = run_main(pnach, ["crc", self.elf])
        self.assertIn("PCSX2 CRC", out)
        self.assertIn("SLUS-99999_%08X.pnach" % self.pcrc, out)

    def test_crc_serial_override_and_unknown(self):
        d = tmpdir(self)
        p = write(os.path.join(d, "game.elf"), self.elf_bytes)
        code, out, _ = run_main(pnach, ["crc", p, "--serial", "SLES-12345", "--json"])
        self.assertTrue(json.loads(out)["pnach"].startswith("SLES-12345_"))
        code, out, _ = run_main(pnach, ["crc", p])
        self.assertIn("unknown", out)

    def test_name_helpers(self):
        self.assertEqual(pnach.serial_from_name("SLUS_203.12"), "SLUS-20312")
        self.assertEqual(pnach.serial_from_name("SCES_123.45;1"), "SCES-12345")
        self.assertIsNone(pnach.serial_from_name("main.elf"))
        self.assertEqual(pnach.crc_from_pnach_name("SLUS-20312_1A2B3C4D.pnach"), 0x1A2B3C4D)
        self.assertEqual(pnach.crc_from_pnach_name("1a2b3c4d.PNACH"), 0x1A2B3C4D)
        self.assertIsNone(pnach.crc_from_pnach_name("cheats.pnach"))

    def test_bad_elves_exit_2_without_traceback(self):
        d = tmpdir(self)
        cases = {
            "short": b"\x7fELF",
            "magic": b"NOPE" + bytes(60),
            "class64": b"\x7fELF\x02\x01" + bytes(60),
            "bigendian": b"\x7fELF\x01\x02" + bytes(60),
            "x86": self.elf_bytes[:18] + struct.pack("<H", 3) + self.elf_bytes[20:],
            "truncated": self.elf_bytes[:120],
        }
        for name, data in cases.items():
            p = write(os.path.join(d, name), data)
            code, out, err = run_main(pnach, ["crc", p])
            self.assertEqual(code, 2, name)
            self.assertIn("error:", err)
            self.assertNotIn("Traceback", err)
        code, _, err = run_main(pnach, ["crc", os.path.join(d, "missing")])
        self.assertEqual(code, 2)
        self.assertIn("not found", err)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


class TestClassify(Fixture):
    def classify(self, text, elf=None, funcs=None, sections=None):
        p = write(os.path.join(self.d, "t.pnach"), text)
        pn = pnach.load_pnach(p, sections)
        return pnach.classify(pn, pnach.Elf(elf or self.elf), funcs)

    def test_every_class(self):
        res = self.classify(PNACH)
        cls = dict((it["write"]["line"], it["class"]) for it in res["items"])
        self.assertEqual(cls[7], "CODE")
        self.assertEqual(cls[8], "CODE")
        self.assertEqual(cls[9], "CODE")
        for ln in (13, 14, 15, 16, 17, 18):
            self.assertEqual(cls[ln], "DATA", ln)
        self.assertEqual(cls[21], "BSS")
        self.assertEqual(cls[22], "BSS")
        self.assertEqual(cls[23], "HEAP")
        manual = dict((m["line"], m["reason"]) for m in res["manual"])
        self.assertIn("scratchpad", manual[26])
        self.assertIn("IOP", manual[27])

    def test_byte_and_short_merge_into_original_word(self):
        res = self.classify(PNACH)
        words_ = dict((cw["vaddr"], cw) for cw in res["code"])
        self.assertEqual(words_[0x0010001C]["original"], 0x10400003)
        self.assertEqual(words_[0x0010001C]["new"], 0x10000003)     # beq $v0 -> beq $zero: always branch
        self.assertEqual(words_[0x00100004]["new"], 0x34420000)     # ori low half cleared
        self.assertEqual(words_[0x00100018]["new"], 0)
        self.assertEqual(pnach.describe(0x10000003, 0x0010001C), "beq $zero, $zero, 0x0010002C")
        self.assertEqual(pnach.describe(0x10400003), "beq $v0, $zero, +16")
        self.assertEqual(pnach.value_text(bytes.fromhex("0F270000")), "0x0000270F")
        self.assertEqual(pnach.value_text(bytes.fromhex("DEADBEEF01")), "DEADBEEF01")

    def test_two_writes_same_word_last_wins(self):
        res = self.classify("patch=1,EE,00100018,byte,11\npatch=1,EE,00100019,byte,22\npatch=1,EE,00100018,byte,33\n")
        self.assertEqual(len(res["code"]), 1)
        self.assertEqual(res["code"][0]["new"], 0x8C822233)
        self.assertEqual(res["code"][0]["lines"], [1, 2, 3])

    def test_double_write_in_code_spans_two_words(self):
        res = self.classify("patch=1,EE,00100018,double,0000000000000000\n")
        self.assertEqual([cw["vaddr"] for cw in res["code"]], [0x00100018, 0x0010001C])

    def test_write_straddling_data_and_bss_is_split(self):
        res = self.classify("patch=1,EE,000FFFFE,word,AABBCCDD\npatch=1,EE,002000FE,word,11223344\n")
        classes = [(it["class"], it["ram"], len(it["data"])) for it in res["items"]]
        self.assertIn(("HEAP", 0x000FFFFE, 2), classes)
        self.assertIn(("CODE", 0x00100000, 2), classes)
        self.assertIn(("DATA", 0x002000FE, 2), classes)
        self.assertIn(("BSS", 0x00200100, 2), classes)

    def test_kseg_addresses_normalise(self):
        res = self.classify("patch=1,EE,80200000,word,1\npatch=1,EE,A0100018,word,0\n")
        self.assertEqual(res["items"][0]["class"], "DATA")
        self.assertEqual(res["items"][0]["ram"], 0x00200000)
        self.assertEqual(res["code"][0]["vaddr"], 0x00100018)

    def test_hardware_register_is_outside(self):
        res = self.classify("patch=1,EE,1000F000,word,1\npatch=1,EE,12000000,word,1\n")
        self.assertEqual(res["items"], [])
        self.assertIn("hardware registers", res["manual"][0]["reason"])
        self.assertIn("GS", res["manual"][1]["reason"])

    def test_syscall_and_cop0_originals_flag_toml_gates(self):
        res = self.classify("patch=1,EE,00100014,word,00000000\npatch=1,EE,0010002C,word,00000000\n")
        notes = [" ".join(cw["notes"]) for cw in res["code"]]
        self.assertIn("patch_syscalls = true", notes[0])
        self.assertIn("patch_cop0 = true", notes[1])

    def test_no_change_noted(self):
        res = self.classify("patch=1,EE,00100018,word,8C820010\n")
        self.assertIn("no change", res["code"][0]["notes"][0])

    def test_cave_jal_into_new_code_on_heap(self):
        text = ("[Cave]\npatch=1,EE,01F00000,word,24020001\npatch=1,EE,01F00004,word,03E00008\n"
                "patch=1,EE,01F00008,word,00000000\npatch=1,EE,00100020,word,0C7C0000\n"
                "[Fine]\npatch=1,EE,00100018,word,00000000\n")
        res = self.classify(text)
        caves = [m for m in res["manual"] if m["reason"].startswith("CAVE")]
        self.assertEqual(sorted(m["line"] for m in caves), [2, 3, 4, 5])
        self.assertEqual([cw["vaddr"] for cw in res["code"]], [0x00100018])

    def test_cave_function_body_written_into_data(self):
        res = self.classify("patch=1,EE,00200020,word,24020001\npatch=1,EE,00200024,word,03E00008\n")
        self.assertTrue(all(it["class"] == "CAVE" for it in res["items"]))

    def test_jump_into_data_is_a_cave(self):
        res = self.classify("patch=1,EE,00100020,word,08080000\n")   # j 0x00200000
        self.assertEqual(res["code"], [])
        self.assertIn("outside the game's code", res["manual"][0]["reason"])

    def test_jump_to_existing_code_is_fine(self):
        res = self.classify("patch=1,EE,00100020,word,0C040000\n")   # jal 0x00100000
        self.assertEqual(len(res["code"]), 1)
        self.assertEqual(res["manual"], [])

    def test_ghidra_map_refines_rwx_segment(self):
        e = write(os.path.join(self.d, "RWX.ELF"), rwx_elf())
        # Without a map: the RWX, section-less segment makes code detection low confidence.
        res = self.classify("patch=1,EE,00100018,word,0\npatch=1,EE,00100100,word,5\n", elf=e)
        self.assertTrue(all(it["class"] == "CODE" and it["confidence"] == "low" for it in res["items"]))
        self.assertEqual(pnach.write_bucket(res["items"][1]), ["load", "every"])
        # With a map: only the function range is code.
        m = write(os.path.join(self.d, "map.csv"), "Name,Start,End,Size\nsub_00100000,0x00100000,0x00100030,48\n")
        res = self.classify("patch=1,EE,00100018,word,0\npatch=1,EE,00100100,word,5\n", elf=e, funcs=pnach.FuncMap(m))
        self.assertEqual([it["class"] for it in res["items"]], ["CODE", "DATA"])

    def test_map_flags_code_outside_functions_as_cave(self):
        m = write(os.path.join(self.d, "map.csv"), "Name,Start,End,Size\nsub_00100000,0x00100000,0x00100010,16\n")
        res = self.classify("patch=1,EE,00100018,word,0\npatch=1,EE,00100004,word,0\n", funcs=pnach.FuncMap(m))
        self.assertEqual([cw["vaddr"] for cw in res["code"]], [0x00100004])
        self.assertIn("no function in the Ghidra map", res["manual"][0]["reason"])
        self.assertTrue(res["manual"][0]["reason"].startswith("CAVE"))

    def test_kernel_memory_is_outside(self):
        res = self.classify("patch=1,EE,00012340,word,1\npatch=1,EE,80001000,word,1\n")
        self.assertEqual(res["items"], [])
        self.assertTrue(all("kernel" in m["reason"] for m in res["manual"]))

    def test_bad_map(self):
        m = write(os.path.join(self.d, "bad.csv"), "Name,Start,End,Size\nfoo,zz,yy,1\n")
        with self.assertRaises(pnach.UsageError):
            pnach.FuncMap(m)

    def test_section_less_elf_uses_segment_flags(self):
        e = write(os.path.join(self.d, "NOSEC.ELF"), standard_elf(sections=False))
        res = self.classify("patch=1,EE,00100018,word,0\npatch=1,EE,00200000,word,1\npatch=1,EE,00200800,word,1\n", elf=e)
        self.assertEqual([it["class"] for it in res["items"]], ["CODE", "DATA", "BSS"])
        self.assertEqual(res["items"][0]["confidence"], "medium")

    def test_buckets_follow_place(self):
        res = self.classify(PNACH)
        b = dict((it["write"]["line"], pnach.write_bucket(it)) for it in res["items"])
        self.assertEqual(b[13], ["load"])
        self.assertEqual(b[14], ["load", "every"])
        self.assertEqual(b[21], ["every"])
        self.assertEqual(b[22], ["first"])
        self.assertEqual(b[23], ["every"])
        self.assertEqual(b[7], [])


# --------------------------------------------------------------------------
# Commands: classify, toml, override
# --------------------------------------------------------------------------


class TestCommands(Fixture):
    def test_parse_command(self):
        code, out, _ = run_main(pnach, ["parse", self.pn])
        self.assertEqual(code, 1)  # IOP and scratchpad lines need attention
        self.assertIn("[Data]", out)
        self.assertIn("Needs a manual port", out)
        code, out, _ = run_main(pnach, ["parse", self.pn, "--json", "--section", "Data"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["writes"]), 6)

    def test_classify_command_text_and_json(self):
        code, out, _ = run_main(pnach, ["classify", self.pn, "--elf", self.elf])
        self.assertEqual(code, 1)
        self.assertIn("matches the file name", out)
        self.assertIn("CODE 3", out)
        code, out, _ = run_main(pnach, ["classify", self.pn, "--elf", self.elf, "--json", "--section", "Data"])
        self.assertEqual(code, 0)
        j = json.loads(out)
        self.assertTrue(j["crc_match"])
        self.assertEqual(j["counts"], {"DATA": 6})

    def test_crc_mismatch_refused_unless_ignored(self):
        bad = write(os.path.join(self.d, "SLUS-99999_00000000.pnach"), PNACH)
        code, _, err = run_main(pnach, ["classify", bad, "--elf", self.elf])
        self.assertEqual(code, 2)
        self.assertIn("addresses will not line up", err)
        code, out, _ = run_main(pnach, ["classify", bad, "--elf", self.elf, "--ignore-crc"])
        self.assertEqual(code, 1)
        self.assertIn("DOES NOT match", out)

    def test_toml_blocks(self):
        code, out, _ = run_main(pnach, ["toml", self.pn, "--elf", self.elf])
        self.assertEqual(code, 1)
        blocks = re.findall(r'\[\[patches\.instructions\]\]\naddress = "(0x[0-9A-F]{8})"\nvalue = "(0x[0-9A-F]{8})"', out)
        self.assertEqual(blocks, [("0x00100004", "0x34420000"), ("0x00100018", "0x00000000"),
                                  ("0x0010001C", "0x10000003")])
        self.assertIn("original 0x10400003 (beq $v0, $zero, 0x0010002C) -> new 0x10000003 (beq $zero, $zero, 0x0010002C)", out)
        self.assertIn("line 7: patch=1,EE,00100018,word,00000000", out)
        self.assertIn("pnach.py override", out)
        try:
            import tomllib
        except ImportError:
            tomllib = None
        if tomllib:
            doc = tomllib.loads(out)
            self.assertEqual(len(doc["patches"]["instructions"]), 3)
            for p in doc["patches"]["instructions"]:
                int(p["address"], 0), int(p["value"], 0)

    def test_toml_gate_note_and_out_file(self):
        p = write(os.path.join(self.d, "g.pnach"), "patch=1,EE,00100014,word,00000000\n")
        out = os.path.join(self.d, "o", "patches.toml")
        code, _, err = run_main(pnach, ["toml", p, "--elf", self.elf, "--out", out])
        self.assertEqual(code, 0)
        text = open(out).read()
        self.assertIn("patch_syscalls = true", text)
        code, _, err = run_main(pnach, ["toml", p, "--elf", self.elf, "--out", out])
        self.assertEqual(code, 2)
        self.assertIn("--force", err)

    def test_toml_without_code(self):
        p = write(os.path.join(self.d, "d.pnach"), "patch=1,EE,00200000,word,1\n")
        code, out, _ = run_main(pnach, ["toml", p, "--elf", self.elf])
        self.assertIn("No code patches", out)
        self.assertNotIn("\n[[patches.instructions]]\n", out)

    def override_text(self, hook=None, sections=None, name=None):
        pn = pnach.load_pnach(self.pn, sections)
        e = pnach.Elf(self.elf)
        res = pnach.classify(pn, e)
        return pnach.override_text(pn, e, res, hook, name or "pnach test", pnach.slugify(name or "pnach test"))

    def test_override_contents_with_frame_hook(self):
        text = self.override_text(hook=0x00100018)
        e = pnach.Elf(self.elf)
        self.assertIn('PS2_REGISTER_GAME_OVERRIDE("pnach test", "SLUS_999.99", 0x00100000u, 0x%08Xu, &applyOverride)'
                      % e.crc32(), text)
        self.assertIn("Ps2FastWrite32(rdram, 0x00200000u, 0x0000270Fu);", text)
        self.assertIn("Ps2FastWrite32(rdram, 0x00200008u, 0x12345678u);", text)     # mirror normalised
        self.assertIn("Ps2FastWrite32(rdram, 0x0020000Cu, 0xDDCCBBAAu);", text)     # beword as LE word
        self.assertIn("Ps2FastWrite64(rdram, 0x00200010u, 0x0102030405060708ULL);", text)
        self.assertIn("kBytes[] = {0xDE, 0xAD, 0xBE, 0xEF, 0x01}", text)
        self.assertIn("constexpr uint32_t kFrameHookAddress = 0x00100018u;", text)
        self.assertIn("runtime.lookupFunction(kFrameHookAddress)", text)
        self.assertIn("runtime.replaceFunction(kFrameHookAddress, &frameHook);", text)
        self.assertIn("if (current != &frameHook)", text)
        self.assertNotIn("0x00100004u", text)    # code patches stay in the TOML
        self.assertIsNone(re.search(r"@[A-Z][A-Z0-9_]*@", text))
        every = text.split("void applyEveryFrameWrites")[1].split("constexpr")[0]
        self.assertIn("0x00200100u", every)       # BSS place 1
        self.assertIn("0x01000000u", every)       # heap place 1
        self.assertIn("0x00200004u", every)       # data place 1 also every frame
        first = text.split("void applyFirstFrameWrites")[1].split("void applyEveryFrameWrites")[0]
        self.assertIn("Ps2FastWrite8(rdram, 0x00200200u, 0x01u);", first)
        self.assertIn("Not ported (2", text)

    def test_override_without_hook_warns(self):
        text = self.override_text(hook=None)
        self.assertIn("WARNING", text)
        self.assertIn("[[maybe_unused]] void applyEveryFrameWrites", text)
        self.assertNotIn("frameHook", text)

    @unittest.skipUnless(CXX, "no C++ compiler")
    def test_override_compiles_against_mock_headers(self):
        for hook in (None, 0x00100018):
            ok, err = compile_ok(self, self.override_text(hook=hook))
            self.assertTrue(ok, err)

    @unittest.skipUnless(CXX, "no C++ compiler")
    def test_two_overrides_compile_in_one_unity_file(self):
        a = self.override_text(hook=0x00100018, name="mod one")
        b = self.override_text(hook=0x00100018, name="mod two")
        ok, err = compile_ok(self, a + "\n" + b)
        self.assertTrue(ok, err)

    def test_override_command_writes_files(self):
        out = os.path.join(self.d, "console", "patches", "inf.cpp")
        code, so, _ = run_main(pnach, ["override", self.pn, "--elf", self.elf, "--frame-hook", "0x00100018",
                                       "--out", out, "--name", "Infinite things"])
        self.assertEqual(code, 1)
        self.assertTrue(os.path.isfile(out))
        cm = os.path.join(self.d, "console", "patches", "console_mods.cmake")
        self.assertTrue(os.path.isfile(cm))
        self.assertEqual(open(cm).read(), open(os.path.join(SKILL, "templates", "console_mods.cmake")).read())
        self.assertIn("namespace console_mod_infinite_things", open(out).read())
        self.assertIn("install:", so)
        code, _, err = run_main(pnach, ["override", self.pn, "--elf", self.elf, "--out", out])
        self.assertEqual(code, 2)
        self.assertIn("--force", err)
        code, so, _ = run_main(pnach, ["override", self.pn, "--elf", self.elf, "--out", out, "--force"])
        self.assertIn("WARNING", so)
        self.assertIn("already next to it", so)

    def test_override_default_path(self):
        code, so, _ = run_main(pnach, ["override", self.pn, "--elf", self.elf, "--section", "Data",
                                       "--out", os.path.join(self.d, "console", "patches", "x.cpp")])
        # place 1 writes with no --frame-hook are written but not wired up: exit 1, with the warning
        self.assertEqual(code, 1)
        self.assertIn("need --frame-hook", so)
        code, so, _ = run_main(pnach, ["override", self.pn, "--elf", self.elf, "--section", "Data",
                                       "--frame-hook", "0x00100000", "--force",
                                       "--out", os.path.join(self.d, "console", "patches", "x.cpp")])
        self.assertNotIn("need --frame-hook", so)

    def test_override_refuses_runner_and_bad_hook(self):
        runner = os.path.join(self.d, "PS2Recomp", "ps2xRuntime", "src", "runner", "mod.cpp")
        code, _, err = run_main(pnach, ["override", self.pn, "--elf", self.elf, "--out", runner])
        self.assertEqual(code, 2)
        self.assertIn("runner", err)
        for hook in ("0x00200000", "0x00100002", "zz"):
            code, _, err = run_main(pnach, ["override", self.pn, "--elf", self.elf, "--frame-hook", hook,
                                            "--out", os.path.join(self.d, "h.cpp")])
            self.assertEqual(code, 2, hook)


# --------------------------------------------------------------------------
# override.py
# --------------------------------------------------------------------------


class TestReachability(Fixture):
    """replaceFunction only sees table calls: jal yes, a j tail jump no (control_flow_emitter.cpp)."""

    def test_call_sites(self):
        e = pnach.Elf(self.elf)
        self.assertEqual(pnach.call_sites(e, 0x00100018), {"jal": [0x00100030], "j": []})
        self.assertEqual(pnach.call_sites(e, 0x00100024), {"jal": [], "j": [0x00100038]})
        self.assertEqual(pnach.call_sites(e, 0x00100000), {"jal": [], "j": []})
        ok = override.ElfKeys(self.elf)
        self.assertEqual(ok.call_sites(0x00100024), {"jal": [], "j": [0x00100038]})

    def test_frame_hook_reached_only_by_j_warns(self):
        p = write(os.path.join(self.d, "d.pnach"), "patch=1,EE,00200000,word,1\n")
        out = os.path.join(self.d, "h.cpp")
        code, so, _ = run_main(pnach, ["override", p, "--elf", self.elf, "--frame-hook", "0x00100024", "--out", out])
        self.assertEqual(code, 1)
        self.assertIn("one level up", so)
        self.assertIn("0 jal and 1 j", open(out).read())
        code, so, _ = run_main(pnach, ["override", p, "--elf", self.elf, "--frame-hook", "0x00100018",
                                       "--out", out, "--force"])
        self.assertEqual(code, 0)
        self.assertIn("1 jal and 0 j", so)
        self.assertIn("ps2EntryRunner executable", so)

    def test_override_py_warns_about_tail_jumps(self):
        out = os.path.join(self.d, "w.cpp")
        code, so, _ = run_main(override, ["new", "--name", "w", "--elf", self.elf, "--wrap", "0x00100024=w",
                                          "--out", out])
        self.assertEqual(code, 1)
        self.assertIn("tail jump", so)
        self.assertIn("one level up", so)
        code, so, _ = run_main(override, ["new", "--name", "r", "--elf", self.elf, "--replace", "0x00100018=r",
                                          "--out", os.path.join(self.d, "r.cpp")])
        self.assertEqual(code, 0, so)
        self.assertIn("reached by 1 jal and 0 j", open(os.path.join(self.d, "r.cpp")).read())


class TestOverridePy(Fixture):
    def repo(self):
        r = os.path.join(self.d, "PS2Recomp")
        os.makedirs(os.path.join(r, "ps2xRuntime", "include"), exist_ok=True)
        write(os.path.join(r, "ps2xRuntime", "include", "ps2_call_list.h"),
              "#pragma once\n#define PS2_SYSCALL_LIST(X) \\\n    X(FlushCache) \\\n    X(_ResetEE)\n\n"
              "#define PS2_STUB_LIST(X) \\\n    X(ret0) \\\n    X(sceCdRead)\n")
        return r

    def new(self, *extra):
        out = os.path.join(self.d, "console", "patches", "gold.cpp")
        return run_main(override, ["new", "--name", "Gold rifle", "--elf", self.elf, "--out", out] + list(extra)), out

    def test_new_bind_replace_wrap(self):
        (code, so, _), out = self.new("--bind", "0x00100018=sceCdRead", "--replace", "0x00100000=gold_damage",
                                      "--wrap", "0x0010001C=gold_ammo", "--repo", self.repo())
        self.assertEqual(code, 0, so)
        text = open(out).read()
        e = pnach.Elf(self.elf)
        self.assertIn('PS2_REGISTER_GAME_OVERRIDE("Gold rifle", "SLUS_999.99", 0x00100000u, 0x%08Xu, &applyOverride)'
                      % e.crc32(), text)
        self.assertIn('ps2_game_overrides::bindAddressHandler(runtime, 0x00100018u, "sceCdRead");', text)
        self.assertIn("void gold_damage(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)", text)
        self.assertIn("getRegU32(ctx, 4)", text)
        self.assertIn("ctx->pc = getRegU32(ctx, 31);", text)
        self.assertIn("runtime.replaceFunction(0x00100000u, &gold_damage)", text)
        self.assertIn("g_original_gold_ammo = current;", text)
        self.assertIn("if (ctx->pc != returnAddress)", text)
        self.assertIn("namespace console_mod_gold_rifle", text)
        code_only = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("//"))
        self.assertNotIn("Runtime::OverrideFunction", code_only)
        self.assertNotIn("Runtime.h", code_only)
        self.assertIsNone(re.search(r"@[A-Z_]+@", text))

    @unittest.skipUnless(CXX, "no C++ compiler")
    def test_new_compiles_against_mock_headers(self):
        (code, so, _), out = self.new("--bind", "0x00100018=ret0", "--replace", "0x00100000=gold_damage",
                                      "--wrap", "0x0010001C=gold_ammo")
        ok, err = compile_ok(self, open(out).read())
        self.assertTrue(ok, err)

    def test_handler_alias_and_unknown(self):
        (code, so, _), out = self.new("--bind", "0x00100018=ResetEE", "--repo", self.repo())
        self.assertEqual(code, 0)
        self.assertIn('"_ResetEE"', open(out).read())
        (code, _, err), _ = self.new("--bind", "0x00100018=NotAHandler", "--repo", self.repo(), "--force")
        self.assertEqual(code, 2)
        self.assertIn("PS2_SYSCALL_LIST", err)

    def test_unchecked_handler_warns(self):
        (code, so, _), _ = self.new("--bind", "0x00100018=SomethingElse")
        self.assertEqual(code, 1)
        self.assertIn("not checked", so)

    def test_bad_arguments(self):
        bad = [
            [],
            ["--replace", "00100000"],
            ["--replace", "0x00100002=f"],
            ["--replace", "0x00100000=9bad"],
            ["--replace", "0x00100000=class"],
            ["--replace", "0x00100000=f", "--wrap", "0x00100004=f"],
            ["--replace", "0x00100000=f", "--wrap", "0x00100000=g"],
            ["--replace", "zz=f"],
        ]
        for extra in bad:
            (code, _, err), _ = self.new(*extra)
            self.assertEqual(code, 2, extra)
            self.assertNotIn("Traceback", err)

    def test_address_warnings(self):
        m = write(os.path.join(self.d, "map.csv"), "Name,Start,End,Size\nsub_00100000,0x00100000,0x00100030,48\n")
        (code, so, _), _ = self.new("--replace", "0x00200000=f", "--map", m)
        self.assertEqual(code, 1)
        self.assertIn("not inside an executable segment", so)
        self.assertIn("not a function start", so)

    def test_refuses_runner_and_existing(self):
        runner = os.path.join(self.d, "x", "ps2xRuntime", "src", "runner", "m.cpp")
        code, _, err = run_main(override, ["new", "--name", "x", "--elf", self.elf, "--replace", "0x00100000=f",
                                           "--out", runner])
        self.assertEqual(code, 2)
        code, _, err = run_main(override, ["new", "--name", "x", "--elf", self.elf, "--replace", "0x00100000=f",
                                           "--out", os.path.join(self.d, "m.h")])
        self.assertEqual(code, 2)
        (code, _, _), out = self.new("--replace", "0x00100000=f")
        (code, _, err), out = self.new("--replace", "0x00100000=f")
        self.assertEqual(code, 2)

    def test_keys(self):
        code, out, _ = run_main(override, ["keys", self.elf])
        self.assertEqual(code, 0)
        self.assertIn('"SLUS_999.99", 0x00100000u, 0x%08Xu' % (zlib.crc32(self.elf_bytes) & 0xFFFFFFFF), out)
        code, _, err = run_main(override, ["keys", os.path.join(self.d, "nope")])
        self.assertEqual(code, 2)

    def test_slug_and_c_string(self):
        self.assertEqual(override.slugify("Gold rifle!"), "gold_rifle")
        self.assertEqual(override.slugify("2 games in 1"), "m_2_games_in_1")
        self.assertEqual(override.slugify("***"), "mod")
        self.assertEqual(override.c_string('a"b\\c'), 'a\\"b\\\\c')

    def test_template_fields_and_api(self):
        tpl = open(os.path.join(SKILL, "templates", "override.cpp")).read()
        for k in override.PLACEHOLDERS:
            self.assertIn("@%s@" % k, tpl)
        self.assertEqual(set(re.findall(r"@([A-Z][A-Z0-9_]*)@", tpl)), set(override.PLACEHOLDERS))
        for api in ("game_overrides.h", "ps2_runtime.h", "ps2_runtime_macros.h", "PS2_REGISTER_GAME_OVERRIDE",
                    "getRegU32", "setReturnU32", "runtime.memory().getRDRAM()"):
            self.assertIn(api, tpl)

    @unittest.skipUnless(CXX, "no C++ compiler")
    def test_bare_template_compiles_when_filled_empty(self):
        text = override.render_module("Empty", "SLUS_000.00", 0x00100008, 0, "empty", "", "", ["note"])
        ok, err = compile_ok(self, text)
        self.assertTrue(ok, err)


# --------------------------------------------------------------------------
# CLI, help, and house rules
# --------------------------------------------------------------------------


class TestCli(Fixture):
    def test_help_for_every_command(self):
        for script, cmds in (("pnach.py", ["parse", "classify", "toml", "override", "crc"]),
                             ("override.py", ["new", "keys"])):
            top = cli(script, "--help")
            self.assertEqual(top.returncode, 0, top.stderr)
            for c in cmds:
                self.assertIn(c, top.stdout)
                r = cli(script, c, "--help")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("usage:", r.stdout)
            r = cli(script)
            self.assertEqual(r.returncode, 2)

    def test_end_to_end_subprocess(self):
        r = cli("pnach.py", "crc", self.elf)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = cli("pnach.py", "toml", self.pn, "--elf", self.elf)
        self.assertIn("[[patches.instructions]]", r.stdout)
        r = cli("pnach.py", "override", self.pn, "--elf", self.elf, "--frame-hook", "0x00100018", cwd=self.d)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.d, "console", "patches", "slus_99999_%08x.cpp" % self.pcrc)))
        r = cli("override.py", "new", "--name", "Moon jump", "--elf", self.elf, "--wrap", "0x00100000=moon", cwd=self.d)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.d, "console", "patches", "moon_jump.cpp")))
        r = cli("pnach.py", "classify", os.path.join(self.d, "nope.pnach"), "--elf", self.elf)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)

    def test_no_em_dashes_in_skill(self):
        for dirpath, _, files in os.walk(SKILL):
            for f in files:
                with open(os.path.join(dirpath, f), "rb") as fh:
                    self.assertNotIn(chr(0x2014).encode("utf-8"), fh.read(), f)

    def test_skill_md_links_exist(self):
        md = open(os.path.join(SKILL, "SKILL.md")).read()
        self.assertTrue(md.startswith("---\nname: console-mod\n"))
        for ref in re.findall(r"`(?:\$\{CLAUDE_SKILL_DIR\}/)?((?:reference|templates)/[^`\s]+)`", md):
            self.assertTrue(os.path.isfile(os.path.join(SKILL, ref)), ref)
        for ref in re.findall(r"\$\{CLAUDE_SKILL_DIR\}/([^\s`\"]+)", md):
            self.assertTrue(os.path.exists(os.path.join(SKILL, ref)), ref)
        for ref in re.findall(r"\]\(((?:reference|templates)/[^)]+)\)", md):
            self.assertTrue(os.path.isfile(os.path.join(SKILL, ref)), ref)


if __name__ == "__main__":
    unittest.main()
