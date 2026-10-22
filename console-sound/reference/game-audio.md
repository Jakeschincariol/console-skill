# Where PS2 game audio lives, and how the port plays it

## On the disc

| what you find | what it is | tool |
| --- | --- | --- |
| `*.VAG`, files starting `VAGp` | one sound or one music track, ADPCM with a header | `vag.py towav` |
| `*.HD` + `*.BD` (+ `*.SQ`) | a Sony sound bank: `.hd` header (starts `IECSsreV`), `.bd` raw ADPCM, `.sq` a MIDI-like sequence that plays notes from the bank | `vag.py split` on the `.bd` |
| big files of high entropy, often in a `SOUND` or `BGM` folder | streamed music in a game-specific container, usually raw interleaved ADPCM, sometimes with a small header per block | `vag.py towav --offset --rate --channels 2 --interleave` by trial |
| `*.PSS` (starts `00 00 01 BA`) | a movie: MPEG-2 video plus ADPCM or PCM audio in an MPEG program stream | out of scope |
| VAGs inside archives (AFS, PAK, BIN) | effects packed with a level | `vag.py scan --extract`, or /console-levels unpack |

`datascan.py scan --embedded` (from /console-levels) labels these kinds for a whole folder and counts
VAG headers inside big files.

## Numbers that matter

- The SPU2 has 2 MB of sound RAM for every loaded sample at once. A game loads a bank per level or
  scene into fixed regions, so a replacement that is bigger than the original can overwrite the next
  sound. `fromwav --like` refuses that unless you pass `--allow-bigger`.
- Common sample rates: 48000 (the SPU2's output rate), 44100, 32000, 24000, 22050, 11025. The game
  usually sets the pitch itself, so a replacement must use the original's rate or it plays too fast
  or too slow.
- ADPCM is 3.5:1 against 16-bit PCM: 16 bytes hold 28 samples. One minute of mono 44.1 kHz is about
  1.5 MB.

## How the PS2Recomp port plays sound (commit eb8688b)

- The game's sound driver talks to the IOP's libsd over SIF RPC. `ps2xIOP/src/modules/libsd.cpp`
  answers SID `0x80000701` and forwards each request to the host's audio backend.
- `ps2xRuntime/src/lib/ps2_audio.cpp` (`PS2AudioBackend`) decodes VAGp blocks as the game transfers
  them (`onVagTransfer`, `onVagTransferFromBuffer`, keyed by address) and, on a voice command
  (`LIBSD_CMD_SET_VOICE` `0x8010` or the `0x81xx` family), plays the matching sample through raylib
  (`LoadSoundFromWave`, `PlaySound`) with the pitch it reads from the command.
- The decoder (`ps2_audio_vag.cpp`) needs a `VAGp` header. Headerless bank data, loops, envelopes,
  reverb and streams decoded by a game's own code are not emulated the way an SPU2 would; expect
  partial audio in early ports. That is a runtime gap for /console-backend, not a problem with your
  file.
- Disc files come from the folder the ELF is in (`cdRoot`), looked up by name, case-insensitively if
  the exact case does not exist. The runtime caches each lookup for the session, so swap files while
  the port is closed; the new size is used from the next start.

## Replacing a sound, by container

| container | how |
| --- | --- |
| a loose `.VAG` | `fromwav --like ORIGINAL.VAG`, back up the original, copy yours over it in the extracted disc folder |
| a VAG inside an archive | /console-levels `unpack`, swap the entry with your `fromwav --like` output, `repack` |
| a sound in a `.bd` bank | write your frames over the old ones at the same offset, same size or smaller (pad with silent 16-byte frames, end flag on your last frame); do it with a small script and keep the original. The `.hd` keeps pointing at the same offset |
| streamed music | match the original's rate, channels and interleave exactly; keep the total size unless you also change the game's stream table |

Test the result in the port and, where you can, compare with the original in PCSX2 to tell a bad file
from a runtime gap.

## Rights

Your replacement has to be audio you made or are allowed to use. Ripped game audio is for your own
listening and editing on your own machine; it is never published, uploaded or shared.
