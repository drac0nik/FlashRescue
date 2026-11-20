"""LZO1X decompression in pure Python (the format btrfs uses for its 'lzo' compression).
A straight port of the reference decoder (Linux lib/lzo/lzo1x_decompress_safe.c)."""


class LzoError(ValueError):
    pass


def _zeros_run(src, ip):
    """Long lengths: each 0 byte adds 255, then one more byte."""
    start = ip
    while src[ip] == 0:
        ip += 1
    return (ip - start) * 255 + src[ip], ip + 1


def lzo1x_decompress(src, max_out=1 << 30):
    src = bytes(src)
    n = len(src)
    op = bytearray()
    ip = 0
    state = 0
    nxt = 0

    def copy_match(dist, length):
        start = len(op) - dist
        if start < 0:
            raise LzoError('lookbehind overrun')
        if dist >= length:
            op.extend(op[start:start + length])
        else:
            for k in range(length):
                op.append(op[start + k])

    try:
        if src[0] > 17:
            t = src[0] - 17
            ip = 1
            if t < 4:
                op.extend(src[ip:ip + t])
                ip += t
                state = t
            else:
                op.extend(src[ip:ip + t])
                ip += t
                state = 4
        while True:
            t = src[ip]
            ip += 1
            if t < 16:
                if state == 0:
                    if t == 0:
                        extra, ip = _zeros_run(src, ip)
                        t = 15 + extra
                    t += 3
                    op.extend(src[ip:ip + t])
                    ip += t
                    state = 4
                    continue
                elif state != 4:
                    nxt = t & 3
                    dist = 1 + (t >> 2) + (src[ip] << 2)
                    ip += 1
                    copy_match(dist, 2)
                else:
                    nxt = t & 3
                    dist = 1 + 0x0800 + (t >> 2) + (src[ip] << 2)
                    ip += 1
                    copy_match(dist, 3)
            elif t >= 64:
                nxt = t & 3
                dist = 1 + ((t >> 2) & 7) + (src[ip] << 3)
                ip += 1
                copy_match(dist, (t >> 5) + 1)
            elif t >= 32:
                length = (t & 31) + 2
                if length == 2:
                    extra, ip = _zeros_run(src, ip)
                    length = 2 + 31 + extra
                v = src[ip] | (src[ip + 1] << 8)
                ip += 2
                nxt = v & 3
                copy_match(1 + (v >> 2), length)
            else:
                length = (t & 7) + 2
                if length == 2:
                    extra, ip = _zeros_run(src, ip)
                    length = 2 + 7 + extra
                v = src[ip] | (src[ip + 1] << 8)
                ip += 2
                nxt = v & 3
                dist = ((t & 8) << 11) + (v >> 2)
                if dist == 0:
                    return bytes(op)                 # end of stream
                copy_match(dist + 0x4000, length)
            state = nxt
            if nxt:
                op.extend(src[ip:ip + nxt])
                ip += nxt
            if len(op) > max_out:
                raise LzoError('output too large')
    except IndexError:
        raise LzoError('truncated LZO data')


def btrfs_lzo(data, out_len=None):
    """Unpack a btrfs LZO extent: total length, then (length, LZO1X segment) pairs, a segment header
    never straddling a 4 KB boundary."""
    if len(data) < 4:
        raise LzoError('too short')
    total = int.from_bytes(data[:4], 'little')
    total = min(total, len(data))
    pos = 4
    out = bytearray()
    while pos + 4 <= total:
        left = 4096 - pos % 4096
        if left < 4:
            pos += left
            continue
        seg = int.from_bytes(data[pos:pos + 4], 'little')
        pos += 4
        if not seg or pos + seg > len(data):
            break
        out += lzo1x_decompress(data[pos:pos + seg])
        pos += seg
        if out_len and len(out) >= out_len:
            break
    return bytes(out)
