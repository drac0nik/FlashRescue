"""
Runs a scan in a helper process.

Python runs only one thread at a time, so a busy scanner thread makes the window stutter.
In its own process the scanner gets a CPU core to itself and the window stays smooth. The
helper opens its own read-only handle on the drive and sends results back in batches. Saving,
previews and recovering stay in the main app, which has its own handle.
"""

import multiprocessing as mp
import queue
import threading
import time
import traceback

ID_STRIDE = 100_000_000


class StartFailed(RuntimeError):
    """The helper process could not be started (the caller falls back to a thread)."""


def _child(spec, opts, out_q, cancel_ev, id_base):
    import engine
    engine.FoundFile._next_id = id_base
    try:
        src = open_spec(spec)
    except Exception as ex:
        out_q.put(('error', f'{type(ex).__name__}: {ex}', traceback.format_exc()))
        return
    buf = []
    lock = threading.Lock()
    state = {'last': time.time(), 'prog': 0.0}

    def flush(force=False):
        with lock:
            if buf and (force or len(buf) >= 500 or time.time() - state['last'] > 0.05):
                out_q.put(('items', list(buf)))
                buf.clear()
                state['last'] = time.time()

    def on_item(it):
        with lock:
            buf.append(it)
        flush()

    def on_status(s):
        flush(True)
        out_q.put(('status', s))

    def on_progress(f, a, b):
        now = time.time()
        if f is None or now - state['prog'] >= 0.1 or (b and a >= b):
            state['prog'] = now
            flush()
            out_q.put(('progress', f, a, b))

    def on_remove(it):
        flush(True)
        out_q.put(('remove', it.id))

    sc = engine.Scanner(src, engine.ScanOptions(*opts), on_item, on_status, on_progress, on_remove=on_remove)

    def watch():
        cancel_ev.wait()
        sc.cancel.set()
    threading.Thread(target=watch, daemon=True).start()
    try:
        summary = sc.run()
        flush(True)
        out_q.put(('done', summary, list(zip(src.bad_starts, src.bad_ends)), src.bad_reads,
                   engine.FoundFile._next_id))
    except Exception as ex:
        flush(True)
        out_q.put(('error', f'{type(ex).__name__}: {ex}', traceback.format_exc()))
    finally:
        src.close()


def portable_spec(src):
    """A description the helper process can open the same source from, or None (then the scan
    stays on a thread). Plain drives and images go by path; an unlocked encrypted volume goes as
    its (already derived) volume key plus where its data sits - never the password."""
    import images
    import encrypted
    if isinstance(src, encrypted.DecryptedSource):
        parent = portable_spec(src.parent)
        if not parent:
            return None
        c = src.cipher
        return {'unlock': {'parent': parent, 'data_offset': src.data_offset, 'size': src.size,
                           'mode': ('xts-' if c.kind == 'xts' else 'cbc-' if c.kind == 'cbc' else 'ecb') +
                           ('' if c.kind == 'ecb' else c.iv), 'key': c.key.hex(), 'unit': c.unit,
                           'iv_shift': c.iv_shift, 'unit_base': src.unit_base, 'label': src.label,
                           'kind': src.kind},
                'path': src.path, 'label': src.label}
    if type(src).__name__ in ('VirtualRaid', 'LvSource', 'SubSource'):
        return None
    return {'path': src.path, 'label': src.label, 'map': src.map_path, 'image': isinstance(src, images._ImageBase)}


def open_spec(spec):
    import engine
    import images
    if 'unlock' in spec:
        import encrypted
        u = spec['unlock']
        parent = open_spec(u['parent'])
        cip = encrypted.SectorCipher('aes', u['mode'], bytes.fromhex(u['key']), unit=u['unit'], iv_shift=u['iv_shift'])
        return encrypted.DecryptedSource(parent, u['data_offset'], u['size'], cip, u['label'], u['unit_base'],
                                         u['kind'])
    src = (images.open_image(spec['path'], spec.get('label')) if spec.get('image')
           else engine.RawSource(spec['path'], spec.get('label')))
    if spec.get('map'):
        src.load_rescue_map(spec['map'])
    return src


class ProcessScanner:
    """Same interface as engine.Scanner (run(), cancel, callbacks), but the work happens in a
    helper process. `src` is the app's own RawSource: unreadable areas the helper found are
    copied into it at the end, so recovered files are flagged the same way."""

    def __init__(self, src, opts, on_item, on_status, on_progress, on_remove=None):
        import engine
        self.engine = engine
        self.src = src
        self.opts = (opts.deleted, opts.deep, opts.free_only, opts.include_existing)
        self.on_item = on_item
        self.on_status = on_status
        self.on_progress = on_progress
        self.on_remove = on_remove
        self.ctx = mp.get_context('spawn')
        self.cancel = self.ctx.Event()
        self.by_id = {}
        self.summary = ''

    def run(self):
        engine = self.engine
        spec = portable_spec(self.src)
        if spec is None:
            raise StartFailed('this source can only be scanned inside the app')
        id_base = (engine.FoundFile._next_id // ID_STRIDE + 1) * ID_STRIDE
        q = self.ctx.Queue()
        p = self.ctx.Process(target=_child, args=(spec, self.opts, q, self.cancel, id_base), daemon=True,
                             name='FlashRescue scanner')
        try:
            p.start()
        except Exception as ex:
            raise StartFailed(str(ex))
        got_any = False
        try:
            while True:
                try:
                    m = q.get(timeout=0.5)
                except queue.Empty:
                    if not p.is_alive():
                        if not got_any:
                            raise StartFailed(f'helper exited with code {p.exitcode}')
                        raise RuntimeError(f'the scanner process stopped unexpectedly (code {p.exitcode})')
                    continue
                got_any = True
                kind = m[0]
                if kind == 'items':
                    for it in m[1]:
                        self.by_id[it.id] = it
                        self.on_item(it)
                elif kind == 'progress':
                    self.on_progress(*m[1:])
                elif kind == 'status':
                    self.on_status(m[1])
                elif kind == 'remove':
                    it = self.by_id.get(m[1])
                    if it is not None and self.on_remove:
                        self.on_remove(it)
                elif kind == 'done':
                    _, summary, bad, bad_reads, next_id = m
                    for a, b in bad:
                        self.src.add_bad(a, b - a)
                    self.src.bad_reads += bad_reads
                    with engine.FoundFile._id_lock:
                        engine.FoundFile._next_id = max(engine.FoundFile._next_id, next_id)
                    self.summary = summary
                    return summary
                elif kind == 'error':
                    raise RuntimeError(m[1] + '\n' + m[2])
        finally:
            p.join(timeout=10)
            if p.is_alive():
                p.terminate()
