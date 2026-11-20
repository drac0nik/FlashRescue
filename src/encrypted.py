"""
Encrypted volumes: unlock with the password, then scan the decrypted view like any drive.
Nothing is ever written; the decrypted data only exists in memory while FlashRescue reads it.

  LUKS1 / LUKS2  (Linux, many NAS boxes)   key slots with PBKDF2 or Argon2i/Argon2id;
                                           AES in XTS, CBC-ESSIV and CBC modes
  VeraCrypt      (and TrueCrypt-mode)      AES with SHA-512 or SHA-256 key derivation,
                                           normal and hidden volumes, custom PIM

Not covered: Serpent/Twofish/Camellia and cascades, Whirlpool/Streebog key derivation, VeraCrypt
system (boot) encryption, BitLocker.
"""
import base64
import hashlib
import json
import struct
import zlib

import engine
from engine import MB

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    HAVE_CRYPTO = True
except ImportError:                       # pragma: no cover
    HAVE_CRYPTO = False

LUKS_MAGIC = b'LUKS\xba\xbe'


class UnlockError(ValueError):
    pass


def _need_crypto():
    if not HAVE_CRYPTO:
        raise UnlockError('the "cryptography" package is missing from this build')


# --------------------------------------------------------------------------------------
# sector ciphers
# --------------------------------------------------------------------------------------

