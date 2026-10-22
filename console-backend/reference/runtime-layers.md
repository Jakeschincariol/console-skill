# Runtime layers: how a recompiled game reaches the host

Adapted from hkmodd/ps2-recomp-Agent-SKILL (MIT), updated for PS2Recomp as of October 2026.

Checked against `ran-j/PS2Recomp` commit eb8688b. Paths are under `ps2xRuntime/src/lib/` unless they
start with another folder. Grep the user's checkout before relying on a detail.

## 1. The three layers

| Layer | What it is | Who edits it |
| --- | --- | --- |
| Recompiled code | one C++ function per guest function in `ps2xRuntime/src/runner/`, operating on `R5900Context` (registers) and `rdram` (32 MB guest RAM) | nobody: `ps2_recomp` writes it |
| Runtime | `ps2xRuntime` + `ps2xIOP`: memory, the function table, syscalls, SDK stubs, GS/VU, audio, pad, files, the IOP | you, in `.cpp` files |
| Host | raylib (window, input, audio), the file system, FFmpeg (movies) | nobody: libraries |

Dispatch is by address, never by name. The generated code and the runtime look functions up in one
dense table indexed by guest address (`lookupFunction(addr)`, `replaceFunction(addr, fn)`;
`registerFunction` is the same call). Renaming a function in Ghidra changes nothing at run time; binding
an address does. An address with no entry logs `Error: No exact recompiled function for guest PC
0x... codeRegion=yes|no` and stops.

## 2. How a call reaches a handler

| The guest does | The runtime does | Where |
| --- | --- | --- |
| `jal` to a function the TOML binds (`"sceCdRead@0x00123456"` or a known name in `stubs`) | the generated wrapper sets `ctx->pc = ra` and calls `ps2_stubs::sceCdRead` or `ps2_syscalls::<Name>` | handler in `Kernel/Stubs/*.cpp` or `Kernel/Syscalls/*.cpp` |
| `jal` to a stub whose name has no handler | the wrapper calls `TODO_NAMED("<name>")`: logs `Warning: Unimplemented PS2 stub called. name=... PC=..., RA=...` and throws | `Kernel/Stubs/Unimplemented.cpp` |
| `jal` to a `skip` function | always `TODO_NAMED`: logs and throws (skip is NOT step-over) | same |
| `syscall` | `runtime->handleSyscall(rdram, ctx, encoded)`: the number is the encoded immediate, else `$v1`; `Kernel/Syscalls/Dispatcher.cpp` switches on it | unknown numbers log `[Syscall TODO]` + `Warning: Unimplemented PS2 syscall called` and RETURN 0 (the game carries on) |
| SIF RPC to the IOP | ps2xIOP delivers it to the IRX module that registered the SID, or a generic HLE service | `Kernel/Syscalls/RPC.cpp`, `ps2_iop_host.cpp`, `ps2xIOP/` |

The recompiler also auto-binds `J`/`JAL` call sites through relocation symbols when the ELF has them,
so symbol-rich builds need fewer manual bindings.

A handler has this shape (the same for stubs, syscalls and override functions):

```cpp
void Name(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime);
// arguments:  getRegU32(ctx, 4) .. getRegU32(ctx, 7) are a0..a3; 8..11 are t0..t3 (args 5..8)
// return:     setReturnU32(ctx, v) or setReturnS32(ctx, v) (v0)
// guest RAM:  getMemPtr(rdram, guestAddr) returns a host pointer, or nullptr if the address is invalid
```

A function that replaces a guest function (instead of calling the original) must leave with
`ctx->pc = getRegU32(ctx, 31);`, exactly as the runtime's `ret0`, `ret1` and `reta0` do
(`Kernel/Stubs/Compatibility.cpp`).

## 3. Triage stubs are temporary

`ret0`, `ret1` and `reta0` return 0, 1 or the first argument. Bind them with `"ret0@0xADDR"` in the
TOML's `stubs`, or `ps2_game_overrides::bindAddressHandler(runtime, 0xADDR, "ret0")` in an override.
They answer one question: does the game get further if this function "succeeds"? Then promote:

1. Find out what the function is (Ghidra: decompile it and its callers; strings nearby).
2. An SDK function the runtime implements: bind the real handler by name.
3. A runtime handler that is still a `TODO_NAMED` (`stubs.py handlers --repo ... --todo`): implement
   it in its `Kernel/Stubs/*.cpp` file, for every game.
