# rock-band-tools

A small collection of Python scripts (Windows, tkinter-based) for working with
Rock Band DLC song packages and RPCS3 Rock Band content.

## Scripts at a glance

| Script | What it does |
| --- | --- |
| `rename_rb_files.pyw` | Renames Rock Band DLC song packages to their real in-game `Artist - Song` names |
| `rb_content_manager.pyw` | Browses RPCS3 Rock Band installs and moves unwanted content to the Recycle Bin |
| `compare_pkg_con.pyw` | Compares the contents of a PKG folder against a CON folder by file name |

All three are GUI tools (double-click to run) and also accept command-line
arguments for scripting / headless use.

---

# `rename_rb_files.pyw` — Rock Band Song Renamer

Renames Rock Band DLC song packages to their real in-game display names
(`Artist - Song`) instead of the cryptic file names they ship with.

It reads the actual title from each package header (or from the `songs.dta`
inside the package when needed), sanitizes it for use as a filename, and
renames the file. Colliding copies (e.g. `Song (2)`) are resolved by keeping
the best duplicate and moving the rest to a `bkp/` folder.

## Supported formats

| Format | Extension | Source of the name |
| --- | --- | --- |
| Xbox 360 STFS (`CON`, `LIVE`, `PIRS`) | none | STFS header `DisplayName[0]`, then `songs.dta` / `ArcadeInfo.xml`, then ASCII header scan |
| PS3 PKG | `.pkg` | `songs.dta` (decrypted via the pypkg stream cipher), then content ID |

## Usage

**Double-click `rename_rb_files.pyw`** to run it. A folder picker opens so
you can choose which directory to process. All output is shown in a scrollable
log window. The `bkp/` folder (if any duplicates need to be backed up) is
created inside the directory you select.

You can also run it from a terminal:

```
python rename_rb_files.pyw [--dir PATH] [--dry-run] [--no-collisions]
```

| Option | Description |
| --- | --- |
| `--dir PATH` | Process a specific directory (skips the folder picker) |
| `--dry-run` | Show what would be renamed without touching any files |
| `--no-collisions` | Skip the collision-resolution step |

> **Tip:** run with `--dry-run` first to preview what the tool will do.

## How renaming works

1. Every file in the selected directory is inspected by its magic bytes:
   STFS packages (`CON `, `LIVE`, `PIRS`) and PS3 PKGs (`\x7fPKG`) are
   recognized; anything else is left alone.
2. The real display name is extracted from the package header, falling back to
   parsing `songs.dta` inside the package if needed.
3. The name is sanitized (illegal filename characters and periods are
   stripped) and the file is renamed. PS3 PKGs keep their `.pkg` extension.

> Accented / special characters are preserved (`Naïve`, `¡Viva la Gloria!`):
> `songs.dta` is decoded as UTF-8 with a Windows-1252 fallback, and a mangled
> STFS header is ignored in favor of the accented title inside the package.
4. After renaming, files that collide with a `Name (2)`-style copy are grouped
   by canonical name. The largest / most recently modified file wins and is
   kept (renamed to the clean name); the rest are moved to `bkp/`.

---

# `rb_content_manager.pyw` — RPCS3 Rock Band Content Manager

Helps you see and clean up the Rock Band content installed in an RPCS3 HDD's
`dev_hdd0/game` folder.

## What it does

- Scans `C:\Games\Emulators\RPCS3\dev_hdd0\game` (or any folder you pick) and
  lists only **Rock Band** content (matched by the `PARAM.SFO` title).
- For each Rock Band game it shows the song packages / exports / licenses
  inside its `USRDIR`, the real **Artist - Song** names (read from each
  package's `Songs.dta`), and each item's size.
- Each game is labeled with its **official name**, looked up from
  SerialStation's PS3 title-id database (`https://serialstation.com`), so the
  correct name is shown for every PS3 id.
- **Search box**: type an artist, a song, or an `Artist - Song` combination and
  the list filters live - including songs that live inside a pack.
- **Duplications are highlighted in yellow**: any item whose song also exists
  in another item (e.g. a track pack *and* a standalone DLC) is tinted yellow.
- **Show duplications only** checkbox narrows the list to those items.
- You can mark **one item, a whole game's content, or everything** for
  deletion. Marked items are moved to the **Windows Recycle Bin** (reversible),
  never deleted permanently.
- Empty Rock Band installs are shown too, and you can delete an entire game
  folder from the right-click menu.

## Usage

**Double-click `rb_content_manager.pyw`** to run it. The game folder is scanned
automatically in the background.

Optional CLI arguments:

| Option | Description |
| --- | --- |
| `--dir PATH` | Open a specific game directory (skips the default / picker) |

## Controls

| Control | What it does |
| --- | --- |
| `Search` box | Filters the tree as you type (artist, song, or `Artist - Song`; matches inside packs too) |
| `Show duplications only` | Shows only items whose songs also appear in another item |
| `Mark` column | Click the ☐/☑ cell on a song to mark/unmark it; click a game's cell to mark/unmark all of its songs |
| `Select all` / `Select none` | Mark / unmark everything |
| `Delete marked (Recycle Bin)` | Confirms first, then moves marked content (and any marked whole games) to the Recycle Bin |
| Right-click a row | Open the folder in Explorer, toggle its mark, **Show duplicated items**, or delete the entire game |
| Double-click a song row | Open that song's folder in Explorer |

**Show duplicated items** (right-click menu) selects the next item that
shares a song with the one you clicked; each further click cycles through all
the duplicates so you can review them one by one.

Only folders in a game's `USRDIR` are treated as content; the game engine
folder (`gen`) and loose files (`EBOOT.BIN`, `.dta`, etc.) are left alone.

---

# `compare_pkg_con.pyw` — PKG vs CON Comparer

Compares a folder of PS3 `.pkg` song packages against a folder of Xbox 360
`.con` packages and shows which files exist in one but not the other. The
comparison ignores file extensions and is case-insensitive (matching how
Windows treats file names).

## What it does

- Scans the top-level files of the two folders you pick (PKG dir and CON dir).
- Logs a full breakdown:
  - Files present **only in the PKG folder**
  - Files present **only in the CON folder**
  - How many files are present in **both**
  - **Count mismatches** — the same stem, but a different number of files on
    each side
  - **Ambiguous stems** — multiple files in one folder sharing the same stem
    (which would make the comparison ambiguous)

## Usage

**Double-click `compare_pkg_con.pyw`** to open the GUI: type or browse to the
two folders, then hit **Compare**. Results appear in the log window.

You can also run it entirely from a terminal:

```
python compare_pkg_con.pyw --pkg PATH --con PATH
```

| Option | Description |
| --- | --- |
| `--pkg PATH` | PKG directory (skips that folder picker) |
| `--con PATH` | CON directory (skips that folder picker) |

When both `--pkg` and `--con` are given, the comparison prints to the
terminal; otherwise the GUI opens so you can pick the folders.

---

## Requirements

- Python 3 on Windows — the standard CPython build includes `tkinter`, which
  all three tools use for their folder pickers and log windows.

## License

Provided as-is for personal use with legally owned Rock Band DLC.
