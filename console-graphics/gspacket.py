#!/usr/bin/env python3
"""gspacket.py: read the packets a PS2 game sends to its graphics chip, from a RAM dump.

  dma   walk a DMA source chain from its first tag: every tag (cnt, next, ref, call, ret, end ...),
        then decode the data it moves as a VIF1 stream (the usual 3D path) or as GIF packets
  vif   decode a VIF1 stream: VIF codes, UNPACK sizes, MPG microcode uploads, DIRECT payloads
  gif   decode GIF packets: tags, PACKED / REGLIST / IMAGE, every GS register written

Each command ends with the issues it found: a chain that loops or leaves the dump, a VIF code the
hardware does not know, a stream that ends in the middle of a command, a GIF packet that runs off
the end, an image upload whose size does not match its BITBLTBUF/TRXREG, a prohibited primitive.
Those are the usual reasons for a black screen or garbage textures in a port.

The input is a raw dump of guest memory: bytes that start at guest address --base (default 0, so
a full 32 MB RDRAM dump needs no --base). Sizes follow the PS2Recomp runtime's own VIF1
interpreter (ps2xRuntime/src/lib/ps2_vif1_interpreter.cpp) and GS frontend.

This grew out of vif_gif_surgeon.py in hkmodd/ps2-recomp-Agent-SKILL (MIT), which decodes one DMA
tag and its two VIF codes. Python 3.8+, standard library only.
"""

import argparse
import json
import os
import struct
import sys

TOOL = "gspacket.py"

DMA_IDS = {0: "refe", 1: "cnt", 2: "next", 3: "ref", 4: "refs", 5: "call", 6: "ret", 7: "end"}

# VIF codes (bits 24-30 of the code; bit 31 asks for an interrupt). Same values as the runtime's
# VIFCmd enum. Note MSCALF is 0x15 and MSCNT is 0x17.
VIF_NAMES = {
    0x00: "NOP", 0x01: "STCYCL", 0x02: "OFFSET", 0x03: "BASE", 0x04: "ITOP", 0x05: "STMOD",
    0x06: "MSKPATH3", 0x07: "MARK", 0x10: "FLUSHE", 0x11: "FLUSH", 0x13: "FLUSHA", 0x14: "MSCAL",
    0x15: "MSCALF", 0x17: "MSCNT", 0x20: "STMASK", 0x30: "STROW", 0x31: "STCOL", 0x4A: "MPG",
    0x50: "DIRECT", 0x51: "DIRECTHL",
}
UNPACK_FORMATS = {
    (0, 0): "S-32", (0, 1): "S-16", (0, 2): "S-8",
    (1, 0): "V2-32", (1, 1): "V2-16", (1, 2): "V2-8",
    (2, 0): "V3-32", (2, 1): "V3-16", (2, 2): "V3-8",
    (3, 0): "V4-32", (3, 1): "V4-16", (3, 2): "V4-8", (3, 3): "V4-5",
}

# GIF tag register descriptors (PACKED / REGLIST)
GIF_DESC = {
    0x0: "PRIM", 0x1: "RGBAQ", 0x2: "ST", 0x3: "UV", 0x4: "XYZF2", 0x5: "XYZ2", 0x6: "TEX0_1",
    0x7: "TEX0_2", 0x8: "CLAMP_1", 0x9: "CLAMP_2", 0xA: "FOG", 0xB: "(reserved)", 0xC: "XYZF3",
    0xD: "XYZ3", 0xE: "A+D", 0xF: "NOP",
}
GIF_FLG = {0: "PACKED", 1: "REGLIST", 2: "IMAGE", 3: "DISABLED"}

