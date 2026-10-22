"""console-saves tests: memcard.py.

Every card image here is formatted in code (with and without ECC) and holds
two made-up saves. No real save, card dump or game file is used."""

import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT

mc = load("console-saves", "memcard")

MAGIC = b"Sony PS2 Memory Card Format "
DIRENT = struct.Struct("<HHL8sLL8sL28x448s")
TOD = struct.Struct("<xBBBBBH")
MODE_DIR, MODE_FILE = 0x8427, 0x8497
FREE, END = 0x7FFFFFFF, 0xFFFFFFFF


def rb(path):
    with open(path, "rb") as fh:
        return fh.read()


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mc.main(argv)
    return code, out.getvalue(), err.getvalue()


def tod(y, mo, d, h, mi, s):
    return TOD.pack(s, mi, h, d, mo, y)


def icon_sys(line1, line2):
    """A minimal icon.sys: PS2D magic, line-break offset, Shift-JIS title at 0xC0."""
    blob = bytearray(964)
    blob[0:4] = b"PS2D"
    t1, t2 = line1.encode("cp932"), line2.encode("cp932")
    struct.pack_into("<H", blob, 6, len(t1))
    title = t1 + t2
    blob[0xC0:0xC0 + len(title)] = title
    blob[0x104:0x104 + 8] = b"icon.ico"
    return bytes(blob)


T_SAVE = tod(2004, 5, 6, 7, 8, 9)
SAVES = [
    (b"BASLUS-20312GAME", T_SAVE, [
        (b"icon.sys", icon_sys(u"ＧＡＭＥ", u"セーブ")),
        (b"DATA", bytes((i * 7 + 3) & 0xFF for i in range(3000))),
        (b"icon.ico", b"\x01" * 100),
    ]),
    (b"BESLES-50001SAVE", tod(2005, 12, 31, 23, 59, 58), [
        (b"BIG.DAT", bytes((i * 13) & 0xFF for i in range(2048))),
        (b"EMPTY", b""),
    ]),
]


