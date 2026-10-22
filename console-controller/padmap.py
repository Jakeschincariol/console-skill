#!/usr/bin/env python3
"""padmap.py: controller mapping for a PS2Recomp port (console-controller).

Reads a JSON mapping (console/controls.json: a layout preset plus overrides) and
writes a drop-in replacement for ps2xRuntime/src/lib/ps2_pad.cpp: the same
PSPadBackend::readState(int port, int slot, uint8_t *data, size_t size), the same
32-byte DualShock 2 report (buttons active-low in data[2..3], sticks RX RY LX LY
in data[4..7], 0x80 centre), raylib calls only. Optional input replay for
scripted test runs (env PS2X_INPUT_SCRIPT) and recording (env PS2X_INPUT_RECORD).

Python 3.8+ standard library only.
"""

import argparse
import datetime
import json
import os
import shutil
import sys

# ---------------------------------------------------------------- DS2 facts

# DualShock 2 button bits as the runtime uses them (ps2_pad.cpp, Kernel/Stubs/Pad.cpp).
# The report stores them ACTIVE-LOW: data[2] = low byte, data[3] = high byte, 0xFFFF = none.
DS2_BUTTONS = [
    ("SELECT", 0x0001), ("L3", 0x0002), ("R3", 0x0004), ("START", 0x0008),
    ("UP", 0x0010), ("RIGHT", 0x0020), ("DOWN", 0x0040), ("LEFT", 0x0080),
    ("L2", 0x0100), ("R2", 0x0200), ("L1", 0x0400), ("R1", 0x0800),
    ("TRIANGLE", 0x1000), ("CIRCLE", 0x2000), ("CROSS", 0x4000), ("SQUARE", 0x8000),
]
DS2_MASK = dict(DS2_BUTTONS)
DS2_NAMES = [n for n, _ in DS2_BUTTONS]
# Order used in tables and generated code: the way people read a pad.
DS2_ORDER = ["UP", "DOWN", "LEFT", "RIGHT", "CROSS", "CIRCLE", "SQUARE", "TRIANGLE",
             "L1", "R1", "L2", "R2", "L3", "R3", "START", "SELECT"]

STICK_DIRS = {
    "LSTICK_UP": (0, 1, -1.0), "LSTICK_DOWN": (0, 1, 1.0),
    "LSTICK_LEFT": (0, 0, -1.0), "LSTICK_RIGHT": (0, 0, 1.0),
    "RSTICK_UP": (1, 1, -1.0), "RSTICK_DOWN": (1, 1, 1.0),
    "RSTICK_LEFT": (1, 0, -1.0), "RSTICK_RIGHT": (1, 0, 1.0),
}

# raylib 5.5 GamepadButton names (without the GAMEPAD_BUTTON_ prefix). Positional:
# RIGHT_FACE_DOWN is the bottom face button on every pad (Cross, Xbox A, Nintendo B).
RAYLIB_BUTTONS = [
    "LEFT_FACE_UP", "LEFT_FACE_RIGHT", "LEFT_FACE_DOWN", "LEFT_FACE_LEFT",
    "RIGHT_FACE_UP", "RIGHT_FACE_RIGHT", "RIGHT_FACE_DOWN", "RIGHT_FACE_LEFT",
    "LEFT_TRIGGER_1", "LEFT_TRIGGER_2", "RIGHT_TRIGGER_1", "RIGHT_TRIGGER_2",
    "MIDDLE_LEFT", "MIDDLE", "MIDDLE_RIGHT", "LEFT_THUMB", "RIGHT_THUMB",
]
# Analog trigger axes usable as buttons (pressed at trigger_threshold).
TRIGGER_AXES = {"LEFT_TRIGGER_AXIS": "GAMEPAD_AXIS_LEFT_TRIGGER",
                "RIGHT_TRIGGER_AXIS": "GAMEPAD_AXIS_RIGHT_TRIGGER"}
GAMEPAD_SOURCES = RAYLIB_BUTTONS + list(TRIGGER_AXES)

# raylib 5.5 KeyboardKey names (without the KEY_ prefix).
RAYLIB_KEYS = (
    ["APOSTROPHE", "COMMA", "MINUS", "PERIOD", "SLASH", "ZERO", "ONE", "TWO", "THREE",
     "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE", "SEMICOLON", "EQUAL"]
    + [chr(c) for c in range(ord("A"), ord("Z") + 1)]
    + ["LEFT_BRACKET", "BACKSLASH", "RIGHT_BRACKET", "GRAVE", "SPACE", "ESCAPE", "ENTER",
       "TAB", "BACKSPACE", "INSERT", "DELETE", "RIGHT", "LEFT", "DOWN", "UP", "PAGE_UP",
       "PAGE_DOWN", "HOME", "END", "CAPS_LOCK", "SCROLL_LOCK", "NUM_LOCK", "PRINT_SCREEN",
       "PAUSE"]
    + ["F%d" % i for i in range(1, 13)]
    + ["LEFT_SHIFT", "LEFT_CONTROL", "LEFT_ALT", "LEFT_SUPER", "RIGHT_SHIFT",
       "RIGHT_CONTROL", "RIGHT_ALT", "RIGHT_SUPER", "KB_MENU"]
    + ["KP_%d" % i for i in range(10)]
    + ["KP_DECIMAL", "KP_DIVIDE", "KP_MULTIPLY", "KP_SUBTRACT", "KP_ADD", "KP_ENTER",
       "KP_EQUAL", "BACK", "MENU", "VOLUME_UP", "VOLUME_DOWN"]
)
RAYLIB_KEY_SET = set(RAYLIB_KEYS)

POSITIONAL = {
    "UP": ["LEFT_FACE_UP"], "DOWN": ["LEFT_FACE_DOWN"],
    "LEFT": ["LEFT_FACE_LEFT"], "RIGHT": ["LEFT_FACE_RIGHT"],
    "CROSS": ["RIGHT_FACE_DOWN"], "CIRCLE": ["RIGHT_FACE_RIGHT"],
    "SQUARE": ["RIGHT_FACE_LEFT"], "TRIANGLE": ["RIGHT_FACE_UP"],
    "L1": ["LEFT_TRIGGER_1"], "R1": ["RIGHT_TRIGGER_1"],
    "L2": ["LEFT_TRIGGER_2"], "R2": ["RIGHT_TRIGGER_2"],
    "L3": ["LEFT_THUMB"], "R3": ["RIGHT_THUMB"],
    "START": ["MIDDLE_RIGHT"], "SELECT": ["MIDDLE_LEFT"],
}

KEYBOARD_DEFAULT = {
    "UP": ["UP"], "DOWN": ["DOWN"], "LEFT": ["LEFT"], "RIGHT": ["RIGHT"],
    "CROSS": ["X", "SPACE"], "CIRCLE": ["C"], "SQUARE": ["Z"], "TRIANGLE": ["V"],
    "L1": ["Q"], "R1": ["E"], "L2": ["ONE"], "R2": ["THREE"],
    "L3": ["LEFT_SHIFT"], "R3": ["F"], "START": ["ENTER"], "SELECT": ["TAB"],
    "LSTICK_UP": ["W"], "LSTICK_DOWN": ["S"], "LSTICK_LEFT": ["A"], "LSTICK_RIGHT": ["D"],
    "RSTICK_UP": ["I"], "RSTICK_DOWN": ["K"], "RSTICK_LEFT": ["J"], "RSTICK_RIGHT": ["L"],
}

