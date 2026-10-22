# Mod layers: where each kind of change goes in a PS2Recomp port

## The four fix layers

| layer | file | rebuild cost | good for |
| --- | --- | --- | --- |
| TOML instruction patch | the game's ps2_recomp TOML: `[[patches.instructions]]` with `address` and `value` (a raw 32-bit instruction word) | re-run `ps2_recomp`, then an incremental build of the changed functions | changing what an instruction does |
| game override | `console/patches/<slug>.cpp`, compiled into `ps2EntryRunner` | one file | data writes, per-frame writes, replacing or wrapping functions |
| asset swap | files in the extracted disc folder next to the ELF (the runtime's `cdRoot`) | none | textures, sounds, levels (`/console-graphics`, `/console-sound`, `/console-levels`) |
| runtime handler | `ps2xRuntime/src/lib/Kernel/Stubs/*.cpp` (`/console-backend`) | one file | behaviour every game shares; not for one game's mod |

## How ps2_recomp applies an instruction patch (checked in ps2_recompiler.cpp)

- `config_manager.cpp` reads `[[patches.instructions]]`: `address` and
  `value` as strings (`"0x0012A3B4"`, parsed with base 0) or integers. The
  last entry for an address wins.
- `PS2Recompiler::decodeFunction` swaps the word in while decoding a
  function, so a patch outside every recompiled function is silently unused.
- The patch is classified by the ORIGINAL instruction: if the original is a
  SYSCALL, a COP0 instruction or a CACHE instruction, it applies only with
  `patch_syscalls`, `patch_cop0` or `patch_cache` set to true in `[general]`.
  Everything else always applies.
- The patched word is recompiled like any other instruction. If it changes a
  branch or jump, re-read the ps2_recomp report; add `entry_points` if a new
  target is reported missing.

## How a game override runs (checked in ps2_runtime.cpp and game_overrides.cpp)

`PS2Runtime::loadELF` copies every PT_LOAD segment into RDRAM, zero-fills the
rest of each segment, computes the CRC-32 of the ELF file, then calls
`ps2_game_overrides::applyMatching`. Every registered override whose ELF name
(case-insensitive), entry point and CRC-32 match (0 means "any") has its apply
function called once. So at apply time the ELF's data is in RDRAM and the
game's code has not run.

The game's own startup code then clears its BSS and sets up its heap. That is
why BSS and heap writes go in the frame hook.

## Frame hooks

PCSX2 re-applies place 1 patches every vsync. The port has no vsync patch
hook, so `pnach.py override --frame-hook 0xADDR` wraps one of the game's
functions that runs once per frame: the wrapper writes the values, then calls
the game's function.

How to find one:

1. In Ghidra, follow the main loop from the entry: the loop body calls the
   game's per-frame update, and something in it waits for vsync (a call into
   `sceGsSyncV`, or a wait on a semaphore a vsync interrupt handler signals).
   A function called from the loop body once per pass is a good hook.
2. Check it in PCSX2: a breakpoint with a hit counter on it should count about
   60 times a second (NTSC) or 50 (PAL) during gameplay.
3. It must be reached with `jal` (or `jalr`). The wrapper goes into the
   runtime's function table, and only calls that go through the table see it:
   in `control_flow_emitter.cpp` a `jal` becomes
   `runtime->dispatchGuestBranch(...)` (a table lookup), but a `j` to a known
   function becomes a direct call, `sub_XXXXXXXX(rdram, ctx, runtime); return;`,
   which skips the table. `pnach.py override --frame-hook` counts the `jal` and
   `j` instructions that reach the address and warns. If only `j` reaches it,
   pick the caller one level up (the function that `j` sits in is itself
   called with `jal`), or confirm in Ghidra (references of type CALL, not
   JUMP). To double-check the generated code, search it for that direct-call
   pattern with the function's name; never list or edit `src/runner/`.
4. It must be a function start (the CSV's `Start` column). The tool refuses an
   address outside the code and warns when the map disagrees.

Writing every frame is cheap: a few memory stores. If a value must only be
written while a menu is open, write C++ that reads the game state first.

## Code caves

Cheat makers often write a few new instructions into unused memory and patch
a `j` or `jal` to reach them. A recompiled port has no function for that new
code, so `classify` marks the jump and the new code CAVE. Port it by reading
what the cave does (it is usually short) and writing the same logic in C++:

- if the cave runs instead of the original call: `override.py new --replace
  0xTARGET=fn` on the function the `jal` used to call;
- if it runs extra code then continues: `--wrap`.

## Installing override modules

`ps2xRuntime/CMakeLists.txt` builds `ps2EntryRunner` from `src/runner/*.cpp`
(generated) and links the `ps2_runtime` STATIC library (an explicit file list,
`game_overrides.cpp` among them, plus a `src/lib/Kernel/*.cpp` glob). Override
modules must be compiled into the executable itself: an object file in the
static library that nothing references (and a module holding only a
`PS2_REGISTER_GAME_OVERRIDE` registration is exactly that) is dropped by the
linker, and its registration never runs. So never drop a module into
`src/lib/` or `src/lib/Kernel/`. Instead:

1. Keep modules in `console/patches/` (never in `src/runner/`, which belongs
   to ps2_recomp).
2. Add once, at the end of `ps2xRuntime/CMakeLists.txt`:
   `include("<abs path>/console/patches/console_mods.cmake" OPTIONAL)`.
   It adds every `.cpp` in that folder to `ps2EntryRunner` with
   `SKIP_UNITY_BUILD_INCLUSION`, so the unity batches (and the recompiled game)
   are not rebuilt.
3. `cmake --build out/build --config Debug`. Adding or removing a module
   re-runs the configure step (the glob uses `CONFIGURE_DEPENDS`).

Each generated module lives in its own `console_mod_<slug>` namespace, so
several mods can be built together, even inside one unity batch.

## Testing order

1. PCSX2 with the pnach: proves the address and the value.
2. The port with the TOML patch and the override.
3. `/console-tester`: boot with `--expect`, compare a run with and without the
   mod, then play the part of the game the mod touches.
