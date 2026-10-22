# Design doc: {{TITLE}} ({{SERIAL}})

Written by Claude from what the user says about the game and what the port reveals. It is a working
map, not a spec: mark every line as SEEN (observed in the port, PCSX2 or Ghidra) or SAID (the user's
memory of the game) until it is SEEN. Update it at the end of every phase. It describes the game in
your own words; it never copies the game's text, art or code.

## The game in one paragraph
What kind of game, what the player does, what makes it this game.

## The loop
The moment-to-moment loop (input, what moves, what scores), then the session loop (level, race, match,
mission), then the long loop (progress, unlocks, saves).

## Screens and menus
Boot order (logos, movies, title, main menu), every menu the user remembers, which buttons move
through them, and where a fresh save starts. One line per screen.

| Screen | How you reach it | What it loads (disc files, modules) | Port status |
| --- | --- | --- | --- |

## Files on the disc
From `iso.py ls -r` and `elf.py info`: the boot ELF, IRX modules, big archives, movies (PSS),
sound banks, level data. What each is FOR, once known (/console-levels maps them in depth).

| File or folder | Size | What it is (guess or known) | Loaded when |
| --- | --- | --- | --- |

## Code map
Functions found so far (Ghidra names, addresses). Start with the boot path: entry, main, the per-frame
function, the loader, the menu state machine, the save code.

| Address | Name | What it does | Evidence |
| --- | --- | --- | --- |

## Systems
One short section each, filled in as the port reaches them: rendering, audio, input, file loading,
saves, physics, AI. Note which PS2Recomp layer handles each (see reference/guardrails.md in
/console-reverse) and which /console-* skill owns the work.

## Mod ideas
What the user wants to change, and which numbers, files or functions it will touch.