_PS_LABELS = {
    "LEFT_FACE_UP": "D-pad up", "LEFT_FACE_DOWN": "D-pad down",
    "LEFT_FACE_LEFT": "D-pad left", "LEFT_FACE_RIGHT": "D-pad right",
    "RIGHT_FACE_DOWN": "Cross", "RIGHT_FACE_RIGHT": "Circle",
    "RIGHT_FACE_LEFT": "Square", "RIGHT_FACE_UP": "Triangle",
    "LEFT_TRIGGER_1": "L1", "RIGHT_TRIGGER_1": "R1",
    "LEFT_TRIGGER_2": "L2", "RIGHT_TRIGGER_2": "R2",
    "LEFT_THUMB": "L3 (left stick click)", "RIGHT_THUMB": "R3 (right stick click)",
    "MIDDLE_LEFT": "Create / Share / Select", "MIDDLE_RIGHT": "Options / Start",
    "MIDDLE": "PS button", "LEFT_TRIGGER_AXIS": "L2 (analog)",
    "RIGHT_TRIGGER_AXIS": "R2 (analog)",
}
_XBOX_LABELS = dict(_PS_LABELS)
_XBOX_LABELS.update({
    "RIGHT_FACE_DOWN": "A", "RIGHT_FACE_RIGHT": "B", "RIGHT_FACE_LEFT": "X",
    "RIGHT_FACE_UP": "Y", "LEFT_TRIGGER_1": "LB", "RIGHT_TRIGGER_1": "RB",
    "LEFT_TRIGGER_2": "LT", "RIGHT_TRIGGER_2": "RT", "LEFT_THUMB": "LS (left stick click)",
    "RIGHT_THUMB": "RS (right stick click)", "MIDDLE_LEFT": "View / Back",
    "MIDDLE_RIGHT": "Menu / Start", "MIDDLE": "Xbox button",
    "LEFT_TRIGGER_AXIS": "LT (analog)", "RIGHT_TRIGGER_AXIS": "RT (analog)",
})
_SWITCH_LABELS = dict(_PS_LABELS)
_SWITCH_LABELS.update({
    "RIGHT_FACE_DOWN": "B (bottom)", "RIGHT_FACE_RIGHT": "A (right)",
    "RIGHT_FACE_LEFT": "Y (left)", "RIGHT_FACE_UP": "X (top)",
    "LEFT_TRIGGER_1": "L", "RIGHT_TRIGGER_1": "R", "LEFT_TRIGGER_2": "ZL",
    "RIGHT_TRIGGER_2": "ZR", "LEFT_THUMB": "left stick click",
    "RIGHT_THUMB": "right stick click", "MIDDLE_LEFT": "Minus (-)",
    "MIDDLE_RIGHT": "Plus (+)", "MIDDLE": "Home",
    "LEFT_TRIGGER_AXIS": "ZL", "RIGHT_TRIGGER_AXIS": "ZR",
})

_SWITCH_BY_LABEL = dict(POSITIONAL)
_SWITCH_BY_LABEL.update({
    # Matched by printed letter (Xbox A = Cross = Nintendo A, which sits on the RIGHT).
    "CROSS": ["RIGHT_FACE_RIGHT"], "CIRCLE": ["RIGHT_FACE_DOWN"],
    "SQUARE": ["RIGHT_FACE_UP"], "TRIANGLE": ["RIGHT_FACE_LEFT"],
})

PRESETS = {
    "playstation": {
        "title": "PlayStation (DualSense, DualShock 4, DualShock 3)",
        "buttons": POSITIONAL, "labels": _PS_LABELS, "gamepad": "first",
        "notes": "Same buttons in the same places as the DualShock 2.",
    },
    "xbox": {
        "title": "Xbox (Series, One, 360) and most PC pads",
        "buttons": POSITIONAL, "labels": _XBOX_LABELS, "gamepad": "first",
        "notes": "Mapped by position: A is Cross, B is Circle, X is Square, Y is Triangle.",
    },
    "switch": {
        "title": "Nintendo Switch Pro, by position",
        "buttons": POSITIONAL, "labels": _SWITCH_LABELS, "gamepad": "first",
        "notes": ("By position: the bottom button (Nintendo B) is Cross, the right one "
                  "(Nintendo A) is Circle. Use switch-labels to match the printed letters."),
    },
    "switch-labels": {
        "title": "Nintendo Switch Pro, by printed letter",
        "buttons": _SWITCH_BY_LABEL, "labels": _SWITCH_LABELS, "gamepad": "first",
        "notes": ("By letter: Nintendo A (east) is Cross, B (south) is Circle, X (north) "
                  "is Square, Y (west) is Triangle. If presses land on the wrong button, "
                  "your mapping database reports the pad by label already: use switch."),
    },
    "keyboard": {
        "title": "Keyboard only (gamepads ignored)",
        "buttons": POSITIONAL, "labels": _PS_LABELS, "gamepad": "none",
        "notes": ("Arrows = D-pad, WASD = left stick, IJKL = right stick, X/Space = Cross, "
                  "C = Circle, Z = Square, V = Triangle, Q/E = L1/R1, 1/3 = L2/R2, "
                  "Left Shift/F = L3/R3, Enter = Start, Tab = Select."),
    },
}
LAYOUTS = list(PRESETS)

DEFAULTS = {
    "layout": "playstation",
    "buttons": {},
    "keyboard": {},
    "deadzone": 0.15,
    "invert_y": False,
    "stick_swap": False,
    "trigger_threshold": None,
    "gamepad": None,
    "players": 1,
    "keyboard_always": True,
    "touch": [],
    "touch_on_desktop": False,
    "allow_duplicates": [],
}
KNOWN_KEYS = set(DEFAULTS) | {"$comment", "comment", "notes"}

MARKER = "generated by console-controller (padmap.py gen)"


class UsageError(Exception):
    """Bad input: reported as one line on stderr with exit code 2."""


# ---------------------------------------------------------------- config

def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return value
    return [value]


