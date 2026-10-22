---
name: console-sound
description: >-
  Sound Designer. Rips the music and sound effects and puts your own tracks back in. Decodes PS2 VAG
  files and raw SPU2 ADPCM from sound banks (.bd) to WAV, finds VAGs hidden inside archives and disc
  files, and encodes your own 16-bit WAV back to VAG at the original's sample rate, looping and size.
  Everything it writes goes under console/audio/; it refuses a replacement bigger than the original
  unless told otherwise, and never writes into the disc folder or the PS2Recomp checkout. Use when the
  user says "rip the music", "extract the sound effects", "put my song in the game", "replace the
  soundtrack", "convert this VAG", "what is in this .bd", "the music loops wrong", or after
  /console-levels finds audio files on the disc.
argument-hint: "info|towav|fromwav|scan|split <file> [flags]"
---

# console-sound

The Sound Designer gets a game's audio out as WAV files you can listen to, and puts your own sounds
back in the same format. PS2 audio is Sony's SPU2 ADPCM: 16-byte frames of 28 samples each. It lives in
`.vag` files (a 48-byte `VAGp` header, then the frames), in sound banks (a `.hd` header plus a `.bd` of
headerless frames) and in game-specific streams. `vag.py` decodes exactly the way the PS2Recomp runtime
does (`ps2xRuntime/src/lib/ps2_audio_vag.cpp`), so what you hear is what the port plays.

