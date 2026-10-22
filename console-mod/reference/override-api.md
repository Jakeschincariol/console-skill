# The PS2Recomp game override API (as it exists in the checkout)

Every name here was checked in `ps2xRuntime/include/game_overrides.h`,
`ps2xRuntime/include/ps2_runtime.h`, `ps2xRuntime/include/ps2_runtime_macros.h`,
`ps2xRuntime/include/runtime/ps2_memory.h` and their `.cpp` files. The original
agent skill's template used `Runtime.h` and `Runtime::OverrideFunction`, which
do not exist; `templates/override.cpp` is the corrected version.

## Registration

```cpp
#include "game_overrides.h"
#include "ps2_runtime.h"
#include "ps2_runtime_macros.h"

void applyOverride(PS2Runtime &runtime);   // ps2_game_overrides::ApplyFn
PS2_REGISTER_GAME_OVERRIDE("My mod", "SLUS_203.12", 0x00100008u, 0x1A2B3C4Du, &applyOverride)
```

- Arguments: a name for the log, the ELF file name (compared case-insensitively
  with the booted file's name), the entry point, the CRC-32 of the whole ELF
  file, the apply function. Entry 0 or CRC 0 match anything; ship real values.
- `override.py keys <ELF>` prints the right three keys.
- The macro expands to a static registration in an anonymous namespace (names
  built from `__LINE__`), so keep each module in its own named namespace.

## Inside apply

| call | what it does |
| --- | --- |
| `runtime.memory().getRDRAM()` | `uint8_t *` to the 32 MB of main RAM, already holding the ELF |
| `Ps2FastWrite8/16/32/64(rdram, addr, v)` | write guest memory (address masked with `PS2_RAM_MASK`) |
| `Ps2FastRead8/16/32/64(rdram, addr)` | read guest memory |
| `runtime.hasFunction(addr)` | true when the generated function table has an entry at `addr` |
| `runtime.lookupFunction(addr)` | the current `PS2Runtime::RecompiledFunction` at `addr` (logs an error and returns a fallback when there is none, so check `hasFunction` first) |
| `runtime.replaceFunction(addr, fn)` | put `fn` in the table at `addr`; false when `addr` is outside the generated table |
| `runtime.registerFunction(addr, fn)` | the same as `replaceFunction` (kept for old code) |
| `ps2_game_overrides::bindAddressHandler(runtime, addr, "name")` | route `addr` to a runtime syscall or stub handler by name (from `PS2_SYSCALL_LIST` / `PS2_STUB_LIST` in `ps2_call_list.h`; a leading underscore alias is accepted); false and a log line when the name is unknown |

## Writing a replacement function

```cpp
void myFunction(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
{
    const uint32_t a0 = getRegU32(ctx, 4);   // $a0
    const float f12 = ctx->f[12];            // first float argument ($f12)
    // ... read and write guest memory with Ps2FastRead32 / Ps2FastWrite32 ...
    setReturnU32(ctx, 1u);                   // $v0 (setReturnS32 for signed)
    ctx->pc = getRegU32(ctx, 31);            // return to the caller
}
```

| register | number | use |
| --- | --- | --- |
| `$v0`, `$v1` | 2, 3 | return values |
| `$a0` to `$a3` | 4 to 7 | first four integer or pointer arguments |
| `$t0` to `$t3` | 8 to 11 | arguments 5 to 8 (the PS2 toolchain uses the MIPS EABI), otherwise temporaries |
| `$s0` to `$s7` | 16 to 23 | saved: a replacement must leave them as it found them |
| `$gp`, `$sp`, `$fp`, `$ra` | 28, 29, 30, 31 | global pointer, stack, frame, return address |
| `ctx->f[0]` | | float return |
| `ctx->f[12]`, `ctx->f[13]`, ... `ctx->f[19]` | | float arguments, in order (EABI) |

The runtime's own triage stubs end exactly like this (`ret0`, `ret1`, `reta0`
in `Kernel/Stubs/Compatibility.cpp`): set `$v0`, then `ctx->pc = $ra`.

## Wrapping (call the game's function, then adjust)

```cpp
PS2Runtime::RecompiledFunction g_original = nullptr;

void wrapper(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
{
    const uint32_t returnAddress = getRegU32(ctx, 31);
    g_original(rdram, ctx, runtime);
    if (ctx->pc != returnAddress)
        return;          // paused by the EE scheduler: it resumes later without us
    setReturnU32(ctx, getRegU32(ctx, 2) * 3u);
}

void applyOverride(PS2Runtime &runtime)
{
    if (runtime.hasFunction(0x00123450u))
    {
        auto current = runtime.lookupFunction(0x00123450u);
        if (current != &wrapper)
            g_original = current;           // safe if loadELF runs twice
        runtime.replaceFunction(0x00123450u, &wrapper);
    }
}
```

Why the `ctx->pc` check: generated functions can return early at a scheduler
checkpoint (`eeCheckpointDue`) with `ctx->pc` inside the function, and resume
later through the function table at that inner address, which still points at
the game's function. Only adjust results when the call really finished.

## Which calls see a replacement

The table is a dense array (`g_ps2RecompiledFunctionTable`, generated into
`register_functions.cpp`). `jal`, `jalr` and register jumps go through
`dispatchGuestBranch`, which looks the target up in the table: they see your
function. A `j` to a known function is emitted as a direct C++ call
(`sub_XXXXXXXX(rdram, ctx, runtime); return;`) and does not, and neither does
a `jal` the recompiler bound by relocation to a runtime handler (those call
`ps2_stubs::`/`ps2_syscalls::` directly). `override.py new` and `pnach.py
override --frame-hook` count the `jal` and `j` instructions that reach each
address and warn about `j`. If a target is reached only by `j`, replace or
wrap the caller one level up instead. If a hook never fires, check the
callers in Ghidra (CALL versus JUMP references).

## Where the module is compiled

`ps2_runtime` is a static library. A `.cpp` that only holds
`PS2_REGISTER_GAME_OVERRIDE` (a static self-registration object) is dropped
by the linker if it is compiled into that library, because nothing references
it. Compile modules into the `ps2EntryRunner` executable instead:
`console/patches/console_mods.cmake` does that (see `mod-layers.md`).

## Rules

- Never edit `ps2xRuntime/include/*.h`: every generated file includes them.
- Never put modules in `ps2xRuntime/src/runner/`: that folder is ps2_recomp's
  output and is derived from the game's code.
- Keep each module's names in its own namespace (`console_mod_<slug>`).
