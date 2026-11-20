"""
Fragmented files in the deep scan.

A deep scan finds a file by its header and normally assumes the rest follows on directly. On a
well-used card or drive, big photos and videos are often split in two: the first part, then some
clusters belonging to another file, then the rest. This module checks each carved file of the
types below and, when the data stops making sense part-way, looks ahead for the piece that
continues it (the common two-piece case).

  PNG        every chunk has a CRC: a join is either exactly right or rejected
  ZIP/Office every entry has a CRC (and the deflate stream must keep decoding)
  JPEG       restart markers, when the camera writes them, prove a join; otherwise the candidates
             are decoded and the one whose picture continues most smoothly wins
  MP4/MOV    camera files with the index (moov) at the end: the gap is where moov really is versus
             where it should be, and the break is found by checking each video sample
"""
import io
import struct
import time
import zlib

MB = 1024 * 1024
WINDOW = 32 * MB              # how far ahead to look for the continuation
MAX_CANDIDATES = 12           # JPEG candidates that get a full decode
TIME_LIMIT = 3.0              # seconds per file before giving up on a reassembly
_ASCII = bytes(range(128))
_deadline = [0.0]


def _late():
    return time.time() > _deadline[0]


def be16(b, o): return struct.unpack_from('>H', b, o)[0]
def be32(b, o): return struct.unpack_from('>I', b, o)[0]
def be64(b, o): return struct.unpack_from('>Q', b, o)[0]


class _Win:
    """The file's first bytes plus the look-ahead window, read once."""

    def __init__(self, src, off, size):
        self.src, self.off = src, off
        self.data = src.read(off, size)

    def __len__(self):
        return len(self.data)


def _suspicious_block(b):
    """A cluster that can't be the middle of compressed picture/video data."""
    if not b:
        return True
    if b.count(0) == len(b):
        return True
    return len(b) >= 512 and not b.translate(None, _ASCII)     # nothing but 7-bit text


# --------------------------------------------------------------------------------------
# PNG
# --------------------------------------------------------------------------------------

def _png_walk(d, pos=8):
    """('end', length) | ('break', chunk_start) walking chunks with CRC checks."""
    while pos + 12 <= len(d):
        ln = be32(d, pos)
        typ = d[pos + 4:pos + 8]
        if not all(65 <= c <= 90 or 97 <= c <= 122 for c in typ) or ln > 64 * MB:
            return 'break', pos
        end = pos + 12 + ln
        if end > len(d):
            return 'break', pos
        if zlib.crc32(d[pos + 4:pos + 8 + ln]) != be32(d, pos + 8 + ln):
            return 'break', pos
        if typ == b'IEND':
            return 'end', end
        pos = end
    return 'break', pos


def png(win, cs):
    d = win.data
    st, p = _png_walk(d)
    if st == 'end':
        return [(0, p)], 'Good'
    # the chunk starting at p is broken: its start and length are right, the rest continues later
    ln = be32(d, p) if p + 8 <= len(d) else 0
    if ln > 64 * MB:
        return None
    end = p + 12 + ln
    first_b = (p + 8) // cs * cs + cs                     # boundaries inside the chunk's data
    for B in range(first_b, end + cs, cs):
        if B >= end + 4:
            break
        head = d[p + 4:B]
        crc0 = zlib.crc32(head) if B <= end - 4 else None
        rest = end - B
        for C in range(B + cs, len(d) - rest, cs):
            if _late():
                return None
            tail = d[C:C + rest]
            # the next chunk header must make sense right after the joined chunk
            nxt = d[C + rest:C + rest + 8]
            if len(nxt) == 8 and not all(65 <= c <= 90 or 97 <= c <= 122 for c in nxt[4:]):
                continue
            joined = d[p:B] + tail
            if zlib.crc32(joined[4:8 + ln]) != be32(joined, 8 + ln):
                continue
            st2, p2 = _png_walk(d[:B] + d[C:], 8)          # rest of the file must check out too
            if st2 == 'end':
                tail_len = p2 - B
                return [(0, B), (C, tail_len)], 'Reassembled from 2 pieces (checksums match)'
    return None


# --------------------------------------------------------------------------------------
# ZIP / Office
# --------------------------------------------------------------------------------------

