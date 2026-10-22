import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest

from _load import load, ROOT

gp = load("console-graphics", "gspacket")
TOOL = os.path.join(ROOT, "console-graphics", "gspacket.py")

# --- builders: every packet here is synthetic -------------------------------------------------


def vifcode(op, num=0, imm=0, irq=0):
    return struct.pack("<I", (irq << 31) | (op << 24) | (num << 16) | imm)


def giftag(nloop, flg, regs, eop=1, pre=0, prim=0):
    nreg = len(regs) % 16
    lo = nloop | (eop << 15) | (pre << 46) | (prim << 47) | (flg << 58) | (nreg << 60)
    hi = 0
    for i, r in enumerate(regs):
        hi |= r << (4 * i)
    return struct.pack("<QQ", lo, hi)


def ad(reg, value):
    return struct.pack("<QQ", value, reg)


def dmatag(qwc, tid, addr=0, vif0=0, vif1=0, irq=0):
    return struct.pack("<IIII", qwc | (tid << 28) | (irq << 31), addr, vif0, vif1)


def bitbltbuf(dbp, dbw, dpsm):
    return (dbp << 32) | (dbw << 48) | (dpsm << 56)


def trxreg(w, h):
    return w | (h << 32)


def upload_32(w, h, image_qw=None, eop=1):
    """A complete host -> GS upload of a w x h PSMCT32 image."""
    setup = giftag(4, 0, [0xE], eop=0)
    setup += ad(0x50, bitbltbuf(0x2000, 1, 0x00))
    setup += ad(0x51, 0)
    setup += ad(0x52, trxreg(w, h))
    setup += ad(0x53, 0)
    qw = (w * h * 4) // 16 if image_qw is None else image_qw
    return setup + giftag(qw, 2, [], eop=eop) + b"\x11" * (qw * 16)


def draw_sprite(prim_type=6, eop=1):
    return giftag(1, 0, [0x0, 0x1, 0x5, 0x5], eop=eop) + \
        struct.pack("<QQ", prim_type, 0) + struct.pack("<QQ", 0x80808080, 0) + \
        struct.pack("<QQ", 0, 0) + struct.pack("<QQ", (100 << 4) | ((50 << 4) << 32), 0)


def pad_to(buf, size):
    return buf + b"\x00" * (size - len(buf))


class Tables(unittest.TestCase):
    def test_vif_opcodes_match_the_runtime(self):
        # the base skill's script had these two swapped
        self.assertEqual(gp.VIF_NAMES[0x15], "MSCALF")
        self.assertEqual(gp.VIF_NAMES[0x17], "MSCNT")
        self.assertEqual(gp.VIF_NAMES[0x4A], "MPG")
        self.assertEqual(gp.VIF_NAMES[0x50], "DIRECT")

    def test_gif_descriptors(self):
        self.assertEqual(gp.GIF_DESC[0xE], "A+D")
        self.assertEqual(gp.GIF_DESC[0xF], "NOP")
        self.assertEqual(gp.GS_REGS[0x3F], "TEXFLUSH")
        self.assertEqual(gp.GS_REGS[0x50], "BITBLTBUF")

    def test_unpack_sizes(self):
        self.assertEqual(gp.unpack_bytes(3, 0, 2, 1, 1), 32)      # V4-32 x2
        self.assertEqual(gp.unpack_bytes(3, 3, 4, 1, 1), 8)       # V4-5 x4: 16 bits each
        self.assertEqual(gp.unpack_bytes(2, 2, 3, 1, 1), 12)      # V3-8 x3: 9 bytes, word padded
        self.assertEqual(gp.unpack_bytes(0, 1, 1, 1, 1), 4)       # S-16 x1: 2 bytes, word padded
        self.assertEqual(gp.unpack_bytes(3, 0, 8, 1, 4), 32)      # CL=1 WL=4: 2 source vectors
        self.assertEqual(gp.unpack_bytes(3, 0, 256, 0, 0), 4096)  # NUM=0 is 256

    def test_physical_addresses(self):
        P = gp.Dump.physical
        self.assertEqual(P(0x00123450), 0x00123450)
        self.assertEqual(P(0x80123450), 0x00123450)
        self.assertEqual(P(0xA0123450), 0x00123450)
        self.assertEqual(P(0x20123450), 0x00123450)
        self.assertEqual(P(0x30123450), 0x00123450)

    def test_register_details(self):
        tex0 = (0x1000) | (4 << 14) | (0x13 << 20) | (8 << 26) | (7 << 30) | (0x3000 << 37)
        txt = gp.reg_detail(0x06, tex0)
        self.assertIn("PSMT8", txt)
        self.assertIn("256x128", txt)
        self.assertIn("CBP=0x3000", txt)
        self.assertIn("sprite", gp.reg_detail(0x00, 6))
        self.assertIn("upload", gp.reg_detail(0x53, 0))


