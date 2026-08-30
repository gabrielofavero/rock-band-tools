#!/usr/bin/env python3
"""rb_content_manager.pyw - RPCS3 Rock Band HDD content manager.

Lets you see every Rock Band game installed in an RPCS3 `dev_hdd0/game`
folder (identified by its PARAM.SFO title + game id), the song packages /
exports / licenses inside each game's USRDIR (with real song names read from
`Songs.dta`), and mark any of them for deletion (moved to the Windows
Recycle Bin, so it's reversible).

Double-click to run. Optional CLI usage:
    rb_content_manager.pyw [--dir PATH] [--show-all]

    --dir PATH      game directory to open (default:
                    C:\\Games\\Emulators\\RPCS3\\dev_hdd0\\game)
    --show-all      start with the "Only Rock Band content" filter off
"""

import os
import queue
import re
import struct
import sys
import threading

# tkinter is bundled with the standard Windows CPython build
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except Exception:  # pragma: no cover - only matters for headless runs
    tk = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GAME_DIR_DEFAULT = r'C:\Games\Emulators\RPCS3\dev_hdd0\game'

# USRDIR subfolders that are part of the game itself, never "content".
SYSTEM_USRDIR_SUBDIRS = {'gen'}

# RPCS3 internal dirs inside dev_hdd0/game that are not games.
SYSTEM_GAME_DIRS = {'locks'}

# Rock Band family titles all contain "rock band"
# (Rock Band, Rock Band 2/3, Rock Band Blitz, The Beatles: Rock Band,
#  Green Day: Rock Band, Rock Band 4 ...). Fallback id list is used when the
# PARAM.SFO is missing / unreadable.
RB_TITLE_KEYWORDS = ('rock band',)
KNOWN_RB_IDS = {
    # Rock Band
    'BLUS30050', 'BLES00532', 'NPUA30005',
    # Rock Band 2
    'BLUS30282', 'BLES00986', 'NPUB30000',
    # Rock Band 3
    'BLUS30463', 'BLES01103', 'NPUB30506',
    # Rock Band Blitz
    'NPUB30749', 'NPEB00737',
    # The Beatles: Rock Band
    'BLUS30359', 'BLES00808', 'NPUB30434',
    # Green Day: Rock Band
    'BLUS30439', 'BLES01043', 'NPUB30531',
    # Rock Band 4 (PS4)
    'BLUS31654',
}

# Fields in Songs.dta use quoted values; keys may be bare or single-quoted:
#   (name "Pressure")   or   ('name' "Pressure")
# The value may be single- OR double-quoted and can contain apostrophes, so
# capture the opening quote and close on the matching quote via backreference.
SONG_NAME_RE = re.compile(r"\(\s*'?name'?\s+([\"'])(.*?)\1")
SONG_ARTIST_RE = re.compile(r"\(\s*'?artist'?\s+([\"'])(.*?)\1")


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------
def parse_sfo(path):
    """Return a dict of the UTF-8 keys from a PS3 PARAM.SFO file."""
    try:
        data = open(path, 'rb').read()
    except OSError:
        return {}
    if data[:4] != b'\x00PSF':
        return {}
    key_off = struct.unpack_from('<I', data, 0x08)[0]
    data_off = struct.unpack_from('<I', data, 0x0C)[0]
    n = struct.unpack_from('<I', data, 0x10)[0]
    out = {}
    for i in range(n):
        e = 0x14 + i * 16
        koff, fmt, ln, maxln, doff = struct.unpack_from('<HHIII', data, e)
        key = data[key_off + koff:key_off + koff + 64].split(b'\x00')[0].decode('utf-8', 'replace')
        if fmt == 0x0204:
            val = data[data_off + doff:data_off + doff + ln].rstrip(b'\x00').decode('utf-8', 'replace')
            out[key] = val
    return out