def _zip_entries(d, pos=0):
    """('end', length) | ('break', entry_start, data_start, csize, crc, method)."""
    while pos + 30 <= len(d):
        sig = d[pos:pos + 4]
        if sig == b'PK\x01\x02' or sig == b'PK\x05\x06':
            e = d.find(b'PK\x05\x06', pos)
            if e < 0 or e + 22 > len(d):
                return 'break', pos, None, 0, 0, 0
            return 'end', e + 22 + struct.unpack_from('<H', d, e + 20)[0]
        if sig != b'PK\x03\x04':
            return 'break', pos, None, 0, 0, 0
        flags, method = struct.unpack_from('<HH', d, pos + 6)
        crc, csize, usize = struct.unpack_from('<III', d, pos + 14)
        nl, xl = struct.unpack_from('<HH', d, pos + 26)
        ds = pos + 30 + nl + xl
        if flags & 8 and csize == 0:                    # sizes after the data: decode to find the end
            if method != 8:
                return 'break', pos, ds, 0, 0, method
            dec = zlib.decompressobj(-15)
            try:
                dec.decompress(d[ds:ds + 64 * MB])
            except zlib.error:
                return 'break', pos, ds, 0, 0, method
            if not dec.eof:
                return 'break', pos, ds, 0, 0, method
            used = len(d[ds:ds + 64 * MB]) - len(dec.unused_data)
            dd = ds + used
            if d[dd:dd + 4] == b'PK\x07\x08':
                dd += 16
            else:
                dd += 12
            pos = dd
            continue
        end = ds + csize
        if end > len(d):
            return 'break', pos, ds, csize, crc, method
        data = d[ds:end]
        try:
            raw = zlib.decompressobj(-15).decompress(data) if method == 8 else data
        except zlib.error:
            return 'break', pos, ds, csize, crc, method
        if zlib.crc32(raw) != crc:
            return 'break', pos, ds, csize, crc, method
        pos = end + (16 if flags & 8 else 0)
    return 'break', pos, None, 0, 0, 0


