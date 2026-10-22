---
name: console-levels
description: >-
  Level Designer. Maps the disc's data files, unpacks the archives the levels live in, and swaps in
  your edits. Inventories an extracted PS2 disc (size, type by magic, entropy, a table-of-contents
  guess with a confidence score), unpacks CRI AFS archives and detected table-of-contents archives
  into a folder with an index.json, repacks them in the same order and alignment (keeping every offset
  when your edits fit), diffs two files or folders to show which bytes a change touched, lists strings,
  and reads the port's log to show which disc files a level loads. Writes under console/ and never
  modifies the original disc files. Use when the user says "where are the levels", "unpack this
  archive", "what's in DATA.BIN", "edit the level", "swap in my map", "what changed between these two
  files", "which files does stage 3 load", or before /console-graphics or /console-sound needs a file
  that sits inside an archive.
argument-hint: "scan|unpack|repack|diff|strings|files <path> [flags]"
---

# console-levels

The Level Designer maps a game's data and gives you a way in. A PS2 disc is a folder of files: the ELF,
IRX modules, and data, usually a few huge archives that hold every level, model, texture and sound.
`datascan.py` tells you what each file is, finds the table of contents inside an archive, splits it
into files you can edit, and puts it back together so the game still reads it. Then the port loads
your edited archive straight from the extracted disc folder, with no ISO to rebuild.

Be honest with the user: every game's level format is its own. This tool gives the map and the knife
(what the files are, how the archives split, which bytes changed). Working out what the bytes inside a
level mean is per game: diff two levels, diff before and after an in-game change, and read the loader
in Ghidra. Compressed or encrypted archives (high entropy everywhere, no table found) need the game's
own decompressor found in its code first.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/datascan.py" scan console/disc/ --sort size --embedded     # map the disc
python3 "${CLAUDE_SKILL_DIR}/datascan.py" files console/runs/<newest>.log       # files the port looked up
python3 "${CLAUDE_SKILL_DIR}/datascan.py" unpack console/disc/DATA/STAGE.AFS --out console/levels/STAGE
python3 "${CLAUDE_SKILL_DIR}/datascan.py" diff console/levels/STAGE/files/0003_ST01.DAT console/levels/STAGE/files/0004_ST02.DAT --as f32
python3 "${CLAUDE_SKILL_DIR}/datascan.py" strings console/disc/SLUS_203.12 --grep .afs
python3 "${CLAUDE_SKILL_DIR}/datascan.py" repack console/levels/STAGE --out console/levels/STAGE.AFS
```

Other flags: `--json` everywhere; `scan --min-size N`; `unpack --format afs|toc --layout KIND --header N
--force-toc`; `repack --layout auto|keep|pack --force`; `diff --gap N --max N`; `strings -n N
--encoding ascii|sjis|utf16le|all --max N`. Exit codes: 0 ok, 1 needs attention (no table found, an
entry outgrew its slot, a missing disc file; for `diff`, the inputs differ), 2 bad input.

## What it reads and writes

| path | what |
| --- | --- |
| the extracted disc folder | read only: `scan`, `unpack`, `strings`, `diff` never change it |
| a runtime log (`console/runs/*.log`) | `files` reads the `[sceCdSearchFile:ok]` and `sceCdSearchFile failed:` lines |
| `console/levels/<NAME>/files/` | the unpacked entries, numbered in archive order (`0003_ST01.DAT`) |
| `console/levels/<NAME>/index.json` | format, table layout, alignment, offsets, slot sizes, hashes: everything `repack` needs |
| `console/levels/<NAME>/_header.bin`, `_extra_*.bin` | the archive's header bytes and any bytes outside the entries, kept so a repack is exact |
| `--out` of `repack` | the rebuilt archive (refuses to overwrite an existing file without `--force`) |

Formats, the table detector, AFS details and the index.json schema:
[reference/formats.md](reference/formats.md). Finding what level bytes mean (diffing, Ghidra, PCSX2):
[reference/level-workflow.md](reference/level-workflow.md).

## The method

1. **Map the disc.** `scan <extracted disc> --sort size --embedded`. Note the biggest files, their kind
   and entropy, and any table of contents found (`AFS, N entries` or `count+pairs n=.. (0.9)`). Below
   7.5 bits per byte with a table found: an archive you can split. Above 7.5 everywhere: compressed or
   encrypted; find the decompressor in the ELF before going further.
2. **Find where the levels live.** Run the port into the level (/console-tester `run.py boot`), then
   `files console/runs/<newest>.log` lists the disc files it looked up, in order. The files looked up
   right when the level starts are the level's. `strings <ELF> --grep .bin` (or `.afs`, `stage`, `map`)
   finds file names the code uses. In PCSX2 you can watch the same thing in its log.
3. **Unpack.** `unpack <archive> --out console/levels/<NAME>`. AFS is parsed exactly. Other archives
   use the best table-of-contents guess at confidence 0.6 or more; below that it refuses and says why.
   If you know the layout from Ghidra, force it with `--layout` and `--header`.
4. **Find the structures.** `diff` two levels, or the same level before and after a small change you
   make, with `--as f32` to see coordinates and `--as u16|u32` for counts and ids. Read the loader in
   Ghidra: find the file name string, follow its cross-reference to the code that parses the buffer.
   Write what you learn into `console/design.md` (the game's design doc from /console-reverse).
5. **Edit.** Change the entry files in `console/levels/<NAME>/files/` (same names). Keep sizes when you
   can.
6. **Repack.** `repack console/levels/<NAME> --out console/levels/<NAME>.AFS`. With the default
   `--layout auto`, entries that still fit stay at their exact offsets (the safest result: nothing else
   moves). If one outgrew its slot, offsets are recomputed with the original alignment and the table is
   rewritten; it says so. Use `--layout keep` when the game hard-codes offsets in its code (it then
   refuses rather than move anything). Every repack reads itself back to verify.
7. **Swap it in.** Close the port. Copy the original archive aside (`STAGE.AFS.orig`, or keep it in
   `console/`), copy the repacked one over it in the extracted disc folder. The port reads `cdrom0:\`
   from the ELF's folder (`cdRoot`) and looks files up by name with their real size, so no ISO rebuild.
8. **Playtest.** /console-tester, then load the level. Record what you changed in `console/STATE.md`.

## Guard rails

- **Bring your own game.** Work only on discs you own and dumped yourself (ImgBurn on Windows, `dd`
  from a DVD drive on macOS or Linux). Never download ISOs, ELFs or BIOS files, never help find them,
  never ask for them.
- **Nothing from the game gets published.** Unpacked archives, level files, models, textures and the
  disc itself stay on your machine, like the ELF, the IRX modules and the recompiled C++. Share your
  edits as a patch or a script that changes the user's own files, never the files themselves.
- **Never overwrite your only copy.** `datascan.py` never writes into the disc folder; you copy the
  repacked file in yourself, after keeping the original.
- **Not affiliated with Sony.** PlayStation is a trademark of Sony Interactive Entertainment.

## Hands off to

- /console-graphics for TIM2 textures found inside an archive (`tim2.py` works on unpacked entries).
- /console-sound for VAGs and sound banks found inside an archive.
- /console-physics and /console-mod when the change belongs in code (speeds, rules) rather than data.
- /console-tester to playtest the swapped level and check nothing else broke.
