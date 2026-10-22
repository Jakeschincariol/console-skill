# Console project state: {{TITLE}}

> Read this file first, every session, before any other step. It is the memory between sessions.
> Rules that never bend (full list in /console-reverse):
> 1. Never clean the build. No `--clean-first`, no `--target clean`, no deleting the build folder.
> 2. Never edit generated code (`ps2xRuntime/src/runner/*.cpp`) and never list or search `runner/`.
> 3. Never edit a header without the user's OK: most headers rebuild every generated file.
> 4. No destructive git (checkout, reset, clean, stash, pull) in the PS2Recomp checkout.
> 5. The run command is the one under "Active runner command". Read it, never retype it from memory.
> 6. Three failed fixes for the same problem: stop (see "Circuit breaker").
> 7. Nothing from the game gets published: disc, ELF, IRX, assets, generated C++.

## Game
- Title: {{TITLE}}
- Serial: {{SERIAL}}
- Region: {{REGION}}
- Boot ELF name: {{ELF_NAME}}

## Workspace paths
- Game workspace: {{WORKSPACE}}
- Console folder: {{CONSOLE_DIR}}
- Disc image: {{DISC_IMAGE}}
- Extracted disc (cdRoot): {{DISC_DIR}}
- Boot ELF path: {{ELF_PATH}}
- PS2Recomp checkout: {{PS2RECOMP}}
- Recompiler config (TOML): {{TOML}}
- Ghidra function map (CSV): {{GHIDRA_CSV}}
- Generated C++ (TOML output): {{RECOMP_OUTPUT}}

## Build config
- Host: {{HOST}}
- Build dir: {{BUILD_DIR}}
- Generator: {{GENERATOR}}
- C++ compiler: {{COMPILER}}
- Build type: {{BUILD_TYPE}}
- Runner executable: {{RUNNER_EXE}}
- Runner .cpp count (count only, never list): {{RUNNER_CPP_COUNT}}

## Current phase
- Phase: {{PHASE}}

Phases: PHASE_SETUP (0), PHASE_EXTRACT (1), PHASE_FUNCTION_MAP (2), PHASE_RECOMPILE (3),
PHASE_BUILD_RUN (4), PHASE_IO_MENUS (5).

## Active runner command
```
{{RUNNER_CMD}}
```

## Binaries
| Binary | Path | TOML | Status |
| --- | --- | --- | --- |
| {{ELF_NAME}} (boot ELF) | {{ELF_PATH}} | {{TOML}} | not recompiled yet |

## Stubs and syscalls
Paste rows from `stubs.py scan --markdown` (/console-backend). Mark every ret0/ret1/reta0 as TEMPORARY.

| # | Kind | What | Calls | First PC | First RA | Fix tool | Where | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |

## Crashes
| # | Symptom / PC | Layer | Fix tool | Strikes | Status |
| --- | --- | --- | --- | --- | --- |

## Circuit breaker
One line per open problem, kept by `state.py strike`. At 3 strikes: stop guessing.

## Known upstream issues
Recompiler or runtime bugs found in PS2Recomp itself: function or address, what the MIPS does, what
was generated, the workaround, and the GitHub issue if there is one.

## Learned patterns
Write patterns, not events: "X causes Y, fix with Z".

## Dead ends
What was tried, what happened, why it is not the answer. Read before trying something "new".

## Session log
