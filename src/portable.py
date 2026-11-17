"""Portable mode: FlashRescue running from a USB stick on someone else's PC.

A file called portable.txt next to FlashRescue.exe switches it on. Then everything FlashRescue
writes for itself stays on the stick:
  settings, the last-scan file      ->  <stick>\\FlashRescue\\Data
  temp files (previews, checks,
  the scanner helper process)       ->  <stick>\\FlashRescue\\Data\\temp
  ffmpeg                            ->  <stick>\\FlashRescue\\tools
and the folder version of the program is used, so nothing is unpacked into the host PC's temp
folder either. Recovered files and rescue images go wherever you choose.

What no program can prevent: Windows itself notes that a program ran (Prefetch, a few registry
entries). That's why scanning the drive Windows is running from is still a last resort.
"""
import os
import shutil
import sys
import tempfile

MARKER = 'portable.txt'
MARKER_TEXT = """This copy of FlashRescue is portable.

While this file sits next to FlashRescue.exe, FlashRescue keeps its settings and temp files
in the "Data" folder here and ffmpeg in the "tools" folder here, so it writes nothing to the
PC it runs on (except what Windows itself records about any program that runs).

Delete this file to make it an ordinary copy again.

Recovering from a PC's own Windows drive? Windows keeps writing to that drive while it runs.
Best: shut the PC down and move the drive to another PC (or a USB dock), or boot the PC from a
rescue stick (WinPE) and run FlashRescue from there. Always save to a different drive.
"""


def app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # the folder above src


def is_portable():
    return os.path.isfile(os.path.join(app_dir(), MARKER))


def is_folder_build():
    """The folder version (FlashRescue.exe + _internal) - the one that can run without unpacking."""
    return getattr(sys, 'frozen', False) and os.path.isdir(os.path.join(app_dir(), '_internal'))


def data_dir():
    if is_portable():
        return os.path.join(app_dir(), 'Data')
    base = os.environ.get('APPDATA') or os.path.expanduser('~')
    return os.path.join(base, 'FlashRescue')


def tools_dir():
    if is_portable():
        return os.path.join(app_dir(), 'tools')
    base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA') or os.path.expanduser('~')
    return os.path.join(base, 'FlashRescue', 'tools')


def temp_dir():
    return os.path.join(data_dir(), 'temp')


def setup():
    """Call first thing at start-up (before any helper process starts: they inherit it)."""
    if not is_portable():
        return False
    t = temp_dir()
    try:
        if os.path.isdir(t):                 # leftovers from last time (previews, checks)
            shutil.rmtree(t, ignore_errors=True)
        os.makedirs(t, exist_ok=True)
    except OSError:
        return True
    for k in ('TEMP', 'TMP', 'TMPDIR'):
        os.environ[k] = t
    tempfile.tempdir = None                  # make tempfile pick the new folder up
    return True


def drive_of(path):
    path = os.path.abspath(path)
    return path[0].upper() if len(path) > 1 and path[1] == ':' else None


def stick_letter():
    return drive_of(app_dir())


def host_windows_letter():
    sd = os.environ.get('SystemDrive') or 'C:'
    return sd[0].upper()


def copy_app(target, tools=None, progress=lambda done, total: None, settings=None):
    """Copy the running folder build to `target` (e.g. E:\\FlashRescue) and make it portable.
    `tools`: (ffmpeg, ffprobe) paths to copy along, or None. Returns the target folder."""
    import json
    src = app_dir()
    files = [os.path.join(src, 'FlashRescue.exe')]
    for root, _, names in os.walk(os.path.join(src, '_internal')):
        files += [os.path.join(root, n) for n in names]
    if tools and tools[0]:
        files += [t for t in tools if t]
    total = sum(os.path.getsize(f) for f in files)
    done = 0
    old_internal = os.path.join(target, '_internal')
    if os.path.isdir(old_internal):
        shutil.rmtree(old_internal)
    for f in files:
        if tools and f in tools:
            dst = os.path.join(target, 'tools', os.path.basename(f))
        else:
            dst = os.path.join(target, os.path.relpath(f, src))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(f, dst)
        done += os.path.getsize(f)
        progress(done, total)
    finish_stick(target, settings)
    return target


def finish_stick(target, settings=None):
    import json
    with open(os.path.join(target, MARKER), 'w', encoding='utf-8') as f:
        f.write(MARKER_TEXT)
    os.makedirs(os.path.join(target, 'Data'), exist_ok=True)
    sp = os.path.join(target, 'Data', 'settings.json')
    if not os.path.exists(sp):
        s = {k: v for k, v in (settings or {}).items() if k in ('timeout', 'columns')}
        s['check_updates'] = False           # a stick on a client's PC shouldn't phone home
        with open(sp, 'w', encoding='utf-8') as f:
            json.dump(s, f, indent=1)


def extract_zip(zip_path, target, progress=lambda done, total: None):
    """Unpack a FlashRescue-Portable zip into `target` (its top folder is dropped)."""
    import zipfile
    with zipfile.ZipFile(zip_path) as z:
        infos = [(i.filename.replace('\\', '/'), i) for i in z.infolist() if not i.is_dir()]
        names = [n for n, _ in infos]
        top = os.path.commonprefix([n.split('/', 1)[0] + '/' for n in names])
        total = sum(i.file_size for _, i in infos) or 1
        done = 0
        old_internal = os.path.join(target, '_internal')
        if os.path.isdir(old_internal):
            shutil.rmtree(old_internal)
        for n, info in infos:
            rel = n[len(top):] if top and n.startswith(top) else n
            if not rel or rel.startswith('Data/') or '..' in rel.split('/'):
                continue
            dst = os.path.join(target, *rel.split('/'))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with z.open(info) as s, open(dst, 'wb') as d:
                shutil.copyfileobj(s, d, 1 << 20)
            done += info.file_size
            progress(done, total)
    return target


def swap_script(new_dir, pid):
    """Write the little script that updates a stick in place once FlashRescue has closed:
    swap in the new program files, keep Data, tools and portable.txt, start it again."""
    app = app_dir()
    cmd = os.path.join(app, 'flashrescue_update.cmd')
    lines = [
        '@echo off',
        'title Updating FlashRescue',
        ':wait',
        f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul && (ping -n 2 127.0.0.1 >nul & goto wait)',
        f'if exist "{app}\\_internal" rmdir /s /q "{app}\\_internal"',
        f'move /y "{new_dir}\\_internal" "{app}\\_internal" >nul',
        f'move /y "{new_dir}\\FlashRescue.exe" "{app}\\FlashRescue.exe" >nul',
        f'rmdir /s /q "{new_dir}"',
        f'start "" "{app}\\FlashRescue.exe"',
        '(goto) 2>nul & del "%~f0"',
    ]
    with open(cmd, 'w', encoding='mbcs' if os.name == 'nt' else 'utf-8') as f:
        f.write('\r\n'.join(lines) + '\r\n')
    return cmd
