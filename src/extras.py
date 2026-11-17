"""
FlashRescue extras:
  * NTFS recovery after a quick format (rebuilding the old file table from leftover records)
  * Lost / deleted partition finder (leftover boot sectors and their backups)
  * Recycle Bin ($I / $R) mapping back to original names, folders and deletion dates
  * Thumbnail cache extraction (thumbcache_*.db, iconcache_*.db, Thumbs.db)
  * Date-taken naming for deep-scan photos and videos (EXIF, QuickTime mvhd)
  * Saving / loading scan results
  * Duplicate detection
"""

import os
import re
import json
import base64
import bisect
import struct
import hashlib
import datetime

import engine
from engine import (u16, u32, u64, FoundFile, human_size, ntfs_fixup, ntfs_attrs, ntfs_resident_value,
                    ntfs_runlist, filetime, head_matches_ext, NtfsVolume, ExfatVolume, FatVolume, MB, KB)

MASK48 = 0xFFFFFFFFFFFF


# ======================================================================================
# Small helpers
# ======================================================================================

class BytesSource:
    """Lets the carvers run over a file's contents held in memory."""

    def __init__(self, data):
        self.data = data
        self.size = len(data)

    def read(self, off, n):
        return self.data[off:off + n]


def slice_runs(runs, start, length):
    """The (offset, length) runs covering bytes [start, start+length) of a file stored in `runs`."""
    out = []
    pos = 0
    end = start + length
    for off, ln in runs:
        a, b = max(start, pos), min(end, pos + ln)
        if b > a:
            out.append((None if off is None else off + (a - pos), b - a))
        pos += ln
        if pos >= end:
            break
    return out


def content_condition(src, item):
    """Same checks the filesystem scans use: zero-filled or wrong magic number."""
    engine._VolumeBase.check_condition(None, src, item)


def allocation_condition(volumes, runs, size):
    """Is this byte range in use by a *current* filesystem? -> condition word."""
    used = total = 0
    first_used = None
    for off, ln in runs:
        if off is None:
            continue
        for v in volumes:
            vs = getattr(v, 'vol_off', 0)
            ve = vs + (getattr(v, 'size_bytes', 0) or 0)
            if vs <= off < ve:
                u = v.bytes_in_use(off, ln)
                if first_used is None:
                    first_used = v.bytes_in_use(off, 1) > 0
                used += u
                break
        total += ln
    if used == 0:
        return 'Good'
    if first_used and used >= total * 0.9:
        return 'Overwritten'
    return 'Partially overwritten'


# ======================================================================================
# 1. NTFS after a quick format: rebuild the old file table from leftover FILE records
# ======================================================================================

FILE_RE = re.compile(rb'FILE0\x00[\x03\x09]\x00')


def data_from_attrs(datas):
    """{size, inline, runs, flags} from a list of unnamed $DATA attributes."""
    if not datas:
        return None
    res = [a for a in datas if a[8] == 0]
    if res:
        v = ntfs_resident_value(res[0])
        return {'size': len(v), 'inline': v, 'runs': None, 'flags': u16(res[0], 12)}
    by_vcn = {}
    for a in datas:
        by_vcn.setdefault(u64(a, 16), a)
    parts = [by_vcn[k] for k in sorted(by_vcn)]
    if u64(parts[0], 16) != 0:
        return None
    runs = []
    for a in parts:
        runs += ntfs_runlist(a)
    return {'size': u64(parts[0], 48), 'inline': None, 'runs': runs, 'flags': u16(parts[0], 12)}