# GS registers by address (A+D writes). Same values as GSRegId in runtime/gs/gs_types.h.
GS_REGS = {
    0x00: "PRIM", 0x01: "RGBAQ", 0x02: "ST", 0x03: "UV", 0x04: "XYZF2", 0x05: "XYZ2",
    0x06: "TEX0_1", 0x07: "TEX0_2", 0x08: "CLAMP_1", 0x09: "CLAMP_2", 0x0A: "FOG", 0x0C: "XYZF3",
    0x0D: "XYZ3", 0x14: "TEX1_1", 0x15: "TEX1_2", 0x16: "TEX2_1", 0x17: "TEX2_2",
    0x18: "XYOFFSET_1", 0x19: "XYOFFSET_2", 0x1A: "PRMODECONT", 0x1B: "PRMODE", 0x1C: "TEXCLUT",
    0x22: "SCANMSK", 0x34: "MIPTBP1_1", 0x35: "MIPTBP1_2", 0x36: "MIPTBP2_1", 0x37: "MIPTBP2_2",
    0x3B: "TEXA", 0x3D: "FOGCOL", 0x3F: "TEXFLUSH", 0x40: "SCISSOR_1", 0x41: "SCISSOR_2",
    0x42: "ALPHA_1", 0x43: "ALPHA_2", 0x44: "DIMX", 0x45: "DTHE", 0x46: "COLCLAMP", 0x47: "TEST_1",
    0x48: "TEST_2", 0x49: "PABE", 0x4A: "FBA_1", 0x4B: "FBA_2", 0x4C: "FRAME_1", 0x4D: "FRAME_2",
    0x4E: "ZBUF_1", 0x4F: "ZBUF_2", 0x50: "BITBLTBUF", 0x51: "TRXPOS", 0x52: "TRXREG",
    0x53: "TRXDIR", 0x54: "HWREG", 0x60: "SIGNAL", 0x61: "FINISH", 0x62: "LABEL",
}
PRIM_TYPES = {0: "point", 1: "line", 2: "line strip", 3: "triangle", 4: "triangle strip",
              5: "triangle fan", 6: "sprite", 7: "PROHIBITED"}
PSM = {0x00: ("PSMCT32", 32), 0x01: ("PSMCT24", 24), 0x02: ("PSMCT16", 16), 0x0A: ("PSMCT16S", 16),
       0x13: ("PSMT8", 8), 0x14: ("PSMT4", 4), 0x1B: ("PSMT8H", 8), 0x24: ("PSMT4HL", 4),
       0x2C: ("PSMT4HH", 4), 0x30: ("PSMZ32", 32), 0x31: ("PSMZ24", 24), 0x32: ("PSMZ16", 16),
       0x3A: ("PSMZ16S", 16)}

ERROR = "error"
WARN = "warning"
NOTE = "note"


class UsageError(Exception):
    """Bad input the user can fix: printed as one line, exit code 2."""


def fail(msg):
    raise UsageError(msg)


def hx(v, width=8):
    return "0x%0*X" % (width, v)


def bits(v, lo, n):
    return (v >> lo) & ((1 << n) - 1)


def psm_name(v):
    return PSM.get(v, ("PSM 0x%02X?" % v, 0))[0]


# --------------------------------------------------------------------------
# The dump
# --------------------------------------------------------------------------


class Dump(object):
    """Guest memory from a file: bytes starting at guest address `base`."""

    def __init__(self, data, base=0):
        self.data = data
        self.base = base

    @classmethod
    def load(cls, path, base=0):
        if not os.path.isfile(path):
            fail("%s does not exist" % path)
        with open(path, "rb") as fh:
            data = fh.read()
        if not data:
            fail("%s is empty" % path)
        return cls(data, base)

    @staticmethod
    def physical(addr):
        """Guest address -> RDRAM physical address (KSEG0/KSEG1 and the uncached mirrors)."""
        if 0x20000000 <= addr < 0x22000000 or 0x30000000 <= addr < 0x32000000:
            return addr & 0x01FFFFFF
        if addr >= 0x80000000:
            return addr & 0x1FFFFFFF
        return addr

    def offset(self, addr):
        """File offset for a guest address, or None if the dump does not hold it."""
        off = self.physical(addr) - self.physical(self.base)
        if off < 0 or off >= len(self.data):
            return None
        return off

    def addr_of(self, off):
        return self.physical(self.base) + off

    def read(self, addr, size):
        off = self.offset(addr)
        if off is None or off + size > len(self.data):
            return None
        return self.data[off:off + size]


# --------------------------------------------------------------------------
# DMA source chain
# --------------------------------------------------------------------------


