#!/usr/bin/env python3
"""targets.py: build and package a PS2Recomp port for other devices (console-anywhere).

doctor       checks this machine's toolchain for a target and prints the install command for
             anything missing (Windows, macOS, Linux, Steam Deck, Android, PS Vita, web)
plan         prints the build steps for a target, taken from PS2Recomp's README.md,
             android/README.md, android/app/build.gradle, vita/setup.sh and vita/build.sh
package      builds a player folder (runner + libraries next to it, an EMPTY game/ folder,
             PLAYERS.md, a launch script, .recomp.json) and REFUSES if anything that looks
             like game data is inside
guard        runs the same game-data check on any folder (before you zip or push it)
recomp-json  writes the optional .recomp.json that describes a recomp project

Python 3.8+ standard library only.
"""

import argparse
import datetime
import fnmatch
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys

TARGETS = ["windows", "macos", "linux", "steamdeck", "android", "vita", "web"]
PACKAGE_TARGETS = ["windows", "macos", "linux", "steamdeck", "android", "vita"]
TARGET_TITLES = {
    "windows": "Windows (x64)", "macos": "macOS (Apple silicon or Intel)", "linux": "Linux (x86_64)",
    "steamdeck": "Steam Deck (SteamOS, Linux x86_64)", "android": "Android phone, tablet or TV box",
    "vita": "PS Vita (homebrew)", "web": "web browser (snuri00/ps2-web-recomp, a separate toolchain)",
}
STATUSES = ["exploring", "in-progress", "playable", "released", "complete", "paused"]
REGIONS = {"SLUS": "USA", "SCUS": "USA", "SLES": "Europe", "SCES": "Europe", "SLPS": "Japan",
           "SLPM": "Japan", "SCPS": "Japan", "SLKA": "Korea", "SCKA": "Korea", "SCAJ": "Asia"}
ANDROID_FILES = "/storage/emulated/0/Android/data/com.ps2x.runner/files"
ANDROID_NDK = "28.2.13676358"
VITA_DEFAULT_DIR = "ux0:data/ps2x"
MAX_FILE = 512 * 1024 * 1024
DIST_MARKER = ".console-dist.json"
PLACEHOLDER = "PUT-YOUR-GAME-HERE.txt"
HERE = os.path.dirname(os.path.abspath(__file__))


class UsageError(Exception):
    pass


# ======================================================================= doctor

