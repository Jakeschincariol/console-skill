# Guard rails: the fix taxonomy, the decision flow, red flags and the subsystem map

Adapted from hkmodd/ps2-recomp-Agent-SKILL (MIT), updated for PS2Recomp as of October 2026.

Load this on any crash, build error or wrong behaviour, before writing C++, and whenever the same
mistake comes back. File names below were checked against PS2Recomp commit eb8688b.

## 1. The four fix tools (there is no fifth)

| # | Tool | What it changes | Use it when | Where |
| --- | --- | --- | --- | --- |
| 1 | TOML | how the recompiler sees the ELF | a function start is missing (`entry_points`), a function should call a runtime handler (`name@0xADDR`), a triage return (`ret0@`/`ret1@`/`reta0@`), an instruction must be replaced before translation (`[[patches.instructions]]`), a jump table was missed | `console/<serial>.toml`, then tool 4 |
| 2 | Runtime C++ | the hand-written runtime | a syscall, SDK stub, file, GS, audio, pad or IOP path is missing or wrong for every game | `.cpp` files under `ps2xRuntime/src/lib/` (never headers without asking) |
| 3 | Game override | one game build only | a recompiled function must be replaced, an address bound to a handler, or data written at load, for this game only | a `PS2_REGISTER_GAME_OVERRIDE` module kept in `console/patches/`, compiled into the runner by /console-mod's CMake include (see pipeline.md) |
| 4 | Re-run the recompiler | the generated code | the TOML changed, or a new ELF needs translating | `ps2_recomp <toml>`, copy outputs, build |

Never: edit generated `runner/*.cpp`, paste MIPS disassembly into C++ by hand, add
`if (pc == 0x...) return;` hacks to the shared runtime, or patch a symptom. If no tool fits, the
problem is not understood yet: go back to the reasoning loop (section 4) or ask the user.

Which skill owns the work once the tool is chosen:

| Problem | Skill |
| --- | --- |
| disc, ELF, function map, missing function, recompiler bug, phases | /console-reverse |
| unimplemented stub or syscall, file and disc I/O, IOP modules and RPC | /console-backend |
| black screen, wrong draws, textures, widescreen | /console-graphics |
| no input, remapping | /console-controller |
| silence, music, sound effects | /console-sound |
| data archives, level files | /console-levels |
| memory card, saves | /console-saves |
| change game behaviour, cheats, patches, overrides | /console-mod |
| speed, gravity, frame rate numbers | /console-physics |
| other platforms and packaging | /console-anywhere |
| repeated runs, crash ranking, regressions | /console-tester |

## 2. The decision flow

```
BUILD ERROR
  in generated runner/*.cpp ......... never edit it. Missing symbol: bind or implement (TOML / runtime).
                                      Bad translation: report upstream, work around with an override.
  in ps2xRuntime/src/lib/*.cpp ....... your code: fix it there.
  link error, PS2 SDK name ........... stub binding in the TOML or implement in the runtime.
  link error, host library ........... CMake / packages (FFmpeg via pkg-config, raylib deps).
  SSE4.1 "target feature" error ...... configure flags (pipeline.md, Phase 0), not code.

RUNTIME STOP (read the log with stubs.py scan, /console-backend)
  Unimplemented PS2 stub called ...... a handler that is still a TODO (runtime C++), or a skipped /
                                      unbound function (TOML: bind it, or recompile it).
  [Syscall TODO] ..................... runtime C++: add the case in Kernel/Syscalls/Dispatcher.cpp.
                                      Note: the runtime returns 0 and carries on, so later symptoms
                                      may be caused by this one.
  No exact recompiled function ....... codeRegion=yes: missing function start, add an entry_points
                                      hint and re-run the recompiler. codeRegion=no: overlay, second
                                      program or a bad pointer: look at the caller.
  fatal exception: Unhandled ... ..... the recompiler could not translate an instruction: override
                                      the function, report upstream.
  [IOP/RPC trace:unhandled] .......... no IRX or HLE service answers that SID: module missing from
                                      the disc folder, or ps2xIOP lacks an import (/console-backend).
HANG (no crash, no output)
  find the loop (Ghidra on the last PCs, the F1 debug panel's threads and semaphores). Waiting on
  hardware the runtime does not model: runtime C++ for that device. Game-specific wait: override.
WRONG BEHAVIOUR
  graphics -> GS layer; audio -> audio layer; file not found -> I/O roots and case;
  game logic -> A/B against PCSX2, then an override of that one function.
```

