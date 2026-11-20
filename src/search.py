"""
Search inside the data: find a word or phrase anywhere on the drive (or only in its free space,
or only inside the files found), as plain text (ASCII / UTF-8) and as UTF-16 (how Windows and
Office store a lot of text). Each hit is mapped to the found file it sits in, when there is one.

Text files have no signature, so a deep scan can't find them; but a search hit inside one lets
you save the whole block of text around it - which brings back lost notes, CSVs, logs, code...
"""
import bisect
import re

MB = 1024 * 1024
CHUNK = 8 * MB


def build_pattern(terms, case=False, utf16=True):
    """One compiled regex for all terms, as UTF-8 and (optionally) UTF-16LE."""
    alts = []
    kinds = []
    for t in terms:
        t = t.strip()
        if not t:
            continue
        alts.append(re.escape(t.encode('utf-8')))
        kinds.append((t, 'text'))
        if utf16:
            alts.append(re.escape(t.encode('utf-16-le')))
            kinds.append((t, 'UTF-16'))
    if not alts:
        raise ValueError('nothing to search for')
    pat = re.compile(b'|'.join(b'(' + a + b')' for a in alts), 0 if case else re.IGNORECASE)
    return pat, kinds


TEXT_RE = re.compile(rb'(?:[\t\n\r\x20-\x7e]|[\xc2-\xdf][\x80-\xbf]|[\xe0-\xef][\x80-\xbf]{2}|'
                     rb'[\xf0-\xf4][\x80-\xbf]{3})+')
# UTF-16: Latin, Latin-1 and the common European scripts (Greek, Cyrillic) plus punctuation
TEXT16_RE = re.compile(rb'(?:[\t\n\r\x20-\x7e\xa0-\xff]\x00|[\x00-\xff][\x01-\x05]|[\x10-\x3f]\x20)+')


def _run_around(data, i, enc):
    """(start, end) of the run of real text containing position i."""
    rx = TEXT16_RE if enc == 'UTF-16' else TEXT_RE
    w = min(i, 4096)
    if enc == 'UTF-16':
        w -= w % 2                                # keep the same byte parity as the hit
    for m in rx.finditer(data, i - w, min(len(data), i + 8192)):
        if m.start() <= i < m.end():
            return m.start(), m.end()
    return i, i


def context(data, i, n, enc):
    """Readable text around a hit (cut where the real text stops)."""
    a, b = _run_around(data, i, enc)
    if enc == 'UTF-16':
        a = max(a, (i - 120) & ~1)
        piece = data[a:min(b, i + n + 120)].decode('utf-16-le', 'replace')
    else:
        a = max(a, i - 60)
        piece = data[a:min(b, i + n + 60)].decode('utf-8', 'replace')
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f\ufffd]+', ' ', piece).replace('\n', ' ').strip()


class FileIndex:
    """Which found file covers a given byte offset."""

    def __init__(self, items):
        spans = []
        for it in items:
            for o, n in it.runs:
                if o is not None and n > 0:
                    spans.append((o, o + n, it))
        spans.sort(key=lambda s: s[0])
        self.starts = [s[0] for s in spans]
        self.spans = spans

    def at(self, off):
        k = bisect.bisect_right(self.starts, off) - 1
        for _ in range(64):                       # spans can nest/overlap a little; look a few back
            if k < 0:
                break
            a, b, it = self.spans[k]
            if a <= off < b:
                return it
            k -= 1
        return None


def regions_for(src, scope, items=None):
    """[(offset, length)] to search: 'all', 'free' (unused space) or 'files' (found files)."""
    if scope == 'files' and items is not None:
        out = []
        for it in items:
            out += [(o, n) for o, n in it.runs if o is not None and n > 0]
        out.sort()
        merged = []
        for o, n in out:
            if merged and o <= merged[-1][0] + merged[-1][1]:
                mo, mn = merged[-1]
                merged[-1] = (mo, max(mn, o + n - mo))
            else:
                merged.append((o, n))
        return merged
    if scope == 'free':
        import engine
        out = []
        try:
            for p in engine.find_partitions(src):
                if p.volume is not None and hasattr(p.volume, 'free_runs'):
                    rem = p.remap
                    for o, n in p.volume.free_runs():
                        out += rem([(o, n)]) if rem else [(o, n)]
        except Exception:
            out = []
        if out:
            return sorted((o, n) for o, n in out if o is not None)
    return [(0, src.size)]


def search(src, terms, scope='all', items=None, case=False, utf16=True, progress=lambda f: None,
           cancel=None, max_hits=5000):
    """Yields (offset, term, encoding, context) for every hit."""
    pat, kinds = build_pattern(terms, case, utf16)
    longest = max(len(t.encode('utf-16-le')) for t, _ in kinds) + 2
    regions = regions_for(src, scope, items)
    total = sum(n for _, n in regions) or 1
    done = 0
    hits = 0
    for o, n in regions:
        pos, end = o, o + n
        while pos < end:
            if cancel is not None and cancel.is_set():
                return
            take = min(CHUNK, end - pos)
            data = src.read(pos, take + longest)
            if not data:
                break
            for m in pat.finditer(data, 0, min(len(data), take + longest)):
                if m.start() >= take:              # belongs to the next chunk
                    break
                term, enc = kinds[m.lastindex - 1]
                yield pos + m.start(), term, enc, context(data, m.start(), m.end() - m.start(), enc)
                hits += 1
                if hits >= max_hits:
                    return
            pos += take
            done += take
            progress(done / total)


def text_block(src, off, enc, limit=16 * MB):
    """(start, bytes) of the whole run of text around a hit - for saving a lost text file."""
    rx = TEXT16_RE if enc == 'UTF-16' else TEXT_RE
    back = min(off, limit // 2)
    while True:
        if enc == 'UTF-16':
            back -= back % 2                      # same byte parity as the hit
        base = off - back
        data = src.read(base, back + limit // 2)
        start = end = None
        for m in rx.finditer(data):
            if m.start() <= off - base < m.end():
                start, end = m.start(), m.end()
                break
        if start is None:
            return off, b''
        if start == 0 and base > 0 and back < limit:      # the text goes back further: widen
            back = min(off, back * 2 + 4096)
            continue
        return base + start, data[start:end][:limit]
