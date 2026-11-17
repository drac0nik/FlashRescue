"""
Drive health (SMART) for FlashRescue - Windows only.

Tries, in order:
  1. NVMe health log (IOCTL_STORAGE_QUERY_PROPERTY, protocol-specific)
  2. ATA SMART via SMART_RCV_DRIVE_DATA (SATA drives on most controllers)
  3. ATA SMART via IOCTL_ATA_PASS_THROUGH (works through some USB adapters)
  4. Windows' own yes/no failure prediction (IOCTL_STORAGE_PREDICT_FAILURE)

Many USB adapters and almost all flash drives/memory cards don't pass SMART through;
that's reported as "not available", which is not a sign of a problem.
"""

import os
import struct

SMART_RCV_DRIVE_DATA = 0x0007C088
IOCTL_ATA_PASS_THROUGH = 0x0004D02C
IOCTL_STORAGE_PREDICT_FAILURE = 0x002D1100
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400

ATTR_NAMES = {
    1: 'Read error rate', 3: 'Spin-up time', 4: 'Start/stop count', 5: 'Reallocated sectors',
    7: 'Seek error rate', 9: 'Power-on hours', 10: 'Spin retry count', 12: 'Power cycles',
    177: 'Wear leveling count', 183: 'Runtime bad blocks', 184: 'End-to-end errors',
    187: 'Reported uncorrectable errors', 188: 'Command timeouts', 190: 'Airflow temperature',
    194: 'Temperature', 196: 'Reallocation events', 197: 'Pending (unreadable) sectors',
    198: 'Offline uncorrectable sectors', 199: 'Interface CRC errors (cable)', 231: 'SSD life left',
    233: 'Media wearout', 241: 'Total data written', 242: 'Total data read',
}


def _ioctl(f, code, inbuf, outsize):
    import engine
    return engine.win_ioctl(f, code, inbuf, outsize)


def _open_disk(n):
    return open(f'\\\\.\\PhysicalDrive{n}', 'rb', buffering=0)


# ---------------------------------------------------------------------------- ATA

def _parse_ata_tables(attr_data, thr_data):
    thresholds = {}
    if thr_data and len(thr_data) >= 362:
        for k in range(30):
            e = thr_data[2 + 12 * k: 14 + 12 * k]
            if e[0]:
                thresholds[e[0]] = e[1]
    attrs = []
    for k in range(30):
        e = attr_data[2 + 12 * k: 14 + 12 * k]
        if len(e) < 12 or e[0] == 0:
            continue
        aid, flags, cur, worst = e[0], struct.unpack_from('<H', e, 1)[0], e[3], e[4]
        raw = int.from_bytes(e[5:11], 'little')
        attrs.append({'id': aid, 'name': ATTR_NAMES.get(aid, f'Attribute {aid}'), 'current': cur,
                      'worst': worst, 'threshold': thresholds.get(aid, 0), 'raw': raw,
                      'prefail': bool(flags & 1)})
    return attrs


def _ata_smart_rcv(f, feature):
    # SENDCMDINPARAMS (packed): cBufferSize, IDEREGS[8], bDriveNumber, reserved[3], dwReserved[4], bBuffer[1]
    regs = bytes([feature, 1, 1, 0x4F, 0xC2, 0xA0, 0xB0, 0])
    inbuf = struct.pack('<I', 512) + regs + bytes([0, 0, 0, 0]) + bytes(16) + b'\x00'
    out = _ioctl(f, SMART_RCV_DRIVE_DATA, inbuf, 16 + 512)
    if out and len(out) >= 16 + 362:
        return out[16:16 + 512]
    return None


def _ata_passthrough(f, feature):
    # ATA_PASS_THROUGH_EX (64-bit layout, 48 bytes) followed by a 512-byte data buffer
    hdr_size = 48 if struct.calcsize('P') == 8 else 40
    data_off = hdr_size
    flags = 0x01 | 0x02                       # DRDY required | data in
    task = bytes([feature, 1, 1, 0x4F, 0xC2, 0xA0, 0xB0, 0])
    if hdr_size == 48:
        hdr = struct.pack('<HHBBBBIII4xQ8s8s', hdr_size, flags, 0, 0, 0, 0, 512, 5, 0, data_off, bytes(8), task)
    else:
        hdr = struct.pack('<HHBBBBIIII8s8s', hdr_size, flags, 0, 0, 0, 0, 512, 5, 0, data_off, bytes(8), task)
    buf = hdr + bytes(512)
    out = _ioctl(f, IOCTL_ATA_PASS_THROUGH, buf, len(buf))
    if out and len(out) >= data_off + 362:
        return out[data_off:data_off + 512]
    return None


