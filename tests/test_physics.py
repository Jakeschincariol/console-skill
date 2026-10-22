"""Tests for console-physics: tune.py. Fully offline, synthetic fixtures only."""

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
from contextlib import redirect_stderr, redirect_stdout

from _load import load, ROOT

tune = load("console-physics", "tune")
SKILL = os.path.join(ROOT, "console-physics")
PF_X, PF_W, PF_R = 1, 2, 4


def words(*ws):
    return b"".join(struct.pack("<I", w) for w in ws)


def f2u(f):
    return struct.unpack("<I", struct.pack("<f", f))[0]


TEXT = words(
    0x3C1C0021,  # 00100000 lui   $gp, 0x0021
    0x279C8000,  # 00100004 addiu $gp, $gp, -0x8000      -> $gp = 0x00208000
    0xC7818000,  # 00100008 lwc1  $f1, -0x8000($gp)       -> reads 0x00200000 (9.8)
    0x3C02411C,  # 0010000C lui   $v0, 0x411C
    0x3442CCCD,  # 00100010 ori   $v0, $v0, 0xCCCD
    0x44826000,  # 00100014 mtc1  $v0, $f12               -> 9.8
    0x3C014148,  # 00100018 lui   $at, 0x4148
    0x44811000,  # 0010001C mtc1  $at, $f2                -> 12.5 (lone lui)
    0x3C043C89,  # 00100020 lui   $a0, 0x3C89
    0x24848889,  # 00100024 addiu $a0, $a0, -0x7777
    0x44841800,  # 00100028 mtc1  $a0, $f3                -> 1/60 (lui + addiu)
    0x3C080020,  # 0010002C lui   $t0, 0x0020
    0xC5040004,  # 00100030 lwc1  $f4, 4($t0)             -> reads 0x00200004 (1/60)
    0x03E00008,  # 00100034 jr    $ra
    0x00000000,  # 00100038 nop
    0x3C0341F0,  # 0010003C lui   $v1, 0x41F0             (30.0 ...)
    0x00001821,  # 00100040 addu  $v1, $zero, $zero       (... overwritten)
    0x44832800,  # 00100044 mtc1  $v1, $f5                -> NOT 30.0
    0x3C0942F0,  # 00100048 lui   $t1, 0x42F0             (120.0 ...)
    0x08040000,  # 0010004C j     0x00100000
    0x00000000,  # 00100050 nop (delay slot)
    0x44893000,  # 00100054 mtc1  $t1, $f6                -> NOT tracked across the jump
    0x3C0A4270,  # 00100058 lui   $t2, 0x4270
    0x448A3800,  # 0010005C mtc1  $t2, $f7                -> 60.0
    0x03E00008,  # 00100060 jr    $ra
    0x00000000,  # 00100064 nop
)
DATA = words(f2u(9.8), f2u(1.0 / 60.0), f2u(30.0), f2u(9.81), f2u(-9.8), 0, f2u(0.75), f2u(1.0)) + bytes(0x100 - 32)


def make_elf(segments, entry=0x00100000, sections=True):
    phnum = len(segments)
    off = 52 + 32 * phnum
    body = bytearray()
    placed = []
    for vaddr, data, memsz, flags, name in segments:
        body += bytes((-(off + len(body))) % 16)
        placed.append(off + len(body))
        body += data
    shoff = shnum = shstrndx = 0
    shdrs = b""
    if sections:
        names = b"\0"
        sh = [(0,) * 10]
        for (vaddr, data, memsz, flags, name), foff in zip(segments, placed):
            nidx = len(names)
            names += name.encode() + b"\0"
            sh.append((nidx, 1, 2 | (4 if flags & PF_X else 0) | (1 if flags & PF_W else 0), vaddr, foff, len(data), 0, 0, 16, 0))
            if memsz > len(data):
                nidx = len(names)
                names += b".bss\0"
                sh.append((nidx, 8, 3, vaddr + len(data), foff + len(data), memsz - len(data), 0, 0, 16, 0))
        nidx = len(names)
        names += b".shstrtab\0"
        sh.append((nidx, 3, 0, 0, off + len(body), len(names), 0, 0, 1, 0))
        body += names
        body += bytes((-(off + len(body))) % 4)
        shoff = off + len(body)
        shdrs = b"".join(struct.pack("<IIIIIIIIII", *s) for s in sh)
        shnum, shstrndx = len(sh), len(sh) - 1
    hdr = b"\x7fELF" + bytes([1, 1, 1, 0]) + bytes(8)
    hdr += struct.pack("<HHIIIIIHHHHHH", 2, 8, 1, entry, 52, shoff, 0, 52, 32, phnum, 40, shnum, shstrndx)
    ph = b"".join(struct.pack("<IIIIIIII", 1, foff, s[0], s[0], len(s[1]), s[2], s[3], 16)
                  for s, foff in zip(segments, placed))
    return hdr + ph + bytes(body) + shdrs


