"""
Disk image files other than plain raw copies.

  VHD   (Virtual PC / Hyper-V / Windows backup)  fixed, dynamic, differencing
  VHDX  (Hyper-V, Windows 8+)                     fixed, dynamic, differencing
  VMDK  (VMware)                                  monolithic/split sparse, streamOptimized, flat, snapshots
  qcow2 (QEMU / KVM / Proxmox)                    incl. zlib/zstd compressed clusters, backing files
  VDI   (VirtualBox)                              fixed and dynamic
  E01   (EnCase / FTK / ewfacquire evidence)     multi-segment (.E01, .E02, ...), compressed chunks
  .001  split raw images (.001, .002, ...)        joined back together

open_image(path) returns a source that reads the virtual disk as if it were a raw drive; plain raw
files get an ordinary engine.RawSource. All of them are read-only.
"""

import os
import re
import struct
import threading
import uuid
import zlib
from collections import OrderedDict

import engine
from engine import MB

SECTOR = 512


class ImageError(ValueError):
    pass


class _ImageBase(engine.RawSource):
    """Shared plumbing: bad-range bookkeeping and the read-ahead cache, without RawSource.__init__."""

    kind = 'image'
    BLOCK_CACHE = 16

    def _init_common(self, path, label, size, sector_size=512):
        self.path = path
        self.label = label
        self.size = size
        self.sector_size = sector_size
        self.ALIGN = 4096
        self.is_device = False
        self.lock = threading.Lock()
        self.lock_fg = threading.Lock()
        self.f_fg = None
        self.fg_failed = True
        self.bg_threads = set()
        self.bad_reads = 0
        self.bad_starts, self.bad_ends = [], []
        self.map_path = None
        self.ra = {}
        self.ra_lock = threading.Lock()
        self._cache = OrderedDict()
        self._cache_lock = threading.Lock()
        self._files = []
        self.parent_src = None
        self.notes = []

    # files are shared by the scanner thread and the window thread: every read takes the lock
    def _open(self, p):
        f = open(p, 'rb')
        self._files.append(f)
        return f

    def _pread(self, f, off, n):
        with self.lock:
            f.seek(off)
            return f.read(n)

    def close(self):
        for f in self._files:
            try:
                f.close()
            except OSError:
                pass
        self._files = []
        if self.parent_src:
            self.parent_src.close()

    def load_rescue_map(self, map_path):
        raise ValueError('rescue maps go with raw images')

    def read(self, offset, length):
        hit = self.ra_hit(offset, length) if self.ra else None
        if hit is not None:
            return hit
        if offset >= self.size or length <= 0:
            return b''
        length = min(length, self.size - offset)
        bs = self.block_size
        out = bytearray()
        pos = offset
        end = offset + length
        while pos < end:
            b = pos // bs
            within = pos - b * bs
            n = min(bs - within, end - pos)
            blk = self._cached_block(b)
            if blk is None:
                out += self._parent_read(pos, n)
            else:
                piece = blk[within:within + n]
                if len(piece) < n:
                    piece += bytes(n - len(piece))
                out += piece
            pos += n
        return bytes(out)

    def _parent_read(self, pos, n):
        if self.parent_src is not None:
            d = self.parent_src.read(pos, n)
            return d + bytes(n - len(d))
        return bytes(n)

    def _cached_block(self, b):
        with self._cache_lock:
            if b in self._cache:
                self._cache.move_to_end(b)
                return self._cache[b]
        blk = self._block(b)
        if blk is not None and not isinstance(blk, memoryview):
            with self._cache_lock:
                self._cache[b] = blk
                if len(self._cache) > self.BLOCK_CACHE:
                    self._cache.popitem(last=False)
        return blk

    def _block(self, b):          # bytes of virtual block b, or None = "not here" (zeros / parent)
        raise NotImplementedError

    def describe(self):
        return self.label


def _find_parent(child_path, *candidates):
    """Locate a parent / backing image: absolute path, path relative to the child, or the same
    file name next to the child (images are often moved together)."""
    base = os.path.dirname(os.path.abspath(child_path))
    tried = []
    for c in candidates:
        if not c:
            continue
        c = c.replace('\\', os.sep).replace('/', os.sep)
        for p in (c, os.path.join(base, c), os.path.join(base, os.path.basename(c))):
            tried.append(p)
            if os.path.isfile(p):
                return p
    return None