class MftCollector:
    """Fed every NTFS FILE record the deep scan comes across (outside current file tables)."""

    def __init__(self, src, exclude_ranges):
        self.src = src
        ex = sorted(exclude_ranges)
        self.ex_starts = [a for a, b in ex]
        self.ex_ends = [b for a, b in ex]
        self.groups = {}        # group key -> {recno: info}
        self.ext = {}           # (group, base recno) -> [attrs]
        self.count = 0

    def drop_ranges(self, ranges):
        """Forget records that belong to a file table we can read properly (e.g. a lost partition)."""
        if not ranges:
            return
        rs = sorted(ranges)
        st = [a for a, b in rs]
        for key in list(self.groups):
            g = self.groups[key]
            for rn in list(g):
                off = g[rn]['off']
                i = bisect.bisect_right(st, off) - 1
                if i >= 0 and off < rs[i][1]:
                    del g[rn]
            if not g:
                del self.groups[key]

    def excluded(self, off):
        i = bisect.bisect_right(self.ex_starts, off) - 1
        return i >= 0 and off < self.ex_ends[i]

    def add(self, off, raw):
        if self.excluded(off) or len(raw) < 1024:
            return
        rs = u32(raw, 0x1C)
        if rs not in (1024, 4096) or len(raw) < rs:
            return
        rec, intact = ntfs_fixup(raw[:rs])
        recno = u32(rec, 0x2C)
        if recno < 24 and recno != 5:
            return
        group = off - recno * rs
        base = u64(rec, 32) & MASK48
        attrs = list(ntfs_attrs(rec))
        if base:
            self.ext.setdefault((group, base), []).extend(attrs)
            return
        flags = u16(rec, 22)
        g = self.groups.setdefault(group, {})
        if recno in g:
            return
        info = NtfsVolume._names(attrs)
        datas = [a for t, a in attrs if t == 0x80 and a[9] == 0]
        g[recno] = {
            'rs': rs, 'off': off, 'seq': u16(rec, 16), 'in_use': bool(flags & 1), 'is_dir': bool(flags & 2),
            'names': info, 'datas': datas, 'has_alist': any(t == 0x20 for t, _ in attrs), 'intact': intact,
        }
        self.count += 1

    # ------------------------------------------------------------------ geometry
    def _samples(self, recs, limit=40):
        out = []
        for r in recs.values():
            if r['is_dir'] or not r['names'] or not r['datas']:
                continue
            d = data_from_attrs(r['datas'])
            if not d or d['inline'] is not None or not d['runs'] or d['runs'][0][0] is None:
                continue
            ext = os.path.splitext(r['names'][0])[1].lower().lstrip('.')
            if head_matches_ext(ext, b'') is None:
                continue
            out.append((ext, d['runs'][0][0]))
            if len(out) >= limit:
                break
        return out

    def _geometry(self, recs, bases):
        samples = self._samples(recs)
        if not samples:
            return None, 0, 0
        best, best_score = None, 0
        for cs in (4096, 8192, 16384, 32768, 65536, 2048, 1024, 512):
            for base in bases:
                score = 0
                for ext, lcn in samples:
                    off = base + lcn * cs
                    if 0 <= off < self.src.size and head_matches_ext(ext, self.src.read(off, 16)):
                        score += 1
                if score > best_score:
                    best, best_score = (base, cs), score
            if best_score >= 0.8 * len(samples):
                break
        if best_score >= max(2, 0.25 * len(samples)):
            return best, best_score, len(samples)
        return None, best_score, len(samples)

    # ------------------------------------------------------------------ results
    def finish(self, bases, current_volumes, known_starts, emit, status=None):
        if not self.groups:
            return 0
        if status:
            status(f'Rebuilding file tables from before a format ({self.count:,} old file records)...')
        # groups that are pieces of one old file table share geometry -> merge them
        sets = {}
        for key, recs in self.groups.items():
            if len(recs) < 2:
                continue
            geo, score, n = self._geometry(recs, bases)
            sets.setdefault(geo, []).append((key, recs))
        found = 0
        multi = len([g for g in sets if g is not None]) > 1
        for k, (geo, members) in enumerate(sorted(sets.items(), key=lambda kv: (kv[0] is None, kv[0] or (0, 0)))):
            recs = {}
            exts = {}
            for key, g in members:
                for rn, r in g.items():
                    recs.setdefault(rn, r)
                for (gk, base), attrs in self.ext.items():
                    if gk == key:
                        exts.setdefault(base, []).extend(attrs)
            dirs = {}
            for rn, r in recs.items():
                if r['is_dir'] and r['names']:
                    name, parent, pseq, _ = r['names']
                    dirs[rn] = (name, parent, pseq, r['seq'], r['in_use'])
            resolve = folder_resolver(dirs)
            label = '/(Before format)' if not multi else f'/(Before format {k + 1})'
            for rn, r in recs.items():
                if r['is_dir']:
                    continue
                datas = list(r['datas'])
                if rn in exts:
                    datas += [a for t, a in exts[rn] if t == 0x80 and a[9] == 0]
                d = data_from_attrs(datas)
                if not d or d['size'] <= 0:
                    continue
                if r['names']:
                    name, parent, pseq, mtime = r['names']
                    folder = label + ('' if resolve(parent, pseq) == '/' else resolve(parent, pseq))
                else:
                    if rn in exts:
                        nm = NtfsVolume._names(exts[rn])
                    else:
                        nm = None
                    if nm:
                        name, parent, pseq, mtime = nm
                        folder = label + ('' if resolve(parent, pseq) == '/' else resolve(parent, pseq))
                    else:
                        name, mtime, folder = None, None, label + '/(name lost)'
                if d['inline'] is not None:
                    runs = []
                elif geo is None:
                    continue                      # can't place the data without the old geometry
                else:
                    base, cs = geo
                    runs = []
                    left = d['size']
                    for lcn, cnt in d['runs']:
                        if left <= 0:
                            break
                        n = min(cnt * cs, left)
                        runs.append((None if lcn is None else base + lcn * cs, n))
                        left -= n
                    if left > 0:
                        runs.append((None, left))
                    if runs and runs[0][0] is not None and runs[0][0] in known_starts:
                        continue                  # already found by the normal scan
                if name is None:
                    head = d['inline'][:16] if d['inline'] is not None else (
                        self.src.read(runs[0][0], 16) if runs and runs[0][0] is not None else b'')
                    name = f'File record {rn}.{engine.guess_ext(head) or "bin"}'
                if d['flags'] & 0x0001:
                    cond = 'Compressed (not supported)'
                elif d['flags'] & 0x4000:
                    cond = 'Encrypted (not supported)'
                elif d['inline'] is not None:
                    cond = 'Good'
                else:
                    cond = allocation_condition(current_volumes, runs, d['size'])
                item = FoundFile(name, folder, d['size'], runs, 'Before format', cond, mtime,
                                 'was in the file table before the drive was formatted'
                                 + ('' if r['in_use'] else ' (already deleted then)'))
                if d['inline'] is not None:
                    item.inline = d['inline']
                elif cond == 'Good':
                    content_condition(self.src, item)
                emit(item)
                found += 1
        return found


