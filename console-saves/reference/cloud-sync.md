# Syncing mc0 through a cloud folder

`memcard.py sync <mc0> --cloud <folder>` keeps the port's memory card folder and a folder your cloud
app syncs in step. It never talks to a cloud service itself: iCloud Drive, Dropbox, Google Drive,
OneDrive or Syncthing moves the files between devices; `sync` only decides, per file, which way to copy
on this device.

## Where the cloud folder is

Use one folder per game, for example `console-saves/SLUS-20312`, inside:

| app | macOS | Windows | Linux / Steam Deck (desktop mode) |
| --- | --- | --- | --- |
| iCloud Drive | `~/Library/Mobile Documents/com~apple~CloudDocs/` | `%USERPROFILE%\iCloudDrive\` | not available |
| Dropbox | `~/Library/CloudStorage/Dropbox/` (older installs: `~/Dropbox/`) | `%USERPROFILE%\Dropbox\` | `~/Dropbox/` |
| Google Drive | `~/Library/CloudStorage/GoogleDrive-<account>/My Drive/` | `G:\My Drive\` (the drive letter can differ) | no official client |
| OneDrive | `~/Library/CloudStorage/OneDrive-Personal/` | `%USERPROFILE%\OneDrive\` | no official client |
| Syncthing | any folder you share | any folder you share | any folder you share (default `~/Sync`) |

Paths vary by version and account; check the app's settings if a folder is not there. `sync` refuses a
cloud path whose parent folder does not exist, so a typo cannot create a stray folder.

`memcard.py` runs wherever Python 3 runs (Windows, macOS, Linux, a Steam Deck in desktop mode). On an
Android device or a PS Vita there is no Python: copy the save folder in and out of the port's mc0 by
hand (after a `backup` on the computer), or point a Syncthing folder at it on Android.

## The rules

`sync` remembers what both sides looked like after the last sync (a SHA-256 and size per file) in
`.console-sync.json` next to mc0, keyed by the mc0 path and the cloud path. Each run compares both
sides with that:

| here | in the cloud | result |
| --- | --- | --- |
| same content | same content | nothing to do |
| changed since the last sync | unchanged | push (copy up) |
| unchanged | changed since the last sync | pull (copy down; mc0 is zipped to `console/saves/backups/` first) |
| changed | changed | **conflict**: neither side is touched |
| present | missing | push: a sync never deletes, so a file deleted on one side comes back |
| missing | present | pull |
| different, never synced before | different | **conflict** (nothing says which is newer progress) |

The side that changed wins, which in normal use is the newest save. Modification times are not
trusted for that (cloud apps and copies change them); they are used only when you ask for
`--prefer newest`.

## Conflicts

A conflict copies both versions to `<cloud>/.console-conflicts/<UTC time>-<device>/here/...` and
`.../cloud/...`, leaves both live files as they were, does not update the sync state for that file, and
exits 1. To settle it, load each version in the port if you need to see which is further along, then:

| command | result |
| --- | --- |
| `sync ... --prefer local` | this device's version goes up |
| `sync ... --prefer cloud` | the cloud's version comes down (after a backup of mc0) |
| `sync ... --prefer newest` | the newer modification time wins; equal times stay a conflict |

The losing version is still kept in `.console-conflicts`. Delete those folders yourself when you no
longer need them.

## Good habits

- Close the port before syncing. The runtime writes save files directly into mc0.
- Sync when you stop playing on one device and before you start on the next. Two devices playing the
  same game offline will conflict; that is the point of the conflict rule.
- `--dry-run` shows what would happen. `--push` and `--pull` limit a run to one direction.
- iCloud Drive can leave files in the cloud only (shown as `.NAME.icloud` placeholders). `sync`
  refuses while any are present; choose Download Now (or keep the folder downloaded) and run again.
- Files `sync` ignores: `.DS_Store`, `Thumbs.db`, `desktop.ini`, `._*`, PCSX2 `_pcsx2_*` metadata and
  its own `.console-*` files.
- A deleted save comes back on the next sync if the other side still has it. To remove a save
  everywhere, delete it in mc0 and in the cloud folder, then sync.
- Keep the cloud folder private. Saves are yours; never share it as a public link.
