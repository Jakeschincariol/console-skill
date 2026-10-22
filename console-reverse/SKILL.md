---
name: console-reverse
description: >-
  Reverse Engineer. Opens your own disc image, finds the game's code and maps how it works: the boot
  ELF, every function, the design doc. Drives the whole port and is the entry point of the Console
  pack. Reads ISO 9660 images without loading them whole, reads SYSTEM.CNF and the boot ELF
  (segments, CRC-32 and PCSX2 CRC, IRX modules), writes a starter PS2Recomp TOML with real keys
  only, and keeps console/STATE.md, the memory every /console-* skill reads first. Carries the
  PS2Recomp rules: never clean the build, never edit generated code or headers, a build gate, and a
  three-strike circuit breaker. Use when the user says "port my PS2 game", "recompile my game",
  "open my ISO", "what's on this disc", "set up PS2Recomp", "where are we", "continue the port", or
  at the start of any Console session.
argument-hint: "[iso|elf|state] <subcommand> [flags]"
---

# console-reverse

The Reverse Engineer turns a PS2 disc the user owns into a native program with ran-j/PS2Recomp,
one phase at a time: dump, extract, map the functions, recompile, build, boot, then I/O and menus.
It keeps the project memory in `console/STATE.md`, writes the design doc, and routes each problem
to the skill that owns it. Every Console session starts here.