4. Game code the translation breaks: a game override replacing that one function.
5. A loop waiting on hardware: implement that device in the runtime, or an override for a
   game-specific wait.

Every triage binding goes in STATE.md's stub table marked TEMPORARY. A `ret0` on a function that
allocates memory or creates a thread hides a crash that comes back later as something stranger.
More than about five overrides for one subsystem means the runtime is missing that subsystem.

To learn what a stubbed function receives before deciding, probe it for one run (a game override;
the source lives in `console/patches/`):

```cpp
#include "game_overrides.h"
#include "ps2_runtime.h"
#include <cstdio>

namespace
{
    void slus20312Probe00123456(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t a0 = getRegU32(ctx, 4);
        std::fprintf(stderr, "[probe] 0x00123456 a0=0x%08X a1=0x%08X a2=0x%08X ra=0x%08X\n",
                     a0, getRegU32(ctx, 5), getRegU32(ctx, 6), getRegU32(ctx, 31));
        if (const uint8_t *p = getMemPtr(rdram, a0))
        {
            std::fprintf(stderr, "[probe]   *a0 = %02X %02X %02X %02X\n", p[0], p[1], p[2], p[3]);
        }
        setReturnS32(ctx, 0);              // temporary: record it in STATE.md
        ctx->pc = getRegU32(ctx, 31);      // return to the caller
    }

    void slus20312ProbeApply(PS2Runtime &runtime)
    {
        runtime.replaceFunction(0x00123456u, &slus20312Probe00123456);
    }
}

PS2_REGISTER_GAME_OVERRIDE("probe 0x00123456", "SLUS_203.12", 0u, 0u, slus20312ProbeApply);
```

## 4. Adding or fixing a handler without a full rebuild

| Change | Files | Rebuild cost |
| --- | --- | --- |
| Implement a handler that exists but is a `TODO_NAMED` | its `Kernel/Stubs/<File>.cpp` | the runtime library only |
| Map an unhandled syscall number to an existing handler | a `case` in `Kernel/Syscalls/Dispatcher.cpp` | the runtime library only |
| New syscall handler | the function in the matching `Kernel/Syscalls/<File>.cpp`, its declaration in the private `Kernel/Syscalls/<File>.h`, a `case` in Dispatcher.cpp | the runtime library only; the private kernel headers are not in the runner's precompiled header (still say so before editing) |
| New NAMED stub for `name@0xADDR` bindings | `X(Name)` in `PS2_STUB_LIST`, `ps2xRuntime/include/ps2_call_list.h` | EVERY generated file: ask first |
| Same behaviour, no header | a game override: `runtime.replaceFunction(0xADDR, myFn)` | the override file only |

Negative syscall numbers (interrupt-context variants) are written
`case static_cast<uint32_t>(-0x1A):` in Dispatcher.cpp. The handler-name lists are macros:
`#define PS2_SYSCALL_LIST(X)` and `#define PS2_STUB_LIST(X)` with one `X(Name)` per handler; names
resolve with or without one leading underscore. `ps2xRuntime/include/ps2_stubs.h` pulls in
`Kernel/Stubs/Unimplemented.h`, so that one kernel header IS in the runner's precompiled header.

## 5. The IOP (ps2xIOP)

ps2xIOP runs the game's own IRX modules on an R3000A interpreter with a virtual IOP kernel: no BIOS.
A physical IRX RPC server is authoritative for its SID. Generic HLE services exist only for MCSERV
(SIDs `0x80000400`, `0x80000480`), LIBSD (`0x80000701`) and DBCMAN (`0x80001300`), each switched on
when its module is loaded (`ps2xIOP/src/modules/{mcserv,libsd,dbcman}.cpp`). Unknown modules fail to
load and unknown SIDs stay unhandled. IOP RAM is separate from EE RAM.

What to read: `[SIF module] load-emulated id=N ref=1 path="cdrom0:\MODULES\X.IRX;1"` (a module
loaded), `[IOP/RPC trace:unhandled] sid=0x... rpc=0x... pc=... ra=... loadedModules=[...]` (nobody
answered; needs `PS2X_ENABLE_IOP_RPC_TRACE`, on by default), `[ps2xIOP:error] ...` and
`[ps2xIOP:warning] ...`. `rom0:` modules come from the runtime's ROM device (`ps2_rom_device.cpp`).
The F1 debug panel shows loaded modules, HLE services, SIF RPC state and an RPC tracer. ps2xIOP's own
tests build separately: `cmake -S ps2xIOP -B out/build/iop-tests -DPS2X_IOP_BUILD_TESTS=ON`.

