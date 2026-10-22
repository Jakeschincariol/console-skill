# Targets: what each build needs, and where the facts come from

Checked against the PS2Recomp checkout at commit eb8688b (October 2026). File names are relative to
that checkout. When the checkout you build from is newer, re-read the files named here.

## Shared facts

- One CMake project. Root `CMakeLists.txt` needs CMake 3.21 (the README says 3.20+; the root file is
  the stricter one). Options: `PS2X_BUILD_RECOMP`, `PS2X_BUILD_RUNTIME`, `PS2X_BUILD_ANALYZER`,
  `PS2X_BUILD_TEST`, `PS2X_BUILD_STUDIO`. The runner target is `ps2EntryRunner` (an executable; a
  shared library on Android).
- The runner is built from `ps2xRuntime/src/runner/*.cpp` (the recompiled game) plus
  `ps2xRuntime/src/main.cpp`. Build where the recompiled code is.
- `ps2EntryRunner <path to ELF>` boots the game; with no argument it uses the compile-time
  `PS2X_DEFAULT_BOOT_ELF` (how Android and the Vita work). The disc's other files are read from the
  ELF's folder (`cdRoot`) and saves go to `mc0/` beside it.
- raylib 5.5 is fetched by CMake (desktop and Android). FFmpeg (`PS2X_ENABLE_FFMPEG`, ON by default
  except Android) decodes MPEG cutscenes: prebuilt DLLs downloaded on Windows, `pkg-config` libraries
  on macOS and Linux. Off means cutscenes show stub frames.
- ARM hosts (Apple silicon, ARM Linux, Android arm64, the Vita) fetch `sse2neon` v1.9.1 and define
  `USE_SSE2NEON` automatically.
- Useful runtime options (`ps2xRuntime/CMakeLists.txt`): `PS2X_ENABLE_DEBUG_UI` (F1 panel, desktop),
  `PS2X_SHOW_WINDOWS_CONSOLE` (Windows Release console window), `PS2X_ENABLE_RUNTIME_LOGS`,
  `PS2X_ENABLE_IOP_RPC_TRACE` (keep both on while testing).

## Windows

From `README.md`: tested mainly with MSVC. Visual Studio 2022 with the C++ workload, CMake, Git.
`cmake -S . -B out/build` then `cmake --build out/build --config Release --target ps2EntryRunner`.
The runner lands in `out/build/ps2xRuntime/Release/` with the FFmpeg DLLs copied beside it
(`cmake/CopyFfmpegDlls.cmake`). `package` copies every `.dll` next to the runner.

## macOS

Xcode Command Line Tools, `brew install cmake ninja pkg-config ffmpeg`. Single-config generators put
the runner in `<build>/ps2xRuntime/ps2EntryRunner`. It links Homebrew's FFmpeg dynamically, so the
Mac that plays it needs `brew install ffmpeg` too (or build with `-DPS2X_ENABLE_FFMPEG=OFF`).
`package` notices the FFmpeg link and says so in `PLAYERS.md`. An unsigned runner copied between
Macs may be quarantined: `xattr -dr com.apple.quarantine <folder>` on your own machine.

## Linux

Compiler with C++20, CMake, Ninja, Git, `pkg-config`, FFmpeg dev packages, and raylib's X11/GL
headers (`libx11-dev libxrandr-dev libxinerama-dev libxcursor-dev libxi-dev libgl1-mesa-dev` on
Debian and Ubuntu). The runner links the system FFmpeg; `package` copies any `.so` next to it.

## Steam Deck

The Deck runs SteamOS (Arch based, x86_64, read-only system folders). Two routes:

1. Native: the Linux build. Build on the Deck in Desktop Mode inside `distrobox`, or on an x86_64
   Linux machine whose libraries are not newer than SteamOS's. `-DPS2X_ENABLE_FFMPEG=OFF` removes the
   FFmpeg dependency if the libraries do not match. Add `play.sh` to Steam as a non-Steam game.
2. Proton: the Windows build added as a non-Steam game with `game\<ELF>` in its launch options and a
   Proton version forced. Not tested by this skill.

Steam Input turns the Deck's controls into an Xbox-style pad, so `/console-controller`'s `xbox`
layout fits.

## Android

From `android/README.md`, `android/app/build.gradle` and `android/gradle.properties`:

- Android Studio, or Gradle 8.7+ with JDK 17. SDK 34, NDK `28.2.13676358` (pinned), CMake 3.22.1
  from the SDK. ABIs `arm64-v8a` and `x86_64`, `minSdk 28`, application id `com.ps2x.runner`.
