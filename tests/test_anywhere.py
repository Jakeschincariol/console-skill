"""Tests for console-anywhere/targets.py (offline: fake toolchains, synthesized files)."""

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

from _load import load, ROOT

targets = load("console-anywhere", "targets")
SKILL = os.path.join(ROOT, "console-anywhere")


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = targets.main(list(argv))
        except SystemExit as exc:  # argparse errors
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class FakeProbe:
    def __init__(self, system="Darwin", machine="arm64", tools=(), runs=None, paths=(), env=None, files=None):
        self.system = system
        self.machine = machine
        self.tools = {t: "/usr/bin/" + t for t in tools}
        self.runs = runs or {}
        self.paths = set(paths)
        self.env = env or {"HOME": "/home/u"}
        self.files = files or {}

    def which(self, name):
        return self.tools.get(name)

    def run(self, cmd, timeout=15):
        key = os.path.basename(cmd[0]) + " " + " ".join(cmd[1:])
        for prefix, result in self.runs.items():
            if key.startswith(prefix):
                return result
        return None, ""

    def exists(self, path):
        return path in self.paths

    def read(self, path):
        return self.files.get(path, "")


def by_name(checks):
    return {c["name"]: c for c in checks}


MAC_TOOLS = ("clang++", "cmake", "git", "ninja", "pkg-config")
MAC_RUNS = {"xcode-select -p": (0, "/Library/Developer/CommandLineTools\n"),
            "cmake --version": (0, "cmake version 3.29.2\n"),
            "pkg-config --exists": (0, "")}