Honest limits. The port's audio is high level: `ps2xRuntime/src/lib/ps2_audio.cpp` decodes the VAGp
blocks a game transfers and plays them through raylib when the game sends libsd voice commands. It is
not a full SPU2, so loop flags, reverb, envelopes and music streamed through a game's own driver may not
play in the port yet (that is /console-backend work). Banks have no rate in the `.bd`; it comes from the
`.hd` or from your ears. Movies (PSS) are out of scope.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/vag.py" info  console/disc/SOUND/BGM01.VAG          # rate, length, loop points, header
python3 "${CLAUDE_SKILL_DIR}/vag.py" towav console/disc/SOUND/BGM01.VAG          # -> console/audio/BGM01.wav
python3 "${CLAUDE_SKILL_DIR}/vag.py" scan  console/disc/DATA/SOUND.PAK --extract console/audio/pak
python3 "${CLAUDE_SKILL_DIR}/vag.py" split console/disc/SOUND/SE.BD --rate 22050 --out console/audio/se
python3 "${CLAUDE_SKILL_DIR}/vag.py" towav console/disc/SOUND/STREAM.BIN --rate 48000 --channels 2 --interleave 0x800
python3 "${CLAUDE_SKILL_DIR}/vag.py" fromwav my_song.wav --like console/disc/SOUND/BGM01.VAG --out console/audio/BGM01.VAG
```

Other flags: `--offset N` (a VAG or raw data inside a bigger file), `--all` (decode past end flags),
`--json`. For `fromwav`: `--loop`, `--loop-start N`, `--no-loop`, `--rate N`, `--mono`,
`--stereo split|interleave --interleave N`, `--allow-bigger`, `--exhaustive`, `--no-lead-frame`,
`--name`. Exit codes: 0 ok, 1 needs attention (odd frames, a replacement too big), 2 bad input.

## What it reads and writes

| file | what |
| --- | --- |
| `*.vag` | VAGp header + ADPCM frames: read by `info`/`towav`, written by `fromwav` |
| `*.bd` (+ `*.hd`) | a sound bank. `split` cuts the `.bd` at end flags (a heuristic); the `.hd` (starts `IECSsreV`) holds the real list |
| any big file | `scan` finds VAGp headers inside it; `--extract` writes each one out |
| `*.wav` | 16-bit PCM, mono or stereo, in and out. Loop points travel in a `smpl` chunk both ways |
| `console/audio/` | the default output folder |
| `console/audio/<bank>/split.json` | offset, size and loop of every sound `split` found |

Format details, the encoder and what is unverified: [reference/vag-format.md](reference/vag-format.md).
Where audio lives on a PS2 disc and how the port plays it: [reference/game-audio.md](reference/game-audio.md).

## The method

1. **Find the audio.** Map the extracted disc with /console-levels
   (`datascan.py scan <disc folder> --embedded`). Look for kind `VAG`, `IECS` (a bank header), `MPEG-PS`
   (movies) and big high-entropy `data` files (streams or archives). Run `vag.py scan` on archives to
   find VAGs inside them. `datascan.py files <run log>` shows which files the port looked up, in order.
2. **Rip it.** `towav` for a VAG, `scan --extract` for VAGs inside a file, `split --rate N` for a `.bd`
   bank. A bank's rate is in its `.hd`; if you cannot read it, try 22050, then 44100, 32000 and 48000
   and pick the one that sounds right. For a raw stereo stream try `--channels 2` with interleave sizes
   such as 0x800, 0x1000 and 0x4000 until it sounds clean. A `WARNING ... out-of-range filter or shift`
   means a wrong offset or not ADPCM at all.
3. **Make the replacement.** Your own recording, or music you have the rights to. Export a 16-bit PCM
   WAV. Shorter than the original is safest. Set loop points in your audio editor (they go in the
   WAV's `smpl` chunk) or pass `--loop` / `--loop-start N`.
4. **Encode against the original.** `fromwav mine.wav --like ORIGINAL.VAG --out console/audio/NAME.VAG`
   copies the original's rate (resampling linearly if needed), header version and name, and looping,
   and refuses if the result is bigger: games often load sounds into fixed slots in the SPU2's 2 MB of
   sound RAM. Read the quality line: above about 25 dB signal to noise is clean. A loop length that is
   not a multiple of 28 samples gets a short gap at the loop point; the tool says so.
5. **Swap it in.** Close the port first. It reads `cdrom0:\` from the folder the game's ELF is in (`cdRoot`, set by
   `PS2Runtime::configureIoPathsFromElf`). Copy the original aside first (`BGM01.VAG.orig`), then copy
   your file over it with the same name. For a sound inside an archive, use /console-levels `unpack`,
   replace the entry, `repack`. The runtime looks disc files up by name and gives each its real host
   size, so a new size is fine for files the game finds by name; a game that reads fixed sector numbers
   needs the exact original size.
6. **Playtest.** Run the port with /console-tester. If your sound is silent, check whether the
   original plays in the port. If neither does, the gap is the runtime's audio, not your file: hand it
   to /console-backend with the table below.
7. **Write it down.** One line in the session log of `console/STATE.md`: which file, where the original
   is backed up, the rate and size.

## Where audio fixes go in PS2Recomp

| symptom | layer | where |
| --- | --- | --- |
| a VAG plays wrong in the port but right in `towav` | runtime decoder | `ps2xRuntime/src/lib/ps2_audio_vag.cpp` |
| no sound at all, game sends libsd commands | runtime audio backend | `ps2xRuntime/src/lib/ps2_audio.cpp` (`PS2AudioBackend::onSoundCommand`, libsd SID `0x80000701`) |
| sound RPCs never reach the backend | IOP module HLE | `ps2xIOP/src/modules/libsd.cpp` |
| an audio library call logs `Unimplemented PS2 stub` | kernel stubs | `ps2xRuntime/src/lib/Kernel/Stubs/Audio.cpp` |
| your file is right but the game ignores it | data | wrong file swapped, or the sound lives in an archive: /console-levels |

Never edit headers or `ps2xRuntime/src/runner/` for audio: a header change rebuilds every generated
file. Runtime `.cpp` changes are /console-backend's job.

## Guard rails

- **Bring your own game.** Rip only from discs you own and dumped yourself (ImgBurn on Windows, `dd`
  from a DVD drive on macOS or Linux). Never download ISOs, ELFs or BIOS files, never help find them,
  never ask the user for them. PS2Recomp needs no BIOS.
- **Nothing from the game gets published.** Ripped music, voices and effects stay on your machine, like
  the disc, the ELF, the IRX modules and the recompiled C++. Share your own tracks, the steps and
  patches, never the game's audio or a modified disc.
- **Your replacement must be yours.** Use music you made or have the rights to use.
- **Back up before you swap.** Keep the original next to it (`.orig`) or in `console/`, and never write
  over your only copy of a disc file. This tool never writes into the disc folder itself.
- **Not affiliated with Sony.** PlayStation is a trademark of Sony Interactive Entertainment.

## Hands off to

- /console-levels to unpack and repack archives that hold sounds, and to find which files a level loads.
- /console-tester to playtest the swap and check nothing else broke.
- /console-backend when the port itself does not play audio (runtime decoder, audio backend, libsd).
- /console-mod for behaviour changes around sound (a new trigger, a volume change in code).
