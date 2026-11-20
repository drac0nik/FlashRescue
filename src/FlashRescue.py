"""
FlashRescue - recover deleted files from USB flash drives and memory cards.

The drive is only ever read, never written to. Recovered files must be saved to a
different drive.
"""

import io
import os
import sys
import time
import queue
import shutil
import tempfile
import threading
import datetime
import subprocess

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import engine
import rescue
import health
import extras
import media
import raid
import worker
import updater
import portable
import images
import recoverability
import previews
import encrypted
import search
from engine import human_size

try:
    from PIL import Image, ImageTk
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

APP = 'FlashRescue'
VERSION = '1.9.0'
IS_WIN = os.name == 'nt'

IMAGE_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'tif', 'tiff', 'ico', 'cr2', 'nef', 'arw', 'dng', 'pef'}
TEXT_EXTS = {'txt', 'csv', 'log', 'md', 'xml', 'json', 'html', 'htm', 'ini', 'cfg', 'srt', 'lua', 'py', 'bat'}
BAD_CONDITIONS = {'Overwritten', 'Possibly overwritten', 'Partially overwritten', 'Erased (all zeros)',
                  'Compressed (not supported)', 'Encrypted (not supported)'}


# ------------------------------------------------------------------------------------
# Windows helpers
# ------------------------------------------------------------------------------------

def is_admin():
    if not IS_WIN:
        return os.geteuid() == 0 if hasattr(os, 'geteuid') else True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    import ctypes
    if getattr(sys, 'frozen', False):
        exe, params = sys.executable, ' '.join(f'"{a}"' for a in sys.argv[1:])
    else:
        exe, params = sys.executable, ' '.join(f'"{a}"' for a in [os.path.abspath(sys.argv[0])] + sys.argv[1:])
    rc = ctypes.windll.shell32.ShellExecuteW(None, 'runas', exe, params, None, 1)
    return rc > 32


def open_with_default_app(path):
    if IS_WIN:
        os.startfile(path)
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', path])
    else:
        subprocess.Popen(['xdg-open', path])


def drive_letter_of(path):
    path = os.path.abspath(path)
    return path[0].upper() if len(path) > 1 and path[1] == ':' else None


def _settings_path():
    return os.path.join(portable.data_dir(), 'settings.json')      # on the stick in portable mode


def load_settings():
    try:
        import json
        with open(_settings_path(), 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data):
    try:
        import json
        os.makedirs(os.path.dirname(_settings_path()), exist_ok=True)
        with open(_settings_path(), 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=1)
    except Exception:
        pass


def _decode_preview(data, box):
    """Small preview image, made off the window thread. JPEGs are decoded at reduced size
    (much faster), and absurd sizes from damaged files are refused instead of eating memory."""
    try:
        im = Image.open(io.BytesIO(data))
        if im.width * im.height > 250_000_000:
            return 'too big'
        if im.format == 'JPEG':
            im.draft('RGB', (box[0] * 2, box[1] * 2))
        im.load()
        im.thumbnail(box)
        if im.mode not in ('RGB', 'RGBA'):
            im = im.convert('RGBA')
        return im
    except Exception:
        return None


def resource_path(name):
    base = getattr(sys, '_MEIPASS', None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def set_window_icon(win):
    try:
        if IS_WIN:
            win.iconbitmap(default=resource_path('icon.ico'))     # also every window opened later
        else:
            win.iconphoto(True, tk.PhotoImage(file=resource_path('icon.png')))
    except Exception:
        pass


def _last_scan_path():
    d = portable.data_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, 'Last scan.frscan')


def hexdump(data, width=16):
    lines = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hx = ' '.join(f'{b:02X}' for b in chunk)
        asc = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f'{i:08X}  {hx:<{width * 3}} {asc}')
    return '\n'.join(lines)


# ------------------------------------------------------------------------------------
# Main window
# ------------------------------------------------------------------------------------

