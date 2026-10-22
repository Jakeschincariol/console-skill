# Formats datascan.py knows

## Kinds by magic (`scan`)

| kind | how it is recognised | where it goes next |
| --- | --- | --- |
| `TIM2` | `TIM2` at 0 | /console-graphics `tim2.py` |
| `VAG` | `VAGp` or `VAGi` at 0 | /console-sound `vag.py` |
| `IECS` | `IECSsreV` at 0 (a sound bank `.hd` or a `.sq` sequence) | /console-sound |
| `MPEG-PS` | `00 00 01 BA` (a PSS movie) | leave it |
| `MPEG` | `00 00 01 B3` (an MPEG video stream) | leave it |
| `IPU` | `ipum` (MPEG-2 intra frames for the IPU) | leave it |
| `ELF` | `7F 45 4C 46`, MIPS (machine 8); type 2 = EE executable, type 1 = relocatable (often an overlay) | /console-reverse |
| `IRX` | an ELF of type `0xFF80`, or with a `.iopmod` section: an IOP module | /console-backend |
| `gzip` | `1F 8B 08` | `gzip -d` a copy |
| `zlib` | `78 01`, `78 5E`, `78 9C` or `78 DA` and the first 4 KB inflates | Python `zlib.decompress` a copy |
| `ZIP` | `PK 03 04` | unzip a copy |
| `RIFF` | `RIFF` (the detail says `WAVE`, `AVI ` ...) | |
| `AFS` | `AFS\0` (CRI archive) | `unpack` |
| `CPK` | `CPK ` (CRI CPK) | not unpacked by this tool |
| `icon.sys` | `PS2D` | /console-saves |
| `memcard` | `Sony PS2 Memory Card Format ` | /console-saves |
| `ISO9660` | `CD001` at 0x8001 | /console-reverse `iso.py` |
| `text` | no control bytes in the first 4 KB (`SYSTEM.CNF`, scripts, configs) | read it |
| `data` | anything else: the table-of-contents detector runs on these | `unpack` if a table is found |

Entropy is measured on up to three 64 KB windows (start, middle, end), in bits per byte: under 1
mostly empty, under 6 structured, 6 to 7.5 mixed, over 7.5 compressed, encrypted or ADPCM audio.
`--embedded` also counts `TIM2`, `VAGp`, `IECSsreV`, `AFS\0`, ELF, MPEG-PS and `PS2D` signatures
inside each file, with the first offsets.

## The table-of-contents detector

Many PS2 archives start with a table: a count N, then N entries, then the data. The detector tries
these layouts (all little-endian u32) after a header of 0, 4, 8, 12 or 16 bytes:

| layout | shape | entry size |
| --- | --- | --- |
| `count+pairs` | N, then N x (offset, size) | the size field |
| `count+offsets` | N, then N offsets; optionally an end offset after them (`sentinel`) | next offset minus this one |
| `pairs` | (offset, size) pairs with no count; the first offset says where the table ends | the size field |
| `offsets` | offsets with no count; the first offset says where the table ends | next offset minus this one |

Offsets may be in bytes or in 2048-byte sectors, and sizes in bytes or sectors; all combinations are
tried. A candidate must have at least two entries, every entry inside the file and after the table,
offsets in order, no overlaps, and at most half the entries empty. Its score (0 to 1) weighs how much
of the file the entries cover, whether gaps between entries are only alignment padding, how aligned the
offsets are (16 bytes or more is best), whether the data starts right after the table, and whether the
last entry reaches the end of the file. Layouts without a count or without sizes are trusted a little
less. `unpack` uses the best candidate when it scores 0.6 or more; `--force-toc` takes the best guess
anyway, `--layout` and `--header` restrict the guesses to what you found in the game's code.

Big-endian tables, nested tables, tables at the end of the file and compressed entries are not
detected. Look in the loader in Ghidra and, if needed, write a small per-game script.

## AFS (CRI)

| offset | field |
| --- | --- |
| 0x00 | `AFS\0` |
| 0x04 | N, the entry count |
| 0x08 | N x (offset u32, size u32) |
| then | (offset u32, length u32) of the name table: right after the entries (`after_toc`) or in the 8 bytes just before the first file (`before_data`) |
| data | each entry aligned, usually to 2048 bytes |
| name table | N x 48 bytes: name (32, NUL padded), year, month, day, hour, minute, second (6 x u16), and one u32 |

The last u32 of each name record differs by tool: a copy of the entry's offset, of its size, or of the
header word at `4 + 4 x index`. `unpack` detects which and `repack` regenerates it the same way (or
keeps the raw values). The two pointer positions match the two layouts PuyoTools writes (its "version
1" and "version 2"). Some games read AFS entries by index only, others also hard-code offsets: when in
doubt, keep edits small enough for `--layout keep`.

## index.json (written by unpack, read by repack)

```json
{
  "tool": "datascan.py", "version": 1, "format": "afs",
  "source": "STAGE.AFS", "source_size": 1234944, "source_sha256": "...",
  "layout": {"kind": "afs", "n": 12, "table_at": 8, "count_at": 4, "off_scale": 1, "size_scale": 1, "sentinel": false},
  "alignment": 2048, "pad_byte": 0, "header_file": "_header.bin",
  "afs": {"meta_where": "after_toc", "meta_ptr_at": 104, "meta_offset": 1232896, "meta_length": 576,
          "name_rule": "offset", "raw_tail": null},
  "extras": [],
  "entries": [{"index": 0, "offset": 2048, "size": 30120, "slot": 30720, "name": "ST01.DAT",
               "date": [2003, 7, 14, 10, 20, 30], "file": "files/0000_ST01.DAT", "sha256": "..."}]
}
```

- `slot` is the room an entry has before the next thing in the file: an edit up to that size keeps every
  offset.
- `_header.bin` is every byte before the first entry (the table included). `repack` patches the table
  fields in it and keeps the rest byte for byte.
- `extras` are byte ranges outside every entry that are not padding. `--layout keep` puts them back at
  their offsets; `--layout pack` keeps those after the last entry and reports any it had to drop.
- Do not change the number of entries, their order or the `file` names: `repack` rebuilds from this file.

## Repack layouts

| layout | what happens | when |
| --- | --- | --- |
| `auto` (default) | `keep` if every entry fits its slot, else `pack` | most edits |
| `keep` | every entry at its original offset, the file keeps its size; refuses if an entry outgrew its slot | the game hard-codes offsets, or you want the smallest change |
| `pack` | entries laid out again in order from the first data offset, each aligned to the original alignment; the table and the AFS name table are rewritten | an entry had to grow and the game reads offsets from the table |

After writing, `repack` reads every entry back through the new table and compares it with your file.
