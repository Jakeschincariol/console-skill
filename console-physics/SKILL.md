---
name: console-physics
description: >-
  Physics Tuner. Finds the numbers that decide how the game moves (speed,
  gravity, grip, jump height, frame rate) and tunes them. Scans the user's own
  game ELF for a float32 value stored in data or built in code (lui, ori or
  addiu, then mtc1), shows the instructions that read it, lists frame-timing
  candidates, and prints the exact pnach lines to test a new value in PCSX2
  plus the PS2Recomp fix (TOML instruction patches or the data write
  /console-mod bakes in). Reads only; writes nothing. Use when the user says
  "make the car faster", "lower the gravity", "jump higher", "more grip",
  "change the damage", "wider camera", "unlock 60 fps", "find the speed value",
  or when /console-mod needs a number found.
argument-hint: "scan <ELF> --value N | fps <ELF> | patch <ELF> --addr 0x.. --float N"
---

# console-physics

The Physics Tuner finds the constants behind how a game feels and changes
them. PS2 games do their physics in single-precision floats (the EE's FPU has
no doubles), so a top speed, a gravity or a jump impulse is a 32-bit float
somewhere: in the ELF's data, or built straight into the code by two
instructions. The tool finds the candidates; PCSX2 tells you which one is the
real one; `/console-mod` builds the winner into the port.

Honest limits: a value the game computes while it runs (from a car's stats
file, a level script, a menu setting) is not in the ELF. Then you find it in
RAM while playing and change the code or the file that sets it. Units are
rarely obvious: 9.8 may be stored as 980, 0.163 (per frame) or 588 (per
second squared at 60 fps). And frame-rate unlocks are often more than one
number.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/tune.py" scan SLUS_203.12 --value 9.8              # data floats + lui/ori/mtc1 constants
python3 "${CLAUDE_SKILL_DIR}/tune.py" scan SLUS_203.12 --value 9.8 --tol 0.05   # a wider net
python3 "${CLAUDE_SKILL_DIR}/tune.py" fps SLUS_203.12                           # 1/60, 1/30, 60, 30, PAL 50/25
python3 "${CLAUDE_SKILL_DIR}/tune.py" patch SLUS_203.12 --addr 0x0034A2C0 --float 12.5
```

Flags: `--json` on all three, `--limit N`, `--place 0|1|2` (pnach place; the
default is 0 for data, 1 for code), `--exact` (refuse rather than round when a
lone `lui` cannot hold the new value).

## What it reads and writes

| file | what |
| --- | --- |
| the game's main ELF | read only: data words, code, sections, `$gp` |
| stdout | hits, the instructions around them, pnach lines, TOML blocks, the override write |
| nothing else | the user (or `/console-mod`) puts the lines where they belong |

## How a float hides in a PS2 ELF

| form | looks like | `scan` reports | `patch` prints |
| --- | --- | --- | --- |
| a word in data (`.data`, `.rodata`, `.lit4`, `.sdata`) | `0x411CCCCD` = 9.8 | DATA, plus the `lwc1`/`lw` that read it (through `lui` or `$gp`) | one pnach line and the override write `/console-mod` bakes in at load |
| built in code | `lui $v0, 0x411C` + `ori $v0, $v0, 0xCCCD` + `mtc1 $v0, $f12` | CODE with the instructions around it | two pnach lines (new lui and ori words) and two TOML instruction patches |
| `lui` + `addiu` | the low half is sign-extended | CODE | the high half is adjusted for the carry |
| a lone `lui` | values with a zero low half (1.0, 0.5, 12.5, 60.0) | CODE | one word; a value needing a low half is rounded to the nearest one it can hold (said plainly, exit 1) |
| set at run time | zero-filled BSS, heap | nothing in the ELF | a place 1 pnach line and the `/console-mod` frame-hook route |

Float encodings, unit tricks and the Ghidra and PCSX2 steps are in
[reference/float-hunting.md](reference/float-hunting.md).

## The method

1. **Name the feel and guess the number.** "The car is too slow": top speed,
   acceleration, a drag factor. Ask what the game shows (a speedometer, a
   jump that clears two crates). Start from a value you can see.
2. **Scan.** `tune.py scan <ELF> --value N`. Too many hits: narrow by section
   (physics constants live in `.rodata`/`.lit4` or in code next to `mtc1`),
   by the readers (one `lwc1` in a function Ghidra calls `UpdateCar` beats 40
   readers), or by a second related value nearby. None: widen `--tol`, try
   other units, or go to step 3.
3. **Find it while playing** (when the scan cannot): PCSX2's debugger memory
   search for the float, change it in game (jump, brake), search again; then a
   write breakpoint on the address shows the code that computes it, and that
   code's constants are what you tune. With GhydraMCP (optional, see
   `/console-reverse`) ask for the function's xrefs and decompile it.
4. **Change it in PCSX2 first.** `tune.py patch <ELF> --addr ... --float ...`
   prints the pnach lines. Put them in the game's pnach (`/console-mod`
   `pnach.py crc` names the file), enable, play. One value at a time, write
   down what changed.
5. **Bake it in** with `/console-mod`: code constants go in the game's TOML
   (`patch` prints the `[[patches.instructions]]` blocks; re-run `ps2_recomp`),
   data constants go in a game override (`pnach.py override` on the pnach, or
   paste the printed `Ps2FastWrite32` line into an `override.py` module).
6. **Playtest** with `/console-tester`, then play the part of the game it
   touches. A shared constant (`patch` warns when several instructions read
   it) changes everything that reads it.

## Typical targets

| feel | look for | note |
| --- | --- | --- |
| top speed, acceleration | the max-speed clamp and the per-frame add | often per frame: divide km/h by the frame rate |
| gravity, jump height | a negative per-frame velocity add, the jump impulse | jump height grows with the square of the impulse |
| grip, friction | a multiplier just under 1.0 (0.95, 0.98) applied every frame | small changes are large |
| damage | integers more often than floats: find them in RAM, see `/console-mod` | tables live in data |
| camera FOV | a float near 0.75 to 1.3, or a focal length near 1.0 to 2.0 | `/console-graphics` `widescreen.py` scans the aspect ones |
| frame rate | `tune.py fps`; the vsync wait count in the main loop | read the caveat below |

**Frame rate, plainly:** `fps` lists candidates, not answers. Many games pace
frames with a vsync count (wait 2 vsyncs for 30 fps), an integer the scan
cannot see. A 60 fps unlock also needs the game's logic to scale by the frame
time: if movement, physics or animation are counted per frame, doubling the
frame rate doubles the game speed. Test in PCSX2 first.

## Guard rails

1. **Bring your own game.** Only a game the user owns, dumped from their own
   disc. Never download or look for ISOs, ELFs or BIOS files, never ask for
   them. PS2Recomp needs no BIOS.
2. **Nothing from the game gets published.** Share the pnach lines, TOML
   patches and override files. Never the ELF, the disc or the generated
   `runner/*.cpp`.
3. **Not affiliated with Sony.** PlayStation is a trademark of Sony
   Interactive Entertainment.
4. The tool only reads. It never edits the ELF, the TOML or the runtime: the
   user decides what gets applied, PCSX2 first. Change one value per test.

## Hands off to

- `/console-mod` to bake a tested value into the port (TOML or override).
- `/console-graphics` for camera aspect and widescreen.
- `/console-tester` to playtest the change and check nothing else broke.
