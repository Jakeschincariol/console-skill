"""Tests for console-reverse: iso.py, elf.py, state.py and the SKILL.md contract.

Every fixture is synthesized here: a tiny ISO 9660 image, a tiny MIPS ELF, a fake PS2Recomp tree.
No game data, BIOS or Sony file is used or needed.
"""

import contextlib
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

from _load import load, ROOT

SKILL = "console-reverse"
iso = load(SKILL, "iso")
elf = load(SKILL, "elf")
state = load(SKILL, "state")

BLOCK = 2048
SYNC = b"\x00" + b"\xff" * 10 + b"\x00"


# ---------------------------------------------------------------- fixture builders

def run(mod, *argv):
    """Call a tool's main() in-process; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mod.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def both_endian32(v):
    return struct.pack("<I", v) + struct.pack(">I", v)


def both_endian16(v):
    return struct.pack("<H", v) + struct.pack(">H", v)


def dir_record(raw_name, lba, size, flags):
    nl = len(raw_name)
    length = 33 + nl + (1 if nl % 2 == 0 else 0)
    rec = bytearray(length)
    rec[0] = length
    rec[2:10] = both_endian32(lba)
    rec[10:18] = both_endian32(size)
    rec[18:25] = bytes([126, 1, 1, 0, 0, 0, 0])
    rec[25] = flags
    rec[28:32] = both_endian16(1)
    rec[32] = nl
    rec[33:33 + nl] = raw_name
    return bytes(rec)


def pack_records(records):
    """Pack records into 2048-byte sectors; a record never crosses a sector."""
    sectors = [bytearray()]
    for r in records:
        if len(sectors[-1]) + len(r) > BLOCK:
            sectors.append(bytearray())
        sectors[-1] += r
    return [bytes(s) + b"\x00" * (BLOCK - len(s)) for s in sectors]


def make_iso(files, layout="2048", extra_root=(), volume_id=b"TESTDISC", truncate=0):
    """files: {"DIR/NAME.EXT": bytes}. extra_root: [(raw_name, [chunk, ...], flags_list)]."""
    # tree of directories
    dirs = {"": []}
    for path in sorted(files):
        parts = path.split("/")
        for i in range(1, len(parts)):
            d = "/".join(parts[:i])
            if d not in dirs:
                dirs[d] = []
                dirs["/".join(parts[:i - 1])].append(("dir", parts[i - 1], d))
        dirs["/".join(parts[:-1])].append(("file", parts[-1], path))
    order = sorted(dirs, key=lambda d: (d.count("/") if d else -1, d))

    def records_for(d, lbas, sizes, flba):
        recs = [dir_record(b"\x00", lbas[d], sizes[d], 2),
                dir_record(b"\x01", lbas[d.rpartition("/")[0]] if d else lbas[""], 0, 2)]
        for kind, name, full in sorted(dirs[d], key=lambda t: t[1]):
            if kind == "dir":
                recs.append(dir_record(name.encode(), lbas[full], sizes[full], 2))
            else:
                recs.append(dir_record((name + ";1").encode(), flba[full], len(files[full]), 0))
        if d == "":
            for raw, chunks, flags in extra_root:
                for chunk_lba, chunk, fl in zip(flba[raw], chunks, flags):
                    recs.append(dir_record(raw, chunk_lba, len(chunk), fl))
        return recs

    # pass 1: sizes (lbas do not change record lengths)
    zero = {d: 0 for d in dirs}
    zf = {p: 0 for p in files}
    for raw, chunks, _ in extra_root:
        zf[raw] = [0] * len(chunks)
    sizes = {d: len(pack_records(records_for(d, zero, zero, zf))) * BLOCK for d in order}
    nxt = 18
    lbas = {}
    for d in order:
        lbas[d] = nxt
        nxt += sizes[d] // BLOCK
    flba = {}
    blobs = []
    for p in sorted(files):
        flba[p] = nxt
        data = files[p]
        nblk = max(1, (len(data) + BLOCK - 1) // BLOCK)
        blobs.append((nxt, data))
        nxt += nblk
    for raw, chunks, _ in extra_root:
        flba[raw] = []
        for c in chunks:
            flba[raw].append(nxt)
            blobs.append((nxt, c))
            nxt += max(1, (len(c) + BLOCK - 1) // BLOCK)
    total = nxt
    blocks = [b"\x00" * BLOCK for _ in range(total)]
    pvd = bytearray(BLOCK)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[8:40] = b"PLAYSTATION".ljust(32)
    pvd[40:72] = volume_id.ljust(32)
    pvd[80:88] = both_endian32(total)
    pvd[128:132] = both_endian16(BLOCK)
    pvd[156:190] = dir_record(b"\x00", lbas[""], sizes[""], 2)
    blocks[16] = bytes(pvd)
    term = bytearray(BLOCK)
    term[0] = 255
    term[1:6] = b"CD001"
    term[6] = 1
    blocks[17] = bytes(term)
    for d in order:
        for i, sec in enumerate(pack_records(records_for(d, lbas, sizes, flba))):
            blocks[lbas[d] + i] = sec
    for start, data in blobs:
        for i in range(0, max(1, len(data)), BLOCK):
            chunk = data[i:i + BLOCK]
            blocks[start + i // BLOCK] = chunk + b"\x00" * (BLOCK - len(chunk))
    if layout == "2048":
        img = b"".join(blocks)
    else:
        raw = []
        for n, b in enumerate(blocks):
            mode = 1 if layout == "mode1" else 2
            hdr = SYNC + bytes([0, 2, n % 75, mode])
            if mode == 1:
                raw.append(hdr + b + b"\x00" * 288)
            else:
                raw.append(hdr + b"\x00" * 8 + b + b"\x00" * 280)
        img = b"".join(raw)
    if truncate:
        img = img[:-truncate]
    return img


def make_elf(entry=0x00100008, machine=8, etype=2, sections=True, symtab=True, strings=b"",
             big_endian=False, elf64=False, code=None):
    code = code if code is not None else bytes(range(256)) * 4      # 1 KB "code"
    data = b"DATA" * 16 + strings
    data += b"\x00" * ((-len(data)) % 4)
    phnum = 2
    ehdr_size, ph_size = 52, 32
    code_off = 0x100
    data_off = code_off + len(code)
    body = bytearray(code_off)
    body += code
    body += data
    segs = [(code_off, 0x00100000, len(code), len(code), 5),          # R-X
            (data_off, 0x00100000 + len(code), len(data), len(data) + 0x200, 6)]   # RW- with bss
    shoff = shnum = shstrndx = 0
    if sections:
        names = b"\x00.text\x00.data\x00.bss\x00.shstrtab\x00.symtab\x00"
        shstr_off = len(body)
        body += names
        sym_off = len(body)
        nsyms = 3
        if symtab:
            body += b"\x00" * 16 * nsyms
        while len(body) % 4:
            body += b"\x00"
        shoff = len(body)
        sh = []
        sh.append(struct.pack("<IIIIIIIIII", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        sh.append(struct.pack("<IIIIIIIIII", 1, 1, 6, 0x00100000, code_off, len(code), 0, 0, 16, 0))
        sh.append(struct.pack("<IIIIIIIIII", 7, 1, 3, 0x00100000 + len(code), data_off, len(data), 0, 0, 16, 0))
        sh.append(struct.pack("<IIIIIIIIII", 13, 8, 3, 0x00100000 + len(code) + len(data), 0, 0x200, 0, 0, 16, 0))
        sh.append(struct.pack("<IIIIIIIIII", 18, 3, 0, 0, shstr_off, len(names), 0, 0, 1, 0))
        if symtab:
            sh.append(struct.pack("<IIIIIIIIII", 28, 2, 0, 0, sym_off, 16 * nsyms, 4, 1, 4, 16))
        shnum = len(sh)
        shstrndx = 4
        body += b"".join(sh)
    ident = b"\x7fELF" + bytes([2 if elf64 else 1, 2 if big_endian else 1, 1]) + b"\x00" * 9
    hdr = ident + struct.pack("<HHIIIIIHHHHHH", etype, machine, 1, entry, ehdr_size, shoff, 0x20924001,
                              ehdr_size, ph_size, phnum, 40, shnum, shstrndx)
    body[0:52] = hdr
    for i, (off, vaddr, filesz, memsz, flags) in enumerate(segs):
        body[52 + i * 32:52 + (i + 1) * 32] = struct.pack("<IIIIIIII", 1, off, vaddr, vaddr, filesz, memsz,
                                                         flags, 16)
    return bytes(body)


def xor_words(data):
    acc = 0
    for i in range(0, len(data) - len(data) % 4, 4):
        acc ^= struct.unpack_from("<I", data, i)[0]
    return acc


CNF = b"BOOT2 = cdrom0:\\SLUS_203.12;1\r\nVER = 1.00\r\nVMODE = NTSC\r\n"
IRX_STRINGS = b"cdrom0:\\MODULES\\SIO2MAN.IRX;1\x00rom0:PADMAN\x00cdrom0:\\DATA\\LEVEL1.BIN;1\x00"


def game_files():
    return {
        "SYSTEM.CNF": CNF,
        "SLUS_203.12": make_elf(strings=IRX_STRINGS),
        "MODULES/SIO2MAN.IRX": b"\x7fIRX" + b"\x01" * 100,
        "DATA/LEVEL1.BIN": bytes((i * 7) & 0xFF for i in range(5000)),
        "DATA/SUB/DEEP.DAT": b"deep",
        "EMPTY.TXT": b"",
    }


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="console-reverse-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, data):
        p = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p


# ---------------------------------------------------------------- iso.py

class IsoTests(Tmp):
    def iso(self, layout="2048", **kw):
        return self.write("game_%s.iso" % layout, make_iso(game_files(), layout=layout, **kw))

    def test_info_complete(self):
        code, out, _ = run(iso, "info", self.iso())
        self.assertEqual(code, 0)
        self.assertIn("TESTDISC", out)
        self.assertIn("complete", out)

    def test_info_json_and_truncated_dump(self):
        code, out, _ = run(iso, "info", self.iso(truncate=3000), "--json")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["complete"])

    def test_ls_root(self):
        code, out, _ = run(iso, "ls", self.iso())
        self.assertEqual(code, 0)
        self.assertIn("/SYSTEM.CNF", out)
        self.assertIn("/SLUS_203.12", out)
        self.assertIn("/MODULES/", out)
        self.assertNotIn(";1", out)
        self.assertNotIn("/DATA/LEVEL1.BIN", out)

    def test_ls_subdir_and_recursive_json(self):
        p = self.iso()
        code, out, _ = run(iso, "ls", p, "DATA")
        self.assertIn("/DATA/LEVEL1.BIN", out)
        code, out, _ = run(iso, "ls", p, "-r", "--json")
        rows = json.loads(out)
        paths = {r["path"]: r for r in rows}
        self.assertIn("/DATA/SUB/DEEP.DAT", paths)
        self.assertEqual(paths["/DATA/LEVEL1.BIN"]["size"], 5000)
        self.assertEqual(paths["/MODULES"]["type"], "dir")

    def test_ls_case_insensitive_and_device_prefix(self):
        p = self.iso()
        code, out, _ = run(iso, "ls", p, "cdrom0:\\modules\\sio2man.irx;1")
        self.assertEqual(code, 0)
        self.assertIn("SIO2MAN.IRX", out)

    def test_ls_missing_path(self):
        code, _, err = run(iso, "ls", self.iso(), "NOPE")
        self.assertEqual(code, 2)
        self.assertIn("not found", err)

    def test_extract_all_round_trip(self):
        out_dir = os.path.join(self.tmp, "console", "disc")
        code, out, _ = run(iso, "extract", self.iso(), "--out", out_dir)
        self.assertEqual(code, 0, out)
        for path, data in game_files().items():
            with open(os.path.join(out_dir, *path.split("/")), "rb") as f:
                self.assertEqual(f.read(), data, path)
        self.assertIn("Boot ELF", out)
        self.assertFalse([f for f in os.listdir(out_dir) if f.endswith(".part")])

    def test_extract_only_keeps_tree(self):
        out_dir = os.path.join(self.tmp, "x")
        code, out, _ = run(iso, "extract", self.iso(), "--out", out_dir, "--only", "DATA/SUB", "SYSTEM.CNF",
                           "--json")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(out_dir, "DATA", "SUB", "DEEP.DAT")))
        self.assertTrue(os.path.isfile(os.path.join(out_dir, "SYSTEM.CNF")))
        self.assertFalse(os.path.exists(os.path.join(out_dir, "DATA", "LEVEL1.BIN")))
        self.assertEqual(json.loads(out)["written"], 2)

    def test_extract_keeps_existing_files_unless_forced(self):
        p = self.iso()
        out_dir = os.path.join(self.tmp, "d")
        run(iso, "extract", p, "--out", out_dir)
        edited = os.path.join(out_dir, "DATA", "LEVEL1.BIN")
        with open(edited, "wb") as f:
            f.write(b"my edit")
        code, out, _ = run(iso, "extract", p, "--out", out_dir)
        self.assertEqual(code, 0)
        self.assertIn("Kept", out)
        with open(edited, "rb") as f:
            self.assertEqual(f.read(), b"my edit")
        run(iso, "extract", p, "--out", out_dir, "--force")
        with open(edited, "rb") as f:
            self.assertEqual(f.read(), game_files()["DATA/LEVEL1.BIN"])

    def test_extract_refuses_path_traversal(self):
        bad = make_iso({"SYSTEM.CNF": CNF}, extra_root=[(b"../EVIL.TXT;1", [b"x"], [0])])
        p = self.write("bad.iso", bad)
        out_dir = os.path.join(self.tmp, "out")
        code, _, err = run(iso, "extract", p, "--out", out_dir)
        self.assertEqual(code, 2)
        self.assertIn("unsafe", err)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "EVIL.TXT")))
        self.assertFalse(os.path.exists(os.path.join(out_dir, "SYSTEM.CNF")), "nothing written")

    def test_extract_refuses_backslash_and_dotdot_names(self):
        for raw in (b"..", b"A\\B.TXT;1", b"C:X;1"):
            bad = make_iso({"SYSTEM.CNF": CNF}, extra_root=[(raw, [b"x"], [0])])
            p = self.write("bad2.iso", bad)
            code, _, err = run(iso, "extract", p, "--out", os.path.join(self.tmp, "o2"))
            self.assertEqual(code, 2, raw)

    def test_extract_truncated_image_refused_before_writing(self):
        p = self.write("short.iso", make_iso(game_files(), truncate=4000))
        out_dir = os.path.join(self.tmp, "o")
        code, _, err = run(iso, "extract", p, "--out", out_dir)
        self.assertEqual(code, 2)
        self.assertIn("truncated", err)
        self.assertFalse(os.path.exists(os.path.join(out_dir, "SYSTEM.CNF")))

    def test_raw_2352_layouts(self):
        for layout in ("mode1", "mode2"):
            p = self.iso(layout)
            code, out, _ = run(iso, "info", p)
            self.assertEqual(code, 0, layout)
            self.assertIn("2352", out)
            out_dir = os.path.join(self.tmp, "raw_" + layout)
            self.assertEqual(run(iso, "extract", p, "--out", out_dir)[0], 0)
            with open(os.path.join(out_dir, "DATA", "LEVEL1.BIN"), "rb") as f:
                self.assertEqual(f.read(), game_files()["DATA/LEVEL1.BIN"])

    def test_plain_converts_raw_images(self):
        for layout in ("mode1", "mode2"):
            raw = self.iso(layout)
            out = os.path.join(self.tmp, "plain_%s.iso" % layout)
            code, msg, _ = run(iso, "plain", raw, "--out", out)
            self.assertEqual(code, 0, msg)
            with open(out, "rb") as f:
                self.assertEqual(f.read(), make_iso(game_files()), layout)
            self.assertEqual(run(iso, "plain", raw, "--out", out)[0], 1, "refuses to overwrite")
            self.assertEqual(run(iso, "plain", raw, "--out", out, "--force")[0], 0)
        code, msg, _ = run(iso, "plain", self.iso(), "--out", os.path.join(self.tmp, "x.iso"))
        self.assertEqual(code, 0)
        self.assertIn("already has 2048-byte sectors", msg)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "x.iso")))

    def test_multi_extent_file_is_joined(self):
        a, b = b"A" * BLOCK, b"B" * 100
        img = make_iso({"SYSTEM.CNF": CNF}, extra_root=[(b"BIG.DAT;1", [a, b], [0x80, 0])])
        p = self.write("multi.iso", img)
        code, out, _ = run(iso, "ls", p, "--json")
        rows = {r["path"]: r for r in json.loads(out)}
        self.assertEqual(rows["/BIG.DAT"]["size"], len(a) + len(b))
        out_dir = os.path.join(self.tmp, "m")
        run(iso, "extract", p, "--out", out_dir)
        with open(os.path.join(out_dir, "BIG.DAT"), "rb") as f:
            self.assertEqual(f.read(), a + b)

    def test_many_entries_span_directory_sectors(self):
        files = {"SYSTEM.CNF": CNF}
        for i in range(120):
            files["MANY/FILE_%03d.DAT" % i] = bytes([i])
        p = self.write("many.iso", make_iso(files))
        code, out, _ = run(iso, "ls", p, "MANY", "--json")
        self.assertEqual(len(json.loads(out)), 120)

    def test_not_an_iso(self):
        p = self.write("junk.iso", b"\x00" * (BLOCK * 20))
        code, _, err = run(iso, "info", p)
        self.assertEqual(code, 2)
        self.assertIn("not an ISO 9660", err)
        p = self.write("tiny.iso", b"abc")
        self.assertEqual(run(iso, "ls", p)[0], 2)

    def test_missing_file(self):
        code, _, err = run(iso, "info", os.path.join(self.tmp, "nope.iso"))
        self.assertEqual(code, 2)
        self.assertIn("cannot open", err)

    def test_boot_from_iso(self):
        code, out, _ = run(iso, "boot", self.iso(), "--json")
        self.assertEqual(code, 0)
        info = json.loads(out)
        self.assertEqual(info["elf"], "SLUS_203.12")
        self.assertEqual(info["serial"], "SLUS-20312")
        self.assertEqual(info["region"], "USA")
        self.assertEqual(info["ver"], "1.00")
        self.assertEqual(info["vmode"], "NTSC")
        self.assertTrue(info["elf_found"])

    def test_boot_from_extracted_dir(self):
        out_dir = os.path.join(self.tmp, "disc")
        run(iso, "extract", self.iso(), "--out", out_dir)
        code, out, _ = run(iso, "boot", out_dir)
        self.assertEqual(code, 0)
        self.assertIn("SLUS-20312", out)
        self.assertIn(os.path.join(out_dir, "SLUS_203.12"), out)

    def test_boot_missing_elf_exit_1(self):
        p = self.write("noelf.iso", make_iso({"SYSTEM.CNF": CNF}))
        code, out, _ = run(iso, "boot", p)
        self.assertEqual(code, 1)
        self.assertIn("NO", out)

    def test_boot_ps1_disc_refused(self):
        p = self.write("ps1.iso", make_iso({"SYSTEM.CNF": b"BOOT = cdrom:\\SLUS_000.01;1\r\n"}))
        code, _, err = run(iso, "boot", p)
        self.assertEqual(code, 2)
        self.assertIn("PlayStation 1", err)

    def test_boot_no_system_cnf(self):
        p = self.write("nocnf.iso", make_iso({"README.TXT": b"hi"}))
        code, _, err = run(iso, "boot", p)
        self.assertEqual(code, 2)
        self.assertIn("SYSTEM.CNF", err)
        code, _, err = run(iso, "boot", self.tmp)
        self.assertEqual(code, 2)

    def test_region_prefixes_and_serial(self):
        for name, region in (("SCES_501.23", "Europe"), ("SLPM_650.01", "Japan"), ("SLKA_250.01", "Korea")):
            info = iso.boot_info_from_cnf("BOOT2 = cdrom0:\\%s;1\n" % name)
            self.assertEqual(info["region"], region)
        info = iso.boot_info_from_cnf("BOOT2 = cdrom0:\\GAME.ELF;1\n")
        self.assertIsNone(info["serial"])


# ---------------------------------------------------------------- elf.py

class ElfTests(Tmp):
    def elf_file(self, name="SLUS_203.12", **kw):
        return self.write(name, make_elf(**kw))

    def test_info_text(self):
        p = self.elf_file(strings=IRX_STRINGS)
        code, out, _ = run(elf, "info", p)
        self.assertEqual(code, 0)
        self.assertIn("SLUS-20312 (USA)", out)
        self.assertIn("0x00100008", out)
        self.assertIn("R-X", out)
        self.assertIn("RW-", out)
        self.assertIn("bss", out)
        self.assertIn("SIO2MAN.IRX", out)
        self.assertIn("rom0:PADMAN", out)
        self.assertIn("LEVEL1.BIN", out)

    def test_crcs_match_reference(self):
        data = make_elf(strings=IRX_STRINGS)
        p = self.write("SLUS_203.12", data)
        code, out, _ = run(elf, "info", p, "--json")
        info = json.loads(out)
        self.assertEqual(info["crc32"], "0x%08X" % (zlib.crc32(data) & 0xFFFFFFFF))
        self.assertEqual(info["pnach_crc"], "0x%08X" % xor_words(data))
        self.assertEqual(info["pnach_file"], "SLUS-20312_%08X.pnach" % xor_words(data))

    def test_pnach_crc_ignores_trailing_bytes(self):
        self.assertEqual(elf.crc_pnach(b"\x01\x00\x00\x00\x02\x00\x00\x00\xff"), 3)
        self.assertEqual(elf.crc_pnach(b""), 0)
        self.assertEqual(elf.crc_pnach(b"\xff\xff\xff\xff\xff\xff\xff\xff"), 0)

    def test_crc_subcommand(self):
        p = self.elf_file()
        code, out, _ = run(elf, "crc", p, "--json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["pnach_file"].startswith("SLUS-20312_"))
        code, out, _ = run(elf, "crc", p)
        self.assertIn("runtime override key", out)

    def test_stripped_and_no_sections(self):
        code, out, _ = run(elf, "info", self.elf_file(symtab=False))
        self.assertIn("stripped", out)
        code, out, _ = run(elf, "info", self.elf_file(sections=False), "--json")
        info = json.loads(out)
        self.assertTrue(info["stripped"])
        self.assertEqual(info["section_count"], 0)

    def test_symbols_counted(self):
        code, out, _ = run(elf, "info", self.elf_file(), "--json")
        info = json.loads(out)
        self.assertFalse(info["stripped"])
        self.assertEqual(info["symbols"], 2)
        self.assertIn(".text", info["sections"])

    def test_irx_detected(self):
        code, out, _ = run(elf, "info", self.elf_file("SIO2MAN.IRX", etype=0xFF80))
        self.assertEqual(code, 0)
        self.assertIn("IOP module", out)

    def test_bad_inputs_exit_2(self):
        cases = {
            "notelf": b"hello world" * 10,
            "short": b"\x7fELF\x01\x01",
            "arm": make_elf(machine=40),
            "be": make_elf(big_endian=True),
            "elf64": make_elf(elf64=True),
        }
        for name, data in cases.items():
            p = self.write(name, data)
            code, _, err = run(elf, "info", p)
            self.assertEqual(code, 2, name)
            self.assertTrue(err.startswith("elf.py: "), name)
            self.assertNotIn("Traceback", err)

    def test_truncated_segment_exit_2(self):
        data = make_elf()
        p = self.write("cut", data[:0x180])
        code, _, err = run(elf, "info", p)
        self.assertEqual(code, 2)
        self.assertIn("truncated", err)

    def test_folder_and_missing(self):
        self.assertEqual(run(elf, "info", self.tmp)[0], 2)
        self.assertEqual(run(elf, "info", os.path.join(self.tmp, "nope"))[0], 2)

    def test_toml_writes_real_keys_only(self):
        p = self.elf_file()
        out = os.path.join(self.tmp, "console", "SLUS-20312.toml")
        code, msg, _ = run(elf, "toml", p, "--out", out)
        self.assertEqual(code, 0, msg)
        with open(out, encoding="utf-8") as f:
            text = f.read()
        keys = set(re.findall(r"^([a-z_]+) = ", text, re.M))
        allowed = {"input", "output", "ghidra_output", "single_file_output", "low_memory_mode",
                   "output_worker_threads", "patch_syscalls", "patch_cop0", "patch_cache", "stubs",
                   "entry_points", "skip"}
        self.assertTrue(keys <= allowed, keys - allowed)
        self.assertIn("patch_syscalls = false", text)
        self.assertIn("[general]", text)
        self.assertNotIn("code_base", text)
        self.assertNotIn("runtime_header", text)
        self.assertIn(os.path.join(self.tmp, "console", "output").replace("\\", "\\\\"), text)
        try:
            import tomllib
        except ImportError:
            tomllib = None
        if tomllib:
            cfg = tomllib.loads(text)
            self.assertEqual(cfg["general"]["input"], os.path.abspath(p))
            self.assertEqual(cfg["general"]["stubs"], [])
            self.assertFalse(cfg["general"]["patch_syscalls"])

    def test_toml_refuses_overwrite_and_force(self):
        p = self.elf_file()
        out = os.path.join(self.tmp, "c.toml")
        self.assertEqual(run(elf, "toml", p, "--out", out)[0], 0)
        code, _, err = run(elf, "toml", p, "--out", out)
        self.assertEqual(code, 1)
        self.assertIn("--force", err)
        self.assertEqual(run(elf, "toml", p, "--out", out, "--force")[0], 0)

    def test_toml_with_ghidra_csv(self):
        p = self.elf_file()
        csv = self.write("functions.csv", b"Name,Start,End,Size\nsub_00100230,0x00100230,0x00100290,96\n")
        out = os.path.join(self.tmp, "g.toml")
        code, msg, _ = run(elf, "toml", p, "--out", out, "--ghidra-csv", csv, "--output-dir",
                           os.path.join(self.tmp, "gen"), "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(msg)["ghidra_rows"], 1)
        with open(out, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("ghidra_output = ", text)
        self.assertIn("gen", text)

    def test_toml_bad_csv_and_irx(self):
        p = self.elf_file()
        bad = self.write("bad.csv", b"a,b\n1,2\n")
        self.assertEqual(run(elf, "toml", p, "--out", os.path.join(self.tmp, "b.toml"), "--ghidra-csv", bad)[0], 2)
        self.assertEqual(run(elf, "toml", p, "--out", os.path.join(self.tmp, "b.toml"), "--ghidra-csv",
                             os.path.join(self.tmp, "none.csv"))[0], 2)
        irx = self.elf_file("X.IRX", etype=0xFF80)
        self.assertEqual(run(elf, "toml", irx, "--out", os.path.join(self.tmp, "i.toml"))[0], 2)

    def test_toml_escapes_windows_style_paths(self):
        self.assertEqual(elf.toml_str('C:\\Games\\"x"'), '"C:\\\\Games\\\\\\"x\\""')

    def test_serial_both_ways(self):
        code, out, _ = run(elf, "serial", "SLUS_203.12", "--json")
        self.assertEqual(json.loads(out), {"serial": "SLUS-20312", "elf_name": "SLUS_203.12",
                                           "prefix": "SLUS", "region": "USA"})
        for form in ("SLUS-20312", "slus20312", "SLUS 20312", "cdrom0:\\SLUS_203.12;1", "/x/y/SLUS_203.12"):
            code, out, _ = run(elf, "serial", form, "--json")
            self.assertEqual(code, 0, form)
            self.assertEqual(json.loads(out)["elf_name"], "SLUS_203.12", form)
        code, out, _ = run(elf, "serial", "SCES-50123")
        self.assertIn("Europe", out)
        code, out, _ = run(elf, "serial", "ABCD-12345", "--json")
        self.assertEqual(json.loads(out)["region"], "unknown region prefix")
        self.assertEqual(run(elf, "serial", "hello")[0], 2)


# ---------------------------------------------------------------- state.py

def fake_ps2recomp(root, cpp_files=3):
    os.makedirs(os.path.join(root, "ps2xRecomp"), exist_ok=True)
    os.makedirs(os.path.join(root, "ps2xRuntime", "src", "runner"), exist_ok=True)
    with open(os.path.join(root, "ps2xRuntime", "CMakeLists.txt"), "w") as f:
        f.write("project(PS2Runtime)\n")
    for i in range(cpp_files):
        with open(os.path.join(root, "ps2xRuntime", "src", "runner", "sub_%08X.cpp" % i), "w") as f:
            f.write("// generated\n")
    build = os.path.join(root, "out", "build")
    os.makedirs(os.path.join(build, "ps2xRuntime"), exist_ok=True)
    with open(os.path.join(build, "CMakeCache.txt"), "w") as f:
        f.write("CMAKE_GENERATOR:INTERNAL=Ninja\nCMAKE_CXX_COMPILER:FILEPATH=/usr/bin/clang++\n"
                "CMAKE_BUILD_TYPE:STRING=Release\n")
    runner = os.path.join(build, "ps2xRuntime", "ps2EntryRunner")
    with open(runner, "w") as f:
        f.write("#!/bin/sh\n")
    return runner


class StateTests(Tmp):
    def setUp(self):
        super().setUp()
        self.ws = os.path.join(self.tmp, "game")
        os.makedirs(os.path.join(self.ws, "console", "disc"))
        self.write("game/console/disc/SYSTEM.CNF", CNF)
        self.write("game/console/disc/SLUS_203.12", make_elf())
        self.repo = os.path.join(self.tmp, "PS2Recomp")
        self.runner = fake_ps2recomp(self.repo)

    def st(self, *argv):
        return run(state, *(list(argv) + ["--workspace", self.ws]))

    def state_text(self):
        with open(os.path.join(self.ws, "console", "STATE.md"), encoding="utf-8") as f:
            return f.read()

    def test_detect_finds_everything_without_listing_runner(self):
        code, out, _ = self.st("detect", "--json")
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertEqual(d["serial"], "SLUS-20312")
        self.assertEqual(d["elf_path"], os.path.join(self.ws, "console", "disc", "SLUS_203.12"))
        self.assertEqual(d["ps2recomp"], self.repo)
        self.assertEqual(d["generator"], "Ninja")
        self.assertEqual(d["build_type"], "Release")
        self.assertEqual(d["runner_exe"], self.runner)
        self.assertEqual(d["runner_cpp_count"], 3)
        self.assertNotIn("sub_00000000", out, "never list runner/")
        code, out, _ = self.st("detect")
        self.assertIn("Runner .cpp files", out)
        self.assertNotIn("sub_0000", out)

    def test_detect_reads_paths_from_the_toml(self):
        self.write("game/console/SLUS-20312.toml",
                   b'[general]\ninput = "/g/SLUS_203.12"\noutput = "C:\\\\work\\\\out"\n'
                   b'ghidra_output = "/g/functions.csv"\n')
        code, out, _ = self.st("detect", "--json")
        d = json.loads(out)
        self.assertEqual(d["recomp_output"], "C:\\work\\out")
        self.assertEqual(d["ghidra_csvs"], ["/g/functions.csv"])
        self.st("init")
        text = self.state_text()
        self.assertIn("- Generated C++ (TOML output): C:\\work\\out", text)
        self.assertIn("- Ghidra function map (CSV): /g/functions.csv", text)

    def test_detect_bad_ps2recomp_path(self):
        code, out, _ = self.st("detect", "--ps2recomp", self.tmp, "--json")
        d = json.loads(out)
        self.assertTrue(any("not a PS2Recomp checkout" in n for n in d["notes"]))

    def test_init_fills_template_and_refuses_overwrite(self):
        code, out, _ = self.st("init", "--game", "Test Racer")
        self.assertEqual(code, 0, out)
        text = self.state_text()
        self.assertNotIn("{{", text)
        self.assertIn("- Title: Test Racer", text)
        self.assertIn("- Serial: SLUS-20312", text)
        self.assertIn("- Region: USA", text)
        self.assertIn("- Generator: Ninja", text)
        self.assertIn("PHASE_SETUP (0 of 5", text)
        self.assertIn('"%s" "%s"' % (self.runner, os.path.join(self.ws, "console", "disc", "SLUS_203.12")), text)
        self.assertRegex(text, r"- \d{4}-\d\d-\d\d \d\d:\d\d  state file created")
        self.assertTrue(os.path.isfile(os.path.join(self.ws, "console", ".gitignore")))
        code, _, err = self.st("init")
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

    def test_init_serial_argument_normalised_and_bad(self):
        shutil.rmtree(os.path.join(self.ws, "console", "disc"))
        code, out, _ = self.st("init", "--serial", "sles_501.23", "--json")
        self.assertEqual(code, 0)
        self.assertIn("- Serial: SLES-50123", self.state_text())
        self.assertIn("- Region: Europe", self.state_text())
        self.assertIn("title", json.loads(out)["unknown"])
        os.remove(os.path.join(self.ws, "console", "STATE.md"))
        self.assertEqual(self.st("init", "--serial", "nope")[0], 2)

    def test_gitignore_not_overwritten(self):
        self.write("game/console/.gitignore", b"mine\n")
        self.st("init")
        with open(os.path.join(self.ws, "console", ".gitignore"), "rb") as f:
            self.assertEqual(f.read(), b"mine\n")

    def test_commands_need_a_state_file(self):
        for argv in (("show",), ("log", "x"), ("phase", "1"), ("set", "title", "x"), ("strike", "k", "n")):
            code, _, err = self.st(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("state.py init", err)

    def test_phase_by_name_number_and_bad(self):
        self.st("init")
        code, out, _ = self.st("phase", "PHASE_EXTRACT")
        self.assertEqual(code, 0)
        self.assertIn("Done when", out)
        self.assertIn("- Phase: PHASE_EXTRACT (1 of 5", self.state_text())
        self.st("phase", "4")
        self.assertIn("- Phase: PHASE_BUILD_RUN (4 of 5", self.state_text())
        self.st("phase", "io_menus")
        self.assertIn("PHASE_IO_MENUS", self.state_text())
        self.assertIn("phase -> PHASE_BUILD_RUN", self.state_text())
        self.assertEqual(self.st("phase", "PHASE_9")[0], 2)
        self.assertEqual(self.st("phase", "6")[0], 2)

    def test_log_appends_dated_lines_in_order(self):
        self.st("init")
        self.st("log", "booted to the first file read")
        self.st("log", "second", "line")
        text = self.state_text()
        sec = text[text.index("## Session log"):]
        self.assertRegex(sec, r"- \d{4}-\d\d-\d\d \d\d:\d\d  booted to the first file read\n")
        self.assertLess(sec.index("booted"), sec.index("second line"))
        self.assertEqual(self.st("log", "   ")[0], 2)

    def test_set_fields_and_runner_cmd(self):
        self.st("init")
        self.assertEqual(self.st("set", "build_dir", "/x/out/build")[0], 0)
        self.assertIn("- Build dir: /x/out/build", self.state_text())
        self.st("set", "runner_cmd", '"/r/ps2EntryRunner" "/g/SLUS_203.12"')
        self.assertIn('```\n"/r/ps2EntryRunner" "/g/SLUS_203.12"\n```', self.state_text())
        self.assertEqual(self.st("set", "nonsense", "x")[0], 2)
        self.assertEqual(self.st("set", "title", " ")[0], 2)
        code, out, _ = self.st("show", "--json")
        fields = json.loads(out)
        self.assertEqual(fields["build_dir"], "/x/out/build")
        self.assertEqual(fields["runner_cmd"], '"/r/ps2EntryRunner" "/g/SLUS_203.12"')

    def test_show_section_and_trimmed_log(self):
        self.st("init")
        for i in range(30):
            self.st("log", "entry %d" % i)
        code, out, _ = self.st("show")
        self.assertIn("earlier log lines hidden", out)
        self.assertIn("entry 29", out)
        self.assertNotIn("entry 3\n", out)
        code, out, _ = self.st("show", "--all")
        self.assertIn("entry 3\n", out)
        code, out, _ = self.st("show", "--section", "Active runner command")
        self.assertTrue(out.startswith("## Active runner command"))
        self.assertNotIn("## Binaries", out)
        self.assertEqual(self.st("show", "--section", "Nope")[0], 2)

    def test_strike_circuit_breaker(self):
        self.st("init")
        key = "PC 0x00123456 hang"
        self.assertEqual(self.st("strike", key, "bound ret0")[0], 0)
        code, out, _ = self.st("strike", key, "bound ret1")
        self.assertEqual(code, 0)
        self.assertIn("Strike 2 of 3", out)
        code, out, _ = self.st("strike", key, "nop patch")
        self.assertEqual(code, 1)
        self.assertIn("STOP", out)
        text = self.state_text()
        self.assertIn("- [%s] 3/3 strikes; last: nop patch" % key, text)
        self.assertEqual(text.count("- [%s]" % key), 1)
        code, out, _ = self.st("strike", "--list")
        self.assertEqual(code, 1)
        self.st("strike", "other", "x")
        self.assertEqual(self.st("strike", key, "--clear")[0], 0)
        text = self.state_text()
        self.assertNotIn("- [%s]" % key, text)
        self.assertIn("- [other] 1/3 strike;", text)
        self.assertEqual(self.st("strike", "--list")[0], 0)
        self.assertEqual(self.st("strike", "bad]key", "x")[0], 2)
        self.assertEqual(self.st("strike")[0], 2)

    def test_design_doc_created_once(self):
        self.st("init", "--game", "Test Racer")
        code, out, _ = self.st("design")
        self.assertEqual(code, 0)
        with open(os.path.join(self.ws, "console", "design.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Test Racer (SLUS-20312)", text)
        self.assertEqual(self.st("design")[0], 1)

    def test_state_found_by_walking_up(self):
        self.st("init")
        old = os.getcwd()
        try:
            os.chdir(os.path.join(self.ws, "console", "disc"))
            code, out, _ = run(state, "show", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(os.path.realpath(json.loads(out)["state"]),
                             os.path.realpath(os.path.join(self.ws, "console", "STATE.md")))
        finally:
            os.chdir(old)


# ---------------------------------------------------------------- the reference's C++ compiles

MOCK_OVERRIDES_H = r"""#pragma once
#include <cstdint>
#include <string_view>
class PS2Runtime;
namespace ps2_game_overrides
{
    using ApplyFn = void (*)(PS2Runtime &runtime);
    struct Descriptor { const char *name = ""; const char *elfName = ""; uint32_t entry = 0; uint32_t crc32 = 0; ApplyFn apply = nullptr; };
    class AutoRegister { public: explicit AutoRegister(const Descriptor &descriptor) { (void)descriptor; } };
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

MOCK_RUNTIME_H = r"""#pragma once
#include <cstdint>
struct R5900Context { uint32_t pc = 0; };
class PS2Runtime
{
public:
    using RecompiledFunction = void (*)(uint8_t *, R5900Context *, PS2Runtime *);
    bool replaceFunction(uint32_t address, RecompiledFunction func);
    bool registerFunction(uint32_t address, RecompiledFunction func);
    RecompiledFunction lookupFunction(uint32_t address);
};
inline uint32_t getRegU32(const R5900Context *ctx, int reg) { (void)ctx; (void)reg; return 0; }
inline void setReturnU32(R5900Context *ctx, uint32_t value) { (void)ctx; (void)value; }
"""


@unittest.skipUnless(shutil.which("c++"), "no C++ compiler on PATH")
class ReferenceSnippetCompiles(unittest.TestCase):
    def test_trace_override_compiles_against_the_real_api_shape(self):
        with open(os.path.join(ROOT, SKILL, "reference", "ghidra-and-pcsx2.md"), encoding="utf-8") as f:
            blocks = [b for b in re.findall(r"```cpp\n(.*?)```", f.read(), re.S)
                      if "PS2_REGISTER_GAME_OVERRIDE(" in b]
        self.assertEqual(len(blocks), 1)
        tmp = tempfile.mkdtemp(prefix="console-reverse-cpp-")
        try:
            for name, body in (("game_overrides.h", MOCK_OVERRIDES_H), ("ps2_runtime.h", MOCK_RUNTIME_H),
                               ("trace.cpp", blocks[0])):
                with open(os.path.join(tmp, name), "w") as f:
                    f.write(body)
            r = subprocess.run(["c++", "-std=c++20", "-fsyntax-only", "-Wall", "-I", tmp,
                                os.path.join(tmp, "trace.cpp")], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr.decode(errors="replace")[-1500:])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- SKILL.md and CLI contract

class SkillContract(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, SKILL, "SKILL.md"), encoding="utf-8") as f:
            self.text = f.read()

    def test_frontmatter(self):
        m = re.match(r"^---\nname: console-reverse\ndescription: >-\n(.*?)\nargument-hint: \"[^\"]+\"\n---\n",
                     self.text, re.S)
        self.assertIsNotNone(m)
        self.assertIn("Reverse Engineer", m.group(1))

    def test_length_and_links(self):
        self.assertLess(len(self.text.splitlines()), 260)
        for rel in re.findall(r"`((?:reference|templates)/[A-Za-z0-9_.-]+)`", self.text):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, SKILL, rel)), rel)
        for rel in re.findall(r"\$\{CLAUDE_SKILL_DIR\}/([A-Za-z0-9_./-]+)", self.text):
            self.assertTrue(os.path.exists(os.path.join(ROOT, SKILL, rel)), rel)
        for sub in ("reference", "templates"):
            for f in os.listdir(os.path.join(ROOT, SKILL, sub)):
                self.assertIn("%s/%s" % (sub, f), self.text)

    def test_no_em_dashes_in_this_skill(self):
        for base, _, files in os.walk(os.path.join(ROOT, SKILL)):
            for f in files:
                if f.endswith((".md", ".py")):
                    with open(os.path.join(base, f), encoding="utf-8") as fh:
                        self.assertNotIn("\u2014", fh.read(), f)
        with open(__file__, encoding="utf-8") as fh:
            self.assertNotIn("\u2014", fh.read())

    def test_adapted_reference_files_credit_the_base(self):
        line = "Adapted from hkmodd/ps2-recomp-Agent-SKILL (MIT), updated for PS2Recomp as of October 2026."
        for f in ("pipeline.md", "guardrails.md", "ghidra-and-pcsx2.md"):
            with open(os.path.join(ROOT, SKILL, "reference", f), encoding="utf-8") as fh:
                self.assertIn(line, fh.read(), f)

    def test_no_dead_paths_from_the_base_skill(self):
        for base, _, files in os.walk(os.path.join(ROOT, SKILL)):
            for f in files:
                with open(os.path.join(base, f), encoding="utf-8") as fh:
                    text = fh.read()
                for dead in ("scripts/vif_gif_surgeon.py", "scripts/install_ghydramcp.py", "Runtime::OverrideFunction",
                             "PS2_PROJECT_STATE.md"):
                    self.assertNotIn(dead, text, "%s mentions %s" % (f, dead))

    def test_every_subcommand_help_runs(self):
        for tool, subs in (("iso.py", ("info", "ls", "extract", "plain", "boot")),
                           ("elf.py", ("info", "crc", "toml", "serial")),
                           ("state.py", ("detect", "init", "design", "show", "set", "phase", "log", "strike"))):
            path = os.path.join(ROOT, SKILL, tool)
            for argv in [["--help"]] + [[s, "--help"] for s in subs]:
                r = subprocess.run([sys.executable, path] + argv, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=30)
                self.assertEqual(r.returncode, 0, "%s %s: %s" % (tool, argv, r.stderr[-300:]))
                self.assertIn(b"usage", r.stdout.lower())

    def test_usage_errors_exit_2_without_traceback(self):
        for tool in ("iso.py", "elf.py", "state.py"):
            r = subprocess.run([sys.executable, os.path.join(ROOT, SKILL, tool)], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=30)
            self.assertEqual(r.returncode, 2, tool)
            self.assertNotIn(b"Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