def folder_resolver(dirs, root=5):
    cache = {}

    def folder_of(parent, pseq):
        key = (parent, pseq)
        if key in cache:
            return cache[key]
        parts = []
        cur, exp = parent, pseq
        lost = False
        for _ in range(128):
            if cur == root:
                break
            d = dirs.get(cur)
            if d is None:
                lost = True
                break
            dname, dpar, dps, dseq, d_in_use = d
            if dseq != exp and not (not d_in_use and dseq == ((exp + 1) & 0xFFFF)) and dseq != ((exp + 1) & 0xFFFF):
                lost = True
                break
            parts.append(dname)
            cur, exp = dpar, dps
        path = '/'.join(reversed(parts))
        path = ('/(lost folder)/' + path if lost else '/' + path).rstrip('/') or '/'
        cache[key] = path
        return path

    return folder_of


# ======================================================================================
# 2. Lost / deleted partitions
# ======================================================================================

class BootCollector:
    """Fed every sector that looks like a filesystem boot sector (primary or backup)."""

    def __init__(self):
        self.cands = {}          # start -> boot sector bytes
        self.linux = {}          # start -> ('ext', superblock bytes) / ('btrfs', mirror offset)

    def add_linux(self, start, kind, extra):
        self.linux.setdefault(start, (kind, extra))

    def add(self, off, bs):
        if len(bs) < 512 or bs[510:512] != b'\x55\xaa':
            return
        bps = u16(bs, 11) if bs[3:11] != b'EXFAT   ' else (1 << bs[108] if 9 <= bs[108] <= 12 else 0)
        if bps not in (512, 1024, 2048, 4096):
            return
        if bs[3:11] == b'NTFS    ':
            self.cands.setdefault(off, bs)
            tot = u64(bs, 40)
            if tot and off - tot * bps >= 0:
                self.cands.setdefault(off - tot * bps, bs)        # this may be the backup copy
        elif bs[3:11] == b'EXFAT   ':
            self.cands.setdefault(off, bs)
            if off - 12 * bps >= 0:
                self.cands.setdefault(off - 12 * bps, bs)         # backup boot region
        elif bs[82:90] == b'FAT32   ':
            self.cands.setdefault(off, bs)
            bk = u16(bs, 50)
            if bk and off - bk * bps >= 0:
                self.cands.setdefault(off - bk * bps, bs)
        elif bs[54:59] in (b'FAT16', b'FAT12'):
            self.cands.setdefault(off, bs)

    def volumes(self, src, current):
        """Readable filesystems at candidate starts that aren't one of the current ones."""
        cur = [(p.offset, p.offset + p.size) for p in current]
        out = []
        taken = []
        for start in sorted(self.cands):
            if any(a <= start < b for a, b in cur) or any(a <= start < b for a, b in taken):
                continue
            bs = self.cands[start]
            v = None
            for cls in (ExfatVolume, NtfsVolume, FatVolume):
                if cls.probe(bs):
                    try:
                        v = cls(src, start, bs)
                    except Exception:
                        v = None
                    break
            if not v:
                continue
            size = getattr(v, 'size_bytes', 0) or 0
            if size <= 0 or start + size > src.size + 1 * MB:
                continue
            # a real filesystem has a readable root folder
            try:
                ok = self._sane(v)
            except Exception:
                ok = False
            if not ok:
                continue
            taken.append((start, start + size))
            out.append(v)
        import linuxfs
        for start in sorted(self.linux):
            if any(a <= start < b for a, b in cur) or any(a <= start < b for a, b in taken):
                continue
            kind, extra = self.linux[start]
            v = None
            try:
                if kind == 'btrfs':
                    v = linuxfs.BtrfsVolume(src, start, sb_offset=extra)
                else:
                    v = linuxfs.Ext4Volume(src, start, sb_override=extra)
            except Exception:
                v = None
            if not v or v.size_bytes <= 0 or start + v.size_bytes > src.size + 1 * MB:
                continue
            taken.append((start, start + v.size_bytes))
            out.append(v)
        return out

    @staticmethod
    def _sane(v):
        if isinstance(v, NtfsVolume):
            return v.read_record(5)[:4] == b'FILE'
        if isinstance(v, ExfatVolume):
            return any(True for _ in engine.parse_exfat_dir(v._root[:4096])) or v._root[:1] in (b'\x83', b'\x81')
        return len(v.root_data()) > 0