def walk_dma(dump, tag_addr, tte=True, tie=False, max_tags=10000):
    """Follow a source chain. Returns (packets, stream, segments, issues).

    stream is the bytes the channel would send (tag words when tte, then each packet's data);
    segments maps stream offsets back to guest addresses: (stream_off, length, guest_addr, packet).
    """
    packets, issues, segments = [], [], []
    stream = bytearray()
    stack = []
    seen = {}
    addr = tag_addr
    for n in range(max_tags):
        if addr % 16:
            issues.append((ERROR, "tag at %s is not 16-byte aligned" % hx(addr), addr))
            break
        raw = dump.read(addr, 16)
        if raw is None:
            issues.append((ERROR, "tag at %s is outside the dump (the chain points off the end, "
                                  "or --base is wrong)" % hx(addr), addr))
            break
        lo, hi, w2, w3 = struct.unpack("<IIII", raw)
        qwc = lo & 0xFFFF
        pce = bits(lo, 26, 2)
        tid = bits(lo, 28, 3)
        irq = bits(lo, 31, 1)
        daddr = hi & 0x7FFFFFFF
        spr = hi >> 31
        name = DMA_IDS[tid]
        key = (addr, tuple(stack))
        if key in seen:
            issues.append((ERROR, "the chain loops: tag %s was already visited as packet #%d"
                                  % (hx(addr), seen[key]), addr))
            break
        seen[key] = n

        if tid in (0, 3, 4):        # refe, ref, refs: data lives at ADDR
            data_addr = daddr
        else:                       # cnt, next, call, ret, end: data follows the tag
            data_addr = addr + 16
        pkt = {"index": n, "tag": addr, "id": name, "qwc": qwc, "addr": daddr, "spr": spr,
               "irq": irq, "pce": pce, "data": data_addr, "vif": [w2, w3]}
        packets.append(pkt)

        if spr and tid in (0, 3, 4):
            issues.append((WARN, "packet #%d reads from scratchpad %s (not in a RAM dump)"
                                 % (n, hx(daddr)), addr))
        if tte:
            segments.append((len(stream), 8, addr + 8, n))
            stream += raw[8:16]
        if qwc:
            payload = dump.read(data_addr, qwc * 16)
            if payload is None:
                issues.append((ERROR, "packet #%d (%s) moves %d qwords from %s, outside the dump"
                                      % (n, name, qwc, hx(data_addr)), addr))
                break
            segments.append((len(stream), qwc * 16, data_addr, n))
            stream += payload
        elif tid in (1, 2, 3, 4):
            issues.append((NOTE, "packet #%d (%s) has QWC=0: it moves no data" % (n, name), addr))

        if irq and tie:
            issues.append((NOTE, "packet #%d has IRQ set and TIE is on: the transfer stops here"
                                 % n, addr))
            break
        if tid == 1:                # cnt
            addr = data_addr + qwc * 16
        elif tid == 2:              # next
            addr = daddr
        elif tid in (3, 4):         # ref, refs
            addr = addr + 16
        elif tid == 5:              # call
            if len(stack) >= 2:
                issues.append((ERROR, "packet #%d is a third nested call: the DMAC only keeps two "
                                      "return addresses" % n, addr))
                break
            stack.append(data_addr + qwc * 16)
            addr = daddr
        elif tid == 6:              # ret
            if not stack:
                break               # ret with an empty stack ends the transfer
            addr = stack.pop()
        else:                       # refe, end
            break
    else:
        issues.append((WARN, "stopped after %d tags (--max-tags): the chain may not end" % max_tags,
                       addr))
    return packets, bytes(stream), segments, issues


def guest_of(segments, off):
    for start, length, gaddr, pkt in segments:
        if start <= off < start + length:
            return gaddr + (off - start), pkt
    return None, None


# --------------------------------------------------------------------------
# VIF1 stream
# --------------------------------------------------------------------------


def unpack_bytes(vn, vl, count, cl, wl):
    """Bytes an UNPACK reads, the way the runtime's VIF1 interpreter counts them."""
    comp_bits = {0: 32, 1: 16, 2: 8, 3: 4 if vn == 3 else 16}[vl]
    vec_bits = 16 if (vl == 3 and vn == 3) else (vn + 1) * comp_bits
    per_vec = (vec_bits + 7) // 8
    cl = cl or 1
    wl = wl or 1
    src = count
    if cl < wl:
        full, rem = divmod(count, wl)
        src = full * cl + min(rem, cl)
    return (src * per_vec + 3) & ~3


