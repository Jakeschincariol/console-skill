---
name: console-anywhere
description: >-
  Play Anywhere. Builds the port for Windows, Mac, Linux, Steam Deck, Android and PS Vita and
  packages it without any game files inside. doctor checks this machine's tools for a target and
  prints the exact install command for anything missing; plan prints the build steps taken from
  PS2Recomp's own README, android/README.md and vita scripts; package makes a player folder in
  console/dist/ (the runner, its libraries, an empty game/ folder, PLAYERS.md, a launch script,
  .recomp.json) and refuses outright if a disc image, PS2 ELF, IRX module, recompiled runner source
  or other game data is inside; guard runs that check on any folder; recomp-json writes the
  project's optional .recomp.json. Use when the user says "put it on my Steam Deck", "build it for
  Android", "play it on my phone", "on my TV", "Vita build", "Mac build", "package it", "make a
  release folder", "is it safe to zip this", or after the port plays on the machine it was built on.
argument-hint: "doctor|plan|package|guard|recomp-json --target windows|macos|linux|steamdeck|android|vita|web"
---

# console-anywhere

A PS2Recomp port is a normal native program (`ps2EntryRunner`) built from the recompiled game code
plus the runtime, drawn with raylib. The same CMake project builds it on Windows, macOS (Apple
silicon through sse2neon), Linux and the Steam Deck; `android/` makes an APK and `vita/` makes a
vpk. This skill checks the toolchain, prints the right steps for each device, and packages the
result so the player brings the game from their own disc.

Honest limits: PS2Recomp says it is tested mainly with MSVC, so Mac, Linux, Android and Vita builds
get less testing; the Vita is far slower than a PC; Android has no on-screen controls yet. And the
runner is compiled from your game's recompiled code, so a package is for your own devices: it
never contains the disc, but it is still derived from the game, so it is not something to upload.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/targets.py" doctor                       # this machine's own OS
python3 "${CLAUDE_SKILL_DIR}/targets.py" doctor --target android --target vita   # or --target all
python3 "${CLAUDE_SKILL_DIR}/targets.py" plan --target steamdeck --elf-name SLUS_203.12
python3 "${CLAUDE_SKILL_DIR}/targets.py" package --runner out/release/ps2xRuntime/ps2EntryRunner \
        --target linux --elf-name SLUS_203.12 --game "My Game" --serial SLUS-20312 \
        --out console/dist/mygame-linux
python3 "${CLAUDE_SKILL_DIR}/targets.py" guard console/dist/mygame-linux   # before you zip or push anything
python3 "${CLAUDE_SKILL_DIR}/targets.py" recomp-json --game "My Game" --serial SLUS-20312 --status playable
```

Targets: `windows`, `macos`, `linux`, `steamdeck`, `android`, `vita`, and `web` (plan and doctor
only: a separate toolchain). `plan` takes `--repo` (your PS2Recomp checkout) and `--build-dir`.
`package` takes `--lib FILE` (extra libraries), `--allow FILE`, `--replace`, `--status`, `--json`.
Exit codes: 0 ok, 1 something missing or refused, 2 bad input.

## What it reads and writes

| file | what |
| --- | --- |
| your build output | `ps2EntryRunner` (+ `.dll` / `.so` / `.dylib` next to it), `app-release.apk`, or `ps2EntryRunner.vpk` |
| `console/dist/<name>/` | the player folder: runner, libraries, `game/` (empty), `PLAYERS.md`, `play.bat` / `play.command` / `play.sh` / `install-android.sh`, `.recomp.json`, `.console-dist.json` |
| `templates/PLAYERS.md` | the player instructions template |
| `.recomp.json` | optional project description read by recomp lists (PS2Recomp itself ignores it) |
| `console/STATE.md` | read the build folder and checkout path from it; log what was built |

## Method

1. **Ask where they want to play** and on what: a PC, a Mac, a Steam Deck, an Android phone,
   tablet or TV box, a homebrew Vita. "On the TV" means an Android TV box with a Bluetooth pad, or a
   PC or Deck plugged into the TV with a controller.
2. **The port must already run here.** Build it and check it with `/console-tester` on the machine
   you develop on first. A crash on the desktop will not get better on a phone.
3. **Doctor.** `targets.py doctor --target <t>`. Install what is MISSING with the printed command,
   then run doctor again.
4. **Plan.** `targets.py plan --target <t> --elf-name <the disc's ELF>`. Follow the steps in the
   user's checkout (the one holding their recompiled code). Per-target details and the source of
   each fact: [reference/targets.md](reference/targets.md).