def load_config(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise UsageError("no such file: %s (create one with: padmap.py init)" % path)
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError("cannot read %s: %s" % (path, exc))
    except json.JSONDecodeError as exc:
        raise UsageError("%s is not valid JSON (line %d: %s)" % (path, exc.lineno, exc.msg))
    if not isinstance(data, dict):
        raise UsageError("%s must hold a JSON object" % path)
    return data


def resolve(config):
    """Merge a controls.json dict with its preset. Returns (resolved, errors, warnings)."""
    errors, warnings = [], []
    cfg = dict(DEFAULTS)
    for key, value in config.items():
        if key not in KNOWN_KEYS:
            warnings.append("unknown setting %r is ignored" % key)
            continue
        cfg[key] = value

    layout = cfg["layout"]
    if layout not in PRESETS:
        errors.append("layout %r is not one of: %s" % (layout, ", ".join(LAYOUTS)))
        layout = "playstation"
    preset = PRESETS[layout]

    # gamepad buttons
    buttons = {name: list(preset["buttons"].get(name, [])) for name in DS2_ORDER}
    overrides = {} if cfg["buttons"] is None else cfg["buttons"]
    if not isinstance(overrides, dict):
        errors.append("buttons must be an object of DS2 button -> raylib button")
        overrides = {}
    for name, value in overrides.items():
        uname = str(name).upper()
        if uname not in DS2_MASK:
            errors.append("buttons: %r is not a DS2 button (%s)" % (name, ", ".join(DS2_ORDER)))
            continue
        srcs = []
        for src in _as_list(value):
            usrc = str(src).upper()
            if usrc.startswith("GAMEPAD_BUTTON_"):
                usrc = usrc[len("GAMEPAD_BUTTON_"):]
            if usrc not in GAMEPAD_SOURCES:
                errors.append("buttons.%s: %r is not a raylib gamepad button (see padmap.py table)"
                              % (uname, src))
                continue
            srcs.append(usrc)
        buttons[uname] = srcs

    # keyboard
    keyboard = {name: list(v) for name, v in KEYBOARD_DEFAULT.items()}
    kb_over = {} if cfg["keyboard"] is None else cfg["keyboard"]
    if not isinstance(kb_over, dict):
        errors.append("keyboard must be an object of DS2 button or stick direction -> key names")
        kb_over = {}
    for name, value in kb_over.items():
        uname = str(name).upper()
        if uname not in DS2_MASK and uname not in STICK_DIRS:
            errors.append("keyboard: %r is not a DS2 button or LSTICK_/RSTICK_ direction" % name)
            continue
        keys = []
        for key in _as_list(value):
            ukey = str(key).upper()
            if ukey.startswith("KEY_"):
                ukey = ukey[4:]
            if ukey not in RAYLIB_KEY_SET:
                errors.append("keyboard.%s: %r is not a raylib key name" % (uname, key))
                continue
            keys.append(ukey)
        keyboard[uname] = keys

    # gamepad selection
    gamepad = cfg["gamepad"] if cfg["gamepad"] is not None else preset["gamepad"]
    if isinstance(gamepad, bool) or not (
            gamepad in ("first", "none") or (isinstance(gamepad, int) and 0 <= gamepad <= 3)):
        errors.append('gamepad must be "first", "none" or a raylib pad index 0 to 3')
        gamepad = "first"

    players = cfg["players"]
    if isinstance(players, bool) or players not in (1, 2):
        errors.append("players must be 1 or 2")
        players = 1

    # sticks
    dz = cfg["deadzone"]
    if isinstance(dz, dict):
        pairs = [("left", dz.get("left", 0.15)), ("right", dz.get("right", 0.15))]
    else:
        pairs = [("left", dz), ("right", dz)]
    dz_values = []
    for label, val in pairs:
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not 0.0 <= val <= 0.9:
            errors.append("deadzone (%s) must be a number from 0 to 0.9" % label)
            val = 0.15
        dz_values.append(float(val))
    dz_left, dz_right = dz_values

    inv = cfg["invert_y"]
    invert = {"false": (False, False), "true": (False, True), "left": (True, False),
              "right": (False, True), "both": (True, True), "none": (False, False)}
    if isinstance(inv, bool):
        inv_key = "true" if inv else "false"
    elif isinstance(inv, str) and inv.lower() in ("left", "right", "both", "none"):
        inv_key = inv.lower()
    else:
        errors.append('invert_y must be false, true (the right stick), "left", "right" or "both"')
        inv_key = "false"
    inv_left, inv_right = invert[inv_key]

    if not isinstance(cfg["stick_swap"], bool):
        errors.append("stick_swap must be true or false")
    if not isinstance(cfg["keyboard_always"], bool):
        errors.append("keyboard_always must be true or false")
    if not isinstance(cfg["touch_on_desktop"], bool):
        errors.append("touch_on_desktop must be true or false")

    tt = cfg["trigger_threshold"]
    if tt is not None and (isinstance(tt, bool) or not isinstance(tt, (int, float))
                           or not 0.0 < tt <= 1.0):
        errors.append("trigger_threshold must be null or a number above 0 and at most 1")
        tt = None
    if tt is not None:
        # Analog triggers: L2/R2 read the trigger axes instead of raylib's trigger buttons.
        for name, axis_src, btn in (("L2", "LEFT_TRIGGER_AXIS", "LEFT_TRIGGER_2"),
                                    ("R2", "RIGHT_TRIGGER_AXIS", "RIGHT_TRIGGER_2")):
            buttons[name] = [axis_src if s == btn else s for s in buttons[name]]

    # touch zones
    touch_buttons, touch_sticks = [], []
    zones = [] if cfg["touch"] is None else cfg["touch"]
    if not isinstance(zones, list):
        errors.append("touch must be a list of zones")
        zones = []
    for i, zone in enumerate(zones):
        where = "touch[%d]" % i
        if not isinstance(zone, dict):
            errors.append("%s must be an object" % where)
            continue
        nums = {}
        for key in ("x", "y", "r"):
            val = zone.get(key)
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                errors.append("%s.%s must be a number" % (where, key))
                nums = None
                break
            nums[key] = float(val)
        if nums is None:
            continue
        if not (0.0 <= nums["x"] <= 1.0 and 0.0 <= nums["y"] <= 1.0):
            errors.append("%s: x and y are fractions of the screen, 0 to 1" % where)
            continue
        if not 0.0 < nums["r"] <= 0.5:
            errors.append("%s: r is a fraction of the shorter screen side, above 0 and at most 0.5"
                          % where)
            continue
        if "button" in zone and "stick" not in zone:
            b = str(zone["button"]).upper()
            if b not in DS2_MASK:
                errors.append("%s.button %r is not a DS2 button" % (where, zone["button"]))
                continue
            touch_buttons.append((b, nums["x"], nums["y"], nums["r"]))
        elif "stick" in zone and "button" not in zone:
            s = str(zone["stick"]).lower()
            if s not in ("left", "right"):
                errors.append('%s.stick must be "left" or "right"' % where)
                continue
            touch_sticks.append((s, nums["x"], nums["y"], nums["r"]))
        else:
            errors.append('%s needs exactly one of "button" or "stick"' % where)

    allow = set()
    for item in _as_list(cfg["allow_duplicates"]):
        u = str(item).upper()
        allow.add(u[4:] if u.startswith("KEY_") else u)

    resolved = {
        "layout": layout,
        "buttons": buttons,
        "keyboard": keyboard,
        "gamepad": gamepad,
        "players": int(players),
        "deadzone_left": dz_left,
        "deadzone_right": dz_right,
        "invert_left": inv_left,
        "invert_right": inv_right,
        "stick_swap": bool(cfg["stick_swap"]) if isinstance(cfg["stick_swap"], bool) else False,
        "trigger_threshold": float(tt) if tt is not None else None,
        "keyboard_always": cfg["keyboard_always"] if isinstance(cfg["keyboard_always"], bool) else True,
        "touch_buttons": touch_buttons,
        "touch_sticks": touch_sticks,
        "touch_on_desktop": cfg["touch_on_desktop"] is True,
        "allow_duplicates": sorted(allow),
    }
    return resolved, errors, warnings


def validate(resolved):
    """Rule checks on a resolved mapping. Returns (errors, warnings)."""
    errors, warnings = [], []
    allow = set(resolved["allow_duplicates"])
    uses_pad = resolved["gamepad"] != "none"

    if uses_pad:
        for name in DS2_ORDER:
            if not resolved["buttons"][name]:
                errors.append("%s is not bound to any gamepad button" % name)
        owners = {}
        for name in DS2_ORDER:
            for src in resolved["buttons"][name]:
                owners.setdefault(src, []).append(name)
        for src, names in sorted(owners.items()):
            if len(names) > 1 and src not in allow:
                errors.append("gamepad %s is bound to %s (add it to allow_duplicates if you mean it)"
                              % (src, " and ".join(names)))
        for name in DS2_ORDER:
            if "MIDDLE" in resolved["buttons"][name]:
                warnings.append("%s uses the guide button (MIDDLE): Steam, Windows and macOS often "
                                "take that press for themselves" % name)

    kb = resolved["keyboard"]
    for name in DS2_ORDER:
        if not kb.get(name):
            msg = "%s has no keyboard key" % name
            (errors if not uses_pad else warnings).append(msg)
    key_owners = {}
    for target, keys in kb.items():
        for key in keys:
            key_owners.setdefault(key, []).append(target)
    for key, targets in sorted(key_owners.items()):
        if len(targets) > 1 and key not in allow:
            errors.append("key %s is bound to %s (add it to allow_duplicates if you mean it)"
                          % (key, " and ".join(targets)))
    if "ESCAPE" in key_owners:
        warnings.append("ESCAPE is bound, but raylib closes the window on Escape (the runtime "
                        "never calls SetExitKey), so that press also quits the game")
    if "F1" in key_owners:
        warnings.append("F1 is bound, but desktop builds with the debug UI toggle the debug "
                        "panel on F1 (ps2_debug_panel.cpp)")
    if not resolved["keyboard_always"] and resolved["gamepad"] == "none":
        warnings.append("keyboard_always is false but gamepads are ignored: the keyboard is used anyway")
    if resolved["players"] == 2 and resolved["gamepad"] == "none":
        errors.append("players 2 needs gamepads (gamepad is \"none\")")
    if resolved["touch_on_desktop"] and not (resolved["touch_buttons"] or resolved["touch_sticks"]):
        warnings.append("touch_on_desktop is true but there are no touch zones")
    return errors, warnings


# ---------------------------------------------------------------- replay scripts

def parse_button_token(token):
    """Return the PRESSED mask (active-high, DS2 bit values) for a script token."""
    if token in ("-", "none", "NONE"):
        return 0
    if token[:2].lower() == "0x":
        try:
            value = int(token[2:], 16)
        except ValueError:
            raise ValueError("bad hex mask %r" % token)
        if not 0 <= value <= 0xFFFF:
            raise ValueError("mask %r is above 0xFFFF" % token)
        return value
    mask = 0
    for part in token.split("+"):
        upart = part.upper()
        if upart not in DS2_MASK:
            raise ValueError("%r is not a DS2 button" % part)
        mask |= DS2_MASK[upart]
    return mask


def _parse_byte(token):
    try:
        value = int(token, 16) if token[:2].lower() == "0x" else int(token, 10)
    except ValueError:
        raise ValueError("stick value %r is not a number" % token)
    if not 0 <= value <= 255:
        raise ValueError("stick value %r is outside 0 to 255" % token)
    return value


def parse_script(text):
    """Parse a replay script. Returns a list of (frame, pressed, lx, ly, rx, ry)."""
    lines = []
    last_frame = -1
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) not in (2, 6):
            raise ValueError("line %d: expected 'frame buttons' or 'frame buttons lx ly rx ry'"
                             % number)
        try:
            frame = int(parts[0], 10)
        except ValueError:
            raise ValueError("line %d: frame %r is not a whole number" % (number, parts[0]))
        if frame < 0 or frame > 0xFFFFFFFF:
            raise ValueError("line %d: frame %d is out of range" % (number, frame))
        if frame < last_frame:
            raise ValueError("line %d: frame %d comes before the previous line's %d"
                             % (number, frame, last_frame))
        try:
            pressed = parse_button_token(parts[1])
            sticks = [_parse_byte(p) for p in parts[2:]] if len(parts) == 6 else [128] * 4
        except ValueError as exc:
            raise ValueError("line %d: %s" % (number, exc))
        lines.append((frame, pressed, sticks[0], sticks[1], sticks[2], sticks[3]))
        last_frame = frame
    return lines


