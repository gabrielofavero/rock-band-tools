#!/usr/bin/env python3
"""compare_pkg_con.pyw - Compare a PKG dir against a CON dir.

Lets you select two directories (the "pkg" dir and the "con" dir) and
compares the file names inside them, ignoring the file extension, then logs
which files exist in one directory but not the other.

Double-click to run. Optional CLI usage:
    compare_pkg_con.pyw [--pkg PATH] [--con PATH]

    --pkg PATH   pkg directory (skips that folder picker)
    --con PATH   con directory (skips that folder picker)

When both --pkg and --con are given the comparison runs in the terminal;
otherwise a small GUI opens to pick the directories.
"""

import argparse
import os
import sys

# tkinter is bundled with the standard Windows CPython build
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, scrolledtext
except Exception:  # pragma: no cover - only matters for headless runs
    tk = ttk = filedialog = scrolledtext = None

_HAS_TK = tk is not None


# ---------------------------------------------------------------------------
# Comparison logic
# ---------------------------------------------------------------------------
def scan_files(directory):
    """Return {stem_lower: [filenames...]} for the top-level files in a dir.

    The stem is the file name with its extension stripped (comparison is
    case-insensitive, matching how Windows handles file names).
    """
    out = {}
    try:
        entries = os.listdir(directory)
    except OSError:
        return out
    for name in sorted(entries):
        full = os.path.join(directory, name)
        if not os.path.isfile(full):
            continue
        stem = os.path.splitext(name)[0].lower()
        out.setdefault(stem, []).append(name)
    return out


def compare_dirs(pkg_dir, con_dir, log=print):
    """Compare two directories by file name (extension ignored).

    `log` is a callable that receives one string line at a time.
    """
    pkg_dir = os.path.abspath(pkg_dir)
    con_dir = os.path.abspath(con_dir)

    log(f"PKG directory: {pkg_dir}")
    log(f"CON directory: {con_dir}")
    log()

    if not os.path.isdir(pkg_dir):
        log(f"! PKG directory does not exist: {pkg_dir}")
        return
    if not os.path.isdir(con_dir):
        log(f"! CON directory does not exist: {con_dir}")
        return

    pkg = scan_files(pkg_dir)
    con = scan_files(con_dir)

    # Multiple files sharing one stem (would make the comparison ambiguous).
    ambiguous = False
    for label, mapping in (("PKG", pkg), ("CON", con)):
        for stem, names in sorted(mapping.items()):
            if len(names) > 1:
                ambiguous = True
                log(f"! {label} has {len(names)} files sharing the stem "
                    f'"{stem}": {", ".join(names)}')
    if ambiguous:
        log()

    pkg_stems = set(pkg)
    con_stems = set(con)

    only_pkg = sorted(pkg_stems - con_stems)
    only_con = sorted(con_stems - pkg_stems)
    common = sorted(pkg_stems & con_stems)

    only_pkg_files = [name for stem in only_pkg for name in pkg[stem]]
    only_con_files = [name for stem in only_con for name in con[stem]]

    log(f"Files in PKG only ({len(only_pkg_files)}):")
    if only_pkg_files:
        for name in only_pkg_files:
            log(f"  - {name}")
    else:
        log("  (none)")

    log()
    log(f"Files in CON only ({len(only_con_files)}):")
    if only_con_files:
        for name in only_con_files:
            log(f"  - {name}")
    else:
        log("  (none)")

    log()
    log(f"Present in both: {len(common)}")

    mismatch = [s for s in common if len(pkg[s]) != len(con[s])]
    if mismatch:
        log()
        log(f"Count mismatches (same stem, different file count) ({len(mismatch)}):")
        for stem in mismatch:
            log(f'  - "{stem}": PKG={len(pkg[stem])} CON={len(con[stem])}')


# ---------------------------------------------------------------------------
# GUI: folder pickers + log window
# ---------------------------------------------------------------------------
def pick_directory(title, initial=None):
    """Open a native folder picker. Returns the chosen path or None."""
    root = tk.Tk()
    root.withdraw()  # hide the empty root window
    path = filedialog.askdirectory(
        title=title,
        initialdir=initial or os.getcwd(),
        parent=root,
    )
    root.destroy()
    return path or None