class TestDoctor(unittest.TestCase):
    def test_mac_all_good(self):
        probe = FakeProbe(tools=MAC_TOOLS, runs=MAC_RUNS)
        checks = targets.doctor(probe, "macos")
        self.assertTrue(all(c["ok"] for c in checks if c["required"]), checks)
        names = by_name(checks)
        self.assertIn("Apple silicon: sse2neon", names)
        self.assertEqual(names["cmake 3.21+"]["found"], "3.29.2")

    def test_mac_missing_ffmpeg_and_old_cmake(self):
        runs = dict(MAC_RUNS)
        runs["cmake --version"] = (0, "cmake version 3.16.3\n")
        probe = FakeProbe(tools=("clang++", "cmake", "git"), runs=runs)
        names = by_name(targets.doctor(probe, "macos"))
        self.assertFalse(names["cmake 3.21+"]["ok"])
        ff = names["pkg-config + FFmpeg libraries"]
        self.assertFalse(ff["ok"])
        self.assertIn("brew install pkg-config ffmpeg", ff["fix"])
        self.assertIn("-DPS2X_ENABLE_FFMPEG=OFF", ff["fix"])
        self.assertFalse(names["ninja"]["required"])

    def test_linux_and_steamdeck(self):
        runs = {"cmake --version": (0, "cmake version 3.28.3"), "pkg-config --exists libavcodec": (0, ""),
                "pkg-config --exists x11": (1, "")}
        probe = FakeProbe(system="Linux", machine="x86_64", tools=("c++", "cmake", "git", "pkg-config"),
                          runs=runs, files={"/etc/os-release": "ID=steamos\n"})
        names = by_name(targets.doctor(probe, "steamdeck"))
        self.assertFalse(names["raylib's X11 and OpenGL headers"]["ok"])
        self.assertIn("libx11-dev", names["raylib's X11 and OpenGL headers"]["fix"])
        self.assertIn("distrobox", names["SteamOS: writable build environment"]["fix"])
        arm = FakeProbe(system="Linux", machine="aarch64", tools=("c++", "cmake"), runs=runs)
        self.assertFalse(by_name(targets.doctor(arm, "steamdeck"))["x86_64 CPU"]["ok"])

    def test_wrong_host(self):
        mac = FakeProbe()
        for target, needle in (("windows", "Windows"), ("linux", "Linux"), ("steamdeck", "Proton")):
            checks = targets.doctor(mac, target)
            self.assertEqual(len(checks), 1)
            self.assertFalse(checks[0]["ok"])
            self.assertIn(needle, checks[0]["name"] + checks[0]["fix"])
        lin = FakeProbe(system="Linux", machine="x86_64")
        self.assertIn("Mac", targets.doctor(lin, "macos")[0]["name"])

    def test_windows_with_vswhere(self):
        vswhere = os.path.join(r"C:\Program Files (x86)", "Microsoft Visual Studio", "Installer", "vswhere.exe")
        probe = FakeProbe(system="Windows", machine="amd64", tools=("cmake", "git"),
                          runs={"cmake --version": (0, "cmake version 3.30.0"),
                                "vswhere.exe -latest": (0, r"C:\VS\2022\Community" + "\n")},
                          paths=(vswhere,), env={"ProgramFiles(x86)": r"C:\Program Files (x86)"})
        names = by_name(targets.doctor(probe, "windows"))
        msvc = [c for n, c in names.items() if n.startswith("MSVC")][0]
        self.assertTrue(msvc["ok"])
        self.assertIn("Community", msvc["found"])

    def test_android(self):
        sdk = "/sdk"
        paths = {os.path.join(sdk, "platforms", "android-34"), os.path.join(sdk, "ndk", "28.2.13676358"),
                 os.path.join(sdk, "cmake", "3.22.1")}
        probe = FakeProbe(tools=("java", "gradle", "adb"),
                          runs={"java -version": (0, 'openjdk version "17.0.9" 2023-10-17\n'),
                                "gradle --version": (0, "\nGradle 8.9\n")},
                          paths=paths, env={"ANDROID_HOME": sdk})
        checks = targets.doctor(probe, "android")
        self.assertTrue(all(c["ok"] for c in checks), checks)
        old = FakeProbe(tools=("java", "gradle"),
                        runs={"java -version": (0, 'java version "1.8.0_292"\n'),
                              "gradle --version": (0, "Gradle 8.2.1\n")}, env={})
        names = by_name(targets.doctor(old, "android"))
        self.assertFalse(names["JDK 17"]["ok"])
        self.assertEqual(names["JDK 17"]["found"], "Java 8")
        self.assertFalse(names["Gradle 8.7+ (or Android Studio)"]["ok"])
        self.assertFalse(names["Android SDK"]["ok"])
        partial = FakeProbe(tools=("java",), runs={"java -version": (0, 'openjdk version "17"')},
                            env={"ANDROID_SDK_ROOT": sdk}, paths={"/Applications/Android Studio.app"})
        names = by_name(targets.doctor(partial, "android"))
        self.assertTrue(names["Gradle 8.7+ (or Android Studio)"]["ok"])
        ndk = [c for n, c in names.items() if n.startswith("NDK")][0]
        self.assertFalse(ndk["ok"])
        self.assertIn("ndk;28.2.13676358", ndk["fix"])

    def test_vita(self):
        probe = FakeProbe(system="Linux", machine="x86_64", tools=("cmake", "curl", "unzip", "bash", "nproc"),
                          env={})
        names = by_name(targets.doctor(probe, "vita"))
        self.assertFalse(names["VITASDK set"]["ok"])
        vs = "/usr/local/vitasdk"
        inc = os.path.join(vs, "arm-vita-eabi", "include")
        ready = FakeProbe(system="Linux", machine="x86_64",
                          tools=("cmake", "curl", "unzip", "bash", "nproc", "arm-vita-eabi-gcc"),
                          env={"VITASDK": vs},
                          paths={os.path.join(vs, "share", "vita.toolchain.cmake"),
                                 os.path.join(inc, "SDL2", "SDL.h"), os.path.join(inc, "gpu_es4", "psp2_pvr_hint.h")})
        self.assertTrue(all(c["ok"] for c in targets.doctor(ready, "vita") if c["required"]))
        mac = FakeProbe(tools=("cmake",), env={})
        names = by_name(targets.doctor(mac, "vita"))
        self.assertIn("coreutils", names["nproc (used by setup.sh and build.sh)"]["fix"])
        self.assertIn("GNU install (setup.sh uses install -D -t)", names)

    def test_web(self):
        probe = FakeProbe(tools=("git", "cmake", "ninja", "clang++", "python3", "bash"),
                          runs={"cmake --version": (0, "cmake version 3.29.0"),
                                "bash --version": (0, "GNU bash, version 3.2.57(1)-release")})
        names = by_name(targets.doctor(probe, "web"))
        self.assertFalse(names["bash 4+ (macOS ships 3.2)"]["ok"])
        self.assertIn("brew install bash", names["bash 4+ (macOS ships 3.2)"]["fix"])
        self.assertFalse(names["7-Zip (7zz)"]["ok"])
        self.assertTrue(names["Emscripten"]["ok"])

    def test_cmd_doctor_output_and_exit(self):
        probe = FakeProbe(tools=MAC_TOOLS, runs=MAC_RUNS)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = targets.cmd_doctor(argparse.Namespace(target=None, json=True), probe)
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(list(data["targets"]), ["macos"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = targets.cmd_doctor(argparse.Namespace(target=["all"], json=False), probe)
        self.assertEqual(code, 1)
        text = out.getvalue()
        for t in targets.TARGETS:
            self.assertIn(t + ": ", text)
        self.assertIn("MISSING", text)
        self.assertIn("-> ", text)

    def test_real_doctor_runs(self):
        code, out, err = cli("doctor", "--json")
        self.assertIn(code, (0, 1), err)
        self.assertIn("targets", json.loads(out))


class TestPlan(unittest.TestCase):
    def plan(self, target, *extra):
        code, out, err = cli("plan", "--target", target, "--elf-name", "SLUS_203.12", *extra)
        self.assertEqual(code, 0, err)
        return out

    def test_every_target(self):
        for target in targets.TARGETS:
            out = self.plan(target)
            self.assertIn("plan: ", out)
            self.assertNotIn("\u2014", out)
            code, js, _ = cli("plan", "--target", target, "--json")
            data = json.loads(js)
            self.assertTrue(data["steps"])
            self.assertTrue(data["sources"])

    def test_desktop_facts(self):
        out = self.plan("windows")
        self.assertIn("git clone --recurse-submodules https://github.com/ran-j/PS2Recomp.git", out)
        self.assertIn("cmake -S . -B out/build", out)
        self.assertIn("cmake --build out/build --config Release --target ps2EntryRunner", out)
        self.assertIn("PS2X_SHOW_WINDOWS_CONSOLE=OFF", out)
        out = self.plan("macos")
        self.assertIn("sse2neon", out)
        self.assertIn("-DPS2X_ENABLE_FFMPEG=OFF", out)
        self.assertIn("--target ps2EntryRunner", out)
        out = self.plan("linux", "--repo", "~/src/PS2Recomp")
        self.assertIn("cd ~/src/PS2Recomp", out)
        self.assertIn("libavcodec-dev", out)
        out = self.plan("steamdeck")
        self.assertIn("Add a Non-Steam Game", out)
        self.assertIn("Proton", out)

    def test_android_facts(self):
        out = self.plan("android")
        self.assertIn("gradle wrapper --gradle-version 8.9", out)
        self.assertIn("./gradlew assembleRelease -Pps2xBootElf=/storage/emulated/0/Android/data/"
                      "com.ps2x.runner/files/SLUS_203.12", out)
        self.assertIn("android/app/build/outputs/apk/release/app-release.apk", out)
        self.assertIn("adb shell mkdir -p /storage/emulated/0/Android/data/com.ps2x.runner/files", out)
        self.assertIn("28.2.13676358", out)
        self.assertIn("ps2_android_runtime.cpp", out)

    def test_vita_and_web_facts(self):
        out = self.plan("vita")
        self.assertIn("./vita/setup.sh", out)
        self.assertIn("./vita/build.sh ux0:data/ps2x/SLUS_203.12", out)
        self.assertIn("CLEAN=1", out)
        self.assertIn("RANJ00001", out)
        out = self.plan("web")
        self.assertIn("second port of the same disc", out)
        self.assertIn("scripts/port.sh mygame", out)
        self.assertIn("scripts/serve.py build/web-mygame", out)
        self.assertIn("WebGPU", out)
        self.assertIn("--native-only", out)

    def test_bad_target(self):
        self.assertEqual(cli("plan", "--target", "gamecube")[0], 2)


# ---------------------------------------------------------------- synthesized files

def elf_header(machine, cls=1):
    head = bytearray(64)
    head[0:4] = b"\x7fELF"
    head[4] = cls
    head[5] = 1
    head[6] = 1
    head[18:20] = machine.to_bytes(2, "little")
    return bytes(head)


def fake_iso(raw=False):
    data = bytearray(0x9400)
    if raw:
        data[0x9319:0x931E] = b"CD001"
    else:
        data[0x8001:0x8006] = b"CD001"
    return bytes(data)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, rel, data=b"", executable=False):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb" if isinstance(data, bytes) else "w") as fh:
            fh.write(data)
        if executable:
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path


class TestGuard(Tmp):
    def reasons(self, folder, allow=()):
        findings, allowed = targets.scan_folder(folder, allow)
        return {f["path"]: f["reason"] for f in findings}, allowed

    def test_flags_game_data(self):
        root = os.path.join(self.tmp, "dist")
        self.put("dist/backup.dat", fake_iso())
        self.put("dist/raw.dat", fake_iso(raw=True))
        self.put("dist/disc.iso", b"")
        self.put("dist/disc.chd.part", b"MComprHD" + bytes(100))
        self.put("dist/disc.cso.part", b"CISO" + bytes(100))
        self.put("dist/main.dat", elf_header(8))
        self.put("dist/SLUS_203.12", b"anything")
        self.put("dist/MODULES/PADMAN.IRX", b"x")
        self.put("dist/SYSTEM.CNF", b"BOOT2 = cdrom0:\\SLUS_203.12;1\n")
        self.put("dist/src/runner/sub_00100000.cpp", b"// code\n")
        self.put("dist/register_functions.cpp", b"")
        self.put("dist/gen.cpp", b"#include <ps2_recompiled_functions.h>\n")
        self.put("dist/game/stuff.bin.txt", b"x")
        found, _ = self.reasons(root)
        expect = {"backup.dat": "ISO 9660", "raw.dat": "ISO 9660", "disc.iso": "disc image",
                  "disc.chd.part": "CHD", "disc.cso.part": "CSO", "main.dat": "MIPS",
                  "SLUS_203.12": "main ELF", "MODULES/PADMAN.IRX": "IRX", "SYSTEM.CNF": "SYSTEM.CNF",
                  "src/runner/sub_00100000.cpp": "runner/", "register_functions.cpp": "recompiled",
                  "gen.cpp": "recompiled", "game/stuff.bin.txt": "inside game/"}
        self.assertEqual(set(found), set(expect))
        for path, needle in expect.items():
            self.assertIn(needle, found[path], path)

    def test_leaves_your_own_files_alone(self):
        root = os.path.join(self.tmp, "ok")
        self.put("ok/ps2EntryRunner", elf_header(62, cls=2))
        self.put("ok/libraylib.so.5", elf_header(62, cls=2))
        self.put("ok/patches/SLUS-20312_1A2B3C4D.pnach", b"patch=1,EE,00100000,word,00000000\n")
        self.put("ok/patches/gold_rifle.cpp", b'#include "game_overrides.h"\n')
        self.put("ok/README.md", b"# my mod\n")
        self.put("ok/game/" + targets.PLACEHOLDER, b"put it here")
        self.put("ok/" + targets.DIST_MARKER, b"{}")
        found, _ = self.reasons(root)
        self.assertEqual(found, {})

    def test_big_files(self):
        path = self.put("big/huge.dat", b"")
        with open(path, "r+b") as fh:
            fh.truncate(targets.MAX_FILE + 1)
        found, _ = self.reasons(os.path.join(self.tmp, "big"))
        self.assertIn("512 MB", found["huge.dat"])

    def test_allow_file(self):
        self.put("d/config.bin", b"my own settings")
        self.put("d/disc.iso", b"")
        allow_path = self.put("allow.txt", "# reviewed\nconfig.bin  # my own settings file, not from the disc\n")
        rules = targets.load_allow(allow_path)
        found, allowed = self.reasons(os.path.join(self.tmp, "d"), rules)
        self.assertEqual(list(found), ["disc.iso"])
        self.assertEqual(allowed[0]["path"], "config.bin")
        bad = self.put("bad.txt", "config.bin\n")
        with self.assertRaises(targets.UsageError):
            targets.load_allow(bad)
        bad2 = self.put("bad2.txt", "#only a comment\n   # \nconfig.bin #\n")
        with self.assertRaises(targets.UsageError):
            targets.load_allow(bad2)

    def test_guard_cli(self):
        self.put("g/disc.iso", b"")
        code, out, _ = cli("guard", os.path.join(self.tmp, "g"))
        self.assertEqual(code, 1)
        self.assertIn("REFUSED  disc.iso", out)
        code, out, _ = cli("guard", os.path.join(self.tmp, "g"), "--json")
        self.assertFalse(json.loads(out)["ok"])
        self.put("clean/readme.txt", b"hi")
        self.assertEqual(cli("guard", os.path.join(self.tmp, "clean"))[0], 0)
        self.assertEqual(cli("guard", os.path.join(self.tmp, "nope"))[0], 2)
        self.assertEqual(cli("guard", os.path.join(self.tmp, "clean"), "--allow",
                             os.path.join(self.tmp, "none.txt"))[0], 2)


class TestPackage(Tmp):
    def out(self, name):
        return os.path.join(self.tmp, "console", "dist", name)

    def staging_left(self):
        dist = os.path.join(self.tmp, "console", "dist")
        return [n for n in os.listdir(dist) if ".staging-" in n] if os.path.isdir(dist) else []

    def test_linux_package(self):
        runner = self.put("build/ps2EntryRunner", elf_header(62, cls=2) + b"\0libavcodec.so.61\0", executable=True)
        self.put("build/libextra.so.1", elf_header(62, cls=2))
        self.put("build/CMakeCache.txt", b"not shipped")
        out = self.out("mygame-linux")
        code, text, err = cli("package", "--runner", runner, "--target", "linux", "--out", out,
                              "--game", "My Game", "--serial", "SLUS_203.12", "--status", "playable")
        self.assertEqual(code, 0, err + text)
        files = sorted(os.listdir(out))
        self.assertEqual(files, sorted([".console-dist.json", ".recomp.json", "PLAYERS.md", "game",
                                        "libextra.so.1", "play.sh", "ps2EntryRunner"]))
        self.assertEqual(os.listdir(os.path.join(out, "game")), [targets.PLACEHOLDER])
        self.assertTrue(os.access(os.path.join(out, "play.sh"), os.X_OK))
        with open(os.path.join(out, "PLAYERS.md")) as fh:
            players = fh.read()
        self.assertNotIn("{{", players)
        self.assertIn("My Game", players)
        self.assertIn("SLUS-20312", players)
        self.assertIn("FFmpeg", players)
        self.assertIn("Never download a disc image", players)
        self.assertNotIn("\u2014", players)
        with open(os.path.join(out, ".recomp.json")) as fh:
            meta = json.load(fh)
        self.assertEqual(meta["status"], "playable")
        with open(os.path.join(out, targets.DIST_MARKER)) as fh:
            self.assertEqual(json.load(fh)["target"], "linux")
        self.assertIn("for your own devices", text)
        self.assertEqual(self.staging_left(), [])

    def test_launch_script_reads_system_cnf(self):
        runner = self.put("build/ps2EntryRunner", "#!/bin/sh\necho \"runner got: $1\"\n", executable=True)
        out = self.out("deck")
        code, text, _ = cli("package", "--runner", runner, "--target", "steamdeck", "--out", out)
        self.assertEqual(code, 0)
        self.assertIn("not a recognised executable", text)
        play = os.path.join(out, "play.sh")
        proc = subprocess.run(["/bin/sh", play], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"Put your extracted disc in game/", proc.stderr)
        with open(os.path.join(out, "game", "SYSTEM.CNF"), "wb") as fh:
            fh.write(b"BOOT2 = cdrom0:\\SLUS_203.12;1\r\nVER = 1.00\r\nVMODE = NTSC\r\n")
        proc = subprocess.run(["/bin/sh", play], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertIn(b"game/SLUS_203.12 not found", proc.stderr)
        open(os.path.join(out, "game", "SLUS_203.12"), "wb").close()
        proc = subprocess.run(["/bin/sh", play], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.stdout.strip(), b"runner got: game/SLUS_203.12")
        os.makedirs(os.path.join(out, "game", "DATA"))
        open(os.path.join(out, "game", "DATA", "MAIN.ELF"), "wb").close()
        with open(os.path.join(out, "game", "SYSTEM.CNF"), "wb") as fh:
            fh.write(b"BOOT2=cdrom0:\\DATA\\MAIN.ELF;1\n")
        proc = subprocess.run(["/bin/sh", play], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.stdout.strip(), b"runner got: game/DATA/MAIN.ELF")
        # game/ now holds a disc: a rebuild must never delete it.
        code, _, err = cli("package", "--runner", runner, "--target", "steamdeck", "--out", out, "--replace")
        self.assertEqual(code, 2)
        self.assertIn("is ever deleted", err)
        self.assertTrue(os.path.isfile(os.path.join(out, "game", "SYSTEM.CNF")))
        shutil.rmtree(os.path.join(out, "game", "DATA"))
        for name in ("SYSTEM.CNF", "SLUS_203.12"):
            os.remove(os.path.join(out, "game", name))
        code, _, _ = cli("package", "--runner", runner, "--target", "steamdeck", "--out", out, "--replace")
        self.assertEqual(code, 0)
        self.assertEqual(os.listdir(os.path.join(out, "game")), [targets.PLACEHOLDER])

    def test_elf_name_baked_in(self):
        runner = self.put("build/ps2EntryRunner", "#!/bin/sh\necho \"runner got: $1\"\n", executable=True)
        out = self.out("baked")
        self.assertEqual(cli("package", "--runner", runner, "--target", "linux", "--out", out,
                             "--elf-name", "SCES_500.51")[0], 0)
        open(os.path.join(out, "game", "SCES_500.51"), "wb").close()
        proc = subprocess.run(["/bin/sh", os.path.join(out, "play.sh")], stdout=subprocess.PIPE)
        self.assertEqual(proc.stdout.strip(), b"runner got: game/SCES_500.51")
        self.assertEqual(cli("package", "--runner", runner, "--target", "linux", "--out", self.out("x"),
                             "--elf-name", "../SLUS_203.12")[0], 2)

    def test_windows_package(self):
        runner = self.put("build/Release/ps2EntryRunner.exe", b"MZ" + bytes(100))
        self.put("build/Release/avcodec-61.dll", b"MZ" + bytes(10))
        out = self.out("win")
        code, text, err = cli("package", "--runner", runner, "--target", "windows", "--out", out,
                              "--elf-name", "SLUS_203.12")
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isfile(os.path.join(out, "avcodec-61.dll")))
        with open(os.path.join(out, "play.bat"), "rb") as fh:
            bat = fh.read()
        self.assertIn(b"\r\n", bat)
        self.assertIn(b'"%~dp0ps2EntryRunner.exe" "game\\%ELF%" %*', bat)
        self.assertIn(b'set "ELF=SLUS_203.12"', bat)
        self.assertIn(b"tokens=1,* delims=\\", bat)
        with open(os.path.join(out, "PLAYERS.md")) as fh:
            self.assertIn("play.bat", fh.read())

    def test_mac_android_vita_packages(self):
        mac = self.put("build/ps2EntryRunner", b"\xcf\xfa\xed\xfe" + bytes(60))
        out = self.out("mac")
        self.assertEqual(cli("package", "--runner", mac, "--target", "macos", "--out", out)[0], 0)
        self.assertTrue(os.access(os.path.join(out, "play.command"), os.X_OK))
        apk = self.put("apk/app-release.apk", b"PK\x03\x04" + bytes(30))
        out = self.out("android")
        code, _, err = cli("package", "--runner", apk, "--target", "android", "--out", out,
                           "--elf-name", "SLUS_203.12")
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isfile(os.path.join(out, "install-android.sh")))
        self.assertTrue(os.path.isfile(os.path.join(out, "install-android.bat")))
        with open(os.path.join(out, "PLAYERS.md")) as fh:
            players = fh.read()
        self.assertIn("/storage/emulated/0/Android/data/com.ps2x.runner/files/SLUS_203.12", players)
        self.assertIn("mc0/` on the device", players)
        vpk = self.put("vpk/ps2EntryRunner.vpk", b"PK\x03\x04" + bytes(30))
        out = self.out("vita")
        self.assertEqual(cli("package", "--runner", vpk, "--target", "vita", "--out", out,
                             "--elf-name", "SLUS_203.12")[0], 0)
        with open(os.path.join(out, "PLAYERS.md")) as fh:
            self.assertIn("ux0:data/ps2x/SLUS_203.12", fh.read())

    def test_refuses_game_data(self):
        runner = self.put("build/ps2EntryRunner", elf_header(62, cls=2), executable=True)
        iso = self.put("stuff/extra.dat", fake_iso())
        out = self.out("refused")
        code, text, _ = cli("package", "--runner", runner, "--target", "linux", "--out", out, "--lib", iso)
        self.assertEqual(code, 1)
        self.assertIn("package REFUSED", text)
        self.assertIn("ISO 9660", text)
        self.assertFalse(os.path.exists(out))
        self.assertEqual(self.staging_left(), [])
        elf = self.put("stuff/SLUS_203.12", elf_header(8))
        self.assertEqual(cli("package", "--runner", runner, "--target", "linux", "--out", out, "--lib", elf)[0], 1)
        self.put("build/PADMAN.IRX", b"x")  # next to the runner, but not a library: never copied
        self.assertEqual(cli("package", "--runner", runner, "--target", "linux", "--out", out)[0], 0)

    def test_allow_file_lets_a_false_positive_through(self):
        runner = self.put("build/ps2EntryRunner", elf_header(62, cls=2), executable=True)
        own = self.put("mine/settings.bin", b"my own")
        allow = self.put("allow.txt", "settings.bin  # my own settings, made by me\n")
        out = self.out("allowed")
        code, text, _ = cli("package", "--runner", runner, "--target", "linux", "--out", out,
                            "--lib", own, "--allow", allow)
        self.assertEqual(code, 0, text)
        with open(os.path.join(out, targets.DIST_MARKER)) as fh:
            self.assertEqual(json.load(fh)["allowed"][0]["allowed_because"], "my own settings, made by me")

    def test_usage_errors(self):
        linux_runner = self.put("build/ps2EntryRunner", elf_header(62, cls=2), executable=True)
        mips = self.put("build/game.elf", elf_header(8))
        macho = self.put("build/mac/ps2EntryRunner", b"\xcf\xfa\xed\xfe" + bytes(60))
        out = self.out("e")
        cases = [
            ["--runner", os.path.join(self.tmp, "none"), "--target", "linux"],
            ["--runner", mips, "--target", "linux"],
            ["--runner", linux_runner, "--target", "windows"],
            ["--runner", macho, "--target", "linux"],
            ["--runner", linux_runner, "--target", "android"],
            ["--runner", linux_runner, "--target", "vita"],
            ["--runner", linux_runner, "--target", "linux", "--game", "X"],
            ["--runner", linux_runner, "--target", "linux", "--lib", os.path.join(self.tmp, "nope.so")],
        ]
        for argv in cases:
            code, _, err = cli("package", "--out", out, *argv)
            self.assertEqual(code, 2, argv)
            self.assertTrue(err.startswith("error: "), err)
        os.makedirs(out)
        self.put("console/dist/e/somebody-else.txt", b"x")
        code, _, err = cli("package", "--runner", linux_runner, "--target", "linux", "--out", out)
        self.assertEqual(code, 2)
        self.assertIn("not made by this tool", err)
        out2 = self.out("twice")
        self.assertEqual(cli("package", "--runner", linux_runner, "--target", "linux", "--out", out2)[0], 0)
        code, _, err = cli("package", "--runner", linux_runner, "--target", "linux", "--out", out2)
        self.assertEqual(code, 2)
        self.assertIn("--replace", err)
        self.assertEqual(cli("package", "--runner", linux_runner, "--target", "linux", "--out", out2,
                             "--replace", "--json")[0], 0)
        self.assertEqual(cli("package", "--runner", linux_runner, "--target", "web", "--out", out2)[0], 2)


