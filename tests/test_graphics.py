"""Tests for console-graphics: tim2.py and widescreen.py. Fully offline, synthetic fixtures only."""

import io
import json
import math
import os
import random
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

tim2 = load("console-graphics", "tim2")
ws = load("console-graphics", "widescreen")
SKILL = os.path.join(ROOT, "console-graphics")


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------
# Synthetic TIM2 builder (made-up colours, no game data)
# --------------------------------------------------------------------------

TEX0 = (0x13 << 20) | (4 << 26) | (4 << 30) | (2 << 14) | (5 << 37)


def picture(w, h, image_type, image, clut=b"", clut_type=0, clut_colors=0, mips=1, mip_sizes=None, tex0=TEX0):
    extra = b""
    if mips > 1:
        extra = struct.pack("<QQ", 0, 0) + struct.pack("<%dI" % mips, *mip_sizes)
    extra += bytes((-(0x30 + len(extra))) % 16)
    header_size = 0x30 + len(extra)
    total = header_size + len(image) + len(clut)
    ph = struct.pack("<IIIHHBBBBHH", total, len(clut), len(image), header_size, clut_colors, 0, mips, clut_type,
                     image_type, w, h)
    ph += struct.pack("<QQII", tex0, 0x60, 0, 0) + extra
    return ph + image + clut


def tim2_file(pics, fmt_id=0, version=4):
    head = b"TIM2" + bytes([version, fmt_id]) + struct.pack("<H", len(pics)) + bytes(8)
    if fmt_id == 1:
        head += bytes(0x80 - 16)
    return head + b"".join(pics)


def logical_color(k):
    return (k, 255 - k, (k * 7) & 0xFF, 0x80)


def rgba32(colors):
    return b"".join(bytes(c) for c in colors)


def pic32():
    px = [(255, 0, 0, 0x80), (0, 255, 0, 0x40), (0, 0, 255, 0x00), (10, 20, 30, 0xFF),
          (1, 2, 3, 0x80), (4, 5, 6, 0x80), (7, 8, 9, 0x20), (200, 100, 50, 0x80)]
    return picture(4, 2, 3, rgba32(px))


def pic24():
    px = bytes(range(3 * 6))   # 3x2, odd width
    return picture(3, 2, 2, px)


def pic16(alpha=True):
    vals = [0x801F, 0x83E0, 0xFC00, 0x7FFF, 0x0000, 0x8000, 0x4210, 0xC631]
    if not alpha:
        vals = [v & 0x7FFF for v in vals]
    return picture(4, 2, 1, struct.pack("<8H", *vals))


def pic8(swizzled=True, w=16, h=16):
    idx = bytes((i * 37 + 11) & 0xFF for i in range(w * h))
    logical = [logical_color(k) for k in range(256)]
    if swizzled:
        stored = [logical[tim2.swizzle(s)] for s in range(256)]
        ctype = 0x03
    else:
        stored = logical
        ctype = 0x83
    return picture(w, h, 5, idx, rgba32(stored), ctype, 256)


def pic4(palettes=1):
    w, h = 8, 4
    idx = [(x + y * 3) & 15 for y in range(h) for x in range(w)]
    image = bytes(idx[i] | (idx[i + 1] << 4) for i in range(0, len(idx), 2))
    pal16 = []
    for p in range(palettes):
        for k in range(16):
            r, g, b = (k * 2 + p) & 31, (31 - k) & 31, (k + 5 * p) & 31
            pal16.append(r | (g << 5) | (b << 10) | 0x8000)
    clut = struct.pack("<%dH" % len(pal16), *pal16)
    return picture(w, h, 4, image, clut, 0x01, 16 * palettes)