def walk_lost_partition(v, index, emit, cancel, status=None):
    label = f'/(Lost partition {index} - {v.kind}, {human_size(v.size_bytes)})'
    n = [0]

    def em(item):
        item.folder = label + ('' if item.folder == '/' else item.folder)
        if item.method == 'Existing':
            item.method = 'Lost partition'
            item.condition = 'Good'
            item.note = (item.note + '; ' if item.note else '') + 'file was on a deleted or lost partition'
            content_condition(v.src, item)
        n[0] += 1
        emit(item)

    if status:
        status(f'Reading lost partition {index} ({v.kind}, {human_size(v.size_bytes)})...')
    v.walk(em, True, cancel, status)
    return n[0]


# ======================================================================================
# 3. Recycle Bin
# ======================================================================================

is_special_name = engine.is_special_name


def in_recycle_bin(folder):
    f = folder.lower()
    return '/$recycle.bin' in f or '/recycler' in f


def parse_recycle_i(data):
    """$I file -> (original path, original size, deletion time) or None."""
    if len(data) < 24:
        return None
    ver = u64(data, 0)
    size = u64(data, 8)
    when = filetime(u64(data, 16))
    if ver == 1 and len(data) >= 24 + 520:
        path = data[24:24 + 520].decode('utf-16-le', 'replace').split('\x00')[0]
    elif ver == 2 and len(data) >= 28:
        n = u32(data, 24)
        path = data[28:28 + 2 * n].decode('utf-16-le', 'replace').split('\x00')[0]
    else:
        return None
    if not path or len(path) < 3:
        return None
    return path, size, when