def folder_size(path):
    """Total size in bytes of a folder tree."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _decode_text(raw):
    """Decode a Songs.dta blob, preserving accented characters.

    Rock Band Songs.dta files aren't always UTF-8 - many are single-byte
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


def read_dta_songs(dta_path):
    """Extract [(name, artist), ...] pairs from a Songs.dta file.

    The `(name "...")` token also appears for the song file path
    (e.g. "songs/foo/foo"); those are filtered out.
    """
    try:
        raw = open(dta_path, 'rb').read()
        txt = _decode_text(raw)
    except OSError:
        return []
    # findall on the backreference pattern returns (quote, value) tuples
    names = [v for _q, v in SONG_NAME_RE.findall(txt)]
    artists = [v for _q, v in SONG_ARTIST_RE.findall(txt)]
    pairs = []
    for i, n in enumerate(names):
        # skip the `(song (name "songs/<id>/<id>"))` path references
        if n.startswith('songs/'):
            continue
        a = artists[i] if i < len(artists) else ''
        pairs.append((n.strip(), a.strip()))
    return pairs


def is_rock_band(game_id, title):
    """A game is a Rock Band title if its PARAM.SFO title says so.

    The title is authoritative: the user's installs are modded and some known
    Rock Band ids have been repurposed (e.g. NPUA30005 is now "Peggle"), so
    never trust the id when a title is present. The known-id list is only a
    fallback for games whose PARAM.SFO is missing or unreadable.
    """
    if title and title != '?':
        return any(kw in title.lower() for kw in RB_TITLE_KEYWORDS)
    return game_id in KNOWN_RB_IDS


def classify_type(folder, songs, subdirs):
    """Pick a short, human label for a content folder."""
    up = folder.upper()
    if 'license' in subdirs and not songs:
        return 'License'
    if 'EXPORT' in up or 'FULLALBUM' in up:
        return 'Export'
    n = len(songs)
    if n == 0:
        return 'Other'
    if n == 1:
        return 'DLC song'
    if n >= 10:
        return 'Track pack'
    return 'DLC pack'


def songs_preview(pairs):
    """Build a short 'Artist - Song' preview for the Songs column."""
    if not pairs:
        return ''
    if len(pairs) == 1:
        n, a = pairs[0]
        return f'{a} - {n}' if a else n
    shown = []
    for n, a in pairs[:3]:
        shown.append(f'{a} - {n}' if a else n)
    text = '; '.join(shown)
    extra = len(pairs) - 3
    if extra > 0:
        text += f'  \u2026 +{extra} more'
    return f'{len(pairs)} songs: {text}'


def human_size(num):
    """Format a byte count as a compact human string."""
    try:
        num = float(num)
    except (TypeError, ValueError):
        return '?'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if num < 1024 or unit == 'TB':
            if unit == 'B':
                return f'{int(num)} {unit}'
            return f'{num:.1f} {unit}'
        num /= 1024.0
    return f'{num:.1f} TB'


