# Float hunting on the PS2

## float32 in one minute

A float32 is 1 sign bit, 8 exponent bits and 23 fraction bits, stored
little-endian in memory. Handy encodings:

| value | bits | value | bits |
| --- | --- | --- | --- |
| 1.0 | 0x3F800000 | 0.5 | 0x3F000000 |
| 2.0 | 0x40000000 | 10.0 | 0x41200000 |
| 9.8 | 0x411CCCCD | -9.8 | 0xC11CCCCD |
| 12.5 | 0x41480000 | 100.0 | 0x42C80000 |
| 30.0 | 0x41F00000 | 60.0 | 0x42700000 |
| 1/30 | 0x3D088889 | 1/60 | 0x3C888889 |
| 1/50 | 0x3CA3D70A | 1/25 | 0x3D23D70A |
| 0.75 | 0x3F400000 | 4/3 | 0x3FAAAAAB |
| pi | 0x40490FDB | pi/180 | 0x3C8EFA35 |

Values whose low 16 bits are zero (1.0, 0.5, 12.5, 60.0) fit in a single
`lui`. The compiler builds them that way, so they never appear as data.

## How compiled code loads a float

```
lui   $v0, 0x411C           # high half
ori   $v0, $v0, 0xCCCD      # low half (or addiu: then the high half carries +1 when bit 15 is set)
mtc1  $v0, $f12             # into the FPU
```

```
lui   $at, %hi(constant)    # address of a constant in .rodata
lwc1  $f0, %lo(constant)($at)
```

```
lwc1  $f0, -0x7F30($gp)     # small constants (.lit4, .sdata) are $gp-relative
```

`tune.py` decodes exactly these shapes. It finds `$gp` from the startup code
(`lui $gp` + `addiu`/`ori $gp` near the entry) and maps every `$gp`-relative
`lwc1`/`lw` to the word it reads, so a DATA hit lists its readers.

Patching rules:

- A data constant read by many functions changes all of them. To change one
  caller only, patch that caller's code (load a different constant, or build
  the value with `lui`/`ori`), or replace the function (`/console-mod`).
- A lone `lui` cannot hold a value with a non-zero low half. `tune.py patch`
  rounds to the nearest value it can hold and says so. For the exact value,
  a function override is the clean way.
- A `lui`/`ori` pair can hold any float32.

## Units and scale

The number a game shows is rarely the number it stores.

| shown | stored as, often |
| --- | --- |
| km/h, mph | metres (or units) per frame: 120 km/h at 60 fps is 0.556 per frame |
| gravity 9.8 | per frame squared (9.8 / 3600 = 0.00272), or in cm (980) |
| degrees | radians (multiply by pi/180) |
| seconds | frames (60 or 50 per second) |
| percentages | 0 to 1 |

Try the obvious conversions with `--tol` before giving up on the scan.

## PCSX2 while playing (the reliable route)

1. PCSX2 debugger, Memory Search: search for the value as a float (or an
   unknown value), do something in game that changes it, search for the
   changed value; repeat until a few addresses remain.
2. Set a write breakpoint on the address: the instruction that stops there is
   the game's update code. Its constants (the ones `tune.py scan` finds next
   to it) are what you tune.
3. Optional, with the PCSX2-MCP server installed (see `/console-reverse`):
   `pcsx2_memory_diff` snapshots and diffs RAM, `pcsx2_set_watchpoint` finds
   the writer.
4. Values the game computes at run time live in BSS or the heap. To force one,
   write it every frame from a `/console-mod` frame hook; to change how it is
   computed, patch the code that writes it.

## Ghidra

- On a DATA hit: Go To the address, read the references (xrefs), open each
  reading function. A name like `UpdateVehicle` or a loop over objects tells
  you whose value it is.
- On a CODE hit: the function containing the `mtc1` is where the value is
  used. Read what it does with `$fN`.
- With GhydraMCP (optional), ask for xrefs and decompilation of the function
  at the address `tune.py` printed.

## Frame rate

`tune.py fps` finds 1/60, 1/30, 60, 30, 1/50, 1/25, 50 and 25 within 0.5%.
What actually sets the frame rate is usually one of:

- a vsync wait count in the main loop (an integer: 1 = 60 fps, 2 = 30 fps);
- a fixed time step (1/30) fed to every update: changing it alone speeds
  the game up;
- both.

Doubling the frame rate is only correct when every per-frame update scales by
the time step. Test each candidate in PCSX2 with the game's speed in view (a
timer, a cutscene with audio) before baking anything in.
