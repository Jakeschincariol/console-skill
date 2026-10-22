# PS2 map: memory, names on the disc, numbers worth knowing

Facts in this pack's own words. For register-level detail read ps2tek by PSI
(https://psi-rockin.github.io/ps2tek/); this pack does not copy it.

## EE address space (the main CPU)

| Range | What | Notes |
| --- | --- | --- |
| `0x00000000` to `0x01FFFFFF` | RDRAM, 32 MB | Games link at `0x00100000` and up; the first MB belongs to the kernel on hardware |
| `0x20100000` to `0x21FFFFFF` | RDRAM, uncached mirror | Same bytes as the address minus `0x20000000` |
| `0x30100000` to `0x31FFFFFF` | RDRAM, uncached accelerated mirror | Same bytes as the address minus `0x30000000` |
| `0x80000000` + offset | KSEG0 view of RDRAM | Mask with `0x1FFFFFFF` for the physical address |
| `0x70000000` to `0x70003FFF` | Scratchpad, 16 KB | Fast on-chip RAM, not part of RDRAM |
| `0x10000000` to `0x1000FFFF` | EE hardware registers | Timers `0x10000000`, IPU `0x10002000`, GIF `0x10003000`, VIF0 `0x10003800`, VIF1 `0x10003C00`, DMAC channels from `0x10008000`, DMAC control `0x1000E000` |
| `0x11000000` to `0x1100FFFF` | VU0 and VU1 micro memory and data memory | |
| `0x12000000` to `0x12001FFF` | GS privileged registers | Display setup, CSR |
| `0x1FC00000` to `0x1FFFFFFF` | Boot ROM (BIOS) | PS2Recomp needs no BIOS: the kernel is reimplemented |

In the runtime: `PS2_RAM_SIZE` is 32 MB and `PS2_RAM_MASK` its mask
(`ps2xRuntime/include/runtime/ps2_memory.h`); the generated function table covers guest code addresses
from `g_ps2RecompiledFunctionTableBase` to `g_ps2RecompiledFunctionTableEnd` (written into the
generated `register_functions.cpp`), and `runtime.replaceFunction(addr, fn)` refuses addresses outside
it. The IOP has its own 2 MB of RAM, separate from EE RAM; ps2xIOP copies data across through SIF.

## Calling convention (what overrides read and write)

| Register | Number | Role |
| --- | --- | --- |
| `a0` to `a3` | 4 to 7 | first four arguments (`getRegU32(ctx, 4)` and so on) |
| `t0` to `t3` | 8 to 11 | arguments five to eight in the EABI the PS2 SDK uses |
| `v0`, `v1` | 2, 3 | return value (`setReturnU32(ctx, v)` / `setReturnS32`); `v1` also carries the syscall number |
| `sp` | 29 | stack pointer |
| `ra` | 31 | return address; the call site is `ra - 8` (the `jal` and its delay slot) |
| `gp` | 28 | small-data base |
| `$f0`, `$f12`.. | FPU | float return and float arguments |

Registers are 128 bits wide on the R5900; most code only uses the low 64 or 32 bits.

## Names on the disc

| Thing | Example | How to read it |
| --- | --- | --- |
| Boot ELF | `SLUS_203.12` | no extension, still an ELF; named after the serial |
| Serial | `SLUS-20312` | the same name with `-` and no dot (`elf.py serial` converts) |
| SYSTEM.CNF | `BOOT2 = cdrom0:\SLUS_203.12;1` | `BOOT2` names the boot ELF; `VER` the disc version; `VMODE` NTSC or PAL. A disc with `BOOT` instead is a PS1 game |
| IOP modules | `MODULES/SIO2MAN.IRX` | run by ps2xIOP, never recompiled |
| `rom0:` modules | `rom0:PADMAN` | BIOS modules; served by the runtime's ROM device and generic services |
| `;1` | `DATA.BIN;1` | ISO 9660 version suffix; ignore it |
| Second programs | `*.ELF`, some `*.BIN` | overlays or extra executables some games load later |

Serial prefixes and regions: `SLUS`/`SCUS` USA, `SLES`/`SCES` Europe, `SLPS`/`SLPM`/`SCPS` Japan,
`SLKA`/`SCKA` Korea, `SCAJ` Asia. `SL..` is a licensed third-party release, `SC..` a Sony release.

## Two CRCs, two jobs

| CRC | How | Used by |
| --- | --- | --- |
| CRC-32 | standard CRC-32 (polynomial `0xEDB88320`, init and final XOR `0xFFFFFFFF`) of the whole ELF file | the runtime's `PS2_REGISTER_GAME_OVERRIDE` match (0 = any) |
| PCSX2 game CRC | XOR of every little-endian 32-bit word of the ELF | PCSX2 patch file names: `<SERIAL>_<CRC>.pnach` |

`elf.py info` and `elf.py crc` print both.

## Sizes and times worth knowing

| Thing | Value |
| --- | --- |
| Generated files for a big game | 30,000 or more (one per function) |
| Full runner rebuild | hours on a big game; the base skill measured about 1 hour with clang-cl + Ninja and most of a day with the Visual Studio generator |
| Incremental rebuild after a runtime `.cpp` edit | seconds to minutes |
| Disc sector | 2048 bytes of data (2352 raw on CD images) |
| DVD-5 / DVD-9 capacity | about 4.7 GB / 8.5 GB |
| Frame rate | NTSC games usually target 60 or 30 fps, PAL 50 or 25 |
