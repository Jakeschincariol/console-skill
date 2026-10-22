# The PS2Recomp pipeline: toolchain, function map, TOML, recompile, build, run

Adapted from hkmodd/ps2-recomp-Agent-SKILL (MIT), updated for PS2Recomp as of October 2026.

Checked against `ran-j/PS2Recomp` at commit eb8688b (Oct 14 2026). PS2Recomp moves fast: when the
user's checkout is newer, grep it before trusting a path, flag or log line written here.

## The programs

| Program | CMake target folder | Single-config build output | Job |
| --- | --- | --- | --- |
| `ps2_analyzer` | `ps2xAnalyzer/` | `<build>/ps2xAnalyzer/ps2_analyzer` | ELF to TOML (fallback path) |
| `ps2_recomp` | `ps2xRecomp/` | `<build>/ps2xRecomp/ps2_recomp` | TOML + ELF to C++ |
| `ps2EntryRunner` | `ps2xRuntime/` | `<build>/ps2xRuntime/ps2EntryRunner` | the port: runtime + generated code |
| `ps2xStudio` | `ps2xStudio/` | `<build>/ps2xStudio/ps2xStudio` | optional GUI (`PS2X_BUILD_STUDIO`, on by default) |
| `ps2x_tests` | `ps2xTest/` | `<build>/ps2xTest/ps2x_tests` | upstream unit tests (`PS2X_BUILD_TEST`) |

With the Visual Studio generator each sits one folder deeper (`Debug/`, `Release/`) and ends in
`.exe`. `ps2_recomp` prints its usage as `ps2recomp <config.toml>`; the file is `ps2_recomp`.
`state.py detect` finds all three in the recorded build dir.

## Phase 0: the toolchain, per OS

Everyone: `git`, CMake 3.21 or newer (the top-level `CMakeLists.txt` asks for 3.21; the README
says 3.20), a C++20 compiler, and network access on the first configure (CMake fetches raylib 5.5,
imgui, and sse2neon on ARM).

```bash
git clone --recurse-submodules https://github.com/ran-j/PS2Recomp.git
```

**Linux (also the Steam Deck in desktop mode).** This is exactly what upstream CI runs:

```bash
sudo apt-get install -y build-essential cmake git ninja-build libgl-dev libx11-dev libxrandr-dev \
  libxinerama-dev libxcursor-dev libxi-dev pkg-config libavcodec-dev libavformat-dev libavutil-dev \
  libswresample-dev libswscale-dev
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_FLAGS=-msse4.1 -DCMAKE_CXX_FLAGS=-msse4.1
cmake --build build --config Release
```

The `-msse4.1` flags are required on x86-64 with GCC or Clang: the runtime headers use SSE4.1
intrinsics and the CMake files do not add the flag. ARM64 Linux needs no SSE flags (sse2neon).

**macOS.** Not in upstream CI, so expect to be first to hit some problems.

```bash
xcode-select --install
brew install cmake ninja pkg-config ffmpeg
cmake -S . -B out/build -G Ninja -DCMAKE_BUILD_TYPE=Release     # Intel Mac: add the two -msse4.1 flags
cmake --build out/build
```

On Apple silicon the top-level CMake sees arm64 and fetches sse2neon (`USE_SSE2NEON`) by itself.
FFmpeg is found through pkg-config and is REQUIRED unless you configure with
`-DPS2X_ENABLE_FFMPEG=OFF` (game movies then show stub frames).

**Windows.** Visual Studio 2022 with the C++ workload. Work inside the "x64 Native Tools Command
Prompt for VS 2022" (or after `vcvars64.bat`): `where cl` must print a path. FFmpeg is downloaded
as a prebuilt by CMake. Upstream CI builds with MSVC like this:

```bat
cmake -B build
cmake --build build --config Release
```

For big games the base skill measured clang-cl + Ninja at about an hour per full runner build
against most of a day for the Visual Studio generator. Install "C++ Clang Compiler for Windows" and
"C++ CMake tools for Windows" in the VS installer, then:

```bat
cmake -S . -B out/build -G Ninja -DCMAKE_C_COMPILER=clang-cl -DCMAKE_CXX_COMPILER=clang-cl ^
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_FLAGS_INIT=-msse4.1 -DCMAKE_CXX_FLAGS_INIT=-msse4.1
cmake --build out/build
```

clang-cl needs SSE4.1 switched on for the same intrinsics; the `_INIT` form keeps CMake's default
`/EHsc`. This exact line is not in upstream CI: read the first build's output before trusting it.
The base skill's advice to delete a unity-build block from `ps2xRuntime/CMakeLists.txt` is out of
date: unity build is now the option `PS2X_ENABLE_RUNNER_UNITY_BUILD` (on, batch 32), with a
precompiled header (`PS2X_ENABLE_RUNNER_PCH`) and sccache when it is on the PATH.

**Android and PS Vita** builds belong to /console-anywhere (`android/README.md`, `vita/build.sh`).

