"""
FlashRescue recovery engine.

Reads a drive (or a disk image) strictly read-only and finds deleted files two ways:

1. Filesystem scan  - reads FAT12/16/32 and exFAT directories and picks up entries that
                      were marked deleted. Keeps original names, folders and dates.
2. Deep scan        - "file carving": looks for known file signatures at sector
                      boundaries and works out where each file ends from its own
                      internal structure. Works after a quick format or on a damaged
                      filesystem, but names/folders are lost and fragmented files
                      can come out broken.

Nothing in this module ever writes to the source.
"""

import os
import re
import struct
import threading
import time
import queue
import zlib
import array
import datetime

KB = 1024
MB = 1024 * KB
GB = 1024 * MB

# --------------------------------------------------------------------------------------
# little helpers
# --------------------------------------------------------------------------------------

def u8(b, o):
    return b[o]

def u16(b, o):
    return struct.unpack_from('<H', b, o)[0]

def u32(b, o):
    return struct.unpack_from('<I', b, o)[0]

def u64(b, o):
    return struct.unpack_from('<Q', b, o)[0]

def be16(b, o):
    return struct.unpack_from('>H', b, o)[0]

def be32(b, o):
    return struct.unpack_from('>I', b, o)[0]

def be64(b, o):
    return struct.unpack_from('>Q', b, o)[0]


def dos_datetime(date, time):
    try:
        if date == 0:
            return None
        return datetime.datetime(((date >> 9) & 0x7F) + 1980, (date >> 5) & 0x0F, date & 0x1F,
                                 time >> 11, (time >> 5) & 0x3F, min((time & 0x1F) * 2, 59))
    except ValueError:
        return None


def human_size(n):
    if n is None:
        return ''
    for unit in ('bytes', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n} {unit}' if unit == 'bytes' else f'{n:.1f} {unit}'
        n /= 1024.0


CATEGORIES = {
    'Pictures': {'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'tif', 'tiff', 'heic', 'heif', 'avif',
                 'cr2', 'cr3', 'nef', 'arw', 'dng', 'pef', 'raf', 'orf', 'rw2', 'psd', 'ico', 'svg'},
    'Videos': {'mp4', 'mov', 'avi', 'mkv', 'webm', 'm4v', '3gp', 'wmv', 'mts', 'm2ts', 'mpg', 'mpeg', 'flv'},
    'Audio': {'mp3', 'wav', 'm4a', 'flac', 'ogg', 'aac', 'wma', 'opus'},
    'Documents': {'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'odt', 'ods', 'odp', 'txt', 'rtf',
                  'csv', 'epub', 'msg', 'md', 'html', 'htm', 'xml', 'json'},
    'Archives': {'zip', '7z', 'rar', 'gz', 'tar', 'apk', 'jar', 'iso'},
}


def category_of(ext):
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return 'Other'


# Magic numbers used to sanity-check that a deleted entry's data still belongs to it.
EXT_SIGNATURES = {
    'jpg': [b'\xff\xd8\xff'], 'jpeg': [b'\xff\xd8\xff'],
    'png': [b'\x89PNG'], 'gif': [b'GIF8'], 'bmp': [b'BM'],
    'pdf': [b'%PDF'],
    'zip': [b'PK'], 'docx': [b'PK'], 'xlsx': [b'PK'], 'pptx': [b'PK'], 'odt': [b'PK'],
    'ods': [b'PK'], 'odp': [b'PK'], 'apk': [b'PK'], 'jar': [b'PK'], 'epub': [b'PK'],
    'doc': [b'\xd0\xcf\x11\xe0'], 'xls': [b'\xd0\xcf\x11\xe0'], 'ppt': [b'\xd0\xcf\x11\xe0'],
    'msg': [b'\xd0\xcf\x11\xe0'],
    '7z': [b'7z\xbc\xaf'], 'rar': [b'Rar!'],
    'wav': [b'RIFF'], 'avi': [b'RIFF'], 'webp': [b'RIFF'],
    'mp3': [b'ID3', b'\xff\xfb', b'\xff\xf3', b'\xff\xf2', b'\xff\xfa'],
    'mkv': [b'\x1a\x45\xdf\xa3'], 'webm': [b'\x1a\x45\xdf\xa3'],
    'tif': [b'II*\x00', b'MM\x00*'], 'tiff': [b'II*\x00', b'MM\x00*'],
    'cr2': [b'II*\x00'], 'nef': [b'MM\x00*', b'II*\x00'], 'arw': [b'II*\x00'], 'dng': [b'II*\x00', b'MM\x00*'],
    'flac': [b'fLaC'], 'ogg': [b'OggS'], 'opus': [b'OggS'],
    'gz': [b'\x1f\x8b'], 'psd': [b'8BPS'], 'rtf': [b'{\\rtf'],
}
TEXT_LIKE = {'txt', 'csv', 'log', 'md', 'xml', 'json', 'html', 'htm', 'ini', 'rtf', 'svg'}
FTYP_EXTS = {'mp4', 'mov', 'm4a', 'm4v', '3gp', 'heic', 'heif', 'avif', 'cr3'}


def is_special_name(name):
    """Files worth picking up even when not deleted: Recycle Bin entries and thumbnail caches."""
    n = name.lower()
    return (len(n) > 2 and n[0] == '$' and n[1] in 'ir') or (n.startswith('thumbcache_') and n.endswith('.db')) \
        or (n.startswith('iconcache_') and n.endswith('.db')) or n == 'thumbs.db'


def guess_ext(head):
    """Best-guess extension from a file's first bytes."""
    if len(head) >= 12 and head[4:8] == b'ftyp':
        return {b'qt  ': 'mov', b'heic': 'heic', b'mif1': 'heic', b'M4A ': 'm4a'}.get(head[8:12], 'mp4')
    if head.startswith(b'RIFF') and len(head) >= 12:
        return {b'WAVE': 'wav', b'AVI ': 'avi', b'WEBP': 'webp'}.get(head[8:12])
    for ext in ('jpg', 'png', 'gif', 'pdf', 'zip', 'doc', '7z', 'rar', 'mp3', 'mkv', 'tif', 'flac',
                'ogg', 'gz', 'psd', 'rtf', 'bmp'):
        if any(head.startswith(sig) for sig in EXT_SIGNATURES[ext]):
            return ext
    return None


def head_matches_ext(ext, head):
    """True/False if we know the ext's magic number, None if we don't."""
    if ext in FTYP_EXTS:
        return len(head) >= 8 and head[4:8] == b'ftyp'
    sigs = EXT_SIGNATURES.get(ext)
    if not sigs:
        return None
    return any(head.startswith(s) for s in sigs)


# --------------------------------------------------------------------------------------
# Raw source (drive or image file)
# --------------------------------------------------------------------------------------

IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
IOCTL_DISK_GET_DRIVE_GEOMETRY = 0x00070000
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400


def win_ioctl(f, code, inbuf=None, outsize=1024):
    """DeviceIoControl on an open file object. Returns the output bytes, or None."""
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        handle = msvcrt.get_osfhandle(f.fileno())
        out = ctypes.create_string_buffer(outsize)
        returned = wintypes.DWORD(0)
        if inbuf is not None:
            ib = ctypes.create_string_buffer(inbuf, len(inbuf))
            ok = ctypes.windll.kernel32.DeviceIoControl(
                wintypes.HANDLE(handle), wintypes.DWORD(code), ib, len(inbuf),
                out, outsize, ctypes.byref(returned), None)
        else:
            ok = ctypes.windll.kernel32.DeviceIoControl(
                wintypes.HANDLE(handle), wintypes.DWORD(code), None, 0,
                out, outsize, ctypes.byref(returned), None)
        if ok:
            return out.raw[:returned.value]
    except Exception:
        pass
    return None


def _win_volume_length(f):
    """Size in bytes of a raw Windows volume/disk handle."""
    out = win_ioctl(f, IOCTL_DISK_GET_LENGTH_INFO, outsize=8)
    return struct.unpack('<q', out[:8])[0] if out and len(out) >= 8 else 0