def pressed_names(mask):
    names = [n for n in DS2_ORDER if mask & DS2_MASK[n]]
    return "+".join(names) if names else "-"


# ---------------------------------------------------------------- C++ generation

def _cfloat(value):
    text = repr(float(value))
    if "e" in text or "E" in text:
        text = "%.9f" % float(value)
    return text + "f"


def _sentinel_table(rows, empty):
    if not rows:
        return ["        %s, // none" % empty]
    return ["        %s," % r for r in rows]


def generate_cpp(resolved, replay=False, source="controls.json"):
    r = resolved
    gp = r["gamepad"]
    gamepad_index = -2 if gp == "none" else (-1 if gp == "first" else int(gp))
    touch = bool(r["touch_buttons"] or r["touch_sticks"])

    button_rows, axis_rows = [], []
    for name in DS2_ORDER:
        for src in r["buttons"][name]:
            if src in TRIGGER_AXES:
                axis_rows.append("{PAD_%s, %s}" % (name, TRIGGER_AXES[src]))
            else:
                button_rows.append("{PAD_%s, GAMEPAD_BUTTON_%s}" % (name, src))
    key_rows, stick_key_rows = [], []
    for name in DS2_ORDER:
        for key in r["keyboard"].get(name, []):
            key_rows.append("{PAD_%s, KEY_%s}" % (name, key))
    for direction in sorted(STICK_DIRS):
        stick, axis, value = STICK_DIRS[direction]
        for key in r["keyboard"].get(direction, []):
            stick_key_rows.append("{%d, %d, %s, KEY_%s} /* %s */"
                                  % (stick, axis, _cfloat(value), key, direction))

    L = []
    w = L.append
    w("// ps2_pad.cpp: %s from %s." % (MARKER, source))
    w("// Layout preset: %s. Edit controls.json and run padmap.py gen again; do not edit this file." % r["layout"])
    w("//")
    w("// Drop-in replacement for ps2xRuntime/src/lib/ps2_pad.cpp in PS2Recomp. Same class, same")
    w("// readState signature, same 32-byte DualShock 2 report: data[1] = 0x73 (analog), buttons")
    w("// ACTIVE-LOW in data[2] (low byte) and data[3] (high byte), sticks in data[4..7] as")
    w("// RX, RY, LX, LY with 0x80 at centre. raylib calls only. Back up the stock file first")
    w("// (padmap.py install does that). Changing this .cpp is an incremental rebuild, no headers.")
    if replay:
        w("//")
        w("// Input replay: set PS2X_INPUT_SCRIPT to a file of 'frame buttons [lx ly rx ry]' lines and")
        w("// port 0 plays it back instead of live input (frame = number of port-0 readState calls).")
        w("// Input recording: set PS2X_INPUT_RECORD to a path and live port-0 input is written there")
        w("// in the same format, one line per change.")
    if replay:
        w("#if defined(_MSC_VER) && !defined(_CRT_SECURE_NO_WARNINGS)")
        w("#define _CRT_SECURE_NO_WARNINGS")
        w("#endif")
    if touch and r["touch_on_desktop"]:
        w("#ifndef PS2X_PAD_TOUCH")
        w("#define PS2X_PAD_TOUCH 1")
        w("#endif")
    w('#include "runtime/ps2_pad.h"')
    w('#include "ps2_host_backend.h"')
    w("#include <cstring>")
    w("#include <atomic>")
    w("#include <cmath>")
    if replay:
        w("#include <cstdio>")
        w("#include <cstdlib>")
        w("#include <mutex>")
        w("#include <string>")
        w("#include <vector>")
    w("")
    w("namespace")
    w("{")
    w("    constexpr uint8_t kPadAnalogMarker = 0x73;")
    w("    constexpr uint8_t kPadStickCenter = 0x80;")
    w("")
    for name in ["LEFT", "DOWN", "RIGHT", "UP", "START", "R3", "L3", "SELECT",
                 "SQUARE", "CROSS", "CIRCLE", "TRIANGLE", "R1", "L1", "R2", "L2"]:
        w("    constexpr uint16_t PAD_%s = 0x%04Xu;" % (name, DS2_MASK[name]))
    w("")
    w("    // ---- settings from %s ----" % source)
    w("    constexpr int kGamepadIndex = %d; // -1 = first connected pads in order, -2 = gamepads ignored" % gamepad_index)
    w("    constexpr int kPlayers = %d; // port 1 gets the second pad when 2" % r["players"])
    w("    constexpr bool kKeyboardAlways = %s; // keyboard also works while a pad is connected" % ("true" if r["keyboard_always"] else "false"))
    w("    constexpr float kDeadzoneLeft = %s; // radial" % _cfloat(r["deadzone_left"]))
    w("    constexpr float kDeadzoneRight = %s;" % _cfloat(r["deadzone_right"]))
    w("    constexpr bool kInvertLeftY = %s;" % ("true" if r["invert_left"] else "false"))
    w("    constexpr bool kInvertRightY = %s;" % ("true" if r["invert_right"] else "false"))
    w("    constexpr bool kStickSwap = %s;" % ("true" if r["stick_swap"] else "false"))
    w("    constexpr float kTriggerThreshold = %s; // analog L2/R2 bindings only"
      % _cfloat(r["trigger_threshold"] if r["trigger_threshold"] is not None else 0.5))
    w("")
    w("    struct ButtonBinding")
    w("    {")
    w("        uint16_t mask;")
    w("        int button;")
    w("    };")
    w("    struct AxisBinding")
    w("    {")
    w("        uint16_t mask;")
    w("        int axis;")
    w("    };")
    w("    struct KeyBinding")
    w("    {")
    w("        uint16_t mask;")
    w("        int key;")
    w("    };")
    w("    struct StickKey")
    w("    {")
    w("        int stick; // 0 = left, 1 = right")
    w("        int axis;  // 0 = x, 1 = y")
    w("        float value;")
    w("        int key;")
    w("    };")
    w("")
    w("    // A row with mask 0 (or key -1) is a placeholder and is skipped.")
    w("    constexpr ButtonBinding kGamepadButtons[] = {")
    L.extend(_sentinel_table(button_rows, "{0u, 0}"))
    w("    };")
    w("    constexpr AxisBinding kTriggerAxes[] = {")
    L.extend(_sentinel_table(axis_rows, "{0u, 0}"))
    w("    };")
    w("    constexpr KeyBinding kKeyButtons[] = {")
    L.extend(_sentinel_table(key_rows, "{0u, -1}"))
    w("    };")
    w("    constexpr StickKey kStickKeys[] = {")
    L.extend(_sentinel_table(stick_key_rows, "{0, 0, 0.0f, -1}"))
    w("    };")
    w("")
    if touch:
        w("#if defined(__ANDROID__) || defined(PS2X_PAD_TOUCH)")
        w("    // Touch zones: x, y are fractions of the screen; r is a fraction of its shorter side.")
        w("    // They are invisible: the runtime draws no overlay (ps2_android_runtime.cpp is a TODO).")
        w("    struct TouchButton")
        w("    {")
        w("        uint16_t mask;")
        w("        float x, y, r;")
        w("    };")
        w("    struct TouchStick")
        w("    {")
        w("        int stick;")
        w("        float x, y, r;")
        w("    };")
        tb = ["{PAD_%s, %s, %s, %s}" % (b, _cfloat(x), _cfloat(y), _cfloat(rr))
              for b, x, y, rr in r["touch_buttons"]]
        ts = ["{%d, %s, %s, %s}" % (0 if s == "left" else 1, _cfloat(x), _cfloat(y), _cfloat(rr))
              for s, x, y, rr in r["touch_sticks"]]
        w("    constexpr TouchButton kTouchButtons[] = {")
        L.extend(_sentinel_table(tb, "{0u, 0.0f, 0.0f, 0.0f}"))
        w("    };")
        w("    constexpr TouchStick kTouchSticks[] = {")
        L.extend(_sentinel_table(ts, "{-1, 0.0f, 0.0f, 0.0f}"))
        w("    };")
        w("#endif")
        w("")
    w("    struct PadFrame")
    w("    {")
    w("        uint16_t buttons = 0xFFFFu; // active-low, like the report")
    w("        float lx = 0.0f;")
    w("        float ly = 0.0f;")
    w("        float rx = 0.0f;")
    w("        float ry = 0.0f;")
    w("    };")
    w("")
    w("    struct Report")
    w("    {")
    w("        uint16_t buttons = 0xFFFFu; // active-low")
    w("        uint8_t lx = kPadStickCenter;")
    w("        uint8_t ly = kPadStickCenter;")
    w("        uint8_t rx = kPadStickCenter;")
    w("        uint8_t ry = kPadStickCenter;")
    w("    };")
    w("")
    w("    std::atomic<bool> g_triggerSignedRange[4][2]{}; // [pad][left, right trigger]")
    w("")
    w("    float clampUnit(float v)")
    w("    {")
    w("        return v < -1.0f ? -1.0f : (v > 1.0f ? 1.0f : v);")
    w("    }")
    w("")
    w("    uint8_t axisToByte(float v)")
    w("    {")
    w("        return static_cast<uint8_t>(std::lround((clampUnit(v) + 1.0f) * 127.5f));")
    w("    }")
    w("")
    w("    void applyRadialDeadzone(float &x, float &y, float deadzone)")
    w("    {")
    w("        const float magnitude = std::sqrt(x * x + y * y);")
    w("        if (magnitude <= deadzone || magnitude <= 0.0f)")
    w("        {")
    w("            x = 0.0f;")
    w("            y = 0.0f;")
    w("            return;")
    w("        }")
    w("        const float clamped = magnitude > 1.0f ? 1.0f : magnitude;")
    w("        const float scale = (clamped - deadzone) / (1.0f - deadzone) / magnitude;")
    w("        x = clampUnit(x * scale);")
    w("        y = clampUnit(y * scale);")
    w("    }")
    w("")
    w("    int resolveGamepad(int player)")
    w("    {")
    w("        if (kGamepadIndex == -2)")
    w("            return -1;")
    w("        if (kGamepadIndex >= 0)")
    w("        {")
    w("            const int pad = kGamepadIndex + player;")
    w("            return IsGamepadAvailable(pad) ? pad : -1;")
    w("        }")
    w("        int seen = 0;")
    w("        for (int pad = 0; pad < 4; ++pad)")
    w("        {")
    w("            if (!IsGamepadAvailable(pad))")
    w("                continue;")
    w("            if (seen == player)")
    w("                return pad;")
    w("            ++seen;")
    w("        }")
    w("        return -1;")
    w("    }")
    w("")
    w("    // Trigger axes rest at -1 on some backends and at 0 on others (some report 0 until")
    w("    // first touched): once a value below zero is seen on a trigger, treat its range as")
    w("    // -1..1 and rescale it to 0..1. Tracked per trigger so one never affects the other.")
    w("    float triggerAmount(int pad, int axis)")
    w("    {")
    w("        float v = GetGamepadAxisMovement(pad, axis);")
    w("        std::atomic<bool> &isSigned = g_triggerSignedRange[pad & 3][axis == GAMEPAD_AXIS_RIGHT_TRIGGER ? 1 : 0];")
    w("        if (v < -0.01f)")
    w("            isSigned.store(true, std::memory_order_relaxed);")
    w("        if (isSigned.load(std::memory_order_relaxed))")
    w("            v = (v + 1.0f) * 0.5f;")
    w("        return v;")
    w("    }")
    w("")
    if touch:
        w("#if defined(__ANDROID__) || defined(PS2X_PAD_TOUCH)")
        w("    void readTouch(PadFrame &frame)")
        w("    {")
        w("        const int count = GetTouchPointCount();")
        w("        const float width = static_cast<float>(GetScreenWidth());")
        w("        const float height = static_cast<float>(GetScreenHeight());")
        w("        if (count <= 0 || width <= 0.0f || height <= 0.0f)")
        w("            return;")
        w("        const float side = width < height ? width : height;")
        w("        for (int i = 0; i < count; ++i)")
        w("        {")
        w("            const Vector2 point = GetTouchPosition(i);")
        w("            for (const auto &zone : kTouchButtons)")
        w("            {")
        w("                if (zone.mask == 0u)")
        w("                    continue;")
        w("                const float dx = (point.x - zone.x * width) / side;")
        w("                const float dy = (point.y - zone.y * height) / side;")
        w("                if (dx * dx + dy * dy <= zone.r * zone.r)")
        w("                    frame.buttons &= static_cast<uint16_t>(~zone.mask);")
        w("            }")
        w("            for (const auto &zone : kTouchSticks)")
        w("            {")
        w("                if (zone.stick < 0)")
        w("                    continue;")
        w("                const float dx = (point.x - zone.x * width) / side / zone.r;")
        w("                const float dy = (point.y - zone.y * height) / side / zone.r;")
        w("                if (dx * dx + dy * dy > 2.25f) // grab radius: 1.5 x r")
        w("                    continue;")
        w("                if (zone.stick == 0)")
        w("                {")
        w("                    frame.lx = clampUnit(dx);")
        w("                    frame.ly = clampUnit(dy);")
        w("                }")
        w("                else")
        w("                {")
        w("                    frame.rx = clampUnit(dx);")
        w("                    frame.ry = clampUnit(dy);")
        w("                }")
        w("            }")
        w("        }")
        w("    }")
        w("#endif")
        w("")
    w("    void readLive(int player, PadFrame &frame)")
    w("    {")
    w("        bool usedGamepad = false;")
    w("        const int pad = resolveGamepad(player);")
    w("        if (pad >= 0)")
    w("        {")
    w("            usedGamepad = true;")
    w("            for (const auto &b : kGamepadButtons)")
    w("            {")
    w("                if (b.mask != 0u && IsGamepadButtonDown(pad, b.button))")
    w("                    frame.buttons &= static_cast<uint16_t>(~b.mask);")
    w("            }")
    w("            for (const auto &t : kTriggerAxes)")
    w("            {")
    w("                if (t.mask != 0u && triggerAmount(pad, t.axis) >= kTriggerThreshold)")
    w("                    frame.buttons &= static_cast<uint16_t>(~t.mask);")
    w("            }")
    w("            float lx = GetGamepadAxisMovement(pad, GAMEPAD_AXIS_LEFT_X);")
    w("            float ly = GetGamepadAxisMovement(pad, GAMEPAD_AXIS_LEFT_Y);")
    w("            float rx = GetGamepadAxisMovement(pad, GAMEPAD_AXIS_RIGHT_X);")
    w("            float ry = GetGamepadAxisMovement(pad, GAMEPAD_AXIS_RIGHT_Y);")
    w("            if (kStickSwap)")
    w("            {")
    w("                const float tx = lx;")
    w("                const float ty = ly;")
    w("                lx = rx;")
    w("                ly = ry;")
    w("                rx = tx;")
    w("                ry = ty;")
    w("            }")
    w("            frame.lx = lx;")
    w("            frame.ly = ly;")
    w("            frame.rx = rx;")
    w("            frame.ry = ry;")
    w("        }")
    w("")
    w("        if (player == 0 && (kKeyboardAlways || !usedGamepad))")
    w("        {")
    w("            for (const auto &k : kKeyButtons)")
    w("            {")
    w("                if (k.key >= 0 && IsKeyDown(k.key))")
    w("                    frame.buttons &= static_cast<uint16_t>(~k.mask);")
    w("            }")
    w("            float keyAxes[2][2] = {{0.0f, 0.0f}, {0.0f, 0.0f}};")
    w("            for (const auto &s : kStickKeys)")
    w("            {")
    w("                if (s.key >= 0 && IsKeyDown(s.key))")
    w("                    keyAxes[s.stick][s.axis] += s.value;")
    w("            }")
    w("            if (keyAxes[0][0] != 0.0f || keyAxes[0][1] != 0.0f)")
    w("            {")
    w("                frame.lx = clampUnit(keyAxes[0][0]);")
    w("                frame.ly = clampUnit(keyAxes[0][1]);")
    w("            }")
    w("            if (keyAxes[1][0] != 0.0f || keyAxes[1][1] != 0.0f)")
    w("            {")
    w("                frame.rx = clampUnit(keyAxes[1][0]);")
    w("                frame.ry = clampUnit(keyAxes[1][1]);")
    w("            }")
    w("        }")
    if touch:
        w("#if defined(__ANDROID__) || defined(PS2X_PAD_TOUCH)")
        w("        if (player == 0)")
        w("            readTouch(frame);")
        w("#endif")
    w("")
    w("        if (kInvertLeftY)")
    w("            frame.ly = -frame.ly;")
    w("        if (kInvertRightY)")
    w("            frame.ry = -frame.ry;")
    w("        applyRadialDeadzone(frame.lx, frame.ly, kDeadzoneLeft);")
    w("        applyRadialDeadzone(frame.rx, frame.ry, kDeadzoneRight);")
    w("    }")
    w("")
    w("    Report toReport(const PadFrame &frame)")
    w("    {")
    w("        Report report;")
    w("        report.buttons = frame.buttons;")
    w("        report.lx = axisToByte(frame.lx);")
    w("        report.ly = axisToByte(frame.ly);")
    w("        report.rx = axisToByte(frame.rx);")
    w("        report.ry = axisToByte(frame.ry);")
    w("        return report;")
    w("    }")
    if replay:
        L.extend(_REPLAY_CPP)
    w("}")
    w("")
    w("bool PSPadBackend::readState(int port, int slot, uint8_t *data, size_t size)")
    w("{")
    w("    (void)slot;")
    w("    if (!data || size < 32)")
    w("        return false;")
    w("")
    w("    std::memset(data, 0, 32);")
    w("    data[0] = 0x01;")
    w("    data[1] = kPadAnalogMarker;")
    w("    data[2] = 0xFF;")
    w("    data[3] = 0xFF;")
    w("    data[4] = data[5] = data[6] = data[7] = kPadStickCenter;")
    w("")
    w("    Report report; // released, sticks centred: what an unused port reports")
    w("    if (port == 0)")
    w("    {")
    if replay:
        w("        if (!replayReport(report))")
        w("        {")
        w("            PadFrame frame;")
        w("            readLive(0, frame);")
        w("            report = toReport(frame);")
        w("            recordReport(report);")
        w("        }")
    else:
        w("        PadFrame frame;")
        w("        readLive(0, frame);")
        w("        report = toReport(frame);")
    w("    }")
    w("    else if (port > 0 && port < kPlayers)")
    w("    {")
    w("        PadFrame frame;")
    w("        readLive(port, frame);")
    w("        report = toReport(frame);")
    w("    }")
    w("")
    w("    data[2] = static_cast<uint8_t>(report.buttons & 0xFFu);")
    w("    data[3] = static_cast<uint8_t>(report.buttons >> 8);")
    w("    data[4] = report.rx;")
    w("    data[5] = report.ry;")
    w("    data[6] = report.lx;")
    w("    data[7] = report.ly;")
    w("    return true;")
    w("}")
    return "\n".join(L) + "\n"


