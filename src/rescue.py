"""
FlashRescue rescue imaging: copy a failing drive to an image file while being as gentle
on it as possible. Modelled on GNU ddrescue, and uses the same map file format, so a
rescue can be continued with ddrescue (or a ddrescue map loaded here).

Passes
  0. (optional, on by default) Copy what matters most first: the partition tables, the file
     tables / folders (by running a quick scan through the imager), then the data of the
     deleted files that scan finds. If the drive dies an hour in, the image already holds
     everything a scan needs, and the files that were asked for.
  1. Copy forward in big blocks. Anything that errors or reads slowly is skipped, with
     the skip growing while trouble continues, so the easy data is saved first and the
     drive isn't hammered on its bad spots.
  2. Copy what was skipped, in reverse (reaches the far edge of bad areas).
  3. Copy anything still untried, without skipping.
  4. Trim: shrink each failed block from both ends, sector by sector.
  5. Scrape: try the remaining insides of failed blocks, sector by sector.
  6. Retry bad sectors (optional, N times).

The map is saved every few seconds, so a crash, disconnect or Stop loses nothing. Image
writes run on their own thread, so the drive never waits for the destination disk; the map
only ever records data that has been flushed to the image.
"""

import os
import sys
import time
import queue
import bisect
import threading

import engine
from engine import human_size

MB = 1024 * 1024

UNTRIED, NONTRIMMED, NONSCRAPED, BAD, FINISHED = '?', '*', '/', '-', '+'
STATUS_NAMES = {UNTRIED: 'Not tried yet', NONTRIMMED: 'Failed block (to trim)', NONSCRAPED: 'Failed block (to scrape)',
                BAD: 'Bad sector', FINISHED: 'Rescued'}
PASS_NAMES = {0: 'First: copying file tables and deleted files', 1: 'Pass 1 of 6: copying the easy parts', 2: 'Pass 2 of 6: copying skipped areas in reverse',
              3: 'Pass 3 of 6: copying what is left', 4: 'Pass 4 of 6: trimming failed blocks',
              5: 'Pass 5 of 6: scraping failed blocks', 6: 'Pass 6 of 6: retrying bad sectors', 7: 'Finished'}

# Windows error codes that mean "the drive went away" rather than "this sector is bad"
DISCONNECT_ERRORS = {2, 3, 6, 15, 21, 55, 433, 1167, 1110, 1006}


# --------------------------------------------------------------------------------------
# Map file (ddrescue format)
# --------------------------------------------------------------------------------------