def standard_elf(sections=True):
    return make_elf([(0x00100000, TEXT, len(TEXT), PF_R | PF_X, ".text"),
                     (0x00200000, DATA, 0x1000, PF_R | PF_W, ".data")], sections=sections)


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tune.main(argv)
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="console_physics_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.elf = self.write("SLUS_999.99", standard_elf())

    def write(self, name, data):
        p = os.path.join(self.d, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def scan_json(self, *args):
        code, out, err = run_main(["scan", self.elf, "--json"] + list(args))
        return code, json.loads(out)


class TestFloatRanges(unittest.TestCase):
    def test_ranges_are_tight(self):
        for value, tol in ((9.8, 0.01), (0.016666668, 1e-6), (-9.8, 0.05), (0.5, 1.0), (1e-30, 1e-31), (3e38, 1e37)):
            for a, b in tune.pattern_ranges(value, tol):
                for bits in (a, b):
                    self.assertLessEqual(abs(tune.u2f(bits) - value), tol * (1 + 1e-9), (value, hex(bits)))
                for bits in (a - 1, b + 1):
                    if bits in (-1, 0x7F800000, 0x80000000 - 1, 0xFF800000) or bits < 0:
                        continue
                    if (bits & 0x7FFFFFFF) > 0x7F7FFFFF:
                        continue
                    self.assertGreater(abs(tune.u2f(bits) - value), tol, (value, hex(bits)))

    def test_range_straddling_zero(self):
        r = tune.pattern_ranges(0.5, 1.0)
        self.assertEqual(len(r), 2)
        self.assertEqual(r[0][0], 0)
        self.assertTrue(r[1][0] & 0x80000000)

    def test_check_value(self):
        for bad in (0.0, float("nan"), float("inf"), 1e39):
            with self.assertRaises(tune.UsageError):
                tune.check_value(bad)
        tune.check_value(9.8)

    def test_split_hi_lo(self):
        for v in (9.8, 1.0 / 60.0, 1.0 / 30.0, -2.5, 1e-3, 123456.0):
            bits = f2u(v)
            hi, lo = tune.split_hi_lo(bits, "ori")
            self.assertEqual((hi << 16) | lo, bits)
            hi, lo = tune.split_hi_lo(bits, "addiu")
            self.assertEqual(((hi << 16) + tune.sext16(lo)) & 0xFFFFFFFF, bits)

    def test_describe(self):
        self.assertEqual(tune.describe(0x3C02411C), "lui $v0, 0x411C")
        self.assertEqual(tune.describe(0x3442CCCD), "ori $v0, $v0, 0xCCCD")
        self.assertEqual(tune.describe(0x44826000), "mtc1 $v0, $f12")
        self.assertEqual(tune.describe(0xC7818000), "lwc1 $f1, -32768($gp)")
        self.assertEqual(tune.describe(0x03E00008), "jr $ra")
        self.assertEqual(tune.describe(0), "nop")


class TestAnalysis(Base):
    def test_gp_consts_and_xrefs(self):
        e = tune.Elf(self.elf)
        ana = tune.analyze(e)
        self.assertEqual(ana["gp"], 0x00208000)
        by = dict((c["mtc1"], c) for c in ana["consts"])
        self.assertEqual(by[0x00100014]["bits"], f2u(9.8))
        self.assertEqual(by[0x00100014]["lo_op"], "ori")
        self.assertEqual(by[0x0010001C]["bits"], 0x41480000)
        self.assertIsNone(by[0x0010001C]["lo"])
        self.assertEqual(by[0x00100028]["bits"], f2u(1.0 / 60.0))
        self.assertEqual(by[0x00100028]["lo_op"], "addiu")
        self.assertNotIn(0x00100044, by)     # register overwritten before the mtc1
        self.assertNotIn(0x00100054, by)     # not followed across a jump
        self.assertEqual(by[0x0010005C]["bits"], f2u(60.0))
        self.assertEqual([x["at"] for x in ana["xrefs"][0x00200000]], [0x00100008])
        self.assertEqual(ana["xrefs"][0x00200000][0]["via"], "$gp")
        self.assertEqual([x["at"] for x in ana["xrefs"][0x00200004]], [0x00100030])

    def test_regions_with_and_without_sections(self):
        e = tune.Elf(self.elf)
        code, data = e.regions()
        self.assertEqual([c[0] for c in code], [0x00100000])
        self.assertEqual([d[0] for d in data], [0x00200000])
        e2 = tune.Elf(self.write("NOSEC", standard_elf(sections=False)))
        code, data = e2.regions()
        self.assertEqual([c[0] for c in code], [0x00100000])
        self.assertEqual([d[0] for d in data], [0x00200000])
        blob = TEXT + DATA
        e3 = tune.Elf(self.write("RWX", make_elf([(0x00100000, blob, len(blob), 7, ".x")], sections=False)))
        code, data = e3.regions()
        self.assertEqual(code, data)


class TestScan(Base):
    def test_scan_finds_data_and_code(self):
        code, j = self.scan_json("--value", "9.8")
        self.assertEqual(code, 0)
        got = [(h["kind"], h["addr"]) for h in j["hits"]]
        self.assertEqual(got, [("code", "0x0010000C"), ("data", "0x00200000")])
        data_hit = j["hits"][1]
        self.assertEqual(data_hit["where"], ".data")
        self.assertEqual(data_hit["read_by"][0]["at"], "0x00100008")
        self.assertEqual(j["gp"], "0x00208000")

    def test_tolerance(self):
        _, j = self.scan_json("--value", "9.8", "--tol", "0.02")
        self.assertIn("0x0020000C", [h["addr"] for h in j["hits"]])   # 9.81
        _, j = self.scan_json("--value", "-9.8")
        self.assertEqual([h["addr"] for h in j["hits"]], ["0x00200010"])

    def test_lone_lui_and_addiu_constants(self):
        _, j = self.scan_json("--value", "12.5")
        self.assertEqual(j["hits"][0]["lo"], None)
        self.assertEqual(j["hits"][0]["mtc1"], "0x0010001C")
        _, j = self.scan_json("--value", "0.016666668")
        kinds = sorted((h["kind"], h["addr"]) for h in j["hits"])
        self.assertEqual(kinds, [("code", "0x00100020"), ("data", "0x00200004")])

    def test_killed_register_not_reported(self):
        _, j = self.scan_json("--value", "30")
        self.assertEqual([(h["kind"], h["addr"]) for h in j["hits"]], [("data", "0x00200008")])

    def test_nothing_found_exit_1(self):
        code, out, _ = run_main(["scan", self.elf, "--value", "120"])
        self.assertEqual(code, 1)
        self.assertIn("Nothing", out)

    def test_text_output(self):
        code, out, _ = run_main(["scan", self.elf, "--value", "9.8"])
        self.assertIn("DATA  0x00200000", out)
        self.assertIn("read by: lwc1 $f1, -32768($gp) at 0x00100008 ($gp)", out)
        self.assertIn("CODE  0x0010000C", out)
        self.assertIn("> 0x00100010  3442CCCD  ori $v0, $v0, 0xCCCD", out)
        self.assertIn("tune.py patch", out)

    def test_limit(self):
        code, out, _ = run_main(["scan", self.elf, "--value", "9.8", "--limit", "1"])
        self.assertIn("--limit", out)

    def test_bad_inputs(self):
        for args in (["--value", "0"], ["--value", "nan"], ["--value", "9.8", "--tol", "-1"]):
            code, _, err = run_main(["scan", self.elf] + args)
            self.assertEqual(code, 2, args)
            self.assertNotIn("Traceback", err)
        for name, data in (("short", b"\x7fELF"), ("notelf", b"MZ" + bytes(80)),
                           ("arm", standard_elf()[:18] + struct.pack("<H", 40) + standard_elf()[20:]),
                           ("trunc", standard_elf()[:100])):
            code, _, err = run_main(["scan", self.write(name, data), "--value", "9.8"])
            self.assertEqual(code, 2, name)
        code, _, err = run_main(["scan", os.path.join(self.d, "missing"), "--value", "1"])
        self.assertEqual(code, 2)


class TestFps(Base):
    def test_fps_candidates_and_caveat(self):
        code, out, _ = run_main(["fps", self.elf])
        self.assertEqual(code, 0)
        self.assertIn("0x00200004", out)    # 1/60 data
        self.assertIn("0x00100020", out)    # 1/60 lui+addiu
        self.assertIn("0x00200008", out)    # 30.0 data
        self.assertIn("0x00100058", out)    # 60.0 code
        self.assertIn("delta", out.replace("frame time", "delta"))
        self.assertIn("test in PCSX2 first", out)
        code, out, _ = run_main(["fps", self.elf, "--json"])
        j = json.loads(out)
        self.assertEqual(len(j["hits"]), 4)
        self.assertIn("60 fps", j["caveat"])

    def test_fps_nothing(self):
        e = self.write("EMPTY", make_elf([(0x00100000, words(0x03E00008, 0), 8, PF_R | PF_X, ".text")]))
        code, out, _ = run_main(["fps", e])
        self.assertEqual(code, 1)


class TestPatch(Base):
    def patch(self, *args):
        code, out, err = run_main(["patch", self.elf, "--json"] + list(args))
        return code, (json.loads(out) if out.strip() else None), err

    def apply(self, result):
        """Write the patched words into the ELF bytes and re-analyse: the round trip."""
        data = bytearray(open(self.elf, "rb").read())
        e = tune.Elf(self.elf)
        for w in result["words"]:
            addr = int(w["addr"], 16)
            seg, _ = e.locate(addr)
            struct.pack_into("<I", data, seg["offset"] + addr - seg["vaddr"], int(w["new"], 16))
        return tune.Elf(self.write("PATCHED", bytes(data)))

    def test_data_patch(self):
        code, r, _ = self.patch("--addr", "00200000", "--float", "12.5")
        self.assertEqual(code, 0)
        self.assertEqual(r["kind"], "data")
        self.assertEqual(r["pnach"], ["patch=0,EE,00200000,word,41480000"])
        self.assertEqual(r["toml"], [])
        self.assertEqual(r["override"], "Ps2FastWrite32(rdram, 0x00200000u, 0x41480000u); // 12.5f")
        self.assertEqual(r["old_value"], "9.80000019")
        self.assertEqual(r["read_by"][0]["at"], "0x00100008")

    def test_lui_ori_patch_from_any_of_its_instructions(self):
        for addr in ("0x0010000C", "0x00100010", "0x00100014"):
            code, r, _ = self.patch("--addr", addr, "--float", "12.5")
            self.assertEqual(code, 0, addr)
            self.assertEqual(r["kind"], "code lui+ori")
            self.assertEqual(r["pnach"], ["patch=1,EE,0010000C,word,3C024148", "patch=1,EE,00100010,word,34420000"])
            self.assertIn('address = "0x0010000C"\nvalue = "0x3C024148"', r["toml"][0])
            self.assertEqual(r["used_by"], "mtc1 $f12 at 0x00100014")
        e = self.apply(r)
        c = [c for c in tune.analyze(e)["consts"] if c["mtc1"] == 0x00100014][0]
        self.assertEqual(tune.u2f(c["bits"]), 12.5)

    def test_lui_addiu_carry_round_trip(self):
        for v in (1.0 / 30.0, 1.0 / 50.0, -3.75, 0.1):
            code, r, _ = self.patch("--addr", "0x00100020", "--float", repr(v))
            self.assertEqual(r["kind"], "code lui+addiu")
            e = self.apply(r)
            c = [c for c in tune.analyze(e)["consts"] if c["mtc1"] == 0x00100028][0]
            self.assertEqual(c["bits"], f2u(v), v)

    def test_unchanged_words_are_not_patched(self):
        code, r, _ = self.patch("--addr", "0x0010000C", "--float", "4.9")     # 0x409CCCCD: same low half
        self.assertEqual(r["pnach"], ["patch=1,EE,0010000C,word,3C02409C"])
        self.assertEqual(len(r["toml"]), 1)
        self.assertEqual(len(r["words"]), 2)
        code, r, _ = self.patch("--addr", "0x0010000C", "--float", "9.8")
        self.assertEqual(r["pnach"], [])
        self.assertIn("already holds", r["note"])

    def test_lone_lui(self):
        code, r, _ = self.patch("--addr", "0x00100018", "--float", "10")
        self.assertEqual(code, 0)
        self.assertEqual(r["pnach"], ["patch=1,EE,00100018,word,3C014120"])
        code, r, _ = self.patch("--addr", "0x00100018", "--float", "9.8")
        self.assertEqual(code, 1)
        self.assertEqual(r["new_bits"], "0x411D0000")
        self.assertIn("nearest", r["note"])
        code, r, err = self.patch("--addr", "0x00100018", "--float", "9.8", "--exact")
        self.assertEqual(code, 2)
        self.assertIn("lone lui", err)

    def test_runtime_value(self):
        code, r, _ = self.patch("--addr", "0x00200800", "--float", "2")
        self.assertEqual(r["kind"], "runtime")
        self.assertEqual(r["pnach"], ["patch=1,EE,00200800,word,40000000"])
        self.assertIn("frame-hook", r["note"])
        code, r, _ = self.patch("--addr", "0x01000000", "--float", "2")
        self.assertIn("outside the ELF", r["note"])

    def test_place_override(self):
        code, r, _ = self.patch("--addr", "0x00200000", "--float", "1", "--place", "1")
        self.assertTrue(r["pnach"][0].startswith("patch=1,"))

    def test_shared_constant_note(self):
        blob = TEXT + words(0xC7818000)   # a second $gp read of 0x00200000
        e = self.write("TWO", make_elf([(0x00100000, blob, len(blob), PF_R | PF_X, ".text"),
                                        (0x00200000, DATA, 0x1000, PF_R | PF_W, ".data")]))
        code, out, _ = run_main(["patch", e, "--addr", "0x00200000", "--float", "3", "--json"])
        self.assertIn("changes every one", json.loads(out)["note"])

    def test_text_output(self):
        code, out, _ = run_main(["patch", self.elf, "--addr", "0x0010000C", "--float", "12.5"])
        self.assertIn("9.80000019 -> 12.5", out)
        self.assertIn("[[patches.instructions]]", out)
        code, out, _ = run_main(["patch", self.elf, "--addr", "0x00200000", "--float", "12.5"])
        self.assertIn("pnach.py override", out)

    def test_bad_patch_inputs(self):
        for args in (["--addr", "0x00100034", "--float", "1"],     # jr $ra: not a float constant
                     ["--addr", "0x00100002", "--float", "1"],
                     ["--addr", "zz", "--float", "1"],
                     ["--addr", "0x70000000", "--float", "1"],
                     ["--addr", "0x00200000", "--float", "nan"]):
            code, _, err = run_main(["patch", self.elf] + args)
            self.assertEqual(code, 2, args)
            self.assertNotIn("Traceback", err)


class TestCli(Base):
    def cli(self, *args):
        return subprocess.run([sys.executable, os.path.join(SKILL, "tune.py")] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)

    def test_help(self):
        r = self.cli("--help")
        self.assertEqual(r.returncode, 0)
        for c in ("scan", "fps", "patch"):
            self.assertIn(c, r.stdout)
            r2 = self.cli(c, "--help")
            self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(self.cli().returncode, 2)

    def test_subprocess_end_to_end(self):
        r = self.cli("scan", self.elf, "--value", "9.8")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.cli("patch", self.elf, "--addr", "0x0010000C", "--float", "4.9")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.cli("fps", os.path.join(self.d, "missing"))
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)

    def test_house_rules(self):
        for dirpath, _, files in os.walk(SKILL):
            for f in files:
                with open(os.path.join(dirpath, f), "rb") as fh:
                    self.assertNotIn(chr(0x2014).encode("utf-8"), fh.read(), f)
        md = open(os.path.join(SKILL, "SKILL.md")).read()
        self.assertTrue(md.startswith("---\nname: console-physics\n"))
        for ref in re.findall(r"`(?:\$\{CLAUDE_SKILL_DIR\}/)?((?:reference|templates)/[^`\s]+)`", md):
            self.assertTrue(os.path.isfile(os.path.join(SKILL, ref)), ref)
        for ref in re.findall(r"\]\(((?:reference|templates)/[^)]+)\)", md):
            self.assertTrue(os.path.isfile(os.path.join(SKILL, ref)), ref)
        for ref in re.findall(r"\$\{CLAUDE_SKILL_DIR\}/([^\s`\"]+)", md):
            self.assertTrue(os.path.exists(os.path.join(SKILL, ref)), ref)


if __name__ == "__main__":
    unittest.main()
