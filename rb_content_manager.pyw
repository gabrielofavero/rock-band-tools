#!/usr/bin/env python3
"""rb_content_manager.pyw - RPCS3 Rock Band HDD content manager.

Lets you see every Rock Band game installed in an RPCS3 `dev_hdd0/game`
folder (identified by its PARAM.SFO title + game id), the song packages /
exports / licenses inside each game's USRDIR (with real song names read from
`Songs.dta`), and mark any of them for deletion (moved to the Windows
Recycle Bin, so it's reversible).

Double-click to run. Optional CLI usage:
    rb_content_manager.pyw [--dir PATH]

    --dir PATH      game directory to open (default:
                    C:\\Games\\Emulators\\RPCS3\\dev_hdd0\\game)
"""

import os
import queue
import re
import struct
import sys
import threading
from itertools import zip_longest

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
#  Green Day: Rock Band, ...).
RB_TITLE_KEYWORDS = ('rock band',)

# Official/original game name for each PS3 Rock Band title id, taken from
# SerialStation's database (https://serialstation.com). Keys are the folder
# ids RPCS3 uses (serial with the dash removed). This lets the app show the
# correct original name for each game instead of relying only on the (often
# modded) PARAM.SFO title.
GAME_ID_ORIGINAL_NAMES = {
    # Rock Band
    'BLUS30050': 'Rock Band',
    'BLES00228': 'Rock Band',
    # Rock Band 2
    'BLUS30147': 'Rock Band 2',
    'BLES00385': 'Rock Band 2',
    # Rock Band 3
    'BLUS30463': 'Rock Band 3',
    'BLES00986': 'Rock Band 3',
    'BLES01611': 'Rock Band 3',
    'BLAS50254': 'Rock Band 3',
    # Rock Band Blitz
    'NPUB30749': 'Rock Band Blitz',
    'NPEB00988': 'Rock Band Blitz',
    # The Beatles: Rock Band
    'BLUS30282': 'The Beatles: Rock Band',
    'BLES00532': 'The Beatles: Rock Band',
    'BLUS30414': 'The Beatles: Rock Band',
    'BLUS30423': 'The Beatles: Rock Band',
    # Green Day: Rock Band
    'BLUS30350': 'Green Day: Rock Band',
    'BLES00787': 'Green Day: Rock Band',
    'BLAS50214': 'Green Day: Rock Band',
    'BLUS30573': 'Green Day: Rock Band Plus',
    'NPUB90411': 'Green Day: Rock Band (Demo)',
    # LEGO Rock Band
    'BLUS30382': 'LEGO Rock Band',
    'BLES00636': 'LEGO Rock Band',
    # AC/DC Live: Rock Band
    'BLUS30235': 'AC/DC Live: Rock Band Track Pack',
    'BLES00453': 'AC/DC Live: Rock Band',
    # Track packs
    'BLUS30195': 'Rock Band: Track Pack Volume 2',
    'BLUS30327': 'Rock Band Track Pack: Classic Rock',
    'BLUS30351': 'Rock Band: Country Track Pack',
    'BLUS30623': 'Rock Band: Country Track Pack 2',
    'BLUS30352': 'Rock Band: Metal Track Pack',
    'BLES00451': 'Rock Band: Song Pack 2',
    # Demo / misc
    'BLUD80001': 'Rock Band (Trade Demo)',
}