def decode_vif(stream, gif=True, where=None):
    """Decode a VIF1 stream. where(off) -> (guest addr, packet) for messages."""
    where = where or (lambda off: (off, None))
    cmds, issues, gif_tags = [], [], []
    stats = {}
    cycle = 0
    pos = 0
    size = len(stream)
    while pos + 4 <= size:
        start = pos
        code = struct.unpack_from("<I", stream, pos)[0]
        pos += 4
        op = (code >> 24) & 0x7F
        num = (code >> 16) & 0xFF
        imm = code & 0xFFFF
        irq = code >> 31
        gaddr, pkt = where(start)
        entry = {"off": start, "addr": gaddr, "packet": pkt, "code": code, "irq": irq}
        need = 0
        if op in VIF_NAMES:
            name = VIF_NAMES[op]
            entry["cmd"] = name
            if op == 0x01:
                cycle = imm
                entry["detail"] = "CL=%d WL=%d" % (imm & 0xFF, imm >> 8)
            elif op in (0x02, 0x03, 0x04):
                entry["detail"] = "%s=%d" % (name, imm & 0x3FF)
            elif op in (0x14, 0x15):
                entry["detail"] = "start VU1 microprogram at %s" % hx(imm * 8, 4)
            elif op == 0x20:
                need = 4
            elif op in (0x30, 0x31):
                need = 16
            elif op == 0x4A:
                count = num or 256
                need = count * 8
                entry["detail"] = "upload %d VU1 instructions to %s" % (count, hx(imm * 8, 4))
                if pos % 8:
                    issues.append((WARN, "MPG data at stream offset %d is not 64-bit aligned"
                                         % pos, gaddr))
            elif op in (0x50, 0x51):
                qw = imm or 65536
                need = qw * 16
                entry["detail"] = "%d qwords to the GIF (PATH2)" % qw
                if pos % 16:
                    issues.append((WARN, "%s data at stream offset %d is not 128-bit aligned: real "
                                         "hardware expects NOP padding before it, so the stream "
                                         "may be decoded from the wrong place" % (name, pos), gaddr))
        elif (op & 0x60) == 0x60:
            vn, vl = (op >> 2) & 3, op & 3
            fmt = UNPACK_FORMATS.get((vn, vl))
            if fmt is None:
                issues.append((ERROR, "UNPACK with an invalid format (vn=%d vl=%d) at stream offset "
                                      "%d" % (vn, vl, start), gaddr))
                entry["cmd"] = "UNPACK ?"
                cmds.append(entry)
                break
            count = num or 256
            need = unpack_bytes(vn, vl, count, cycle & 0xFF, cycle >> 8)
            name = "UNPACK"
            entry["cmd"] = name
            entry["detail"] = "%s x%d to VU1 %s%s%s%s" % (
                fmt, count, hx(imm & 0x3FF, 3), " +TOPS" if imm & 0x8000 else "",
                " unsigned" if imm & 0x4000 else "", " masked" if op & 0x10 else "")
        else:
            name = "UNKNOWN 0x%02X" % op
            entry["cmd"] = name
            cmds.append(entry)
            issues.append((ERROR, "VIF code %s at stream offset %d is not a command the VIF knows: "
                                  "the stream is corrupt, or decoding started in the wrong place"
                                  % (hx(code), start), gaddr))
            break
        stats[name] = stats.get(name, 0) + 1
        if pos + need > size:
            issues.append((ERROR, "%s at stream offset %d needs %d bytes but only %d are left: the "
                                  "stream ends in the middle of a command" % (name, start, need,
                                                                             size - pos), gaddr))
            entry["truncated"] = True
            cmds.append(entry)
            break
        if name in ("DIRECT", "DIRECTHL") and gif:
            tags, gissues = decode_gif(stream[pos:pos + need], where=lambda o, b=pos: where(b + o),
                                       inside_direct=True)
            gif_tags.extend(tags)
            issues.extend(gissues)
        pos += need
        cmds.append(entry)
    if 0 < size - pos < 4:
        issues.append((WARN, "%d stray bytes at the end of the stream" % (size - pos), None))
    return cmds, stats, gif_tags, issues


# --------------------------------------------------------------------------
# GIF packets
# --------------------------------------------------------------------------