def send_to_recycle_bin(path):
    """Move a file/folder to the Windows Recycle Bin (reversible delete).

    Returns None on success, or an error message string on failure.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ('hwnd', wintypes.HWND),
                ('wFunc', wintypes.UINT),
                ('pFrom', wintypes.LPCWSTR),
                ('pTo', wintypes.LPCWSTR),
                ('fFlags', ctypes.c_uint16),
                ('fAnyOperationsAborted', wintypes.BOOL),
                ('hNameMappings', wintypes.LPVOID),
                ('lpszProgressTitle', wintypes.LPCWSTR),
            ]

        if not os.path.exists(path):
            return 'path does not exist'

        FO_DELETE = 0x0003
        FOF_SILENT = 0x0004
        FOF_NOCONFIRMATION = 0x0010
        FOF_ALLOWUNDO = 0x0040

        p_from = ctypes.create_unicode_buffer(path + '\x00')  # double NUL term
        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        op.pFrom = ctypes.cast(p_from, wintypes.LPCWSTR)
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
        res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        return None if res == 0 else f'SHFileOperation returned {res}'
    except Exception as e:  # pragma: no cover
        return str(e)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root, game_dir, show_all):
        self.root = root
        self.game_dir = game_dir
        self.only_rb = tk.BooleanVar(value=not show_all)

        self.item_by_path = {}   # path -> item dict
        self.game_nodes = {}     # game_id -> tree node id
        self.game_paths = {}     # game_id -> game folder path
        self.game_titles = {}    # game_id -> display title
        self.game_delete = {}    # game_id -> bool (mark whole game folder)
        self.ui_queue = queue.Queue()
        self.scanning = False

        self._build_ui()
        self.root.after(60, self._poll_queue)
        self.start_scan()

    # -- UI construction ----------------------------------------------------
    def _build_ui(self):
        self.root.title('RPCS3 Rock Band Content Manager')
        self.root.geometry('1180x720')
        self.root.minsize(860, 500)

        top = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        top.pack(fill='x')

        ttk.Label(top, text='Game dir:').pack(side='left')
        self.dir_var = tk.StringVar(value=self.game_dir)
        self.dir_entry = ttk.Entry(top, textvariable=self.dir_var, width=55)
        self.dir_entry.pack(side='left', padx=(6, 4), fill='x', expand=True)
        ttk.Button(top, text='Browse\u2026', command=self.browse_dir).pack(side='left')
        ttk.Button(top, text='Rescan', command=self.start_scan).pack(side='left', padx=(4, 0))

        filt = ttk.Checkbutton(
            top, text='Only Rock Band content', variable=self.only_rb,
            command=self.start_scan)
        filt.pack(side='left', padx=(10, 0))

        # -- tree ------------------------------------------------------------
        mid = ttk.Frame(self.root, padding=(8, 4, 8, 4))
        mid.pack(fill='both', expand=True)

        cols = ('marked', 'type', 'songs', 'size')
        self.tree = ttk.Treeview(mid, columns=cols, selectmode='browse')
        self.tree.heading('#0', text='Content', anchor='w')
        self.tree.column('#0', width=320, minwidth=170, anchor='w')
        self.tree.heading('marked', text='Mark')
        self.tree.column('marked', width=48, minwidth=42, anchor='center', stretch=False)
        self.tree.heading('type', text='Type')
        self.tree.column('type', width=96, minwidth=72, anchor='w', stretch=False)
        self.tree.heading('songs', text='Song(s)', anchor='w')
        self.tree.column('songs', width=560, minwidth=200, anchor='w')
        self.tree.heading('size', text='Size')
        self.tree.column('size', width=90, minwidth=70, anchor='e', stretch=False)

        vsb = ttk.Scrollbar(mid, orient='vertical', command=self.tree.yview)
        hsb = ttk.Scrollbar(mid, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)

        self.tree.tag_configure('game', font=('Segoe UI', 10, 'bold'))
        self.tree.tag_configure('content', font=('Segoe UI', 10))
        self.tree.tag_configure('nonrb', foreground='#8a8a8a')
        self.tree.tag_configure('empty', foreground='#b06a00')
        self.tree.tag_configure('even', background='#f4f4f4')

        self.tree.bind('<Button-1>', self._on_click)
        self.tree.bind('<Double-1>', self._on_double_click)
        self.tree.bind('<Button-3>', self._on_right_click)
        self.root.bind('<Delete>', lambda e: self.delete_marked())

        # -- context menu -----------------------------------------------------
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label='Open folder in Explorer', command=self._menu_open)
        self.menu.add_command(label='Delete entire game folder\u2026', command=self._menu_delete_game)
        self.menu.add_command(label='Toggle mark', command=self._menu_toggle)
        self.menu.add_separator()
        self.menu.add_command(label='Select all', command=self.select_all)
        self.menu.add_command(label='Select none', command=self.select_none)

        # -- footer -----------------------------------------------------------
        bottom = ttk.Frame(self.root, padding=(8, 4, 8, 8))
        bottom.pack(fill='x')

        self.status_var = tk.StringVar(value='Ready.')
        self.marked_var = tk.StringVar(value='Marked: 0 items \u00b7 0 B')

        ttk.Label(bottom, textvariable=self.status_var).pack(side='left')
        ttk.Label(bottom, textvariable=self.marked_var).pack(side='left', padx=(16, 0))

        ttk.Button(bottom, text='Select all', command=self.select_all).pack(side='right', padx=(6, 0))
        ttk.Button(bottom, text='Select none', command=self.select_none).pack(side='right')
        self.del_btn = ttk.Button(
            bottom, text='Delete marked (Recycle Bin)', command=self.delete_marked)
        self.del_btn.pack(side='right', padx=(0, 12))

    # -- thread <-> UI bridge -------------------------------------------------
    def _post(self, fn):
        """Schedule `fn` to run on the UI thread from a worker."""
        self.ui_queue.put(fn)

    def _poll_queue(self):
        try:
            while True:
                fn = self.ui_queue.get_nowait()
                fn()
        except queue.Empty:
            pass
        self.root.after(60, self._poll_queue)

    # -- scanning --------------------------------------------------------------
    def start_scan(self):
        if self.scanning:
            return
        self.scanning = True
        self.del_btn.state(['disabled'])
        self._clear_tree()
        self.status_var.set('Scanning\u2026')
        self.marked_var.set('Marked: 0 items \u00b7 0 B')
        # capture Tk state on the main thread; never touch Tk vars from the worker
        game_dir = self.dir_var.get().strip().strip('"')
        only_rb = bool(self.only_rb.get())
        threading.Thread(
            target=self._scan_worker, args=(game_dir, only_rb), daemon=True).start()

    def _clear_tree(self):
        for node in self.tree.get_children():
            self.tree.delete(node)
        self.item_by_path.clear()
        self.game_nodes.clear()
        self.game_paths.clear()
        self.game_titles.clear()
        self.game_delete.clear()

    def _scan_worker(self, game_dir, only_rb):
        self.game_dir = game_dir

        try:
            entries = sorted(os.listdir(game_dir))
        except OSError as e:
            self._post(lambda: messagebox.showerror(
                'Cannot open folder', f'Could not read:\n{game_dir}\n\n{e}'))
            self._post(self._scan_done)
            return

        n_games = 0
        for gid in entries:
            gpath = os.path.join(game_dir, gid)
            if not os.path.isdir(gpath):
                continue
            if gid.startswith('\uff04') or 'locks' in gid.lower():
                continue  # RPCS3 internal dirs

            sfo = parse_sfo(os.path.join(gpath, 'PARAM.SFO'))
            title = sfo.get('TITLE') or '?'
            rb = is_rock_band(gid, title)
            if only_rb and not rb:
                continue

            title_show = title
            if 'CACHEDATA' in gid.upper():
                title_show += ' (cache data)'

            usrdir = os.path.join(gpath, 'USRDIR')
            self._post(lambda t=title_show: self.status_var.set(
                f'Scanning {t} ({gid})\u2026'))

            items = []
            size_total = 0
            nsongs = 0
            if os.path.isdir(usrdir):
                for folder in sorted(os.listdir(usrdir)):
                    fp = os.path.join(usrdir, folder)
                    if not os.path.isdir(fp):
                        continue
                    if folder in SYSTEM_USRDIR_SUBDIRS:
                        continue
                    item = self._build_item(gid, title, folder, fp, rb)
                    items.append(item)
                    size_total += item['size']
                    nsongs += len(item['songs'])
                    self._post(lambda g=gid, t=title_show, n=len(items):
                               self.status_var.set(
                                   f'Scanning {t} ({g}) \u2026 {n} items'))

            n_games += 1
            self._post(lambda gi=gid, gp=gpath, ti=title_show, it=items,
                       st=size_total, sn=nsongs, r=rb:
                       self._add_game(gi, gp, ti, it, st, sn, r))

        self._post(lambda ng=n_games: self._scan_done(ng))

    def _build_item(self, gid, title, folder, fp, rb):
        subdirs = [d for d in os.listdir(fp) if os.path.isdir(os.path.join(fp, d))]
        pairs = []
        for cand in (os.path.join(fp, 'songs', 'songs.dta'),
                     os.path.join(fp, 'songs.dta')):
            if os.path.isfile(cand):
                pairs = read_dta_songs(cand)
                if pairs:
                    break
        item = {
            'path': fp,
            'game_id': gid,
            'game_title': title,
            'folder': folder,
            'type': classify_type(folder, pairs, subdirs),
            'songs': pairs,
            'size': folder_size(fp),
            'marked': False,
            'node': None,
            'rb': rb,
        }
        return item

    # -- UI insert --------------------------------------------------------------
    def _add_game(self, gid, gpath, title, items, size_total, nsongs, rb):
        if items:
            header = (f'\U0001f3b8 {title}  ({gid})  \u2014  {len(items)} items '
                      f'\u00b7 {nsongs} songs \u00b7 {human_size(size_total)}')
        else:
            header = f'\U0001f3b8 {title}  ({gid})  \u2014  no song content'
        tags = ['game'] + ([] if rb else ['nonrb']) + (['empty'] if not items else [])
        node = self.tree.insert('', 'end', text=header, values=('', '', '', ''), tags=tags)
        self.game_nodes[gid] = node
        self.game_titles[gid] = title
        self.game_paths[gid] = gpath
        self.game_delete[gid] = False

        for i, item in enumerate(items):
            tags_i = ['content'] + ([] if item['rb'] else ['nonrb'])
            if i % 2:
                tags_i.append('even')
            song_text = songs_preview(item['songs'])
            values = ('\u2610', item['type'], song_text, human_size(item['size']))
            inode = self.tree.insert(node, 'end', text=item['folder'],
                                     values=values, tags=tags_i)
            item['node'] = inode
            self.item_by_path[item['path']] = item

        self.tree.item(node, open=True)
        self._update_game_mark(gid)

    def _scan_done(self, n_games):
        self.scanning = False
        self.del_btn.state(['!disabled'])
        self.status_var.set(
            f'Done. {n_games} game(s) listed \u2014 '
            f'{len(self.item_by_path)} content item(s). '
            f'Deleted items go to the Windows Recycle Bin.')
        self._update_marked_summary()

    # -- marking ---------------------------------------------------------------
    def _on_click(self, event):
        if self.scanning:
            return
        node = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not node or col != '#1':  # only the Mark column toggles
            return
        if node in self.game_nodes.values():
            self._toggle_game(node)
            self._update_marked_summary()
            return
        item = next((it for it in self.item_by_path.values() if it['node'] == node), None)
        if item:
            item['marked'] = not item['marked']
            self._refresh_item(item)
            self._update_game_mark(item['game_id'])
        self._update_marked_summary()

    def _toggle_game(self, node):
        # determine game_id for this header node
        gid = next((g for g, n in self.game_nodes.items() if n == node), None)
        if not gid:
            return
        children = [it for it in self.item_by_path.values() if it['game_id'] == gid]
        if not children:
            # empty game -> toggle whole-folder deletion
            self.game_delete[gid] = not self.game_delete.get(gid, False)
            self._update_game_mark(gid)
            self._update_marked_summary()
            return
        # mark all if any is unmarked, else unmark all
        target = any(not it['marked'] for it in children)
        for it in children:
            it['marked'] = target
            self._refresh_item(it)
        self._update_game_mark(gid)
        self._update_marked_summary()

    def _refresh_item(self, item):
        vals = list(self.tree.item(item['node'], 'values'))
        vals[0] = '\u2611' if item['marked'] else '\u2610'
        self.tree.item(item['node'], values=vals)

    def _update_game_mark(self, gid):
        node = self.game_nodes.get(gid)
        if not node:
            return
        children = [it for it in self.item_by_path.values() if it['game_id'] == gid]
        if not children:
            sym = '\u2611' if self.game_delete.get(gid, False) else '\u2610'
        else:
            marked = sum(1 for it in children if it['marked'])
            if marked == 0:
                sym = '\u2610'
            elif marked == len(children):
                sym = '\u2611'
            else:
                sym = '\u25a3'
        vals = list(self.tree.item(node, 'values'))
        vals[0] = sym
        self.tree.item(node, values=vals)

    def select_all(self):
        for it in self.item_by_path.values():
            if not it['marked']:
                it['marked'] = True
                self._refresh_item(it)
        for gid, items in self._games_items().items():
            if not items:
                self.game_delete[gid] = True
        for gid in self.game_nodes:
            self._update_game_mark(gid)
        self._update_marked_summary()

    def select_none(self):
        for it in self.item_by_path.values():
            if it['marked']:
                it['marked'] = False
                self._refresh_item(it)
        for gid in self._games_items():
            self.game_delete[gid] = False
        for gid in self.game_nodes:
            self._update_game_mark(gid)
        self._update_marked_summary()

    def _games_items(self):
        """Return {game_id: [items...]} built from the item registry."""
        grouped = {}
        for it in self.item_by_path.values():
            grouped.setdefault(it['game_id'], []).append(it)
        return grouped

    def _update_marked_summary(self):
        marked = [it for it in self.item_by_path.values() if it['marked']]
        total = sum(it['size'] for it in marked)
        # whole-folder game deletions
        games_to_delete = [gid for gid, flag in self.game_delete.items() if flag]
        total += sum(folder_size(self.game_paths[gid]) for gid in games_to_delete if gid in self.game_paths)
        count = len(marked) + len(games_to_delete)
        self.marked_var.set(f'Marked: {count} items \u00b7 {human_size(total)}')

    # -- deletion ---------------------------------------------------------------
    def delete_marked(self):
        if self.scanning:
            return
        marked = [it for it in self.item_by_path.values() if it['marked']]
        games = [gid for gid, flag in self.game_delete.items()
                 if flag and gid in self.game_paths]
        if not marked and not games:
            messagebox.showinfo('Nothing selected',
                                'No items are marked for deletion.\n'
                                'Click the Mark column (or use Select all) first.')
            return

        total = sum(it['size'] for it in marked) + sum(
            folder_size(self.game_paths[g]) for g in games)
        names = [f'  \u2022 {it["game_title"]} / {it["folder"]}' for it in marked[:20]]
        names += [f'  \u2022 WHOLE GAME: {self.game_titles.get(g, g)} ({g})'
                  for g in games[:5]]
        if len(marked) + len(games) > 20:
            names.append(f'  \u2026 and {len(marked) + len(games) - 20} more')
        detail = ('\n'.join(names) + f'\n\nTotal: {len(marked) + len(games)} item(s) \u00b7 {human_size(total)}'
                  '\n\nThey will be moved to the Windows Recycle Bin (reversible).')

        ok = messagebox.askyesno('Confirm deletion', detail, icon='warning')
        if not ok:
            return

        errors = []
        for it in marked:
            err = send_to_recycle_bin(it['path'])
            if err:
                errors.append(f'{it["folder"]}: {err}')
        for g in games:
            err = send_to_recycle_bin(self.game_paths[g])
            if err:
                errors.append(f'game {g}: {err}')

        if errors:
            messagebox.showerror(
                'Some items could not be deleted',
                '\n'.join(errors[:20]) + ('\n\u2026' if len(errors) > 20 else ''))
        else:
            messagebox.showinfo('Deleted', f'Moved {len(marked) + len(games)} item(s) to the Recycle Bin.')
        self.start_scan()

    # -- misc interactions --------------------------------------------------------
    def browse_dir(self):
        chosen = filedialog.askdirectory(
            title='Select the RPCS3 dev_hdd0/game folder',
            initialdir=self.dir_var.get() or os.getcwd())
        if chosen:
            self.dir_var.set(chosen)
            self.start_scan()

    def _on_double_click(self, event):
        node = self.tree.identify_row(event.y)
        item = next((it for it in self.item_by_path.values() if it['node'] == node), None)
        if item:
            try:
                os.startfile(item['path'])  # noqa: S606 - open folder in Explorer
            except OSError as e:
                messagebox.showerror('Open folder', str(e))

    def _on_right_click(self, event):
        node = self.tree.identify_row(event.y)
        if node:
            self.tree.selection_set(node)
        self._menu_node = node
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _node_game_id(self, node):
        """Return the game_id for a tree node (header or content row)."""
        if node in self.game_nodes.values():
            return next((g for g, n in self.game_nodes.items() if n == node), None)
        item = self._node_item(node)
        return item['game_id'] if item else None

    def _node_item(self, node):
        return next((it for it in self.item_by_path.values() if it['node'] == node), None)

    def _menu_open(self):
        node = getattr(self, '_menu_node', None)
        gid = self._node_game_id(node)
        if gid:
            try:
                os.startfile(self.game_paths[gid])  # noqa: S606
                return
            except OSError as e:
                messagebox.showerror('Open folder', str(e))
                return
        item = self._node_item(node)
        if item:
            try:
                os.startfile(item['path'])  # noqa: S606
            except OSError as e:
                messagebox.showerror('Open folder', str(e))

    def _menu_toggle(self):
        node = getattr(self, '_menu_node', None)
        gid = self._node_game_id(node)
        if gid and node in self.game_nodes.values():
            self._toggle_game(node)
            self._update_marked_summary()
            return
        item = self._node_item(node)
        if item:
            item['marked'] = not item['marked']
            self._refresh_item(item)
            self._update_game_mark(item['game_id'])
            self._update_marked_summary()

    def _menu_delete_game(self):
        node = getattr(self, '_menu_node', None)
        gid = self._node_game_id(node)
        if not gid or gid not in self.game_paths:
            return
        gpath = self.game_paths[gid]
        title = self.game_titles.get(gid, gid)
        ok = messagebox.askyesno(
            'Delete entire game',
            f'Delete the whole game folder?\n\n  {title}  ({gid})\n  {gpath}\n\n'
            f'Size: {human_size(folder_size(gpath))}\n\n'
            'It will be moved to the Windows Recycle Bin (reversible).',
            icon='warning')
        if not ok:
            return
        err = send_to_recycle_bin(gpath)
        if err:
            messagebox.showerror('Delete failed', f'{title} ({gid}): {err}')
        else:
            messagebox.showinfo('Deleted', f'"{title}" moved to the Recycle Bin.')
        self.start_scan()


def main():
    args = sys.argv[1:]
    game_dir = GAME_DIR_DEFAULT
    show_all = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--dir':
            i += 1
            if i < len(args):
                game_dir = os.path.abspath(args[i])
        elif a == '--show-all':
            show_all = True
        i += 1

    if tk is None:
        print('tkinter is not available - cannot start the GUI.', file=sys.stderr)
        return 1

    root = tk.Tk()
    if os.path.isdir(game_dir):
        app = App(root, game_dir, show_all)
    else:
        # let the user pick a folder
        chosen = filedialog.askdirectory(
            title='Select the RPCS3 dev_hdd0/game folder',
            initialdir=os.path.dirname(game_dir) if os.path.dirname(game_dir) else os.getcwd())
        if not chosen:
            root.destroy()
            return 0
        app = App(root, chosen, show_all)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