def pic8_mips():
    lvl0 = bytes((x ^ y) & 0xFF for y in range(8) for x in range(8))
    lvl1 = bytes((x * 3) & 0xFF for y in range(4) for x in range(4))
    clut = rgba32([logical_color(k) for k in range(256)])
    return picture(8, 8, 5, lvl0 + lvl1, clut, 0x83, 256, mips=2, mip_sizes=[64, 16])


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="console_graphics_")
        self.addCleanup(shutil.rmtree, self.d, True)

    def write(self, name, data):
        p = os.path.join(self.d, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()

    def topng(self, path, *extra):
        out = os.path.join(self.d, "png_" + os.path.basename(path))
        code, so, se = run(tim2, ["topng", path, "--out", out] + list(extra))
        self.assertEqual(code, 0, se)
        return out, so

    def png_of(self, folder, name):
        return tim2.decode_png(self.read(os.path.join(folder, name)))


# --------------------------------------------------------------------------
# Colour helpers and parsing
# --------------------------------------------------------------------------


class TestHelpers(unittest.TestCase):
    def test_alpha_round_trip(self):
        for a in range(129):
            self.assertEqual(tim2.alpha_to_ps2(tim2.alpha_to_png(a)), a)
        self.assertEqual(tim2.alpha_to_png(0x80), 255)
        self.assertEqual(tim2.alpha_to_png(0xFF), 255)
        self.assertEqual(tim2.alpha_to_png(0), 0)

    def test_5bit_round_trip(self):
        for x in range(32):
            self.assertEqual(tim2.pack5(tim2.expand5(x)), x)
        self.assertEqual(tim2.expand5(31), 255)

    def test_swizzle_is_an_involution(self):
        for i in range(512):
            self.assertEqual(tim2.swizzle(tim2.swizzle(i)), i)
        self.assertEqual([tim2.swizzle(i) for i in (0, 7, 8, 15, 16, 23, 24, 40, 48)], [0, 7, 16, 23, 8, 15, 24, 48, 40])

    def test_tex0_fields(self):
        t = tim2.tex0_fields(TEX0)
        self.assertEqual((t["PSM"], t["TW"], t["TH"], t["TBW"], t["CBP"]), ("PSMT8", 4, 4, 2, 5))

    def test_median_cut(self):
        rnd = random.Random(7)
        counts = {}
        for _ in range(3000):
            c = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256), 255)
            counts[c] = counts.get(c, 0) + 1
        pal, mapping = tim2.median_cut(counts, 16)
        self.assertEqual(len(pal), 16)
        self.assertEqual(set(mapping), set(counts))
        self.assertTrue(all(0 <= i < 16 for i in mapping.values()))
        pal, mapping = tim2.median_cut({(1, 2, 3, 255): 5}, 16)
        self.assertEqual(pal, [(1, 2, 3, 255)])


class TestParse(Base):
    def test_every_format_parses(self):
        data = tim2_file([pic32(), pic24(), pic16(), pic8(), pic4(), pic8_mips()])
        hdr, pics = tim2.parse_tim2(data)
        self.assertEqual(hdr["count"], 6)
        self.assertEqual([p.bpp for p in pics], [32, 24, 16, 8, 4, 8])
        self.assertTrue(pics[3].swizzled)
        self.assertFalse(pics[4].swizzled)
        self.assertEqual(pics[5].mips, 2)
        self.assertEqual([lv[1:3] for lv in pics[5].levels], [(8, 8), (4, 4)])
        self.assertEqual(hdr["end"], len(data))

    def test_128_byte_alignment(self):
        hdr, pics = tim2.parse_tim2(tim2_file([pic32()], fmt_id=1))
        self.assertEqual(hdr["align"], 0x80)
        self.assertEqual(pics[0].offset, 0x80)

    def test_bad_files(self):
        good = tim2_file([pic8()])
        cases = [b"TIM", b"NOPE" + good[4:], good[:4] + b"\x04\x07" + good[6:], good[:6] + b"\0\0" + good[8:],
                 good[:40], good[:-10], good[:16 + 0x13] + b"\x09" + good[16 + 0x14:],
                 good[:16 + 0x14] + b"\0\0" + good[16 + 0x16:]]
        for i, data in enumerate(cases):
            with self.assertRaises(tim2.UsageError, msg=str(i)):
                tim2.parse_tim2(data)
        for i, data in enumerate(cases):
            code, _, err = run(tim2, ["info", self.write("bad%d.tm2" % i, data)])
            self.assertEqual(code, 2, i)
            self.assertNotIn("Traceback", err)

    def test_info(self):
        p = self.write("a.tm2", tim2_file([pic8(), pic4(palettes=2)]))
        code, out, _ = run(tim2, ["info", p])
        self.assertEqual(code, 0)
        self.assertIn("16x16 8-bit indexed", out)
        self.assertIn("CSM1 swizzled", out)
        self.assertIn("2 palette(s)", out)
        code, out, _ = run(tim2, ["info", p, "--json"])
        j = json.loads(out)
        self.assertEqual(j["pictures"][0]["tex0"]["PSM"], "PSMT8")
        self.assertEqual(j["pictures"][1]["palettes"], 2)


# --------------------------------------------------------------------------
# Decoding to PNG
# --------------------------------------------------------------------------