class SectorCipher:
    """Decrypts whole sectors: data unit n uses tweak/IV from its sector number."""

    def __init__(self, cipher, mode, key, unit=512, iv_shift=0):
        _need_crypto()
        cipher, mode = cipher.lower(), mode.lower()
        if cipher != 'aes':
            raise UnlockError(f'{cipher} encryption is not supported (only AES)')
        self.key = key
        self.unit = unit
        self.iv_shift = iv_shift          # LUKS2 sector_size > 512 with iv_large_sectors off: IVs count 512 units
        if mode.startswith('xts'):
            self.kind = 'xts'
            self.iv = mode.split('-', 1)[1] if '-' in mode else 'plain64'
        elif mode.startswith('cbc'):
            self.kind = 'cbc'
            self.iv = mode.split('-', 1)[1] if '-' in mode else 'plain'
            if self.iv.startswith('essiv'):
                h = self.iv.split(':', 1)[1] if ':' in self.iv else 'sha256'
                salt = hashlib.new(h, key).digest()
                self.essiv = Cipher(algorithms.AES(salt), modes.ECB()).encryptor()
        elif mode == 'ecb':
            self.kind = 'ecb'
            self.iv = 'none'
        else:
            raise UnlockError(f'cipher mode "{mode}" is not supported')

    def _ivbytes(self, n):
        if self.iv in ('plain64', 'plain64be'):
            return struct.pack('<Q', n & 0xFFFFFFFFFFFFFFFF) + bytes(8)
        if self.iv == 'plain':
            return struct.pack('<I', n & 0xFFFFFFFF) + bytes(12)
        if self.iv.startswith('essiv'):
            return self.essiv.update(struct.pack('<Q', n) + bytes(8))
        return bytes(16)

    def decrypt(self, data, first_unit):
        u = self.unit
        out = bytearray()
        for k in range(0, len(data) - len(data) % u, u):
            n = (first_unit + k // u) << self.iv_shift
            block = data[k:k + u]
            if self.kind == 'xts':
                d = Cipher(algorithms.AES(self.key), modes.XTS(self._ivbytes(n))).decryptor()
            elif self.kind == 'cbc':
                d = Cipher(algorithms.AES(self.key), modes.CBC(self._ivbytes(n))).decryptor()
            else:
                d = Cipher(algorithms.AES(self.key), modes.ECB()).decryptor()
            out += d.update(block) + d.finalize()
        return bytes(out)

    def encrypt(self, data, first_unit):          # used by the tests
        u = self.unit
        out = bytearray()
        for k in range(0, len(data), u):
            n = (first_unit + k // u) << self.iv_shift
            if self.kind == 'xts':
                e = Cipher(algorithms.AES(self.key), modes.XTS(self._ivbytes(n))).encryptor()
            elif self.kind == 'cbc':
                e = Cipher(algorithms.AES(self.key), modes.CBC(self._ivbytes(n))).encryptor()
            else:
                e = Cipher(algorithms.AES(self.key), modes.ECB()).encryptor()
            out += e.update(data[k:k + u]) + e.finalize()
        return bytes(out)


class DecryptedSource(engine.RawSource):
    """The unlocked contents of an encrypted volume, read on the fly."""

    def __init__(self, parent, data_offset, size, cipher, label, unit_base=0, kind='encrypted'):
        self.parent = parent
        self.data_offset = data_offset
        self.cipher = cipher
        self.unit_base = unit_base        # unit number of the first data unit (VeraCrypt counts from the volume start)
        self.kind = kind
        import threading
        self.path = f'{parent.path}#unlocked@{data_offset}'
        self.label = label
        self.size = size
        self.sector_size = 512
        self.ALIGN = max(4096, cipher.unit)
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

    def close(self):
        pass

    def load_rescue_map(self, map_path):
        raise ValueError('load the rescue map with the encrypted drive image instead')

    def read(self, offset, length):
        hit = self.ra_hit(offset, length) if self.ra else None
        if hit is not None:
            return hit
        if offset >= self.size or length <= 0:
            return b''
        length = min(length, self.size - offset)
        u = self.cipher.unit
        a = offset - offset % u
        e = -(-(offset + length) // u) * u
        raw = self.parent.read(self.data_offset + a, e - a)
        raw = raw.ljust(e - a, b'\x00')
        plain = self.cipher.decrypt(raw, self.unit_base + a // u)
        return plain[offset - a: offset - a + length]

    def unreadable_in(self, runs):
        return self.parent.unreadable_in([(self.data_offset + o, n) for o, n in runs])


# --------------------------------------------------------------------------------------
# LUKS
# --------------------------------------------------------------------------------------

def _af_merge(material, key_len, stripes, hash_name):
    """LUKS anti-forensic splitter, undone."""
    dlen = hashlib.new(hash_name).digest_size

    def diffuse(buf):
        out = bytearray()
        for i in range(-(-len(buf) // dlen)):
            blk = buf[i * dlen:(i + 1) * dlen]
            out += hashlib.new(hash_name, struct.pack('>I', i) + blk).digest()[:len(blk)]
        return bytes(out)

    d = bytes(key_len)
    for i in range(stripes - 1):
        s = material[i * key_len:(i + 1) * key_len]
        d = diffuse(bytes(a ^ b for a, b in zip(d, s)))
    last = material[(stripes - 1) * key_len:stripes * key_len]
    return bytes(a ^ b for a, b in zip(d, last))


def luks_info(src, base=0):
    """Basic facts from a LUKS header at `base` (no password needed), or None."""
    h = src.read(base, 4096)
    if h[:6] != LUKS_MAGIC:
        return None
    ver = struct.unpack('>H', h[6:8])[0]
    if ver == 1:
        return {'version': 1, 'cipher': h[8:40].split(b'\x00')[0].decode(), 'mode': h[40:72].split(b'\x00')[0].decode(),
                'uuid': h[168:208].split(b'\x00')[0].decode()}
    if ver == 2:
        hsize = struct.unpack('>Q', h[8:16])[0]
        meta = json.loads(src.read(base + 4096, hsize - 4096).split(b'\x00')[0])
        seg = meta['segments'][sorted(meta['segments'])[0]]
        return {'version': 2, 'cipher': seg['encryption'].split('-')[0], 'mode': seg['encryption'].split('-', 1)[1],
                'uuid': h[168:208].split(b'\x00')[0].decode(), 'label': h[24:72].split(b'\x00')[0].decode(),
                'meta': meta}
    return None


def _kdf(kdf, password, salt, length):
    t = kdf['type']
    if t == 'pbkdf2':
        return hashlib.pbkdf2_hmac(kdf['hash'], password, salt, kdf['iterations'], length)
    if t in ('argon2i', 'argon2id'):
        try:
            from argon2.low_level import hash_secret_raw, Type
        except ImportError:
            raise UnlockError('Argon2 support (argon2-cffi) is missing from this build')
        return hash_secret_raw(password, salt, time_cost=kdf['time'], memory_cost=kdf['memory'],
                               parallelism=kdf['cpus'], hash_len=length,
                               type=Type.ID if t == 'argon2id' else Type.I)
    raise UnlockError(f'key derivation "{t}" is not supported')


def unlock_luks(src, password, base=0, label=None, progress=lambda t: None):
    """DecryptedSource for the LUKS volume at `base`, or raises UnlockError."""
    _need_crypto()
    if isinstance(password, str):
        password = password.encode('utf-8')
    h = src.read(base, 4096)
    if h[:6] != LUKS_MAGIC:
        raise UnlockError('no LUKS header here')
    ver = struct.unpack('>H', h[6:8])[0]
    if ver == 1:
        cipher = h[8:40].split(b'\x00')[0].decode()
        mode = h[40:72].split(b'\x00')[0].decode()
        hname = h[72:104].split(b'\x00')[0].decode()
        payload, key_bytes = struct.unpack('>II', h[104:112])
        mk_digest, mk_salt, mk_iter = h[112:132], h[132:164], struct.unpack('>I', h[164:168])[0]
        for k in range(8):
            ks = h[208 + 48 * k:256 + 48 * k]
            active, iters = struct.unpack('>II', ks[:8])
            if active != 0x00AC71F3:
                continue
            progress(f'Trying key slot {k}...')
            salt = ks[8:40]
            km_off, stripes = struct.unpack('>II', ks[40:48])
            dk = hashlib.pbkdf2_hmac(hname, password, salt, iters, key_bytes)
            area = src.read(base + km_off * 512, -(-stripes * key_bytes // 512) * 512)
            mat = SectorCipher(cipher, mode, dk).decrypt(area, 0)
            mk = _af_merge(mat, key_bytes, stripes, hname)
            if hashlib.pbkdf2_hmac(hname, mk, mk_salt, mk_iter, 20) == mk_digest:
                size = src.size - base - payload * 512
                return DecryptedSource(src, base + payload * 512, size, SectorCipher(cipher, mode, mk),
                                       label or f'LUKS1 volume (unlocked, {cipher}-{mode})', kind='LUKS1')
        raise UnlockError('wrong password (no key slot opened)')
    if ver != 2:
        raise UnlockError(f'LUKS version {ver} is not supported')
    hsize = struct.unpack('>Q', h[8:16])[0]
    meta = json.loads(src.read(base + 4096, hsize - 4096).split(b'\x00')[0])
    digests = meta.get('digests', {})
    for kid in sorted(meta.get('keyslots', {}), key=int):
        ks = meta['keyslots'][kid]
        if ks.get('type') != 'luks2':
            continue
        progress(f'Trying key slot {kid}...')
        area, af, kdf = ks['area'], ks['af'], ks['kdf']
        key_size = ks['key_size']
        salt = base64.b64decode(kdf['salt'])
        enc = area['encryption']
        dk = _kdf(kdf, password, salt, area['key_size'])
        raw = src.read(base + int(area['offset']), int(area['size']))
        stripes = af.get('stripes', 4000)
        need = -(-stripes * key_size // 512) * 512
        c, m = enc.split('-', 1)
        mat = SectorCipher(c, m, dk).decrypt(raw[:need], 0)
        mk = _af_merge(mat, key_size, stripes, af.get('hash', 'sha256'))
        for dg in digests.values():
            if kid not in [str(x) for x in dg.get('keyslots', [])]:
                continue
            want = base64.b64decode(dg['digest'])
            got = hashlib.pbkdf2_hmac(dg['hash'], mk, base64.b64decode(dg['salt']), dg['iterations'], len(want))
            if got != want:
                continue
            seg_id = str(dg['segments'][0]) if dg.get('segments') else sorted(meta['segments'])[0]
            seg = meta['segments'][seg_id]
            off = int(seg['offset'])
            size = src.size - base - off if seg['size'] == 'dynamic' else int(seg['size'])
            sc, sm = seg['encryption'].split('-', 1)
            unit = int(seg.get('sector_size', 512))
            # LUKS2 always counts IVs in its own sector size (cryptsetup sets iv_large_sectors)
            cip = SectorCipher(sc, sm, mk, unit=unit)
            first = int(seg.get('iv_tweak', 0))
            return DecryptedSource(src, base + off, size, cip, label or f'LUKS2 volume (unlocked, {seg["encryption"]})',
                                   unit_base=first, kind='LUKS2')
    raise UnlockError('wrong password (no key slot opened)')


# --------------------------------------------------------------------------------------
# VeraCrypt
# --------------------------------------------------------------------------------------

VC_HASHES = (('sha512', 500000), ('sha256', 500000))


def _vc_iterations(hash_name, pim):
    if not pim:
        return dict(VC_HASHES)[hash_name]
    return 15000 + pim * 1000


def _vc_try_header(raw, password, pim, truecrypt=False):
    """Decrypted 512-byte header + the hash that worked, or None."""
    salt = raw[:64]
    for hname, _ in VC_HASHES:
        iters = (1000 if hname == 'sha512' else 2000) if truecrypt else _vc_iterations(hname, pim)
        key = hashlib.pbkdf2_hmac(hname, password, salt, iters, 64)
        d = Cipher(algorithms.AES(key), modes.XTS(bytes(16))).decryptor()
        plain = d.update(raw[64:512]) + d.finalize()
        magic = b'TRUE' if truecrypt else b'VERA'
        if plain[:4] != magic:
            continue
        hdr = raw[:64] + plain
        if zlib.crc32(hdr[256:512]) != struct.unpack('>I', hdr[72:76])[0]:
            continue
        if zlib.crc32(hdr[64:252]) != struct.unpack('>I', hdr[252:256])[0]:
            continue
        return hdr, hname
    return None


def unlock_veracrypt(src, password, base=0, size=None, pim=0, hidden=False, label=None,
                     progress=lambda t: None, truecrypt=False):
    """DecryptedSource for a VeraCrypt volume (whole drive, partition or container file) at `base`."""
    _need_crypto()
    if isinstance(password, str):
        password = password.encode('utf-8')
    size = size if size is not None else src.size - base
    tries = [(65536, 'hidden'), (0, 'normal')] if hidden else [(0, 'normal'), (65536, 'hidden')]
    tries += [(size - 131072, 'normal (backup header)'), (size - 65536, 'hidden (backup header)')]
    for off, what in tries:
        if off < 0:
            continue
        progress(f'Trying the {what} volume header...')
        raw = src.read(base + off, 512)
        if len(raw) < 512:
            continue
        got = _vc_try_header(raw, password, pim, truecrypt)
        if not got:
            continue
        hdr, hname = got
        vol_size, mk_start, enc_size = struct.unpack('>QQQ', hdr[100:124])
        sector = struct.unpack('>I', hdr[128:132])[0] or 512
        keys = hdr[256:256 + 64]
        cip = SectorCipher('aes', 'xts-plain64', keys, unit=sector)
        kind = 'VeraCrypt hidden' if 'hidden' in what else ('TrueCrypt' if truecrypt else 'VeraCrypt')
        # data units are numbered from the start of the host volume
        return DecryptedSource(src, base + mk_start, enc_size or vol_size, cip,
                               label or f'{kind} volume (unlocked, AES / {hname.upper()})',
                               unit_base=mk_start // sector, kind=kind)
    raise UnlockError('wrong password or PIM, or not a VeraCrypt volume (or it uses a cipher other than AES, '
                      'or Whirlpool/Streebog key derivation)')


def find_luks(src):
    """[(offset, info)] of LUKS headers on the drive (start, or the start of each partition)."""
    out = []
    i = luks_info(src, 0)
    if i:
        return [(0, i)]
    for off, _size in engine._partition_table(src):
        i = luks_info(src, off)
        if i:
            out.append((off, i))
    return out
