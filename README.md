# The Console skill

11 Claude skills that port PlayStation 2 games you own to PC: reverse engineer the code, rebuild the graphics and backend, any controller, play it anywhere, then mod it however you want.

Free, MIT, no signup, no API key. The tools are plain Python with nothing to
install. The heavy lifting is done by [PS2Recomp](https://github.com/ran-j/PS2Recomp),
ran-j's open-source static recompiler, and the method started life as
[hkmodd/ps2-recomp-Agent-SKILL](https://github.com/hkmodd/ps2-recomp-Agent-SKILL).

This is not an emulator. PS2Recomp translates a game's MIPS machine code into
C++ and compiles it into a native program for your computer. Getting a game
from "it compiles" to "it plays" is weeks of reverse engineering: reading
crash logs, finding functions in Ghidra, writing the missing pieces of the
console in C++. That grind is what these skills do with you.

One reverse engineers the game and drives the whole port. One builds the
backend the game runs on. One rebuilds the graphics, one the sound, one hooks
it up to any controller, and one ships it to your laptop, your Steam Deck and
your phone. And the craziest one is the Mod skill: describe a change in plain
English, a new camo, a faster car, a widescreen camera, and Claude builds it
into the game as code that never touches the original files.

> **Bring your own game.** Everything here starts from a disc you own, dumped
> on your own computer. No ISOs, no BIOS, no downloads, and nothing from the
> game ever goes into a repo or a mod you share. See the fine print.

## Install

Paste this into Claude:

```
https://github.com/Jakeschincariol/console-skill

install skill
```

Or as a plugin, in Claude Code:

```
/plugin marketplace add Jakeschincariol/console-skill
/plugin install console-skill@console-skill
```

Claude Code namespaces plugin skills, so installed as a plugin they show up as
`/console-skill:console-reverse` and so on. Copy the folders instead if you
want plain `/console-reverse`:

```bash
git clone https://github.com/Jakeschincariol/console-skill.git
cp -r console-skill/console-* ~/.claude/skills/
```

The tools need Python 3.8 or newer. Nothing to pip install.

## What you need

| what | why |
| --- | --- |
| A PlayStation 2 game you own, and a computer DVD drive | dump your own disc to an ISO. `/console-reverse` shows you how |
| [PS2Recomp](https://github.com/ran-j/PS2Recomp), cloned and built | the recompiler and the runtime the port runs on (CMake 3.20+, a C++20 compiler) |
| [Ghidra](https://github.com/NationalSecurityAgency/ghidra) with the [EmotionEngine Reloaded](https://github.com/chaoticgd/ghidra-emotionengine-reloaded) extension | maps every function in the game. Recommended for any retail game |
| [PCSX2](https://pcsx2.net) (optional) | test patches before you bake them in, and compare the real game against your port |
| [GhydraMCP](https://github.com/starsong-consulting/GhydraMCP) and [PCSX2-MCP](https://github.com/hkmodd/PCSX2-MCP) (optional) | let Claude drive Ghidra and the PCSX2 debugger itself |

Windows, macOS (Apple silicon too) and Linux all work for building. PS2Recomp
also has Android and PS Vita targets.

## The eleven

| command | job | what it does |
| --- | --- | --- |
| `/console-reverse` | Reverse Engineer | Opens your own disc image, finds the game's code and maps how it works: the boot ELF, every function, the design doc. Drives the whole port. |
| `/console-backend` | Backend Engineer | Builds the engine room the recompiled game runs on: syscalls, kernel stubs, file and disc I/O, the IOP. |
| `/console-graphics` | Graphics Artist | Rebuilds the graphics: dumps every texture, lets you repaint or replace them with your own art, finds the widescreen fix. |
| `/console-controller` | Controller Mapper | Hooks the game up to any controller: DualSense, Xbox, Switch Pro, keyboard or touch, remapped how you like. |
| `/console-mod` | Mod Maker | Describe any change in plain English and Claude builds it into the game: patches, cheats, new behaviour, as code that never touches the original files. |
| `/console-levels` | Level Designer | Maps the disc's data files, unpacks the archives the levels live in, and swaps in your edits. |
| `/console-sound` | Sound Designer | Rips the music and sound effects and puts your own tracks back in. |
| `/console-physics` | Physics Tuner | Finds the numbers that decide how the game moves (speed, gravity, grip, jump height, frame rate) and tunes them. |
| `/console-saves` | Save Manager | Brings your old memory card saves over, backs them up and syncs them to the cloud so you continue on any device. |
| `/console-anywhere` | Play Anywhere | Builds the port for Windows, Mac, Linux, Steam Deck, Android and PS Vita and packages it without any game files inside. |
| `/console-tester` | Playtester | Runs the port again and again, reads every crash, ranks what to fix next and checks nothing broke. |

## How to use it

Start with `/console-reverse`. It sets up `console/STATE.md`, the port's
memory: where everything lives, which phase you are in, every crash and fix so
far. Every skill reads it and writes back to it, so you can stop on Monday and
pick up on Thursday in a new chat.

```
reverse -> backend <-> tester               (until it boots, then until it plays)
graphics / sound / controller / physics / levels      (make it yours)
mod                                          (ship your changes as patches)
saves -> anywhere                            (play it everywhere)
```

1. **`/console-reverse`**: dump your disc, read `SYSTEM.CNF` to find the boot
   ELF, pull it out, fingerprint it (serial, region, both CRCs), map it in
   Ghidra, write the PS2Recomp config, recompile, build. It also writes
   `console/design.md`: how the game plays, its menus and its files.
2. **`/console-tester`** boots the port with a hard timeout and reads the log.
   **`/console-backend`** turns every "unimplemented stub" and "syscall TODO"
   into a ranked fix list and implements them in the runtime. Repeat. This
   loop is most of the work.
3. **`/console-controller`** writes your button layout into the runtime's pad
   code. **`/console-graphics`** and **`/console-sound`** dump textures and
   audio so you can replace them with your own. **`/console-physics`** finds
   the numbers behind speed, gravity and frame rate.
4. **`/console-mod`** turns a plain-English change, or a PCSX2 `.pnach` patch
   you already use, into instruction patches and a game override module.
5. **`/console-saves`** brings your PCSX2 or real memory card saves over.
   **`/console-anywhere`** builds and packages the port for each device,
   with a guard that refuses to package anything from the game.

## The tools

All standard-library Python, run from the repo (or from `~/.claude/skills/`).
Everything lands in a `console/` folder in your game workspace.

```bash
python3 console-reverse/iso.py boot game.iso                          # boot ELF, serial, region
python3 console-reverse/iso.py extract game.iso --out console/disc    # the port's cdrom0: root
python3 console-reverse/elf.py info console/disc/SLUS_203.12          # segments, both CRCs, IRX modules
python3 console-reverse/elf.py toml console/disc/SLUS_203.12 --ghidra-csv console/functions.csv
python3 console-reverse/state.py init --game "My Game"                # console/STATE.md
python3 console-tester/run.py boot --runner out/build/ps2xRuntime/ps2EntryRunner --elf console/disc/SLUS_203.12
python3 console-tester/triage.py scan console/runs/latest.log --map console/functions.csv
python3 console-backend/stubs.py scan console/runs/latest.log --repo ../PS2Recomp
python3 console-controller/padmap.py init --layout xbox && python3 console-controller/padmap.py gen
python3 console-graphics/tim2.py topng console/disc/DATA --out console/textures/png
python3 console-graphics/widescreen.py scan console/disc/SLUS_203.12 --out console/patches/widescreen-try.pnach
python3 console-graphics/gspacket.py dma console/dumps/ram.bin --tag 0x01F2A000
python3 console-sound/vag.py towav console/disc/SOUND/BGM01.VAG
python3 console-sound/vag.py fromwav my_song.wav --like console/disc/SOUND/BGM01.VAG --out console/audio/BGM01.VAG
python3 console-levels/datascan.py scan console/disc/ --sort size --embedded
python3 console-physics/tune.py scan console/disc/SLUS_203.12 --value 9.8
python3 console-mod/pnach.py classify my.pnach --elf console/disc/SLUS_203.12
python3 console-mod/override.py new --name "Gold rifle" --elf console/disc/SLUS_203.12 --wrap 0x00123450=gold_damage
python3 console-saves/memcard.py export Mcd001.ps2 --to console/disc/mc0
python3 console-saves/memcard.py sync console/disc/mc0 --cloud ~/Dropbox/console-saves/SLUS-20312
python3 console-anywhere/targets.py doctor --target steamdeck
python3 console-anywhere/targets.py package --runner out/release/ps2xRuntime/ps2EntryRunner --target linux --out console/dist/mygame-linux
```

Every tool has `--help`, most have `--json`, and they share exit codes: 0 ok,
1 something needs your attention, 2 bad input.

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline on fixtures built in code: tiny synthetic ELFs, disc
images, textures, audio, memory cards and logs. No game data, BIOS or Sony
file is in this repo, and a test fails if one ever is.

## What is new since the base skill

[hkmodd/ps2-recomp-Agent-SKILL](https://github.com/hkmodd/ps2-recomp-Agent-SKILL)
is one big behavioural skill for PS2Recomp: a boot sequence, hard prohibitions,
a build gate, a three-strike circuit breaker and a persistent state file. All of
that is kept. This pack adds:

- **Eleven jobs instead of one prompt.** Each skill loads only what its job
  needs, so the context stays small on a port that lasts weeks.
- **Real tools, not just rules.** An ISO 9660 reader, an ELF inspector that
  prints both CRCs (the runtime's CRC-32 and PCSX2's pnach CRC), a log triage
  that ranks crashes and maps them to Ghidra function names, a boot and soak
  runner with hard timeouts, a pnach-to-PS2Recomp converter, a game override
  generator, a float and frame-rate constant finder, a TIM2 texture
  converter, a widescreen candidate finder, a DMA/VIF/GIF packet walker, a VAG
  audio decoder and encoder, a data archive mapper, a memory card reader with
  safe cloud sync, a controller code generator with input replay, and a
  packager that refuses to ship game files. 559 offline tests.
- **Every platform.** The base skill was Windows only (vcvars64, PowerShell,
  `.bat`). This pack covers Windows, macOS, Linux, Steam Deck, Android and
  PS Vita.
- **Up to date with PS2Recomp.** Checked against PS2Recomp as of October 2026:
  the Ghidra export workflow, the real runtime file layout, the real game
  override API (the base skill's override template calls functions that do
  not exist), and the exact log lines the runtime prints.
- **A design doc and a state file every skill shares**, kept in
  `console/STATE.md` and `console/design.md`.

## Fine print

**Bring your own game.** Only port games you own, dumped from your own disc on
your own drive. Never download ISOs, ELFs or BIOS files; PS2Recomp does not
need a BIOS. The skills will not help find any of them.

**Nothing from the game gets published.** The disc image, the ELF, the IRX
modules, textures, audio, level files and the recompiled C++ (it is generated
from the game's code) stay on your machine. Share patches, overrides and your
own original assets only. `/console-anywhere` refuses to package game files.

**It is experimental.** PS2Recomp is under active development and says so:
many games do not boot yet, graphics and vector-unit code are slow, and big
parts of the console are still stubs. Some games will take weeks. Some will not
make it. These skills make the work faster and more methodical; they do not
make it instant.

**Back up first.** Saves are copied, never moved, and every sync keeps both
copies when there is a conflict. Builds are incremental only: a clean rebuild
of a large game can take hours.

**Not affiliated with Sony.** PlayStation and PS2 are trademarks of Sony
Interactive Entertainment Inc. This project is not affiliated with, endorsed
or sponsored by Sony, by ran-j or by hkmodd.

## Credits

- [hkmodd/ps2-recomp-Agent-SKILL](https://github.com/hkmodd/ps2-recomp-Agent-SKILL)
  (MIT): the original PS2Recomp agent skill this pack is built on. Its license
  notice is reproduced below.
- [ran-j/PS2Recomp](https://github.com/ran-j/PS2Recomp) (GPL-3.0): the static
  recompiler and runtime. Not included here.
- [ps2tek](https://psi-rockin.github.io/ps2tek/) by PSI: the PS2 hardware
  reference the skills point to.

<details>
<summary>License notice for hkmodd/ps2-recomp-Agent-SKILL</summary>

The porting workflow, guard rails, phases and debugging protocol in
`console-reverse`, `console-backend` and `console-tester` are adapted from
ps2-recomp-Agent-SKILL, used under its MIT License, reproduced here as
published:

```
MIT License

Copyright (c) 2026 [Your Name / Alias]

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

</details>