def _win_path_to_folder(path):
    p = path.replace('\\', '/')
    if len(p) > 1 and p[1] == ':':
        p = p[0].upper() + p[2:]
    p = p.strip('/')
    folder, _, name = p.rpartition('/')
    return '/(Recycle Bin)/' + folder if folder else '/(Recycle Bin)', name


def map_recycle_bin(src, items):
    """Turn $I/$R pairs (and contents of $R folders) into items with original names and folders.
    Returns the list of items to show."""
    infos = {}          # (folder, id) -> (orig folder, orig name, when)
    for it in items:
        if it.name.lower().startswith('$i') and it.size < 64 * KB:
            try:
                data = it.read_head(src, 64 * KB)
            except Exception:
                continue
            parsed = parse_recycle_i(data)
            if parsed:
                path, size, when = parsed
                folder, name = _win_path_to_folder(path)
                infos[(it.folder.lower(), it.name[2:].lower())] = (folder, name, when)
    out = []
    for it in items:
        lname = it.name.lower()
        if lname.startswith('$i') and (it.folder.lower(), lname[2:]) in infos:
            continue                                  # metadata only, folded into the $R item
        mapped = None
        if lname.startswith('$r'):
            mapped = infos.get((it.folder.lower(), lname[2:]))
            if mapped:
                folder, name, when = mapped
                it.name, it.folder = name, folder
        else:
            # inside a recycled folder: .../$Recycle.Bin/<SID>/$RXXXX/sub/file
            parts = it.folder.split('/')
            for k in range(len(parts) - 1, 0, -1):
                if parts[k].lower().startswith('$r'):
                    key = ('/'.join(parts[:k]).lower(), parts[k][2:].lower())
                    if key in infos:
                        folder, name, when = infos[key]
                        mapped = infos[key]
                        it.folder = folder + '/' + name + ('/' + '/'.join(parts[k + 1:]) if parts[k + 1:] else '')
                    break
        if mapped:
            when = mapped[2]
            it.note = (it.note + '; ' if it.note else '') + (
                f'moved to the Recycle Bin on {when:%Y-%m-%d %H:%M}' if when else 'was in the Recycle Bin')
            if it.method == 'Existing':
                it.method = 'Recycle Bin'
                it.condition = 'Good'
            it.ext = os.path.splitext(it.name)[1].lower().lstrip('.')
            it.category = engine.category_of(it.ext)
        out.append(it)
    return out


# ======================================================================================
# 4. Thumbnail caches
# ======================================================================================

_THUMB_RE = re.compile(rb'\xff\xd8\xff[\xdb\xe0-\xef\xfe\xc0\xc4]|\x89PNG\r\n\x1a\n|BM[\x00-\xff]{4}\x00\x00\x00\x00')


def image_dims(head, ext):
    try:
        if ext == 'png' and head[12:16] == b'IHDR':
            return struct.unpack('>II', head[16:24])
        if ext == 'bmp':
            w, h = struct.unpack_from('<ii', head, 18)
            return abs(w), abs(h)
        if ext == 'jpg':
            p = 2
            while p + 9 < len(head):
                if head[p] != 0xFF:
                    return None
                m = head[p + 1]
                if m in (0xC0, 0xC1, 0xC2):
                    h, w = struct.unpack_from('>HH', head, p + 5)
                    return w, h
                p += 2 + struct.unpack_from('>H', head, p + 2)[0]
    except struct.error:
        pass
    return None