# Fallback id list used when the PARAM.SFO is missing / unreadable.
# It contains the SerialStation ids above plus a few legacy ids seen on
# modded installs that do not appear in the database.
KNOWN_RB_IDS = set(GAME_ID_ORIGINAL_NAMES) | {
    'NPUA30005', 'NPUB30000', 'NPUB30506',
    'NPEB00737', 'NPUB30434', 'NPUB30531',
    'BLUS30359', 'BLES00808', 'BLES01103', 'BLES01043',
    'BLUS30439', 'BLUS31654',
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
    (e.g. "songs/foo/foo"); those path refs are filtered out *before* the
    names are paired with their artists so the indices stay aligned.
    """
    try:
        raw = open(dta_path, 'rb').read()
        txt = _decode_text(raw)
    except OSError:
        return []
    # findall on the backreference pattern returns (quote, value) tuples;
    # drop the `(song (name "songs/<id>/<id>"))` path references first.
    names = [v.strip() for _q, v in SONG_NAME_RE.findall(txt)
             if not v.startswith('songs/')]
    artists = [v.strip() for _q, v in SONG_ARTIST_RE.findall(txt)]
    return [(n, a) for n, a in zip_longest(names, artists, fillvalue='') if n]


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


def original_game_name(game_id):
    """Return the official/original name for a PS3 game id, or ''."""
    return GAME_ID_ORIGINAL_NAMES.get(game_id, '')


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
    def __init__(self, root, game_dir):
        self.root = root
        self.game_dir = game_dir

        self.item_by_path = {}   # path -> item dict
        self.game_nodes = {}     # game_id -> tree node id
        self.game_paths = {}     # game_id -> game folder path
        self.game_titles = {}    # game_id -> display title
        self.game_delete = {}    # game_id -> bool (mark whole game folder)
        self.game_order = []     # game_id list in insertion order
        self.game_items = {}     # game_id -> [item dicts] in tree order
        self.ui_queue = queue.Queue()
        self.scanning = False
        # filters
        self.search_var = tk.StringVar()
        self.dup_only_var = tk.BooleanVar(value=False)
        self._visible = set()    # tree nodes currently displayed
        # duplicate navigation state (context menu -> Show duplicated items)
        self.dup_cycle = []
        self.dup_cycle_index = -1
        self.dup_cycle_source = None

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

        # second toolbar row: search + duplication filter
        tool2 = ttk.Frame(self.root, padding=(8, 0, 8, 4))
        tool2.pack(fill='x')
        ttk.Label(tool2, text='Search:').pack(side='left')
        self.search_entry = ttk.Entry(
            tool2, textvariable=self.search_var, width=42)
        self.search_entry.pack(side='left', padx=(6, 0))
        self.search_entry.bind('<KeyRelease>', self._on_search_change)
        self.dup_only = ttk.Checkbutton(
            tool2, text='Show duplications only', variable=self.dup_only_var,
            command=self._apply_filters)
        self.dup_only.pack(side='left', padx=(12, 0))
        ttk.Label(
            tool2, text='Yellow = song also found in another item',
            foreground='#8a6d00').pack(side='left', padx=(12, 0))

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
        self.tree.tag_configure('dup', background='#ffe08a')

        self.tree.bind('<Button-1>', self._on_click)
        self.tree.bind('<Double-1>', self._on_double_click)
        self.tree.bind('<Button-3>', self._on_right_click)
        self.root.bind('<Delete>', lambda e: self.delete_marked())

        # -- context menu -----------------------------------------------------
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label='Open folder in Explorer', command=self._menu_open)
        self.menu.add_command(label='Delete entire game folder\u2026', command=self._menu_delete_game)
        self.menu.add_command(label='Toggle mark', command=self._menu_toggle)
        self.menu.add_command(
            label='Show duplicated items', command=self._menu_show_dups)
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
        threading.Thread(
            target=self._scan_worker, args=(game_dir,), daemon=True).start()

    def _clear_tree(self):
        for node in self.tree.get_children():
            self.tree.delete(node)
        self.item_by_path.clear()
        self.game_nodes.clear()
        self.game_paths.clear()
        self.game_titles.clear()
        self.game_delete.clear()
        self.game_order.clear()
        self.game_items.clear()
        self._visible.clear()
        self.dup_cycle = []
        self.dup_cycle_index = -1
        self.dup_cycle_source = None

    def _scan_worker(self, game_dir):
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
            if not rb:
                continue  # only Rock Band content is shown

            base_id = gid.upper().replace('CACHEDATA', '')
            title_show = original_game_name(base_id) or (
                title if title != '?' else '?')
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
            header = (f'{title}  ({gid})  \u2014  {len(items)} items '
                      f'\u00b7 {nsongs} songs \u00b7 {human_size(size_total)}')
        else:
            header = f'{title}  ({gid})  \u2014  no song content'
        tags = ['game'] + ([] if rb else ['nonrb']) + (['empty'] if not items else [])
        node = self.tree.insert('', 'end', text=header, values=('', '', '', ''), tags=tags)
        self.game_nodes[gid] = node
        self.game_titles[gid] = title
        self.game_paths[gid] = gpath
        self.game_delete[gid] = False
        self.game_order.append(gid)
        self.game_items[gid] = list(items)
        self._visible.add(node)

        for i, item in enumerate(items):
            tags_i = ['content'] + ([] if item['rb'] else ['nonrb'])
            if i % 2:
                tags_i.append('even')
            song_text = songs_preview(item['songs'])
            values = ('\u2610', item['type'], song_text, human_size(item['size']))
            inode = self.tree.insert(node, 'end', text=item['folder'],
                                     values=values, tags=tags_i)
            item['node'] = inode
            item['has_duplicates'] = False
            self.item_by_path[item['path']] = item
            self._visible.add(inode)

        self.tree.item(node, open=True)
        self._update_game_mark(gid)

    def _scan_done(self, n_games):
        self.scanning = False
        self.del_btn.state(['!disabled'])
        self._compute_duplicates()
        self._apply_filters()
        n_dup = sum(1 for it in self.item_by_path.values()
                    if it.get('has_duplicates'))
        dup_txt = (f' \u00b7 {n_dup} item(s) contain duplicated songs'
                   if n_dup else '')
        self.status_var.set(
            f'Done. {n_games} game(s) listed \u2014 '
            f'{len(self.item_by_path)} content item(s){dup_txt}. '
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

    # -- duplication detection / filtering ------------------------------------
    def _compute_duplicates(self):
        """Mark items whose songs also appear in another item (yellow).

        A song counts as duplicated when the same (artist, name) pair is
        found in two or more items - e.g. inside a track pack AND as a
        standalone DLC item.
        """
        song_items = {}  # (artist, name) -> set of item paths
        for it in self.item_by_path.values():
            seen = set()
            for name, artist in it['songs']:
                name = name.strip().lower()
                if not name:
                    continue
                seen.add((artist.strip().lower(), name))
            for key in seen:
                song_items.setdefault(key, set()).add(it['path'])
        dup_paths = set()
        for paths in song_items.values():
            if len(paths) > 1:
                dup_paths.update(paths)
        for it in self.item_by_path.values():
            it['has_duplicates'] = it['path'] in dup_paths
        self._apply_dup_tags()

    def _apply_dup_tags(self):
        for gid, items in self.game_items.items():
            for i, it in enumerate(items):
                tags = ['content'] + ([] if it['rb'] else ['nonrb'])
                if i % 2:
                    tags.append('even')
                if it.get('has_duplicates'):
                    tags.append('dup')
                self.tree.item(it['node'], tags=tags)

    def _make_search_text(self, it, game_title):
        parts = [it['folder']]
        if game_title:
            parts.append(game_title)
        for name, artist in it['songs']:
            parts.append(name)
            if artist:
                parts.append(artist)
                parts.append(f'{artist} - {name}')
        return ' '.join(parts).lower()

    def _item_search_text(self, it):
        s = it.get('_search')
        if s is None:
            s = self._make_search_text(
                it, self.game_titles.get(it['game_id'], ''))
            it['_search'] = s
        return s

    def _item_matches(self, it, q):
        return q in self._item_search_text(it)

    def _game_matches(self, gid, q):
        if not q:
            return True
        text = f'{gid} {self.game_titles.get(gid, "")}'.lower()
        return q in text

    def _on_search_change(self, _event=None):
        self._apply_filters()

    def _apply_filters(self):
        """Show/hide tree rows based on the search box and dup-only filter."""
        if self.scanning:
            return
        q = self.search_var.get().strip().lower()
        dup_only = bool(self.dup_only_var.get())

        want = []  # ordered (gid, visible_items)
        for gid in self.game_order:
            items = self.game_items.get(gid, [])
            if not items:
                if q and not self._game_matches(gid, q):
                    continue
                want.append((gid, []))
            else:
                vis = [it for it in items
                       if (not dup_only or it.get('has_duplicates'))
                       and (not q or self._item_matches(it, q))]
                if vis:
                    want.append((gid, vis))

        wanted_nodes = set()
        for gid, vis in want:
            wanted_nodes.add(self.game_nodes[gid])
            for it in vis:
                wanted_nodes.add(it['node'])
        for node in list(self._visible):
            if node not in wanted_nodes:
                self.tree.detach(node)
                self._visible.discard(node)

        idx = 0
        for gid, vis in want:
            gnode = self.game_nodes[gid]
            if gnode not in self._visible:
                self.tree.reattach(gnode, '', idx)
                self._visible.add(gnode)
            idx += 1
            for it in vis:
                inode = it['node']
                if inode not in self._visible:
                    self.tree.reattach(inode, gnode, 'end')
                    self._visible.add(inode)

    def _select_and_see(self, node):
        if node not in self._visible:
            return
        self.tree.see(node)
        self.tree.focus(node)
        self.tree.selection_set(node)

    def _menu_show_dups(self):
        """Context menu: jump through the items sharing this item's songs.

        Each click selects the next duplicate partner; re-clicking cycles
        through all of them.
        """
        node = getattr(self, '_menu_node', None)
        gid = self._node_game_id(node)
        if gid and node in self.game_nodes.values():
            source_items = [it for it in self.item_by_path.values()
                            if it['game_id'] == gid]
        else:
            item = self._node_item(node)
            source_items = [item] if item else []
        if not source_items:
            return

        source_keys = set()
        for it in source_items:
            for name, artist in it['songs']:
                name = name.strip().lower()
                if not name:
                    continue
                source_keys.add((artist.strip().lower(), name))

        source_paths = {it['path'] for it in source_items}
        targets = []
        for it in self.item_by_path.values():
            if it['path'] in source_paths:
                continue
            for name, artist in it['songs']:
                if (artist.strip().lower(), name.strip().lower()) in source_keys:
                    targets.append(it)
                    break
        if not targets:
            messagebox.showinfo(
                'No duplicates',
                'This item\u2019s songs do not appear in any other item.')
            return

        # make everything visible so the duplicates can be jumped to
        if self.search_var.get():
            self.search_var.set('')
        if self.dup_only_var.get():
            self.dup_only_var.set(False)
        self._apply_filters()

        src = source_items[0]['path'] if len(source_items) == 1 else f'GAME:{gid}'
        if self.dup_cycle_source != src:
            self.dup_cycle = targets
            self.dup_cycle_index = -1
            self.dup_cycle_source = src
        self.dup_cycle_index = (self.dup_cycle_index + 1) % len(self.dup_cycle)
        self._select_and_see(self.dup_cycle[self.dup_cycle_index]['node'])

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
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--dir':
            i += 1
            if i < len(args):
                game_dir = os.path.abspath(args[i])
        i += 1

    if tk is None:
        print('tkinter is not available - cannot start the GUI.', file=sys.stderr)
        return 1

    root = tk.Tk()
    if os.path.isdir(game_dir):
        app = App(root, game_dir)
    else:
        # let the user pick a folder
        chosen = filedialog.askdirectory(
            title='Select the RPCS3 dev_hdd0/game folder',
            initialdir=os.path.dirname(game_dir) if os.path.dirname(game_dir) else os.getcwd())
        if not chosen:
            root.destroy()
            return 0
        app = App(root, chosen)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