def reg_detail(reg, v):
    """Fields of the GS registers that explain most black screens and broken textures."""
    if reg in (0x06, 0x07):     # TEX0
        return "TBP0=%s TBW=%d %s %dx%d CBP=%s CPSM=%s CSM=%d" % (
            hx(bits(v, 0, 14), 4), bits(v, 14, 6), psm_name(bits(v, 20, 6)), 1 << bits(v, 26, 4),
            1 << bits(v, 30, 4), hx(bits(v, 37, 14), 4), psm_name(bits(v, 51, 4)), bits(v, 55, 1))
    if reg in (0x4C, 0x4D):     # FRAME
        return "FBP=%s FBW=%d %s FBMSK=%s" % (hx(bits(v, 0, 9), 3), bits(v, 16, 6),
                                             psm_name(bits(v, 24, 6)), hx(bits(v, 32, 32)))
    if reg in (0x4E, 0x4F):     # ZBUF
        return "ZBP=%s %s ZMSK=%d" % (hx(bits(v, 0, 9), 3), psm_name(0x30 | bits(v, 24, 4)),
                                      bits(v, 32, 1))
    if reg == 0x50:             # BITBLTBUF
        return "SBP=%s SBW=%d %s -> DBP=%s DBW=%d %s" % (
            hx(bits(v, 0, 14), 4), bits(v, 16, 6), psm_name(bits(v, 24, 6)),
            hx(bits(v, 32, 14), 4), bits(v, 48, 6), psm_name(bits(v, 56, 6)))
    if reg == 0x51:             # TRXPOS
        return "src %d,%d dst %d,%d" % (bits(v, 0, 11), bits(v, 16, 11), bits(v, 32, 11),
                                        bits(v, 48, 11))
    if reg == 0x52:             # TRXREG
        return "%dx%d" % (bits(v, 0, 12), bits(v, 32, 12))
    if reg == 0x53:             # TRXDIR
        return {0: "host -> GS (upload)", 1: "GS -> host (download)", 2: "GS -> GS (copy)",
                3: "off"}[bits(v, 0, 2)]
    if reg == 0x00:             # PRIM
        return "%s%s%s%s" % (PRIM_TYPES[v & 7], " textured" if v & 0x10 else "",
                             " blended" if v & 0x40 else "", " context 2" if v & 0x200 else "")
    if reg in (0x40, 0x41):     # SCISSOR
        return "x %d..%d y %d..%d" % (bits(v, 0, 11), bits(v, 16, 11), bits(v, 32, 11),
                                      bits(v, 48, 11))
    return ""


class GifState(object):
    """What the packets have set so far: used to check image uploads."""

    def __init__(self):
        self.bitblt = None
        self.trxreg = None
        self.expect = None          # bytes still expected by the current host -> GS upload
        self.expect_from = None
        self.uploads = []
        self.prims = {}


def _write(state, reg, value, gaddr, regs_out, issues):
    name = GS_REGS.get(reg, "reg 0x%02X?" % reg)
    regs_out.append({"reg": name, "value": value, "detail": reg_detail(reg, value)})
    if reg not in GS_REGS:
        issues.append((WARN, "write to an unknown GS register 0x%02X" % reg, gaddr))
    if reg == 0x00:
        p = value & 7
        state.prims[PRIM_TYPES[p]] = state.prims.get(PRIM_TYPES[p], 0) + 1
        if p == 7:
            issues.append((ERROR, "PRIM type 7 is prohibited on the GS", gaddr))
    elif reg == 0x50:
        state.bitblt = value
    elif reg == 0x52:
        state.trxreg = value
    elif reg == 0x53:
        _close_upload(state, issues)
        if bits(value, 0, 2) == 0 and state.bitblt is not None and state.trxreg is not None:
            w, h = bits(state.trxreg, 0, 12), bits(state.trxreg, 32, 12)
            dpsm = bits(state.bitblt, 56, 6)
            bpp = PSM.get(dpsm, ("", 0))[1]
            if bpp:
                state.expect = (w * h * bpp + 7) // 8
                state.expect_from = gaddr
                state.uploads.append({"dbp": bits(state.bitblt, 32, 14), "psm": psm_name(dpsm),
                                      "width": w, "height": h, "bytes": state.expect})


def _close_upload(state, issues):
    if state.expect:
        issues.append((WARN, "image upload started at %s is %d bytes short of its TRXREG size: the "
                             "texture will be incomplete" % (
                                 hx(state.expect_from) if state.expect_from is not None else "?",
                                 state.expect), state.expect_from))
    state.expect = None


