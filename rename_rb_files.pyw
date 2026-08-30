#!/usr/bin/env python3
"""process_rb_songs.pyw

Double-click the file to run it: a folder picker opens so you can choose which
directory to process (the bkp/ folder is created inside that same directory),
and all output is streamed into a scrollable log window.

Optional CLI usage:
    process_rb_songs.pyw [--dir PATH] [--dry-run] [--no-collisions]
"""

import hashlib
import os
import re
import shutil
import struct
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, scrolledtext, messagebox
    _HAS_TK = True
except Exception:
    tk = None
    filedialog = scrolledtext = messagebox = None
    _HAS_TK = False

# ---------------------------------------------------------------------------
# STFS constants (layout from the Xbox360Container.bt template / stfschk)
# ---------------------------------------------------------------------------
MAGICS = (b'CON ', b'LIVE', b'PIRS')
MAGIC_NAMES = {b'CON ': 'CON', b'LIVE': 'LIVE', b'PIRS': 'PIRS'}

# XCONTENT_HEADER is always 0x344 bytes: 4 (magic) + 0x228 (signature) +
# 0x100 (license descriptors) + 0x14 (content id) + 4 (size of headers).
# XCONTENT_METADATA starts right after, and DisplayName[0] (the primary display
# name) sits 205 bytes into the metadata. => DisplayName[0] at file offset 0x411.
_METADATA_OFFSET = 0x344
_PRE_DISPLAYNAME = (4 + 4 + 8 + 0x18 + 5 + 8 + 0x24 + 4 + 8 + 4 + 8 + 4
                    + 0x20 + 0x24 + 0x14)
DISPLAYNAME_OFFSET = _METADATA_OFFSET + _PRE_DISPLAYNAME  # 0x411
_UCHAR80_SIZE = 0x100  # 80 UTF-16 code units


# ---------------------------------------------------------------------------
# Name extraction
# ---------------------------------------------------------------------------
def read_header_display_name(path):
    """Read the STFS header DisplayName[0] (what Horizon shows)."""
    with open(path, 'rb') as f:
        f.seek(DISPLAYNAME_OFFSET)
        raw = f.read(_UCHAR80_SIZE)
    name = raw.decode('utf-16-be', errors='replace')
    name = name.strip('\x00 \t\r\n')
    # A misread header shows up as U+FFFD replacement characters; treat it as
    # "no usable name" so the caller falls back to songs.dta / ArcadeInfo.xml,
    # which preserve accented characters like ï and ¡.
    if '\ufffd' in name:
        return ''
    return name


def _decode_text(raw):
    """Decode a package text blob, preserving accented characters.

    Rock Band songs.dta files aren't always UTF-8 - many are single-byte
    Windows-1252. Try strict UTF-8 first so real Unicode survives; on any
    failure (or when the result is full of U+FFFD replacement chars from a
    masked bad decode) fall back to Windows-1252, which maps every byte 1:1
    and never drops characters like ï (0xEF) or ¡ (0xA1).
    """
    try:
        text = raw.decode('utf-8')
        if '\ufffd' not in text:
            return text
    except UnicodeDecodeError:
        pass
    return raw.decode('cp1252', errors='replace')


def _is_ascii(text):
    """True when a string contains only ASCII characters."""
    return all(ord(c) < 128 for c in text)


def _parse_stfs_header(path):
    """Return (magic, data_start, hash_offset, file_table)."""
    with open(path, 'rb') as f:
        fsize = os.path.getsize(path)
        magic = f.read(4)
        if magic not in MAGICS:
            raise ValueError(f"Not a LIVE/PIRS/CON file: {magic}")
        if fsize < 0xD000:
            raise ValueError(f"File too small: {fsize} bytes")

        f.seek(0xC032)
        pathind = struct.unpack(">H", f.read(2))[0]
        start = 0xC000 if pathind == 0xFFFF else 0xD000
        offset = 0x1000 if start == 0xC000 else 0x2000

        f.seek(start + 0x2F)
        firstclust = struct.unpack("<H", f.read(2))[0]
        max_ft_blocks = max(firstclust, 16)

        f.seek(start)
        ft_data = f.read(0x1000 * max_ft_blocks)

    return magic, start, offset, ft_data