class Gif(unittest.TestCase):
    def decode(self, buf):
        state = gp.GifState()
        tags, issues = gp.decode_gif(buf, state=state)
        return tags, issues, state

    def test_clean_upload_and_draw(self):
        tags, issues, state = self.decode(upload_32(8, 8) + draw_sprite())
        self.assertEqual(issues, [])
        self.assertEqual(len(tags), 3)
        self.assertEqual(state.uploads, [{"dbp": 0x2000, "psm": "PSMCT32", "width": 8, "height": 8,
                                          "bytes": 256}])
        self.assertEqual(state.prims, {"sprite": 1})
        self.assertEqual(tags[1]["flg"], "IMAGE")
        self.assertEqual(tags[1]["bytes"], 256)

    def test_short_upload_is_flagged(self):
        _, issues, _ = self.decode(upload_32(8, 8, image_qw=8))
        self.assertTrue(any(s == gp.WARN and "short" in m for s, m, a in issues), issues)

    def test_overrun_upload_is_flagged(self):
        _, issues, _ = self.decode(upload_32(8, 8, image_qw=20))
        self.assertTrue(any("overruns" in m for s, m, a in issues), issues)

    def test_image_without_setup(self):
        _, issues, _ = self.decode(giftag(2, 2, []) + b"\x00" * 32)
        self.assertTrue(any("no upload set up" in m for s, m, a in issues), issues)

    def test_prohibited_prim_is_an_error(self):
        _, issues, _ = self.decode(draw_sprite(prim_type=7))
        self.assertTrue(any(s == gp.ERROR and "prohibited" in m for s, m, a in issues), issues)
        self.assertEqual(gp.exit_code(issues), 1)

    def test_pre_bit_sets_prim(self):
        buf = giftag(1, 0, [0x1], pre=1, prim=3) + struct.pack("<QQ", 0, 0)
        _, issues, state = self.decode(buf)
        self.assertEqual(state.prims, {"triangle": 1})

    def test_reglist_payload_is_padded(self):
        buf = giftag(3, 1, [0x1]) + b"\x00" * 32          # 3 x 8 bytes, padded to 32
        tags, issues, _ = self.decode(buf)
        self.assertEqual(tags[0]["bytes"], 32)
        self.assertEqual(issues, [])

    def test_nreg_zero_means_sixteen(self):
        buf = giftag(1, 0, [0xF] * 16) + b"\x00" * 256
        tags, issues, _ = self.decode(buf)
        self.assertEqual(tags[0]["nreg"], 16)
        self.assertEqual(tags[0]["bytes"], 256)

    def test_truncated_packet_is_an_error(self):
        buf = giftag(4, 0, [0x1]) + b"\x00" * 32
        tags, issues, _ = self.decode(buf)
        self.assertTrue(tags[0]["truncated"])
        self.assertEqual(gp.exit_code(issues), 1)

    def test_missing_eop_is_a_note(self):
        _, issues, _ = self.decode(draw_sprite(eop=0))
        self.assertEqual([s for s, m, a in issues], [gp.NOTE])

    def test_unknown_register(self):
        buf = giftag(1, 0, [0xE]) + ad(0x7E, 1)
        _, issues, _ = self.decode(buf)
        self.assertTrue(any("unknown GS register" in m for s, m, a in issues))