def decode_gif(buf, where=None, inside_direct=False, state=None):
    where = where or (lambda off: (off, None))
    state = state or GifState()
    tags, issues = [], []
    pos = 0
    size = len(buf)
    while pos + 16 <= size:
        lo, hi = struct.unpack_from("<QQ", buf, pos)
        gaddr, pkt = where(pos)
        nloop = lo & 0x7FFF
        eop = bits(lo, 15, 1)
        pre = bits(lo, 46, 1)
        prim = bits(lo, 47, 11)
        flg = bits(lo, 58, 2)
        nreg = bits(lo, 60, 4) or 16
        descs = [(hi >> (4 * i)) & 0xF for i in range(nreg)]
        tag = {"off": pos, "addr": gaddr, "packet": pkt, "nloop": nloop, "eop": eop,
               "flg": GIF_FLG[flg], "nreg": nreg, "regs": [GIF_DESC[d] for d in descs],
               "writes": []}
        pos += 16
        if pre and flg != 2:
            _write(state, 0x00, prim, gaddr, tag["writes"], issues)
        if flg == 0:
            need = nloop * nreg * 16
        elif flg == 1:
            need = ((nloop * nreg * 8) + 15) & ~15
        else:
            need = nloop * 16
            if flg == 3:
                issues.append((WARN, "GIF tag with FLG=3 (disabled): treated as IMAGE", gaddr))
        if pos + need > size:
            tag["truncated"] = True
            tags.append(tag)
            where_txt = "its DIRECT block" if inside_direct else "the buffer"
            issues.append(((NOTE if inside_direct else ERROR),
                           "%s GIF packet needs %d bytes but %s has %d left%s" % (
                               GIF_FLG[flg], need, where_txt, size - pos,
                               " (it may continue in the next DIRECT)" if inside_direct else ""),
                           gaddr))
            break
        if flg == 0:
            for i in range(nloop * nreg):
                d = descs[i % nreg]
                qlo, qhi = struct.unpack_from("<QQ", buf, pos + i * 16)
                if d == 0xE:
                    _write(state, qhi & 0xFF, qlo, gaddr, tag["writes"], issues)
                elif d == 0x0:
                    _write(state, 0x00, qlo & 0x7FF, gaddr, tag["writes"], issues)
                elif d in (0x6, 0x7, 0x8, 0x9):
                    _write(state, d, qlo, gaddr, tag["writes"], issues)
        elif flg == 1:
            for i in range(nloop * nreg):
                d = descs[i % nreg]
                word = struct.unpack_from("<Q", buf, pos + i * 8)[0]
                if d == 0x0:
                    _write(state, 0x00, word & 0x7FF, gaddr, tag["writes"], issues)
                elif d in (0x6, 0x7, 0x8, 0x9):
                    _write(state, d, word, gaddr, tag["writes"], issues)
        else:
            if state.expect is None:
                issues.append((WARN, "IMAGE data with no upload set up before it (BITBLTBUF, "
                                     "TRXREG, then TRXDIR=0)", gaddr))
            else:
                state.expect -= need
                if state.expect < 0:
                    issues.append((WARN, "IMAGE data overruns the upload set up at %s by %d bytes: "
                                         "the wrong TRXREG or pixel format" % (
                                             hx(state.expect_from) if state.expect_from is not None
                                             else "?", -state.expect), gaddr))
                    state.expect = None
                elif state.expect == 0:
                    state.expect = None
        tag["bytes"] = need
        pos += need
        tags.append(tag)
    if not inside_direct:
        _close_upload(state, issues)
        if 0 < size - pos:
            issues.append((WARN, "%d bytes after the last whole GIF packet" % (size - pos), None))
        if tags and not tags[-1]["eop"]:
            issues.append((NOTE, "the last GIF tag has EOP=0: the packet continues past this buffer",
                           tags[-1]["addr"]))
    tags_state = tags
    return tags_state, issues


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def addr_txt(a):
    return hx(a) if isinstance(a, int) else "?"


def print_issues(issues):
    order = {ERROR: 0, WARN: 1, NOTE: 2}
    if not issues:
        print("\nissues: none")
        return
    print("\nissues (%d):" % len(issues))
    for sev, msg, a in sorted(issues, key=lambda i: order[i[0]]):
        print("  %-7s %s%s" % (sev, msg, "" if a is None else "  [%s]" % addr_txt(a)))


def print_gif(tags, limit, state):
    shown = 0
    for t in tags:
        if shown >= limit:
            print("  ... %d more GIF tags (raise --max-lines)" % (len(tags) - shown))
            break
        print("  GIF %s nloop=%d nreg=%d eop=%d regs=%s%s" % (
            t["flg"], t["nloop"], t["nreg"], t["eop"], ",".join(t["regs"]),
            "  @%s" % addr_txt(t["addr"])))
        shown += 1
        for w in t["writes"]:
            if shown >= limit:
                break
            print("      %-10s %s  %s" % (w["reg"], hx(w["value"], 16), w["detail"]))
            shown += 1
    if state.prims:
        print("\nprimitives set: " + ", ".join("%s x%d" % kv for kv in sorted(state.prims.items())))
    if state.uploads:
        print("image uploads: " + "; ".join("%dx%d %s to DBP %s (%d bytes)" % (
            u["width"], u["height"], u["psm"], hx(u["dbp"], 4), u["bytes"]) for u in state.uploads))