_REPLAY_CPP = r"""
    // ---- input replay (PS2X_INPUT_SCRIPT) and recording (PS2X_INPUT_RECORD) ----
    struct ScriptLine
    {
        uint32_t frame;
        uint16_t pressed; // active-high here, flipped when written to the report
        uint8_t lx, ly, rx, ry;
    };

    struct NamedMask
    {
        const char *name;
        uint16_t mask;
    };

    constexpr NamedMask kScriptButtons[] = {
        {"UP", PAD_UP}, {"DOWN", PAD_DOWN}, {"LEFT", PAD_LEFT}, {"RIGHT", PAD_RIGHT},
        {"CROSS", PAD_CROSS}, {"CIRCLE", PAD_CIRCLE}, {"SQUARE", PAD_SQUARE},
        {"TRIANGLE", PAD_TRIANGLE}, {"L1", PAD_L1}, {"R1", PAD_R1}, {"L2", PAD_L2},
        {"R2", PAD_R2}, {"L3", PAD_L3}, {"R3", PAD_R3}, {"START", PAD_START},
        {"SELECT", PAD_SELECT},
    };

    struct ReplayState
    {
        std::mutex lock;
        bool initialized = false;
        bool active = false;
        bool announcedEnd = false;
        std::vector<ScriptLine> lines;
        size_t next = 0;
        uint32_t frame = 0;
        Report current{};
        std::FILE *record = nullptr;
        bool haveLast = false;
        Report last{};
    };

    ReplayState &replayState()
    {
        static ReplayState state;
        return state;
    }

    std::vector<std::string> splitWords(const std::string &line)
    {
        std::vector<std::string> words;
        std::string word;
        for (char c : line)
        {
            if (c == ' ' || c == '\t' || c == '\r' || c == '\n')
            {
                if (!word.empty())
                    words.push_back(word);
                word.clear();
            }
            else
            {
                word.push_back(c);
            }
        }
        if (!word.empty())
            words.push_back(word);
        return words;
    }

    bool parseNumber(const std::string &text, unsigned long max, unsigned long &out)
    {
        if (text.empty())
            return false;
        int base = 10;
        size_t start = 0;
        if (text.size() > 2 && text[0] == '0' && (text[1] == 'x' || text[1] == 'X'))
        {
            base = 16;
            start = 2;
        }
        char *end = nullptr;
        const unsigned long value = std::strtoul(text.c_str() + start, &end, base);
        if (!end || *end != '\0' || end == text.c_str() + start || value > max)
            return false;
        out = value;
        return true;
    }

    bool parseButtons(const std::string &token, uint16_t &pressed)
    {
        pressed = 0u;
        if (token == "-" || token == "none" || token == "NONE")
            return true;
        if (token.size() > 2 && token[0] == '0' && (token[1] == 'x' || token[1] == 'X'))
        {
            unsigned long mask = 0;
            if (!parseNumber(token, 0xFFFFul, mask))
                return false;
            pressed = static_cast<uint16_t>(mask);
            return true;
        }
        size_t start = 0;
        while (true)
        {
            const size_t plus = token.find('+', start);
            std::string name = token.substr(start, plus == std::string::npos ? std::string::npos : plus - start);
            for (char &c : name)
            {
                if (c >= 'a' && c <= 'z')
                    c = static_cast<char>(c - 'a' + 'A');
            }
            bool found = false;
            for (const auto &entry : kScriptButtons)
            {
                if (name == entry.name)
                {
                    pressed = static_cast<uint16_t>(pressed | entry.mask);
                    found = true;
                    break;
                }
            }
            if (!found)
                return false;
            if (plus == std::string::npos)
                return true;
            start = plus + 1;
        }
    }

    bool loadScript(const char *path, std::vector<ScriptLine> &out)
    {
        std::FILE *file = std::fopen(path, "r");
        if (!file)
        {
            std::fprintf(stderr, "[pad-replay] cannot open %s\n", path);
            return false;
        }
        char buffer[512];
        unsigned lineNumber = 0;
        uint32_t lastFrame = 0;
        bool ok = true;
        while (std::fgets(buffer, sizeof(buffer), file))
        {
            ++lineNumber;
            std::string line(buffer);
            const size_t hash = line.find('#');
            if (hash != std::string::npos)
                line.erase(hash);
            const std::vector<std::string> words = splitWords(line);
            if (words.empty())
                continue;
            ScriptLine entry{0u, 0u, kPadStickCenter, kPadStickCenter, kPadStickCenter, kPadStickCenter};
            unsigned long value = 0;
            bool lineOk = (words.size() == 2 || words.size() == 6) &&
                          parseNumber(words[0], 0xFFFFFFFFul, value);
            if (lineOk)
                entry.frame = static_cast<uint32_t>(value);
            lineOk = lineOk && parseButtons(words[1], entry.pressed);
            if (lineOk && words.size() == 6)
            {
                uint8_t *sticks[4] = {&entry.lx, &entry.ly, &entry.rx, &entry.ry};
                for (int i = 0; i < 4 && lineOk; ++i)
                {
                    lineOk = parseNumber(words[2 + i], 255ul, value);
                    *sticks[i] = static_cast<uint8_t>(value);
                }
            }
            if (lineOk && !out.empty() && entry.frame < lastFrame)
                lineOk = false;
            if (!lineOk)
            {
                std::fprintf(stderr, "[pad-replay] %s line %u is not 'frame buttons [lx ly rx ry]' in frame order; replay disabled\n",
                             path, lineNumber);
                ok = false;
                break;
            }
            lastFrame = entry.frame;
            out.push_back(entry);
        }
        std::fclose(file);
        if (!ok)
            out.clear();
        return ok;
    }

    void initReplayLocked(ReplayState &state)
    {
        if (state.initialized)
            return;
        state.initialized = true;
        const char *script = std::getenv("PS2X_INPUT_SCRIPT");
        if (script && script[0] != '\0')
        {
            if (loadScript(script, state.lines))
            {
                state.active = true;
                std::fprintf(stderr, "[pad-replay] loaded %u lines from %s\n",
                             static_cast<unsigned>(state.lines.size()), script);
            }
            return;
        }
        const char *recordPath = std::getenv("PS2X_INPUT_RECORD");
        if (recordPath && recordPath[0] != '\0')
        {
            state.record = std::fopen(recordPath, "w");
            if (state.record)
            {
                std::fprintf(state.record, "# recorded by ps2_pad.cpp: frame buttons lx ly rx ry (one line per change)\n");
                std::fflush(state.record);
                std::fprintf(stderr, "[pad-record] writing %s\n", recordPath);
            }
            else
            {
                std::fprintf(stderr, "[pad-record] cannot open %s\n", recordPath);
            }
        }
    }

    // Port 0 only. Returns true and fills the report while a script is playing.
    bool replayReport(Report &out)
    {
        ReplayState &state = replayState();
        std::lock_guard<std::mutex> guard(state.lock);
        initReplayLocked(state);
        if (!state.active)
            return false;
        while (state.next < state.lines.size() && state.lines[state.next].frame <= state.frame)
        {
            const ScriptLine &line = state.lines[state.next];
            state.current.buttons = static_cast<uint16_t>(~line.pressed);
            state.current.lx = line.lx;
            state.current.ly = line.ly;
            state.current.rx = line.rx;
            state.current.ry = line.ry;
            ++state.next;
        }
        if (state.next >= state.lines.size() && !state.announcedEnd)
        {
            state.announcedEnd = true;
            std::fprintf(stderr, "[pad-replay] script finished at frame %u\n", state.frame);
        }
        out = state.current;
        ++state.frame;
        return true;
    }

    void recordReport(const Report &report)
    {
        ReplayState &state = replayState();
        std::lock_guard<std::mutex> guard(state.lock);
        const uint32_t frame = state.frame++;
        if (!state.record)
            return;
        if (state.haveLast && state.last.buttons == report.buttons && state.last.lx == report.lx &&
            state.last.ly == report.ly && state.last.rx == report.rx && state.last.ry == report.ry)
            return;
        state.haveLast = true;
        state.last = report;
        std::string names;
        const uint16_t pressed = static_cast<uint16_t>(~report.buttons);
        for (const auto &entry : kScriptButtons)
        {
            if (pressed & entry.mask)
            {
                if (!names.empty())
                    names.push_back('+');
                names += entry.name;
            }
        }
        if (names.empty())
            names = "-";
        std::fprintf(state.record, "%u %s %u %u %u %u\n", frame, names.c_str(),
                     static_cast<unsigned>(report.lx), static_cast<unsigned>(report.ly),
                     static_cast<unsigned>(report.rx), static_cast<unsigned>(report.ry));
        std::fflush(state.record);
    }
""".split("\n")