class App:
    COLS = (('name', 'Name', 200), ('saved', 'Saved', 46), ('folder', 'Original folder', 140),
            ('size', 'Size', 72), ('type', 'Type', 44), ('found', 'Found by', 82),
            ('condition', 'Condition', 150), ('check', 'Check', 150), ('modified', 'Modified', 108))

    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.items = {}             # id -> FoundFile
        self.order = []             # ids in arrival order
        self.src = None
        self.scanner = None
        self.scan_thread = None
        self.busy_recovering = False
        self.drives = []
        self.sort_col = None
        self.sort_rev = False
        self.preview_token = 0
        self.preview_img = None
        self.scan_started = 0
        self.last_rate = None
        self.saver = None
        self.settings = load_settings()

        root.title(f'{APP} {VERSION} - recover deleted files' +
                   ((f'  (portable, running from {portable.stick_letter()}:)' if portable.stick_letter()
                     else '  (portable)') if portable.is_portable() else ''))
        root.geometry('1280x800')
        root.minsize(980, 560)
        self._build()
        set_window_icon(root)
        self._update_info = None
        self.refresh_drives()
        root.after(4000, self.start_update_check)
        if portable.is_portable():
            self.status_var.set(f'Portable mode: settings and temp files stay in {portable.app_dir()} - nothing is '
                                'written to this PC. Save recovered files to a drive that is not being scanned.')
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        root.after(120, self.poll)

    # ---------------------------------------------------------------- layout
    def _build(self):
        r = self.root
        style = ttk.Style()
        style.configure('Big.TButton', padding=(14, 6))
        style.configure('Treeview', rowheight=22)

        top = ttk.Frame(r, padding=(10, 8, 10, 4))
        top.pack(fill='x')

        # 1. source
        src = ttk.LabelFrame(top, text=' 1. Choose the drive ', padding=8)
        src.pack(side='left', fill='both', expand=True)
        self.drive_var = tk.StringVar()
        self.drive_box = ttk.Combobox(src, textvariable=self.drive_var, state='readonly', width=58)
        self.drive_box.grid(row=0, column=0, columnspan=6, sticky='we')
        self.drive_box.bind('<<ComboboxSelected>>', self._on_source_chosen)
        ttk.Button(src, text='Refresh', command=self.refresh_drives).grid(row=1, column=0, sticky='w', pady=(6, 0))
        ttk.Button(src, text='Open disk image file...', command=self.pick_image).grid(row=1, column=1, sticky='w',
                                                                                      padx=6, pady=(6, 0))
        ttk.Button(src, text='Check chances...', command=self.open_chances).grid(
            row=1, column=2, sticky='w', padx=(0, 6), pady=(6, 0))
        ttk.Button(src, text='Rescue image (failing drive)...', command=self.open_rescue).grid(
            row=1, column=3, sticky='w', pady=(6, 0))
        tools = ttk.Menubutton(src, text='Tools')
        tm = tk.Menu(tools, tearoff=False)
        tm.add_command(label='Drive health and hidden areas...', command=self.show_health)
        tm.add_command(label='Virtual RAID (NAS / RAID drives)...', command=self.open_raid)
        tm.add_command(label='Unlock encrypted drive (LUKS / VeraCrypt)...', command=self.open_unlock)
        tm.add_separator()
        tm.add_command(label='Search inside the data...', command=self.open_search)
        tools['menu'] = tm
        tools.grid(row=1, column=4, sticky='w', padx=(6, 0), pady=(6, 0))
        src.columnconfigure(5, weight=1)

        # 2. options
        opt = ttk.LabelFrame(top, text=' 2. Scan ', padding=8)
        opt.pack(side='left', fill='both', padx=(10, 0))
        self.v_deleted = tk.BooleanVar(value=True)
        self.v_deep = tk.BooleanVar(value=True)
        self.v_free = tk.BooleanVar(value=True)
        self.v_exist = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text='Find deleted files (fast, keeps names)', variable=self.v_deleted).grid(
            row=0, column=0, sticky='w')
        ttk.Checkbutton(opt, text='Deep scan for lost files (slower, finds more)', variable=self.v_deep,
                        command=self._sync_opts).grid(row=1, column=0, sticky='w')
        self.cb_free = ttk.Checkbutton(opt, text='     only search free space (uncheck after a format)',
                                       variable=self.v_free)
        self.cb_free.grid(row=2, column=0, sticky='w')
        ttk.Checkbutton(opt, text='Also list files that are not deleted', variable=self.v_exist).grid(
            row=3, column=0, sticky='w')
        btns = ttk.Frame(opt)
        btns.grid(row=0, column=1, rowspan=4, padx=(14, 0), sticky='ns')
        self.btn_start = ttk.Button(btns, text='Start scan', style='Big.TButton', command=self.start_scan)
        self.btn_start.pack(fill='x')
        self.btn_stop = ttk.Button(btns, text='Stop', command=self.stop_scan, state='disabled')
        self.btn_stop.pack(fill='x', pady=(6, 0))

        # auto-recover
        auto = ttk.LabelFrame(r, text=' Auto-recover as found (optional) ', padding=(8, 4))
        auto.pack(fill='x', padx=10, pady=(4, 0))
        self.v_auto = tk.BooleanVar(value=self.settings.get('auto', False))
        ttk.Checkbutton(auto, text='Save files automatically while scanning, to:', variable=self.v_auto,
                        command=self._sync_opts).grid(row=0, column=0, sticky='w')
        self.v_auto_dir = tk.StringVar(value=self.settings.get('auto_dir', ''))
        self.auto_widgets = []
        e = ttk.Entry(auto, textvariable=self.v_auto_dir, width=60)
        e.grid(row=0, column=1, columnspan=7, sticky='we', padx=4)
        b = ttk.Button(auto, text='Browse...', command=self.pick_auto_dir)
        b.grid(row=0, column=8, sticky='w')
        self.auto_widgets += [e, b]
        self.auto_widgets.append(ttk.Label(auto, text='Only these types:'))
        self.auto_widgets[-1].grid(row=1, column=0, sticky='e', pady=(4, 0))
        saved_cats = self.settings.get('auto_cats', ['Pictures', 'Videos', 'Audio', 'Documents', 'Archives', 'Other'])
        self.v_auto_cats = {}
        for k, cat in enumerate(['Pictures', 'Videos', 'Audio', 'Documents', 'Archives', 'Other']):
            v = tk.BooleanVar(value=cat in saved_cats)
            self.v_auto_cats[cat] = v
            cb = ttk.Checkbutton(auto, text=cat, variable=v)
            cb.grid(row=1, column=1 + k, sticky='w', padx=(4, 6), pady=(4, 0))
            self.auto_widgets.append(cb)
        self.v_auto_skip = tk.BooleanVar(value=self.settings.get('auto_skip_bad', True))
        cb = ttk.Checkbutton(auto, text='Skip unrecoverable files', variable=self.v_auto_skip)
        cb.grid(row=1, column=7, columnspan=2, sticky='w', padx=(10, 0), pady=(4, 0))
        self.auto_widgets.append(cb)
        auto.columnconfigure(7, weight=1)

        # progress
        prog = ttk.Frame(r, padding=(10, 4, 10, 2))
        prog.pack(fill='x')
        self.pbar = ttk.Progressbar(prog, mode='determinate', maximum=100)
        self.pbar.pack(fill='x')
        line = ttk.Frame(prog)
        line.pack(fill='x', pady=(3, 0))
        self.status_var = tk.StringVar(value='Choose a drive and press Start scan.')
        ttk.Label(line, textvariable=self.status_var).pack(side='left')
        style.configure('Update.TButton', foreground='#15803d', font=('Segoe UI', 9, 'bold') if IS_WIN
                        else ('Helvetica', 9, 'bold'))
        self.update_btn = ttk.Button(line, text='Check for updates', command=self.update_clicked)
        self.update_btn.pack(side='right', padx=(10, 0))
        ttk.Button(line, text='Portable USB...', command=lambda: PortableDialog(self)).pack(side='right', padx=(10, 0))
        self.auto_var = tk.StringVar(value='')
        ttk.Label(line, textvariable=self.auto_var, foreground='#1b6e20').pack(side='right')

        # filters
        flt = ttk.Frame(r, padding=(10, 4, 10, 2))
        flt.pack(fill='x')
        ttk.Label(flt, text='3. Results   Show:').pack(side='left')
        self.v_cat = tk.StringVar(value='All types')
        cat = ttk.Combobox(flt, textvariable=self.v_cat, state='readonly', width=12,
                           values=['All types', 'Pictures', 'Videos', 'Audio', 'Documents', 'Archives', 'Other'])
        cat.pack(side='left', padx=(4, 10))
        cat.bind('<<ComboboxSelected>>', lambda e: self.rebuild())
        self.v_method = tk.StringVar(value='Any method')
        meth = ttk.Combobox(flt, textvariable=self.v_method, state='readonly', width=14,
                            values=['Any method', 'Deleted', 'Deep scan', 'Recycle Bin', 'Before format',
                                    'Lost partition', 'Thumbnail', 'Existing'])
        meth.pack(side='left', padx=(0, 10))
        meth.bind('<<ComboboxSelected>>', lambda e: self.rebuild())
        self.v_hide_bad = tk.BooleanVar(value=True)
        ttk.Checkbutton(flt, text='Hide unrecoverable', variable=self.v_hide_bad,
                        command=self.rebuild).pack(side='left', padx=(0, 10))
        ttk.Label(flt, text='Name contains:').pack(side='left')
        self.v_search = tk.StringVar()
        ent = ttk.Entry(flt, textvariable=self.v_search, width=24)
        ent.pack(side='left', padx=4)
        self.v_search.trace_add('write', lambda *a: self._debounce_rebuild())
        self.count_var = tk.StringVar(value='')
        ttk.Label(flt, textvariable=self.count_var).pack(side='right')

        flt2 = ttk.Frame(r, padding=(10, 0, 10, 2))
        flt2.pack(fill='x')
        self.v_smin, self.v_smax = tk.StringVar(), tk.StringVar()
        self.v_dfrom, self.v_dto = tk.StringVar(), tk.StringVar()
        ttk.Label(flt2, text='Size from').pack(side='left')
        ttk.Entry(flt2, textvariable=self.v_smin, width=7).pack(side='left', padx=3)
        ttk.Label(flt2, text='to').pack(side='left')
        ttk.Entry(flt2, textvariable=self.v_smax, width=7).pack(side='left', padx=3)
        ttk.Label(flt2, text='MB      Modified from').pack(side='left')
        ttk.Entry(flt2, textvariable=self.v_dfrom, width=11).pack(side='left', padx=3)
        ttk.Label(flt2, text='to').pack(side='left')
        ttk.Entry(flt2, textvariable=self.v_dto, width=11).pack(side='left', padx=3)
        ttk.Label(flt2, text='(YYYY-MM-DD)', foreground='#777').pack(side='left', padx=(0, 14))
        self.v_hide_dup = tk.BooleanVar(value=False)
        ttk.Checkbutton(flt2, text='Hide duplicates', variable=self.v_hide_dup, command=self.rebuild).pack(side='left')
        ttk.Button(flt2, text='Find duplicates', command=self.find_duplicates).pack(side='left', padx=6)
        for v in (self.v_smin, self.v_smax, self.v_dfrom, self.v_dto):
            v.trace_add('write', lambda *a: self._debounce_rebuild(500))

        # results + preview
        pw = ttk.PanedWindow(r, orient='horizontal')
        pw.pack(fill='both', expand=True, padx=10, pady=4)

        left = ttk.Frame(pw)
        cols = [c for c, _, _ in self.COLS]
        self.tree = ttk.Treeview(left, columns=cols, show='headings', selectmode='extended')
        for c, label, w in self.COLS:
            self.tree.heading(c, text=label, command=lambda c=c: self.sort_by(c))
            anchor = 'e' if c == 'size' else 'w'
            self.tree.column(c, width=w, anchor='center' if c == 'saved' else anchor, stretch=c in ('name', 'folder'))
        vs = ttk.Scrollbar(left, orient='vertical', command=self.tree.yview)
        hs = ttk.Scrollbar(left, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        vs.grid(row=0, column=1, sticky='ns')
        hs.grid(row=1, column=0, sticky='we')
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.tree.tag_configure('good', foreground='#1b6e20')
        self.tree.tag_configure('warn', foreground='#a35a00')
        self.tree.tag_configure('bad', foreground='#a01818')
        self.tree.tag_configure('carved', foreground='#1f4e9a')
        self.tree.tag_configure('exist', foreground='#555555')
        self.tree.bind('<<TreeviewSelect>>', self.on_select)
        self.tree.bind('<Double-1>', lambda e: self.open_selected())
        self.tree.bind('<Control-a>', lambda e: (self.tree.selection_set(self.tree.get_children()), 'break'))
        pw.add(left, weight=4)

        right = ttk.LabelFrame(pw, text=' Preview ', padding=6)
        self.pv_image = ttk.Label(right, anchor='center')
        self.pv_image.pack(fill='both', expand=True)
        self.pv_text = tk.Text(right, height=12, width=46, wrap='word', relief='flat',
                               font=('Consolas', 9) if IS_WIN else ('Courier', 9))
        self.pv_text.pack(fill='both', expand=False)
        self.pv_text.configure(state='disabled')
        pw.add(right, weight=2)

        # bottom
        bot = ttk.Frame(r, padding=(10, 4, 10, 10))
        bot.pack(fill='x')
        self.sel_var = tk.StringVar(value='Nothing selected')
        ttk.Label(bot, textvariable=self.sel_var).pack(side='left')
        ttk.Button(bot, text='Recover all shown...', style='Big.TButton',
                   command=lambda: self.recover(all_shown=True)).pack(side='right')
        ttk.Button(bot, text='Recover selected...', style='Big.TButton',
                   command=lambda: self.recover(all_shown=False)).pack(side='right', padx=6)
        ttk.Button(bot, text='Open selected', command=self.open_selected).pack(side='right', padx=6)
        ttk.Button(bot, text='Repair video...', command=self.open_repair).pack(side='right', padx=(6, 0))
        ttk.Button(bot, text='Check files', command=self.check_files).pack(side='right', padx=(6, 0))
        ttk.Button(bot, text='Load results...', command=self.load_results).pack(side='left', padx=(16, 0))
        ttk.Button(bot, text='Save results...', command=self.save_results).pack(side='left', padx=(6, 0))
        self._sync_opts()
        self._rebuild_job = None
        self._rebuild_gen = None
        self.shown = 0
        self.to_insert = []
        self._read_filters()

    def _sync_opts(self):
        self.cb_free.configure(state='normal' if self.v_deep.get() else 'disabled')
        st = 'normal' if self.v_auto.get() else 'disabled'
        for w in getattr(self, 'auto_widgets', []):
            w.configure(state=st)

    def pick_auto_dir(self):
        d = filedialog.askdirectory(title='Save recovered files to... (NOT the drive being scanned)',
                                    initialdir=self.v_auto_dir.get() or None, mustexist=True)
        if d:
            self.v_auto_dir.set(os.path.normpath(d))

    def _save_settings(self):
        self.settings.update({
            'auto': self.v_auto.get(),
            'auto_dir': self.v_auto_dir.get(),
            'auto_cats': [c for c, v in self.v_auto_cats.items() if v.get()],
            'auto_skip_bad': self.v_auto_skip.get(),
        })
        save_settings(self.settings)

    # ---------------------------------------------------------------- sources
    def refresh_drives(self):
        try:
            drives = engine.list_drives()
        except Exception as ex:
            drives = []
            self.status_var.set(f'Could not list drives: {ex}')

        def order(d):
            if d['kind'] == 'disk':
                return (3, d.get('number', 0))
            return (2 if d['system'] else (0 if d['type'] == 'Removable' else 1), d['letter'])

        drives.sort(key=order)
        images = [d for d in self.drives if d.get('image')]
        self.drives = drives + images
        labels = []
        for d in drives:
            if d['kind'] == 'disk':
                where = f"  - has {', '.join(l + ':' for l in sorted(d['letters']))}" if d['letters'] else ''
                bus = f", {d['type']}" if d['type'] else ''
                labels.append(f"Whole disk {d['number']}:  {d['label']}   ({human_size(d['size'])}{bus}){where}")
            else:
                name = d['label'] or ('USB Drive' if d['type'] == 'Removable' else 'Local Disk')
                extra = '  - WINDOWS DRIVE' if d['system'] else ''
                labels.append(f"{d['letter']}:  {name}   ({d['fs'] or 'unknown'}, {human_size(d['size'])}, "
                              f"{d['type']}){extra}")
        labels += [d['label'] for d in images]
        self.drive_box['values'] = labels
        if IS_WIN and not getattr(self, '_shadow_thread', None):
            def find_shadows():
                try:
                    self.q.put(('shadows', engine.list_shadow_copies()))
                finally:
                    self._shadow_thread = None
            self._shadow_thread = threading.Thread(target=find_shadows, daemon=True)
            self._shadow_thread.start()
        if labels and self.drive_var.get() not in labels:
            self.drive_box.current(0)
        if not labels:
            self.drive_var.set('')
            if IS_WIN:
                self.status_var.set('No drives found. Plug in the drive and press Refresh '
                                    '(whole disks only show up when running as administrator).')

    def pick_image(self):
        path = filedialog.askopenfilename(
            title='Open a disk image (made with a tool like dd, HDD Raw Copy or Win32 Disk Imager)',
            filetypes=[('Disk images', images.IMAGE_PATTERNS), ('All files', '*.*')])
        if path:
            self.add_image(path)

    def add_image(self, path):
        label = f'Image file: {path}'
        vals = list(self.drive_box['values'])
        if label not in vals:
            vals.append(label)
            self.drive_box['values'] = vals
            self.drives.append({'path': path, 'letter': None, 'letters': set(), 'image': True,
                                'label': label, 'kind': 'image', 'system': False})
        self.drive_var.set(label)

    def _selected_source(self):
        idx = self.drive_box.current()
        if idx < 0 or idx >= len(self.drives):
            return None
        return self.drives[idx]

    # ---------------------------------------------------------------- scanning
    def start_scan(self):
        if self.scan_thread and self.scan_thread.is_alive():
            return
        d = self._selected_source()
        if not d:
            messagebox.showinfo(APP, 'Choose a drive (or open a disk image) first.')
            return
        if not (self.v_deleted.get() or self.v_deep.get() or self.v_exist.get()):
            messagebox.showinfo(APP, 'Tick at least one scan option.')
            return
        if d.get('system'):
            what = 'This disk holds Windows' if d.get('kind') == 'disk' else 'This is your Windows drive'
            best = ('The safest way is not to run this Windows at all: shut the PC down and connect the drive to '
                    'another PC (a USB dock or adapter), or boot this PC from a rescue stick (WinPE) and run '
                    'FlashRescue from there.\n\nIf you go ahead here:\n') if portable.is_portable() else \
                'For the best chance:\n'
            if not messagebox.askyesno(APP, f'{what}, and Windows is running from it right now.\n\n'
                    'Windows and its programs write to it constantly, which can overwrite deleted files at '
                    f'any moment. {best}'
                    '  - close other programs and avoid downloading or installing anything\n'
                    '  - save recovered files to a different drive (a USB stick or external drive)\n\n'
                    'Scan it now?', icon='warning'):
                return
        if not self._ssd_trim_warning(d):
            return
        if portable.is_portable() and portable.stick_letter() and portable.stick_letter() in d.get('letters', set()):
            if not messagebox.askyesno(APP, 'FlashRescue is running from this drive, and keeps its settings and temp '
                                            'files on it while it runs - which could overwrite deleted files here.'
                                            '\n\nTo recover from this stick, run FlashRescue from somewhere else. '
                                            'Scan it anyway?', icon='warning'):
                return
        if d.get('kind') in ('volume', 'disk'):
            rep = self._health_for(d)
            if rep and rep['verdict'] == 'failing':
                ans = messagebox.askyesnocancel(
                    APP, rep['summary'] + '\n\nMake a rescue image first? (recommended)\n\n'
                         'Yes = open Rescue image\nNo = scan the drive directly anyway', icon='warning')
                if ans is None:
                    return
                if ans:
                    self.open_rescue()
                    return
        auto_dest = None
        if self.v_auto.get():
            auto_dest = self.v_auto_dir.get().strip()
            cats = {c for c, v in self.v_auto_cats.items() if v.get()}
            if not auto_dest or not os.path.isdir(auto_dest):
                messagebox.showinfo(APP, 'Auto-recover is on: choose an existing folder to save into '
                                         '(on a different drive), or untick auto-recover.')
                return
            if self._same_drive(auto_dest):
                messagebox.showerror(APP, 'The auto-recover folder is on the drive (or disk) being scanned.\n\n'
                                          'Pick a folder on a different drive, otherwise saving could overwrite '
                                          'the files you are trying to get back.')
                return
            if not cats:
                messagebox.showinfo(APP, 'Tick at least one file type to auto-recover.')
                return
            skip_bad = self.v_auto_skip.get()
        self._save_settings()
        if self.items and not messagebox.askyesno(APP, 'Start a new scan? The current results will be cleared.'):
            return
        if self.src and not getattr(self, '_src_is_virtual', False):
            self.src.close()
        self.src = None
        self._src_is_virtual = bool(d.get('source_obj'))
        try:
            if d.get('source_obj'):
                self.src = d['source_obj']
            elif d.get('image'):
                self.src = images.open_image(d['path'], d.get('label'))      # raw, VHD(X), VMDK, E01, ...
            else:
                self.src = engine.RawSource(d['path'], d.get('label'))
        except images.ImageError as ex:
            messagebox.showerror(APP, f'Could not read this image file:\n{ex}')
            return
        except PermissionError:
            messagebox.showerror(APP, 'Windows blocked direct access to the drive.\n\n'
                                      'Close FlashRescue and start it with right-click > "Run as administrator".')
            return
        except OSError as ex:
            messagebox.showerror(APP, f'Could not open the drive:\n{ex}')
            return
        map_note = ''
        if getattr(self.src, 'kind', 'raw') not in ('raw', 'image') and isinstance(self.src, images._ImageBase):
            map_note = f' Image type: {self.src.kind}.' + ''.join(f' Note: {n}.' for n in self.src.notes)
        if d.get('image') and not isinstance(self.src, images._ImageBase):
            mp = rescue.find_map_for_image(d['path'])
            if mp:
                try:
                    self.src.load_rescue_map(mp)
                    bad = sum(b - a for a, b in zip(self.src.bad_starts, self.src.bad_ends))
                    map_note = (f' Rescue map loaded: {human_size(bad)} could not be copied from the drive; '
                                'files touching those areas are marked.') if bad else ' Rescue map loaded: image is complete.'
                except Exception as ex:
                    map_note = f' (Could not read the rescue map: {ex})'
        self.map_note = map_note
        self.items.clear()
        self.order.clear()
        self._rebuild_gen = None
        self.to_insert = []
        self.tree.delete(*self.tree.get_children())
        self.shown = 0
        self._clear_preview()
        opts = engine.ScanOptions(self.v_deleted.get(), self.v_deep.get(), self.v_free.get(), self.v_exist.get())

        saver = None
        self.auto_var.set('')
        if auto_dest:
            out_root = os.path.join(auto_dest, f'FlashRescue {datetime.datetime.now():%Y-%m-%d %H.%M.%S}')
            saver = engine.AutoSaver(self.src, out_root,
                                     on_saved=lambda it: self.q.put(('saved', it)),
                                     on_error=lambda m: self.q.put(('autosave_error', m)))
            self.auto_var.set(f'Auto-recover on: saving to {out_root}')
        self.saver = saver

        def on_item(it):
            self.q.put(('item', it))
            if saver and it.category in cats and not (skip_bad and it.condition in BAD_CONDITIONS):
                saver.add(it)

        # Plain drives and images are scanned in a helper process (own CPU core, smooth window);
        # assembled RAID / LVM volumes live in this process, so they're scanned on a thread.
        so = d.get('source_obj')
        scanner_cls = worker.ProcessScanner if (not so or worker.portable_spec(so)) else engine.Scanner
        self.scanner = scanner_cls(
            self.src, opts,
            on_item=on_item,
            on_status=lambda s: self.q.put(('status', s)),
            on_progress=lambda f, a, b: self.q.put(('progress', f, a, b)),
            on_remove=lambda it: self.q.put(('remove', it)))
        self.scan_started = time.time()
        self.last_rate = None
        self.btn_start.configure(state='disabled')
        self.btn_stop.configure(state='normal')
        self.pbar.configure(mode='determinate', value=0)

        def finish_saving():
            if not saver:
                return ''
            saver.finish()
            if saver.thread.is_alive() and saver.pending():
                self.q.put(('status', f'Scan finished - saving the last {saver.pending():,} file(s)...'))
            saver.thread.join()
            text = f' Auto-saved {saver.saved:,} file(s) ({human_size(saver.bytes)}).'
            if saver.damaged:
                text += f' {len(saver.damaged)} of them have unreadable parts.'
            if saver.failed:
                text += f' {len(saver.failed)} could not be saved.'
            if saver.records:
                try:
                    engine.write_report(saver.out_root, saver.records, self.src.label)
                except OSError:
                    pass
            return text

        def work():
            try:
                try:
                    summary = self.scanner.run()
                except worker.StartFailed:
                    sc = self.scanner
                    self.scanner = engine.Scanner(self.src, opts, sc.on_item, sc.on_status, sc.on_progress,
                                                  on_remove=sc.on_remove)
                    if sc.cancel.is_set():
                        self.scanner.cancel.set()
                    summary = self.scanner.run()
                summary += finish_saving() + getattr(self, 'map_note', '')
                self.q.put(('done', summary))
            except Exception as ex:
                import traceback
                traceback.print_exc()
                finish_saving()
                self.q.put(('error', f'{type(ex).__name__}: {ex}'))

        self.scan_thread = threading.Thread(target=work, daemon=True)
        self.scan_thread.start()

    def stop_scan(self):
        if self.scanner:
            self.scanner.cancel.set()
            self.status_var.set('Stopping...' + (' (finishing saving files already found)' if self.saver else ''))

    def poll(self):
        # Work in short slices (about 30 ms) so the window always stays responsive, however
        # fast results arrive; whatever is left is picked up a moment later.
        t_end = time.perf_counter() + 0.012
        new_items = []
        last_progress = None
        try:
            while time.perf_counter() < t_end and len(new_items) < 3000:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == 'item':
                    new_items.append(msg[1])
                elif kind == 'status':
                    self.status_var.set(msg[1])
                elif kind == 'progress':
                    last_progress = msg[1:]
                elif kind == 'done':
                    self._scan_finished(msg[1])
                elif kind == 'error':
                    self._scan_finished('Scan failed: ' + msg[1])
                    messagebox.showerror(APP, 'The scan stopped with an error:\n\n' + msg[1])
                elif kind == 'recover_progress':
                    self.pbar.configure(mode='determinate', value=msg[1] * 100)
                    self.status_var.set(msg[2])
                elif kind == 'recover_done':
                    self._recover_finished(*msg[1:])
                elif kind == 'preview':
                    self._show_preview(*msg[1:])
                elif kind == 'open_file':
                    if msg[1]:
                        try:
                            open_with_default_app(msg[1])
                        except Exception as ex:
                            messagebox.showerror(APP, f'Could not open the file:\n{ex}')
                    else:
                        messagebox.showerror(APP, f'Could not open the file:\n{msg[2]}')
                elif kind == 'remove':
                    it = msg[1]
                    if it.id in self.items and not it.saved_path:
                        del self.items[it.id]
                        try:
                            self.order.remove(it.id)
                        except ValueError:
                            pass
                        if self.tree.exists(str(it.id)):
                            self.tree.delete(str(it.id))
                            self.shown -= 1
                    else:
                        new_items = [x for x in new_items if x.id != it.id]
                elif kind == 'checked':
                    it = msg[1]
                    if self.tree.exists(str(it.id)):
                        self.tree.set(str(it.id), 'check', it.check or '')
                        self.tree.item(str(it.id), tags=(('good' if it.check_ok == 'ok' else
                                                           'bad' if it.check_ok == 'bad' else 'warn'),))
                    self.status_var.set(msg[2])
                elif kind == 'busy_done':
                    self.pbar.stop()
                    self.pbar.configure(mode='determinate', value=100)
                    self.status_var.set(msg[1])
                    if msg[2]:
                        self.rebuild()
                elif kind == 'busy_progress':
                    self.pbar.configure(mode='determinate', value=msg[1] * 100)
                elif kind == 'shadows':
                    self._add_shadows(msg[1])
                elif kind == 'saved':
                    iid = str(msg[1].id)
                    if self.tree.exists(iid):
                        self.tree.set(iid, 'saved', '✓')
                    s = self.saver
                    if s:
                        self.auto_var.set(f'Auto-saved {s.saved:,} file(s), {human_size(s.bytes)}'
                                          + (f' - {s.pending():,} waiting' if s.pending() > 5 else ''))
                elif kind == 'call':
                    msg[1](*msg[2:])
                elif kind == 'autosave_error':
                    self.auto_var.set('Auto-recover stopped: destination full')
                    messagebox.showwarning(APP, msg[1])
        except queue.Empty:
            pass
        if last_progress:
            self._progress(*last_progress)
        if new_items:
            for it in new_items:
                self.items[it.id] = it
                self.order.append(it.id)
                if self._passes(it) and self.sort_col is None and self._rebuild_gen is None:
                    self.to_insert.append(it)
            if self.sort_col is not None or self._rebuild_gen is not None:
                self._debounce_rebuild(1500)
        if self.to_insert:
            t_end = time.perf_counter() + 0.02
            while self.to_insert and time.perf_counter() < t_end:
                for it in self.to_insert[:100]:
                    if it.id in self.items:
                        self._insert(it)
                del self.to_insert[:100]
        if new_items or self.to_insert:
            self._update_count()
        busy = not self.q.empty() or self.to_insert
        self.root.after(15 if busy else 100, self.poll)

    def _progress(self, frac, done, total):
        if frac is None:
            if str(self.pbar['mode']) != 'indeterminate':
                self.pbar.configure(mode='indeterminate')
                self.pbar.start(12)
            return
        if str(self.pbar['mode']) != 'determinate':
            self.pbar.stop()
            self.pbar.configure(mode='determinate')
            self.deep_started = time.time()
            self.deep_start_done = done
        self.pbar['value'] = frac * 100
        elapsed = time.time() - getattr(self, 'deep_started', self.scan_started)
        moved = done - getattr(self, 'deep_start_done', 0)
        rate = moved / elapsed if elapsed > 1 else 0
        eta = ''
        if rate > 0:
            left = (total - done) / rate
            eta = f' - {rate / engine.MB:.0f} MB/s - about {self._fmt_secs(left)} left'
        self.status_var.set(f'Deep scan: {human_size(done)} of {human_size(total)} ({frac * 100:.0f}%){eta}')

    @staticmethod
    def _fmt_secs(s):
        s = int(s)
        if s < 60:
            return f'{s} sec'
        if s < 3600:
            return f'{s // 60} min'
        return f'{s // 3600} h {(s % 3600) // 60} min'

    def _scan_finished(self, summary):
        self.pbar.stop()
        self.pbar.configure(mode='determinate', value=100)
        self.status_var.set(summary)
        self.btn_start.configure(state='normal')
        self.btn_stop.configure(state='disabled')
        if self.sort_col is not None:
            self.rebuild()
        self._update_count()
        try:
            extras.save_results(_last_scan_path(), self.src, [self.items[i] for i in self.order],
                                {'label': self.src.label})
        except Exception:
            pass
        s = self.saver
        if s:
            self.auto_var.set(f'Auto-saved {s.saved:,} file(s), {human_size(s.bytes)}')
            if s.saved:
                msg = f'{summary}\n\nAuto-recovered files are in:\n{s.out_root}'
                if s.failed:
                    msg += f'\n\n{len(s.failed)} could not be saved:\n' + '\n'.join(s.failed[:8])
                if messagebox.askyesno(APP, msg + '\n\nOpen the folder now?'):
                    try:
                        open_with_default_app(s.out_root)
                    except Exception:
                        pass

    # ---------------------------------------------------------------- list
    def _passes(self, it):
        cat = self.v_cat.get()
        if cat != 'All types' and it.category != cat:
            return False
        m = self.v_method.get()
        if m != 'Any method' and it.method != m:
            return False
        if self.v_hide_bad.get() and it.condition in BAD_CONDITIONS:
            return False
        s = self.v_search.get().strip().lower()
        if s and s not in it.name.lower() and s not in it.folder.lower():
            return False
        f = self._filter_vals
        if f['smin'] is not None and it.size < f['smin']:
            return False
        if f['smax'] is not None and it.size > f['smax']:
            return False
        if f['dfrom'] and (not it.mtime or it.mtime < f['dfrom']):
            return False
        if f['dto'] and (not it.mtime or it.mtime >= f['dto']):
            return False
        if self.v_hide_dup.get() and it.dup_of:
            return False
        return True

    def _read_filters(self):
        def num(v):
            try:
                return float(v.get().replace(',', '.')) * 1024 * 1024 if v.get().strip() else None
            except ValueError:
                return None

        def day(v, plus=0):
            try:
                return datetime.datetime.strptime(v.get().strip(), '%Y-%m-%d') + datetime.timedelta(days=plus) \
                    if v.get().strip() else None
            except ValueError:
                return None
        self._filter_vals = {'smin': num(self.v_smin), 'smax': num(self.v_smax),
                             'dfrom': day(self.v_dfrom), 'dto': day(self.v_dto, 1)}

    def _insert(self, it):
        if it.method == 'Existing':
            tag = 'exist'
        elif it.method == 'Deep scan':
            tag = 'carved'
        elif it.condition in BAD_CONDITIONS:
            tag = 'bad'
        elif it.unreadable:
            tag = 'warn'
        elif it.condition == 'Good':
            tag = 'good'
        else:
            tag = 'warn'
        mod = it.mtime.strftime('%Y-%m-%d %H:%M') if it.mtime else ''
        self.shown += 1
        self.tree.insert('', 'end', iid=str(it.id), tags=(tag,),
                         values=(it.name, '✓' if it.saved_path else '', it.folder, human_size(it.size),
                                 it.ext.upper(), it.method, self._cond_text(it), mod))

    @staticmethod
    def _cond_text(it):
        if it.unreadable:
            return f'{it.condition}, {human_size(it.unreadable)} unreadable'
        return it.condition

    def _sort_key(self, it):
        c = self.sort_col
        if c == 'size':
            return it.size
        if c == 'modified':
            return it.mtime or datetime.datetime.min
        return {'name': it.name.lower(), 'folder': it.folder.lower(), 'type': it.ext,
                'found': it.method, 'condition': it.condition, 'saved': bool(it.saved_path),
                'check': it.check or ''}[c]

    def sort_by(self, col):
        if self.sort_col == col:
            self.sort_rev = not self.sort_rev
        else:
            self.sort_col, self.sort_rev = col, False
        for c, label, _ in self.COLS:
            arrow = (' ▼' if self.sort_rev else ' ▲') if c == col else ''
            self.tree.heading(c, text=label + arrow)
        self.rebuild()

    def _debounce_rebuild(self, delay=250):
        if self._rebuild_job:
            self.root.after_cancel(self._rebuild_job)
        self._rebuild_job = self.root.after(delay, self.rebuild)

    def rebuild(self):
        """Refill the list (after sorting / filtering). Big lists are filled in slices so the window
        never locks up; the first screenful appears straight away."""
        self._rebuild_job = None
        self._read_filters()
        sel = set(self.tree.selection())
        old = list(self.tree.get_children())
        self.to_insert = []
        its = [self.items[i] for i in self.order if self._passes(self.items[i])]
        if self.sort_col:
            its.sort(key=self._sort_key, reverse=self.sort_rev)
        gen = object()
        self._rebuild_gen = gen
        # clear the old rows in slices first (deleting 100,000 rows at once freezes Tk)
        if len(old) <= 5000:
            self.tree.delete(*old)
            old = []
        self.shown = len(old)

        def clear():
            if self._rebuild_gen is not gen:
                return
            if old:
                chunk = old[-5000:]
                del old[-5000:]
                self.tree.delete(*[c for c in chunk if self.tree.exists(c)])
                self.shown = len(old)
                self._update_count(building=True)
                self.root.after(1, clear)
                return
            self.shown = 0
            step()

        def step(start=0):
            if self._rebuild_gen is not gen:
                return                       # a newer rebuild took over
            t_end = time.perf_counter() + 0.03
            k = start
            while k < len(its) and time.perf_counter() < t_end:
                for it in its[k:k + 200]:
                    self._insert(it)
                k += 200
            if k < len(its):
                self._update_count(building=True)
                self.root.after(1, lambda: step(k))
                return
            self._rebuild_gen = None
            keep = [s for s in sel if self.tree.exists(s)]
            if keep:
                self.tree.selection_set(keep)
            self._update_count()
        clear()

    def _update_count(self, building=False):
        self.count_var.set(f'Showing {self.shown:,} of {len(self.items):,} found files'
                           + (' (filling the list...)' if building else ''))

    def _selected_items(self):
        return [self.items[int(i)] for i in self.tree.selection() if int(i) in self.items]

    # ---------------------------------------------------------------- preview
    def on_select(self, _e=None):
        its = self._selected_items()
        if not its:
            self.sel_var.set('Nothing selected')
            return
        self.sel_var.set(f'{len(its):,} selected ({human_size(sum(i.size for i in its))})')
        if len(its) != 1:
            return
        it = its[0]
        self.preview_token += 1
        token = self.preview_token
        self._set_info(self._info_text(it) + '\n\nLoading preview...')
        self.pv_image.configure(image='', text='')
        src = self.src

        box = (max(self.pv_image.winfo_width(), 200) - 10, max(self.pv_image.winfo_height(), 200) - 10)

        def work():
            pic = None
            extra = None
            try:
                limit = 40 * engine.MB if it.ext in IMAGE_EXTS else 8 * engine.KB
                data = it.read_head(src, limit)
            except Exception as ex:
                data = None
                err = str(ex)
            else:
                err = ''
                if data and it.ext in IMAGE_EXTS and HAVE_PIL:
                    pic = _decode_preview(data, box)
                if pic is None and token == self.preview_token:
                    try:                       # video frames, document text, raw-photo previews, strings
                        pic2, extra = previews.make(it, src, box, media.find_ffmpeg())
                        if pic2 is not None and HAVE_PIL:
                            pic = pic2
                    except Exception as ex:
                        extra = f'(no preview: {ex})'
            if token == self.preview_token:
                self.q.put(('preview', token, it, data if pic is None else data[:8 * engine.KB], err, pic, extra))

        threading.Thread(target=work, daemon=True).start()

    def _info_text(self, it):
        lines = [it.name,
                 f'Original folder: {it.folder}',
                 f'Size: {human_size(it.size)} ({it.size:,} bytes)',
                 f'Found by: {it.method}',
                 f'Condition: {it.condition}']
        if it.mtime:
            lines.append(f'Modified: {it.mtime:%Y-%m-%d %H:%M:%S}')
        if len(it.runs) > 1:
            lines.append(f'Stored in {len(it.runs)} pieces')
        if it.unreadable:
            lines.append(f'Unreadable: {human_size(it.unreadable)} sit on sectors that could not be read '
                         '(saved as zeros)')
        if it.note:
            lines.append(it.note)
        return '\n'.join(lines)

    def _set_info(self, text):
        self.pv_text.configure(state='normal')
        self.pv_text.delete('1.0', 'end')
        self.pv_text.insert('1.0', text)
        self.pv_text.configure(state='disabled')

    def _clear_preview(self):
        self.pv_image.configure(image='', text='')
        self.preview_img = None
        self._set_info('')

    def _show_preview(self, token, it, data, err, pic=None, extra=None):
        if token != self.preview_token:
            return
        info = self._info_text(it)
        if data is None:
            self._set_info(info + f'\n\nCould not read: {err}')
            return
        shown = False
        if pic is not None and HAVE_PIL and it.ext not in IMAGE_EXTS:
            self.preview_img = ImageTk.PhotoImage(pic)
            self.pv_image.configure(image=self.preview_img, text='')
            self._set_info(info + ('\n\n' + extra if extra else ''))
            return
        if it.ext in IMAGE_EXTS and HAVE_PIL:
            if isinstance(pic, Image.Image):
                self.preview_img = ImageTk.PhotoImage(pic)
                self.pv_image.configure(image=self.preview_img, text='')
            else:
                self.pv_image.configure(image='', text='Picture could not be decoded -\nthe data may be damaged.')
            self._set_info(info)
            shown = True
        if shown:
            return
        self.pv_image.configure(image='', text='')
        if extra and (it.ext in TEXT_EXTS or not extra.startswith('Readable text inside')):
            self._set_info(info + '\n\n' + extra)
        else:
            self._set_info(info + '\n\nFirst bytes:\n' + hexdump(data[:256]) +
                           ('\n\n' + extra if extra else '') + '\n\nDouble-click to open it in its normal program.')

    def open_selected(self):
        its = self._selected_items()
        if not its:
            return
        it = its[0]
        tmp = os.path.join(tempfile.gettempdir(), 'FlashRescue preview')
        if self._same_drive(tmp):
            messagebox.showwarning(APP, 'Your temp folder is on the drive being scanned, so files cannot be opened '
                                        'safely. Use Recover selected and pick another drive.')
            return
        src = self.src
        self.status_var.set(f'Opening {it.name}...')

        def work():                      # never read the drive on the window's own thread
            try:
                path = engine.recover_item(src, it, tmp, keep_folders=False)
                self.q.put(('open_file', path, None))
            except Exception as ex:
                self.q.put(('open_file', None, str(ex)))

        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- recovering
    def _same_drive(self, path):
        d = self._selected_source()
        if not d:
            return False
        return drive_letter_of(path) in d.get('letters', set())

    def recover(self, all_shown):
        if self.busy_recovering:
            return
        if not self.src:
            messagebox.showinfo(APP, 'Run a scan first.')
            return
        if all_shown:
            its = [self.items[int(i)] for i in self.tree.get_children()]
        else:
            its = self._selected_items()
        if not its:
            messagebox.showinfo(APP, 'Select the files you want back first (Ctrl+A selects all, Ctrl/Shift+click '
                                     'to pick several).' if not all_shown else 'There are no files in the list.')
            return
        already = [i for i in its if i.saved_path]
        if already and not messagebox.askyesno(
                APP, f'{len(already):,} of these were already saved by auto-recover.\n\n'
                     'Save those again too?  (No = only save the others)'):
            its = [i for i in its if not i.saved_path]
            if not its:
                return
        dest = filedialog.askdirectory(title='Where should the recovered files be saved? (NOT on the same drive)',
                                       mustexist=True)
        if not dest:
            return
        if self._same_drive(dest):
            messagebox.showerror(APP, 'Please choose a folder on a different drive.\n\n'
                                      'Saving onto the drive you are recovering from can overwrite the very files '
                                      'you are trying to get back.')
            return
        total = sum(i.size for i in its)
        try:
            free = shutil.disk_usage(dest).free
            if free < total:
                messagebox.showerror(APP, f'Not enough space there: need {human_size(total)}, '
                                          f'only {human_size(free)} free.')
                return
        except OSError:
            pass
        out_root = os.path.join(dest, f'FlashRescue {datetime.datetime.now():%Y-%m-%d %H.%M}')
        self.busy_recovering = True
        self.btn_start.configure(state='disabled')
        src = self.src

        # read in disk order: one smooth sweep instead of jumping around (much gentler on hard drives)
        its = sorted(its, key=lambda i: (i.start is None, i.start or 0))

        def work():
            done_bytes = 0
            ok, failed, done_items = 0, [], []
            t0 = time.time()
            for n, it in enumerate(its, 1):
                try:
                    path = engine.recover_item(src, it, out_root, keep_folders=True)
                    if it.inline is None:
                        it.unreadable = src.unreadable_in(it.runs)
                    if not it.saved_path:
                        it.saved_path = path
                    done_items.append((it, path))
                    ok += 1
                except Exception as ex:
                    failed.append(f'{it.folder}/{it.name}: {ex}')
                done_bytes += it.size
                if n % 5 == 0 or n == len(its) or time.time() - t0 > 0.5:
                    t0 = time.time()
                    self.q.put(('recover_progress', done_bytes / max(total, 1),
                                f'Recovering {n:,} of {len(its):,}: {it.name}'))
            damaged = [it for it, _ in done_items if it.unreadable]
            if done_items:
                try:
                    class _R:
                        pass
                    recs = []
                    for it, path in done_items:      # report the paths from this recovery
                        r = _R()
                        r.__dict__.update(it.__dict__)
                        r.saved_path = path
                        recs.append(r)
                    engine.write_report(out_root, recs, src.label)
                except Exception:
                    pass
            self.q.put(('recover_done', out_root, ok, failed, len(damaged)))

        threading.Thread(target=work, daemon=True).start()

    def _recover_finished(self, out_root, ok, failed, damaged=0):
        self.busy_recovering = False
        if not (self.scan_thread and self.scan_thread.is_alive()):
            self.btn_start.configure(state='normal')
        self.rebuild()
        msg = f'Recovered {ok:,} file(s) to:\n{out_root}'
        if damaged:
            msg += (f'\n\n{damaged:,} file(s) include unreadable sectors (filled with zeros). '
                    'See "FlashRescue report.csv" in that folder.')
        if failed:
            msg += f'\n\n{len(failed)} failed:\n' + '\n'.join(failed[:10])
        self.status_var.set(f'Recovered {ok:,} file(s) to {out_root}')
        if messagebox.askyesno(APP, msg + '\n\nOpen the folder now?'):
            try:
                open_with_default_app(out_root)
            except Exception:
                pass

    # ---------------------------------------------------------------- shadow copies
    def _add_shadows(self, shadows):
        if not shadows:
            return
        vals = list(self.drive_box['values'])
        known = {d['path'] for d in self.drives}
        for d in shadows:
            if d['path'] in known:
                continue
            d['label_full'] = f"{d['label']}   (Previous Versions snapshot)"
            self.drives.append(d)
            vals.append(d['label_full'])
        self.drive_box['values'] = vals

    def _on_source_chosen(self, _e=None):
        d = self._selected_source()
        if d and d.get('kind') == 'shadow':
            self.v_exist.set(True)
            self.v_deep.set(False)
            self._sync_opts()
            self.status_var.set('Snapshot chosen: it holds files as they were on that date. "Also list files that are '
                                'not deleted" is ticked so you can browse and restore them.')

    # ---------------------------------------------------------------- duplicates / checks
    def find_duplicates(self):
        if not self.src or not self.items:
            messagebox.showinfo(APP, 'Run a scan first.')
            return
        items = [self.items[i] for i in self.order]
        src = self.src
        self.status_var.set('Looking for duplicate files (reads the start and end of same-sized files)...')

        def work():
            n = extras.find_duplicates(src, items, progress=lambda f: self.q.put(('busy_progress', f)))
            self.q.put(('busy_done', f'Found {n:,} duplicate file(s). Tick "Hide duplicates" to hide them '
                                     '(the best copy of each stays visible).', True))

        threading.Thread(target=work, daemon=True).start()

    def check_files(self):
        if not self.src:
            messagebox.showinfo(APP, 'Run a scan first.')
            return
        its = self._selected_items() or [self.items[int(i)] for i in self.tree.get_children()]
        if not its:
            return
        if len(its) > 200 and not messagebox.askyesno(APP, f'Check all {len(its):,} files shown? Each one is read '
                                                          'in full, which can take a while.'):
            return
        if not media.find_ffmpeg()[0] and any(i.ext in media.VIDEO_EXTS | media.AUDIO_EXTS for i in its):
            self.get_ffmpeg(lambda tools: self._check_files(its, tools))
            return
        self._check_files(its, media.find_ffmpeg())

    def _check_files(self, its, tools):
        src = self.src
        its = sorted(its, key=lambda i: (i.start is None, i.start or 0))     # disk order

        def work():
            # One reader copies the files off the drive in disk order (one head, no thrashing);
            # several workers check them at the same time (decoding pictures, playing video
            # through ffmpeg), so all CPU cores help.
            tmp = tempfile.mkdtemp(prefix='flashrescue_check_')
            workers = max(2, min(8, (os.cpu_count() or 2) - 2))
            todo = queue.Queue(maxsize=workers * 2)
            counts = {'good': 0, 'bad': 0, 'n': 0}
            lock = threading.Lock()

            def checker():
                while True:
                    job = todo.get()
                    if job is None:
                        return
                    it, path, err = job
                    if err:
                        st, txt = 'unknown', f'Could not check ({err})'
                    else:
                        try:
                            st, txt = media.check_path(path, it.ext, tools)
                        except Exception as ex:
                            st, txt = 'unknown', f'Could not check ({ex})'
                        finally:
                            try:
                                os.remove(path)
                            except OSError:
                                pass
                    it.check, it.check_ok = txt, st
                    with lock:
                        counts['good'] += st == 'ok'
                        counts['bad'] += st == 'bad'
                        counts['n'] += 1
                        n = counts['n']
                    self.q.put(('checked', it, f'Checked {n:,} of {len(its):,} ({workers} at a time): {it.name}'))
                    self.q.put(('busy_progress', n / len(its)))

            threads = [threading.Thread(target=checker, daemon=True) for _ in range(workers)]
            for t in threads:
                t.start()
            try:
                for k, it in enumerate(its):
                    sub = os.path.join(tmp, str(k))
                    os.makedirs(sub, exist_ok=True)
                    try:
                        path, err = engine.recover_item(src, it, sub, keep_folders=False), None
                    except Exception as ex:
                        path, err = None, ex
                    todo.put((it, path, err))
            finally:
                for _ in threads:
                    todo.put(None)
                for t in threads:
                    t.join()
                shutil.rmtree(tmp, ignore_errors=True)
            self.q.put(('busy_done', f"Checked {len(its):,} file(s): {counts['good']:,} open fully, "
                                     f"{counts['bad']:,} damaged.", False))

        threading.Thread(target=work, daemon=True).start()

    def open_repair(self):
        its = self._selected_items()
        it = its[0] if its else None
        if it and it.ext not in media.VIDEO_EXTS:
            it = None
        RepairDialog(self, it)

    # ---------------------------------------------------------------- save / load results
    def save_results(self):
        if not self.src or not self.items:
            messagebox.showinfo(APP, 'There are no results to save yet.')
            return
        p = filedialog.asksaveasfilename(title='Save scan results', defaultextension='.frscan',
                                         filetypes=[('FlashRescue results', '*.frscan')],
                                         initialfile=engine.safe_component(f'{self.src.label} results.frscan'))
        if not p:
            return
        try:
            extras.save_results(p, self.src, [self.items[i] for i in self.order], {'label': self.src.label})
            self.status_var.set(f'Results saved to {p}')
        except OSError as ex:
            messagebox.showerror(APP, f'Could not save: {ex}')

    def load_results(self):
        p = filedialog.askopenfilename(title='Open saved scan results', initialdir=os.path.dirname(_last_scan_path()),
                                       filetypes=[('FlashRescue results', '*.frscan'), ('All files', '*.*')])
        if not p:
            return
        try:
            meta, items = extras.load_results(p)
        except Exception as ex:
            messagebox.showerror(APP, f'Could not open those results: {ex}')
            return
        info = meta['source']
        src = None
        try:
            src = (images.open_image(info['path'], info.get('label')) if os.path.isfile(info['path'])
                   else engine.RawSource(info['path'], info.get('label')))
        except (OSError, ValueError):
            pass
        if src is None or (info.get('size') and src.size and src.size != info['size']):
            if src:
                src.close()
            messagebox.showinfo(APP, f"These results are for: {info.get('label') or info['path']}\n\n"
                                     'That drive is not available (or has changed size). Plug it back in and try '
                                     'again, or choose the disk image it came from.')
            img = filedialog.askopenfilename(title='Choose the disk image these results came from',
                                             filetypes=[('Disk images', images.IMAGE_PATTERNS),
                                                        ('All files', '*.*')])
            if not img:
                return
            try:
                src = images.open_image(img)
            except (OSError, ValueError) as ex:
                messagebox.showerror(APP, f'Could not open it: {ex}')
                return
            if info.get('size') and src.size != info['size'] and not messagebox.askyesno(
                    APP, 'That image is a different size from the original drive. Use it anyway?'):
                src.close()
                return
            self.add_image(src.path)
        else:
            for k, d in enumerate(self.drives):
                if d['path'] == info['path']:
                    self.drive_box.current(k)
                    break
            else:
                if os.path.isfile(info['path']):
                    self.add_image(info['path'])
        for a, b in meta.get('bad') or []:
            src.add_bad(a, b - a)
        if self.src:
            self.src.close()
        self.src = src
        self.items.clear()
        self.order.clear()
        for it in items:
            self.items[it.id] = it
            self.order.append(it.id)
        self.rebuild()
        self.status_var.set(f"Loaded {len(items):,} results from {meta.get('saved', '')} "
                            f"({info.get('label') or info['path']}).")

    # ---------------------------------------------------------------- health & rescue
    def _health_for(self, d):
        disks = set(d.get('disks') or ())
        if d.get('kind') == 'disk' and 'number' in d:
            disks = {d['number']}
        if not disks:
            return None
        try:
            return health.check_disk(min(disks))
        except Exception:
            return None

    def show_health(self):
        d = self._selected_source()
        if not d or d.get('image') or d.get('source_obj'):
            messagebox.showinfo(APP, 'Choose a drive first (the health check reads the drive itself, not an image or '
                                     'a virtual RAID - check each member drive instead).')
            return
        self.root.config(cursor='watch')
        self.root.update_idletasks()
        try:
            rep = self._health_for(d)
        finally:
            self.root.config(cursor='')
        if rep is None:
            messagebox.showinfo(APP, 'Health information is not available for this drive.')
            return
        HealthDialog(self, d, rep)

    def open_rescue(self):
        d = self._selected_source()
        if not d or d.get('image') or d.get('source_obj'):
            messagebox.showinfo(APP, 'Choose the failing drive (or whole disk) in the list first. For a RAID, make '
                                     'a rescue image of each member drive, then build the array from the images.')
            return
        if getattr(self, 'rescue_win', None) and self.rescue_win.top.winfo_exists():
            self.rescue_win.top.lift()
            return
        self.rescue_win = RescueWindow(self, d)

    def open_source_for(self, d):
        """A read-only source for a drive-list entry (drive, disk, image file or virtual volume)."""
        if d.get('source_obj'):
            return d['source_obj'], False
        if d.get('image'):
            return images.open_image(d['path'], d.get('label')), True
        return engine.RawSource(d['path'], d.get('label')), True

    def open_unlock(self):
        d = self._selected_source()
        if not d:
            messagebox.showinfo(APP, 'Choose the encrypted drive (or its image file) in the list first.')
            return
        UnlockDialog(self, d)

    def open_search(self):
        if getattr(self, 'search_win', None) and self.search_win.top.winfo_exists():
            self.search_win.top.lift()
            return
        d = self._selected_source()
        if not d:
            messagebox.showinfo(APP, 'Choose a drive (or open a disk image) first.')
            return
        self.search_win = SearchDialog(self, d)

    def open_chances(self):
        d = self._selected_source()
        if not d:
            messagebox.showinfo(APP, 'Choose a drive (or open a disk image) first.')
            return
        ChancesDialog(self, d)

    def _ssd_trim_warning(self, d):
        """Before scanning an SSD with TRIM on: once per drive, offer the recoverability check."""
        if not IS_WIN or d.get('image') or d.get('source_obj'):
            return True
        key = d.get('path')
        warned = getattr(self, '_trim_warned', set())
        self._trim_warned = warned
        if key in warned:
            return True
        disks = sorted(d.get('disks') or ([d['number']] if d.get('number') is not None else []))
        try:
            dev = recoverability.device_info(disks)
        except Exception:
            return True
        warned.add(key)
        if dev.get('ssd') and dev.get('trim') and not dev.get('os_trim_off'):
            ans = messagebox.askyesnocancel(
                APP, 'This is an SSD with TRIM on. SSDs usually erase deleted files within minutes, so deleted '
                     'files may already be gone.\n\nRun a quick recoverability check first (it takes seconds)?\n\n'
                     'Yes = check first, No = scan anyway.', icon='warning')
            if ans is None:
                return False
            if ans:
                ChancesDialog(self, d)
                return False
        return True

    # ---------------------------------------------------------------- updates
    def start_update_check(self, force=False):
        """Ask GitHub (in the background, at most once a day) whether a newer FlashRescue is out."""
        if not (force or updater.due(self.settings)):
            return
        if getattr(self, '_update_checking', False):
            return
        self._update_checking = True
        if force:
            self.update_btn.configure(text='Checking...', state='disabled')
        settings = dict(self.settings)

        def work():
            try:
                info, err = updater.newer(settings, VERSION), None
            except Exception as ex:
                info, err = None, ex
            self.q.put(('call', self._update_checked, info, err, force))
        threading.Thread(target=work, daemon=True).start()

    def _update_checked(self, info, err, force):
        self._update_checking = False
        if err is None:
            self.settings['update_checked'] = time.time()
            save_settings(self.settings)
            self._update_info = info
        self.show_update_state(self._update_info)
        if force and err is not None:
            messagebox.showwarning(APP, f"Couldn't check for updates:\n{err}")
        elif force and not info:
            UpdatesInfo(self)

    def show_update_state(self, info):
        if info:
            self.update_btn.configure(text=f"Update to {info['version']}", style='Update.TButton', state='normal')
        else:
            self.update_btn.configure(text='Check for updates', style='TButton', state='normal')

    def update_clicked(self):
        if self._update_info:
            UpdateDialog(self, self._update_info)
        else:
            self.start_update_check(force=True)

    def busy_reason(self):
        """Why FlashRescue can't close for an update right now (None if it can)."""
        if self.scan_thread and self.scan_thread.is_alive():
            return 'a scan is running'
        if self.busy_recovering:
            return 'files are being recovered'
        rw = getattr(self, 'rescue_win', None)
        if rw and rw.thread and rw.thread.is_alive():
            return 'a rescue image is being made'
        return None

    # ---------------------------------------------------------------- ffmpeg on demand
    def get_ffmpeg(self, then=None, parent=None):
        """Offer to download ffmpeg (video/audio checking and repair). Calls then(tools) when done
        (tools = (ffmpeg, ffprobe), or (None, None) if declined or it failed)."""
        tools = media.find_ffmpeg()
        if tools[0]:
            if then:
                then(tools)
            return
        if not messagebox.askyesno(APP, 'Checking and repairing videos and audio needs ffmpeg (a free video tool, '
                                        'about 100 MB to download from gyan.dev).\n\nDownload it now? It is '
                                        'checked against its published checksum and kept for next time.',
                                   parent=parent or self.root):
            if then:
                then((None, None))
            return
        self.status_var.set('Downloading ffmpeg...')
        self.pbar.configure(mode='determinate', value=0)

        def work():
            try:
                updater.download_ffmpeg(lambda d, t: self.q.put(('busy_progress', d / t if t else 0)))
                tools2, err = media.find_ffmpeg(), None
            except Exception as ex:
                tools2, err = (None, None), ex

            def done():
                self.pbar.configure(value=100)
                if err:
                    self.status_var.set(f'Could not download ffmpeg: {err}')
                    messagebox.showwarning(APP, f'Could not download ffmpeg:\n{err}\n\nYou can also put '
                                                'ffmpeg.exe and ffprobe.exe in a "tools" folder next to '
                                                'FlashRescue.exe.', parent=parent or self.root)
                else:
                    self.status_var.set('ffmpeg is ready.')
                if then:
                    then(tools2)
            self.q.put(('call', done))
        threading.Thread(target=work, daemon=True).start()

    def open_raid(self):
        if getattr(self, 'raid_win', None) and self.raid_win.top.winfo_exists():
            self.raid_win.top.lift()
            return
        self.raid_win = RaidDialog(self)

    def add_virtual(self, src, letters):
        """Put an assembled array / LVM volume into the drive list and select it."""
        label = f'{src.label}   ({human_size(src.size)})'
        d = {'path': src.path + f'#{id(src)}', 'kind': 'virtual', 'letter': None, 'letters': set(letters),
             'label': label, 'system': False, 'source_obj': src, 'disks': set()}
        self.drives.append(d)
        vals = list(self.drive_box['values'])
        vals.append(label)
        self.drive_box['values'] = vals
        self.drive_box.current(len(vals) - 1)
        self.status_var.set(f'{src.label} is ready - press Start scan. Nothing is written to the member drives.')

    def scan_image(self, path):
        """Called when a rescue finishes: scan the image instead of the drive."""
        self.add_image(path)
        self.v_free.set(True)
        self.root.lift()
        self.start_scan()

    # ---------------------------------------------------------------- exit
    def on_close(self):
        if self.saver and self.saver.thread.is_alive() and self.saver.pending():
            if not messagebox.askyesno(APP, f'{self.saver.pending():,} found file(s) are still waiting to be '
                                            'auto-saved.\n\nQuit anyway?'):
                return
        try:
            self._save_settings()
        except Exception:
            pass
        rw = getattr(self, 'rescue_win', None)
        if rw and rw.thread and rw.thread.is_alive():
            if not messagebox.askyesno(APP, 'A rescue is running. Stop it and quit? (Progress is saved.)'):
                return
            rw.imager.stop()
            rw.thread.join(timeout=30)
        if self.scanner:
            self.scanner.cancel.set()
        if self.src:
            self.src.close()
        self.root.destroy()


VERDICT_COLORS = {'good': '#1b6e20', 'warning': '#a35a00', 'failing': '#b01818', 'unknown': '#555555'}
VERDICT_WORDS = {'good': 'Healthy', 'warning': 'Warning signs', 'failing': 'FAILING', 'unknown': 'Not available'}


class HealthDialog:
    def __init__(self, app, d, rep):
        self.app = app
        top = self.top = tk.Toplevel(app.root)
        top.title(f'{APP} - drive health')
        top.geometry('760x520')
        top.transient(app.root)
        name = d['label'] if d['kind'] == 'disk' else f"{d['letter']}: {d['label'] or ''}"
        frm = ttk.Frame(top, padding=12)
        frm.pack(fill='both', expand=True)
        ttk.Label(frm, text=name, font=('Segoe UI', 11, 'bold') if IS_WIN else ('Helvetica', 11, 'bold')).pack(anchor='w')
        tk.Label(frm, text=VERDICT_WORDS[rep['verdict']], fg=VERDICT_COLORS[rep['verdict']],
                 font=('Segoe UI', 18, 'bold') if IS_WIN else ('Helvetica', 18, 'bold')).pack(anchor='w', pady=(4, 2))
        ttk.Label(frm, text=rep['summary'], wraplength=720, justify='left').pack(anchor='w')
        if rep.get('source'):
            ttk.Label(frm, text=f"Source: {rep['source']}", foreground='#666').pack(anchor='w', pady=(4, 0))
        txt = tk.Text(frm, height=14, wrap='none', font=('Consolas', 9) if IS_WIN else ('Courier', 9))
        txt.pack(fill='both', expand=True, pady=8)
        txt.insert('1.0', '\n'.join(rep['lines']) or 'No details.')
        txt.configure(state='disabled')
        btns = ttk.Frame(frm)
        btns.pack(fill='x')
        ttk.Button(btns, text='Close', command=top.destroy).pack(side='right')
        if rep['verdict'] in ('failing', 'warning'):
            ttk.Button(btns, text='Make a rescue image...',
                       command=lambda: (top.destroy(), app.open_rescue())).pack(side='right', padx=6)


class RescueWindow:
    COLS, ROWS, CELL = 100, 22, 8
    COLORS = {'g': '#3a9a3a', 'p': '#a8d8a0', 'u': '#cfcfcf', 'f': '#e0a020', 'b': '#c02828'}

    def __init__(self, app, d):
        self.app = app
        self.d = d
        self.imager = None
        self.thread = None
        self.q = queue.Queue()
        top = self.top = tk.Toplevel(app.root)
        top.title(f'{APP} - rescue image')
        top.geometry('960x860')
        top.protocol('WM_DELETE_WINDOW', self.close)
        try:
            src = engine.RawSource(d['path'], d.get('label'))
            self.size, self.sector = src.size, src.sector_size
            src.close()
        except OSError as ex:
            messagebox.showerror(APP, f'Could not open the drive:\n{ex}\n\nRun FlashRescue as administrator.',
                                 parent=top)
            top.destroy()
            return
        frm = ttk.Frame(top, padding=10)
        frm.pack(fill='both', expand=True)
        name = (f"Whole disk {d.get('number')}: {d['label']}" if d['kind'] == 'disk'
                else f"{d['letter']}: {d['label'] or ''} ({d.get('fs', '')})")
        ttk.Label(frm, text=f'Drive: {name}   ({human_size(self.size)})',
                  font=('Segoe UI', 10, 'bold') if IS_WIN else ('Helvetica', 10, 'bold')).pack(anchor='w')
        ttk.Label(frm, wraplength=920, justify='left', foreground='#444', text=(
            'Copies the drive to an image file once, saving the easy parts first and coming back for the hard parts. '
            'Afterwards all scanning and recovering happens on the image, so the failing drive is not touched again. '
            'Progress is saved continuously: you can stop, unplug or restart and pick up where you left off.')).pack(
            anchor='w', pady=(2, 6))

        dst = ttk.LabelFrame(frm, text=' Save the image to (a different drive with enough space) ', padding=6)
        dst.pack(fill='x')
        self.v_dest = tk.StringVar(value=app.settings.get('rescue_dir', ''))
        ttk.Button(dst, text='Browse...', command=self.pick_dest).pack(side='right', padx=(6, 0))
        ttk.Entry(dst, textvariable=self.v_dest, width=60).pack(side='left', fill='x', expand=True)
        self.v_dest.trace_add('write', lambda *a: self._check_existing())

        opt = ttk.LabelFrame(frm, text=' Options ', padding=6)
        opt.pack(fill='x', pady=6)
        self.v_timeout = tk.IntVar(value=8)
        self.v_retries = tk.IntVar(value=1)
        self.v_cool = tk.IntVar(value=50)
        self.v_reverse = tk.BooleanVar(value=True)
        self.v_skipslow = tk.BooleanVar(value=True)
        self.v_scan = tk.BooleanVar(value=True)
        self.v_first = tk.BooleanVar(value=True)
        ttk.Label(opt, text='Give up on a read after').grid(row=0, column=0, sticky='w')
        ttk.Spinbox(opt, from_=2, to=120, width=5, textvariable=self.v_timeout).grid(row=0, column=1, padx=4)
        ttk.Label(opt, text='seconds').grid(row=0, column=2, sticky='w')
        ttk.Label(opt, text='Retry bad sectors').grid(row=0, column=3, sticky='w', padx=(24, 0))
        ttk.Spinbox(opt, from_=0, to=5, width=4, textvariable=self.v_retries).grid(row=0, column=4, padx=4)
        ttk.Label(opt, text='times at the end').grid(row=0, column=5, sticky='w')
        ttk.Label(opt, text='Rest the drive above').grid(row=1, column=0, sticky='w', pady=(4, 0))
        ttk.Spinbox(opt, from_=0, to=70, width=5, textvariable=self.v_cool).grid(row=1, column=1, padx=4, pady=(4, 0))
        self.lbl_f = ttk.Label(opt, text='')
        self.lbl_f.grid(row=1, column=2, columnspan=4, sticky='w', pady=(4, 0))
        self.v_cool.trace_add('write', lambda *a: self._show_f())
        self._show_f()
        ttk.Checkbutton(opt, text='Skip slow areas on the first pass', variable=self.v_skipslow).grid(
            row=2, column=0, columnspan=3, sticky='w', pady=(4, 0))
        ttk.Checkbutton(opt, text='Reverse pass (reach the far side of bad areas)', variable=self.v_reverse).grid(
            row=2, column=3, columnspan=3, sticky='w', padx=(24, 0), pady=(4, 0))
        ttk.Checkbutton(opt, text='Scan the image for deleted files when finished', variable=self.v_scan).grid(
            row=3, column=0, columnspan=3, sticky='w', pady=(4, 0))
        ttk.Checkbutton(opt, text='Copy file tables and deleted files first (if the drive dies early, they are '
                                  'already saved)', variable=self.v_first).grid(
            row=3, column=3, columnspan=3, sticky='w', padx=(24, 0), pady=(4, 0))

        tips = ttk.Label(frm, wraplength=920, justify='left', foreground='#8a4b00', text=(
            'While rescuing: if Windows offers to "Scan and fix" or format this drive, say NO - repairs write to the '
            'drive and can destroy what is left. Keep the cable still, and use a USB port on the back of the PC. '
            'The PC will not go to sleep during the rescue.'))
        tips.pack(anchor='w', pady=(0, 6))

        btns = ttk.Frame(frm)
        btns.pack(fill='x')
        self.btn_start = ttk.Button(btns, text='Start rescue', style='Big.TButton', command=self.start)
        self.btn_start.pack(side='left')
        self.btn_pause = ttk.Button(btns, text='Pause', command=self.toggle_pause, state='disabled')
        self.btn_pause.pack(side='left', padx=6)
        self.btn_stop = ttk.Button(btns, text='Stop', command=self.stop, state='disabled')
        self.btn_stop.pack(side='left')
        self.btn_scan = ttk.Button(btns, text='Scan the image now', command=self.scan_now, state='disabled')
        self.btn_scan.pack(side='right')

        pf = ttk.Frame(frm)
        pf.pack(fill='x', pady=(8, 0))
        self.pbar = ttk.Progressbar(pf, mode='determinate', maximum=1000)
        self.pbar.pack(fill='x')
        self.eta_var = tk.StringVar(value='Not started')
        ttk.Label(pf, textvariable=self.eta_var, font=('Segoe UI', 10, 'bold') if IS_WIN else
                  ('Helvetica', 10, 'bold')).pack(anchor='w', pady=(2, 0))
        self.rate_avg = 0.0

        st = ttk.Frame(frm)
        st.pack(fill='x', pady=8)
        self.stat_vars = {}
        fields = [('rescued', 'Rescued'), ('bad', 'Bad sectors'), ('untried', 'Not tried yet'),
                  ('failed', 'Failed areas left'), ('pass', 'Stage'), ('pos', 'Position'),
                  ('rate', 'Read speed'), ('errors', 'Read errors'), ('temp', 'Temperature'), ('elapsed', 'Elapsed')]
        for k, (key, label) in enumerate(fields):
            r, c = divmod(k, 2)
            ttk.Label(st, text=label + ':').grid(row=r, column=c * 2, sticky='w', padx=(0 if c == 0 else 30, 6))
            v = tk.StringVar(value='-')
            self.stat_vars[key] = v
            ttk.Label(st, textvariable=v).grid(row=r, column=c * 2 + 1, sticky='w')
        self.msg_var = tk.StringVar(value='')
        ttk.Label(frm, textvariable=self.msg_var, foreground='#1f4e9a', wraplength=920).pack(anchor='w')

        W, H = self.COLS * (self.CELL + 1), self.ROWS * (self.CELL + 1)
        self.canvas = tk.Canvas(frm, width=W, height=H, bg='white', highlightthickness=0)
        self.canvas.pack(anchor='w', pady=(6, 2))
        self.rects = []
        for r in range(self.ROWS):
            for c in range(self.COLS):
                x, y = c * (self.CELL + 1), r * (self.CELL + 1)
                self.rects.append(self.canvas.create_rectangle(x, y, x + self.CELL, y + self.CELL,
                                                               fill=self.COLORS['u'], width=0))
        self.cell_state = ['u'] * len(self.rects)
        leg = ttk.Frame(frm)
        leg.pack(anchor='w')
        for code, text in (('g', 'Rescued'), ('p', 'Partly rescued'), ('u', 'Not tried yet'),
                           ('f', 'Failed - will retry in smaller pieces'), ('b', 'Bad sectors')):
            tk.Label(leg, bg=self.COLORS[code], width=2).pack(side='left', padx=(0, 3))
            ttk.Label(leg, text=text).pack(side='left', padx=(0, 14))
        each = self.size / len(self.rects)
        ttk.Label(frm, text=f'Each square is about {human_size(int(each))} of the drive, start to end, left to right.',
                  foreground='#666').pack(anchor='w', pady=(2, 0))
        self._check_existing()
        self.top.after(500, self.poll)

    def _show_f(self):
        try:
            c = int(self.v_cool.get())
            self.lbl_f.configure(text=f'\u00b0C ({round(c * 9 / 5 + 32)}\u00b0F) - checked every minute; 0 turns this off' if c else 'off')
        except (tk.TclError, ValueError):
            pass

    def _image_paths(self):
        p = self.v_dest.get().strip()
        if not p:
            return None, None
        return p, p + '.map'

    def pick_dest(self):
        base = (f"Disk {self.d.get('number')}" if self.d['kind'] == 'disk' else f"Drive {self.d['letter']}")
        label = engine.safe_component(f"{base} {self.d.get('label') or ''}".strip())
        p = filedialog.asksaveasfilename(parent=self.top, title='Save the rescue image as',
                                         initialfile=f'{label} rescue.img', defaultextension='.img',
                                         filetypes=[('Disk image', '*.img'), ('All files', '*.*')],
                                         confirmoverwrite=False)
        if p:
            self.v_dest.set(os.path.normpath(p))

    def _check_existing(self):
        img, mp = self._image_paths()
        if img and mp and os.path.isfile(mp) and os.path.isfile(img):
            try:
                mb = rescue.Mapbook.load(mp, self.size)
                t = mb.totals()
                self._draw(mb.cells(len(self.rects)))
                self.btn_start.configure(text='Resume rescue')
                done = mb.current_pass >= 7
                self.msg_var.set(f'An earlier rescue of this drive was found: {human_size(t[rescue.FINISHED])} rescued '
                                 f'({100.0 * t[rescue.FINISHED] / max(self.size, 1):.2f}%).'
                                 + (' It is complete.' if done else ' Press Resume to continue it.'))
                self.btn_scan.configure(state='normal')
                return
            except Exception:
                pass
        self.btn_start.configure(text='Start rescue')

    def start(self):
        img, mp = self._image_paths()
        if not img:
            messagebox.showinfo(APP, 'Choose where to save the image first.', parent=self.top)
            return
        folder = os.path.dirname(os.path.abspath(img))
        if not os.path.isdir(folder):
            messagebox.showerror(APP, 'That folder does not exist.', parent=self.top)
            return
        if drive_letter_of(img) in self.d.get('letters', set()):
            messagebox.showerror(APP, 'The image must go on a different drive from the one being rescued.',
                                 parent=self.top)
            return
        try:
            free = shutil.disk_usage(folder).free
            have = os.path.getsize(img) if os.path.exists(img) else 0
            if free + have < self.size and not messagebox.askyesno(
                    APP, f'The destination has {human_size(free)} free but the drive is {human_size(self.size)}.\n\n'
                         'The image only grows as data is rescued, so this may still work if the drive is partly '
                         'empty or unreadable, but it could run out of space. Continue?', parent=self.top):
                return
        except OSError:
            pass
        self.app.settings['rescue_dir'] = img
        self.app._save_settings()
        try:
            timeout = max(2, int(self.v_timeout.get()))
            retries = max(0, int(self.v_retries.get()))
            cool = max(0, int(self.v_cool.get()))
        except (tk.TclError, ValueError):
            messagebox.showerror(APP, 'Please enter whole numbers in the options.', parent=self.top)
            return
        opts = rescue.RescueOptions(timeout=timeout, retries=retries, reverse=self.v_reverse.get(),
                                    slow_secs=2.0 if self.v_skipslow.get() else 1e9, cool_limit_c=cool,
                                    important_first=self.v_first.get())
        disks = set(self.d.get('disks') or ())
        if self.d['kind'] == 'disk':
            disks = {self.d['number']}
        temp_reader = (lambda n=min(disks): health.read_temperature(n)) if disks and cool else None
        try:
            self.imager = rescue.RescueImager(
                self.d['path'], self.size, img, mp, opts,
                on_status=lambda m: self.q.put(('status', m)),
                on_progress=lambda st: self.q.put(('progress', st)),
                source_label=self.d.get('label') or self.d['path'], sector_size=self.sector,
                temp_reader=temp_reader)
        except Exception as ex:
            messagebox.showerror(APP, f'Could not start the rescue:\n{ex}', parent=self.top)
            return

        def work():
            try:
                msg = self.imager.run()
                self.q.put(('done', msg, self.imager.state))
            except Exception as ex:
                self.q.put(('done', f'The rescue stopped with an error: {ex}', 'error'))

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        self.rate_avg = 0.0
        self.eta_var.set('Starting...')
        self.btn_start.configure(state='disabled')
        self.btn_pause.configure(state='normal', text='Pause')
        self.btn_stop.configure(state='normal')
        self.btn_scan.configure(state='disabled')

    def toggle_pause(self):
        if not self.imager:
            return
        if self.imager.pause_ev.is_set():
            self.imager.pause(False)
            self.btn_pause.configure(text='Pause')
            self.msg_var.set('Resuming...')
        else:
            self.imager.pause(True)
            self.btn_pause.configure(text='Resume')

    def stop(self):
        if self.imager:
            self.imager.stop()
            self.msg_var.set('Stopping - saving progress...')

    def scan_now(self):
        img, _ = self._image_paths()
        if img and os.path.isfile(img):
            self.app.scan_image(img)

    def _draw(self, cells):
        for k, c in enumerate(cells):
            if self.cell_state[k] != c:
                self.cell_state[k] = c
                self.canvas.itemconfigure(self.rects[k], fill=self.COLORS[c])

    def poll(self):
        if not self.top.winfo_exists():
            return
        last = None
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == 'status':
                    self.msg_var.set(m[1])
                elif m[0] == 'progress':
                    last = m[1]
                elif m[0] == 'done':
                    self._finished(m[1], m[2])
        except queue.Empty:
            pass
        if last:
            self._show_stats(last)
        if self.imager:
            self._draw(self.imager.map.cells(len(self.rects)))
        self.top.after(700, self.poll)

    def _show_stats(self, st):
        size = max(st['size'], 1)
        v = self.stat_vars
        v['rescued'].set(f"{human_size(st['rescued'])}  ({100.0 * st['rescued'] / size:.2f}%)")
        v['bad'].set(human_size(st['bad']))
        v['untried'].set(human_size(st['untried']))
        v['failed'].set(human_size(st['failed']))
        v['pass'].set(rescue.PASS_NAMES.get(st['pass'], str(st['pass'])))
        v['pos'].set(f"{human_size(st['pos'])} ({100.0 * st['pos'] / size:.1f}%)")
        v['rate'].set(f"{st['rate'] / (1024 * 1024):.1f} MB/s" if st['rate'] else '-')
        v['errors'].set(f"{st['errors']:,}")
        t = st['temp']
        v['temp'].set(f'{t}\u00b0C ({round(t * 9 / 5 + 32)}\u00b0F)' if t is not None else 'n/a')
        v['elapsed'].set(App._fmt_secs(st['elapsed']))
        if st['message']:
            self.msg_var.set(st['message'])
        self._show_eta(st)

    def _show_eta(self, st):
        """Progress bar + time left. While copying, time left = what's not tried yet / recent speed
        (smoothed). The later passes only revisit damaged spots, where speed says little about time."""
        size = max(st['size'], 1)
        settled = st['rescued'] + st['bad']
        self.pbar['value'] = 1000.0 * settled / size
        pct = f'{100.0 * settled / size:.1f}%'
        state = st['state']
        if st['rate']:
            self.rate_avg = st['rate'] if not self.rate_avg else 0.8 * self.rate_avg + 0.2 * st['rate']
        if state in ('paused', 'cooling', 'waiting'):
            self.eta_var.set(f'{pct} done - {st["message"] or state.capitalize()}')
            return
        if state in ('finished', 'stopped', 'error'):
            self.eta_var.set(f'{pct} done - {state}')
            return
        left = st['untried']
        if st['pass'] <= 3 and left > 0:
            if self.rate_avg > 0:
                secs = left / self.rate_avg
                tail = (' + time for any damaged areas' if st['failed'] or st['bad'] else '')
                self.eta_var.set(f'{pct} done - about {App._fmt_secs(secs)} left '
                                 f'at {self.rate_avg / (1024 * 1024):.1f} MB/s{tail}')
            else:
                self.eta_var.set(f'{pct} done - working out the time left...')
        elif st['failed'] or st['pass'] >= 4:
            self.eta_var.set(f'{pct} done - going back over damaged areas ({human_size(st["failed"])} left). '
                             'How long this takes depends on the damage; you can stop at any time and keep '
                             'what has been rescued.')
        else:
            self.eta_var.set(f'{pct} done')

    def _finished(self, msg, state):
        self.msg_var.set(msg)
        self.btn_start.configure(state='normal')
        self.btn_pause.configure(state='disabled', text='Pause')
        self.btn_stop.configure(state='disabled')
        self._check_existing()
        self.msg_var.set(msg)
        img, _ = self._image_paths()
        if img and os.path.isfile(img):
            self.btn_scan.configure(state='normal')
        if state == 'finished' and self.v_scan.get() and img:
            self.app.scan_image(img)
        elif state == 'finished':
            messagebox.showinfo(APP, msg, parent=self.top)

    def close(self):
        if self.thread and self.thread.is_alive():
            if not messagebox.askyesno(APP, 'Stop the rescue and close this window?\n\n'
                                            'Progress is saved - you can resume later by choosing the same image file.',
                                       parent=self.top):
                return
            self.imager.stop()
            self.thread.join(timeout=30)
        self.top.destroy()


class RepairDialog:
    def __init__(self, app, item=None):
        self.app = app
        self.item = item
        self.tools = media.find_ffmpeg()
        top = self.top = tk.Toplevel(app.root)
        top.title(f'{APP} - repair video')
        top.geometry('900x620')
        frm = ttk.Frame(top, padding=10)
        frm.pack(fill='both', expand=True)
        if not self.tools[0]:
            nf = ttk.Frame(frm)
            nf.pack(fill='x', pady=(0, 6))
            self.lbl_noff = ttk.Label(nf, foreground='#b01818', wraplength=700, text=(
                'Repairing videos needs ffmpeg, which is not here yet.'))
            self.lbl_noff.pack(side='left')

            def got(tools):
                if tools[0]:
                    self.tools = tools
                    nf.destroy()
            ttk.Button(nf, text='Download ffmpeg...', command=lambda: app.get_ffmpeg(got, top)).pack(
                side='left', padx=8)
        ttk.Label(frm, wraplength=860, justify='left', foreground='#444', text=(
            'Repairs videos that will not play - typically a recording that was cut off (camera died, card pulled) '
            'or a file that was only partly recovered. If the video does not carry its own codec settings, '
            'a reference helps: any healthy video recorded by the same model of phone or camera at similar '
            'settings. Other clips from the same card or drive are ideal, and FlashRescue suggests them '
            'automatically. Without one, H.264 settings are worked out by analysis.')).pack(anchor='w')

        src_f = ttk.LabelFrame(frm, text=' Video to repair ', padding=6)
        src_f.pack(fill='x', pady=6)
        self.v_input = tk.StringVar(value='item' if item else 'file')
        self.v_path = tk.StringVar()
        if item:
            ttk.Radiobutton(src_f, text=f'From the scan: {item.name}  ({item.folder}, {human_size(item.size)})',
                            variable=self.v_input, value='item').grid(row=0, column=0, columnspan=3, sticky='w')
        ttk.Radiobutton(src_f, text='A video file:', variable=self.v_input, value='file').grid(row=1, column=0,
                                                                                               sticky='w')
        ttk.Entry(src_f, textvariable=self.v_path, width=80).grid(row=1, column=1, sticky='we', padx=4)
        ttk.Button(src_f, text='Browse...', command=self.pick_input).grid(row=1, column=2)
        src_f.columnconfigure(1, weight=1)

        ref_f = ttk.LabelFrame(frm, text=' Reference video (optional) ', padding=6)
        ref_f.pack(fill='x')
        self.refs = []
        if app.items:
            self.refs = media.pick_reference([app.items[i] for i in app.order], item, self.tools)
        choices = ['Automatic (try without, then the suggested clips below)'] + \
                  [f'From the scan: {r.name}  ({r.folder}, {human_size(r.size)})' for r in self.refs] + \
                  ['A video file on this computer...']
        self.v_ref = tk.StringVar(value=choices[0])
        self.ref_box = ttk.Combobox(ref_f, textvariable=self.v_ref, values=choices, state='readonly', width=100)
        self.ref_box.pack(fill='x')
        self.ref_box.bind('<<ComboboxSelected>>', self._ref_chosen)
        self.ref_file = None

        out_f = ttk.LabelFrame(frm, text=' Save the repaired video in ', padding=6)
        out_f.pack(fill='x', pady=6)
        self.v_out = tk.StringVar(value=app.settings.get('repair_dir', ''))
        ttk.Button(out_f, text='Browse...', command=self.pick_out).pack(side='right', padx=(6, 0))
        ttk.Entry(out_f, textvariable=self.v_out, width=60).pack(side='left', fill='x', expand=True)

        b = ttk.Frame(frm)
        b.pack(fill='x')
        self.btn_go = ttk.Button(b, text='Repair', style='Big.TButton', command=self.go)
        self.btn_go.pack(side='left')
        self.btn_open = ttk.Button(b, text='Open result', command=self.open_result, state='disabled')
        self.btn_open.pack(side='left', padx=6)
        self.log = tk.Text(frm, height=14, wrap='word', font=('Consolas', 9) if IS_WIN else ('Courier', 9))
        self.log.pack(fill='both', expand=True, pady=(8, 0))
        self.q = queue.Queue()
        self.result = None
        self.top.after(300, self.poll)

    def pick_input(self):
        p = filedialog.askopenfilename(parent=self.top, title='Video to repair',
                                       filetypes=[('Videos', '*.mp4 *.mov *.m4v *.3gp *.avi *.mkv *.webm *.mts'),
                                                  ('All files', '*.*')])
        if p:
            self.v_path.set(p)
            self.v_input.set('file')

    def pick_out(self):
        d = filedialog.askdirectory(parent=self.top, title='Save the repaired video in', mustexist=True)
        if d:
            self.v_out.set(os.path.normpath(d))

    def _ref_chosen(self, _e=None):
        if self.ref_box.current() == len(self.ref_box['values']) - 1:
            p = filedialog.askopenfilename(parent=self.top, title='Healthy video from the same phone or camera',
                                           filetypes=[('Videos', '*.mp4 *.mov *.m4v *.3gp'), ('All files', '*.*')])
            if p:
                self.ref_file = p
                vals = list(self.ref_box['values'])
                vals.insert(-1, f'File: {p}')
                self.ref_box['values'] = vals
                self.ref_box.current(len(vals) - 2)
            else:
                self.ref_box.current(0)

    def say(self, text):
        self.q.put(('log', text))

    def go(self):
        out_dir = self.v_out.get().strip()
        if not out_dir or not os.path.isdir(out_dir):
            messagebox.showinfo(APP, 'Choose a folder to save the repaired video in.', parent=self.top)
            return
        if self.app._same_drive(out_dir):
            messagebox.showerror(APP, 'Please save to a different drive from the one being recovered.', parent=self.top)
            return
        if self.v_input.get() == 'file' and not os.path.isfile(self.v_path.get()):
            messagebox.showinfo(APP, 'Choose the video to repair.', parent=self.top)
            return
        self.app.settings['repair_dir'] = out_dir
        self.app._save_settings()
        sel = self.ref_box.current()
        vals = list(self.ref_box['values'])
        ref_choice = ('auto', None)
        if 1 <= sel <= len(self.refs):
            ref_choice = ('item', self.refs[sel - 1])
        elif sel > 0 and vals[sel].startswith('File: '):
            ref_choice = ('file', vals[sel][6:])
        self.btn_go.configure(state='disabled')
        self.log.delete('1.0', 'end')
        threading.Thread(target=self._work, args=(out_dir, ref_choice), daemon=True).start()

    def _work(self, out_dir, ref_choice):
        tmp = tempfile.mkdtemp(prefix='flashrescue_repair_')
        try:
            if self.v_input.get() == 'item' and self.item:
                self.say(f'Copying {self.item.name} out of the scan...')
                path = engine.recover_item(self.app.src, self.item, tmp, keep_folders=False)
            else:
                path = self.v_path.get()

            def ref_path(r):
                if r is None:
                    return None
                return engine.recover_item(self.app.src, r, os.path.join(tmp, 'ref'), keep_folders=False)

            attempts = []
            if ref_choice[0] == 'auto':
                attempts = [None] + list(self.refs[:3])
            elif ref_choice[0] == 'item':
                attempts = [ref_choice[1]]
            else:
                attempts = [ref_choice[1]]
            out, msg = None, 'nothing tried'
            fps_hint = None
            if ref_choice[0] == 'auto' and self.refs:
                try:      # a clip from the same device tells us the frame rate even if its settings aren't needed
                    fps_hint = media.frame_rate(ref_path(self.refs[0]), self.tools)
                except Exception:
                    fps_hint = None
            for att in attempts:
                rp = att if isinstance(att, str) else ref_path(att)
                if att is not None:
                    self.say(f'\n--- trying reference: {os.path.basename(rp)}')
                out, msg = media.repair_video(path, out_dir, self.tools, reference=rp, log=self.say, fps_hint=fps_hint)
                self.say(msg)
                if out and msg.startswith(('Rebuilt:', 'Re-wrapped:')):
                    break
            self.q.put(('done', out, msg))
        except Exception as ex:
            self.q.put(('done', None, f'Repair failed: {ex}'))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def poll(self):
        if not self.top.winfo_exists():
            return
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == 'log':
                    self.log.insert('end', m[1] + '\n')
                    self.log.see('end')
                else:
                    self.btn_go.configure(state='normal')
                    self.result = m[1]
                    self.log.insert('end', '\n' + ('Saved: ' + m[1] if m[1] else 'No repaired file was made.') + '\n')
                    self.log.see('end')
                    if m[1]:
                        self.btn_open.configure(state='normal')
        except queue.Empty:
            pass
        self.top.after(300, self.poll)

    def open_result(self):
        if self.result and os.path.exists(self.result):
            open_with_default_app(self.result)


class RaidDialog:
    """Build a virtual RAID from member drives / images."""

    def __init__(self, app):
        self.app = app
        self.members = []        # dicts: {'label','path','src'} or None for a missing drive
        top = self.top = tk.Toplevel(app.root)
        top.title(f'{APP} - virtual RAID')
        top.geometry('980x720')
        frm = ttk.Frame(top, padding=10)
        frm.pack(fill='both', expand=True)
        ttk.Label(frm, wraplength=940, justify='left', foreground='#444', text=(
            'Rebuilds a RAID array from its member drives - from a NAS (Synology, QNAP, ...), a RAID card or '
            'motherboard RAID - without writing anything to them. Add every member (or a rescue image of it; '
            'a failing member should always be imaged first), then let FlashRescue find the settings. Up to one '
            'missing drive can be rebuilt on RAID 5, two on RAID 6.')).pack(anchor='w')

        mf = ttk.LabelFrame(frm, text=' 1. Member drives (in order, for arrays without stored settings) ', padding=6)
        mf.pack(fill='x', pady=6)
        self.lst = tk.Listbox(mf, height=7, font=('Segoe UI', 9) if IS_WIN else ('Helvetica', 9))
        self.lst.grid(row=0, column=0, rowspan=6, sticky='nsew')
        mf.columnconfigure(0, weight=1)
        choices = [d for d in app.drives if d.get('kind') in ('disk', 'image', 'volume') and not d.get('source_obj')]
        self.choices = choices
        self.v_pick = tk.StringVar()
        names = [d.get('label_full') or self._drive_label(d) for d in choices]
        self.pick = ttk.Combobox(mf, textvariable=self.v_pick, values=names, state='readonly', width=46)
        self.pick.grid(row=0, column=1, sticky='we', padx=6)
        ttk.Button(mf, text='Add this drive', command=self.add_drive).grid(row=1, column=1, sticky='we', padx=6)
        ttk.Button(mf, text='Add image file(s)...', command=self.add_images).grid(row=2, column=1, sticky='we', padx=6)
        ttk.Button(mf, text='Add "missing drive"', command=self.add_missing).grid(row=3, column=1, sticky='we', padx=6)
        bb = ttk.Frame(mf)
        bb.grid(row=4, column=1, sticky='we', padx=6)
        ttk.Button(bb, text='Up', width=6, command=lambda: self.move(-1)).pack(side='left')
        ttk.Button(bb, text='Down', width=6, command=lambda: self.move(1)).pack(side='left', padx=4)
        ttk.Button(bb, text='Remove', command=self.remove).pack(side='left')

        af = ttk.LabelFrame(frm, text=' 2. Settings ', padding=6)
        af.pack(fill='x')
        ttk.Button(af, text='Read settings from the drives (NAS / Linux RAID)', command=self.find_md).grid(
            row=0, column=0, columnspan=3, sticky='w')
        ttk.Button(af, text='Work out settings (RAID card / motherboard RAID)', command=self.work_out).grid(
            row=0, column=3, columnspan=4, sticky='w', padx=(10, 0))
        ttk.Label(af, text='Level').grid(row=1, column=0, sticky='w', pady=(8, 0))
        self.v_level = tk.StringVar(value='RAID 5')
        ttk.Combobox(af, textvariable=self.v_level, state='readonly', width=14,
                     values=['RAID 0', 'RAID 1', 'RAID 5', 'RAID 6', 'RAID 10', 'Spanned (JBOD)']).grid(
            row=1, column=1, sticky='w', pady=(8, 0), padx=4)
        ttk.Label(af, text='Stripe size').grid(row=1, column=2, sticky='e', pady=(8, 0))
        self.v_chunk = tk.StringVar(value='64 KB')
        ttk.Combobox(af, textvariable=self.v_chunk, state='readonly', width=8,
                     values=['16 KB', '32 KB', '64 KB', '128 KB', '256 KB', '512 KB', '1 MB']).grid(
            row=1, column=3, sticky='w', pady=(8, 0), padx=4)
        ttk.Label(af, text='Layout').grid(row=1, column=4, sticky='e', pady=(8, 0))
        self.v_layout = tk.StringVar(value='left-symmetric')
        ttk.Combobox(af, textvariable=self.v_layout, state='readonly', width=16, values=raid.LAYOUTS).grid(
            row=1, column=5, sticky='w', pady=(8, 0), padx=4)
        ttk.Label(af, text='Data starts at (KB)').grid(row=1, column=6, sticky='e', pady=(8, 0))
        self.v_offset = tk.StringVar(value='0')
        ttk.Entry(af, textvariable=self.v_offset, width=8).grid(row=1, column=7, sticky='w', pady=(8, 0), padx=4)
        ttk.Button(af, text='Build with these settings', command=self.build_manual).grid(
            row=2, column=0, columnspan=4, sticky='w', pady=(8, 0))

        rf = ttk.LabelFrame(frm, text=' 3. Results - pick one and press "Use selected" ', padding=6)
        rf.pack(fill='both', expand=True, pady=6)
        self.res = tk.Listbox(rf, height=9, font=('Segoe UI', 9) if IS_WIN else ('Helvetica', 9))
        self.res.pack(fill='both', expand=True)
        self.results = []        # (label, factory) where factory() -> source
        b2 = ttk.Frame(frm)
        b2.pack(fill='x')
        self.pbar = ttk.Progressbar(b2, mode='determinate', maximum=100, length=300)
        self.pbar.pack(side='left')
        self.msg = tk.StringVar(value='')
        ttk.Label(b2, textvariable=self.msg).pack(side='left', padx=8)
        ttk.Button(b2, text='Use selected', style='Big.TButton', command=self.use).pack(side='right')
        self.q = queue.Queue()
        self.cancel = threading.Event()
        top.protocol('WM_DELETE_WINDOW', self.close)
        top.after(300, self.poll)

    @staticmethod
    def _drive_label(d):
        if d.get('kind') == 'disk':
            return f"Whole disk {d.get('number')}: {d.get('label')} ({human_size(d.get('size', 0))})"
        if d.get('kind') == 'image':
            return d.get('label')
        return f"{d.get('letter')}: {d.get('label') or ''} ({human_size(d.get('size', 0))})"

    # ---- members
    def _refresh(self):
        self.lst.delete(0, 'end')
        for k, m in enumerate(self.members, 1):
            if m is None:
                self.lst.insert('end', f'{k}.  -- missing drive (rebuilt from parity) --')
            else:
                self.lst.insert('end', f"{k}.  {m['label']}   ({human_size(m['src'].size)})"
                                + ('   + rescue map' if m['src'].map_path else ''))

    def _open(self, path, label, letters=()):
        try:
            src = images.open_image(path, label) if os.path.isfile(path) else engine.RawSource(path, label)
        except (OSError, ValueError) as ex:
            messagebox.showerror(APP, f'Could not open {label}:\n{ex}', parent=self.top)
            return
        mp = rescue.find_map_for_image(path) if os.path.isfile(path) and not isinstance(
            src, images._ImageBase) else None
        if mp:
            try:
                src.load_rescue_map(mp)
            except Exception:
                pass
        self.members.append({'label': label, 'path': path, 'src': src, 'letters': set(letters)})
        self._refresh()

    def add_drive(self):
        i = self.pick.current()
        if i < 0:
            return
        d = self.choices[i]
        self._open(d['path'], self._drive_label(d), d.get('letters', ()))

    def add_images(self):
        ps = filedialog.askopenfilenames(parent=self.top, title='Member drive images (e.g. rescue images)',
                                         filetypes=[('Disk images', images.IMAGE_PATTERNS), ('All', '*.*')])
        for p in ps:
            self._open(p, os.path.basename(p))

    def add_missing(self):
        self.members.append(None)
        self._refresh()

    def move(self, step):
        sel = self.lst.curselection()
        if not sel:
            return
        i = sel[0]
        j = i + step
        if 0 <= j < len(self.members):
            self.members[i], self.members[j] = self.members[j], self.members[i]
            self._refresh()
            self.lst.selection_set(j)

    def remove(self):
        sel = self.lst.curselection()
        if sel:
            m = self.members.pop(sel[0])
            self._refresh()

    def _srcs(self):
        return [m['src'] for m in self.members if m]

    def _letters(self):
        out = set()
        for m in self.members:
            if m:
                out |= m.get('letters', set())
        return out

    # ---- finding settings
    def _set_results(self, results):
        self.results = results
        self.res.delete(0, 'end')
        for label, _f in results:
            self.res.insert('end', label)
        if results:
            self.res.selection_set(0)

    def find_md(self):
        srcs = self._srcs()
        if not srcs:
            messagebox.showinfo(APP, 'Add the member drives first.', parent=self.top)
            return
        self.msg.set('Reading RAID settings from the drives...')

        def work():
            out = []
            try:
                arrays = raid.discover(srcs)
                for fa in arrays:
                    try:
                        arr = raid.build(fa)
                    except ValueError as ex:
                        out.append((f'{fa.describe()}  -  cannot be rebuilt: {ex}', None))
                        continue
                    out.append((f'Array:  {fa.describe()}   ({human_size(arr.size)})', lambda a=arr: a))
                    for lv in raid.find_lvm([arr]):
                        out.append((f'     LVM volume on it:  {lv.label[11:]}   ({human_size(lv.size)})  '
                                    '<- usually the one to scan on Synology/QNAP', lambda l=lv: l))
                if len(arrays) > 1:
                    built = [r[1]() for r in out if r[1] and r[0].startswith('Array')]
                    for lv in raid.find_lvm(built):
                        if not any(lv.label in r[0] for r in out):
                            out.append((f'LVM volume across arrays:  {lv.label[11:]}   ({human_size(lv.size)})',
                                        lambda l=lv: l))
                for lv in raid.find_lvm(srcs):
                    out.append((f'LVM volume directly on the drives:  {lv.label[11:]}   ({human_size(lv.size)})',
                                lambda l=lv: l))
            except Exception as ex:
                self.q.put(('msg', f'Could not read the drives: {ex}'))
            self.q.put(('results', out, 'No Linux RAID settings found on these drives - try "Work out settings".'
                        if not out else f'Found {len(out)} item(s).'))

        threading.Thread(target=work, daemon=True).start()

    def work_out(self):
        mem = [m['src'] if m else None for m in self.members]
        if len(mem) < 2:
            messagebox.showinfo(APP, 'Add at least two member drives (or a "missing drive" placeholder).',
                                parent=self.top)
            return
        if len(mem) > 6:
            messagebox.showinfo(APP, 'Working out settings is limited to 6 drives - for more, enter the order '
                                     'and settings by hand.', parent=self.top)
            return
        try:
            off = int(float(self.v_offset.get() or 0) * 1024)
        except ValueError:
            off = 0
        self.cancel.clear()
        self.msg.set('Trying drive orders, stripe sizes and layouts...')

        def work():
            try:
                res = raid.detect(mem, levels=(5, 0, 1), progress=lambda f: self.q.put(('progress', f)),
                                  cancel=self.cancel, data_offset=off)
            except Exception as ex:
                self.q.put(('msg', f'Could not work it out: {ex}'))
                return
            out = []
            for sc, desc, st in res[:10]:
                def fac(st=st):
                    members = [(mem[i], off) if mem[i] is not None else None for i in st['order']]
                    return raid.VirtualRaid(members, st['level'], st['chunk'], st['layout'])
                out.append((f'score {sc:>5}:  {desc}', fac))
            self.q.put(('results', out, 'Best match first. The top result is usually right when its score is '
                        'clearly higher than the next.' if out else 'No combination made sense - check that every '
                        'member drive is added, and the "Data starts at" value.'))

        threading.Thread(target=work, daemon=True).start()

    def build_manual(self):
        mem = [(m['src'], 0) if m else None for m in self.members]
        if not mem:
            return
        try:
            off = int(float(self.v_offset.get() or 0) * 1024)
        except ValueError:
            off = 0
        mem = [(s, off) if m else None for m in mem for s in ([m[0]] if m else [None])]
        level = {'RAID 0': 0, 'RAID 1': 1, 'RAID 5': 5, 'RAID 6': 6, 'RAID 10': 10,
                 'Spanned (JBOD)': 'linear'}[self.v_level.get()]
        num, unit = self.v_chunk.get().split()
        chunk = int(num) * (1024 if unit == 'KB' else 1024 * 1024)
        try:
            arr = raid.VirtualRaid(mem, level, chunk, self.v_layout.get())
        except ValueError as ex:
            messagebox.showerror(APP, str(ex), parent=self.top)
            return
        out = [(f'Array (your settings): {arr.label}   ({human_size(arr.size)})', lambda a=arr: a)]
        for lv in raid.find_lvm([arr]):
            out.append((f'     LVM volume on it:  {lv.label[11:]}   ({human_size(lv.size)})', lambda l=lv: l))
        self._set_results(out)
        self.msg.set('Built. Pick it and press "Use selected".')

    def use(self):
        sel = self.res.curselection()
        if not sel or not self.results[sel[0]][1]:
            messagebox.showinfo(APP, 'Pick a result first.', parent=self.top)
            return
        src = self.results[sel[0]][1]()
        self.app.add_virtual(src, self._letters())
        self.top.destroy()

    def poll(self):
        if not self.top.winfo_exists():
            return
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == 'results':
                    self.pbar['value'] = 100
                    self._set_results(m[1])
                    self.msg.set(m[2])
                elif m[0] == 'progress':
                    self.pbar['value'] = m[1] * 100
                elif m[0] == 'msg':
                    self.msg.set(m[1])
        except queue.Empty:
            pass
        self.top.after(250, self.poll)

    def close(self):
        self.cancel.set()
        self.top.destroy()


class ChancesDialog:
    """Recoverability check: are the deleted files likely to still be there?"""

    COLORS = {'good': '#15803d', 'mixed': '#b45309', 'poor': '#b91c1c'}

    def __init__(self, app, d):
        self.app, self.d = app, d
        self.cancel = threading.Event()
        top = self.top = tk.Toplevel(app.root)
        top.title(f'{APP} - recoverability check')
        top.geometry('760x520')
        set_window_icon(top)
        frm = ttk.Frame(top, padding=12)
        frm.pack(fill='both', expand=True)
        ttk.Label(frm, text=d.get('label') or d.get('path'), font=('Segoe UI', 10, 'bold') if IS_WIN
                  else ('Helvetica', 10, 'bold')).pack(anchor='w')
        ttk.Label(frm, foreground='#555', wraplength=720, text=(
            'Looks at what kind of drive this is, checks whether a sample of its deleted files still has data, and '
            'samples the unused space - a few seconds, about 500 small reads. On a failing drive, make a rescue '
            'image first and check the image instead.')).pack(anchor='w', pady=(2, 8))
        self.pbar = ttk.Progressbar(frm, mode='determinate', maximum=1000)
        self.pbar.pack(fill='x')
        self.head = tk.StringVar(value='Checking...')
        self.lbl = ttk.Label(frm, textvariable=self.head, font=('Segoe UI', 12, 'bold') if IS_WIN
                             else ('Helvetica', 12, 'bold'))
        self.lbl.pack(anchor='w', pady=(10, 4))
        self.txt = tk.Text(frm, height=14, wrap='word', relief='flat', background=top.cget('background'),
                           font=('Segoe UI', 10) if IS_WIN else ('Helvetica', 10))
        self.txt.pack(fill='both', expand=True)
        b = ttk.Frame(frm)
        b.pack(fill='x')
        self.btn_scan = ttk.Button(b, text='Start scan', style='Big.TButton', state='disabled', command=self.scan)
        self.btn_scan.pack(side='right')
        ttk.Button(b, text='Close', command=self.close).pack(side='right', padx=6)
        top.protocol('WM_DELETE_WINDOW', self.close)
        threading.Thread(target=self.work, daemon=True).start()

    def _ui(self, fn, *a):
        self.app.q.put(('call', lambda: self.top.winfo_exists() and fn(*a)))

    def work(self):
        d = self.d
        try:
            src, own = self.app.open_source_for(d)
        except Exception as ex:
            self._ui(self.show, 'poor', 'Could not open the drive', str(ex))
            return
        try:
            disks = sorted(d.get('disks') or ([d['number']] if d.get('number') is not None else []))
            dev = recoverability.device_info(disks) if not d.get('image') and not d.get('source_obj') else {}
            self._ui(self.head.set, 'Checking deleted files...')
            dl = recoverability.sample_deleted(src, self.cancel)
            self._ui(self.head.set, 'Sampling the free space...')
            fs = recoverability.sample_free_space(src, lambda f: self._ui(self.pbar.__setitem__, 'value', 1000 * f),
                                                  self.cancel)
            if self.cancel.is_set():
                return
            level, head, text = recoverability.verdict(dev, fs, dl)
            self._ui(self.show, level, head, text)
        except Exception as ex:
            self._ui(self.show, 'poor', 'The check failed', str(ex))
        finally:
            if own:
                src.close()

    def show(self, level, head, text):
        self.pbar['value'] = 1000
        self.head.set(head)
        self.lbl.configure(foreground=self.COLORS.get(level, '#000'))
        self.txt.configure(state='normal')
        self.txt.delete('1.0', 'end')
        self.txt.insert('1.0', text)
        self.txt.configure(state='disabled')
        self.btn_scan.configure(state='normal')

    def scan(self):
        self.top.destroy()
        self.app._trim_warned = getattr(self.app, '_trim_warned', set()) | {self.d.get('path')}
        self.app.start_scan()

    def close(self):
        self.cancel.set()
        self.top.destroy()


class UnlockDialog:
    """Unlock a LUKS or VeraCrypt volume; the unlocked view is added to the drive list."""

    def __init__(self, app, d):
        self.app, self.d = app, d
        top = self.top = tk.Toplevel(app.root)
        top.title(f'{APP} - unlock encrypted drive')
        top.geometry('720x470')
        set_window_icon(top)
        frm = ttk.Frame(top, padding=12)
        frm.pack(fill='both', expand=True)
        ttk.Label(frm, text=d.get('label') or d.get('path'), font=('Segoe UI', 10, 'bold') if IS_WIN
                  else ('Helvetica', 10, 'bold')).pack(anchor='w')
        ttk.Label(frm, wraplength=680, foreground='#555', text=(
            'Opens a LUKS (Linux, many NAS boxes) or VeraCrypt volume with its password, read-only. The unlocked '
            'volume appears in the drive list and is scanned like any other drive. Nothing is written, and the '
            'password is not stored. Supported: AES (the default in both); VeraCrypt with SHA-512 or SHA-256. '
            'BitLocker is not supported - unlock it in Windows first, then scan the drive letter.')).pack(
            anchor='w', pady=(2, 8))
        try:
            src, self.own = app.open_source_for(d)
        except Exception as ex:
            ttk.Label(frm, foreground='#b91c1c', text=f'Could not open it: {ex}').pack(anchor='w')
            self.src = None
            return
        self.src = src
        self.places = []                       # (label, offset, size, kind)
        try:
            for off, info in encrypted.find_luks(src):
                self.places.append((f"LUKS{info['version']} volume at {human_size(off)} "
                                    f"({info['cipher']}-{info['mode']})", off, None, 'luks'))
        except Exception:
            pass
        self.places.append(('VeraCrypt: the whole drive / file', 0, src.size, 'vc'))
        try:
            for i, (off, size) in enumerate(engine._partition_table(src), 1):
                self.places.append((f'VeraCrypt: partition {i} at {human_size(off)} ({human_size(size)})', off,
                                    size, 'vc'))
        except Exception:
            pass
        ttk.Label(frm, text='What to unlock:').pack(anchor='w')
        self.v_place = tk.StringVar(value=self.places[0][0])
        ttk.Combobox(frm, textvariable=self.v_place, values=[p[0] for p in self.places], state='readonly',
                     width=80).pack(anchor='w', fill='x')
        pf = ttk.Frame(frm)
        pf.pack(fill='x', pady=(10, 0))
        ttk.Label(pf, text='Password:').grid(row=0, column=0, sticky='w')
        self.v_pw = tk.StringVar()
        self.ent = ttk.Entry(pf, textvariable=self.v_pw, show='\u2022', width=40)
        self.ent.grid(row=0, column=1, sticky='w', padx=6)
        self.v_show = tk.BooleanVar(value=False)
        ttk.Checkbutton(pf, text='Show', variable=self.v_show,
                        command=lambda: self.ent.configure(show='' if self.v_show.get() else '\u2022')).grid(
            row=0, column=2, sticky='w')
        ttk.Label(pf, text='VeraCrypt PIM (blank = default):').grid(row=1, column=0, sticky='w', pady=(6, 0))
        self.v_pim = tk.StringVar()
        ttk.Entry(pf, textvariable=self.v_pim, width=10).grid(row=1, column=1, sticky='w', padx=6, pady=(6, 0))
        self.v_hidden = tk.BooleanVar(value=False)
        ttk.Checkbutton(pf, text='Look for a hidden VeraCrypt volume first', variable=self.v_hidden).grid(
            row=2, column=1, columnspan=2, sticky='w', pady=(6, 0))
        self.msg = tk.StringVar()
        ttk.Label(frm, textvariable=self.msg, wraplength=680).pack(anchor='w', pady=(10, 0))
        b = ttk.Frame(frm)
        b.pack(fill='x', side='bottom')
        self.btn = ttk.Button(b, text='Unlock', style='Big.TButton', command=self.go)
        self.btn.pack(side='right')
        ttk.Button(b, text='Close', command=top.destroy).pack(side='right', padx=6)
        self.ent.focus_set()
        top.bind('<Return>', lambda e: self.go())

    def go(self):
        if self.src is None:
            return
        pw = self.v_pw.get()
        if not pw:
            self.msg.set('Type the password first.')
            return
        place = next(p for p in self.places if p[0] == self.v_place.get())
        try:
            pim = int(self.v_pim.get() or 0)
        except ValueError:
            self.msg.set('The PIM must be a number.')
            return
        self.btn.configure(state='disabled')
        self.msg.set('Checking the password (this takes a few seconds - it is designed to be slow)...')
        hidden = self.v_hidden.get()

        def work():
            try:
                label = f"Unlocked: {self.d.get('label') or self.d.get('path')}"
                if place[3] == 'luks':
                    dec = encrypted.unlock_luks(self.src, pw, base=place[1], label=label + ' (LUKS)',
                                                progress=lambda t: self.app.q.put(('call', self.msg.set, t)))
                else:
                    dec = encrypted.unlock_veracrypt(self.src, pw, base=place[1], size=place[2], pim=pim,
                                                     hidden=hidden, label=label + ' (VeraCrypt)',
                                                     progress=lambda t: self.app.q.put(('call', self.msg.set, t)))
                self.app.q.put(('call', self.done, dec, None))
            except Exception as ex:
                self.app.q.put(('call', self.done, None, ex))
        threading.Thread(target=work, daemon=True).start()

    def done(self, dec, err):
        if not self.top.winfo_exists():
            return
        self.btn.configure(state='normal')
        if err:
            self.msg.set(f'Not unlocked: {err}')
            return
        try:
            v, kind = engine.open_volume(dec)
        except Exception:
            kind = 'unknown'
        self.app.add_virtual(dec, self.d.get('letters', set()))
        self.top.destroy()
        messagebox.showinfo(APP, f'Unlocked ({dec.kind}). Inside: {kind}.\n\nIt is now selected in the drive list - '
                                 'press Start scan.')


class SearchDialog:
    """Search inside the data for words or phrases."""

    def __init__(self, app, d):
        self.app, self.d = app, d
        self.cancel = threading.Event()
        self.hits = []
        top = self.top = tk.Toplevel(app.root)
        top.title(f'{APP} - search inside the data')
        top.geometry('1040x640')
        set_window_icon(top)
        frm = ttk.Frame(top, padding=10)
        frm.pack(fill='both', expand=True)
        ttk.Label(frm, text=d.get('label') or d.get('path'), font=('Segoe UI', 10, 'bold') if IS_WIN
                  else ('Helvetica', 10, 'bold')).pack(anchor='w')
        ttk.Label(frm, wraplength=1000, foreground='#555', text=(
            'Finds words or phrases anywhere in the data - as plain text and as UTF-16 (how Windows and Office '
            'store much of their text). Separate several with commas. Hits inside found files show the file; '
            'hits in text nobody claims can be saved as a text file (text files have no signature, so this is '
            'how lost notes, lists and CSVs come back).')).pack(anchor='w', pady=(2, 6))
        f = ttk.Frame(frm)
        f.pack(fill='x')
        ttk.Label(f, text='Find:').pack(side='left')
        self.v_terms = tk.StringVar()
        e = ttk.Entry(f, textvariable=self.v_terms, width=50)
        e.pack(side='left', padx=6)
        e.focus_set()
        self.v_scope = tk.StringVar(value='Whole drive')
        ttk.Combobox(f, textvariable=self.v_scope, state='readonly', width=22,
                     values=['Whole drive', 'Free space only', 'Inside found files']).pack(side='left', padx=6)
        self.v_case = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text='Match case', variable=self.v_case).pack(side='left', padx=6)
        self.btn = ttk.Button(f, text='Search', command=self.go)
        self.btn.pack(side='left', padx=6)
        ttk.Button(f, text='Stop', command=self.cancel.set).pack(side='left')
        top.bind('<Return>', lambda ev: self.go())
        cols = ('off', 'term', 'text', 'file')
        tf = ttk.Frame(frm)
        tf.pack(fill='both', expand=True, pady=6)
        self.tree = ttk.Treeview(tf, columns=cols, show='headings', selectmode='browse')
        for c, t, w in (('off', 'Position', 120), ('term', 'Found', 120), ('text', 'Text around it', 520),
                        ('file', 'Inside file', 240)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor='w', stretch=(c == 'text'))
        sb = ttk.Scrollbar(tf, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        sb.pack(side='left', fill='y')
        self.tree.bind('<Double-1>', lambda ev: self.show_file())
        b = ttk.Frame(frm)
        b.pack(fill='x')
        self.pbar = ttk.Progressbar(b, mode='determinate', maximum=1000, length=260)
        self.pbar.pack(side='left')
        self.msg = tk.StringVar()
        ttk.Label(b, textvariable=self.msg).pack(side='left', padx=8)
        ttk.Button(b, text='Save the text around it...', command=self.save_text).pack(side='right')
        ttk.Button(b, text='Show the file', command=self.show_file).pack(side='right', padx=6)
        top.protocol('WM_DELETE_WINDOW', self.close)

    def close(self):
        self.cancel.set()
        self.top.destroy()

    def go(self):
        terms = [t.strip() for t in self.v_terms.get().split(',') if t.strip()]
        if not terms:
            return
        self.cancel.clear()
        self.hits = []
        self.tree.delete(*self.tree.get_children())
        scope = {'Whole drive': 'all', 'Free space only': 'free', 'Inside found files': 'files'}[self.v_scope.get()]
        items = [self.app.items[i] for i in self.app.order]
        if scope == 'files' and not items:
            self.msg.set('Scan first - there are no found files yet.')
            return
        self.btn.configure(state='disabled')
        self.msg.set('Searching...')
        same = self.app.src is not None and self.d.get('path') and getattr(self.app.src, 'path', None) == (
            self.d.get('source_obj').path if self.d.get('source_obj') else self.d.get('path'))
        index = search.FileIndex(items) if same else None
        case = self.v_case.get()

        def work():
            try:
                src, own = self.app.open_source_for(self.d)
            except Exception as ex:
                self.app.q.put(('call', self.finished, f'Could not open the drive: {ex}'))
                return
            batch = []
            n = 0
            try:
                for off, term, enc, ctx in search.search(
                        src, terms, scope, items, case, progress=lambda f: self.app.q.put(
                            ('call', self._progress, f)), cancel=self.cancel):
                    it = index.at(off) if index else None
                    batch.append((off, term, enc, ctx, it))
                    n += 1
                    if len(batch) >= 50:
                        self.app.q.put(('call', self._add, batch))
                        batch = []
                if batch:
                    self.app.q.put(('call', self._add, batch))
                note = f'{n:,} hit(s)' + (' - stopped' if self.cancel.is_set() else '') + \
                    (' (showing the first 5,000)' if n >= 5000 else '')
            except Exception as ex:
                note = f'Search failed: {ex}'
            finally:
                if own:
                    src.close()
            self.app.q.put(('call', self.finished, note))
        threading.Thread(target=work, daemon=True).start()

    def _progress(self, f):
        if self.top.winfo_exists():
            self.pbar['value'] = 1000 * f

    def _add(self, batch):
        if not self.top.winfo_exists():
            return
        for off, term, enc, ctx, it in batch:
            k = len(self.hits)
            self.hits.append((off, term, enc, ctx, it))
            self.tree.insert('', 'end', iid=str(k), values=(f'{off:,}', f'{term} ({enc})', ctx,
                                                             f'{it.folder}/{it.name}' if it else '(not in a found file)'))

    def finished(self, note):
        if self.top.winfo_exists():
            self.btn.configure(state='normal')
            self.pbar['value'] = 1000
            self.msg.set(note)

    def _sel(self):
        s = self.tree.selection()
        return self.hits[int(s[0])] if s else None

    def show_file(self):
        h = self._sel()
        if not h or not h[4]:
            self.msg.set('That hit is not inside a found file - use "Save the text around it".')
            return
        it = h[4]
        if self.app.tree.exists(str(it.id)):
            self.app.tree.selection_set(str(it.id))
            self.app.tree.see(str(it.id))
            self.app.root.lift()
        else:
            self.msg.set('That file is hidden by the current filters in the main list.')

    def save_text(self):
        h = self._sel()
        if not h:
            return
        off, term, enc, ctx, it = h
        try:
            src, own = self.app.open_source_for(self.d)
            start, data = search.text_block(src, off, enc)
            if own:
                src.close()
        except Exception as ex:
            messagebox.showerror(APP, f'Could not read it: {ex}', parent=self.top)
            return
        if not data:
            messagebox.showinfo(APP, 'No readable text around that hit.', parent=self.top)
            return
        path = filedialog.asksaveasfilename(parent=self.top, title='Save the text as',
                                            initialfile=f'text_at_{start}.txt', defaultextension='.txt',
                                            filetypes=[('Text', '*.txt'), ('All files', '*.*')])
        if not path:
            return
        if self.app._same_drive(path):
            messagebox.showwarning(APP, 'Save it to a different drive than the one being searched.', parent=self.top)
            return
        text = data.decode('utf-16-le' if enc == 'UTF-16' else 'utf-8', 'replace')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        self.msg.set(f'Saved {human_size(len(data))} of text to {path}')


class PortableDialog:
    """Put a portable FlashRescue on a USB stick (or any folder) to take to someone else's PC."""

    def __init__(self, app):
        self.app = app
        self.cancel = threading.Event()
        top = self.top = tk.Toplevel(app.root)
        top.title(f'{APP} - portable USB')
        top.geometry('760x500')
        set_window_icon(top)
        frm = ttk.Frame(top, padding=12)
        frm.pack(fill='both', expand=True)
        ttk.Label(frm, wraplength=720, justify='left', text=(
            'Makes a copy of FlashRescue that runs from a USB stick without installing. On the PC it runs on, '
            'it keeps everything it writes for itself (settings, temp files, ffmpeg) on the stick, and nothing is '
            'unpacked onto that PC. Recovered files and rescue images go wherever you choose - ideally an external '
            'drive, as most sticks are slow for large images.')).pack(anchor='w')
        if portable.is_portable():
            ttk.Label(frm, foreground='#555', wraplength=720, text=(
                f'This copy is already portable ({portable.app_dir()}). You can still copy it to another stick.')).pack(
                anchor='w', pady=(6, 0))

        tf = ttk.LabelFrame(frm, text=' Where ', padding=8)
        tf.pack(fill='x', pady=8)
        self.sticks = [d for d in app.drives if d.get('kind') == 'volume' and d.get('letter')
                       and not d.get('system')]
        self.sticks.sort(key=lambda d: (d.get('type') != 'Removable', d['letter']))
        names = [f"{d['letter']}:  {d.get('label') or ''}  ({human_size(d.get('size', 0))}"
                 f"{', USB stick / card' if d.get('type') == 'Removable' else ''})" for d in self.sticks]
        self.v_target = tk.StringVar()
        self.cb = ttk.Combobox(tf, values=names, state='readonly', width=60)
        self.cb.grid(row=0, column=0, sticky='we')
        self.cb.bind('<<ComboboxSelected>>', self._picked)
        ttk.Button(tf, text='Other folder...', command=self._browse).grid(row=0, column=1, padx=6)
        ttk.Label(tf, text='Folder:').grid(row=1, column=0, sticky='w', pady=(6, 0))
        ttk.Entry(tf, textvariable=self.v_target, width=70).grid(row=2, column=0, columnspan=2, sticky='we')
        tf.columnconfigure(0, weight=1)
        if self.sticks:
            self.cb.current(0)
            self._picked()

        self.v_ff = tk.BooleanVar(value=True)
        have = media.find_ffmpeg()[0]
        ttk.Checkbutton(frm, variable=self.v_ff, text=(
            'Include ffmpeg (video checking and repair work offline)' + ('' if have else
                                                                         ' - downloaded from gyan.dev, about 100 MB'))).pack(anchor='w')
        self.how = ('copy' if portable.is_folder_build() else 'download')
        ttk.Label(frm, foreground='#555', wraplength=720, text=(
            'The program files are copied from this installed copy.' if self.how == 'copy' else
            'This copy can\'t be copied as a portable one (it is a single-file or source version), so the portable '
            'version of the newest release is downloaded from GitHub and checked against its checksum.')).pack(
            anchor='w', pady=(6, 0))
        self.pbar = ttk.Progressbar(frm, mode='determinate', maximum=1000)
        self.pbar.pack(fill='x', pady=(12, 0))
        self.msg = tk.StringVar()
        ttk.Label(frm, textvariable=self.msg, wraplength=720).pack(anchor='w')
        b = ttk.Frame(frm)
        b.pack(fill='x', side='bottom')
        self.btn = ttk.Button(b, text='Make portable copy', style='Big.TButton', command=self.go)
        self.btn.pack(side='right')
        ttk.Button(b, text='Close', command=self.close).pack(side='right', padx=6)
        top.protocol('WM_DELETE_WINDOW', self.close)

    def _picked(self, _e=None):
        i = self.cb.current()
        if i >= 0:
            self.v_target.set(f"{self.sticks[i]['letter']}:\\FlashRescue")

    def _browse(self):
        p = filedialog.askdirectory(parent=self.top, title='Folder for the portable FlashRescue')
        if p:
            self.v_target.set(os.path.join(os.path.normpath(p), 'FlashRescue'))

    def close(self):
        self.cancel.set()
        self.top.destroy()

    def _ui(self, fn, *a):
        self.app.q.put(('call', lambda: self.top.winfo_exists() and fn(*a)))

    def go(self):
        target = self.v_target.get().strip()
        if not target:
            messagebox.showinfo(APP, 'Choose a USB stick or folder first.', parent=self.top)
            return
        target = os.path.abspath(target)
        if os.path.normcase(target) == os.path.normcase(portable.app_dir()):
            messagebox.showinfo(APP, 'That is where this copy is running from - pick another place.', parent=self.top)
            return
        drv = drive_letter_of(target)
        if drv and drv == portable.host_windows_letter() and not messagebox.askyesno(
                APP, f'{drv}: is this PC\'s Windows drive, not a USB stick. Make the portable copy there anyway?',
                parent=self.top):
            return
        try:
            os.makedirs(target, exist_ok=True)
            free = shutil.disk_usage(target).free
        except OSError as ex:
            messagebox.showerror(APP, f'Cannot use {target}:\n{ex}', parent=self.top)
            return
        need = (200 if self.v_ff.get() else 60) * 1024 * 1024
        if free < need:
            messagebox.showerror(APP, f'Not enough free space there ({human_size(free)} free, about '
                                      f'{human_size(need)} needed).', parent=self.top)
            return
        self.btn.configure(state='disabled')
        want_ff = self.v_ff.get()
        settings = dict(self.app.settings)

        def prog(label):
            return lambda d, t: self._ui(self._show, label, d, t)

        def work():
            try:
                tools = media.find_ffmpeg() if want_ff else None
                if self.how == 'copy':
                    portable.copy_app(target, tools if tools and tools[0] else None,
                                      prog('Copying FlashRescue'), settings)
                else:
                    self._ui(self.msg.set, 'Looking up the newest release...')
                    info = updater.latest(settings)
                    if not info or not info.get('portable'):
                        raise RuntimeError('the newest release on GitHub has no portable version to download')
                    z = updater.download(info['portable'], prog(f"Downloading FlashRescue {info['version']}"),
                                         self.cancel.is_set)
                    if not z:
                        return
                    portable.extract_zip(z, target, prog('Unpacking'))
                    os.remove(z)
                    portable.finish_stick(target, settings)
                    if tools and tools[0]:
                        os.makedirs(os.path.join(target, 'tools'), exist_ok=True)
                        for t in tools:
                            shutil.copyfile(t, os.path.join(target, 'tools', os.path.basename(t)))
                if want_ff and not (tools and tools[0]):
                    updater.download_ffmpeg(prog('Downloading ffmpeg'), self.cancel.is_set,
                                            out_dir=os.path.join(target, 'tools'))
                self._ui(self._done, target, None)
            except Exception as ex:
                self._ui(self._done, target, ex)
        threading.Thread(target=work, daemon=True).start()

    def _show(self, label, d, t):
        self.pbar['value'] = 1000 * d / t if t else 0
        self.msg.set(f'{label}... {d / 1e6:.0f} / {t / 1e6:.0f} MB')

    def _done(self, target, err):
        self.btn.configure(state='normal')
        if err:
            self.msg.set(f'Failed: {err}')
            return
        self.pbar['value'] = 1000
        self.msg.set(f'Ready: {os.path.join(target, "FlashRescue.exe")}  - run it from the stick on any Windows 10/11 '
                     'PC (it asks for administrator rights).')


class UpdatesInfo:
    """'You have the newest version' + the automatic-check setting."""

    def __init__(self, app):
        top = tk.Toplevel(app.root)
        top.title(f'{APP} - updates')
        top.resizable(False, False)
        set_window_icon(top)
        frm = ttk.Frame(top, padding=14)
        frm.pack(fill='both', expand=True)
        ttk.Label(frm, text=f'FlashRescue {VERSION} is the newest version.',
                  font=('Segoe UI', 10, 'bold') if IS_WIN else ('Helvetica', 10, 'bold')).pack(anchor='w')
        v = tk.BooleanVar(value=bool(app.settings.get('check_updates', True)))

        def changed():
            app.settings['check_updates'] = v.get()
            save_settings(app.settings)
        ttk.Checkbutton(frm, text='Check for new versions by itself (asks GitHub once a day - nothing about '
                                  'you, your drives or files is sent)', variable=v, command=changed).pack(
            anchor='w', pady=(8, 0))
        ttk.Label(frm, foreground='#666', text=f'Updates come from github.com/{updater.repo(app.settings)}').pack(
            anchor='w', pady=(4, 0))
        ttk.Button(frm, text='OK', command=top.destroy).pack(anchor='e', pady=(10, 0))


class UpdateDialog:
    """What's new + Download and install."""

    def __init__(self, app, info):
        self.app, self.info = app, info
        self.cancel = threading.Event()
        top = self.top = tk.Toplevel(app.root)
        top.title(f'{APP} - update available')
        top.geometry('640x520')
        set_window_icon(top)
        frm = ttk.Frame(top, padding=12)
        frm.pack(fill='both', expand=True)
        ttk.Label(frm, text=f"FlashRescue {info['version']} is out. You have {VERSION}.",
                  font=('Segoe UI', 11, 'bold') if IS_WIN else ('Helvetica', 11, 'bold')).pack(anchor='w')
        ttk.Label(frm, text="What's new:").pack(anchor='w', pady=(8, 2))
        txt = tk.Text(frm, height=14, wrap='word', relief='solid', borderwidth=1)
        import re as _re
        notes = _re.sub(r'(?m)^#+\s*', '', info.get('notes') or '').replace('**', '').strip()
        txt.insert('1.0', notes or '(no notes)')
        txt.configure(state='disabled')
        txt.pack(fill='both', expand=True)
        self.installed, self.built = updater.is_installed(), updater.is_built()
        self.portable = portable.is_portable() and portable.is_folder_build() and bool(info.get('portable'))
        extra = ''
        if self.portable:
            extra = (f'This is a portable copy ({portable.app_dir()}). The update replaces the program files here; '
                     'Data, tools and portable.txt stay as they are. FlashRescue closes and starts again.')
        elif portable.is_portable():
            extra = 'This portable copy can\'t update itself, so the download page opens instead.'
        elif self.built:
            extra = ('This copy was built with Setup.bat. The update installs FlashRescue in its usual place '
                     '(your settings stay as they are) with Start menu and desktop shortcuts - later updates then '
                     'install by themselves.')
        elif not self.installed:
            extra = 'This copy runs from the Python source files, so the download page opens instead.'
        if extra:
            ttk.Label(frm, text=extra, wraplength=600, foreground='#555').pack(anchor='w', pady=(6, 0))
        self.pbar = ttk.Progressbar(frm, mode='determinate', maximum=1000)
        self.pbar.pack(fill='x', pady=(8, 0))
        self.msg = tk.StringVar(value='')
        ttk.Label(frm, textvariable=self.msg).pack(anchor='w')
        b = ttk.Frame(frm)
        b.pack(fill='x', pady=(6, 0))
        self.can_install = self.portable or ((self.installed or self.built) and not portable.is_portable())
        self.btn_go = ttk.Button(b, style='Big.TButton', command=self.go,
                                 text=('Download and update this copy' if self.portable else
                                       'Download and install') if self.can_install else 'Open the download page')
        self.btn_go.pack(side='right')
        ttk.Button(b, text='Later', command=self.close).pack(side='right', padx=6)
        top.protocol('WM_DELETE_WINDOW', self.close)

    def close(self):
        self.cancel.set()
        self.top.destroy()

    def go(self):
        if not self.can_install:
            open_with_default_app(self.info['page'])
            self.close()
            return
        self.btn_go.configure(state='disabled')
        self.msg.set('Downloading...')

        def progress(done, total):
            self.app.q.put(('call', self._progress, done, total))

        def work():
            try:
                if self.portable:
                    new_dir = os.path.join(portable.app_dir(), '_update_new')
                    shutil.rmtree(new_dir, ignore_errors=True)
                    z = updater.download(self.info['portable'], progress, self.cancel.is_set,
                                         dest_dir=portable.temp_dir())
                    path = None
                    if z:
                        self.app.q.put(('call', self.msg.set, 'Unpacking...'))
                        portable.extract_zip(z, new_dir)
                        os.remove(z)
                        if not os.path.isfile(os.path.join(new_dir, 'FlashRescue.exe')):
                            raise RuntimeError('the portable download has no FlashRescue.exe')
                        path = new_dir
                else:
                    path = updater.download(self.info, progress, self.cancel.is_set)
                err = None
            except Exception as ex:
                path, err = None, ex
            self.app.q.put(('call', self._downloaded, path, err))
        threading.Thread(target=work, daemon=True).start()

    def _progress(self, done, total):
        if self.top.winfo_exists():
            self.pbar['value'] = 1000 * done / total if total else 0
            self.msg.set(f"Downloading FlashRescue {self.info['version']}... {done / 1e6:.1f} / {total / 1e6:.1f} MB")

    def _downloaded(self, path, err):
        if not self.top.winfo_exists() or (path is None and err is None):
            return
        if err:
            self.btn_go.configure(state='normal')
            self.msg.set(f'Download failed: {err}')
            return
        self.msg.set('Downloaded and checked.')
        while True:
            why = self.app.busy_reason()
            if not why:
                break
            if not messagebox.askretrycancel(APP, f'The update is ready, but {why}. Let it finish (or stop it), '
                                                  'then press Retry.', parent=self.top):
                self.btn_go.configure(state='normal')
                return
        if not messagebox.askyesno(APP, f"FlashRescue {self.info['version']} is downloaded and checked. FlashRescue "
                                        'will close, update and start again (your settings stay as they are). '
                                        'Install now?', parent=self.top):
            self.btn_go.configure(state='normal')
            return
        if self.portable:
            import subprocess
            script = portable.swap_script(path, os.getpid())
            subprocess.Popen(['cmd', '/c', script], creationflags=0x00000008 | 0x00000200 | 0x08000000,
                             close_fds=True)
        else:
            updater.run_installer(path)
        self.app.root.destroy()


def selftest(out_path):
    """Used by the release build on GitHub: proves the built .exe can start its scanner helper
    process and find a file. Writes a short report; exit code 0 = fine."""
    import tempfile
    lines = []
    code = 1
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.new('RGB', (64, 48), (200, 30, 30)).save(buf, 'JPEG')
        jpg = buf.getvalue()
        d = tempfile.mkdtemp(prefix='flashrescue_selftest_')
        img = os.path.join(d, 'test.img')
        with open(img, 'wb') as f:
            f.write(bytes(1 << 20) + jpg + bytes((4 << 20) - len(jpg)))
        src = engine.RawSource(img)
        found = []
        sc = worker.ProcessScanner(src, engine.ScanOptions(True, True, False, False), found.append,
                                   lambda s: None, lambda *a: None)
        summary = sc.run()
        ok = any(it.ext == 'jpg' and it.read_head(src, len(jpg)) == jpg for it in found)
        lines.append(f'version {VERSION}')
        lines.append(f'helper process scan: {summary}')
        lines.append(f'found the test JPEG intact: {ok}')
        lines.append(f'icon bundled: {os.path.exists(resource_path("icon.ico"))}')
        src.close()
        code = 0 if ok and os.path.exists(resource_path('icon.ico')) else 2
        # the libraries the encryption / compression support needs must be inside the build
        try:
            import zstandard
            from argon2.low_level import hash_secret_raw, Type
            import lzo
            key = bytes(range(64))
            cip = encrypted.SectorCipher('aes', 'xts-plain64', key)
            blob = os.urandom(4096)
            libs_ok = (cip.decrypt(cip.encrypt(blob, 7), 7) == blob and
                       zstandard.ZstdDecompressor().decompressobj().decompress(
                           zstandard.ZstdCompressor().compress(blob)) == blob and
                       len(hash_secret_raw(b'pw', b'saltsaltsalt', time_cost=1, memory_cost=1024, parallelism=1,
                                           hash_len=32, type=Type.ID)) == 32 and
                       lzo.lzo1x_decompress(bytes([17 + 3]) + b'abc' + bytes([0x11, 0, 0])) == b'abc')
        except Exception as ex:
            libs_ok = f'no ({ex})'
        lines.append(f'encryption / compression libraries work: {libs_ok}')
        if libs_ok is not True:
            code = 4
        if portable.is_portable():
            here = os.path.normcase(portable.app_dir())
            inside = all(os.path.normcase(os.path.abspath(p)).startswith(here) for p in
                         (tempfile.gettempdir(), d, _settings_path(), _last_scan_path(), updater.ffmpeg_dir()))
            lines.append(f'portable: temp={tempfile.gettempdir()} settings={_settings_path()} '
                         f'ffmpeg={updater.ffmpeg_dir()} -> all on the stick: {inside}')
            if not inside:
                code = 3
    except Exception as ex:
        import traceback
        lines.append(traceback.format_exc())
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return code


def main():
    portable.setup()                      # portable: settings/temp on the stick, before anything else
    if len(sys.argv) > 2 and sys.argv[1] == '--selftest':
        sys.exit(selftest(sys.argv[2]))
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        try:
            import ctypes
            # the same app id the installer gives the shortcuts, so the taskbar shows FlashRescue's
            # icon at once - also on the first start after an update
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('FlashRescue.App')
        except Exception:
            pass
    root = tk.Tk()
    if IS_WIN and not is_admin():
        root.withdraw()
        if messagebox.askyesno(APP, 'FlashRescue needs administrator rights to read drives directly.\n\n'
                                    'Restart as administrator now?'):
            if relaunch_as_admin():
                root.destroy()
                return
        root.deiconify()
    app = App(root)
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):   # FlashRescue.exe "drive.img"
        app.add_image(os.path.abspath(sys.argv[1]))
    root.mainloop()


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()      # the .exe also starts the scanner helper process
    sys.setswitchinterval(0.002)          # hand the window the CPU more often while threads work
    main()