class TestToPng(Base):
    def test_32bit_alpha_scaled(self):
        out, _ = self.topng(self.write("c32.tm2", tim2_file([pic32()])))
        w, h, rgba, idx, pal = self.png_of(out, "c32.png")
        self.assertEqual((w, h), (4, 2))
        self.assertEqual([c[3] for c in rgba], [255, 128, 0, 255, 255, 255, 64, 255])
        self.assertEqual(rgba[0][:3], (255, 0, 0))

    def test_24bit_odd_width(self):
        out, _ = self.topng(self.write("c24.tm2", tim2_file([pic24()])))
        w, h, rgba, _, _ = self.png_of(out, "c24.png")
        self.assertEqual((w, h), (3, 2))
        self.assertEqual(rgba[1], (3, 4, 5, 255))

    def test_16bit(self):
        out, _ = self.topng(self.write("c16.tm2", tim2_file([pic16()])))
        _, _, rgba, _, _ = self.png_of(out, "c16.png")
        self.assertEqual(rgba[0], (255, 0, 0, 255))
        self.assertEqual(rgba[1], (0, 255, 0, 255))
        self.assertEqual(rgba[3], (255, 255, 255, 0))     # alpha bit clear
        out, _ = self.topng(self.write("n16.tm2", tim2_file([pic16(alpha=False)])))
        _, _, rgba, _, _ = self.png_of(out, "n16.png")
        self.assertTrue(all(c[3] == 255 for c in rgba))   # alpha bit unused: shown opaque

    def test_8bit_swizzled_palette(self):
        out, _ = self.topng(self.write("s8.tm2", tim2_file([pic8(True)])))
        w, h, rgba, idx, pal = self.png_of(out, "s8.png")
        self.assertIsNotNone(idx)                       # written as a palette PNG
        for i in range(w * h):
            k = (i * 37 + 11) & 0xFF
            self.assertEqual(rgba[i], logical_color(k)[:3] + (255,))
        out2, _ = self.topng(self.write("l8.tm2", tim2_file([pic8(False)])))
        self.assertEqual(self.png_of(out2, "l8.png")[2], rgba)   # same picture either way

    def test_4bit_nibbles_and_palettes(self):
        p = self.write("c4.tm2", tim2_file([pic4(palettes=2)]))
        out, _ = self.topng(p, "--all-palettes")
        a = self.png_of(out, "c4_pal0.png")
        b = self.png_of(out, "c4_pal1.png")
        self.assertEqual(a[3], b[3])                     # same indices
        self.assertNotEqual(a[2], b[2])                  # different colours
        self.assertEqual(a[3][:4], [0, 1, 2, 3])
        out, _ = self.topng(p, "--rgba")
        self.assertIsNone(self.png_of(out, "c4.png")[3])

    def test_mips_and_multi_picture_names(self):
        p = self.write("m.tm2", tim2_file([pic8_mips(), pic32()]))
        out, so = self.topng(p, "--mips")
        names = sorted(os.listdir(out))
        self.assertEqual(names, ["m_0.png", "m_0_mip1.png", "m_1.png"])
        self.assertEqual(self.png_of(out, "m_0_mip1.png")[:2], (4, 4))

    def test_folder_with_a_broken_file(self):
        self.write("tex/a.tm2", tim2_file([pic32()]))
        self.write("tex/sub/b.TM2", tim2_file([pic8()]))
        self.write("tex/sub/broken.tm2", b"TIM2junk")
        self.write("tex/readme.txt", b"hi")
        out = os.path.join(self.d, "pngs")
        code, so, se = run(tim2, ["topng", os.path.join(self.d, "tex"), "--out", out])
        self.assertEqual(code, 1)
        self.assertIn("skipped", se)
        self.assertTrue(os.path.isfile(os.path.join(out, "a.png")))
        self.assertTrue(os.path.isfile(os.path.join(out, "sub", "b.png")))

    def test_no_files(self):
        os.makedirs(os.path.join(self.d, "empty"))
        code, _, err = run(tim2, ["topng", os.path.join(self.d, "empty"), "--out", os.path.join(self.d, "o")])
        self.assertEqual(code, 2)


# --------------------------------------------------------------------------
# Rebuilding from PNG
# --------------------------------------------------------------------------


