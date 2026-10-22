# The game-data guard

`targets.py package` builds the folder in a staging copy, runs the guard on it, and only moves it
into place when nothing is refused. `targets.py guard <folder>` runs the same check on any folder:
a release zip, a mod repository before `git push`, a shared drive.

## What it refuses

| check | how it is detected |
| --- | --- |
| disc images | extensions `.iso .bin .img .mdf .mds .nrg .chd .cso .zso .ecm .cue .ccd .sub .isz .dax`; content: `CD001` at 0x8001 (ISO 9660, 2048-byte sectors) or 0x9319 (raw 2352-byte sectors), `MComprHD` (CHD), `CISO` / `ZISO` (compressed ISO), under any name |
| PS2 programs | an ELF that is 32-bit with machine 8 (MIPS): the game's main ELF, overlays, IRX modules |
| disc file names | `SLUS_203.12`-style names (four letters, `_` or `-`, digits), `SYSTEM.CNF`, any `.irx` |
| recompiled game code | `register_functions.cpp`, `ps2_recompiled_functions.h`, `ps2_recompiled_stubs.h`, any C/C++ file under a `runner/` folder, any C/C++ file that includes `ps2_recompiled_functions.h` or uses `g_ps2RecompiledFunctionTable` |
| the game folder | anything inside `game/` except `PUT-YOUR-GAME-HERE.txt`: it ships empty |
| size | any file over 512 MB |

Not refused, on purpose: your own `.pnach` patches (even named `SLUS-20312_1A2B3C4D.pnach`), game
override `.cpp` files you wrote, controller mappings, replay scripts, saves you made (still
personal: think before sharing), and the host runner and its libraries. Textures, audio and level
files are not detected by format, because your own replacement art uses the same formats: keep
ripped originals out of shared folders yourself.

## The allow file

For a false positive only: a file that is truly yours but trips a check (a `.bin` settings file of
your own, a big video you made). One line per file or pattern, with the reason after `#`:

```
# console/dist-allow.txt
settings.bin            # my own key bindings export, not from the disc
videos/intro-mine.mp4   # my own 600 MB intro video
```

A line without a reason is an error. Patterns use shell globs (`videos/*.mp4`) matched against the
path inside the folder. Allowed files are listed in the output and in `.console-dist.json`, so the
decision stays visible. Claude never writes an allow line for a disc image, a PS2 ELF or IRX,
`SYSTEM.CNF`, runner sources or anything in `game/`: those are the game.

## Why the runner itself is not refused, and why the package is still private

The runner is a host program (x86-64, arm64, a PE or Mach-O binary, an APK or a vpk), so the
checks above pass it. But it is compiled from `runner/*.cpp`, which the recompiler derived from the
game's code. The package therefore contains no game files, yet it is still derived from the game:
it is for the person who owns the disc, on their own devices. To share a project, share the
patches, overrides, TOML, mappings and your own assets, and let each player build and supply
their own disc.
