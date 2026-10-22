# Ghidra and PCSX2: reading the original code, and watching it run

Adapted from hkmodd/ps2-recomp-Agent-SKILL (MIT), updated for PS2Recomp as of October 2026.

Everything here is optional. The pipeline works with the Ghidra window alone; the MCP servers just
let Claude read the decompiler and the emulator directly instead of asking the user to copy things.
When an MCP server is connected, use it rather than asking the user to look something up.

## Ghidra (static: what the code does)

1. Install Ghidra and the `ghidra-emotionengine-reloaded` extension
   (https://github.com/chaoticgd/ghidra-emotionengine-reloaded): pick the extension release built for
   your Ghidra version, then File > Install Extensions and restart. Plain MIPS decompiles PS2 code
   badly (128-bit registers, MMI, VU0 macro instructions).
2. Import the boot ELF from `console/disc/`, choose the Emotion Engine (R5900) language the extension
   adds, and run auto-analysis.
3. Script Manager > Manage Script Directories: add `<PS2Recomp>/ps2xRecomp/tools/ghidra`. Run
   `ExportPS2Functions.java` (category PS2Recomp). It asks for the TOML path first, then the CSV path:
   save both in `console/`. See pipeline.md, Phase 2, for what it writes.
4. Rename functions as you learn them; record each in `console/design.md` (code map). The export uses
   Ghidra's names, and the generated file names and headers follow them, so a re-export plus re-run
   rebuilds the whole runner: rename freely, re-export only when you need new function bounds, in
   batches.

Decompiler tells: `qword`/`undefined16` locals and odd 128-bit moves mean MMI or VU0 code; a
signature with too few arguments means Ghidra missed `a2`/`a3` or `t0` to `t3` (fix the signature).

### GhydraMCP (optional)

GhydraMCP is a Ghidra extension plus an MCP bridge (https://github.com/starsong-consulting/GhydraMCP;
install it by its README). The base skill's playbook used these tool names (prefix `mcp_ghydra_`);
in Claude Code they appear as `mcp__<your server name>__<tool>`, so use the names your client lists:

| Tool (base playbook name) | Use |
| --- | --- |
| `instances_list`, `instances_use(port)` | find and select the open Ghidra project |
| `functions_decompile(address)` | read what `sub_00123456` really does |
| `xrefs_list(to_addr)` | who calls it, who reads that address |
| `data_list_strings(filter)` | find systems by their strings ("memcard", "load", "pad") |
| `functions_rename(address, new_name)` | name what you learned |
| `functions_set_signature(address, signature)` | fix missed arguments |

Recipes:
- **Unknown function at a crash PC:** decompile it, list xrefs to it, decompile the caller at
  `RA - 8`. Write one line: what it does, which layer (guardrails.md section 5) handles that.
- **Find the per-frame function** (needed by /console-mod's frame hooks and /console-physics): xrefs
  to the vsync wait or to the pad read; the function that calls both once per loop is the frame.
- **Find a system by its strings:** filter strings, xref the string, decompile the user.

## PCSX2 (dynamic: what the code did)

PCSX2 running the same dump is the ground truth. Its built-in debugger (breakpoints, memory view,
memory search, disassembly) is enough for the A/B below, done by hand with the user.

### PCSX2-MCP (optional, unverified here)

The base skill drove a PCSX2 build with a debug server and an MCP bridge. This pack has not verified
that project: install and connect it by its own README, and check the tool list your client shows.
The base playbook's tools (prefix `mcp_pcsx2_`):

| Group | Tools |
| --- | --- |
| connect | `pcsx2_connect`, `pcsx2_game_info` (check it is the same game), `pcsx2_status` |
| execution | `pcsx2_pause`, `pcsx2_continue`, `pcsx2_step`, `pcsx2_step_over` |
| registers | `pcsx2_read_registers(category)`, `pcsx2_evaluate(expression)`, `pcsx2_write_register` (user OK first) |
| memory | `pcsx2_read_memory(addr, format)`, `pcsx2_read_string`, `pcsx2_find_pattern` (`??` wildcards), `pcsx2_memory_diff(addr, name)`, `pcsx2_write_memory` (save state first) |
| breakpoints | `pcsx2_set_breakpoint(addr, condition, temporary)`, `pcsx2_remove_breakpoint`, `pcsx2_list_breakpoints`, `pcsx2_clear_all_breakpoints`, `pcsx2_set_watchpoint(addr, type)`, `pcsx2_remove_watchpoint`, `pcsx2_list_watchpoints` |
| code and state | `pcsx2_disassemble(addr, count)`, `pcsx2_save_state(slot)`, `pcsx2_load_state(slot)`, `pcsx2_get_threads`, `pcsx2_get_modules` |

Safety: pause before reading registers (running values are noise); save a state before any memory
write; never write a register without the user's OK; remove breakpoints when done; make sure the game
has reached the point where the code is loaded before breaking on it.

### The A/B comparison (the first divergence is the bug)

Use it for a crash or a wrong value with no obvious cause. Not for build errors, missing stubs
(the log already names them) or TOML problems.

1. **Side A, PCSX2.** Get to the same moment, break on the function, record `a0` to `a3`, `sp`,
   `ra`, the memory it reads, then its return value (`v0`) at its `jr ra`.
2. **Side B, the port.** Wrap the same function in a trace override and run it once:

```cpp
#include "game_overrides.h"
#include "ps2_runtime.h"
#include <cstdio>

namespace
{
    PS2Runtime::RecompiledFunction g_slus20312TraceOrig = nullptr;

    void slus20312TraceUpdate(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        std::fprintf(stderr, "[trace] in  a0=0x%08X a1=0x%08X ra=0x%08X\n",
                     getRegU32(ctx, 4), getRegU32(ctx, 5), getRegU32(ctx, 31));
        g_slus20312TraceOrig(rdram, ctx, runtime);
        std::fprintf(stderr, "[trace] out v0=0x%08X\n", getRegU32(ctx, 2));
    }

    void slus20312TraceApply(PS2Runtime &runtime)
    {
        g_slus20312TraceOrig = runtime.lookupFunction(0x00123456u);
        runtime.replaceFunction(0x00123456u, &slus20312TraceUpdate);
    }
}

PS2_REGISTER_GAME_OVERRIDE("trace 0x00123456", "SLUS_203.12", 0u, 0u, slus20312TraceApply);
```

3. **Diff.** Same inputs and different output: the bug is inside (translation or a runtime call it
   makes). Different inputs: walk back to the caller. Register and memory dumps can be compared with
   /console-tester's `abdiff.py`.
4. Fix with one of the four tools, rebuild, re-run both sides, and write the result under "Learned
   patterns" in STATE.md. Remove the trace override afterwards.

A replacement that does NOT call the original must set `ctx->pc = getRegU32(ctx, 31);` before it
returns, like the runtime's own `ret0`/`ret1`/`reta0` handlers do.