## 3. Red flags: you are in the wrong layer

| You catch yourself | Why it is wrong | Do instead |
| --- | --- | --- |
| opening `runner/<function>.cpp` to edit it | the recompiler overwrites it; it is the game's code | TOML, runtime C++ or an override |
| listing or searching `runner/` | 30,000+ files flood the context | `state.py detect` counts them; open ONE known file if you must read it |
| adding a header field "just this once" | the runner's precompiled header pulls in `ps2_runtime.h`, `ps2_runtime_macros.h`, `ps2_syscalls.h`, `ps2_stubs.h`, `ps2_call_list.h`: every generated file rebuilds | a file-scope `static` in the `.cpp`, or an `extern` between two `.cpp` files; if a header change is unavoidable, stop and tell the user the cost |
| binding the tenth `ret0@` | each one hides a function the game expected to work | promote the important ones to real code; keep a list in STATE.md |
| a fourth attempt at the same crash | guessing | the circuit breaker (SKILL.md) |
| "it compiles, so it works" | compiling proves syntax only | build, run, compare, then record |
| reading a 500 KB log whole | context gone | `stubs.py scan`, or the last 5,000 characters |

## 4. The reasoning loop

1. **Observe.** The exact symptom: log line, PC, RA, what the screen shows.
2. **Locate.** Which layer (section 5), which subsystem.
3. **Understand.** What the original code does: decompile it in Ghidra, watch it in PCSX2.
4. **Decide.** Exactly one of the four tools. If you cannot pick one, step 3 is not finished.
5. **Verify.** Build (exit 0), run, compare with what should happen, record the result.

Before any C++: propose (address, logic, file, why it fixes the cause), then attack your own plan
(Is the address inside 32 MB? Does it hide a crash that corrupts later? Is it a header? Did you read
the code or remember it? Does it break an earlier milestone in STATE.md?), and only then write it.

Escalation when stuck: the log, then the subsystem's source file, then Ghidra, then a PCSX2 A/B
(ghidra-and-pcsx2.md), then the circuit breaker. More logging after one round of logging failed is not
a new approach.

## 5. Subsystem map (real files at eb8688b)

Paths are under `ps2xRuntime/src/lib/` unless they start with another folder.

