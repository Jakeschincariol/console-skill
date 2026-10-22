"""Tests for console-controller/padmap.py (offline, synthesized fixtures only)."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from _load import load, ROOT

padmap = load("console-controller", "padmap")
SKILL = os.path.join(ROOT, "console-controller")
CXX = shutil.which("c++")


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = padmap.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def mock_raylib_h():
    """A minimal raylib.h with the names the pad layer uses (values follow raylib's order)."""
    lines = ["#pragma once", "typedef struct Vector2 { float x; float y; } Vector2;"]
    buttons = ["GAMEPAD_BUTTON_UNKNOWN = 0"] + ["GAMEPAD_BUTTON_%s = %d" % (b, i + 1)
                                                for i, b in enumerate(padmap.RAYLIB_BUTTONS)]
    lines.append("typedef enum { %s } GamepadButton;" % ", ".join(buttons))
    lines.append("typedef enum { GAMEPAD_AXIS_LEFT_X = 0, GAMEPAD_AXIS_LEFT_Y = 1, "
                 "GAMEPAD_AXIS_RIGHT_X = 2, GAMEPAD_AXIS_RIGHT_Y = 3, "
                 "GAMEPAD_AXIS_LEFT_TRIGGER = 4, GAMEPAD_AXIS_RIGHT_TRIGGER = 5 } GamepadAxis;")
    keys = ["KEY_NULL = 0"] + ["KEY_%s = %d" % (k, i + 1) for i, k in enumerate(padmap.RAYLIB_KEYS)]
    lines.append("typedef enum { %s } KeyboardKey;" % ", ".join(keys))
    lines += [
        "bool IsGamepadAvailable(int gamepad);",
        "bool IsGamepadButtonDown(int gamepad, int button);",
        "float GetGamepadAxisMovement(int gamepad, int axis);",
        "bool IsKeyDown(int key);",
        "int GetTouchPointCount(void);",
        "Vector2 GetTouchPosition(int index);",
        "int GetScreenWidth(void);",
        "int GetScreenHeight(void);",
    ]
    return "\n".join(lines) + "\n"


# The real ps2xRuntime/include/runtime/ps2_pad.h, verbatim in substance.
PAD_H = """#ifndef PS2_PAD_H
#define PS2_PAD_H

#include <cstddef>
#include <cstdint>

class PSPadBackend
{
public:
    PSPadBackend() = default;
    ~PSPadBackend() = default;

    bool readState(int port, int slot, uint8_t *data, size_t size);
};

#endif
"""

HOST_H = '#pragma once\n\n#include "raylib.h"\n'

# A tiny raylib stand-in driven by commands on stdin, printing the 8 report bytes.
HARNESS = r"""
#include "runtime/ps2_pad.h"
#include "raylib.h"
#include <cstdio>
#include <cstring>
#include <iostream>
#include <string>

static bool g_avail[4];
static bool g_btn[4][32];
static float g_axis[4][8];
static bool g_key[1024];
static int g_touchCount = 0;
static Vector2 g_touch[8];

bool IsGamepadAvailable(int p) { return p >= 0 && p < 4 && g_avail[p]; }
bool IsGamepadButtonDown(int p, int b) { return p >= 0 && p < 4 && b >= 0 && b < 32 && g_btn[p][b]; }
float GetGamepadAxisMovement(int p, int a) { return (p >= 0 && p < 4 && a >= 0 && a < 8) ? g_axis[p][a] : 0.0f; }
bool IsKeyDown(int k) { return k >= 0 && k < 1024 && g_key[k]; }
int GetTouchPointCount(void) { return g_touchCount; }
Vector2 GetTouchPosition(int i) { return (i >= 0 && i < 8) ? g_touch[i] : Vector2{0.0f, 0.0f}; }
int GetScreenWidth(void) { return 1000; }
int GetScreenHeight(void) { return 500; }

int main()
{
    PSPadBackend pad;
    std::string cmd;
    while (std::cin >> cmd)
    {
        if (cmd == "pad") { int p, v; std::cin >> p >> v; g_avail[p] = v != 0; }
        else if (cmd == "btn") { int p, b, v; std::cin >> p >> b >> v; g_btn[p][b] = v != 0; }
        else if (cmd == "axis") { int p, a; float v; std::cin >> p >> a >> v; g_axis[p][a] = v; }
        else if (cmd == "key") { int k, v; std::cin >> k >> v; g_key[k] = v != 0; }
        else if (cmd == "touch") { float x, y; std::cin >> x >> y; g_touch[g_touchCount++] = Vector2{x, y}; }
        else if (cmd == "notouch") { g_touchCount = 0; }
        else if (cmd == "read")
        {
            int port; std::cin >> port;
            uint8_t data[32];
            std::memset(data, 0xEE, sizeof(data));
            bool ok = pad.readState(port, 0, data, sizeof(data));
            std::printf("%d", ok ? 1 : 0);
            for (int i = 0; i < 8; ++i) std::printf(" %02X", data[i]);
            std::printf("\n");
        }
        else if (cmd == "short")
        {
            uint8_t data[16];
            std::printf("%d\n", pad.readState(0, 0, data, sizeof(data)) ? 1 : 0);
        }
    }
    return 0;
}
"""


def write_mocks(folder):
    os.makedirs(os.path.join(folder, "runtime"), exist_ok=True)
    with open(os.path.join(folder, "raylib.h"), "w") as fh:
        fh.write(mock_raylib_h())
    with open(os.path.join(folder, "ps2_host_backend.h"), "w") as fh:
        fh.write(HOST_H)
    with open(os.path.join(folder, "runtime", "ps2_pad.h"), "w") as fh:
        fh.write(PAD_H)


def btn(name):
    return padmap.RAYLIB_BUTTONS.index(name) + 1


def key(name):
    return padmap.RAYLIB_KEYS.index(name) + 1


class TestTablesAndPresets(unittest.TestCase):
    def test_bit_table_matches_runtime(self):
        expect = {"SELECT": 0x0001, "L3": 0x0002, "R3": 0x0004, "START": 0x0008, "UP": 0x0010,
                  "RIGHT": 0x0020, "DOWN": 0x0040, "LEFT": 0x0080, "L2": 0x0100, "R2": 0x0200,
                  "L1": 0x0400, "R1": 0x0800, "TRIANGLE": 0x1000, "CIRCLE": 0x2000,
                  "CROSS": 0x4000, "SQUARE": 0x8000}
        self.assertEqual(padmap.DS2_MASK, expect)
        self.assertEqual(sorted(padmap.DS2_ORDER), sorted(expect))

    def test_table_cli(self):
        code, out, _ = run_cli("table", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        cross = [r for r in data["buttons"] if r["button"] == "CROSS"][0]
        self.assertEqual(cross["mask"], "0x4000")
        self.assertEqual(cross["byte"], "data[3]")
        self.assertEqual(cross["bit"], 6)
        self.assertEqual(data["sticks"]["data[6]"], "LX")
        code, out, _ = run_cli("table")
        self.assertEqual(code, 0)
        self.assertIn("ACTIVE-LOW", out)

    def test_presets_cli(self):
        code, out, _ = run_cli("presets")
        self.assertEqual(code, 0)
        for name in ("playstation", "xbox", "switch", "switch-labels", "keyboard"):
            self.assertIn(name + ":", out)
        code, out, _ = run_cli("presets", "switch-labels", "--json")
        data = json.loads(out)
        self.assertEqual(data["switch-labels"]["buttons"]["CROSS"][0]["raylib"], "RIGHT_FACE_RIGHT")
        self.assertIn("A", data["switch-labels"]["buttons"]["CROSS"][0]["label"])

    def test_switch_position_vs_label(self):
        pos = padmap.PRESETS["switch"]["buttons"]
        lab = padmap.PRESETS["switch-labels"]["buttons"]
        self.assertEqual(pos["CROSS"], ["RIGHT_FACE_DOWN"])
        self.assertEqual(lab["CROSS"], ["RIGHT_FACE_RIGHT"])
        self.assertEqual(lab["CIRCLE"], ["RIGHT_FACE_DOWN"])
        self.assertEqual(lab["SQUARE"], ["RIGHT_FACE_UP"])
        self.assertEqual(lab["TRIANGLE"], ["RIGHT_FACE_LEFT"])

    def test_every_preset_is_valid(self):
        for name in padmap.LAYOUTS:
            res, errors, warnings = padmap.resolve({"layout": name})
            e2, _ = padmap.validate(res)
            self.assertEqual(errors + e2, [], name)

    def test_default_keyboard_avoids_escape_and_f1(self):
        keys = [k for v in padmap.KEYBOARD_DEFAULT.values() for k in v]
        self.assertNotIn("ESCAPE", keys)
        self.assertNotIn("F1", keys)
        self.assertEqual(len(keys), len(set(keys)))


class TestResolveAndValidate(unittest.TestCase):
    def check(self, config):
        res, errors, warnings = padmap.resolve(config)
        e2, w2 = padmap.validate(res)
        return res, errors + e2, warnings + w2

    def test_override_by_name_and_prefix(self):
        res, errors, _ = self.check({"buttons": {"cross": "GAMEPAD_BUTTON_RIGHT_FACE_RIGHT",
                                                 "CIRCLE": "RIGHT_FACE_DOWN"}})
        self.assertEqual(errors, [])
        self.assertEqual(res["buttons"]["CROSS"], ["RIGHT_FACE_RIGHT"])

    def test_duplicate_gamepad_binding(self):
        _, errors, _ = self.check({"buttons": {"CROSS": "RIGHT_FACE_RIGHT"}})
        self.assertTrue(any("RIGHT_FACE_RIGHT" in e and "CIRCLE" in e for e in errors), errors)
        _, errors, _ = self.check({"buttons": {"CROSS": "RIGHT_FACE_RIGHT"},
                                   "allow_duplicates": ["RIGHT_FACE_RIGHT"]})
        self.assertEqual(errors, [])

    def test_unbound_button(self):
        _, errors, _ = self.check({"buttons": {"L3": []}})
        self.assertTrue(any(e.startswith("L3 is not bound") for e in errors), errors)
        _, errors, _ = self.check({"buttons": {"L3": None}})
        self.assertTrue(errors)

    def test_duplicate_key(self):
        _, errors, _ = self.check({"keyboard": {"CROSS": ["Z"]}})
        self.assertTrue(any("key Z" in e for e in errors), errors)
        _, errors, _ = self.check({"keyboard": {"CROSS": ["Z"]}, "allow_duplicates": ["KEY_Z"]})
        self.assertEqual(errors, [])

    def test_unknown_names(self):
        _, errors, warnings = self.check({"buttons": {"HOME": "MIDDLE", "CROSS": "BIG_RED"},
                                          "keyboard": {"START": "NOPE", "LSTICK_SIDEWAYS": "A"},
                                          "layout": "gamecube", "colour": "red"})
        joined = " | ".join(errors)
        self.assertIn("HOME", joined)
        self.assertIn("BIG_RED", joined)
        self.assertIn("NOPE", joined)
        self.assertIn("LSTICK_SIDEWAYS", joined)
        self.assertIn("gamecube", joined)
        self.assertTrue(any("colour" in w for w in warnings))

    def test_ranges(self):
        for bad in ({"deadzone": 1.5}, {"deadzone": -0.1}, {"deadzone": True},
                    {"deadzone": {"left": 0.2, "right": "x"}}, {"invert_y": "up"},
                    {"invert_y": 1}, {"invert_y": [True]}, {"players": 3}, {"players": True},
                    {"gamepad": 7}, {"gamepad": "second"}, {"trigger_threshold": 0},
                    {"trigger_threshold": 2}, {"stick_swap": "yes"}, {"keyboard_always": 1},
                    {"buttons": []}, {"keyboard": "WASD"}, {"touch": {}}):
            _, errors, _ = self.check(bad)
            self.assertTrue(errors, bad)

    def test_deadzone_per_stick_and_invert_forms(self):
        res, errors, _ = self.check({"deadzone": {"left": 0.1, "right": 0.25}, "invert_y": "both"})
        self.assertEqual(errors, [])
        self.assertEqual((res["deadzone_left"], res["deadzone_right"]), (0.1, 0.25))
        self.assertEqual((res["invert_left"], res["invert_right"]), (True, True))
        res, _, _ = self.check({"invert_y": True})
        self.assertEqual((res["invert_left"], res["invert_right"]), (False, True))
        res, _, _ = self.check({"invert_y": "left"})
        self.assertEqual((res["invert_left"], res["invert_right"]), (True, False))

    def test_trigger_threshold_moves_l2_r2_to_axes(self):
        res, errors, _ = self.check({"trigger_threshold": 0.3})
        self.assertEqual(errors, [])
        self.assertEqual(res["buttons"]["L2"], ["LEFT_TRIGGER_AXIS"])
        self.assertEqual(res["buttons"]["R2"], ["RIGHT_TRIGGER_AXIS"])

    def test_escape_and_f1_warn(self):
        _, errors, warnings = self.check({"keyboard": {"CIRCLE": ["C", "ESCAPE"], "SELECT": ["F1"]}})
        self.assertEqual(errors, [])
        self.assertTrue(any("ESCAPE" in w for w in warnings))
        self.assertTrue(any("F1" in w for w in warnings))

    def test_keyboard_layout_needs_every_key(self):
        _, errors, _ = self.check({"layout": "keyboard", "keyboard": {"START": []}})
        self.assertTrue(any("START has no keyboard key" in e for e in errors), errors)
        _, errors, warnings = self.check({"layout": "xbox", "keyboard": {"START": []}})
        self.assertEqual(errors, [])
        self.assertTrue(any("START has no keyboard key" in w for w in warnings))

    def test_players_two_needs_pads(self):
        _, errors, _ = self.check({"layout": "keyboard", "players": 2})
        self.assertTrue(any("players 2" in e for e in errors))

    def test_touch_zones(self):
        res, errors, _ = self.check({"touch": [
            {"button": "cross", "x": 0.9, "y": 0.8, "r": 0.06},
            {"stick": "LEFT", "x": 0.15, "y": 0.75, "r": 0.12}]})
        self.assertEqual(errors, [])
        self.assertEqual(res["touch_buttons"][0][0], "CROSS")
        self.assertEqual(res["touch_sticks"][0][0], "left")
        for bad in ({"button": "CROSS", "x": 1.2, "y": 0.5, "r": 0.1},
                    {"button": "CROSS", "x": 0.5, "y": 0.5, "r": 0.0},
                    {"button": "CROSS", "x": 0.5, "y": 0.5, "r": 0.9},
                    {"button": "HOME", "x": 0.5, "y": 0.5, "r": 0.1},
                    {"stick": "middle", "x": 0.5, "y": 0.5, "r": 0.1},
                    {"button": "CROSS", "stick": "left", "x": 0.5, "y": 0.5, "r": 0.1},
                    {"x": 0.5, "y": 0.5, "r": 0.1}, {"button": "CROSS", "x": "a", "y": 0, "r": 0.1},
                    "zone"):
            _, errors, _ = self.check({"touch": [bad]})
            self.assertTrue(errors, bad)


class TestScripts(unittest.TestCase):
    def test_parse_forms(self):
        text = ("# walk to the menu\n"
                "0 -\n"
                "120 START\n"
                "125 none 128 128 128 128   # release\n"
                "300 cross+UP 128 0 128 128\n"
                "300 0x4000 255 128 128 128\n"
                "\n")
        lines = padmap.parse_script(text)
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[1], (120, 0x0008, 128, 128, 128, 128))
        self.assertEqual(lines[3], (300, 0x4000 | 0x0010, 128, 0, 128, 128))
        self.assertEqual(lines[4][1], 0x4000)

    def test_parse_errors(self):
        for bad, needle in (("5 START\n4 -\n", "comes before"), ("x START\n", "whole number"),
                            ("1 JUMP\n", "JUMP"), ("1 START 1 2\n", "expected"),
                            ("1 START 0 0 0 300\n", "outside"), ("1 0x1FFFF\n", "above"),
                            ("1 0xZZ\n", "hex"), ("-1 START\n", "out of range"),
                            ("1 START 0 0 0 q\n", "not a number")):
            with self.assertRaises(ValueError) as ctx:
                padmap.parse_script(bad)
            self.assertIn(needle, str(ctx.exception), bad)

    def test_pressed_names_roundtrip(self):
        mask = padmap.parse_button_token("CROSS+L1+START")
        self.assertEqual(padmap.parse_button_token(padmap.pressed_names(mask)), mask)
        self.assertEqual(padmap.pressed_names(0), "-")

    def test_template_script_is_valid(self):
        path = os.path.join(SKILL, "templates", "menu-walk.txt")
        with open(path) as fh:
            lines = padmap.parse_script(fh.read())
        self.assertGreater(len(lines), 3)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def test_init_check_gen(self):
        cfg = self.path("console", "controls.json")
        code, out, _ = run_cli("init", "--layout", "xbox", "--out", cfg)
        self.assertEqual(code, 0)
        with open(cfg) as fh:
            self.assertEqual(json.load(fh)["layout"], "xbox")
        code, _, err = run_cli("init", "--out", cfg)
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        code, out, _ = run_cli("check", "--config", cfg)
        self.assertEqual(code, 0, out)
        out_cpp = self.path("console", "ps2_pad.cpp")
        code, out, _ = run_cli("gen", "--config", cfg, "--out", out_cpp, "--replay")
        self.assertEqual(code, 0)
        with open(out_cpp) as fh:
            text = fh.read()
        self.assertIn(padmap.MARKER, text)
        self.assertIn("PS2X_INPUT_SCRIPT", text)
        self.assertIn("PS2X_INPUT_RECORD", text)

    def test_generated_file_is_a_drop_in(self):
        res, _, _ = padmap.resolve({})
        text = padmap.generate_cpp(res)
        self.assertIn('#include "runtime/ps2_pad.h"', text)
        self.assertIn('#include "ps2_host_backend.h"', text)
        self.assertIn("#include <cstring>", text)
        self.assertIn("bool PSPadBackend::readState(int port, int slot, uint8_t *data, size_t size)", text)
        self.assertIn("if (!data || size < 32)", text)
        self.assertIn("data[1] = kPadAnalogMarker;", text)
        self.assertIn("constexpr uint8_t kPadAnalogMarker = 0x73;", text)
        self.assertIn("data[6] = report.lx;", text)
        self.assertIn("data[4] = report.rx;", text)
        self.assertNotIn("PS2X_INPUT_SCRIPT", text)
        self.assertNotIn("GetTouchPosition", text)
        self.assertNotIn("\u2014", text)

    def test_check_failures_and_bad_input(self):
        cfg = self.path("bad.json")
        with open(cfg, "w") as fh:
            json.dump({"keyboard": {"CROSS": ["Z"]}}, fh)
        code, out, _ = run_cli("check", "--config", cfg)
        self.assertEqual(code, 1)
        self.assertIn("ERROR", out)
        code, out, _ = run_cli("check", "--config", cfg, "--json")
        self.assertFalse(json.loads(out)["ok"])
        code, _, err = run_cli("gen", "--config", cfg, "--out", self.path("x.cpp"))
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(self.path("x.cpp")))
        with open(cfg, "w") as fh:
            fh.write("{not json")
        code, _, err = run_cli("check", "--config", cfg)
        self.assertEqual(code, 2)
        self.assertIn("not valid JSON", err)
        with open(cfg, "w") as fh:
            fh.write("[1, 2]")
        self.assertEqual(run_cli("check", "--config", cfg)[0], 2)
        code, _, err = run_cli("check", "--config", self.path("missing.json"))
        self.assertEqual(code, 2)
        self.assertEqual(run_cli()[0], 2)

    def test_check_strict_and_script(self):
        cfg = self.path("warn.json")
        with open(cfg, "w") as fh:
            json.dump({"keyboard": {"CIRCLE": ["C", "ESCAPE"]}}, fh)
        self.assertEqual(run_cli("check", "--config", cfg)[0], 0)
        self.assertEqual(run_cli("check", "--config", cfg, "--strict")[0], 1)
        script = self.path("s.txt")
        with open(script, "w") as fh:
            fh.write("10 START\n5 -\n")
        code, out, _ = run_cli("check", "--config", cfg, "--script", script)
        self.assertEqual(code, 1)
        self.assertIn("comes before", out)
        self.assertEqual(run_cli("check", "--script", self.path("nope.txt"))[0], 2)

    def test_gen_without_config_uses_preset(self):
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            code, out, _ = run_cli("gen", "--layout", "switch", "--out", "-")
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0)
        self.assertIn("Layout preset: switch", out)

    def make_repo(self):
        repo = self.path("PS2Recomp")
        lib = os.path.join(repo, "ps2xRuntime", "src", "lib")
        os.makedirs(lib)
        with open(os.path.join(lib, "ps2_pad.cpp"), "w") as fh:
            fh.write("// stock\nbool PSPadBackend::readState(int, int, uint8_t *, size_t) { return false; }\n")
        return repo, os.path.join(lib, "ps2_pad.cpp")

    def test_install_and_restore(self):
        repo, target = self.make_repo()
        gen = self.path("console", "ps2_pad.cpp")
        backups = self.path("console", "backups")
        self.assertEqual(run_cli("gen", "--out", gen, "--config", self.path("none.json"),
                                 "--layout", "xbox")[0], 2)  # explicit missing config
        self.assertEqual(run_cli("gen", "--out", gen, "--layout", "xbox")[0], 0)
        code, out, _ = run_cli("install", "--repo", repo, "--from", gen, "--backups", backups)
        self.assertEqual(code, 0, out)
        with open(target) as fh:
            self.assertIn(padmap.MARKER, fh.read())
        with open(os.path.join(backups, "ps2_pad.cpp.orig")) as fh:
            self.assertIn("// stock", fh.read())
        # A second install keeps the .orig and writes a timestamped backup.
        code, out, _ = run_cli("install", "--repo", repo, "--from", gen, "--backups", backups)
        self.assertEqual(code, 0)
        with open(os.path.join(backups, "ps2_pad.cpp.orig")) as fh:
            self.assertIn("// stock", fh.read())
        code, out, _ = run_cli("restore", "--repo", repo, "--backups", backups)
        self.assertEqual(code, 0)
        with open(target) as fh:
            self.assertIn("// stock", fh.read())

    def test_install_refusals(self):
        repo, target = self.make_repo()
        foreign = self.path("foreign.cpp")
        with open(foreign, "w") as fh:
            fh.write("bool PSPadBackend::readState(int, int, uint8_t *, size_t) { return true; }\n")
        code, _, err = run_cli("install", "--repo", repo, "--from", foreign,
                               "--backups", self.path("b"))
        self.assertEqual(code, 2)
        self.assertIn("not written by padmap.py", err)
        code, _, err = run_cli("install", "--repo", self.path("nowhere"), "--from", foreign)
        self.assertEqual(code, 2)
        self.assertIn("not a PS2Recomp checkout", err)
        self.assertEqual(run_cli("install", "--repo", repo, "--from", self.path("missing.cpp"))[0], 2)
        self.assertEqual(run_cli("restore", "--repo", repo, "--backups", self.path("empty"))[0], 2)


