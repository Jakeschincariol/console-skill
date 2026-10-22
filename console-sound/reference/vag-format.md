# VAG and SPU2 ADPCM: the format vag.py reads and writes

Facts here are in our own words. The decoder rules were checked against PS2Recomp
`ps2xRuntime/src/lib/ps2_audio_vag.cpp` (commit eb8688b). Items marked "unverified" come from community
knowledge and were not confirmed against a primary source.

## The ADPCM frame (16 bytes, 28 samples)

| byte | meaning |
| --- | --- |
| 0 | low nibble = shift (0..12), high nibble = filter (0..4) |
| 1 | flags: `0x04` loop start, `0x02` loop / repeat, `0x01` end; `0x07` on its own = end-of-data marker |
| 2..15 | 28 signed 4-bit samples, LOW nibble first |

Decoding one sample, with `s1` and `s2` the previous two output samples:

```
step   = 1 << (12 - shift)
pred   = (k1 * s1 + k2 * s2 + 32) / 64        # C integer division: truncates toward zero
sample = clamp(nibble * step + pred, -32768, 32767)
```

Filters `(k1, k2)`: `(0, 0)`, `(60, 0)`, `(115, -52)`, `(98, -55)`, `(122, -60)`.

Runtime details `vag.py` copies on purpose:

- Shift 13, 14 or 15 is played as 9. Filter 5, 6 or 7 is played as 0 (the filter field is masked to 3
  bits). `vag.py` counts such frames as "odd" and warns: real encoders never write them, so many odd
  frames mean the offset is wrong or the data is not ADPCM.
- The division truncates toward zero. Real hardware and PCSX2 shift right (round down); the difference
  is at most one step on negative predictions and is inaudible. `vag.py` matches the port.
- The runtime decodes every frame up to the header's data size and ignores the end and loop flags.
  `vag.py towav` stops at the first end flag (or the `0x07` marker) unless you pass `--all`.

## Flags and loops

| frames | one-shot sound | looping sound |
| --- | --- | --- |
| before the loop | `0x00` | `0x00` |
| first frame of the loop | | `0x06` (loop start + repeat) |
| inside the loop | `0x00` | `0x02` |
| last frame | `0x01` (end: the voice stops) | `0x03` (end + repeat: jump back to the loop start) |
| after the last frame | one `0x07` marker frame | none |

A loop always runs to the last frame, and the loop start must sit on a frame boundary. `fromwav`
therefore pads the start of the sound with silence so your loop start lands on a frame, and warns
when the loop length is not a multiple of 28 samples (the padding at the end would play as a short
gap at the loop point).

## Lead frame and end marker

Sony's tools start the data with one all-zero frame (silent; unverified as a hard rule but common) and
end a one-shot sound with a marker frame whose flag byte is `0x07`. Many files fill the marker's sample
bytes with `0x77`. `vag.py` writes the marker with ZERO sample bytes because the PS2Recomp runtime
decodes every frame including the marker, and `0x77` samples at shift 0 would play as a loud click.
`--no-lead-frame` drops the lead frame.

## The VAGp header (48 bytes, big-endian fields)

| offset | size | field |
| --- | --- | --- |
| 0x00 | 4 | magic `VAGp` (`VAGi` is seen on interleaved files; treated the same) |
| 0x04 | 4 | version (`vag.py` writes `0x20`; `--like` copies the original's) |
| 0x08 | 4 | reserved (0) |
| 0x0C | 4 | data size in bytes, after the header |
| 0x10 | 4 | sample rate in Hz (0 is played as 44100 by the runtime) |
| 0x14 | 12 | reserved |
| 0x20 | 16 | name, ASCII, NUL padded |
| 0x30 | | ADPCM frames |

Some tools count the header in the data size; `vag.py` accepts a size up to 48 bytes too large and says
so. A size larger than that is a truncated file and is refused (exit 2).

## Stereo

There is no stereo frame. Stereo is two mono streams interleaved in blocks: N bytes of left frames, N
bytes of right, and so on (N a multiple of 16). `towav --channels 2 --interleave N` splits them;
`fromwav` writes two mono files by default (`_L`, `_R`) or one interleaved file with `--stereo
interleave --interleave N`. The interleave size is per game and is not stored in the file: try values
until both channels sound clean.

## How the encoder chooses

For each frame of 28 samples `fromwav` tries every filter. For each filter it estimates the shift from
the largest prediction error, then encodes the frame closed-loop (exactly as the decoder will rebuild
it, with clamping) at that shift and its two neighbours, and keeps the combination with the smallest
squared error. `--exhaustive` tries all 13 shifts for every filter. Encoding stops early once a
candidate is already worse than the best, so even in pure Python it runs many times faster than
real time. After writing, it decodes its own output and prints the signal-to-noise ratio. Above about
25 dB is clean; a full-scale square wave scores lower because clamping is lossy.

## Resampling

`--rate N` and `--like` resample with straight linear interpolation. It is fine for effects and
speech; for music, resample in an audio editor first (to the original's rate, see `vag.py info`) and
let `fromwav` keep it as is.

## Sound banks (.hd / .bd)

A `.bd` is headerless ADPCM: many sounds back to back, each normally starting with a silent frame and
ending with an end flag. The matching `.hd` starts with `IECSsreV` and lists each sound's offset into
the `.bd`, its rate and its loop settings. `vag.py split` cuts the `.bd` at end flags, which finds most
one-shot sounds but cannot know the rates or the real list; reading the `.hd` chunk layout is per
case (unverified here, so this tool does not parse it). Treat split output as a way to hear what is in
the bank, and put replacements back at the same offsets and sizes.