Be honest about the size of the job. PS2Recomp is experimental: a first boot can take days, a game
that plays end to end can take weeks, and GS/VU-heavy games run slowly for now. This skill does not
make that disappear; it keeps the work methodical, resumable and inside the four fix tools.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/state.py" show                        # every session starts here
python3 "${CLAUDE_SKILL_DIR}/state.py" detect                      # what can be found without asking
python3 "${CLAUDE_SKILL_DIR}/state.py" init --game "Title"         # first session: console/STATE.md
python3 "${CLAUDE_SKILL_DIR}/iso.py" info game.iso                 # is the dump complete?
python3 "${CLAUDE_SKILL_DIR}/iso.py" boot game.iso                 # boot ELF, serial, region, VMODE
python3 "${CLAUDE_SKILL_DIR}/iso.py" extract game.iso --out console/disc
python3 "${CLAUDE_SKILL_DIR}/iso.py" ls game.iso -r                # the whole disc tree
python3 "${CLAUDE_SKILL_DIR}/elf.py" info console/disc/SLUS_203.12 # segments, CRCs, IRX modules
python3 "${CLAUDE_SKILL_DIR}/elf.py" toml console/disc/SLUS_203.12 --ghidra-csv console/functions.csv
python3 "${CLAUDE_SKILL_DIR}/state.py" phase PHASE_EXTRACT
python3 "${CLAUDE_SKILL_DIR}/state.py" log "boots to the first file read"
python3 "${CLAUDE_SKILL_DIR}/state.py" strike "PC 0x00123456 hang" "bound ret0, still hangs"
```

Other commands: `iso.py plain game.bin --out console/game.iso` (raw 2352-byte dump to a plain
2048-byte image), `elf.py crc`, `elf.py serial SLUS-20312`, `state.py set runner_cmd "..."`,
`state.py design`, `state.py show --section Crashes`, `state.py strike --list`. Most take `--json`;
`--workspace PATH` points `state.py` at a game workspace other than the current folder.

## What it reads and writes

| File | What |
| --- | --- |
| your disc image (`.iso`, or a raw 2352-byte `.bin`) | read only, streamed; `iso.py info` says if the dump is short |
| `console/disc/` | the extracted disc: the runtime's `cdrom0:` root. `extract` keeps files that already exist (your edits) unless `--force` |
| `console/<serial>.toml` | `elf.py toml`: a starter config; refuses to overwrite |
| `console/STATE.md` | from `templates/STATE.md`: paths, build config, phase, runner command, stubs, crashes, strikes, upstream issues, learned patterns, dead ends, session log |
| `console/design.md` | from `templates/design.md`: how the game plays, its loop, menus, files, code map |
| `console/.gitignore` | written once by `state.py init`: keeps the disc, generated C++ and other game-derived folders out of git |
| the PS2Recomp checkout | read by `state.py detect` (build dir, CMakeCache.txt, a COUNT of runner files); never written by these tools |

## Boot sequence: every session, before anything else

1. **State first.** `state.py show`. If it exists, read it all, rules header included, and resume
   at its phase. If it does not, run `state.py detect`.
2. **Detect, then ask once.** Detection covers the workspace, the extracted disc and boot ELF, disc
   images, TOMLs, Ghidra maps, the PS2Recomp checkout, its build dir (generator, compiler, build
   type from `CMakeCache.txt`) and the runner. Ask the user only for what it could not find, in one
   message, then record it (`state.py init`, later `state.py set`). Never assume a path, and never
   assume the game files live inside the PS2Recomp checkout.
3. **Record the run command** with `state.py set runner_cmd "..."`. From then on read it from
   STATE.md verbatim; never retype it from memory.
4. **Load references when the situation calls for them**, not all at once:
   `reference/pipeline.md` (fresh project or a new phase), `reference/guardrails.md` (any error,
   before any C++), `reference/ps2-map.md` (addresses, names, CRCs),
   `reference/ghidra-and-pcsx2.md` (reading the original code, A/B debugging).
5. **Comprehension check** before the first code change of a session. Answer from the references:
   what does `ps2_recomp` generate and where does it go; a crash inside a generated function, where
   does the fix go (never in that file); how does a TOML `name@0xADDR` binding differ from a game
   override. If any answer is shaky, re-read `reference/pipeline.md`.

Refresh triggers: before writing C++, re-read the prohibitions and STATE.md; before any build, run
the build gate; before running the game, read the runner command from STATE.md; after any error,
STATE.md plus `reference/guardrails.md`; every 15 or so tool calls, `state.py show` and the
prohibitions again. Feeling sure without having just checked is the signal to check.

## The phases

| Phase | Goal | Tools | Done when |
| --- | --- | --- | --- |
| 0 `PHASE_SETUP` | PS2Recomp builds on this machine | `reference/pipeline.md` (Windows clang-cl + Ninja or MSVC, macOS, Linux), `state.py detect` | `ps2_recomp`, `ps2_analyzer`, `ps2EntryRunner` exist; paths recorded |
| 1 `PHASE_EXTRACT` | the disc and the boot ELF | `iso.py info/boot/extract`, `elf.py info` | `console/disc/` holds the disc, the boot ELF is known, design.md started |
| 2 `PHASE_FUNCTION_MAP` | every function's bounds | Ghidra + `ExportPS2Functions.java` (preferred), or `ps2_analyzer`, or `elf.py toml` | a TOML (and CSV) in `console/` |
| 3 `PHASE_RECOMPILE` | C++ from the ELF | `ps2_recomp console/<serial>.toml` | its report is read and logged; outputs copied into the runtime |
| 4 `PHASE_BUILD_RUN` | a native program that boots | build gate, `ps2EntryRunner <elf>`, /console-tester, /console-backend | builds with exit 0 and gets past boot |
| 5 `PHASE_IO_MENUS` | files, menus, input | /console-backend, /console-controller, /console-graphics, /console-sound, /console-saves | menus reached and playable input; temporary stubs replaced |

Move with `state.py phase <NAME>`: it logs the move and prints the exit test. Phase 0 per OS, the
Ghidra export, every TOML key, the recompiler report, copying outputs and where an override file
must go are all in `reference/pipeline.md`. Run the ELF that sits inside `console/disc/`: the
runtime reads `cdrom0:` from the ELF's folder and keeps the memory card in `mc0/` next to it.

## The design doc

At Phase 1 run `state.py design` and fill `console/design.md`: what kind of game it is, the core
loop, the screens and menus in boot order, the files on the disc and what each is for, and the
code map as functions get named. Start from what the user says about the game (mark it SAID) and
upgrade lines to SEEN as the port, PCSX2 or Ghidra confirms them. Update it at the end of every
phase. It describes the game in your own words and never copies its text, art or code. The other
skills read it (levels, physics, mods).

## The four fix tools, and who owns what

| Tool | Where | For |
| --- | --- | --- |
| 1. TOML | `console/<serial>.toml`, then tool 4 | missing function starts (`entry_points`), handler bindings (`name@0xADDR`), temporary `ret0@`/`ret1@`/`reta0@`, instruction patches |
| 2. Runtime C++ | `.cpp` files under `ps2xRuntime/src/lib/` | syscalls, SDK stubs, I/O, GS, audio, pad, IOP glue: anything every game needs |
| 3. Game override | `PS2_REGISTER_GAME_OVERRIDE` module, source in `console/patches/` | one game build only: replace a function, bind an address, write data at load |
| 4. Re-run the recompiler | `ps2_recomp`, copy outputs, build | after TOML changes, in batches |

Stubs, syscalls, files and the IOP: /console-backend. Graphics: /console-graphics. Input:
/console-controller. Audio: /console-sound. Disc data and levels: /console-levels. Saves:
/console-saves. Behaviour changes and overrides: /console-mod. Numbers like speed and gravity:
/console-physics. Other platforms: /console-anywhere. Runs, crash ranking, regressions:
/console-tester. The decision flow, red flags and the real subsystem map are in
`reference/guardrails.md`.

## Prohibitions

1. **Never clean the build.** No `--clean-first`, `--target clean`, deleting the build folder or
   object files, or `CLEAN=1` for `vita/build.sh`. A full runner rebuild costs hours.
2. **Never edit generated code** (`ps2xRuntime/src/runner/*.cpp` written by `ps2_recomp`, and the
   two generated headers). The next recompile overwrites it, and it is the game's code.
3. **Never edit a header without the user's OK.** The runner's precompiled header includes
   `ps2_runtime.h`, `ps2_runtime_macros.h`, `ps2_syscalls.h`, `ps2_stubs.h` and `ps2_call_list.h`:
   one changed line rebuilds every generated file. Prefer a file-scope `static` or an `extern`
   between `.cpp` files; if a header change is unavoidable, stop, give the cost, ask.
4. **Never list, search or open-by-wildcard `runner/`.** It can hold 30,000+ files. Count with
   `state.py detect`; open one named file only when you must.
5. **No destructive git** in the PS2Recomp checkout: no `checkout`, `reset`, `clean`, `stash`,
   `pull`. Updating PS2Recomp is the user's decision.
6. **Never claim it compiles or works without the output.** Exit code 0 for builds; a run log for
   behaviour.
7. **Never delete or overwrite a build artifact, a save, or an extracted file without asking.**
8. **Never pipe a build into a file and forget it, or read a huge log whole.** Read build output
   directly; for run logs, the last 5,000 characters or `stubs.py scan`.
9. **Never create files in the PS2Recomp checkout, or loose in the game workspace root,** except
   what the user approves (generated outputs, /console-mod's one-line CMake include). Notes and
   outputs go in `console/`; scratch files in a temp folder.
10. **Never guess paths, names or addresses.** Check them on disk, in Ghidra, or in the source.

## Build gate: before every build command

1. **Inspect** the command: no `--clean-first`, no `--target clean`, no `rm`/`Remove-Item`, no
   reconfigure of an existing build dir without the user's OK.
2. **Environment.** Windows: inside the VS developer prompt (`where cl` prints a path, or
   `VSINSTALLDIR` is set). macOS: `xcrun --show-sdk-path` works. Linux: `c++ --version` works.
3. **Directory** is the build dir recorded in STATE.md and it contains `CMakeCache.txt`.
4. **Run** `cmake --build <build_dir>`, with `--target ps2EntryRunner` once only the port changes
   (plus `--config <type>` only for the Visual Studio generator, and `-j N` if wanted). Nothing else.
   Read the whole output; exit 0 or it failed.

## Circuit breaker: three strikes

Every failed fix attempt at the same problem is a strike: `state.py strike "<problem>" "<what was
tried>"`. Strike 2: before the next attempt, load the reference for that subsystem and re-read the
original code. Strike 3 (the tool exits 1 and prints STOP): no more guessing. Re-read STATE.md and
`reference/guardrails.md`, decompile the code in Ghidra, A/B it against PCSX2 if available
(`reference/ghidra-and-pcsx2.md`), search for known community fixes, then ask the user one specific
question with what is known and what was tried. When it is fixed: `state.py strike "<problem>"
--clear "what fixed it"`, and write the pattern under "Learned patterns".

## Context survival and session close

The context is finite. Read at most about 200 lines of a file at a time; run tests with short
timeouts (boot 10 to 15 s, menus 30 s) and read only the last 5,000 characters; one log per run;
load references on demand. Every 15 or so tool calls, check yourself without looking: the build dir
name, the files you must not edit, the only safe build command. Two wrong: re-read STATE.md and the
prohibitions. Worse: stop and tell the user a fresh session is safer.

Before the session ends: write patterns (not events) under "Learned patterns" ("X causes Y, fix with
Z"), what failed under "Dead ends", update the stubs and crash tables and the phase, add a log line,
then read STATE.md back once to check it is coherent.

## Guard rails

- **Bring your own game.** Only games the user owns, dumped from their own disc (ImgBurn on Windows,
  `dd` from a PC DVD drive on macOS or Linux). Never download ISOs, ELFs or BIOS files, never help
  find them, never ask for them. PS2Recomp needs no BIOS.
- **Nothing from the game gets published.** The disc, the ELF, the IRX modules, textures, audio,
  level files and the recompiled C++ (derived from the game's code) stay on the user's machine. Share
  patches, overrides and your own original assets only. `state.py init` writes a `.gitignore` for
  this; never remove it to push a repo.
- **Not affiliated with Sony.** PlayStation is a trademark of Sony Interactive Entertainment.
  PS2Recomp is a separate GPL-3 project by ran-j, cloned by the user, not bundled here.
- **Skill-specific:** extraction never overwrites existing files without `--force`; the TOML writer
  never overwrites a config without `--force`; these tools never write into the PS2Recomp checkout.

## Hands off to

Phase 4 onward, `/console-tester` runs the port and ranks the crashes, and `/console-backend` turns
stub and syscall logs into fixes. Then `/console-graphics`, `/console-controller`, `/console-sound`,
`/console-saves` and `/console-levels` for their systems, `/console-mod` and `/console-physics` once
it plays, and `/console-anywhere` to build it for other machines. Every one of them reads
`console/STATE.md` first and logs back into it.
