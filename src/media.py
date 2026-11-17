"""
FlashRescue media tools (need ffmpeg / ffprobe next to the app, in a 'tools' folder, or on PATH).

Check files
  Pictures: fully decoded with Pillow.  Office/zip files: every member CRC-checked.
  PDF: structure end markers.  Video/audio: decoded end to end with ffmpeg ("plays fully" /
  "breaks at 0:42 of 3:10" / "won't play").

Repair video
  1. Container fix: anything ffmpeg can open is re-wrapped, dropping the damaged tail.
  2. MP4/MOV with no index (recording cut off, file partly recovered): the video frames are
     pulled out of the raw media data and re-wrapped. The codec settings (SPS/PPS) come from
       a) inside the video itself (many dashcams, action cams, drones, DVRs repeat them), or
       b) a reference video - any healthy clip from the same model of phone/camera, often found
          on the same card or drive, or
       c) H.264 only: a search - the slice headers are analysed to work out the frame-number and
          picture-order layout, then resolution and coding tools are found by test-decoding.
     Uncompressed (PCM) audio is rebuilt too; compressed (AAC) audio usually can't be without
     the index, so repaired clips may be silent.
"""

import io
import os
import re
import sys
import json
import mmap
import shutil
import struct
import tempfile
import subprocess
import zipfile

KB = 1024
MB = 1024 * KB

VIDEO_EXTS = {'mp4', 'mov', 'm4v', '3gp', 'avi', 'mkv', 'webm', 'mts', 'm2ts', 'mpg', 'mpeg', 'wmv', 'flv'}
AUDIO_EXTS = {'mp3', 'wav', 'm4a', 'flac', 'ogg', 'aac', 'wma', 'opus'}
IMAGE_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'tif', 'tiff'}
ZIP_EXTS = {'zip', 'docx', 'xlsx', 'pptx', 'odt', 'ods', 'odp', 'epub', 'apk', 'jar'}


# ======================================================================================
# ffmpeg plumbing
# ======================================================================================

def _app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def find_ffmpeg():
    """(ffmpeg, ffprobe) paths or (None, None)."""
    exe = '.exe' if os.name == 'nt' else ''
    base = _app_dir()
    try:
        import updater
        fetched = updater.ffmpeg_dir()             # downloaded by FlashRescue on first use
    except Exception:
        fetched = base
    for d in (base, os.path.join(base, 'tools'), os.path.join(os.path.dirname(base), 'tools'), fetched):
        f, p = os.path.join(d, 'ffmpeg' + exe), os.path.join(d, 'ffprobe' + exe)
        if os.path.isfile(f) and os.path.isfile(p):
            return f, p
    f, p = shutil.which('ffmpeg'), shutil.which('ffprobe')
    return (f, p) if f and p else (None, None)


def _run(cmd, timeout=600, input_bytes=None, binary=False):
    kw = {}
    if os.name == 'nt':
        kw['creationflags'] = 0x08000000           # CREATE_NO_WINDOW
    p = subprocess.run(cmd, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=timeout, **kw)
    if binary:
        return p.returncode, p.stdout, p.stderr.decode('utf-8', 'replace')
    return p.returncode, p.stdout.decode('utf-8', 'replace'), p.stderr.decode('utf-8', 'replace')


def probe(ffprobe, path):
    try:
        rc, out, err = _run([ffprobe, '-v', 'error', '-print_format', 'json', '-show_streams', '-show_format', path],
                            timeout=60)
    except (subprocess.TimeoutExpired, OSError) as ex:
        return None, str(ex)
    if rc != 0:
        return None, err.strip()
    try:
        return json.loads(out), ''
    except ValueError:
        return None, 'unreadable probe output'


def fmt_time(s):
    s = int(s or 0)
    return f'{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}' if s >= 3600 else f'{s // 60}:{s % 60:02d}'


# ======================================================================================
# Check files
# ======================================================================================

def check_path(path, ext, tools=None):
    """('ok'|'bad'|'unknown', text)"""
    ext = ext.lower()
    try:
        if ext in IMAGE_EXTS:
            try:
                from PIL import Image, ImageFile
                ImageFile.LOAD_TRUNCATED_IMAGES = False
            except ImportError:
                return 'unknown', 'Not checked (Pillow missing)'
            try:
                with Image.open(path) as im:
                    im.load()
                    return 'ok', f'Opens fully ({im.width}x{im.height})'
            except Exception as ex:
                msg = str(ex).lower()
                if 'truncated' in msg:
                    return 'bad', 'Picture is cut short (bottom part missing)'
                return 'bad', "Won't open"
        if ext in ZIP_EXTS:
            try:
                with zipfile.ZipFile(path) as z:
                    badname = z.testzip()
                    if badname:
                        return 'bad', f'Damaged ({badname})'
                    return 'ok', f'Opens fully ({len(z.namelist())} parts)'
            except zipfile.BadZipFile:
                return 'bad', "Won't open"
        if ext == 'pdf':
            with open(path, 'rb') as f:
                head = f.read(8)
                f.seek(max(0, os.path.getsize(path) - 2048))
                tail = f.read()
            if not head.startswith(b'%PDF'):
                return 'bad', "Won't open"
            if b'%%EOF' in tail and b'startxref' in tail:
                return 'ok', 'Looks complete'
            return 'bad', 'Cut short (end missing)'
        if ext in VIDEO_EXTS | AUDIO_EXTS:
            ff, fp = tools or find_ffmpeg()
            if not ff:
                return 'unknown', 'Needs ffmpeg'
            return _check_av(ff, fp, path)
    except OSError as ex:
        return 'unknown', f'Could not check ({ex})'
    return 'unknown', 'Not checked (type not supported)'


