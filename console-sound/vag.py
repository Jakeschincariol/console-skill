#!/usr/bin/env python3
"""vag.py: PS2 SPU2 ADPCM (VAG) audio for /console-sound.

Decode VAG files (and raw ADPCM from sound banks) to WAV, encode your own
16-bit WAV back to VAG, and find VAGs hidden inside bigger files.

The ADPCM format: 16-byte frames, each 28 samples.
  byte 0  = shift (low nibble) | filter (high nibble)
  byte 1  = flags: 0x04 loop start, 0x02 loop/repeat, 0x01 end, 0x07 = end-of-data marker
  bytes 2..15 = 28 signed 4-bit samples, low nibble first
  filters (k1, k2) / 64: (0,0) (60,0) (115,-52) (98,-55) (122,-60)
The decoder follows PS2Recomp's ps2xRuntime/src/lib/ps2_audio_vag.cpp exactly
(C integer division, shift 13..15 played as 9, filter 5..7 as 0), so what you
hear here is what the port plays.

Python 3.8+ standard library only. Exit codes: 0 ok, 1 needs attention,
2 bad input.
"""

import argparse
import array
import json
import math
import os
import struct
import sys

FILTERS = ((0, 0), (60, 0), (115, -52), (98, -55), (122, -60))
FLAG_END = 0x01
FLAG_REPEAT = 0x02
FLAG_LOOP_START = 0x04
FLAG_MARKER = 0x07
HEADER_SIZE = 0x30
FRAME_BYTES = 16
SAMPLES_PER_FRAME = 28
MAGICS = (b"VAGp", b"VAGi")
DEFAULT_VERSION = 0x20
HEADER_SLACK = HEADER_SIZE  # some tools count the header in the data size field

# signed (low, high) nibble pair for every byte value
_NIB = []
for _b in range(256):
    _lo = _b & 0x0F
    _hi = _b >> 4
    _NIB.append((_lo - 16 if _lo & 8 else _lo, _hi - 16 if _hi & 8 else _hi))
del _b, _lo, _hi


class ToolError(Exception):
    """Bad input: exit 2 with a one-line message."""


# ---------------------------------------------------------------- decoding

def _div64(t):
    # C integer division by 64 (truncates toward zero), as the runtime does
    return t >> 6 if t >= 0 else -((-t) >> 6)


def decode_adpcm(data, start=0, end=None, stop_at_end=True):
    """Decode raw SPU ADPCM frames in data[start:end].

    Returns (samples, info). info has frames, loop_start / loop_end (sample
    indexes from the frame flags, or None), end ('end-flag', 'marker' or
    'data-end') and bad_frames (frames with shift > 12 or filter > 4).
    """
    if end is None or end > len(data):
        end = len(data)
    out = []
    append = out.append
    s1 = s2 = 0
    loop_start = loop_end = None
    end_kind = "data-end"
    frames = bad = 0
    nib = _NIB
    pos = start
    while pos + FRAME_BYTES <= end:
        b0 = data[pos]
        flags = data[pos + 1]
        if stop_at_end and (flags & FLAG_MARKER) == FLAG_MARKER:
            end_kind = "marker"
            break
        shift = b0 & 0x0F
        filt = (b0 >> 4) & 0x07
        odd = False
        if shift > 12:
            shift = 9
            odd = True
        if filt > 4:
            filt = 0
            odd = True
        if b0 & 0x80:
            odd = True
        if odd:
            bad += 1
        if flags & FLAG_LOOP_START and loop_start is None:
            loop_start = len(out)
        mul = 1 << (12 - shift)
        if filt == 0:
            for i in range(pos + 2, pos + FRAME_BYTES):
                lo, hi = nib[data[i]]
                append(lo * mul)
                append(hi * mul)
            s1 = out[-1]
            s2 = out[-2]
        else:
            k1, k2 = FILTERS[filt]
            for i in range(pos + 2, pos + FRAME_BYTES):
                lo, hi = nib[data[i]]
                t = k1 * s1 + k2 * s2 + 32
                v = lo * mul + (t >> 6 if t >= 0 else -((-t) >> 6))
                if v > 32767:
                    v = 32767
                elif v < -32768:
                    v = -32768
                s2 = s1
                s1 = v
                t = k1 * s1 + k2 * s2 + 32
                v = hi * mul + (t >> 6 if t >= 0 else -((-t) >> 6))
                if v > 32767:
                    v = 32767
                elif v < -32768:
                    v = -32768
                s2 = s1
                s1 = v
                append(s2)
                append(s1)
        frames += 1
        pos += FRAME_BYTES
        if flags & FLAG_END:
            if flags & FLAG_REPEAT:
                loop_end = len(out)
            if stop_at_end:
                end_kind = "end-flag"
                break
    info = {"frames": frames, "loop_start": loop_start, "loop_end": loop_end,
            "end": end_kind, "bad_frames": bad, "bytes_used": pos - start}
    return out, info