5. **Build** with the plan's commands. Incremental only; never clean a build folder, never
   `CLEAN=1` for the Vita. A separate release folder (`out/release`) keeps the day-to-day build intact.
6. **Package.** `targets.py package --runner <built file> --target <t> --out console/dist/<name>`.
   Add `--elf-name` (else the launch script reads `BOOT2` from the player's `SYSTEM.CNF`) and
   `--game`/`--serial` for `.recomp.json`.
7. **Read the guard's verdict.** A refusal lists each file and why. Fix the cause (wrong `--lib`,
   a disc file next to the runner). Never add an allow line for a disc image, a PS2 ELF, an IRX,
   `SYSTEM.CNF` or runner sources: the allow file is only for a false positive that is truly the
   user's own file, with the reason written next to it. Details: [reference/guard.md](reference/guard.md).
8. **Install on the device** following `PLAYERS.md`, then the player dumps their own disc into
   `game/`. Test it there, and log the result in `console/STATE.md`.

## Per target in one line

| target | runner | how the ELF is found | notes |
| --- | --- | --- | --- |
| Windows | `ps2EntryRunner.exe` + FFmpeg DLLs | `play.bat` passes `game\<ELF>` | MSVC, the most tested path |
| macOS | `ps2EntryRunner` | `play.command` passes `game/<ELF>` | arm64 builds through sse2neon; needs Homebrew FFmpeg unless built without it |
| Linux | `ps2EntryRunner` | `play.sh` | raylib needs X11 and GL headers to build |
| Steam Deck | the Linux build | `play.sh`, added to Steam as a non-Steam game | or the Windows build under Proton (untested here) |
| Android | `app-release.apk` | baked in with `-Pps2xBootElf=...` | files go to `/storage/emulated/0/Android/data/com.ps2x.runner/files/` with `adb push`; pair a controller |
| PS Vita | `ps2EntryRunner.vpk` | baked in by `./vita/build.sh <ux0:data/... path>` | `./vita/setup.sh` once; install with VitaShell; slow |
| web | (not a PS2Recomp target) | | `snuri00/ps2-web-recomp` is a different recompiler: a second port of the same disc, experimental |

## Guard rails

- **Bring your own game.** Every player supplies the game from a disc they own, dumped themselves.
  Never download ISOs, ELFs or BIOS files, never help find them, never ask for them. No BIOS needed.
- **Nothing from the game gets published.** `package` refuses disc images (ISO, BIN, IMG, MDF, NRG,
  CHD, CSO and ISO 9660 content under any name), PS2 ELFs and IRX modules (by magic and machine),
  disc-style names like `SLUS_203.12`, `SYSTEM.CNF`, recompiled `runner/` sources, anything in
  `game/`, and any file over 512 MB. The runner itself is built from the recompiled game code: the
  package is for the user's own devices, not for upload. Share patches, overrides and original assets.
- **Not affiliated with Sony.** PlayStation is a trademark of Sony Interactive Entertainment. PS
  Vita homebrew, Steam, Android and the other platforms belong to their owners.
- **Never destroy the user's files.** `--replace` only replaces a folder this tool made, and refuses
  while `game/` holds anything (the player's disc, saves in `mc0/`).

## Hands off to

- `/console-tester` to run the build on this machine before packaging, and after.
- `/console-controller` for the device's controls (`xbox` layout on the Steam Deck, touch zones or
  a Bluetooth pad on Android).
- `/console-saves` to move saves between devices (`mc0/` sits next to the game's ELF).
