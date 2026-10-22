# Finding what level bytes mean

Every game stores its levels differently. This is the general way in, using `datascan.py`, Ghidra and
PCSX2 (both optional, see /console-reverse for setup). Work on copies under `console/`, never on the
only copy of a disc file.

## 1. Which file is the level?

- **The port's log.** Boot into the level with /console-tester and run `datascan.py files` on the newest
  log in `console/runs/`. The runtime logs each `sceCdSearchFile` lookup (the first 128, then every
  512th) with the file's size, and every lookup that failed. The files that appear right when the level
  starts belong to it.
- **The code's strings.** `datascan.py strings <ELF> --grep stage` (or `map`, `level`, `.bin`, `.afs`,
  the level's name) lists file names and format strings like `STAGE%02d.BIN` with their offsets.
- **Sizes.** Levels are usually the biggest files or the biggest entries inside the biggest archive.
- **PCSX2.** Its log shows the same file reads when you start the level on real game code.

## 2. Is it compressed?

`scan` reports entropy. Plain level data (coordinates, ids, counts) sits well under 7 bits per byte and
diffs cleanly. Over 7.5 everywhere, with no table, means compressed or encrypted. Then find the
decompressor: in Ghidra, follow the file name string to the function that loads the buffer; the call
right after the read is usually the decompressor (common shapes: LZ77 variants with a flag byte every
8 tokens, or zlib, which `scan` recognises). Port that one function to a small Python script, unpack,
edit, and write the compressor too (or store uncompressed if the game's loader allows a flag for it).

## 3. Diff to find structures

- **Two levels.** `diff LEVEL1 LEVEL2` on two entries of the same type: shared headers are identical,
  counts and offsets differ in small fields, bulk data differs everywhere.
- **Before and after.** In PCSX2 (or the port), change one thing you can control, save or dump, and
  diff. The changed range is where that thing lives.
- **Read the values.** `--as f32` shows floats: positions and sizes in level data look like plausible
  coordinates (hundreds or thousands, not 1e-39). `--as u16` / `--as u32` show counts and ids.
  Repeating patterns every 16, 32 or 48 bytes are arrays of records: the period is the record size.
- **Offsets inside a file.** A u32 that is a little smaller than the file size and points at the start
  of a repeated block is a pointer relative to the file start (common on PS2). Changing a record's size
  means fixing every pointer after it, so prefer same-size edits.

## 4. Read the loader

In Ghidra, search for the file name string, follow the cross-reference to the code that opens the
file, then to the function that walks the loaded buffer. Field offsets in that code (`lw v0, 0x14(a0)`,
`lwc1 f0, 0x8(s1)`) are your record layout. Name the fields in Ghidra and write the layout into
`console/design.md`. With GhydraMCP connected (see /console-reverse) Claude can do this lookup for you.

## 5. Edit, repack, test

- Keep sizes when you can: the default repack keeps every offset and the file size, the safest change.
- Change one thing at a time and playtest each (/console-tester). Keep a list in `console/STATE.md`.
- If the level crashes the port but plays in PCSX2 with the same edit, it is a runtime problem, not
  your data: /console-tester ranks it and /console-backend fixes it.

## What this cannot do for you

It does not know any game's level format, does not decompress game-specific formats, and does not
edit 3D geometry visually. It finds the files, splits and rebuilds archives safely, and shows which
bytes matter. The rest is per game, built from those pieces.