def exit_code(issues):
    return 1 if any(i[0] == ERROR for i in issues) else 0


def jsonable_issues(issues):
    return [{"severity": s, "message": m, "addr": a} for s, m, a in issues]


def parse_int(text):
    try:
        return int(text, 0)
    except ValueError:
        raise argparse.ArgumentTypeError("not a number: %s (use 0x... for hex)" % text)


def region(dump, args):
    """The bytes a vif/gif command reads: --offset (file) or --addr (guest), then --length."""
    if args.addr is not None:
        off = dump.offset(args.addr)
        if off is None:
            fail("%s is not inside the dump (base %s, %d bytes)" % (hx(args.addr), hx(dump.base),
                                                                    len(dump.data)))
    else:
        off = args.offset or 0
        if off >= len(dump.data):
            fail("offset %d is past the end of the dump (%d bytes)" % (off, len(dump.data)))
    end = len(dump.data) if args.length is None else min(len(dump.data), off + args.length)
    return off, dump.data[off:end]


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_dma(args):
    dump = Dump.load(args.dump, args.base)
    tte = args.channel == "vif1" and not args.no_tte
    packets, stream, segments, issues = walk_dma(dump, args.tag, tte=tte, tie=args.tie,
                                                 max_tags=args.max_tags)
    state = GifState()
    if args.channel == "vif1":
        cmds, stats, gif_tags, vissues = decode_vif(stream, gif=not args.no_gif,
                                                    where=lambda o: guest_of(segments, o))
    else:
        cmds, stats = [], {}
        gif_tags, vissues = decode_gif(stream, where=lambda o: guest_of(segments, o), state=state)
    issues = issues + vissues
    if args.json:
        print(json.dumps({"packets": packets, "stream_bytes": len(stream), "vif": cmds,
                          "vif_counts": stats, "gif_tags": gif_tags,
                          "issues": jsonable_issues(issues)}, indent=1))
        return exit_code(issues)
    print("DMA chain from %s (%s channel, TTE %s): %d packets, %d bytes of data" % (
        hx(args.tag), args.channel.upper(), "on" if tte else "off", len(packets), len(stream)))
    for p in packets[:args.max_lines]:
        extra = ""
        if tte:
            extra = "  tag VIF: %s, %s" % (_vif_name(p["vif"][0]), _vif_name(p["vif"][1]))
        print("  #%-4d %s %-4s qwc=%-5d data@%s%s%s" % (
            p["index"], hx(p["tag"]), p["id"], p["qwc"], hx(p["data"]),
            "  irq" if p["irq"] else "", extra))
    if len(packets) > args.max_lines:
        print("  ... %d more packets" % (len(packets) - args.max_lines))
    if args.channel == "vif1":
        _print_vif(cmds, stats, gif_tags, args)
    else:
        print("\nGIF packets: %d" % len(gif_tags))
        print_gif(gif_tags, args.max_lines, state)
    print_issues(issues)
    return exit_code(issues)


def _vif_name(code):
    op = (code >> 24) & 0x7F
    if op in VIF_NAMES:
        return VIF_NAMES[op]
    if (op & 0x60) == 0x60:
        return "UNPACK"
    return "UNKNOWN 0x%02X" % op


def _print_vif(cmds, stats, gif_tags, args):
    print("\nVIF1 stream: %d commands (%s)" % (len(cmds), ", ".join(
        "%s x%d" % kv for kv in sorted(stats.items(), key=lambda kv: -kv[1]))))
    for c in cmds[:args.max_lines]:
        print("  %s %-9s%s%s" % (addr_txt(c["addr"]), c["cmd"],
                                 "  " + c["detail"] if c.get("detail") else "",
                                 "  (interrupt)" if c["irq"] else ""))
    if len(cmds) > args.max_lines:
        print("  ... %d more VIF commands" % (len(cmds) - args.max_lines))
    if gif_tags:
        state = GifState()
        for t in gif_tags:
            for w in t["writes"]:
                if w["reg"] == "PRIM":
                    name = PRIM_TYPES[w["value"] & 7]
                    state.prims[name] = state.prims.get(name, 0) + 1
        print("\nGIF tags inside DIRECT blocks: %d" % len(gif_tags))
        if args.verbose:
            print_gif(gif_tags, args.max_lines, state)
        elif state.prims:
            print("primitives set: " + ", ".join("%s x%d" % kv for kv in sorted(state.prims.items())))


