# PS2 memory card images, .psu files, and the port's folder card

Facts in our own words. The image layout matches mymc (Ross Ridge's memory card tool, as kept in
github.com/ps2dev/mymc). The folder card behaviour was read from PS2Recomp (commit eb8688b):
`ps2xRuntime/src/lib/Kernel/Stubs/MemoryCard.cpp`, `ps2xRuntime/src/lib/ps2_runtime.cpp`
(`configureIoPathsFromElf`) and `ps2xIOP/src/modules/mcserv.cpp`.

## Image size and ECC

A standard card is 8 MB: 16,384 pages of 512 bytes, 2 pages per 1 KB cluster, 16 pages per erase block.

| file | bytes | page on disk |
| --- | --- | --- |
| no ECC (raw dumps, many `.mcd`) | 8,388,608 | 512 |
| with ECC (PCSX2 `.ps2`, many dumps) | 8,650,752 | 512 data + 16 spare (ECC) |

`memcard.py` reads the geometry from the superblock and accepts any card whose file size matches
`pages x page size` or `pages x (page size + spare)`, where spare is 4 bytes per 128 bytes of page. So
16, 32 and 64 MB cards made by PCSX2 read too. Any other size is refused as truncated. ECC bytes are
skipped, not checked.

## Superblock (page 0, little-endian)

| offset | field | standard 8 MB value |
| --- | --- | --- |
| 0x00 | magic, 28 bytes | `Sony PS2 Memory Card Format ` |
| 0x1C | version, 12 bytes | `1.2.0.0` (also 1.1, 1.0) |
| 0x28 | page_len u16 | 512 |
| 0x2A | pages_per_cluster u16 | 2 |
| 0x2C | pages_per_block u16 | 16 |
| 0x2E | u16 | 0xFF00 |
| 0x30 | clusters_per_card u32 | 8192 |
| 0x34 | alloc_offset u32 (first allocatable cluster) | 41 |
| 0x38 | alloc_end u32 (allocatable clusters, counted from alloc_offset) | 8135 |
| 0x3C | rootdir_cluster u32 (relative to alloc_offset) | 0 |
| 0x40 | backup_block1 u32 | 1023 |
| 0x44 | backup_block2 u32 | 1022 |
| 0x50 | ifc_list u32[32]: indirect FAT clusters (absolute) | 8, 0, ... |
| 0xD0 | bad_block_list u32[32] | all 0xFFFFFFFF |
| 0x150 | card_type u8 | 2 (PS2) |
| 0x151 | card_flags u8 | varies |

## The FAT

Two levels. FAT entry `n` (for allocatable cluster `n`, relative to alloc_offset) is found like this,
with 256 u32 entries per 1 KB cluster:

```
fat_cluster_index = n // 256
indirect_cluster  = ifc_list[fat_cluster_index // 256]          # absolute cluster number
fat_cluster       = u32 at (fat_cluster_index % 256) in indirect_cluster   # absolute
entry             = u32 at (n % 256) in fat_cluster
```

| entry | meaning |
| --- | --- |
| bit 31 set, low 31 bits = next cluster | allocated, chain continues |
| `0xFFFFFFFF` (bit 31 set, low bits all ones) | allocated, end of chain |
| bit 31 clear (formatted cards use `0x7FFFFFFF`) | free |

Note: the build spec's "0xFFFFFFFF = free" is the other way round; mymc's constants are
`CHAIN_END = 0xFFFFFFFF` and `CHAIN_END_UNALLOC = 0x7FFFFFFF`, and `memcard.py` follows them (only the
free-space count depends on it). Chains are followed with loop and bounds checks. A loop, a jump into a
free cluster or a chain shorter than its file means "the card is damaged": `export` skips that save
with a warning, writes nothing for it, copies the healthy ones and exits 1; damaged directories stop
`ls` and `export` with exit 2.

## Directory entries (512 bytes, `<HHL8sLL8sL28x448s`)

