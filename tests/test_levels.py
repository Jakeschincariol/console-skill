"""console-levels tests: datascan.py.

Every archive, ELF header, log and data file is synthesized here. The AFS
builder follows the two layouts PuyoTools writes (name-table pointer right
after the table of contents, or just before the first file)."""

import gzip
import io
import json
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
import zlib
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT

ds = load("console-levels", "datascan")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ds.main(argv)
    return code, out.getvalue(), err.getvalue()


def up(v, a):
    return (v + a - 1) // a * a


def blob(n, seed):
    rng = random.Random(seed)
    return bytes(rng.getrandbits(8) for _ in range(n))


def make_afs(entries, version=1, block=2048):
    """entries: [(name, data)]. Returns the archive bytes."""
    n = len(entries)
    first = up(16 + 8 * n, block)
    out = bytearray(first)
    out[0:4] = b"AFS\0"
    struct.pack_into("<I", out, 4, n)
    pos = first
    toc = []
    for name, data in entries:
        if len(out) < pos:
            out += bytes(pos - len(out))
        out += data
        toc.append((pos, len(data)))
        pos = up(pos + len(data), block)
    for i, (o, s) in enumerate(toc):
        struct.pack_into("<II", out, 8 + 8 * i, o, s)
    meta = pos
    out += bytes(meta - len(out))
    struct.pack_into("<II", out, 8 + 8 * n if version == 1 else first - 8, meta, 48 * n)
    for i, (name, data) in enumerate(entries):
        rec = bytearray(48)
        rec[:len(name)] = name
        struct.pack_into("<6H", rec, 32, 2003, 7, 14 + i, 10, 20, 30)
        tail = toc[i][0] if version == 1 else struct.unpack_from("<I", out, 4 + 4 * i)[0]
        struct.pack_into("<I", rec, 44, tail)
        out += rec
    out += bytes(up(len(out), block) - len(out))
    return bytes(out)