def build_card(saves=SAVES, ecc=False, clusters=8192, deleted=True):
    """Format a card image the way mcman/mymc lay it out, then add the saves.

    Clusters are handed out in a scattered order so file chains are not
    contiguous."""
    page, ppc, ppb, cs = 512, 2, 16, 1024
    alloc_offset = 41
    backup2 = clusters // (ppb // ppc) - 2
    alloc_end = backup2 * (ppb // ppc) - alloc_offset
    img = bytearray(clusters * cs)
    sb = bytearray(0x154)
    sb[0:28] = MAGIC
    sb[0x1C:0x23] = b"1.2.0.0"
    struct.pack_into("<HHHH", sb, 0x28, page, ppc, ppb, 0xFF00)
    struct.pack_into("<6I", sb, 0x30, clusters, alloc_offset, alloc_end, 0, backup2 + 1, backup2)
    nfat = -(-alloc_end // 256)
    struct.pack_into("<I", sb, 0x50, 8)
    sb[0xD0:0x150] = b"\xff" * 128
    sb[0x150], sb[0x151] = 2, 0x52
    img[0:len(sb)] = sb
    for i in range(nfat):
        struct.pack_into("<I", img, 8 * cs + 4 * i, 9 + i)
    fat = [FREE] * (nfat * 256)
    pool = list(range(1, alloc_end))
    pool = pool[::3] + pool[1::3] + pool[2::3]

    def alloc(n, first=None):
        got = [first] if first is not None else []
        while len(got) < n:
            got.append(pool.pop(0))
        for a, b in zip(got, got[1:]):
            fat[a] = 0x80000000 | b
        fat[got[-1]] = END
        return got

    def write(chain, blob):
        for i, c in enumerate(chain):
            piece = blob[i * cs:(i + 1) * cs]
            at = (alloc_offset + c) * cs
            img[at:at + len(piece)] = piece

    def dirent(mode, length, t, cluster, name, parent=0):
        return DIRENT.pack(mode, 0, length, t, cluster, parent, t, 0, name)

    root_entries = [None, None]
    for name, t, files in saves:
        ents = [None, None]
        for fname, data in files:
            if data:
                ch = alloc(-(-len(data) // cs))
                write(ch, data)
                ents.append(dirent(MODE_FILE, len(data), t, ch[0], fname))
            else:
                ents.append(dirent(MODE_FILE, 0, t, FREE, fname))
        ch = alloc(-(-len(ents) // 2))
        ents[0] = dirent(MODE_DIR, len(ents), t, 0, b".")
        ents[1] = dirent(0xA426, 0, t, 0, b"..")
        write(ch, b"".join(ents))
        root_entries.append(dirent(MODE_DIR, len(ents), t, ch[0], name))
    if deleted:
        root_entries.append(dirent(MODE_DIR & 0x7FFF, 2, T_SAVE, 5, b"OLDSAVE"))
    root_entries[0] = dirent(MODE_DIR, len(root_entries), T_SAVE, 0, b".")
    root_entries[1] = dirent(0xA426, 0, T_SAVE, 0, b"..")
    rch = alloc(-(-len(root_entries) // 2), first=0)
    write(rch, b"".join(root_entries))
    for i, v in enumerate(fat):
        struct.pack_into("<I", img, (9 + i // 256) * cs + 4 * (i % 256), v)
    if not ecc:
        return bytes(img)
    spare = b"\x5a" * 16
    return b"".join(img[p * page:(p + 1) * page] + spare for p in range(clusters * ppc))


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cards = tempfile.mkdtemp()
        cls.plain = os.path.join(cls.cards, "card.mcd")
        cls.ecc = os.path.join(cls.cards, "card.ps2")
        with open(cls.plain, "wb") as fh:
            fh.write(build_card(ecc=False))
        with open(cls.ecc, "wb") as fh:
            fh.write(build_card(ecc=True))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.cards, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def p(self, *parts):
        return os.path.join(self.tmp, *parts)

    def read(self, *parts):
        with open(self.p(*parts), "rb") as fh:
            return fh.read()

    def export(self, mc0="mc0", card=None):
        code, out, err = run(["export", card or self.plain, "--to", self.p("game", mc0)])
        self.assertEqual(code, 0, err)
        return self.p("game", mc0)


class CardImage(Base):
    def test_sizes_are_the_standard_8mb_card(self):
        self.assertEqual(os.path.getsize(self.plain), 8388608)
        self.assertEqual(os.path.getsize(self.ecc), 8650752)

    def test_info_without_and_with_ecc(self):
        for path, ecc in ((self.plain, False), (self.ecc, True)):
            code, out, err = run(["info", path, "--json"])
            self.assertEqual(code, 0, err)
            res = json.loads(out)
            self.assertEqual(res["ecc"], ecc)
            self.assertEqual(res["version"], "1.2.0.0")
            self.assertEqual(res["clusters_per_card"], 8192)
            self.assertEqual(res["alloc_offset"], 41)
            self.assertEqual(res["alloc_end"], 8135)
            self.assertEqual(res["card_type"], 2)
            self.assertEqual(res["usable_kb"], 8135)
            # used: root 3 clusters (5 entries), save dirs 3 + 2, files 1 + 3 + 1 and 2
            self.assertEqual(res["free_kb"], 8135 - (3 + 3 + 2 + 1 + 3 + 1 + 2))

    def test_info_text(self):
        code, out, _ = run(["info", self.ecc])
        self.assertEqual(code, 0)
        self.assertIn("with ECC", out)
        self.assertIn("8 MB", out)

    def test_ls_both_saves_with_titles_and_serials(self):
        for path in (self.plain, self.ecc):
            code, out, err = run(["ls", path, "--json"])
            self.assertEqual(code, 0, err)
            rows = json.loads(out)
            self.assertEqual([r["name"] for r in rows], ["BASLUS-20312GAME", "BESLES-50001SAVE"])
            game = rows[0]
            self.assertEqual(game["title"], u"GAME / セーブ")
            self.assertEqual(game["serial"], "SLUS-20312")
            self.assertEqual(game["region"], "USA")
            self.assertEqual(game["files"], 3)
            self.assertEqual(game["bytes"], 964 + 3000 + 100)
            self.assertEqual(game["modified"], "2004-05-06 07:08")
            self.assertEqual(rows[1]["region"], "Europe")
            self.assertIsNone(rows[1]["title"])

    def test_deleted_entries_are_hidden(self):
        code, out, _ = run(["ls", self.plain])
        self.assertEqual(code, 0)
        self.assertNotIn("OLDSAVE", out)
        self.assertIn("2 save(s)", out)

    def test_ls_files_lists_modes(self):
        code, out, _ = run(["ls", self.plain, "--files"])
        self.assertEqual(code, 0)
        self.assertIn("DATA", out)
        self.assertIn("0x8497", out)

    def test_card_reads_the_same_with_and_without_ecc(self):
        a, b = mc.Card(self.plain), mc.Card(self.ecc)
        try:
            ta, tb = a.tree(), b.tree()
            for ea, eb in zip(ta, tb):
                for fa, fb in zip(ea.children, eb.children):
                    self.assertEqual(a.read_file(fa), b.read_file(fb))
        finally:
            a.close()
            b.close()

    def test_wrong_magic_is_refused(self):
        bad = self.p("bad.ps2")
        with open(bad, "wb") as fh:
            fh.write(b"\0" * 8388608)
        code, _, err = run(["ls", bad])
        self.assertEqual(code, 2)
        self.assertIn("not a PS2 memory card image", err)

    def test_truncated_card_is_refused(self):
        cut = self.p("cut.ps2")
        with open(self.ecc, "rb") as src, open(cut, "wb") as dst:
            dst.write(src.read(5000000))
        code, _, err = run(["info", cut])
        self.assertEqual(code, 2)
        self.assertIn("truncated", err)
        self.assertNotIn("Traceback", err)

    def test_tiny_file_is_refused(self):
        tiny = self.p("tiny.ps2")
        with open(tiny, "wb") as fh:
            fh.write(MAGIC)
        self.assertEqual(run(["info", tiny])[0], 2)

    def test_missing_card(self):
        self.assertEqual(run(["ls", self.p("nope.ps2")])[0], 2)

    def _patch_fat(self, rel, value):
        img = bytearray(build_card())
        struct.pack_into("<I", img, (9 + rel // 256) * 1024 + 4 * (rel % 256), value)
        path = self.p("broken.mcd")
        with open(path, "wb") as fh:
            fh.write(img)
        return path

    def _data_chain_start(self):
        card = mc.Card(self.plain)
        try:
            data = [c for c in card.tree()[0].children if c.name == "DATA"][0]
            return data.cluster
        finally:
            card.close()

    def test_fat_loop_is_reported_not_followed(self):
        start = self._data_chain_start()
        path = self._patch_fat(start, 0x80000000 | start)
        code, out, err = run(["export", path, "--to", self.p("mc0")])
        self.assertEqual(code, 1)
        self.assertIn("loops", out)

    def test_damaged_save_is_skipped_and_the_rest_come_over(self):
        start = self._data_chain_start()
        path = self._patch_fat(start, 0x80000000 | start)
        run(["export", path, "--to", self.p("mc0")])
        # no half-written save, no temp folder: only the healthy save
        self.assertEqual(os.listdir(self.p("mc0")), ["BESLES-50001SAVE"])

    def test_chain_into_free_cluster_is_reported(self):
        start = self._data_chain_start()
        path = self._patch_fat(start, 0x00000123)
        code, out, err = run(["export", path, "--to", self.p("mc0")])
        self.assertEqual(code, 1)
        self.assertIn("free cluster", out)

    def test_short_chain_is_reported(self):
        start = self._data_chain_start()
        path = self._patch_fat(start, END)
        code, _, err = run(["ls", path, "--json"])
        self.assertEqual(code, 0)  # ls reads only the directories
        code, out, err = run(["export", path, "--to", self.p("mc0")])
        self.assertEqual(code, 1)
        self.assertIn("damaged", out)

    def test_damaged_root_directory_is_exit_2(self):
        img = bytearray(build_card(clusters=1024))
        # root "." entry claims 0 entries: nothing can be listed
        struct.pack_into("<I", img, 41 * 1024 + 4, 0)
        path = self.p("noroot.mcd")
        with open(path, "wb") as fh:
            fh.write(img)
        code, _, err = run(["ls", path])
        self.assertEqual(code, 2)
        self.assertIn("damaged", err)

    def test_smaller_card_geometry_from_the_superblock(self):
        path = self.p("small.mcd")
        with open(path, "wb") as fh:
            fh.write(build_card(clusters=1024))
        code, out, err = run(["info", path, "--json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["clusters_per_card"], 1024)
        self.assertEqual(run(["ls", path])[0], 0)


class Export(Base):
    def test_export_matches_the_runtime_folder_layout(self):
        mc0 = self.export()
        # <elf dir>/mc0/<SAVE>/<file>, exactly the names on the card
        self.assertEqual(sorted(os.listdir(mc0)), ["BASLUS-20312GAME", "BESLES-50001SAVE"])
        self.assertEqual(self.read("game", "mc0", "BASLUS-20312GAME", "DATA"), SAVES[0][2][1][1])
        self.assertEqual(self.read("game", "mc0", "BESLES-50001SAVE", "BIG.DAT"), SAVES[1][2][0][1])
        self.assertEqual(self.read("game", "mc0", "BESLES-50001SAVE", "EMPTY"), b"")
        self.assertFalse(os.path.exists(os.path.join(mc0, "OLDSAVE")))

    def test_export_from_ecc_card(self):
        mc0 = self.export(card=self.ecc)
        self.assertEqual(self.read("game", "mc0", "BASLUS-20312GAME", "icon.ico"), b"\x01" * 100)

    def test_export_sets_times_from_the_card(self):
        mc0 = self.export()
        st = os.stat(os.path.join(mc0, "BASLUS-20312GAME", "DATA"))
        # 2004-05-06 07:08:09 in Japan time is 2004-05-05 22:08:09 UTC
        self.assertEqual(int(st.st_mtime), 1083794889)

    def test_export_refuses_to_overwrite_without_force(self):
        mc0 = self.export()
        target = os.path.join(mc0, "BASLUS-20312GAME", "DATA")
        with open(target, "wb") as fh:
            fh.write(b"newer progress")
        code, _, err = run(["export", self.plain, "--to", mc0])
        self.assertEqual(code, 1)
        self.assertIn("--force", err)
        self.assertEqual(self.read("game", "mc0", "BASLUS-20312GAME", "DATA"), b"newer progress")

    def test_export_force_backs_up_first(self):
        mc0 = self.export()
        with open(os.path.join(mc0, "BASLUS-20312GAME", "DATA"), "wb") as fh:
            fh.write(b"newer progress")
        backups = self.p("bk")
        code, out, err = run(["export", self.plain, "--to", mc0, "--force", "--backups", backups])
        self.assertEqual(code, 0, err)
        zips = os.listdir(backups)
        self.assertEqual(len(zips), 1)
        with zipfile.ZipFile(os.path.join(backups, zips[0])) as z:
            self.assertEqual(z.read("BASLUS-20312GAME/DATA"), b"newer progress")
        self.assertEqual(self.read("game", "mc0", "BASLUS-20312GAME", "DATA"), SAVES[0][2][1][1])

    def test_export_only_one_save(self):
        code, _, err = run(["export", self.plain, "--to", self.p("mc0"), "--only", "BESLES-50001SAVE"])
        self.assertEqual(code, 0, err)
        self.assertEqual(os.listdir(self.p("mc0")), ["BESLES-50001SAVE"])

    def test_export_only_unknown_save(self):
        code, _, err = run(["export", self.plain, "--to", self.p("mc0"), "--only", "NOPE"])
        self.assertEqual(code, 2)
        self.assertIn("NOPE", err)

    def test_export_from_a_pcsx2_folder_card_skips_its_metadata(self):
        src = self.p("Mcd001.ps2")
        os.makedirs(os.path.join(src, "BASLUS-20312GAME"))
        for rel, data in (("_pcsx2_superblock", b"x"), ("BASLUS-20312GAME/_pcsx2_index", b"y"),
                          ("BASLUS-20312GAME/DATA", b"save bytes")):
            with open(os.path.join(src, *rel.split("/")), "wb") as fh:
                fh.write(data)
        code, out, err = run(["export", src, "--to", self.p("mc0")])
        self.assertEqual(code, 0, err)
        self.assertEqual(os.listdir(self.p("mc0", "BASLUS-20312GAME")), ["DATA"])
        self.assertEqual(self.read("mc0", "BASLUS-20312GAME", "DATA"), b"save bytes")

    def test_export_to_itself_is_refused(self):
        os.makedirs(self.p("mc0"))
        self.assertEqual(run(["export", self.p("mc0"), "--to", self.p("mc0")])[0], 2)

    def test_ls_of_an_mc0_folder(self):
        mc0 = self.export()
        code, out, err = run(["ls", mc0, "--json"])
        self.assertEqual(code, 0, err)
        rows = json.loads(out)
        self.assertEqual(rows[0]["title"], u"GAME / セーブ")
        self.assertEqual(rows[1]["files"], 2)

    def test_unsafe_card_names_are_skipped(self):
        saves = [(b"BASLUS-20312GAME", T_SAVE, [(b"OK", b"fine"), (b"A:B", b"bad")])]
        path = self.p("odd.mcd")
        with open(path, "wb") as fh:
            fh.write(build_card(saves=saves, clusters=1024, deleted=False))
        code, out, err = run(["export", path, "--to", self.p("mc0")])
        self.assertEqual(code, 1)
        self.assertIn("WARNING", out)
        self.assertEqual(os.listdir(self.p("mc0", "BASLUS-20312GAME")), ["OK"])


class Psu(Base):
    def test_round_trip_through_psu(self):
        mc0 = self.export()
        out = self.p("save.psu")
        code, _, err = run(["psu", "export", os.path.join(mc0, "BASLUS-20312GAME"), "--out", out])
        self.assertEqual(code, 0, err)
        blob = rb(out)
        self.assertEqual(len(blob) % 512, 0)
        head = DIRENT.unpack(blob[:512])
        self.assertEqual(head[0], MODE_DIR)
        self.assertEqual(head[2], 3 + 2)  # three files plus . and ..
        self.assertEqual(head[4], 0)      # cluster fields are 0
        self.assertEqual(head[8][:16], b"BASLUS-20312GAME")
        self.assertEqual(DIRENT.unpack(blob[512:1024])[8][:2], b".\0")
        self.assertEqual(DIRENT.unpack(blob[1024:1536])[8][:3], b"..\0")
        first = DIRENT.unpack(blob[1536:2048])
        self.assertEqual(first[0], MODE_FILE)
        # every file's data is padded to 1024
        self.assertEqual(len(blob), 512 * 6 + 1024 + 3072 + 1024)
        code, _, err = run(["psu", "import", out, "--to", self.p("mc0b")])
        self.assertEqual(code, 0, err)
        for name, data in SAVES[0][2]:
            self.assertEqual(self.read("mc0b", "BASLUS-20312GAME", name.decode()), data)

    def test_psu_export_straight_from_a_card_image(self):
        out = self.p("s.psu")
        code, _, err = run(["psu", "export", self.ecc, "--save", "BESLES-50001SAVE", "--out", out])
        self.assertEqual(code, 0, err)
        code, text, _ = run(["psu", "ls", out, "--json"])
        self.assertEqual(code, 0)
        res = json.loads(text)
        self.assertEqual(res["folder"], "BESLES-50001SAVE")
        self.assertEqual([f["name"] for f in res["files"]], ["BIG.DAT", "EMPTY"])
        self.assertEqual(res["modified"], "2005-12-31 23:59")

    def test_psu_export_card_needs_save_name(self):
        code, _, err = run(["psu", "export", self.plain, "--out", self.p("x.psu")])
        self.assertEqual(code, 2)
        self.assertIn("--save", err)

    def test_psu_export_default_path_and_no_overwrite(self):
        mc0 = self.export()
        code, out, err = run(["psu", "export", os.path.join(mc0, "BESLES-50001SAVE")])
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isfile(self.p("console", "saves", "BESLES-50001SAVE.psu")))
        self.assertEqual(run(["psu", "export", os.path.join(mc0, "BESLES-50001SAVE")])[0], 2)

    def test_psu_refuses_subfolders(self):
        os.makedirs(self.p("mc0", "SAVE", "SUB"))
        code, _, err = run(["psu", "export", self.p("mc0", "SAVE"), "--out", self.p("x.psu")])
        self.assertEqual(code, 2)
        self.assertIn("subfolder", err)

    def _psu(self, folder=b"SAVE", files=((b"A", b"abc"),), count=None):
        t = T_SAVE
        n = len(files) if count is None else count
        blob = DIRENT.pack(MODE_DIR, 0, n + 2, t, 0, 0, t, 0, folder)
        blob += DIRENT.pack(MODE_DIR, 0, 0, t, 0, 0, t, 0, b".")
        blob += DIRENT.pack(MODE_DIR, 0, 0, t, 0, 0, t, 0, b"..")
        for name, data in files:
            blob += DIRENT.pack(MODE_FILE, 0, len(data), t, 0, 0, t, 0, name) + data + b"\0" * ((-len(data)) % 1024)
        path = self.p("t.psu")
        with open(path, "wb") as fh:
            fh.write(blob)
        return path, blob

    def test_import_truncated_psu(self):
        path, blob = self._psu(files=((b"A", b"x" * 3000),))
        with open(path, "wb") as fh:
            fh.write(blob[:2000])
        code, _, err = run(["psu", "import", path, "--to", self.p("mc0")])
        self.assertEqual(code, 2)
        self.assertIn("truncated", err)

    def test_import_missing_header(self):
        path, _ = self._psu(count=3)
        code, _, err = run(["psu", "import", path, "--to", self.p("mc0")])
        self.assertEqual(code, 2)

    def test_import_refuses_path_tricks(self):
        for folder, files in ((b"..", ((b"A", b"1"),)), (b"SAVE", ((b"../../evil", b"1"),)),
                              (b"a/b", ((b"A", b"1"),))):
            path, _ = self._psu(folder, files)
            code, _, err = run(["psu", "import", path, "--to", self.p("mc0")])
            self.assertEqual(code, 2, (folder, files))
        self.assertFalse(os.path.exists(self.p("evil")))

    def test_not_a_psu(self):
        path = self.p("x.psu")
        with open(path, "wb") as fh:
            fh.write(b"\0" * 4096)
        self.assertEqual(run(["psu", "ls", path])[0], 2)

    def test_import_existing_needs_force_and_backs_up(self):
        path, _ = self._psu()
        self.assertEqual(run(["psu", "import", path, "--to", self.p("mc0")])[0], 0)
        with open(self.p("mc0", "SAVE", "A"), "wb") as fh:
            fh.write(b"mine")
        code, _, err = run(["psu", "import", path, "--to", self.p("mc0")])
        self.assertEqual(code, 1)
        self.assertEqual(self.read("mc0", "SAVE", "A"), b"mine")
        code, _, err = run(["psu", "import", path, "--to", self.p("mc0"), "--force", "--backups", self.p("bk")])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read("mc0", "SAVE", "A"), b"abc")
        self.assertEqual(len(os.listdir(self.p("bk"))), 1)


class BackupRestore(Base):
    def setUp(self):
        Base.setUp(self)
        self.mc0 = self.export()

    def test_backup_zip_holds_every_file(self):
        code, out, err = run(["backup", self.mc0, "--to", self.p("bk")])
        self.assertEqual(code, 0, err)
        (name,) = os.listdir(self.p("bk"))
        self.assertRegex(name, r"^game-mc0-\d{8}T\d{6}Z\.zip$")
        with zipfile.ZipFile(self.p("bk", name)) as z:
            self.assertEqual(sorted(z.namelist()), sorted([
                "BASLUS-20312GAME/DATA", "BASLUS-20312GAME/icon.ico", "BASLUS-20312GAME/icon.sys",
                "BESLES-50001SAVE/BIG.DAT", "BESLES-50001SAVE/EMPTY"]))

    def test_keep_prunes_only_this_cards_backups(self):
        os.makedirs(self.p("bk"))
        other = self.p("bk", "othergame-mc0-20200101T000000Z.zip")
        with zipfile.ZipFile(other, "w"):
            pass
        for _ in range(4):
            self.assertEqual(run(["backup", self.mc0, "--to", self.p("bk"), "--keep", "2"])[0], 0)
        mine = sorted(f for f in os.listdir(self.p("bk")) if f.startswith("game-mc0-"))
        self.assertEqual(len(mine), 2)
        self.assertTrue(os.path.exists(other))

    def test_backup_inside_the_card_folder_refused(self):
        code, _, err = run(["backup", self.mc0, "--to", os.path.join(self.mc0, "bk")])
        self.assertEqual(code, 2)
        self.assertIn("outside", err)

    def test_backup_of_missing_folder(self):
        self.assertEqual(run(["backup", self.p("nope")])[0], 2)

    def test_restore_into_empty_folder(self):
        run(["backup", self.mc0, "--to", self.p("bk")])
        (z,) = os.listdir(self.p("bk"))
        code, out, err = run(["restore", self.p("bk", z), "--to", self.p("mc0new")])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read("mc0new", "BASLUS-20312GAME", "DATA"), SAVES[0][2][1][1])

    def test_restore_over_existing_backs_up_first(self):
        run(["backup", self.mc0, "--to", self.p("bk")])
        (z,) = os.listdir(self.p("bk"))
        with open(os.path.join(self.mc0, "BASLUS-20312GAME", "DATA"), "wb") as fh:
            fh.write(b"later")
        code, _, err = run(["restore", self.p("bk", z), "--to", self.mc0])
        self.assertEqual(code, 1)
        self.assertEqual(self.read("game", "mc0", "BASLUS-20312GAME", "DATA"), b"later")
        code, _, err = run(["restore", self.p("bk", z), "--to", self.mc0, "--force", "--backups", self.p("bk2")])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read("game", "mc0", "BASLUS-20312GAME", "DATA"), SAVES[0][2][1][1])
        (saved,) = os.listdir(self.p("bk2"))
        with zipfile.ZipFile(self.p("bk2", saved)) as zz:
            self.assertEqual(zz.read("BASLUS-20312GAME/DATA"), b"later")

    def test_restore_refuses_zip_slip(self):
        evil = self.p("evil.zip")
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("../outside.txt", "x")
        code, _, err = run(["restore", evil, "--to", self.p("mc0x")])
        self.assertEqual(code, 2)
        self.assertIn("unsafe", err)
        self.assertFalse(os.path.exists(self.p("outside.txt")))

    def test_restore_not_a_zip(self):
        bad = self.p("bad.zip")
        with open(bad, "wb") as fh:
            fh.write(b"nope")
        self.assertEqual(run(["restore", bad, "--to", self.p("m")])[0], 2)


class Sync(Base):
    def setUp(self):
        Base.setUp(self)
        self.mc0 = self.export()
        self.cloud = self.p("iCloud", "console-saves", "SLUS-20312")
        os.makedirs(os.path.dirname(self.cloud))

    def sync(self, *extra, mc0=None):
        return run(["sync", mc0 or self.mc0, "--cloud", self.cloud, "--backups", self.p("bk")] + list(extra))

    def write(self, path, data):
        with open(path, "wb") as fh:
            fh.write(data)
        t = time.time() + 5
        os.utime(path, (t, t))

    def test_first_sync_pushes_everything(self):
        code, out, err = self.sync()
        self.assertEqual(code, 0, err)
        self.assertIn("5 pushed", out)
        with open(os.path.join(self.cloud, "BASLUS-20312GAME", "DATA"), "rb") as fh:
            self.assertEqual(fh.read(), SAVES[0][2][1][1])
        # the manifest lives next to mc0, never inside it (the runtime lists every file in mc0)
        self.assertTrue(os.path.isfile(self.p("game", ".console-sync.json")))
        self.assertFalse(any(n.startswith(".console") for n in os.listdir(self.mc0)))
        code, out, _ = self.sync()
        self.assertEqual(code, 0)
        self.assertIn("0 pushed, 0 pulled, 5 already the same", out)

    def test_second_device_pulls(self):
        self.sync()
        other = self.p("deck", "mc0")
        os.makedirs(other)
        code, out, err = self.sync(mc0=other)
        self.assertEqual(code, 0, err)
        self.assertIn("5 pulled", out)
        with open(os.path.join(other, "BESLES-50001SAVE", "BIG.DAT"), "rb") as fh:
            self.assertEqual(fh.read(), SAVES[1][2][0][1])

    def test_one_side_changed_wins(self):
        self.sync()
        data = os.path.join(self.mc0, "BASLUS-20312GAME", "DATA")
        self.write(data, b"level 2")
        code, out, _ = self.sync()
        self.assertEqual(code, 0)
        self.assertIn("1 pushed", out)
        with open(os.path.join(self.cloud, "BASLUS-20312GAME", "DATA"), "rb") as fh:
            self.assertEqual(fh.read(), b"level 2")
        self.write(os.path.join(self.cloud, "BASLUS-20312GAME", "DATA"), b"level 3 from the deck")
        code, out, _ = self.sync()
        self.assertEqual(code, 0)
        self.assertIn("1 pulled", out)
        with open(data, "rb") as fh:
            self.assertEqual(fh.read(), b"level 3 from the deck")
        self.assertTrue(os.listdir(self.p("bk")), "mc0 is backed up before a pull writes into it")

    def test_both_changed_is_a_conflict_and_nothing_is_overwritten(self):
        self.sync()
        data = os.path.join(self.mc0, "BASLUS-20312GAME", "DATA")
        cdata = os.path.join(self.cloud, "BASLUS-20312GAME", "DATA")
        self.write(data, b"played here")
        self.write(cdata, b"played on the deck")
        code, out, _ = self.sync()
        self.assertEqual(code, 1)
        self.assertIn("1 conflict", out)
        self.assertEqual(rb(data), b"played here")
        self.assertEqual(rb(cdata), b"played on the deck")
        (cdir,) = os.listdir(os.path.join(self.cloud, ".console-conflicts"))
        base = os.path.join(self.cloud, ".console-conflicts", cdir)
        self.assertEqual(rb(os.path.join(base, "here", "BASLUS-20312GAME", "DATA")), b"played here")
        self.assertEqual(rb(os.path.join(base, "cloud", "BASLUS-20312GAME", "DATA")),
                         b"played on the deck")
        # still a conflict next time: the manifest did not move
        self.assertEqual(self.sync()[0], 1)
        # an explicit choice settles it
        code, out, _ = self.sync("--prefer", "local")
        self.assertEqual(code, 0)
        self.assertEqual(rb(cdata), b"played here")
        self.assertEqual(self.sync()[0], 0)

    def test_never_synced_and_different_is_a_conflict(self):
        os.makedirs(os.path.join(self.cloud, "BASLUS-20312GAME"))
        with open(os.path.join(self.cloud, "BASLUS-20312GAME", "DATA"), "wb") as fh:
            fh.write(b"someone else's progress")
        code, out, _ = self.sync()
        self.assertEqual(code, 1)
        self.assertIn("never synced", out)
        self.assertEqual(rb(os.path.join(self.mc0, "BASLUS-20312GAME", "DATA")), SAVES[0][2][1][1])

    def test_prefer_cloud_and_newest(self):
        self.sync()
        data = os.path.join(self.mc0, "BASLUS-20312GAME", "DATA")
        cdata = os.path.join(self.cloud, "BASLUS-20312GAME", "DATA")
        self.write(data, b"here")
        self.write(cdata, b"cloud")
        t = time.time() + 100
        os.utime(cdata, (t, t))
        code, out, _ = self.sync("--prefer", "newest")
        self.assertEqual(code, 0)
        self.assertEqual(rb(data), b"cloud")
        self.write(data, b"here again")
        self.write(cdata, b"cloud again")
        self.assertEqual(self.sync("--prefer", "cloud")[0], 0)
        self.assertEqual(rb(data), b"cloud again")

    def test_push_only_never_pulls(self):
        self.sync()
        cdata = os.path.join(self.cloud, "BASLUS-20312GAME", "DATA")
        self.write(cdata, b"cloud change")
        code, out, _ = self.sync("--push")
        self.assertEqual(code, 0)
        self.assertIn("1 skipped", out)
        self.assertEqual(rb(os.path.join(self.mc0, "BASLUS-20312GAME", "DATA")), SAVES[0][2][1][1])
        code, out, _ = self.sync("--pull")
        self.assertIn("1 pulled", out)

    def test_dry_run_changes_nothing(self):
        code, out, _ = self.sync("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("dry run: 5 pushed", out)
        self.assertFalse(os.path.exists(self.cloud))
        self.assertFalse(os.path.exists(self.p("game", ".console-sync.json")))
        code, out, _ = self.sync("--dry-run", "--json")
        self.assertEqual({r["action"] for r in json.loads(out)}, {"push"})

    def test_icloud_placeholders_block_the_sync(self):
        os.makedirs(os.path.join(self.cloud, "BASLUS-20312GAME"))
        with open(os.path.join(self.cloud, "BASLUS-20312GAME", ".DATA.icloud"), "wb") as fh:
            fh.write(b"bplist")
        code, _, err = self.sync()
        self.assertEqual(code, 1)
        self.assertIn("placeholder", err)
        self.assertFalse(os.path.exists(os.path.join(self.cloud, "BASLUS-20312GAME", "DATA")))

    def test_deleted_files_come_back_rather_than_vanish(self):
        self.sync()
        os.remove(os.path.join(self.cloud, "BASLUS-20312GAME", "icon.ico"))
        code, out, _ = self.sync()
        self.assertEqual(code, 0)
        self.assertIn("sync never deletes", out)
        self.assertTrue(os.path.exists(os.path.join(self.cloud, "BASLUS-20312GAME", "icon.ico")))

    def test_nested_folders_refused(self):
        code, _, err = run(["sync", self.mc0, "--cloud", os.path.join(self.mc0, "cloud")])
        self.assertEqual(code, 2)

    def test_cloud_path_that_is_a_file(self):
        with open(self.cloud, "wb") as fh:
            fh.write(b"x")
        self.assertEqual(self.sync()[0], 2)

    def test_cloud_parent_must_exist(self):
        code, _, err = run(["sync", self.mc0, "--cloud", self.p("no", "such", "place")])
        self.assertEqual(code, 2)
        self.assertIn("check the cloud path", err)

    def test_json_summary(self):
        code, out, _ = self.sync("--json")
        self.assertEqual(code, 0)
        res = json.loads(out)
        self.assertEqual(res["pushed"], 5)
        self.assertEqual(len(res["plan"]), 5)


class Helpers(unittest.TestCase):
    def test_tod_is_japan_time(self):
        t = mc.tod_unpack(mc.epoch_to_tod(0))
        self.assertEqual(t, (1970, 1, 1, 9, 0, 0))
        self.assertEqual(mc.tod_to_epoch(t), 0)

    def test_bad_tod_is_none(self):
        self.assertIsNone(mc.tod_to_epoch((2004, 13, 40, 99, 0, 0)))

    def test_serials(self):
        self.assertEqual(mc.serial_of("BISLPS-25051SAVE"), ("SLPS-25051", "Japan/Asia"))
        self.assertIsNone(mc.serial_of("MYSAVE"))

    def test_icon_title_needs_magic(self):
        self.assertIsNone(mc.icon_title(b"\0" * 964))
        self.assertIsNone(mc.icon_title(b"PS2D"))

    def test_host_name_rules(self):
        self.assertEqual(mc.host_name(b"BASLUS-20312GAME"), "BASLUS-20312GAME")
        for bad in (b"", b".", b"..", b"a/b", b"a\\b", b"x" * 40, b"tab\there"):
            with self.assertRaises(mc.ToolError):
                mc.host_name(bad)


class Cli(unittest.TestCase):
    def test_help_everywhere(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "console-saves", "memcard.py"), "--help"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(r.returncode, 0)
        self.assertIn(b"usage", r.stdout)
        for argv in (["info"], ["ls"], ["export"], ["psu"], ["psu", "export"], ["psu", "import"], ["psu", "ls"],
                     ["backup"], ["restore"], ["sync"]):
            out = io.StringIO()
            with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
                mc.main(argv + ["--help"])
            self.assertEqual(cm.exception.code, 0, argv)
            self.assertIn("usage", out.getvalue())

    def test_usage_error_is_exit_2(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            mc.main(["sync", "x"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
