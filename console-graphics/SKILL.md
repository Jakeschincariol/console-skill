---
name: console-graphics
description: >-
  Graphics Artist. Rebuilds the graphics: dumps every texture, lets you repaint
  or replace them with your own art, finds the widescreen fix. Converts PS2
  TIM2 textures (.tm2, 32/24/16-bit and 8/4-bit indexed) from the user's own
  extracted disc to PNG, rebuilds them from edited PNGs at the same size and
  format (GS registers kept, palettes refit or quantised), finds TIM2 blobs
  inside archives and puts edited ones back, and lists the 4:3 aspect and FOV
  constants in the ELF with 16:9 pnach lines to try in PCSX2. Reads the
  packets the game sends the GS from a RAM dump (DMA chains, VIF codes, GIF
  tags, every register write) and flags what breaks rendering. Writes to
  console/textures/ and console/patches/, never into the disc image. Use when
  the user says "dump the textures", "repaint", "replace this texture", "my own
  art", "HD textures", "make it widescreen", "16:9", "the picture is
  stretched", "black screen", "garbage textures", "nothing draws", or "how
  does the port draw".
argument-hint: "tim2.py <info|topng|frompng|scan|insert> | widescreen.py <scan|values> | gspacket.py <dma|vif|gif> [flags]"
---

# console-graphics

The Graphics Artist works on what the player sees. Textures: find them on the
extracted disc (loose `.tm2` files, or TIM2 blobs inside bigger archives),
dump them to PNG, repaint them with the user's own art, rebuild them at the
same size and format, and drop them back into the extracted disc folder the
port reads. Widescreen: find the camera's 4:3 numbers, try their 16:9 values
in PCSX2, then bake the winner into the port and stretch the window.