def _read_ata(f):
    for getter in (_ata_smart_rcv, _ata_passthrough):
        try:
            a = getter(f, 0xD0)
        except Exception:
            a = None
        if a and any(a[2 + 12 * k] for k in range(30)):
            try:
                t = getter(f, 0xD1)
            except Exception:
                t = None
            return _parse_ata_tables(a, t), ('SMART' if getter is _ata_smart_rcv else 'SMART via pass-through')
    return None, None


# ---------------------------------------------------------------------------- NVMe

def _read_nvme(f):
    # STORAGE_PROPERTY_QUERY{PropertyId=50 StorageDeviceProtocolSpecificProperty, QueryType=0}
    # + STORAGE_PROTOCOL_SPECIFIC_DATA{ProtocolType=3 NVMe, DataType=2 log page, RequestValue=2 health log,
    #   SubValue=0, DataOffset=40, DataLength=512, ...}
    spec = struct.pack('<10I', 3, 2, 2, 0, 40, 512, 0, 0, 0, 0)
    inbuf = struct.pack('<II', 50, 0) + spec + bytes(512)
    out = _ioctl(f, IOCTL_STORAGE_QUERY_PROPERTY, inbuf, len(inbuf))
    if not out or len(out) < 48 + 192:
        return None
    off = struct.unpack_from('<I', out, 8 + 16)[0]     # ProtocolDataOffset, relative to the spec block
    d = out[8 + off: 8 + off + 512]
    if len(d) < 192:
        return None
    u128 = lambda o: int.from_bytes(d[o:o + 16], 'little')
    temp_k = struct.unpack_from('<H', d, 1)[0]
    return {
        'critical_warning': d[0], 'temp_c': temp_k - 273 if temp_k else None,
        'spare': d[3], 'spare_threshold': d[4], 'used_pct': d[5],
        'data_read': u128(32) * 512000, 'data_written': u128(48) * 512000,
        'power_cycles': u128(112), 'power_on_hours': u128(128), 'unsafe_shutdowns': u128(144),
        'media_errors': u128(160), 'error_log_entries': u128(176),
    }


def _predict_failure(f):
    out = _ioctl(f, IOCTL_STORAGE_PREDICT_FAILURE, None, 4 + 512)
    if out and len(out) >= 4:
        return bool(struct.unpack_from('<I', out, 0)[0])
    return None


# ---------------------------------------------------------------------------- verdict