def _check_av(ff, fp, path):
    info, err = probe(fp, path)
    if not info or not info.get('streams'):
        if 'moov atom not found' in err:
            return 'bad', "Won't play - index missing (try Repair video)"
        return 'bad', "Won't play"
    dur = float(info.get('format', {}).get('duration') or 0)
    try:
        rc, out, err = _run([ff, '-v', 'error', '-xerror', '-nostats', '-progress', 'pipe:1', '-i', path,
                             '-f', 'null', '-'], timeout=max(120, int(dur * 2) + 60))
    except subprocess.TimeoutExpired:
        return 'unknown', 'Check timed out'
    last = 0.0
    for line in out.splitlines():
        if line.startswith('out_time_us='):
            try:
                last = int(line.split('=', 1)[1]) / 1e6
            except ValueError:
                pass
    errors = [l for l in err.splitlines() if l.strip()]
    if rc == 0 and not errors:
        return 'ok', f'Plays fully ({fmt_time(dur)})' if dur else 'Plays fully'
    if dur and last >= dur - 1.0 and rc == 0:
        return 'ok', f'Plays fully, minor glitches ({fmt_time(dur)})'
    if last > 0.5:
        return 'bad', f'Breaks at {fmt_time(last)} of {fmt_time(dur)}' if dur else f'Breaks at {fmt_time(last)}'
    return 'bad', "Won't play"


def check_item(src, item, tools=None, tmpdir=None):
    import engine
    tmp = tmpdir or tempfile.mkdtemp(prefix='flashrescue_check_')
    path = None
    try:
        path = engine.recover_item(src, item, tmp, keep_folders=False)
        return check_path(path, item.ext, tools)
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        if not tmpdir:
            shutil.rmtree(tmp, ignore_errors=True)


# ======================================================================================
# Bitstream helpers
# ======================================================================================

def unescape(b):
    """Remove H.264/HEVC emulation-prevention bytes (00 00 03 -> 00 00)."""
    return b.replace(b'\x00\x00\x03', b'\x00\x00')


def escape(b):
    out = bytearray()
    zeros = 0
    for x in b:
        if zeros >= 2 and x <= 3:
            out.append(3)
            zeros = 0
        out.append(x)
        zeros = zeros + 1 if x == 0 else 0
    return bytes(out)


