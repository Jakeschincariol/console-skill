# {{GAME}}: {{TARGET}}

This folder is a native port of {{GAME}} made with PS2Recomp. It contains the port's program and
nothing from the game: no disc, no game files. You supply the game from your own disc.

This copy was built for the person who owns the disc, for their own devices. The program is built
from that game's recompiled code, so do not upload it or pass it on.

## 1. Dump your own disc

Use the disc you own ({{SERIAL}}).

- Windows: ImgBurn, "Create image file from disc", saves an .iso.
- macOS or Linux with a DVD drive: find the drive (`diskutil list` on macOS, `lsblk` on Linux), then
  `dd if=/dev/<your drive> of=mygame.iso bs=2048` (on macOS unmount it first with
  `diskutil unmountDisk /dev/<your drive>`).

Never download a disc image, and never ask anyone for one. No BIOS is needed.

## 2. Extract it into game/

Open the .iso (double-click on Windows and macOS, or any archive tool such as 7-Zip) and copy
EVERYTHING on it into the `game/` folder here, keeping its folders. You should end up with
`game/SYSTEM.CNF`, the main program `{{ELF_SHOWN}}` and the disc's folders.

## 3. Play

{{HOW_TO_RUN}}

Needs: {{NEEDS}}

Saves go to {{SAVES}} (a folder memory card next to the game's main program). Copy that folder
to keep or move your saves.

## What is in this folder

{{FILES}}
- `game/` (empty until you add your own disc)

PS2Recomp (github.com/ran-j/PS2Recomp, GPL-3.0) does the recompiling and provides the runtime.
Not affiliated with or endorsed by Sony. PlayStation is a trademark of Sony Interactive Entertainment.