class App:
    def __init__(self, root, pkg_dir, con_dir):
        self.root = root
        self.pkg_var = tk.StringVar(value=pkg_dir or "")
        self.con_var = tk.StringVar(value=con_dir or "")
        self._build_ui()

    # -- UI construction ----------------------------------------------------
    def _build_ui(self):
        self.root.title("RB PKG vs CON comparer")
        self.root.geometry("900x620")
        self.root.minsize(640, 400)

        top = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        top.pack(fill='x')

        # -- pkg row ---------------------------------------------------------
        ttk.Label(top, text='PKG dir:').grid(row=0, column=0, sticky='w')
        self.pkg_entry = ttk.Entry(top, textvariable=self.pkg_var, width=60)
        self.pkg_entry.grid(row=0, column=1, sticky='ew', padx=(6, 4))
        ttk.Button(top, text='Browse\u2026', command=self.browse_pkg).grid(
            row=0, column=2)

        # -- con row ---------------------------------------------------------
        ttk.Label(top, text='CON dir:').grid(row=1, column=0, sticky='w', pady=(6, 0))
        self.con_entry = ttk.Entry(top, textvariable=self.con_var, width=60)
        self.con_entry.grid(row=1, column=1, sticky='ew', padx=(6, 4), pady=(6, 0))
        ttk.Button(top, text='Browse\u2026', command=self.browse_con).grid(
            row=1, column=2, pady=(6, 0))

        top.columnconfigure(1, weight=1)

        # -- log -------------------------------------------------------------
        mid = ttk.Frame(self.root, padding=(8, 6, 8, 4))
        mid.pack(fill='both', expand=True)

        self.text = scrolledtext.ScrolledText(mid, wrap='word', font=("Consolas", 10))
        self.text.pack(fill='both', expand=True)

        # -- footer ----------------------------------------------------------
        bottom = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        bottom.pack(fill='x')

        self.compare_btn = ttk.Button(
            bottom, text='Compare', command=self.run_compare)
        self.compare_btn.pack(side='right', padx=(0, 4))
        self.clear_btn = ttk.Button(bottom, text='Clear log', command=self.clear_log)
        self.clear_btn.pack(side='right')
        self.status_var = tk.StringVar(value='Pick the two directories, then hit Compare.')
        ttk.Label(bottom, textvariable=self.status_var).pack(side='left')

    # -- actions --------------------------------------------------------------
    def browse_pkg(self):
        chosen = pick_directory('Select the PKG directory',
                                self.pkg_var.get() or os.getcwd())
        if chosen:
            self.pkg_var.set(chosen)

    def browse_con(self):
        chosen = pick_directory('Select the CON directory',
                                self.con_var.get() or os.getcwd())
        if chosen:
            self.con_var.set(chosen)

    def clear_log(self):
        self.text.delete('1.0', 'end')

    def _log(self, string=''):
        if not string:
            string = '\n'
        try:
            self.text.insert('end', string + '\n')
            self.text.see('end')
            self.root.update_idletasks()
        except tk.TclError:
            pass

    def run_compare(self):
        pkg_dir = self.pkg_var.get().strip().strip('"')
        con_dir = self.con_var.get().strip().strip('"')
        if not pkg_dir or not con_dir:
            self.status_var.set('Both a PKG dir and a CON dir are required.')
            return
        self.clear_log()
        self.compare_btn.state(['disabled'])
        self.status_var.set('Comparing\u2026')
        try:
            compare_dirs(pkg_dir, con_dir, log=self._log)
            self.status_var.set('Comparison finished.')
        finally:
            self.compare_btn.state(['!disabled'])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Compare a PKG dir against a CON dir by file name.')
    parser.add_argument('--pkg', metavar='PATH', help='pkg directory')
    parser.add_argument('--con', metavar='PATH', help='con directory')
    args = parser.parse_args(argv)

    if _HAS_TK and not (args.pkg and args.con):
        root = tk.Tk()
        app = App(root, args.pkg, args.con)
        root.mainloop()
    elif not (args.pkg and args.con):
        print('Both --pkg and --con are required when running from a terminal.')
        print('Run without arguments to use the GUI folder pickers.')
        return 2
    else:
        compare_dirs(args.pkg, args.con, log=print)
    return 0


if __name__ == '__main__':
    sys.exit(main())
