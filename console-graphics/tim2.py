#!/usr/bin/env python3
"""tim2.py: PS2 TIM2 textures (.tm2) to PNG and back, same size and format.

  info     every picture in a .tm2: size, pixel format, palette, mipmaps, GS registers
  topng    .tm2 (or a folder of them) -> PNG: 32/24/16-bit direct colour, 8/4-bit indexed
           (CSM1 palette swizzle undone; PS2 alpha 0x80 = opaque becomes 0xFF)
  frompng  your edited PNG -> a new .tm2 with the SAME size, format and GS registers
           (indexed: the PNG must fit the palette, or --quantize does a median cut)
  scan     find TIM2 blobs inside bigger files (archives, ELFs), with offsets; --extract
  insert   put an edited .tm2 back into a bigger file at the same offset, same size

PNG encoder and decoder are built in (zlib + struct). Python 3.8+, standard library only.
"""

import argparse
import json
import os
import re
import shutil
import struct
import sys
import zlib

TOOL = "tim2.py"
MAGIC = b"TIM2"
CHUNK = 8 * 1024 * 1024     # scan reads big files in pieces this size
TYPES = {1: ("16-bit RGBA5551", 16), 2: ("24-bit RGB", 24), 3: ("32-bit RGBA", 32), 4: ("4-bit indexed", 4),
         5: ("8-bit indexed", 8)}
CLUT_FORMATS = {1: "16-bit", 2: "24-bit", 3: "32-bit"}
PSM = {0x00: "PSMCT32", 0x01: "PSMCT24", 0x02: "PSMCT16", 0x0A: "PSMCT16S", 0x13: "PSMT8", 0x14: "PSMT4",
       0x1B: "PSMT8H", 0x24: "PSMT4HL", 0x2C: "PSMT4HH"}


class UsageError(Exception):
    """Bad input the user can fix: printed as one line, exit code 2."""


def fail(msg):
    raise UsageError(msg)


# --------------------------------------------------------------------------
# Colour helpers
# --------------------------------------------------------------------------


def alpha_to_png(a):
    """PS2 alpha (0x80 = opaque) to 8-bit alpha."""
    return 255 if a >= 128 else (a * 255 + 64) // 128


def alpha_to_ps2(a):
    return (a * 128 + 127) // 255


def expand5(x):
    return (x << 3) | (x >> 2)


def pack5(v):
    return (v * 31 + 127) // 255


def decode_colors(raw, fmt, count, alpha_unused=None):
    """Raw texels or CLUT entries -> list of (r, g, b, a)."""
    out = []
    if fmt == 32:
        for i in range(count):
            r, g, b, a = raw[4 * i:4 * i + 4]
            out.append((r, g, b, alpha_to_png(a)))
    elif fmt == 24:
        for i in range(count):
            r, g, b = raw[3 * i:3 * i + 3]
            out.append((r, g, b, 255))
    elif fmt == 16:
        vals = struct.unpack_from("<%dH" % count, raw, 0) if count else ()
        if alpha_unused is None:
            alpha_unused = not any(v & 0x8000 for v in vals)
        for v in vals:
            a = 255 if (alpha_unused or v & 0x8000) else 0
            out.append((expand5(v & 31), expand5((v >> 5) & 31), expand5((v >> 10) & 31), a))
    return out


def encode_color(c, fmt, alpha_unused=False):
    r, g, b, a = c
    if fmt == 32:
        return bytes((r, g, b, alpha_to_ps2(a)))
    if fmt == 24:
        return bytes((r, g, b))
    v = pack5(r) | (pack5(g) << 5) | (pack5(b) << 10) | (0 if alpha_unused else (0x8000 if a >= 128 else 0))
    return struct.pack("<H", v)


def swizzle(i):
    """CSM1 palette order: entries 8-15 and 16-23 of every 32 are swapped (its own inverse)."""
    m = i & 0x1F
    if 8 <= m < 16:
        return i + 8
    if 16 <= m < 24:
        return i - 8
    return i


# --------------------------------------------------------------------------
# TIM2 parsing
# --------------------------------------------------------------------------


class Picture(object):
    pass