# ---------------------------------------------------------------- printing

def table_rows():
    rows = []
    for name in DS2_ORDER:
        mask = DS2_MASK[name]
        bit = mask.bit_length() - 1
        byte = 2 if bit < 8 else 3
        rows.append({
            "button": name, "mask": "0x%04X" % mask, "byte": "data[%d]" % byte,
            "bit": bit % 8, "raylib": "GAMEPAD_BUTTON_" + POSITIONAL[name][0],
            "keyboard": "/".join(KEYBOARD_DEFAULT[name]),
        })
    return rows


def print_table(as_json):
    rows = table_rows()
    if as_json:
        print(json.dumps({"buttons": rows, "sticks": {
            "data[4]": "RX", "data[5]": "RY", "data[6]": "LX", "data[7]": "LY",
            "centre": "0x80", "range": "0x00 (left/up) to 0xFF (right/down)"},
            "active_low": True}, indent=2))
        return
    print("DualShock 2 report (32 bytes) as PS2Recomp's PSPadBackend::readState fills it")
    print("  data[0] = 0x01, data[1] = 0x73 (analog), buttons ACTIVE-LOW: 0 = pressed, 0xFFFF = none")
    print("")
    print("  %-9s %-7s %-8s %-4s %-32s %s" % ("button", "mask", "byte", "bit", "raylib (default)", "keyboard"))
    for r in rows:
        print("  %-9s %-7s %-8s %-4d %-32s %s" % (r["button"], r["mask"], r["byte"], r["bit"],
                                                 r["raylib"], r["keyboard"]))
    print("")
    print("  sticks: data[4] RX, data[5] RY, data[6] LX, data[7] LY; 0x80 centre,")
    print("          0x00 = full left/up, 0xFF = full right/down")