@unittest.skipUnless(CXX, "no c++ compiler on PATH")
class TestCompile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.inc = os.path.join(cls.tmp, "include")
        write_mocks(cls.inc)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def syntax(self, config, replay, defines=()):
        res, errors, _ = padmap.resolve(config)
        self.assertEqual(errors, [])
        src = os.path.join(self.tmp, "pad_%d.cpp" % abs(hash((json.dumps(config, sort_keys=True), replay))))
        with open(src, "w") as fh:
            fh.write(padmap.generate_cpp(res, replay=replay))
        cmd = [CXX, "-std=c++20", "-fsyntax-only", "-I", self.inc] + ["-D" + d for d in defines] + [src]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode(errors="replace"))

    def test_plain_compiles(self):
        self.syntax({}, False)

    def test_everything_compiles(self):
        config = {"layout": "switch-labels", "trigger_threshold": 0.4, "players": 2,
                  "stick_swap": True, "invert_y": "both", "gamepad": 1,
                  "touch": [{"button": "CROSS", "x": 0.9, "y": 0.8, "r": 0.06},
                            {"stick": "right", "x": 0.8, "y": 0.4, "r": 0.1}]}
        self.syntax(config, True, defines=("PS2X_PAD_TOUCH",))

    def test_stock_file_shape_compiles_with_same_mocks(self):
        # The mocks must be faithful enough for the stock-style calls too.
        stock = ('#include "runtime/ps2_pad.h"\n#include "ps2_host_backend.h"\n#include <cstring>\n'
                 "bool PSPadBackend::readState(int, int, uint8_t *data, size_t size)\n{\n"
                 "    if (!data || size < 32) return false;\n"
                 "    if (IsGamepadAvailable(0) && IsGamepadButtonDown(0, GAMEPAD_BUTTON_RIGHT_FACE_DOWN)) data[2] = 0;\n"
                 "    data[6] = static_cast<uint8_t>(128 + GetGamepadAxisMovement(0, GAMEPAD_AXIS_LEFT_X) * 127);\n"
                 "    if (IsKeyDown(KEY_X) || IsKeyDown(KEY_KP_0)) data[3] = 0;\n    return true;\n}\n")
        src = os.path.join(self.tmp, "stock.cpp")
        with open(src, "w") as fh:
            fh.write(stock)
        proc = subprocess.run([CXX, "-std=c++20", "-fsyntax-only", "-I", self.inc, src],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode(errors="replace"))