Order of suspicion for an unhandled SID: the IRX is missing from the extracted disc (or its name's
case is wrong), the module failed to load (an import the virtual kernel lacks:
`ps2xIOP/src/emulator/`), then a service the game expects from a BIOS module.

## 6. Disc and file I/O

`PS2Runtime::IoPaths` holds `elfPath`, `elfDirectory`, `hostRoot`, `cdRoot`, `mcRoot` and `cdImage`.
`loadELF` sets them from the ELF given as `argv[1]`: `cdRoot` and `hostRoot` are the ELF's folder,
`mcRoot` is `<that folder>/mc0`, and `cdImage` is empty. Game overrides run after that, so an override
can change them with `PS2Runtime::getIoPaths()` and `PS2Runtime::setIoPaths(paths)`.

| Guest path | Resolves to | Notes |
| --- | --- | --- |
| `cdrom0:\DATA\X.BIN;1` | `cdRoot/DATA/X.BIN` | `;1` stripped, backslashes become slashes, case kept as written |
| `host:` / `host0:` | `hostRoot/...` | |
| `mc0:` | the memory card folder | /console-saves fills it |
| `rom0:` | the ROM device | BIOS module names |

`sceCdSearchFile` also has a case-insensitive fallback index and gives each found file a pseudo sector
number; `sceCdRead` of those sectors reads the host file. Plain file opens use the path as written,
so on Linux keep the disc's upper case. A game that reads sectors by number from its own table logs
`sceCdRead unresolved LBN 0x... (no mapped file and no configured CD image)`: give the runtime the
user's own image (2048-byte sectors) from the game override:

```cpp
#include "game_overrides.h"
#include "ps2_runtime.h"

namespace
{
    void slus20312DiscImage(PS2Runtime &runtime)
    {
        (void)runtime;
        PS2Runtime::IoPaths paths = PS2Runtime::getIoPaths();
        paths.cdImage = "/Users/you/Games/console/game.iso";   // the user's own dump; never shared
        PS2Runtime::setIoPaths(paths);
    }
}

PS2_REGISTER_GAME_OVERRIDE("disc image", "SLUS_203.12", 0u, 0u, slus20312DiscImage);
```

Swapping a disc file needs no ISO rebuild: replace it in the extracted folder (same name), keep the
original in a backup. /console-levels does this for level data.

## 7. Which subsystem file

| Symptom or log | First file to read |
| --- | --- |
| threads, semaphores, event flags, alarms | `Kernel/Syscalls/Thread.cpp`, `Kernel/Syscalls/Sync.cpp`, `Kernel/EeScheduler.cpp` |
| interrupts, vsync | `Kernel/Syscalls/Interrupt.cpp` |
| memory setup, OSD config, `GsSetCrt`, `SetSyscall`, heap | `Kernel/Syscalls/System.cpp` |
| SIF RPC, module loading | `Kernel/Syscalls/RPC.cpp`, `Kernel/Stubs/SIF.cpp`, `ps2_iop_host.cpp` |
| `fio*` errors | `Kernel/Syscalls/FileIO.cpp`, `ps2_vfs.cpp` |
| `sceCd*` | `Kernel/Stubs/CD.cpp`, `Kernel/Stubs/Helpers/Support.h` |
| `sceMc*`, `mc*` | `Kernel/Stubs/MemoryCard.cpp` |
| `scePad*` | `Kernel/Stubs/Pad.cpp`, `ps2_pad.cpp` (/console-controller) |
| `sceSd*`, sound banks | `Kernel/Stubs/Audio.cpp`, `ps2_audio.cpp` (/console-sound) |
| `sceGs*`, `sceGif*`, `sceVif1*` | `Kernel/Stubs/GS.cpp`, `gs/` (/console-graphics) |
| `sceMpeg*`, `sceIpu*` | `Kernel/Stubs/MPEG.cpp`, `Kernel/Stubs/IPU.cpp` |
| libc (`malloc`, `printf`, `str*`) | `Kernel/Stubs/LibC.cpp`, `Kernel/Stubs/Compatibility.cpp` |