def print_presets(names, as_json):
    out = {}
    for name in names:
        p = PRESETS[name]
        out[name] = {
            "title": p["title"], "notes": p["notes"], "gamepad": p["gamepad"],
            "buttons": {b: [{"raylib": s, "label": p["labels"].get(s, s)} for s in p["buttons"][b]]
                        for b in DS2_ORDER},
        }
    if as_json:
        out_kb = {"keyboard": KEYBOARD_DEFAULT}
        out_kb.update(out)
        print(json.dumps(out_kb, indent=2))
        return
    for name in names:
        p = PRESETS[name]
        print("%s: %s" % (name, p["title"]))
        print("  " + p["notes"])
        if p["gamepad"] == "none":
            print("  gamepads ignored; keyboard below")
        else:
            for b in DS2_ORDER:
                srcs = p["buttons"][b]
                print("  %-9s <- %s" % (b, ", ".join("%s (%s)" % (p["labels"].get(s, s), s) for s in srcs)))
        print("")
    print("keyboard (every preset, works next to a pad unless keyboard_always is false):")
    for target, keys in KEYBOARD_DEFAULT.items():
        print("  %-13s <- %s" % (target, ", ".join(keys)))


def starter_config(layout):
    return {
        "$comment": "console-controller mapping. Edit, then: padmap.py check && padmap.py gen",
        "layout": layout,
        "buttons": {},
        "keyboard": {},
        "deadzone": 0.15,
        "invert_y": False,
        "stick_swap": False,
        "trigger_threshold": None,
        "players": 1,
        "keyboard_always": True,
        "touch": [],
        "allow_duplicates": [],
    }


# ---------------------------------------------------------------- commands

def _write_text(path, text):
    folder = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except OSError as exc:
        raise UsageError("cannot write %s: %s" % (path, exc))


