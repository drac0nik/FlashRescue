"""
Linux filesystems for FlashRescue: ext2/3/4 and btrfs (what NAS boxes and Linux PCs use).

ext4
  * Live files with names, folders and dates (extents, ext2/3 block maps, inline data)
  * Deleted files: names come from leftovers in directory blocks; ext4 wipes a deleted file's
    data map, so the map is taken from older copies of its inode kept in the filesystem journal
    (the same trick as extundelete / ext4magic)
  * Free-space map from the block bitmaps, for deep scans and "is it overwritten?" checks

btrfs (see BtrfsVolume below)
"""

import os
import struct
import zlib
import bisect
import datetime

import engine
from engine import u16, u32, u64, FoundFile, human_size, KB, MB

BE32 = lambda b, o: struct.unpack_from('>I', b, o)[0]


def unix_time(t):
    try:
        return datetime.datetime.fromtimestamp(t) if t else None
    except (OverflowError, OSError, ValueError):
        return None


# ======================================================================================
# ext2 / ext3 / ext4
# ======================================================================================

S_IFDIR, S_IFREG, S_IFLNK = 0x4000, 0x8000, 0xA000
EXTENTS_FL, INLINE_FL = 0x80000, 0x10000000


class Ext4Volume(engine._BitmapAlloc, engine._VolumeBase):
    kind = 'ext4'

    @staticmethod
    def probe_at(src, off):
        sb = src.read(off + 1024, 1024)
        return len(sb) == 1024 and u16(sb, 56) == 0xEF53 and u32(sb, 24) <= 6

    @staticmethod
    def probe(bs):
        return False            # needs the superblock at +1024; engine calls probe_at

    def __init__(self, src, vol_off, bs=None, sb_override=None):
        self.src = src
        self.vol_off = vol_off
        sb = src.read(vol_off + 1024, 1024)
        backup_group = 0
        if u16(sb, 56) != 0xEF53 and sb_override:
            sb = sb_override                     # primary superblock destroyed: use a backup copy
            backup_group = u16(sb, 90)
        if u16(sb, 56) != 0xEF53:
            raise ValueError('not ext')
        self.sb = sb
        self.bs = bs_ = 1024 << u32(sb, 24)
        self.cluster_size = bs_
        self.inodes_count = u32(sb, 0)
        self.first_data_block = u32(sb, 20)
        self.blocks_per_group = u32(sb, 32)
        self.inodes_per_group = u32(sb, 40)
        self.rev = u32(sb, 76)
        self.inode_size = u16(sb, 88) if self.rev >= 1 else 128
        self.compat, self.incompat, self.ro_compat = u32(sb, 92), u32(sb, 96), u32(sb, 100)
        self.is64 = bool(self.incompat & 0x80)
        self.desc_size = (u16(sb, 254) or 64) if self.is64 else 32
        blocks = u32(sb, 4) | ((u32(sb, 336) << 32) if self.is64 else 0)
        self.blocks_count = blocks
        self.size_bytes = blocks * bs_
        self.max_cluster = blocks - 1
        self.bit_base = self.first_data_block
        self.nbits = blocks - self.first_data_block
        self.label = sb[120:136].split(b'\x00')[0].decode('utf-8', 'replace')
        self.has_journal = bool(self.compat & 0x4)
        self.filetype = bool(self.incompat & 0x2)
        if self.incompat & 0x40:
            self.kind = 'ext4'
        elif self.has_journal:
            self.kind = 'ext3'
        else:
            self.kind = 'ext2'
        if not (self.inodes_per_group and self.blocks_per_group) or self.inode_size < 128:
            raise ValueError('bad ext superblock')
        ngroups = -(-(blocks - self.first_data_block) // self.blocks_per_group)
        self.ngroups = ngroups
        gdt_block = backup_group * self.blocks_per_group + self.first_data_block + 1
        raw = src.read(vol_off + gdt_block * bs_, ngroups * self.desc_size)
        self.groups = []
        for g in range(ngroups):
            d = raw[g * self.desc_size:(g + 1) * self.desc_size]
            if len(d) < 32:
                break
            bb, ib, it = u32(d, 0), u32(d, 4), u32(d, 8)
            if self.is64 and len(d) >= 64:
                bb |= u32(d, 0x20) << 32
                ib |= u32(d, 0x24) << 32
                it |= u32(d, 0x28) << 32
            self.groups.append((bb, ib, it, u16(d, 18)))
        self._bitmap = None
        self._journal_index = None

    # ---- geometry
    def cluster_offset(self, b):
        return self.vol_off + b * self.bs

    def cluster_index(self, off):
        return (off - self.vol_off) // self.bs

    def carve_base(self):
        return self.vol_off

    @property
    def bitmap(self):
        if self._bitmap is None:
            per = self.blocks_per_group // 8
            parts = []
            for g, (bb, ib, it, flags) in enumerate(self.groups):
                if flags & 0x2:                 # BLOCK_UNINIT: nothing allocated in this group yet
                    parts.append(b'\x00' * per)
                else:
                    parts.append(self.src.read(self.vol_off + bb * self.bs, per).ljust(per, b'\xff'))
            self._bitmap = b''.join(parts)
        return self._bitmap

    # ---- inodes
    def inode_location(self, ino):
        g, idx = divmod(ino - 1, self.inodes_per_group)
        if g >= len(self.groups):
            return None
        table = self.groups[g][2]
        off = idx * self.inode_size
        return table + off // self.bs, off % self.bs

    def read_inode(self, ino):
        loc = self.inode_location(ino)
        if not loc:
            return None
        blk, within = loc
        return self.src.read(self.vol_off + blk * self.bs + within, self.inode_size)

    @staticmethod
    def inode_size_of(ib):
        return u32(ib, 4) | (u32(ib, 108) << 32)

    def block_runs(self, ib, limit_blocks=None):
        """[(physical block or None for a hole, count)] in file order, or None if unreadable."""
        flags = u32(ib, 32)
        iblock = ib[40:100]
        if flags & INLINE_FL:
            return 'inline'
        if flags & EXTENTS_FL:
            out = []
            if not self._extents(iblock, out, 0):
                return None
            out.sort()
            runs = []
            pos = 0
            for lblk, cnt, pblk in out:
                if lblk > pos:
                    runs.append((None, lblk - pos))
                runs.append((pblk, cnt))
                pos = lblk + cnt
            return runs
        return self._blockmap(iblock, limit_blocks)

    def _extents(self, node, out, depth):
        if len(node) < 12 or u16(node, 0) != 0xF30A or depth > 8:
            return False
        entries, dep = u16(node, 2), u16(node, 6)
        if entries > (len(node) - 12) // 12:
            return False
        for k in range(entries):
            e = node[12 + 12 * k: 24 + 12 * k]
            if dep == 0:
                lblk, ln, hi, lo = struct.unpack('<IHHI', e)
                if ln > 32768:
                    ln -= 32768                 # unwritten extent (reads as zeros)
                    out.append((lblk, ln, None))
                    continue
                out.append((lblk, ln, (hi << 32) | lo))
            else:
                lblk, lo, hi = struct.unpack_from('<IIH', e, 0)
                child = self.src.read(self.vol_off + ((hi << 32) | lo) * self.bs, self.bs)
                if not self._extents(child, out, depth + 1):
                    return False
        return True

    def _blockmap(self, iblock, limit_blocks):
        ptrs = struct.unpack('<15I', iblock)
        per = self.bs // 4
        runs = []
        count = [0]
        cap = limit_blocks or (1 << 31)

        def add(p):
            if count[0] >= cap:
                return
            count[0] += 1
            b = p or None
            if runs and ((runs[-1][0] is None and b is None) or
                         (b is not None and runs[-1][0] is not None and runs[-1][0] + runs[-1][1] == b)):
                runs[-1] = (runs[-1][0], runs[-1][1] + 1)
            else:
                runs.append((b, 1))

        def indirect(blk, level):
            if count[0] >= cap:
                return
            if not blk:
                for _ in range(per ** level):
                    add(0)
                    if count[0] >= cap:
                        return
                return
            data = self.src.read(self.vol_off + blk * self.bs, self.bs)
            for p in struct.unpack('<%dI' % per, data):
                if level == 1:
                    add(p)
                else:
                    indirect(p, level - 1)
                if count[0] >= cap:
                    return

        for p in ptrs[:12]:
            add(p)
        for lvl, p in ((1, ptrs[12]), (2, ptrs[13]), (3, ptrs[14])):
            if count[0] >= cap:
                break
            indirect(p, lvl)
        return runs

    def file_runs(self, ib, size):
        """Device (offset, length) runs for a file's bytes."""
        need = -(-size // self.bs)
        br = self.block_runs(ib, need)
        if br is None or br == 'inline':
            return br
        out = []
        left = size
        for blk, cnt in br:
            if left <= 0:
                break
            n = min(cnt * self.bs, left)
            off = None if blk is None else self.vol_off + blk * self.bs
            if out and off is not None and out[-1][0] is not None and out[-1][0] + out[-1][1] == off:
                out[-1] = (out[-1][0], out[-1][1] + n)
            else:
                out.append((off, n))
            left -= n
        if left > 0:
            out.append((None, left))
        return out

    def read_file(self, ib, cap=64 * MB):
        size = min(self.inode_size_of(ib), cap)
        if u32(ib, 32) & INLINE_FL:
            return ib[40:100][:size]
        runs = self.file_runs(ib, size)
        if not runs:
            return b''
        return b''.join(b'\x00' * ln if off is None else self.src.read(off, ln) for off, ln in runs)

    # ---- directories
    def dir_entries(self, data, with_deleted=True):
        """(inode, name, type, deleted) for every entry, including leftovers of deleted ones."""
        out = []
        bs = self.bs
        for base in range(0, len(data), bs):
            blk = data[base:base + bs]
            p = 0
            while p + 8 <= len(blk):
                ino, rec_len = u32(blk, p), u16(blk, p + 4)
                nlen = blk[p + 6] if self.filetype else u16(blk, p + 6)
                ftype = blk[p + 7] if self.filetype else 0
                if rec_len < 8 or p + rec_len > len(blk) or rec_len % 4:
                    break
                real = (8 + nlen + 3) & ~3
                if ino and nlen and 8 + nlen <= rec_len:
                    name = blk[p + 8:p + 8 + nlen].decode('utf-8', 'replace')
                    if name not in ('.', '..'):
                        out.append((ino, name, ftype, False))
                elif ino == 0 and nlen and with_deleted:          # first entry of a block, deleted
                    name = blk[p + 8:p + 8 + nlen].decode('utf-8', 'replace')
                    out.append((0, name, ftype, True))
                if with_deleted and rec_len > real + 8:
                    self._slack(blk, p + (real if ino or nlen else 8), p + rec_len, out)
                p += rec_len
        return out

    def _slack(self, blk, start, end, out):
        q = (start + 3) & ~3
        while q + 12 <= end:
            ino, rl = u32(blk, q), u16(blk, q + 4)
            nlen = blk[q + 6]
            ft = blk[q + 7] if self.filetype else 0
            if 0 < ino <= self.inodes_count and 0 < nlen and 12 <= rl and q + 8 + nlen <= end and ft <= 7:
                raw = blk[q + 8:q + 8 + nlen]
                if all(32 <= c < 127 or c >= 0x80 for c in raw) and b'/' not in raw:
                    out.append((ino, raw.decode('utf-8', 'replace'), ft, True))
                    q += (8 + nlen + 3) & ~3
                    continue
            q += 4

    # ---- journal (older copies of inodes)
    def _journal(self):
        if self._journal_index is not None:
            return self._journal_index
        self._journal_index = {}
        jino = u32(self.sb, 224) or 8
        if not self.has_journal:
            return self._journal_index
        ib = self.read_inode(jino)
        if not ib or not (u16(ib, 0) & 0xF000):
            return self._journal_index
        jsize = self.inode_size_of(ib)
        runs = self.file_runs(ib, jsize)
        if not runs or runs == 'inline':
            return self._journal_index
        bs = self.bs
        # journal block number -> device offset
        jmap = []
        for off, ln in runs:
            for k in range(ln // bs):
                jmap.append(None if off is None else off + k * bs)
        if not jmap:
            return self._journal_index
        jsb = self.src.read(jmap[0], 1024)
        if BE32(jsb, 0) != 0xC03B3998:
            return self._journal_index
        jfirst = BE32(jsb, 20) or 1
        incompat = BE32(jsb, 40)
        csum3 = bool(incompat & 0x10)
        is64 = bool(incompat & 0x2)
        csum = bool(incompat & 0x18) or bool(BE32(jsb, 36) & 0x1)
        table_ranges = []
        per_table = -(-self.inodes_per_group * self.inode_size // bs)
        for g, (bb, ibm, it, fl) in enumerate(self.groups):
            table_ranges.append((it, it + per_table, g))
        table_ranges.sort()
        starts = [r[0] for r in table_ranges]
        n = len(jmap)
        i = jfirst
        while i < n:
            off = jmap[i]
            blk = self.src.read(off, bs) if off is not None else b''
            if len(blk) < 12 or BE32(blk, 0) != 0xC03B3998 or BE32(blk, 4) != 1:
                i += 1
                continue
            seq = BE32(blk, 8)
            p = 12
            k = 0
            end = bs - (4 if csum else 0)
            while p + 8 <= end:
                if csum3:
                    fsblk, flags, hi = BE32(blk, p), BE32(blk, p + 4), BE32(blk, p + 8)
                    p += 16
                else:
                    fsblk, flags = BE32(blk, p), struct.unpack_from('>H', blk, p + 6)[0]
                    hi = BE32(blk, p + 8) if is64 else 0
                    p += 12 if is64 else 8
                if is64 or csum3:
                    fsblk |= hi << 32
                if not flags & 0x2:
                    p += 16                       # tag followed by the journal UUID
                k += 1
                data_j = i + k
                if data_j >= n:
                    break
                t = bisect.bisect_right(starts, fsblk) - 1
                if t >= 0 and fsblk < table_ranges[t][1]:
                    self._journal_index.setdefault(fsblk, []).append((seq, jmap[data_j], bool(flags & 0x1)))
                if flags & 0x8:
                    break
            i += k + 1
        for v in self._journal_index.values():
            v.sort(reverse=True)
        return self._journal_index

    def journal_inode(self, ino):
        """Most recent journal copy of this inode that still has a data map."""
        loc = self.inode_location(ino)
        if not loc:
            return None
        blk, within = loc
        for seq, off, escaped in self._journal().get(blk, []):
            ib = self.src.read(off + within, self.inode_size)
            if escaped and within == 0:
                ib = struct.pack('>I', 0xC03B3998) + ib[4:]
            mode = u16(ib, 0)
            if (mode & 0xF000) != S_IFREG or u16(ib, 26) == 0 or u32(ib, 20):
                continue
            if self.inode_size_of(ib) == 0:
                continue
            runs = self.block_runs(ib, 1)
            if runs and runs != 'inline' and any(b is not None for b, c in runs):
                return ib
        return None

    # ---- walking
    def walk(self, emit, include_existing, cancel, status=None):
        root = self.read_inode(2)
        if not root or (u16(root, 0) & 0xF000) != S_IFDIR:
            raise ValueError('root folder unreadable')
        stack = [('', 2)]
        seen_dirs = {2}
        live_inodes = set()
        deleted = []                     # (folder, name, ino)
        dirs = 0
        while stack:
            if cancel.is_set():
                return
            path, dino = stack.pop()
            ib = self.read_inode(dino)
            if not ib:
                continue
            data = self.read_file(ib, 256 * MB)
            dirs += 1
            if status and dirs % 50 == 0:
                status(f'Reading {self.kind} folders... {dirs:,} checked')
            for ino, name, ftype, gone in self.dir_entries(data):
                if gone:
                    deleted.append((path or '/', name, ino, ftype))
                    continue
                live_inodes.add(ino)
                if ftype == 2 or (not self.filetype and ino not in seen_dirs and
                                  (u16(self.read_inode(ino) or b'\0\0', 0) & 0xF000) == S_IFDIR):
                    if ino not in seen_dirs and ino <= self.inodes_count:
                        seen_dirs.add(ino)
                        stack.append((f'{path}/{name}', ino))
                    continue
                if include_existing and (ftype in (0, 1)):
                    fib = self.read_inode(ino)
                    if not fib or (u16(fib, 0) & 0xF000) != S_IFREG:
                        continue
                    size = self.inode_size_of(fib)
                    if size <= 0:
                        continue
                    item = self._item(name, path or '/', fib, size, 'Existing', 'Not deleted')
                    if item:
                        emit(item)
        # deleted files: names from directory leftovers, data maps from the inode or the journal
        if status:
            status(f'Looking for deleted {self.kind} files ({len(deleted):,} names found)...')
        done = set()
        no_map = 0
        for folder, name, ino, ftype in deleted:
            if cancel.is_set():
                return
            if ino == 0 or ino in live_inodes or ino in done or ftype == 2:
                continue
            done.add(ino)
            ib = self.read_inode(ino)
            if not ib:
                continue
            mode = u16(ib, 0)
            if mode and (mode & 0xF000) not in (S_IFREG, 0):
                continue
            src_note = ''
            use = None
            if (mode & 0xF000) == S_IFREG and self.inode_size_of(ib) > 0:
                br = self.block_runs(ib, 1)
                if br and br != 'inline' and any(b is not None for b, c in br):
                    use = ib                  # ext2/3-style delete left the map in place
            if use is None:
                use = self.journal_inode(ino)
                src_note = 'data location taken from the filesystem journal'
            if use is None:
                no_map += 1
                continue
            size = self.inode_size_of(use)
            item = self._item(name, folder, use, size, 'Deleted', None, src_note, deleted_mtime=u32(ib, 20))
            if item:
                emit(item)
        # deleted files whose names are gone, but whose inodes survive in the journal
        if self.has_journal:
            for blk, copies in self._journal().items():
                if cancel.is_set():
                    return
                g = None
                for gg, (bb, ibm, it, fl) in enumerate(self.groups):
                    per_table = -(-self.inodes_per_group * self.inode_size // self.bs)
                    if it <= blk < it + per_table:
                        g = gg
                        break
                if g is None:
                    continue
                first_ino = g * self.inodes_per_group + (blk - self.groups[g][2]) * (self.bs // self.inode_size) + 1
                for k in range(self.bs // self.inode_size):
                    ino = first_ino + k
                    if ino in live_inodes or ino in done or ino < 12:
                        continue
                    cur = self.read_inode(ino)
                    if cur and u16(cur, 26) and not u32(cur, 20):
                        continue                  # still in use: not deleted
                    old = self.journal_inode(ino)
                    if not old:
                        continue
                    done.add(ino)
                    size = self.inode_size_of(old)
                    head = b''
                    runs = self.file_runs(old, min(size, 64))
                    if runs and runs != 'inline' and runs[0][0] is not None:
                        head = self.src.read(runs[0][0], 16)
                    item = self._item(f'inode {ino}.{engine.guess_ext(head) or "bin"}', '/(name lost)', old, size,
                                      'Deleted', None, 'name lost; data location from the journal')
                    if item:
                        emit(item)
        self.names_without_data = no_map

    def _item(self, name, folder, ib, size, method, cond, note='', deleted_mtime=None):
        if u32(ib, 32) & INLINE_FL:
            it = FoundFile(name, folder, size, [], method, cond or 'Good', unix_time(u32(ib, 16)), note)
            it.inline = ib[40:100][:size]
            return it
        runs = self.file_runs(ib, size)
        if not runs or runs == 'inline':
            return None
        it = FoundFile(name, folder, size, runs, method, cond or 'Good', unix_time(u32(ib, 16)), note)
        if cond is None:
            used = sum(self.bytes_in_use(o, l) for o, l in runs if o is not None)
            total = sum(l for o, l in runs if o is not None) or 1
            if used == 0:
                it.condition = 'Good'
            elif used >= total * 0.9:
                it.condition = 'Overwritten'
            else:
                it.condition = 'Partially overwritten'
            self.check_condition(self.src, it)
        return it


# ======================================================================================
# btrfs
# ======================================================================================

BT_MAGIC = b'_BHRfS_M'
HDR = 0x65                                  # btrfs node header size
INODE_ITEM, INODE_REF, DIR_ITEM, DIR_INDEX, EXTENT_DATA = 1, 12, 84, 96, 108
ROOT_ITEM, ROOT_BACKREF, ROOT_REF = 132, 144, 156
EXTENT_ITEM, METADATA_ITEM, BLOCK_GROUP_ITEM, CHUNK_ITEM = 168, 169, 192, 228


def bt_time(b, o):
    return unix_time(u64(b, o))


class _OldItems:
    """Items gathered from old copies of btrfs file-tree leaves (newest generation wins)."""

    def __init__(self):
        self.inodes = {}       # (tree, ino) -> (gen, item bytes)
        self.refs = {}         # (tree, ino) -> (gen, parent, name)
        self.extents = {}      # (tree, ino) -> {file_off: (gen, item bytes)}
        self.leaves = 0

    def add_leaf(self, node, owner):
        gen = u64(node, 0x50)
        n = u32(node, 0x60)
        self.leaves += 1
        for i in range(min(n, 2000)):
            p = HDR + 25 * i
            if p + 25 > len(node):
                break
            oid, typ, off = struct.unpack_from('<QBQ', node, p)
            doff, dsize = u32(node, p + 17), u32(node, p + 21)
            data = node[HDR + doff: HDR + doff + dsize]
            if len(data) < dsize:
                continue
            key = (owner, oid)
            if typ == INODE_ITEM and dsize >= 160:
                if key not in self.inodes or self.inodes[key][0] < gen:
                    self.inodes[key] = (gen, data)
            elif typ == INODE_REF and dsize >= 10:
                nl = u16(data, 8)
                name = data[10:10 + nl].decode('utf-8', 'replace')
                if key not in self.refs or self.refs[key][0] < gen:
                    self.refs[key] = (gen, off, name)
            elif typ == EXTENT_DATA and dsize >= 21:
                d = self.extents.setdefault(key, {})
                if off not in d or d[off][0] < gen:
                    d[off] = (gen, data)


class BtrfsVolume(engine._VolumeBase):
    kind = 'btrfs'

    @staticmethod
    def probe_at(src, off):
        return src.read(off + 0x10040, 8) == BT_MAGIC

    @staticmethod
    def probe(bs):
        return False

    def __init__(self, src, vol_off, bs=None, sb_offset=0x10000):
        self.src = src
        self.vol_off = vol_off
        sb = src.read(vol_off + 0x10000, 4096)
        if sb[0x40:0x48] != BT_MAGIC:
            sb = src.read(vol_off + sb_offset, 4096)     # primary destroyed: use a mirror copy
        if sb[0x40:0x48] != BT_MAGIC:
            raise ValueError('not btrfs')
        self.sb = sb
        self.fsid = sb[0x20:0x30]
        self.generation = u64(sb, 0x48)
        self.root_tree = u64(sb, 0x50)
        self.chunk_root = u64(sb, 0x58)
        self.size_bytes = u64(sb, 0x70)
        self.sectorsize = u32(sb, 0x90)
        self.nodesize = u32(sb, 0x94)
        self.cluster_size = self.sectorsize
        self.max_cluster = self.size_bytes // self.sectorsize
        self.label = sb[0x12B:0x22B].split(b'\x00')[0].decode('utf-8', 'replace')
        self.chunks = []           # (logical, length, physical)
        n = u32(sb, 0xA0)
        arr = sb[0x32B:0x32B + n]
        p = 0
        while p + 17 + 48 <= len(arr):
            oid, typ, logical = struct.unpack_from('<QBQ', arr, p)
            p += 17
            ns = u16(arr, p + 44)
            self._add_chunk(logical, arr[p:p + 48 + 32 * ns])
            p += 48 + 32 * ns
        self._sort_chunks()
        for _k, _o, data in self.iter_tree(self.chunk_root, want=(CHUNK_ITEM,)):
            self._add_chunk(_k[2], data)
        self._sort_chunks()
        self.trees = {}            # tree id -> (root logical, root item)
        for key, _o, data in self.iter_tree(self.root_tree, want=(ROOT_ITEM, ROOT_REF)):
            if key[1] == ROOT_ITEM and len(data) >= 239:
                self.trees[key[0]] = (u64(data, 176), data)
            elif key[1] == ROOT_REF:
                self.trees.setdefault(('ref', key[2]), (key[0], u64(data, 0),
                                                       data[18:18 + u16(data, 16)].decode('utf-8', 'replace')))
        self._alloc = None

    # ---- chunk mapping
    def _add_chunk(self, logical, data):
        if len(data) < 48 + 32:
            return
        length = u64(data, 0)
        phys = u64(data, 48 + 8)
        if not any(c[0] == logical for c in self.chunks):
            self.chunks.append((logical, length, phys))

    def _sort_chunks(self):
        self.chunks.sort()
        self.cstarts = [c[0] for c in self.chunks]

    def to_dev(self, logical):
        i = bisect.bisect_right(self.cstarts, logical) - 1
        if i < 0:
            return None
        lo, ln, ph = self.chunks[i]
        if logical >= lo + ln:
            return None
        return self.vol_off + ph + (logical - lo)

    def to_logical(self, dev):
        rel = dev - self.vol_off
        for lo, ln, ph in self.chunks:
            if ph <= rel < ph + ln:
                return lo + (rel - ph)
        return None

    def read_node(self, logical):
        off = self.to_dev(logical)
        if off is None:
            return None
        node = self.src.read(off, self.nodesize)
        if len(node) < HDR or node[0x20:0x30] != self.fsid or u64(node, 0x30) != logical:
            return None
        return node

    def iter_tree(self, root, want=None, depth=0, seen=None):
        """Yield (key, owner, item data) for every leaf item of the tree rooted at `root`."""
        seen = seen if seen is not None else set()
        if root in seen or depth > 12:
            return
        seen.add(root)
        node = self.read_node(root)
        if node is None:
            return
        n, level = u32(node, 0x60), node[0x64]
        owner = u64(node, 0x58)
        if level == 0:
            for i in range(min(n, 4000)):
                p = HDR + 25 * i
                if p + 25 > len(node):
                    break
                oid, typ, off = struct.unpack_from('<QBQ', node, p)
                if want and typ not in want:
                    continue
                doff, dsize = u32(node, p + 17), u32(node, p + 21)
                yield (oid, typ, off), owner, node[HDR + doff:HDR + doff + dsize]
        else:
            for i in range(min(n, 4000)):
                p = HDR + 33 * i
                if p + 33 > len(node):
                    break
                yield from self.iter_tree(u64(node, p + 17), want, depth + 1, seen)

    # ---- allocation (extent tree)
    def _allocated(self):
        if self._alloc is None:
            rngs = []
            ext = self.trees.get(2)
            if ext:
                for key, _o, data in self.iter_tree(ext[0], want=(EXTENT_ITEM, METADATA_ITEM)):
                    ln = key[2] if key[1] == EXTENT_ITEM else self.nodesize
                    rngs.append((key[0], key[0] + ln))
            rngs.sort()
            merged = []
            for a, b in rngs:
                if merged and a <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], b))
                else:
                    merged.append((a, b))
            self._alloc = merged
            self._astarts = [a for a, b in merged]
        return self._alloc

    def logical_in_use(self, lo, ln):
        al = self._allocated()
        i = max(0, bisect.bisect_right(self._astarts, lo) - 1)
        used = 0
        end = lo + ln
        while i < len(al) and al[i][0] < end:
            a, b = max(lo, al[i][0]), min(end, al[i][1])
            if b > a:
                used += b - a
            i += 1
        return used

    def bytes_in_use(self, off, length):
        lg = self.to_logical(off)
        if lg is None:
            return 0                       # space not handed to any chunk: free
        return self.logical_in_use(lg, length)

    def free_runs(self):
        al = self._allocated()
        out = []
        for lo, ln, ph in self.chunks:
            pos = lo
            i = max(0, bisect.bisect_right(self._astarts, lo) - 1)
            while pos < lo + ln:
                while i < len(al) and al[i][1] <= pos:
                    i += 1
                nxt = al[i][0] if i < len(al) else lo + ln
                if nxt > pos:
                    a, b = pos, min(nxt, lo + ln)
                    out.append((self.vol_off + ph + (a - lo), b - a))
                    pos = b
                if i < len(al) and al[i][0] <= pos:
                    pos = max(pos, al[i][1])
                    i += 1
                elif i >= len(al):
                    break
        used = sorted((ph, ph + ln) for lo, ln, ph in self.chunks)
        pos = 1 * MB
        for a, b in used:                  # space no chunk uses yet
            if a > pos:
                out.append((self.vol_off + pos, a - pos))
            pos = max(pos, b)
        if self.size_bytes > pos:
            out.append((self.vol_off + pos, self.size_bytes - pos))
        return sorted(out)

    def carve_base(self):
        return self.vol_off

    # ---- files
    def _subvol_names(self):
        names = {5: ''}
        refs = {k[1]: v for k, v in self.trees.items() if isinstance(k, tuple) and k[0] == 'ref'}
        for tid in [t for t in self.trees if isinstance(t, int)]:
            chain = []
            cur = tid
            for _ in range(16):
                if cur in refs:
                    parent, dirid, name = refs[cur]
                    chain.append(name)
                    cur = parent
                else:
                    break
            names[tid] = '/'.join(reversed(chain))
        return names

    MAX_UNPACK = 1 << 30            # compressed files are unpacked in memory

    @staticmethod
    def _decompress(comp, data, ram):
        """btrfs compression: 1 = zlib, 2 = LZO, 3 = zstd."""
        if comp == 1:
            return zlib.decompressobj().decompress(data)
        if comp == 2:
            import lzo
            return lzo.btrfs_lzo(data, ram)
        if comp == 3:
            import zstandard
            return zstandard.ZstdDecompressor().decompressobj().decompress(data)
        raise ValueError('unknown compression')

    def _data_runs(self, exts, size):
        """Device runs from EXTENT_DATA items -> (runs, inline bytes or None, problem or '').
        Files with any compressed part come back as bytes (unpacked); others as runs on the drive."""
        pieces = []                 # (file offset, ('runs', dev, n) | ('bytes', data))
        pos = 0
        compressed = False
        for foff in sorted(exts):
            d = exts[foff]
            ram, comp, etype = u64(d, 8), d[16], d[20]
            if comp > 3:
                return None, None, 'Compressed (not supported)'
            if etype == 0:
                payload = d[21:]
                if comp:
                    compressed = True
                    try:
                        payload = self._decompress(comp, payload, ram)
                    except Exception:
                        return None, None, 'damaged compressed data'
                pieces.append((foff, ('bytes', payload[:ram] if ram else payload)))
                pos = foff + len(payload)
                continue
            disk, dnum, eoff, nb = u64(d, 21), u64(d, 29), u64(d, 37), u64(d, 45)
            if comp:
                compressed = True
                dev = self.to_dev(disk)
                if dev is None:
                    return None, None, 'data location unknown'
                try:
                    raw = self._decompress(comp, self.src.read(dev, dnum), ram)
                except Exception:
                    raw = b''
                seg = raw[eoff:eoff + nb]
                pieces.append((foff, ('bytes', seg.ljust(nb, b'\x00'))))
            elif disk == 0:
                pieces.append((foff, ('hole', nb)))
            else:
                dev = self.to_dev(disk + eoff)
                if dev is None:
                    return None, None, 'data location unknown'
                pieces.append((foff, ('runs', dev, nb)))
            pos = foff + nb
        if compressed:
            if size > self.MAX_UNPACK:
                return None, None, 'Compressed (too large to unpack)'
            out = bytearray()
            for foff, p in pieces:
                if foff > len(out):
                    out += bytes(foff - len(out))
                if p[0] == 'bytes':
                    out[foff:foff + len(p[1])] = p[1]
                elif p[0] == 'hole':
                    out[foff:foff + p[1]] = bytes(p[1])
                else:
                    out[foff:foff + p[2]] = self.src.read(p[1], p[2]).ljust(p[2], b'\x00')
            return [], bytes(out[:size]).ljust(size, b'\x00') if size else bytes(out), ''
        runs = []
        inline = None
        pos = 0
        for foff, p in pieces:
            if p[0] == 'bytes':
                inline = (inline or b'') + p[1]
                continue
            if foff > pos:
                runs.append((None, foff - pos))
            if p[0] == 'hole':
                runs.append((None, p[1]))
                pos = foff + p[1]
                continue
            dev, nb = p[1], p[2]
            if runs and runs[-1][0] is not None and runs[-1][0] + runs[-1][1] == dev:
                runs[-1] = (runs[-1][0], runs[-1][1] + nb)
            else:
                runs.append((dev, nb))
            pos = foff + nb
        if inline is not None and not runs:
            return [], inline[:size], ''
        if inline is not None:
            return None, None, 'Compressed (not supported)'
        out, left = [], size
        for off, ln in runs:
            if left <= 0:
                break
            out.append((off, min(ln, left)))
            left -= min(ln, left)
        if left > 0:
            out.append((None, left))
        return out, None, ''

    def _tree_files(self, root_logical, owner_id):
        """Everything in one file tree: inodes, refs, extents."""
        inodes, refs, exts = {}, {}, {}
        for key, _o, data in self.iter_tree(root_logical, want=(INODE_ITEM, INODE_REF, EXTENT_DATA)):
            oid, typ, off = key
            if typ == INODE_ITEM:
                inodes[oid] = data
            elif typ == INODE_REF and len(data) >= 10:
                refs[oid] = (off, data[10:10 + u16(data, 8)].decode('utf-8', 'replace'))
            elif typ == EXTENT_DATA:
                exts.setdefault(oid, {})[off] = data
        return inodes, refs, exts

    def walk(self, emit, include_existing, cancel, status=None):
        names = self._subvol_names()
        old = _OldItems()
        self.old_items = old
        live = {}                                     # tree -> set(ino)
        live_paths = {}                               # tree -> {path: item}
        dirs_by_tree = {}
        for tid in sorted(t for t in self.trees if isinstance(t, int) and (t == 5 or 256 <= t < (1 << 63))):
            if cancel.is_set():
                return
            root, ritem = self.trees[tid]
            if status:
                status(f'Reading btrfs {"subvolume " + names.get(tid, str(tid)) if tid != 5 else "file tree"}...')
            inodes, refs, exts = self._tree_files(root, tid)
            live[tid] = set(inodes)
            dirs = {ino: refs[ino] for ino, d in inodes.items() if (u32(d, 52) & 0xF000) == 0x4000 and ino in refs}
            dirs_by_tree[tid] = dirs
            prefix = ('/' + names[tid]) if names.get(tid) else ''
            paths = {}
            for ino, d in inodes.items():
                if (u32(d, 52) & 0xF000) != 0x8000 or ino not in refs:
                    continue
                parent, name = refs[ino]
                folder = prefix + _bt_path(dirs, parent)
                size = u64(d, 16)
                paths[(folder[len(prefix):] or '/') + '/' + name] = (ino, d, exts.get(ino, {}), name, folder, size)
                if include_existing and size > 0:
                    it = self._make(name, folder or '/', size, exts.get(ino, {}), 'Existing', 'Not deleted', d)
                    if it:
                        emit(it)
            live_paths[tid] = paths
        # snapshots: files that have since been deleted from the subvolume they were taken of
        uuids = {ri[247:263]: tid for tid, (r, ri) in self.trees.items() if isinstance(tid, int) and len(ri) >= 279}
        for tid, (root, ri) in self.trees.items():
            if not isinstance(tid, int) or len(ri) < 279 or tid not in live_paths:
                continue
            parent_uuid = ri[263:279]
            src_tid = uuids.get(parent_uuid) if parent_uuid.strip(b'\x00') else None
            if src_tid is None or src_tid not in live_paths or include_existing:
                continue
            for rel, (ino, d, ex, name, folder, size) in live_paths[tid].items():
                if rel not in live_paths[src_tid] and size > 0:
                    it = self._make(name, folder or '/', size, ex, 'Snapshot', 'Good', d,
                                    f'deleted from {names.get(src_tid) or "the volume"}, still in snapshot '
                                    f'{names.get(tid) or tid}')
                    if it:
                        emit(it)
        # older generations of the file trees (backup roots in the superblock)
        for k in range(4):
            b = self.sb[0xB2B + 168 * k: 0xB2B + 168 * (k + 1)]
            if len(b) < 168:
                break
            old_root_tree = u64(b, 0)
            if not old_root_tree or old_root_tree == self.root_tree:
                continue
            try:
                for key, _o, data in self.iter_tree(old_root_tree, want=(ROOT_ITEM,)):
                    tid = key[0]
                    if (tid == 5 or 256 <= tid < (1 << 63)) and len(data) >= 239:
                        self._collect_old(u64(data, 176), tid, old)
            except Exception:
                pass
        self.live_inodes = live
        self.dirs_by_tree = dirs_by_tree
        self.names = names
        self.finish_old(old, emit, from_scan=False)

    def _collect_old(self, root, tid, old, depth=0, seen=None):
        seen = seen if seen is not None else set()
        if root in seen or depth > 12:
            return
        seen.add(root)
        node = self.read_node(root)
        if node is None:
            return
        if node[0x64] == 0:
            old.add_leaf(node, tid)
            return
        for i in range(min(u32(node, 0x60), 4000)):
            p = HDR + 33 * i
            self._collect_old(u64(node, p + 17), tid, old, depth + 1, seen)

    def finish_old(self, old, emit, from_scan=True):
        """Deleted files: inodes known from old leaves that the current trees no longer have."""
        done = getattr(self, '_old_done', set())
        self._old_done = done
        found = 0
        for key, (gen, ref_parent_name) in [(k, (v[0], v)) for k, v in old.refs.items()]:
            tid, ino = key
            if key in done or ino in self.live_inodes.get(tid, ()):
                continue
            inode = old.inodes.get(key)
            if not inode:
                continue
            d = inode[1]
            if (u32(d, 52) & 0xF000) != 0x8000:
                continue
            size = u64(d, 16)
            ex = {off: v[1] for off, v in old.extents.get(key, {}).items()}
            if size <= 0 or not ex:
                continue
            _g, parent, name = ref_parent_name
            dirs = dict(self.dirs_by_tree.get(tid, {}))
            for (t2, i2), (g2, p2, n2) in old.refs.items():
                if t2 == tid and i2 not in dirs and (t2, i2) in old.inodes and \
                        (u32(old.inodes[(t2, i2)][1], 52) & 0xF000) == 0x4000:
                    dirs[i2] = (p2, n2)
            prefix = ('/' + self.names[tid]) if self.names.get(tid) else ''
            folder = prefix + _bt_path(dirs, parent)
            it = self._make(name, folder or '/', size, ex, 'Deleted', None, d,
                            'found in an older copy of the btrfs file tree')
            if it:
                done.add(key)
                emit(it)
                found += 1
        return found

    def _make(self, name, folder, size, exts, method, cond, inode, note=''):
        runs, inline, problem = self._data_runs(exts, size)
        mtime = bt_time(inode, 136) if inode is not None and len(inode) >= 148 else None
        if problem:
            if method == 'Existing':
                return None
            it = FoundFile(name, folder, size, [(None, size)], method, problem, mtime, note)
            return it
        if inline is not None:
            it = FoundFile(name, folder, size, [], method, cond or 'Good', mtime, note)
            it.inline = inline
            return it
        it = FoundFile(name, folder, size, runs, method, cond or 'Good', mtime, note)
        if cond is None:
            used = sum(self.bytes_in_use(o, l) for o, l in runs if o is not None)
            total = sum(l for o, l in runs if o is not None) or 1
            it.condition = 'Good' if used == 0 else ('Overwritten' if used >= total * 0.9 else 'Partially overwritten')
            self.check_condition(self.src, it)
        return it


def _bt_path(dirs, parent, root=256):
    parts = []
    cur = parent
    for _ in range(128):
        if cur == root or cur not in dirs:
            break
        p, name = dirs[cur]
        parts.append(name)
        cur = p
    lost = cur != root
    path = '/'.join(reversed(parts))
    return ('/(lost folder)/' + path if lost else '/' + path).rstrip('/') if (path or lost) else ''


class BtrfsLeafCollector:
    """Fed by the deep scan: old btrfs file-tree leaves sitting in free space."""

    def __init__(self, volumes):
        self.vols = {v.fsid: v for v in volumes}
        self.old = {v.fsid: _OldItems() for v in volumes}

    def scan_chunk(self, data, pos, n):
        for fsid, v in self.vols.items():
            start = 0
            while True:
                i = data.find(fsid, start, n + 0x20)
                if i < 0:
                    break
                start = i + 1
                node_off = pos + i - 0x20
                if node_off < 0 or (node_off - v.vol_off) % v.sectorsize:
                    continue
                rel = i - 0x20
                node = data[rel:rel + v.nodesize] if rel >= 0 and rel + v.nodesize <= len(data) else \
                    v.src.read(node_off, v.nodesize)
                if len(node) < HDR or node[0x64] != 0:
                    continue
                owner = u64(node, 0x58)
                if not (owner == 5 or 256 <= owner < (1 << 63)):
                    continue
                lg = v.to_logical(node_off)
                if lg is not None and u64(node, 0x30) != lg:
                    continue
                self.old[fsid].add_leaf(node, owner)

    def finish(self, emit):
        n = 0
        for fsid, v in self.vols.items():
            if hasattr(v, 'live_inodes'):
                n += v.finish_old(self.old[fsid], emit)
        return n