Be plain about the limits. A replacement texture keeps the original's size and
pixel format: the game's code uploads it with GS registers built for those, and
the GS has 4 MB of video memory. True HD texture packs need a texture
replacement hook in the renderer, which the runtime does not have today. Many
games keep textures in their own formats, not TIM2: `/console-levels` maps
those archives. Widescreen is a candidate hunt, not a lookup.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/tim2.py" info  console/disc/DATA/TITLE.TM2                      # pictures, formats, GS TEX0
python3 "${CLAUDE_SKILL_DIR}/tim2.py" topng console/disc/DATA --out console/textures/png     # every .tm2 under a folder
python3 "${CLAUDE_SKILL_DIR}/tim2.py" frompng console/disc/DATA/TITLE.TM2 my_title.png --out console/textures/new/TITLE.TM2
python3 "${CLAUDE_SKILL_DIR}/tim2.py" scan  console/disc/DATA/PACK.BIN --extract console/textures/found
python3 "${CLAUDE_SKILL_DIR}/tim2.py" insert console/disc/DATA/PACK.BIN --offset 0x1A2000 --tm2 new.tm2 --out console/textures/new/PACK.BIN
python3 "${CLAUDE_SKILL_DIR}/widescreen.py" scan SLUS_203.12 --out console/patches/widescreen-try.pnach
python3 "${CLAUDE_SKILL_DIR}/widescreen.py" values --fov 60                           # 4:3 -> 16:9 table, Hor+ FOV
python3 "${CLAUDE_SKILL_DIR}/gspacket.py" dma console/dumps/ram.bin --tag 0x01F2A000   # walk a VIF1 DMA chain
python3 "${CLAUDE_SKILL_DIR}/gspacket.py" gif console/dumps/ram.bin --addr 0x00403C10 --length 4096
```

Other flags: `topng --rgba` (RGBA instead of palette PNGs), `--palette N`,
`--all-palettes`, `--mips`; `frompng --quantize` (median cut when the art has
too many colours), `--picture N`, `--keep-mips`, `--force`; `--json` on `info`,
`scan` and `widescreen.py`.

## What it reads and writes

| file | what |
| --- | --- |
| the extracted disc folder (next to the ELF) | read: `.tm2` files and archives that hold TIM2 blobs |
| `console/textures/png/` | the dumped PNGs: the user's working copies, never published |
| `console/textures/new/` | rebuilt `.tm2` files and archives, same size as the originals |
| the game's main ELF | read by `widescreen.py` |
| `console/patches/widescreen-try.pnach` | one `[section]` per widescreen candidate, to toggle in PCSX2 |
| `console/dumps/*.bin` | raw guest RAM dumps that `gspacket.py` reads (game data: never published) |

## How the port draws

- The game's own code builds GIF packets and DMA chains as on a PS2.
  `ps2xRuntime/src/lib/gs/gs_frontend.cpp` (class `GS`) takes the packets and
  register writes, `gs/ps2_gif_arbiter.cpp` orders the GIF paths,
  `gs/ps2_gs_memory.cpp` holds the 4 MB of GS memory, and
  `gs/gs_cpu_backend.cpp` (`GSCpuBackend`) rasterises sprites, triangles and
  lines in software, sampling textures and CLUTs from that memory.
- Each frame, `ps2_runtime.cpp` copies the displayed frame (640x448 by
  default) into a raylib texture and draws it scaled to fit the window,
  keeping its shape (`DrawTexturePro`).
- Desktop builds have a debug panel (`ps2_debug_panel.cpp`, toggled with F1):
  the GS tab shows the GS registers and draw history, the File/CD tab shows
  which disc files the game has open, which is how you learn which file a
  texture comes from.

## Textures: the method

1. **Find them.** `tim2.py info` on loose `.tm2` files. For archives,
   `tim2.py scan <file> --extract DIR` finds TIM2 blobs and their offsets. No
   hits: the game uses its own format; watch the File/CD tab when the texture
   appears, and hand the file to `/console-levels`.
2. **Dump.** `tim2.py topng <file or folder> --out console/textures/png`.
   Indexed textures become palette PNGs (exact indices kept); the PS2's alpha
   (0x80 = opaque) becomes 0xFF; CSM1 palette swizzle is undone.
3. **Repaint** with the user's own art at the SAME width and height. Keep an
   indexed texture within its palette size (16 or 256 colours) in the editor,
   or use `--quantize`. Untouched pixels stay byte-identical.
4. **Rebuild.** `tim2.py frompng <original.tm2> <edited.png> --out ...`. The
   header and GS registers are copied unchanged; mip levels are regenerated
   from the new art (or kept with `--keep-mips`). For a blob in an archive:
   `tim2.py insert` (same offset, same size; never in place without `--force`).
5. **Drop it in.** Back up the original, then put the new file in the
   extracted disc folder next to the ELF (the runtime's `cdRoot`): no ISO
   rebuild. Boot the port (`/console-tester`) and look.

Format details (headers, CLUT swizzle, alpha, mipmaps): [reference/tim2-format.md](reference/tim2-format.md).

## Widescreen: the method

1. `widescreen.py scan <ELF> --out console/patches/widescreen-try.pnach`
   lists every 4:3 constant (1.3333334, 0.75, 640/448, 512/448 ...) in data and
   in `lui`/`ori`/`mtc1` code, each with the 16:9 value (1.7777778, 0.5625,
   x4/3 or x3/4) and its pnach line(s). It cannot know which is the camera.
2. In PCSX2, copy the sections into the game's pnach (`/console-mod`
   `pnach.py crc` names it) and enable ONE at a time. The right one widens the
   view (more on the sides) instead of stretching it. Some games need two
   (projection and culling); menus and HUDs may need their own.
3. Bake the winner in with `/console-mod` (`pnach.py toml` for code, `pnach.py
   override` for data).
4. Stretch the port's output to 16:9: the presenter in `ps2_runtime.cpp`
   keeps the frame's shape, so the window must be told to show it 16:9 wide.
   Exact steps and the FOV rules: [reference/widescreen.md](reference/widescreen.md).

## Black screen or garbage: read the packets

When the port runs but draws nothing, or draws noise, look at what the game
actually sends before touching the renderer.

1. **Get a RAM dump** of the moment it goes wrong: 32 MB of RDRAM from guest
   address 0 (no `--base` needed). From the port, write
   `runtime.memory().getRDRAM()` to `console/dumps/ram.bin` from a game
   override (`/console-mod` `override.py new --wrap` on the function that kicks
   the DMA); from PCSX2, its debugger's memory tools or PCSX2-MCP's memory read.
   A smaller dump works too: pass its start address with `--base`.
2. **Find the chain.** The port's debug panel (F1), DMA/IO tab, shows
   `VIF1_TADR` and `GIF_TADR`: the address of the next DMA tag. In PCSX2, a
   write breakpoint on `D1_CHCR` (0x10009000) stops where the game starts it.
3. **Walk it.** `gspacket.py dma <dump> --tag <TADR>` lists every tag (cnt,
   next, ref, call, ret, end), decodes the data as a VIF1 stream (UNPACK sizes
   follow the runtime's own interpreter, STCYCL included), and the GIF packets
   inside every DIRECT block. `--channel gif` for PATH3 chains, `--verbose`
   for every GS register write, `--json` to keep it.
4. **Read the issues at the bottom.** A chain that loops or leaves RAM, an
   unknown VIF code (the stream is corrupt or decoded from the wrong place), a
   command cut off mid-stream, an image upload shorter or longer than its
   `BITBLTBUF`/`TRXREG` (a broken texture), a prohibited primitive. Exit 1 means
   at least one error. The `TEX0` and `FRAME` fields it prints say which texture
   and which framebuffer a draw uses.
5. **Then decide the layer.** Dump the same moment in PCSX2 and compare
   (`/console-tester` `abdiff.py mem`). Same packets as PCSX2 but a wrong
   picture: the runtime draws them wrong (`gs/gs_frontend.cpp`,
   `gs/gs_cpu_backend.cpp`, `ps2_vif1_interpreter.cpp`), a `/console-backend`
   fix. Different packets: something upstream (a stub returning the wrong
   value, a missing function) fed the game bad data, so `/console-tester`
   triage first.

This replaces the base skill's `vif_gif_surgeon.py`, which decodes one DMA tag
and its two VIF codes (and swaps MSCALF and MSCNT).

## Guard rails

1. **Bring your own game.** Only a game the user owns, dumped from their own
   disc. Never download or look for ISOs, ELFs, BIOS files or texture packs
   made from other people's dumps, never ask for them.
2. **Nothing from the game gets published.** The game's textures, the dumped
   PNGs and the rebuilt `.tm2` files stay on the user's machine. Share only
   the user's OWN art (and the pnach/override for widescreen), never the
   game's textures, not even edited ones.
3. **Not affiliated with Sony.** PlayStation is a trademark of Sony
   Interactive Entertainment.
4. Never overwrite a disc file without a backup; the tools write new files and
   refuse to overwrite without `--force`. Same size and format, always.

## Hands off to

- `/console-levels` for archives that are not TIM2 and for repacking them.
- `/console-mod` to bake a widescreen or camera fix into the port.
- `/console-physics` for FOV values that are not aspect ratios.
- `/console-tester` to boot the port and check the new art and the 16:9 view.
