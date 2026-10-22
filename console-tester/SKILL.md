---
name: console-tester
description: >-
  Playtester. Runs the port again and again, reads every crash, ranks what to fix next and checks
  nothing broke. run.py starts ps2EntryRunner with your game's ELF under a hard timeout, kills the
  whole process tree, keeps the log in console/runs/ and calls the run PASS, HANG, CRASH or FAIL
  (soak repeats it N times; --input replays a controller script). triage.py reads the runtime's own
  log lines, clusters them by PC, names the function from your Ghidra CSV map, ranks them and names
  the /console-* skill that owns each; compare shows fixed, still there and new. abdiff.py finds
  the first divergence between PCSX2 and the port in register or memory dumps. Use when the user
  says "run it", "does it boot", "test the port", "why did it crash", "what do I fix next", "did
  that break anything", "run it 100 times", "compare with PCSX2", or after every build.
argument-hint: "run.py boot|soak|last, triage.py scan|compare|lookup, abdiff.py regs|mem"
---

# console-tester

Every fix in a port ends the same way: build, run, read the log, decide. This skill makes that loop
fast and honest. `run.py` never leaves a game window running, never floods the context (it keeps
the first and last 2000 lines and prints only the last 5000 characters), and judges each run the
same way every time. `triage.py` turns a log into a ranked fix list with an owner per item, and
`compare` tells you whether a change fixed something, broke something, or just let the game get
further and meet the next blocker.

Honest scope: `soak` repeats the same boot with the same scripted input; it finds crashes that come
and go (threads, timing, IOP). It does not play the game for you. "It plays" still means a person
played it.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/run.py" boot --runner out/build/ps2xRuntime/ps2EntryRunner \
        --elf console/disc/SLUS_203.12 --seconds 15                          # one run