class Probe:
    """What doctor may look at on this machine. Tests pass a fake one."""

    def __init__(self):
        self.system = platform.system()
        self.machine = platform.machine().lower()
        self.env = dict(os.environ)

    def which(self, name):
        return shutil.which(name)

    def run(self, cmd, timeout=15):
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
            return proc.returncode, proc.stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            return None, ""

    def exists(self, path):
        return os.path.exists(path)

    def read(self, path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""


def _version(text, pattern):
    m = re.search(pattern, text or "")
    if not m:
        return None
    return tuple(int(x) for x in re.findall(r"\d+", m.group(1))[:3])


def _check(name, ok, required=True, found="", fix=""):
    return {"name": name, "ok": bool(ok), "required": required, "found": found, "fix": fix}


def _host_target(probe):
    return {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(probe.system, "linux")


def _pkg_manager_hint(probe, apt, brew, dnf=None):
    if probe.system == "Darwin":
        return brew
    if probe.system == "Linux":
        return apt + ("  (Fedora: %s)" % dnf if dnf else "")
    return apt


def check_cmake(probe, minimum=(3, 21)):
    path = probe.which("cmake")
    if not path:
        return _check("cmake %d.%d+" % minimum, False, fix=_pkg_manager_hint(
            probe, "sudo apt install cmake", "brew install cmake", "sudo dnf install cmake")
            if probe.system != "Windows" else "winget install Kitware.CMake")
    _, out = probe.run([path, "--version"])
    ver = _version(out, r"cmake version (\d+\.\d+(?:\.\d+)?)")
    ok = ver is not None and ver[:2] >= minimum
    return _check("cmake %d.%d+" % minimum, ok, found=".".join(map(str, ver)) if ver else "unknown version",
                  fix="" if ok else "install a newer CMake (https://cmake.org/download/)")


def check_tool(probe, name, label=None, required=True, fix="", alternatives=()):
    for candidate in (name,) + tuple(alternatives):
        path = probe.which(candidate)
        if path:
            return _check(label or name, True, required, found=path)
    return _check(label or name, False, required, fix=fix)


def check_ffmpeg(probe):
    if not probe.which("pkg-config"):
        return _check("pkg-config + FFmpeg libraries", False, fix=_pkg_manager_hint(
            probe, "sudo apt install pkg-config libavcodec-dev libavformat-dev libavutil-dev "
                   "libswresample-dev libswscale-dev",
            "brew install pkg-config ffmpeg") + "   (or configure with -DPS2X_ENABLE_FFMPEG=OFF: "
                                               "cutscenes then show stub frames)")
    rc, _ = probe.run(["pkg-config", "--exists", "libavcodec", "libavformat", "libavutil",
                       "libswresample", "libswscale"])
    return _check("FFmpeg libraries (pkg-config)", rc == 0, found="found" if rc == 0 else "",
                  fix="" if rc == 0 else _pkg_manager_hint(
                      probe, "sudo apt install libavcodec-dev libavformat-dev libavutil-dev "
                             "libswresample-dev libswscale-dev", "brew install ffmpeg",
                      "sudo dnf install ffmpeg-free-devel")
                  + "   (or -DPS2X_ENABLE_FFMPEG=OFF)")


def doctor_desktop(probe, target):
    checks = []
    if target == "windows":
        if probe.system != "Windows":
            return [_check("a Windows PC", False, fix="build the Windows runner on Windows (cross-"
                           "compiling is not set up here), or use the steamdeck target / Proton")]
        checks.append(check_cmake(probe))
        checks.append(check_tool(probe, "git", fix="winget install Git.Git"))
        cl = probe.which("cl")
        vswhere = os.path.join(probe.env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                               "Microsoft Visual Studio", "Installer", "vswhere.exe")
        vs_path = ""
        if not cl and probe.exists(vswhere):
            rc, out = probe.run([vswhere, "-latest", "-products", "*", "-requires",
                                 "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property",
                                 "installationPath"])
            vs_path = out.strip() if rc == 0 else ""
        checks.append(_check("MSVC (Visual Studio 2022, Desktop development with C++)", bool(cl or vs_path),
                             found=cl or vs_path,
                             fix="install Visual Studio 2022 or its Build Tools with the C++ workload, then "
                                 "use the 'x64 Native Tools Command Prompt'"))
        checks.append(check_tool(probe, "clang-cl", required=False,
                                 fix="optional: the clang tools from the Visual Studio installer"))
        checks.append(check_tool(probe, "ninja", required=False, fix="optional: winget install Ninja-build.Ninja"))
        return checks
    if target == "macos" and probe.system != "Darwin":
        return [_check("a Mac", False, fix="build the macOS runner on a Mac (Apple silicon or Intel)")]
    if target in ("linux", "steamdeck") and probe.system != "Linux":
        hint = ("build on Linux x86_64: a PC, a VM, WSL2 on Windows, or the Steam Deck itself in "
                "Desktop Mode inside distrobox" if target == "steamdeck" else
                "build on a Linux machine (or WSL2 on Windows)")
        if target == "steamdeck":
            hint += "; or run the Windows build under Proton"
        return [_check("a Linux machine", False, fix=hint)]
    if target == "steamdeck" and probe.machine not in ("x86_64", "amd64"):
        checks.append(_check("x86_64 CPU", False, found=probe.machine,
                             fix="the Deck is x86_64: build on an x86_64 Linux machine"))
    if probe.system == "Darwin":
        rc, out = probe.run(["xcode-select", "-p"])
        checks.append(_check("Xcode Command Line Tools", rc == 0, found=out.strip() if rc == 0 else "",
                             fix="xcode-select --install"))
        checks.append(check_tool(probe, "clang++", label="clang++ (C++20)", fix="xcode-select --install"))
        if probe.machine in ("arm64", "aarch64"):
            checks.append(_check("Apple silicon: sse2neon", True, required=False,
                                 found="CMake fetches sse2neon automatically on arm64"))
    else:
        checks.append(check_tool(probe, "c++", label="C++20 compiler (g++ 11+ or clang++ 14+)",
                                 alternatives=("g++", "clang++"),
                                 fix="sudo apt install build-essential   (Fedora: sudo dnf install gcc-c++)"))
    checks.append(check_cmake(probe))
    checks.append(check_tool(probe, "git", fix=_pkg_manager_hint(probe, "sudo apt install git",
                                                                 "brew install git", "sudo dnf install git")))
    checks.append(check_tool(probe, "ninja", required=False, fix=_pkg_manager_hint(
        probe, "sudo apt install ninja-build", "brew install ninja", "sudo dnf install ninja-build")))
    checks.append(check_ffmpeg(probe))
    if probe.system == "Linux":
        rc, _ = (probe.run(["pkg-config", "--exists", "x11", "xrandr", "xinerama", "xcursor", "xi", "gl"])
                 if probe.which("pkg-config") else (None, ""))
        checks.append(_check("raylib's X11 and OpenGL headers", rc == 0,
                             fix="sudo apt install libx11-dev libxrandr-dev libxinerama-dev libxcursor-dev "
                                 "libxi-dev libgl1-mesa-dev"))
        os_release = probe.read("/etc/os-release")
        if target == "steamdeck" and "ID=steamos" in os_release:
            checks.append(_check("SteamOS: writable build environment", bool(probe.which("distrobox")),
                                 required=False, found=probe.which("distrobox") or "",
                                 fix="SteamOS's system folders are read-only: build inside distrobox "
                                     "(distrobox create, then install the packages there)"))
    return checks


def _android_sdk(probe):
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if probe.env.get(key):
            return probe.env[key]
    home = probe.env.get("HOME") or probe.env.get("USERPROFILE") or ""
    for candidate in (os.path.join(home, "Library", "Android", "sdk"), os.path.join(home, "Android", "Sdk"),
                      os.path.join(probe.env.get("LOCALAPPDATA", ""), "Android", "Sdk")):
        if candidate and probe.exists(candidate):
            return candidate
    return None


def doctor_android(probe):
    checks = []
    java = probe.which("java")
    if java:
        _, out = probe.run([java, "-version"])
        ver = _version(out, r'version "(\d+(?:\.\d+)*)')
        major = (ver[1] if ver and ver[0] == 1 and len(ver) > 1 else ver[0]) if ver else None
        checks.append(_check("JDK 17", major == 17, found="Java %s" % major if major else "unknown",
                             fix="" if major == 17 else "install JDK 17 (Android Studio bundles one; "
                                                       "brew install openjdk@17 / sudo apt install openjdk-17-jdk)"))
    else:
        checks.append(_check("JDK 17", False, fix="install Android Studio (bundles a JDK) or JDK 17"))
    gradle = probe.which("gradle")
    studio = any(probe.exists(p) for p in ("/Applications/Android Studio.app", "/opt/android-studio",
                                           os.path.join(probe.env.get("ProgramFiles", r"C:\Program Files"),
                                                        "Android", "Android Studio")))
    if gradle:
        _, out = probe.run([gradle, "--version"])
        ver = _version(out, r"Gradle (\d+\.\d+(?:\.\d+)?)")
        ok = ver is not None and ver[:2] >= (8, 7)
        checks.append(_check("Gradle 8.7+ (or Android Studio)", ok or studio,
                             found=("Gradle " + ".".join(map(str, ver))) if ver else gradle,
                             fix="" if ok else "install Gradle 8.7+ or use Android Studio"))
    else:
        checks.append(_check("Gradle 8.7+ (or Android Studio)", studio, found="Android Studio" if studio else "",
                             fix="install Android Studio (recommended) or Gradle 8.7+"))
    sdk = _android_sdk(probe)
    checks.append(_check("Android SDK", bool(sdk), found=sdk or "",
                         fix="install it with Android Studio, then export ANDROID_HOME=<sdk folder>"))
    if sdk:
        for label, rel, fix in (
                ("SDK platform 34", os.path.join("platforms", "android-34"), "sdkmanager \"platforms;android-34\""),
                ("NDK %s (pinned in android/app/build.gradle)" % ANDROID_NDK, os.path.join("ndk", ANDROID_NDK),
                 "sdkmanager \"ndk;%s\"   (Android Studio installs it on first sync)" % ANDROID_NDK),
                ("CMake 3.22.1 from the SDK", os.path.join("cmake", "3.22.1"),
                 "sdkmanager \"cmake;3.22.1\"   (Android Studio installs it on demand)")):
            checks.append(_check(label, probe.exists(os.path.join(sdk, rel)), fix=fix))
    adb = probe.which("adb") or (sdk and probe.exists(os.path.join(sdk, "platform-tools", "adb")) and
                                 os.path.join(sdk, "platform-tools", "adb"))
    checks.append(_check("adb (to install and push files)", bool(adb), found=adb or "",
                         fix="sdkmanager platform-tools, then add <sdk>/platform-tools to PATH"))
    return checks


def doctor_vita(probe):
    checks = []
    if probe.system == "Windows":
        checks.append(_check("Linux or WSL", False, fix="vita/setup.sh and vita/build.sh are bash scripts: "
                                                        "run them in WSL2 (Ubuntu) or on Linux"))
    vitasdk = probe.env.get("VITASDK")
    checks.append(_check("VITASDK set", bool(vitasdk), found=vitasdk or "",
                         fix="install VitaSDK (https://vitasdk.org) and export VITASDK=/usr/local/vitasdk"))
    if vitasdk:
        checks.append(_check("VitaSDK toolchain file", probe.exists(os.path.join(vitasdk, "share",
                                                                                  "vita.toolchain.cmake")),
                             fix="reinstall VitaSDK: $VITASDK/share/vita.toolchain.cmake is missing"))
        gcc = probe.which("arm-vita-eabi-gcc")
        checks.append(_check("arm-vita-eabi-gcc in PATH", bool(gcc), found=gcc or "",
                             fix="export PATH=$VITASDK/bin:$PATH"))
        prefix = os.path.join(vitasdk, "arm-vita-eabi", "include")
        ready = probe.exists(os.path.join(prefix, "SDL2", "SDL.h")) and probe.exists(
            os.path.join(prefix, "gpu_es4", "psp2_pvr_hint.h"))
        checks.append(_check("GL stack from vita/setup.sh (SDL2 + PVR headers)", ready,
                             fix="run ./vita/setup.sh once in the PS2Recomp checkout"))
    for tool in ("cmake", "curl", "unzip", "bash"):
        checks.append(check_tool(probe, tool, fix=_pkg_manager_hint(probe, "sudo apt install " + tool,
                                                                    "brew install " + tool)))
    checks.append(check_tool(probe, "nproc", label="nproc (used by setup.sh and build.sh)",
                             fix="brew install coreutils and put its gnubin folder first in PATH"
                             if probe.system == "Darwin" else "sudo apt install coreutils"))
    if probe.system == "Darwin":
        checks.append(_check("GNU install (setup.sh uses install -D -t)", False, required=False,
                             fix="macOS's install lacks -D/-t: run setup.sh in Linux or WSL, or put GNU "
                                 "coreutils' gnubin first in PATH"))
    checks.append(check_tool(probe, "ninja", required=False, fix="optional: build.sh uses Ninja when present"))
    return checks


def doctor_web(probe):
    checks = [check_tool(probe, "git", fix=_pkg_manager_hint(probe, "sudo apt install git", "brew install git")),
              check_cmake(probe, (3, 20)),
              check_tool(probe, "ninja", fix=_pkg_manager_hint(probe, "sudo apt install ninja-build",
                                                               "brew install ninja")),
              check_tool(probe, "clang++", label="C++20 compiler (clang preferred)", alternatives=("c++", "g++"),
                         fix=_pkg_manager_hint(probe, "sudo apt install clang", "xcode-select --install")),
              check_tool(probe, "python3", fix="install Python 3")]
    if probe.system == "Darwin":
        bash = probe.which("bash")
        _, out = probe.run([bash, "--version"]) if bash else (None, "")
        ver = _version(out, r"version (\d+\.\d+)")
        checks.append(_check("bash 4+ (macOS ships 3.2)", ver is not None and ver[0] >= 4,
                             found=".".join(map(str, ver)) if ver else "", fix="brew install bash"))
        checks.append(check_tool(probe, "7zz", label="7-Zip (7zz)", alternatives=("7z",),
                                 fix="brew install sevenzip"))
    checks.append(_check("Emscripten", True, required=False,
                         found="installed by scripts/port.sh on first run into tools/emsdk"))
    return checks


def doctor(probe, target):
    if target == "android":
        return doctor_android(probe)
    if target == "vita":
        return doctor_vita(probe)
    if target == "web":
        return doctor_web(probe)
    return doctor_desktop(probe, target)


def cmd_doctor(args, probe=None):
    probe = probe or Probe()
    targets = args.target or [_host_target(probe)]
    if "all" in targets:
        targets = list(TARGETS)
    report = {}
    missing = 0
    for target in targets:
        checks = doctor(probe, target)
        report[target] = checks
        missing += sum(1 for c in checks if c["required"] and not c["ok"])
    if args.json:
        print(json.dumps({"host": {"system": probe.system, "machine": probe.machine}, "targets": report,
                          "missing": missing}, indent=2))
    else:
        print("doctor: this machine is %s %s" % (probe.system, probe.machine))
        for target, checks in report.items():
            print("")
            print("%s: %s" % (target, TARGET_TITLES[target]))
            for c in checks:
                if c["ok"]:
                    mark = "ok     "
                elif c["required"]:
                    mark = "MISSING"
                else:
                    mark = "note   "
                line = "  %s %s" % (mark, c["name"])
                if c["found"]:
                    line += "  (%s)" % c["found"]
                print(line)
                if not c["ok"] and c["fix"]:
                    print("          -> %s" % c["fix"])
        print("")
        print("all required tools found" if missing == 0 else "%d required item(s) missing" % missing)
    return 0 if missing == 0 else 1


# ======================================================================= plan

def _plan(target, repo, build, elf):
    clone = "git clone --recurse-submodules https://github.com/ran-j/PS2Recomp.git"
    runner_ready = ("Your recompiled game code is already in %s/ps2xRuntime/src/runner/ (from "
                    "/console-reverse). The runner is built from it, so build in the checkout you "
                    "recompiled into, not a fresh clone." % repo)
    if target == "windows":
        return [
            ("Install the tools", ["winget install Kitware.CMake Git.Git"],
             ["Visual Studio 2022 (or its Build Tools) with 'Desktop development with C++'. The "
              "PS2Recomp README says it is tested mainly with MSVC.",
              "FFmpeg needs nothing: CMake downloads prebuilt LGPL DLLs and copies them next to the runner."]),
            ("Get PS2Recomp (skip if you already have it)", [clone], [runner_ready]),
            ("Build the runner, Release (x64 Native Tools Command Prompt)",
             ["cd %s" % repo, "cmake -S . -B %s" % build,
              "cmake --build %s --config Release --target ps2EntryRunner" % build],
             ["Optional at configure time: -DPS2X_SHOW_WINDOWS_CONSOLE=OFF hides the console window in "
              "Release builds; -DPS2X_ENABLE_DEBUG_UI=OFF drops the F1 debug panel.",
              "Never clean the build folder: an incremental build is minutes, a full one is an hour."]),
            ("Try it", ["%s\\ps2xRuntime\\Release\\ps2EntryRunner.exe C:\\games\\mygame\\%s"
                        % (build.replace("/", "\\"), elf)],
             ["The runner takes the ELF path as its first argument. The disc's files sit next to the ELF "
              "(cdRoot is the ELF's folder) and saves go to mc0\\ beside it."]),
            ("Package it", ["python3 targets.py package --runner %s/ps2xRuntime/Release/ps2EntryRunner.exe "
                            "--target windows --elf-name %s --out console/dist/mygame-windows" % (build, elf)],
             ["The FFmpeg DLLs next to the runner are copied too."]),
        ]
    if target in ("macos", "linux"):
        if target == "macos":
            tools = (["xcode-select --install", "brew install cmake ninja pkg-config ffmpeg"],
                     ["Apple silicon works: CMake sees arm64 and fetches sse2neon itself (USE_SSE2NEON).",
                      "No FFmpeg wanted? Configure with -DPS2X_ENABLE_FFMPEG=OFF: cutscenes show stub frames."])
        else:
            tools = (["sudo apt install build-essential cmake ninja-build git pkg-config libavcodec-dev "
                      "libavformat-dev libavutil-dev libswresample-dev libswscale-dev libx11-dev "
                      "libxrandr-dev libxinerama-dev libxcursor-dev libxi-dev libgl1-mesa-dev"],
                     ["Debian and Ubuntu names; Fedora has the same packages under dnf names.",
                      "The X11 and GL headers are for raylib, which CMake fetches (raylib 5.5).",
                      "No FFmpeg wanted? Configure with -DPS2X_ENABLE_FFMPEG=OFF."])
        release = "out/release"
        steps = [
            ("Install the tools", tools[0], tools[1]),
            ("Get PS2Recomp (skip if you already have it)", [clone], [runner_ready]),
            ("Build the runner, Release, in its own folder",
             ["cd %s" % repo, "cmake -S . -B %s -G Ninja -DCMAKE_BUILD_TYPE=Release" % release,
              "cmake --build %s --target ps2EntryRunner" % release],
             ["A separate folder keeps your day-to-day build (%s, the one in console/STATE.md) "
              "untouched. A folder never changes generator once configured." % build,
              "The README build (cmake -S . -B out/build; cmake --build out/build --config Debug) works "
              "too; the Release runner is faster to play.",
              "The README says PS2Recomp is tested mainly with MSVC: Mac and Linux builds work through "
              "the same CMake but get less testing."]),
            ("Try it", ["./%s/ps2xRuntime/ps2EntryRunner ~/games/mygame/%s" % (release, elf)],
             ["Or run it through /console-tester: run.py boot --runner ... --elf ..."]),
            ("Package it", ["python3 targets.py package --runner %s/ps2xRuntime/ps2EntryRunner --target %s "
                            "--elf-name %s --out console/dist/mygame-%s" % (release, target, elf, target)],
             ["The runner links FFmpeg dynamically: the other machine needs FFmpeg installed too "
              "(PLAYERS.md says so), unless you built with -DPS2X_ENABLE_FFMPEG=OFF."]),
        ]
        if target == "macos":
            steps[3][2].append("A runner copied from another Mac may be quarantined by Gatekeeper: on "
                               "your own Mac, xattr -dr com.apple.quarantine <the folder>.")
        return steps
    if target == "steamdeck":
        return [
            ("Pick a route", [],
             ["A: a native Linux build (best). B: the Windows build under Proton."]),
            ("A1. Build for Linux x86_64",
             ["python3 targets.py plan --target linux"],
             ["On a Linux PC or VM, or on the Deck itself in Desktop Mode inside distrobox (SteamOS's "
              "system folders are read-only, distrobox gives you a normal distro).",
              "Build on a distro no newer than SteamOS, or inside distrobox on the Deck, so the C++ and "
              "FFmpeg libraries match. -DPS2X_ENABLE_FFMPEG=OFF removes the FFmpeg dependency entirely."]),
            ("A2. Package and copy it over",
             ["python3 targets.py package --runner out/release/ps2xRuntime/ps2EntryRunner --target steamdeck "
              "--elf-name %s --out console/dist/mygame-deck" % elf],
             ["Copy the folder to the Deck (USB stick, scp, or Warpinator), e.g. to ~/Games/mygame. Put "
              "your extracted disc in game/."]),
            ("A3. Add it to Steam", [],
             ["Desktop Mode, Steam, Add a Non-Steam Game, browse to play.sh (show all file types).",
              "Steam Input presents the Deck's controls as an Xbox-style pad: use the xbox layout in "
              "/console-controller."]),
            ("B. Proton instead",
             ["python3 targets.py plan --target windows"],
             ["Add ps2EntryRunner.exe as a non-Steam game, put game\\%s in its Launch Options, and "
              "force a Proton version under Compatibility. Not tested by this skill." % elf]),
        ]
    if target == "android":
        device_elf = "%s/%s" % (ANDROID_FILES, elf)
        return [
            ("Install the tools", [],
             ["From android/README.md: Android Studio (recommended) or Gradle 8.7+ and JDK 17; Android "
              "SDK 34 and the NDK (android/app/build.gradle pins ndkVersion %s); CMake 3.22.1 from the "
              "SDK. Android Studio installs the NDK and CMake on first sync." % ANDROID_NDK]),
            ("Decide where the ELF will live on the device", [],
             ["Android has no command line, so the ELF path is baked in at build time through the "
              "ps2xBootElf Gradle property (it becomes PS2X_DEFAULT_BOOT_ELF).",
              "Use %s (keep the disc's real ELF name: the game database and game overrides key on it)."
              % device_elf]),
            ("Build the APK",
             ["cd %s/android" % repo, "gradle wrapper --gradle-version 8.9",
              "./gradlew assembleRelease -Pps2xBootElf=%s" % device_elf],
             ["Or in Android Studio: set ps2xBootElf=%s in android/gradle.properties and run 'app'."
              % device_elf,
              "Output: android/app/build/outputs/apk/release/app-release.apk (arm64-v8a and x86_64, "
              "minSdk 28). build.gradle signs release with the debug key: fine for your own devices."]),
            ("Install and copy your game",
             ["adb install app/build/outputs/apk/release/app-release.apk",
              "adb shell mkdir -p %s" % ANDROID_FILES,
              "adb push \"path/to/your/extracted/disc/.\" %s/" % ANDROID_FILES],
             ["An Android TV box works the same way (adb connect <its IP>, then the same commands) with "
              "a Bluetooth controller. The manifest has no Android TV (LEANBACK_LAUNCHER) entry, so open "
              "it from Settings, Apps, or a sideload launcher."]),
            ("Logs and controls",
             ["adb logcat -s raylib ps2x"],
             ["main.cpp in this checkout forwards the runtime's stdout and stderr to logcat under the "
              "tag ps2x (android/README.md predates that and says they are not redirected).",
              "No on-screen controls: ps2_android_runtime.cpp is only a TODO for a touch overlay. Pair a "
              "controller; /console-controller can add invisible touch zones."]),
            ("Package it",
             ["python3 targets.py package --runner %s/android/app/build/outputs/apk/release/app-release.apk "
              "--target android --elf-name %s --out console/dist/mygame-android" % (repo, elf)],
             ["The package adds install scripts that run the adb steps above."]),
        ]
    if target == "vita":
        vita_elf = "%s/%s" % (VITA_DEFAULT_DIR, elf)
        return [
            ("What you need", [],
             ["A PS Vita that already runs homebrew, with VitaShell.",
              "A Linux machine or WSL2: vita/setup.sh uses GNU install -D and nproc (on macOS put GNU "
              "coreutils first in PATH, or use a Linux VM)."]),
            ("Install VitaSDK", ["export VITASDK=/usr/local/vitasdk", "export PATH=$VITASDK/bin:$PATH"],
             ["https://vitasdk.org has the installer (vdpm)."]),
            ("Set up the GL stack, once", ["cd %s" % repo, "./vita/setup.sh"],
             ["Downloads and installs into $VITASDK: PVR_PSP2 3.9 (GPU driver modules), gl4es4vita 1.1.4, "
              "SDL2 2.32.2 rebuilt with PVR, and Quenom's raylib 5.5 Vita port. It stages the .suprx "
              "modules into ps2xRuntime/vita/module/ so the vpk bundles them. Safe to re-run."]),
            ("Build the vpk", ["./vita/build.sh %s" % vita_elf],
             ["The argument is the ELF's path ON THE VITA, baked in (default %s/game.elf; the "
              "PS2X_BOOT_ELF environment variable works too). Keep the disc's real ELF name." % VITA_DEFAULT_DIR,
              "Build folder: outvita (BUILD_DIR changes it). Do not set CLEAN=1: it wipes the folder "
              "and forces a full rebuild.",
              "build.sh prints the vpk's path when it is done (ps2EntryRunner.vpk, made in "
              "outvita/ps2xRuntime/): title id RANJ00001, bubble name 'PS2 Retro X' (the "
              "PS2X_VITA_TITLEID and PS2X_VITA_APP_NAME CMake cache variables)."]),
            ("Install and copy your game", [],
             ["Install the vpk with VitaShell, then copy your extracted disc (with the ELF) to %s/ "
              "over VitaShell's FTP or USB mode." % VITA_DEFAULT_DIR,
              "Expect it to be slow: the Vita is far weaker than a PC and the README already calls VU "
              "and GS performance very bad. Logs go to the Vita's TTY (ps2_vita_runtime.cpp)."]),
            ("Package it",
             ["python3 targets.py package --runner outvita/ps2xRuntime/ps2EntryRunner.vpk --target vita --elf-name %s "
              "--out console/dist/mygame-vita" % elf], []),
        ]
    if target == "web":
        return [
            ("What this is", [],
             ["The browser path is a different toolchain: snuri00/ps2-web-recomp (MIT) is its own PS2 "
              "static recompiler and runtime (eerecomp, not PS2Recomp) that compiles to WebAssembly and "
              "draws with WebGPU. So it is a second port of the same disc, not a build target of your "
              "PS2Recomp port: your overrides and TOML do not carry over.",
              "Two games have been brought up there so far (Gran Turismo 4, God of War II); others will "
              "likely stop at an unhandled syscall or IOP function. This skill has not been tested "
              "against it."]),
            ("Install the tools", ["brew install bash cmake ninja sevenzip   (macOS, Apple silicon)"],
             ["git, CMake, Ninja, a C++20 compiler (clang preferred) and Python 3. Linux x86-64 is the "
              "tested host. Emscripten installs itself into tools/emsdk on the first run. About 300 MB "
              "per game."]),
            ("Port your own disc image",
             ["git clone https://github.com/snuri00/ps2-web-recomp.git", "cd ps2-web-recomp",
              "scripts/port.sh mygame \"My Game.iso\""],
             ["--native-only skips the browser build and only makes the native runner "
              "(build/native-mygame/ps2run, SDL2 + wgpu-native; Steam Deck controls work).",
              "It applies PCSX2 pnach files itself: --pnach FILE --pnach-section \"Widescreen 16:9\" "
              "(patches from /console-mod or /console-graphics)."]),
            ("Play it in the browser",
             ["python3 scripts/serve.py build/web-mygame games/mygame/disc.iso"],
             ["Serves http://localhost:8620/ on your own machine. The browser needs WebGPU: Chrome or "
              "Edge on Windows and macOS; on Linux enable chrome://flags enable-unsafe-webgpu and "
              "enable-vulkan.",
              "Keep it local. Serving it to others would hand them the game."]),
        ]
    raise UsageError("unknown target %r" % target)


PLAN_SOURCES = {
    "windows": ["README.md (Build, Requirements)", "ps2xRuntime/CMakeLists.txt (runner target, FFmpeg DLLs)"],
    "macos": ["README.md", "CMakeLists.txt (sse2neon on ARM)", "ps2xRuntime/CMakeLists.txt (FFmpeg via pkg-config)"],
    "linux": ["README.md", "ps2xRuntime/CMakeLists.txt (FFmpeg via pkg-config, raylib 5.5 FetchContent)"],
    "steamdeck": ["README.md", "ps2xRuntime/CMakeLists.txt"],
    "android": ["android/README.md", "android/app/build.gradle", "android/gradle.properties",
                "ps2xRuntime/src/main.cpp (logcat redirect)", "ps2xRuntime/src/lib/ps2_android_runtime.cpp"],
    "vita": ["vita/setup.sh", "vita/build.sh", "ps2xRuntime/Readme.md (Vita notes)",
             "ps2xRuntime/CMakeLists.txt (vpk, title id)", "ps2xRuntime/src/lib/ps2_vita_runtime.cpp"],
    "web": ["snuri00/ps2-web-recomp README (third-party, not part of PS2Recomp)"],
}


def cmd_plan(args):
    elf = args.elf_name or "SLUS_203.12"
    steps = _plan(args.target, args.repo, args.build_dir, elf)
    if args.json:
        print(json.dumps({"target": args.target, "title": TARGET_TITLES[args.target],
                          "steps": [{"title": t, "commands": c, "notes": n} for t, c, n in steps],
                          "sources": PLAN_SOURCES[args.target]}, indent=2))
        return 0
    print("plan: %s" % TARGET_TITLES[args.target])
    if not args.elf_name and args.target != "web":
        print("(SLUS_203.12 below stands for your disc's ELF name: pass --elf-name)")
    for i, (title, commands, notes) in enumerate(steps, 1):
        print("")
        print("%d. %s" % (i, title))
        for c in commands:
            print("     $ " + c)
        for n in notes:
            print("     - " + n)
    print("")
    print("from: " + "; ".join(PLAN_SOURCES[args.target]))
    print("Players always supply their own disc. Nothing from the game goes in a package.")
    return 0


# ======================================================================= guard

DISC_EXTS = {".iso", ".bin", ".img", ".mdf", ".mds", ".nrg", ".chd", ".cso", ".zso", ".ecm", ".cue",
             ".ccd", ".sub", ".isz", ".dax"}
SOURCE_EXTS = {".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hh"}
SERIAL_NAME = re.compile(r"^[A-Za-z]{4}[_-]\d{3}\.?\d{2}(;\d+)?$")
RECOMP_MARKERS = (b"ps2_recompiled_functions.h", b"g_ps2RecompiledFunctionTable")
RECOMP_NAMES = {"register_functions.cpp", "ps2_recompiled_functions.h", "ps2_recompiled_stubs.h"}


def load_allow(path):
    rules = []
    if not path:
        return rules
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        raise UsageError("no such allow file: %s" % path)
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError("cannot read %s: %s" % (path, exc))
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "#" not in line:
            raise UsageError("%s line %d: every allowed path needs a reason: '<path or pattern>  # why'"
                             % (path, number))
        pattern, reason = line.split("#", 1)
        pattern, reason = pattern.strip(), reason.strip()
        if not pattern or not reason:
            raise UsageError("%s line %d: expected '<path or pattern>  # why'" % (path, number))
        rules.append((pattern.replace("\\", "/"), reason))
    return rules


def _read_head(path, size):
    try:
        with open(path, "rb") as fh:
            return fh.read(size)
    except OSError:
        return b""


def classify_file(path, rel, size):
    """Return the reason this file looks like game data, or None."""
    name = os.path.basename(rel)
    lower = name.lower()
    ext = os.path.splitext(lower)[1]
    parts = rel.replace("\\", "/").split("/")
    if parts[0] == "game" and len(parts) > 1 and name != PLACEHOLDER:
        return "inside game/: ship that folder empty, the player adds their own disc"
    if size > MAX_FILE:
        return "over 512 MB: only disc images and dumps are that big"
    if ext in DISC_EXTS:
        return "a disc image file (%s)" % ext
    if ext == ".irx":
        return "an IOP module (.IRX) from the disc"
    if lower == "system.cnf":
        return "the disc's SYSTEM.CNF"
    if SERIAL_NAME.match(name):
        return "named like a disc's main ELF (%s)" % name
    if name in RECOMP_NAMES:
        return "recompiled game code (%s)" % name
    if ext in SOURCE_EXTS and "runner" in [p.lower() for p in parts[:-1]]:
        return "a source file from a runner/ folder (recompiled game code)"
    head = _read_head(path, 0x9320)
    if head[:4] == b"\x7fELF" and len(head) >= 20:
        machine = int.from_bytes(head[18:20], "little" if head[5] == 1 else "big")
        if head[4] == 1 and machine == 8:
            return "a PS2 ELF or IRX (32-bit MIPS)"
    if head[:8] == b"MComprHD":
        return "a CHD disc image"
    if head[:4] in (b"CISO", b"ZISO"):
        return "a compressed ISO (CSO/ZSO)"
    if head[0x8001:0x8006] == b"CD001" or head[0x9319:0x931E] == b"CD001":
        return "an ISO 9660 disc image"
    if ext in SOURCE_EXTS:
        blob = _read_head(path, 256 * 1024)
        if any(marker in blob for marker in RECOMP_MARKERS):
            return "recompiled game code (references the generated function table)"
    return None


def scan_folder(root, allow_rules=()):
    findings, allowed = [], []
    for folder, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if rel == DIST_MARKER:
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            reason = classify_file(path, rel, size)
            if reason is None:
                continue
            rule = next((r for r in allow_rules if fnmatch.fnmatchcase(rel, r[0]) or rel == r[0]), None)
            if rule:
                allowed.append({"path": rel, "reason": reason, "allowed_because": rule[1]})
            else:
                findings.append({"path": rel, "reason": reason})
    return findings, allowed


def _print_findings(findings, allowed, root):
    for f in findings:
        print("  REFUSED  %s: %s" % (f["path"], f["reason"]))
    for a in allowed:
        print("  allowed  %s: %s (allow file: %s)" % (a["path"], a["reason"], a["allowed_because"]))
    if findings:
        print("Nothing from the game may be packaged or published: the disc, its ELF and IRX modules, "
              "its assets and the recompiled runner/ code stay on your machine.")


def cmd_guard(args):
    if not os.path.isdir(args.folder):
        raise UsageError("not a folder: %s" % args.folder)
    findings, allowed = scan_folder(args.folder, load_allow(args.allow))
    if args.json:
        print(json.dumps({"folder": args.folder, "refused": findings, "allowed": allowed,
                          "ok": not findings}, indent=2))
    else:
        print("guard: %s" % args.folder)
        _print_findings(findings, allowed, args.folder)
        if not findings:
            print("  ok: nothing that looks like game data")
    return 1 if findings else 0


# ======================================================================= recomp-json

def normalize_serial(text):
    t = (text or "").strip().upper().replace(";1", "")
    m = re.fullmatch(r"([A-Z]{4})[_-]?(\d{3})\.?(\d{2})", t)
    if not m:
        raise UsageError("%r is not a disc serial like SLUS-20312 or SLUS_203.12" % text)
    return "%s-%s%s" % (m.group(1), m.group(2), m.group(3))


LINKED_FILE = re.compile(r"(?i)(magnet:|(https?|ftp)://\S+\.(iso|bin|img|chd|cso|zso|elf|irx|7z|zip|rar|"
                         r"nrg|mdf)(\?\S*)?$)")


def _game_file_links(obj, path="$"):
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            hits += _game_file_links(v, "%s.%s" % (path, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += _game_file_links(v, "%s[%d]" % (path, i))
    elif isinstance(obj, str) and LINKED_FILE.search(obj.strip()):
        hits.append(path)
    return hits


def build_recomp_json(game, serial, status, region=None, existing=None):
    if not game or not game.strip():
        raise UsageError("--game needs the title as it shipped")
    if status not in STATUSES:
        raise UsageError("--status must be one of: %s" % ", ".join(STATUSES))
    serial = normalize_serial(serial)
    region = region or REGIONS.get(serial[:4])
    if not region:
        raise UsageError("cannot tell the region from %s: pass --region" % serial)
    data = {"$schema": "https://recomp.fyi/schema/v1.json", "game": game.strip(), "system": "PS2",
            "type": "recomp", "toolchain": "PS2Recomp", "status": status,
            "original": {"region": region, "serial": serial}}
    if existing:
        merged = dict(existing)
        original = dict(existing.get("original") or {})
        original.update(data["original"])
        merged.update({k: v for k, v in data.items() if k != "original"})
        merged["original"] = original
        data = merged
    bad = _game_file_links(data)
    if bad:
        raise UsageError("refusing: %s link(s) to a game file or archive (%s). Never put a game file, "
                         "or a link to one, in .recomp.json" % (len(bad), ", ".join(bad)))
    return data


def cmd_recomp_json(args):
    existing = None
    if os.path.exists(args.out):
        try:
            with open(args.out, "r", encoding="utf-8") as fh:
                existing = json.load(fh)
        except (OSError, ValueError) as exc:
            raise UsageError("%s exists but is not valid JSON (%s); fix or remove it" % (args.out, exc))
        if not isinstance(existing, dict):
            raise UsageError("%s exists but is not a JSON object" % args.out)
    data = build_recomp_json(args.game, args.serial, args.status, args.region, existing)
    folder = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(folder, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    print("%s %s: %s (%s, %s), status %s" % ("updated" if existing else "wrote", args.out, data["game"],
                                             data["original"]["serial"], data["original"]["region"],
                                             data["status"]))
    print("Optional file: PS2Recomp does not read it; recomp lists such as recomp.fyi do. Update the "
          "status when the project moves on.")
    return 0


# ======================================================================= package

def detect_binary(path):
    head = _read_head(path, 64)
    if head[:2] == b"MZ":
        return "windows"
    if head[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce",
                    b"\xca\xfe\xba\xbe"):
        return "macos"
    if head[:4] == b"\x7fELF" and len(head) >= 20:
        machine = int.from_bytes(head[18:20], "little" if head[5] == 1 else "big")
        return {8: "mips", 40: "arm32", 62: "linux", 183: "arm64-linux"}.get(machine, "elf")
    if head[:4] == b"PK\x03\x04":
        return "zip"
    return None


def _needs_ffmpeg(path):
    try:
        with open(path, "rb") as fh:
            tail = b""
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    return False
                if b"libavcodec" in tail + chunk:
                    return True
                tail = chunk[-16:]
    except OSError:
        return False


def _render_template(text, values):
    for key, value in values.items():
        text = text.replace("{{%s}}" % key, value)
    return text


def _launch_posix(runner_name, elf):
    return """#!/bin/sh
# Starts the game with YOUR disc: put your extracted disc in game/ first (see PLAYERS.md).
cd "$(dirname "$0")" || exit 1
ELF="%s"
if [ -z "$ELF" ]; then
  if [ ! -f game/SYSTEM.CNF ]; then
    echo "Put your extracted disc in game/ first (see PLAYERS.md)." >&2
    exit 1
  fi
  line=$(grep -i '^BOOT2' game/SYSTEM.CNF | head -n 1 | tr -d '\\r')
  # BOOT2 = cdrom0:\\SLUS_203.12;1  ->  SLUS_203.12 (a subfolder becomes DIR/NAME)
  ELF=$(printf '%%s' "$line" | sed -e 's/^[^=]*=[[:space:]]*//' -e 's/^cdrom0://' -e 's/;.*$//' \\
        | tr '\\\\' '/' | sed -e 's#^/*##' -e 's/[[:space:]]*$//')
  if [ -z "$ELF" ]; then
    echo "No BOOT2 line in game/SYSTEM.CNF: is this the whole extracted disc?" >&2
    exit 1
  fi
fi
if [ ! -f "game/$ELF" ]; then
  echo "game/$ELF not found. Put your extracted disc in game/ (see PLAYERS.md)." >&2
  exit 1
fi
exec "./%s" "game/$ELF" "$@"
""" % (elf, runner_name)


def _launch_bat(runner_name, elf):
    return "\r\n".join([
        "@echo off",
        "rem Starts the game with YOUR disc: put your extracted disc in game\\ first (see PLAYERS.md).",
        "setlocal",
        "cd /d \"%~dp0\"",
        "set \"ELF=%s\"" % elf,
        "if not \"%ELF%\"==\"\" goto run",
        "if not exist \"game\\SYSTEM.CNF\" (",
        "  echo Put your extracted disc in game\\ first ^(see PLAYERS.md^).",
        "  exit /b 1",
        ")",
        "rem BOOT2 = cdrom0:\\SLUS_203.12;1 : keep what follows the first backslash, then drop ;1",
        "for /f \"tokens=1,* delims=\\\" %%A in ('findstr /b /i \"BOOT2\" \"game\\SYSTEM.CNF\"') do set \"ELF=%%B\"",
        "if \"%ELF%\"==\"\" (",
        "  echo No BOOT2 line in game\\SYSTEM.CNF: is this the whole extracted disc?",
        "  exit /b 1",
        ")",
        "set \"ELF=%ELF:;1=%\"",
        ":run",
        "if not exist \"game\\%ELF%\" (",
        "  echo game\\%ELF% not found. Put your extracted disc in game\\ ^(see PLAYERS.md^).",
        "  exit /b 1",
        ")",
        "\"%%~dp0%s\" \"game\\%%ELF%%\" %%*" % runner_name,
        "",
    ])


def _install_android_sh(apk):
    return """#!/bin/sh
# Installs the port on a phone, tablet or TV box with USB or wireless debugging on,
# then copies YOUR extracted disc from game/ to the folder the app reads.
cd "$(dirname "$0")" || exit 1
command -v adb >/dev/null || { echo "adb not found: install Android platform-tools" >&2; exit 1; }
if [ -z "$(ls -A game 2>/dev/null | grep -v '%s')" ]; then
  echo "Put your extracted disc in game/ first (see PLAYERS.md)." >&2
  exit 1
fi
adb install -r "%s" || exit 1
adb shell mkdir -p %s
adb push game/. %s/
""" % (PLACEHOLDER, apk, ANDROID_FILES, ANDROID_FILES)


def _install_android_bat(apk):
    return "\r\n".join([
        "@echo off",
        "rem Installs the port with adb, then copies YOUR extracted disc from game\\ to the device.",
        "setlocal",
        "cd /d \"%~dp0\"",
        "where adb >nul 2>nul || (echo adb not found: install Android platform-tools & exit /b 1)",
        "if not exist \"game\\SYSTEM.CNF\" (echo Put your extracted disc in game\\ first ^(see PLAYERS.md^). & exit /b 1)",
        "adb install -r \"%s\" || exit /b 1" % apk,
        "adb shell mkdir -p %s" % ANDROID_FILES,
        "adb push game\\. %s/" % ANDROID_FILES,
        "",
    ])


TARGET_HOWTO = {
    "windows": ("Double-click play.bat (or run it from a terminal). It starts {{RUNNER}} with "
                "game\\{{ELF_SHOWN}}.", "Windows 10 or 11, x64."),
    "macos": ("Double-click play.command (or run ./play.command in Terminal). It starts {{RUNNER}} with "
              "game/{{ELF_SHOWN}}. If macOS blocks it the first time, right-click play.command and choose "
              "Open.", "A Mac of the same kind it was built on (Apple silicon or Intel)."),
    "linux": ("Run ./play.sh. It starts {{RUNNER}} with game/{{ELF_SHOWN}}.",
              "Linux x86_64 with OpenGL."),
    "steamdeck": ("Desktop Mode: Steam, Add a Non-Steam Game, pick play.sh (show all file types), then "
                  "start it from Game Mode. Steam Input makes the Deck's controls an Xbox-style pad.",
                  "Steam Deck (SteamOS) or any Linux x86_64 PC."),
    "android": ("With USB debugging on (or wireless debugging plus adb connect), run install-android.sh "
                "(or install-android.bat on Windows). It installs {{RUNNER}} and copies game/ to "
                "{{ANDROID_FILES}}. The app looks for {{ANDROID_ELF}} there.",
                "Android 9 or newer, arm64 (x86_64 for emulators). Pair a controller: there are no "
                "on-screen buttons."),
    "vita": ("Install {{RUNNER}} with VitaShell, then copy everything inside game/ to {{VITA_DIR}} on "
             "the Vita (VitaShell's FTP or USB mode). The vpk looks for {{VITA_ELF}}.",
             "A PS Vita that already runs homebrew, with VitaShell."),
}


def _write(path, text, executable=False, newline="\n"):
    with open(path, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(text)
    if executable:
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def cmd_package(args):
    runner = args.runner
    if not os.path.isfile(runner):
        raise UsageError("runner not found: %s (build it first: targets.py plan --target %s)"
                         % (runner, args.target))
    kind = detect_binary(runner)
    lower = runner.lower()
    expect = {"windows": ("windows",), "macos": ("macos",), "linux": ("linux", "arm64-linux"),
              "steamdeck": ("linux",)}
    warnings = []
    if args.target == "android" and not lower.endswith(".apk"):
        raise UsageError("the Android runner is the APK (android/app/build/outputs/apk/release/app-release.apk)")
    if args.target == "vita" and not lower.endswith(".vpk"):
        raise UsageError("the Vita runner is the .vpk that vita/build.sh makes")
    if args.target == "windows" and not lower.endswith(".exe"):
        raise UsageError("the Windows runner is ps2EntryRunner.exe")
    if kind == "mips":
        raise UsageError("%s is a PS2 (MIPS) ELF: that is the game, not the runner" % runner)
    if args.target in expect:
        if kind is None:
            warnings.append("%s is not a recognised executable; check it is the runner built for %s"
                            % (runner, args.target))
        elif kind not in expect[args.target]:
            raise UsageError("%s looks like a %s binary, not a %s one" % (runner, kind, args.target))
    if args.elf_name:
        if "/" in args.elf_name or "\\" in args.elf_name or args.elf_name.startswith("."):
            raise UsageError("--elf-name is just the ELF's file name, like SLUS_203.12")
    for lib in args.lib:
        if not os.path.isfile(lib):
            raise UsageError("--lib file not found: %s" % lib)
    allow = load_allow(args.allow)
    meta = None
    if args.game or args.serial:
        if not (args.game and args.serial):
            raise UsageError("--game and --serial go together (they fill .recomp.json)")
        meta = build_recomp_json(args.game, args.serial, args.status, args.region)

    out = os.path.abspath(args.out)
    if os.path.exists(out):
        if not os.path.isdir(out):
            raise UsageError("%s exists and is not a folder" % out)
        if os.listdir(out):
            if not os.path.exists(os.path.join(out, DIST_MARKER)):
                raise UsageError("%s is not empty and was not made by this tool; pick another --out" % out)
            if not args.replace:
                raise UsageError("%s already holds a package; pass --replace to rebuild it" % out)
            old_game = os.path.join(out, "game")
            if os.path.isdir(old_game) and [n for n in os.listdir(old_game) if n != PLACEHOLDER]:
                raise UsageError("%s/game/ holds files (your disc, or saves in mc0/): move them somewhere "
                                 "safe first, or package to a new --out. Nothing in game/ is ever deleted"
                                 % out)
    staging = out + ".staging-%d" % os.getpid()
    if os.path.exists(staging):
        shutil.rmtree(staging)
    os.makedirs(os.path.join(staging, "game"))
    try:
        runner_name = os.path.basename(runner)
        shutil.copy2(runner, os.path.join(staging, runner_name))
        copied = [runner_name]
        lib_exts = {"windows": (".dll",), "macos": (".dylib",), "linux": (".so",), "steamdeck": (".so",)}
        runner_dir = os.path.dirname(os.path.abspath(runner))
        if args.target in lib_exts:
            for name in sorted(os.listdir(runner_dir)):
                full = os.path.join(runner_dir, name)
                low = name.lower()
                if not os.path.isfile(full) or name == runner_name:
                    continue
                if low.endswith(lib_exts[args.target]) or (args.target in ("linux", "steamdeck") and ".so." in low):
                    shutil.copy2(full, os.path.join(staging, name))
                    copied.append(name)
        for lib in args.lib:
            shutil.copy2(lib, os.path.join(staging, os.path.basename(lib)))
            copied.append(os.path.basename(lib))

        elf = args.elf_name or ""
        elf_shown = elf or "<the ELF named in your disc's SYSTEM.CNF>"
        _write(os.path.join(staging, "game", PLACEHOLDER),
               "Put the files from YOUR OWN disc here (the whole extracted disc: SYSTEM.CNF, the main "
               "ELF%s, the folders).\nSee PLAYERS.md. Nothing from the game ships with this folder.\n"
               % (" " + elf if elf else ""))
        scripts = []
        if args.target == "windows":
            _write(os.path.join(staging, "play.bat"), _launch_bat(runner_name, elf), newline="")
            scripts.append("play.bat")
        elif args.target == "macos":
            _write(os.path.join(staging, "play.command"), _launch_posix(runner_name, elf), executable=True)
            scripts.append("play.command")
        elif args.target in ("linux", "steamdeck"):
            _write(os.path.join(staging, "play.sh"), _launch_posix(runner_name, elf), executable=True)
            scripts.append("play.sh")
        elif args.target == "android":
            _write(os.path.join(staging, "install-android.sh"), _install_android_sh(runner_name), executable=True)
            _write(os.path.join(staging, "install-android.bat"), _install_android_bat(runner_name), newline="")
            scripts += ["install-android.sh", "install-android.bat"]
        if os.name != "nt":
            os.chmod(os.path.join(staging, runner_name),
                     os.stat(os.path.join(staging, runner_name)).st_mode | stat.S_IXUSR)

        deps = []
        if args.target in ("macos", "linux", "steamdeck") and _needs_ffmpeg(runner):
            deps.append("FFmpeg's libraries: " + ("brew install ffmpeg" if args.target == "macos" else
                                                  "sudo apt install ffmpeg (or your distro's ffmpeg package)"))
        howto, needs = TARGET_HOWTO[args.target]
        values = {
            "GAME": (meta or {}).get("game") or args.name or "this game",
            "TARGET": TARGET_TITLES[args.target],
            "RUNNER": runner_name,
            "ELF_SHOWN": elf_shown,
            "ANDROID_FILES": ANDROID_FILES,
            "ANDROID_ELF": "%s/%s" % (ANDROID_FILES, elf or "<the ELF name it was built with>"),
            "VITA_DIR": VITA_DEFAULT_DIR + "/",
            "VITA_ELF": "%s/%s" % (VITA_DEFAULT_DIR, elf or "<the ELF name it was built with>"),
            "SERIAL": (meta or {}).get("original", {}).get("serial", "the serial on your disc"),
            "SAVES": {"android": "`%s/mc0/` on the device" % ANDROID_FILES,
                      "vita": "`%s/mc0/` on the Vita" % VITA_DEFAULT_DIR,
                      "windows": "`game\\mc0\\`"}.get(args.target, "`game/mc0/`"),
        }
        howto = _render_template(howto, values)
        values["HOW_TO_RUN"] = howto
        values["NEEDS"] = needs + ((" Also: " + "; ".join(deps) + ".") if deps else "")
        values["FILES"] = "\n".join("- `%s`" % f for f in copied + scripts)
        with open(os.path.join(HERE, "templates", "PLAYERS.md"), "r", encoding="utf-8") as fh:
            players = _render_template(fh.read(), values)
        _write(os.path.join(staging, "PLAYERS.md"), players)
        if meta:
            _write(os.path.join(staging, ".recomp.json"), json.dumps(meta, indent=2) + "\n")

        findings, allowed = scan_folder(staging, allow)
        if findings:
            print("package REFUSED: %d file(s) look like game data" % len(findings))
            _print_findings(findings, allowed, staging)
            shutil.rmtree(staging, ignore_errors=True)
            return 1
        manifest = {"tool": "console-anywhere targets.py", "target": args.target, "runner": runner_name,
                    "elf": elf or None, "files": sorted(copied + scripts + ["PLAYERS.md"]
                                                         + ([".recomp.json"] if meta else [])),
                    "allowed": allowed,
                    "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        _write(os.path.join(staging, DIST_MARKER), json.dumps(manifest, indent=2) + "\n")
        if os.path.exists(out):
            shutil.rmtree(out)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        os.rename(staging, out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    if args.json:
        print(json.dumps({"ok": True, "out": out, "manifest": manifest, "warnings": warnings}, indent=2))
    else:
        for w in warnings:
            print("warning: " + w)
        for a in allowed:
            print("allowed  %s: %s (allow file: %s)" % (a["path"], a["reason"], a["allowed_because"]))
        print("packaged %s for %s" % (out, TARGET_TITLES[args.target]))
        for f in manifest["files"]:
            print("  " + f)
        print("  game/  (empty: the player puts their own extracted disc here)")
        print("The runner is built from your game's recompiled code: this folder is for your own devices. "
              "Do not upload or share it.")
    return 0


# ======================================================================= CLI

def build_parser():
    p = argparse.ArgumentParser(
        prog="targets.py",
        description="Build and package a PS2Recomp port for Windows, Mac, Linux, Steam Deck, Android and "
                    "PS Vita, with no game files inside.",
        epilog="Typical: targets.py doctor --target android; targets.py plan --target android "
               "--elf-name SLUS_203.12; targets.py package --runner app-release.apk --target android "
               "--elf-name SLUS_203.12 --out console/dist/mygame-android")
    sub = p.add_subparsers(dest="cmd", metavar="command")

    sp = sub.add_parser("doctor", help="check this machine's tools for a target")
    sp.add_argument("--target", action="append", choices=TARGETS + ["all"],
                    help="repeatable; default: this machine's own OS")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("plan", help="print the build steps for a target")
    sp.add_argument("--target", required=True, choices=TARGETS)
    sp.add_argument("--repo", default="PS2Recomp", help="your PS2Recomp checkout (default PS2Recomp)")
    sp.add_argument("--build-dir", default="out/build", help="your build folder (default out/build)")
    sp.add_argument("--elf-name", help="the disc's main ELF name, like SLUS_203.12")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("package", help="build a player folder with no game files inside")
    sp.add_argument("--runner", required=True, help="ps2EntryRunner(.exe), the APK, or the .vpk")
    sp.add_argument("--target", required=True, choices=PACKAGE_TARGETS)
    sp.add_argument("--out", required=True, help="output folder, like console/dist/mygame-windows")
    sp.add_argument("--elf-name", help="the ELF name the launch script passes (else read from SYSTEM.CNF)")
    sp.add_argument("--name", help="what PLAYERS.md calls the game")
    sp.add_argument("--game", help="title as it shipped (for .recomp.json)")
    sp.add_argument("--serial", help="disc serial, like SLUS-20312 (for .recomp.json)")
    sp.add_argument("--status", default="in-progress", choices=STATUSES)
    sp.add_argument("--region", help="only if the serial prefix does not say")
    sp.add_argument("--lib", action="append", default=[], help="an extra library to ship next to the runner")
    sp.add_argument("--allow", help="allow file: '<path or pattern>  # reason' per line")
    sp.add_argument("--replace", action="store_true", help="rebuild a package this tool made before")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_package)

    sp = sub.add_parser("guard", help="check any folder for game data before you zip or push it")
    sp.add_argument("folder")
    sp.add_argument("--allow", help="allow file: '<path or pattern>  # reason' per line")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_guard)

    sp = sub.add_parser("recomp-json", help="write the optional .recomp.json")
    sp.add_argument("--game", required=True, help="title as it shipped")
    sp.add_argument("--serial", required=True, help="like SLUS-20312 or SLUS_203.12")
    sp.add_argument("--status", default="in-progress", choices=STATUSES)
    sp.add_argument("--region", help="only if the serial prefix does not say")
    sp.add_argument("--out", default=".recomp.json")
    sp.set_defaults(func=cmd_recomp_json)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except UsageError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