def parse_tim2(data, base=0, name="file"):
    """Parse a TIM2 at data[base:]. Returns (header dict, [Picture]). Raises UsageError."""
    if len(data) - base < 16:
        fail("%s is too short to be a TIM2 file" % name)
    if data[base:base + 4] != MAGIC:
        fail("%s is not a TIM2 file (no 'TIM2' magic)" % name)
    version, fmt_id, count = struct.unpack_from("<BBH", data, base + 4)
    if fmt_id not in (0, 1):
        fail("%s has an unknown TIM2 alignment id %d" % (name, fmt_id))
    if count == 0:
        fail("%s holds no pictures" % name)
    pos = base + (0x80 if fmt_id == 1 else 0x10)
    pics = []
    for n in range(count):
        if pos + 0x30 > len(data):
            fail("%s is truncated: picture %d header runs past the end" % (name, n))
        (total, clut_size, image_size, header_size, clut_colors, pict_fmt, mips, clut_type, image_type,
         width, height) = struct.unpack_from("<IIIHHBBBBHH", data, pos)
        tex0, tex1 = struct.unpack_from("<QQ", data, pos + 0x18)
        regs, texclut = struct.unpack_from("<II", data, pos + 0x28)
        if image_type not in TYPES:
            fail("%s picture %d has an unknown image type %d" % (name, n, image_type))
        if header_size < 0x30 or total < header_size + image_size + clut_size:
            fail("%s picture %d has inconsistent sizes" % (name, n))
        if width == 0 or height == 0:
            fail("%s picture %d has a zero size" % (name, n))
        if pos + total > len(data):
            fail("%s is truncated: picture %d runs past the end of the file" % (name, n))
        p = Picture()
        p.index, p.offset, p.total, p.clut_size, p.image_size = n, pos, total, clut_size, image_size
        p.header_size, p.clut_colors, p.pict_fmt, p.mips = header_size, clut_colors, pict_fmt, max(mips, 1)
        p.clut_type, p.image_type, p.width, p.height = clut_type, image_type, width, height
        p.tex0, p.tex1, p.regs, p.texclut = tex0, tex1, regs, texclut
        p.bpp = TYPES[image_type][1]
        p.indexed = image_type in (4, 5)
        p.image_off = pos + header_size
        p.clut_off = p.image_off + image_size
        if p.indexed:
            cf = clut_type & 0x3F
            if cf not in CLUT_FORMATS or clut_colors == 0 or clut_size == 0:
                fail("%s picture %d is indexed but has no usable palette" % (name, n))
            p.clut_bpp = {1: 16, 2: 24, 3: 32}[cf]
            if clut_size < clut_colors * p.clut_bpp // 8:
                fail("%s picture %d palette is shorter than its colour count" % (name, n))
            p.pal_size = 256 if image_type == 5 else 16
            p.swizzled = (not (clut_type & 0x80)) if image_type == 5 else bool(clut_type & 0x40)
            p.palettes = max(1, clut_colors // p.pal_size)
        else:
            p.clut_bpp, p.pal_size, p.swizzled, p.palettes = 0, 0, False, 0
        # mip levels: sizes from the MIPMAP header when present
        p.levels = []
        dims = [(max(1, width >> k), max(1, height >> k)) for k in range(p.mips)]
        sizes = None
        if p.mips > 1 and header_size >= 0x30 + 16 + 4 * p.mips:
            sizes = list(struct.unpack_from("<%dI" % p.mips, data, pos + 0x30 + 16))
        off = p.image_off
        for k, (w, h) in enumerate(dims):
            need = (w * h * p.bpp + 7) // 8
            size = sizes[k] if sizes else need
            if size < need or off + size > p.image_off + image_size:
                fail("%s picture %d mip level %d does not fit its image data" % (name, n, k))
            p.levels.append((off, w, h, size))
            off += size
        pics.append(p)
        pos += total
    return {"version": version, "align": 0x80 if fmt_id == 1 else 0x10, "count": count, "end": pos}, pics


def level_stride(p, w, h, size):
    """Bytes per row (rows padded to whole bytes), or None for one continuous bit stream."""
    row = (w * p.bpp + 7) // 8
    if size >= row * h:
        return row
    return None


def read_indices_or_raw(data, p, level=0):
    off, w, h, size = p.levels[level]
    stride = level_stride(p, w, h, size)
    bpp = p.bpp
    if bpp >= 8:
        step = bpp // 8
        out = []
        for y in range(h):
            row = off + (y * stride if stride else y * w * step)
            out.extend(bytes(data[row + x * step:row + x * step + step]) for x in range(w))
        return out
    idx = []
    for y in range(h):
        for x in range(w):
            if stride:
                byte = data[off + y * stride + x // 2]
                idx.append(byte & 0x0F if x % 2 == 0 else byte >> 4)
            else:
                pix = y * w + x
                byte = data[off + pix // 2]
                idx.append(byte & 0x0F if pix % 2 == 0 else byte >> 4)
    return idx


def palette(data, p, which=0):
    """Logical palette (swizzle undone) as RGBA tuples, padded to pal_size."""
    n = min(p.pal_size, p.clut_colors - which * p.pal_size)
    if n <= 0:
        fail("picture %d has no palette %d (it has %d)" % (p.index, which, p.palettes))
    esize = p.clut_bpp // 8
    start = p.clut_off + which * p.pal_size * esize
    raw = bytes(data[start:start + n * esize])
    stored = decode_colors(raw, p.clut_bpp, n)
    pal = []
    for k in range(p.pal_size):
        s = swizzle(k) if p.swizzled else k
        pal.append(stored[s] if s < len(stored) else (0, 0, 0, 0))
    return pal


def decode_picture(data, p, level=0, which=0):
    """-> (width, height, rgba list, indices or None, palette or None)."""
    _, w, h, _ = p.levels[level]
    vals = read_indices_or_raw(data, p, level)
    if p.indexed:
        pal = palette(data, p, which)
        if p.bpp == 8:
            vals = [v[0] for v in vals]
        return w, h, [pal[i] for i in vals], vals, pal
    raw = b"".join(vals)
    alpha_unused = None
    if p.bpp == 16:
        lvl0 = b"".join(read_indices_or_raw(data, p, 0)) if level else raw
        alpha_unused = not any(v & 0x8000 for v in struct.unpack("<%dH" % (len(lvl0) // 2), lvl0))
    return w, h, decode_colors(raw, p.bpp, w * h, alpha_unused), None, None


def tex0_fields(t):
    return {"TBP0": t & 0x3FFF, "TBW": (t >> 14) & 0x3F, "PSM": PSM.get((t >> 20) & 0x3F, hex((t >> 20) & 0x3F)),
            "TW": (t >> 26) & 0xF, "TH": (t >> 30) & 0xF, "TCC": (t >> 34) & 1, "TFX": (t >> 35) & 3,
            "CBP": (t >> 37) & 0x3FFF, "CPSM": PSM.get((t >> 51) & 0xF, hex((t >> 51) & 0xF)), "CSM": (t >> 55) & 1,
            "CSA": (t >> 56) & 0x1F, "CLD": (t >> 61) & 7}


# --------------------------------------------------------------------------
# PNG encoder / decoder
# --------------------------------------------------------------------------

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(kind, payload):
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def encode_png(w, h, rgba=None, indices=None, pal=None):
    """RGBA list -> RGB(A) PNG; or indices + palette -> 8-bit palette PNG (tRNS for alpha)."""
    rows = bytearray()
    if indices is not None:
        ihdr = struct.pack(">IIBBBBB", w, h, 8, 3, 0, 0, 0)
        for y in range(h):
            rows.append(0)
            rows += bytes(indices[y * w:(y + 1) * w])
        extra = _chunk(b"PLTE", b"".join(bytes(c[:3]) for c in pal))
        alphas = [c[3] for c in pal]
        if any(a != 255 for a in alphas):
            last = max(i for i, a in enumerate(alphas) if a != 255)
            extra += _chunk(b"tRNS", bytes(alphas[:last + 1]))
    else:
        has_alpha = any(c[3] != 255 for c in rgba)
        ihdr = struct.pack(">IIBBBBB", w, h, 8, 6 if has_alpha else 2, 0, 0, 0)
        n = 4 if has_alpha else 3
        for y in range(h):
            rows.append(0)
            for c in rgba[y * w:(y + 1) * w]:
                rows += bytes(c[:n])
        extra = b""
    return PNG_SIG + _chunk(b"IHDR", ihdr) + extra + _chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + _chunk(b"IEND", b"")


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_png(blob, name="PNG"):
    """-> (w, h, rgba list, palette indices or None, palette or None). 8-bit, non-interlaced only."""
    if blob[:8] != PNG_SIG:
        fail("%s is not a PNG" % name)
    pos = 8
    ihdr = None
    plte = None
    trns = None
    idat = bytearray()
    while pos + 8 <= len(blob):
        length, kind = struct.unpack_from(">I4s", blob, pos)
        if pos + 12 + length > len(blob):
            fail("%s is truncated (chunk %r)" % (name, kind))
        payload = blob[pos + 8:pos + 8 + length]
        crc = struct.unpack_from(">I", blob, pos + 8 + length)[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != crc:
            fail("%s is corrupt (bad CRC in chunk %r)" % (name, kind.decode("ascii", "replace")))
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", payload)
        elif kind == b"PLTE":
            plte = [tuple(payload[i:i + 3]) for i in range(0, len(payload) - 2, 3)]
        elif kind == b"tRNS":
            trns = bytes(payload)
        elif kind == b"IDAT":
            idat += payload
        elif kind == b"IEND":
            break
        pos += 12 + length
    if ihdr is None:
        fail("%s has no IHDR" % name)
    w, h, depth, ctype, comp, filt, interlace = ihdr
    if interlace:
        fail("%s is interlaced: save it again without interlacing" % name)
    if comp or filt:
        fail("%s uses an unknown compression or filter method" % name)
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if channels is None:
        fail("%s has an unknown colour type %d" % (name, ctype))
    if depth != 8 and not (ctype == 3 and depth in (1, 2, 4)):
        fail("%s is %d-bit per channel: save it as an 8-bit PNG (RGB, RGBA, grey or palette)" % (name, depth))
    if ctype == 3 and not plte:
        fail("%s is a palette PNG without a palette" % name)
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        fail("%s has corrupt image data" % name)
    stride = (w * channels * depth + 7) // 8
    bpp = max(1, channels * depth // 8)
    if len(raw) < h * (stride + 1):
        fail("%s image data is truncated" % name)
    prev = bytearray(stride)
    rows = []
    for y in range(h):
        ft = raw[y * (stride + 1)]
        cur = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if ft == 1:
            for i in range(bpp, stride):
                cur[i] = (cur[i] + cur[i - bpp]) & 0xFF
        elif ft == 2:
            for i in range(stride):
                cur[i] = (cur[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                left = cur[i - bpp] if i >= bpp else 0
                cur[i] = (cur[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(stride):
                left = cur[i - bpp] if i >= bpp else 0
                up_left = prev[i - bpp] if i >= bpp else 0
                cur[i] = (cur[i] + _paeth(left, prev[i], up_left)) & 0xFF
        elif ft != 0:
            fail("%s has an unknown row filter %d" % (name, ft))
        rows.append(cur)
        prev = cur
    rgba = []
    indices = None
    pal = None
    if ctype == 3:
        alphas = list(trns or b"")
        pal = [tuple(c) + ((alphas[i] if i < len(alphas) else 255),) for i, c in enumerate(plte)]
        indices = []
        per = 8 // depth
        mask = (1 << depth) - 1
        for row in rows:
            for x in range(w):
                if depth == 8:
                    i = row[x]
                else:
                    byte = row[x // per]
                    i = (byte >> (8 - depth * (x % per + 1))) & mask
                if i >= len(pal):
                    fail("%s uses palette entry %d but its palette has %d" % (name, i, len(pal)))
                indices.append(i)
        rgba = [pal[i] for i in indices]
    else:
        key = None
        if trns and ctype == 0 and len(trns) >= 2:
            key = (trns[1],)
        if trns and ctype == 2 and len(trns) >= 6:
            key = (trns[1], trns[3], trns[5])
        for row in rows:
            for x in range(w):
                px = tuple(row[x * channels:(x + 1) * channels])
                if ctype == 0:
                    c = (px[0], px[0], px[0], 0 if key == px else 255)
                elif ctype == 4:
                    c = (px[0], px[0], px[0], px[1])
                elif ctype == 2:
                    c = px + ((0 if key == px else 255),)
                else:
                    c = px
                rgba.append(c)
    return w, h, rgba, indices, pal


# --------------------------------------------------------------------------
# Median cut quantiser (for --quantize)
# --------------------------------------------------------------------------


def median_cut(counts, k):
    """counts: {rgba: n}. Returns (palette list, {rgba: palette index})."""
    boxes = [list(counts.items())]
    while len(boxes) < k:
        best, best_i, best_ch = -1, None, 0
        for i, box in enumerate(boxes):
            if len(box) < 2:
                continue
            for ch in range(4):
                vals = [c[0][ch] for c in box]
                rng = max(vals) - min(vals)
                if rng > best:
                    best, best_i, best_ch = rng, i, ch
        if best_i is None or best <= 0:
            break
        box = sorted(boxes[best_i], key=lambda c: c[0][best_ch])
        half = sum(n for _, n in box) / 2.0
        acc = 0
        cut = 1
        for j, (_, n) in enumerate(box):
            acc += n
            if acc >= half:
                cut = min(max(j + 1, 1), len(box) - 1)
                break
        boxes[best_i:best_i + 1] = [box[:cut], box[cut:]]
    pal = []
    mapping = {}
    for i, box in enumerate(boxes):
        tot = sum(n for _, n in box)
        pal.append(tuple(int(round(sum(c[ch] * n for c, n in box) / float(tot))) for ch in range(4)))
        for c, _ in box:
            mapping[c] = i
    return pal, mapping


# --------------------------------------------------------------------------
# Rebuilding a picture from a PNG
# --------------------------------------------------------------------------


def nearest_level(vals, w, h, w2, h2):
    return [vals[(y * h // h2) * w + (x * w // w2)] for y in range(h2) for x in range(w2)]


def write_level(buf, p, level, vals):
    off, w, h, size = p.levels[level]
    stride = level_stride(p, w, h, size)
    if p.bpp >= 8:
        step = p.bpp // 8
        for y in range(h):
            row = off + (y * stride if stride else y * w * step)
            for x in range(w):
                v = vals[y * w + x]
                buf[row + x * step:row + x * step + step] = v if isinstance(v, (bytes, bytearray)) else bytes((v,))
        return
    for y in range(h):
        for x in range(w):
            i = vals[y * w + x] & 0x0F
            pix = y * w + x
            at = off + (y * stride + x // 2 if stride else pix // 2)
            low = (x % 2 == 0) if stride else (pix % 2 == 0)
            buf[at] = (buf[at] & 0xF0) | i if low else (buf[at] & 0x0F) | (i << 4)


def rebuild(data, p, png, which=0, quantize=False, keep_mips=False):
    """Returns (new file bytes, notes). Same size and format; header untouched."""
    w, h, rgba, _png_idx, _png_pal = png
    if (w, h) != (p.width, p.height):
        fail("the PNG is %dx%d but picture %d is %dx%d: keep the same size (the game's code and GS registers "
             "expect it)" % (w, h, p.index, p.width, p.height))
    notes = []
    buf = bytearray(data)
    ow, oh, orig_rgba, orig_idx, orig_pal = decode_picture(data, p, 0, which)
    if p.bpp == 24 and any(c[3] != 255 for c in rgba):
        notes.append("24-bit textures have no alpha: transparency in the PNG was dropped")
    if not p.indexed:
        raw = read_indices_or_raw(data, p, 0)
        alpha_unused = False
        if p.bpp == 16:
            alpha_unused = not any(struct.unpack("<H", r)[0] & 0x8000 for r in raw)
        vals = [raw[i] if rgba[i] == orig_rgba[i] else encode_color(rgba[i], p.bpp, alpha_unused)
                for i in range(w * h)]
        changed = sum(1 for i in range(w * h) if rgba[i] != orig_rgba[i])
    else:
        lookup = {}
        for i, c in enumerate(orig_pal):
            lookup.setdefault(c, i)
        if all(c in lookup for c in rgba):
            idx = [orig_idx[i] if orig_pal[orig_idx[i]] == rgba[i] else lookup[rgba[i]] for i in range(w * h)]
            new_pal = list(orig_pal)
        else:
            counts = {}
            for c in rgba:
                counts[c] = counts.get(c, 0) + 1
            if len(counts) > p.pal_size and not quantize:
                fail("the edit uses %d colours but this %d-bit texture holds %d: reduce the colours in your editor "
                     "(indexed/palette mode), or pass --quantize" % (len(counts), p.bpp, p.pal_size))
            if len(counts) <= p.pal_size:
                new_pal = list(orig_pal)
                keep = {}
                for i, c in enumerate(rgba):
                    if c in lookup and orig_pal[orig_idx[i]] == c:
                        keep[c] = orig_idx[i]
                for c in counts:
                    if c in lookup and c not in keep:
                        keep[c] = lookup[c]
                used = set(keep.values())
                free = [k for k in range(p.pal_size) if k not in used]
                fresh = [c for c in counts if c not in keep]
                if len(fresh) <= len(free):
                    for c, slot in zip(fresh, free):
                        new_pal[slot] = c
                        keep[c] = slot
                else:
                    new_pal = list(counts) + [(0, 0, 0, 0)] * (p.pal_size - len(counts))
                    keep = dict((c, i) for i, c in enumerate(counts))
                idx = [keep[c] for c in rgba]
            else:
                pal, mapping = median_cut(counts, p.pal_size)
                new_pal = pal + [(0, 0, 0, 0)] * (p.pal_size - len(pal))
                idx = [mapping[c] for c in rgba]
                notes.append("quantised %d colours down to %d (median cut)" % (len(counts), len(pal)))
        vals = idx
        changed = sum(1 for i in range(w * h) if rgba[i] != orig_rgba[i])
        if new_pal != orig_pal:
            n = min(p.pal_size, p.clut_colors - which * p.pal_size)
            esize = p.clut_bpp // 8
            start = p.clut_off + which * p.pal_size * esize
            stored_raw = bytes(data[start:start + n * esize])
            stored = decode_colors(stored_raw, p.clut_bpp, n)
            alpha_unused = p.clut_bpp == 16 and not any(struct.unpack("<H", stored_raw[2 * i:2 * i + 2])[0] & 0x8000 for i in range(n))
            for k in range(p.pal_size):
                s = swizzle(k) if p.swizzled else k
                if s >= n:
                    continue
                if new_pal[k] == orig_pal[k]:
                    continue
                buf[start + s * esize:start + (s + 1) * esize] = encode_color(new_pal[k], p.clut_bpp, alpha_unused)
            notes.append("palette %d rewritten (%d entries changed)" % (which, sum(1 for a, b in zip(new_pal, orig_pal) if a != b)))
    write_level(buf, p, 0, vals)
    if p.mips > 1 and changed:
        if keep_mips:
            notes.append("kept the original %d smaller mip levels (they still show the old art far away)" % (p.mips - 1))
        else:
            for k in range(1, p.mips):
                _, w2, h2, _ = p.levels[k]
                write_level(buf, p, k, nearest_level(vals, w, h, w2, h2))
            notes.append("regenerated %d mip level(s) from your image" % (p.mips - 1))
    notes.insert(0, "%d of %d pixels changed" % (changed, w * h))
    return bytes(buf), notes


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def read_bytes(path, what="file"):
    if not os.path.isfile(path):
        fail("%s not found: %s" % (what, path))
    with open(path, "rb") as f:
        return f.read()


def pic_info(p):
    d = {"index": p.index, "offset": p.offset, "width": p.width, "height": p.height,
         "type": TYPES[p.image_type][0], "bpp": p.bpp, "mipmaps": p.mips, "image_bytes": p.image_size,
         "header_bytes": p.header_size, "tex0": tex0_fields(p.tex0), "tex0_raw": "0x%016X" % p.tex0,
         "tex1_raw": "0x%016X" % p.tex1, "gs_regs": "0x%08X" % p.regs, "texclut": "0x%08X" % p.texclut}
    if p.indexed:
        d.update(palette_format=CLUT_FORMATS[p.clut_type & 0x3F], palette_colors=p.clut_colors,
                 palettes=p.palettes, palette_order="CSM1 swizzled" if p.swizzled else "linear",
                 clut_type="0x%02X" % p.clut_type)
    return d


def cmd_info(a):
    data = read_bytes(a.file, "TIM2")
    hdr, pics = parse_tim2(data, 0, os.path.basename(a.file))
    if a.json:
        print(json.dumps({"file": os.path.basename(a.file), "version": hdr["version"], "align": hdr["align"],
                          "size": len(data), "pictures": [pic_info(p) for p in pics]}, indent=2))
        return 0
    print("%s: TIM2 version %d, %d picture(s), %d-byte alignment, %d bytes" % (
        os.path.basename(a.file), hdr["version"], hdr["count"], hdr["align"], len(data)))
    for p in pics:
        t = tex0_fields(p.tex0)
        print("\n  picture %d at 0x%X: %dx%d %s%s" % (p.index, p.offset, p.width, p.height, TYPES[p.image_type][0],
                                                    (", %d mip levels" % p.mips) if p.mips > 1 else ""))
        if p.indexed:
            print("    palette   %d x %s colours (%d palette(s)), %s order" % (
                p.clut_colors, CLUT_FORMATS[p.clut_type & 0x3F], p.palettes, "CSM1 swizzled" if p.swizzled else "linear"))
        print("    TEX0      PSM %s, TW %d (%d px), TH %d (%d px), TBW %d, CPSM %s, CSM %d" % (
            t["PSM"], t["TW"], 1 << t["TW"], t["TH"], 1 << t["TH"], t["TBW"], t["CPSM"], t["CSM"]))
    if len(data) > hdr["end"]:
        print("\n  %d extra bytes after the last picture" % (len(data) - hdr["end"]))
    return 0


def tm2_files(path):
    if os.path.isdir(path):
        out = []
        for dp, _, files in os.walk(path):
            for f in sorted(files):
                if f.lower().endswith((".tm2", ".tim2")):
                    out.append(os.path.join(dp, f))
        return sorted(out)
    return [path]


def cmd_topng(a):
    files = tm2_files(a.input)
    if not files:
        fail("no .tm2 files under %s" % a.input)
    os.makedirs(a.out, exist_ok=True)
    root = a.input if os.path.isdir(a.input) else os.path.dirname(a.input)
    written = []
    problems = []
    for path in files:
        rel = os.path.relpath(path, root) if os.path.isdir(a.input) else os.path.basename(path)
        stem = os.path.splitext(rel)[0]
        try:
            data = read_bytes(path, "TIM2")
            _, pics = parse_tim2(data, 0, rel)
            for p in pics:
                levels = range(p.mips) if a.mips else [0]
                pals = range(p.palettes) if (a.all_palettes and p.indexed) else [a.palette if p.indexed else 0]
                for lv in levels:
                    for pal_i in pals:
                        w, h, rgba, idx, pal = decode_picture(data, p, lv, pal_i)
                        name = stem + ("_%d" % p.index if len(pics) > 1 else "")
                        name += ("_pal%d" % pal_i) if (p.indexed and (a.all_palettes or pal_i)) else ""
                        name += ("_mip%d" % lv) if lv else ""
                        dst = os.path.join(a.out, name + ".png")
                        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                        blob = encode_png(w, h, rgba) if (idx is None or a.rgba) else encode_png(w, h, indices=idx, pal=pal)
                        with open(dst, "wb") as f:
                            f.write(blob)
                        written.append(dst)
        except UsageError as e:
            if not os.path.isdir(a.input):
                raise
            problems.append("%s: %s" % (rel, e))
    for wpath in written:
        print("wrote " + wpath)
    for prob in problems:
        print("skipped " + prob, file=sys.stderr)
    print("%d PNG(s) from %d file(s). Repaint them with your own art, keep the size, then: tim2.py frompng" % (
        len(written), len(files)))
    if any(re.search(r"_\d+(_pal\d+)?(_mip\d+)?\.png$", x) for x in written):
        print("(name_N.png is picture N of a multi-picture file: frompng --picture N; _palN: --palette N)")
    return 1 if problems else 0


def cmd_frompng(a):
    data = read_bytes(a.original, "TIM2")
    _, pics = parse_tim2(data, 0, os.path.basename(a.original))
    if not 0 <= a.picture < len(pics):
        fail("picture %d does not exist (the file has %d)" % (a.picture, len(pics)))
    p = pics[a.picture]
    if p.indexed and not 0 <= a.palette < p.palettes:
        fail("palette %d does not exist (picture %d has %d)" % (a.palette, p.index, p.palettes))
    png = decode_png(read_bytes(a.png, "PNG"), os.path.basename(a.png))
    new, notes = rebuild(data, p, png, a.palette if p.indexed else 0, a.quantize, a.keep_mips)
    if os.path.abspath(a.out) == os.path.abspath(a.original) and not a.force:
        fail("--out is the original file: write a new file (or pass --force; keep a backup)")
    if os.path.exists(a.out) and not a.force:
        fail("%s exists: pass --force to overwrite it" % a.out)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "wb") as f:
        f.write(new)
    assert len(new) == len(data)
    print("wrote %s (%d bytes, same size and format as %s; GS registers unchanged)" % (
        a.out, len(new), os.path.basename(a.original)))
    for n in notes:
        print("  " + n)
    return 0


def find_blobs(path, limit=0):
    """Stream a big file for TIM2 blobs. Yields (offset, size, header, pictures)."""
    size = os.path.getsize(path)
    found = []
    with open(path, "rb") as f:
        chunk = max(CHUNK, 64)
        pos = 0
        tail = b""
        skip_until = 0
        while pos < size:
            f.seek(pos)
            block = f.read(chunk)
            buf = tail + block
            base = pos - len(tail)
            i = buf.find(MAGIC)
            while i >= 0:
                off = base + i
                if off >= skip_until:
                    f.seek(off)
                    head = f.read(16)
                    blob = None
                    if len(head) == 16:
                        _v, fmt_id, count = struct.unpack_from("<BBH", head, 4)
                        if fmt_id in (0, 1) and 0 < count <= 4096:
                            blob = _probe(f, off, size, fmt_id, count)
                    if blob is not None:
                        found.append(blob)
                        skip_until = off + blob[1]
                        if limit and len(found) >= limit:
                            return found
                i = buf.find(MAGIC, i + 1)
            tail = buf[-3:]
            pos += len(block)
    return found


def _probe(f, off, size, fmt_id, count):
    pos = off + (0x80 if fmt_id == 1 else 0x10)
    total_all = pos - off
    for _ in range(count):
        if pos + 0x30 > size:
            return None
        f.seek(pos)
        h = f.read(0x30)
        total, clut_size, image_size, header_size = struct.unpack_from("<IIIH", h, 0)
        image_type, width, height = h[0x13], struct.unpack_from("<H", h, 0x14)[0], struct.unpack_from("<H", h, 0x16)[0]
        if image_type not in TYPES or header_size < 0x30 or width == 0 or height == 0:
            return None
        if total < header_size + image_size + clut_size or pos + total > size:
            return None
        if image_size < (width * height * TYPES[image_type][1] + 7) // 8:
            return None
        pos += total
        total_all += total
    f.seek(off)
    data = f.read(total_all)
    try:
        hdr, pics = parse_tim2(data, 0, "blob")
    except UsageError:
        return None
    return (off, total_all, hdr, pics)


def cmd_scan(a):
    if not os.path.isfile(a.file):
        fail("file not found: %s" % a.file)
    blobs = find_blobs(a.file, a.limit)
    rows = []
    for off, n, hdr, pics in blobs:
        p = pics[0]
        rows.append({"offset": "0x%X" % off, "size": n, "pictures": len(pics),
                     "first": "%dx%d %s" % (p.width, p.height, TYPES[p.image_type][0])})
    if a.extract:
        os.makedirs(a.extract, exist_ok=True)
        stem = os.path.basename(a.file)
        with open(a.file, "rb") as f:
            for (off, n, _, _), row in zip(blobs, rows):
                f.seek(off)
                dst = os.path.join(a.extract, "%s_%08X.tm2" % (stem, off))
                with open(dst, "wb") as g:
                    g.write(f.read(n))
                row["extracted"] = dst
    if a.json:
        print(json.dumps({"file": os.path.basename(a.file), "blobs": rows}, indent=2))
        return 0 if rows else 1
    print("%s: %d TIM2 blob(s)" % (os.path.basename(a.file), len(rows)))
    for r in rows:
        print("  %-12s %9d bytes  %d picture(s), first %s%s" % (r["offset"], r["size"], r["pictures"], r["first"],
                                                             ("  -> " + r["extracted"]) if "extracted" in r else ""))
    if rows and not a.extract:
        print("Extract them with --extract DIR, edit, then put one back with: tim2.py insert")
    return 0 if rows else 1


def cmd_insert(a):
    try:
        off = int(a.offset, 16)
    except ValueError:
        fail("--offset is not a hex number: %r" % a.offset)
    if not os.path.isfile(a.container):
        fail("file not found: %s" % a.container)
    new = read_bytes(a.tm2, "TIM2")
    _, new_pics = parse_tim2(new, 0, os.path.basename(a.tm2))
    with open(a.container, "rb") as f:
        size = os.path.getsize(a.container)
        if off + 16 > size:
            fail("offset 0x%X is past the end of %s" % (off, os.path.basename(a.container)))
        f.seek(off)
        head = f.read(16)
        if head[:4] != MAGIC:
            fail("there is no TIM2 at 0x%X in %s (tim2.py scan lists the offsets)" % (off, os.path.basename(a.container)))
        _v, fmt_id, count = struct.unpack_from("<BBH", head, 4)
        blob = _probe(f, off, size, fmt_id, count) if fmt_id in (0, 1) and count else None
    if blob is None:
        fail("the TIM2 at 0x%X in %s is not readable" % (off, os.path.basename(a.container)))
    _, old_size, _, old_pics = blob
    if len(new) != old_size:
        fail("%s is %d bytes but the TIM2 at 0x%X is %d: it must be exactly the same size" % (
            os.path.basename(a.tm2), len(new), off, old_size))
    for p, q in zip(old_pics, new_pics):
        if (p.width, p.height, p.image_type, p.clut_colors) != (q.width, q.height, q.image_type, q.clut_colors):
            fail("picture %d differs in size or format from the one in the container" % p.index)
    if os.path.abspath(a.out) == os.path.abspath(a.container) and not a.force:
        fail("--out is the container itself: write a new file (or pass --force; keep a backup)")
    if os.path.exists(a.out) and not a.force:
        fail("%s exists: pass --force to overwrite it" % a.out)
    if os.path.abspath(a.out) != os.path.abspath(a.container):
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        shutil.copyfile(a.container, a.out)
    with open(a.out, "r+b") as f:
        f.seek(off)
        f.write(new)
    print("wrote %s: %d bytes at 0x%X replaced, file size unchanged" % (a.out, len(new), off))
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="tim2.py",
        description="PS2 TIM2 textures: dump to PNG, repaint, rebuild at the same size and format, find them "
                    "inside bigger files.",
        epilog="Exit codes: 0 ok, 1 nothing found or some files skipped, 2 bad input.")
    sub = p.add_subparsers(dest="cmd")
    sp = sub.add_parser("info", help="list the pictures in a .tm2")
    sp.add_argument("file")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_info)
    sp = sub.add_parser("topng", help="convert a .tm2 (or every .tm2 under a folder) to PNG")
    sp.add_argument("input", help="a .tm2 file or a folder")
    sp.add_argument("--out", required=True, help="output folder")
    sp.add_argument("--rgba", action="store_true", help="write indexed textures as RGBA instead of palette PNGs")
    sp.add_argument("--palette", type=int, default=0, help="which palette of an indexed texture (default 0)")
    sp.add_argument("--all-palettes", action="store_true", help="one PNG per palette")
    sp.add_argument("--mips", action="store_true", help="also write the smaller mip levels")
    sp.set_defaults(func=cmd_topng)
    sp = sub.add_parser("frompng", help="rebuild a .tm2 from your edited PNG (same size and format)")
    sp.add_argument("original", help="the original .tm2")
    sp.add_argument("png", help="your edited PNG, same width and height")
    sp.add_argument("--out", required=True, help="the new .tm2")
    sp.add_argument("--picture", type=int, default=0, help="which picture in a multi-picture file (default 0)")
    sp.add_argument("--palette", type=int, default=0, help="which palette to rewrite (default 0)")
    sp.add_argument("--quantize", action="store_true", help="reduce extra colours with a median cut")
    sp.add_argument("--keep-mips", action="store_true", help="keep the original smaller mip levels")
    sp.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    sp.set_defaults(func=cmd_frompng)
    sp = sub.add_parser("scan", help="find TIM2 blobs inside a bigger file")
    sp.add_argument("file")
    sp.add_argument("--extract", metavar="DIR", help="write each blob to DIR as <file>_<offset>.tm2")
    sp.add_argument("--limit", type=int, default=0, help="stop after N blobs")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_scan)
    sp = sub.add_parser("insert", help="put an edited .tm2 back into a bigger file at the same offset")
    sp.add_argument("container", help="the archive or file that holds the texture")
    sp.add_argument("--offset", required=True, help="hex offset from tim2.py scan")
    sp.add_argument("--tm2", required=True, help="the edited .tm2 (same size as the one it replaces)")
    sp.add_argument("--out", required=True, help="the new container file")
    sp.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    sp.set_defaults(func=cmd_insert)
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