@unittest.skipUnless(CXX, "no c++ compiler on PATH")
class TestBehaviour(unittest.TestCase):
    """Builds the generated pad with a scriptable raylib stand-in and checks the report bytes."""

    @classmethod
    def build(cls, name, config, replay, defines=()):
        res, errors, _ = padmap.resolve(config)
        assert not errors, errors
        src = os.path.join(cls.tmp, name + ".cpp")
        with open(src, "w") as fh:
            fh.write(padmap.generate_cpp(res, replay=replay))
        exe = os.path.join(cls.tmp, name)
        cmd = [CXX, "-std=c++20", "-O0", "-I", cls.inc] + ["-D" + d for d in defines] + [
            src, cls.harness, "-o", exe]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if proc.returncode != 0:
            raise AssertionError(proc.stdout.decode(errors="replace"))
        return exe

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.inc = os.path.join(cls.tmp, "include")
        write_mocks(cls.inc)
        cls.harness = os.path.join(cls.tmp, "harness.cpp")
        with open(cls.harness, "w") as fh:
            fh.write(HARNESS)
        cls.default_exe = cls.build("pad_default", {}, True)
        cls.custom_exe = cls.build("pad_custom", {
            "players": 2, "invert_y": True, "stick_swap": True, "trigger_threshold": 0.4,
            "deadzone": 0.0, "keyboard_always": False,
            "touch": [{"button": "CROSS", "x": 0.9, "y": 0.8, "r": 0.06},
                      {"stick": "left", "x": 0.2, "y": 0.5, "r": 0.1}]},
            False, defines=("PS2X_PAD_TOUCH",))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_pad(self, exe, commands, env_extra=None):
        env = dict(os.environ)
        env.pop("PS2X_INPUT_SCRIPT", None)
        env.pop("PS2X_INPUT_RECORD", None)
        env.update(env_extra or {})
        proc = subprocess.run([exe], input="\n".join(commands).encode(), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = [line.split() for line in proc.stdout.decode().splitlines()]
        return rows, proc.stderr.decode()

    def test_idle_report(self):
        rows, _ = self.run_pad(self.default_exe, ["read 0", "read 1", "short"])
        self.assertEqual(rows[0], ["1", "01", "73", "FF", "FF", "80", "80", "80", "80"])
        self.assertEqual(rows[1], ["1", "01", "73", "FF", "FF", "80", "80", "80", "80"])
        self.assertEqual(rows[2], ["0"])  # buffers under 32 bytes are refused, like the stock file

    def test_buttons_are_active_low(self):
        rows, _ = self.run_pad(self.default_exe, [
            "pad 0 1", "btn 0 %d 1" % btn("RIGHT_FACE_DOWN"), "read 0",
            "btn 0 %d 0" % btn("RIGHT_FACE_DOWN"), "btn 0 %d 1" % btn("LEFT_FACE_UP"),
            "btn 0 %d 1" % btn("MIDDLE_RIGHT"), "read 0"])
        self.assertEqual(rows[0][3:5], ["FF", "BF"])   # CROSS 0x4000 cleared
        self.assertEqual(rows[1][3:5], ["E7", "FF"])   # UP 0x10 and START 0x08 cleared

    def test_sticks_deadzone_and_bytes(self):
        rows, _ = self.run_pad(self.default_exe, [
            "pad 0 1", "axis 0 0 1.0", "read 0", "axis 0 0 0.0", "axis 0 1 -1.0", "read 0",
            "axis 0 0 0.1", "axis 0 1 0.0", "read 0",
            "axis 0 0 0.5", "axis 0 1 0.5", "read 0",
            "axis 0 0 0", "axis 0 2 -1.0", "read 0"])
        self.assertEqual(rows[0][7:9], ["FF", "80"])   # LX full right in data[6]
        self.assertEqual(rows[1][7:9], ["80", "00"])   # LY full up in data[7]
        self.assertEqual(rows[2][7:9], ["80", "80"])   # inside the 0.15 radial deadzone
        self.assertEqual(rows[3][7:9], ["BB", "BB"])   # 0.707 radius rescaled: 187
        self.assertEqual(rows[4][5], "00")             # RX full left in data[4]

    def test_keyboard_alongside_pad(self):
        rows, _ = self.run_pad(self.default_exe, [
            "key %d 1" % key("X"), "key %d 1" % key("W"), "read 0", "read 1",
            "pad 0 1", "read 0"])
        self.assertEqual(rows[0][3:5], ["FF", "BF"])
        self.assertEqual(rows[0][8], "00")             # W pushes LY up
        self.assertEqual(rows[1][3:], ["FF", "FF", "80", "80", "80", "80"])  # port 1 untouched
        self.assertEqual(rows[2][3:5], ["FF", "BF"])   # keyboard_always: still works with a pad

    def test_first_available_pad(self):
        rows, _ = self.run_pad(self.default_exe, [
            "pad 2 1", "btn 2 %d 1" % btn("RIGHT_FACE_UP"), "read 0"])
        self.assertEqual(rows[0][4], "EF")             # TRIANGLE 0x1000 from pad index 2

    def test_custom_mapping(self):
        rows, _ = self.run_pad(self.custom_exe, [
            "pad 0 1", "pad 1 1",
            "btn 1 %d 1" % btn("RIGHT_FACE_RIGHT"), "read 1",            # player 2's CIRCLE
            "axis 0 4 -1.0", "read 0",                                   # trigger at rest
            "axis 0 4 0.0", "read 0",                                    # half way = 0.5 >= 0.4
            "axis 0 4 -1.0", "axis 0 3 -1.0", "read 0",                  # right Y up, swapped + inverted
            "key %d 1" % key("X"), "read 0",                             # keyboard off with a pad
            "touch 900 400", "read 0", "notouch",
            "touch 230 250", "read 0"])
        self.assertEqual(rows[0][3:5], ["FF", "DF"])
        self.assertEqual(rows[1][3:5], ["FF", "FF"])
        self.assertEqual(rows[2][3:5], ["FF", "FE"])   # L2 0x0100 via the axis
        self.assertEqual(rows[3][7:9], ["80", "00"])   # swapped onto the left stick, not inverted
        self.assertEqual(rows[4][3:5], ["FF", "FF"])
        self.assertEqual(rows[5][3:5], ["FF", "BF"])   # touch zone at (0.9, 0.8) is CROSS
        self.assertEqual(rows[6][7], "CC")             # touch stick 0.6 right: round(1.6*127.5) = 204

    def test_replay_script(self):
        script = os.path.join(self.tmp, "walk.txt")
        with open(script, "w") as fh:
            fh.write("# test\n0 -\n2 START\n3 CROSS 255 128 128 0\n5 -\n")
        rows, err = self.run_pad(self.default_exe,
                                 ["pad 0 1", "btn 0 %d 1" % btn("RIGHT_FACE_UP")] + ["read 0"] * 6
                                 + ["read 1"],
                                 {"PS2X_INPUT_SCRIPT": script})
        self.assertIn("[pad-replay] loaded 4 lines from", err)
        self.assertIn("[pad-replay] script finished at frame 5", err)
        self.assertEqual(rows[0][3:5], ["FF", "FF"])   # live TRIANGLE ignored while replaying
        self.assertEqual(rows[2][3:5], ["F7", "FF"])   # frame 2: START
        self.assertEqual(rows[3][3:], ["FF", "BF", "80", "00", "FF", "80"])
        self.assertEqual(rows[5][3:5], ["FF", "FF"])
        self.assertEqual(rows[6][3:5], ["FF", "FF"])   # port 1 is not scripted

    def test_bad_script_disables_replay(self):
        script = os.path.join(self.tmp, "bad.txt")
        with open(script, "w") as fh:
            fh.write("5 START\n1 -\n")
        rows, err = self.run_pad(self.default_exe,
                                 ["pad 0 1", "btn 0 %d 1" % btn("RIGHT_FACE_UP"), "read 0"],
                                 {"PS2X_INPUT_SCRIPT": script})
        self.assertIn("replay disabled", err)
        self.assertEqual(rows[0][4], "EF")             # falls back to live input

    def test_record_then_replay_roundtrip(self):
        record = os.path.join(self.tmp, "rec.txt")
        rows, err = self.run_pad(self.default_exe, [
            "pad 0 1", "read 0", "btn 0 %d 1" % btn("RIGHT_FACE_DOWN"), "read 0", "read 0",
            "btn 0 %d 0" % btn("RIGHT_FACE_DOWN"), "axis 0 0 1.0", "read 0"],
            {"PS2X_INPUT_RECORD": record})
        self.assertIn("[pad-record] writing", err)
        with open(record) as fh:
            text = fh.read()
        lines = padmap.parse_script(text)
        self.assertEqual([l[0] for l in lines], [0, 1, 3])
        self.assertEqual(lines[1][1], 0x4000)
        self.assertEqual(lines[2][2], 255)
        replayed, _ = self.run_pad(self.default_exe, ["read 0"] * 4, {"PS2X_INPUT_SCRIPT": record})
        self.assertEqual([r[1:] for r in replayed], [r[1:] for r in rows])


class TestDocs(unittest.TestCase):
    def test_skill_md_and_files(self):
        import re
        with open(os.path.join(SKILL, "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: console-controller\n"))
        refs = set(re.findall(r"\$\{CLAUDE_SKILL_DIR\}/([A-Za-z0-9_./-]+)", text))
        refs |= set(re.findall(r"(?<![\w/])((?:reference|templates)/[A-Za-z0-9_.-]+)", text))
        self.assertIn("padmap.py", refs)
        for rel in refs:
            self.assertTrue(os.path.exists(os.path.join(SKILL, rel)), rel)
        for folder, _, files in os.walk(SKILL):
            for name in files:
                with open(os.path.join(folder, name), encoding="utf-8") as fh:
                    self.assertNotIn("\u2014", fh.read(), name)
        for sub in ("init", "check", "gen", "install", "restore", "presets", "table"):
            with self.assertRaises(SystemExit) as ctx:
                with contextlib.redirect_stdout(io.StringIO()):
                    padmap.main([sub, "--help"])
            self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