class TestFromPng(Base):
    def roundtrip(self, name, data, *topng_extra):
        src = self.write(name, data)
        out, _ = self.topng(src, *topng_extra)
        png = [f for f in os.listdir(out) if f.endswith(".png")][0]
        dst = os.path.join(self.d, "new_" + name)
        code, so, se = run(tim2, ["frompng", src, os.path.join(out, png), "--out", dst])
        self.assertEqual(code, 0, se)
        return src, dst, so

    def test_unchanged_round_trip_is_byte_identical(self):
        for name, data in (("a.tm2", tim2_file([pic32()])), ("b.tm2", tim2_file([pic24()])),
                           ("c.tm2", tim2_file([pic16()])), ("d.tm2", tim2_file([pic16(False)])),
                           ("e.tm2", tim2_file([pic8(True)])), ("f.tm2", tim2_file([pic8(False)])),
                           ("g.tm2", tim2_file([pic4()])), ("h.tm2", tim2_file([pic8_mips()])),
                           ("i.tm2", tim2_file([pic32()], fmt_id=1))):
            src, dst, so = self.roundtrip(name, data)
            self.assertEqual(self.read(dst), self.read(src), name)
            self.assertIn("0 of", so)
        src, dst, _ = self.roundtrip("j.tm2", tim2_file([pic8(True)]), "--rgba")
        self.assertEqual(self.read(dst), self.read(src))

    def edit(self, data, change, name="x.tm2", extra=()):
        src = self.write(name, data)
        out, _ = self.topng(src, "--rgba")
        png = os.path.join(out, os.listdir(out)[0])
        w, h, rgba, _, _ = tim2.decode_png(self.read(png))
        rgba = change(list(rgba))
        edited = self.write("edited_" + name + ".png", tim2.encode_png(w, h, rgba))
        dst = os.path.join(self.d, "new_" + name)
        res = run(tim2, ["frompng", src, edited, "--out", dst] + list(extra))
        return src, dst, rgba, res

    def check_decodes_to(self, dst, rgba, picture=0):
        data = self.read(dst)
        _, pics = tim2.parse_tim2(data)
        self.assertEqual(tim2.decode_picture(data, pics[picture])[2], rgba)

    def test_direct_colour_edit(self):
        def change(px):
            px[0] = (12, 34, 56, 255)
            px[5] = (99, 98, 97, 128)
            return px
        src, dst, rgba, (code, so, se) = self.edit(tim2_file([pic32()]), change)
        self.assertEqual(code, 0, se)
        self.assertIn("2 of 8 pixels changed", so)
        new, old = self.read(dst), self.read(src)
        self.assertEqual(len(new), len(old))
        self.assertEqual(new[:16 + 0x30], old[:16 + 0x30])     # header and GS registers untouched
        self.check_decodes_to(dst, rgba)

    def test_16bit_edit_keeps_unused_alpha(self):
        src, dst, rgba, (code, _, se) = self.edit(tim2_file([pic16(False)]), lambda px: [(255, 255, 0, 255)] + px[1:])
        self.assertEqual(code, 0, se)
        vals = struct.unpack_from("<8H", self.read(dst), 16 + 0x30)
        self.assertFalse(any(v & 0x8000 for v in vals))
        self.check_decodes_to(dst, rgba)

    def test_indexed_edit_with_existing_colours_keeps_palette(self):
        def change(px):
            px[0], px[1] = px[1], px[0]
            return px
        src, dst, rgba, (code, so, se) = self.edit(tim2_file([pic8(True)]), change)
        self.assertEqual(code, 0, se)
        self.assertNotIn("palette 0 rewritten", so)
        old, new = self.read(src), self.read(dst)
        self.assertEqual(old[-1024:], new[-1024:])
        self.check_decodes_to(dst, rgba)

    def test_indexed_new_colour_goes_in_a_free_slot_and_respects_swizzle(self):
        # 8x8 image using only logical entries 0..3, so entries 4..255 are free
        idx = bytes(i % 4 for i in range(64))
        logical = [logical_color(k) for k in range(256)]
        stored = [logical[tim2.swizzle(s)] for s in range(256)]
        data = tim2_file([picture(8, 8, 5, idx, rgba32(stored), 0x03, 256)])

        def change(px):
            px[10] = (1, 2, 3, 255)
            return px
        src, dst, rgba, (code, so, se) = self.edit(data, change)
        self.assertEqual(code, 0, se)
        self.assertIn("palette 0 rewritten (1 entries changed)", so)
        self.check_decodes_to(dst, rgba)
        new = self.read(dst)
        _, pics = tim2.parse_tim2(new)
        slot = new[pics[0].image_off + 10]
        self.assertGreaterEqual(slot, 4)
        stored_at = pics[0].clut_off + 4 * tim2.swizzle(slot)
        self.assertEqual(new[stored_at:stored_at + 4], bytes((1, 2, 3, 0x80)))

    def test_too_many_colours_needs_quantize(self):
        def change(px):
            return [(i % 256, (i * 7) % 256, (i * 13) % 256, 255) for i in range(len(px))]
        data = tim2_file([pic4()])
        src, dst, rgba, (code, _, err) = self.edit(data, change)
        self.assertEqual(code, 2)
        self.assertIn("--quantize", err)
        src, dst, rgba, (code, so, err) = self.edit(data, change, name="q.tm2", extra=["--quantize"])
        self.assertEqual(code, 0, err)
        self.assertIn("quantised", so)
        data2 = self.read(dst)
        _, pics = tim2.parse_tim2(data2)
        self.assertLessEqual(len(set(tim2.decode_picture(data2, pics[0])[2])), 16)
        self.assertEqual(len(data2), len(data))

    def test_second_palette_edit_leaves_first(self):
        src = self.write("p2.tm2", tim2_file([pic4(palettes=2)]))
        _, pics = tim2.parse_tim2(self.read(src))
        w, h, rgba, _, _ = tim2.decode_picture(self.read(src), pics[0], 0, 1)
        first = rgba[0]
        rgba = [(255, 0, 255, 255) if c == first else c for c in rgba]   # recolour one entry (5-bit exact)
        png = self.write("p2.png", tim2.encode_png(w, h, rgba))
        dst = os.path.join(self.d, "p2_new.tm2")
        code, _, err = run(tim2, ["frompng", src, png, "--out", dst, "--palette", "1"])
        self.assertEqual(code, 0, err)
        new = self.read(dst)
        self.assertEqual(tim2.decode_picture(new, pics[0], 0, 0)[2], tim2.decode_picture(self.read(src), pics[0], 0, 0)[2])
        self.assertEqual(tim2.decode_picture(new, pics[0], 0, 1)[2], rgba)

    def test_mips_regenerated_or_kept(self):
        data = tim2_file([pic8_mips()])

        def change(px):
            return [px[0]] * len(px)
        src, dst, rgba, (code, so, _) = self.edit(data, change)
        self.assertIn("regenerated 1 mip level", so)
        new = self.read(dst)
        _, pics = tim2.parse_tim2(new)
        self.assertEqual(set(tim2.decode_picture(new, pics[0], 1)[2]), {rgba[0]})
        src, dst, rgba, (code, so, _) = self.edit(data, change, name="k.tm2", extra=["--keep-mips"])
        self.assertIn("kept the original", so)
        self.assertEqual(self.read(dst)[-1024 - 16:-1024], self.read(src)[-1024 - 16:-1024])

    def test_24bit_alpha_dropped_note(self):
        src, dst, rgba, (code, so, _) = self.edit(tim2_file([pic24()]), lambda px: [(1, 2, 3, 0)] + px[1:])
        self.assertIn("no alpha", so)

    def test_wrong_size_and_bad_choices(self):
        src = self.write("s.tm2", tim2_file([pic32()]))
        png = self.write("big.png", tim2.encode_png(5, 2, [(0, 0, 0, 255)] * 10))
        code, _, err = run(tim2, ["frompng", src, png, "--out", os.path.join(self.d, "o.tm2")])
        self.assertEqual(code, 2)
        self.assertIn("same size", err)
        ok = self.write("ok.png", tim2.encode_png(4, 2, [(0, 0, 0, 255)] * 8))
        for extra in (["--picture", "3"], ["--out", src]):
            args = ["frompng", src, ok] + (["--out", os.path.join(self.d, "o2.tm2")] if "--out" not in extra else []) + extra
            code, _, err = run(tim2, args)
            self.assertEqual(code, 2, extra)
        dst = os.path.join(self.d, "o3.tm2")
        self.assertEqual(run(tim2, ["frompng", src, ok, "--out", dst])[0], 0)
        self.assertEqual(run(tim2, ["frompng", src, ok, "--out", dst])[0], 2)
        self.assertEqual(run(tim2, ["frompng", src, ok, "--out", dst, "--force"])[0], 0)