class TestRecompJson(Tmp):
    def test_write_and_fields(self):
        path = os.path.join(self.tmp, ".recomp.json")
        code, out, err = cli("recomp-json", "--game", "My Game", "--serial", "SLUS_203.12", "--out", path)
        self.assertEqual(code, 0, err)
        with open(path) as fh:
            data = json.load(fh)
        self.assertEqual(list(data), ["$schema", "game", "system", "type", "toolchain", "status", "original"])
        self.assertEqual(data["$schema"], "https://recomp.fyi/schema/v1.json")
        self.assertEqual((data["system"], data["type"], data["toolchain"]), ("PS2", "recomp", "PS2Recomp"))
        self.assertEqual(data["status"], "in-progress")
        self.assertEqual(data["original"], {"region": "USA", "serial": "SLUS-20312"})

    def test_serials_and_regions(self):
        self.assertEqual(targets.normalize_serial("sces_500.51;1"), "SCES-50051")
        self.assertEqual(targets.normalize_serial("SLPM-65432"), "SLPM-65432")
        self.assertEqual(targets.build_recomp_json("G", "SCES-50051", "paused")["original"]["region"], "Europe")
        self.assertEqual(targets.build_recomp_json("G", "SLPS_250.01", "complete")["original"]["region"], "Japan")
        with self.assertRaises(targets.UsageError):
            targets.normalize_serial("game.iso")
        with self.assertRaises(targets.UsageError):
            targets.build_recomp_json("G", "ABCD-12345", "playable")
        self.assertEqual(targets.build_recomp_json("G", "ABCD-12345", "playable", region="USA")
                         ["original"]["region"], "USA")
        with self.assertRaises(targets.UsageError):
            targets.build_recomp_json("  ", "SLUS-20312", "playable")
        with self.assertRaises(targets.UsageError):
            targets.build_recomp_json("G", "SLUS-20312", "done")

    def test_update_keeps_other_fields(self):
        path = os.path.join(self.tmp, ".recomp.json")
        with open(path, "w") as fh:
            json.dump({"game": "Old", "status": "exploring", "links": {"repo": "https://github.com/me/port"},
                       "original": {"serial": "SLUS-20312", "region": "USA", "notes": "greatest hits"}}, fh)
        code, out, _ = cli("recomp-json", "--game", "My Game", "--serial", "SLUS-20312", "--status",
                           "playable", "--out", path)
        self.assertEqual(code, 0)
        self.assertIn("updated", out)
        with open(path) as fh:
            data = json.load(fh)
        self.assertEqual(data["status"], "playable")
        self.assertEqual(data["links"]["repo"], "https://github.com/me/port")
        self.assertEqual(data["original"]["notes"], "greatest hits")

    def test_refuses_links_to_game_files(self):
        path = os.path.join(self.tmp, ".recomp.json")
        with open(path, "w") as fh:
            json.dump({"links": {"download": "https://example.com/files/mygame.iso"}}, fh)
        code, _, err = cli("recomp-json", "--game", "G", "--serial", "SLUS-20312", "--out", path)
        self.assertEqual(code, 2)
        self.assertIn("link", err)
        with open(path, "w") as fh:
            json.dump({"links": ["magnet:?xt=urn:btih:abc"]}, fh)
        self.assertEqual(cli("recomp-json", "--game", "G", "--serial", "SLUS-20312", "--out", path)[0], 2)
        with open(path, "w") as fh:
            fh.write("{broken")
        self.assertEqual(cli("recomp-json", "--game", "G", "--serial", "SLUS-20312", "--out", path)[0], 2)
        self.assertEqual(cli("recomp-json", "--game", "G", "--serial", "SLUS-20312", "--status", "done",
                             "--out", path)[0], 2)


class TestDocs(unittest.TestCase):
    def test_skill_md_and_files(self):
        with open(os.path.join(SKILL, "SKILL.md")) as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: console-anywhere\n"))
        refs = re.findall(r"`\$\{CLAUDE_SKILL_DIR\}/([^`\s]+)", text) + re.findall(r"\(((?:reference|templates)/[^)]+)\)", text)
        self.assertTrue(refs)
        for rel in refs:
            self.assertTrue(os.path.exists(os.path.join(SKILL, rel)), rel)
        for folder, _, files in os.walk(SKILL):
            for name in files:
                with open(os.path.join(folder, name), encoding="utf-8") as fh:
                    self.assertNotIn("\u2014", fh.read(), name)
        self.assertEqual(cli()[0], 2)
        for sub in ("doctor", "plan", "package", "guard", "recomp-json"):
            self.assertEqual(cli(sub, "--help")[0], 0)


if __name__ == "__main__":
    unittest.main()
