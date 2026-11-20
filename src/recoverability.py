"""Recoverability check: a quick look at whether deleted files are likely to still be there,
before spending hours on a scan.

Two kinds of evidence:
  1. What the drive is (Windows only): SSD or hard disk, whether TRIM is on, how it's connected.
     On an SSD with TRIM, Windows tells the drive which blocks were freed and the drive usually
     wipes them within seconds to minutes - deleted files are then gone for good.
  2. What the free space holds: a few hundred small samples spread over the unused part of each
     filesystem. Free space that still holds old data is where deleted files live; free space that
     reads back as zeros has been wiped (TRIM, a full format, or a wiping tool).
"""
import os
import struct
import time

import engine
from engine import MB, human_size

BUS = {1: 'SCSI', 2: 'ATAPI', 3: 'ATA', 4: 'FireWire', 5: 'SSA', 6: 'Fibre Channel', 7: 'USB', 8: 'RAID',
       9: 'iSCSI', 10: 'SAS', 11: 'SATA', 12: 'SD card', 13: 'MMC card', 14: 'virtual', 15: 'virtual',
       16: 'storage spaces', 17: 'NVMe', 18: 'SCM', 19: 'UFS'}

IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400


def _query(f, prop, outsize=1024):
    inbuf = struct.pack('<II4x', prop, 0)
    return engine.win_ioctl(f, IOCTL_STORAGE_QUERY_PROPERTY, inbuf, outsize)


def device_info(disk_numbers):
    """{'ssd': True/False/None, 'trim': True/False/None, 'bus': 'USB'|..., 'removable': bool,
    'model': str, 'os_trim_off': bool} for the first physical disk that answers (Windows only)."""
    info = {'ssd': None, 'trim': None, 'bus': None, 'removable': None, 'model': '', 'os_trim_off': None}
    if os.name != 'nt':
        return info
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\FileSystem') as k:
            info['os_trim_off'] = bool(winreg.QueryValueEx(k, 'DisableDeleteNotification')[0])
    except OSError:
        pass
    for n in disk_numbers:
        try:
            f = open(f'\\\\.\\PhysicalDrive{n}', 'rb', buffering=0)
        except OSError:
            continue
        try:
            d = _query(f, 0)                         # STORAGE_DEVICE_DESCRIPTOR
            if d and len(d) >= 32:
                info['removable'] = bool(d[10])
                info['bus'] = BUS.get(struct.unpack('<I', d[28:32])[0], 'other')
                parts = []
                vo, po = struct.unpack('<II', d[12:20])
                for o in (vo, po):
                    if o and o < len(d):
                        parts.append(d[o:d.find(b'\x00', o)].decode('ascii', 'replace').strip())
                info['model'] = ' '.join(p for p in parts if p)
            s = _query(f, 7, 64)                     # DEVICE_SEEK_PENALTY_DESCRIPTOR
            if s and len(s) >= 9:
                info['ssd'] = not bool(s[8])
            t = _query(f, 8, 64)                     # DEVICE_TRIM_DESCRIPTOR
            if t and len(t) >= 9:
                info['trim'] = bool(t[8])
            return info
        except OSError:
            continue
        finally:
            f.close()
    return info


def _classify(data):
    if not data:
        return 'unreadable'
    if data.count(0) == len(data):
        return 'zero'
    if data.count(0xFF) == len(data):
        return 'ff'
    return 'data'


def sample_free_space(src, progress=lambda f: None, cancel=None, samples=480, size=64 * 1024):
    """Sample the unused space of every filesystem found (or the whole source when none is).
    Returns {'where': text, 'counts': {zero, ff, data, unreadable}, 'free': bytes, 'volumes': [...]}."""
    regions = []           # (reader, offset, length)
    vols = []
    try:
        parts = engine.find_partitions(src)
    except Exception:
        parts = []
    for p in parts:
        v = p.volume
        if v is None or not hasattr(v, 'free_runs'):
            continue
        try:
            runs = v.free_runs()
        except Exception:
            continue
        reader = getattr(v, 'src', src)
        free = sum(n for _, n in runs)
        vols.append((p.kind, p.size, free))
        regions += [(reader, o, n) for o, n in runs if n >= 4096]
    where = 'free space'
    if not regions:
        where = 'whole drive (no filesystem found, so free space is unknown)'
        regions = [(src, 0, src.size)]
    total = sum(n for _, _, n in regions)
    counts = {'zero': 0, 'ff': 0, 'data': 0, 'unreadable': 0}
    if total <= 0:
        return {'where': where, 'counts': counts, 'free': 0, 'volumes': vols}
    step = total / samples
    k = 0
    acc = 0
    ri = 0
    for k in range(samples):
        if cancel is not None and cancel.is_set():
            break
        target = int(step * (k + 0.5))
        while ri < len(regions) and acc + regions[ri][2] <= target:
            acc += regions[ri][2]
            ri += 1
        if ri >= len(regions):
            break
        reader, o, n = regions[ri]
        off = o + (target - acc)
        off -= off % 4096
        take = min(size, o + n - off)
        try:
            d = reader.read(off, take)
        except Exception:
            d = b''
        counts[_classify(d)] += 1
        progress((k + 1) / samples)
    return {'where': where, 'counts': counts, 'free': total, 'volumes': vols}