def check_disk(n):
    """Returns {available, verdict ('good'/'warning'/'failing'/'unknown'), summary, lines, temp_c, source}."""
    rep = {'available': False, 'verdict': 'unknown', 'summary': '', 'lines': [], 'temp_c': None, 'source': ''}
    if os.name != 'nt':
        rep['summary'] = 'Health check is only available on Windows.'
        return rep
    try:
        f = _open_disk(n)
    except OSError as ex:
        rep['summary'] = f'Could not open the disk ({ex}). Run FlashRescue as administrator.'
        return rep
    with f:
        problems, warnings, lines = [], [], []
        nv = None
        try:
            nv = _read_nvme(f)
        except Exception:
            nv = None
        if nv:
            rep['available'], rep['source'] = True, 'NVMe health log'
            rep['temp_c'] = nv['temp_c']
            cw = nv['critical_warning']
            if cw & 0x01:
                problems.append('spare blocks have run low')
            if cw & 0x02:
                warnings.append('temperature warning')
            if cw & 0x04:
                problems.append('the drive reports its reliability is degraded')
            if cw & 0x08:
                problems.append('the drive has switched itself to read-only')
            if nv['spare'] < nv['spare_threshold']:
                problems.append(f"available spare {nv['spare']}% is below the {nv['spare_threshold']}% threshold")
            if nv['used_pct'] >= 100:
                problems.append(f"rated life used up ({nv['used_pct']}%)")
            elif nv['used_pct'] >= 90:
                warnings.append(f"{nv['used_pct']}% of rated life used")
            if nv['media_errors']:
                warnings.append(f"{nv['media_errors']:,} media errors recorded")
            lines += [f"Temperature: {_fmt_temp(nv['temp_c'])}",
                      f"Life used: {nv['used_pct']}%   Spare: {nv['spare']}%",
                      f"Power-on hours: {nv['power_on_hours']:,}   Power cycles: {nv['power_cycles']:,}",
                      f"Unsafe shutdowns: {nv['unsafe_shutdowns']:,}   Media errors: {nv['media_errors']:,}"]
        else:
            attrs, src = _read_ata(f)
            if attrs:
                rep['available'], rep['source'] = True, src
                by = {a['id']: a for a in attrs}
                for a in attrs:
                    if a['threshold'] and a['current'] <= a['threshold'] and a['prefail']:
                        problems.append(f"{a['name']} is below the failure threshold")
                pend = by.get(197, {}).get('raw', 0) & 0xFFFFFFFF
                unc = by.get(198, {}).get('raw', 0) & 0xFFFFFFFF
                realloc = by.get(5, {}).get('raw', 0) & 0xFFFFFFFF
                rep_unc = by.get(187, {}).get('raw', 0) & 0xFFFFFFFF
                crc = by.get(199, {}).get('raw', 0) & 0xFFFFFFFF
                if pend:
                    problems.append(f'{pend:,} sectors are currently unreadable (pending)')
                if unc:
                    problems.append(f'{unc:,} sectors could not be read in self-tests')
                if realloc > 100:
                    problems.append(f'{realloc:,} sectors have been replaced (reallocated)')
                elif realloc:
                    warnings.append(f'{realloc:,} sectors have been replaced (reallocated)')
                if rep_unc:
                    warnings.append(f'{rep_unc:,} uncorrectable read errors reported')
                if crc:
                    warnings.append(f'{crc:,} cable/connection errors - try another cable or port')
                t = by.get(194) or by.get(190)
                if t:
                    rep['temp_c'] = t['raw'] & 0xFF
                life = by.get(231) or by.get(233)
                if life and life['current'] <= 10:
                    problems.append(f"SSD life nearly used up ({life['current']}% left)")
                for a in attrs:
                    raw = a['raw']
                    if a['id'] in (194, 190):
                        val = _fmt_temp(raw & 0xFF)
                    elif a['id'] == 9:
                        val = f'{raw & 0xFFFFFFFF:,} h'
                    else:
                        val = f'{raw:,}'
                    lines.append(f"{a['id']:>3}  {a['name']:<32} {val:>14}   (value {a['current']}, "
                                 f"threshold {a['threshold']})")
        try:
            pf = _predict_failure(f)
        except Exception:
            pf = None
        if pf:
            problems.insert(0, 'Windows reports that this drive predicts its own failure')
            rep['available'] = True
            rep['source'] = rep['source'] or 'Windows failure prediction'
        elif pf is False and not rep['available']:
            rep['available'] = True
            rep['source'] = 'Windows failure prediction only'
            lines.append('Detailed SMART data is not available, but the drive does not predict a failure.')

        if rep['temp_c'] is not None and rep['temp_c'] >= 55:
            warnings.append(f"running hot ({_fmt_temp(rep['temp_c'])})")

        if not rep['available']:
            rep['summary'] = ('Health data is not available for this drive. That is normal for USB sticks, memory '
                              'cards and many USB adapters, and is not a sign of a problem.')
        elif problems:
            rep['verdict'] = 'failing'
            rep['summary'] = ('This drive is failing: ' + '; '.join(problems) + '. Make a rescue image first and '
                              'recover from the image, so the drive is read as little as possible.')
        elif warnings:
            rep['verdict'] = 'warning'
            rep['summary'] = 'Some warning signs: ' + '; '.join(warnings) + '. A rescue image is the safer choice.'
        else:
            rep['verdict'] = 'good'
            rep['summary'] = 'No problems reported by the drive.'
        rep['lines'] = lines
        return rep


def read_temperature(n):
    """Current temperature in C, or None. Cheap enough to call every minute."""
    if os.name != 'nt':
        return None
    try:
        with _open_disk(n) as f:
            nv = _read_nvme(f)
            if nv and nv['temp_c'] is not None:
                return nv['temp_c']
            attrs, _ = _read_ata(f)
            if attrs:
                for a in attrs:
                    if a['id'] in (194, 190):
                        return a['raw'] & 0xFF
    except Exception:
        pass
    return None


def _fmt_temp(c):
    if c is None:
        return 'n/a'
    return f'{c}°C ({round(c * 9 / 5 + 32)}°F)'