python3 "${CLAUDE_SKILL_DIR}/run.py" boot --runner ... --elf ... --expect "title" --until-expect
python3 "${CLAUDE_SKILL_DIR}/run.py" boot --runner ... --elf ... --input console/inputs/menu.txt
python3 "${CLAUDE_SKILL_DIR}/run.py" soak --runner ... --elf ... --runs 20 --seconds 30
python3 "${CLAUDE_SKILL_DIR}/run.py" last                                       # the latest result
python3 "${CLAUDE_SKILL_DIR}/triage.py" scan console/runs/latest.log --map console/functions.csv
python3 "${CLAUDE_SKILL_DIR}/triage.py" scan console/runs/latest.log --markdown   # for STATE.md
python3 "${CLAUDE_SKILL_DIR}/triage.py" compare console/runs/<before>.log console/runs/latest.log
python3 "${CLAUDE_SKILL_DIR}/triage.py" lookup 0x00123456 --map console/functions.csv
python3 "${CLAUDE_SKILL_DIR}/abdiff.py" regs console/ab/pcsx2.json console/ab/port.txt --width 32
python3 "${CLAUDE_SKILL_DIR}/abdiff.py" mem console/ab/pcsx2.bin console/ab/port.bin --base 0x00100000
```

`run.py` flags: `--expect REGEX` (repeatable), `--until-expect`, `--input FILE` / `--record FILE`
(controller replay and recording, needs a pad from `/console-controller` `gen --replay`), `--out`
(default `console/runs`), `--arg`, `--cwd`, `--keep 2000`, `--tail-chars 5000`, `--json`.
Exit codes everywhere: 0 ok (PASS, no issues, dumps match), 1 something needs attention, 2 bad input.

## What it reads and writes

| file | what |
| --- | --- |
| `console/STATE.md` | the run command and build folder (read them, never reconstruct them); one result line per run goes back in |
| `console/runs/<UTC stamp>.log` | stdout + stderr of one run; first and last 2000 lines when huge |
| `console/runs/latest.log`, `latest.json` | the last run and its verdict: result, reason, exit code or signal, expects, counts, first fatal line |
| `console/runs/soak-<stamp>.json` | a soak summary: totals, flaky or not, fatal lines by count |
| your Ghidra CSV | `Name,Start,End,Size` from `ExportPS2Functions.java` (End is exclusive), to name PCs |
| `console/ab/` | your A/B dumps (PCSX2 side and port side) |

## Results

| result | means |
| --- | --- |
| PASS | every `--expect` line appeared (or, with none given, it ran the whole timeout without a crash) |
| HANG | the timeout came first and an expected line never did. Read the last lines: what was it waiting for? |
| CRASH | a fatal line (`Error during program execution: ...`, `[main] fatal exception`, `[terminate]`, a failed ELF load), a non-zero exit, a signal, or it stopped after jumping to a missing function. The runtime catches game-thread exceptions and still exits 0, so the log decides, not the exit code |
| FAIL | it exited by itself, cleanly, before the expected lines (window closed, game returned) |

The log lines triage knows, their source files and owners: [reference/log-lines.md](reference/log-lines.md).

## The standard test protocol

Full version, with the circuit breaker and the PCSX2 A/B workflow:
[reference/test-protocol.md](reference/test-protocol.md).

1. **Read the run command from `console/STATE.md`.** Runner path, ELF path, build folder. Never
   rebuild it from memory. If it is missing, ask once and record it.
2. **Build incrementally first** (`cmake --build <build dir>`, nothing else). Never clean.
3. **Run short.** `run.py boot` with 10 to 15 seconds for boot, 30 for menus. Add an `--expect`
   for the milestone you are chasing (a log line the game or runtime prints there).
4. **Read the verdict and the tail** it prints (last 5000 characters). Do not open the whole log.
5. **Triage.** `triage.py scan console/runs/latest.log --map <csv>`. Work the top item: the first
   fatal, then missing functions, stubs, syscalls, IOP, file I/O. The owner column says which
   `/console-*` skill fixes it.
6. **Fix, rebuild, rerun, compare.** `triage.py compare <before>.log console/runs/latest.log`.
   FIXED is progress. NEW right after the old blocker usually means the game got further.
   NEW elsewhere means the change broke something: look before the next fix.
7. **Record one line** in `console/STATE.md`: date, what changed, the result, the log name.
8. **Regression list.** Keep each milestone's exact `run.py` command (with its `--expect` and
   `--input`) in `console/STATE.md` under "Regression runs". After any runtime change, run them all.
   A milestone that passed before and fails now is the first thing to fix.
9. **Circuit breaker.** Three failed fix attempts on the same crash: stop guessing. Re-read
   `console/STATE.md`, look the code up in Ghidra, and do the PCSX2 A/B comparison. Then ask the
   user a specific question if it is still unclear.

## What "it plays" means at each phase

| phase | milestone | how to check |
| --- | --- | --- |
| boot | `ELF file loaded successfully` (with runtime logs on) and no fatal line | `--expect "ELF file loaded"` |
| bootloader | IOP modules load, no missing functions in the first seconds | `triage.py scan`: no tier 0 to 2 items |
| first picture | the game draws (logos, a loading screen) | look at the window; `soak` for flakiness |
| title screen | a log line the game prints there, or the screen itself | `--expect`, then `--input` to press Start |
| menus | a scripted walk through the menus survives | `--input console/inputs/menu.txt --seconds 60` |
| in game | a person plays a level start to finish | the user, with a controller; record a script of it for regressions |

## A/B against PCSX2 (optional)

When the log does not explain a crash or a wrong value, run the original in PCSX2 as ground truth.
With the optional PCSX2-MCP debug server: break at the same address, read the registers and the
memory you care about, save them as JSON or binary. On the port side, a temporary trace override
prints the same registers at that address (template in the protocol reference). `abdiff.py regs`
and `abdiff.py mem` print the first divergence: that is where to look. Without PCSX2-MCP, PCSX2's
own debugger can show the same values by hand.

## Guard rails

- **Bring your own game.** Only games you own, dumped from your own disc. Never download ISOs, ELFs
  or BIOS files, never help find them. PS2Recomp needs no BIOS; PCSX2 needs one dumped from your
  own console.
- **Nothing from the game gets published.** Logs can quote game strings and memory dumps are game
  data: they stay on your machine with the disc, the ELF and the recompiled `runner/*.cpp`.
  Share your fixes, overrides and replay scripts, not dumps.
- **Not affiliated with Sony.** PlayStation is a trademark of Sony Interactive Entertainment.
- **Test safely.** Short timeouts, one run at a time, every process killed at the end, never clean
  the build to "make sure", never edit `runner/*.cpp` or headers to make a test pass.

## Hands off to

The owner column decides: `/console-reverse` (missing functions: `entry_points`, re-export from
Ghidra, re-run the recompiler), `/console-backend` (stubs, syscalls, IOP, file and disc I/O),
`/console-graphics` (GS, VU, MPEG video), `/console-sound` (audio), `/console-saves` (memory card),
`/console-controller` (pad, replay scripts), `/console-mod` (overrides), `/console-anywhere`
(build options such as FFmpeg).