| Subsystem | Identifier | Files | Typical symptom |
| --- | --- | --- | --- |
| EE code and dispatch | recompiled functions, function table | `ps2_runtime.cpp`, generated `src/runner/*.cpp` | missing function, wrong logic |
| Memory | RDRAM, scratchpad, address translation | `ps2_memory.cpp`, `ps2xRuntime/include/runtime/ps2_memory.h` | bad pointer, out-of-range access |
| Kernel: threads, semaphores, alarms, interrupts | syscall numbers | `Kernel/Syscalls/{Thread,Sync,Interrupt,System}.cpp`, `Kernel/EeScheduler.cpp`, dispatch in `Kernel/Syscalls/Dispatcher.cpp` | `[Syscall TODO]`, hangs in a wait |
| SDK functions (libc, sce*) | named handlers in `ps2_call_list.h` | `Kernel/Stubs/{LibC,CD,DMA,GS,Pad,MemoryCard,SIF,RPC,MPEG,IPU,Audio,VU,TTY,Font,FileIO,Deci2,System,Compatibility,Unimplemented}.cpp` | `Unimplemented PS2 stub called` |
| File and disc I/O | `cdrom0:`, `host:`, `mc0:` | `ps2_vfs.cpp`, `Kernel/Syscalls/FileIO.cpp`, `Kernel/Stubs/FileIO.cpp`, `Kernel/Stubs/CD.cpp`, `ps2xIOP/src/ps2_path.cpp` | file not found, endless loading |
| SIF, RPC and the IOP | IRX modules, RPC SIDs | `Kernel/Syscalls/RPC.cpp`, `Kernel/Stubs/SIF.cpp`, `ps2_iop_host.cpp`, `ps2xIOP/src/emulator/`, `ps2xIOP/src/modules/{mcserv,libsd,dbcman}.cpp` | `[IOP/RPC trace:unhandled]`, boot hang |
| GS | privileged registers at 0x12000000 | `gs/gs_frontend.cpp`, `gs/gs_cpu_backend.cpp`, `gs/ps2_gs_memory.cpp`, `Kernel/Stubs/GS.cpp` | black screen, wrong draws |
| GIF | 0x10003000 | `gs/ps2_gif_arbiter.cpp` | draws never arrive |
| VIF1 and VU1 | 0x10003C00 | `ps2_vif1_interpreter.cpp`, `vu/ps2_vu1_core.cpp`, `vu/ps2_vu1_upper.cpp`, `vu/ps2_vu1_lower.cpp` | missing or broken geometry |
| VU0 macro mode | COP2 inside EE code | translated by the recompiler; `sceVu0*` helpers in `Kernel/Stubs/VU.cpp` | wrong transforms |
| DMA | DMAC channels at 0x10008000 and up | `Kernel/Stubs/DMA.cpp`, `ps2_memory.cpp` | waits on DMA forever |
| Audio | SPU2 on the IOP side | `ps2_audio.cpp`, `ps2_audio_vag.cpp`, `Kernel/Stubs/Audio.cpp`, `ps2xIOP/src/modules/libsd.cpp` | silence |
| Pad | libpad | `ps2_pad.cpp`, `Kernel/Stubs/Pad.cpp` | no input |
| Memory card | `mc0:` | `Kernel/Stubs/MemoryCard.cpp`, `ps2xIOP/src/modules/mcserv.cpp` | saves fail |
| Movies | IPU at 0x10002000, PSS files | `Kernel/Stubs/MPEG.cpp`, `Kernel/Stubs/IPU.cpp` (FFmpeg) | black movies |
| ROM device | `rom0:` | `ps2_rom_device.cpp` | rom0 module or file missing |
| Game overrides | per game build | `game_overrides.cpp` (registry and matching), header `ps2xRuntime/include/game_overrides.h` | override not applied |
| Debug panel | F1 in the window | `ps2_debug_panel.cpp` | none: it is the tool |
| Platform glue | Android, Vita | `ps2_android_runtime.cpp`, `ps2_vita_runtime.cpp` | platform-only bugs |

The old names in the base skill (`ps2_gs_gpu.cpp`, `ps2_syscalls.cpp`, `ps2_stubs.cpp`,
`ps2_iop.cpp`, `src/lib/overrides/`) no longer exist.

## 6. Your own failure modes

| Mistake | Prevention |
| --- | --- |
| trusting memory over the file | re-read STATE.md and the source before claiming anything |
| temp files in the project or checkout | the scratch folder only; tools write under `console/` |
| leaving the game running after a test | kill it every time (the test protocol) |
| appending runs into one log | one log per run (`console/runs/`, /console-tester) |
| silent workaround of an upstream bug | write it under "Known upstream issues" with the address and the workaround |
| a fix without reading STATE.md first | it is the first step of every session and every fix |

## 7. Upstream awareness

PS2Recomp is experimental and changes weekly. Suspect the tool, not your fix, when the generated C++
clearly disagrees with the MIPS in Ghidra, one function translates wrongly while a similar one is
fine, or the recompiler crashes or skips functions. Confirm against the disassembly, work around
with the TOML or an override (never by editing generated code), record it, and offer the user a
minimal upstream issue with no game data in it (an address, the one instruction word and the line
generated for it are enough). Issues: https://github.com/ran-j/PS2Recomp/issues
