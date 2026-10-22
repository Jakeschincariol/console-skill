---
name: console-controller
description: >-
  Controller Mapper. Hooks the game up to any controller: DualSense, Xbox, Switch Pro, keyboard or
  touch, remapped how you like. Reads a mapping in console/controls.json (a layout preset plus
  per-button overrides, radial deadzone, invert Y, stick swap, analog trigger threshold, keyboard
  keys, two players, Android touch zones) and writes console/ps2_pad.cpp, a drop-in replacement for
  PS2Recomp's ps2xRuntime/src/lib/ps2_pad.cpp with the same class, signature and DualShock 2 report.
  Optional input replay and recording for scripted test runs. Backs up the stock file before
  installing and only touches the PS2Recomp checkout you name. Use when the user says "my controller
  doesn't work", "remap the buttons", "use my Xbox pad", "Switch Pro buttons are swapped", "invert
  the camera", "keyboard controls", "two players", "touch controls on Android", or when
  /console-tester needs scripted input.
argument-hint: "init|check|gen|install|restore|presets|table [flags]"
---

# console-controller

The recompiled game asks the runtime for the pad state many times a second through
`PSPadBackend::readState(int port, int slot, uint8_t *data, size_t size)` in
`ps2xRuntime/src/lib/ps2_pad.cpp`. The stock file maps raylib's gamepad 0 and a fixed keyboard
layout onto the 32-byte DualShock 2 report. This skill replaces that one file with a generated one
built from your `console/controls.json`, so any controller raylib can see becomes a DualShock 2,
mapped your way. Nothing else in the runtime changes, so the rebuild is incremental.