def make_toc(blobs, align=16, magic=b"", off_scale=1, kind="count+pairs", sentinel=False, gap_junk=None):
    n = len(blobs)
    stride = 8 if kind == "count+pairs" else 4
    table_at = len(magic) + 4
    table_end = table_at + n * stride + (4 if sentinel else 0)
    pos = up(table_end, max(align, off_scale))
    out = bytearray(pos)
    out[:len(magic)] = magic
    struct.pack_into("<I", out, len(magic), n)
    ents = []
    for i, b in enumerate(blobs):
        pos = up(pos, max(align, off_scale))
        out += bytes(pos - len(out))
        out += b
        ents.append((pos, len(b)))
        pos += len(b)
        if gap_junk is not None and i == gap_junk:
            out += b"\xab" * 8
            pos += 8
    end = up(len(out), max(align, off_scale))
    out += bytes(end - len(out))
    for i, (o, s) in enumerate(ents):
        if kind == "count+pairs":
            struct.pack_into("<II", out, table_at + 8 * i, o // off_scale, s)
        else:
            struct.pack_into("<I", out, table_at + 4 * i, o // off_scale)
    if sentinel:
        struct.pack_into("<I", out, table_at + 4 * n, len(out) // off_scale)
    return bytes(out), ents


def elf(e_type=2, machine=8, iopmod=False, bits=1):
    hdr = bytearray(52)
    hdr[0:4] = b"\x7fELF"
    hdr[4], hdr[5], hdr[6] = bits, 1, 1
    struct.pack_into("<HHI", hdr, 16, e_type, machine, 1)
    if not iopmod:
        return bytes(hdr) + bytes(64)
    strtab = b"\0.iopmod\0.shstrtab\0"
    shoff = 52 + len(strtab)
    struct.pack_into("<I", hdr, 0x20, shoff)
    struct.pack_into("<HHH", hdr, 0x2E, 40, 3, 2)
    sh = bytes(40)
    sh += struct.pack("<10I", 1, 0x70000080, 0, 0, 0, 0, 0, 0, 4, 0)
    sh += struct.pack("<10I", 9, 3, 0, 0, 52, len(strtab), 0, 0, 1, 0)
    return bytes(hdr) + strtab + sh


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def p(self, *parts):
        return os.path.join(self.tmp, *parts)

    def put(self, name, data):
        path = self.p(*name.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def get(self, *parts):
        with open(self.p(*parts), "rb") as fh:
            return fh.read()


AFS_ENTRIES = [(b"STAGE1.DAT", blob(3000, 1)), (b"STAGE1.TM2", b"TIM2" + blob(5000, 2)), (b"MUSIC.VAG", b"VAGp" + blob(100, 3))]


class Afs(Base):
    def test_unpack_repack_is_byte_identical(self):
        for version in (1, 2):
            src = self.put("v%d/LEVEL.AFS" % version, make_afs(AFS_ENTRIES, version))
            out = self.p("v%d" % version, "unpacked")
            code, text, err = run(["unpack", src, "--out", out])
            self.assertEqual(code, 0, err)
            self.assertEqual(sorted(os.listdir(os.path.join(out, "files"))),
                             ["0000_STAGE1.DAT", "0001_STAGE1.TM2", "0002_MUSIC.VAG"])
            with open(os.path.join(out, "index.json")) as fh:
                idx = json.load(fh)
            self.assertEqual(idx["format"], "afs")
            self.assertEqual(idx["alignment"], 2048)
            self.assertEqual(idx["afs"]["meta_where"], "after_toc" if version == 1 else "before_data")
            self.assertEqual(idx["afs"]["name_rule"], "offset" if version == 1 else "header_seq")
            self.assertEqual(idx["entries"][1]["date"], [2003, 7, 15, 10, 20, 30])
            code, text, err = run(["repack", out, "--out", self.p("v%d" % version, "NEW.AFS")])
            self.assertEqual(code, 0, err)
            self.assertIn("layout keep", text)
            self.assertEqual(self.get("v%d" % version, "NEW.AFS"), self.get("v%d" % version, "LEVEL.AFS"))

    def test_bigger_edit_repacks_with_new_offsets(self):
        src = self.put("LEVEL.AFS", make_afs(AFS_ENTRIES, 2))
        out = self.p("u")
        run(["unpack", src, "--out", out])
        bigger = blob(9000, 9)
        self.put("u/files/0000_STAGE1.DAT", bigger)
        code, text, err = run(["repack", out, "--out", self.p("NEW.AFS")])
        self.assertEqual(code, 0, err)
        self.assertIn("layout pack", text)
        self.assertIn("offsets changed", text)
        afs = ds.parse_afs(self.p("NEW.AFS"))
        self.assertEqual(afs["names"], ["STAGE1.DAT", "STAGE1.TM2", "MUSIC.VAG"])
        self.assertEqual(afs["meta_where"], "before_data")
        with open(self.p("NEW.AFS"), "rb") as fh:
            data = fh.read()
        for (o, s), want in zip(afs["entries"], [bigger, AFS_ENTRIES[1][1], AFS_ENTRIES[2][1]]):
            self.assertEqual(o % 2048, 0)
            self.assertEqual(data[o:o + s], want)
        self.assertEqual(len(data) % 2048, 0)

    def test_keep_layout_refuses_growth(self):
        src = self.put("LEVEL.AFS", make_afs(AFS_ENTRIES))
        run(["unpack", src, "--out", self.p("u")])
        self.put("u/files/0001_STAGE1.TM2", blob(7000, 4))
        code, _, err = run(["repack", self.p("u"), "--out", self.p("NEW.AFS"), "--layout", "keep"])
        self.assertEqual(code, 1)
        self.assertIn("slot", err)
        self.assertFalse(os.path.exists(self.p("NEW.AFS")))

    def test_smaller_edit_keeps_every_offset(self):
        orig = make_afs(AFS_ENTRIES)
        src = self.put("LEVEL.AFS", orig)
        run(["unpack", src, "--out", self.p("u")])
        self.put("u/files/0000_STAGE1.DAT", b"short")
        code, text, err = run(["repack", self.p("u"), "--out", self.p("NEW.AFS")])
        self.assertEqual(code, 0, err)
        a, b = ds.parse_afs(src), ds.parse_afs(self.p("NEW.AFS"))
        self.assertEqual([o for o, _ in a["entries"]], [o for o, _ in b["entries"]])
        self.assertEqual(b["entries"][0][1], 5)
        self.assertEqual(len(self.get("NEW.AFS")), len(orig))

    def test_pack_layout_on_request(self):
        src = self.put("LEVEL.AFS", make_afs(AFS_ENTRIES, 1))
        run(["unpack", src, "--out", self.p("u")])
        code, text, err = run(["repack", self.p("u"), "--out", self.p("N.AFS"), "--layout", "pack", "--json"])
        self.assertEqual(code, 0, err)
        rep = json.loads(text)
        self.assertEqual(rep["layout"], "pack")
        self.assertTrue(rep["verified"])

    def test_unsafe_names_stay_inside_the_folder(self):
        src = self.put("EVIL.AFS", make_afs([(b"../../evil.bin", b"x" * 10), (b"a/b\\c", b"y" * 10)]))
        code, _, err = run(["unpack", src, "--out", self.p("u")])
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(self.p("evil.bin")))
        names = os.listdir(self.p("u", "files"))
        self.assertTrue(all("/" not in n and ".." not in n for n in names), names)
        self.assertEqual(len(names), 2)

    def test_index_with_path_tricks_is_refused(self):
        src = self.put("LEVEL.AFS", make_afs(AFS_ENTRIES))
        run(["unpack", src, "--out", self.p("u")])
        with open(self.p("u", "index.json")) as fh:
            idx = json.load(fh)
        idx["entries"][0]["file"] = "../../outside.bin"
        with open(self.p("u", "index.json"), "w") as fh:
            json.dump(idx, fh)
        code, _, err = run(["repack", self.p("u"), "--out", self.p("N.AFS")])
        self.assertEqual(code, 2)
        self.assertIn("outside", err)

    def test_truncated_afs(self):
        data = make_afs(AFS_ENTRIES)
        src = self.put("CUT.AFS", data[:5000])
        code, _, err = run(["unpack", src, "--out", self.p("u")])
        self.assertEqual(code, 2)
        self.assertIn("outside the file", err)

    def test_repack_refuses_to_overwrite(self):
        src = self.put("LEVEL.AFS", make_afs(AFS_ENTRIES))
        run(["unpack", src, "--out", self.p("u")])
        code, _, err = run(["repack", self.p("u"), "--out", src])
        self.assertEqual(code, 2)
        self.assertIn("exists", err)

    def test_unpack_refuses_non_empty_folder(self):
        src = self.put("LEVEL.AFS", make_afs(AFS_ENTRIES))
        self.put("u/keep.txt", b"mine")
        self.assertEqual(run(["unpack", src, "--out", self.p("u")])[0], 2)

    def test_repack_without_index(self):
        os.makedirs(self.p("empty"))
        self.assertEqual(run(["repack", self.p("empty"), "--out", self.p("x")])[0], 2)


class GenericToc(Base):
    BLOBS = [blob(700, 11), blob(1500, 12), blob(33, 13), blob(2100, 14)]

    def test_count_pairs_detected_and_round_trips(self):
        data, ents = make_toc(self.BLOBS)
        src = self.put("STAGE.PAK", data)
        cands = ds.detect_toc(data, len(data))
        best = cands[0]
        self.assertEqual((best["kind"], best["header"], best["n"]), ("count+pairs", 0, 4))
        self.assertGreaterEqual(best["score"], 0.8)
        self.assertEqual(best["entries"], ents)
        code, _, err = run(["unpack", src, "--out", self.p("u")])
        self.assertEqual(code, 0, err)
        code, _, err = run(["repack", self.p("u"), "--out", self.p("NEW.PAK")])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.get("NEW.PAK"), data)

    def test_header_magic_before_the_count(self):
        data, ents = make_toc(self.BLOBS, magic=b"PAK1")
        best = ds.detect_toc(data, len(data))[0]
        self.assertEqual((best["kind"], best["header"]), ("count+pairs", 4))

    def test_sector_offsets(self):
        data, ents = make_toc(self.BLOBS, off_scale=2048)
        best = ds.detect_toc(data, len(data))[0]
        self.assertEqual((best["off_scale"], best["size_scale"]), (2048, 1))
        src = self.put("SECT.BIN", data)
        run(["unpack", src, "--out", self.p("u")])
        self.assertEqual(run(["repack", self.p("u"), "--out", self.p("same.bin")])[0], 0)
        self.assertEqual(self.get("same.bin"), data)
        # grow the first entry: offsets move, stay sector-aligned, table in sectors
        self.put("u/files/0000.bin", blob(5000, 99))
        code, text, err = run(["repack", self.p("u"), "--out", self.p("grown.bin")])
        self.assertEqual(code, 0, err)
        new = self.get("grown.bin")
        for i in range(4):
            off = struct.unpack_from("<I", new, 4 + 8 * i)[0] * 2048
            size = struct.unpack_from("<I", new, 8 + 8 * i)[0]
            want = blob(5000, 99) if i == 0 else self.BLOBS[i]
            self.assertEqual(new[off:off + size], want)

    def test_count_offsets_with_end_sentinel(self):
        data, ents = make_toc(self.BLOBS, kind="count+offsets", sentinel=True)
        best = ds.detect_toc(data, len(data))[0]
        self.assertEqual(best["kind"], "count+offsets")
        self.assertTrue(best["sentinel"])
        src = self.put("OFFS.BIN", data)
        self.assertEqual(run(["unpack", src, "--out", self.p("u")])[0], 0)
        self.assertEqual(run(["repack", self.p("u"), "--out", self.p("same.bin")])[0], 0)
        self.assertEqual(self.get("same.bin"), data)

    def test_bytes_outside_the_table_are_kept(self):
        data, ents = make_toc(self.BLOBS, gap_junk=1)
        src = self.put("JUNK.PAK", data)
        code, text, err = run(["unpack", src, "--out", self.p("u")])
        self.assertEqual(code, 0, err)
        self.assertIn("_extra_", text)
        self.assertEqual(run(["repack", self.p("u"), "--out", self.p("same.pak")])[0], 0)
        self.assertEqual(self.get("same.pak"), data)
        # a pack layout cannot place bytes between entries: it says so and exits 1
        self.put("u/files/0000.bin", blob(4000, 5))
        code, text, err = run(["repack", self.p("u"), "--out", self.p("grown.pak")])
        self.assertEqual(code, 1)
        self.assertIn("dropped", text)

    def test_random_data_has_no_table(self):
        data = blob(20000, 7)
        self.assertFalse([c for c in ds.detect_toc(data, len(data)) if c["score"] >= ds.TOC_THRESHOLD])
        src = self.put("noise.bin", data)
        code, _, err = run(["unpack", src, "--out", self.p("u")])
        self.assertEqual(code, 1)
        self.assertIn("per-game work", err)

    def test_zero_filled_file_has_no_table(self):
        self.assertEqual(ds.detect_toc(bytes(4096), 4096), [])

    def test_force_layout_hint(self):
        data, _ = make_toc(self.BLOBS, magic=b"PAK1")
        src = self.put("H.PAK", data)
        code, _, err = run(["unpack", src, "--out", self.p("u"), "--header", "4", "--layout", "count+pairs"])
        self.assertEqual(code, 0, err)
        with open(self.p("u", "index.json")) as fh:
            self.assertEqual(json.load(fh)["layout"]["header"], 4)


class ScanAndSniff(Base):
    def make_tree(self):
        afs = make_afs(AFS_ENTRIES)
        toc, _ = make_toc(GenericToc.BLOBS)
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w") as z:
            z.writestr("a.txt", "hello")
        files = {
            "SYSTEM.CNF": b"BOOT2 = cdrom0:\\SLUS_203.12;1\nVER = 1.00\nVMODE = NTSC\n",
            "SLUS_203.12": elf(2),
            "MODULES/SIO2MAN.IRX": elf(0xFF80),
            "MODULES/OLD.IRX": elf(1, iopmod=True),
            "OTHER.ELF": elf(2, machine=40),
            "DATA/LEVEL.AFS": afs,
            "DATA/STAGE.PAK": toc,
            "DATA/TITLE.TM2": b"TIM2" + bytes(60),
            "SOUND/SE.VAG": b"VAGp" + bytes(60),
            "SOUND/SE.HD": b"IECSsreV" + bytes(56),
            "MOVIE/OPEN.PSS": b"\x00\x00\x01\xba" + bytes(60),
            "MOVIE/OPEN.IPU": b"ipum" + bytes(60),
            "PACK/A.GZ": gzip.compress(b"x" * 1000),
            "PACK/B.Z": zlib.compress(b"y" * 1000),
            "PACK/C.ZIP": zbuf.getvalue(),
            "PACK/D.WAV": b"RIFF" + bytes(4) + b"WAVE" + bytes(40),
            "PACK/E.CPK": b"CPK " + bytes(60),
            "PACK/NOISE.BIN": blob(5000, 3),
            "PACK/ZERO.BIN": bytes(5000),
            "EMPTY.DAT": b"",
        }
        for name, data in files.items():
            self.put("disc/" + name, data)
        return files

    def test_scan_names_every_kind(self):
        self.make_tree()
        code, out, err = run(["scan", self.p("disc"), "--json"])
        self.assertEqual(code, 0, err)
        rows = {r["path"].replace(os.sep, "/"): r for r in json.loads(out)}
        kinds = {k: v["kind"] for k, v in rows.items()}
        self.assertEqual(kinds["SYSTEM.CNF"], "text")
        self.assertEqual(kinds["SLUS_203.12"], "ELF")
        self.assertEqual(rows["SLUS_203.12"]["detail"], "MIPS executable (EE)")
        self.assertEqual(kinds["MODULES/SIO2MAN.IRX"], "IRX")
        self.assertEqual(kinds["MODULES/OLD.IRX"], "IRX")
        self.assertIn("not MIPS", rows["OTHER.ELF"]["detail"])
        for path, kind in (("DATA/LEVEL.AFS", "AFS"), ("DATA/TITLE.TM2", "TIM2"), ("SOUND/SE.VAG", "VAG"),
                           ("SOUND/SE.HD", "IECS"), ("MOVIE/OPEN.PSS", "MPEG-PS"), ("MOVIE/OPEN.IPU", "IPU"),
                           ("PACK/A.GZ", "gzip"), ("PACK/B.Z", "zlib"), ("PACK/C.ZIP", "ZIP"),
                           ("PACK/D.WAV", "RIFF"), ("PACK/E.CPK", "CPK"), ("PACK/NOISE.BIN", "data"),
                           ("EMPTY.DAT", "empty")):
            self.assertEqual(kinds[path], kind, path)
        self.assertEqual(rows["DATA/LEVEL.AFS"]["toc"]["n"], 3)
        self.assertEqual(rows["DATA/STAGE.PAK"]["toc"]["kind"], "count+pairs")
        self.assertNotIn("toc", rows["PACK/NOISE.BIN"])
        self.assertGreater(rows["PACK/NOISE.BIN"]["entropy"], 7.5)
        self.assertEqual(rows["PACK/ZERO.BIN"]["entropy"], 0.0)

    def test_scan_text_summary_and_sort(self):
        self.make_tree()
        code, out, _ = run(["scan", self.p("disc"), "--sort", "size"])
        self.assertEqual(code, 0)
        self.assertIn("By kind:", out)
        self.assertIn("Biggest:", out)
        self.assertIn("AFS, 3 entries, names", out)

    def test_scan_embedded_signatures(self):
        inner = b"junk" * 10 + b"TIM2" + bytes(20) + b"VAGp" + bytes(40) + b"TIM2"
        self.put("big.bin", inner)
        code, out, _ = run(["scan", self.p("big.bin"), "--embedded", "--json"])
        self.assertEqual(code, 0)
        emb = json.loads(out)[0]["embedded"]
        self.assertEqual(emb["TIM2"]["count"], 2)
        self.assertEqual(emb["TIM2"]["first_offsets"], [40, 108])
        self.assertEqual(emb["VAG"]["count"], 1)

    def test_embedded_across_read_chunks(self):
        data = (b"\x01" * 61 + b"TIM2") * 20
        path = self.put("c.bin", data)
        old = ds.CHUNK
        ds.CHUNK = 64
        try:
            found = ds.embedded_signatures(path)
        finally:
            ds.CHUNK = old
        self.assertEqual(found["TIM2"]["count"], 20)
        self.assertEqual(found["TIM2"]["first_offsets"][:3], [61, 126, 191])

    def test_scan_missing(self):
        self.assertEqual(run(["scan", self.p("nope")])[0], 2)

    def test_entropy_bounds(self):
        self.assertEqual(ds.entropy(b""), 0.0)
        self.assertAlmostEqual(ds.entropy(bytes(range(256))), 8.0)


class Diff(Base):
    def test_ranges_merge_and_values(self):
        a = bytearray(1000)
        b = bytearray(1000)
        struct.pack_into("<f", b, 100, 9.8)
        b[110] = 1
        b[500] = 7
        self.put("a.bin", bytes(a))
        self.put("b.bin", bytes(b))
        code, out, _ = run(["diff", self.p("a.bin"), self.p("b.bin"), "--json", "--as", "f32"])
        self.assertEqual(code, 1)
        res = json.loads(out)
        self.assertEqual([(r["start"], r["end"]) for r in res["ranges"]], [(100, 111), (500, 501)])
        self.assertEqual(res["ranges"][0]["b_values"][0], "9.8")
        code, out, _ = run(["diff", self.p("a.bin"), self.p("b.bin"), "--json", "--gap", "2"])
        self.assertEqual(len(json.loads(out)["ranges"]), 3)

    def test_identical_and_size_change(self):
        self.put("a.bin", b"same")
        self.put("b.bin", b"same")
        code, out, _ = run(["diff", self.p("a.bin"), self.p("b.bin")])
        self.assertEqual((code, out.strip()), (0, "identical (4 bytes)"))
        self.put("c.bin", b"same plus")
        code, out, _ = run(["diff", self.p("a.bin"), self.p("c.bin")])
        self.assertEqual(code, 1)
        self.assertIn("sizes differ", out)

    def test_ranges_across_read_chunks(self):
        a = bytes(300)
        b = bytearray(300)
        b[60:70] = b"\x01" * 10  # crosses the 64-byte chunk edge
        self.put("a.bin", a)
        self.put("b.bin", bytes(b))
        old = ds.CHUNK
        ds.CHUNK = 64
        try:
            ranges, count, _, _, _ = ds.diff_ranges(self.p("a.bin"), self.p("b.bin"))
        finally:
            ds.CHUNK = old
        self.assertEqual((ranges, count), ([(60, 70)], 10))

    def test_folders(self):
        self.put("x/same.bin", b"1")
        self.put("y/same.bin", b"1")
        self.put("x/level.dat", b"abcd")
        self.put("y/level.dat", b"abXd")
        self.put("x/gone.bin", b"g")
        self.put("y/new.bin", b"n")
        code, out, _ = run(["diff", self.p("x"), self.p("y"), "--json"])
        self.assertEqual(code, 1)
        res = json.loads(out)
        self.assertEqual(res["added"], ["new.bin"])
        self.assertEqual(res["removed"], ["gone.bin"])
        self.assertEqual([c["path"] for c in res["changed"]], ["level.dat"])

    def test_file_against_folder(self):
        self.put("a.bin", b"1")
        os.makedirs(self.p("d"))
        self.assertEqual(run(["diff", self.p("a.bin"), self.p("d")])[0], 2)


class Strings(Base):
    def test_ascii_with_offsets(self):
        self.put("s.bin", b"\0\0STAGE_01\0\x01ab\0enemy_spawn\xff")
        code, out, _ = run(["strings", self.p("s.bin"), "--json"])
        rows = json.loads(out)
        self.assertEqual([(r["offset"], r["text"]) for r in rows], [(2, "STAGE_01"), (15, "enemy_spawn")])

    def test_utf16_and_sjis(self):
        jp = u"ステージ１".encode("cp932")
        self.put("s.bin", b"\x01\x02" + "Level".encode("utf-16-le") + b"\0\0\x03" + jp + b"\0\x9f\xff")
        code, out, _ = run(["strings", self.p("s.bin"), "--encoding", "all", "--json"])
        rows = json.loads(out)
        texts = {(r["encoding"], r["text"]) for r in rows}
        self.assertIn(("utf16le", "Level"), texts)
        self.assertIn(("sjis", u"ステージ１"), texts)

    def test_sjis_mostly_ignores_random_bytes(self):
        path = self.put("noise.bin", blob(50000, 21))
        self.assertLess(len(list(ds.iter_strings(path, 4, "sjis"))), 20)

    def test_grep_and_max(self):
        self.put("s.bin", b"alpha\0BOSS_ROOM\0boss_hp\0other\0")
        code, out, _ = run(["strings", self.p("s.bin"), "--grep", "boss"])
        self.assertEqual(len(out.strip().splitlines()), 2)
        code, out, _ = run(["strings", self.p("s.bin"), "--max", "1"])
        self.assertIn("more", out)

    def test_strings_across_read_chunks(self):
        data = b"\0" * 60 + b"crossing_the_edge" + b"\0" * 10 + b"ab" + b"\0" * 50 + b"tail_string"
        path = self.put("c.bin", data)
        old = ds.CHUNK
        ds.CHUNK = 64
        try:
            rows = list(ds.iter_strings(path, 4, "ascii"))
        finally:
            ds.CHUNK = old
        self.assertEqual([(o, t) for o, _, t in rows], [(60, "crossing_the_edge"), (139, "tail_string")])

    def test_strings_on_folder(self):
        self.assertEqual(run(["strings", self.tmp])[0], 2)


class Files(Base):
    LOG = (
        "Using argv boot path\n"
        "ELF file loaded successfully. Entry point: 0x00100008\n"
        '[sceCdSearchFile] pc=0x1000 ra=0x2000 file=0x3000 pathAddr=0x4000 path="\\DATA\\STAGE1.AFS;1"\n'
        '[sceCdSearchFile:ok] path="\\DATA\\STAGE1.AFS;1" lsn=0x1a2 size=0x8000 sectors=0x10\n'
        "sceCdSearchFile failed: \\DATA\\MISSING.BIN;1 (root: /games/x, repeat=1)\n"
        '[sceCdSearchFile:ok] path="\\DATA\\STAGE1.AFS;1" lsn=0x1a2 size=0x8000 sectors=0x10\n'
        '[sceCdSearchFile:ok] path="\\SOUND\\BGM.VAG;1" lsn=0x300 size=0x100 sectors=0x1\n'
    )

    def test_files_from_a_runtime_log(self):
        log = self.put("run.log", self.LOG.encode())
        code, out, _ = run(["files", log, "--json"])
        self.assertEqual(code, 1)
        rows = json.loads(out)
        self.assertEqual([r["path"] for r in rows], ["\\DATA\\STAGE1.AFS;1", "\\DATA\\MISSING.BIN;1", "\\SOUND\\BGM.VAG;1"])
        self.assertEqual(rows[0]["lookups"], 2)
        self.assertEqual(rows[0]["size"], 0x8000)
        self.assertEqual(rows[1]["status"], "missing")

    def test_no_lookups(self):
        log = self.put("run.log", b"nothing here\n")
        code, out, _ = run(["files", log])
        self.assertEqual(code, 0)
        self.assertIn("no sceCdSearchFile", out)


class Cli(unittest.TestCase):
    def test_help_everywhere(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "console-levels", "datascan.py"), "--help"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(r.returncode, 0)
        self.assertIn(b"usage", r.stdout)
        for cmd in ("scan", "unpack", "repack", "diff", "strings", "files"):
            out = io.StringIO()
            with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
                ds.main([cmd, "--help"])
            self.assertEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
