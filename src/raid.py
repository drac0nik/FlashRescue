"""
FlashRescue virtual RAID: rebuilds an array from its member drives (or rescue images of them)
without writing anything, and presents it as one source that every scan works on.

  * Levels: RAID 0, 1, 5, 6, 10 (near copies) and spanned/JBOD (linear)
  * RAID 5 / 6 layouts: left/right x symmetric/asymmetric (all four md layouts)
  * Missing or unreadable members are rebuilt from parity (RAID 5: one, RAID 6: two)
  * Settings are read from Linux software-RAID superblocks (v0.90, 1.0, 1.1, 1.2) - this is what
    Synology, QNAP, Asustor, TerraMaster, Netgear ReadyNAS and most other NAS boxes use - including
    arrays that live inside partitions
  * LVM2 logical volumes on top (Synology SHR / QNAP) are found and assembled, even across arrays
  * For arrays with no readable settings (hardware RAID cards, motherboard RAID), level, drive
    order, stripe size and layout can be worked out by trying candidates against the filesystem
"""

import os
import re
import struct
import bisect
import itertools
import threading

import engine
from engine import u16, u32, u64, human_size, KB, MB, GB

MD_MAGIC = 0xA92B4EFC
LAYOUTS = ['left-asymmetric', 'right-asymmetric', 'left-symmetric', 'right-symmetric']   # md numbers 0..3


# ======================================================================================
# Sources that live inside other sources
# ======================================================================================

class _SourceBase(engine.RawSource):
    """Shared bits of RawSource (bad-range bookkeeping) without opening a file."""

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

    def read(self, offset, length):
        hit = self.ra_hit(offset, length) if self.ra else None
        return hit if hit is not None else self._read(offset, length)

    def load_rescue_map(self, map_path):
        raise ValueError('rescue maps belong to the member drives, not to the assembled array')


class SubSource(_SourceBase):
    """A byte range of another source (a partition, or a member's data area)."""

    def __init__(self, parent, offset, size, label=None):
        self._init_common(f'{parent.path}@{offset}', label or f'{parent.label} @ {human_size(offset)}',
                          max(0, min(size, parent.size - offset)), parent.sector_size)
        self.parent = parent
        self.offset = offset

    def _read(self, offset, length):
        if offset >= self.size:
            return b''
        length = min(length, self.size - offset)
        return self.parent.read(self.offset + offset, length)

    def unreadable_here(self, offset, length):
        return self.parent.unreadable_in([(self.offset + offset, length)])

    def close(self):
        pass


def unreadable(src, offset, length):
    if isinstance(src, SubSource):
        return src.unreadable_here(offset, length)
    return src.unreadable_in([(offset, length)])


# ======================================================================================
# GF(2^8) for RAID 6 (polynomial 0x11d, generator 2 - same as Linux md)
# ======================================================================================

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def gf_mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def gf_inv(a):
    return _EXP[255 - _LOG[a]]


_MUL_TABLES = {}


def gf_mul_bytes(data, c):
    """Multiply every byte by constant c (bytes.translate does it in C)."""
    if c == 1:
        return data
    t = _MUL_TABLES.get(c)
    if t is None:
        t = bytes(gf_mul(i, c) for i in range(256))
        _MUL_TABLES[c] = t
    return data.translate(t)


def xor_bytes(a, b):
    n = len(a)
    return (int.from_bytes(a, 'little') ^ int.from_bytes(b, 'little')).to_bytes(n, 'little')


# ======================================================================================
# The virtual array
# ======================================================================================

