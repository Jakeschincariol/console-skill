# controls.json and input scripts

`padmap.py` reads `console/controls.json`. Every key is optional; anything left out comes from the
`layout` preset. Unknown keys are ignored with a warning (`$comment` is allowed).

```json
{
  "$comment": "Xbox pad, inverted camera, keyboard as a fallback",
  "layout": "xbox",
  "buttons": { "L3": "RIGHT_FACE_LEFT", "SQUARE": ["LEFT_THUMB"] },
  "keyboard": { "CROSS": ["SPACE"], "LSTICK_UP": ["W", "UP"], "UP": [] },
  "deadzone": { "left": 0.12, "right": 0.2 },
  "invert_y": true,
  "stick_swap": false,
  "trigger_threshold": 0.4,
  "gamepad": "first",
  "players": 1,
  "keyboard_always": true,
  "touch": [
    { "button": "CROSS", "x": 0.90, "y": 0.80, "r": 0.06 },
    { "stick": "left", "x": 0.15, "y": 0.75, "r": 0.12 }
  ],
  "touch_on_desktop": false,
  "allow_duplicates": ["UP"]
}
```

| key | meaning | values |
| --- | --- | --- |
| `layout` | the preset everything else starts from | `playstation`, `xbox`, `switch`, `switch-labels`, `keyboard` |
| `buttons` | DS2 button to raylib gamepad button(s) | DS2 names below; raylib names with or without `GAMEPAD_BUTTON_`; a string or a list; `[]` unbinds |
| `keyboard` | DS2 button or stick direction to raylib key(s) | key names with or without `KEY_`; stick directions `LSTICK_UP` `LSTICK_DOWN` `LSTICK_LEFT` `LSTICK_RIGHT` and the same for `RSTICK_` |
| `deadzone` | radial deadzone, rescaled so the stick still reaches full travel | 0 to 0.9 (default 0.15), or `{"left": .., "right": ..}` |
| `invert_y` | flip a stick's vertical axis | `false`, `true` (right stick, the usual camera), `"left"`, `"right"`, `"both"` |
| `stick_swap` | swap the controller's left and right sticks | `true` / `false` |
| `trigger_threshold` | read L2 and R2 from the analog trigger axes, pressed at this amount | `null` (use raylib's trigger buttons) or above 0 up to 1 |
| `gamepad` | which raylib pad is player 1 | `"first"` (first connected, default), `"none"` (ignore pads), or 0 to 3 |
| `players` | 2 gives pad port 2 the second connected pad | 1 or 2 |
| `keyboard_always` | keys work while a pad is connected | `true` (default) / `false` (keys only with no pad) |
| `touch` | invisible screen zones, Android builds | buttons: `button`, `x`, `y`, `r`; sticks: `stick` (`left`/`right`), `x`, `y`, `r` |
| `touch_on_desktop` | also compile the touch code on desktop (mouse tests) | `true` / `false` |
| `allow_duplicates` | keys or pad buttons you mean to bind twice | names |

DS2 button names: `UP DOWN LEFT RIGHT CROSS CIRCLE SQUARE TRIANGLE L1 R1 L2 R2 L3 R3 START SELECT`.

raylib gamepad buttons: `LEFT_FACE_UP LEFT_FACE_DOWN LEFT_FACE_LEFT LEFT_FACE_RIGHT` (D-pad),
`RIGHT_FACE_DOWN RIGHT_FACE_RIGHT RIGHT_FACE_LEFT RIGHT_FACE_UP` (face buttons by position),
`LEFT_TRIGGER_1 RIGHT_TRIGGER_1` (bumpers), `LEFT_TRIGGER_2 RIGHT_TRIGGER_2` (triggers),
`LEFT_THUMB RIGHT_THUMB` (stick clicks), `MIDDLE_LEFT MIDDLE_RIGHT` (select / start),
`MIDDLE` (guide button: the OS or Steam often keeps it), plus `LEFT_TRIGGER_AXIS RIGHT_TRIGGER_AXIS`
(analog triggers as buttons at `trigger_threshold`).

Touch geometry: `x` and `y` are fractions of the screen width and height (0,0 is the top left);
`r` is a fraction of the shorter screen side. A stick zone follows a finger within 1.5 x `r` of its
centre and reaches full travel at `r`. Zones are not drawn: tell the player where they are.

## What `check` refuses

- a DS2 button with no gamepad binding (or no key, for the `keyboard` layout)
- one key or pad button bound to two things, unless it is in `allow_duplicates`
- unknown DS2, raylib button or key names; values out of range; `players: 2` with `gamepad: "none"`

Warnings: Escape (raylib quits on it; the runtime never calls `SetExitKey`), F1 (toggles the
debug panel in desktop builds with the debug UI), the guide button.

## Input scripts (replay and record)

Generated with `gen --replay`, the pad reads env `PS2X_INPUT_SCRIPT` once, at the first pad read.
Each line is `frame buttons [lx ly rx ry]`:

- `frame`: the number of times the game has read pad port 0 so far, starting at 0. Most games read
  once per video frame, so 60 is about one second on NTSC. Lines must be in frame order.
- `buttons`: names joined with `+` (`CROSS+UP`), `-` or `none` for nothing, or `0xMASK` of the
  PRESSED DS2 bits (`0x4000` is Cross; the pad flips it to active-low).
- `lx ly rx ry`: report bytes 0 to 255, 128 centre, 0 full left or up. Leave all four out for
  centred sticks.
- A state holds until the next line. `#` starts a comment.

While a script plays, live input on port 0 is ignored, so a run is the same every time. The pad
prints to stderr (which `run.py` captures):

```
[pad-replay] loaded 12 lines from console/inputs/menu.txt
[pad-replay] script finished at frame 1560
```

A bad line prints `[pad-replay] <file> line N is not 'frame buttons [lx ly rx ry]' in frame order;
replay disabled` and the pad falls back to live input. `padmap.py check --script FILE` catches the
same mistakes before a run.

Recording: set env `PS2X_INPUT_RECORD` to a path instead and play; every change on port 0 is written
in the same format (`[pad-record] writing <path>` on stderr). Replay that file to repeat the run.
Start both from a cold boot: a script recorded from the title screen means nothing mid-level.

`templates/menu-walk.txt` is an example to copy. Its frame numbers are made up; record your own.
