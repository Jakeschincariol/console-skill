---
name: console-mod
description: >-
  Mod Maker. Describe any change in plain English and Claude builds it into the
  game: patches, cheats, new behaviour, as code that never touches the original
  files. Ports PCSX2 .pnach patches to the PS2Recomp port (code writes become
  ps2_recomp TOML instruction patches, data writes become a C++ game override
  that writes RDRAM at load or every frame, the rest are flagged for a manual
  port with the reason) and scaffolds game overrides that bind, replace or wrap
  a game function with the real PS2Recomp API. Writes to console/patches/,
  never to the game's files, the generated runner/*.cpp or headers. Use when the
  user says "mod this game", "add a cheat", "infinite health", "port this
  pnach", "make my gun do X", "change how X works", "game override", "turn this
  PCSX2 patch into the port", or once the port boots and they want to change it.
argument-hint: "pnach.py <parse|classify|toml|override|crc> | override.py <new|keys> [flags]"
---

# console-mod

The Mod Maker turns a request ("my rifle does triple damage", "start with 99
lives", "skip the intro") into a change built into the port. A mod is a few
small text files you own: a `.pnach` to try the idea in PCSX2, TOML instruction
patches for `ps2_recomp`, and a C++ game override compiled into the runner. The
disc, the ELF and the recompiled C++ are never edited.

Be honest about the hard part: finding WHERE the game keeps a value or does a
thing is the real work, and it differs per game. This skill gives the method
and the tools. Big mods ("two games in one", new modes, new levels) are long
projects built from these same pieces, one function and one value at a time.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/pnach.py" crc SLUS_203.12                       # both CRCs + the pnach name PCSX2 wants
python3 "${CLAUDE_SKILL_DIR}/pnach.py" parse my.pnach                         # every line, section and code kind
python3 "${CLAUDE_SKILL_DIR}/pnach.py" classify my.pnach --elf SLUS_203.12   # CODE / DATA / BSS / HEAP / OUTSIDE / CAVE
python3 "${CLAUDE_SKILL_DIR}/pnach.py" toml my.pnach --elf SLUS_203.12       # [[patches.instructions]] blocks
python3 "${CLAUDE_SKILL_DIR}/pnach.py" override my.pnach --elf SLUS_203.12 --frame-hook 0x0012A3B0
python3 "${CLAUDE_SKILL_DIR}/override.py" new --name "Gold rifle" --elf SLUS_203.12 --wrap 0x00123450=gold_damage
python3 "${CLAUDE_SKILL_DIR}/override.py" keys SLUS_203.12                    # keys for a hand-written override
```

Other flags: `--map console/functions.csv` (the CSV from Ghidra's
`ExportPS2Functions.java`: tells code from data in single-segment ELFs and
checks function starts), `--section NAME` (port only some `[sections]`),
`--ignore-crc`, `--json`, `--out`, `--force`, `--repo <PS2Recomp>` (checks
`--bind` handler names against `ps2_call_list.h`). Exit 1 means something needs
a manual port or a warning; read it out.

## What it reads and writes

| file | what |
| --- | --- |
| the game's main ELF (`SLUS_203.12`) | read only: segments, sections, original instruction words, both CRCs |
| `*.pnach` | read only: PCSX2 patches, yours or a community patch you trust |
| `console/functions.csv` | optional: `Name,Start,End,Size` from `ExportPS2Functions.java` |
| `console/patches/<slug>.cpp` | the override module: yours, safe to share |
| `console/patches/console_mods.cmake` | written once: compiles every `.cpp` in that folder into `ps2EntryRunner` |
| the game's ps2_recomp TOML | never written by the tools: paste the `toml` output yourself, after a backup |
| `templates/override.cpp` | the corrected override template both tools fill in (the original skill's template called an API that never existed) |
| `templates/console_mods.cmake` | copied next to the overrides the first time |

## From a request to a mod

1. **Pin the change down.** "Triple damage" means: which number, for which
   weapon, when. Ask one question if it is ambiguous. Write it in
   `console/STATE.md` as the goal.
2. **Pick the layer.**

   | the change | layer | how |
   | --- | --- | --- |
   | a value the game keeps in RAM (lives, ammo, money, a timer) | data write, re-applied every frame if the game rewrites it | pnach, then `pnach.py override --frame-hook` |
   | a constant (speed, gravity, jump height, damage table) | data or a constant built in code | `/console-physics` finds it, `tune.py patch` writes the lines |
   | one instruction (skip a check, stop a counter going down) | code patch | pnach, then `pnach.py toml` |
   | new behaviour (a function does something else) | function override | `override.py new --replace` or `--wrap` |
   | the runtime already has a handler for that function | bind | `override.py new --bind 0xADDR=handler` (or `handler@0xADDR` in TOML `stubs`) |
   | a texture, a sound, a level | asset swap | `/console-graphics`, `/console-sound`, `/console-levels` |

3. **Find the address.** PCSX2's debugger memory search while playing (search
   the value, change it in game, search again), then a write breakpoint shows the
   code that changes it; Ghidra (GhydraMCP if installed, see `/console-reverse`)
   for the function and its callers; `/console-physics` `tune.py scan` for
   floats. Addresses belong to ONE build: always use the user's ELF.
4. **Test in PCSX2 first.** `pnach.py crc <ELF>` prints the file name PCSX2
   looks for (`SLUS-20312_1A2B3C4D.pnach`, in its `patches/` or `cheats/`
   folder). Write the lines, enable them, check the effect. Cheap to iterate.
5. **Bake it into the port.**
   - `pnach.py classify` first. Read out every class and every manual line.
   - CODE: `pnach.py toml` prints `[[patches.instructions]]` blocks. Back up the
     game's TOML, paste them at the end, re-run `ps2_recomp`, rebuild
     incrementally. ps2_recomp only patches words inside functions it decodes,
     and SYSCALL/COP0/CACHE originals need `patch_syscalls`/`patch_cop0`/
     `patch_cache = true` (the output says when).
   - DATA, BSS, HEAP: `pnach.py override` writes the module. Place 1 and 2
     writes and anything the game clears at boot need `--frame-hook`, a function
     the game calls once per frame AND reaches with `jal` (see
     [reference/mod-layers.md](reference/mod-layers.md)). A `j` tail jump to a
     known function compiles to a direct C++ call that skips the runtime's
     function table, so the hook would not run on that path. The tool counts
     the `jal` and `j` references and warns; if only `j` reaches it, hook the
     caller one level up. The same holds for `override.py --replace/--wrap/--bind`.
   - New behaviour: `override.py new`, then fill in the C++ (the stubs show how
     to read arguments with `getRegU32(ctx, 4)` and return with
     `setReturnU32`). API details: [reference/override-api.md](reference/override-api.md).
   - Install once per PS2Recomp checkout, with the user's OK: add
     `include("<abs path>/console/patches/console_mods.cmake" OPTIONAL)` at the
     end of `ps2xRuntime/CMakeLists.txt`. It adds every `.cpp` in
     `console/patches/` to the `ps2EntryRunner` executable itself: `ps2_runtime`
     is a static library, and the linker drops an object file there that only
     holds a self-registration, so never put overrides in `src/lib/`. Then
     `cmake --build out/build --config Debug`. The files stay out of the unity
     batches, so the recompiled game does not rebuild.
6. **Playtest** with `/console-tester` (`run.py boot ... --expect`) and play
   it yourself. The runtime logs `[game_overrides] applying '<name>'` when
   the module matched (runtime logs on).
7. **Record it**: one line per mod in `console/STATE.md` (name, files, status).

The pnach format, every line kind and the CodeBreaker extended codes are in
[reference/pnach-format.md](reference/pnach-format.md).

## What `classify` means

| class | where the write lands | becomes |
| --- | --- | --- |
| CODE | an executable section, a function in the Ghidra map, or an executable segment | a TOML instruction patch; byte and short writes are merged into the original word from the ELF |
| DATA | data the ELF loads from the file | an RDRAM write in the override at load (place 1/2: also every frame) |
| BSS | the zero-filled part of a segment | the frame hook: once on the first frame (place 0) or every frame (place 1/2) |
| HEAP | RAM beyond the ELF (heap, stack, loaded files) | the frame hook, same as BSS |
| OUTSIDE | scratchpad, hardware registers, BIOS, EE kernel RAM (below 0x00080000) | manual: no RDRAM write does what it did on a PS2 |
| CAVE | new code written into memory, a jump or branch into patched memory, or (with `--map`) code where no function is | manual: rewrite the logic as an override function |

A low-confidence CODE write (one read-write-execute segment, no sections, no
map) is both patched in the TOML and written to RDRAM, so it works whether it
is code or data. Pass `--map` to settle it. IOP patches, `dpatch` lines and
extended codes other than plain writes are listed as manual with the reason.

## Guard rails

1. **Bring your own game.** Only a game the user owns, dumped from their own
   disc. Never download or look for ISOs, ELFs or BIOS files, never ask for
   them. PS2Recomp needs no BIOS.
2. **Nothing from the game gets published.** Share the `.pnach`, the TOML
   patch lines, `console/patches/*.cpp` and the user's own assets. Never the
   disc, the ELF, IRX modules, ripped assets or the generated `runner/*.cpp`.
3. **Not affiliated with Sony.** PlayStation is a trademark of Sony
   Interactive Entertainment.
4. Never edit the game's files, `ps2xRuntime/src/runner/` or any header (a
   header change rebuilds every generated file); never clean the build. The
   tools refuse to write into `runner/`, refuse to overwrite without `--force`,
   and refuse a pnach whose file-name CRC is for another build. Back up the TOML
   before pasting. A mod that crashes the port is reverted first, then fixed.

## Hands off to

- `/console-physics` to find and tune the numbers (speed, gravity, frame rate).
- `/console-graphics`, `/console-sound`, `/console-levels` for asset swaps.
- `/console-tester` to boot, playtest and check nothing else broke.
- `/console-reverse` to re-run `ps2_recomp` after TOML patches and log STATE.md.
- `/console-anywhere` to package: mods ship as patches and overrides, never game files.