def _load_resolved(args):
    if args.config and os.path.exists(args.config):
        config = load_config(args.config)
        source = os.path.basename(args.config)
    elif args.config and args.config != DEFAULT_CONFIG:
        raise UsageError("no such file: %s" % args.config)
    else:
        config = {}
        source = "the %s preset" % (args.layout or "playstation")
    if getattr(args, "layout", None):
        config = dict(config)
        config["layout"] = args.layout
    return resolve(config), source


DEFAULT_CONFIG = os.path.join("console", "controls.json")


def cmd_init(args):
    if os.path.exists(args.out) and not args.force:
        raise UsageError("%s already exists (pass --force to replace it)" % args.out)
    _write_text(args.out, json.dumps(starter_config(args.layout), indent=2) + "\n")
    print("wrote %s (layout %s). Next: padmap.py check --config %s" % (args.out, args.layout, args.out))
    return 0


def cmd_check(args):
    (resolved, errors, warnings), source = _load_resolved(args)
    e2, w2 = validate(resolved)
    errors += e2
    warnings += w2
    script_info = None
    if args.script:
        try:
            with open(args.script, "r", encoding="utf-8") as fh:
                lines = parse_script(fh.read())
            script_info = {"path": args.script, "lines": len(lines),
                           "last_frame": lines[-1][0] if lines else None}
        except FileNotFoundError:
            raise UsageError("no such file: %s" % args.script)
        except (OSError, UnicodeDecodeError) as exc:
            raise UsageError("cannot read %s: %s" % (args.script, exc))
        except ValueError as exc:
            errors.append("script %s: %s" % (args.script, exc))
    ok = not errors and not (args.strict and warnings)
    if args.json:
        print(json.dumps({"ok": ok, "source": source, "layout": resolved["layout"],
                          "errors": errors, "warnings": warnings, "script": script_info}, indent=2))
    else:
        print("checked %s (layout %s)" % (source, resolved["layout"]))
        for e in errors:
            print("  ERROR   " + e)
        for w_ in warnings:
            print("  warning " + w_)
        if script_info:
            print("  script %s: %d lines, last change at frame %s"
                  % (script_info["path"], script_info["lines"], script_info["last_frame"]))
        print("  OK: every DS2 button is bound" if ok else "  NOT OK")
    return 0 if ok else 1


def cmd_gen(args):
    (resolved, errors, warnings), source = _load_resolved(args)
    e2, w2 = validate(resolved)
    errors += e2
    warnings += w2
    if errors:
        for e in errors:
            print("error: " + e, file=sys.stderr)
        raise UsageError("%s has %d problem(s); fix them (padmap.py check) and run gen again"
                         % (source, len(errors)))
    text = generate_cpp(resolved, replay=args.replay, source=source)
    if args.out == "-":
        sys.stdout.write(text)
        return 0
    _write_text(args.out, text)
    for w_ in warnings:
        print("warning: " + w_)
    print("wrote %s (%d lines, layout %s%s)" % (args.out, text.count("\n"), resolved["layout"],
                                              ", input replay + record" if args.replay else ""))
    print("install: padmap.py install --repo <your PS2Recomp checkout> --from %s" % args.out)
    print("then rebuild incrementally: cmake --build <your build dir>")
    return 0


def _repo_pad_path(repo):
    path = os.path.join(repo, "ps2xRuntime", "src", "lib", "ps2_pad.cpp")
    if not os.path.isfile(path):
        raise UsageError("%s is not a PS2Recomp checkout (no ps2xRuntime/src/lib/ps2_pad.cpp)" % repo)
    return path


def cmd_install(args):
    target = _repo_pad_path(args.repo)
    try:
        with open(args.source, "r", encoding="utf-8") as fh:
            new_text = fh.read()
    except FileNotFoundError:
        raise UsageError("no such file: %s (run padmap.py gen first)" % args.source)
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError("cannot read %s: %s" % (args.source, exc))
    if MARKER not in new_text or "PSPadBackend::readState" not in new_text:
        raise UsageError("%s was not written by padmap.py gen; refusing to install it" % args.source)
    with open(target, "r", encoding="utf-8", errors="replace") as fh:
        current = fh.read()
    if "PSPadBackend::readState" not in current:
        raise UsageError("%s does not define PSPadBackend::readState; this checkout's pad layer "
                         "changed, read it before replacing it" % target)
    os.makedirs(args.backups, exist_ok=True)
    orig = os.path.join(args.backups, "ps2_pad.cpp.orig")
    if MARKER not in current and not os.path.exists(orig):
        shutil.copy2(target, orig)
        print("backed up the stock file to %s" % orig)
    else:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bak = os.path.join(args.backups, "ps2_pad.cpp.%s.bak" % stamp)
        shutil.copy2(target, bak)
        print("backed up the current file to %s" % bak)
    if current == new_text:
        print("%s is already this mapping; nothing to do" % target)
        return 0
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(new_text)
    print("installed %s -> %s" % (args.source, target))
    print("rebuild incrementally: cmake --build <your build dir> (one .cpp, no headers changed)")
    return 0


def cmd_restore(args):
    target = _repo_pad_path(args.repo)
    orig = os.path.join(args.backups, "ps2_pad.cpp.orig")
    if not os.path.isfile(orig):
        raise UsageError("no backup at %s" % orig)
    shutil.copy2(orig, target)
    print("restored the stock ps2_pad.cpp from %s" % orig)
    return 0


def cmd_presets(args):
    names = [args.name] if args.name else LAYOUTS
    print_presets(names, args.json)
    return 0


def cmd_table(args):
    print_table(args.json)
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="padmap.py",
        description=("Controller mapping for a PS2Recomp port: controls.json in, a drop-in "
                     "ps2xRuntime/src/lib/ps2_pad.cpp out (raylib, DualShock 2 report)."),
        epilog=("Typical: padmap.py init --layout xbox; edit console/controls.json; "
                "padmap.py check; padmap.py gen --replay; "
                "padmap.py install --repo ~/PS2Recomp"))
    sub = p.add_subparsers(dest="cmd", metavar="command")

    def common(sp, layout=True):
        sp.add_argument("--config", default=DEFAULT_CONFIG,
                        help="mapping file (default console/controls.json; a preset is used if it is missing)")
        if layout:
            sp.add_argument("--layout", choices=LAYOUTS, help="override the layout preset")
        sp.add_argument("--json", action="store_true", help="machine-readable output")

    sp = sub.add_parser("init", help="write a starter console/controls.json from a preset")
    sp.add_argument("--layout", choices=LAYOUTS, default="playstation")
    sp.add_argument("--out", default=DEFAULT_CONFIG)
    sp.add_argument("--force", action="store_true", help="replace an existing file")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("check", help="validate a mapping (every DS2 button bound, no clashes, ranges)")
    common(sp)
    sp.add_argument("--script", help="also validate an input replay script")
    sp.add_argument("--strict", action="store_true", help="warnings fail the check too")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("gen", help="write the drop-in ps2_pad.cpp")
    common(sp)
    sp.add_argument("--out", default=os.path.join("console", "ps2_pad.cpp"),
                    help="output file (default console/ps2_pad.cpp, - for stdout)")
    sp.add_argument("--replay", action="store_true",
                    help="compile in input replay (PS2X_INPUT_SCRIPT) and recording (PS2X_INPUT_RECORD)")
    sp.set_defaults(func=cmd_gen)

    sp = sub.add_parser("install", help="back up the checkout's ps2_pad.cpp and copy the generated one over it")
    sp.add_argument("--repo", required=True, help="your PS2Recomp checkout (explicit, never guessed)")
    sp.add_argument("--from", dest="source", default=os.path.join("console", "ps2_pad.cpp"))
    sp.add_argument("--backups", default=os.path.join("console", "backups"))
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("restore", help="put the stock ps2_pad.cpp back from the backup")
    sp.add_argument("--repo", required=True)
    sp.add_argument("--backups", default=os.path.join("console", "backups"))
    sp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("presets", help="print the playstation, xbox, switch and keyboard presets")
    sp.add_argument("name", nargs="?", choices=LAYOUTS)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_presets)

    sp = sub.add_parser("table", help="print the DualShock 2 button bit table")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_table)
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