def extract_thumbnails(src, item, max_bytes=512 * MB):
    """Pictures stored inside a Windows thumbnail cache file -> new items pointing into it."""
    if item.inline is not None or item.size > max_bytes:
        return []
    data = item.read_head(src, item.size)
    bsrc = BytesSource(data)
    out = []
    pos = 0
    n = 0
    db = item.name
    while True:
        m = _THUMB_RE.search(data, pos)
        if not m:
            break
        off = m.start()
        sig = data[off:off + 2]
        fn = engine.carve_jpeg if sig == b'\xff\xd8' else (engine.carve_png if sig == b'\x89P' else engine.carve_bmp)
        try:
            res = fn(engine._Reader(bsrc, off, min(len(data) - off, 32 * MB)))
        except (struct.error, IndexError, ValueError, KeyError):
            res = None
        if not res or res[0] < 64:
            pos = off + 2
            continue
        ln, ext = res[0], res[1]
        dims = image_dims(data[off:off + 64 * KB], ext)
        n += 1
        size_txt = f' {dims[0]}x{dims[1]}' if dims else ''
        sub = FoundFile(f'{os.path.splitext(db)[0]} {n:05d}{size_txt}.{ext}', f'/(Thumbnails){item.folder.rstrip("/")}/{db}',
                        ln, slice_runs(item.runs, off, ln), 'Thumbnail', 'Good', item.mtime,
                        'small copy kept in a Windows thumbnail cache')
        sub.unreadable = src.unreadable_in(sub.runs)
        out.append(sub)
        pos = off + ln
    return out


# ======================================================================================
# 5. Date taken (EXIF / QuickTime) for deep-scan files
# ======================================================================================

def parse_exif_tiff(t):
    """(datetime taken, camera model) from a TIFF/EXIF block."""
    if len(t) < 8 or t[:2] not in (b'II', b'MM'):
        return None, None
    e = '<' if t[:2] == b'II' else '>'
    try:
        def ifd(o):
            n = struct.unpack_from(e + 'H', t, o)[0]
            if n > 500:
                return {}
            tags = {}
            for k in range(n):
                tag, typ, cnt, val = struct.unpack_from(e + 'HHII', t, o + 2 + 12 * k)
                if typ == 2:
                    raw = t[o + 2 + 12 * k + 8: o + 2 + 12 * k + 8 + cnt] if cnt <= 4 else t[val:val + cnt]
                    tags[tag] = raw.split(b'\x00')[0].decode('ascii', 'replace').strip()
                elif typ in (3, 4):
                    tags[tag] = val if typ == 4 else (val & 0xFFFF if e == '<' else val >> 16)
            return tags
        ifd0 = ifd(struct.unpack_from(e + 'I', t, 4)[0])
        model = ifd0.get(0x0110) or None
        when = ifd0.get(0x0132)
        if 0x8769 in ifd0:
            ex = ifd(ifd0[0x8769])
            when = ex.get(0x9003) or ex.get(0x9004) or when
        dt = None
        if when:
            try:
                dt = datetime.datetime.strptime(when[:19], '%Y:%m:%d %H:%M:%S')
            except ValueError:
                dt = None
        return dt, model
    except (struct.error, IndexError):
        return None, None


def media_date(src, off, length, ext):
    """When was this photo/video taken? -> (datetime or None, camera model or None)"""
    try:
        if ext in ('jpg', 'heic', 'avif'):
            head = src.read(off, min(length, 256 * KB))
            i = head.find(b'Exif\x00\x00')
            if i >= 0:
                return parse_exif_tiff(head[i + 6:])
        elif ext in ('tif', 'cr2', 'nef', 'arw', 'dng', 'pef'):
            return parse_exif_tiff(src.read(off, min(length, 1 * MB)))
        elif ext in ('mp4', 'mov', 'm4v', '3gp', 'cr3'):
            pos = 0
            for _ in range(64):
                h = src.read(off + pos, 16)
                if len(h) < 8:
                    break
                size, typ = struct.unpack('>I4s', h[:8])
                if size == 1:
                    size = struct.unpack('>Q', h[8:16])[0]
                if size < 8:
                    break
                if typ == b'moov':
                    moov = src.read(off + pos, min(size, 64 * KB))
                    j = moov.find(b'mvhd')
                    if j >= 4:
                        ver = moov[j + 4]
                        secs = struct.unpack_from('>Q' if ver == 1 else '>I', moov, j + 8)[0]
                        if secs > 0:
                            dt = datetime.datetime(1904, 1, 1) + datetime.timedelta(seconds=secs)
                            return (dt if 2000 <= dt.year <= datetime.datetime.now().year + 1 else None), None
                    break
                pos += size
                if pos >= length:
                    break
    except Exception:
        pass
    return None, None


