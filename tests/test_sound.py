"""console-sound tests: vag.py.

All audio is synthesized here (sine waves, chords, squares). WAVs are written
and read back with the stdlib wave module so the tool's own WAV code is
checked against an independent reader."""

import io
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT

vag = load("console-sound", "vag")

RATE = 22050


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = vag.main(argv)
    return code, out.getvalue(), err.getvalue()


def sine(n, freq=440.0, amp=12000, rate=RATE, phase=0.0):
    return [int(amp * math.sin(2 * math.pi * freq * i / rate + phase)) for i in range(n)]


def write_wav(path, chans, rate=RATE, width=2):
    with wave.open(path, "wb") as w:
        w.setnchannels(len(chans))
        w.setsampwidth(width)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(len(chans[0])):
            for c in chans:
                frames += struct.pack("<h", c[i]) if width == 2 else struct.pack("<B", (c[i] >> 8) + 128)
        w.writeframes(bytes(frames))


def read_wav(path):
    with wave.open(path, "rb") as w:
        ch, rate, n = w.getnchannels(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    vals = struct.unpack("<%dh" % (len(raw) // 2), raw)
    return rate, [list(vals[i::ch]) for i in range(ch)]


def snr(ref, test):
    return vag.snr_db(ref, test)


def ref_decode_frame(frame, s1=0, s2=0):
    """Straight-line reference of ps2_audio_vag.cpp for one frame (C division)."""
    filt_tab = [(0, 0), (60, 0), (115, -52), (98, -55), (122, -60)]
    shift = frame[0] & 0x0F
    filt = (frame[0] >> 4) & 7
    if shift > 12:
        shift = 9
    if filt > 4:
        filt = 0
    out = []
    for i in range(28):
        b = frame[2 + i // 2]
        nib = (b >> 4) if i & 1 else (b & 0x0F)
        if nib & 8:
            nib -= 16
        k1, k2 = filt_tab[filt]
        t = k1 * s1 + k2 * s2 + 32
        v = nib * (1 << (12 - shift)) + int(t / 64)  # int() truncates like C
        v = max(-32768, min(32767, v))
        s2, s1 = s1, v
        out.append(v)
    return out


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

    def bytes_of(self, *parts):
        with open(self.p(*parts), "rb") as fh:
            return fh.read()

    def make_vag(self, samples, name="v.vag", extra=(), rate=RATE):
        wav = self.p(name + ".wav")
        write_wav(wav, [samples], rate)
        code, out, err = run(["fromwav", wav, "--out", self.p(name)] + list(extra))
        self.assertEqual(code, 0, err)
        return self.p(name)


class RoundTrip(Base):
    def test_sine_round_trip_snr_above_25_db(self):
        x = sine(RATE // 2)
        path = self.make_vag(x)
        code, out, err = run(["towav", path, "--out", self.p("back.wav")])
        self.assertEqual(code, 0, err)
        rate, (y,) = read_wav(self.p("back.wav"))
        self.assertEqual(rate, RATE)
        # the first frame is the silent lead frame Sony tools write
        self.assertEqual(y[:28], [0] * 28)
        self.assertGreater(snr(x, y[28:28 + len(x)]), 25.0)

    def test_chord_round_trip(self):
        x = [a + b for a, b in zip(sine(8000, 220, 9000), sine(8000, 1375, 6000))]
        path = self.make_vag(x, extra=["--no-lead-frame"])
        _, out, _ = run(["towav", path, "--out", self.p("b.wav")])
        _, (y,) = read_wav(self.p("b.wav"))
        self.assertGreater(snr(x, y[:len(x)]), 25.0)

    def test_fromwav_reports_quality(self):
        x = sine(4000)
        wav = self.p("q.wav")
        write_wav(wav, [x])
        code, out, _ = run(["fromwav", wav, "--out", self.p("q.vag")])
        self.assertEqual(code, 0)
        db = float(out.split("quality: ")[1].split(" dB")[0])
        self.assertGreater(db, 25.0)

    def test_exhaustive_is_at_least_as_good(self):
        x = [a + b for a, b in zip(sine(4200, 300, 15000), sine(4200, 4100, 9000))]
        fast = vag.decode_adpcm(vag.encode_adpcm(x, lead_frame=False))[0]
        full = vag.decode_adpcm(vag.encode_adpcm(x, lead_frame=False, exhaustive=True))[0]
        self.assertGreaterEqual(snr(x, full[:len(x)]), snr(x, fast[:len(x)]) - 0.5)

    def test_full_scale_square_clamps_cleanly(self):
        x = [32767 if (i // 20) % 2 else -32768 for i in range(2800)]
        data = vag.encode_adpcm(x, lead_frame=False)
        y, info = vag.decode_adpcm(data)
        self.assertEqual(len(y), 2800)
        self.assertTrue(all(-32768 <= v <= 32767 for v in y))
        self.assertEqual(info["bad_frames"], 0)
        self.assertGreater(snr(x, y), 10.0)

    def test_silence_encodes_to_zero_frames(self):
        data = vag.encode_adpcm([0] * 56, lead_frame=False, end_marker=False)
        self.assertEqual(data[0], 0)
        self.assertEqual(data[2:16], bytes(14))

    def test_encode_block_stays_in_range(self):
        for x in (sine(28, 9000, 30000), [32767] * 28, [-32768, 32767] * 14, list(range(-14, 14))):
            hdr, nibs, s1, s2, err = vag.encode_block(x, 0, 0)
            self.assertLessEqual(hdr & 0x0F, 12)
            self.assertLessEqual(hdr >> 4, 4)
            self.assertTrue(all(0 <= n <= 15 for n in nibs))


class Decoder(Base):
    def test_matches_the_runtime_reference(self):
        frames = [bytes([0x1C, 0]) + bytes([0x11] * 14), bytes([0x2A, 0]) + bytes([0x9F, 0x71] * 7),
                  bytes([0x4C, 0]) + bytes([0xF8, 0x08] * 7), bytes([0x30, 0]) + bytes([0x77] * 14)]
        s1 = s2 = 0
        want = []
        for f in frames:
            got = ref_decode_frame(f, s1, s2)
            want += got
            s1, s2 = got[-1], got[-2]
        y, _ = vag.decode_adpcm(b"".join(frames))
        self.assertEqual(y, want)

    def test_c_division_truncates_toward_zero(self):
        # nibble -1 at step 1 with filter 1: (60 * -1 + 32) / 64 is 0 in C (floor would give -1)
        frame = bytes([0x1C, 0]) + bytes([0xFF] * 14)
        y, _ = vag.decode_adpcm(frame)
        self.assertEqual(y[:4], [-1, -1, -1, -1])

    def test_out_of_range_shift_and_filter_follow_the_runtime(self):
        odd = bytes([0x5D, 0]) + bytes([0x11] * 14)  # filter 5 -> 0, shift 13 -> 9
        y, info = vag.decode_adpcm(odd)
        self.assertEqual(y[:2], [1 << 3, 1 << 3])
        self.assertEqual(info["bad_frames"], 1)

    def test_end_flag_and_marker(self):
        one = bytes([0x0C, 0]) + bytes([0x11] * 14)
        end = bytes([0x0C, 1]) + bytes([0x11] * 14)
        marker = bytes([0x00, 7]) + bytes([0x77] * 14)
        y, info = vag.decode_adpcm(one + end + one)
        self.assertEqual((len(y), info["end"]), (56, "end-flag"))
        y, info = vag.decode_adpcm(one + marker + one)
        self.assertEqual((len(y), info["end"]), (28, "marker"))
        y, info = vag.decode_adpcm(one + end + one, stop_at_end=False)
        self.assertEqual(len(y), 84)


class Header(Base):
    def test_vagp_header_fields(self):
        path = self.make_vag(sine(1000), name="JUMP_SFX.vag")
        blob = self.bytes_of("JUMP_SFX.vag")
        self.assertEqual(blob[:4], b"VAGp")
        self.assertEqual(struct.unpack(">I", blob[4:8])[0], 0x20)
        self.assertEqual(struct.unpack(">I", blob[0x0C:0x10])[0], len(blob) - 0x30)
        self.assertEqual(struct.unpack(">I", blob[0x10:0x14])[0], RATE)
        self.assertEqual(blob[0x20:0x30].rstrip(b"\0"), b"JUMP_SFX")
        self.assertEqual(len(blob) % 16, 0)

    def test_one_shot_flags(self):
        path = self.make_vag(sine(1000))
        data = self.bytes_of("v.vag")[0x30:]
        frames = [data[i:i + 16] for i in range(0, len(data), 16)]
        self.assertEqual(frames[0], bytes(16))                       # silent lead frame
        self.assertTrue(all(f[1] == 0 for f in frames[1:-2]))
        self.assertEqual(frames[-2][1], 0x01)                        # end
        self.assertEqual(frames[-1], bytes([0, 7]) + bytes(14))      # end-of-data marker, silent

    def test_loop_flags_and_info(self):
        path = self.make_vag(sine(1000), extra=["--loop"])
        data = self.bytes_of("v.vag")[0x30:]
        flags = [data[i + 1] for i in range(0, len(data), 16)]
        self.assertEqual(flags[0], 0)
        self.assertEqual(flags[1], 0x06)
        self.assertTrue(all(f == 0x02 for f in flags[2:-1]))
        self.assertEqual(flags[-1], 0x03)
        code, out, _ = run(["info", path, "--json"])
        self.assertEqual(code, 0)
        res = json.loads(out)
        self.assertTrue(res["loops"])
        self.assertEqual(res["loop_start_sample"], 28)
        self.assertEqual(res["rate"], RATE)

    def test_loop_points_survive_wav_and_back(self):
        x = sine(2800)
        path = self.make_vag(x, extra=["--loop-start", "1000"])
        code, out, _ = run(["info", path, "--json"])
        res = json.loads(out)
        # 1000 is padded to 1008 (a frame boundary), plus the 28-sample lead frame
        self.assertEqual(res["loop_start_sample"], 1036)
        code, out, err = run(["towav", path, "--out", self.p("l.wav")])
        self.assertEqual(code, 0, err)
        self.assertIn("smpl", out)
        # the WAV's smpl loop is picked up again on the way back
        code, out, err = run(["fromwav", self.p("l.wav"), "--out", self.p("l2.vag"), "--no-lead-frame"])
        self.assertEqual(code, 0, err)
        self.assertIn("smpl", out)
        res2 = json.loads(run(["info", self.p("l2.vag"), "--json"])[1])
        self.assertEqual(res2["loop_start_sample"], 1036)

    def test_no_loop_overrides_smpl(self):
        path = self.make_vag(sine(1000), extra=["--loop"])
        run(["towav", path, "--out", self.p("l.wav")])
        run(["fromwav", self.p("l.wav"), "--out", self.p("n.vag"), "--no-loop"])
        self.assertFalse(json.loads(run(["info", self.p("n.vag"), "--json"])[1])["loops"])

    def test_info_text(self):
        path = self.make_vag(sine(500))
        code, out, _ = run(["info", path])
        self.assertEqual(code, 0)
        self.assertIn("one-shot", out)
        self.assertIn("22050 Hz", out)

    def test_header_size_quirk_is_accepted(self):
        path = self.make_vag(sine(500))
        blob = bytearray(self.bytes_of("v.vag"))
        struct.pack_into(">I", blob, 0x0C, len(blob))  # counts the header too
        with open(path, "wb") as fh:
            fh.write(blob)
        code, out, err = run(["towav", path, "--out", self.p("x.wav")])
        self.assertEqual(code, 0, err)
        self.assertIn("counts the header", out)

    def test_truncated_vag_is_refused(self):
        path = self.make_vag(sine(5000))
        blob = self.bytes_of("v.vag")
        with open(path, "wb") as fh:
            fh.write(blob[:len(blob) // 2])
        code, _, err = run(["towav", path])
        self.assertEqual(code, 2)
        self.assertIn("truncated", err)
        self.assertNotIn("Traceback", err)

    def test_zero_rate_defaults_like_the_runtime(self):
        path = self.make_vag(sine(500))
        blob = bytearray(self.bytes_of("v.vag"))
        struct.pack_into(">I", blob, 0x10, 0)
        with open(path, "wb") as fh:
            fh.write(blob)
        code, out, _ = run(["towav", path, "--out", self.p("z.wav")])
        self.assertEqual(code, 0)
        self.assertEqual(read_wav(self.p("z.wav"))[0], 44100)


class Inputs(Base):
    def test_raw_adpcm_needs_a_rate(self):
        raw = vag.encode_adpcm(sine(1000))
        path = self.p("bank.bd")
        with open(path, "wb") as fh:
            fh.write(raw)
        code, _, err = run(["towav", path])
        self.assertEqual(code, 2)
        self.assertIn("--rate", err)
        code, out, err = run(["towav", path, "--rate", "11025", "--out", self.p("r.wav")])
        self.assertEqual(code, 0, err)
        self.assertEqual(read_wav(self.p("r.wav"))[0], 11025)
        self.assertEqual(run(["info", path])[0], 2)
        self.assertEqual(run(["info", path, "--rate", "11025"])[0], 0)

    def test_offset_outside_file(self):
        path = self.make_vag(sine(100))
        self.assertEqual(run(["towav", path, "--offset", "0x100000"])[0], 2)

    def test_default_output_goes_under_console_audio(self):
        path = self.make_vag(sine(300), name="theme.vag")
        code, out, _ = run(["towav", path])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(self.p("console", "audio", "theme.wav")))

    def test_not_a_wav(self):
        bad = self.p("x.wav")
        with open(bad, "wb") as fh:
            fh.write(b"hello there, not audio")
        code, _, err = run(["fromwav", bad])
        self.assertEqual(code, 2)
        self.assertIn("RIFF", err)

    def test_8_bit_wav_is_refused(self):
        wav = self.p("8.wav")
        write_wav(wav, [sine(100)], width=1)
        code, _, err = run(["fromwav", wav])
        self.assertEqual(code, 2)
        self.assertIn("16-bit", err)

    def test_empty_wav_refused(self):
        wav = self.p("empty.wav")
        write_wav(wav, [[]])
        code, _, err = run(["fromwav", wav])
        self.assertEqual(code, 2)
        self.assertIn("no audio", err)

    def test_three_channels_refused(self):
        wav = self.p("3.wav")
        write_wav(wav, [sine(100)] * 3)
        self.assertEqual(run(["fromwav", wav])[0], 2)

    def test_missing_file(self):
        self.assertEqual(run(["info", self.p("nope.vag")])[0], 2)
        self.assertEqual(run(["fromwav", self.p("nope.wav")])[0], 2)

    def test_bad_frames_warn(self):
        path = self.p("noise.bin")
        with open(path, "wb") as fh:
            fh.write(bytes([0xF7, 0] + [0x55] * 14) * 4)
        code, out, _ = run(["towav", path, "--rate", "8000", "--out", self.p("n.wav")])
        self.assertEqual(code, 1)
        self.assertIn("WARNING", out)


class Stereo(Base):
    def setUp(self):
        Base.setUp(self)
        self.left = sine(3000, 440)
        self.right = sine(3000, 660, 9000)
        self.wav = self.p("st.wav")
        write_wav(self.wav, [self.left, self.right])

    def test_split_into_two_files(self):
        code, out, err = run(["fromwav", self.wav, "--out", self.p("st.vag")])
        self.assertEqual(code, 0, err)
        for side, ref in (("L", self.left), ("R", self.right)):
            run(["towav", self.p("st_%s.vag" % side), "--out", self.p("%s.wav" % side)])
            _, (y,) = read_wav(self.p("%s.wav" % side))
            self.assertGreater(snr(ref, y[28:28 + len(ref)]), 25.0)

    def test_interleaved_round_trip(self):
        code, out, err = run(["fromwav", self.wav, "--out", self.p("i.vag"), "--stereo", "interleave",
                              "--interleave", "256"])
        self.assertEqual(code, 0, err)
        code, out, err = run(["towav", self.p("i.vag"), "--channels", "2", "--interleave", "256",
                              "--out", self.p("i.wav")])
        self.assertEqual(code, 0, err)
        rate, (l, r) = read_wav(self.p("i.wav"))
        self.assertGreater(snr(self.left, l[28:28 + 3000]), 25.0)
        self.assertGreater(snr(self.right, r[28:28 + 3000]), 25.0)

    def test_stereo_decode_needs_interleave(self):
        run(["fromwav", self.wav, "--out", self.p("i.vag"), "--interleave", "256"])
        self.assertEqual(run(["towav", self.p("i.vag"), "--channels", "2"])[0], 2)
        self.assertEqual(run(["towav", self.p("i.vag"), "--channels", "2", "--interleave", "100"])[0], 2)

    def test_mono_mixdown(self):
        code, out, _ = run(["fromwav", self.wav, "--out", self.p("m.vag"), "--mono"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(self.p("m.vag")))
        self.assertIn("mixed stereo down", out)


class Like(Base):
    def test_like_copies_rate_and_refuses_bigger(self):
        orig = self.make_vag(sine(2000, rate=11025), name="orig.vag", rate=11025)
        longer = self.p("mine.wav")
        write_wav(longer, [sine(44100, rate=44100)], 44100)  # 1 s at 44.1 kHz
        code, out, err = run(["fromwav", longer, "--like", orig, "--out", self.p("new.vag")])
        self.assertEqual(code, 1)
        self.assertIn("SPU2 RAM", err)
        self.assertFalse(os.path.exists(self.p("new.vag")))
        code, out, err = run(["fromwav", longer, "--like", orig, "--out", self.p("new.vag"), "--allow-bigger"])
        self.assertEqual(code, 0, err)
        res = json.loads(run(["info", self.p("new.vag"), "--json"])[1])
        self.assertEqual(res["rate"], 11025)
        self.assertAlmostEqual(res["seconds"], 1.0, delta=0.02)
        self.assertIn("resampled 44100 Hz to 11025 Hz", out)

    def test_like_fits_and_copies_looping(self):
        orig = self.make_vag(sine(4000), name="loop.vag", extra=["--loop"])
        short = self.p("s.wav")
        write_wav(short, [sine(1000)])
        code, out, err = run(["fromwav", short, "--like", orig, "--out", self.p("n.vag")])
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(run(["info", self.p("n.vag"), "--json"])[1])["loops"])

    def test_like_must_be_a_vag(self):
        wav = self.p("w.wav")
        write_wav(wav, [sine(100)])
        self.assertEqual(run(["fromwav", wav, "--like", wav])[0], 2)


class Scan(Base):
    def blob(self):
        a = self.bytes_of(os.path.basename(self.make_vag(sine(700), name="a.vag")))
        b = self.bytes_of(os.path.basename(self.make_vag(sine(900, 300), name="b.vag", rate=32000)))
        fake = b"VAGp" + bytes(12) + struct.pack(">I", 7) + bytes(28)  # rate 0, size 0
        junk = bytes((i * 31) & 0xFF for i in range(0x100))
        return junk + a + bytes(0x1345) + b + fake + junk, a, b

    def test_scan_finds_embedded_vags(self):
        data, a, b = self.blob()
        path = self.p("pack.dat")
        with open(path, "wb") as fh:
            fh.write(data)
        code, out, err = run(["scan", path, "--json"])
        self.assertEqual(code, 0, err)
        hits = json.loads(out)
        self.assertEqual([h["offset"] for h in hits], [0x100, 0x100 + len(a) + 0x1345])
        self.assertEqual([h["rate"] for h in hits], [RATE, 32000])
        code, out, _ = run(["scan", path, "--all", "--json"])
        self.assertEqual(len(json.loads(out)), 3)

    def test_scan_extract(self):
        data, a, b = self.blob()
        path = self.p("pack.dat")
        with open(path, "wb") as fh:
            fh.write(data)
        code, out, _ = run(["scan", path, "--extract", self.p("out")])
        self.assertEqual(code, 0)
        files = sorted(os.listdir(self.p("out")))
        self.assertEqual(len(files), 2)
        self.assertEqual(self.bytes_of("out", files[0]), a)
        self.assertEqual(self.bytes_of("out", files[1]), b)

    def test_scan_across_read_chunks(self):
        data, a, b = self.blob()
        path = self.p("pack.dat")
        with open(path, "wb") as fh:
            fh.write(data)
        for chunk in (5, 64, 257):
            hits = [h["offset"] for h in vag._scan_hits(path, chunk=chunk) if h["valid"]]
            self.assertEqual(hits, [0x100, 0x100 + len(a) + 0x1345], chunk)

    def test_scan_nothing(self):
        path = self.p("empty.dat")
        with open(path, "wb") as fh:
            fh.write(b"\0" * 100)
        code, out, _ = run(["scan", path])
        self.assertEqual(code, 0)
        self.assertIn("no VAGp", out)

    def test_scan_folder_is_refused(self):
        self.assertEqual(run(["scan", self.tmp])[0], 2)


class Split(Base):
    def test_split_a_headerless_bank(self):
        one = vag.encode_adpcm(sine(600))
        two = vag.encode_adpcm(sine(900, 200), loop_start=0)
        bank = self.p("se.bd")
        with open(bank, "wb") as fh:
            fh.write(one + two + bytes(64))
        code, out, err = run(["split", bank, "--rate", "22050", "--out", self.p("se"), "--vag", "--json"])
        self.assertEqual(code, 0, err)
        rows = json.loads(out)
        self.assertEqual(len(rows), 2)
        self.assertFalse(rows[0]["loops"])
        self.assertTrue(rows[1]["loops"])
        self.assertEqual(rows[1]["offset"], len(one))
        with open(self.p("se", "split.json")) as fh:
            self.assertEqual(len(json.load(fh)["sounds"]), 2)
        _, (y,) = read_wav(rows[0]["wav"])
        self.assertGreater(snr(sine(600), y[28:628]), 25.0)
        self.assertEqual(run(["info", rows[1]["vag"]])[0], 0)

    def test_split_refuses_a_headed_file(self):
        path = self.make_vag(sine(100))
        self.assertEqual(run(["split", path, "--rate", "8000", "--out", self.p("o")])[0], 2)


class Helpers(unittest.TestCase):
    def test_resample_lengths(self):
        self.assertEqual(len(vag.resample(list(range(100)), 100, 50)), 50)
        self.assertEqual(vag.resample([1, 2, 3], 10, 10), [1, 2, 3])
        self.assertEqual(vag.resample([], 10, 20), [])

    def test_align_loop(self):
        s, ls, pad = vag.align_loop([1] * 50, 30)
        self.assertEqual((ls, pad, len(s)), (56, 26, 76))
        self.assertEqual(vag.align_loop([1] * 50, 28)[2], 0)

    def test_encode_rejects_unaligned_loop(self):
        with self.assertRaises(ValueError):
            vag.encode_adpcm([0] * 100, loop_start=5)

    def test_parse_header_rejects_other_magic(self):
        self.assertIsNone(vag.parse_header(b"RIFF" + bytes(60)))
        self.assertIsNone(vag.parse_header(b"VAGp"))


class Cli(unittest.TestCase):
    def test_help_everywhere(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "console-sound", "vag.py"), "--help"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(r.returncode, 0)
        self.assertIn(b"usage", r.stdout)
        for cmd in ("info", "towav", "fromwav", "scan", "split"):
            out = io.StringIO()
            with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
                vag.main([cmd, "--help"])
            self.assertEqual(cm.exception.code, 0)
            self.assertIn("usage", out.getvalue())


if __name__ == "__main__":
    unittest.main()