- No argv: the ELF path comes from the `ps2xBootElf` Gradle property, default
  `/storage/emulated/0/Android/data/com.ps2x.runner/files/game.elf`. Pass the real ELF name:
  `./gradlew assembleRelease -Pps2xBootElf=/storage/emulated/0/Android/data/com.ps2x.runner/files/SLUS_203.12`
  (after `gradle wrapper --gradle-version 8.9` once; no wrapper is committed).
- APK: `android/app/build/outputs/apk/release/app-release.apk`, signed with the debug key.
- `adb install`, `adb shell mkdir -p .../files`, `adb push "path/to/game/." .../files/`.
- Logs: `adb logcat -s raylib`. This checkout's `src/main.cpp` also forwards stdout and stderr to
  logcat under the tag `ps2x` (the Android README predates that).
- `ps2_android_runtime.cpp` is a one-line TODO for an on-screen touch overlay: use a controller.
- TV boxes: the same APK; `adb connect <ip>` for wireless install. The manifest
  (`android/app/src/main/AndroidManifest.xml`, app name "PS2 Recomp", landscape) has only the
  normal `LAUNCHER` entry, no `LEANBACK_LAUNCHER`, so an Android TV home screen will not list it:
  open it from Settings, Apps, or a sideload launcher.

## PS Vita

From `vita/setup.sh`, `vita/build.sh`, `ps2xRuntime/Readme.md` and `ps2xRuntime/CMakeLists.txt`:

- VitaSDK with `$VITASDK` exported and `$VITASDK/bin` on `PATH`.
- `./vita/setup.sh` once: PVR_PSP2 v3.9, gl4es4vita 1.1.4, SDL2 2.32.2 with `-DVIDEO_VITA_PVR=ON`,
  Quenom's raylib 5.5 Vita port; stages `.suprx` modules into `ps2xRuntime/vita/module/`. It uses
  `curl`, `unzip`, `cmake`, GNU `install -D -t` and `nproc`: run it on Linux or WSL.
- `./vita/build.sh [boot ELF path on the Vita]` (default `ux0:data/ps2x/game.elf`; also env
  `PS2X_BOOT_ELF`, `BUILD_DIR` default `outvita`, `CLEAN=1` wipes the build folder). It configures
  with `-DPS2X_BUILD_RECOMP=OFF -DPS2X_BUILD_ANALYZER=OFF -DPS2X_BUILD_TEST=OFF
  -DPS2X_BUILD_STUDIO=OFF -DPS2X_ENABLE_SCCACHE=OFF` and prints the vpk path when done.
- vpk: title id `RANJ00001`, name "PS2 Retro X" (`PS2X_VITA_TITLEID`, `PS2X_VITA_APP_NAME`).
  The runtime reserves a 96 MB heap (`ps2_vita_runtime.cpp`); logs go to the Vita's TTY.
- Install with VitaShell; copy the extracted disc to the folder the boot path names, keeping the
  ELF's real name.

## Web (not a PS2Recomp target)

`snuri00/ps2-web-recomp` (MIT) is a separate PS2 static recompiler and runtime (`eerecomp`) that
compiles to WebAssembly and draws with WebGPU; it also builds a native runner
(`build/native-<game>/ps2run`, SDL2 + wgpu-native). One script ports a disc image:
`scripts/port.sh mygame "My Game.iso"`, then `python3 scripts/serve.py build/web-mygame
games/mygame/disc.iso` serves `http://localhost:8620/` (`--native-only` skips the browser build).
Needs git, CMake, Ninja, a C++20 compiler (clang preferred), Python 3 and Emscripten (installed on
first run into `tools/emsdk`); Linux x86-64 is tested, Apple silicon builds with a newer bash
(`brew install bash cmake ninja sevenzip`); about 300 MB per game. It applies PCSX2 pnach files
(`--pnach FILE --pnach-section "Widescreen 16:9"`). WebGPU: Chrome or Edge on Windows and macOS; on
Linux enable `chrome://flags` `enable-unsafe-webgpu` and `enable-vulkan`. Two games are up so far
(Gran Turismo 4, God of War II); others will likely stop at an unhandled syscall or IOP function.
It is a second port of the same disc, not a build of your PS2Recomp port, and nothing in this pack
has been tested against it. Keep it on localhost: serving it to others hands them the game.
