---
name: console-saves
description: >-
  Save Manager. Brings your old memory card saves over, backs them up and syncs them to the cloud so
  you continue on any device. Reads PS2 memory card images (PCSX2 .ps2 and .mcd, raw 8 MB dumps, with
  or without ECC) and PCSX2 folder cards, lists every save with its title, copies saves into the port's
  folder memory card (mc0 next to the game's ELF), imports and exports single saves as .psu, makes
  timestamped zip backups, and syncs mc0 with an iCloud Drive, Dropbox, Google Drive or Syncthing
  folder. Card images are read only; mc0 is backed up before anything replaces a save; a sync never
  silently overwrites (when both sides changed it keeps both and reports a conflict). Use when the user
  says "bring my saves over", "import my PCSX2 save", "my memory card", "back up my saves", "sync my
  saves", "continue on my Steam Deck", "restore a backup", "convert this .psu", or before the first run
  of a port that needs a save.
argument-hint: "info|ls|export|psu|backup|restore|sync <card or mc0> [flags]"
---

# console-saves

The Save Manager moves your progress into the port and keeps it safe. The PS2Recomp runtime does not
use a card image: it keeps memory card slot 1 as plain folders in `mc0` next to the game's ELF, one
folder per save, exactly as the game names them (`mc0/BASLUS-20312GAME/...`), and slot 2 in the sibling
folder `mc1` (`ps2xRuntime/src/lib/Kernel/Stubs/MemoryCard.cpp`, `getMcRootPath`). `memcard.py` reads
your old card, writes those folders, and then backs them up and syncs them.

Honest limits: card images are read only (writing them back needs ECC and is out of scope; PCSX2's own
memory card manager can import a `.psu`). Saves from other formats (`.max`, `.cbs`, `.xps`) need a
converter first. A save copied to another region's version of a game usually will not load. Whether the
port loads a given save depends on how far the port is: test it.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/memcard.py" ls ~/Documents/PCSX2/memcards/Mcd001.ps2      # saves, titles, serials
python3 "${CLAUDE_SKILL_DIR}/memcard.py" export Mcd001.ps2 --to console/disc/mc0 --only BASLUS-20312GAME
python3 "${CLAUDE_SKILL_DIR}/memcard.py" psu import BASLUS-20312GAME.psu --to console/disc/mc0
python3 "${CLAUDE_SKILL_DIR}/memcard.py" psu export console/disc/mc0/BASLUS-20312GAME --out console/saves/game.psu
python3 "${CLAUDE_SKILL_DIR}/memcard.py" backup console/disc/mc0                                # console/saves/backups/
python3 "${CLAUDE_SKILL_DIR}/memcard.py" sync console/disc/mc0 --cloud "$HOME/Library/Mobile Documents/com~apple~CloudDocs/console-saves/SLUS-20312"
```

Other commands and flags: `info <card>` (format, ECC, free space), `ls --files`, `restore <zip> --to
mc0 [--force]`, `export --force` and `psu import --force` (replace, after a backup), `backup --keep N
--label NAME`, `sync --push | --pull`, `sync --prefer local|cloud|newest`, `sync --dry-run`, `--json`.
Exit codes: 0 ok, 1 needs attention (a conflict, a save already there, a damaged save skipped, a cloud
file not downloaded), 2 bad input (not a card, truncated, damaged directories, an unsafe name).

## What it reads and writes

| path | what |
| --- | --- |
| `*.ps2`, `*.mcd`, `*.bin` card image | read only: superblock, FAT, directories (8 MB = 8,388,608 bytes plain or 8,650,752 with ECC) |
| a PCSX2 folder card | read only; its `_pcsx2_*` metadata files are skipped |
| `<elf dir>/mc0/<SAVE>/<file>` | the port's memory card slot 1 (slot 2: `<elf dir>/mc1`) |
| `*.psu` | one save folder: written by `psu export`, read by `psu import` and `psu ls` |
| `console/saves/backups/<game>-mc0-<UTC time>.zip` | backups; the newest 20 per card are kept |
| `<elf dir>/.console-sync.json` | what the last sync saw, per mc0 and cloud folder (kept out of mc0) |
| `<cloud>/.console-conflicts/<time>-<device>/` | both versions of every conflict (`here/` and `cloud/`) |

Card format, `.psu` layout and the runtime's folder card in detail:
[reference/memcard-format.md](reference/memcard-format.md). Cloud folders per device, the sync rules
and settling conflicts: [reference/cloud-sync.md](reference/cloud-sync.md).

## The method

1. **Find your saves.** PCSX2 keeps cards in its `memcards` folder (`Mcd001.ps2`, `Mcd002.ps2`, or a
   folder per card when folder cards are on). From a real PS2, copy the card with homebrew you already
   run (uLaunchELF copies a save folder to USB as a `.psu`), or use the image file an SD-card memory
   card adapter keeps. `ls` the card: each row shows the folder, size, date, the serial in the folder
   name (`BASLUS-20312...` is SLUS-20312, USA) and the title from `icon.sys`.
2. **Match the game.** The save folder's serial must match the disc the port runs (`console/STATE.md`
   or `/console-reverse`'s `iso.py boot`). Another region's save usually will not load.
3. **Close the port.** Never write into mc0 while the game runs.
4. **Bring it over.** `export <card> --to <elf dir>/mc0 [--only SAVE]`, or `psu import <x.psu> --to
   <elf dir>/mc0`. Both refuse to replace a save that is already there; `--force` replaces it after
   zipping the whole mc0 into `console/saves/backups/`.
5. **Back up.** `backup <elf dir>/mc0` before every session that matters, and before any change by hand.
   `restore <zip> --to mc0` puts one back (it backs up what is there first and needs `--force` to
   replace a non-empty mc0).
6. **Sync (optional).** `sync <elf dir>/mc0 --cloud <a folder your cloud app syncs>`. First run on the
   device with the saves; then on every other device. Use one cloud folder per game. Read the summary:
   a conflict means both sides changed since the last sync; nothing was overwritten, both versions are
   in `.console-conflicts`, and `--prefer local|cloud|newest` settles it.
7. **Playtest.** Start the port and load the save (/console-tester). Write one line in
   `console/STATE.md`: which save, from where, backup name.

## Guard rails

- **Bring your own game.** Saves go with a game you own and dumped yourself. Never download ISOs, ELFs
  or BIOS files, never help find them, never ask for them.
- **Nothing from the game gets published.** The disc, ELF, IRX modules and recompiled C++ stay on your
  machine. Your saves are yours: keep the cloud folder private, never a public share link.
- **Back up before every change.** `export --force`, `psu import --force`, `restore --force` and any
  `sync` that writes into mc0 make a zip first. Do the same before editing saves by hand.
- **Formatting deletes mc0.** In the port, a game's "format memory card" option removes the whole mc0
  folder (`sceMcFormat` and `sceMcUnformat` call `remove_all` on it). Back up before letting a game
  format a card.
- **Card images are read only.** This tool never writes a `.ps2` or `.mcd`.
- **Not affiliated with Sony.** PlayStation is a trademark of Sony Interactive Entertainment.

## Hands off to

- /console-tester to boot the port and check the save loads.
- /console-backend when the game cannot save or load in the port (`Kernel/Stubs/MemoryCard.cpp`, the
  `sceMc*` handlers, and `ps2xIOP/src/modules/mcserv.cpp` for games that talk to MCSERV over RPC).
- /console-anywhere to run the same port on another device; then `sync` there.
