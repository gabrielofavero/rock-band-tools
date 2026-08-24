# Rock Band Song Renamer

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

## Requirements

- Python 3 (Windows CPython includes `tkinter`, which is required for the
  GUI folder picker and log window)

## Usage

**Double-click `process_rb_songs.pyw`** to run it. A folder picker opens so
you can choose which directory to process. All output is shown in a scrollable
log window. The `bkp/` folder (if any duplicates need to be backed up) is
created inside the directory you select.

You can also run it from a terminal:

```
python process_rb_songs.pyw [--dir PATH] [--dry-run] [--no-collisions]
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
4. After renaming, files that collide with a `Name (2)`-style copy are grouped
   by canonical name. The largest / most recently modified file wins and is
   kept (renamed to the clean name); the rest are moved to `bkp/`.

## License

Provided as-is for personal use with legally owned Rock Band DLC.