def zipf(win, cs):
    d = win.data
    r = _zip_entries(d)
    if r[0] == 'end':
        return [(0, r[1])], 'Good'
    _, ep, ds, csize, crc, method = r
    if ds is None or not csize or method not in (0, 8):
        return None
    end = ds + csize
    bounds = list(range((ds // cs + 1) * cs, end, cs))
    # The central directory at the end says where it should start; where it really is gives the
    # gap size, so each possible break point has exactly one candidate continuation.
    e = d.find(b'PK\x05\x06', end)
    while e >= 0 and e + 22 <= len(d):
        cd_size, cd_off = struct.unpack_from('<II', d, e + 12)
        gap = (e - cd_size) - cd_off
        if gap > 0 and gap % cs == 0 and d[e - cd_size:e - cd_size + 4] == b'PK\x01\x02':
            for B in bounds:
                C = B + gap
                data = d[ds:B] + d[C:C + end - B]
                try:
                    raw = zlib.decompressobj(-15).decompress(data) if method == 8 else data
                except zlib.error:
                    continue
                if zlib.crc32(raw) == crc:
                    r2 = _zip_entries(d[:B] + d[C:], 0)
                    if r2[0] == 'end':
                        return [(0, B), (C, r2[1] - B)], 'Reassembled from 2 pieces (checksums match)'
            break
        e = d.find(b'PK\x05\x06', e + 4)
    if method == 8:
        # where does the contiguous data stop decoding? The break is at, or a little before, that point
        probe = zlib.decompressobj(-15)
        p_err = end
        seen_text = 0
        for k in range(ds, end, cs):
            try:
                out = probe.decompress(d[k:min(k + cs, end)])
            except zlib.error:
                p_err = k + cs
                break
            if len(out) >= 256:                           # text so far, then not: that's the break too
                is_text = sum(32 <= c < 127 or c in (9, 10, 13) for c in out[:2048]) >= 0.97 * min(len(out), 2048)
                if is_text:
                    seen_text += 1
                elif seen_text >= 2:
                    p_err = k + cs
                    break
        near = [b for b in bounds if p_err - 24 * cs <= b <= p_err]
        bounds = sorted(near, reverse=True) + [b for b in bounds if b not in near]
    for B in bounds:
        rest = end - B
        pre = None
        tail_out = b''
        if method == 8:                                   # decoder state at B
            pre = zlib.decompressobj(-15)
            try:
                tail_out = pre.decompress(d[ds:B])[-4096:]
            except zlib.error:
                continue
        texty = len(tail_out) >= 256 and sum(32 <= c < 127 or c in (9, 10, 13) for c in tail_out) > 0.97 * len(tail_out)
        for C in range(B + cs, len(d) - rest, cs):
            if _late():
                return None
            if pre is not None:                           # quick test: does the stream keep decoding?
                t = pre.copy()
                try:
                    out = t.decompress(d[C:C + 8192])
                except zlib.error:
                    continue
                if texty and (len(out) < 512 or sum(32 <= c < 127 or c in (9, 10, 13) for c in out[:2048]) <
                              0.97 * min(len(out), 2048)):
                    continue                              # the entry was text so far; this isn't
                try:                                      # second, longer test before a full decode
                    t.decompress(d[C + 8192:C + 65536])
                except zlib.error:
                    continue
            data = d[ds:B] + d[C:C + rest]
            try:
                raw = zlib.decompressobj(-15).decompress(data) if method == 8 else data
            except zlib.error:
                continue
            if zlib.crc32(raw) != crc:
                continue
            r2 = _zip_entries(d[:B] + d[C:], 0)
            if r2[0] == 'end':
                return [(0, B), (C, r2[1] - B)], 'Reassembled from 2 pieces (checksums match)'
    return None


# --------------------------------------------------------------------------------------
# JPEG
# --------------------------------------------------------------------------------------

def _jpeg_headers(d):
    """Position of the first entropy-coded byte and whether restart markers are used."""
    pos, rst = 2, False
    while pos + 4 <= len(d):
        if d[pos] != 0xFF:
            return None, rst
        m = d[pos + 1]
        if m == 0xFF:
            pos += 1
            continue
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            pos += 2
            continue
        seg = be16(d, pos + 2)
        if m == 0xDD:
            rst = be16(d, pos + 4) > 0
        if m == 0xDA:
            return pos + 2 + seg, rst
        pos += 2 + seg
    return None, rst


def _scan_entropy(d, pos, cs, base, expect_rst=None, stop=None):
    """Walk entropy-coded data from `pos`. Returns ('end', eoi_end, rst) or ('break', at, rst).
    `base` is the absolute offset of d[0] (cluster checks use absolute alignment)."""
    n = len(d) if stop is None else min(len(d), stop)
    rst = expect_rst
    # clusters to sanity-check: the one starting at `pos` if it is a boundary, then every later one
    blk = pos if (base + pos) % cs == 0 else ((base + pos) // cs + 1) * cs - base
    while True:
        i = d.find(b'\xff', pos, n)
        lim = n if i < 0 else i
        while blk < lim:                                 # whole clusters of zeros/text can't be in here
            if _suspicious_block(d[blk:min(blk + cs, n)]) and blk + cs <= n:
                return 'break', blk, rst
            blk += cs
        if i < 0 or i + 1 >= n:
            return 'open', n, rst
        m = d[i + 1]
        if m == 0x00 or m == 0xFF:
            pos = i + 1 if m == 0xFF else i + 2
            continue
        if 0xD0 <= m <= 0xD7:
            k = m - 0xD0
            if rst is not None and k != rst:
                return 'break', i, rst
            rst = (k + 1) % 8
            pos = i + 2
            continue
        if m == 0xD9:
            return 'end', i + 2, rst
        if m in (0xC4, 0xDA, 0xDD, 0xFE) or 0xE0 <= m <= 0xEF:     # progressive JPEG: more tables / scans
            if i + 4 > n:
                return 'open', n, rst
            seg = be16(d, i + 2)
            pos = i + 2 + seg
            continue
        return 'break', i, rst


def _decode_score(data):
    """Lower = smoother picture; None = doesn't decode."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        w, h = im.size
        im.draft('L', (max(1, w // 8), max(1, h // 8)))
        im = im.convert('L')
        im.load()
    except Exception:
        return None
    w, h = im.size
    px = im.tobytes()
    if h < 3:
        return 0.0
    diffs = []
    for r in range(1, h):
        a, b = px[(r - 1) * w:r * w], px[r * w:(r + 1) * w]
        diffs.append(sum(abs(x - y) for x, y in zip(a[::4], b[::4])) / max(1, len(a[::4])))
    diffs.sort()
    return sum(diffs[-3:]) / 3.0 + (diffs[-1] - diffs[len(diffs) // 2])


def jpeg(win, cs):
    d = win.data
    sos, uses_rst = _jpeg_headers(d)
    if sos is None:
        return None
    st, at, rst = _scan_entropy(d, sos, cs, win.off)
    if st == 'end':
        return [(0, at)], 'Good'
    if st == 'open':
        return None
    # break somewhere at or just before `at`: try the cluster boundary there and one before
    boundaries = sorted({max(sos, at // cs * cs), max(sos, at // cs * cs - cs)}, reverse=True)
    found = []
    for B in boundaries:
        if B <= sos:
            continue
        _, _, rst_before = _scan_entropy(d, sos, cs, win.off, stop=B)
        for C in range(B + cs, len(d) - cs, cs):
            if _late():
                break
            if d[C:C + 2] == b'\xff\xd8':
                continue                                  # another photo's start, not our continuation
            st2, at2, _ = _scan_entropy(d, C, cs, win.off, expect_rst=rst_before if uses_rst else None,
                                        stop=min(len(d), C + 64 * 1024) if not uses_rst else None)
            if st2 == 'break':
                continue
            if uses_rst:                                  # restart markers line up: the join is proven
                st3, end3, _ = _scan_entropy(d, C, cs, win.off, expect_rst=rst_before)
                if st3 == 'end':
                    return [(0, B), (C, end3 - C)], 'Reassembled from 2 pieces (restart markers match)'
                continue
            st3, end3, _ = _scan_entropy(d, C, cs, win.off)
            if st3 != 'end':
                continue
            found.append((B, C, end3))
            if len(found) >= MAX_CANDIDATES:
                break
        if found or uses_rst:
            break
    best = None
    for B, C, end3 in found:
        sc = _decode_score(d[:B] + d[C:end3])
        if sc is not None and (best is None or sc < best[0]):
            best = (sc, B, C, end3)
    if best:
        _, B, C, end3 = best
        return [(0, B), (C, end3 - C)], ('Reassembled from 2 pieces (best match)' if len(found) > 1
                                         else 'Reassembled from 2 pieces')
    return None


# --------------------------------------------------------------------------------------
# MP4 / MOV (camera layout: ftyp, mdat, moov at the end)
# --------------------------------------------------------------------------------------

def _boxes(d, pos, end):
    out = []
    while pos + 8 <= end:
        sz = be32(d, pos)
        typ = d[pos + 4:pos + 8]
        hl = 8
        if sz == 1:
            sz = be64(d, pos + 8)
            hl = 16
        if sz < hl or pos + sz > end:
            break
        out.append((typ, pos, sz, hl))
        pos += sz
    return out


def _find(d, pos, end, path):
    for typ, p, sz, hl in _boxes(d, pos, end):
        if typ == path[0]:
            if len(path) == 1:
                return p + hl, p + sz
            skip = 8 if typ == b'stsd' else 0
            r = _find(d, p + hl + skip, p + sz, path[1:])
            if r:
                return r
    return None


def _video_chunks(moov):
    """[(file offset, codec)] of the video track's chunks, from the moov box contents."""
    out = []
    for typ, p, sz, hl in _boxes(moov, 0, len(moov)):
        if typ != b'trak':
            continue
        t = moov[p + hl:p + sz]
        hd = _find(t, 0, len(t), [b'mdia', b'hdlr'])
        if not hd or t[hd[0] + 8:hd[0] + 12] != b'vide':
            continue
        sd = _find(t, 0, len(t), [b'mdia', b'minf', b'stbl', b'stsd'])
        codec = t[sd[0] + 12:sd[0] + 16] if sd else b''
        co = _find(t, 0, len(t), [b'mdia', b'minf', b'stbl', b'stco'])
        big = False
        if not co:
            co = _find(t, 0, len(t), [b'mdia', b'minf', b'stbl', b'co64'])
            big = True
        if not co:
            continue
        n = be32(t, co[0] + 4)
        for k in range(n):
            o = be64(t, co[0] + 8 + 8 * k) if big else be32(t, co[0] + 8 + 4 * k)
            out.append((o, codec))
    return sorted(out)


def _nal_ok(b, codec):
    if len(b) < 6:
        return False
    ln = be32(b, 0)
    if not 1 <= ln <= 64 * MB:
        return False
    h = b[4]
    if h & 0x80:
        return False
    if codec in (b'hvc1', b'hev1'):
        return ((h >> 1) & 0x3F) <= 40
    return 1 <= (h & 0x1F) <= 23


def mp4(win, cs):
    d = win.data
    cs = max(cs, 4096)            # camera cards use 4 KB+ clusters; finer steps can't place the break
    boxes = _boxes(d, 0, len(d))
    if not boxes or boxes[0][0] != b'ftyp':
        return None
    names = [b[0] for b in boxes]
    if b'moov' in names and b'mdat' in names:
        end = boxes[-1][1] + boxes[-1][2]
        return [(0, end)], 'Good'
    mdat = next((b for b in boxes if b[0] == b'mdat'), None)
    if mdat is None:
        return None
    expect = mdat[1] + mdat[2]                            # where moov should start
    for k in range(1, (len(d) - expect) // cs):
        P = expect + k * cs
        if d[P + 4:P + 8] != b'moov':
            continue
        msz = be32(d, P)
        if not 8 < msz <= 64 * MB or P + msz > len(d):
            continue
        gap = k * cs
        chunks = _video_chunks(d[P + 8:P + msz])
        if not chunks:
            continue
        # first chunk that isn't valid where it would be without the gap, but is valid after it
        brk = None
        for o, codec in chunks:
            if o >= expect:
                break
            if not _nal_ok(d[o:o + 6], codec):
                if _nal_ok(d[o + gap:o + gap + 6], codec):
                    brk = o
                break
        if brk is None:
            continue
        prev = max((o for o, _ in chunks if o < brk), default=mdat[1] + mdat[3])
        B = max(prev + 6, brk // cs * cs)
        B = (B + cs - 1) // cs * cs if B % cs else B
        if B > brk:
            B = brk // cs * cs
        later = [o for o, c in chunks if o >= brk][:20]
        if all(_nal_ok(d[o + gap:o + gap + 6], codec) for o in later):
            return [(0, B), (B + gap, expect + msz - B)], 'Reassembled from 2 pieces (video samples check out)'
    return None


CHECKERS = {'png': png, 'jpg': jpeg, 'zip': zipf, 'docx': zipf, 'xlsx': zipf, 'pptx': zipf,
            'mp4': mp4, 'mov': mp4, 'm4v': mp4, '3gp': mp4}


def check(src, off, ext, length, cs, window=WINDOW):
    """For a carved file: (runs relative to `off`, condition) after checking it, or None if no
    sensible layout was found. `length` is the contiguous carve's length (or None).
    Reads just the file first; the look-ahead window only when the file turns out to be broken."""
    fn = CHECKERS.get(ext)
    if fn is None:
        return None
    cs = max(512, cs)
    _deadline[0] = time.time() + TIME_LIMIT
    try:
        if length:
            r = fn(_Win(src, off, length + 64 * 1024), cs)
            if r and r[1] == 'Good':
                return r
        if not window:
            return None
        return fn(_Win(src, off, (length or 0) + window), cs)
    except (struct.error, IndexError, ValueError, ZeroDivisionError):
        return None


def valid(ext, data):
    """True / False if `data` checks out as a complete file of this type, None if it can't be told."""
    try:
        if ext == 'png':
            return data[:8] == b'\x89PNG\r\n\x1a\n' and _png_walk(data)[0] == 'end'
        if ext in ('zip', 'docx', 'xlsx', 'pptx'):
            return data[:4] == b'PK\x03\x04' and _zip_entries(data)[0] == 'end'
        if ext in ('jpg', 'jpeg'):
            sos, _ = _jpeg_headers(data)
            if sos is None:
                return False
            return _scan_entropy(data, sos, 4096, 0)[0] == 'end'
    except (struct.error, IndexError, ValueError):
        return False
    return None
