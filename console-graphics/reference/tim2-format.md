# The TIM2 texture format (.tm2)

TIM2 is the PS2 SDK's standard texture container. `tim2.py` reads and writes
the parts below. All numbers are little-endian.

## File header (16 bytes)

| offset | size | field |
| --- | --- | --- |
| 0x00 | 4 | magic `TIM2` |
| 0x04 | 1 | format version (3 and 4 are common) |
| 0x05 | 1 | alignment id: 0 = data aligned to 16 bytes, 1 = aligned to 128 (the first picture then starts at 0x80) |
| 0x06 | 2 | picture count |
| 0x08 | 8 | reserved |

## Picture header (48 bytes, then optional extra header)

| offset | size | field |
| --- | --- | --- |
| 0x00 | 4 | total size of this picture (header + image + palette): the next picture starts here plus this |
| 0x04 | 4 | palette (CLUT) size in bytes |
| 0x08 | 4 | image size in bytes (all mip levels) |
| 0x0C | 2 | header size (48 plus the mipmap and user headers) |
| 0x0E | 2 | palette colour count (more than 16 or 256 = several palettes) |
| 0x10 | 1 | picture format (0) |
| 0x11 | 1 | mipmap count (1 = no extra levels) |
| 0x12 | 1 | palette type: low bits = colour format (1 = 16-bit, 2 = 24-bit, 3 = 32-bit); 0x80 and 0x40 = storage order flags |
| 0x13 | 1 | image type: 1 = 16-bit, 2 = 24-bit, 3 = 32-bit, 4 = 4-bit indexed, 5 = 8-bit indexed |
| 0x14 | 2 | width |
| 0x16 | 2 | height |
| 0x18 | 8 | GS TEX0 register value |
| 0x20 | 8 | GS TEX1 register value |
| 0x28 | 4 | GS TEXA / FBA / PABE bits |
| 0x2C | 4 | GS TEXCLUT register value |

With mipmaps, the extra header starts with the MIPTBP1 and MIPTBP2 register
values (8 bytes each) followed by one 32-bit byte size per level. The image
data holds the levels one after another, largest first.

`tim2.py` never changes a header: a rebuilt texture has the same size, format,
palette size and GS registers, so the game's upload code still fits it.

## Pixels

| type | per pixel | stored as |
| --- | --- | --- |
| 32-bit | 4 bytes | R, G, B, A. A runs 0 to 0x80, where 0x80 is fully opaque (values above 0x80 brighten when blending) |
| 24-bit | 3 bytes | R, G, B (no alpha) |
| 16-bit | 2 bytes | RGBA5551: red bits 0-4, green 5-9, blue 10-14, alpha bit 15 |
| 8-bit | 1 byte | palette index |
| 4-bit | half a byte | palette index, the left pixel in the LOW nibble |

Rows run top to bottom. `tim2.py` converts PS2 alpha to PNG alpha as
`a * 255 / 128` (0x80 becomes 0xFF) and back as `a * 128 / 255`; every value
from 0 to 0x80 survives the round trip. A 16-bit texture whose alpha bit is
never set is shown opaque (the game relies on the GS TEXA register for its
alpha), and new pixels keep the bit clear.

## Palettes and the CSM1 swizzle

The GS reads a 256-colour palette in a block layout ("CSM1"), so TIM2 files
usually store 8-bit palettes reordered: within every 32 entries, entries 8 to
15 and 16 to 23 are swapped. The swap is its own inverse.

| palette type flags | 8-bit images | 4-bit images |
| --- | --- | --- |
| no flag | stored swizzled | stored in order |
| 0x80 | stored in order (CSM2) | stored in order |
| 0x40 | | stored swizzled |

`tim2.py` undoes the swizzle on the way out and redoes it on the way in, so the
PNG's palette is in plain order.

## Rebuilding rules (`frompng`)

- Same width and height, or it refuses.
- Pixels you did not touch keep their exact original bytes (and indices).
- Indexed: colours already in the palette reuse their entries; new colours go
  into palette entries no pixel uses; more colours than the palette holds
  needs `--quantize` (median cut, weighted by pixel counts).
- Several palettes: only `--palette N` is rewritten.
- 24-bit has no alpha: transparency in the PNG is dropped (with a note).
- 16-bit and 24/32-bit palettes store 5 or 8 bits per channel: 16-bit colours
  round to the nearest of 32 levels.

## Inside archives

`tim2.py scan` reads big files in pieces and checks each `TIM2` magic it finds
(alignment id, picture headers, sizes inside the file) before reporting it, so
random bytes that spell TIM2 are skipped. `insert` refuses a replacement that
is not exactly the same size, or whose pictures differ in size or format.
Compressed archives hide their textures from `scan`: unpack them first with
`/console-levels`.
