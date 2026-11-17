"""Updates: FlashRescue asks GitHub (at most once a day) whether a newer version has been
released, and if so shows an "Update to x.y.z" button. One click downloads the installer,
checks it against the checksum published with it, and runs it; the installer closes
FlashRescue, replaces the program and starts it again. Settings live in a separate folder
(%APPDATA%\\FlashRescue, or the Data folder of a portable copy) and aren't touched.

What is sent: an ordinary request for the project's latest-release page on GitHub, with the
user agent "FlashRescue-updater". Nothing about you, your drives or your files.
"""
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.request

# The GitHub project the releases come from ("owner/repo"). Empty = update checks are off.
UPDATE_REPO = "drac0nik/FlashRescue"

CHECK_EVERY = 24 * 3600
API = os.environ.get("FLASHRESCUE_UPDATE_API") or "https://api.github.com/repos/{repo}/releases/latest"
INSTALLER_RE = re.compile(r"^FlashRescue-Setup-(\d+\.\d+\.\d+)\.exe$", re.I)
PORTABLE_RE = re.compile(r"^FlashRescue-Portable-(\d+\.\d+\.\d+)\.zip$", re.I)
UA = {"User-Agent": "FlashRescue-updater", "Accept": "application/vnd.github+json"}


def repo(settings=None):
    r = ((settings or {}).get("update_repo") or UPDATE_REPO or "").strip().strip("/")
    return r if re.fullmatch(r"[\w.-]+/[\w.-]+", r) else ""


def vtuple(v):
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", v or "")
    return tuple(int(x or 0) for x in m.groups()) if m else (0, 0, 0)


def is_installed():
    """Running the program the installer put in place (not from source, not a Setup.bat build)."""
    if not getattr(sys, "frozen", False):
        return False
    return os.path.exists(os.path.join(os.path.dirname(sys.executable), "unins000.exe"))


def is_built():
    """A FlashRescue.exe made by Setup.bat (updates install the regular version alongside)."""
    return getattr(sys, "frozen", False) and not is_installed()


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest(settings=None, timeout=20):
    """The newest release: {"version", "notes", "page", "installer", "size", "sha256" (or None), "name"},
    or None when update checks are off or nothing usable was published."""
    rp = repo(settings)
    if not rp:
        return None
    data = json.loads(_get(API.format(repo=rp), timeout))
    if data.get("draft") or data.get("prerelease"):
        return None
    assets = {a.get("name", ""): a for a in data.get("assets", [])}
    inst = next((a for n, a in assets.items() if INSTALLER_RE.match(n)), None)
    port = next((a for n, a in assets.items() if PORTABLE_RE.match(n)), None)
    if not inst:
        return None

    def sha_of(a):
        digest = a.get("digest") or ""          # GitHub publishes "sha256:..." for each asset
        if digest.lower().startswith("sha256:"):
            return digest.split(":", 1)[1].lower()
        side = assets.get(a["name"] + ".sha256")
        if side:
            try:
                text = _get(side["browser_download_url"], timeout).decode("utf-8", "replace")
                m = re.search(r"\b([0-9a-f]{64})\b", text, re.I)
                return m.group(1).lower() if m else None
            except Exception:
                return None
        return None

    info = {"version": INSTALLER_RE.match(inst["name"]).group(1), "notes": data.get("body") or "",
            "page": data.get("html_url") or f"https://github.com/{rp}/releases/latest",
            "installer": inst["browser_download_url"], "size": int(inst.get("size") or 0), "sha256": sha_of(inst),
            "name": inst["name"], "portable": None}
    if port:
        info["portable"] = {"installer": port["browser_download_url"], "size": int(port.get("size") or 0),
                            "sha256": sha_of(port), "name": port["name"], "version": info["version"]}
    return info


def due(settings):
    if os.environ.get("FLASHRESCUE_NO_UPDATE_CHECK"):
        return False
    return bool(repo(settings)) and settings.get("check_updates", True) and \
        time.time() - float(settings.get("update_checked", 0) or 0) >= CHECK_EVERY


def newer(settings, current):
    """The newest release if it's newer than `current`, else None. (Network; run off the window thread.)"""
    info = latest(settings)
    if info and vtuple(info["version"]) > vtuple(current):
        return info
    return None


def download(info, progress=lambda done, total: None, cancelled=lambda: False, dest_dir=None):
    """The installer, downloaded to the temp folder and checked. Returns its path, or None if
    cancelled. Raises if the download is incomplete or doesn't match its checksum."""
    if not info.get("sha256"):
        raise RuntimeError("the release has no checksum to check the installer against")
    dest = os.path.join(dest_dir or tempfile.gettempdir(), info["name"])
    part = dest + ".part"
    h = hashlib.sha256()
    req = urllib.request.Request(info["installer"], headers={"User-Agent": UA["User-Agent"]})
    done = 0
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or info.get("size") or 0)
        while True:
            if cancelled():
                f.close()
                os.remove(part)
                return None
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            progress(done, total)
    if info.get("size") and done != info["size"]:
        os.remove(part)
        raise RuntimeError("the download was incomplete")
    if h.hexdigest().lower() != info["sha256"]:
        os.remove(part)
        raise RuntimeError("the downloaded installer doesn't match its published checksum - not run")
    os.replace(part, dest)
    return dest


def run_installer(path):
    """Start the installer for an update: it closes FlashRescue, installs, and starts it again."""
    import subprocess
    flags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0     # DETACHED_PROCESS | NEW_PROCESS_GROUP
    subprocess.Popen([path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                     creationflags=flags, close_fds=True)


# --------------------------------------------------------------------------------------
# ffmpeg (video checking / repair) - fetched on first use instead of shipped in the installer
# --------------------------------------------------------------------------------------

FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


def ffmpeg_dir():
    import portable
    return portable.tools_dir()


def download_ffmpeg(progress=lambda done, total: None, cancelled=lambda: False, out_dir=None):
    """Download gyan.dev's ffmpeg build, check it against the checksum gyan.dev publishes next to
    it, and put ffmpeg.exe + ffprobe.exe in %LOCALAPPDATA%\\FlashRescue\\tools. Returns that folder."""
    import zipfile
    want = _get(FFMPEG_URL + ".sha256", 30).decode("ascii", "replace")
    m = re.search(r"\b([0-9a-f]{64})\b", want, re.I)
    if not m:
        raise RuntimeError("gyan.dev didn't give a checksum for the ffmpeg download")
    want = m.group(1).lower()
    out = out_dir or ffmpeg_dir()
    os.makedirs(out, exist_ok=True)
    part = os.path.join(out, "ffmpeg.zip.part")
    h = hashlib.sha256()
    done = 0
    req = urllib.request.Request(FFMPEG_URL, headers={"User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        while True:
            if cancelled():
                f.close()
                os.remove(part)
                return None
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            progress(done, total)
    if h.hexdigest().lower() != want:
        os.remove(part)
        raise RuntimeError("the ffmpeg download doesn't match its published checksum - not used")
    got = 0
    with zipfile.ZipFile(part) as z:
        for name in z.namelist():
            base = name.rsplit("/", 1)[-1].lower()
            if base in ("ffmpeg.exe", "ffprobe.exe"):
                with z.open(name) as src, open(os.path.join(out, base), "wb") as dst:
                    while True:
                        b = src.read(1 << 20)
                        if not b:
                            break
                        dst.write(b)
                got += 1
    os.remove(part)
    if got < 2:
        raise RuntimeError("ffmpeg.exe / ffprobe.exe weren't in the download")
    return out
