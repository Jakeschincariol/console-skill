Adapted from hkmodd/ps2-recomp-Agent-SKILL (MIT), updated for PS2Recomp as of October 2026.

# Test protocol, circuit breaker and PCSX2 A/B

The base skill's test protocol was written around a Windows `.bat` runner and an agent's terminal
tool. This version is the same discipline on any OS, with `run.py` doing the timing, capture and
killing, and `triage.py` doing the first read of the log.

## Why the protocol is strict

A long log eats the context window and buries the one line that matters. A game left running eats
memory and confuses the next run. Re-running unchanged code hoping for a different result wastes a
build cycle (only `soak` re-runs on purpose, to measure flakiness). So: short timeout, read the end,
kill, diagnose, record.

## Every run

| step | do | tool |
| --- | --- | --- |
| A | Read the run command from `console/STATE.md`. Never reconstruct it. | |
| B | Build incrementally: `cmake --build <build dir>` only. No clean, no `--clean-first`. | |
| C | Run with a short timeout: 10 to 15 s for boot, 30 s for menus, 60 s for a scripted walk. | `run.py boot` |
| D | Read the verdict and the last 5000 characters. Never more than about 10 000 characters of one log. | printed by `run.py` |
| E | The process tree is already killed. Never leave a runner open between steps. | `run.py` |
| F | Diagnose from what you read. Same code gives the same crash: fix first, then run again. | `triage.py scan` |
| G | One line in `console/STATE.md`: date, change, result, log file. | |
| H | If the log does not explain it: A/B against PCSX2 (below). | `abdiff.py` |

`run.py` exits 0 only on PASS. Use `--json` when you want to read the verdict as data.

## Reading a CRASH

1. The first fatal line is the end of the story, not the cause. Read the lines just before it.
2. `Error during program execution: Unimplemented PS2 stub called: NAME` means the stub `NAME` threw.
   The `Warning: Unimplemented PS2 stub called. name=NAME PC=0x.. RA=0x..` line before it gives the
   caller (RA). `triage.py` merges the two.
3. `Error: No exact recompiled function for guest PC 0x..` plus `[guest-branch:missing-target]`
   means the game jumped to code the recompiler never emitted. With `codeRegion=yes` the function
   exists in the ELF: add it to `entry_points` or define it in Ghidra and re-export, then re-run
   `ps2_recomp`. With `codeRegion=no` it jumped into data: a bad pointer earlier on, so look at the
   caller.
4. `Warning: Unimplemented PS2 syscall called ... Chosen=0x..` does not stop the game (the runtime
   returns 0) but the game may misbehave later because of it.
5. `[IOP/RPC trace:unhandled] sid=0x.. rpc=0x..` means the EE asked an IOP service nobody answered.
   The `loadedModules=[...]` list says what was running.

## Circuit breaker: three strikes

If the same crash survives three `fix, build, run` attempts:

1. Stop. Do not guess a fourth time.
2. Re-read `console/STATE.md`: the build folder, the run command, what was already tried.
3. Look the code up: decompile the function at the PC and its caller (Ghidra, GhydraMCP if set up).
4. Check the hardware facts: ps2tek (https://psi-rockin.github.io/ps2tek/) for registers and DMA.
5. A/B against PCSX2 (below). The first divergence is the root cause.
6. Search for others who met the same stub or syscall in PS2Recomp's issues and discussions.
7. Still stuck: ask the user one specific technical question, with the PC, the log line and what
   was tried.

Record each strike in `console/STATE.md` so a new session does not start the count again at zero.

## A/B comparison with PCSX2

PCSX2 runs the original game; the port runs the recompiled one. At the same address with the same
inputs they should agree. The first register or memory difference is where the port went wrong.

**Side A (PCSX2, ground truth).** With the optional PCSX2-MCP debug server (a PCSX2 build with its
DebugServer; the tool names below are the ones the base skill's playbook used, and your MCP client
may prefix them):

| step | tool |
| --- | --- |
| connect, check it is the same game and region | `pcsx2_connect`, `pcsx2_game_info`, `pcsx2_status` |
| save a state before anything risky | `pcsx2_save_state` |
| stop at the address | `pcsx2_set_breakpoint` (temporary when possible), `pcsx2_continue` |
| pause, then read | `pcsx2_pause`, `pcsx2_read_registers`, `pcsx2_read_memory` |
| watch who writes a value | `pcsx2_set_watchpoint` |
| clean up | `pcsx2_list_breakpoints`, `pcsx2_clear_all_breakpoints` |

Rules from the playbook: pause before reading registers, save a state before writing memory, never
write a register without the user's say-so, remove breakpoints when done, and make sure the game
has actually loaded the code you break on (overlays). Save the registers as JSON
(`console/ab/pcsx2.json`, name to hex value) and memory as a raw binary file. Without PCSX2-MCP,
PCSX2's debugger window shows the same values; type them into a JSON file.

**Side B (the port).** A temporary game override that prints the registers when the function at
that address is called, built and placed the way `/console-mod` describes for override modules:

```cpp
#include "game_overrides.h"
#include "ps2_runtime.h"
#include <cstdio>

namespace
{
    constexpr uint32_t kTraceAddr = 0x00123456u; // the address you broke on in PCSX2
    PS2Runtime::RecompiledFunction g_original = nullptr;

    void traceEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        static const char *kNames[32] = {"zero", "at", "v0", "v1", "a0", "a1", "a2", "a3",
                                         "t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7",
                                         "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7",
                                         "t8", "t9", "k0", "k1", "gp", "sp", "fp", "ra"};
        std::fprintf(stderr, "[ab-trace] pc=0x%08x", kTraceAddr);
        for (int r = 1; r < 32; ++r)
            std::fprintf(stderr, " %s=0x%08x", kNames[r], getRegU32(ctx, r));
        std::fprintf(stderr, "\n");
        if (g_original)
            g_original(rdram, ctx, runtime);
    }

    void applyTrace(PS2Runtime &runtime)
    {
        g_original = runtime.lookupFunction(kTraceAddr);
        runtime.replaceFunction(kTraceAddr, traceEntry);
    }
}

// ELF name as on the disc; entry 0 and CRC 0 match any build of that name.
PS2_REGISTER_GAME_OVERRIDE("A/B trace", "SLUS_203.12", 0u, 0u, applyTrace);
```

`replaceFunction` catches every call that goes through the runtime's dispatch (`JAL` and `JALR`
calls do). A plain `J` tail jump to a known function is compiled as a direct C++ call and is not
caught: if the trace never prints, trace the caller instead. Run with `run.py boot`, copy the
`[ab-trace]` line into `console/ab/port.txt`, and remove the override when done.

**Diff.** `abdiff.py regs console/ab/pcsx2.json console/ab/port.txt --width 32` (the port side
prints 32-bit values). If `pc` differs, the two sides are not at the same point yet. For memory,
dump the same range on both sides and run `abdiff.py mem A.bin B.bin --base 0x<start>`; ignore
counters and timers with `--ignore-range`.

When A/B helps: a crash at a known address with an unknown cause, a function returning the wrong
value, DMA or GS output that looks wrong while the code looks right. When it does not: build
errors, a missing stub (the log already names it), TOML mistakes.