def sample_deleted(src, cancel=None, limit=400, seconds=25.0, status=lambda t: None):
    """Read the file tables and look at up to `limit` deleted files: is their data still there?
    Returns {'n', 'intact', 'zeros', 'overwritten', 'unknown'} (counts)."""
    import threading
    items = []
    stop = threading.Event()
    sc = engine.Scanner(src, engine.ScanOptions(deleted=True, deep=False, free_only=True, include_existing=False),
                        lambda it: (items.append(it), len(items) >= limit and sc.cancel.set()),
                        status, lambda *a: None)
    t0 = time.time()

    def watchdog():
        while not stop.wait(0.2):
            if (cancel is not None and cancel.is_set()) or time.time() - t0 > seconds:
                sc.cancel.set()
                return
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        sc.run()
    except Exception:
        pass
    finally:
        stop.set()
    out = {'n': 0, 'intact': 0, 'zeros': 0, 'overwritten': 0, 'unknown': 0}
    for it in items:
        if it.method not in ('Deleted', 'Recycle Bin') or it.inline is not None or it.start is None or not it.size:
            continue
        out['n'] += 1
        try:
            head = src.read(it.start, 4096)
        except Exception:
            head = b''
        ok = engine.head_matches_ext(it.ext, head[:16])
        if head and head.count(0) == len(head):
            out['zeros'] += 1
        elif ok is True:
            out['intact'] += 1
        elif ok is False:
            out['overwritten'] += 1
        else:
            out['unknown'] += 1                      # a type without a known signature
    return out


def verdict(dev, s, dl=None):
    """(level, headline, explanation) - level is 'good', 'mixed' or 'poor'.
    s = free-space sample, dl = deleted-file sample (the stronger evidence, when there is one)."""
    c = s['counts']
    n = sum(c.values()) or 1
    blank = (c['zero'] + c['ff']) / n
    data = c['data'] / n
    old_data = int(data * s['free'])
    ssd_trim = bool(dev.get('ssd') and dev.get('trim') and not dev.get('os_trim_off'))
    lines = []
    if dev.get('bus') or dev.get('ssd') is not None:
        kind = 'SSD' if dev.get('ssd') else 'hard disk' if dev.get('ssd') is False else 'drive'
        lines.append(f"Drive: {dev.get('model') or kind} ({kind}"
                     f"{', ' + dev['bus'] if dev.get('bus') else ''}"
                     f"{', TRIM on' if dev.get('trim') else ', TRIM off' if dev.get('trim') is False else ''})")
    judged = 0
    if dl and dl['n']:
        judged = dl['intact'] + dl['zeros'] + dl['overwritten'] + dl['unknown']
        lines.append(f"Deleted files checked: {dl['n']} - data still there for {dl['intact'] + dl['unknown']}"
                     + (f" ({dl['unknown']} of them a type whose contents can't be confirmed)" if dl['unknown'] else '')
                     + f", wiped (zeros) for {dl['zeros']}, overwritten by other data for {dl['overwritten']}.")
    lines.append(f"Free space sampled ({s['where']}): about {human_size(old_data)} still holds old data "
                 f"({data:.0%}), {blank:.0%} reads blank"
                 + (f", {c['unreadable']} spot(s) unreadable" if c['unreadable'] else '') + '.')
    body = '\n'.join(lines)
    trim_note = (' On this SSD, TRIM can keep wiping freed space while the drive is in use - scan it now or image '
                 'it first, and use it as little as possible.') if ssd_trim else ''
    if judged >= 5:
        intact, zeros = (dl['intact'] + dl['unknown']) / judged, dl['zeros'] / judged
        if zeros >= 0.75:
            if ssd_trim:
                return ('poor', 'Deleted files have very probably been wiped by the SSD (TRIM).',
                        body + '\n\nOn an SSD with TRIM, Windows tells the drive which blocks were freed and the '
                        'drive erases them, usually within minutes. A scan will still list deleted file NAMES, but '
                        'their contents read as zeros. Realistic hope: the Recycle Bin, shadow copies, backups and '
                        'cloud copies.')
            return ('poor', 'Most deleted files have been wiped.',
                    body + '\n\nThe data of most deleted files reads as zeros: a wiping tool, a full format or '
                    'TRIM erased it. Some files may still be found; expect few.')
        if intact >= 0.6:
            return ('good', 'Good chances: most deleted files still have their data.',
                    body + '\n\nA quick scan should bring most of them back with their names.' + trim_note)
        return ('mixed', 'Mixed: some deleted files are intact, others are wiped or overwritten.',
                body + '\n\nWorth scanning - recent deletions are usually in better shape than old ones.' + trim_note)
    # nothing deleted to look at (formatted, damaged, or nothing deleted): judge by the free space
    if data < 0.02 and old_data < 32 * MB:
        why = ('the SSD has wiped it (TRIM)' if ssd_trim else
               'the drive is new or barely used, had a full format (Windows writes zeros), or was wiped')
        return ('poor', 'Little to find: the free space is blank.',
                body + f'\n\nNo deleted entries were found in the file tables, and the unused space reads as '
                f'zeros - {why}. A deep scan is unlikely to find much.')
    msg = ('No deleted entries were found in the file tables (after a format or with a damaged filesystem that is '
           'normal), but the free space still holds old data - a deep scan is the way to go.')
    if dev.get('bus') in ('USB', 'SD card', 'MMC card') or dev.get('removable'):
        msg += ' Memory cards and USB sticks are normally not TRIMmed, so old files stay until overwritten.'
    return ('good' if data >= 0.2 else 'mixed', 'Old data is still there - a deep scan should find files.',
            body + '\n\n' + msg + trim_note)