class BitReader:
    def __init__(self, data, start_bit=0):
        self.d = data
        self.p = start_bit

    def u(self, n):
        v = 0
        for _ in range(n):
            byte = self.p >> 3
            if byte >= len(self.d):
                raise EOFError
            v = (v << 1) | ((self.d[byte] >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v

    def ue(self):
        z = 0
        while self.u(1) == 0:
            z += 1
            if z > 31:
                raise ValueError('bad exp-golomb')
        return (1 << z) - 1 + (self.u(z) if z else 0)

    def se(self):
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


class BitWriter:
    def __init__(self):
        self.bits = []

    def u(self, n, v):
        for i in range(n - 1, -1, -1):
            self.bits.append((v >> i) & 1)

    def ue(self, v):
        v += 1
        n = v.bit_length()
        self.u(n - 1, 0)
        self.u(n, v)

    def se(self, v):
        self.ue(2 * v - 1 if v > 0 else -2 * v)

    def rbsp(self):
        self.bits.append(1)
        while len(self.bits) % 8:
            self.bits.append(0)
        out = bytearray()
        for i in range(0, len(self.bits), 8):
            b = 0
            for bit in self.bits[i:i + 8]:
                b = (b << 1) | bit
            out.append(b)
        return bytes(out)


# ======================================================================================
# Pulling video frames (NAL units) out of raw MP4 media data
# ======================================================================================

H264_OK = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12}
HEVC_OK = set(range(0, 10)) | set(range(16, 22)) | set(range(32, 41))
_CAND_H264 = re.compile(rb'\x00[\x00-\x01][\x00-\xff]{2}[\x01\x21\x41\x61\x25\x45\x65\x06\x09\x27\x47\x67\x28\x48\x68]')
_CAND_HEVC = re.compile(rb'\x00[\x00-\x01][\x00-\xff]{2}[\x00-\x13\x20-\x2b\x40-\x51][\x01-\x07]')


def nal_valid(mm, pos, end, codec, lsize=4):
    if pos + lsize + 2 > end:
        return False
    L = int.from_bytes(mm[pos:pos + lsize], 'big')
    if L < 2 or L > 48 * MB or pos + lsize + L > end:
        return False
    h = mm[pos + lsize]
    if h & 0x80:
        return False
    if codec == 'h264':
        t, ref = h & 0x1F, h >> 5
        if t not in H264_OK:
            return False
        if t in (6, 9, 10, 11, 12) and ref:
            return False
        if t in (5, 7, 8) and not ref:
            return False
        return True
    h2 = mm[pos + lsize + 1]
    t = (h >> 1) & 0x3F
    return t in HEVC_OK and (h & 1) == 0 and (h2 >> 3) == 0 and (h2 & 7) >= 1


def frame_start_plausible(mm, pos, codec, lsize=4):
    """Does a frame (picture slice) start here? Decodes the first slice-header fields."""
    try:
        if codec == 'h264':
            t = mm[pos + lsize] & 0x1F
            if t not in (1, 5):
                return t in (7, 9, 6)                 # SPS / access-unit delimiter / SEI open a frame too
            r = BitReader(unescape(bytes(mm[pos + lsize + 1:pos + lsize + 12])))
            return r.ue() == 0 and r.ue() <= 9 and r.ue() <= 3
        t = (mm[pos + lsize] >> 1) & 0x3F
        if t > 21:
            return t in (32, 35, 39)                 # VPS / AUD / SEI
        r = BitReader(unescape(bytes(mm[pos + lsize + 2:pos + lsize + 12])))
        if r.u(1) != 1:                               # first_slice_segment_in_pic_flag
            return False
        if 16 <= t <= 23:
            r.u(1)
        return r.ue() <= 63
    except (EOFError, ValueError, IndexError):
        return False


def walk_nals(mm, start, end, codec, lsize=4, resync_window=2 * MB, max_gap=16 * MB, allowed=None):
    """Yield ('nal', off, total_len) and ('gap', off, len) over [start, end).
    `allowed`: header bytes this video really uses - a frame found by resyncing after audio must
    use one of them (uncompressed audio can mimic a frame's length + header by chance)."""
    cand = _CAND_H264 if codec == 'h264' else _CAND_HEVC
    pos = start
    gap_start = None
    while pos < end - lsize - 2:
        if nal_valid(mm, pos, end, codec, lsize):
            L = int.from_bytes(mm[pos:pos + lsize], 'big')
            if gap_start is not None:
                yield 'gap', gap_start, pos - gap_start
                gap_start = None
            yield 'nal', pos, lsize + L
            pos += lsize + L
            continue
        if gap_start is None:
            gap_start = pos
        found = -1
        search = pos + 1
        limit = min(end, pos + 1 + resync_window)
        while True:
            m = cand.search(mm, search, limit)
            if not m:
                break
            c = m.start() if lsize == 4 else m.start()
            if nal_valid(mm, c, end, codec, lsize) and (allowed is None or mm[c + lsize] in allowed):
                n2 = c + lsize + int.from_bytes(mm[c:c + lsize], 'big')
                if n2 >= end - lsize - 2 or nal_valid(mm, n2, end, codec, lsize) \
                        or frame_start_plausible(mm, c, codec, lsize):
                    found = c
                    break
            search = m.start() + 1
        if found < 0:
            if limit >= end or (limit - gap_start) > max_gap:
                break
            pos = limit
            continue
        pos = found
    if gap_start is not None and pos > gap_start:
        yield 'gap', gap_start, pos - gap_start


def header_profile(mm, start, end, codec, lsize=4, limit=64 * MB):
    """Header bytes making up at least 1% of the frames (plus parameter sets/keyframes)."""
    counts = {}
    total = 0
    for kind, off, ln in walk_nals(mm, start, min(end, start + limit), codec, lsize):
        if kind == 'nal':
            b = mm[off + lsize]
            counts[b] = counts.get(b, 0) + 1
            total += 1
    keep = {b for b, n in counts.items() if n >= max(2, total * 0.01)}
    for b in counts:
        t = (b & 0x1F) if codec == 'h264' else (b >> 1) & 0x3F
        if (codec == 'h264' and t in (5, 7, 8)) or (codec == 'hevc' and t in (19, 20, 21, 32, 33, 34)):
            keep.add(b)
    return keep or None


def guess_codec(mm, start, end):
    """Which codec do the frames look like? 'h264' / 'hevc' / None"""
    scores = {}
    for codec in ('h264', 'hevc'):
        n = good = 0
        for kind, off, ln in walk_nals(mm, start, min(end, start + 8 * MB), codec):
            n += 1
            if kind == 'nal':
                good += ln
            if n > 400:
                break
        scores[codec] = good
    best = max(scores, key=scores.get)
    return best if scores[best] > 64 * KB or scores[best] > (end - start) * 0.3 else None


def mp4_boxes(mm, size):
    """Top-level boxes: [(type, offset, total_size, header_size)]"""
    out = []
    pos = 0
    while pos + 8 <= size and len(out) < 10000:
        sz, typ = struct.unpack('>I4s', mm[pos:pos + 8])
        hdr = 8
        if sz == 1 and pos + 16 <= size:
            sz = struct.unpack('>Q', mm[pos + 8:pos + 16])[0]
            hdr = 16
        elif sz == 0:
            sz = size - pos
        if sz < hdr or not all(32 <= c < 127 for c in typ):
            break
        out.append((typ.decode('latin-1'), pos, min(sz, size - pos), hdr))
        pos += sz
    return out


def find_media_data(mm, size):
    """(start, end) of the media data: mdat contents, or everything after the header boxes."""
    boxes = mp4_boxes(mm, size)
    for typ, off, sz, hdr in boxes:
        if typ == 'mdat':
            return off + hdr, min(off + sz, size)
    if boxes:
        last = boxes[-1]
        return last[1] + last[2], size
    return 0, size


# ======================================================================================
# Parameter sets: in-band, from a reference video, or found by search (H.264)
# ======================================================================================

def parse_avcc(b):
    lsize = (b[4] & 3) + 1
    n = b[5] & 0x1F
    p = 6
    sets = []
    for _ in range(n):
        L = struct.unpack('>H', b[p:p + 2])[0]
        sets.append(b[p + 2:p + 2 + L])
        p += 2 + L
    m = b[p]
    p += 1
    for _ in range(m):
        L = struct.unpack('>H', b[p:p + 2])[0]
        sets.append(b[p + 2:p + 2 + L])
        p += 2 + L
    return 'h264', lsize, sets


def parse_hvcc(b):
    lsize = (b[21] & 3) + 1
    n = b[22]
    p = 23
    sets = []
    for _ in range(n):
        p += 1
        cnt = struct.unpack('>H', b[p:p + 2])[0]
        p += 2
        for _ in range(cnt):
            L = struct.unpack('>H', b[p:p + 2])[0]
            sets.append(b[p + 2:p + 2 + L])
            p += 2 + L
    return 'hevc', lsize, sets


def reference_info(path, tools):
    """Codec settings of a healthy reference video: dict or None (with reason)."""
    ff, fp = tools
    info, err = probe(fp, path)
    if not info:
        return None, f'reference does not play ({err[:80]})'
    with open(path, 'rb') as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            data = None
            for typ, off, sz, hdr in mp4_boxes(mm, len(mm)):
                if typ == 'moov':
                    data = mm[off:off + min(sz, 64 * MB)]
            if data is None:
                return None, 'reference has no index (moov)'
            res = None
            for tag, parser in ((b'avcC', parse_avcc), (b'hvcC', parse_hvcc)):
                i = data.find(tag)
                if i >= 4:
                    sz = struct.unpack('>I', data[i - 4:i])[0]
                    res = parser(data[i + 4:i - 4 + sz])
                    break
            if not res:
                return None, 'reference video is not H.264 or H.265'
        finally:
            mm.close()
    codec, lsize, sets = res
    v = next((s for s in info['streams'] if s.get('codec_type') == 'video'), {})
    a = next((s for s in info['streams'] if s.get('codec_type') == 'audio'), None)
    fps = v.get('avg_frame_rate') or v.get('r_frame_rate') or '30/1'
    return {'codec': codec, 'lsize': lsize, 'sets': sets, 'fps': fps, 'width': v.get('width'),
            'height': v.get('height'), 'audio': a}, ''


def inband_sets(mm, start, end, codec, lsize=4, limit=64 * MB):
    """Parameter sets repeated inside the video itself."""
    want = {7: 'sps', 8: 'pps'} if codec == 'h264' else {32: 'vps', 33: 'sps', 34: 'pps'}
    got = {}
    for kind, off, ln in walk_nals(mm, start, min(end, start + limit), codec, lsize):
        if kind != 'nal':
            continue
        h = mm[off + lsize]
        t = (h & 0x1F) if codec == 'h264' else (h >> 1) & 0x3F
        if t in want and want[t] not in got:
            got[want[t]] = bytes(mm[off + lsize:off + ln])
        if len(got) == len(want):
            break
    order = ['sps', 'pps'] if codec == 'h264' else ['vps', 'sps', 'pps']
    return [got[k] for k in order] if len(got) == len(want) else None


# ---- H.264 parameter search -----------------------------------------------------------

def make_sps(w, h, log2_fn, poc_type, log2_poc, profile=100, level=51, num_ref=4, transform8x8=True):
    mbw, mbh = (w + 15) // 16, (h + 15) // 16
    bw = BitWriter()
    bw.u(8, profile)
    bw.u(8, 0)
    bw.u(8, level)
    bw.ue(0)                        # seq_parameter_set_id
    if profile in (100, 110, 122, 244):
        bw.ue(1)                    # chroma 4:2:0
        bw.ue(0)
        bw.ue(0)                    # 8-bit
        bw.u(1, 0)
        bw.u(1, 0)                  # no scaling matrices
    bw.ue(log2_fn - 4)
    bw.ue(poc_type)
    if poc_type == 0:
        bw.ue(log2_poc - 4)
    bw.ue(num_ref)
    bw.u(1, 0)
    bw.ue(mbw - 1)
    bw.ue(mbh - 1)
    bw.u(1, 1)                      # frame_mbs_only
    bw.u(1, 1)                      # direct_8x8_inference
    crop_r, crop_b = (mbw * 16 - w) // 2, (mbh * 16 - h) // 2
    if crop_r or crop_b:
        bw.u(1, 1)
        bw.ue(0)
        bw.ue(crop_r)
        bw.ue(0)
        bw.ue(crop_b)
    else:
        bw.u(1, 0)
    bw.u(1, 0)                      # no VUI
    return b'\x67' + escape(bw.rbsp())


def make_pps(cabac, transform8x8, deblock_present, num_ref_default=0, init_qp=0, weighted=0):
    bw = BitWriter()
    bw.ue(0)
    bw.ue(0)
    bw.u(1, 1 if cabac else 0)
    bw.u(1, 0)                      # bottom_field_pic_order_in_frame_present
    bw.ue(0)                        # one slice group
    bw.ue(num_ref_default)
    bw.ue(0)
    bw.u(1, weighted)
    bw.u(2, 0)
    bw.se(init_qp)
    bw.se(0)
    bw.se(0)                        # chroma_qp_index_offset
    bw.u(1, 1 if deblock_present else 0)
    bw.u(1, 0)
    bw.u(1, 0)
    bw.u(1, 1 if transform8x8 else 0)
    bw.u(1, 0)
    bw.se(0)
    return b'\x68' + escape(bw.rbsp())


def _slice_samples(mm, start, end, lsize=4, n=300):
    out = []
    for kind, off, ln in walk_nals(mm, start, end, 'h264', lsize):
        if kind != 'nal':
            continue
        h = mm[off + lsize]
        t = h & 0x1F
        if t in (1, 5):
            out.append((t, h >> 5, unescape(bytes(mm[off + lsize + 1:off + lsize + 1 + 96]))))
            if len(out) >= n:
                break
    return out


def _parse_header(rbsp, t, ref, k, poc_type, m, cabac, deblock, weighted):
    """Parse an H.264 slice header with guessed SPS/PPS fields; returns a plausibility score.
    Only fields that come after the guessed ones earn points, so every guess is judged alike."""
    r = BitReader(rbsp)
    first_mb = r.ue()
    st = r.ue()
    if st > 9:
        return -5, None
    st %= 5
    pps_id = r.ue()
    if pps_id > 3:
        return -5, None
    fn = r.u(k)
    score = 0
    if t == 5:
        idr_id = r.ue()
        score += 1 if idr_id < 256 else -2
    poc = None
    if poc_type == 0:
        poc = r.u(m)
        if t == 5 and poc != 0:
            score -= 2
    if st == 1:                                     # B slice: too many variables, skip the rest
        return score, (first_mb, st, fn, poc)
    nref = 1
    if st in (0, 3):                                # P / SP
        if r.u(1):
            nr = r.ue()
            if nr >= 16:
                return score - 3, (first_mb, st, fn, poc)
            nref = nr + 1
        if r.u(1):                                  # list modification: stop here
            return score, (first_mb, st, fn, poc)
        if weighted:
            ld, cd = r.ue(), r.ue()
            if ld > 7 or cd > 7:
                return score - 4, (first_mb, st, fn, poc)
            score += 1
            for _ in range(nref):
                if r.u(1):
                    w, o = r.se(), r.se()
                    if not (-128 <= w <= 127 and -128 <= o <= 127):
                        return score - 3, (first_mb, st, fn, poc)
                if r.u(1):
                    for _c in range(2):
                        w, o = r.se(), r.se()
                        if not (-128 <= w <= 127 and -128 <= o <= 127):
                            return score - 3, (first_mb, st, fn, poc)
    if ref:
        if t == 5:
            r.u(1)
            lt = r.u(1)
            score += 1 if lt == 0 else 0
        elif r.u(1):                                # adaptive marking: stop here
            return score, (first_mb, st, fn, poc)
    if cabac and st in (0, 3):
        ci = r.ue()
        score += 1 if ci <= 2 else -4
    qpd = r.se()
    score += 2 if -20 <= qpd <= 20 else -4
    if deblock:
        idc = r.ue()
        if idc > 2:
            return score - 4, (first_mb, st, fn, poc)
        score += 1
        if idc != 1:
            a, b = r.se(), r.se()
            score += 1 if -6 <= a <= 6 and -6 <= b <= 6 else -3
    return score, (first_mb, st, fn, poc, qpd)


def analyse_slices(samples):
    """Work out frame_num width, picture order layout, entropy coder and deblocking flag."""
    # frame_num width: IDR pictures must read 0, and it must count up by 0/1 between pictures
    best_k, best = None, -1e9
    for k in range(4, 17):
        sc = 0
        prev = None
        for t, ref, rb in samples:
            try:
                r = BitReader(rb)
                if r.ue() != 0:
                    continue
                r.ue()
                r.ue()
                fn = r.u(k)
            except (EOFError, ValueError):
                sc -= 2
                continue
            if t == 5:
                sc += 3 if fn == 0 else -3
            elif prev is not None:
                d = (fn - prev) % (1 << k)
                sc += 2 if d in (0, 1) else -2
            prev = fn
        if sc > best:
            best_k, best = k, sc
    k = best_k
    combos = [(0, m) for m in range(4, 17)] + [(2, 0)]
    results = []
    for poc_type, m in combos:
        for cabac in (1, 0):
            for deblock in (1, 0):
                for weighted in (0, 1):
                    total = 0
                    qps = []
                    pocs = []
                    for t, ref, rb in samples:
                        try:
                            s, info = _parse_header(rb, t, ref, k, poc_type, m, cabac, deblock, weighted)
                        except (EOFError, ValueError):
                            s, info = -3, None
                        total += s
                        if info and len(info) == 5:
                            qps.append(info[4])
                        if info and poc_type == 0 and info[3] is not None and info[1] != 1:
                            pocs.append(info[3])
                    if poc_type == 0 and len(pocs) > 3:
                        # picture order counts climb by a small even step between pictures
                        good = sum(1 for a_, b_ in zip(pocs, pocs[1:]) if 0 < (b_ - a_) % (1 << m) <= 8)
                        total += good - (len(pocs) - 1 - good)
                    results.append((total, poc_type, m, cabac, deblock, weighted, qps))
    results.sort(key=lambda r: -r[0])
    alts = []
    for r_ in results[1:]:
        if r_[0] >= results[0][0] - max(10, abs(results[0][0]) * 0.1) and len(alts) < 4:
            alts.append({'poc_type': r_[1], 'm': r_[2] or 4, 'cabac': r_[3], 'deblock': r_[4], 'weighted': r_[5]})
    total, poc_type, m, cabac, deblock, weighted, qps = results[0]
    qps.sort()
    med = qps[len(qps) // 2] if qps else 0
    init_qp = 0 if 16 <= 26 + med <= 36 else max(-26, min(25, -med))
    max_first_mb = 0
    for t, ref, rb in samples:
        try:
            max_first_mb = max(max_first_mb, BitReader(rb).ue())
        except (EOFError, ValueError):
            pass
    return {'k': k, 'poc_type': poc_type, 'm': m or 4, 'cabac': cabac, 'deblock': deblock, 'weighted': weighted,
            'init_qp': init_qp, 'max_first_mb': max_first_mb, 'alts': alts}


RESOLUTIONS = [(1920, 1080), (1280, 720), (1080, 1920), (720, 1280), (3840, 2160), (2160, 3840), (2560, 1440),
               (1440, 2560), (2704, 1520), (2704, 2028), (2688, 1512), (1440, 1080), (1920, 1440), (4096, 2160),
               (960, 540), (854, 480), (640, 480), (640, 360), (480, 640), (1280, 960), (1600, 1200),
               (1024, 768), (800, 600), (720, 480), (720, 576), (352, 288), (320, 240), (2048, 1536)]


_CONCEAL_RE = re.compile(r'concealing (\d+) DC')


def _decode(ff, sps, pps, frames_bytes, nframes=1):
    """Decode a test stream -> (gray frames, trouble). Trouble counts macroblocks ffmpeg had to
    invent ('concealing ...', logged at debug level) plus hard errors, so a wrong guess - too
    many macroblocks or too few - always scores worse than the right one."""
    stream = b''.join(b'\x00\x00\x00\x01' + x for x in (sps, pps)) + frames_bytes
    try:
        rc, out, err = _run([ff, '-v', 'debug', '-f', 'h264', '-i', 'pipe:0', '-frames:v', str(nframes),
                             '-f', 'rawvideo', '-pix_fmt', 'gray', 'pipe:1'],
                            timeout=60, input_bytes=stream, binary=True)
    except subprocess.TimeoutExpired:
        return b'', 10 ** 6
    trouble = sum(int(m) for m in _CONCEAL_RE.findall(err))
    for line in err.splitlines():
        low = line.lower()
        if ('error' in low and 'decode errors;' not in low and 'concealing' not in low) or 'invalid' in low \
                or 'out of range' in low:
            trouble += 100
    return out, trouble


def _smoothness(img, w, h):
    """Natural pictures change gently between neighbouring pixels; wrong sizes shear and scramble."""
    diff = cnt = 0
    for y in range(1, h - 1, max(1, h // 120)):
        row, prev_row = y * w, (y - 1) * w
        for x in range(1, w, max(1, w // 160)):
            diff += abs(img[row + x] - img[row + x - 1]) + abs(img[row + x] - img[prev_row + x])
            cnt += 1
    return diff / max(cnt, 1)


def _clipped(img):
    return (img.count(0) + img.count(1) + img.count(254) + img.count(255)) / max(len(img), 1)


def _flat_tail(img, w, h):
    """A picture decoded at too large a size ends in a flat filler band (no data for those rows)."""
    band = max(16, h // 10)
    vals = img[(h - band) * w:h * w:max(1, w // 64)]
    if not vals:
        return False
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return var < 2.0


def _score(ff, sps, pps, frames, w, h, nframes=1):
    out, errs = _decode(ff, sps, pps, frames, nframes)
    if len(out) < w * h:
        return 1e8 + errs
    first = out[:w * h]
    pen = 5000 if _flat_tail(first, w, h) else 0
    return errs * 1000 + pen + _smoothness(first, w, h)


def search_h264_params(mm, start, end, tools, log=print):
    ff, _ = tools
    samples = _slice_samples(mm, start, end)
    if len(samples) < 5:
        return None, 'too few video frames to analyse'
    a = analyse_slices(samples)
    log(f"  frame numbering: {a['k']} bits, picture order type {a['poc_type']}"
        + (f" ({a['m']} bits)" if a['poc_type'] == 0 else '') +
        f", {'CABAC' if a['cabac'] else 'CAVLC'}, deblocking control {'on' if a['deblock'] else 'off'}, "
        f"weighted prediction {'on' if a['weighted'] else 'off'}")
    # the first keyframe (IDR) and the frames after it, for test decodes
    gop, leading = [], []
    for kind, off, ln in walk_nals(mm, start, end, 'h264'):
        if kind != 'nal':
            continue
        t = mm[off + 4] & 0x1F
        if t not in (1, 5) or (t == 1 and not gop):
            continue
        nal = b'\x00\x00\x00\x01' + bytes(mm[off + 4:off + ln])
        gop.append(nal)
        if t == 5 and len(leading) == len(gop) - 1:
            leading.append(nal)
        if len(gop) > 60:
            break
    if not leading:
        return None, 'no keyframe found'
    idr = b''.join(leading)
    gop_bytes = b''.join(gop)
    mbs_needed = a['max_first_mb'] + 1
    sizes = [(w, h) for w, h in RESOLUTIONS if ((w + 15) // 16) * ((h + 15) // 16) >= mbs_needed]

    def sps_for(w, h, lay=None, nr=4):
        lay = lay or a                     # (looked up at call time: stage 1 may refine the layout)
        return make_sps(w, h, a['k'], lay['poc_type'], lay['m'], num_ref=nr)

    def pps_for(t8, q, lay=None, nref=0):
        lay = lay or a
        return make_pps(lay['cabac'], t8, lay['deblock'], num_ref_default=nref, init_qp=q, weighted=lay['weighted'])

    # stage 1: starting QP. With CABAC a wrong value makes decoding fail outright, and so does a
    # wrong header layout - so close-call layouts are tried here too.
    qp = None
    common = sizes[:6]
    layouts = [a] + a.get('alts', [])
    if a['cabac']:
        for q in sorted(range(-16, 17), key=abs):
            for lay in layouts:
                if not lay['cabac']:
                    continue
                for w, h in common:
                    for t8 in (1, 0):
                        out, trouble = _decode(ff, sps_for(w, h, lay), pps_for(t8, q, lay), idr)
                        if trouble == 0 and len(out) >= w * h:
                            qp = q
                            a = dict(a, **{k_: lay[k_] for k_ in ('poc_type', 'm', 'cabac', 'deblock', 'weighted')})
                            break
                    if qp is not None:
                        break
                if qp is not None:
                    break
            if qp is not None:
                break
        if qp is None:
            qp = a['init_qp']
    # stage 2: picture size and 8x8 transform. Too few macroblocks always produces decode errors;
    # too many decodes "cleanly" with the extra area concealed - so the right size is the error-free
    # one with the fewest macroblocks, and smoothness only breaks ties (e.g. landscape vs portrait).
    results = []
    tried = 0
    for w, h in sizes:
        for t8 in (1, 0):
            out, errs = _decode(ff, sps_for(w, h), pps_for(t8, qp if qp is not None else 0), gop_bytes,
                                nframes=min(12, len(gop)))
            tried += 1
            if len(out) < w * h:
                continue
            mbs = ((w + 15) // 16) * ((h + 15) // 16)
            results.append((errs, mbs, _smoothness(out[:w * h], w, h), w, h, t8))
    if not results:
        return None, 'no combination decoded'
    least = min(r[0] for r in results)
    best_set = [r for r in results if r[0] == least]
    fewest = min(r[1] for r in best_set)
    pick = min((r for r in best_set if r[1] == fewest), key=lambda r: r[2])
    _, _, _, w, h, t8 = pick
    # CAVLC: a wrong QP doesn't break decoding but saturates (too high) or flattens (too low) the
    # picture. Take the highest QP that doesn't add clipped pixels.
    if not a['cabac']:
        clips = []
        for q in range(-16, 17):
            out, errs = _decode(ff, sps_for(w, h), pps_for(t8, q), idr)
            if len(out) >= w * h and errs == 0:
                clips.append((q, _clipped(out[:w * h])))
        if clips:
            lo = min(c for _, c in clips)
            qp = max(q for q, c in clips if c <= lo + 0.0002)
        else:
            qp = a['init_qp']
    log(f'  picture size {w}x{h}, 8x8 transform {"on" if t8 else "off"}, starting QP {26 + qp} '
        f'(tested {tried} sizes)')
    # stage 3: close calls on the header layout -> decode a run of frames with each
    layouts = [a] + [l_ for l_ in a.get('alts', []) if any(l_[k_] != a[k_] for k_ in ('poc_type', 'm', 'cabac', 'deblock', 'weighted'))]
    best_l = None
    for lay in layouts:
        sc = _score(ff, sps_for(w, h, lay), pps_for(t8, qp, lay), gop_bytes, w, h, nframes=min(30, len(gop)))
        if best_l is None or sc < best_l[0]:
            best_l = (sc, lay)
    lay = best_l[1]
    # stage 4: default reference-frame count (affects P frames only)
    best_ref = None
    for nr in (0, 1, 2, 3):
        sc = _score(ff, sps_for(w, h, lay), pps_for(t8, qp, lay, nr), gop_bytes, w, h, nframes=min(30, len(gop)))
        if best_ref is None or sc < best_ref[0]:
            best_ref = (sc, nr)
    sps = sps_for(w, h, lay)
    pps = pps_for(t8, qp, lay, best_ref[1])
    return {'codec': 'h264', 'lsize': 4, 'sets': [sps, pps], 'fps': None, 'width': w, 'height': h,
            'audio': None}, ''


# ======================================================================================
# Repair
# ======================================================================================

def _ok_output(tools, path):
    st, txt = check_path(path, os.path.splitext(path)[1].lstrip('.'), tools)
    return st == 'ok', txt


def repair_video(path, out_dir, tools=None, reference=None, log=print, allow_search=True, fps_hint=None):
    """Returns (output path or None, message)."""
    tools = tools or find_ffmpeg()
    ff, fp = tools
    if not ff:
        return None, 'ffmpeg was not found (FlashRescue offers to download it; or put ffmpeg.exe and ffprobe.exe in the tools folder)'
    base = os.path.splitext(os.path.basename(path))[0]
    ext = os.path.splitext(path)[1].lower().lstrip('.') or 'mp4'
    os.makedirs(out_dir, exist_ok=True)

    def outname(suffix, e):
        p = os.path.join(out_dir, f'{base} (repaired{suffix}).{e}')
        k = 2
        while os.path.exists(p):
            p = os.path.join(out_dir, f'{base} (repaired{suffix} {k}).{e}')
            k += 1
        return p

    # 1. anything ffmpeg can open: re-wrap, dropping damage
    info, err = probe(fp, path)
    if info and info.get('streams'):
        log('The file opens - re-wrapping it and dropping damaged parts...')
        e = 'mkv' if ext in ('mkv', 'webm') else ('avi' if ext == 'avi' else 'mp4')
        out = outname('', e)
        extra = ['-fflags', '+genpts+discardcorrupt'] if ext in ('avi', 'mpg', 'mpeg', 'mts', 'm2ts', 'flv') else []
        cmd = [ff, '-v', 'error', '-y', '-err_detect', 'ignore_err'] + extra + ['-i', path, '-map', '0', '-c', 'copy']
        if e == 'mp4':
            cmd += ['-movflags', '+faststart']
        rc, o, er = _run(cmd + [out], timeout=3600)
        if rc == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            ok, txt = _ok_output(tools, out)
            if ok:
                return out, f'Re-wrapped: {txt}'
            log(f'  re-wrapped copy still has problems ({txt}); trying a full rebuild...')
        else:
            log('  re-wrapping failed; trying a full rebuild...')
        if ext not in ('mp4', 'mov', 'm4v', '3gp'):
            return (out if os.path.exists(out) else None), 'Could not repair this file completely'

    # 2. raw rebuild from the media data
    with open(path, 'rb') as f:
        size = os.path.getsize(path)
        if size < 4 * KB:
            return None, 'file is too small'
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            start, end = find_media_data(mm, size)
            log(f'Media data: {(end - start) / MB:.1f} MB')
            params, why_ref = None, ''
            if reference:
                params, why_ref = reference_info(reference, tools)
                if params:
                    log(f"Using codec settings from the reference video ({params['codec'].upper()}, "
                        f"{params['width']}x{params['height']}).")
                else:
                    log(f'Reference video not usable: {why_ref}')
            codec = params['codec'] if params else guess_codec(mm, start, end)
            if not codec:
                return None, 'No H.264/H.265 video frames were found in the file'
            lsize = params['lsize'] if params else 4
            inb = inband_sets(mm, start, end, codec, lsize)
            if inb and not params:
                log(f'Codec settings found inside the video itself ({codec.upper()}).')
                params = {'codec': codec, 'lsize': lsize, 'sets': inb, 'fps': None, 'audio': None}
            if not params:
                if codec == 'h264' and allow_search:
                    log('No settings in the file and no reference - searching for them (H.264)...')
                    params, why = search_h264_params(mm, start, end, tools, log)
                    if not params:
                        return None, f'Could not work out the codec settings ({why}). Try a reference video.'
                else:
                    return None, ('This is H.265/HEVC video without its settings. Choose a reference video: any healthy '
                                  'clip recorded by the same model of phone or camera (often on the same card).')
            # write the frames out as a raw elementary stream
            tmpd = tempfile.mkdtemp(prefix='flashrescue_repair_')
            es = os.path.join(tmpd, 'video.' + ('h264' if codec == 'h264' else 'hevc'))
            audio_raw = os.path.join(tmpd, 'audio.raw')
            a = params.get('audio')
            pcm = a and str(a.get('codec_name', '')).startswith('pcm_')
            nframes = 0
            allowed = header_profile(mm, start, end, codec, lsize)
            with open(es, 'wb') as out:
                for s in params['sets']:
                    out.write(b'\x00\x00\x00\x01' + s)
                af = open(audio_raw, 'wb') if pcm else None
                try:
                    for kind, off, ln in walk_nals(mm, start, end, codec, lsize, allowed=allowed):
                        if kind == 'nal':
                            out.write(b'\x00\x00\x00\x01')
                            out.write(mm[off + lsize:off + ln])
                            nframes += 1
                        elif af:
                            af.write(mm[off:off + ln])
                finally:
                    if af:
                        af.close()
            log(f'Recovered {nframes:,} video pieces.')
        finally:
            mm.close()
    fps = params.get('fps') or fps_hint or '30'
    out = outname('', 'mp4')
    with_audio = pcm and os.path.getsize(audio_raw) > 0
    # step 1: wrap the video on its own (gives every frame a timestamp)
    vid_only = os.path.join(tmpd, 'video_only.mp4') if with_audio else out
    cmd = [ff, '-v', 'error', '-y', '-fflags', '+genpts', '-r', str(fps), '-f',
           'h264' if codec == 'h264' else 'hevc', '-i', es, '-c:v', 'copy']
    if codec == 'hevc':
        cmd += ['-tag:v', 'hvc1']
    cmd += ['-movflags', '+faststart', vid_only]
    rc, o, er = _run(cmd, timeout=3600)
    # step 2: add the rebuilt uncompressed audio
    if rc == 0 and with_audio:
        fmt = a['codec_name'].replace('pcm_', '')
        cmd = [ff, '-v', 'error', '-y', '-i', vid_only, '-f', fmt, '-ar', str(a.get('sample_rate', 48000)),
               '-ac', str(a.get('channels', 2)), '-i', audio_raw, '-map', '0:v', '-map', '1:a', '-c:v', 'copy',
               '-c:a', 'aac', '-b:a', '256k', '-shortest', '-movflags', '+faststart', out]
        rc, o, er = _run(cmd, timeout=3600)
    shutil.rmtree(tmpd, ignore_errors=True)
    if rc != 0 or not os.path.exists(out):
        return None, f'ffmpeg could not rebuild the video: {er.strip()[:200]}'
    ok, txt = _ok_output(tools, out)
    sound = '' if pcm else ' (no sound - compressed audio cannot be rebuilt without the index)'
    if not params.get('fps') and not fps_hint:
        sound += ' Speed assumed to be 30 frames per second.'
    elif not params.get('fps'):
        sound += f' Speed taken from a clip by the same device ({fps_hint} fps).'
    return out, (f'Rebuilt: {txt}{sound}' if ok else f'Rebuilt, but it still has problems: {txt}{sound}')


def frame_rate(path, tools):
    info, _ = probe(tools[1], path) if tools and tools[1] else (None, '')
    if not info:
        return None
    v = next((s for s in info['streams'] if s.get('codec_type') == 'video'), None)
    fr = v and (v.get('avg_frame_rate') or v.get('r_frame_rate'))
    return fr if fr and fr != '0/0' else None


def pick_reference(items, broken, tools, src=None):
    """From scan results, the healthy video most likely to share the broken one's settings."""
    fam = {'mp4', 'mov', 'm4v', '3gp'}
    cands = [i for i in items if i.ext in fam and i is not broken and i.condition in ('Good', 'Not deleted')
             and 'Index missing' not in i.condition and i.size > 100 * KB]
    if getattr(broken, 'size', 0):
        cands.sort(key=lambda i: (i.ext != broken.ext, abs(i.size - broken.size)))
    return cands[:12]