CMake options worth knowing (all real; changing one means a reconfigure that can rebuild a lot, so
ask first): `PS2X_BUILD_STUDIO`, `PS2X_BUILD_TEST`, `PS2X_ENABLE_RUNNER_UNITY_BUILD`,
`PS2X_RUNNER_UNITY_BUILD_BATCH_SIZE`, `PS2X_ENABLE_RUNNER_PCH`, `PS2X_ENABLE_SCCACHE`,
`PS2X_ENABLE_RUNTIME_LOGS`, `PS2X_ENABLE_AGRESSIVE_LOGS` (sic), `PS2X_ENABLE_IOP_RPC_TRACE`,
`PS2X_ENABLE_DEBUG_UI`, `PS2X_ENABLE_FFMPEG`, `PS2X_DEFAULT_BOOT_ELF`.

Exit: the three programs exist, `state.py detect` shows the build dir, generator, compiler and build
type, and `state.py init` has recorded them.

## Phase 1: the disc and the boot ELF

```bash
python3 "${CLAUDE_SKILL_DIR}/iso.py" info game.iso                         # is the dump complete?
python3 "${CLAUDE_SKILL_DIR}/iso.py" boot game.iso                         # BOOT2, serial, region
python3 "${CLAUDE_SKILL_DIR}/iso.py" extract game.iso --out console/disc   # the whole disc, tree kept
python3 "${CLAUDE_SKILL_DIR}/elf.py" info console/disc/SLUS_203.12         # segments, CRCs, IRX modules
```

The runtime takes the guest ELF path as `argv[1]` and sets its I/O roots from it: `cdrom0:` and
`host:` read the ELF's own folder, and the memory card is the folder `mc0/` beside it. So run the
ELF that sits inside the extracted disc folder. Paths are used as written: on Linux the case must
match the disc (keep the extracted names as they are). `;1` version suffixes are stripped by the
runtime.