# --------------------------------------------------------------------------------------
# VHD
# --------------------------------------------------------------------------------------

class VhdSource(_ImageBase):
    kind = 'VHD'

    def __init__(self, path, label=None, depth=0):
        f = open(path, 'rb')
        self._init_common(path, label or os.path.basename(path), 0)
        self._files.append(f)
        f.seek(0, 2)
        fsize = f.tell()
        foot = self._pread(f, fsize - 512, 512)
        if foot[:8] != b'conectix':
            foot = self._pread(f, fsize - 511, 511) + b'\x00'          # pre-2004 images: 511-byte footer
            if foot[:8] != b'conectix':
                foot = self._pread(f, 0, 512)                            # dynamic disks keep a copy at 0
        if foot[:8] != b'conectix':
            raise ImageError('not a VHD')
        self.f = f
        self.size = struct.unpack('>Q', foot[48:56])[0]
        self.disk_type = struct.unpack('>I', foot[60:64])[0]
        if self.disk_type == 2:                                          # fixed: raw data + footer
            self.block_size = 2 * MB
            self.kind = 'VHD (fixed)'
            return
        if self.disk_type not in (3, 4):
            raise ImageError(f'unsupported VHD type {self.disk_type}')
        dyn_off = struct.unpack('>Q', foot[16:24])[0]
        dh = self._pread(f, dyn_off, 1024)
        if dh[:8] != b'cxsparse':
            raise ImageError('VHD dynamic header missing')
        bat_off = struct.unpack('>Q', dh[16:24])[0]
        n = struct.unpack('>I', dh[28:32])[0]
        self.block_size = struct.unpack('>I', dh[32:36])[0]
        self.bat = struct.unpack(f'>{n}I', self._pread(f, bat_off, 4 * n))
        spb = self.block_size // SECTOR
        self.bitmap_bytes = -(-(-(-spb // 8)) // SECTOR) * SECTOR
        self.kind = 'VHD (dynamic)'
        if self.disk_type == 4:
            self.kind = 'VHD (differencing)'
            pname = dh[64:576].decode('utf-16-be', 'replace').rstrip('\x00')
            locs = []
            for k in range(8):
                e = dh[576 + 24 * k:600 + 24 * k]
                code, space, length, off = e[:4], *struct.unpack('>IIxxxxQ', e[4:])
                if code in (b'W2ru', b'W2ku') and length:
                    locs.append(self._pread(f, off, length).decode('utf-16-le', 'replace').rstrip('\x00'))
                elif code in (b'Wi2r', b'Wi2k') and length:
                    locs.append(self._pread(f, off, length).decode('latin-1', 'replace').rstrip('\x00'))
            pp = _find_parent(path, *locs, pname)
            if pp and depth < 16:
                self.parent_src = open_image(pp, depth=depth + 1)
            else:
                self.notes.append(f'parent disk "{pname}" not found - its parts read as zeros')

    def read(self, offset, length):
        if self.disk_type == 2:
            if offset >= self.size:
                return b''
            return self._pread(self.f, offset, min(length, self.size - offset))
        return super().read(offset, length)

    def _block(self, b):
        if b >= len(self.bat) or self.bat[b] == 0xFFFFFFFF:
            return None
        base = self.bat[b] * SECTOR
        data = self._pread(self.f, base + self.bitmap_bytes, self.block_size)
        if self.disk_type == 4 and self.parent_src is not None:
            bm = self._pread(self.f, base, self.bitmap_bytes)
            if bm.count(0xFF) * 8 < self.block_size // SECTOR:      # some sectors live in the parent
                out = bytearray(data.ljust(self.block_size, b'\x00'))
                for s in range(self.block_size // SECTOR):
                    if not (bm[s >> 3] >> (7 - (s & 7))) & 1:
                        out[s * SECTOR:(s + 1) * SECTOR] = self.parent_src.read(
                            b * self.block_size + s * SECTOR, SECTOR).ljust(SECTOR, b'\x00')
                return bytes(out)
        return data


# --------------------------------------------------------------------------------------
# VHDX
# --------------------------------------------------------------------------------------

_G = lambda s: uuid.UUID(s).bytes_le
VHDX_BAT = _G('2DC27766-F623-4200-9D64-115E9BFD4A08')
VHDX_META = _G('8B7CA206-4790-4B9A-B8FE-575F050F886E')
VHDX_FILEPARAM = _G('CAA16737-FA36-4D43-B3B6-33F0AA44E76B')
VHDX_VSIZE = _G('2FA54224-CD1B-4876-B211-5DBED83BF4B8')
VHDX_LSS = _G('8141BF1D-A96F-4709-BA47-F233A8FAAB5F')
VHDX_PARENT = _G('A8D35F2D-B30B-454D-ABF7-D3D84834AB0C')


class VhdxSource(_ImageBase):
    kind = 'VHDX'

    def __init__(self, path, label=None, depth=0):
        self._init_common(path, label or os.path.basename(path), 0)
        f = self.f = self._open(path)
        if self._pread(f, 0, 8) != b'vhdxfile':
            raise ImageError('not a VHDX')
        heads = []
        for off in (64 * 1024, 128 * 1024):
            h = self._pread(f, off, 4096)
            if h[:4] == b'head':
                heads.append((struct.unpack('<Q', h[8:16])[0], h))
        if not heads:
            raise ImageError('VHDX headers damaged')
        head = max(heads)[1]
        if head[48:64] != bytes(16):
            self.notes.append('the image was not closed cleanly (unreplayed log) - the newest writes may be missing')
        reg = None
        for off in (192 * 1024, 256 * 1024):
            r = self._pread(f, off, 64 * 1024)
            if r[:4] == b'regi':
                reg = r
                break
        if reg is None:
            raise ImageError('VHDX region table damaged')
        regions = {}
        for k in range(struct.unpack('<I', reg[8:12])[0]):
            e = reg[16 + 32 * k:48 + 32 * k]
            regions[e[:16]] = struct.unpack('<QI', e[16:28])
        if VHDX_BAT not in regions or VHDX_META not in regions:
            raise ImageError('VHDX regions missing')
        moff, mlen = regions[VHDX_META]
        meta = self._pread(f, moff, mlen)
        if meta[:8] != b'metadata':
            raise ImageError('VHDX metadata damaged')
        items = {}
        for k in range(struct.unpack('<H', meta[10:12])[0]):
            e = meta[32 + 32 * k:64 + 32 * k]
            io, il = struct.unpack('<II', e[16:24])
            items[e[:16]] = meta[io:io + il]
        self.block_size, flags = struct.unpack('<II', items[VHDX_FILEPARAM][:8])
        self.size = struct.unpack('<Q', items[VHDX_VSIZE][:8])[0]
        self.sector_size = struct.unpack('<I', items[VHDX_LSS][:4])[0] if VHDX_LSS in items else 512
        self.chunk_ratio = (2 ** 23 * self.sector_size) // self.block_size
        boff, blen = regions[VHDX_BAT]
        raw = self._pread(f, boff, blen)
        self.bat = struct.unpack(f'<{len(raw) // 8}Q', raw[:len(raw) // 8 * 8])
        self.has_parent = bool(flags & 2)
        self.kind = 'VHDX (differencing)' if self.has_parent else 'VHDX'
        if self.has_parent:
            kv = {}
            pl = items.get(VHDX_PARENT, b'')
            if len(pl) >= 20:
                cnt = struct.unpack('<H', pl[18:20])[0]
                for k in range(cnt):
                    ko, vo, kl, vl = struct.unpack('<IIHH', pl[20 + 12 * k:32 + 12 * k])
                    kv[pl[ko:ko + kl].decode('utf-16-le', 'replace')] = pl[vo:vo + vl].decode('utf-16-le', 'replace')
            pp = _find_parent(path, kv.get('absolute_win32_path'), kv.get('relative_path'),
                              kv.get('volume_path'))
            if pp and depth < 16:
                self.parent_src = open_image(pp, depth=depth + 1)
            else:
                self.notes.append('parent disk not found - its parts read as zeros')

    def _entry(self, b):
        i = b + b // self.chunk_ratio
        return self.bat[i] if i < len(self.bat) else 0

    def _block(self, b):
        e = self._entry(b)
        state, off = e & 7, (e >> 20) * MB
        if state == 6 and off:
            return self._pread(self.f, off, self.block_size)
        if state == 7 and off:                       # partially present: per-sector bitmap
            data = bytearray(self._pread(self.f, off, self.block_size).ljust(self.block_size, b'\x00'))
            chunk = b // self.chunk_ratio
            be = self.bat[(chunk + 1) * (self.chunk_ratio + 1) - 1] if \
                (chunk + 1) * (self.chunk_ratio + 1) - 1 < len(self.bat) else 0
            spb = self.block_size // self.sector_size
            if (be & 7) == 6 and self.parent_src is not None:
                bm = self._pread(self.f, (be >> 20) * MB + (b % self.chunk_ratio) * spb // 8, spb // 8)
                for s in range(spb):
                    if not (bm[s >> 3] >> (s & 7)) & 1:
                        p = b * self.block_size + s * self.sector_size
                        data[s * self.sector_size:(s + 1) * self.sector_size] = \
                            self.parent_src.read(p, self.sector_size).ljust(self.sector_size, b'\x00')
            return bytes(data)
        if state in (0, 1, 3) and self.has_parent:
            return None                              # parent's data
        return None if self.parent_src is None or state == 2 else None


# --------------------------------------------------------------------------------------
# VMDK
# --------------------------------------------------------------------------------------

class _SparseExtent:
    """One VMware sparse extent file (monolithicSparse, split sparse, streamOptimized)."""

    def __init__(self, owner, path):
        self.o = owner
        self.f = owner._open(path)
        h = owner._pread(self.f, 0, 512)
        if h[:4] != b'KDMV':
            raise ImageError(f'{os.path.basename(path)} is not a VMware sparse extent')
        self._parse(h)
        if self.gd_off == 0xFFFFFFFFFFFFFFFF:      # streamOptimized: real header in the footer
            self.f.seek(0, 2)
            end = self.f.tell()
            foot = owner._pread(self.f, end - 1024, 512)
            if foot[:4] == b'KDMV':
                self._parse(foot)
        self.gt_cover = self.gtes * self.grain
        n_gd = -(-self.capacity // self.gt_cover)
        self.gd = struct.unpack(f'<{n_gd}I', owner._pread(self.f, self.gd_off * SECTOR, 4 * n_gd))
        self.gts = {}

    def _parse(self, h):
        (self.version, self.flags, self.capacity, self.grain, self.desc_off, self.desc_size,
         self.gtes, self.rgd_off, self.gd_off, self.overhead) = struct.unpack('<IIQQQQIQQQ', h[4:4 + 68])
        self.compress = struct.unpack('<H', h[77:79])[0]
        self.compressed = bool(self.flags & (1 << 16)) and self.compress == 1

    def descriptor(self):
        if not self.desc_off:
            return ''
        return self.o._pread(self.f, self.desc_off * SECTOR, self.desc_size * SECTOR).split(b'\x00')[0].decode(
            'utf-8', 'replace')

    def grain_bytes(self, g):
        """Data of grain g of this extent, or None if it isn't stored here."""
        gti = g // self.gtes
        if gti >= len(self.gd) or self.gd[gti] == 0:
            return None
        gt = self.gts.get(gti)
        if gt is None:
            gt = struct.unpack(f'<{self.gtes}I', self.o._pread(self.f, self.gd[gti] * SECTOR, 4 * self.gtes))
            self.gts[gti] = gt
        sec = gt[g % self.gtes]
        if sec == 0:
            return None
        if sec == 1:                                 # explicit zero grain
            return bytes(self.grain * SECTOR)
        if self.compressed:
            hdr = self.o._pread(self.f, sec * SECTOR, 12)
            _lba, csize = struct.unpack('<QI', hdr)
            comp = self.o._pread(self.f, sec * SECTOR + 12, csize)
            try:
                return zlib.decompress(comp).ljust(self.grain * SECTOR, b'\x00')
            except zlib.error:
                return bytes(self.grain * SECTOR)
        return self.o._pread(self.f, sec * SECTOR, self.grain * SECTOR)


class VmdkSource(_ImageBase):
    kind = 'VMDK'

    def __init__(self, path, label=None, depth=0):
        self._init_common(path, label or os.path.basename(path), 0)
        with open(path, 'rb') as f:
            head = f.read(512)
        if head[:4] == b'KDMV':
            ext = _SparseExtent(self, path)
            desc = ext.descriptor()
            if not desc:                             # no embedded descriptor: just this extent
                self.extents = [('SPARSE', ext.capacity, ext, 0)]
                self._finish(ext.grain * SECTOR, depth, '')
                return
            text = desc
            self._first = ext
        else:
            with open(path, 'rb') as f:
                text = f.read(64 * 1024).split(b'\x00')[0].decode('utf-8', 'replace')
            if 'createType' not in text and 'RW ' not in text:
                raise ImageError('not a VMDK descriptor')
            self._first = None
        self._from_descriptor(text, depth)

    def _from_descriptor(self, text, depth):
        base = os.path.dirname(os.path.abspath(self.path))
        self.extents = []
        grain = 64 * 1024
        for m in re.finditer(r'^\s*(RW|RDONLY|NOACCESS)\s+(\d+)\s+(\w+)\s*(?:"([^"]*)")?\s*(\d+)?', text, re.M):
            sectors, kind, fname, off = int(m.group(2)), m.group(3).upper(), m.group(4), int(m.group(5) or 0)
            p = os.path.join(base, fname) if fname else None
            if kind == 'ZERO' or not p:
                self.extents.append(('ZERO', sectors, None, 0))
            elif kind in ('FLAT', 'VMFS', 'VMFSRAW', 'VMFSRDM'):
                if not os.path.isfile(p):
                    raise ImageError(f'missing extent file {fname}')
                self.extents.append(('FLAT', sectors, self._open(p), off))
            else:                                    # SPARSE / VMFSSPARSE / streamOptimized
                if self._first is not None and os.path.abspath(p) == os.path.abspath(self.path):
                    ext = self._first
                else:
                    if not os.path.isfile(p):
                        raise ImageError(f'missing extent file {fname}')
                    ext = _SparseExtent(self, p)
                grain = ext.grain * SECTOR
                self.extents.append(('SPARSE', sectors, ext, 0))
        if not self.extents:
            raise ImageError('VMDK descriptor lists no extents')
        ct = re.search(r'createType\s*=\s*"([^"]+)"', text)
        if ct:
            self.kind = f'VMDK ({ct.group(1)})'
        m = re.search(r'parentFileNameHint\s*=\s*"([^"]+)"', text)
        self._finish(grain, depth, m.group(1) if m else '')

    def _finish(self, grain, depth, parent_hint):
        self.block_size = grain
        self.starts = []
        pos = 0
        for e in self.extents:
            self.starts.append(pos)
            pos += e[1] * SECTOR
        self.size = pos
        if parent_hint:
            pp = _find_parent(self.path, parent_hint)
            if pp and depth < 16:
                self.parent_src = open_image(pp, depth=depth + 1)
                self.kind = self.kind.rstrip(')') + ', snapshot)' if '(' in self.kind else 'VMDK (snapshot)'
            else:
                self.notes.append(f'parent disk "{parent_hint}" not found - its parts read as zeros')

    def _block(self, b):
        pos = b * self.block_size
        out = bytearray()
        need = min(self.block_size, self.size - pos)
        while len(out) < need:
            p = pos + len(out)
            k = max(i for i, s in enumerate(self.starts) if s <= p)
            kind, sectors, obj, off = self.extents[k]
            within = p - self.starts[k]
            n = min(need - len(out), sectors * SECTOR - within)
            if kind == 'FLAT':
                d = self._pread(obj, off * SECTOR + within, n)
                out += d.ljust(n, b'\x00')
            elif kind == 'SPARSE':
                g_size = obj.grain * SECTOR
                while n > 0:
                    g, gin = divmod(within, g_size)
                    take = min(n, g_size - gin)
                    gd = obj.grain_bytes(g)
                    if gd is None:
                        out += self._parent_read(self.starts[k] + within, take)
                    else:
                        out += gd[gin:gin + take]
                    within += take
                    n -= take
            else:
                out += bytes(n)
        return bytes(out)


# --------------------------------------------------------------------------------------
# qcow2
# --------------------------------------------------------------------------------------

class Qcow2Source(_ImageBase):
    kind = 'qcow2'

    def __init__(self, path, label=None, depth=0):
        self._init_common(path, label or os.path.basename(path), 0)
        f = self.f = self._open(path)
        h = self._pread(f, 0, 512)
        if h[:4] != b'QFI\xfb':
            raise ImageError('not a qcow2 image')
        version, bf_off, bf_size, cbits, size, crypt, l1_size, l1_off = struct.unpack('>IQIIQIIQ', h[4:48])
        if version not in (2, 3):
            raise ImageError(f'qcow version {version} is not supported')
        if crypt:
            raise ImageError('this qcow2 image is encrypted')
        self.size = size
        self.cbits = cbits
        self.block_size = 1 << cbits
        self.zstd = False
        if version == 3:
            incompat = struct.unpack('>Q', h[72:80])[0]
            hlen = struct.unpack('>I', h[100:104])[0]
            if incompat & 0x14:
                raise ImageError('this qcow2 image uses an external data file / extended L2 entries')
            if incompat & 0x08 and hlen > 104:
                self.zstd = h[104] == 1
        self.l1 = struct.unpack(f'>{l1_size}Q', self._pread(f, l1_off, 8 * l1_size)) if l1_size else ()
        self.l2_entries = self.block_size // 8
        self.l2_cache = OrderedDict()
        self.kind = 'qcow2' + (f' v{version}')
        if bf_off:
            name = self._pread(f, bf_off, bf_size).decode('utf-8', 'replace')
            pp = _find_parent(path, name)
            if pp and depth < 16:
                self.parent_src = open_image(pp, depth=depth + 1)
                self.kind += ' (with backing file)'
            else:
                self.notes.append(f'backing file "{name}" not found - its parts read as zeros')

    def _block(self, b):
        l1i, l2i = divmod(b, self.l2_entries)
        if l1i >= len(self.l1):
            return None
        l2_off = self.l1[l1i] & 0x00FFFFFFFFFFFE00
        if not l2_off:
            return None
        l2 = self.l2_cache.get(l2_off)
        if l2 is None:
            l2 = struct.unpack(f'>{self.l2_entries}Q', self._pread(self.f, l2_off, self.block_size))
            self.l2_cache[l2_off] = l2
            if len(self.l2_cache) > 64:
                self.l2_cache.popitem(last=False)
        e = l2[l2i]
        if e & (1 << 62):                            # compressed cluster
            x = 62 - (self.cbits - 8)
            off = e & ((1 << x) - 1)
            nsec = ((e >> x) & ((1 << (self.cbits - 8)) - 1)) + 1
            comp = self._pread(self.f, off, nsec * 512 - (off & 511))
            try:
                if self.zstd:
                    import zstandard
                    return zstandard.ZstdDecompressor().decompressobj().decompress(comp)[:self.block_size].ljust(
                        self.block_size, b'\x00')
                return zlib.decompressobj(-12).decompress(comp)[:self.block_size].ljust(self.block_size, b'\x00')
            except Exception:
                return bytes(self.block_size)
        if e & 1:                                    # v3 "reads as zeros"
            return bytes(self.block_size)
        off = e & 0x00FFFFFFFFFFFE00
        if not off:
            return None
        return self._pread(self.f, off, self.block_size)


# --------------------------------------------------------------------------------------
# VDI
# --------------------------------------------------------------------------------------

class VdiSource(_ImageBase):
    kind = 'VDI'

    def __init__(self, path, label=None, depth=0):
        self._init_common(path, label or os.path.basename(path), 0)
        f = self.f = self._open(path)
        h = self._pread(f, 0, 512)
        if struct.unpack('<I', h[64:68])[0] != 0xBEDA107F:
            raise ImageError('not a VDI image')
        hsize = struct.unpack('<I', h[72:76])[0]
        hdr = h[76:76 + hsize]
        itype = struct.unpack('<I', hdr[0:4])[0]
        bmap_off, data_off = struct.unpack('<II', hdr[264:272])
        if len(hdr) < 312:
            hdr = self._pread(f, 76, 400)
        self.size = struct.unpack('<Q', hdr[292:300])[0]
        self.block_size = struct.unpack('<I', hdr[300:304])[0]
        nblocks = struct.unpack('<I', hdr[308:312])[0]
        if not self.size or not self.block_size:
            raise ImageError('VDI header not understood')
        self.data_off = data_off
        self.bmap = struct.unpack(f'<{nblocks}I', self._pread(f, bmap_off, 4 * nblocks))
        self.kind = 'VDI (fixed)' if itype == 2 else 'VDI (dynamic)'

    def _block(self, b):
        if b >= len(self.bmap) or self.bmap[b] >= 0xFFFFFFFE:
            return None
        return self._pread(self.f, self.data_off + self.bmap[b] * self.block_size, self.block_size)


# --------------------------------------------------------------------------------------
# E01 (Expert Witness Format)
# --------------------------------------------------------------------------------------

EVF_SIG = b'EVF\x09\x0d\x0a\xff\x00'


def _e01_segments(path):
    m = re.match(r'(.*\.)([Ee])(\d\d)$', path)
    if not m:
        return [path]
    stem, e = m.group(1), m.group(2)
    out = []
    n = 1
    while True:
        if n < 100:
            p = f'{stem}{e}{n:02d}'
        else:                                        # after .E99: .EAA ... .EZZ, .FAA ...
            k = n - 100
            a = 'A' if e == 'E' else 'a'
            p = f'{stem}{chr(ord(e) + k // 676)}{chr(ord(a) + (k // 26) % 26)}{chr(ord(a) + k % 26)}'
        if not os.path.isfile(p):
            break
        out.append(p)
        n += 1
    return out or [path]


class E01Source(_ImageBase):
    kind = 'E01'
    BLOCK_CACHE = 32

    def __init__(self, path, label=None, depth=0):
        self._init_common(path, label or os.path.basename(path), 0)
        self.chunks = []                             # (file, offset, compressed, stored_size)
        self.chunk_size = 0
        sectors = bps = 0
        self.errors = []
        for seg in _e01_segments(path):
            f = self._open(seg)
            if self._pread(f, 0, 8) != EVF_SIG:
                raise ImageError(f'{os.path.basename(seg)} is not an E01 segment')
            f.seek(0, 2)
            fsize = f.tell()
            off = 13
            seg_chunks = []
            while off and off + 76 <= fsize:
                d = self._pread(f, off, 76)
                stype = d[:16].split(b'\x00')[0].decode('ascii', 'replace')
                nxt, ssize = struct.unpack('<QQ', d[16:32])
                body = off + 76
                if stype in ('volume', 'disk'):
                    v = self._pread(f, body, 94)
                    _chunks, spc, bps, sectors = struct.unpack('<IIIQ', v[4:24])
                    self.chunk_size = spc * bps
                elif stype == 'table':
                    t = self._pread(f, body, 24)
                    n, base = struct.unpack('<I4xQ', t[:16])
                    ents = struct.unpack(f'<{n}I', self._pread(f, body + 24, 4 * n))
                    end_of_data = off                    # chunks end where the table section starts
                    offs = []
                    for e in ents:
                        offs.append((base + (e & 0x7FFFFFFF), bool(e & 0x80000000)))
                    for k, (o, comp) in enumerate(offs):
                        stop = offs[k + 1][0] if k + 1 < len(offs) else self._sectors_end(f, o, end_of_data)
                        seg_chunks.append((f, o, comp, stop - o))
                elif stype == 'error2':
                    try:
                        cnt = struct.unpack('<I', self._pread(f, body, 4))[0]
                        raw = self._pread(f, body + 520, 8 * cnt)
                        for k in range(cnt):
                            s0, ns = struct.unpack('<II', raw[8 * k:8 * k + 8])
                            self.errors.append((s0, ns))
                    except struct.error:
                        pass
                elif stype in ('done', 'next'):
                    break
                if nxt == off or nxt == 0:
                    break
                off = nxt
            self.chunks += seg_chunks
        if not self.chunk_size or not self.chunks:
            raise ImageError('E01 image has no readable chunk tables')
        self.sector_size = bps or 512
        self.size = sectors * self.sector_size if sectors else len(self.chunks) * self.chunk_size
        self.block_size = self.chunk_size
        segs = len(_e01_segments(path))
        self.kind = f'E01 ({segs} segment{"s" if segs != 1 else ""})'
        for s0, ns in self.errors:                   # areas the acquisition couldn't read
            self.add_bad(s0 * self.sector_size, ns * self.sector_size)
        if self.errors:
            self.notes.append(f'{len(self.errors)} area(s) were unreadable when the image was made')

    def _sectors_end(self, f, o, table_off):
        return table_off

    def _block(self, b):
        if b >= len(self.chunks):
            return bytes(self.chunk_size)
        f, o, comp, n = self.chunks[b]
        raw = self._pread(f, o, n)
        if comp:
            try:
                return zlib.decompressobj().decompress(raw)[:self.chunk_size]
            except zlib.error:
                return bytes(self.chunk_size)
        return raw[:self.chunk_size]


# --------------------------------------------------------------------------------------
# split raw (.001, .002, ...)
# --------------------------------------------------------------------------------------

class SplitRawSource(_ImageBase):
    kind = 'split raw'

    def __init__(self, parts, label=None):
        self._init_common(parts[0], label or os.path.basename(parts[0]), 0)
        self.parts = []
        pos = 0
        for p in parts:
            f = self._open(p)
            f.seek(0, 2)
            n = f.tell()
            self.parts.append((pos, n, f))
            pos += n
        self.size = pos
        self.block_size = 4 * MB
        self.kind = f'split raw ({len(parts)} parts)'

    def read(self, offset, length):
        hit = self.ra_hit(offset, length) if self.ra else None
        if hit is not None:
            return hit
        if offset >= self.size:
            return b''
        length = min(length, self.size - offset)
        out = bytearray()
        for start, n, f in self.parts:
            if offset + length <= start or offset >= start + n:
                continue
            a = max(offset, start)
            b = min(offset + length, start + n)
            out += self._pread(f, a - start, b - a)
        return bytes(out)


def _split_parts(path):
    m = re.match(r'(.*\.)(\d{3})$', path)
    if not m or int(m.group(2)) != 1:
        return None
    parts = []
    n = 1
    while os.path.isfile(f'{m.group(1)}{n:03d}'):
        parts.append(f'{m.group(1)}{n:03d}')
        n += 1
    return parts if len(parts) > 1 else None


# --------------------------------------------------------------------------------------

def detect(path):
    """The container format of an image file, or None for plain raw."""
    try:
        with open(path, 'rb') as f:
            head = f.read(1024)
            f.seek(0, 2)
            n = f.tell()
            f.seek(max(0, n - 512))
            tail = f.read(512)
    except OSError:
        return None
    if head[:8] == EVF_SIG:
        return 'e01'
    if head[:8] == b'vhdxfile':
        return 'vhdx'
    if head[:4] == b'KDMV' or head.lstrip().startswith(b'# Disk DescriptorFile'):
        return 'vmdk'
    if head[:4] == b'QFI\xfb':
        return 'qcow2'
    if len(head) >= 68 and struct.unpack('<I', head[64:68])[0] == 0xBEDA107F:
        return 'vdi'
    if tail[:8] == b'conectix' or head[:8] == b'conectix':
        return 'vhd'
    if _split_parts(path):
        return 'split'
    return None


def open_image(path, label=None, depth=0):
    """A source for any supported image file (plain raw -> engine.RawSource)."""
    kind = detect(path)
    cls = {'e01': E01Source, 'vhdx': VhdxSource, 'vmdk': VmdkSource, 'qcow2': Qcow2Source,
           'vdi': VdiSource, 'vhd': VhdSource}.get(kind)
    if cls:
        return cls(path, label, depth) if kind != 'e01' else cls(path, label)
    if kind == 'split':
        return SplitRawSource(_split_parts(path), label)
    return engine.RawSource(path, label)


IMAGE_PATTERNS = '*.img *.dd *.raw *.bin *.iso *.001 *.vhd *.vhdx *.avhdx *.vmdk *.qcow2 *.qcow *.vdi *.E01 *.e01'