# --------------------------------------------------------------------------
# PNG decoder
# --------------------------------------------------------------------------


def png_raw(w, h, ctype, depth, rows, filt=None, interlace=0, extra=b"", plte=None):
    ihdr = struct.pack(">IIBBBBB", w, h, depth, ctype, 0, 0, interlace)
    body = bytearray()
    for y, row in enumerate(rows):
        body.append(filt[y] if filt else 0)
        body += row
    out = tim2.PNG_SIG + tim2._chunk(b"IHDR", ihdr)
    if plte:
        out += tim2._chunk(b"PLTE", plte)
    return out + extra + tim2._chunk(b"IDAT", zlib.compress(bytes(body))) + tim2._chunk(b"IEND", b"")


def filter_rows(rows, bpp, types):
    """Forward PNG filters (test side) so the decoder's reverse filters are exercised."""
    out = []
    prev = bytearray(len(rows[0]))
    for row, t in zip(rows, types):
        r = bytearray(row)
        f = bytearray(len(r))
        for i in range(len(r)):
            a = r[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            pred = {0: 0, 1: a, 2: b, 3: (a + b) >> 1, 4: tim2._paeth(a, b, c)}[t]
            f[i] = (r[i] - pred) & 0xFF
        out.append(bytes(f))
        prev = r
    return out


class TestPng(unittest.TestCase):
    def test_encode_decode_rgba_and_palette(self):
        px = [(i, 255 - i, i // 2, 255 if i % 3 else 128) for i in range(12)]
        self.assertEqual(tim2.decode_png(tim2.encode_png(4, 3, px))[2], px)
        opaque = [c[:3] + (255,) for c in px]
        blob = tim2.encode_png(4, 3, opaque)
        self.assertEqual(blob[25], 2)                    # RGB when nothing is transparent
        self.assertEqual(tim2.decode_png(blob)[2], opaque)
        pal = [(10, 20, 30, 255), (40, 50, 60, 0), (70, 80, 90, 128)]
        idx = [0, 1, 2, 1, 0, 2]
        w, h, rgba, i2, p2 = tim2.decode_png(tim2.encode_png(3, 2, indices=idx, pal=pal))
        self.assertEqual(i2, idx)
        self.assertEqual(p2, pal)

    def test_all_filters(self):
        rnd = random.Random(3)
        rows = [bytes(rnd.randrange(256) for _ in range(5 * 4)) for _ in range(5)]
        types = [0, 1, 2, 3, 4]
        blob = png_raw(5, 5, 6, 8, filter_rows(rows, 4, types), filt=types)
        w, h, rgba, _, _ = tim2.decode_png(blob)
        flat = b"".join(rows)
        self.assertEqual(rgba, [tuple(flat[i:i + 4]) for i in range(0, len(flat), 4)])

    def test_grey_grey_alpha_and_trns(self):
        g = tim2.decode_png(png_raw(2, 1, 0, 8, [bytes((7, 9))], extra=tim2._chunk(b"tRNS", b"\x00\x09")))
        self.assertEqual(g[2], [(7, 7, 7, 255), (9, 9, 9, 0)])
        ga = tim2.decode_png(png_raw(1, 1, 4, 8, [bytes((5, 6))]))
        self.assertEqual(ga[2], [(5, 5, 5, 6)])
        rgb = tim2.decode_png(png_raw(2, 1, 2, 8, [bytes((1, 2, 3, 4, 5, 6))],
                                      extra=tim2._chunk(b"tRNS", b"\x00\x04\x00\x05\x00\x06")))
        self.assertEqual(rgb[2], [(1, 2, 3, 255), (4, 5, 6, 0)])

    def test_low_bit_depth_palette(self):
        plte = bytes((0, 0, 0, 255, 255, 255, 255, 0, 0, 0, 0, 255))
        blob = png_raw(4, 1, 3, 2, [bytes((0b00011011,))], plte=plte)
        self.assertEqual(tim2.decode_png(blob)[3], [0, 1, 2, 3])
        blob = png_raw(3, 1, 3, 4, [bytes((0x12, 0x00))], plte=plte)
        self.assertEqual(tim2.decode_png(blob)[3], [1, 2, 0])

    def test_refusals(self):
        good = tim2.encode_png(2, 2, [(1, 2, 3, 255)] * 4)
        bad_crc = bytearray(good)
        bad_crc[30] ^= 0xFF
        cases = {
            "notpng": b"GIF89a" + bytes(30),
            "crc": bytes(bad_crc),
            "interlaced": png_raw(1, 1, 2, 8, [bytes(3)], interlace=1),
            "16bit": png_raw(1, 1, 2, 16, [bytes(6)]),
            "truncated": good[:40],
            "badfilter": png_raw(1, 1, 2, 8, [bytes(3)], filt=[9]),
            "noplte": png_raw(1, 1, 3, 8, [bytes(1)]),
            "badindex": png_raw(1, 1, 3, 8, [bytes((5,))], plte=bytes(6)),
            "colortype": png_raw(1, 1, 5, 8, [bytes(1)]),
        }
        for name, blob in cases.items():
            with self.assertRaises(tim2.UsageError, msg=name):
                tim2.decode_png(blob, name)


# --------------------------------------------------------------------------
# Finding TIM2 inside bigger files, and putting them back
# --------------------------------------------------------------------------


class TestScanInsert(Base):
    def container(self):
        rnd = random.Random(11)
        a = tim2_file([pic8(True)])
        b = tim2_file([pic32(), pic16()], fmt_id=1)
        fake = b"TIM2" + bytes([4, 9, 1, 0]) + bytes(8)        # bad alignment id: not a TIM2
        junk = lambda n: bytes(rnd.randrange(256) for _ in range(n))
        blob = junk(100) + fake + junk(37) + a + junk(5) + b + junk(64)
        offs = (100 + 16 + 37, 100 + 16 + 37 + len(a) + 5)
        return self.write("archive.bin", blob), offs, (a, b)

    def test_scan_and_extract(self):
        path, offs, blobs = self.container()
        code, out, _ = run(tim2, ["scan", path, "--json", "--extract", os.path.join(self.d, "ex")])
        self.assertEqual(code, 0)
        j = json.loads(out)
        self.assertEqual([int(r["offset"], 16) for r in j["blobs"]], list(offs))
        self.assertEqual([r["size"] for r in j["blobs"]], [len(b) for b in blobs])
        for r, b in zip(j["blobs"], blobs):
            self.assertEqual(self.read(r["extracted"]), b)
        code, out, _ = run(tim2, ["scan", path])
        self.assertIn("2 TIM2 blob(s)", out)

    def test_scan_across_chunk_boundaries(self):
        path, offs, _ = self.container()
        old = tim2.CHUNK
        try:
            for size in (64, 97, 150):
                tim2.CHUNK = size
                self.assertEqual([b[0] for b in tim2.find_blobs(path)], list(offs), size)
        finally:
            tim2.CHUNK = old

    def test_scan_nothing(self):
        p = self.write("plain.bin", b"no textures here" * 10)
        code, out, _ = run(tim2, ["scan", p])
        self.assertEqual(code, 1)

    def test_insert_round_trip(self):
        path, offs, blobs = self.container()
        _, pics = tim2.parse_tim2(blobs[0])
        new_blob = bytearray(blobs[0])
        new_blob[pics[0].image_off] ^= 0xFF
        edited = self.write("edited.tm2", bytes(new_blob))
        out = os.path.join(self.d, "patched", "archive.bin")
        code, so, err = run(tim2, ["insert", path, "--offset", hex(offs[0]), "--tm2", edited, "--out", out])
        self.assertEqual(code, 0, err)
        old, new = self.read(path), self.read(out)
        self.assertEqual(len(old), len(new))
        self.assertEqual(new[offs[0]:offs[0] + len(new_blob)], bytes(new_blob))
        self.assertEqual(new[:offs[0]], old[:offs[0]])
        self.assertEqual(new[offs[0] + len(new_blob):], old[offs[0] + len(new_blob):])

    def test_insert_refusals(self):
        path, offs, blobs = self.container()
        wrong = self.write("wrong.tm2", tim2_file([pic32()]))
        right = self.write("right.tm2", blobs[0])
        for args in (["--offset", hex(offs[0]), "--tm2", wrong],          # different size
                     ["--offset", "0x10", "--tm2", right],                # no TIM2 there
                     ["--offset", "0xFFFFFFF", "--tm2", right],           # past the end
                     ["--offset", "zz", "--tm2", right],
                     ["--offset", hex(offs[0]), "--tm2", right, "--out", path]):   # in place without --force
            if "--out" not in args:
                args = args + ["--out", os.path.join(self.d, "o.bin")]
            code, _, err = run(tim2, ["insert", path] + args)
            self.assertEqual(code, 2, args)
            self.assertNotIn("Traceback", err)


# --------------------------------------------------------------------------
# widescreen.py
# --------------------------------------------------------------------------

PF_X, PF_W, PF_R = 1, 2, 4


def f2u(f):
    return struct.unpack("<I", struct.pack("<f", f))[0]


def words(*ws_):
    return b"".join(struct.pack("<I", w) for w in ws_)


def make_elf(text, data):
    segs = [(0x00100000, text, PF_R | PF_X), (0x00200000, data, PF_R | PF_W)]
    off = 52 + 32 * len(segs)
    body = bytearray()
    placed = []
    for vaddr, blob, flags in segs:
        body += bytes((-(off + len(body))) % 16)
        placed.append(off + len(body))
        body += blob
    hdr = b"\x7fELF" + bytes([1, 1, 1, 0]) + bytes(8)
    hdr += struct.pack("<HHIIIIIHHHHHH", 2, 8, 1, 0x00100000, 52, 0, 0, 52, 32, len(segs), 40, 0, 0)
    ph = b"".join(struct.pack("<IIIIIIII", 1, foff, v, v, len(b), len(b), fl, 16) for (v, b, fl), foff in zip(segs, placed))
    return hdr + ph + bytes(body)


WS_TEXT = words(
    0x3C023FAA,  # lui  $v0, 0x3FAA
    0x3442AAAB,  # ori  $v0, $v0, 0xAAAB      -> 4/3
    0x44826000,  # mtc1 $v0, $f12
    0x3C033F40,  # lui  $v1, 0x3F40           -> 0.75 (lone lui)
    0x44831000,  # mtc1 $v1, $f2
    0x03E00008, 0x00000000,
)
WS_DATA = words(f2u(4.0 / 3.0), f2u(0.75), f2u(640.0 / 448.0), f2u(1.0), f2u(448.0 / 512.0))


class TestWidescreen(Base):
    def setUp(self):
        Base.setUp(self)
        self.elf = self.write("SLUS_999.99", make_elf(WS_TEXT, WS_DATA))

    def test_scan_candidates_and_16_9_values(self):
        code, out, _ = run(ws, ["scan", self.elf, "--json"])
        self.assertEqual(code, 0)
        j = json.loads(out)
        by = dict((c["addr"], c) for c in j["candidates"])
        self.assertEqual(sorted(by), ["0x00100000", "0x0010000C", "0x00200000", "0x00200004", "0x00200008", "0x00200010"])
        self.assertEqual(by["0x00200000"]["wide_bits"], "0x3FE38E39")       # 16:9 = 1.7777778
        self.assertEqual(by["0x00200000"]["pnach"], ["patch=1,EE,00200000,word,3FE38E39"])
        self.assertEqual(by["0x00200004"]["wide_bits"], "0x3F100000")       # 0.5625
        self.assertEqual(by["0x00200008"]["wide_value"], "1.90476191")
        self.assertEqual(by["0x00200010"]["wide_value"], "0.65625")
        self.assertEqual(by["0x00100000"]["pnach"], ["patch=1,EE,00100000,word,3C023FE3",
                                                      "patch=1,EE,00100004,word,34428E39"])
        self.assertEqual(by["0x0010000C"]["pnach"], ["patch=1,EE,0010000C,word,3C033F10"])
        self.assertTrue(j["pcsx2_pnach"].startswith("SLUS-99999_"))
        self.assertIn("cannot know", j["honest"])

    def test_lone_lui_rounding_note(self):
        text = words(0x3C033FAA, 0x44831000, 0x03E00008, 0)   # lui 0x3FAA alone = 1.328125, not quite 4/3
        e = self.write("LUI.ELF", make_elf(text, words(0)))
        cands = ws.candidates(ws.Elf(e), 10)
        self.assertEqual(cands, [])                           # 1.328 is outside the 0.05% net
        text = words(0x3C033FA0, 0x44831000, 0x03E00008, 0)   # 1.25 = 640/512, wide 1.6666666 needs a low half
        e = self.write("LUI2.ELF", make_elf(text, words(0)))
        c = ws.candidates(ws.Elf(e), 10)[0]
        self.assertIn("lone lui", c["notes"][0])
        self.assertEqual(c["pnach"], ["patch=1,EE,00100000,word,3C033FD5"])

    def test_out_pnach_parses_with_console_mod(self):
        out = os.path.join(self.d, "console", "patches", "widescreen-try.pnach")
        code, so, _ = run(ws, ["scan", self.elf, "--out", out])
        self.assertEqual(code, 0)
        self.assertIn("cannot know", so)
        text = open(out).read()
        self.assertEqual(text.count("gsaspectratio=16:9"), 6)
        pnach = load("console-mod", "pnach")
        res = pnach.parse_pnach_text(text)
        self.assertEqual(len(res["sections"]), 6)
        self.assertEqual(res["errors"], [])
        self.assertEqual(len(res["writes"]), 7)
        self.assertEqual(run(ws, ["scan", self.elf, "--out", out])[0], 2)
        self.assertEqual(run(ws, ["scan", self.elf, "--out", out, "--force"])[0], 0)

    def test_text_output(self):
        code, out, _ = run(ws, ["scan", self.elf])
        self.assertIn("try 01", out)
        self.assertIn("-> 1.77777779", out)
        self.assertIn("/console-mod", out)

    def test_values_and_fov(self):
        code, out, _ = run(ws, ["values", "--fov", "60", "--json"])
        j = json.loads(out)
        self.assertAlmostEqual(j["fov"]["horizontal_16_9"], 75.1782, places=3)
        self.assertEqual(j["aspects"][0]["wide_bits"], "0x3FE38E39")
        code, out, _ = run(ws, ["values"])
        self.assertIn("2*atan", out)
        self.assertEqual(run(ws, ["values", "--fov", "200"])[0], 2)

    def test_nothing_and_bad_elf(self):
        e = self.write("NONE.ELF", make_elf(words(0x03E00008, 0), words(f2u(2.0))))
        self.assertEqual(run(ws, ["scan", e])[0], 1)
        code, _, err = run(ws, ["scan", self.write("bad", b"\x7fELF\x01\x01")])
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)


class TestCli(Base):
    def cli(self, script, *args):
        return subprocess.run([sys.executable, os.path.join(SKILL, script)] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)

    def test_help(self):
        for script, cmds in (("tim2.py", ["info", "topng", "frompng", "scan", "insert"]),
                             ("widescreen.py", ["scan", "values"])):
            top = self.cli(script, "--help")
            self.assertEqual(top.returncode, 0)
            for c in cmds:
                self.assertIn(c, top.stdout)
                r = self.cli(script, c, "--help")
                self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self.cli(script).returncode, 2)

    def test_subprocess_end_to_end(self):
        src = self.write("t.tm2", tim2_file([pic8()]))
        out = os.path.join(self.d, "o")
        r = self.cli("tim2.py", "topng", src, "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.cli("tim2.py", "frompng", src, os.path.join(out, "t.png"), "--out", os.path.join(self.d, "n.tm2"))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.cli("tim2.py", "info", os.path.join(self.d, "missing.tm2"))
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)
        r = self.cli("widescreen.py", "values")
        self.assertEqual(r.returncode, 0)

    def test_house_rules(self):
        for dirpath, _, files in os.walk(SKILL):
            for f in files:
                with open(os.path.join(dirpath, f), "rb") as fh:
                    self.assertNotIn(chr(0x2014).encode("utf-8"), fh.read(), f)
        md = open(os.path.join(SKILL, "SKILL.md")).read()
        self.assertTrue(md.startswith("---\nname: console-graphics\n"))
        for ref in re.findall(r"`(?:\$\{CLAUDE_SKILL_DIR\}/)?((?:reference|templates)/[^`\s]+)`", md):
            self.assertTrue(os.path.isfile(os.path.join(SKILL, ref)), ref)
        for ref in re.findall(r"\]\(((?:reference|templates)/[^)]+)\)", md):
            self.assertTrue(os.path.isfile(os.path.join(SKILL, ref)), ref)
        for ref in re.findall(r"\$\{CLAUDE_SKILL_DIR\}/([^\s`\"]+)", md):
            self.assertTrue(os.path.exists(os.path.join(SKILL, ref)), ref)


if __name__ == "__main__":
    unittest.main()