def frame_stats(data, start=0, end=None):
    """Walk the frames without decoding: flags, loop points, odd frames."""
    if end is None or end > len(data):
        end = len(data)
    frames = bad = 0
    loop_start = loop_end = end_frame = marker_frame = None
    lead_zero = end - start >= FRAME_BYTES and not any(data[start:start + FRAME_BYTES])
    pos = start
    while pos + FRAME_BYTES <= end:
        b0 = data[pos]
        flags = data[pos + 1]
        if (flags & FLAG_MARKER) == FLAG_MARKER:
            marker_frame = frames
            break
        if (b0 & 0x0F) > 12 or ((b0 >> 4) & 0x07) > 4 or b0 & 0x80:
            bad += 1
        if flags & FLAG_LOOP_START and loop_start is None:
            loop_start = frames
        if flags & FLAG_END:
            end_frame = frames
            if flags & FLAG_REPEAT:
                loop_end = frames
            frames += 1
            break
        frames += 1
        pos += FRAME_BYTES
    return {"frames": frames, "bad_frames": bad, "lead_zero_frame": lead_zero,
            "loop_start_frame": loop_start, "loop_end_frame": loop_end,
            "end_flag_frame": end_frame, "marker_frame": marker_frame}


# ---------------------------------------------------------------- encoding

def _try_block(x, s1, s2, k1, k2, shift, limit):
    """Closed-loop encode of one frame with one filter and shift.

    Returns (error, nibbles, s1, s2) or None as soon as the squared error
    reaches limit."""
    sh = 12 - shift
    step = 1 << sh
    half = step >> 1
    err = 0
    nibs = []
    for v in x:
        t = k1 * s1 + k2 * s2 + 32
        pred = t >> 6 if t >= 0 else -((-t) >> 6)
        r = v - pred
        if r >= 0:
            n = (r + half) >> sh
            if n > 7:
                n = 7
        else:
            n = -((half - r) >> sh)
            if n < -8:
                n = -8
        y = n * step + pred
        if y > 32767:
            y = 32767
        elif y < -32768:
            y = -32768
        d = v - y
        err += d * d
        if err >= limit:
            return None
        nibs.append(n & 0x0F)
        s2 = s1
        s1 = y
    return err, nibs, s1, s2


def encode_block(x, s1, s2, exhaustive=False):
    """Pick the filter and shift with the least squared error for 28 samples.

    Every filter is tried. For each, the shift is estimated from the residual
    and that shift plus its neighbours are tried closed-loop (exactly as the
    decoder will rebuild them, with clamping); exhaustive=True tries all 13.
    Returns (header_byte, nibbles, s1, s2, error)."""
    if not any(x):
        return 0x00, [0] * SAMPLES_PER_FRAME, 0, 0, 0
    ests = []
    for f, (k1, k2) in enumerate(FILTERS):
        p1, p2 = s1, s2
        mx = 0
        for v in x:
            t = k1 * p1 + k2 * p2 + 32
            r = v - (t >> 6 if t >= 0 else -((-t) >> 6))
            if r < 0:
                r = -r
            if r > mx:
                mx = r
            p2 = p1
            p1 = v
        sh = 12
        while sh > 0 and (15 << (12 - sh)) >> 1 < mx:  # 7.5 steps
            sh -= 1
        ests.append((mx, f, sh))
    ests.sort()
    best = None
    best_err = 1 << 62
    for _mx, f, sh in ests:
        if exhaustive:
            shifts = sorted(range(13), key=lambda s: abs(s - sh))
        else:
            shifts = [s for s in (sh, sh + 1, sh - 1) if 0 <= s <= 12]
        k1, k2 = FILTERS[f]
        for s in shifts:
            res = _try_block(x, s1, s2, k1, k2, s, best_err)
            if res is not None and res[0] < best_err:
                best_err = res[0]
                best = (f, s, res)
    f, s, (err, nibs, n1, n2) = best
    return (f << 4) | s, nibs, n1, n2, err