**More than one program.** Some games load a second executable or overlay later (look for other
`.ELF`/`.BIN` names in `elf.py info`'s disc paths, or a runtime `Error: No exact recompiled function
... codeRegion=no`). `ps2_recomp` handles one ELF per run and every run writes its own
`register_functions.cpp`, so combining two programs in one runner is not a documented workflow at this
commit. Check the upstream wiki and issues first and record it in STATE.md as an open question.

## Phase 2: the function map

**Preferred (retail, stripped):** Ghidra with the `ghidra-emotionengine-reloaded` extension (R5900
language; plain MIPS decompiles PS2 code badly). Import the ELF, let analysis finish, then run
`ps2xRecomp/tools/ghidra/ExportPS2Functions.java` from the Script Manager (category PS2Recomp). It
asks for a TOML path, then a CSV path, and writes:

- the CSV map, `Name,Start,End,Size`, one row per function and executable label, e.g.
  `sub_00100230,0x00100230,0x00100290,96`;
- a TOML with `[general]` `input`, `output` (`<folder of the TOML>/output`), `ghidra_output`,
  `single_file_output = false`, `patch_syscalls = false`, `patch_cop0 = true`, `patch_cache = true`,
  `stubs` (names it matched against its copy of the runtime's handler list), `untracked_stubs`
  (library-like names with no handler) and `skip = []`, plus a `[ghidra_export]` stats table.

Save both into `console/`. Details and the MCP route: `ghidra-and-pcsx2.md`.

**Fallback:** `ps2_analyzer <input_elf> <output_toml> [sce_symbol_db_dir]`. It uses DWARF debug
info when the ELF has it, otherwise a JAL scanner (quick bring-up only), plus an embedded SCE SDK
symbol database (override with the optional folder or `PS2RECOMP_SCE_SYMBOL_DB`).

**Starter by hand:** `elf.py toml <elf> --out console/<serial>.toml [--ghidra-csv map.csv]`.

## The TOML (what `ps2xRecomp/src/lib/config_manager.cpp` reads)

| Key | Type | Meaning |
| --- | --- | --- |
| `[general] input` | path | the ELF (required) |
| `output` | path | folder for generated C++ (required) |
| `ghidra_output` | path | Ghidra CSV map; its bounds win over heuristics |
| `single_file_output` | bool | one `ps2_recompiled_functions.cpp` instead of one file per function |
| `low_memory_mode` | bool | lower peak memory, serial output |
| `output_worker_threads` | int | 0 = CPU threads minus one; 1 = serial |
| `patch_syscalls` / `patch_cop0` / `patch_cache` | bool | defaults false / true / true; gate patches on those instruction kinds |
| `stubs` | strings | `"name"`, `"handler@0xADDR"`, or triage `"ret0@0x.."`, `"ret1@0x.."`, `"reta0@0x.."` |
| `entry_points` | strings | extra function starts, `"0xADDR"` or `"name@0xADDR"` (`untracked_stubs` is also read as hints) |
| `skip` | strings | legacy: the function is not translated and calling it THROWS (see below) |
| `[[patches.instructions]]` | `address`, `value` | raw replacement instruction word at an address, before translation |
| `[mmio]` | `"0xINST" = "0xMMIO"` | per-instruction MMIO address hints |
| `[[jump_tables.table]]` | `address`, `base_register`, `entries = [{index, target}]` | jump tables the analysis missed |

Corrections to the base skill: `code_base` and `runtime_header` are not read by anything. `skip` does
not step over a function: its wrapper calls `ps2_stubs::TODO_NAMED`, which logs `Warning:
Unimplemented PS2 stub called. name=...` and throws, ending the run. To make a function return without
running, bind it: `"ret0@0xADDR"` (or `ret1`, `reta0`). Handler names must exist in
`PS2_SYSCALL_LIST` or `PS2_STUB_LIST` in `ps2xRuntime/include/ps2_call_list.h` (a leading underscore
is tolerated either way). Addresses are function starts in THIS build of the ELF.

## Phase 3: recompile

```bash
<build>/ps2xRecomp/ps2_recomp console/SLUS-20312.toml
```

It ends with a block headed `========== PS2Recomp report ==========` (functions discovered,
recompiled, stubs, skipped, decode failures, `Unhandled instructions: N`, `Warnings: N, errors: N`)
and `Recompilation completed successfully` (exit 0) or `Recompilation failed`. Read the block, not
the whole stream. An instruction it cannot translate becomes
`throw std::runtime_error("Unhandled ... at 0x.. raw=0x..")` in the generated code: harmless until
executed. Log each one under "Known upstream issues".

Output in `output`: one `.cpp` per function (name plus `_0x<addr>`), `register_functions.cpp`,
`ps2_recompiled_functions.h` and `ps2_recompiled_stubs.h`. Copy the `.cpp` files into
`ps2xRuntime/src/runner/` (this replaces the placeholder `register_functions.cpp`) and the two
headers into `ps2xRuntime/include/`, without printing file names:

```bash
R=/path/to/PS2Recomp
rsync -rc --include='*.cpp' --exclude='*' console/output/ "$R/ps2xRuntime/src/runner/"   # unchanged files keep their timestamps
cp console/output/ps2_recompiled_functions.h console/output/ps2_recompiled_stubs.h "$R/ps2xRuntime/include/"
```

```powershell
Copy-Item console\output\*.cpp "$R\ps2xRuntime\src\runner\"
Copy-Item console\output\ps2_recompiled_*.h "$R\ps2xRuntime\include\"
```

A re-run that changes the function list leaves old generated files behind; removing them is the
user's call (ask). New generated headers rebuild every runner file, so batch TOML changes and
re-run the recompiler once per batch, never "to see if it helps".

## Phase 4: build and run

Build gate first (SKILL.md), then `cmake --build <build_dir> --target ps2EntryRunner` (add
`--config <type>` only for Visual Studio). The runner sources are globbed with `CONFIGURE_DEPENDS`, so
freshly copied generated files are picked up by the next build without a manual reconfigure.

Run: `<build>/ps2xRuntime/ps2EntryRunner console/disc/SLUS_203.12`. Good signs on stdout:
`Using argv boot path`, `ELF file loaded successfully. Entry point: 0x...` and
`[game_overrides] applying '<name>'` (these need `PS2X_ENABLE_RUNTIME_LOGS`, on by default). The
window is raylib; F1 toggles the debug panel (`PS2X_ENABLE_DEBUG_UI`): threads, IOP/SIF modules, the
RPC tracer, pad bytes, GS state, I/O paths, CDVD and memory card state.

Standard test protocol: run with a short timeout (boot 10 to 15 s, menus 30 s), read only the last
5,000 characters, kill the process, diagnose, write one line in STATE.md. `/console-tester` (`run.py
boot`) does exactly this and keeps the log in `console/runs/`. `/console-backend` (`stubs.py scan`)
turns the log into a ranked fix list.

## Phase 5: I/O and menus

The game reads `cdrom0:` from the extracted disc folder, its IOP modules run in ps2xIOP, the memory
card is `mc0/`. Replace every temporary `ret0`/`ret1` with real code, then hand the systems to their
skills (graphics, sound, controller, saves). The exit test is the user playing from boot to the first
real gameplay with no crash in a 30 s menu run.

## Where a game override file goes (checked in `ps2xRuntime/CMakeLists.txt`)

- `src/lib/Kernel/**/*.cpp` is globbed into the static library `ps2_runtime`. A linker only pulls an
  object out of a static library when something references it, and nothing references a
  self-registering `PS2_REGISTER_GAME_OVERRIDE` file, so one placed there compiles and then silently
  never runs.
- `src/runner/*.cpp` is globbed into the `ps2EntryRunner` executable, but it is `ps2_recomp`'s
  output (derived from the game) and part of the unity batches: keep hand-written files out of it.
- So the source stays in `console/patches/`, and /console-mod compiles it into `ps2EntryRunner`
  through one CMake include line added at the end of `ps2xRuntime/CMakeLists.txt` (with the user's
  OK, once per checkout). Its sources are excluded from the unity build, so adding a mod does not
  rebuild the generated code. Give every helper in an override file a game-specific name.
