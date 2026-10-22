# Controllers: why any pad works, and how to connect one

## Why any controller works

The runtime never talks to a controller directly. It calls raylib (`IsGamepadAvailable`,
`IsGamepadButtonDown`, `GetGamepadAxisMovement`, `IsKeyDown`), and raylib turns whatever is
plugged in into one standard layout:

- **Desktop (Windows, macOS, Linux, Steam Deck):** raylib uses GLFW, which recognises controllers
  through the SDL_GameControllerDB mapping list. DualSense, DualShock 4, Xbox pads, Switch Pro and
  most known third-party pads arrive as the same standard pad: a D-pad, four face buttons by
  position, two bumpers, two triggers, two sticks, two stick clicks, select, start, guide.
- **Steam Deck and Steam:** Steam Input presents the Deck's own controls (and any pad Steam knows)
  as an Xbox-style pad, so the `xbox` preset is the natural one there.
- **Android:** raylib's Android backend reads Bluetooth and USB pads through Android's input events.
- **PS Vita:** the Vita build uses Quenom's SDL2-based raylib 5.5 Vita port (see `vita/setup.sh`),
  and its gamepad layer is what reports the Vita's own buttons and sticks. Keep `gamepad: "first"`
  there so the pad index that port uses does not matter, and test every button on the device.

A pad that is missing from the mapping list shows up with odd buttons or not at all. The easy fix
is Steam Input (add the port as a non-Steam game) or a newer driver. Adding a mapping string inside
the runtime is a code change, not a `controls.json` setting.

## Position versus label

raylib names face buttons by where they sit: `RIGHT_FACE_DOWN` is the bottom one. So by position:

| position | raylib | DualShock 2 | PlayStation pad | Xbox pad | Nintendo pad |
| --- | --- | --- | --- | --- | --- |
| bottom | `RIGHT_FACE_DOWN` | Cross | Cross | A | B |
| right | `RIGHT_FACE_RIGHT` | Circle | Circle | B | A |
| left | `RIGHT_FACE_LEFT` | Square | Square | X | Y |
| top | `RIGHT_FACE_UP` | Triangle | Triangle | Y | X |

Nintendo puts A on the right. The `switch` preset maps by position (Nintendo B is Cross), which keeps
the PS2 feel. `switch-labels` maps by printed letter the way Xbox letters line up (Nintendo A is
Cross, B Circle, X Square, Y Triangle). Some mapping databases already report a Nintendo pad by
label rather than position; if the buttons come out mirrored, switch to the other preset. Test by
pressing the bottom button in a menu: with `switch` it should confirm like Cross.

## Connecting

| pad | USB | Bluetooth |
| --- | --- | --- |
| DualSense / DualShock 4 | plug in a data cable | hold Create (Share on DS4) + PS until the light flashes fast, then pair in the OS settings |
| Xbox Series / One | USB-C or micro-USB cable | models with Bluetooth: hold the pair button until the logo flashes fast. Older ones need Microsoft's wireless adapter on Windows |
| Switch Pro | USB-C cable (some OSes need Steam running for wired use) | hold the small sync button on top until the lights run |
| Steam Deck | built in | built in |

Pair before starting the port: the runtime asks raylib which pads are present on every read, so a
pad that connects later also works, but a game may only check for a controller at boot.

## Two players

`players: 2` gives pad port 2 the second connected pad (in raylib's order) and leaves it released
when there is none. The stock `ps2_pad.cpp` sends the same pad to both ports, which can make a game
think player 2 pressed Start.

## Troubleshooting

| symptom | likely cause | fix |
| --- | --- | --- |
| nothing works, keyboard does | pad not seen by raylib | try USB, check the OS sees it, try Steam Input |
| sticks drift | worn stick | raise `deadzone` (0.2 to 0.25) |
| camera upside down | game or habit | `invert_y: true` |
| L2/R2 fire on a light touch or never | analog trigger behaviour | set `trigger_threshold` (0.3 to 0.6) |
| face buttons mirrored on a Switch pad | label vs position | the other `switch` preset |
| Escape quits the game | raylib's exit key | bind something else (`check` warns) |
| game ignores the sticks | the pad is in digital mode | the game switches modes itself; some games need the analog toggle in their options |
| a pressure-sensitive move fails | pressure is digital here | nothing to map; a game override could change the move (/console-mod) |