Honest limits: rumble is not wired in the runtime, pressure-sensitive buttons are reported as fully
pressed or not pressed, and touch zones on Android are invisible (the runtime draws no overlay).

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/padmap.py" init --layout xbox          # writes console/controls.json
python3 "${CLAUDE_SKILL_DIR}/padmap.py" check                       # every DS2 button bound, no clashes
python3 "${CLAUDE_SKILL_DIR}/padmap.py" gen --replay                # writes console/ps2_pad.cpp
python3 "${CLAUDE_SKILL_DIR}/padmap.py" install --repo ~/PS2Recomp  # backs up, then copies it in
python3 "${CLAUDE_SKILL_DIR}/padmap.py" restore --repo ~/PS2Recomp  # puts the stock file back
python3 "${CLAUDE_SKILL_DIR}/padmap.py" presets switch-labels       # playstation xbox switch switch-labels keyboard
python3 "${CLAUDE_SKILL_DIR}/padmap.py" table                       # the DS2 bit table
python3 "${CLAUDE_SKILL_DIR}/padmap.py" check --script console/inputs/menu.txt   # validate a replay script
```

Flags: `--config FILE` (default `console/controls.json`; the preset alone is used if it is missing),
`--layout NAME`, `--out FILE` (`-` for stdout), `--json`, `--strict` (warnings fail `check`),
`--backups DIR` (default `console/backups`). Exit codes: 0 ok, 1 `check` found a problem, 2 bad input.

## What it reads and writes

| file | what |
| --- | --- |
| `console/controls.json` | your mapping: `layout` + overrides. Full format: [reference/controls-json.md](reference/controls-json.md) |
| `console/ps2_pad.cpp` | the generated drop-in pad file (from `gen`) |
| `<PS2Recomp>/ps2xRuntime/src/lib/ps2_pad.cpp` | replaced by `install`, only when you pass `--repo` |
| `console/backups/ps2_pad.cpp.orig` | the stock file, saved once and never overwritten; later installs also keep a timestamped copy |
| `templates/menu-walk.txt` | an example input replay script to copy and edit |

## The DualShock 2 report the file fills

`data[0] = 0x01`, `data[1] = 0x73` (analog), buttons ACTIVE-LOW in `data[2]` (low byte) and
`data[3]` (high byte), so `0xFFFF` means nothing pressed. Sticks: `data[4]` RX, `data[5]` RY,
`data[6]` LX, `data[7]` LY, `0x80` centre, `0x00` full left or up. Bits: SELECT 0x0001, L3 0x0002,
R3 0x0004, START 0x0008, UP 0x0010, RIGHT 0x0020, DOWN 0x0040, LEFT 0x0080, L2 0x0100, R2 0x0200,
L1 0x0400, R1 0x0800, TRIANGLE 0x1000, CIRCLE 0x2000, CROSS 0x4000, SQUARE 0x8000.
`Kernel/Stubs/Pad.cpp` copies bytes 2 to 7 into the game's pad buffer, sets the mode byte itself
(digital 0x41 until the game switches the pad to analog) and fills the pressure bytes as 0xFF or 0.

## Method

1. **Find out what they play with.** Controller model, how it connects (USB or Bluetooth), which
   OS, one or two players, and whether they want keyboard or touch. If a button already lands in
   the wrong place, ask which physical button they pressed and what the game did.
2. **Start from a preset.** `init --layout playstation|xbox|switch|switch-labels|keyboard`.
   raylib reports pads by POSITION: the bottom face button is Cross on every pad (Xbox A, Nintendo
   B). `switch-labels` matches Nintendo's printed letters instead (A, on the right, is Cross).
   Details and why some Switch Pro setups arrive already swapped: [reference/controllers.md](reference/controllers.md).
3. **Apply the user's changes** in `controls.json`: `buttons` overrides, `keyboard` keys,
   `deadzone` (radial, default 0.15), `invert_y` (`true` means the right stick, the camera in most
   games), `stick_swap`, `trigger_threshold` (reads L2/R2 from the analog trigger axes), `players: 2`
   (port 2 gets the second pad), `touch` zones for Android. Keep it a mapping, never game code.
4. **Check.** `padmap.py check`. Fix every ERROR. Read the warnings out: Escape quits the game
   (raylib's default exit key; the runtime never changes it) and F1 opens the debug panel on
   desktop builds with the debug UI.
5. **Generate.** `padmap.py gen` (add `--replay` when /console-tester will drive scripted runs).
6. **Install.** `padmap.py install --repo <their PS2Recomp checkout>` backs the stock file up to
   `console/backups/ps2_pad.cpp.orig` and copies the new one in. Then the normal incremental build
   (`cmake --build <build dir>`, the same command recorded in `console/STATE.md`). One `.cpp`
   changes and no header does, so it is quick. Never clean the build for this.
7. **Test with the controller in hand.** Run the port, press every button once in a menu that
   reacts to all of them (or a game's controller test screen). If one is wrong, change
   `controls.json`, `gen`, `install`, rebuild. Log the result in `console/STATE.md`.
8. **Scripted input for tests** (with `--replay`): set `PS2X_INPUT_RECORD=console/inputs/run1.txt`,
   play the bit you want to repeat, quit; that file replays with `PS2X_INPUT_SCRIPT` (which
   `/console-tester`'s `run.py --input` sets for you). The format is in
   [reference/controls-json.md](reference/controls-json.md). Frame numbers count the game's reads of
   pad port 0, so a script stays in step with the game, not with the wall clock.

## Rumble, pressure, analog, touch

| feature | status in this PS2Recomp checkout |
| --- | --- |
| Rumble | not wired: `scePadSetActDirect` and `scePadSetActAlign` in `Kernel/Stubs/Pad.cpp` return 1 and do nothing (`scePadInfoAct` reports two motors). Adding it is runtime C++ work for /console-backend, not a mapping |
| Pressure-sensitive buttons | digital: when a game turns pressure mode on, `Pad.cpp` reports 0xFF for a pressed button and 0 otherwise |
| Analog mode | the game switches it (`scePadSetMainMode`); sticks are always sent and the game ignores them in digital mode |
| Touch (Android) | `ps2_android_runtime.cpp` is only a TODO for an on-screen overlay, so the stock port has no touch controls. `touch` zones work as invisible areas; a Bluetooth controller is the better answer |
| Two players | the stock file feeds the same pad to both ports; the generated one gives port 2 its own pad with `players: 2`, or a released pad otherwise |

## Guard rails

- **Bring your own game.** Only games you own, dumped from your own disc. Never download ISOs, ELFs
  or BIOS files, never help find them. PS2Recomp needs no BIOS.
- **Nothing from the game gets published.** The disc, ELF, IRX modules, assets and the recompiled
  `runner/*.cpp` stay on your machine. A `controls.json`, a generated `ps2_pad.cpp` and replay
  scripts you wrote are yours to share.
- **Not affiliated with Sony.** PlayStation and DualShock are trademarks of Sony Interactive
  Entertainment. DualSense, Xbox and Nintendo Switch belong to their owners.
- **Back up, then replace one file.** `install` refuses a source it did not generate, never
  overwrites the `.orig` backup, and touches only the checkout you name. Never edit headers
  (`ps2_pad.h` included: a header change rebuilds every generated file) and never clean the build.

## Hands off to

- `/console-tester` to run the port with a replay script (`run.py boot --input ...`).
- `/console-anywhere` to build for the device the controller is for (Steam Deck, Android, Vita).
- `/console-backend` if the game needs rumble or a pad call the runtime does not implement yet.
