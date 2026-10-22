---
name: console-backend
description: >-
  Backend Engineer. Builds the engine room the recompiled game runs on: syscalls, kernel stubs, file
  and disc I/O, the IOP. Reads PS2Recomp runtime logs and turns every "Unimplemented PS2 stub",
  "[Syscall TODO]", missing function, unhandled IOP RPC, failed disc read and untranslated
  instruction into a ranked fix list: count, first PC and RA, and the exact fix tool (a TOML binding,
  the runtime .cpp file to implement it in, an entry point and a recompile, or a game override).
  With the user's PS2Recomp checkout it reads the real handler names and syscall numbers. Writes
  nothing into the checkout; prints a table for console/STATE.md. Use when the user says
  "unimplemented stub", "syscall TODO", "it crashes at boot", "fix the backend", "it can't find a
  file", "the IOP", "what is syscall 0x3C", or after /console-tester reports a stop.
argument-hint: "scan <log> [--repo PATH] [--markdown] | handlers --repo PATH | syscall <num>"
---

# console-backend

The Backend Engineer makes the runtime under the recompiled game complete enough for it to run.
The generated code is a literal translation of the game; everything it expects from the PS2 (the
kernel, the SDK libraries, the disc, the I/O processor) has to exist in `ps2xRuntime` and `ps2xIOP`.
Every missing piece shows up as a log line. This skill reads those lines, ranks them, and says which
of the four fix tools applies and in which file.

