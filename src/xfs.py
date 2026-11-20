"""
XFS (Linux servers, some NAS boxes, Red Hat/CentOS default).

  Live files      names and folders from the directory tree (shortform, block, leaf and node
                  directories; extent and B+tree file maps)
  Deleted files   XFS clears a deleted file's map in its inode, so - as with ext4 - the data
                  location comes from the filesystem log, which keeps recent copies of inodes.
                  Names come from leftovers in directory free space and from older copies of
                  small directories in the log.
  Free space      from the free-space B+trees, so the deep scan can stick to unused space.
"""
import datetime
import struct

import engine
from engine import FoundFile, MB

S_IFMT, S_IFDIR, S_IFREG = 0xF000, 0x4000, 0x8000
XFS_LI_INODE = 0x123b
ILOG_DDATA, ILOG_DEXT, ILOG_DBROOT = 0x2, 0x4, 0x8
LEAF_OFFSET = 32 * 1024 ** 3                     # directory data blocks live below 32 GB


def b16(b, o): return struct.unpack_from('>H', b, o)[0]
def b32(b, o): return struct.unpack_from('>I', b, o)[0]
def b64(b, o): return struct.unpack_from('>Q', b, o)[0]
def l16(b, o): return struct.unpack_from('<H', b, o)[0]
def l32(b, o): return struct.unpack_from('<I', b, o)[0]
def l64(b, o): return struct.unpack_from('<Q', b, o)[0]


def _ts(sec, big=False):
    try:
        if big:
            sec = sec / 1e9 - 2 ** 31
        if sec <= 0:
            return None
        return datetime.datetime.fromtimestamp(sec)
    except (OverflowError, OSError, ValueError):
        return None


def decode_extents(raw, n):
    out = []
    for k in range(n):
        if 16 * k + 16 > len(raw):
            break
        l0, l1 = struct.unpack_from('>QQ', raw, 16 * k)
        out.append(((l0 & ((1 << 63) - 1)) >> 9, ((l0 & 0x1FF) << 43) | (l1 >> 21), l1 & ((1 << 21) - 1),
                    bool(l0 >> 63)))
    return out