def cmd_vif(args):
    dump = Dump.load(args.dump, args.base)
    off, data = region(dump, args)
    cmds, stats, gif_tags, issues = decode_vif(
        data, gif=not args.no_gif, where=lambda o: (dump.addr_of(off + o), None))
    if args.json:
        print(json.dumps({"vif": cmds, "vif_counts": stats, "gif_tags": gif_tags,
                          "issues": jsonable_issues(issues)}, indent=1))
        return exit_code(issues)
    _print_vif(cmds, stats, gif_tags, args)
    print_issues(issues)
    return exit_code(issues)


def cmd_gif(args):
    dump = Dump.load(args.dump, args.base)
    off, data = region(dump, args)
    state = GifState()
    tags, issues = decode_gif(data, where=lambda o: (dump.addr_of(off + o), None), state=state)
    if args.json:
        print(json.dumps({"gif_tags": tags, "prims": state.prims, "uploads": state.uploads,
                          "issues": jsonable_issues(issues)}, indent=1))
        return exit_code(issues)
    print("GIF packets from %s: %d tags" % (hx(dump.addr_of(off)), len(tags)))
    print_gif(tags, args.max_lines, state)
    print_issues(issues)
    return exit_code(issues)


def build_parser():
    p = argparse.ArgumentParser(
        prog=TOOL, formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Walk DMA chains, VIF1 streams and GIF packets in a PS2 RAM dump, and flag "
                    "what breaks rendering.",
        epilog="examples:\n"
               "  gspacket.py dma ram.bin --tag 0x01F2A000                  # VIF1 chain, TTE on\n"
               "  gspacket.py dma ram.bin --tag 0x00402000 --channel gif\n"
               "  gspacket.py vif ram.bin --addr 0x00403C10 --length 4096\n"
               "  gspacket.py gif packet.bin --json\n"
               "exit codes: 0 no errors, 1 errors found, 2 bad input")
    sub = p.add_subparsers(dest="cmd")

    def common(sp, region_args=True):
        sp.add_argument("dump", help="raw guest memory dump (a full 32 MB RDRAM dump needs no --base)")
        sp.add_argument("--base", type=parse_int, default=0,
                        help="guest address of the dump's first byte (default 0)")
        if region_args:
            sp.add_argument("--offset", type=parse_int, help="start at this file offset")
            sp.add_argument("--addr", type=parse_int, help="start at this guest address")
            sp.add_argument("--length", type=parse_int, help="bytes to read (default: to the end)")
        sp.add_argument("--max-lines", type=int, default=400, help="cap on listed items (default 400)")
        sp.add_argument("--json", action="store_true", help="machine-readable output")

    d = sub.add_parser("dma", help="walk a DMA source chain from its first tag")
    common(d, region_args=False)
    d.add_argument("--tag", type=parse_int, required=True, help="guest address of the first DMA tag")
    d.add_argument("--channel", choices=("vif1", "gif"), default="vif1",
                   help="vif1 (D1, the usual 3D path) or gif (D2, PATH3)")
    d.add_argument("--no-tte", action="store_true",
                   help="VIF1: the tag's upper 64 bits are NOT sent (Dn_CHCR.TTE=0)")
    d.add_argument("--tie", action="store_true", help="stop at a tag with IRQ set (Dn_CHCR.TIE=1)")
    d.add_argument("--max-tags", type=int, default=10000, help="give up after this many tags")
    d.add_argument("--no-gif", action="store_true", help="do not decode GIF packets inside DIRECT")
    d.add_argument("--verbose", action="store_true", help="list every GIF tag and register write")
    d.set_defaults(func=cmd_dma)

    v = sub.add_parser("vif", help="decode a VIF1 stream")
    common(v)
    v.add_argument("--no-gif", action="store_true", help="do not decode GIF packets inside DIRECT")
    v.add_argument("--verbose", action="store_true", help="list every GIF tag and register write")
    v.set_defaults(func=cmd_vif)

    g = sub.add_parser("gif", help="decode GIF packets (PATH2/PATH3 data)")
    common(g)
    g.set_defaults(func=cmd_gif)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except UsageError as e:
        sys.stderr.write("%s: %s\n" % (TOOL, e))
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
