"""
F2FS (Flash-Friendly File System: some Android phones and memory cards, some Linux devices).

  Live files      through the node address table (incl. the copies kept in the checkpoint)
  Deleted files   F2FS never edits in place: a deleted file's old inode block usually survives in
                  the main area until its segment is cleaned. The segment summary area says which
                  node each block held, so old inodes can be found without reading the whole drive.
                  F2FS inodes also store their own name and parent folder, so names come back even
                  when the directory entry is gone. Removed directory entries stay readable too.
  Not covered     F2FS compression, and files encrypted by Android (file-based encryption) - their
                  contents are unreadable without the phone's keys.
"""
import datetime
import struct

import engine
from engine import FoundFile, MB

F2FS_MAGIC = 0xF2F52010
S_IFMT, S_IFDIR, S_IFREG = 0xF000, 0x4000, 0x8000
NAT_ENTRY = 9
NULL_ADDR, NEW_ADDR = 0, 0xFFFFFFFF


def u16(b, o): return struct.unpack_from('<H', b, o)[0]
def u32(b, o): return struct.unpack_from('<I', b, o)[0]
def u64(b, o): return struct.unpack_from('<Q', b, o)[0]


class F2fsVolume(engine._BitmapAlloc, engine._VolumeBase):
    kind = 'F2FS'

    @staticmethod
    def probe_at(src, off):
        sb = src.read(off + 1024, 8)
        return len(sb) == 8 and u32(sb, 0) == F2FS_MAGIC

    @staticmethod
    def probe(bs):
        return False

    def __init__(self, src, vol_off, bs=None):
        self.src = src
        self.vol_off = vol_off
        sb = src.read(vol_off + 1024, 3072)
        if u32(sb, 0) != F2FS_MAGIC:
            sb = src.read(vol_off + 4096 + 1024, 3072)          # second superblock copy
        if u32(sb, 0) != F2FS_MAGIC:
            raise ValueError('not F2FS')
        self.bs = 1 << u32(sb, 16)
        if self.bs != 4096:
            raise ValueError('unsupported F2FS block size')
        self.cluster_size = self.bs
        self.bps = 1 << u32(sb, 20)                              # blocks per segment
        self.block_count = u64(sb, 36)
        self.seg_main = u32(sb, 68)
        self.seg_nat = u32(sb, 60)
        self.cp_blk, self.sit_blk, self.nat_blk, self.ssa_blk, self.main_blk = struct.unpack_from('<5I', sb, 76)
        self.root_ino = u32(sb, 96)
        self.cp_payload = u32(sb, 1664)
        self.feature = u32(sb, 2180)
        self.label = sb[124:124 + 1024].decode('utf-16-le', 'replace').split('\x00')[0]
        self.size_bytes = self.block_count * self.bs
        self.encrypted = bool(self.feature & 0x1)
        self.compression = bool(self.feature & 0x2000)
        self.kind = 'F2FS' + (' (encryption enabled)' if self.encrypted else '')
        self._load_checkpoint()
        self.nbits = self.block_count
        self.bit_base = 0
        self.max_cluster = self.block_count - 1
        self._bitmap = None
        self._ssa = None
        self.names_without_data = 0

    # ---- checkpoint, NAT
    def _blk(self, n):
        return self.src.read(self.vol_off + n * self.bs, self.bs)

    def _load_checkpoint(self):
        best = None
        for start in (self.cp_blk, self.cp_blk + self.bps):
            cp = self._blk(start)
            ver = u64(cp, 0)
            total = u32(cp, 136)
            if not 0 < total < 1024:
                continue
            tail = self._blk(start + total - 1)
            if u64(tail, 0) != ver:
                continue
            if best is None or ver > best[0]:
                best = (ver, start, cp)
        if best is None:
            raise ValueError('no valid F2FS checkpoint')
        self.cp_ver, start, cp = best
        self.cp = cp
        flags = u32(cp, 132)
        sit_bm, nat_bm = u32(cp, 156), u32(cp, 160)
        if flags & 0x400:                                        # CP_LARGE_NAT_BITMAP
            self.nat_bitmap = cp[192 + 4:192 + 4 + nat_bm]
        elif self.cp_payload:
            self.nat_bitmap = cp[192:192 + nat_bm]
        else:
            self.nat_bitmap = cp[192 + sit_bm:192 + sit_bm + nat_bm]
        self.cur_node = [(u32(cp, 36 + 4 * k), u16(cp, 68 + 2 * k)) for k in range(3)]
        self.cur_data = [(u32(cp, 84 + 4 * k), u16(cp, 116 + 2 * k)) for k in range(3)]
        # the NAT journal: newest NAT entries kept in the checkpoint's data summaries
        self.nat_journal = {}
        sum_start = start + u32(cp, 140)
        blk = self._blk(sum_start)
        if flags & 0x4:                                          # compact summaries
            n = u16(blk, 0)
            for k in range(min(n, 300)):
                e = blk[2 + 13 * k:15 + 13 * k]
                self.nat_journal[u32(e, 0)] = (e[4], u32(e, 5), u32(e, 9))
        else:
            n = u16(blk, 3584)
            for k in range(min(n, 38)):
                e = blk[3586 + 13 * k:3599 + 13 * k]
                self.nat_journal[u32(e, 0)] = (e[4], u32(e, 5), u32(e, 9))
        self.per_nat = self.bs // NAT_ENTRY

    def nat_lookup(self, nid):
        """(version, ino, block address) for a node id."""
        if nid in self.nat_journal:
            return self.nat_journal[nid]
        b = nid // self.per_nat
        seg_off = b // self.bps
        blk = self.nat_blk + seg_off * 2 * self.bps + b % self.bps
        if b // 8 < len(self.nat_bitmap) and (self.nat_bitmap[b // 8] >> (7 - b % 8)) & 1:
            blk += self.bps
        raw = self._blk(blk)
        e = (nid % self.per_nat) * NAT_ENTRY
        return raw[e], u32(raw, e + 1), u32(raw, e + 5)

    def node(self, nid):
        ver, ino, addr = self.nat_lookup(nid)
        if addr in (NULL_ADDR, NEW_ADDR) or addr >= self.block_count:
            return None
        n = self._blk(addr)
        return n if u32(n, self.bs - 24) == nid else None

    # ---- geometry
    def cluster_offset(self, b):
        return self.vol_off + b * self.bs

    def cluster_index(self, off):
        return (off - self.vol_off) // self.bs

    def carve_base(self):
        return self.vol_off + self.main_blk * self.bs

    @property
    def bitmap(self):
        """Valid blocks from the segment information table (SIT)."""
        if self._bitmap is None:
            bm = bytearray(b'\xff' * (-(-self.block_count // 8)))
            per = self.bs // 74                                  # SIT entries per block
            sit_bm = self.cp[192:192 + u32(self.cp, 156)] if not self.cp_payload else \
                self._blk(self.cp_blk + 1)[:u32(self.cp, 156)]
            for blk_i in range(-(-self.seg_main // per)):
                pos = self.sit_blk + (blk_i // self.bps) * 2 * self.bps + blk_i % self.bps
                if blk_i // 8 < len(sit_bm) and (sit_bm[blk_i // 8] >> (7 - blk_i % 8)) & 1:
                    pos += self.bps
                raw = self._blk(pos)
                for k in range(per):
                    seg = blk_i * per + k
                    if seg >= self.seg_main:
                        break
                    e = raw[74 * k:74 * k + 74]
                    valid = e[2:66]
                    first = self.main_blk + seg * self.bps
                    for i in range(self.bps):
                        if not (valid[i >> 3] >> (7 - (i & 7))) & 1:
                            b = first + i
                            bm[b >> 3] &= ~(1 << (b & 7)) & 0xFF
            self._bitmap = bytes(bm)
        return self._bitmap

    # ---- inodes and file maps
    def _layout(self, ib):
        inline = ib[3]
        extra = (u16(ib, 360) // 4) if inline & 0x20 else 0
        xattr = 0
        if inline & 0x01:
            xattr = (u16(ib, 362) // 4) if (inline & 0x20 and self.feature & 0x40) else 50
        n_addr = 923 - extra - xattr
        return inline, extra, n_addr

    def file_runs(self, ib, size, lookup=None):
        """Runs on the drive for an inode, following direct / indirect node blocks."""
        lookup = lookup or self.node
        inline, extra, n_addr = self._layout(ib)
        addrs = [u32(ib, 360 + 4 * (extra + k)) for k in range(n_addr)]
        need = -(-size // self.bs)
        if len(addrs) < need:
            nids = [u32(ib, 4052 + 4 * k) for k in range(5)]
            for k, nid in enumerate(nids):
                if len(addrs) >= need:
                    break
                depth = 0 if k < 2 else 1 if k < 4 else 2
                addrs += self._walk_node(nid, depth, lookup, need - len(addrs))
        runs, pos = [], 0
        for a in addrs[:need]:
            n = min(self.bs, size - pos)
            off = None if a in (NULL_ADDR, NEW_ADDR, 0xFFFFFFFE) or a >= self.block_count \
                else self.vol_off + a * self.bs
            if runs and off is not None and runs[-1][0] is not None and runs[-1][0] + runs[-1][1] == off:
                runs[-1] = (runs[-1][0], runs[-1][1] + n)
            elif runs and off is None and runs[-1][0] is None:
                runs[-1] = (None, runs[-1][1] + n)
            else:
                runs.append((off, n))
            pos += n
        if pos < size:
            runs.append((None, size - pos))
        return runs

    def _walk_node(self, nid, depth, lookup, limit):
        if not nid:
            return [0] * min(limit, 1018 * (1018 ** depth))
        n = lookup(nid)
        if n is None:
            return [0] * min(limit, 1018 * (1018 ** depth))
        vals = [u32(n, 4 * k) for k in range(1018)]
        if depth == 0:
            return vals[:limit]
        out = []
        for c in vals:
            if len(out) >= limit:
                break
            out += self._walk_node(c, depth - 1, lookup, limit - len(out))
        return out

    def inline_data(self, ib, size):
        inline, extra, n_addr = self._layout(ib)
        start = 360 + 4 * (extra + 1)
        return ib[start:start + min(size, 4 * (n_addr - 1))]

    # ---- directories
    @staticmethod
    def _dentries(raw, nr, bitmap_off, dent_off, name_off, deleted):
        out = []
        bm = raw[bitmap_off:bitmap_off + -(-nr // 8)]
        k = 0
        while k < nr:
            used = (bm[k >> 3] >> (k & 7)) & 1
            e = dent_off + 11 * k
            h, ino, nl, ft = struct.unpack_from('<IIHB', raw, e)
            slots = max(1, -(-nl // 8))
            name = raw[name_off + 8 * k:name_off + 8 * k + nl]
            if used:
                if nl:
                    out.append((ino, name.decode('utf-8', 'replace'), ft))
                k += slots
                continue
            if ino and 0 < nl <= 255 and k + slots <= nr and ft in (1, 2, 7) and \
                    all(c >= 0x20 and c != 0x2F for c in name):
                try:
                    deleted.append((ino, name.decode('utf-8'), ft))
                    k += slots
                    continue
                except UnicodeDecodeError:
                    pass
            k += 1
        return out

    def dir_entries(self, ib, deleted):
        inline, extra, n_addr = self._layout(ib)
        size = u64(ib, 16)
        if inline & 0x04:                                        # inline dentries inside the inode
            data = self.inline_data(ib, 4 * (n_addr - 1))
            maxd = len(data)
            nr = (maxd * 8) // ((11 + 8) * 8 + 1)
            bmsize = -(-nr // 8)
            reserved = maxd - (nr * (11 + 8) + bmsize)
            return self._dentries(data, nr, 0, bmsize + reserved, bmsize + reserved + 11 * nr, deleted)
        out = []
        for off, n in self.file_runs(ib, size):
            if off is None:
                continue
            raw = self.src.read(off, n)
            for k in range(0, len(raw) - 4095, 4096):
                out += self._dentries(raw[k:k + 4096], 214, 0, 30, 30 + 214 * 11, deleted)
        return out

    # ---- old nodes (deleted files)
    def ssa_index(self):
        """{nid: [block address, ...]} for blocks the segment summaries say held node `nid`."""
        if self._ssa is None:
            idx = {}
            for seg in range(self.seg_main):
                s = self._blk(self.ssa_blk + seg)
                if not s[4095] & 1:                              # footer entry type: 1 = node segment
                    continue
                first = self.main_blk + seg * self.bps
                for k in range(min(512, self.bps)):
                    nid = u32(s, 7 * k)
                    if nid:
                        idx.setdefault(nid, []).append(first + k)
            # the segments being written at checkpoint time have no summary block yet: read their
            # block footers directly (3 segments, so this stays quick)
            for seg, _off in self.cur_node:
                if seg >= self.seg_main:
                    continue
                first = self.main_blk + seg * self.bps
                raw = self.src.read(self.vol_off + first * self.bs, self.bps * self.bs)
                for k in range(self.bps):
                    nid = u32(raw, k * self.bs + self.bs - 24)
                    if nid and first + k not in idx.get(nid, ()):
                        idx.setdefault(nid, []).append(first + k)
            self._ssa = idx
        return self._ssa

    def old_node(self, nid, want_inode=False):
        """The newest surviving copy of node `nid` (current or old)."""
        best = None
        for addr in self.ssa_index().get(nid, []):
            n = self._blk(addr)
            f_nid, f_ino, _flag, cp = struct.unpack_from('<IIIQ', n, self.bs - 24)
            if f_nid != nid or (want_inode and f_ino != nid):
                continue
            if best is None or cp > best[0]:
                best = (cp, n)
        return best[1] if best else None

    # ---- scanning
    def _mtime(self, ib):
        try:
            return datetime.datetime.fromtimestamp(u64(ib, 48))
        except (OverflowError, OSError, ValueError):
            return None

    def walk(self, emit, include_existing, cancel, status=None):
        root = self.node(self.root_ino)
        if root is None or (u16(root, 0) & S_IFMT) != S_IFDIR:
            raise ValueError('F2FS root folder unreadable')
        paths = {self.root_ino: ''}
        live = set()
        deleted = []
        stack = [self.root_ino]
        n_dirs = 0
        while stack:
            if cancel.is_set():
                return
            dino = stack.pop()
            ib = self.node(dino)
            if ib is None:
                continue
            n_dirs += 1
            if status and n_dirs % 50 == 0:
                status(f'Reading F2FS folders... {n_dirs:,} checked')
            gone = []
            for ino, name, ft in self.dir_entries(ib, gone):
                if name in ('.', '..'):
                    continue
                live.add(ino)
                if ft == 2:
                    if ino not in paths:
                        paths[ino] = f'{paths[dino]}/{name}'
                        stack.append(ino)
                    continue
                if include_existing and ft == 1:
                    fib = self.node(ino)
                    if fib is None or (u16(fib, 0) & S_IFMT) != S_IFREG:
                        continue
                    size = u64(fib, 16)
                    if size > 0:
                        it = self._file(name, paths[dino] or '/', fib, size, 'Existing', 'Not deleted')
                        if it:
                            emit(it)
            for ino, name, ft in gone:
                if ft == 1:
                    deleted.append((ino, name, paths[dino] or '/'))
        if status:
            status(f'Looking for deleted F2FS files ({len(deleted):,} names found)...')
        done = set()
        no_map = 0
        for ino, name, folder in deleted:
            if cancel.is_set():
                return
            if ino in live or ino in done:
                continue
            it = self._deleted(ino, name, folder)
            if it:
                done.add(ino)
                emit(it)
            else:
                no_map += 1
        # old inodes nobody points at any more (names from the inode itself)
        for nid in list(self.ssa_index()):
            if cancel.is_set():
                return
            if nid in live or nid in done or nid in paths or nid < 4:
                continue
            ver, ino, addr = self.nat_lookup(nid)
            if addr not in (NULL_ADDR,):
                continue                                         # still allocated
            it = self._deleted(nid, None, None, paths)
            if it:
                done.add(nid)
                emit(it)
        self.names_without_data = no_map

    def _deleted(self, ino, name, folder, paths=None):
        ib = self.old_node(ino, want_inode=True)
        if ib is None or (u16(ib, 0) & S_IFMT) != S_IFREG:
            return None
        size = u64(ib, 16)
        if size <= 0:
            return None
        if name is None:                                         # the inode keeps its own name
            nl = u32(ib, 88)
            name = ib[92:92 + min(nl, 255)].decode('utf-8', 'replace') or f'inode {ino}'
            pino = u32(ib, 84)
            p = (paths or {}).get(pino)
            folder = (p or '/') if p is not None else '/(folder lost)'
        note = 'from an old copy of the file\'s inode'
        it = self._file(name, folder or '/', ib, size, 'Deleted', None, lookup=lambda nid: self.old_node(nid))
        if it is None:
            return None
        it.note = note
        if it.inline is None:
            used = sum(self.bytes_in_use(o, l) for o, l in it.runs if o is not None)
            total = sum(l for o, l in it.runs if o is not None) or 1
            it.condition = 'Good' if used == 0 else 'Overwritten' if used >= total * 0.9 else 'Partially overwritten'
            self.check_condition(self.src, it)
        return it

    def _file(self, name, folder, ib, size, method, cond, lookup=None):
        inline, extra, n_addr = self._layout(ib)
        if self.encrypted and u32(ib, 80) & 0x800:               # FS_ENCRYPT_FL: Android file encryption
            it = FoundFile(name, folder, size, [], method, 'Encrypted (not supported)', self._mtime(ib))
            return it
        if inline & 0x02:
            it = FoundFile(name, folder, size, [], method, cond or 'Good', self._mtime(ib))
            it.inline = self.inline_data(ib, size)[:size]
            return it
        runs = self.file_runs(ib, size, lookup)
        if not any(o is not None for o, _ in runs):
            return None
        return FoundFile(name, folder, size, runs, method, cond or 'Good', self._mtime(ib))