class RawSource:
    """Read-only, sector-aligned random access to a drive or image file.
    Unreadable sectors are returned as zeros so a failing drive doesn't stop a scan."""

    ALIGN = 4096  # works for both 512-byte and 4K-sector devices

    def __init__(self, path, label=None):
        self.path = path
        self.label = label or path
        self.is_device = path.startswith('\\\\.\\') or path.startswith('\\\\?\\')
        self.f = open(path, 'rb', buffering=0)
        self.lock = threading.Lock()
        self.f_fg = None                # second read-only handle for everything except the scan
        self.lock_fg = threading.Lock()
        self.fg_failed = False
        self.bg_threads = set()         # threads that read through the main handle (the scanner)
        self.bad_reads = 0
        self.bad_starts = []            # sorted, merged unreadable ranges: starts...
        self.bad_ends = []              # ...and matching ends
        self.map_path = None
        self.ra = {}                    # read-ahead cache: chunk start -> bytes (filled by the deep scan)
        self.ra_lock = threading.Lock()
        self.size = 0
        if self.is_device and os.name == 'nt':
            self.size = _win_volume_length(self.f)
        if not self.size:
            try:
                self.f.seek(0, 2)
                self.size = self.f.tell()
            except OSError:
                self.size = 0
        self.sector_size = 512           # logical sector size; 4096 on "4K native" drives
        if self.is_device and os.name == 'nt':
            g = win_ioctl(self.f, IOCTL_DISK_GET_DRIVE_GEOMETRY, outsize=24)
            if g and len(g) >= 24 and u32(g, 20) in (512, 1024, 2048, 4096):
                self.sector_size = u32(g, 20)
        self.ALIGN = max(4096, self.sector_size)

    def close(self):
        for fh in (self.f, self.f_fg):
            try:
                if fh:
                    fh.close()
            except Exception:
                pass

    def add_bad(self, start, length):
        """Remember an unreadable range (merged with neighbours)."""
        import bisect
        end = start + length
        i = bisect.bisect_left(self.bad_ends, start)
        j = bisect.bisect_right(self.bad_starts, end)
        if i < j:
            start = min(start, self.bad_starts[i])
            end = max(end, self.bad_ends[j - 1])
        self.bad_starts[i:j] = [start]
        self.bad_ends[i:j] = [end]

    def load_rescue_map(self, map_path):
        """Treat everything a rescue didn't copy as unreadable."""
        import rescue
        mb = rescue.Mapbook.load(map_path, self.size)
        for a, b in mb.bad_ranges():
            self.add_bad(a, b - a)
        self.map_path = map_path
        return mb

    def unreadable_in(self, runs):
        """How many bytes of these (offset, length) runs fall in unreadable ranges."""
        if not self.bad_starts:
            return 0
        import bisect
        total = 0
        for off, length in runs:
            if off is None:
                continue
            end = off + length
            i = max(0, bisect.bisect_right(self.bad_starts, off) - 1)
            while i < len(self.bad_starts) and self.bad_starts[i] < end:
                a, b = max(off, self.bad_starts[i]), min(end, self.bad_ends[i])
                if b > a:
                    total += b - a
                i += 1
        return total

    def _raw(self, start, length, fh=None):
        """Aligned read; falls back to block-by-block with zero fill on errors."""
        fh = fh or self.f
        try:
            fh.seek(start)
            data = fh.read(length)
            if data is not None and len(data) == length:
                return data
        except OSError:
            data = None
        out = bytearray()
        pos = start
        end = start + length
        blk = self.ALIGN
        while pos < end:
            try:
                fh.seek(pos)
                d = fh.read(blk) or b''
            except OSError:
                d = b''
                self.bad_reads += 1
                self.add_bad(pos, blk)
            if len(d) < blk:
                d = d + b'\x00' * (blk - len(d))
            out += d
            pos += blk
        return bytes(out[:length])

    def ra_hit(self, offset, length):
        """Serve a read from chunks the deep scan has already read ahead, so checking a file that
        starts in the current chunk doesn't send the drive head elsewhere and back."""
        if not self.ra:
            return None
        with self.ra_lock:
            for st, d in self.ra.items():
                if st <= offset and offset + length <= st + len(d):
                    return d[offset - st: offset - st + length]
        return None

    def read(self, offset, length):
        if self.size and offset >= self.size:
            return b''
        if self.size:
            length = min(length, self.size - offset)
        if length <= 0:
            return b''
        hit = self.ra_hit(offset, length) if self.ra else None
        if hit is not None:
            return hit
        a = self.ALIGN
        start = offset - (offset % a)
        end = offset + length
        end_al = -(-end // a) * a
        if threading.get_ident() in self.bg_threads or self.fg_failed:
            with self.lock:
                data = self._raw(start, end_al - start)
        else:
            # Everything that isn't the scan itself (previews, opening, recovering, checking) reads
            # through its own handle, so it never waits behind the scanner - on Windows a busy
            # thread can otherwise grab a lock back over and over and starve everyone else.
            with self.lock_fg:
                if self.f_fg is None:
                    try:
                        self.f_fg = open(self.path, 'rb', buffering=0)
                    except OSError:
                        self.fg_failed = True
                if self.f_fg is not None:
                    data = self._raw(start, end_al - start, self.f_fg)
            if self.fg_failed:
                with self.lock:
                    data = self._raw(start, end_al - start)
        return data[offset - start: offset - start + length]


# --------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------

class FoundFile:
    _next_id = 0
    _id_lock = threading.Lock()

    def __init__(self, name, folder, size, runs, method, condition, mtime=None, note=''):
        with FoundFile._id_lock:
            FoundFile._next_id += 1
            self.id = FoundFile._next_id
        self.name = name
        self.folder = folder            # e.g. "/DCIM/100CANON" or "Deep scan/JPG"
        self.size = size
        self.runs = runs                # list of (device_offset, length)
        self.method = method            # 'Deleted', 'Deep scan', 'Existing'
        self.condition = condition      # 'Good', 'Fragmented (best guess)', 'Overwritten', ...
        self.mtime = mtime
        self.note = note
        self.saved_path = None          # set once auto-recover has saved it
        self.inline = None              # bytes, for data kept inside an NTFS MFT record
        self.unreadable = 0             # bytes of this file sitting on unreadable sectors
        self.special = False            # picked up only for Recycle Bin / thumbnail processing
        self.check = None               # result of "Check files"
        self.dup_of = None              # id of the better copy, if this is a duplicate
        self.ext = os.path.splitext(name)[1].lower().lstrip('.')
        self.category = category_of(self.ext)

    @property
    def start(self):
        for off, _ in self.runs:
            if off is not None:
                return off
        return None

    def iter_data(self, src, chunk=4 * MB, limit=None):
        left = self.size if limit is None else min(limit, self.size)
        if self.inline is not None:            # tiny NTFS file stored inside its MFT record
            yield self.inline[:left]
            return
        for off, length in self.runs:
            p = 0
            while p < length and left > 0:
                n = min(chunk, length - p, left)
                if off is None:                # sparse region: reads back as zeros
                    yield b'\x00' * n
                    p += n
                    left -= n
                    continue
                d = src.read(off + p, n)
                if not d:
                    return
                yield d
                p += n
                left -= n

    def read_head(self, src, n):
        return b''.join(self.iter_data(src, limit=n))


def runs_from_clusters(cluster_iter, cluster_offset, cluster_size, size):
    """Merge a sequence of cluster numbers into (offset, length) runs covering `size` bytes."""
    runs = []
    left = size
    for c in cluster_iter:
        if left <= 0:
            break
        off = cluster_offset(c)
        n = min(cluster_size, left)
        if runs and runs[-1][0] + runs[-1][1] == off:
            runs[-1] = (runs[-1][0], runs[-1][1] + n)
        else:
            runs.append((off, n))
        left -= n
    return runs


_ZERO_RUN = re.compile(rb'\x00+')
_NOT_FULL = re.compile(rb'[^\xff]+')


def bitmap_free_runs(bm, nbits):
    """Runs of clear bits (0 = free, least-significant bit first) as (first_bit, count).
    Whole free/used bytes are skipped in C, so multi-terabyte bitmaps stay fast."""
    runs = []

    def add(a, n):
        if runs and runs[-1][0] + runs[-1][1] == a:
            runs[-1] = (runs[-1][0], runs[-1][1] + n)
        else:
            runs.append((a, n))

    def mixed(i):
        b = bm[i]
        for k in range(8):
            if not (b >> k) & 1:
                add(i * 8 + k, 1)

    for m in _NOT_FULL.finditer(bm):
        a, b = m.span()
        p = a
        for z in _ZERO_RUN.finditer(bm, a, b):
            za, zb = z.span()
            for i in range(p, za):
                mixed(i)
            add(za * 8, (zb - za) * 8)
            p = zb
        for i in range(p, b):
            mixed(i)
    out = []
    for a, n in runs:
        if a >= nbits:
            break
        out.append((a, min(n, nbits - a)))
    return out


def bitmap_count_used(bm, start, count):
    """Number of set (in-use) bits in [start, start+count). Bits past the end count as used."""
    if count <= 0:
        return 0
    end = start + count
    used = 0
    i = start

    def bit(i):
        byte = i >> 3
        return (bm[byte] >> (i & 7)) & 1 if 0 <= byte < len(bm) else 1

    while i < end and i & 7:
        used += bit(i)
        i += 1
    a, b = i >> 3, end >> 3
    if b > a:
        seg = bm[a:b]
        used += ((b - a) - len(seg)) * 8
        for p in range(0, len(seg), 65536):
            piece = seg[p:p + 65536]
            z = piece.count(0)
            if z == len(piece):
                continue
            f = piece.count(255)
            if z + f == len(piece):
                used += f * 8
            else:
                used += bin(int.from_bytes(piece, 'little')).count('1')
        i = b << 3
    while i < end:
        used += bit(i)
        i += 1
    return used


class _BitmapAlloc:
    """Allocation tracked by a bitmap (exFAT, NTFS). Needs .bitmap, .bit_base, .nbits."""

    bit_base = 0

    def is_free(self, c):
        i = c - self.bit_base
        byte = i >> 3
        if i < 0 or byte >= len(self.bitmap):
            return False
        return not (self.bitmap[byte] >> (i & 7)) & 1

    def _range_free(self, a, b):
        return bitmap_count_used(self.bitmap, a - self.bit_base, b - a + 1) == 0

    def free_runs(self):
        cs = self.cluster_size
        return [(self.cluster_offset(a + self.bit_base), n * cs) for a, n in bitmap_free_runs(self.bitmap, self.nbits)]


_HEAD_DEFER = threading.local()


class _VolumeBase:
    """Shared logic for working out where a deleted file's data probably is."""

    kind = '?'

    def is_free(self, c):
        raise NotImplementedError

    def carve_base(self):
        return self.cluster_offset(2)

    def cluster_index(self, off):
        return 2 + (off - self.cluster_offset(2)) // self.cluster_size

    def bytes_in_use(self, off, length):
        """Roughly how many bytes of [off, off+length) the current filesystem is using."""
        cs = self.cluster_size
        c0 = self.cluster_index(off)
        c1 = self.cluster_index(off + max(length, 1) - 1)
        if c0 < 2 or c1 > self.max_cluster:
            return length
        if hasattr(self, 'bitmap'):
            return bitmap_count_used(self.bitmap, c0 - self.bit_base, c1 - c0 + 1) * cs
        used = 0
        for c in range(c0, min(c1, c0 + (1 << 20)) + 1):
            if not self.is_free(c):
                used += cs
        return min(used, length)

    def cluster_offset(self, c):
        raise NotImplementedError

    def plan_deleted(self, first, size, known_chain=None):
        """Return (runs, condition) for a deleted file starting at cluster `first`."""
        cs = self.cluster_size
        if size <= 0 or first < 2 or first > self.max_cluster:
            return None, None
        need = -(-size // cs)
        if known_chain and len(known_chain) >= need and all(self.is_free(c) for c in known_chain[:need]):
            return runs_from_clusters(known_chain[:need], self.cluster_offset, cs, size), 'Good'
        if not self.is_free(first):
            last = min(first + need - 1, self.max_cluster)
            return runs_from_clusters(range(first, last + 1), self.cluster_offset, cs, size), 'Overwritten'
        last = first + need - 1
        if last <= self.max_cluster and self._range_free(first, last):
            return runs_from_clusters(range(first, last + 1), self.cluster_offset, cs, size), 'Good'

        # Something else now sits in the middle: skip the clusters in use and take the
        # next free ones (what FAT drivers typically do when they write a file).
        def gen():
            c = first
            got = 0
            while got < need and c <= self.max_cluster:
                if self.is_free(c):
                    yield c
                    got += 1
                c += 1
        runs = runs_from_clusters(gen(), self.cluster_offset, cs, size)
        if sum(l for _, l in runs) < size:
            return runs, 'Partially overwritten'
        return runs, 'Fragmented (best guess)'

    def _range_free(self, a, b):
        for c in range(a, b + 1):
            if not self.is_free(c):
                return False
        return True

    def free_runs(self):
        """Unallocated space as (offset, length) byte ranges."""
        runs = []
        start = None
        c = 2
        cs = self.cluster_size
        for c in range(2, self.max_cluster + 1):
            if self.is_free(c):
                if start is None:
                    start = c
            elif start is not None:
                runs.append((self.cluster_offset(start), (c - start) * cs))
                start = None
        if start is not None:
            runs.append((self.cluster_offset(start), (self.max_cluster + 1 - start) * cs))
        return runs

    def check_condition(self, src, item, head=None):
        """Downgrade 'Good' to 'Possibly overwritten' if the data no longer starts with the
        magic number its extension promises."""
        if item.condition in ('Good', 'Fragmented (best guess)') and item.start is not None:
            q = getattr(_HEAD_DEFER, 'q', None)
            if head is None and q is not None:
                # During a scan these small reads are collected and done later in disk order, so a
                # drive with thousands of deleted files isn't sent seeking back and forth.
                item._head = (src, item.start)
                return
            if head is None:
                head = src.read(item.start, 64)
            known = head_matches_ext(item.ext, b'') is not None or item.ext in TEXT_LIKE
            if known and len(head) >= 16 and not head.strip(b'\x00') and item.size >= 64:
                item.condition = 'Erased (all zeros)'
                return
            if item.condition == 'Good' and head_matches_ext(item.ext, head[:16]) is False:
                item.condition = 'Possibly overwritten'


# --------------------------------------------------------------------------------------
# FAT12 / FAT16 / FAT32
# --------------------------------------------------------------------------------------

def _lfn_checksum(name11):
    s = 0
    for ch in name11:
        s = (((s & 1) << 7) + (s >> 1) + ch) & 0xFF
    return s


class _FatEntry:
    __slots__ = ('name', 'deleted', 'is_dir', 'cluster', 'size', 'mtime', 'raw_first')


def parse_fat_dir(data):
    """Yield every file/folder entry (live and deleted) in a FAT directory buffer."""
    lfn = []          # list of (chars, checksum, seq_byte) in physical order
    for i in range(0, len(data) - 31, 32):
        e = data[i:i + 32]
        first = e[0]
        if first == 0x00:
            break
        attr = e[11]
        if (attr & 0x3F) == 0x0F:   # long-file-name piece
            if first != 0xE5 and (first & 0x40):
                lfn = []
            chars = e[1:11] + e[14:26] + e[28:32]
            lfn.append((chars, e[13], first))
            continue
        pieces = lfn
        lfn = []
        if attr & 0x08:            # volume label
            continue
        deleted = first == 0xE5
        raw11 = bytearray(e[0:11])
        if raw11[0] == 0x05:
            raw11[0] = 0xE5
        base = bytes(raw11[0:8]).rstrip(b' ')
        ext = bytes(raw11[8:11]).rstrip(b' ')
        if base in (b'.', b'..'):
            continue
        # sanity: short name bytes must be printable-ish
        if any(c < 0x20 and c != 0x05 for c in e[0:11]):
            continue

        long_name = None
        if pieces:
            cks = pieces[0][1]
            if all(p[1] == cks for p in pieces):
                valid = False
                if deleted:
                    rest = bytes(e[1:11])
                    valid = any(_lfn_checksum(bytes([b]) + rest) == cks for b in range(0x20, 0x100))
                else:
                    valid = _lfn_checksum(bytes(e[0:11])) == cks
                if valid:
                    raw = b''.join(p[0] for p in reversed(pieces))
                    s = raw.decode('utf-16-le', 'replace')
                    s = s.split('\x00')[0].replace('￿', '')
                    if s.strip():
                        long_name = s

        if long_name:
            name = long_name
        else:
            nt = e[12]
            b = base.decode('cp437', 'replace')
            x = ext.decode('cp437', 'replace')
            if nt & 0x08:
                b = b.lower()
            if nt & 0x10:
                x = x.lower()
            if deleted:
                b = '_' + b[1:]
            name = b + ('.' + x if x else '')

        ent = _FatEntry()
        ent.name = name
        ent.deleted = deleted
        ent.is_dir = bool(attr & 0x10)
        ent.cluster = (u16(e, 20) << 16) | u16(e, 26)
        ent.size = u32(e, 28)
        ent.mtime = dos_datetime(u16(e, 24), u16(e, 22))
        ent.raw_first = first
        yield ent


class FatVolume(_VolumeBase):

    @staticmethod
    def probe(bs):
        if len(bs) < 512:
            return False
        bps = u16(bs, 11)
        spc = bs[13]
        if bps not in (512, 1024, 2048, 4096) or spc == 0 or (spc & (spc - 1)):
            return False
        if u16(bs, 14) == 0 or bs[16] == 0:
            return False
        if bs[0] not in (0xEB, 0xE9) and bs[510:512] != b'\x55\xaa':
            return False
        fatsz = u16(bs, 22) or u32(bs, 36)
        tot = u16(bs, 19) or u32(bs, 32)
        return fatsz > 0 and tot > 0 and bs[3:11] not in (b'NTFS    ', b'EXFAT   ')

    def __init__(self, src, vol_off, bs):
        self.src = src
        self.vol_off = vol_off
        self.bps = u16(bs, 11)
        self.spc = bs[13]
        self.rsvd = u16(bs, 14)
        self.nfats = bs[16]
        self.root_ents = u16(bs, 17)
        tot = u16(bs, 19) or u32(bs, 32)
        self.size_bytes = tot * self.bps
        self.fatsz = u16(bs, 22) or u32(bs, 36)
        root_dir_secs = (self.root_ents * 32 + self.bps - 1) // self.bps
        self.first_data_sec = self.rsvd + self.nfats * self.fatsz + root_dir_secs
        clusters = (tot - self.first_data_sec) // self.spc
        if u16(bs, 22) == 0:          # FAT32 BPB (some formatters make small FAT32 volumes)
            self.bits = 32
        elif clusters < 4085:
            self.bits = 12
        elif clusters < 65525:
            self.bits = 16
        else:
            self.bits = 32
        self.kind = f'FAT{self.bits}'
        self.cluster_size = self.bps * self.spc
        self.max_cluster = clusters + 1
        self.root_cluster = u32(bs, 44) if self.bits == 32 else 0
        self.label = bs[71:82] if self.bits == 32 else bs[43:54]
        fat_off = vol_off + self.rsvd * self.bps
        self.fat = src.read(fat_off, self.fatsz * self.bps)
        self.eoc = {12: 0xFF8, 16: 0xFFF8, 32: 0x0FFFFFF8}[self.bits]
        # keep the cluster count honest if the FAT is smaller than claimed
        entries = (len(self.fat) * 8) // self.bits
        self.max_cluster = min(self.max_cluster, entries - 1)
        if self.bits == 32:
            self._fat32 = array.array('I')
            self._fat32.frombytes(self.fat[:len(self.fat) // 4 * 4])
        elif self.bits == 16:
            self._fat16 = array.array('H')
            self._fat16.frombytes(self.fat[:len(self.fat) // 2 * 2])

    def fat_entry(self, c):
        if self.bits == 32:
            return self._fat32[c] & 0x0FFFFFFF
        if self.bits == 16:
            return self._fat16[c]
        off = c + c // 2
        v = u16(self.fat, off)
        return (v >> 4) if (c & 1) else (v & 0xFFF)

    def is_free(self, c):
        return self.fat_entry(c) == 0

    def cluster_offset(self, c):
        return self.vol_off + (self.first_data_sec + (c - 2) * self.spc) * self.bps

    def chain(self, c, max_clusters=1 << 22):
        out = []
        seen = set()
        while 2 <= c <= self.max_cluster and c not in seen and len(out) < max_clusters:
            out.append(c)
            seen.add(c)
            c = self.fat_entry(c)
            if c >= self.eoc or c == 0:
                break
        return out

    def read_clusters(self, clusters):
        return b''.join(self.src.read(self.cluster_offset(c), self.cluster_size) for c in clusters)

    def root_data(self):
        if self.bits == 32:
            return self.read_clusters(self.chain(self.root_cluster))
        off = self.vol_off + (self.rsvd + self.nfats * self.fatsz) * self.bps
        return self.src.read(off, self.root_ents * 32)

    def _looks_like_dir_start(self, data, cluster):
        if len(data) < 64 or data[0:11] != b'.          ' or not (data[11] & 0x10):
            return False
        return ((u16(data, 20) << 16) | u16(data, 26)) == cluster

    def read_deleted_dir(self, c):
        data = self.src.read(self.cluster_offset(c), self.cluster_size)
        if not self._looks_like_dir_start(data, c):
            return None
        out = data
        nxt = c + 1
        # a folder bigger than one cluster: keep going while the folder hasn't ended
        for _ in range(64):
            if out[-32] == 0 or nxt > self.max_cluster or not self.is_free(nxt):
                break
            more = self.src.read(self.cluster_offset(nxt), self.cluster_size)
            out += more
            nxt += 1
        return out

    def walk(self, emit, include_existing, cancel, status=None):
        visited = set()
        stack = [('', self.root_data(), False)]
        dirs = 0
        while stack:
            if cancel.is_set():
                return
            path, data, in_deleted = stack.pop()
            dirs += 1
            if status and dirs % 20 == 0:
                status(f'Reading folders... {dirs} checked')
            for ent in parse_fat_dir(data):
                deleted = ent.deleted or in_deleted
                if ent.is_dir:
                    c = ent.cluster
                    if c < 2 or c > self.max_cluster or c in visited:
                        continue
                    visited.add(c)
                    sub_path = f'{path}/{ent.name}'
                    if deleted:
                        sub = self.read_deleted_dir(c)
                        if sub:
                            stack.append((sub_path, sub, True))
                    else:
                        stack.append((sub_path, self.read_clusters(self.chain(c, 16384)), False))
                    continue
                if deleted:
                    runs, cond = self.plan_deleted(ent.cluster, ent.size)
                    if not runs:
                        continue
                    item = FoundFile(ent.name, path or '/', ent.size, runs, 'Deleted', cond, ent.mtime)
                    self.check_condition(self.src, item)
                    emit(item)
                elif (include_existing or is_special_name(ent.name) or _in_recycle(path)) and ent.size > 0 \
                        and ent.cluster >= 2:
                    chain = self.chain(ent.cluster)
                    runs = runs_from_clusters(chain, self.cluster_offset, self.cluster_size, ent.size)
                    it = FoundFile(ent.name, path or '/', ent.size, runs, 'Existing', 'Not deleted', ent.mtime)
                    it.special = not include_existing
                    emit(it)


# --------------------------------------------------------------------------------------
# exFAT
# --------------------------------------------------------------------------------------

def exfat_set_checksum(entries):
    """Entry-set checksum with in-use bits restored (deleted sets have them cleared)."""
    cs = 0
    for k, e in enumerate(entries):
        b = bytearray(e)
        b[0] |= 0x80
        for i, byte in enumerate(b):
            if k == 0 and i in (2, 3):
                continue
            cs = ((0x8000 if cs & 1 else 0) + (cs >> 1) + byte) & 0xFFFF
    return cs


class _ExEntry:
    __slots__ = ('name', 'deleted', 'is_dir', 'cluster', 'size', 'mtime', 'no_fat_chain', 'checksum_ok')


def parse_exfat_dir(data):
    n = len(data) // 32
    i = 0
    while i < n:
        t = data[i * 32]
        if t == 0x00:
            break
        if t in (0x85, 0x05):
            deleted = t == 0x05
            sc = data[i * 32 + 1]
            if 2 <= sc <= 18 and i + sc < n:
                ents = [data[(i + k) * 32:(i + k + 1) * 32] for k in range(sc + 1)]
                types = [e[0] for e in ents]
                consistent = all((tt & 0x80) == (t & 0x80) for tt in types)
                if (types[1] & 0x7F) == 0x40 and consistent:
                    stream = ents[1]
                    name_ents = [e for e in ents[2:] if (e[0] & 0x7F) == 0x41]
                    namelen = stream[3]
                    name = ''.join(e[2:32].decode('utf-16-le', 'replace') for e in name_ents)[:namelen]
                    name = name.split('\x00')[0]
                    ent = _ExEntry()
                    ent.name = name or 'unnamed'
                    ent.deleted = deleted
                    ent.is_dir = bool(u16(ents[0], 4) & 0x10)
                    ts = u32(ents[0], 12)
                    ent.mtime = dos_datetime(ts >> 16, ts & 0xFFFF)
                    ent.no_fat_chain = bool(stream[1] & 0x02)
                    ent.cluster = u32(stream, 20)
                    ent.size = u64(stream, 24)
                    ent.checksum_ok = exfat_set_checksum(ents) == u16(ents[0], 2)
                    yield ent
                    i += sc + 1
                    continue
        i += 1


class ExfatVolume(_BitmapAlloc, _VolumeBase):
    bit_base = 2
    kind = 'exFAT'

    @staticmethod
    def probe(bs):
        return len(bs) >= 512 and bs[3:11] == b'EXFAT   '

    def __init__(self, src, vol_off, bs):
        self.src = src
        self.vol_off = vol_off
        self.bps = 1 << bs[108]
        self.spc = 1 << bs[109]
        self.fat_off = u32(bs, 80)
        self.fat_len = u32(bs, 84)
        self.heap_off = u32(bs, 88)
        self.cluster_count = u32(bs, 92)
        self.root_cluster = u32(bs, 96)
        self.cluster_size = self.bps * self.spc
        self.max_cluster = self.cluster_count + 1
        self.nbits = self.cluster_count
        self.size_bytes = u64(bs, 72) * self.bps
        fat = src.read(vol_off + self.fat_off * self.bps, min(self.fat_len * self.bps, (self.max_cluster + 1) * 4))
        self._fat = struct.unpack_from('<%dI' % (len(fat) // 4), fat, 0)
        self.bitmap = b''
        root = self.read_chain_bytes(self.root_cluster, None, False)
        for k in range(0, len(root) - 31, 32):
            if root[k] == 0x81:
                bc = u32(root, k + 20)
                bl = u64(root, k + 24)
                self.bitmap = self.read_chain_bytes(bc, bl, False)
                break
            if root[k] == 0:
                break
        self._root = root

    def fat_entry(self, c):
        return self._fat[c] if c < len(self._fat) else 0

    def cluster_offset(self, c):
        return self.vol_off + (self.heap_off + (c - 2) * self.spc) * self.bps

    def chain(self, c, max_clusters=1 << 22):
        out = []
        seen = set()
        while 2 <= c <= self.max_cluster and c not in seen and len(out) < max_clusters:
            out.append(c)
            seen.add(c)
            c = self.fat_entry(c)
            if c >= 0xFFFFFFF7 or c == 0:
                break
        return out

    def read_chain_bytes(self, c, length, contiguous):
        if c < 2:
            return b''
        cs = self.cluster_size
        if contiguous:
            n = max(1, -(-(length or cs) // cs))
            data = self.src.read(self.cluster_offset(c), n * cs)
        else:
            limit = None if length is None else max(1, -(-length // cs))
            clusters = self.chain(c, limit or (1 << 16))
            data = b''.join(self.src.read(self.cluster_offset(x), cs) for x in clusters)
        return data if length is None else data[:length]

    def walk(self, emit, include_existing, cancel, status=None):
        visited = {self.root_cluster}
        stack = [('', self._root, False)]
        dirs = 0
        cs = self.cluster_size
        while stack:
            if cancel.is_set():
                return
            path, data, in_deleted = stack.pop()
            dirs += 1
            if status and dirs % 20 == 0:
                status(f'Reading folders... {dirs} checked')
            for ent in parse_exfat_dir(data):
                deleted = ent.deleted or in_deleted
                if ent.is_dir:
                    c = ent.cluster
                    if c < 2 or c > self.max_cluster or c in visited:
                        continue
                    visited.add(c)
                    if deleted:
                        sub = self.src.read(self.cluster_offset(c), max(cs, min(ent.size, 4 * MB)))
                        # a deleted folder's chain is gone; only trust it if it parses
                        if not any(True for _ in parse_exfat_dir(sub[:cs])):
                            continue
                    else:
                        sub = self.read_chain_bytes(c, ent.size, ent.no_fat_chain)
                    stack.append((f'{path}/{ent.name}', sub, deleted))
                    continue
                if deleted:
                    known = None
                    if not ent.no_fat_chain and ent.size > cs:
                        chain = self.chain(ent.cluster)
                        if len(chain) > 1:
                            known = chain
                    runs, cond = self.plan_deleted(ent.cluster, ent.size, known)
                    if not runs:
                        continue
                    item = FoundFile(ent.name, path or '/', ent.size, runs, 'Deleted', cond, ent.mtime,
                                     '' if ent.checksum_ok else 'entry checksum mismatch')
                    self.check_condition(self.src, item)
                    emit(item)
                elif (include_existing or is_special_name(ent.name) or _in_recycle(path)) and ent.size > 0 \
                        and ent.cluster >= 2:
                    if ent.no_fat_chain:
                        runs = [(self.cluster_offset(ent.cluster), ent.size)]
                    else:
                        runs = runs_from_clusters(self.chain(ent.cluster), self.cluster_offset, cs, ent.size)
                    it = FoundFile(ent.name, path or '/', ent.size, runs, 'Existing', 'Not deleted', ent.mtime)
                    it.special = not include_existing
                    emit(it)


# --------------------------------------------------------------------------------------
# NTFS
# --------------------------------------------------------------------------------------

def filetime(ft):
    if not ft:
        return None
    try:
        return datetime.datetime(1601, 1, 1) + datetime.timedelta(microseconds=ft // 10)
    except (OverflowError, ValueError):
        return None


def ntfs_fixup(rec, stride=512):
    """Undo NTFS's per-sector update-sequence protection. Returns (bytes, intact)."""
    if len(rec) < 48:
        return rec, False
    usa_off, usa_cnt = u16(rec, 4), u16(rec, 6)
    if usa_cnt < 2 or usa_off + 2 * usa_cnt > len(rec):
        return rec, False
    b = bytearray(rec)
    usn = rec[usa_off:usa_off + 2]
    ok = True
    for i in range(1, usa_cnt):
        p = i * stride - 2
        if p + 2 > len(b):
            break
        if b[p:p + 2] != usn:
            ok = False
        b[p:p + 2] = rec[usa_off + 2 * i: usa_off + 2 * i + 2]
    return bytes(b), ok


def ntfs_attrs(rec):
    pos = u16(rec, 20)
    while pos + 16 <= len(rec):
        t = u32(rec, pos)
        if t in (0xFFFFFFFF, 0):
            break
        ln = u32(rec, pos + 4)
        if ln < 16 or pos + ln > len(rec):
            break
        yield t, rec[pos:pos + ln]
        pos += ln


def ntfs_resident_value(a):
    off, ln = u16(a, 20), u32(a, 16)
    return a[off:off + ln]


def ntfs_runlist(a):
    """Decode a non-resident attribute's mapping pairs into [(lcn or None, clusters)]."""
    pos = u16(a, 32)
    runs = []
    lcn = 0
    while pos < len(a):
        h = a[pos]
        if h == 0:
            break
        ls, os_ = h & 0x0F, h >> 4
        pos += 1
        if ls == 0 or ls > 8 or os_ > 8 or pos + ls + os_ > len(a):
            break
        length = int.from_bytes(a[pos:pos + ls], 'little')
        pos += ls
        if os_ == 0:
            runs.append((None, length))             # sparse
        else:
            lcn += int.from_bytes(a[pos:pos + os_], 'little', signed=True)
            pos += os_
            runs.append((lcn, length))
    return runs


_SPECIAL_UTF16 = ('$\x00I\x00', '$\x00R\x00', 't\x00h\x00u\x00m\x00b\x00', 'T\x00h\x00u\x00m\x00b\x00',
                  'i\x00c\x00o\x00n\x00c\x00a\x00c\x00h\x00e\x00_\x00')
_SPECIAL_UTF16 = tuple(x.encode('latin-1') for x in _SPECIAL_UTF16)


def _maybe_special(buf, o, rs):
    """Cheap test: could this MFT record be a Recycle Bin or thumbnail-cache file?"""
    rec = buf[o:o + rs]
    return any(p in rec for p in _SPECIAL_UTF16)


class NtfsVolume(_BitmapAlloc, _VolumeBase):
    kind = 'NTFS'
    bit_base = 0

    @staticmethod
    def probe(bs):
        return len(bs) >= 512 and bs[3:11] == b'NTFS    '

    def __init__(self, src, vol_off, bs):
        self.src = src
        self.vol_off = vol_off
        self.bps = u16(bs, 11)
        spc = bs[13]
        if spc > 0x80:
            spc = 1 << (256 - spc)
        if self.bps not in (512, 1024, 2048, 4096) or spc == 0:
            raise ValueError('bad NTFS boot sector')
        self.spc = spc
        self.cluster_size = cs = self.bps * spc
        tot = u64(bs, 40)
        self.size_bytes = (tot + 1) * self.bps
        self.cluster_count = tot // spc
        self.nbits = self.cluster_count
        self.max_cluster = self.cluster_count - 1
        c = struct.unpack_from('<b', bs, 64)[0]
        self.rec_size = c * cs if c > 0 else 1 << (-c)
        if self.rec_size not in (512, 1024, 2048, 4096, 8192):
            raise ValueError('bad MFT record size')

        rec0 = None
        for lcn in (u64(bs, 48), u64(bs, 56)):      # $MFT, then its mirror
            r = src.read(vol_off + lcn * cs, self.rec_size)
            if r[:4] == b'FILE':
                rec0 = ntfs_fixup(r)[0]
                break
        if rec0 is None:
            raise ValueError('MFT not found')
        # bootstrap with the base record's runs, then add any extension records
        first = [a for t, a in ntfs_attrs(rec0) if t == 0x80 and a[9] == 0 and a[8]]
        if not first:
            raise ValueError('MFT has no data')
        self.mft_size = u64(first[0], 48)
        self.mft_vmap = self._vmap(ntfs_runlist(first[0]))
        d = self._collect_data(rec0, 0)
        if d and d['runs']:
            self.mft_vmap = self._vmap(d['runs'])
            self.mft_size = d['size']

        self.bitmap_ok = False
        self.bitmap = bytes(-(-self.cluster_count // 8))
        try:
            r6 = self.read_record(6)
            if r6[:4] == b'FILE':
                d = self._collect_data(ntfs_fixup(r6)[0], 6)
                if d and d['runs']:
                    want = min(d['size'], -(-self.cluster_count // 8))
                    self.bitmap = self._read_mapped(self._vmap(d['runs']), 0, want)
                    self.bitmap_ok = True
        except Exception:
            pass

    # -- mapping helpers
    def cluster_offset(self, c):
        return self.vol_off + c * self.cluster_size

    def carve_base(self):
        return self.vol_off

    def cluster_index(self, off):
        return (off - self.vol_off) // self.cluster_size

    def bytes_in_use(self, off, length):
        c0 = self.cluster_index(off)
        c1 = self.cluster_index(off + max(length, 1) - 1)
        if c0 < 0 or c1 >= self.cluster_count:
            return length
        return bitmap_count_used(self.bitmap, c0, c1 - c0 + 1) * self.cluster_size

    def mft_device_ranges(self):
        cs = self.cluster_size
        return [(self.vol_off + lcn * cs, self.vol_off + (lcn + cnt) * cs)
                for vcn, lcn, cnt in self.mft_vmap if lcn is not None]

    @staticmethod
    def _vmap(runs):
        vm = []
        vcn = 0
        for lcn, cnt in runs:
            vm.append((vcn, lcn, cnt))
            vcn += cnt
        return vm

    def _read_mapped(self, vmap, offset, length):
        cs = self.cluster_size
        out = bytearray()
        pos, end = offset, offset + length
        idx = 0
        while pos < end:
            vcn = pos // cs
            while idx < len(vmap) and vmap[idx][0] + vmap[idx][2] <= vcn:
                idx += 1
            if idx >= len(vmap) or vmap[idx][0] > vcn:
                if idx < len(vmap):
                    nxt = vmap[idx][0] * cs
                    n = min(end, nxt) - pos
                    out += b'\x00' * n
                    pos += n
                    continue
                out += b'\x00' * (end - pos)
                break
            vs, lcn, cnt = vmap[idx]
            n = min(end, (vs + cnt) * cs) - pos
            if lcn is None:
                out += b'\x00' * n
            else:
                out += self.src.read(self.vol_off + (lcn + vcn - vs) * cs + pos % cs, n)
            pos += n
        return bytes(out)

    def read_record(self, num):
        return self._read_mapped(self.mft_vmap, num * self.rec_size, self.rec_size)

    # -- record parsing
    def _collect_data(self, rec, num, ext_index=None):
        """Unnamed $DATA stream of a base record, following $ATTRIBUTE_LIST into extension records.
        Returns {size, inline, runs, flags} or None."""
        datas = []
        alist = None
        seen = set()
        for t, a in ntfs_attrs(rec):
            if t == 0x80 and a[9] == 0:
                datas.append(a)
            elif t == 0x20:
                alist = a
        if alist is not None:
            if alist[8] == 0:
                raw = ntfs_resident_value(alist)
            else:
                raw = self._read_mapped(self._vmap(ntfs_runlist(alist)), 0, min(u64(alist, 48), 1 * MB))
            p = 0
            while p + 26 <= len(raw):
                t, ln = u32(raw, p), u16(raw, p + 4)
                if ln < 26 or t == 0:
                    break
                if t == 0x80 and raw[p + 6] == 0:
                    ref = u64(raw, p + 16) & 0xFFFFFFFFFFFF
                    if ref != num and ref not in seen:
                        seen.add(ref)
                        r = self.read_record(ref)
                        if r[:4] == b'FILE':
                            r = ntfs_fixup(r)[0]
                            if (u64(r, 32) & 0xFFFFFFFFFFFF) == num:
                                datas += [a2 for t2, a2 in ntfs_attrs(r) if t2 == 0x80 and a2[9] == 0]
                p += ln
        for ref in (ext_index or {}).get(num, []):      # pieces the list no longer mentions
            if alist is not None and ref in seen:
                continue
            r = self.read_record(ref)
            if r[:4] == b'FILE':
                r = ntfs_fixup(r)[0]
                if (u64(r, 32) & 0xFFFFFFFFFFFF) == num:
                    datas += [a2 for t2, a2 in ntfs_attrs(r) if t2 == 0x80 and a2[9] == 0 and a2[8]]
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

    def _ext_attrs(self, rec, num, ext_index=None):
        """Attributes kept in extension records (from $ATTRIBUTE_LIST and the extension index)."""
        alist = next((a for t, a in ntfs_attrs(rec) if t == 0x20), None)
        raw = b''
        if alist is not None:
            if alist[8] == 0:
                raw = ntfs_resident_value(alist)
            else:
                raw = self._read_mapped(self._vmap(ntfs_runlist(alist)), 0, min(u64(alist, 48), 1 * MB))
        refs = list((ext_index or {}).get(num, []))
        p = 0
        while p + 26 <= len(raw):
            t, ln = u32(raw, p), u16(raw, p + 4)
            if ln < 26 or t == 0:
                break
            ref = u64(raw, p + 16) & 0xFFFFFFFFFFFF
            if ref != num and ref not in refs:
                refs.append(ref)
            p += ln
        out = []
        for ref in refs[:64]:
            r = self.read_record(ref)
            if r[:4] == b'FILE':
                r = ntfs_fixup(r)[0]
                if (u64(r, 32) & 0xFFFFFFFFFFFF) == num:
                    out += list(ntfs_attrs(r))
        return out

    @staticmethod
    def _names(attrs):
        best = None
        mtime = None
        for t, a in attrs:
            if a[8] != 0:
                continue
            if t == 0x10:
                v = ntfs_resident_value(a)
                if len(v) >= 16:
                    mtime = filetime(u64(v, 8))
            elif t == 0x30:
                v = ntfs_resident_value(a)
                if len(v) < 66:
                    continue
                nl, ns = v[64], v[65]
                name = v[66:66 + 2 * nl].decode('utf-16-le', 'replace')
                pref = {1: 0, 3: 0, 0: 1, 2: 2}.get(ns, 3)       # Win32 name over DOS 8.3 alias
                if best is None or pref < best[0]:
                    best = (pref, name, u64(v, 0))
                if mtime is None:
                    mtime = filetime(u64(v, 16))
        if not best:
            return None
        ref = best[2]
        return best[1], ref & 0xFFFFFFFFFFFF, ref >> 48, mtime

    def _device_runs(self, runs, size):
        cs = self.cluster_size
        out = []
        left = size
        for lcn, cnt in runs:
            if left <= 0:
                break
            n = min(cnt * cs, left)
            off = None if lcn is None else self.vol_off + lcn * cs
            if out and off is not None and out[-1][0] is not None and out[-1][0] + out[-1][1] == off:
                out[-1] = (out[-1][0], out[-1][1] + n)
            elif out and off is None and out[-1][0] is None:
                out[-1] = (None, out[-1][1] + n)
            else:
                out.append((off, n))
            left -= n
        return out, left

    def _deleted_condition(self, runs, size):
        cs = self.cluster_size
        need = -(-size // cs)
        used = total = 0
        first_used = None
        for lcn, cnt in runs:
            if need <= 0:
                break
            take = min(cnt, need)
            need -= take
            if lcn is None:
                continue
            u = bitmap_count_used(self.bitmap, lcn, take)
            if first_used is None:
                first_used = not self.is_free(lcn)
            used += u
            total += take
        if not self.bitmap_ok:
            return 'Unknown (no free-space map)'
        if used == 0:
            return 'Good'
        if used >= total and first_used:
            return 'Overwritten'
        return 'Partially overwritten'

    def walk(self, emit, include_existing, cancel, status=None):
        rs = self.rec_size
        total = self.mft_size // rs
        dirs = {}
        files = []
        ext_index = {}
        deferred = []

        def process(num, rec, in_use, is_dir, intact, force=False):
            info = self._names(ntfs_attrs(rec))
            if info is None:          # name moved to an extension record
                info = self._names(list(ntfs_attrs(rec)) + self._ext_attrs(rec, num, ext_index))
            if info is None:
                if in_use or is_dir:
                    return
                info = (None, None, None, None)   # name is gone; the data map may survive
            name, parent, pseq, mtime = info
            if is_dir:
                if name is not None:
                    dirs[num] = (name, parent, pseq, u16(rec, 16), bool(in_use))
                return
            special = False
            if in_use and not include_existing:
                if not (force or (name and is_special_name(name))):
                    return
                special = True
            d = self._collect_data(rec, num, ext_index)
            if not d or d['size'] <= 0:
                return
            files.append((name, parent, pseq, mtime, bool(in_use), d, intact, num, special))

        per = max(1, (4 * MB) // rs)
        n = 0
        while n < total:
            if cancel.is_set():
                return
            cnt = min(per, total - n)
            buf = self._read_mapped(self.mft_vmap, n * rs, cnt * rs)
            for k in range(cnt):
                num = n + k
                o = k * rs
                if buf[o:o + 4] != b'FILE':
                    continue
                base = u64(buf, o + 32) & 0xFFFFFFFFFFFF
                if base:                                   # extension record: remember its owner
                    ext_index.setdefault(base, []).append(num)
                    continue
                flags = u16(buf, o + 22)
                in_use, is_dir = flags & 1, flags & 2
                if in_use and not is_dir and not include_existing and not _maybe_special(buf, o, rs):
                    continue
                if num < 24 and num != 5:                  # NTFS's own metadata files
                    continue
                rec, intact = ntfs_fixup(buf[o:o + rs])
                if any(t == 0x20 for t, _ in ntfs_attrs(rec)):
                    deferred.append((num, rec, in_use, is_dir, intact))   # needs the full index
                    continue
                process(num, rec, in_use, is_dir, intact)
            n += cnt
            if status:
                status(f'Reading the NTFS file table... {n * 100 // max(total, 1)}%  '
                       f'({len(files):,} candidate files)')
        for args in deferred:
            if cancel.is_set():
                return
            process(*args)

        # Files inside folders that were sent to the Recycle Bin ($R folders) aren't deleted,
        # so a second, targeted pass picks them up.
        if not include_existing:
            rdirs = {n for n, d in dirs.items() if d[0].lower().startswith('$r') and d[0].lower() != '$recycle.bin'}
            if rdirs:
                grew = True
                while grew:
                    grew = False
                    for n, d in dirs.items():
                        if n not in rdirs and d[1] in rdirs:
                            rdirs.add(n)
                            grew = True
                n = 0
                while n < total and not cancel.is_set():
                    cnt = min(per, total - n)
                    buf = self._read_mapped(self.mft_vmap, n * rs, cnt * rs)
                    for k in range(cnt):
                        o = k * rs
                        if buf[o:o + 4] != b'FILE' or u64(buf, o + 32) & 0xFFFFFFFFFFFF:
                            continue
                        flags = u16(buf, o + 22)
                        if not flags & 1 or flags & 2:
                            continue
                        rec, intact = ntfs_fixup(buf[o:o + rs])
                        nm = self._names(ntfs_attrs(rec))
                        if nm and nm[1] in rdirs:
                            process(n + k, rec, 1, 0, intact, force=True)
                    n += cnt

        cache = {}

        def folder_of(parent, pseq):
            key = (parent, pseq)
            if key in cache:
                return cache[key]
            parts = []
            cur, exp = parent, pseq
            lost = False
            for _ in range(128):
                if cur == 5:
                    break
                d = dirs.get(cur)
                if d is None:
                    lost = True
                    break
                dname, dpar, dps, dseq, d_in_use = d
                if dseq != exp and not (not d_in_use and dseq == ((exp + 1) & 0xFFFF)):
                    lost = True                            # folder record reused by something else
                    break
                parts.append(dname)
                cur, exp = dpar, dps
            path = '/'.join(reversed(parts))
            path = ('/(lost folder)/' + path if lost else '/' + path).rstrip('/') or '/'
            cache[key] = path
            return path

        for name, parent, pseq, mtime, in_use, d, intact, num, special in files:
            if cancel.is_set():
                return
            nameless = name is None
            size, inline, runs, aflags = d['size'], d['inline'], d['runs'], d['flags']
            if inline is not None:
                dev, short = [], 0
            else:
                dev, short = self._device_runs(runs, size)
            if aflags & 0x0001:
                cond = 'Compressed (not supported)'
            elif aflags & 0x4000:
                cond = 'Encrypted (not supported)'
            elif in_use:
                cond = 'Not deleted'
            elif inline is not None:
                cond = 'Good'
            else:
                cond = self._deleted_condition(runs, size)
            if short > 0:
                dev.append((None, short))
                if not in_use:
                    cond = 'Incomplete record'
            if nameless:
                head = d['inline'][:16] if inline is not None else (
                    self.src.read(dev[0][0], 16) if dev and dev[0][0] is not None else b'')
                name = f'File record {num}.{guess_ext(head) or "bin"}'
            item = FoundFile(name, '/(name lost)' if nameless else folder_of(parent, pseq), size, dev,
                             'Existing' if in_use else 'Deleted', cond, mtime,
                             '' if intact else 'file record was partly damaged')
            item.special = special
            if inline is not None:
                item.inline = inline
                item.note = (item.note + '; ' if item.note else '') + 'small file stored in the file table'
            elif not in_use:
                self.check_condition(self.src, item)
            emit(item)


# --------------------------------------------------------------------------------------
# Locating the filesystem
# --------------------------------------------------------------------------------------

def _probe_at(src, off, depth=0):
    """(volume or None, kind) for whatever filesystem starts at byte `off`. Looks inside Linux RAID
    members (mirror / single-disk NAS volumes) and LVM physical volumes as well."""
    bs = src.read(off, 512)
    if len(bs) < 512:
        return None, 'Unknown'
    if bs[:6] == b'LUKS\xba\xbe':
        return None, 'LUKS encrypted (unlock it with Tools > Unlock encrypted drive)'
    for cls in (ExfatVolume, NtfsVolume, FatVolume):
        if cls.probe(bs):
            try:
                v = cls(src, off, bs)
                return v, v.kind
            except Exception:
                name = cls.kind if isinstance(cls.kind, str) and cls.kind != '?' else 'FAT'
                return None, f'{name} (damaged)'
    try:
        import linuxfs
        import xfs
        import f2fs
        for cls in (linuxfs.Ext4Volume, linuxfs.BtrfsVolume, xfs.XfsVolume, f2fs.F2fsVolume):
            if cls.probe_at(src, off):
                try:
                    v = cls(src, off)
                    return v, v.kind
                except Exception:
                    return None, f'{cls.kind} (damaged)'
    except ImportError:
        pass
    if depth < 2:
        try:
            import raid
            size = max(0, src.size - off)
            for sb in raid.md_superblocks(src, off, size):
                # a mirror member (or single-disk NAS volume) holds the whole filesystem itself
                if sb['level'] in (1, -1) or sb['raid_disks'] == 1:
                    v, k = _probe_at(src, off + sb['data_offset'], depth + 1)
                    if v or k != 'Unknown':
                        return v, k
                    return None, f"Linux RAID {sb['level']} member" if sb['level'] != 1 else 'Linux RAID 1 member'
                return None, f"Linux RAID {sb['level']} member (use Virtual RAID)"
            pv = raid.lvm_pv(src, off)
            if pv:
                return None, 'LVM'
        except Exception:
            pass
    return None, 'Unknown'


def _lvm_location(src, off):
    """Where the LVM label is: right here, or inside a Linux RAID member that starts here."""
    import raid
    if raid.lvm_pv(src, off):
        return off
    for sb in raid.md_superblocks(src, off, max(0, src.size - off)):
        if raid.lvm_pv(src, off + sb['data_offset']):
            return off + sb['data_offset']
    return off


def _lvm_partitions(src, off, first_index):
    """LVM logical volumes in a physical volume at `off`. Single-piece volumes are read in place;
    multi-piece ones through an LvSource whose file positions get mapped back afterwards."""
    import raid
    out = []
    try:
        pv = raid.lvm_pv(src, off)
        if not pv:
            return out
        lvs = raid.assemble_lvm([(src, pv)])
    except Exception:
        return out
    for k, lv in enumerate(lvs):
        segs = [sg for sg in lv.segs if sg[2] is not None]
        idx = first_index + k
        if len(lv.segs) == 1 and segs and segs[0][2] is src:
            v, kind = _probe_at(src, segs[0][3], 2)
            p = Partition(idx, segs[0][3], lv.size, v, f'{kind} in LVM {lv.label[11:]}')
        else:
            v, kind = _probe_at(lv, 0, 2)
            p = Partition(idx, segs[0][3] if segs else off, lv.size, v, f'{kind} in LVM {lv.label[11:]}')
            p.remap = lv.to_parent_runs if all(sg[2] is src for sg in segs) else None
            if p.remap is None:
                p.volume = None
                p.kind += ' (spans other drives - use Virtual RAID)'
        out.append(p)
    return out


class Partition:
    def __init__(self, index, offset, size, volume, kind):
        self.index = index
        self.offset = offset
        self.size = size
        self.volume = volume
        self.kind = kind
        self.remap = None           # maps file positions from a container (LVM) back to the source


def _partition_table(src):
    """[(offset, size)] from an MBR (incl. extended/logical partitions) or GPT."""
    ss = src.sector_size
    bs = src.read(0, 512)
    if len(bs) < 512 or bs[510:512] != b'\x55\xaa':
        return []
    out = []
    gpt = False
    for k in range(4):
        e = bs[446 + 16 * k: 462 + 16 * k]
        ptype, lba, cnt = e[4], u32(e, 8), u32(e, 12)
        if ptype == 0xEE:
            gpt = True
        elif ptype in (0x05, 0x0F, 0x85) and lba:          # extended: walk the EBR chain
            ebr = lba
            for _ in range(256):
                eb = src.read(ebr * ss, 512)
                if len(eb) < 512 or eb[510:512] != b'\x55\xaa':
                    break
                l_type, l_rel, l_cnt = eb[446 + 4], u32(eb, 446 + 8), u32(eb, 446 + 12)
                if l_type and l_rel:
                    out.append(((ebr + l_rel) * ss, l_cnt * ss))
                n_type, n_rel = eb[462 + 4], u32(eb, 462 + 8)
                if n_type not in (0x05, 0x0F, 0x85) or not n_rel:
                    break
                ebr = lba + n_rel
        elif ptype and lba:
            out.append((lba * ss, cnt * ss))
    if gpt:
        for sz in dict.fromkeys((ss, 512, 4096)):
            hdr = src.read(sz, 512)
            if hdr[:8] != b'EFI PART':
                continue
            ent_lba, count, esz = u64(hdr, 72), u32(hdr, 80), u32(hdr, 84)
            if esz < 128 or esz > 4096:
                break
            table = src.read(ent_lba * sz, min(count, 256) * esz)
            for j in range(0, len(table) - esz + 1, esz):
                if not table[j:j + 16].strip(b'\x00'):
                    continue
                first, last = u64(table, j + 32), u64(table, j + 40)
                if first and last >= first:
                    out.append((first * sz, (last - first + 1) * sz))
            break
    return sorted(out)


def find_partitions(src):
    """Every filesystem on the source. A drive letter is one volume; a whole physical
    disk or disk image may hold several partitions (and LVM volumes inside them)."""
    v, kind = _probe_at(src, 0)
    if v:
        return [Partition(1, 0, v.size_bytes or src.size, v, kind)]
    if kind == 'LVM':
        return _lvm_partitions(src, _lvm_location(src, 0), 1)
    parts = []
    for i, (off, size) in enumerate(_partition_table(src), 1):
        pv, pk = _probe_at(src, off)
        if pk == 'LVM':
            parts += _lvm_partitions(src, _lvm_location(src, off), len(parts) + 1)
            continue
        parts.append(Partition(len(parts) + 1, off, size, pv, pk))
    if not parts and kind != 'Unknown':
        parts.append(Partition(1, 0, src.size, None, kind))
    return parts


def open_volume(src):
    """First readable filesystem: (volume or None, description)."""
    parts = find_partitions(src)
    for p in parts:
        if p.volume:
            return p.volume, p.kind
    return None, (parts[0].kind if parts else 'Unknown / damaged')


# --------------------------------------------------------------------------------------
# Deep scan (file carving)
# --------------------------------------------------------------------------------------

class _Reader:
    """Cached random access within one candidate file, relative to its start."""

    BLOCK = 1 * MB

    def __init__(self, src, base, limit):
        self.src = src
        self.base = base
        self.limit = limit
        self.c_off = -1
        self.c = b''

    def at(self, pos, n):
        if pos < 0 or pos >= self.limit:
            return b''
        n = min(n, self.limit - pos)
        if self.c_off >= 0 and self.c_off <= pos and pos + n <= self.c_off + len(self.c):
            return self.c[pos - self.c_off: pos - self.c_off + n]
        if n > self.BLOCK:
            return self.src.read(self.base + pos, n)
        self.c_off = pos
        self.c = self.src.read(self.base + pos, min(self.BLOCK, self.limit - pos))
        return self.c[:n]

    def find(self, pat, start, end, step=4 * MB):
        end = min(end, self.limit)
        pos = start
        while pos < end:
            want = min(step + len(pat) - 1, end - pos)
            chunk = self.src.read(self.base + pos, want)
            i = chunk.find(pat)
            if i >= 0 and pos + i + len(pat) <= end:
                return pos + i
            if len(chunk) < want or len(chunk) < len(pat):
                break
            pos += step
        return -1


# Each carver returns (length, ext) or None.

def carve_jpeg(r):
    pos = 2
    for _ in range(2000):
        b = r.at(pos, 4)
        if len(b) < 4 or b[0] != 0xFF:
            return None
        m = b[1]
        if m == 0xFF:
            pos += 1
            continue
        if m == 0xD8 or 0xD0 <= m <= 0xD7 or m == 0x01:
            pos += 2
            continue
        if m == 0xD9:
            return pos + 2, 'jpg'
        if m < 0xC0:
            return None
        seg = be16(b, 2)
        if seg < 2:
            return None
        pos += 2 + seg
        if m == 0xDA:
            e = r.find(b'\xff\xd9', pos, r.limit)
            return (e + 2, 'jpg') if e >= 0 else None
    return None


def carve_png(r):
    pos = 8
    first = True
    for _ in range(100000):
        h = r.at(pos, 8)
        if len(h) < 8:
            return None
        ln, typ = be32(h, 0), h[4:8]
        if not all(65 <= c <= 90 or 97 <= c <= 122 for c in typ):
            return None
        if first and typ != b'IHDR':
            return None
        first = False
        pos += 12 + ln
        if pos > r.limit:
            return None
        if typ == b'IEND':
            return pos, 'png'
    return None


def carve_gif(r):
    h = r.at(0, 13)
    if len(h) < 13:
        return None
    pos = 13
    if h[10] & 0x80:
        pos += 3 * (2 ** ((h[10] & 7) + 1))

    def skip_sub(p):
        for _ in range(1 << 20):
            b = r.at(p, 1)
            if not b:
                return -1
            p += 1
            if b[0] == 0:
                return p
            p += b[0]
        return -1

    for _ in range(100000):
        t = r.at(pos, 1)
        if not t:
            return None
        if t == b';':
            return pos + 1, 'gif'
        if t == b'!':
            pos = skip_sub(pos + 2)
        elif t == b',':
            d = r.at(pos, 10)
            if len(d) < 10:
                return None
            pos += 10
            if d[9] & 0x80:
                pos += 3 * (2 ** ((d[9] & 7) + 1))
            pos = skip_sub(pos + 1)
        else:
            return None
        if pos < 0:
            return None
    return None


def carve_pdf(r):
    e = r.find(b'%%EOF', 5, r.limit)
    if e < 0:
        return None
    end = e + 5
    while True:   # incremental updates append more sections, each ending in %%EOF
        nxt = r.find(b'%%EOF', end, min(end + 1 * MB, r.limit))
        if nxt < 0 or r.find(b'%PDF-', end, nxt) >= 0:
            break
        end = nxt + 5
    t = r.at(end, 2)
    if t.startswith(b'\r\n'):
        end += 2
    elif t[:1] in (b'\n', b'\r'):
        end += 1
    return end, 'pdf'


def carve_zip(r):
    lh = r.at(0, 30)
    if len(lh) < 30 or u16(lh, 8) not in (0, 1, 6, 8, 9, 12, 14, 93, 95, 98, 99):
        return None
    search = 30
    for _ in range(10000):
        e = r.find(b'PK\x05\x06', search, r.limit)
        if e < 0:
            return None
        rec = r.at(e, 22)
        if len(rec) < 22:
            return None
        cd_size, cd_off, clen = u32(rec, 12), u32(rec, 16), u16(rec, 20)
        if cd_off == 0xFFFFFFFF or e == cd_off + cd_size:
            names = r.at(cd_off, min(cd_size, 2 * MB)) if cd_off != 0xFFFFFFFF else r.at(0, 64 * KB)
            ext = 'zip'
            if b'word/document' in names:
                ext = 'docx'
            elif b'xl/workbook' in names:
                ext = 'xlsx'
            elif b'ppt/presentation' in names:
                ext = 'pptx'
            elif b'AndroidManifest.xml' in names:
                ext = 'apk'
            else:
                head = r.at(30, 100)
                if head.startswith(b'mimetype'):
                    if b'opendocument.text' in head:
                        ext = 'odt'
                    elif b'opendocument.spreadsheet' in head:
                        ext = 'ods'
                    elif b'opendocument.presentation' in head:
                        ext = 'odp'
                    elif b'epub' in head:
                        ext = 'epub'
                elif b'META-INF/MANIFEST.MF' in names:
                    ext = 'jar'
            return e + 22 + clen, ext
        search = e + 4
    return None


MP4_TOP = {b'ftyp', b'moov', b'mdat', b'free', b'skip', b'wide', b'uuid', b'meta', b'pdin', b'moof',
           b'mfra', b'styp', b'sidx', b'ssix', b'prft', b'emsg', b'junk', b'pnot', b'PICT', b'udta', b'idat'}
FTYP_BRANDS = {b'qt  ': 'mov', b'heic': 'heic', b'heix': 'heic', b'mif1': 'heic', b'msf1': 'heic',
               b'hevc': 'heic', b'avif': 'avif', b'M4A ': 'm4a', b'M4V ': 'm4v', b'crx ': 'cr3'}


_AVCC_HDR = frozenset(b'\x01\x21\x41\x61\x25\x45\x65\x06\x09\x27\x47\x67\x28\x48\x68'
                      b'\x02\x22\x26\x40\x42\x44\x4e\x50\x00\x12\x10\x14\x2a\x28\x20')


def _mdat_extent(r, start, window=1 * MB):
    """End of the media data in an MP4 whose length field was never written: follow the chain of
    length-prefixed video frames, hopping over audio, until it stops."""
    pos = start
    last_good = start
    misses = 0
    while pos < r.limit and misses < 3:
        h = r.at(pos, 5)
        if len(h) < 5:
            break
        ln = be32(h, 0)
        if 0 < ln < 32 * MB and h[4] in _AVCC_HDR and not h[4] & 0x80 and pos + 4 + ln <= r.limit:
            pos += 4 + ln
            last_good = pos
            misses = 0
            continue
        # resync: look ahead for the next plausible frame (audio sits between video frames)
        chunk = r.at(pos + 1, window)
        found = -1
        for i in range(0, len(chunk) - 5):
            if chunk[i] == 0 and chunk[i + 1] < 0x40 and chunk[i + 4] in _AVCC_HDR:
                ln = be32(chunk, i)
                if 0 < ln < 32 * MB:
                    j = i + 4 + ln
                    nxt = chunk[j:j + 5] if j + 5 <= len(chunk) else r.at(pos + 1 + j, 5)
                    if len(nxt) == 5 and 0 < be32(nxt, 0) < 32 * MB and nxt[4] in _AVCC_HDR:
                        found = i
                        break
        if found < 0:
            misses += 1
            break
        pos = pos + 1 + found
    return last_good


def carve_mp4(r):
    h0 = r.at(0, 12)
    if be32(h0, 0) < 8 or be32(h0, 0) > 4096:
        return None
    brand = h0[8:12]
    pos = 0
    seen = set()
    open_mdat = False
    for _ in range(100000):
        h = r.at(pos, 16)
        if len(h) < 8:
            break
        size, typ = be32(h, 0), h[4:8]
        if typ not in MP4_TOP:
            break
        if size == 1:
            if len(h) < 16:
                return None
            size = be64(h, 8)
        elif size == 0:
            if typ != b'mdat':
                return None
            seen.add(typ)
            pos = _mdat_extent(r, pos + 8)
            open_mdat = True
            break
        if size < 8:
            break
        seen.add(typ)
        if typ == b'mdat' and pos + size > r.limit:
            pos = _mdat_extent(r, pos + 8)
            open_mdat = True
            break
        pos += size
        if pos > r.limit:
            return None
    if b'mdat' not in seen:
        return None
    ext = FTYP_BRANDS.get(brand)
    if not ext:
        ext = '3gp' if brand.startswith(b'3g') else 'mp4'
    if not (seen & {b'moov', b'meta', b'moof'}) or open_mdat:
        if pos < 64 * KB:
            return None
        return pos, ext, 'Index missing - try Repair video'
    return pos, ext


def carve_riff(r):
    h = r.at(0, 12)
    size = u32(h, 4) + 8
    typ = h[8:12]
    ext = {b'WAVE': 'wav', b'AVI ': 'avi', b'WEBP': 'webp'}.get(typ)
    if not ext or size < 44 or size > r.limit:
        return None
    if size & 1:
        size += 1
    if ext == 'avi':   # OpenDML: AVI > 1 GB continues in RIFF AVIX chunks
        for _ in range(1000):
            nx = r.at(size, 12)
            if len(nx) == 12 and nx[0:4] == b'RIFF' and nx[8:12] == b'AVIX':
                size += u32(nx, 4) + 8
            else:
                break
        if size > r.limit:
            return None
    return size, ext


def carve_bmp(r):
    h = r.at(0, 30)
    if len(h) < 30:
        return None
    size, off, dib = u32(h, 2), u32(h, 10), u32(h, 14)
    if dib not in (12, 40, 52, 56, 64, 108, 124) or not (26 <= off < size) or size < 64 or size > r.limit:
        return None
    if dib >= 40 and (u16(h, 26) != 1 or u16(h, 28) not in (1, 4, 8, 16, 24, 32)):
        return None
    return size, 'bmp'


def carve_7z(r):
    h = r.at(0, 32)
    if len(h) < 32 or zlib.crc32(h[12:32]) != u32(h, 8):
        return None
    size = 32 + u64(h, 12) + u64(h, 20)
    if size > r.limit:
        return None
    return size, '7z'


def carve_ole(r):
    h = r.at(0, 512)
    if len(h) < 512:
        return None
    shift = u16(h, 30)
    if shift not in (9, 12):
        return None
    ss = 1 << shift
    num_fat = u32(h, 44)
    if num_fat == 0 or num_fat > 100000:
        return None
    difat = [u32(h, 76 + 4 * i) for i in range(109)]
    nxt, num_difat = u32(h, 68), u32(h, 72)
    per = ss // 4
    for _ in range(min(num_difat, 10000)):
        if nxt >= 0xFFFFFFFA:
            break
        d = r.at((nxt + 1) * ss, ss)
        if len(d) < ss:
            return None
        vals = struct.unpack('<%dI' % per, d)
        difat.extend(vals[:-1])
        nxt = vals[-1]
    fat_secs = [s for s in difat if s < 0xFFFFFFFA][:num_fat]
    max_used = -1
    for idx, fs in enumerate(fat_secs):
        d = r.at((fs + 1) * ss, ss)
        if len(d) < ss:
            return None
        vals = struct.unpack('<%dI' % per, d)
        for j in range(per - 1, -1, -1):
            if vals[j] != 0xFFFFFFFF:
                max_used = max(max_used, idx * per + j)
                break
    if max_used < 0:
        return None
    size = (max_used + 2) * ss
    if size > r.limit:
        return None
    blob = r.at(0, min(size, 32 * MB))
    ext = 'ole'
    for nm, e in (('WordDocument', 'doc'), ('Workbook', 'xls'), ('PowerPoint Document', 'ppt'),
                  ('__substg1.0_', 'msg'), ('Book', 'xls')):
        if nm.encode('utf-16-le') in blob:
            ext = e
            break
    return size, ext


_TIFF_TSZ = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8}


def carve_tiff(r):
    h = r.at(0, 8)
    e = '<' if h[:2] == b'II' else '>'
    U16 = lambda b, o: struct.unpack_from(e + 'H', b, o)[0]
    U32 = lambda b, o: struct.unpack_from(e + 'I', b, o)[0]
    first_ifd = U32(h, 4)
    if first_ifd < 8 or first_ifd > 64 * MB:
        return None
    end = 8
    make = b''
    dng = False
    queue = [first_ifd]
    seen = set()
    ifds = 0
    has_image = False
    while queue and ifds < 64:
        o = queue.pop()
        if o in seen or o < 8 or o >= r.limit:
            continue
        seen.add(o)
        nb = r.at(o, 2)
        if len(nb) < 2:
            continue
        n = U16(nb, 0)
        if n == 0 or n > 600:
            if ifds == 0:
                return None
            continue
        ifds += 1
        ents = r.at(o + 2, n * 12 + 4)
        if len(ents) < n * 12 + 4:
            return None
        end = max(end, o + 2 + n * 12 + 4)
        vals = {}
        for k in range(n):
            tag, typ, cnt = struct.unpack_from(e + 'HHI', ents, k * 12)
            tsz = _TIFF_TSZ.get(typ)
            if not tsz:
                continue
            total = tsz * cnt
            if total > 4:
                voff = U32(ents, k * 12 + 8)
                if voff + total <= r.limit:
                    end = max(end, voff + total)
                raw = r.at(voff, total) if tag in (0x111, 0x117, 0x144, 0x145, 0x14A, 0x10F) and total <= 1 * MB else b''
            else:
                raw = ents[k * 12 + 8: k * 12 + 8 + total]
            if tag == 0x100:
                has_image = True
            if tag == 0xC612:
                dng = True
            if tag == 0x10F:
                make = raw
            if tag in (0x111, 0x117, 0x144, 0x145, 0x201, 0x202, 0x14A, 0x8769) and raw:
                if typ == 3:
                    vals[tag] = list(struct.unpack_from(e + '%dH' % (len(raw) // 2), raw, 0))
                elif typ in (4, 13):
                    vals[tag] = list(struct.unpack_from(e + '%dI' % (len(raw) // 4), raw, 0))
        for a, b in ((0x111, 0x117), (0x144, 0x145), (0x201, 0x202)):
            if a in vals and b in vals:
                for off, ln in zip(vals[a], vals[b]):
                    if off + ln <= r.limit:
                        end = max(end, off + ln)
        queue.extend(vals.get(0x14A, []))
        queue.extend(vals.get(0x8769, []))
        nxt = U32(ents, n * 12)
        if nxt:
            queue.append(nxt)
    if not has_image or end < 256:
        return None
    if r.at(8, 2) == b'CR':
        ext = 'cr2'
    elif dng:
        ext = 'dng'
    elif make.upper().startswith(b'NIKON'):
        ext = 'nef'
    elif make.upper().startswith(b'SONY'):
        ext = 'arw'
    elif make.upper().startswith(b'CANON'):
        ext = 'cr2'
    elif make.upper().startswith(b'PENTAX'):
        ext = 'pef'
    else:
        ext = 'tif'
    return end, ext


_MP3_BR = {
    (3, 1): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    (3, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    (3, 3): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    (2, 3): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    (2, 1): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_MP3_SR = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def mp3_frame_len(b):
    if len(b) < 4 or b[0] != 0xFF or (b[1] & 0xE0) != 0xE0:
        return 0
    ver = (b[1] >> 3) & 3
    layer = 4 - ((b[1] >> 1) & 3)     # 1, 2, 3 (4 = reserved)
    bri = b[2] >> 4
    sri = (b[2] >> 2) & 3
    pad = (b[2] >> 1) & 1
    if ver == 1 or layer == 4 or bri in (0, 15) or sri == 3:
        return 0
    key_layer = {1: 3, 2: 2, 3: 1}[layer]           # table keys use the raw layer bits
    br = _MP3_BR[(3 if ver == 3 else 2, key_layer)][bri] * 1000
    sr = _MP3_SR[ver][sri]
    if layer == 1:
        return (12 * br // sr + pad) * 4
    if layer == 3 and ver != 3:
        return 72 * br // sr + pad
    return 144 * br // sr + pad


def carve_mp3(r):
    h = r.at(0, 10)
    if len(h) < 10 or any(c >= 0x80 for c in h[6:10]):
        return None
    tag = (h[6] << 21) | (h[7] << 14) | (h[8] << 7) | h[9]
    pos = 10 + tag + (10 if h[5] & 0x10 else 0)
    # some encoders pad past the declared tag size with zeros
    for _ in range(4096):
        if r.at(pos, 1) == b'\x00':
            pos += 1
        else:
            break
    frames = 0
    while pos < r.limit:
        fl = mp3_frame_len(r.at(pos, 4))
        if fl < 24:
            break
        pos += fl
        frames += 1
    if frames < 8:
        return None
    if r.at(pos, 3) == b'TAG':
        pos += 128
    return min(pos, r.limit), 'mp3'


def _ebml_vint(b, p):
    if p >= len(b) or b[p] == 0:
        return None, 0, False
    first = b[p]
    ln = 1
    mask = 0x80
    while not (first & mask):
        mask >>= 1
        ln += 1
    if p + ln > len(b):
        return None, 0, False
    val = first & (mask - 1)
    ones = val == mask - 1
    for k in range(1, ln):
        val = (val << 8) | b[p + k]
        ones = ones and b[p + k] == 0xFF
    return val, ln, ones


def carve_mkv(r):
    b = r.at(0, 256)
    size, ln, unk = _ebml_vint(b, 4)
    if size is None or unk or size > 200:
        return None
    hdr_end = 4 + ln + size
    if b[hdr_end:hdr_end + 4] != b'\x18\x53\x80\x67':
        return None
    seg, ln2, unk2 = _ebml_vint(b, hdr_end + 4)
    if seg is None or unk2:
        return None
    total = hdr_end + 4 + ln2 + seg
    if total > r.limit:
        return None
    return total, ('webm' if b'webm' in b[:hdr_end] else 'mkv')


# (regex, carver, max size)
CARVERS = [
    (rb'\xff\xd8\xff', carve_jpeg, 100 * MB),
    (rb'\x89PNG\r\n\x1a\n', carve_png, 200 * MB),
    (rb'GIF8[79]a', carve_gif, 64 * MB),
    (rb'%PDF-', carve_pdf, 512 * MB),
    (rb'PK\x03\x04', carve_zip, 2 * GB),
    (rb'\x00\x00[\x00-\xff]{2}ftyp', carve_mp4, 64 * GB),
    (rb'RIFF[\x00-\xff]{4}(?:WAVE|AVI |WEBP)', carve_riff, 8 * GB),
    (rb'BM[\x00-\xff]{4}\x00\x00\x00\x00', carve_bmp, 256 * MB),
    (rb'7z\xbc\xaf\x27\x1c', carve_7z, 64 * GB),
    (rb'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1', carve_ole, 1 * GB),
    (rb'II\*\x00|MM\x00\*', carve_tiff, 300 * MB),
    (rb'ID3[\x02-\x04]\x00', carve_mp3, 300 * MB),
    (rb'\x1a\x45\xdf\xa3', carve_mkv, 64 * GB),
]
_SIG_RE = re.compile(b'|'.join(b'(' + p + b')' for p, _, _ in CARVERS), re.S)
_FIRST_BYTES = frozenset(b'\xff\x89G%P\x00RB7\xd0IM\x1a')


_FILE_RE = re.compile(rb'FILE0\x00[\x03\x09]\x00')
_BOOT_FIRST = frozenset(b'\xeb\xe9')
_DATED = {'jpg', 'heic', 'avif', 'tif', 'cr2', 'nef', 'arw', 'dng', 'pef', 'mp4', 'mov', 'm4v', '3gp', 'cr3'}


def _byte_table(chars):
    t = bytearray(256)
    for c in chars:
        t[c] = 1
    return bytes(t)


_T_FIRST = _byte_table(set(_FIRST_BYTES) - {0})
_T_FIRST_BOOT = _byte_table((set(_FIRST_BYTES) - {0}) | set(_BOOT_FIRST))
_T_ZERO = _byte_table([0])
_T_F = _byte_table(b'f')
_ONE_RE = re.compile(b'\x01')


class _Prefetch:
    """Reads the deep-scan chunks one step ahead on its own thread. The drive is kept busy while
    the previous chunk is searched, so it streams instead of stopping and starting (faster, and
    gentler on an old drive). Chunks stay in the source's read-ahead cache until searched, so
    look-ups inside them never move the drive head."""

    DEPTH = 4

    def __init__(self, src, jobs, cancel):
        self.src = src
        self.jobs = jobs
        self.cancel = cancel
        self.q = queue.Queue(self.DEPTH)
        self.stop = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        self.src.bg_threads.add(threading.get_ident())
        try:
            jobs = self.jobs
            prev = None
            for k, (pos, n) in enumerate(jobs):
                if self.cancel.is_set() or self.stop.is_set():
                    prev = None
                    break
                # Each chunk needs the first 64 bytes of what follows. When the next chunk follows on
                # directly they're taken from it, so nothing is ever read twice or out of order.
                nxt_contig = k + 1 < len(jobs) and jobs[k + 1][0] == pos + n
                data = self.src.read(pos, n if nxt_contig else n + 64)
                with self.src.ra_lock:
                    self.src.ra[pos] = data
                if prev is not None:
                    ppos, pn, pdata = prev
                    if not self._put(ppos, pn, pdata + data[:64]):
                        prev = None
                        break
                prev = (pos, n, data) if nxt_contig else None
                if not nxt_contig and not self._put(pos, n, data):
                    break
            if prev is not None:
                self._put(prev[0], prev[1], prev[2])
        finally:
            self.src.bg_threads.discard(threading.get_ident())
            self.q.put(None)

    def _put(self, pos, n, data):
        with self.src.ra_lock:
            self.src.ra[pos] = data
        while not self.cancel.is_set() and not self.stop.is_set():
            try:
                self.q.put((pos, n, data), timeout=0.5)
                return True
            except queue.Full:
                pass
        return False

    def __iter__(self):
        while True:
            try:
                job = self.q.get(timeout=0.5)
            except queue.Empty:
                if self.cancel.is_set():
                    return
                continue
            if job is None:
                return
            yield job

    def done_with(self, pos):
        with self.src.ra_lock:
            self.src.ra.pop(pos, None)

    def close(self):
        self.stop.set()
        self.t.join(timeout=30)
        with self.src.ra_lock:
            self.src.ra.clear()


class Carver:
    CHUNK = 8 * MB

    def __init__(self, src, regions, known_starts, emit, progress, cancel, mft=None, boot=None, btrfs=None):
        self.src = src
        self.regions = regions          # [(start, length, align, align_base)]
        self.known = known_starts       # {offset: length} already found by the filesystem scan
        self.emit = emit
        self.progress = progress
        self.cancel = cancel
        self.mft = mft                  # collects old NTFS file records (recovery after a format)
        self.boot = boot                # collects boot sectors (lost partitions)
        self.btrfs = btrfs              # collects old btrfs file-tree leaves
        self.count = 0
        self.counts = {}

    def _jobs(self):
        for start, length, align, align_base in self.regions:
            pos, end = start, start + length
            while pos < end:
                n = min(self.CHUNK, end - pos)
                yield pos, n, align, align_base
                pos += n

    def run(self):
        total = sum(r[1] for r in self.regions) or 1
        jobs = list(self._jobs())
        info = {p: (a, b) for p, n, a, b in jobs}
        pf = _Prefetch(self.src, [(p, n) for p, n, _, _ in jobs], self.cancel)
        done = 0
        skip_until = -1
        table = _T_FIRST_BOOT if self.boot is not None else _T_FIRST
        try:
            for pos, n, data in pf:
                if self.cancel.is_set():
                    return
                align, align_base = info[pos]
                if not data:
                    pf.done_with(pos)
                    continue
                if len(data) < n + 8:
                    data = data + bytes(n + 8 - len(data))
                f0 = (-pos) % 512
                if self.mft is not None:
                    col = data[f0:n:512]
                    k = col.find(b'F')
                    while k >= 0:
                        r = f0 + k * 512
                        if _FILE_RE.match(data, r):
                            rs = 4096 if data[r + 6] == 9 else 1024
                            raw = data[r:r + rs]
                            if len(raw) < rs:
                                raw = self.src.read(pos + r, rs)
                            self.mft.add(pos + r, raw)
                        k = col.find(b'F', k + 1)
                if self.btrfs is not None:
                    self.btrfs.scan_chunk(data, pos, n)
                if self.boot is not None:
                    self._linux_superblocks(data, pos, n)
                # Files only ever start on a sector/cluster boundary, so look only there. The likely
                # starts are picked out with whole-chunk byte operations (C speed) instead of a Python
                # loop over every sector: an allowed first byte, or 00 00 .. .. 'f' for MP4/MOV.
                first = (align_base - pos) % align
                step = align
                firsts = data[first:n:step]
                cnt = len(firsts)
                if not cnt:
                    pf.done_with(pos)
                    done += n
                    self.progress(done / total, done, total)
                    continue
                mask = int.from_bytes(firsts.translate(table), 'big')
                z = int.from_bytes(firsts.translate(_T_ZERO), 'big')
                if z:
                    z &= int.from_bytes(data[first + 1:n + 1:step][:cnt].translate(_T_ZERO), 'big')
                    if z:
                        z &= int.from_bytes(data[first + 4:n + 4:step][:cnt].translate(_T_F), 'big')
                        mask |= z
                if mask:
                    for mm in _ONE_RE.finditer(mask.to_bytes(cnt, 'big')):
                        k = mm.start()
                        b = firsts[k]
                        rel = first + k * step
                        if b in _BOOT_FIRST and self.boot is not None:
                            if data[rel + 3:rel + 11] in (b'NTFS    ', b'EXFAT   ') or \
                                    data[rel + 82:rel + 90] == b'FAT32   ' or data[rel + 54:rel + 59] in (b'FAT16', b'FAT12'):
                                sec = data[rel:rel + 512]
                                if len(sec) < 512:
                                    sec = self.src.read(pos + rel, 512)
                                self.boot.add(pos + rel, sec)
                            continue
                        off = pos + rel
                        if off < skip_until:
                            continue
                        m = _SIG_RE.match(data, rel)
                        if not m:
                            continue
                        if off in self.known:
                            skip_until = off + self.known[off]
                            continue
                        idx = m.lastindex - 1
                        res = self._try(off, idx)
                        res = self._fragments(off, idx, res, align)
                        if res:
                            ln, ext, cond, runs = res
                            self.count += 1
                            self.counts[ext] = self.counts.get(ext, 0) + 1
                            item = FoundFile(f'{self.counts[ext]:05d}.{ext}', f'Deep scan/{ext.upper()}', ln,
                                             runs, 'Deep scan', cond)
                            item.note = f'Found at byte offset {off:,}'
                            if ext in _DATED:
                                try:
                                    import extras
                                    extras.name_by_date(self.src, item)
                                except Exception:
                                    pass
                            self.emit(item)
                            skip_until = off + runs[0][1]       # the first piece (a second one lies further on)
                pf.done_with(pos)
                done += n
                self.progress(done / total, done, total)
        finally:
            pf.close()

    def _linux_superblocks(self, data, pos, n):
        """ext2/3/4 and btrfs superblocks (and their backups) -> candidate lost partitions."""
        f0 = (-pos) % 512                       # superblocks always start on a 512-byte boundary
        col = data[f0 + 0x40:n + 0x40:512]
        k = col.find(b'_')
        while k >= 0:
            sb = f0 + k * 512
            if data[sb + 0x40:sb + 0x48] == b'_BHRfS_M':
                sb_off = pos + sb
                for mirror in (0x10000, 64 * MB, 256 * GB):
                    if sb_off - mirror >= 0:
                        self.boot.add_linux(sb_off - mirror, 'btrfs', mirror)
            k = col.find(b'_', k + 1)
        col = data[f0 + 56:n + 56:512]
        k = col.find(b'\x53')
        while k >= 0:
            sb = f0 + k * 512
            k = col.find(b'\x53', k + 1)
            if data[sb + 57] != 0xef or sb + 100 > len(data):
                continue
            off = pos + sb
            lbs = u32(data, sb + 24)
            if lbs <= 6 and u32(data, sb + 76) <= 1 and u32(data, sb + 32) == 8 * (1024 << lbs) \
                    and u32(data, sb + 40) and u16(data, sb + 58) <= 3:
                bsz = 1024 << lbs
                g = u16(data, sb + 90)
                bpg = u32(data, sb + 32)
                if g == 0:
                    start = off - 1024
                else:
                    start = off - g * bpg * bsz - (1024 if bsz == 1024 else 0)
                if start >= 0:
                    self.boot.add_linux(start, 'ext', bytes(data[sb:sb + 1024]))

    FRAG_KINDS = {0: 'jpg', 1: 'png', 4: 'zip', 5: 'mp4'}       # CARVERS index -> type the checker knows
    VERIFY_MAX = 64 * MB
    REPAIR_BUDGET = 120.0           # seconds of reassembly per scan; after that files are only checked

    def _fragments(self, off, idx, res, align):
        """Check a carved file (or one that failed to carve) for being split in two, and put it
        back together. Returns (length, ext, condition, runs) or None."""
        kind = self.FRAG_KINDS.get(idx)
        if res:
            ln, ext, cond = res
            runs = [(off, ln)]
            if kind is None or (kind == 'mp4') or ln > self.VERIFY_MAX:
                return ln, ext, cond, runs
        elif kind not in ('png', 'mp4', 'zip', 'jpg'):
            return None
        else:
            ln, ext, cond, runs = None, kind, None, None
        spent = getattr(self, '_frag_spent', 0.0)
        t0 = time.time()
        try:
            import fragments
            fr = fragments.check(self.src, off, kind if kind != 'zip' or not res else ext, ln, align,
                                 window=fragments.WINDOW if spent < self.REPAIR_BUDGET else 0)
        except Exception:
            fr = None
        self._frag_spent = spent + (time.time() - t0)
        if fr:
            rel, c2 = fr
            if c2 == 'Good':
                return (ln or rel[0][1]), ext, cond or 'Carved (assumes unfragmented)', runs or [(off, rel[0][1])]
            total = sum(n for _, n in rel)
            return total, ext, c2, [(off + o, n) for o, n in rel]
        if res:
            if kind in ('jpg', 'png'):
                return ln, ext, 'Damaged or fragmented (could not put it back together)', runs
            return ln, ext, cond, runs
        return None

    def _try(self, off, idx):
        _, fn, max_size = CARVERS[idx]
        limit = min(max_size, (self.src.size - off) if self.src.size else max_size)
        if limit <= 16:
            return None
        try:
            res = fn(_Reader(self.src, off, limit))
        except (struct.error, IndexError, ValueError, ZeroDivisionError, KeyError):
            return None
        if not res:
            return None
        ln, ext = res[0], res[1]
        cond = res[2] if len(res) > 2 else 'Carved (assumes unfragmented)'
        if ln < 16 or ln > limit:
            return None
        return ln, ext, cond


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------

class ScanOptions:
    def __init__(self, deleted=True, deep=True, free_only=True, include_existing=False):
        self.deleted = deleted
        self.deep = deep
        self.free_only = free_only
        self.include_existing = include_existing


class Scanner:
    """Runs a full scan. Callbacks are called from the worker thread."""

    def __init__(self, src, opts, on_item, on_status, on_progress, on_remove=None):
        self.src = src
        self.opts = opts
        self.on_remove = on_remove
        self.on_item = on_item
        self.on_status = on_status
        self.on_progress = on_progress
        self.cancel = threading.Event()
        self.volume = None
        self.fs_desc = ''
        self.partitions = []
        self.summary = ''

    def run(self):
        me = threading.get_ident()
        self.src.bg_threads.add(me)
        try:
            return self._run()
        finally:
            self.src.bg_threads.discard(me)

    def _run(self):
        try:
            return self._run2()
        finally:
            _HEAD_DEFER.q = None

    def _run2(self):
        found = {'Deleted': 0, 'Deep scan': 0, 'Existing': 0}
        known = {}

        carved = {}
        waiting = []
        _HEAD_DEFER.q = waiting

        def flush_heads():
            """Do the deferred 'does the data still start right' checks in disk order, then list them."""
            if not waiting:
                return
            batch = sorted(waiting, key=lambda it: it.start if it.start is not None else -1)
            waiting.clear()
            for it in batch:
                src, off = it.__dict__.pop('_head')
                if self.cancel.is_set():          # stopping: list them without the extra reads
                    emit_now(it)
                    continue
                try:
                    head = src.read(off, 64)
                except Exception:
                    head = b''
                _VolumeBase.check_condition(None, src, it, head=head)
                emit_now(it)

        def emit(item):
            if '_head' in item.__dict__:
                waiting.append(item)
                if len(waiting) >= 20000:
                    flush_heads()
                return
            emit_now(item)

        def verify_layout(item):
            """A deleted file whose clusters had to be guessed: check the guess against the data,
            and try the straight-run layout too."""
            if item.condition != 'Fragmented (best guess)' or not item.size or item.size > 64 * MB:
                return
            ext = (item.ext or '').lower()
            if ext not in ('jpg', 'jpeg', 'png', 'zip', 'docx', 'xlsx', 'pptx'):
                return
            try:
                import fragments
                src = self.src
                data = b''.join(src.read(o, n) if o is not None else bytes(n) for o, n in item.runs)
                ok = fragments.valid(ext, data)
                if ok:
                    item.condition = 'Good (layout checked)'
                    return
                straight = src.read(item.start, item.size)
                if ok is False and fragments.valid(ext, straight):
                    item.runs = [(item.start, item.size)]
                    item.condition = 'Good (layout checked)'
            except Exception:
                pass

        def emit_now(item):
            verify_layout(item)
            if item.inline is None:
                item.unreadable = self.src.unreadable_in(item.runs)
            st = item.start
            if item.method == 'Deep scan':
                carved[st] = item
            elif st is not None and st in carved and carved[st].size == item.size:
                dup = carved.pop(st)      # the same file turned up with its real name: drop the numbered copy
                found['Deep scan'] -= 1
                if self.on_remove:
                    self.on_remove(dup)
            found[item.method] = found.get(item.method, 0) + 1
            if item.method == 'Deleted' and item.condition == 'Good':
                known[item.start] = item.size
            self.on_item(item)

        self.on_status('Reading drive layout...')
        try:
            self.partitions = find_partitions(self.src)
        except Exception as ex:
            self.partitions = []
            self.fs_desc = f'Unreadable ({ex})'
        parts_list = self.partitions
        vols = [p for p in parts_list if p.volume]
        multi = len(parts_list) > 1
        if not parts_list:
            self.fs_desc = self.fs_desc or 'No filesystem found'
        elif not multi:
            self.fs_desc = parts_list[0].kind
        else:
            self.fs_desc = f'{len(parts_list)} partitions (' + ', '.join(p.kind for p in parts_list) + ')'
        self.volume = vols[0].volume if vols else None

        if self.opts.deleted or self.opts.include_existing:
            if not vols:
                self.on_status(f'Filesystem: {self.fs_desc} - skipping to deep scan')
            for p in vols:
                if self.cancel.is_set():
                    break
                v = p.volume
                prefix = f'/Partition {p.index} ({p.kind})' if multi else ''
                held = []

                def emit_p(item, prefix=prefix, held=held, remap=p.remap):
                    if remap and item.runs:
                        item.runs = remap(item.runs)
                    if prefix:
                        item.folder = prefix + ('' if item.folder == '/' else item.folder)
                    if item.special or _in_recycle(item.folder):
                        held.append(item)          # Recycle Bin / thumbnail caches: processed below
                    else:
                        emit(item)

                self.on_progress(None, 0, 0)
                where = f'partition {p.index} ' if multi else ''
                self.on_status(f'Looking for deleted files on {where}({v.kind})...')
                try:
                    v.walk(emit_p, self.opts.include_existing, self.cancel, self.on_status)
                except Exception as ex:
                    self.on_status(f'Could not read the {v.kind} folders ({ex}) - continuing')
                flush_heads()
                if held and not self.cancel.is_set():
                    self._special_files(held, emit)
                    flush_heads()

        if self.opts.deep and not self.cancel.is_set():
            size = self.src.size
            if vols and self.opts.free_only:
                self.on_status('Mapping free space...')
                regions = []
                for p in vols:
                    v = p.volume
                    # check every sector boundary, not just this filesystem's clusters: files left over
                    # from before a reformat sit on the *old* cluster size
                    fr = v.free_runs()
                    if p.remap:
                        fr = p.remap(fr)
                    regions += [(o, l, 512, 0) for o, l in fr if o is not None]
                # also everything outside readable filesystems: unpartitioned space,
                # deleted or damaged partitions
                pos = 0
                for a, b in sorted((p.offset, p.offset + p.size) for p in vols):
                    if a > pos:
                        regions.append((pos, a - pos, 512, 0))
                    pos = max(pos, b)
                if size > pos:
                    regions.append((pos, size - pos, 512, 0))
                regions.sort()
                where = 'free space'
            else:
                regions = [(0, size, 512, 0)] if size else []
                where = 'whole drive'
            total = sum(r[1] for r in regions)
            self.on_status(f'Deep scan of {where} ({human_size(total)})...')
            import extras
            exclude = []
            for p in vols:
                if isinstance(p.volume, NtfsVolume):
                    exclude += p.volume.mft_device_ranges()
            mft = extras.MftCollector(self.src, exclude)
            boot = extras.BootCollector()
            btr = None
            try:
                import linuxfs
                bvols = [p.volume for p in vols if isinstance(p.volume, linuxfs.BtrfsVolume) and not p.remap]
                if bvols:
                    btr = linuxfs.BtrfsLeafCollector(bvols)
            except ImportError:
                pass
            carver = Carver(self.src, regions, known, emit, self.on_progress, self.cancel, mft=mft, boot=boot,
                            btrfs=btr)
            carver.run()
            if btr and not self.cancel.is_set():
                self.on_status('Rebuilding deleted btrfs files from old file-tree copies...')
                try:
                    btr.finish(emit)
                    flush_heads()
                except Exception as ex:
                    self.on_status(f'Could not use old btrfs file-tree copies ({ex})')
            if not self.cancel.is_set():
                self.on_progress(None, 0, 0)
                # partitions that were deleted, or whose table entry was lost
                try:
                    lost = boot.volumes(self.src, parts_list)
                except Exception:
                    lost = []
                for k, lv in enumerate(lost, 1):
                    if self.cancel.is_set():
                        break
                    try:
                        extras.walk_lost_partition(lv, k, emit, self.cancel, self.on_status)
                        flush_heads()
                    except Exception as ex:
                        self.on_status(f'Could not read lost partition {k} ({ex})')
                self.lost_partitions = lost
                # NTFS file tables from before a quick format
                try:
                    mft.drop_ranges([r for lv in lost if isinstance(lv, NtfsVolume) for r in lv.mft_device_ranges()])
                    bases = sorted({0} | {p.offset for p in parts_list} | {lv.vol_off for lv in lost})
                    cur = [p.volume for p in vols] + lost
                    mft.finish(bases, cur, known, emit, self.on_status)
                except Exception as ex:
                    self.on_status(f'Could not rebuild the old file table ({ex})')
        flush_heads()

        parts = []
        if self.opts.deleted:
            parts.append(f"{found['Deleted']} deleted entries")
        if self.opts.deep:
            parts.append(f"{found['Deep scan']} files from deep scan")
        for key, label in (('Recycle Bin', 'from the Recycle Bin'), ('Thumbnail', 'thumbnails'),
                           ('Before format', 'from before a format'), ('Lost partition', 'from lost partitions')):
            if found.get(key):
                parts.append(f'{found[key]} {label}')
        if found.get('Snapshot'):
            parts.append(f"{found['Snapshot']} deleted files still in snapshots")
        if self.opts.include_existing:
            parts.append(f"{found['Existing']} existing files")
        lost_maps = sum(getattr(p.volume, 'names_without_data', 0) for p in getattr(self, 'partitions', []) if p.volume)
        if lost_maps:
            parts.append(f'{lost_maps} deleted names whose data location is gone (try a deep scan)')
        if getattr(self, 'lost_partitions', None):
            self.fs_desc += f" + {len(self.lost_partitions)} lost partition(s) found"
        state = 'Stopped' if self.cancel.is_set() else 'Done'
        self.summary = f'{state}. Found ' + ', '.join(parts) + f'. Filesystem: {self.fs_desc}.'
        if self.src.bad_reads:
            self.summary += f' {self.src.bad_reads} unreadable blocks were skipped.'
        return self.summary


    def _special_files(self, held, emit):
        import extras
        thumbs = [i for i in held if i.name.lower().endswith('.db') and
                  (i.name.lower().startswith(('thumbcache_', 'iconcache_')) or i.name.lower() == 'thumbs.db')]
        if thumbs:
            self.on_status(f'Extracting pictures from {len(thumbs)} thumbnail cache file(s)...')
            for t in thumbs:
                if self.cancel.is_set():
                    return
                try:
                    for sub in extras.extract_thumbnails(self.src, t):
                        emit(sub)
                except Exception:
                    pass
        rb = [i for i in held if _in_recycle(i.folder)]
        rest = [i for i in held if not _in_recycle(i.folder)]     # (decide before renaming)
        if rb:
            self.on_status('Matching Recycle Bin files to their original names...')
            try:
                rb = extras.map_recycle_bin(self.src, rb)
            except Exception:
                pass
        for it in rb + rest:
            if it.method == 'Existing' and it.special:
                continue                   # only needed for processing, not deleted
            if it.name.lower().startswith('$i') and it.method == 'Existing':
                continue
            emit(it)


def _in_recycle(folder):
    f = folder.lower()
    return '/$recycle.bin' in f or '/recycler' in f


# --------------------------------------------------------------------------------------
# Writing results
# --------------------------------------------------------------------------------------

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {f'COM{i}' for i in range(1, 10)} | {f'LPT{i}' for i in range(1, 10)}


def safe_component(name):
    name = _BAD_CHARS.sub('_', name).rstrip(' .') or '_'
    if name.split('.')[0].upper() in _RESERVED:
        name = '_' + name
    return name[:180]


def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    k = 2
    while os.path.exists(f'{base} ({k}){ext}'):
        k += 1
    return f'{base} ({k}){ext}'


def recover_item(src, item, dest_root, keep_folders=True):
    parts = [safe_component(p) for p in item.folder.strip('/').split('/') if p] if keep_folders else []
    folder = os.path.join(dest_root, *parts)
    os.makedirs(folder, exist_ok=True)
    path = unique_path(os.path.join(folder, safe_component(item.name)))
    try:
        with open(path, 'wb') as f:
            for d in item.iter_data(src):
                f.write(d)
    except BaseException:
        try:                      # don't leave a half-written file behind (e.g. disk full)
            os.remove(path)
        except OSError:
            pass
        raise
    if item.mtime:
        try:
            t = item.mtime.timestamp()
            os.utime(path, (t, t))
        except (OSError, ValueError, OverflowError):
            pass
    return path


class AutoSaver:
    """Saves files on a background thread while the scan keeps going. The scan never waits.
    Waiting files are taken in disk order (nearest after the last one first), so a hard
    drive's heads sweep across the disk instead of jumping back and forth."""

    def __init__(self, src, out_root, on_saved, on_error):
        self.src = src
        self.out_root = out_root
        self.on_saved = on_saved
        self.on_error = on_error
        self.cv = threading.Condition()
        self.waiting = []               # sorted by (start, id)
        self.closed = False
        self.saved = 0
        self.bytes = 0
        self.failed = []
        self.damaged = []
        self.records = []
        self.stopped = False
        self.head = 0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def add(self, item):
        if self.stopped:
            return
        import bisect
        with self.cv:
            bisect.insort(self.waiting, ((item.start if item.start is not None else -1), item.id, item))
            self.cv.notify()

    def pending(self):
        return len(self.waiting)

    def finish(self):
        with self.cv:
            self.closed = True
            self.cv.notify()

    def _next(self):
        import bisect
        with self.cv:
            while not self.waiting and not self.closed:
                self.cv.wait(0.5)
            if not self.waiting:
                return None
            k = bisect.bisect_left(self.waiting, (self.head, -1))
            if k >= len(self.waiting):
                k = 0                   # wrap around to the start of the disk
            return self.waiting.pop(k)[2]

    def _run(self):
        while True:
            item = self._next()
            if item is None:
                return
            if self.stopped:
                continue
            try:
                item.saved_path = recover_item(self.src, item, self.out_root, keep_folders=True)
                if item.inline is None:
                    item.unreadable = self.src.unreadable_in(item.runs)
                if item.unreadable:
                    self.damaged.append(item)
                self.saved += 1
                self.bytes += item.size
                self.records.append(item)
                if item.start is not None:
                    self.head = item.start + item.size
                self.on_saved(item)
            except OSError as ex:
                if getattr(ex, 'winerror', None) in (39, 112) or ex.errno == 28:
                    self.stopped = True
                    self.on_error('The destination drive is full. Auto-recover has stopped; '
                                  'the scan is still running.')
                else:
                    self.failed.append(f'{item.folder}/{item.name}: {ex}')
            except Exception as ex:
                self.failed.append(f'{item.folder}/{item.name}: {ex}')


def write_report(out_root, items, source_label=''):
    """CSV listing what was recovered and how complete each file is."""
    import csv
    os.makedirs(out_root, exist_ok=True)
    path = os.path.join(out_root, 'FlashRescue report.csv')
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['Source', source_label])
        w.writerow([])
        w.writerow(['Saved as', 'Name', 'Original folder', 'Size (bytes)', 'Found by', 'Condition',
                    'Unreadable bytes', 'Complete', 'Modified'])
        for it in items:
            rel = os.path.relpath(it.saved_path, out_root) if it.saved_path else ''
            w.writerow([rel, it.name, it.folder, it.size, it.method, it.condition, it.unreadable,
                        'no' if it.unreadable else 'yes', it.mtime.strftime('%Y-%m-%d %H:%M:%S') if it.mtime else ''])
    return path


# --------------------------------------------------------------------------------------
# Windows drive list
# --------------------------------------------------------------------------------------

def _volume_disk_numbers(letter):
    """Physical disk numbers a drive letter lives on."""
    try:
        with open(f'\\\\.\\{letter}:', 'rb', buffering=0) as f:
            out = win_ioctl(f, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, outsize=8 + 24 * 16)
    except OSError:
        return set()
    if not out or len(out) < 8:
        return set()
    n = u32(out, 0)
    return {u32(out, 8 + 24 * k) for k in range(n) if 8 + 24 * k + 4 <= len(out)}


_BUS = {1: 'SCSI', 2: 'ATAPI', 3: 'ATA', 4: 'FireWire', 7: 'USB', 8: 'RAID', 10: 'SAS', 11: 'SATA',
        12: 'SD card', 13: 'MMC', 17: 'NVMe'}


def _disk_model(f):
    out = win_ioctl(f, IOCTL_STORAGE_QUERY_PROPERTY, struct.pack('<II4x', 0, 0), 1024)
    if not out or len(out) < 32:
        return '', ''

    def cstr(off):
        if not off or off >= len(out):
            return ''
        return out[off:out.index(b'\x00', off) if b'\x00' in out[off:] else len(out)].decode('ascii', 'replace').strip()

    model = ' '.join(x for x in (cstr(u32(out, 12)), cstr(u32(out, 16))) if x)
    return model, _BUS.get(u32(out, 28), '')


def list_drives():
    """Everything that can be scanned (Windows only):
    drive letters (one volume each) and whole physical disks (all partitions, plus
    unpartitioned space where deleted partitions may still be).
    Each entry: {path, kind ('volume'/'disk'), letter, letters, label, fs, size, type, system, model, bus}"""
    if os.name != 'nt':
        return []
    import ctypes
    import string
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    sysdrive = os.environ.get('SystemDrive', 'C:').upper()[:1]
    out = []
    disk_letters = {}
    for i, letter in enumerate(string.ascii_uppercase):
        if not (mask >> i) & 1:
            continue
        root = f'{letter}:\\'
        dtype = k32.GetDriveTypeW(root)
        if dtype not in (2, 3):     # removable, fixed
            continue
        label = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        ok = k32.GetVolumeInformationW(root, label, 261, None, None, None, fs, 261)
        total = ctypes.c_ulonglong(0)
        k32.GetDiskFreeSpaceExW(root, None, ctypes.byref(total), None)
        size = total.value
        if not size:
            try:
                rs = RawSource(f'\\\\.\\{letter}:')
                size = rs.size
                rs.close()
            except OSError:
                size = 0
        disks = _volume_disk_numbers(letter)
        for dn in disks:
            disk_letters.setdefault(dn, set()).add(letter)
        out.append({
            'path': f'\\\\.\\{letter}:',
            'kind': 'volume',
            'letter': letter,
            'letters': {letter},
            'disks': disks,
            'label': label.value if ok else '',
            'fs': fs.value if ok else 'RAW / unreadable',
            'size': size,
            'type': 'Removable' if dtype == 2 else 'Fixed',
            'system': letter == sysdrive,
        })
    for dn in range(32):
        path = f'\\\\.\\PhysicalDrive{dn}'
        try:
            with open(path, 'rb', buffering=0) as f:
                size = _win_volume_length(f)
                model, bus = _disk_model(f)
        except OSError:
            continue
        if not size:
            continue
        letters = disk_letters.get(dn, set())
        out.append({
            'path': path,
            'kind': 'disk',
            'letter': None,
            'letters': letters,
            'disks': {dn},
            'label': model or f'Disk {dn}',
            'fs': '',
            'size': size,
            'type': bus,
            'system': sysdrive in letters,
            'number': dn,
        })
    return out


def list_shadow_copies():
    """Windows 'Previous Versions' snapshots (Volume Shadow Copies) of the local drives.
    Each one can be opened and scanned like a drive: it holds the files as they were then."""
    if os.name != 'nt':
        return []
    import ctypes
    import string
    import subprocess
    ps = ("Get-CimInstance Win32_ShadowCopy | ForEach-Object { $_.DeviceObject + '|' + $_.VolumeName + '|' + "
          "$_.InstallDate.ToString('yyyy-MM-dd HH:mm') }")
    try:
        p = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps], capture_output=True,
                           timeout=30, creationflags=0x08000000)
        lines = p.stdout.decode('utf-8', 'replace').splitlines()
    except Exception:
        return []
    k32 = ctypes.windll.kernel32
    guid_to_letter = {}
    mask = k32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if (mask >> i) & 1:
            buf = ctypes.create_unicode_buffer(64)
            if k32.GetVolumeNameForVolumeMountPointW(f'{letter}:\\', buf, 64):
                guid_to_letter[buf.value.lower()] = letter
    out = []
    for line in lines:
        parts = line.strip().split('|')
        if len(parts) != 3 or not parts[0]:
            continue
        dev, vol, when = parts
        letter = guid_to_letter.get(vol.lower())
        if not letter:
            continue
        out.append({'path': dev, 'kind': 'shadow', 'letter': letter, 'letters': {letter}, 'disks': set(),
                    'label': f'{letter}: as it was on {when}', 'fs': 'NTFS', 'size': 0, 'type': 'Shadow copy',
                    'system': False, 'when': when})
    out.sort(key=lambda d: (d['letter'], d['when']), reverse=True)
    return out