def encode_adpcm(samples, loop_start=None, lead_frame=True, end_marker=True, exhaustive=False):
    """Encode 16-bit samples to raw ADPCM frames.

    loop_start: None for a one-shot sound, else the sample index the loop
    returns to; it must be a multiple of 28 (see align_loop). The loop end is
    always the last frame."""
    n = len(samples)
    nframes = max(1, -(-n // SAMPLES_PER_FRAME))
    lsf = None
    if loop_start is not None:
        if loop_start % SAMPLES_PER_FRAME:
            raise ValueError("loop start must be a multiple of 28 samples")
        lsf = min(loop_start // SAMPLES_PER_FRAME, nframes - 1)
    out = bytearray()
    if lead_frame:
        out += bytes(FRAME_BYTES)
    s1 = s2 = 0
    for fi in range(nframes):
        x = list(samples[fi * SAMPLES_PER_FRAME:(fi + 1) * SAMPLES_PER_FRAME])
        if len(x) < SAMPLES_PER_FRAME:
            x += [0] * (SAMPLES_PER_FRAME - len(x))
        hdr, nibs, s1, s2, _err = encode_block(x, s1, s2, exhaustive)
        last = fi == nframes - 1
        if lsf is None:
            flags = FLAG_END if last else 0
        elif fi < lsf:
            flags = 0
        else:
            flags = FLAG_REPEAT
            if fi == lsf:
                flags |= FLAG_LOOP_START
            if last:
                flags |= FLAG_END
        out.append(hdr)
        out.append(flags)
        for i in range(0, SAMPLES_PER_FRAME, 2):
            out.append(nibs[i] | (nibs[i + 1] << 4))
    if lsf is None and end_marker:
        # end-of-data marker; zero sample bytes so a decoder that ignores
        # the flags (the PS2Recomp runtime does) plays silence, not a click
        out += bytes([0x00, FLAG_MARKER]) + bytes(14)
    return bytes(out)


def align_loop(samples, loop_start):
    """Pad the front with silence so the loop start lands on a frame."""
    pad = (-loop_start) % SAMPLES_PER_FRAME
    if pad:
        samples = [0] * pad + list(samples)
    return samples, loop_start + pad, pad


# ---------------------------------------------------------------- headers

def parse_header(buf, offset=0):
    """Parse a 48-byte VAGp header at offset. Returns a dict or None."""
    if offset + HEADER_SIZE > len(buf) or bytes(buf[offset:offset + 4]) not in MAGICS:
        return None
    version, reserved, size, rate = struct.unpack_from(">IIII", buf, offset + 4)
    name = bytes(buf[offset + 0x20:offset + 0x30]).split(b"\0")[0].decode("latin-1")
    return {"magic": bytes(buf[offset:offset + 4]).decode("ascii"), "version": version,
            "reserved": reserved, "data_size": size, "rate": rate, "name": name}


def make_header(data_size, rate, name="", version=DEFAULT_VERSION, magic=b"VAGp"):
    h = bytearray(HEADER_SIZE)
    h[0:4] = magic
    struct.pack_into(">I", h, 4, version)
    struct.pack_into(">I", h, 0x0C, data_size)
    struct.pack_into(">I", h, 0x10, rate)
    nm = name.encode("ascii", "replace")[:16]
    h[0x20:0x20 + len(nm)] = nm
    return bytes(h)


def header_name(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    return "".join(c if 32 < ord(c) < 127 else "_" for c in stem)[:16]


def data_range(buf, offset, hdr):
    """(start, end) of the ADPCM data after a header, refusing truncation."""
    start = offset + HEADER_SIZE
    avail = len(buf) - start
    size = hdr["data_size"]
    if size == 0:
        return start, len(buf), None
    if size > avail:
        if size - avail <= HEADER_SLACK:
            return start, len(buf), "the header's data size counts the header too; using the %d bytes present" % avail
        raise ToolError("truncated VAG: the header says %d bytes of data but only %d follow" % (size, max(avail, 0)))
    return start, start + size, None


# ---------------------------------------------------------------- WAV

def read_wav(path):
    """Read a 16-bit PCM WAV. Returns (rate, [channel lists], loop or None)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        raise ToolError("cannot read %s: %s" % (path, e.strerror or e))
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ToolError("%s is not a RIFF/WAVE file" % path)
    pos = 12
    fmt = pcm = loop = None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        body = data[pos + 8:pos + 8 + size]
        if len(body) < size and cid != b"data":
            raise ToolError("%s is truncated inside its %r chunk" % (path, cid.decode("latin-1")))
        if cid == b"fmt ":
            if len(body) < 16:
                raise ToolError("%s has a short fmt chunk" % path)
            tag, ch, rate, _br, _ba, bits = struct.unpack_from("<HHIIHH", body, 0)
            if tag == 0xFFFE and len(body) >= 26:
                tag = struct.unpack_from("<H", body, 24)[0]
            fmt = (tag, ch, rate, bits)
        elif cid == b"data":
            pcm = body
        elif cid == b"smpl" and len(body) >= 36:
            nloops = struct.unpack_from("<I", body, 28)[0]
            if nloops and len(body) >= 36 + 24:
                _cue, _typ, ls, le = struct.unpack_from("<IIII", body, 36)
                loop = (ls, le + 1)
        pos += 8 + size + (size & 1)
    if fmt is None or pcm is None:
        raise ToolError("%s has no fmt or data chunk" % path)
    tag, ch, rate, bits = fmt
    if tag != 1 or bits != 16:
        raise ToolError("%s is not 16-bit PCM (format %d, %d-bit): export it as signed 16-bit PCM WAV first"
                        % (path, tag, bits))
    if ch not in (1, 2):
        raise ToolError("%s has %d channels: only mono or stereo" % (path, ch))
    if rate <= 0:
        raise ToolError("%s has sample rate 0" % path)
    usable = len(pcm) - (len(pcm) % (2 * ch))
    a = array.array("h")
    a.frombytes(pcm[:usable])
    if sys.byteorder == "big":
        a.byteswap()
    chans = [a[i::ch].tolist() for i in range(ch)]
    return rate, chans, loop


def write_wav(path, chans, rate, loop=None):
    ch = len(chans)
    n = max(len(c) for c in chans) if chans else 0
    a = array.array("h", bytes(2 * n * ch))
    for i, c in enumerate(chans):
        col = array.array("h", c)
        if len(col) < n:
            col.extend([0] * (n - len(col)))
        a[i::ch] = col
    if sys.byteorder == "big":
        a.byteswap()
    pcm = a.tobytes()
    chunks = [b"fmt " + struct.pack("<IHHIIHH", 16, 1, ch, rate, rate * ch * 2, ch * 2, 16)]
    if loop is not None and loop[1] > loop[0]:
        period = int(round(1e9 / rate))
        smpl = struct.pack("<IIIIIIIII", 0, 0, period, 60, 0, 0, 0, 1, 0)
        smpl += struct.pack("<IIIIII", 0, 0, loop[0], loop[1] - 1, 0, 0)
        chunks.append(b"smpl" + struct.pack("<I", len(smpl)) + smpl)
    chunks.append(b"data" + struct.pack("<I", len(pcm)) + pcm + (b"\0" if len(pcm) & 1 else b""))
    body = b"WAVE" + b"".join(chunks)
    _ensure_parent(path)
    with open(path, "wb") as fh:
        fh.write(b"RIFF" + struct.pack("<I", len(body)) + body)


def resample(x, src, dst):
    """Linear-interpolation resample (simple; resample in an editor for best quality)."""
    if src == dst or not x:
        return list(x)
    n_out = max(1, int(round(len(x) * dst / float(src))))
    ratio = src / float(dst)
    last = len(x) - 1
    out = []
    for i in range(n_out):
        p = i * ratio
        j = int(p)
        if j >= last:
            out.append(x[last])
        else:
            fr = p - j
            out.append(int(round(x[j] + (x[j + 1] - x[j]) * fr)))
    return out


def snr_db(ref, test):
    n = min(len(ref), len(test))
    sig = sum(v * v for v in ref[:n])
    noise = sum((ref[i] - test[i]) ** 2 for i in range(n))
    if noise == 0:
        return float("inf")
    if sig == 0:
        return 0.0
    return 10.0 * math.log10(sig / float(noise))


# ---------------------------------------------------------------- helpers

def _ensure_parent(path):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)


def _read(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except IsADirectoryError:
        raise ToolError("%s is a folder, not a file" % path)
    except OSError as e:
        raise ToolError("cannot read %s: %s" % (path, e.strerror or e))


def _int(text):
    try:
        return int(text, 0)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("not a number: %r" % text)


def _secs(samples, rate):
    return samples / float(rate) if rate else 0.0


def _interleave_split(data, start, end, channels, interleave):
    """Split interleaved ADPCM data into per-channel byte strings."""
    chans = [bytearray() for _ in range(channels)]
    pos = start
    i = 0
    while pos < end:
        chans[i % channels] += data[pos:min(pos + interleave, end)]
        pos += interleave
        i += 1
    return [bytes(c) for c in chans]


def _interleave_join(chans, interleave):
    longest = max(len(c) for c in chans)
    blocks = -(-longest // interleave)
    out = bytearray()
    for b in range(blocks):
        for c in chans:
            piece = c[b * interleave:(b + 1) * interleave]
            out += piece + bytes(interleave - len(piece))
    return bytes(out)


def _source(buf, offset, rate):
    """Resolve header / raw at offset. Returns (hdr or None, start, end, rate, note)."""
    if offset < 0 or offset >= len(buf):
        raise ToolError("offset 0x%x is outside the file (%d bytes)" % (offset, len(buf)))
    hdr = parse_header(buf, offset)
    if hdr is not None:
        start, end, note = data_range(buf, offset, hdr)
        r = rate or hdr["rate"]
        if not r:
            r = 44100
            note = (note + "; " if note else "") + "header rate is 0, assuming 44100 Hz (the runtime does the same)"
        return hdr, start, end, r, note
    if bytes(buf[offset:offset + 4]) == b"pGAV":
        raise ToolError("byte-swapped VAG header ('pGAV'): not a PS2 layout this tool reads")
    if not rate:
        raise ToolError("no VAGp header at offset 0x%x: for raw ADPCM pass --rate N (the sample rate)" % offset)
    return None, offset, len(buf), rate, None


# ---------------------------------------------------------------- commands

def cmd_info(args):
    buf = _read(args.file)
    hdr, start, end, rate, note = _source(buf, args.offset, args.rate)
    st = frame_stats(buf, start, end)
    samples = st["frames"] * SAMPLES_PER_FRAME
    res = {"file": args.file, "offset": args.offset, "header": hdr, "rate": rate,
           "data_bytes": end - start, "frames": st["frames"], "samples": samples,
           "seconds": round(_secs(samples, rate), 3), "lead_zero_frame": st["lead_zero_frame"],
           "loop_start_sample": None if st["loop_start_frame"] is None else st["loop_start_frame"] * SAMPLES_PER_FRAME,
           "loops": st["loop_end_frame"] is not None,
           "end_flag_frame": st["end_flag_frame"], "end_marker_frame": st["marker_frame"],
           "bad_frames": st["bad_frames"], "note": note}
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        if hdr:
            print("%s: %s header, version 0x%x, name %r" % (args.file, hdr["magic"], hdr["version"], hdr["name"]))
            print("  data size   %d bytes (header field %d)" % (end - start, hdr["data_size"]))
        else:
            print("%s: raw ADPCM from offset 0x%x (no header)" % (args.file, args.offset))
        print("  sample rate %d Hz" % rate)
        print("  frames      %d (%d samples, %.3f s, up to the end flag)" % (st["frames"], samples, res["seconds"]))
        if res["loops"]:
            print("  loop        from sample %s to the end" % res["loop_start_sample"])
        else:
            print("  loop        none (one-shot)")
        print("  lead frame  %s" % ("silent first frame (Sony convention)" if st["lead_zero_frame"] else "no"))
        if st["bad_frames"]:
            print("  WARNING: %d frames have an out-of-range filter or shift: this may not be ADPCM at this offset"
                  % st["bad_frames"])
        if note:
            print("  note: %s" % note)
    return 1 if st["bad_frames"] else 0


def cmd_towav(args):
    buf = _read(args.file)
    hdr, start, end, rate, note = _source(buf, args.offset, args.rate)
    if args.channels not in (1, 2):
        raise ToolError("--channels must be 1 or 2")
    if args.channels == 2:
        if not args.interleave:
            raise ToolError("stereo needs --interleave N (bytes per channel block, a multiple of 16)")
        if args.interleave % 16:
            raise ToolError("--interleave must be a multiple of 16")
        parts = _interleave_split(buf, start, end, 2, args.interleave)
        chans, infos = [], []
        for p in parts:
            s, inf = decode_adpcm(p, 0, None, stop_at_end=not args.all)
            chans.append(s)
            infos.append(inf)
        info = infos[0]
    else:
        s, info = decode_adpcm(buf, start, end, stop_at_end=not args.all)
        chans = [s]
    out = args.out or os.path.join("console", "audio", os.path.splitext(os.path.basename(args.file))[0] + ".wav")
    loop = None
    if info["loop_start"] is not None and info["loop_end"] is not None and not args.no_loop_chunk:
        loop = (info["loop_start"], info["loop_end"])
    write_wav(out, chans, rate, loop)
    n = max(len(c) for c in chans)
    print("wrote %s: %d Hz, %s, %d samples (%.3f s)%s" % (
        out, rate, "stereo" if len(chans) == 2 else "mono", n, _secs(n, rate),
        ", loop %d..%d in a smpl chunk" % loop if loop else ""))
    if note:
        print("note: %s" % note)
    if info["bad_frames"]:
        print("WARNING: %d frames had an out-of-range filter or shift; check the offset and format" % info["bad_frames"])
        return 1
    return 0


def _like(path):
    buf = _read(path)
    hdr = parse_header(buf, 0)
    if hdr is None:
        raise ToolError("--like %s has no VAGp header" % path)
    start, end, _note = data_range(buf, 0, hdr)
    st = frame_stats(buf, start, end)
    hdr["loops"] = st["loop_end_frame"] is not None
    hdr["bytes"] = end - start
    return hdr


def cmd_fromwav(args):
    rate, chans, wav_loop = read_wav(args.wav)
    if not chans[0]:
        raise ToolError("%s has no audio samples" % args.wav)
    like = _like(args.like) if args.like else None
    target_rate = args.rate or (like["rate"] if like and like["rate"] else rate)
    notes = []
    if args.mono and len(chans) == 2:
        chans = [[(a + b) // 2 for a, b in zip(chans[0], chans[1])]]
        notes.append("mixed stereo down to mono")
    if target_rate != rate:
        chans = [resample(c, rate, target_rate) for c in chans]
        if wav_loop:
            wav_loop = (int(round(wav_loop[0] * target_rate / float(rate))),
                        int(round(wav_loop[1] * target_rate / float(rate))))
        notes.append("resampled %d Hz to %d Hz (linear; for best quality resample in an audio editor)"
                     % (rate, target_rate))
    # loop decision
    loop_start = None
    if not args.no_loop:
        if args.loop_start is not None:
            loop_start = args.loop_start
        elif wav_loop is not None:
            loop_start = wav_loop[0]
            n = len(chans[0])
            if wav_loop[1] < n:
                chans = [c[:wav_loop[1]] for c in chans]
                notes.append("trimmed after the WAV's loop end (sample %d): a VAG loops to its last frame" % wav_loop[1])
            notes.append("loop taken from the WAV's smpl chunk")
        elif args.loop or (like and like["loops"]):
            loop_start = 0
            if not args.loop:
                notes.append("loops because the original (--like) loops")
    if loop_start is not None:
        if loop_start < 0 or loop_start >= max(1, len(chans[0])):
            raise ToolError("loop start %d is outside the sound (%d samples)" % (loop_start, len(chans[0])))
        new = []
        pad = 0
        for c in chans:
            c2, ls2, pad = align_loop(c, loop_start)
            new.append(c2)
        chans = new
        if pad:
            notes.append("added %d samples of silence at the start so the loop begins on a 28-sample frame" % pad)
        loop_start += pad
        loop_len = len(chans[0]) - loop_start
        if loop_len % SAMPLES_PER_FRAME:
            notes.append("loop length %d is not a multiple of 28: %d samples of silence will play at the loop point"
                         " (trim the loop to a multiple of 28 to avoid it)"
                         % (loop_len, SAMPLES_PER_FRAME - loop_len % SAMPLES_PER_FRAME))
    lead = not args.no_lead_frame
    encoded = [encode_adpcm(c, loop_start, lead_frame=lead, exhaustive=args.exhaustive) for c in chans]
    magic = b"VAGp"
    version = DEFAULT_VERSION
    if like:
        magic = like["magic"].encode("ascii")
        version = like["version"]
    out = args.out or os.path.join("console", "audio", os.path.splitext(os.path.basename(args.wav))[0] + ".vag")
    name = args.name if args.name is not None else (like["name"] if like and like["name"] else header_name(out))
    stereo_mode = args.stereo
    if len(encoded) == 2 and args.interleave and stereo_mode is None:
        stereo_mode = "interleave"
    outputs = []
    if len(encoded) == 1:
        outputs.append((out, encoded[0]))
    elif stereo_mode == "interleave":
        if not args.interleave or args.interleave % 16:
            raise ToolError("interleaved stereo needs --interleave N, a multiple of 16")
        outputs.append((out, _interleave_join(encoded, args.interleave)))
        notes.append("stereo interleaved every %d bytes (left block, then right)" % args.interleave)
    else:
        stem, ext = os.path.splitext(out)
        outputs.append((stem + "_L" + (ext or ".vag"), encoded[0]))
        outputs.append((stem + "_R" + (ext or ".vag"), encoded[1]))
        notes.append("stereo written as two mono files (_L and _R)")
    if like and not args.allow_bigger:
        for path, data in outputs:
            if len(data) > like["bytes"]:
                print("refused: %s would be %d bytes of ADPCM, the original is %d. Games often load sounds into"
                      " fixed slots in the 2 MB of SPU2 RAM; shorten the sound, lower the rate, or pass"
                      " --allow-bigger if you know it fits." % (path, len(data), like["bytes"]), file=sys.stderr)
                return 1
    for path, data in outputs:
        _ensure_parent(path)
        with open(path, "wb") as fh:
            fh.write(make_header(len(data), target_rate, name, version, magic) + data)
    # quality check: decode what we wrote and compare
    quality = []
    for c, data in zip(chans, encoded):
        dec, _ = decode_adpcm(data, 0, None, stop_at_end=True)
        skip = SAMPLES_PER_FRAME if lead else 0
        quality.append(snr_db(c, dec[skip:skip + len(c)]))
    for path, data in outputs:
        frames = len(data) // FRAME_BYTES
        print("wrote %s: %d Hz, %d bytes of ADPCM (%d frames, %.3f s), %s" % (
            path, target_rate, len(data), frames,
            _secs(len(chans[0]), target_rate), "loops from sample %d" % (loop_start + (SAMPLES_PER_FRAME if lead else 0))
            if loop_start is not None else "one-shot"))
    q = min(quality)
    print("quality: %s dB signal to noise after decoding" % ("inf" if q == float("inf") else "%.1f" % q))
    for n in notes:
        print("note: %s" % n)
    if like:
        print("matched the original: rate %d, magic %s, version 0x%x, name %r (original %d bytes)"
              % (target_rate, magic.decode(), version, name, like["bytes"]))
    return 0


def _scan_hits(path, chunk=4 << 20):
    size = os.path.getsize(path)
    hits = []
    with open(path, "rb") as fh:
        base = 0
        carry = b""
        while True:
            block = fh.read(chunk)
            if not block:
                break
            data = carry + block
            start = 0
            while True:
                i = data.find(b"VAG", start)
                if i < 0 or i + 4 > len(data):
                    break
                if data[i:i + 4] in MAGICS:
                    hits.append(base - len(carry) + i)
                start = i + 1
            carry = data[-3:]
            base += len(block)
        for off in sorted(set(hits)):
            fh.seek(off)
            head = fh.read(HEADER_SIZE)
            hdr = parse_header(head, 0)
            if hdr is None:
                continue
            avail = size - off - HEADER_SIZE
            ok = (1000 <= hdr["rate"] <= 192000 and 0 < hdr["data_size"]
                  and hdr["data_size"] <= avail + HEADER_SLACK)
            hdr["offset"] = off
            hdr["valid"] = ok
            hdr["bytes"] = min(hdr["data_size"], max(avail, 0))
            hdr["seconds"] = round(hdr["bytes"] // FRAME_BYTES * SAMPLES_PER_FRAME / float(hdr["rate"]), 3) if hdr["rate"] else 0
            yield hdr


def cmd_scan(args):
    if not os.path.isfile(args.file):
        raise ToolError("%s is not a file" % args.file)
    found = [h for h in _scan_hits(args.file) if h["valid"] or args.all]
    if args.extract:
        os.makedirs(args.extract, exist_ok=True)
        with open(args.file, "rb") as fh:
            for i, h in enumerate(found):
                if not h["valid"]:
                    continue
                fh.seek(h["offset"])
                blob = fh.read(HEADER_SIZE + h["bytes"])
                safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in h["name"])[:24] or "vag"
                h["extracted"] = os.path.join(args.extract, "%03d_%08x_%s.vag" % (i, h["offset"], safe))
                with open(h["extracted"], "wb") as out:
                    out.write(blob)
    if args.json:
        print(json.dumps(found, indent=2))
    else:
        if not found:
            print("no VAGp headers found in %s" % args.file)
        else:
            print("%-12s %-5s %-8s %-10s %-9s %s" % ("offset", "magic", "rate", "bytes", "seconds", "name"))
            for h in found:
                print("0x%-10x %-5s %-8d %-10d %-9.3f %s%s" % (
                    h["offset"], h["magic"], h["rate"], h["bytes"], h["seconds"], h["name"],
                    "" if h["valid"] else "  (implausible header, skipped)"))
            print("%d VAG header(s)%s" % (len(found), (", extracted to %s" % args.extract) if args.extract else ""))
    return 0


def split_segments(buf, start=0, end=None):
    """Split raw ADPCM (a .bd style bank) into sounds at end flags."""
    if end is None:
        end = len(buf)
    segs = []
    seg_start = start
    pos = start
    while pos + FRAME_BYTES <= end:
        flags = buf[pos + 1]
        if (flags & FLAG_MARKER) == FLAG_MARKER:
            if pos > seg_start:
                segs.append((seg_start, pos))
            seg_start = pos + FRAME_BYTES
        elif flags & FLAG_END:
            segs.append((seg_start, pos + FRAME_BYTES))
            seg_start = pos + FRAME_BYTES
        pos += FRAME_BYTES
    if seg_start < pos:
        segs.append((seg_start, pos))
    return [(a, b) for a, b in segs if any(buf[a:b])]


def cmd_split(args):
    buf = _read(args.file)
    if args.offset < 0 or args.offset >= len(buf):
        raise ToolError("offset 0x%x is outside the file" % args.offset)
    if parse_header(buf, args.offset) is not None:
        raise ToolError("this file has a VAGp header: use towav (or scan --extract for several)")
    segs = split_segments(buf, args.offset)
    os.makedirs(args.out, exist_ok=True)
    stem = os.path.splitext(os.path.basename(args.file))[0]
    index = []
    bad_total = 0
    for i, (a, b) in enumerate(segs):
        samples, info = decode_adpcm(buf, a, b, stop_at_end=True)
        bad_total += info["bad_frames"]
        loop = (info["loop_start"], info["loop_end"]) if info["loop_end"] is not None and info["loop_start"] is not None else None
        wav = os.path.join(args.out, "%s_%03d.wav" % (stem, i))
        write_wav(wav, [samples], args.rate, loop)
        row = {"index": i, "offset": a, "bytes": b - a, "samples": len(samples),
               "seconds": round(_secs(len(samples), args.rate), 3), "loops": loop is not None,
               "wav": wav, "bad_frames": info["bad_frames"]}
        if args.vag:
            vag = os.path.join(args.out, "%s_%03d.vag" % (stem, i))
            with open(vag, "wb") as fh:
                fh.write(make_header(b - a, args.rate, "%s_%03d" % (stem[:11], i)) + buf[a:b])
            row["vag"] = vag
        index.append(row)
    with open(os.path.join(args.out, "split.json"), "w") as fh:
        json.dump({"source": os.path.basename(args.file), "rate": args.rate, "sounds": index}, fh, indent=2)
    if args.json:
        print(json.dumps(index, indent=2))
    else:
        for r in index:
            print("%3d  0x%08x  %8d bytes  %7.3f s%s" % (r["index"], r["offset"], r["bytes"], r["seconds"],
                                                         "  loops" if r["loops"] else ""))
        print("%d sound(s) written to %s (heuristic split at end flags; the bank's .hd decides the real list)"
              % (len(index), args.out))
    if bad_total:
        print("WARNING: %d frames had an out-of-range filter or shift: this may not be raw ADPCM" % bad_total)
        return 1
    return 0


# ---------------------------------------------------------------- CLI

def build_parser():
    p = argparse.ArgumentParser(
        prog="vag.py",
        description="PS2 SPU2 ADPCM (VAG) audio: decode to WAV, encode your own WAV to VAG, find VAGs in bigger files.",
        epilog="Rip only from discs you own; never publish ripped game audio. Outputs go under console/audio/ by default.")
    sub = p.add_subparsers(dest="cmd", metavar="command")
    sub.required = True

    s = sub.add_parser("info", help="header, rate, length, loop points of a VAG (or raw ADPCM with --rate)")
    s.add_argument("file")
    s.add_argument("--offset", type=_int, default=0, help="byte offset of the VAG or raw data (default 0)")
    s.add_argument("--rate", type=_int, default=0, help="sample rate (required for raw ADPCM)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("towav", help="decode a VAG (or raw ADPCM) to a 16-bit WAV")
    s.add_argument("file")
    s.add_argument("--out", help="WAV path (default console/audio/<name>.wav)")
    s.add_argument("--rate", type=_int, default=0, help="sample rate: overrides the header, required for raw ADPCM")
    s.add_argument("--offset", type=_int, default=0, help="byte offset of the VAG or raw data")
    s.add_argument("--channels", type=_int, default=1, help="2 for interleaved stereo")
    s.add_argument("--interleave", type=_int, default=0, help="bytes per channel block for stereo (multiple of 16)")
    s.add_argument("--all", action="store_true", help="decode every frame, ignoring end flags")
    s.add_argument("--no-loop-chunk", action="store_true", help="do not write loop points into a smpl chunk")
    s.set_defaults(func=cmd_towav)

    s = sub.add_parser("fromwav", help="encode a 16-bit PCM WAV to a VAG")
    s.add_argument("wav")
    s.add_argument("--out", help="VAG path (default console/audio/<name>.vag)")
    s.add_argument("--rate", type=_int, default=0, help="resample to this rate first")
    s.add_argument("--like", help="the original VAG you are replacing: copy its rate, header and looping, refuse if bigger")
    s.add_argument("--allow-bigger", action="store_true", help="with --like, write even if bigger than the original")
    s.add_argument("--loop", action="store_true", help="loop the whole sound")
    s.add_argument("--loop-start", type=_int, default=None, help="loop back to this sample (implies a loop)")
    s.add_argument("--no-loop", action="store_true", help="never loop, even if the WAV has a smpl loop")
    s.add_argument("--mono", action="store_true", help="mix stereo down to one channel")
    s.add_argument("--stereo", choices=("split", "interleave"), default=None,
                   help="stereo input: two mono files (default) or one interleaved file")
    s.add_argument("--interleave", type=_int, default=0, help="bytes per channel block for --stereo interleave")
    s.add_argument("--name", default=None, help="name stored in the header (16 characters)")
    s.add_argument("--no-lead-frame", action="store_true", help="omit the silent first frame Sony tools write")
    s.add_argument("--exhaustive", action="store_true", help="try all 13 shifts for every filter (slower)")
    s.set_defaults(func=cmd_fromwav)

    s = sub.add_parser("scan", help="find VAGp headers inside a bigger file (archive, bank, disc file)")
    s.add_argument("file")
    s.add_argument("--extract", metavar="DIR", help="write each VAG found to DIR")
    s.add_argument("--all", action="store_true", help="also list implausible headers")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("split", help="split headerless ADPCM (a .bd sound bank) into WAVs at end flags (heuristic)")
    s.add_argument("file")
    s.add_argument("--rate", type=_int, required=True, help="sample rate to write (banks do not store it in the .bd)")
    s.add_argument("--out", required=True, help="folder for the WAVs and split.json")
    s.add_argument("--offset", type=_int, default=0)
    s.add_argument("--vag", action="store_true", help="also write each sound as a VAG with a header")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_split)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ToolError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
