# The .pnach format (PCSX2 patches and cheats)

What `pnach.py parse` understands, and how each line ports to PS2Recomp.

## File name and the two CRCs

PCSX2 finds a patch file by the game's serial and a CRC of the main ELF:
`SLUS-20312_1A2B3C4D.pnach` in its `patches/` or `cheats/` folder (PCSX2 2.x).
Older builds used `1A2B3C4D.pnach` alone. `pnach.py crc <ELF>` prints both.

| CRC | how it is computed | who uses it |
| --- | --- | --- |
| PCSX2 CRC | XOR of every little-endian 32-bit word of the ELF file (trailing bytes ignored) | pnach file names, PCSX2 |
| CRC-32 | the standard CRC-32 (poly 0xEDB88320, init and final XOR 0xFFFFFFFF) of the whole ELF file | PS2Recomp game overrides (`PS2_REGISTER_GAME_OVERRIDE`) |

They are different numbers for the same file. A pnach written for another
region or version has a different PCSX2 CRC and its addresses will not line up:
`pnach.py` refuses it unless you pass `--ignore-crc`.

## Line kinds

```
gametitle=Example Game [SLUS-20312] (U)
comment=what this file does
[Infinite health]                       a named group (PCSX2 2.x lets you toggle each one)
author=you
description=Health never goes down
gsaspectratio=16:9                      PCSX2 display settings (not ported: PCSX2-only)
gsinterlacemode=1
// a comment
patch=1,EE,0012A3B4,word,00000000       place,cpu,address,type,value
```

Lines before the first `[section]` are always on. `pnach.py --section NAME`
ports only the groups you name. Unknown `key=value` lines are kept as metadata.
`dpatch=` (pattern-matched dynamic patches) is reported as manual.

### place

| place | PCSX2 applies it | the port |
| --- | --- | --- |
| 0 | once, when the game's ELF starts | code: TOML patch. data: written at load. BSS/heap: first call of the frame hook |
| 1 | continuously, every vsync | code: TOML patch. data/BSS/heap: written every call of the frame hook (data also at load) |
| 2 | both | same as 1 |
| 3 | on load, or when enabled in the UI (newer PCSX2) | same as 0 |

### cpu

`EE` is the main CPU, the one the port recompiles. `IOP` patches change the
I/O processor's memory; the port runs IRX modules in `ps2xIOP`, so they are
listed as manual.

### type

| type | writes | memory order |
| --- | --- | --- |
| byte | 1 byte | |
| short | 2 bytes | little-endian |
| word | 4 bytes | little-endian |
| double | 8 bytes | little-endian |
| beshort, beword, bedouble | 2, 4, 8 bytes | big-endian: the value's bytes appear in memory in the order written |
| bytes | any number of bytes (newer PCSX2) | the hex string in memory order |
| extended | CodeBreaker-style code (below) | |

Addresses are EE addresses. Mirrors normalise to the same RAM byte: `0x20xxxxxx`
and `0x30xxxxxx` (uncached), `0x80xxxxxx` and `0xA0xxxxxx` (KSEG0/1). The
scratchpad (`0x70000000`, 16 KB), hardware registers (`0x10000000`, `0x12000000`)
and the BIOS are not main RAM: OUTSIDE. So is the EE kernel's RAM below `0x00080000`:
the port's kernel is high-level emulation, nothing of the PS2 kernel lives there.

## Extended (CodeBreaker-style) codes

With type `extended`, the top hex digit of the address is the command and the
rest (masked to 28 bits) is the address. The exact forms of the non-write commands vary a
little between CodeBreaker versions; the port treats all of them as manual.

| command | form | meaning | ported |
| --- | --- | --- | --- |
| 0 | `0aaaaaaa 000000vv` | 8-bit write | yes, as a plain write |
| 1 | `1aaaaaaa 0000vvvv` | 16-bit write | yes |
| 2 | `2aaaaaaa vvvvvvvv` | 32-bit write | yes |
| 3 | `3000nnvv aaaaaaaa` and friends | increment or decrement (30400000/30500000 take a second line) | manual |
| 4 | `4aaaaaaa nnnnssss` + `vvvvvvvv iiiiiiii` | serial write: n words, step s, value step i | manual |
| 5 | `5sssssss nnnnnnnn` + `0ddddddd 00000000` | copy n bytes | manual |
| 6 | `6aaaaaaa vvvvvvvv` + `000tnnnn iiiiiiii` + more offset lines | write through a pointer chain | manual |
| 7 | `7aaaaaaa 00t0vvvv` | bitwise OR/AND/XOR | manual |
| C | `Caaaaaaa vvvvvvvv` | run every following code only if the word equals v | manual, and gates everything after it |
| D | `Daaaaaaa 00t0vvvv` | 16-bit test, gates the next code | manual, gates one line |
| E | `Ezyyvvvv taaaaaaa` | test, gates the next yy codes | manual, gates yy lines |
| others | | master codes, hooks, timers | manual |

Multi-line codes consume their extra lines: a second line such as
`00000005 00000001` is data for the code above it, never an 8-bit write.
Writes gated by a conditional are not ported as unconditional writes.

How to port the manual ones: a conditional or a counter is a few lines of C++
in a frame hook (read with `Ps2FastRead32`, decide, write with
`Ps2FastWrite32`); a pointer write follows the pointer each frame. Write them in
an `override.py new` module, or extend the module `pnach.py override` wrote.

## What PCSX2 does that the port cannot copy blindly

- PCSX2 patches memory. The port runs recompiled C++: changing an instruction
  word in RDRAM does nothing, it must go through `ps2_recomp` as a TOML patch.
- Code caves (new code written into free memory and jumped to) have no
  recompiled function. Rewrite the cave's logic in C++ (`override.py new
  --replace` or `--wrap`).
- `gsaspectratio`, `gsinterlacemode` and other PCSX2 display keys change
  PCSX2's renderer, not the game. For widescreen in the port see
  `/console-graphics`.
