"""
Previews beyond pictures: so you (and the client) can tell the right files before recovering them.

  video       a frame from about 1 second in, plus length / size / codec (ffmpeg)
  audio       length, format and tags (ffprobe)
  documents   the text of Word / Excel / PowerPoint (new and old formats), OpenDocument, PDF, RTF,
              e-mail and web pages
  camera raw  the camera's own embedded JPEG preview (CR2, NEF, ARW, DNG, RAF, ORF, RW2, PEF, ...)
  anything    a hex dump plus any readable text found inside
"""
import io
import json
import os
import re
import subprocess
import tempfile
import zipfile
import zlib

MB = 1024 * 1024
VIDEO = {'mp4', 'mov', 'm4v', '3gp', 'avi', 'mkv', 'webm', 'mts', 'm2ts', 'mpg', 'mpeg', 'wmv', 'flv', 'vob'}
AUDIO = {'mp3', 'wav', 'm4a', 'flac', 'ogg', 'aac', 'wma', 'opus'}
RAW = {'cr2', 'cr3', 'nef', 'arw', 'dng', 'raf', 'orf', 'rw2', 'pef', 'srw', 'nrw', 'crw', 'heic'}
OOXML = {'docx', 'xlsx', 'pptx', 'docm', 'xlsm', 'pptm'}
ODF = {'odt', 'ods', 'odp'}
OLE = {'doc', 'xls', 'ppt', 'msg'}
MARKUP = {'html', 'htm', 'xml', 'eml', 'mht', 'svg'}
TEXT = {'txt', 'csv', 'log', 'md', 'json', 'ini', 'cfg', 'srt', 'lua', 'py', 'bat', 'tsv', 'vcf', 'ics'}
VIDEO_MAX = 512 * MB          # biggest video copied to a temp file for a frame
TEXT_LIMIT = 6000
_NOWIN = 0x08000000 if os.name == 'nt' else 0