| field | notes |
| --- | --- |
| mode u16 | bits below |
| unused u16 | |
| length u32 | folders: entry count including `.` and `..`; files: bytes |
| created, 8 bytes | `<xBBBBBH`: unused, sec, min, hour, day, month, year |
| cluster u32 | first cluster of the data (relative to alloc_offset) |
| dir_entry u32 | index of this folder's entry in its parent |
| modified, 8 bytes | as created |
| attr u32 | |
| 28 bytes padding | |
| name | NUL-terminated within 32 bytes, the rest of the 448 bytes zero |

Every folder starts with `.` and `..`. Deleted entries keep their slot with the exists bit cleared and
are skipped. Card times are the PS2 clock, which runs on Japan time (UTC+9); `memcard.py` shows them as
stored and converts with +9 hours when it sets host file times (mymc does the same).

| mode bit | name (ps2sdk / mymc) |
| --- | --- |
| 0x0001 0x0002 0x0004 | read, write, execute |
| 0x0008 | copy protected |
| 0x0010 | file |
| 0x0020 | folder |
| 0x0080 | closed (the runtime calls it `kMcAttrClosed`) |
| 0x0400 | set on everything the PS2 creates; meaning not documented (ps2sdk calls it `sceMcFile0400`, unverified) |
| 0x0800 | PocketStation |
| 0x1000 | PS1 save |
| 0x2000 | hidden (the root's `..`) |
| 0x8000 | exists |

A folder is typically `0x8427`, a file `0x8497`.

## icon.sys (the save's title)

Starts with `PS2D`. The u16 at 0x06 is the byte offset where the second title line starts; the title is
68 bytes of Shift-JIS at 0xC0. `ls` decodes it and turns full-width letters into normal ones. The file
is 964 bytes (0x3C4).

## Save folder names

`B` + region letter + serial + game-chosen suffix: `BASLUS-20312GAME`. `A` = USA, `E` = Europe, `I` =
Japan and Asia. `ls` shows the serial (`SLUS-20312`) so you can match it with the disc.

## .psu (one save, the EMS / uLaunchELF format)

```
512-byte entry for the folder   (mode 0x8427, length = files + 2, cluster 0)
512-byte entry "."              (mode 0x8427, length 0)
512-byte entry ".."             (mode 0x8427, length 0)
for each file:
  512-byte entry                (mode from the card or 0x8497, length = bytes, cluster 0)
  the data, padded with zeros to a multiple of 1024
```

No subfolders. `psu import` refuses truncated files, subfolders, unsafe names (`/`, `\`, `:`, `..`,
control characters, longer than 31 bytes) and two files whose names differ only in case.

## The port's folder memory card

- `mcRoot` defaults to `<elf dir>/mc0` (`PS2Runtime::configureIoPathsFromElf`). Port 1 is the sibling
  `mc1` folder (`getMcRootPath`). Only slot 0 of each port exists.
- A guest path `mc0:/BASLUS-20312GAME/DATA` maps to `<mcRoot>/BASLUS-20312GAME/DATA`, case kept as the
  game wrote it (`normalizeGuestMcPathLocked`, `guestMcPathToHostPath`). `fio` calls with an `mc0:`
  prefix go to the same root.
- `sceMcGetDir` lists every host file in the folder, sorted case-insensitively, plus `.` and `..`.
  That is why `memcard.py` keeps its own files (`.console-sync.json`, conflict copies) out of mc0.
- `sceMcGetInfo` always reports a formatted PS2 card with 0x2000 free clusters (8 MB).
- `sceMcFormat` and `sceMcUnformat` delete the whole folder (`std::filesystem::remove_all`) and
  recreate it empty. Back up first.
- Games that talk to MCSERV over SIF RPC instead of the libmc stubs are answered by
  `ps2xIOP/src/modules/mcserv.cpp`, which forwards to the same host memory card.