def _get_cluster(startclust, offset):
    """Real starting cluster offset (wxPirs algorithm)."""
    rst = 0
    while startclust >= 170:
        startclust //= 170
        rst += (startclust + 1) * offset
    return rst


def _list_entries(path):
    """List file entries in the STFS package."""
    _, start, offset, ft_data = _parse_stfs_header(path)
    paths = {0xFFFF: ""}
    entries = []

    for i in range(len(ft_data) // 64):
        cur = ft_data[i * 64:(i + 1) * 64]
        namelen_flags = cur[40]
        name_len = namelen_flags & 0x3F
        is_dir = bool(namelen_flags & 0x80)

        if name_len == 0:
            break
        if name_len < 1 or name_len > 40:
            continue

        outname = cur[0:name_len].decode('ascii', errors='replace')
        startclust = struct.unpack("<H", cur[47:49])[0] + (cur[49] << 16)
        pathind = struct.unpack(">H", cur[50:52])[0]
        filelen = struct.unpack(">I", cur[52:56])[0]
        parent = paths.get(pathind, "")

        if is_dir:
            paths[i] = parent + outname + "/"
        else:
            entries.append({
                "name": outname, "path": parent + outname, "size": filelen,
                "startclust": startclust, "is_dir": False,
            })

    return entries, start, offset


def _read_entry_bytes(path, entry, start, offset):
    """Read one file entry's bytes from the package."""
    adstart = entry["startclust"] * 0x1000 + start
    remaining = entry["size"]
    data = bytearray()
    cur_clust = entry["startclust"]

    with open(path, 'rb') as f:
        while remaining > 0:
            realstart = adstart + _get_cluster(cur_clust, offset)
            f.seek(realstart)
            chunk = f.read(min(0x1000, remaining))
            if not chunk:
                break
            data.extend(chunk)
            cur_clust += 1
            adstart += 0x1000
            remaining -= len(chunk)

    return bytes(data[:entry["size"]])


def _parse_dta_artist_name(text):
    """Pull 'artist' and top-level 'name' out of a Rock Band songs.dta."""
    def grab(key):
        m = re.search(r"'" + key + r"'\s*\"([^\"]*)\"", text)
        return m.group(1).strip() if m else None

    artist = grab('artist')
    name = grab('name')
    if artist and name:
        return f"{artist} - {name}"
    return name or artist


def read_contents_name(path):
    """Fallback: parse songs.dta / ArcadeInfo.xml inside the package."""
    try:
        entries, start, offset = _list_entries(path)
    except Exception:
        return None

    for e in entries:
        lower = e["name"].lower()

        if lower == "songs.dta":
            raw = _read_entry_bytes(path, e, start, offset)
            text = _decode_text(raw)
            name = _parse_dta_artist_name(text)
            if name:
                return name

        if lower == "arcadeinfo.xml":
            raw = _read_entry_bytes(path, e, start, offset)
            try:
                root = ET.fromstring(raw)
            except Exception:
                # Latin-1/Windows-1252 XML without a declaration - decode and
                # re-parse so accented characters aren't lost.
                try:
                    root = ET.fromstring(_decode_text(raw))
                except Exception:
                    continue
            for elem in root.iter():
                if elem.tag.lower() in ("name", "title", "displayname"):
                    text = (elem.text or "").strip()
                    if text:
                        return text

    return None


def read_ascii_scan_name(path, start=0x300, end=0xD00):
    """Last resort: scan a header region for a readable ASCII string."""
    with open(path, 'rb') as f:
        f.seek(start)
        data = f.read(end - start)

    best = ""
    current = []
    for byte in data:
        if 32 <= byte < 127:
            current.append(chr(byte))
        else:
            if len(current) >= 5:
                word = "".join(current).strip()
                alpha = sum(1 for c in word if c.isalnum())
                if alpha >= 4 and len(word) > len(best):
                    best = word
            current = []
    if len(current) >= 5:
        word = "".join(current).strip()
        alpha = sum(1 for c in word if c.isalnum())
        if alpha >= 4 and len(word) > len(best):
            best = word

    return best or None


def get_stfs_real_name(path):
    """Return the real display name for an STFS package, or None."""
    try:
        header_name = read_header_display_name(path)
    except Exception:
        header_name = None

    # When the header is missing or only holds the ASCII form, look inside the
    # package: songs.dta / ArcadeInfo.xml carry the real accented title.
    if not header_name or _is_ascii(header_name):
        contents_name = read_contents_name(path)
        if contents_name:
            # Prefer the contents name when it keeps accented characters the
            # ASCII header dropped, e.g. "Naïve", "¡Viva la Gloria!".
            if not header_name or not _is_ascii(contents_name):
                return contents_name
            return header_name

    if header_name:
        return header_name

    return read_ascii_scan_name(path)


# ---------------------------------------------------------------------------
# PS3 PKG constants (layout from aldostools' pypkg, used by RB3 PS3 converters)
# ---------------------------------------------------------------------------
PKG_MAGIC = b'\x7fPKG'  # 0x7F504B47

_PKG_HEADER_FMT = struct.Struct('>IIIIIIQQQ')  # 0x00..0x2F
_PKG_CONTENT_ID_OFF = 0x30
_PKG_CONTENT_ID_LEN = 0x30
_PKG_QA_DIGEST_OFF = 0x60
_PKG_QA_DIGEST_LEN = 0x10
_PKG_HEADER_SIZE = 0x80


def _pkg_key_to_context(qa_digest):
    """Expand the 16-byte QA digest into the 0x40-byte stream-cipher context."""
    key = bytes(qa_digest[0:16])
    return key[0:8] + key[0:8] + key[8:16] + key[8:16] + b'\x00' * 0x20


def _pkg_crypt_slice(fh, qa_digest, data_off, start, length):
    """Decrypt data[start:start+length] of a pkg data section.

    The pypkg stream cipher XORs each 16-byte block with SHA1 of a 0x40-byte
    context whose last 8 bytes are a big-endian block counter (starting at 0).
    Only the requested slice is decrypted, so extracting a small file from a
    large package stays fast.
    """
    if length <= 0:
        return b''
    prefix = _pkg_key_to_context(qa_digest)[0:0x38]
    out = bytearray()
    pos = start
    end = start + length
    block = start // 16
    while pos < end:
        key = prefix + struct.pack('>Q', block & 0xFFFFFFFFFFFFFFFF)
        digest = hashlib.sha1(key).digest()
        off_in_block = pos - block * 16
        take = min(16 - off_in_block, end - pos)
        fh.seek(data_off + pos)
        chunk = fh.read(take)
        for i in range(take):
            out.append(chunk[i] ^ digest[off_in_block + i])
        pos += take
        block += 1
    return bytes(out)


def _read_pkg_header(path):
    """Parse a pypkg-style PS3 PKG header. Returns a dict, or None."""
    with open(path, 'rb') as f:
        head = f.read(_PKG_HEADER_SIZE)
    if len(head) < _PKG_HEADER_SIZE:
        return None
    magic, pkg_type, _, _, head_size, item_count, _, data_off, data_size = \
        _PKG_HEADER_FMT.unpack_from(head, 0)
    if magic != 0x7F504B47:
        return None
    fsize = os.path.getsize(path)
    if head_size != _PKG_HEADER_SIZE:
        return None
    if not (1 <= item_count <= 0x10000):
        return None
    if data_off >= fsize or data_size > fsize:
        return None
    content_id = head[_PKG_CONTENT_ID_OFF:
                      _PKG_CONTENT_ID_OFF + _PKG_CONTENT_ID_LEN]
    content_id = content_id.split(b'\x00', 1)[0].decode('latin1', 'replace')
    qa_digest = head[_PKG_QA_DIGEST_OFF:
                     _PKG_QA_DIGEST_OFF + _PKG_QA_DIGEST_LEN]
    return {
        'type': pkg_type,
        'item_count': item_count,
        'data_off': data_off,
        'data_size': data_size,
        'content_id': content_id,
        'qa_digest': qa_digest,
    }


def read_pkg_content_id(path):
    """Read the content ID string from a PS3 PKG header (offset 0x30)."""
    with open(path, 'rb') as f:
        head = f.read(_PKG_CONTENT_ID_OFF + _PKG_CONTENT_ID_LEN)
    if len(head) < _PKG_CONTENT_ID_OFF + _PKG_CONTENT_ID_LEN:
        return None
    if head[0:4] != PKG_MAGIC:
        return None
    cid = head[_PKG_CONTENT_ID_OFF:
               _PKG_CONTENT_ID_OFF + _PKG_CONTENT_ID_LEN]
    cid = cid.split(b'\x00', 1)[0].decode('latin1', 'replace')
    return cid or None


def read_pkg_header_name(path):
    """'Package name' of a PS3 PKG: the content ID (or its song-ID part)."""
    cid = read_pkg_content_id(path)
    if not cid:
        return None
    # Content IDs look like:  UP0001-BLUS30463_00-<song id>
    parts = cid.split('-', 2)
    if len(parts) == 3 and parts[2]:
        return parts[2].strip()
    return cid.strip()


def _pkg_list_entries(path, header):
    """Read the (decrypted) file table of a pypkg-style PS3 PKG."""
    entries = []
    with open(path, 'rb') as f:
        desc = _pkg_crypt_slice(f, header['qa_digest'], header['data_off'], 0,
                                header['item_count'] * 0x20)
        for i in range(header['item_count']):
            fh = desc[i * 0x20:(i + 1) * 0x20]
            name_off, name_len = struct.unpack_from('>II', fh, 0)
            foff, fsize = struct.unpack_from('>QQ', fh, 8)
            name = _pkg_crypt_slice(f, header['qa_digest'],
                                    header['data_off'], name_off, name_len)
            name = name.split(b'\x00', 1)[0].decode('latin1', 'replace')
            entries.append({'name': name, 'size': fsize, 'foff': foff})
    return entries


def read_pkg_contents_name(path):
    """Parse songs.dta from inside a PS3 PKG (requires stream-cipher decrypt)."""
    try:
        header = _read_pkg_header(path)
        if header is None:
            return None
        entries = _pkg_list_entries(path, header)
    except Exception:
        return None

    for e in entries:
        if e['name'].lower().endswith('songs.dta'):
            with open(path, 'rb') as f:
                raw = _pkg_crypt_slice(f, header['qa_digest'],
                                       header['data_off'], e['foff'], e['size'])
            text = _decode_text(raw)
            name = _parse_dta_artist_name(text)
            if name:
                return name
    return None


def get_pkg_real_name(path):
    """Return the real display name for a PS3 PKG package, or None.

    A PKG header only stores a content ID, not a display name, so the real
    "Artist - Song" comes from songs.dta inside the package. The content ID
    (the "package name") is used when the package contents can't be read.
    """
    name = read_pkg_contents_name(path)
    if name:
        return name
    name = read_pkg_header_name(path)
    if name:
        return name
    return read_ascii_scan_name(path, 0x0, 0x800)


# ---------------------------------------------------------------------------
# Sanitization & renaming
# ---------------------------------------------------------------------------
INVALID_CHARS = set('<>:"/\\|?*')


def sanitize_filename(name):
    """Strip characters that are illegal in filenames, plus '.' (period)."""
    out = []
    for c in name:
        o = ord(c)
        if o < 32 or c in INVALID_CHARS or c == '.':
            continue  # control chars, illegal chars, and periods
        out.append(c)
    return ''.join(out).strip(' .')


def unique_target(dirpath, target):
    """Return a non-colliding filename inside dirpath."""
    if not os.path.exists(os.path.join(dirpath, target)):
        return target
    i = 2
    while True:
        candidate = f"{target} ({i})"
        if not os.path.exists(os.path.join(dirpath, candidate)):
            return candidate
        i += 1


# ---------------------------------------------------------------------------
# Collision resolution (after renames)
# ---------------------------------------------------------------------------
COPY_PATTERN = re.compile(
    r"^(?P<stem>.+) \((?P<number>[1-9]\d*)\)(?P<suffix>\..*)?$"
)


def canonical_name(filename):
    """Collision-free name for a copy-suffixed filename, or None."""
    match = COPY_PATTERN.fullmatch(filename)
    if not match:
        return None
    return f"{match.group('stem')}{match.group('suffix') or ''}"


def unique_backup_path(backup_dir, filename):
    """Return a non-colliding destination inside backup_dir for filename."""
    destination = backup_dir / filename
    if not destination.exists():
        return destination

    path = Path(filename)
    suffixes = "".join(path.suffixes)
    stem = filename[:-len(suffixes)] if suffixes else filename
    counter = 1

    while True:
        destination = backup_dir / f"{stem} ({counter}){suffixes}"
        if not destination.exists():
            return destination
        counter += 1


def _pick_winner(candidates, clean_name):
    """Pick which duplicate to keep.

    The largest file wins. On equal size the most recently modified wins. If
    recency cannot be determined (or everything ties) the first one is kept
    (the cleanly named file, then alphabetical order).
    """
    ordered = sorted(candidates, key=lambda p: (p.name != clean_name, p.name))

    def sort_key(p):
        try:
            st = p.stat()
            return (st.st_size, st.st_mtime)
        except OSError:
            return (-1, float('-inf'))

    return max(ordered, key=sort_key)


def resolve_collisions(directory, dry_run=False):
    """Move copy-suffixed duplicates to bkp/, keeping one winner per name.

    Groups files by canonical name *including the extension*, so a CON and a
    .pkg sharing the same base name are different groups and both are kept.
    """
    directory = Path(directory).resolve()
    backup_dir = directory / "bkp"
    groups = defaultdict(list)

    for path in directory.iterdir():
        if not path.is_file():
            continue
        clean_name = canonical_name(path.name)
        if clean_name is not None:
            groups[clean_name].append(path)

    resolved = 0

    for clean_name in sorted(groups):
        clean_path = directory / clean_name
        candidates = groups[clean_name]
        if clean_path.is_file():
            candidates.append(clean_path)

        candidates = list(dict.fromkeys(candidates))
        winner = _pick_winner(candidates, clean_name)

        print(f"\n{clean_name}")
        print(f"  keep: {winner.name} ({winner.stat().st_size} bytes)")

        for path in candidates:
            if path == winner:
                continue
            destination = unique_backup_path(backup_dir, path.name)
            print(f"  backup: {path.name} -> bkp/{destination.name}")
            if not dry_run:
                backup_dir.mkdir(exist_ok=True)
                shutil.move(str(path), str(destination))

        if winner.name != clean_name:
            print(f"  rename: {winner.name} -> {clean_name}")
            if not dry_run:
                winner.rename(clean_path)

        resolved += 1

    if resolved == 0:
        print("No filename collisions found.")
    else:
        print(f"\nResolved {resolved} filename group(s).")

    return resolved


# ---------------------------------------------------------------------------
# GUI: folder picker + log window
# ---------------------------------------------------------------------------
def pick_directory(initial=None):
    """Open a native folder picker. Returns the chosen path or None if canceled."""
    root = tk.Tk()
    root.withdraw()  # hide the empty root window
    path = filedialog.askdirectory(
        title="Select the directory to process",
        initialdir=initial or os.getcwd(),
        parent=root,
    )
    root.destroy()
    return path or None


class _LogRedirect:
    """Redirect sys.stdout/sys.stderr into a scrollable Tk text widget."""

    def __init__(self, text_widget, root):
        self._text = text_widget
        self._root = root

    def write(self, string):
        if not string:
            return len(string)
        try:
            self._text.insert('end', string)
            self._text.see('end')
            self._root.update_idletasks()
            self._root.update()
        except tk.TclError:
            pass  # window was closed mid-run; keep processing silently
        return len(string)

    def flush(self):
        pass


def run_with_log_window(target_dir, dry_run, no_collisions):
    """Run processing, streaming all output into a scrollable Tk window."""
    if not _HAS_TK:
        return _process(target_dir, dry_run, no_collisions)

    root = tk.Tk()
    root.title(f"Process RB Songs - {os.path.basename(os.path.normpath(target_dir))}")
    root.geometry("960x640")
    root.minsize(640, 400)

    text = scrolledtext.ScrolledText(root, wrap='word', font=("Consolas", 10))
    text.pack(fill='both', expand=True)

    redirect = _LogRedirect(text, root)
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = redirect
    sys.stderr = redirect

    try:
        rc = _process(target_dir, dry_run, no_collisions)
        redirect.write(f"\n{'=' * 44}\nDone. You can close this window.\n")
    finally:
        sys.stdout = old_out
        sys.stderr = old_err

    try:
        root.mainloop()
    except tk.TclError:
        pass
    return rc


def _show_error(detail):
    """Show an unexpected error in a small Tk window (for .pyw runs)."""
    if not _HAS_TK:
        return
    root = tk.Tk()
    root.title("Process RB Songs - Error")
    text = scrolledtext.ScrolledText(root, wrap='word', font=("Consolas", 10))
    text.pack(fill='both', expand=True)
    text.insert('1.0', detail)
    text.see('1.0')
    tk.Button(root, text="Close", command=root.destroy).pack()
    root.mainloop()


# ---------------------------------------------------------------------------
# Main processing (all output goes through the log redirect)
# ---------------------------------------------------------------------------
def _process(target_dir, dry_run, no_collisions):
    print(f"Scanning: {target_dir}")
    if dry_run:
        print("DRY RUN - no files will be renamed\n")
    else:
        print()

    renamed = 0
    skipped = 0
    already = 0
    failures = []

    for fname in sorted(os.listdir(target_dir)):
        fpath = os.path.join(target_dir, fname)
        if not os.path.isfile(fpath):
            continue

        with open(fpath, 'rb') as f:
            magic = f.read(4)
        if magic in MAGICS:
            kind = MAGIC_NAMES[magic]
            real = get_stfs_real_name(fpath)
        elif magic == PKG_MAGIC:
            kind = "PKG"
            real = get_pkg_real_name(fpath)
        else:
            continue

        print(f"[{kind}] {fname}")

        if not real:
            print("       ! could not determine a real name, skipping")
            skipped += 1
            continue

        clean = sanitize_filename(real)
        if not clean:
            print(f"       ! name became empty after sanitizing ({real!r}), skipping")
            skipped += 1
            continue

        # A PS3 PKG keeps its ".pkg" extension; STFS packages stay extensionless.
        if kind == "PKG":
            clean += ".pkg"

        if fname == clean:
            print("       = already named correctly")
            already += 1
            continue

        target = unique_target(target_dir, clean)
        if dry_run:
            print(f"       -> {target}")
            renamed += 1
            continue

        try:
            os.rename(fpath, os.path.join(target_dir, target))
            print(f"       -> {target}")
            renamed += 1
        except OSError as e:
            print(f"       ! rename failed: {e}")
            failures.append((fname, str(e)))

    print("\nRename summary:")
    print(f"  renamed:      {renamed}")
    print(f"  already ok:   {already}")
    print(f"  skipped:      {skipped}")
    if failures:
        print(f"  failed:       {len(failures)}")
        for f, err in failures:
            print(f"    - {f}: {err}")

    if not no_collisions:
        print("\n" + "=" * 44)
        print("Collision resolution")
        print("=" * 44)
        resolve_collisions(target_dir, dry_run)
    else:
        print("\nCollision resolution skipped (--no-collisions).")

    return 0


def main():
    args = sys.argv[1:]
    target_dir = None
    dry_run = False
    no_collisions = False

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--dir":
            i += 1
            target_dir = os.path.abspath(args[i])
        elif a == "--dry-run":
            dry_run = True
        elif a == "--no-collisions":
            no_collisions = True
        else:
            if _HAS_TK:
                messagebox.showerror(
                    "Process RB Songs",
                    f"Unknown argument: {a}\n\n"
                    "Usage: process_rb_songs.pyw [--dir PATH] [--dry-run] [--no-collisions]",
                )
            else:
                print(f"Unknown argument: {a}")
            return 1
        i += 1

    # No directory given -> ask the user which folder to process.
    if target_dir is None:
        if not _HAS_TK:
            print("GUI is not available; pass a directory with --dir PATH.")
            return 1
        target_dir = pick_directory()
        if not target_dir:
            return 0  # user canceled

    if not os.path.isdir(target_dir):
        if _HAS_TK:
            messagebox.showerror("Process RB Songs",
                                 f"Directory not found:\n{target_dir}")
        else:
            print(f"Directory not found: {target_dir}")
        return 1

    return run_with_log_window(target_dir, dry_run, no_collisions)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        import traceback
        _show_error(traceback.format_exc())
        sys.exit(1)