def _clean(text):
    text = re.sub(r'[ \t\r\f\v]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()[:TEXT_LIMIT]


def _strip_tags(x):
    x = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', x)
    x = re.sub(r'(?i)<(br|/p|/div|/tr|/h\d|w:p|/w:p|text:p|/text:p|a:p|/a:p)\b[^>]*>', '\n', x)
    x = re.sub(r'<[^>]+>', ' ', x)
    for a, b in (('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'), ('&quot;', '"'), ('&#39;', "'"), ('&nbsp;', ' ')):
        x = x.replace(a, b)
    return x


# ---------------------------------------------------------------------------- documents

def ooxml_text(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    parts = []
    if 'word/document.xml' in names:
        parts.append(z.read('word/document.xml').decode('utf-8', 'replace'))
    shared = []
    if 'xl/sharedStrings.xml' in names:
        x = z.read('xl/sharedStrings.xml').decode('utf-8', 'replace')
        shared = [_strip_tags(s) for s in re.findall(r'<si>(.*?)</si>', x, re.S)]
        parts.append('\n'.join(shared[:400]))
    slides = sorted((n for n in names if re.match(r'ppt/slides/slide\d+\.xml$', n)),
                    key=lambda n: int(re.findall(r'\d+', n)[-1]))
    for n in slides[:30]:
        parts.append(z.read(n).decode('utf-8', 'replace') + '\n')
    title = ''
    if 'docProps/core.xml' in names:
        m = re.search(r'<dc:title>(.*?)</dc:title>', z.read('docProps/core.xml').decode('utf-8', 'replace'))
        title = f'Title: {m.group(1)}\n\n' if m and m.group(1).strip() else ''
    return title + _clean(_strip_tags('\n'.join(parts)))


def odf_text(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    return _clean(_strip_tags(z.read('content.xml').decode('utf-8', 'replace')))


def pdf_text(data):
    """Rough text from a PDF: decompress the content streams and pull out the shown strings."""
    out = []
    m = re.search(rb'/Title\s*\(([^)]{1,200})\)', data)
    if m:
        out.append('Title: ' + m.group(1).decode('latin-1', 'replace') + '\n')
    n_pages = len(re.findall(rb'/Type\s*/Page[^s]', data))
    if n_pages:
        out.append(f'Pages: {n_pages}\n')
    for sm in re.finditer(rb'stream\r?\n', data):
        if sum(len(x) for x in out) > TEXT_LIMIT:
            break
        start = sm.end()
        end = data.find(b'endstream', start)
        if end < 0:
            break
        raw = data[start:end]
        try:
            raw = zlib.decompressobj().decompress(raw)
        except zlib.error:
            continue
        if b'Tj' not in raw and b'TJ' not in raw:
            continue
        for t in re.finditer(rb'\((?:\\.|[^\\)])*\)\s*Tj|\[(.*?)\]\s*TJ|T\*|Td|TD', raw, re.S):
            s = t.group(0)
            if s in (b'T*',) or s.endswith((b'Td', b'TD')):
                out.append(' ')
                continue
            for piece in re.findall(rb'\(((?:\\.|[^\\)])*)\)', s):
                piece = re.sub(rb'\\([nrt()\\])', lambda m: {b'n': b'\n', b'r': b'', b't': b' '}.get(m.group(1), m.group(1)),
                               piece)
                out.append(piece.decode('latin-1', 'replace'))
    text = ''.join(out)
    return _clean(text) if text.strip() else '(no extractable text - probably scanned pages)'


def ole_text(data):
    """Old Office files: the text is stored as UTF-16 (or 8-bit) runs inside the file."""
    runs = re.findall(rb'(?:[\x20-\x7e\xa0-\xff]\x00|\r\x00|\n\x00){6,}', data)
    text = '\n'.join(r.decode('utf-16-le', 'replace') for r in runs[:2000])
    if len(text) < 200:
        text += '\n' + '\n'.join(r.decode('latin-1') for r in re.findall(rb'[\x20-\x7e]{8,}', data)[:500])
    return _clean(text)


def rtf_text(data):
    x = data.decode('latin-1', 'replace')
    x = re.sub(r'\\par[d]?\b', '\n', x)
    x = re.sub(r"\\'([0-9a-f]{2})", lambda m: chr(int(m.group(1), 16)), x)
    x = re.sub(r'\\[a-z]+-?\d* ?|[{}]', '', x)
    return _clean(x)


def strings(data, n=40):
    found = re.findall(rb'[\x20-\x7e]{6,}', data[:256 * 1024])
    found += [r.decode('utf-16-le').encode() for r in re.findall(rb'(?:[\x20-\x7e]\x00){6,}', data[:256 * 1024])]
    return [s.decode('ascii', 'replace') for s in found[:n]]


def document_text(ext, data):
    """Text for a preview, or None if this isn't a document type."""
    try:
        if ext in OOXML:
            return ooxml_text(data)
        if ext in ODF:
            return odf_text(data)
        if ext == 'pdf':
            return pdf_text(data)
        if ext in OLE:
            return ole_text(data)
        if ext == 'rtf':
            return rtf_text(data)
        if ext in MARKUP:
            t = data.decode('utf-8', 'replace')
            if ext == 'eml':
                head, _, body = t.partition('\n\n')
                keep = [l for l in head.splitlines() if l.lower().startswith(('from:', 'to:', 'subject:', 'date:'))]
                return _clean('\n'.join(keep) + '\n\n' + _strip_tags(body))
            return _clean(_strip_tags(t))
        if ext in TEXT:
            for enc in ('utf-8', 'utf-16'):
                try:
                    return data[:TEXT_LIMIT * 2].decode(enc)[:TEXT_LIMIT]
                except UnicodeDecodeError:
                    continue
            return data[:TEXT_LIMIT].decode('latin-1')
    except Exception as ex:
        return f'(could not read the document text: {ex} - the file may be damaged)'
    return None


# ---------------------------------------------------------------------------- raw photos

def embedded_jpeg(data):
    """The biggest complete JPEG inside a camera raw file (its built-in preview)."""
    best = None
    pos = 0
    while True:
        s = data.find(b'\xff\xd8\xff', pos)
        if s < 0:
            break
        e = data.find(b'\xff\xd9', s + 3)
        while e > 0 and e - s < 4096:
            e = data.find(b'\xff\xd9', e + 2)
        if e < 0:
            break
        if best is None or e + 2 - s > best[1] - best[0]:
            best = (s, e + 2)
        pos = s + 3
        if pos > len(data):
            break
    return data[best[0]:best[1]] if best else None


# ---------------------------------------------------------------------------- video / audio

def probe(tools, path):
    ffprobe = tools[1] if tools else None
    if not ffprobe:
        return None
    try:
        r = subprocess.run([ffprobe, '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', path],
                           capture_output=True, timeout=20, creationflags=_NOWIN)
        return json.loads(r.stdout or b'{}')
    except Exception:
        return None


def describe_media(info):
    if not info:
        return ''
    f = info.get('format', {})
    lines = []
    try:
        d = float(f.get('duration', 0))
        if d:
            lines.append(f'Length: {int(d // 3600):d}:{int(d % 3600 // 60):02d}:{d % 60:04.1f}')
    except ValueError:
        pass
    for s in info.get('streams', []):
        if s.get('codec_type') == 'video':
            lines.append(f"Video: {s.get('codec_name', '?')} {s.get('width', '?')}x{s.get('height', '?')}"
                         + (f" @ {s['r_frame_rate']}" if s.get('r_frame_rate', '0/0') != '0/0' else ''))
        elif s.get('codec_type') == 'audio':
            lines.append(f"Audio: {s.get('codec_name', '?')} {s.get('sample_rate', '')} Hz, "
                         f"{s.get('channels', '?')} channel(s)")
    tags = f.get('tags', {})
    for k in ('title', 'artist', 'album', 'creation_time', 'com.apple.quicktime.model', 'make', 'model'):
        if tags.get(k):
            lines.append(f'{k.replace("com.apple.quicktime.", "").replace("_", " ").capitalize()}: {tags[k]}')
    return '\n'.join(lines)


def video_frame(tools, path, box):
    ffmpeg = tools[0] if tools else None
    if not ffmpeg:
        return None
    for ss in ('1', '0'):
        try:
            r = subprocess.run([ffmpeg, '-v', 'error', '-ss', ss, '-i', path, '-frames:v', '1',
                                '-vf', f'scale={box[0]}:{box[1]}:force_original_aspect_ratio=decrease',
                                '-f', 'image2pipe', '-vcodec', 'png', 'pipe:1'],
                               capture_output=True, timeout=30, creationflags=_NOWIN)
            if r.stdout:
                from PIL import Image
                im = Image.open(io.BytesIO(r.stdout))
                im.load()
                return im
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------- entry point

def make(it, src, box, tools):
    """(picture or None, text) for the preview panel. Runs off the window thread."""
    ext = (it.ext or '').lower()
    if ext in VIDEO or ext in AUDIO:
        if not tools or not tools[0]:
            return None, 'Install ffmpeg (FlashRescue offers to download it when you check or repair videos) ' \
                         'to see a frame and details here.'
        if it.size > VIDEO_MAX:
            head = it.read_head(src, 64 * MB)
            note = f'\n(Only the first 64 MB were read for the preview - the file is {it.size // MB} MB.)'
        else:
            head = it.read_head(src, it.size)
            note = ''
        fd, tmp = tempfile.mkstemp(suffix='.' + ext, prefix='flashrescue_pv_')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(head)
            info = describe_media(probe(tools, tmp))
            pic = video_frame(tools, tmp, box) if ext in VIDEO else None
            if ext in VIDEO and pic is None:
                info += '\nNo frame could be decoded - the video may be damaged (try Repair video).'
            if not info:
                info = '' if pic is not None else 'ffmpeg could not read this file - it may be damaged.'
            return pic, info + note
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    if ext in RAW:
        data = it.read_head(src, min(it.size, 48 * MB))
        j = embedded_jpeg(data)
        if j:
            try:
                from PIL import Image
                im = Image.open(io.BytesIO(j))
                im.draft('RGB', (box[0] * 2, box[1] * 2))
                im.load()
                im.thumbnail(box)
                return im, "The camera's built-in preview of this raw photo."
            except Exception:
                pass
        return None, 'No built-in preview found in this raw photo.'
    limit = 24 * MB if ext in OOXML | ODF | OLE | {'pdf'} else 256 * 1024
    data = it.read_head(src, min(it.size, limit))
    text = document_text(ext, data)
    if text is not None:
        return None, text or '(no text found)'
    words = strings(data)
    return None, (('Readable text inside:\n' + '\n'.join(words)) if words else '')