class VirtualRaid(_SourceBase):
    """members: list of (source, data_offset_bytes) or None for a missing drive, in slot order."""

    def __init__(self, members, level, chunk=64 * KB, layout='left-symmetric', member_size=None,
                 label=None, copies=2):
        self.members = members
        self.level = level
        self.n = len(members)
        self.chunk = chunk
        self.layout = layout
        self.copies = copies
        sizes = [src.size - off for m in members if m for src, off in [m]]
        if not sizes:
            raise ValueError('no member drives')
        ms = member_size or min(sizes)
        if level not in ('linear', 1):
            ms -= ms % chunk
        self.member_size = ms
        n = self.n
        if level == 0:
            size = ms * n
        elif level == 1:
            size = ms
        elif level == 5:
            size = ms * (n - 1)
        elif level == 6:
            size = ms * (n - 2)
        elif level == 10:
            size = ms * n // copies
            size -= size % chunk
        elif level == 'linear':
            size = sum(src.size - off for src, off in members if True) if all(members) else 0
        else:
            raise ValueError(f'unsupported RAID level {level}')
        missing = sum(1 for m in members if m is None)
        redundancy = {0: 0, 'linear': 0, 1: n - 1, 5: 1, 6: 2, 10: copies - 1}[level]
        if missing > redundancy and level != 10:
            raise ValueError(f'{missing} drive(s) missing but RAID {level} can only rebuild {redundancy}')
        name = 'Spanned (JBOD)' if level == 'linear' else f'RAID {level}'
        self._init_common(f'raid://{name}/{n}x{chunk}/{layout}', label or
                          f'Virtual {name}, {n} drives' + (f', {missing} missing' if missing else ''), size)
        self.redundancy = redundancy
        self.missing = missing

    # ---- mapping
    def _r5(self, stripe, k):
        n = self.n
        lay = self.layout
        if lay.startswith('left'):
            p = n - 1 - (stripe % n)
        else:
            p = stripe % n
        if lay.endswith('symmetric') and not lay.endswith('asymmetric'):
            d = (p + 1 + k) % n
        else:
            d = k + 1 if k >= p else k
        return p, d

    def _r6(self, stripe, k):
        n = self.n
        lay = self.layout
        p = n - 1 - (stripe % n) if lay.startswith('left') else stripe % n
        if lay.endswith('asymmetric'):
            q = p + 1
            d = k
            if p == n - 1:
                d += 1
                q = 0
            elif d >= p:
                d += 2
        else:
            q = (p + 1) % n
            d = (p + 2 + k) % n
        return p, q, d

    def _q_order(self, p, q):
        """Data disks in the order md weights them in the Q syndrome."""
        n = self.n
        d0 = 0 if q == n - 1 else q + 1
        order = []
        i = d0
        for _ in range(n):
            if i not in (p, q):
                order.append(i)
            i = (i + 1) % n
        return order

    def _member(self, i):
        m = self.members[i]
        return m

    def _read_member(self, i, off, n):
        """Data from member i, or None if missing/unreadable there."""
        m = self.members[i]
        if m is None:
            return None
        src, base = m
        d = src.read(base + off, n)
        if len(d) < n or unreadable(src, base + off, n):
            return None
        return d

    def _piece(self, off, n):
        """Read n bytes at virtual offset off, where [off, off+n) stays inside one chunk."""
        lv, c = self.level, self.chunk
        if lv == 'linear':
            pos = off
            for m in self.members:
                src, base = m
                span = src.size - base
                if pos < span:
                    d = src.read(base + pos, n)
                    if unreadable(src, base + pos, n):
                        self.add_bad(off, n)
                    return d
                pos -= span
            return b'\x00' * n
        if lv == 1:
            for i in range(self.n):
                d = self._read_member(i, off, n)
                if d is not None:
                    return d
            self.add_bad(off, n)
            return self._fallback(0, off, n)
        ci, within = divmod(off, c)
        if lv == 0:
            disk, stripe = ci % self.n, ci // self.n
            moff = stripe * c + within
            d = self._read_member(disk, moff, n)
            if d is None:
                self.add_bad(off, n)
                return self._fallback(disk, moff, n)
            return d
        if lv == 10:
            for k in range(self.copies):
                slot = ci * self.copies + k
                disk, row = slot % self.n, slot // self.n
                d = self._read_member(disk, row * c + within, n)
                if d is not None:
                    return d
            self.add_bad(off, n)
            return b'\x00' * n
        if lv == 5:
            stripe, k = divmod(ci, self.n - 1)
            p, disk = self._r5(stripe, k)
            moff = stripe * c + within
            d = self._read_member(disk, moff, n)
            if d is not None:
                return d
            acc = None
            for i in range(self.n):
                if i == disk:
                    continue
                x = self._read_member(i, moff, n)
                if x is None:
                    self.add_bad(off, n)
                    return self._fallback(disk, moff, n)
                acc = x if acc is None else xor_bytes(acc, x)
            return acc
        if lv == 6:
            stripe, k = divmod(ci, self.n - 2)
            p, q, disk = self._r6(stripe, k)
            moff = stripe * c + within
            d = self._read_member(disk, moff, n)
            if d is not None:
                return d
            return self._r6_rebuild(p, q, disk, moff, n, off)
        raise ValueError('bad level')

    def _fallback(self, disk, moff, n):
        m = self.members[disk]
        if m is None:
            return b'\x00' * n
        src, base = m
        d = src.read(base + moff, n)
        return d + b'\x00' * (n - len(d))

    def _r6_rebuild(self, p, q, want, moff, n, voff):
        order = self._q_order(p, q)
        coef = {disk: i for i, disk in enumerate(order)}
        data = {}
        lost = []
        for disk in order:
            x = self._read_member(disk, moff, n)
            if x is None:
                lost.append(disk)
            else:
                data[disk] = x
        P = self._read_member(p, moff, n)
        Q = self._read_member(q, moff, n)
        zero = b'\x00' * n
        if len(lost) == 1:
            x = lost[0]
            if P is not None:
                acc = P
                for disk, d in data.items():
                    acc = xor_bytes(acc, d)
                return acc
            if Q is not None:
                acc = Q
                for disk, d in data.items():
                    acc = xor_bytes(acc, gf_mul_bytes(d, _EXP[coef[disk]]))
                return gf_mul_bytes(acc, gf_inv(_EXP[coef[x]]))
        elif len(lost) == 2 and P is not None and Q is not None:
            x, y = sorted(lost, key=lambda dd: coef[dd])
            cx, cy = coef[x], coef[y]
            pxy, qxy = P, Q
            for disk, d in data.items():
                pxy = xor_bytes(pxy, d)
                qxy = xor_bytes(qxy, gf_mul_bytes(d, _EXP[coef[disk]]))
            g = _EXP[(cy - cx) % 255]
            denom = gf_inv(g ^ 1)
            A = gf_mul(g, denom)
            B = gf_mul(gf_inv(_EXP[cx]), denom)
            dx = xor_bytes(gf_mul_bytes(pxy, A), gf_mul_bytes(qxy, B))
            dy = xor_bytes(pxy, dx)
            return dx if want == x else dy
        self.add_bad(voff, n)
        return zero

    def _read(self, offset, length):
        if offset >= self.size:
            return b''
        length = min(length, self.size - offset)
        out = bytearray()
        pos = offset
        end = offset + length
        step = self.chunk if self.level not in ('linear', 1) else 4 * MB
        while pos < end:
            n = min(end - pos, step - (pos % step))
            out += self._piece(pos, n)
            pos += n
        return bytes(out)

    def close(self):
        for m in self.members:
            if m:
                try:
                    m[0].close()
                except Exception:
                    pass

    def parity_agreement(self, samples=64):
        """RAID 5 sanity: fraction of sampled stripes whose members XOR to zero."""
        if self.level != 5 or self.missing:
            return None
        good = tot = 0
        rows = max(1, self.member_size // self.chunk)
        for s in range(samples):
            row = (s * 7919) % rows
            blocks = [self._read_member(i, row * self.chunk, 4096) for i in range(self.n)]
            if any(b is None for b in blocks):
                continue
            if not any(b.strip(b'\x00') for b in blocks):
                continue
            acc = blocks[0]
            for b in blocks[1:]:
                acc = xor_bytes(acc, b)
            tot += 1
            good += not acc.strip(b'\x00')
        return good / tot if tot else None


# ======================================================================================
# Linux software RAID (md) superblocks
# ======================================================================================

def _parse_v1(b, version):
    if len(b) < 512 or u32(b, 0) != MD_MAGIC or u32(b, 4) != 1:
        return None
    max_dev = u32(b, 220)
    dev_number = u32(b, 160)
    role = u16(b, 256 + 2 * dev_number) if dev_number < max_dev and 256 + 2 * dev_number + 2 <= len(b) else 0xFFFF
    level = struct.unpack_from('<i', b, 72)[0]
    return {
        'version': version, 'uuid': b[16:32].hex(), 'name': b[32:64].split(b'\x00')[0].decode('utf-8', 'replace'),
        'level': level, 'layout': u32(b, 76), 'dev_size': u64(b, 80) * 512, 'chunk': u32(b, 88) * 512,
        'raid_disks': u32(b, 92), 'data_offset': u64(b, 128) * 512, 'data_size': u64(b, 136) * 512,
        'role': role, 'events': u64(b, 200), 'feature_map': u32(b, 8),
    }


def _parse_v090(b):
    if len(b) < 4096 or u32(b, 0) != MD_MAGIC or u32(b, 4) != 0:
        return None
    w = struct.unpack_from('<1024I', b, 0)
    uuid = struct.pack('<4I', w[5], w[13], w[14], w[15]).hex()
    return {
        'version': '0.90', 'uuid': uuid, 'name': '', 'level': struct.unpack('<i', struct.pack('<I', w[7]))[0],
        'layout': w[64], 'dev_size': w[8] * 1024, 'chunk': w[65], 'raid_disks': w[10], 'data_offset': 0,
        'data_size': w[8] * 1024, 'role': w[992 + 3], 'events': (w[39] << 32) | w[40], 'feature_map': 0,
    }


def md_superblocks(src, base, size):
    """All md superblocks found in [base, base+size) of src."""
    out = []
    for ver, off in (('1.2', 4096), ('1.1', 0), ('1.0', ((size - 8192) & ~4095))):
        if off < 0 or off + 4096 > size:
            continue
        sb = _parse_v1(src.read(base + off, 4096), ver)
        if sb:
            out.append(sb)
    off = (size & ~65535) - 65536
    if off > 0:
        sb = _parse_v090(src.read(base + off, 4096))
        if sb:
            out.append(sb)
    return out


# ======================================================================================
# LVM2
# ======================================================================================

_TOKEN = re.compile(r'\s*(?:(#[^\n]*)|("(?:[^"\\]|\\.)*")|(\[)|(\])|(\{)|(\})|(=)|(,)|([^\s=\[\]{},"#]+))')


def parse_lvm_text(text):
    """LVM metadata text -> nested dicts."""
    toks = []
    for m in _TOKEN.finditer(text):
        if m.group(1):
            continue
        for g in range(2, 10):
            if m.group(g) is not None:
                toks.append((g, m.group(g)))
                break
    pos = 0

    def value():
        nonlocal pos
        g, t = toks[pos]
        pos += 1
        if g == 2:
            return t[1:-1]
        if g == 3:
            arr = []
            while toks[pos][0] != 4:
                if toks[pos][0] == 8:
                    pos += 1
                    continue
                arr.append(value())
            pos += 1
            return arr
        try:
            return int(t)
        except ValueError:
            try:
                return float(t)
            except ValueError:
                return t

    def block():
        nonlocal pos
        d = {}
        while pos < len(toks) and toks[pos][0] != 6:
            g, name = toks[pos]
            pos += 1
            if pos < len(toks) and toks[pos][0] == 5:
                pos += 1
                d[name] = block()
                pos += 1
            elif pos < len(toks) and toks[pos][0] == 7:
                pos += 1
                d[name] = value()
        return d

    return block()


def lvm_pv(src, base=0):
    """PV label + current metadata at the start of [base...]: dict or None."""
    for sector in range(4):
        lab = src.read(base + sector * 512, 512)
        if lab[:8] == b'LABELONE' and lab[24:32] == b'LVM2 001':
            break
    else:
        return None
    ph = base + sector * 512 + u32(lab, 20)
    hdr = src.read(ph, 512)
    pv_uuid = hdr[:32].decode('ascii', 'replace')
    p = 40
    data_areas, meta_areas = [], []
    for lst in (data_areas, meta_areas):
        while p + 16 <= len(hdr):
            o, s = u64(hdr, p), u64(hdr, p + 8)
            p += 16
            if o == 0 and s == 0:
                break
            lst.append((o, s))
    for mo, ms in meta_areas:
        mh = src.read(base + mo, 512)
        if mh[4:20] != b' LVM2 x[5A%r0N*>':
            continue
        start = u64(mh, 24)
        size = u64(mh, 32)
        lo, ls = u64(mh, 40), u64(mh, 48)
        if not lo or not ls:
            continue
        if lo + ls <= size:
            text = src.read(base + mo + lo, ls)
        else:                                       # metadata wraps around the circular buffer
            first = size - lo
            text = src.read(base + mo + lo, first) + src.read(base + mo + 512, ls - first)
        try:
            meta = parse_lvm_text(text.split(b'\x00')[0].decode('utf-8', 'replace'))
        except Exception:
            continue
        for vgname, vg in meta.items():
            if isinstance(vg, dict) and 'physical_volumes' in vg:
                return {'pv_uuid': pv_uuid, 'vg': vgname, 'meta': vg, 'base': base}
    return None


class LvSource(_SourceBase):
    """A logical volume: pieces of one or more physical volumes."""

    def __init__(self, name, segments, size):
        self._init_common(f'lvm://{name}', f'LVM volume {name}', size)
        self.segs = segments                # sorted [(lv_off, length, src or None, src_off)]
        self.starts = [s[0] for s in segments]

    def _read(self, offset, length):
        if offset >= self.size:
            return b''
        length = min(length, self.size - offset)
        out = bytearray()
        pos, end = offset, offset + length
        while pos < end:
            i = bisect.bisect_right(self.starts, pos) - 1
            if i < 0:
                out += b'\x00' * (end - pos)
                break
            lo, ln, src, so = self.segs[i]
            n = min(end, lo + ln) - pos
            if n <= 0:
                out += b'\x00' * (end - pos)
                break
            if src is None:
                out += b'\x00' * n
                self.add_bad(pos, n)
            else:
                d = src.read(so + pos - lo, n)
                if unreadable(src, so + pos - lo, n):
                    self.add_bad(pos, n)
                out += d + b'\x00' * (n - len(d))
            pos += n
        return bytes(out)

    def to_parent_runs(self, runs):
        """(offset, length) runs inside this volume -> runs on the underlying source."""
        out = []
        for off, ln in runs:
            if off is None:
                out.append((None, ln))
                continue
            pos, end = off, off + ln
            while pos < end:
                i = bisect.bisect_right(self.starts, pos) - 1
                lo, sl, src, so = self.segs[i] if i >= 0 else (0, 0, None, 0)
                n = min(end, lo + sl) - pos if i >= 0 else end - pos
                if n <= 0:
                    out.append((None, end - pos))
                    break
                out.append((None if src is None else so + pos - lo, n))
                pos += n
        return out

    def close(self):
        pass


def assemble_lvm(pvs):
    """pvs: list of (source, pv dict). Returns [LvSource] for every logical volume found."""
    vgs = {}
    for src, pv in pvs:
        vgs.setdefault(pv['vg'], []).append((src, pv))
    out = []
    for vgname, members in vgs.items():
        meta = max((pv['meta'] for _, pv in members), key=lambda m: m.get('seqno', 0))
        ext = int(meta.get('extent_size', 8192)) * 512
        by_uuid = {pv['pv_uuid'].replace('-', ''): (src, pv) for src, pv in members}
        pvmap = {}
        for pvname, info in meta.get('physical_volumes', {}).items():
            uid = str(info.get('id', '')).replace('-', '')
            hit = by_uuid.get(uid)
            pvmap[pvname] = (hit[0], hit[1]['base'] + int(info.get('pe_start', 2048)) * 512) if hit else None
        for lvname, lv in meta.get('logical_volumes', {}).items():
            segs = []
            total = 0
            for key, seg in lv.items():
                if not key.startswith('segment') or not isinstance(seg, dict):
                    continue
                start = int(seg.get('start_extent', 0)) * ext
                count = int(seg.get('extent_count', 0)) * ext
                stripes = seg.get('stripes', [])
                stype = seg.get('type', 'striped')
                if stype != 'striped' or len(stripes) != 2:
                    continue                       # thin pools, mirrors, multi-stripe: not supported
                pvname, pe = stripes[0], int(stripes[1])
                loc = pvmap.get(pvname)
                segs.append((start, count, loc[0] if loc else None, (loc[1] + pe * ext) if loc else 0))
                total = max(total, start + count)
            if segs:
                segs.sort()
                out.append(LvSource(f'{vgname}/{lvname}', segs, total))
    return out


# ======================================================================================
# Discovery
# ======================================================================================

class FoundArray:
    def __init__(self, uuid, name, level, layout, chunk, raid_disks, members, events_note=''):
        self.uuid, self.name, self.level, self.layout, self.chunk = uuid, name, level, layout, chunk
        self.raid_disks, self.members, self.note = raid_disks, members, events_note

    def describe(self):
        lv = 'JBOD' if self.level == 'linear' else f'RAID {self.level}'
        have = sum(1 for m in self.members if m)
        return (f"{self.name or 'md array'}: {lv}, {have} of {self.raid_disks} drives"
                + (f', {human_size(self.chunk)} stripes' if self.level not in (1, 'linear') else '')
                + (f', {self.layout}' if self.level in (5, 6) else '') + (f' - {self.note}' if self.note else ''))


def _regions(src):
    out = [(0, src.size)]
    try:
        out += engine._partition_table(src)
    except Exception:
        pass
    return out


def discover(sources, status=None):
    """Look for md arrays and LVM volumes on the given drives/images.
    Returns (arrays [FoundArray], builders) where each array can be turned into a VirtualRaid."""
    found = {}
    for src in sources:
        for base, size in _regions(src):
            for sb in md_superblocks(src, base, size):
                found.setdefault(sb['uuid'], []).append((src, base, size, sb))
    arrays = []
    for uuid, entries in found.items():
        best = max(entries, key=lambda e: e[3]['events'])[3]
        lvl = best['level']
        level = {-1: 'linear', 0: 0, 1: 1, 4: 5, 5: 5, 6: 6, 10: 10}.get(lvl)
        if level is None:
            continue
        n = best['raid_disks']
        slots = [None] * n
        stale = []
        for src, base, size, sb in sorted(entries, key=lambda e: -e[3]['events']):
            r = sb['role']
            if r >= n or slots[r] is not None:
                continue
            if sb['events'] + 1 < best['events']:
                stale.append((r, src, base, sb))
                continue
            slots[r] = (src, base, sb)
        note = ''
        redundancy = {0: 0, 'linear': 0, 1: n - 1, 5: 1, 6: 2, 10: 1}[level]
        missing = slots.count(None)
        if stale and missing > redundancy:
            for r, src, base, sb in stale:            # out of date, but better than nothing
                if slots[r] is None:
                    slots[r] = (src, base, sb)
            note = 'some drives are out of date - results may be partly old'
        elif stale:
            note = f'{len(stale)} out-of-date drive(s) left out and rebuilt from parity'
        layout = best['layout']
        if level in (5, 6):
            layout = LAYOUTS[layout] if layout < 4 else 'left-symmetric'
        copies = (best['layout'] & 0xFF) or 2 if level == 10 else 2
        members = []
        for s in slots:
            if s is None:
                members.append(None)
            else:
                src, base, sb = s
                members.append((SubSource(src, base, sb['data_offset'] + (sb['dev_size'] or sb['data_size']),
                                          f'{src.label} @ {human_size(base)}'), sb['data_offset']))
        fa = FoundArray(uuid, best['name'], level, layout, best['chunk'] or 64 * KB, n, members, note)
        fa.member_size = best['dev_size'] or best['data_size'] or None
        fa.copies = copies
        arrays.append(fa)
    return arrays


def build(fa):
    return VirtualRaid(fa.members, fa.level, fa.chunk, fa.layout if isinstance(fa.layout, str) else 'left-symmetric',
                       member_size=getattr(fa, 'member_size', None), copies=getattr(fa, 'copies', 2),
                       label=f"Virtual {fa.describe()}")


def find_lvm(sources):
    """LVM volumes on any of these sources (arrays, drives or partitions)."""
    pvs = []
    for src in sources:
        for base, size in _regions(src):
            pv = lvm_pv(src, base)
            if pv:
                pvs.append((src, pv))
    return assemble_lvm(pvs) if pvs else []


# ======================================================================================
# Working out settings when the drives don't carry any (hardware / motherboard RAID)
# ======================================================================================

CHUNKS = [64 * KB, 128 * KB, 256 * KB, 512 * KB, 32 * KB, 16 * KB, 1 * MB]


def _structure_score(src):
    """How much sense does this candidate array make? Higher is better. Checks that the filesystem's
    own structures line up AND that files all over the volume start with the right signatures - a
    wrong drive order, stripe size or layout scrambles one or the other."""
    try:
        parts = engine.find_partitions(src)
    except Exception:
        return 0
    best = 0
    for p in parts:
        v = p.volume
        if not v:
            continue
        score = 5
        try:
            if isinstance(v, engine.NtfsVolume):
                ok = 0
                for i in range(16, 1200):
                    r = v.read_record(i)
                    if r[:4] == b'FILE' and u32(r, 0x2C) == i:
                        ok += 1
                score += ok // 4
            items = []

            class Enough(Exception):
                pass

            def em(it):
                items.append(it)
                if len(items) >= 400:
                    raise Enough()
            try:
                v.walk(em, True, threading.Event())
            except Enough:
                pass
            good = 0
            for it in items:
                if it.inline is not None or it.start is None:
                    continue
                ok = engine.head_matches_ext(it.ext, src.read(it.start, 16))
                good += 4 if ok else (0 if ok is None else -2)
            score += len(items) // 8 + good
        except Exception:
            pass
        best = max(best, score)
    return best


def _bootish(src, off=0):
    b = src.read(off, 512)
    return len(b) == 512 and b[510:512] == b'\x55\xaa'


def detect(members, levels=(5, 0), chunks=CHUNKS, progress=None, cancel=None, data_offset=0):
    """Try level / drive order / stripe size / layout combinations and score each by how well the
    resulting filesystem holds together. members: sources, or None for a missing drive.
    Returns [(score, description, settings dict)] best first."""
    n = len(members)
    results = []
    present = [m for m in members if m is not None]
    if len(present) >= 2 and 1 in levels or (len(present) == 2 and n == 2):
        a, b = present[0].read(data_offset + 1 * MB, 64 * KB), present[1].read(data_offset + 1 * MB, 64 * KB)
        if a == b and a.strip(b'\x00'):
            results.append((10 ** 6, 'RAID 1 (mirror): drives hold the same data',
                            {'level': 1, 'order': list(range(n)), 'chunk': 64 * KB, 'layout': 'left-symmetric'}))
    # the drive that starts with a partition table / boot sector is first in line
    firsts = [i for i, m in enumerate(members) if m is not None and _bootish(m, data_offset)] or \
             [i for i, m in enumerate(members) if m is not None]
    combos = []
    for level in levels:
        if level == 5 and n < 3 or level == 0 and n < 2:
            continue
        layouts = LAYOUTS if level == 5 else ['left-symmetric']
        for lay in layouts:
            # which slot holds the array's first chunk (where the partition table / boot sector is)
            slot0 = 1 if (level == 5 and lay.startswith('right')) else 0
            for first in firsts:
                rest = [i for i in range(n) if i != first]
                for perm in itertools.permutations(rest):
                    order = list(perm)
                    order.insert(slot0, first)
                    for chunk in chunks:
                        combos.append((level, order, chunk, lay))
    total = len(combos) or 1
    for k, (level, order, chunk, lay) in enumerate(combos):
        if cancel is not None and cancel.is_set():
            break
        if progress and k % 4 == 0:
            progress(k / total)
        mem = [(members[i], data_offset) if members[i] is not None else None for i in order]
        try:
            v = VirtualRaid(mem, level, chunk, lay)
        except ValueError:
            continue
        sc = _structure_score(v)
        if sc > 0:
            desc = f"RAID {level}, order {' > '.join(str(i + 1) for i in order)}, {human_size(chunk)} stripes" \
                   + (f', {lay}' if level == 5 else '')
            results.append((sc, desc, {'level': level, 'order': order, 'chunk': chunk, 'layout': lay}))
    results.sort(key=lambda r: -r[0])
    return results