def name_by_date(src, item):
    dt, model = media_date(src, item.start, item.size, item.ext)
    if dt and 1995 <= dt.year <= datetime.datetime.now().year + 1:
        item.name = f'{dt:%Y-%m-%d %H.%M.%S}.{item.ext}'
        item.mtime = dt
        extra = f'taken {dt:%Y-%m-%d %H:%M:%S}' + (f' with {model}' if model else '')
        item.note = (item.note + '; ' if item.note else '') + extra
        return True
    return False


# ======================================================================================
# 6. Saving / loading scan results
# ======================================================================================

RESULTS_VERSION = 1


def save_results(path, src, items, extra=None):
    data = {
        'app': 'FlashRescue', 'version': RESULTS_VERSION,
        'saved': datetime.datetime.now().isoformat(timespec='seconds'),
        'source': {'path': src.path, 'label': src.label, 'size': src.size, 'map': src.map_path},
        'bad': list(zip(src.bad_starts, src.bad_ends)),
        'extra': extra or {},
        'items': [],
    }
    for it in items:
        data['items'].append({
            'name': it.name, 'folder': it.folder, 'size': it.size, 'runs': it.runs, 'method': it.method,
            'condition': it.condition, 'mtime': it.mtime.isoformat() if it.mtime else None, 'note': it.note,
            'inline': base64.b64encode(it.inline).decode('ascii') if it.inline is not None else None,
            'unreadable': it.unreadable, 'saved': it.saved_path, 'check': getattr(it, 'check', None),
        })
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    os.replace(tmp, path)


def load_results(path):
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if data.get('app') != 'FlashRescue':
        raise ValueError('not a FlashRescue results file')
    items = []
    for d in data['items']:
        it = FoundFile(d['name'], d['folder'], d['size'], [tuple(r) for r in d['runs']], d['method'],
                       d['condition'], datetime.datetime.fromisoformat(d['mtime']) if d['mtime'] else None,
                       d.get('note') or '')
        if d.get('inline') is not None:
            it.inline = base64.b64decode(d['inline'])
        it.unreadable = d.get('unreadable') or 0
        it.saved_path = d.get('saved')
        it.check = d.get('check')
        items.append(it)
    return data, items


# ======================================================================================
# 7. Duplicates
# ======================================================================================

def quick_hash(src, item):
    """Size + first 64 KB + last 64 KB: cheap and reliable enough to spot copies."""
    h = hashlib.sha1(str(item.size).encode())
    h.update(item.read_head(src, 64 * KB))
    if item.size > 128 * KB:
        tail = 64 * KB
        if item.inline is not None:
            h.update(item.inline[-tail:])
        else:
            runs = slice_runs(item.runs, item.size - tail, tail)
            for off, ln in runs:
                h.update(b'\x00' * ln if off is None else src.read(off, ln))
    return h.hexdigest()


def find_duplicates(src, items, progress=None, cancel=None):
    """Marks later copies with .dup_of = id of the best copy. Returns number of duplicates."""
    rank = {'Good': 0}
    groups = {}
    by_size = {}
    for it in items:
        if it.size > 0:
            by_size.setdefault(it.size, []).append(it)
    cands = [g for g in by_size.values() if len(g) > 1]
    total = sum(len(g) for g in cands) or 1
    done = 0
    dups = 0
    for it in items:
        it.dup_of = None
    for g in cands:
        for it in g:
            if cancel and cancel.is_set():
                return dups
            try:
                key = quick_hash(src, it)
            except Exception:
                key = None
            done += 1
            if progress and done % 20 == 0:
                progress(done / total)
            if key:
                groups.setdefault(key, []).append(it)
    for g in groups.values():
        if len(g) < 2:
            continue
        g.sort(key=lambda i: (rank.get(i.condition, 1), i.unreadable, i.method != 'Deleted', i.id))
        for other in g[1:]:
            other.dup_of = g[0].id
            dups += 1
    return dups