class Vif(unittest.TestCase):
    def stream(self):
        s = vifcode(0x01, imm=0x0101)                    # STCYCL CL=1 WL=1
        s += vifcode(0x6C, num=2, imm=0x0010)            # UNPACK V4-32 x2
        s += b"\x00" * 32
        s += vifcode(0x00) + vifcode(0x4A, num=1, imm=0)  # NOP, MPG 1 instruction (8-aligned)
        s += b"\x00" * 8
        s += vifcode(0x14, imm=0x10)                     # MSCAL
        s += vifcode(0x00) * 4                           # pad so DIRECT data starts at 80
        gif = draw_sprite()
        s += vifcode(0x50, imm=len(gif) // 16)
        s += gif
        return s

    def test_clean_stream(self):
        cmds, stats, gif_tags, issues = gp.decode_vif(self.stream())
        self.assertEqual(issues, [])
        self.assertEqual([c["cmd"] for c in cmds],
                         ["STCYCL", "UNPACK", "NOP", "MPG", "MSCAL", "NOP", "NOP", "NOP", "NOP", "DIRECT"])
        self.assertEqual(stats["NOP"], 5)
        self.assertEqual(len(gif_tags), 1)
        self.assertIn("V4-32 x2", cmds[1]["detail"])
        self.assertIn("0x0080", cmds[4]["detail"])

    def test_unknown_code_stops_with_an_error(self):
        cmds, _, _, issues = gp.decode_vif(vifcode(0x00) + vifcode(0x08) + vifcode(0x00))
        self.assertEqual(cmds[-1]["cmd"], "UNKNOWN 0x08")
        self.assertEqual(gp.exit_code(issues), 1)

    def test_truncated_unpack(self):
        cmds, _, _, issues = gp.decode_vif(vifcode(0x6C, num=4) + b"\x00" * 16)
        self.assertTrue(cmds[-1].get("truncated"))
        self.assertTrue(any("ends in the middle" in m for s, m, a in issues))

    def test_misaligned_direct_is_a_warning(self):
        s = vifcode(0x50, imm=1) + giftag(0, 0, [0x1])
        _, _, _, issues = gp.decode_vif(s)
        self.assertTrue(any(s_ == gp.WARN and "128-bit" in m for s_, m, a in issues), issues)

    def test_unpack_respects_stcycl(self):
        s = vifcode(0x01, imm=0x0401) + vifcode(0x6C, num=8) + b"\x00" * 32 + vifcode(0x00)
        cmds, _, _, issues = gp.decode_vif(s)
        self.assertEqual(issues, [])
        self.assertEqual(cmds[-1]["cmd"], "NOP")

    def test_stmask_strow_take_their_words(self):
        s = vifcode(0x20) + b"\x01\x02\x03\x04" + vifcode(0x30) + b"\x00" * 16 + vifcode(0x07, imm=5)
        cmds, _, _, issues = gp.decode_vif(s)
        self.assertEqual([c["cmd"] for c in cmds], ["STMASK", "STROW", "MARK"])
        self.assertEqual(issues, [])


class Dma(unittest.TestCase):
    def chain_dump(self):
        """cnt -> next -> ref -> call(sub: ret) -> end, all inside a 64 KB dump at 0x00100000."""
        base = 0x00100000
        mem = bytearray(0x10000)

        def put(addr, data):
            mem[addr - base:addr - base + len(data)] = data

        nop = struct.unpack("<I", vifcode(0x00))[0]
        mark = lambda n: struct.unpack("<I", vifcode(0x07, imm=n))[0]  # noqa: E731
        put(0x00100000, dmatag(1, 1, 0, nop, mark(1)))                 # cnt, 1 qw follows
        put(0x00100010, vifcode(0x07, imm=2) * 4)
        put(0x00100020, dmatag(0, 2, 0x00100400, nop, mark(3)))        # next -> 0x400
        put(0x00100400, dmatag(1, 3, 0x00100800, nop, nop))            # ref data at 0x800
        put(0x00100800, vifcode(0x07, imm=4) * 4)
        put(0x00100410, dmatag(0, 5, 0x00100C00, nop, nop))            # call 0xC00
        put(0x00100C00, dmatag(1, 6, 0, nop, nop))                     # ret, 1 qw follows
        put(0x00100C10, vifcode(0x07, imm=5) * 4)
        put(0x00100420, dmatag(0, 7, 0, nop, mark(6)))                 # end
        return gp.Dump(bytes(mem), base)

    def test_walks_every_tag_kind(self):
        dump = self.chain_dump()
        packets, stream, segments, issues = gp.walk_dma(dump, 0x00100000)
        self.assertEqual([p["id"] for p in packets], ["cnt", "next", "ref", "call", "ret", "end"])
        self.assertEqual(packets[2]["data"], 0x00100800)
        self.assertEqual([i for i in issues if i[0] != gp.NOTE], [])
        cmds, _, _, vissues = gp.decode_vif(stream, where=lambda o: gp.guest_of(segments, o))
        marks = [struct.unpack("<I", struct.pack("<I", c["code"]))[0] & 0xFFFF
                 for c in cmds if c["cmd"] == "MARK"]
        self.assertEqual(marks, [1, 2, 2, 2, 2, 3, 4, 4, 4, 4, 5, 5, 5, 5, 6])
        self.assertEqual(vissues, [])
        first_ref_mark = [c for c in cmds if c["cmd"] == "MARK"][6]
        self.assertEqual(first_ref_mark["addr"], 0x00100800)

    def test_without_tte_the_tag_words_are_skipped(self):
        packets, stream, _, _ = gp.walk_dma(self.chain_dump(), 0x00100000, tte=False)
        self.assertEqual(len(stream), 16 * 3)

    def test_loop_is_an_error(self):
        mem = pad_to(dmatag(0, 2, 0x0), 64)
        _, _, _, issues = gp.walk_dma(gp.Dump(mem, 0), 0)
        self.assertTrue(any(s == gp.ERROR and "loops" in m for s, m, a in issues))

    def test_off_the_end_is_an_error(self):
        mem = pad_to(dmatag(0, 2, 0x00200000), 64)
        _, _, _, issues = gp.walk_dma(gp.Dump(mem, 0), 0)
        self.assertTrue(any("outside the dump" in m for s, m, a in issues))

    def test_third_nested_call_is_an_error(self):
        mem = bytearray(256)
        mem[0:16] = dmatag(0, 5, 0x40)
        mem[0x40:0x50] = dmatag(0, 5, 0x80)
        mem[0x80:0x90] = dmatag(0, 5, 0xC0)
        mem[0xC0:0xD0] = dmatag(0, 7)
        _, _, _, issues = gp.walk_dma(gp.Dump(bytes(mem), 0), 0)
        self.assertTrue(any("third nested call" in m for s, m, a in issues))

    def test_tie_stops_at_irq(self):
        mem = bytearray(128)
        mem[0:16] = dmatag(0, 2, 0x40, irq=1)
        mem[0x40:0x50] = dmatag(0, 7)
        packets, _, _, _ = gp.walk_dma(gp.Dump(bytes(mem), 0), 0, tie=True)
        self.assertEqual(len(packets), 1)
        packets, _, _, _ = gp.walk_dma(gp.Dump(bytes(mem), 0), 0, tie=False)
        self.assertEqual(len(packets), 2)

    def test_unaligned_tag(self):
        _, _, _, issues = gp.walk_dma(gp.Dump(b"\x00" * 64, 0), 4)
        self.assertTrue(any("aligned" in m for s, m, a in issues))

    def test_max_tags(self):
        mem = bytearray(16 * 40)
        for i in range(40):
            mem[i * 16:i * 16 + 16] = dmatag(0, 1)       # cnt with no data: the next tag follows
        _, _, _, issues = gp.walk_dma(gp.Dump(bytes(mem), 0), 0, max_tags=10)
        self.assertTrue(any("--max-tags" in m for s, m, a in issues))


class Cli(unittest.TestCase):
    def run_tool(self, *args):
        r = subprocess.run([sys.executable, TOOL] + list(args), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=30)
        return r.returncode, r.stdout.decode(), r.stderr.decode()

    def write(self, data):
        fd, path = tempfile.mkstemp(suffix=".bin")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        self.addCleanup(os.remove, path)
        return path

    def test_gif_command(self):
        path = self.write(upload_32(4, 4) + draw_sprite())
        code, out, err = self.run_tool("gif", path)
        self.assertEqual(code, 0, err)
        self.assertIn("image uploads: 4x4 PSMCT32 to DBP 0x2000 (64 bytes)", out)
        self.assertIn("issues: none", out)

    def test_gif_json_and_error_exit(self):
        path = self.write(draw_sprite(prim_type=7))
        code, out, _ = self.run_tool("gif", path, "--json")
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertEqual(data["issues"][0]["severity"], "error")

    def test_vif_command_with_addr_and_base(self):
        path = self.write(b"\x00" * 32 + Vif().stream())
        code, out, err = self.run_tool("vif", path, "--base", "0x00400000", "--addr", "0x00400020",
                                       "--verbose")
        self.assertEqual(code, 0, err)
        self.assertIn("UNPACK", out)
        self.assertIn("sprite", out)
        self.assertIn("0x00400020", out)

    def test_dma_command(self):
        dump = Dma().chain_dump()
        path = self.write(dump.data)
        code, out, err = self.run_tool("dma", path, "--base", "0x00100000", "--tag", "0x00100000")
        self.assertEqual(code, 0, err)
        self.assertIn("6 packets", out)
        self.assertIn("call", out)
        code, out, _ = self.run_tool("dma", path, "--base", "0x00100000", "--tag", "0x00100000",
                                     "--json")
        self.assertEqual(len(json.loads(out)["packets"]), 6)

    def test_dma_gif_channel(self):
        mem = bytearray(0x200)
        payload = upload_32(4, 4) + draw_sprite()
        qw = len(payload) // 16
        mem[0:16] = dmatag(qw, 7)
        mem[16:16 + len(payload)] = payload
        path = self.write(bytes(mem))
        code, out, err = self.run_tool("dma", path, "--tag", "0", "--channel", "gif")
        self.assertEqual(code, 0, err)
        self.assertIn("TTE off", out)
        self.assertIn("sprite", out)

    def test_bad_input_exits_2(self):
        code, _, err = self.run_tool("gif", os.path.join(ROOT, "no-such-dump.bin"))
        self.assertEqual(code, 2)
        self.assertIn("does not exist", err)
        path = self.write(b"\x00" * 64)
        code, _, err = self.run_tool("vif", path, "--addr", "0x00900000")
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)
        code, _, _ = self.run_tool()
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