class XfsVolume(engine._BitmapAlloc, engine._VolumeBase):
    kind = 'XFS'

    @staticmethod
    def probe_at(src, off):
        return src.read(off, 4) == b'XFSB'

    @staticmethod
    def probe(bs):
        return bs[:4] == b'XFSB'

    def __init__(self, src, vol_off, bs=None):
        self.src = src
        self.vol_off = vol_off
        sb = src.read(vol_off, 512)
        if sb[:4] != b'XFSB':
            raise ValueError('not XFS')
        self.bs = b32(sb, 4)
        self.cluster_size = self.bs
        self.dblocks = b64(sb, 8)
        self.rootino = b64(sb, 56)
        self.logstart = b64(sb, 48)
        self.agblocks = b32(sb, 84)
        self.agcount = b32(sb, 88)
        self.logblocks = b32(sb, 96)
        ver = b16(sb, 100)
        self.v5 = (ver & 0xF) == 5
        self.sectsize = b16(sb, 102)
        self.isize = b16(sb, 104)
        self.label = sb[108:120].split(b'\x00')[0].decode('utf-8', 'replace')
        self.inopblog = sb[123]
        self.agblklog = sb[124]
        self.dirblk = self.bs << sb[192]
        features2 = b32(sb, 200)
        incompat = b32(sb, 216) if self.v5 else 0
        self.ftype = bool(incompat & 1) if self.v5 else bool(features2 & 0x200)
        self.nrext64 = bool(incompat & 0x20)
        self.core = 176 if self.v5 else 100
        if not (512 <= self.bs <= 65536 and self.agblocks and self.agcount and 256 <= self.isize <= 2048):
            raise ValueError('bad XFS superblock')
        self.size_bytes = self.dblocks * self.bs
        self.kind = 'XFS' + (' (v5)' if self.v5 else ' (v4)')
        self.nbits = self.agcount * self.agblocks
        self.bit_base = 0
        self.max_cluster = self.nbits - 1
        self._bitmap = None
        self._log = None
        self.names_without_data = 0

    # ---- geometry
    def fsb_to_lin(self, fsb):
        return (fsb >> self.agblklog) * self.agblocks + (fsb & ((1 << self.agblklog) - 1))

    def cluster_offset(self, b):
        return self.vol_off + b * self.bs

    def cluster_index(self, off):
        return (off - self.vol_off) // self.bs

    def carve_base(self):
        return self.vol_off

    def inode_offset(self, ino):
        shift = self.agblklog + self.inopblog
        agno = ino >> shift
        agino = ino & ((1 << shift) - 1)
        agbno = agino >> self.inopblog
        idx = agino & ((1 << self.inopblog) - 1)
        return self.vol_off + (agno * self.agblocks + agbno) * self.bs + idx * self.isize

    def read_inode(self, ino):
        if not ino or (ino >> (self.agblklog + self.inopblog)) >= self.agcount:
            return None
        ib = self.src.read(self.inode_offset(ino), self.isize)
        return ib if ib[:2] == b'IN' else None

    # ---- free space: the free-space-by-block-number B+tree of every allocation group
    @property
    def bitmap(self):
        if self._bitmap is None:
            bm = bytearray(b'\xff' * (-(-self.nbits // 8)))
            hdr = 56 if self.v5 else 16
            for ag in range(self.agcount):
                agf = self.src.read(self.vol_off + ag * self.agblocks * self.bs + self.sectsize, 512)
                if agf[:4] != b'XAGF':
                    continue
                root, level = b32(agf, 16), b32(agf, 28)
                stack = [(root, level)]
                guard = 0
                while stack and guard < 200000:
                    guard += 1
                    blk, lvl = stack.pop()
                    if blk >= self.agblocks:
                        continue
                    nd = self.src.read(self.vol_off + (ag * self.agblocks + blk) * self.bs, self.bs)
                    if nd[:4] not in (b'AB3B', b'ABTB'):
                        continue
                    nrec = b16(nd, 6)
                    if b16(nd, 4) == 0:
                        for k in range(nrec):
                            s, n = struct.unpack_from('>II', nd, hdr + 8 * k)
                            a = ag * self.agblocks + s
                            for i in range(a, min(a + n, self.nbits)):
                                bm[i >> 3] &= ~(1 << (i & 7)) & 0xFF
                    else:
                        maxr = (self.bs - hdr) // 12
                        for k in range(nrec):
                            stack.append((b32(nd, hdr + maxr * 8 + 4 * k), lvl - 1))
            self._bitmap = bytes(bm)
        return self._bitmap

    # ---- file maps
    def fork(self, ib):
        fo = ib[82]
        size = fo * 8 if fo else self.isize - self.core
        return ib[self.core:self.core + size]

    def nextents(self, ib, le=False):
        if self.nrext64 and not le:
            return b64(ib, 24)
        return (l32 if le else b32)(ib, 76)

    def extents_of(self, ib):
        fmt = ib[5]
        f = self.fork(ib)
        if fmt == 2:
            return decode_extents(f, self.nextents(ib))
        if fmt == 3:
            level, nrec = b16(f, 0), b16(f, 2)
            maxr = (len(f) - 4) // 16
            ptrs = [b64(f, 4 + maxr * 8 + 8 * k) for k in range(nrec)]
            return self._bmbt(ptrs, level)
        return []

    def _bmbt(self, ptrs, level):
        hdr = 72 if self.v5 else 24
        out = []
        todo = list(ptrs)
        guard = 0
        while todo and guard < 100000:
            guard += 1
            blk = self.src.read(self.vol_off + self.fsb_to_lin(todo.pop(0)) * self.bs, self.bs)
            if blk[:4] not in (b'BMA3', b'BMAP'):
                continue
            lvl, nrec = b16(blk, 4), b16(blk, 6)
            if lvl == 0:
                out += decode_extents(blk[hdr:], nrec)
            else:
                maxr = (self.bs - hdr) // 16
                todo += [b64(blk, hdr + maxr * 8 + 8 * k) for k in range(nrec)]
        return sorted(out)

    def runs_from(self, exts, size):
        runs, pos = [], 0
        for so, sbk, cnt, unwritten in sorted(exts):
            fo = so * self.bs
            if fo >= size:
                break
            if fo > pos:
                runs.append((None, fo - pos))
                pos = fo
            n = min(cnt * self.bs, size - fo)
            off = None if unwritten else self.vol_off + self.fsb_to_lin(sbk) * self.bs
            if runs and off is not None and runs[-1][0] is not None and runs[-1][0] + runs[-1][1] == off:
                runs[-1] = (runs[-1][0], runs[-1][1] + n)
            else:
                runs.append((off, n))
            pos = fo + n
        if pos < size:
            runs.append((None, size - pos))
        return runs

    def read_data(self, exts, size, cap=64 * MB):
        out = bytearray()
        for off, n in self.runs_from(exts, min(size, cap)):
            out += self.src.read(off, n) if off is not None else bytes(n)
        return bytes(out)

    # ---- directories
    def _sf_entries(self, data, size):
        out = []
        if len(data) < 6:
            return out
        count, i8 = data[0], data[1]
        isz = 8 if i8 else 4
        p = 2 + isz
        for _ in range(count or i8):
            if p + 3 > len(data):
                break
            nl = data[p]
            name = data[p + 3:p + 3 + nl]
            q = p + 3 + nl
            ft = data[q] if self.ftype else 0
            q += 1 if self.ftype else 0
            ino = int.from_bytes(data[q:q + isz], 'big')
            out.append((ino, name.decode('utf-8', 'replace'), ft))
            p = q + isz
        return out

    def _data_block_entries(self, blk, deleted):
        """Live entries of one directory data block; deleted leftovers go to `deleted`."""
        out = []
        m = blk[:4]
        if m in (b'XDB3', b'XDD3'):
            p = 64
        elif m in (b'XD2B', b'XD2D'):
            p = 16
        else:
            return out
        end = len(blk)
        if m in (b'XDB3', b'XD2B'):              # single-block directory: leaf entries + tail at the end
            cnt = b32(blk, end - 8)
            end = end - 8 - cnt * 8
        while p + 8 <= end:
            if b16(blk, p) == 0xFFFF:            # unused space: look inside it for removed entries
                ln = b16(blk, p + 2)
                if ln < 8 or p + ln > end:
                    break
                self._scan_free(blk, p, p + ln, deleted)
                p += ln
                continue
            ino = b64(blk, p)
            nl = blk[p + 8]
            if nl == 0:
                break
            name = blk[p + 9:p + 9 + nl]
            ft = blk[p + 9 + nl] if self.ftype else 0
            size = (8 + 1 + nl + (1 if self.ftype else 0) + 2 + 7) & ~7
            out.append((ino, name.decode('utf-8', 'replace'), ft))
            p += size
        return out

    def _scan_free(self, blk, a, b, deleted):
        """A removed entry keeps its name and the low 32 bits of its inode number; its first 4
        bytes become the free-space marker."""
        p = a
        while p + 16 <= b:
            nl = blk[p + 8]
            if 1 <= nl <= 255 and p + 9 + nl + (1 if self.ftype else 0) + 2 <= b + 2:
                name = blk[p + 9:p + 9 + nl]
                ft = blk[p + 9 + nl] if self.ftype else 1
                ok = all(c >= 0x20 and c != 0x2F for c in name) and (not self.ftype or 1 <= ft <= 7)
                if ok:
                    try:
                        nm = name.decode('utf-8')
                    except UnicodeDecodeError:
                        nm = None
                    lo = b32(blk, p + 4)
                    if nm and lo:
                        deleted.append((lo, nm, ft))
                        p += (8 + 1 + nl + (1 if self.ftype else 0) + 2 + 7) & ~7
                        continue
            p += 8

    def dir_entries(self, ib, deleted):
        fmt = ib[5]
        size = b64(ib, 56)
        if fmt == 1:
            return self._sf_entries(self.fork(ib), size)
        out = []
        for so, sbk, cnt, unw in self.extents_of(ib):
            if so * self.bs >= LEAF_OFFSET or unw:
                continue
            raw = self.src.read(self.vol_off + self.fsb_to_lin(sbk) * self.bs, cnt * self.bs)
            for k in range(0, len(raw), self.dirblk):
                out += self._data_block_entries(raw[k:k + self.dirblk], deleted)
        return out

    # ---- the log: recent copies of inodes and small directories
    def log_inodes(self):
        """{ino: [(lsn, core bytes (little-endian log format), data fork bytes, fields)]} from the log."""
        if self._log is not None:
            return self._log
        self._log = {}
        if not self.logstart or not self.logblocks:
            return self._log
        base = self.vol_off + self.fsb_to_lin(self.logstart) * self.bs
        total = self.logblocks * self.bs
        pos = 0
        pending = {}                              # tid -> list of op payloads (continued ops joined)
        while pos + 512 <= total:
            hdr = self.src.read(base + pos, 512)
            if hdr[:4] != b'\xfe\xed\xba\xbe':
                pos += 512
                continue
            h_len = b32(hdr, 12)
            lsn = b64(hdr, 16)
            nops = b32(hdr, 40)
            h_size = b32(hdr, 320) or 32768
            ext = max(0, -(-h_len // (64 * 512)) - 1) if h_size > 32768 else 0
            if not 0 < h_len <= 256 * 1024:
                pos += 512
                continue
            exts = [self.src.read(base + pos + 512 * (k + 1), 512) for k in range(ext)]
            body = bytearray(self.src.read(base + pos + 512 * (1 + ext), h_len))
            for i in range(0, len(body), 512):    # put back the first word of every 512-byte block
                bi = i // 512
                if bi < 64:
                    word = hdr[44 + 4 * bi:48 + 4 * bi]
                else:
                    e = exts[bi // 64 - 1] if bi // 64 - 1 < len(exts) else None
                    word = e[4 + 4 * (bi % 64):8 + 4 * (bi % 64)] if e else body[i:i + 4]
                body[i:i + 4] = word
            p = 0
            for _ in range(nops):
                if p + 12 > len(body):
                    break
                tid, ln = struct.unpack_from('>II', body, p)
                flags = body[p + 9]
                data = bytes(body[p + 12:p + 12 + ln])
                p += 12 + ln
                lst = pending.setdefault(tid, [])
                if flags & 0x08 and lst:          # continues the previous op of this transaction
                    lst[-1] += data
                else:
                    lst.append(data)
                if flags & 0x02:                  # commit: take the transaction apart
                    self._items(pending.pop(tid, []), lsn)
            pos += 512 * (1 + ext) + ((h_len + 511) // 512) * 512
        return self._log

    def _items(self, ops, lsn):
        k = 0
        while k < len(ops):
            op = ops[k]
            if len(op) >= 52 and l16(op, 0) == XFS_LI_INODE:
                nreg, fields = l16(op, 2), l32(op, 4)
                ino = l64(op, 16)
                regs = ops[k + 1:k + nreg]
                core = regs[0] if regs else b''
                dfork = regs[1] if len(regs) > 1 and fields & (ILOG_DDATA | ILOG_DEXT | ILOG_DBROOT) else b''
                if core[:2] == b'NI':             # 'IN' in the host (little-endian) byte order
                    self._log.setdefault(ino, []).append((lsn, core, dfork, fields))
                k += max(1, nreg)
            else:
                k += 1

    # ---- scanning
    def walk(self, emit, include_existing, cancel, status=None):
        root = self.read_inode(self.rootino)
        if not root or (b16(root, 2) & S_IFMT) != S_IFDIR:
            raise ValueError('XFS root folder unreadable')
        live = set()
        paths = {self.rootino: ''}
        deleted = []                              # (folder, name, ino or low 32 bits, low_only)
        stack = [self.rootino]
        seen = {self.rootino}
        n_dirs = 0
        while stack:
            if cancel.is_set():
                return
            dino = stack.pop()
            ib = self.read_inode(dino)
            if not ib:
                continue
            n_dirs += 1
            if status and n_dirs % 50 == 0:
                status(f'Reading XFS folders... {n_dirs:,} checked')
            gone = []
            for ino, name, ft in self.dir_entries(ib, gone):
                if name in ('.', '..'):
                    continue
                live.add(ino)
                fib = self.read_inode(ino)
                if not fib:
                    continue
                mode = b16(fib, 2) & S_IFMT
                if mode == S_IFDIR and ino not in seen:
                    seen.add(ino)
                    paths[ino] = f'{paths[dino]}/{name}'
                    stack.append(ino)
                elif mode == S_IFREG and include_existing:
                    size = b64(fib, 56)
                    if size > 0:
                        it = self._file(name, paths[dino] or '/', fib, size, 'Existing', 'Not deleted')
                        if it:
                            emit(it)
            for lo, name, ft in gone:
                deleted.append((paths[dino] or '/', name, lo, True, dino))
        # names of removed entries from older copies of small (shortform) directories in the log
        log = self.log_inodes()
        for dino, versions in log.items():
            if dino not in paths:
                continue
            for lsn, core, dfork, fields in versions:
                if core[5] == 1 and fields & ILOG_DDATA and (l16(core, 2) & S_IFMT) == S_IFDIR:
                    for ino, name, ft in self._sf_entries(dfork, len(dfork)):
                        if ino not in live and name not in ('.', '..'):
                            deleted.append((paths[dino] or '/', name, ino, False, dino))
        if status:
            status(f'Looking for deleted XFS files ({len(deleted):,} names found)...')
        mask = 0xFFFFFFFF
        by_low = {}
        for ino in log:
            by_low.setdefault(ino & mask, []).append(ino)
        done = set()
        no_map = 0
        for folder, name, ino, low_only, dino in deleted:
            if cancel.is_set():
                return
            cands = by_low.get(ino & mask, []) if low_only else [ino]
            cands = [c for c in cands if c not in live and c not in done]
            if not cands:
                if (ino if not low_only else None) not in live:
                    no_map += 1
                continue
            got = self._from_log(cands[0], name, folder)
            if got:
                done.add(cands[0])
                emit(got)
            else:
                no_map += 1
        # deleted files whose names are gone, but whose inodes are in the log
        for ino, versions in log.items():
            if cancel.is_set():
                return
            if ino in live or ino in done or ino in paths:
                continue
            cur = self.read_inode(ino)
            if cur and b16(cur, 2):
                continue                          # still in use
            it = self._from_log(ino, None, '/(name lost)')
            if it:
                done.add(ino)
                emit(it)
        self.names_without_data = no_map

    def _from_log(self, ino, name, folder):
        best = None
        for lsn, core, dfork, fields in self.log_inodes().get(ino, []):
            if (l16(core, 2) & S_IFMT) != S_IFREG or not fields & ILOG_DEXT:
                continue
            n = l32(core, 76)
            if n and len(dfork) >= 16 and l64(core, 56) > 0 and (best is None or lsn >= best[0]):
                best = (lsn, core, dfork, n)
        if not best:
            return None
        lsn, core, dfork, n = best
        size = l64(core, 56)
        exts = decode_extents(dfork, n)
        runs = self.runs_from(exts, size)
        if not any(o is not None for o, _ in runs):
            return None
        mtime = _ts(l32(core, 40))
        if name is None:
            head = next((self.src.read(o, 16) for o, _ in runs if o is not None), b'')
            name = f'inode {ino}.{engine.guess_ext(head) or "bin"}'
            note = 'name lost; data location from the XFS log'
        else:
            note = 'data location from the XFS log'
        it = FoundFile(name, folder, size, runs, 'Deleted', 'Good', mtime, note)
        used = sum(self.bytes_in_use(o, l) for o, l in runs if o is not None)
        total = sum(l for o, l in runs if o is not None) or 1
        it.condition = 'Good' if used == 0 else 'Overwritten' if used >= total * 0.9 else 'Partially overwritten'
        self.check_condition(self.src, it)
        return it

    def _file(self, name, folder, ib, size, method, cond):
        big = bool(self.v5 and b64(ib, 120) & 8)
        mtime = _ts(b64(ib, 40) if big else b32(ib, 40), big)
        if ib[5] == 1:                            # tiny file stored inside the inode
            it = FoundFile(name, folder, size, [], method, cond, mtime)
            it.inline = self.fork(ib)[:size]
            return it
        runs = self.runs_from(self.extents_of(ib), size)
        return FoundFile(name, folder, size, runs, method, cond, mtime)