class Mapbook:
    def __init__(self, size):
        self.size = size
        self.regs = [(0, size, UNTRIED)]
        self.starts = [0]
        self.current_pos = 0
        self.current_status = UNTRIED
        self.current_pass = 1
        self.lock = threading.RLock()        # the image writer thread marks finished areas

    def set(self, pos, size, st):
        with self.lock:
            self._set(pos, size, st)

    def _set(self, pos, size, st):
        end = min(pos + size, self.size)
        if pos < 0 or end <= pos:
            return
        i = bisect.bisect_right(self.starts, pos) - 1
        j = bisect.bisect_left(self.starts, end)
        new = []
        a, l, s = self.regs[i]
        if a < pos:
            new.append((a, pos - a, s))
        new.append((pos, end - pos, st))
        a2, l2, s2 = self.regs[j - 1]
        if a2 + l2 > end:
            new.append((end, a2 + l2 - end, s2))
        lo, hi = i, j
        if lo > 0 and self.regs[lo - 1][2] == new[0][2]:
            p = self.regs[lo - 1]
            new[0] = (p[0], p[1] + new[0][1], p[2])
            lo -= 1
        if hi < len(self.regs) and self.regs[hi][2] == new[-1][2]:
            nx = self.regs[hi]
            new[-1] = (new[-1][0], new[-1][1] + nx[1], nx[2])
            hi += 1
        merged = [new[0]]
        for r in new[1:]:
            if r[2] == merged[-1][2]:
                merged[-1] = (merged[-1][0], merged[-1][1] + r[1], r[2])
            else:
                merged.append(r)
        self.regs[lo:hi] = merged
        self.starts[lo:hi] = [r[0] for r in merged]

    def status_at(self, pos):
        with self.lock:
            i = bisect.bisect_right(self.starts, pos) - 1
            return self.regs[i][2]

    def regions(self, statuses, reverse=False):
        with self.lock:
            rs = [r for r in self.regs if r[2] in statuses]
        return list(reversed(rs)) if reverse else rs

    def regions_in(self, a, b):
        """[(start, length, status)] covering [a, b)."""
        with self.lock:
            i = max(0, bisect.bisect_right(self.starts, a) - 1)
            out = []
            while i < len(self.regs) and self.regs[i][0] < b:
                s0, l0, st = self.regs[i]
                lo, hi = max(a, s0), min(b, s0 + l0)
                if hi > lo:
                    out.append((lo, hi - lo, st))
                i += 1
            return out

    def totals(self):
        t = {UNTRIED: 0, NONTRIMMED: 0, NONSCRAPED: 0, BAD: 0, FINISHED: 0}
        with self.lock:
            regs = list(self.regs)
        for _, l, s in regs:
            t[s] = t.get(s, 0) + l
        return t

    def bad_ranges(self):
        """Everything not rescued, as [(start, end)] - used to flag damaged files."""
        with self.lock:
            return [(a, a + l) for a, l, s in self.regs if s != FINISHED]

    def cells(self, n):
        """One status code per cell for the block map: 'g' all rescued, 'p' partly rescued,
        'u' untried, 'f' failed block, 'b' contains bad sectors."""
        if self.size <= 0:
            return ['u'] * n
        flags = [0] * n          # bit0 rescued, bit1 untried, bit2 failed, bit3 bad
        bitof = {FINISHED: 1, UNTRIED: 2, NONTRIMMED: 4, NONSCRAPED: 4, BAD: 8}
        with self.lock:
            regs = list(self.regs)
        for a, l, s in regs:
            c0 = a * n // self.size
            c1 = min(n - 1, (a + l - 1) * n // self.size)
            b = bitof[s]
            for c in range(c0, c1 + 1):
                flags[c] |= b
        out = []
        for f in flags:
            if f & 8:
                out.append('b')
            elif f & 4:
                out.append('f')
            elif f == 1:
                out.append('g')
            elif f & 1:
                out.append('p')
            else:
                out.append('u')
        return out

    # -- file I/O
    def save(self, path, source_label=''):
        tmp = path + '.tmp'
        with self.lock:
            regs = list(self.regs)
        with open(tmp, 'w', encoding='ascii', errors='replace') as f:
            f.write('# Mapfile. Created by FlashRescue (GNU ddrescue compatible)\n')
            f.write(f'# Source: {source_label}\n')
            f.write(f'# Saved: {time.strftime("%Y-%m-%d %H:%M:%S")}\n')
            f.write(f'# current_pos  current_status  current_pass\n')
            f.write(f'0x{self.current_pos:08X}     {self.current_status}               {self.current_pass}\n')
            f.write('#      pos        size  status\n')
            f.write(''.join(f'0x{a:08X}  0x{l:08X}  {s}\n' for a, l, s in regs))
        os.replace(tmp, path)

    @classmethod
    def load(cls, path, size):
        mb = cls(size)
        regs = []
        header = None
        with open(path, 'r', encoding='ascii', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split()
                if header is None:
                    header = parts
                    continue
                if len(parts) >= 3:
                    regs.append((int(parts[0], 0), int(parts[1], 0), parts[2][0]))
        if header:
            mb.current_pos = int(header[0], 0)
            mb.current_status = header[1][0] if len(header) > 1 else UNTRIED
            try:
                mb.current_pass = int(header[2]) if len(header) > 2 else 1
            except ValueError:
                mb.current_pass = 1
        for a, l, s in regs:
            if s in STATUS_NAMES:
                mb.set(a, l, s)
        return mb


def find_map_for_image(image_path):
    for cand in (image_path + '.map', os.path.splitext(image_path)[0] + '.map',
                 image_path + '.mapfile', os.path.splitext(image_path)[0] + '.log'):
        if os.path.isfile(cand):
            return cand
    return None


# --------------------------------------------------------------------------------------
# Reading with a timeout
# --------------------------------------------------------------------------------------

def _cancel_sync_io(native_id):
    """Ask Windows to abort a read that's stuck inside a driver retry loop."""
    if os.name != 'nt' or not native_id:
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenThread(0x0001, False, native_id)      # THREAD_TERMINATE right is required
        if not h:
            return False
        ok = k32.CancelSynchronousIo(h)
        k32.CloseHandle(h)
        return bool(ok)
    except Exception:
        return False


class TimedReader:
    """Reads on a worker thread so a read that hangs on a bad sector can be timed out."""

    def __init__(self, path, opener=None):
        self.path = path
        self.f = (opener or (lambda p: open(p, 'rb', buffering=0)))(path)
        self.req = queue.Queue()
        self.res = queue.Queue()
        self.native_id = None
        self.dead = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        self.native_id = threading.get_native_id() if hasattr(threading, 'get_native_id') else None
        while True:
            job = self.req.get()
            if job is None:
                return
            off, n = job
            t = time.time()
            try:
                self.f.seek(off)
                d = self.f.read(n)
                self.res.put(('ok', d, time.time() - t))
            except OSError as e:
                self.res.put(('err', e, time.time() - t))

    def read(self, off, n, timeout):
        """('ok', data, secs) | ('err', OSError, secs) | ('timeout', None, secs) | ('hung', None, secs)"""
        t = time.time()
        self.req.put((off, n))
        try:
            return self.res.get(timeout=timeout)
        except queue.Empty:
            pass
        _cancel_sync_io(self.native_id)
        try:
            r = self.res.get(timeout=max(20.0, timeout * 3))
            if r[0] == 'ok':
                return r            # finished on its own just after the deadline
            return ('timeout', None, time.time() - t)
        except queue.Empty:
            self.dead = True        # driver won't let go; this handle is abandoned
            return ('hung', None, time.time() - t)

    def close(self):
        if not self.dead:
            self.req.put(None)
            try:
                self.f.close()
            except Exception:
                pass


def keep_awake(on):
    """Stop Windows sleeping in the middle of a long rescue."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:
        pass


def _set_sparse(f):
    """Make the image file sparse so unread areas take no disk space (NTFS)."""
    if os.name == 'nt':
        engine.win_ioctl(f, 0x000900C4, b'\x01', 0)


# --------------------------------------------------------------------------------------
# Image writer (own thread)
# --------------------------------------------------------------------------------------

class ImageWriter:
    """Writes rescued blocks to the image on its own thread, so the failing drive keeps reading
    while the destination disk catches up. Blocks are marked 'rescued' in the map only after a
    flush to disk, so a power cut can never leave the map claiming data the image doesn't have."""

    SYNC_SECS = 5.0

    def __init__(self, imager, max_mb=96):
        self.im = imager
        self.q = queue.Queue()
        self.room = threading.Semaphore(max_mb)      # in-flight limit, counted in MB
        self.unsynced = []
        self.inflight = 0                  # bytes queued or written but not yet flushed (for display)
        self.last_sync = time.time()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def put(self, pos, data):
        units = max(1, len(data) // MB)
        got = 0
        while got < units:
            if self.room.acquire(timeout=0.5):
                got += 1
            elif not self.t.is_alive():
                return False
        self.inflight += len(data)
        self.q.put((pos, data, units))
        return True

    def _sync(self):
        img = self.im.img
        try:
            img.flush()
            os.fsync(img.fileno())
        except (OSError, ValueError):
            return
        for pos, n in self.unsynced:
            self.im.map.set(pos, n, FINISHED)
            self.inflight -= n
        self.unsynced = []
        self.last_sync = time.time()

    def _run(self):
        while True:
            job = self.q.get()
            if job is None:
                self._sync()
                return
            if job[0] == 'sync':
                self._sync()
                job[1].set()
                continue
            pos, data, units = job
            written = False
            while not written:
                try:
                    self.im.img.seek(pos)
                    self.im.img.write(data)
                    written = True
                except OSError as ex:
                    self.im.state = 'paused'
                    self.im.pause_ev.set()
                    self.im.message = (f'Cannot write the image ({ex}). Free up space on the destination, '
                                       'then press Resume.')
                    self.im._report()
                    while self.im.pause_ev.is_set() and not self.im.stop_ev.is_set():
                        time.sleep(0.5)
                    if self.im.stop_ev.is_set():
                        self.inflight -= len(data)
                        break                   # dropped: stays 'not tried' in the map
            if written:
                self.unsynced.append((pos, len(data)))
                if time.time() - self.last_sync >= self.SYNC_SECS:
                    self._sync()
            for _ in range(units):
                self.room.release()

    def drain(self):
        """Return once everything queued so far is written, flushed and marked in the map."""
        ev = threading.Event()
        self.q.put(('sync', ev))
        while not ev.wait(0.5):
            if not self.t.is_alive():
                return

    def close(self):
        self.q.put(None)
        self.t.join()


class CopyOnReadSource(engine.RawSource):
    """What the quick scan sees during the first pass: reads come from the image when that part
    is already copied, otherwise from the drive through the imager (timeouts, bad-sector
    handling) and are written into the image on the way. So exactly the parts of the drive a
    scan needs get copied first - partition tables, file tables, folders."""

    def __init__(self, imager, label):
        self.im = imager
        self.path = imager.src_path + '#imaging'
        self.label = label
        self.size = imager.size
        self.sector_size = imager.sector_size
        self.ALIGN = imager.unit
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
        self.recent = {}                     # grain start -> bytes just copied (writes may be queued)
        self.copied = 0

    def close(self):
        pass

    def read(self, offset, length):
        if offset >= self.size or length <= 0:
            return b''
        length = min(length, self.size - offset)
        g = self.im.block                    # copy whole 1 MB neighbourhoods: the head is there anyway
        a = offset - offset % g
        e = min(self.size, -(-(offset + length) // g) * g)
        out = bytearray()
        p = a
        while p < e:
            grain = min(g, e - p)
            out += self._grain(p, grain)
            p += grain
        return bytes(out[offset - a: offset - a + length])

    def _grain(self, p, n):
        d = self.recent.get(p)
        if d is not None and len(d) >= n:
            return d[:n]
        buf = bytearray(n)
        need_img = False
        for s0, l0, st in self.im.map.regions_in(p, p + n):
            if st == UNTRIED and not self.im.stop_ev.is_set():
                data = self.im._read(s0, l0)
                if data:
                    buf[s0 - p:s0 - p + len(data)] = data
                    self.im._write(s0, data)
                    self.copied += len(data)
                    if len(data) < l0:
                        self.im.map.set(s0 + len(data), l0 - len(data), NONTRIMMED)
                        self.add_bad(s0 + len(data), l0 - len(data))
                else:
                    self.im.map.set(s0, l0, NONTRIMMED)
                    self.add_bad(s0, l0)
            elif st == FINISHED:
                need_img = True
            elif st != UNTRIED:
                self.add_bad(s0, l0)
        if need_img:
            self.im.writer.drain()
            for s0, l0, st in self.im.map.regions_in(p, p + n):
                if st == FINISHED:
                    try:
                        self.im.img.seek(s0)
                        got = self.im.img.read(l0)
                    except OSError:
                        got = b''
                    buf[s0 - p:s0 - p + len(got)] = got
        res = bytes(buf)
        if len(self.recent) > 64:
            self.recent.clear()
        self.recent[p] = res
        return res


# --------------------------------------------------------------------------------------
# The imager
# --------------------------------------------------------------------------------------

class RescueOptions:
    def __init__(self, block=1 * MB, unit=4096, timeout=8.0, slow_secs=2.0, retries=1,
                 reverse=True, cool_limit_c=50, cool_resume_c=None, important_first=True, max_block=4 * MB):
        self.important_first = important_first      # pass 0: file tables + deleted files first
        self.max_block = max_block      # pass 1 grows its reads up to this on healthy stretches
        self.block = block              # pass 1-3 read size
        self.unit = unit                # trim/scrape read size (sector-sized)
        self.timeout = timeout          # give up on a single read after this many seconds
        self.slow_secs = slow_secs      # a read slower than this counts as a "slow area" in pass 1
        self.retries = retries          # extra attempts on bad sectors (pass 6)
        self.reverse = reverse
        self.cool_limit_c = cool_limit_c            # pause above this drive temperature (0 = off)
        self.cool_resume_c = cool_resume_c or max(0, (cool_limit_c or 0) - 5)


class RescueImager:
    MAP_SAVE_SECS = 3.0

    def __init__(self, src_path, src_size, image_path, map_path, opts, on_status, on_progress,
                 source_label='', sector_size=512, temp_reader=None, opener=None):
        self.src_path = src_path
        self.size = src_size
        self.image_path = image_path
        self.map_path = map_path
        self.opts = opts
        self.on_status = on_status
        self.on_progress = on_progress
        self.source_label = source_label
        self.temp_reader = temp_reader      # callable -> degrees C or None
        self.opener = opener
        self.sector_size = sector_size
        self.unit = max(opts.unit, sector_size)
        self.block = max(opts.block - opts.block % self.unit, self.unit)
        self.stop_ev = threading.Event()
        self.pause_ev = threading.Event()
        self.reader = None
        self.img = None
        self.writer = None
        self.priority_note = ''
        self.errors = 0
        self.read_bytes_recent = []
        self.last_map_save = 0
        self.temp_c = None
        self.last_temp_check = 0
        self.state = 'idle'
        self.last_secs = 0
        self.started = None
        self.message = ''
        if map_path and os.path.isfile(map_path):
            self.map = Mapbook.load(map_path, src_size)
            self.resumed = True
        else:
            self.map = Mapbook(src_size)
            self.resumed = False

    # -- control
    def stop(self):
        self.stop_ev.set()

    def pause(self, on):
        if on:
            self.pause_ev.set()
        else:
            self.pause_ev.clear()

    # -- helpers
    def _save_map(self, force=False):
        if force or time.time() - self.last_map_save >= self.MAP_SAVE_SECS:
            try:
                self.map.save(self.map_path, self.source_label)
            except OSError as ex:
                self.on_status(f'Could not save the map file: {ex}')
            self.last_map_save = time.time()

    def stats(self):
        t = self.map.totals()
        now = time.time()
        self.read_bytes_recent = [(ts, n) for ts, n in self.read_bytes_recent if now - ts < 20]
        span = (now - self.read_bytes_recent[0][0]) if len(self.read_bytes_recent) > 1 else 0
        rate = sum(n for _, n in self.read_bytes_recent) / span if span > 0.5 else 0
        fly = max(0, self.writer.inflight) if self.writer else 0     # read, on its way to the image
        return {
            'size': self.size, 'rescued': t[FINISHED] + fly, 'bad': t[BAD], 'untried': max(0, t[UNTRIED] - fly),
            'failed': t[NONTRIMMED] + t[NONSCRAPED], 'pass': self.map.current_pass,
            'pos': self.map.current_pos, 'rate': rate, 'errors': self.errors, 'temp': self.temp_c,
            'state': self.state, 'elapsed': (now - self.started) if self.started else 0,
            'message': self.message,
        }

    def _report(self, force=False):
        now = time.time()
        if force or now - getattr(self, '_last_report', 0) >= 0.25 or self.state != 'running':
            self._last_report = now
            self.on_progress(self.stats())

    def _wait_if_paused_or_hot(self):
        while not self.stop_ev.is_set():
            if self.pause_ev.is_set():
                self.state = 'paused'
                self.message = 'Paused'
                self._save_map(True)
                self._report()
                time.sleep(0.5)
                continue
            if self.temp_reader and self.opts.cool_limit_c and time.time() - self.last_temp_check > 60:
                self.last_temp_check = time.time()
                try:
                    self.temp_c = self.temp_reader()
                except Exception:
                    self.temp_c = None
                if self.temp_c is not None and self.temp_c >= self.opts.cool_limit_c:
                    self.state = 'cooling'
                    self._save_map(True)
                    while not self.stop_ev.is_set() and not self.pause_ev.is_set():
                        self.message = (f'Drive is hot ({self.temp_c}°C) - resting until it cools to '
                                        f'{self.opts.cool_resume_c}°C')
                        self._report()
                        for _ in range(60):
                            if self.stop_ev.is_set() or self.pause_ev.is_set():
                                break
                            time.sleep(0.5)
                        try:
                            self.temp_c = self.temp_reader()
                        except Exception:
                            self.temp_c = None
                        if self.temp_c is None or self.temp_c <= self.opts.cool_resume_c:
                            break
                    self.last_temp_check = time.time()
                    continue
            self.state = 'running'
            return

    def _open_reader(self):
        while not self.stop_ev.is_set():
            try:
                self.reader = TimedReader(self.src_path, self.opener)
                return True
            except OSError:
                self.state = 'waiting'
                self.message = 'Drive not found - reconnect it and the rescue will continue'
                self._report()
                time.sleep(2)
        return False

    def _read(self, pos, n):
        """Returns data (bytes) on success, None for a read error. Waits out disconnects."""
        while not self.stop_ev.is_set():
            self._wait_if_paused_or_hot()
            if self.stop_ev.is_set():
                return None
            if self.reader is None or self.reader.dead:
                if not self._open_reader():
                    return None
            n = min(n, self.size - pos)
            kind, val, secs = self.reader.read(pos, n, self.opts.timeout)
            self.last_secs = secs or 0
            if kind == 'ok':
                if val is None or len(val) < n:
                    if val:            # short read near a bad area: count the tail as failed
                        return val
                    return None
                self.read_bytes_recent.append((time.time(), n))
                return val
            if kind == 'err':
                code = getattr(val, 'winerror', None) or val.errno
                if code in DISCONNECT_ERRORS:
                    self.reader.close()
                    self.reader = None
                    self.state = 'waiting'
                    self.message = 'Drive disconnected - reconnect it and the rescue will continue'
                    self._save_map(True)
                    self._report()
                    time.sleep(2)
                    continue
                self.errors += 1
                return None
            if kind in ('timeout', 'hung'):
                self.errors += 1
                self.message = 'Drive stopped responding on a read - skipped it'
                if kind == 'hung':
                    self.reader = None       # start a fresh handle next time
                return None
        return None

    def _write(self, pos, data):
        """Queue a block for the image (marked rescued once it is on disk)."""
        return self.writer.put(pos, data)

    # -- passes
    def _copy_pass(self, reverse, skipping, only=None):
        """Pass 1-3: read untried areas in blocks. `only`: restrict to these (start, length) ranges."""
        max_skip = max(self.block, min(256 * MB, self.size // 100))
        skip = self.block
        if only is not None:
            regions = []
            for oa, ol in only:
                regions += [r for r in self.map.regions_in(oa, oa + ol) if r[2] == UNTRIED]
        else:
            regions = self.map.regions({UNTRIED}, reverse)
            if not reverse and self.map.current_pos:
                regions = [r for r in regions if r[0] + r[1] > self.map.current_pos]
        blk = self.block
        clean = 0
        grow = (not reverse and skipping and only is None and self.opts.max_block > self.block)
        for a, l, _ in regions:
            if self.stop_ev.is_set():
                return
            b = a + l
            if not reverse:
                pos = max(a, self.map.current_pos) if self.map.current_pos else a
                while pos < b and not self.stop_ev.is_set():
                    if self.map.status_at(pos) != UNTRIED:
                        pos = self._next_boundary(pos)
                        continue
                    end = min(pos + blk - (pos % blk), b)  # stay block-aligned
                    n = end - pos
                    data = self._read(pos, n)
                    if only is None:
                        self.map.current_pos = pos
                    if self.stop_ev.is_set() and data is None:
                        return
                    if data:
                        if not self._write(pos, data):
                            return
                        if len(data) < n:
                            self.map.set(pos + len(data), n - len(data), NONTRIMMED)
                        slow = skipping and self.last_secs > self.opts.slow_secs
                        if slow:
                            self.message = 'Slow area - skipping ahead for now'
                            pos = end + skip
                            skip = min(skip * 2, max_skip)
                            blk, clean = self.block, 0
                        else:
                            skip = self.block
                            pos = end
                            # healthy stretch: bigger reads (fewer commands, steadier streaming);
                            # back to small blocks at the first sign of trouble
                            clean += 1
                            if grow and clean >= 16 and blk < self.opts.max_block and \
                                    self.last_secs < 0.25 * self.opts.slow_secs:
                                blk, clean = blk * 2, 0
                            if len(data) < n:
                                blk, clean = self.block, 0
                    else:
                        self.map.set(pos, n, NONTRIMMED)
                        blk, clean = self.block, 0
                        if skipping:
                            self.message = f'Read error - skipping {human_size(skip)} for now'
                            pos = end + skip
                            skip = min(skip * 2, max_skip)
                        else:
                            pos = end
                    self._save_map()
                    self._report()
            else:
                pos = b
                while pos > a and not self.stop_ev.is_set():
                    if self.map.status_at(pos - 1) != UNTRIED:
                        pos = self._prev_boundary(pos)
                        continue
                    start = max(a, pos - self.block)
                    n = pos - start
                    data = self._read(start, n)
                    self.map.current_pos = start
                    if self.stop_ev.is_set() and data is None:
                        return
                    if data and len(data) == n:
                        if not self._write(start, data):
                            return
                        skip = self.block
                        pos = start
                    else:
                        if data:
                            if not self._write(start, data):
                                return
                            self.map.set(start + len(data), n - len(data), NONTRIMMED)
                        else:
                            self.map.set(start, n, NONTRIMMED)
                        if skipping:
                            pos = start - skip
                            skip = min(skip * 2, max_skip)
                        else:
                            pos = start
                    self._save_map()
                    self._report()
        if only is None:
            self.map.current_pos = 0

    def _important_first(self):
        """Pass 0: quick scan through the imager, then the data of the deleted files it found."""
        src = CopyOnReadSource(self, self.source_label or self.src_path)
        items = []
        self.message = 'Copying partition tables, file tables and folders'
        self._report()
        try:
            sc = engine.Scanner(src, engine.ScanOptions(deleted=True, deep=False, free_only=True),
                                items.append, lambda s: None, lambda *a: None)
            self._pass0_scanner = sc
            sc.run()
        except Exception as ex:
            self.on_status(f'Quick scan during imaging failed ({ex}) - continuing with the full copy')
        if self.stop_ev.is_set():
            return
        self.writer.drain()
        meta = src.copied
        skip_conds = ('Overwritten', 'Erased (all zeros)', 'Compressed (not supported)', 'Encrypted (not supported)')
        runs = []
        for it in items:
            if it.condition in skip_conds or it.inline is not None:
                continue
            runs += [(o, l) for o, l in it.runs if o is not None and l > 0]
        runs.sort()
        # merge, reading straight through small gaps: cheaper than seeking, and needed later anyway
        merged = []
        for o, l in runs:
            if merged and o <= merged[-1][0] + merged[-1][1] + 4 * MB:
                mo, ml = merged[-1]
                merged[-1] = (mo, max(ml, o + l - mo))
            else:
                merged.append((o, l))
        total = sum(l for _, l in merged)
        self.priority_note = (f'Copied file tables ({human_size(meta)}) first; then {len(items):,} deleted '
                              f'files ({human_size(total)})')
        self.message = f'Copying the data of {len(items):,} deleted files ({human_size(total)}) before the rest'
        self._report()
        if merged:
            self._copy_pass(False, True, only=merged)
        self.writer.drain()

    def _next_boundary(self, pos):
        i = bisect.bisect_right(self.map.starts, pos)
        return self.map.starts[i] if i < len(self.map.starts) else self.size

    def _prev_boundary(self, pos):
        i = bisect.bisect_right(self.map.starts, pos - 1) - 1
        return self.map.starts[i]

    def _trim_pass(self):
        u = self.unit
        for a, l, _ in self.map.regions({NONTRIMMED}):
            if self.stop_ev.is_set():
                return
            b = a + l
            lo = a
            while lo < b and not self.stop_ev.is_set():     # from the front
                n = min(u, b - lo)
                data = self._read(lo, n)
                if data and len(data) == n:
                    if not self._write(lo, data):
                        return
                    lo += n
                else:
                    if self.stop_ev.is_set():
                        return
                    self.map.set(lo, n, BAD)
                    lo += n
                    break
                self._save_map()
                self._report()
            hi = b
            while hi > lo and not self.stop_ev.is_set():    # from the back
                n = min(u, hi - lo)
                data = self._read(hi - n, n)
                if data and len(data) == n:
                    if not self._write(hi - n, data):
                        return
                    hi -= n
                else:
                    if self.stop_ev.is_set():
                        return
                    self.map.set(hi - n, n, BAD)
                    hi -= n
                    break
                self._save_map()
                self._report()
            if hi > lo:
                self.map.set(lo, hi - lo, NONSCRAPED)
            self._save_map()

    def _unit_pass(self, statuses):
        u = self.unit
        for a, l, _ in self.map.regions(statuses):
            pos = a
            while pos < a + l and not self.stop_ev.is_set():
                n = min(u, a + l - pos)
                data = self._read(pos, n)
                self.map.current_pos = pos
                if data and len(data) == n:
                    if not self._write(pos, data):
                        return
                elif not self.stop_ev.is_set():
                    self.map.set(pos, n, BAD)
                pos += n
                self._save_map()
                self._report()

    def run(self):
        self.started = time.time()
        keep_awake(True)
        try:
            new = not os.path.exists(self.image_path)
            self.img = open(self.image_path, 'r+b' if not new else 'w+b')
            if new:
                _set_sparse(self.img)
                self.img.truncate(self.size)
            self.writer = ImageWriter(self)
            if not self._open_reader():
                return self._finish('Stopped')
            if self.resumed:
                t = self.map.totals()
                self.on_status(f'Resuming: {human_size(t[FINISHED])} already rescued')
            if self.opts.important_first and self.map.current_pass <= 1 and not self.stop_ev.is_set():
                self.on_status(PASS_NAMES[0])
                self._important_first()
                self._save_map(True)
            plan = [(1, lambda: self._copy_pass(False, True)),
                    (2, lambda: self._copy_pass(True, True) if self.opts.reverse else None),
                    (3, lambda: self._copy_pass(False, False)),
                    (4, self._trim_pass),
                    (5, lambda: self._unit_pass({NONSCRAPED})),
                    (6, self._retry_pass)]
            for num, fn in plan:
                if num < self.map.current_pass:
                    continue
                if self.stop_ev.is_set():
                    break
                self.map.current_pass = num
                self.message = ''
                self.on_status(PASS_NAMES[num])
                fn()
                self.writer.drain()
                if self.stop_ev.is_set():
                    break
                self.map.current_pos = 0
                self._save_map(True)
            if not self.stop_ev.is_set():
                self.map.current_pass = 7
                self._save_map(True)
                return self._finish('Finished')
            return self._finish('Stopped')
        finally:
            keep_awake(False)
            if self.writer:
                self.writer.close()
                self._save_map(True)
            try:
                if self.img:
                    self.img.close()
            except Exception:
                pass
            if self.reader:
                self.reader.close()

    def _retry_pass(self):
        for attempt in range(self.opts.retries):
            self.writer.drain()
            if not self.map.regions({BAD}) or self.stop_ev.is_set():
                return
            self.message = f'Retry {attempt + 1} of {self.opts.retries}'
            self._unit_pass({BAD})

    def _finish(self, how):
        self.state = how.lower()
        if self.writer:
            self.writer.drain()
        self._save_map(True)
        t = self.map.totals()
        pct = 100.0 * t[FINISHED] / max(self.size, 1)
        self.message = (f'{how}. Rescued {human_size(t[FINISHED])} of {human_size(self.size)} ({pct:.3f}%), '
                        f'{human_size(t[BAD])} in bad sectors')
        if t[UNTRIED] + t[NONTRIMMED] + t[NONSCRAPED]:
            self.message += f', {human_size(t[UNTRIED] + t[NONTRIMMED] + t[NONSCRAPED])} not finished'
        if self.priority_note:
            self.message += f'. {self.priority_note}'
        self._report(True)
        return self.message