Honest limits: the tool reads logs and the checkout; it does not write C++ for you. Some fixes are a
one-line `case` in the dispatcher, others are a whole subsystem. A game usually stops at the first
missing piece, so expect one fix per run early on, and fix in order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/stubs.py" scan console/runs/latest.log --repo ../PS2Recomp   # ranked fixes
python3 "${CLAUDE_SKILL_DIR}/stubs.py" scan console/runs/*.log --markdown                # table for STATE.md
python3 "${CLAUDE_SKILL_DIR}/stubs.py" handlers --repo ../PS2Recomp --grep sceCd         # real handler names
python3 "${CLAUDE_SKILL_DIR}/stubs.py" handlers --repo ../PS2Recomp --todo               # handlers still TODO
python3 "${CLAUDE_SKILL_DIR}/stubs.py" syscall 0x3C --repo ../PS2Recomp                  # name a syscall
python3 "${CLAUDE_SKILL_DIR}/stubs.py" syscall -0x1A                                     # interrupt variant
```

`scan` exits 1 when it finds something to fix and 0 on a clean log; every command takes `--json`.

## What it reads and writes

| File | What |
| --- | --- |
| runtime logs (`console/runs/*.log` from /console-tester, or saved stdout+stderr) | read, line by line, any size |
| `ps2xRuntime/include/ps2_call_list.h` (with `--repo`) | `PS2_SYSCALL_LIST` and `PS2_STUB_LIST`: the handler names a binding may use |
| `ps2xRuntime/src/lib/Kernel/Syscalls/Dispatcher.cpp` (with `--repo`) | which syscall number goes to which handler |
| `ps2xRuntime/src/lib/Kernel/**/*.cpp` (with `--repo`) | where each handler lives and which are still `TODO_NAMED` |
| stdout | the ranked list, or a Markdown table to paste into STATE.md's "Stubs and syscalls" |

Without `--repo` it uses a built-in table of standard EE kernel syscalls and guesses files from
names, and says so. Read `reference/runtime-layers.md` before the first fix of a session: the three
layers, how a call reaches a handler, triage stubs, the IOP, disc I/O and the subsystem file map.

## The layers, in short

| Layer | What | Rule |
| --- | --- | --- |
| Recompiled code | `ps2xRuntime/src/runner/*.cpp`, one C++ function per game function, dispatched by guest ADDRESS through one function table | never edited, never listed |
| Runtime | `ps2xRuntime/src/lib/`: kernel syscalls (`Kernel/Syscalls/`), SDK stubs (`Kernel/Stubs/`), memory, GS, VU, audio, pad, files; `ps2xIOP/`: the game's IRX modules | your fixes go in its `.cpp` files |
| Host | raylib (window, input, sound), the file system, FFmpeg | untouched |

A bound stub calls a named handler; an unbound or skipped one logs `Unimplemented PS2 stub called`
and throws; a `syscall` goes through `Kernel/Syscalls/Dispatcher.cpp` and an unknown number logs
`[Syscall TODO]` and returns 0. Subsystem by subsystem, the first file to open is in section 7 of
`reference/runtime-layers.md` (threads and semaphores, interrupts, SIF and modules, `fio*`, `sceCd*`,
memory card, pad, audio, GS, movies, libc).

## The method

1. **Read STATE.md first** (`state.py show` from /console-reverse): the phase, the runner command,
   the stub table, the open strikes.
2. **Get one log per run.** /console-tester (`run.py boot`) keeps them in `console/runs/`; by hand,
   run the command from STATE.md with a short timeout and save stdout and stderr.
3. **Scan it**: `stubs.py scan <log> --repo <PS2Recomp>`. Issues come in the order they first
   appeared. The first one usually caused the stop ("ended the run"); later ones can be
   consequences. Note that unknown syscalls do not stop the game (the runtime returns 0), so an
   early `[Syscall TODO]` is a prime suspect for a strange crash later.
4. **Pick the fix tool the scan names, then check it in the source** before writing anything:

| The log says | Fix tool | Where |
| --- | --- | --- |
| a handler that is still `TODO_NAMED` | Runtime C++ | its `Kernel/Stubs/<File>.cpp` |
| `sub_XXXXXXXX` or a name with no handler | TOML | bind `"<Handler>@0xADDR"`, or recompile it if it is game code, or `ret0@` for one run to classify it |
| a handler that works but logged anyway | TOML | it is in `skip`: move it to `stubs` |
| `[Syscall TODO]` | Runtime C++ | a `case` in `Kernel/Syscalls/Dispatcher.cpp` (+ the handler in `Kernel/Syscalls/<File>.cpp`) |
| missing function, `codeRegion=yes` | Recompiler | `entry_points` hint in the TOML, then re-run `ps2_recomp` (batch them) |
| missing function, `codeRegion=no` | Investigate | overlay, second program or bad pointer: the caller at RA-8 |
| `[IOP/RPC trace:unhandled]` | IOP | is the IRX on the extracted disc and loaded? then ps2xIOP imports |
| `sceCdSearchFile failed` | Disc files | the file, the extraction, the run path |
| `sceCdRead unresolved LBN` | Game override | point `cdImage` at the user's own plain `.iso` |
| `fatal exception: Unhandled ... at 0x.. raw=0x..` | Game override | replace that function; record it as an upstream issue |

5. **Triage stubs are temporary.** `ret0@`/`ret1@`/`reta0@` only answer "does it get further if this
   succeeds?". Record each one in STATE.md as TEMPORARY and promote it: bind the real handler,
   implement the TODO handler, or write an override. Never leave one on a function that allocates
   memory, creates a thread or carries game logic.
6. **Prefer fixes that do not touch headers.** Implementing an existing TODO handler or adding a
   dispatcher `case` rebuilds the runtime library only. A new name in `PS2_STUB_LIST`
   (`ps2_call_list.h`) rebuilds every generated file: ask first, or put the body in a game override
   with `runtime.replaceFunction(addr, fn)` instead.
7. **Every replacement function returns with `ctx->pc = getRegU32(ctx, 31);`** unless it calls the
   original. Arguments are `getRegU32(ctx, 4..7)`, results `setReturnU32/S32`, guest pointers
   `getMemPtr(rdram, addr)` (null when invalid).
8. **Build through /console-reverse's build gate, run, scan again**, and compare with the previous
   log (/console-tester `triage.py compare`). Fixed: update the row in STATE.md and log one line.
   Same stop after a fix: `state.py strike`; at three strikes, stop guessing (circuit breaker).

Write C++ only after the adversarial check from /console-reverse: what you will change, why it fixes
the cause, what it could break, and whether you read the code or remembered it.

## IOP and disc I/O in one paragraph each

**IOP.** ps2xIOP runs the game's own IRX modules from the extracted disc on an R3000A interpreter,
with generic HLE services only for MCSERV, LIBSD and DBCMAN. `[SIF module] load-emulated ...` lines
show what loaded. An unhandled SID means the serving module is missing, failed to load, or needs an
import the virtual IOP kernel lacks (`ps2xIOP/src/emulator/`). No BIOS is involved or wanted.

**Disc and files.** `cdrom0:` is the folder of the ELF passed to the runner, `mc0/` sits beside it,
and the runtime matches names as written (on Linux, case matters). Replacing a file in the extracted
folder swaps it in the game, no ISO rebuild. Games that read raw sectors need a plain 2048-byte
`.iso` of the user's own disc set as `cdImage` in a game override (`reference/runtime-layers.md`
has the snippet; `iso.py plain` in /console-reverse converts a raw `.bin`).

## Guard rails

- **Bring your own game.** Only games the user owns, dumped from their own disc. Never download
  ISOs, ELFs, IRX modules or BIOS files, never help find them, never ask for them. PS2Recomp needs no
  BIOS, and neither do these fixes.
- **Nothing from the game gets published.** Logs can quote game strings and paths: keep them local.
  The disc, the ELF, the IRX modules and the recompiled C++ stay on the user's machine; runtime fixes
  you write are yours to share (they contain no game code), as are overrides that only hold addresses
  and your own logic. Upstream issues carry an address and an instruction word, never game files.
- **Not affiliated with Sony.** PlayStation is a trademark of Sony Interactive Entertainment.
- **Skill-specific:** never edit generated `runner/*.cpp` or list `runner/`; never edit a header
  without the user's OK; never clean the build; read-only access to the checkout from these tools.

## Hands off to

`/console-tester` to run again and compare logs, `/console-reverse` for entry points, re-exports,
recompiles and the build gate, `/console-mod` when the fix is a game-specific override,
`/console-controller`, `/console-graphics`, `/console-sound` and `/console-saves` when the first
issue sits in their subsystem.
