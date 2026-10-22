# Widescreen for a PS2Recomp port

A 4:3 game becomes 16:9 in two places: the game's camera (it must render a
wider view) and the window (it must show the frame 16:9 wide). PCSX2 does the
second part for you with `gsaspectratio=16:9`; the port needs both.

## 1. The camera

Games build their projection from an aspect ratio or a field of view. The
usual constants:

| 4:3 value | float bits | 16:9 value | bits | rule |
| --- | --- | --- | --- | --- |
| 1.3333334 (4/3) | 0x3FAAAAAB | 1.7777778 | 0x3FE38E39 | width/height: x4/3 |
| 0.75 (3/4) | 0x3F400000 | 0.5625 | 0x3F100000 | height/width: x3/4 |
| 1.4285715 (640/448) | 0x3FB6DB6E | 1.9047619 | | x4/3 |
| 1.1428572 (512/448) | 0x3F924925 | 1.5238096 | | x4/3 |
| 1.25 (640/512, PAL) | 0x3FA00000 | 1.6666666 | | x4/3 |

`widescreen.py values` prints the full table. Field of view:

- a horizontal projection scale (the x focal length, often near 1.0 to 2.0):
  multiply by 0.75 to see more;
- a vertical scale or a vertical FOV: leave it (Hor+ widescreen keeps the
  height and adds to the sides);
- a horizontal FOV angle `a`: the 16:9 angle is `2 * atan(tan(a / 2) * 4/3)`
  (`widescreen.py values --fov 60` prints 75.18 degrees).

`widescreen.py scan` finds the aspect constants in data (with the instructions
that read them) and built in code (`lui`, `ori` or `addiu`, `mtc1`). It
cannot tell which one is the camera: many games use 0.75 for other things.
For FOV values that are not aspect ratios, use `/console-physics` `tune.py
scan` with the number, or find the projection setup in Ghidra (the function
that writes a 4x4 matrix with the aspect in it, usually called once per frame
or per camera change).

## 2. Testing in PCSX2

`--out` writes one `[Widescreen try NN: ...]` section per candidate, each with
`gsaspectratio=16:9` and its patch line(s). Copy them into the game's pnach
(named by `/console-mod` `pnach.py crc`) and enable one at a time:

| what you see | meaning |
| --- | --- |
| more of the world on the left and right, nothing stretched | the camera value: keep it |
| everything stretched or squashed | not the camera, or the opposite rule: undo |
| objects pop in and out at the screen edges | the camera is right; a culling value also needs widening |
| menus and HUD stretched | 2D elements use their own scale: often fine, else find their constant too |
| nothing changes | not used in this scene: undo |

## 3. Baking it into the port

1. The working patch lines go through `/console-mod`: `pnach.py toml` for the
   code constants (TOML instruction patches, re-run ps2_recomp), `pnach.py
   override` for data constants (written into RDRAM at load).
2. Stretch the window. `ps2xRuntime/src/lib/ps2_runtime.cpp` draws each frame
   with `DrawTexturePro(frameTex, srcRect, dstRect, ...)` after computing one
   uniform `scale` from the frame size (640x448 by default), so the frame
   keeps its own shape. For 16:9, compute `dstRect` for a 16:9 box instead
   (`dstHeight` from the window height, `dstWidth = dstHeight * 16 / 9`, both
   clamped to the window, centred). That is a `.cpp` edit in the user's
   checkout: back the file up, keep the change small, rebuild incrementally
   (never edit a header). Record it in `console/STATE.md` as a local runtime
   change.
3. Playtest with `/console-tester`, then look at menus, cutscenes and FMVs:
   full-screen movies are 4:3 and will stretch unless their own draw path is
   left at 4:3.

## Honest limits

- Some games bake the aspect into precomputed matrices or tables; there is no
  single constant to change. The candidate list then shows nothing useful, and
  the fix is in the code that builds the matrix (Ghidra, a function override).
- Wider views show things the designers never meant to be seen (missing
  geometry at the edges, culled objects).
- The game still renders at its own resolution; 16:9 widens the view, it does
  not add pixels.
