#!/usr/bin/env python3
"""Nevisar Mic Stream — Windows GUI (Persian, RTL-friendly).

Beautiful simple Persian UI + maximum auto-config:
- Defaults prefilled (API http://192.168.19.54/api, source "Mic Test")
- Remembers API / username / source / mic / ffmpeg path / options
- Optionally remembers password for one-click auto-connect
- Auto-detects ffmpeg (PATH + next to exe), auto-lists microphones,
  auto-logins and resolves the RTMP URL in background on startup.
- One big Start/Stop button. Log panel in Persian.

Build to exe (on Windows):
    pip install pyinstaller
    pyinstaller --noconfirm --onefile --windowed --name NevisarMic nevisar_mic_gui.py
Or just run: build_windows.bat

Only Python standard library is used (tkinter included with python.org /
Microsoft Store Python). No pip packages required to run the exe.

If ffmpeg is missing, the app installs it by itself on first run
(winget, else downloads the official portable build — needs internet).
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

# --------------------------------------------------------------------------
# Constants / config
# --------------------------------------------------------------------------

DEFAULT_SERVER = "192.168.19.54"
DEFAULT_API_BASE = f"http://{DEFAULT_SERVER}/api"
DEFAULT_RTMP_PORT = "1935"
DEFAULT_APP = "live"
DEFAULT_SOURCE = "Mic Test"

APP_NAME = "NevisarMic"


def _config_path() -> Path:
    """Windows: %APPDATA%/NevisarMic/config.json, else ~/.nevisar_mic_stream.json."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home())
        p = Path(base) / "NevisarMic" / "config.json"
    else:
        p = Path.home() / ".nevisar_mic_stream.json"
    return p


def _legacy_config_path() -> Path:
    return Path.home() / ".nevisar_mic_stream.json"


CONFIG_PATH = _config_path()

FFMPEG_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


def _register_bundled_fonts() -> None:
    """Load bundled Vazirmatn TTFs (Windows) so Persian text looks right even
    on PCs without the font installed. Silent fallback to system fonts."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        candidates = [
            base / "fonts" / "Vazirmatn-Regular.ttf",   # frozen (PyInstaller datas)
            base / "fonts" / "Vazirmatn-Bold.ttf",
            base / "assets" / "fonts" / "Vazirmatn-Regular.ttf",  # dev run
            base / "assets" / "fonts" / "Vazirmatn-Bold.ttf",
        ]
        gdi32 = ctypes.windll.gdi32
        for p in candidates:
            try:
                if p.is_file():
                    gdi32.AddFontResourceExW(str(p), 0x10, 0)
            except Exception:
                pass
    except Exception:
        pass


def _detect_fa_family(root) -> str:
    """Best Persian font available: bundled Vazirmatn → known Persian
    system fonts → Segoe UI → Tahoma."""
    try:
        import tkinter.font as tkfont
        avail = set(tkfont.families(root))
    except Exception:
        return "Tahoma"
    for fam in ("Vazirmatn", "IRANSans", "IRANSansX", "B Nazanin", "Segoe UI", "Tahoma"):
        if fam in avail:
            return fam
    return "Tahoma"


def _local_ffmpeg_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "NevisarMic" / "bin"
    return Path.home() / ".local" / "share" / "NevisarMic" / "bin"


def _local_ffmpeg_path() -> Path:
    name = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    return _local_ffmpeg_dir() / name


def _winget_ffmpeg_candidates() -> list[str]:
    if not sys.platform.startswith("win"):
        return []
    local_app = os.environ.get("LOCALAPPDATA") or ""
    if not local_app:
        return []
    pattern = str(Path(local_app) / "Microsoft" / "WinGet" / "Packages"
                  / "Gyan.FFmpeg_*" / "ffmpeg-*" / "bin" / "ffmpeg.exe")
    import glob
    try:
        return sorted(glob.glob(pattern))
    except Exception:
        return []

# --------------------------------------------------------------------------
# Core logic (adapted from stream_mic.py, GUI-friendly: raises, no input())
# --------------------------------------------------------------------------


def find_ffmpeg(extra_hint: str = "") -> str:
    candidates: list[str] = []
    if extra_hint:
        candidates.append(extra_hint)
    env = os.environ.get("FFMPEG", "")
    if env:
        candidates.append(env)
    which = shutil.which("ffmpeg")
    if which:
        candidates.append(which)
    # next to the exe / script (portable ffmpeg.exe)
    try:
        here = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
        for name in ("ffmpeg.exe", "ffmpeg"):
            candidates.append(str(here / name))
            candidates.append(str(here / "bin" / name))
            candidates.append(str(here / "ffmpeg" / "bin" / name))
    except Exception:
        pass
    try:
        candidates.append(str(_local_ffmpeg_path()))
    except Exception:
        pass
    candidates.extend(_winget_ffmpeg_candidates())
    for c in candidates:
        if c and Path(c).is_file():
            return str(Path(c))
        if c and os.path.basename(c) == c and shutil.which(c):
            return shutil.which(c)  # type: ignore[return-value]
    # last try: bare name on PATH
    if shutil.which("ffmpeg"):
        return shutil.which("ffmpeg")  # type: ignore[return-value]
    return ""


def _verify_ffmpeg(path: str) -> bool:
    try:
        res = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=15)
        out = (res.stdout or "") + (res.stderr or "")
        return res.returncode == 0 and "ffmpeg version" in out.lower()
    except Exception:
        return False


def _try_winget_install(log) -> str:
    winget = shutil.which("winget")
    if not winget:
        log("winget پیدا نشد — می‌روم سراغ دانلود مستقیم.")
        return ""
    log("تلاش برای نصب با winget (Gyan.FFmpeg)… این ممکن است چند دقیقه طول بکشد.")
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0
        res = subprocess.run(
            [winget, "install", "-e", "--id", "Gyan.FFmpeg", "--silent",
             "--accept-source-agreements", "--accept-package-agreements"],
            capture_output=True, text=True, timeout=600, creationflags=creationflags)
        if res.returncode != 0:
            tail = ((res.stderr or res.stdout) or "").strip()[-300:]
            log(f"نصب winget ناموفق بود: {tail}")
            return ""
    except subprocess.TimeoutExpired:
        log("نصب winget زمان برد و متوقف شد — می‌روم سراغ دانلود مستقیم.")
        return ""
    except Exception as exc:
        log(f"اجرای winget ناممکن بود: {exc}")
        return ""
    fm = find_ffmpeg("")
    if fm and _verify_ffmpeg(fm):
        log(f"نصب winget موفق بود: {fm}")
        return fm
    log("winget تمام شد ولی ffmpeg هنوز پیدا نشد — می‌روم سراغ دانلود مستقیم.")
    return ""


def _ffmpeg_progress_text(done: int, total: int) -> str:
    if total > 0:
        pct = done * 100 // total
        return (f"در حال دانلود ffmpeg… {done / 1048576:.1f} از "
                f"{total / 1048576:.0f} مگابایت ({pct}٪)")
    return f"در حال دانلود ffmpeg… {done / 1048576:.1f} مگابایت"


def _download_portable_ffmpeg(progress=None) -> str:
    if not sys.platform.startswith("win"):
        raise RuntimeError("دانلود خودکار فقط در ویندوز پشتیبانی می‌شود.")
    dest = _local_ffmpeg_path()
    tmp_zip = ""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(FFMPEG_DOWNLOAD_URL, headers={"User-Agent": "nevisar-mic-gui"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            fd, tmp_zip = tempfile.mkstemp(prefix="nevisar-ffmpeg-", suffix=".zip")
            downloaded = 0
            try:
                with os.fdopen(fd, "wb") as fh:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if progress:
                            try:
                                progress(downloaded, total)
                            except Exception:
                                pass
            except Exception:
                try:
                    os.unlink(tmp_zip)
                except OSError:
                    tmp_zip = ""
                raise
        member = ""
        with zipfile.ZipFile(tmp_zip) as zf:
            for name in zf.namelist():
                if name.replace("\\", "/").endswith("/bin/ffmpeg.exe"):
                    member = name
                    break
            if not member:
                raise RuntimeError("فایل ffmpeg.exe داخل بسته دانلودی پیدا نشد.")
            with zf.open(member) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"دانلود ناموفق بود (اینترنت را چک کنید): {exc}")
    finally:
        if tmp_zip:
            try:
                os.unlink(tmp_zip)
            except OSError:
                pass
    if not _verify_ffmpeg(str(dest)):
        raise RuntimeError("فایل دانلود شد ولی اجرای ffmpeg ناممکن بود.")
    return str(dest)


def ensure_ffmpeg(log, progress=None) -> str:
    fm = find_ffmpeg("")
    if fm and _verify_ffmpeg(fm):
        return fm
    if sys.platform.startswith("win"):
        fm = _try_winget_install(log)
        if fm:
            return fm
        log("دانلود بیلد رسمی ffmpeg (حدود ۱۱۰ مگابایت)…")
        return _download_portable_ffmpeg(progress=progress)
    raise RuntimeError("ffmpeg پیدا نشد. آن را با مدیر بسته سیستم نصب کنید.")


def _run(args: list[str], timeout: int = 25) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def list_windows_devices(fm: str) -> list[dict]:
    res = _run([fm, "-hide_banner", "-f", "dshow", "-list_devices", "true", "-i", "dummy"])
    text = res.stderr or ""
    devices: list[dict] = []
    if "(audio)" in text or "(video)" in text:
        current = None
        for line in text.splitlines():
            body = line.split("]", 1)[-1].strip()
            m_audio = re.match(r'"([^"]+)"\s*\(audio\)', body)
            if m_audio:
                current = {"label": m_audio.group(1), "alt": None}
                devices.append(current)
                continue
            if re.match(r'"[^"]+"\s*\(video\)', body):
                current = None
                continue
            am = re.search(r'Alternative name\s+"([^"]+)"', body)
            if am and current is not None and current["alt"] is None:
                current["alt"] = am.group(1)
    else:  # old ffmpeg section headers
        current = None
        in_audio = False
        for line in text.splitlines():
            if "DirectShow audio devices" in line:
                in_audio = True
                continue
            if "DirectShow video devices" in line:
                in_audio = False
                continue
            if not in_audio:
                continue
            body = line.split("]", 1)[-1].strip()
            m = re.match(r'"([^"]+)"', body)
            if m:
                current = {"label": m.group(1), "alt": None}
                devices.append(current)
            else:
                am = re.search(r'Alternative name\s+"(@device_cm_[^"]+)"', body)
                if am and current is not None:
                    current["alt"] = am.group(1)
    out = []
    for i, d in enumerate(devices, start=1):
        value = f"audio={d['alt']}" if d["alt"] else f"audio={d['label']}"
        out.append({"idx": i, "label": d["label"], "fmt": "dshow", "value": value})
    return out


def list_linux_devices(fm: str) -> list[dict]:
    devices: list[dict] = []
    pactl = shutil.which("pactl")
    if pactl:
        try:
            res = _run([pactl, "list", "short", "sources"], timeout=10)
            names = []
            for line in (res.stdout or "").splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and ".monitor" not in parts[1]:
                    names.append(parts[1])
            if names:
                devices.append({"idx": 1, "label": "System default source", "fmt": "pulse", "value": "default"})
                for i, name in enumerate(names, start=2):
                    devices.append({"idx": i, "label": name, "fmt": "pulse", "value": name})
                return devices
        except Exception:
            pass
    return [{"idx": 1, "label": "System default source", "fmt": "pulse", "value": "default"}]


def list_devices(fm: str) -> list[dict]:
    if sys.platform.startswith("win"):
        return list_windows_devices(fm)
    if sys.platform == "darwin":
        res = _run([fm, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""])
        in_audio = False
        audio: list[tuple[int, str]] = []
        for line in (res.stderr or "").splitlines():
            if "AVFoundation audio devices" in line:
                in_audio = True
                continue
            if "AVFoundation video devices" in line:
                in_audio = False
                continue
            if not in_audio:
                continue
            m = re.search(r"\[\s*(\d+)\]\s+(.+?)\s*$", line.split("]", 1)[-1])
            if m:
                audio.append((int(m.group(1)), m.group(2).strip()))
        return [
            {"idx": i + 1, "label": name, "fmt": "avfoundation", "value": f":{index}"}
            for i, (index, name) in enumerate(audio)
        ]
    return list_linux_devices(fm)


def _http(method: str, url: str, token: str = "", form: dict | None = None,
         payload: dict | None = None, timeout: int = 15):
    headers = {"User-Agent": "nevisar-mic-gui"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def nevisar_login(api: str, username: str, password: str) -> str:
    code, body = _http("POST", f"{api}/auth/token",
                       form={"username": username, "password": password})
    if code == 200:
        try:
            token = json.loads(body).get("access_token")
        except Exception:
            token = None
        if token:
            return token
        raise RuntimeError("ورود موفق بود اما access_token برنگشت.")
    raise RuntimeError(f"ورود ناموفق بود (HTTP {code}): {body.strip()[:200]}")


def fetch_rtmp_sources(api: str, token: str) -> list:
    code, body = _http("GET", f"{api}/live/sources", token=token)
    if code != 200:
        raise RuntimeError(f"دریافت لیست سورس‌ها ناموفق بود (HTTP {code}): {body.strip()[:200]}")
    try:
        sources = json.loads(body)
    except Exception:
        raise RuntimeError("پاسخ لیست سورس‌ها JSON معتبر نبود.")
    if not isinstance(sources, list):
        raise RuntimeError("پاسخ لیست سورس‌ها لیست نبود.")
    return [s for s in sources if s.get("kind") == "rtmp"]


def pick_source(sources: list, want: str) -> dict:
    want_lower = (want or DEFAULT_SOURCE).strip().lower()
    for s in sources:
        if str(s.get("name", "")).strip().lower() == want_lower:
            return s
    avail = ", ".join(f'"{s.get("name", "?")}"' for s in sources) or "(هیچ)"
    raise RuntimeError(f'سورس "{want}" پیدا نشد. سورس‌های موجود: {avail}')


def url_from_source(source: dict, rtmp_host: str) -> str:
    params = source.get("params") or {}
    stream_key = (
        source.get("stream_key")
        or params.get("stream_key")
        or params.get("key")
        or params.get("streamKey")
    )
    if not stream_key:
        raise RuntimeError(
            f'سورس "{source.get("name", "?")}" کلید استریم ندارد. با اکانت ادمین وارد شوید.'
        )
    app = ((source.get("params") or {}).get("app")) or DEFAULT_APP
    return f"rtmp://{rtmp_host}:{DEFAULT_RTMP_PORT}/{app}/{stream_key}"


def build_ffmpeg_cmd(fm: str, device: dict, url: str) -> list[str]:
    cmd = [fm, "-hide_banner", "-nostdin", "-loglevel", "info"]
    if device["fmt"] == "dshow":
        cmd += ["-rtbufsize", "64M"]
    cmd += ["-f", device["fmt"], "-i", device["value"]]
    cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "1", "-f", "flv", url]
    return cmd


def load_config() -> dict:
    cfg: dict = {}
    # new path first, then legacy for migration
    for p in (CONFIG_PATH, _legacy_config_path()):
        try:
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    cfg.update(data)
        except Exception:
            pass
    cfg.setdefault("api_base", os.environ.get("NEVISAR_API_BASE", DEFAULT_API_BASE))
    cfg.setdefault("username", os.environ.get("NEVISAR_USERNAME", ""))
    cfg.setdefault("source", os.environ.get("NEVISAR_SOURCE", DEFAULT_SOURCE))
    cfg.setdefault("mic_label", "")
    cfg.setdefault("ffmpeg", os.environ.get("FFMPEG", ""))
    cfg.setdefault("remember_password", False)
    cfg.setdefault("auto_start", False)
    if not cfg.get("password"):
        env_pw = os.environ.get("NEVISAR_PASSWORD", "")
        if env_pw:
            cfg["password"] = env_pw
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Never store password unless user explicitly opted in.
        to_save = dict(cfg)
        if not to_save.get("remember_password"):
            to_save.pop("password", None)
        CONFIG_PATH.write_text(json.dumps(to_save, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass
    except Exception:
        pass


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

BG = "#eef1f6"
CARD = "#ffffff"
ACCENT = "#1a73e8"
ACCENT_DARK = "#1558b0"
TEXT = "#202124"
MUTED = "#5f6368"
GREEN = "#1e8e3e"
RED = "#d93025"
GRAY = "#9aa0a6"
FONT = ("Tahoma", 10)
FONT_BOLD = ("Tahoma", 10, "bold")
FONT_TITLE = ("Tahoma", 14, "bold")
FONT_BIG = ("Tahoma", 13, "bold")
FONT_SMALL = ("Tahoma", 9)


class App:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.root = root
        self.root.title("استریم میکروفون نویسار — NevisarMic")
        self.root.configure(bg=BG)
        try:
            self.root.geometry("500x600")
            self.root.minsize(460, 520)
        except Exception:
            pass

        self.cfg = load_config()
        self.devices: list[dict] = []
        self.stream_proc: subprocess.Popen | None = None
        self.streaming = False
        self.resolved_url = self.cfg.get("last_url", "")
        self.msg_q: queue.Queue = queue.Queue()
        self._stop_reader = threading.Event()
        self._ffmpeg_log: list[str] = []
        self._ffmpeg_log_file: Path | None = None

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT, font=FONT)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT, font=FONT)
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=FONT_TITLE)
        style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=FONT_SMALL)
        style.configure("TEntry", font=FONT, padding=6)
        style.configure("TCombobox", font=FONT, padding=5)
        style.configure("TButton", font=FONT_BOLD, padding=7)
        style.configure("Accent.TButton", background=ACCENT, foreground="white",
                          borderwidth=0, focusthickness=0)
        style.map("Accent.TButton", background=[("active", ACCENT_DARK), ("disabled", GRAY)])
        style.configure("Big.Accent.TButton", background=ACCENT, foreground="white",
                        borderwidth=0, focusthickness=0, font=FONT_BIG, padding=10)
        style.map("Big.Accent.TButton", background=[("active", ACCENT_DARK), ("disabled", GRAY)])
        style.configure("Big.Ghost.TButton", background="#e8f0fe", foreground=ACCENT,
                        borderwidth=0, font=FONT_BIG, padding=10)
        style.configure("Big.Green.TButton", background=GREEN, foreground="white",
                        borderwidth=0, font=FONT_BIG, padding=10)
        style.map("Big.Green.TButton", background=[("active", "#137333"), ("disabled", GRAY)])
        style.configure("Big.Red.TButton", background=RED, foreground="white",
                        borderwidth=0, font=FONT_BIG, padding=10)
        style.map("Big.Red.TButton", background=[("active", "#a50e0e"), ("disabled", GRAY)])
        style.configure("Ghost.TButton", background="#e8f0fe", foreground=ACCENT, borderwidth=0)

        # ---- header ----
        header = ttk.Frame(root, style="TFrame", padding=(16, 14, 16, 4))
        header.pack(fill="x")
        title_row = ttk.Frame(header, style="TFrame")
        title_row.pack(fill="x")
        self.dot = tk.Canvas(title_row, width=16, height=16, bg=BG, highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 8))
        self._dot_id = self.dot.create_oval(2, 2, 14, 14, fill=GRAY, outline="")
        title = ttk.Label(title_row, text="استریم میکروفون نویسار", style="Title.TLabel",
                          anchor="e", justify="right")
        title.pack(side="right", fill="x", expand=True)
        sub = ttk.Label(header, text="اتصال خودکار به سورس «Mic Test» — فقط ffmpeg لازم است",
                        style="Sub.TLabel", anchor="e", justify="right")
        sub.pack(fill="x", pady=(2, 0))
        self.status_var = tk.StringVar(value="در حال آماده‌سازی…")
        self.status_lbl = ttk.Label(header, textvariable=self.status_var, style="Sub.TLabel",
                                    anchor="e", justify="right")
        self.status_lbl.pack(fill="x")

        # ---- body: scrollable (canvas), minimal — mic + one big button ----
        body_wrap = ttk.Frame(root, style="TFrame", padding=(16, 6, 0, 6))
        body_wrap.pack(fill="both", expand=True)
        self._canvas = tk.Canvas(body_wrap, bg=BG, highlightthickness=0, bd=0)
        vbar = ttk.Scrollbar(body_wrap, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="left", fill="y")
        self._canvas.pack(side="right", fill="both", expand=True)
        body = ttk.Frame(self._canvas, style="TFrame", padding=(0, 0, 16, 0))
        self._body_win = self._canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>",
                  lambda _e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>",
                          lambda e: self._canvas.itemconfig(self._body_win, width=e.width))

        def _on_wheel(e):
            try:
                if str(getattr(e, "widget", "")) == str(self.log_txt):
                    return  # let the log box scroll itself
                delta = getattr(e, "delta", 0)
                steps = -1 * (delta // 120) if delta else 0
                if steps:
                    self._canvas.yview_scroll(steps, "units")
            except Exception:
                pass

        def _on_wheel_up(_e):
            try:
                self._canvas.yview_scroll(-1, "units")
            except Exception:
                pass

        def _on_wheel_down(_e):
            try:
                self._canvas.yview_scroll(1, "units")
            except Exception:
                pass

        self._canvas.bind_all("<MouseWheel>", _on_wheel)
        self._canvas.bind_all("<Button-4>", _on_wheel_up)
        self._canvas.bind_all("<Button-5>", _on_wheel_down)

        # mic card (the only thing most users need)
        self.card2 = ttk.Frame(body, style="Card.TFrame", padding=14)
        self.card2.pack(fill="x", pady=6)
        self._card_title(self.card2, "🎙️  میکروفون")
        mic_row = ttk.Frame(self.card2, style="Card.TFrame")
        mic_row.pack(fill="x")
        mic_row.columnconfigure(0, weight=1)
        self.mic_var = tk.StringVar()
        self.mic_combo = ttk.Combobox(mic_row, textvariable=self.mic_var, state="readonly",
                                      justify="right")
        self.mic_combo.grid(row=0, column=0, sticky="ew", padx=(8, 0))
        refresh_btn = ttk.Button(mic_row, text="تازه‌سازی", style="Ghost.TButton",
                                 command=self.on_refresh_mics, width=10)
        refresh_btn.grid(row=0, column=1, sticky="e")
        self.mic_hint = ttk.Label(self.card2, text="در حال جستجوی میکروفون‌ها…",
                                  style="Card.TLabel", font=FONT_SMALL, foreground=MUTED,
                                  anchor="e", justify="right")
        self.mic_hint.pack(fill="x", pady=(6, 0))

        # ONE big primary action
        self.start_btn = ttk.Button(body, text="▶  شروع استریم", style="Big.Accent.TButton",
                                    command=self.on_start_stop, state="disabled")
        self.start_btn.pack(fill="x", pady=(4, 2), ipady=6)

        self.target_var = tk.StringVar(value="مقصد: —")
        tgt = ttk.Label(body, textvariable=self.target_var, style="TLabel",
                        font=FONT_SMALL, foreground=MUTED, anchor="e", justify="right",
                        wraplength=460)
        tgt.pack(fill="x")

        # settings toggle (advanced stays out of the way)
        self._adv_open = False
        self.adv_toggle_btn = ttk.Button(body, text="⚙️ تنظیمات", style="Ghost.TButton",
                                         command=self.on_toggle_advanced)
        self.adv_toggle_btn.pack(fill="x", pady=(8, 0))

        # advanced frame (hidden by default): connection + ffmpeg + test
        self.adv_frame = ttk.Frame(body, style="TFrame")

        # settings card
        self.card1 = ttk.Frame(self.adv_frame, style="Card.TFrame", padding=14)
        self.card1.pack(fill="x", pady=6)
        self._card_title(self.card1, "⚙️  تنظیمات اتصال")

        # NOTE: card1's title uses pack, so all grid widgets must live
        # inside this inner form frame (never mix pack+grid in one parent).
        form = ttk.Frame(self.card1, style="Card.TFrame")
        form.pack(fill="x")
        form.columnconfigure(0, weight=1)

        self.api_var = tk.StringVar(value=self.cfg.get("api_base", DEFAULT_API_BASE))
        self.user_var = tk.StringVar(value=self.cfg.get("username", ""))
        self.pass_var = tk.StringVar(value=self.cfg.get("password", "") if self.cfg.get("remember_password") else os.environ.get("NEVISAR_PASSWORD", ""))
        self.source_var = tk.StringVar(value=self.cfg.get("source", DEFAULT_SOURCE))
        self.remember_var = tk.BooleanVar(value=bool(self.cfg.get("remember_password", False)))
        self.auto_var = tk.BooleanVar(value=bool(self.cfg.get("auto_start", False)))
        self.show_var = tk.BooleanVar(value=False)

        self._row(form, 0, "آدرس API نویسار", self.api_var, show=None)
        self._row(form, 1, "نام کاربری (ادمین)", self.user_var, show=None)
        pw_entry = self._row(form, 2, "رمز عبور", self.pass_var, show="•")
        self.pw_entry = pw_entry
        self._row(form, 3, "نام سورس", self.source_var, show=None)

        opts = ttk.Frame(form, style="Card.TFrame")
        opts.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        opts.columnconfigure(0, weight=1)
        cb1 = ttk.Checkbutton(opts, text="ذخیره رمز برای اتصال خودکار", variable=self.remember_var)
        cb1.grid(row=0, column=1, sticky="e")
        cb_show = ttk.Checkbutton(opts, text="نمایش رمز", variable=self.show_var,
                                  command=self._toggle_show)
        cb_show.grid(row=0, column=0, sticky="w")
        cb2 = ttk.Checkbutton(opts, text="شروع خودکار استریم پس از باز شدن", variable=self.auto_var)
        cb2.grid(row=1, column=1, sticky="e", pady=(2, 0))

        # ffmpeg card (advanced)
        self.card3 = ttk.Frame(self.adv_frame, style="Card.TFrame", padding=14)
        self.card3.pack(fill="x", pady=6)
        self._card_title(self.card3, "🎬  موتور ffmpeg")
        ff_row = ttk.Frame(self.card3, style="Card.TFrame")
        ff_row.pack(fill="x")
        ff_row.columnconfigure(0, weight=1)
        self.ff_var = tk.StringVar(value=self.cfg.get("ffmpeg", ""))
        ff_entry = ttk.Entry(ff_row, textvariable=self.ff_var, justify="left", font=FONT_SMALL)
        ff_entry.grid(row=0, column=0, sticky="ew", padx=(8, 0))
        browse_btn = ttk.Button(ff_row, text="انتخاب…", style="Ghost.TButton",
                                command=self.on_browse_ffmpeg, width=10)
        browse_btn.grid(row=0, column=1, sticky="e")
        auto_row = ttk.Frame(self.card3, style="Card.TFrame")
        auto_row.pack(fill="x", pady=(6, 0))
        self.ff_auto_btn = ttk.Button(auto_row, text="⬇ دانلود و نصب خودکار ffmpeg",
                                      style="Accent.TButton",
                                      command=self.on_auto_install_ffmpeg)
        self.ff_auto_btn.pack(fill="x")
        self.ff_hint = ttk.Label(self.card3, text="در حال بررسی ffmpeg…",
                                 style="Card.TLabel", font=FONT_SMALL, foreground=MUTED,
                                 anchor="e", justify="right")
        self.ff_hint.pack(fill="x", pady=(6, 0))
        ff_help = ttk.Label(
            self.card3,
            text="پیدا نشد؟ دکمه دانلود خودکار را بزنید (اینترنت لازم است، حدود ۱۱۰ مگابایت).",
            style="Card.TLabel", font=FONT_SMALL, foreground=MUTED,
                         anchor="e", justify="right", wraplength=420)
        ff_help.pack(fill="x")

        # test button lives with the advanced settings
        self.test_btn = ttk.Button(self.adv_frame, text="تست اتصال", style="Ghost.TButton",
                                   command=self.on_test)
        self.test_btn.pack(fill="x", pady=(0, 6))

        # log (compact) — always last, below settings
        self.log_title = ttk.Label(body, text="گزارش", style="TLabel", anchor="e", justify="right")
        self.log_title.pack(fill="x", pady=(6, 2))
        self.log_txt = tk.Text(body, height=6, wrap="word", font=("Consolas", 9, "normal"),
                               bg="#101418", fg="#d7e3f4", insertbackground="white",
                               relief="flat", padx=10, pady=10)
        self.log_txt.pack(fill="both", expand=True)
        self.log_txt.tag_config("fa", font=FONT, justify="right")
        self.log_txt.configure(state="disabled")

        foot = ttk.Label(root, text="فقط یک نفر همزمان استریم کند",
                         style="Sub.TLabel", anchor="center", justify="center", padding=(8, 6, 8, 10))
        foot.pack(fill="x")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(120, self._pump_queue)
        # first run (no login saved yet): open settings so credentials get entered
        if not self.cfg.get("username") and not self.cfg.get("last_url"):
            self.on_toggle_advanced()
        self.root.after(500, self._sync_scroll)
        threading.Thread(target=self._startup_worker, daemon=True).start()

    def on_toggle_advanced(self):
        self._adv_open = not self._adv_open
        if self._adv_open:
            # pack BEFORE the report section so the log always stays at the bottom
            self.adv_frame.pack(fill="x", pady=(6, 0), before=self.log_title)
            self.adv_toggle_btn.configure(text="⚙️ بستن تنظیمات")
        else:
            self.adv_frame.pack_forget()
            self.adv_toggle_btn.configure(text="⚙️ تنظیمات")
        self.root.after(50, self._sync_scroll)

    def _sync_scroll(self):
        try:
            bbox = self._canvas.bbox("all")
            if bbox:
                self._canvas.configure(scrollregion=bbox)
        except Exception:
            pass

    # -- UI helpers --
    def _card_title(self, parent, text):
        from tkinter import ttk
        lbl = ttk.Label(parent, text=text, style="Card.TLabel", font=FONT_BOLD,
                        anchor="e", justify="right")
        lbl.pack(fill="x", pady=(0, 8))

    def _row(self, parent, r, label, var, show):
        from tkinter import ttk
        parent.columnconfigure(0, weight=1)
        lbl = ttk.Label(parent, text=label, style="Card.TLabel", anchor="e", justify="right")
        lbl.grid(row=r, column=1, sticky="e", padx=(8, 0), pady=4)
        entry = ttk.Entry(parent, textvariable=var, justify="right")
        if show:
            entry.configure(show=show)
        entry.grid(row=r, column=0, sticky="ew", pady=4)
        return entry

    def _toggle_show(self):
        try:
            self.pw_entry.configure(show="" if self.show_var.get() else "•")
        except Exception:
            pass

    def set_dot(self, color):
        try:
            self.dot.itemconfig(self._dot_id, fill=color)
        except Exception:
            pass

    def set_status(self, text):
        self.msg_q.put(("status", text))

    def log(self, text, fa=True):
        self.msg_q.put(("log", text))

    def _pump_queue(self):
        try:
            while True:
                kind, text = self.msg_q.get_nowait()
                if kind == "status":
                    self.status_var.set(text)
                elif kind == "log":
                    self.log_txt.configure(state="normal")
                    self.log_txt.insert("end", text + "\n", ("fa",))
                    self.log_txt.see("end")
                    self.log_txt.configure(state="disabled")
                elif kind == "ready":
                    self.start_btn.configure(state="normal")
                    if not self.streaming:
                        self.start_btn.configure(style="Big.Accent.TButton")
                    self.set_dot(GREEN)
                    self.status_var.set(text)
                elif kind == "busy":
                    self.set_dot(GRAY)
                    self.status_var.set(text)
                elif kind == "error":
                    self.start_btn.configure(style="Big.Red.TButton")
                    self.set_dot(RED)
                    self.status_var.set(text)
                elif kind == "target":
                    self.target_var.set(text)
                elif kind == "mics":
                    labels, hint = text
                    self.mic_combo.configure(values=labels)
                    if labels:
                        # restore saved mic if present
                        saved = self.cfg.get("mic_label", "")
                        if saved and saved in labels:
                            self.mic_var.set(saved)
                        else:
                            self.mic_var.set(labels[0])
                    self.mic_hint.configure(text=hint)
                elif kind == "ffmpeg":
                    ok, hint = text
                    self.ff_hint.configure(text=hint,
                                           foreground=GREEN if ok else RED)
                elif kind == "ffmpeg_progress":
                    self.ff_hint.configure(text=text, foreground=MUTED)
                elif kind == "autostart":
                    self.on_start_stop()
        except queue.Empty:
            pass
        self.root.after(120, self._pump_queue)

    # -- persistence --
    def _collect_cfg(self) -> dict:
        self.cfg.update({
            "api_base": self.api_var.get().strip().rstrip("/") or DEFAULT_API_BASE,
            "username": self.user_var.get().strip(),
            "source": self.source_var.get().strip() or DEFAULT_SOURCE,
            "mic_label": self.mic_var.get(),
            "ffmpeg": self.ff_var.get().strip(),
            "remember_password": bool(self.remember_var.get()),
            "auto_start": bool(self.auto_var.get()),
        })
        if self.remember_var.get():
            self.cfg["password"] = self.pass_var.get()
        else:
            self.cfg.pop("password", None)
            # keep in-memory only for this session
            if self.pass_var.get():
                self.cfg["_session_password"] = self.pass_var.get()
        save_config(self.cfg)
        return self.cfg

    def _password(self) -> str:
        pw = self.pass_var.get()
        if pw:
            return pw
        if self.cfg.get("_session_password"):
            return self.cfg["_session_password"]
        return os.environ.get("NEVISAR_PASSWORD", "") or self.cfg.get("password", "")

    def _current_device(self) -> dict | None:
        label = self.mic_var.get()
        for d in self.devices:
            if d["label"] == label:
                return d
        return self.devices[0] if self.devices else None

    # -- workers --
    def _startup_worker(self):
        try:
            self.msg_q.put(("busy", "در حال آماده‌سازی…"))
            self.log("سلام! 👋 در حال آماده‌سازی خودکار…")
            # 1) ffmpeg (install automatically if missing)
            fm = find_ffmpeg(self.cfg.get("ffmpeg", ""))
            if fm:
                if not self.ff_var.get():
                    self.ff_var.set(fm)
                self.msg_q.put(("ffmpeg", (True, f"ffmpeg پیدا شد: {fm}")))
                self.log(f"ffmpeg OK: {fm}")
            else:
                self.log("ffmpeg پیدا نشد — نصب خودکار شروع شد (نیاز به اینترنت)…")
                try:
                    fm = ensure_ffmpeg(
                        log=self.log,
                        progress=lambda done, total: self.msg_q.put(
                            ("ffmpeg_progress", _ffmpeg_progress_text(done, total))),
                    )
                except Exception as exc:
                    self.msg_q.put(("ffmpeg", (False, "نصب خودکار ناموفق بود — دکمه دانلود خودکار را بزنید یا مسیرش را انتخاب کنید.")))
                    self.log(f"❌ نصب خودکار ffmpeg ناموفق: {exc}")
                    self.log("راه دستی: winget install Gyan.FFmpeg — یا ffmpeg.exe را کنار برنامه بگذارید.")
                    self.msg_q.put(("error", "ffmpeg پیدا نشد"))
                    return
                self.cfg["ffmpeg"] = fm
                save_config(self.cfg)
                if not self.ff_var.get():
                    self.ff_var.set(fm)
                self.msg_q.put(("ffmpeg", (True, f"ffmpeg نصب شد: {fm}")))
                self.log(f"✅ ffmpeg نصب شد: {fm}")
            # 2) mics
            self._refresh_mics_sync(fm)
            if not self.devices:
                self.msg_q.put(("error", "میکروفونی پیدا نشد"))
                return
            # 3) try auto-resolve URL if we have credentials
            self._auto_resolve()
            self.msg_q.put(("ready", "آماده — دکمه شروع را بزنید"))
            # 4) auto-start if enabled
            if self.cfg.get("auto_start") and self.resolved_url and self.devices:
                self.log("شروع خودکار فعال است…")
                self.msg_q.put(("autostart", ""))
        except Exception as exc:
            import traceback
            self.log(f"❌ خطای آماده‌سازی: {exc}")
            self.log(traceback.format_exc().strip()[-800:])
            self.msg_q.put(("error", f"خطای آماده‌سازی: {exc}"))

    def _refresh_mics_sync(self, fm: str):
        try:
            devs = list_devices(fm)
        except Exception as exc:
            self.log(f"خطا در لیست میکروفون‌ها: {exc}")
            devs = []
        self.devices = devs
        labels = [d["label"] for d in devs]
        if labels:
            hint = f"{len(labels)} میکروفون پیدا شد — انتخاب شد: {labels[0]}"
            self.log(f"{len(labels)} میکروفون پیدا شد.")
        else:
            hint = "میکروفونی پیدا نشد."
        self.msg_q.put(("mics", (labels, hint)))

    def _auto_resolve(self):
        api = (self.api_var.get().strip() or DEFAULT_API_BASE).rstrip("/")
        user = self.user_var.get().strip() or self.cfg.get("username", "")
        pw = (self.cfg.get("password", "") if self.cfg.get("remember_password")
              else os.environ.get("NEVISAR_PASSWORD", "")) or self.cfg.get("_session_password", "")
        want = self.source_var.get().strip() or DEFAULT_SOURCE
        cached = self.cfg.get("last_url", "")
        if not user or not pw:
            if cached:
                self.resolved_url = cached
                self.msg_q.put(("target", f"مقصد (کش‌شده): {cached}"))
                self.log("لاگین ذخیره نشده؛ از آدرس کش‌شده استفاده می‌شود (ممکن است قدیمی باشد).")
            else:
                self.log("نام کاربری/رمز وارد نشده — برای اتصال خودکار آن‌ها را وارد کنید و تست اتصال بزنید.")
            return
        try:
            self.log("در حال ورود و دریافت کلید استریم…")
            token = nevisar_login(api, user, pw)
            sources = fetch_rtmp_sources(api, token)
            src = pick_source(sources, want)
            host = self.cfg.get("rtmp_host") or (urlsplit(api).hostname or DEFAULT_SERVER)
            url = url_from_source(src, host)
            self.resolved_url = url
            self.cfg.update({"source": src.get("name"), "rtmp_host": host, "last_url": url})
            save_config(self.cfg)
            self.msg_q.put(("target", f'مقصد: سورس "{src.get("name")}" آماده است'))
            self.log(f'سورس "{src.get("name")}" آماده است. کلید تازه دریافت شد.')
        except Exception as exc:
            if cached:
                self.resolved_url = cached
                self.msg_q.put(("target", f"مقصد (کش‌شده): {cached}"))
                self.log(f"هشدار: {exc} — از آدرس کش‌شده استفاده می‌شود.")
            else:
                self.log(f"خطای اتصال خودکار: {exc}")

    # -- button handlers --
    def on_refresh_mics(self):
        def worker():
            fm = self.ff_var.get().strip() or find_ffmpeg("")
            if not fm:
                self.log("اول ffmpeg را مشخص کنید.")
                return
            self.log("در حال تازه‌سازی لیست میکروفون‌ها…")
            self._refresh_mics_sync(fm)
        threading.Thread(target=worker, daemon=True).start()

    def on_browse_ffmpeg(self):
        from tkinter import filedialog
        init = self.ff_var.get().strip() or "C:\\"
        path = filedialog.askopenfilename(
            title="انتخاب ffmpeg",
            initialdir=str(Path(init).parent) if init else "C:\\",
            filetypes=[("ffmpeg", "ffmpeg.exe"), ("All files", "*.*")])
        if path:
            self.ff_var.set(path)
            self.msg_q.put(("ffmpeg", (True, f"ffmpeg انتخاب شد: {path}")))
            self.log(f"ffmpeg انتخاب شد: {path}")
            self.on_refresh_mics()

    def on_auto_install_ffmpeg(self):
        self.root.after(0, lambda: self.ff_auto_btn.configure(state="disabled"))
        self.log("نصب خودکار ffmpeg شروع شد…")

        def worker():
            try:
                fm = ensure_ffmpeg(
                    log=self.log,
                    progress=lambda done, total: self.msg_q.put(
                        ("ffmpeg_progress", _ffmpeg_progress_text(done, total))),
                )
            except Exception as exc:
                self.log(f"❌ نصب خودکار ناموفق: {exc}")
                self.log("راه دستی: winget install Gyan.FFmpeg — یا ffmpeg.exe را کنار برنامه بگذارید.")
                self.msg_q.put(("ffmpeg", (False, "نصب خودکار ناموفق بود — دوباره تلاش کنید یا دستی نصب کنید.")))
            else:
                self.cfg["ffmpeg"] = fm
                save_config(self.cfg)
                self.root.after(0, lambda: self.ff_var.set(fm))
                self.msg_q.put(("ffmpeg", (True, f"ffmpeg نصب شد: {fm}")))
                self.log(f"✅ ffmpeg نصب شد: {fm}")
                self.on_refresh_mics()
                if not self.devices:
                    threading.Thread(target=self._startup_worker, daemon=True).start()
            finally:
                self.root.after(0, lambda: self.ff_auto_btn.configure(state="normal"))
        threading.Thread(target=worker, daemon=True).start()

    def on_test(self):
        def worker():
            self._collect_cfg()
            self.msg_q.put(("busy", "در حال تست اتصال…"))
            self.log("تست اتصال به نویسار…")
            fm = self.ff_var.get().strip() or find_ffmpeg("")
            if not fm:
                self.log("خطا: ffmpeg پیدا نشد.")
                self.msg_q.put(("error", "ffmpeg پیدا نشد"))
                return
            try:
                api = self.api_var.get().strip().rstrip("/") or DEFAULT_API_BASE
                user = self.user_var.get().strip()
                pw = self._password()
                want = self.source_var.get().strip() or DEFAULT_SOURCE
                if not user or not pw:
                    raise RuntimeError("نام کاربری و رمز را وارد کنید.")
                token = nevisar_login(api, user, pw)
                sources = fetch_rtmp_sources(api, token)
                src = pick_source(sources, want)
                host = urlsplit(api).hostname or DEFAULT_SERVER
                url = url_from_source(src, host)
                self.resolved_url = url
                self.cfg.update({"source": src.get("name"), "rtmp_host": host, "last_url": url,
                                 "api_base": api, "username": user})
                save_config(self.cfg)
                # TCP check
                try:
                    with socket.create_connection((host, int(DEFAULT_RTMP_PORT)), timeout=3):
                        self.log(f"✅ تست موفق: سورس «{src.get('name')}» + پورت 1935 باز است.")
                except Exception as exc:
                    self.log(f"⚠️ لاگین موفق ولی پورت 1935 در دسترس نیست ({exc}) — VPN/شبکه را چک کنید.")
                self.msg_q.put(("target", f'مقصد: سورس "{src.get("name")}" آماده است'))
                self.msg_q.put(("ready", "تست موفق — آماده شروع"))
            except Exception as exc:
                self.log(f"❌ تست ناموفق: {exc}")
                self.msg_q.put(("error", "تست ناموفق — گزارش را ببینید"))
        threading.Thread(target=worker, daemon=True).start()

    def on_start_stop(self):
        if self.streaming:
            self._stop_stream()
            return
        self._collect_cfg()
        dev = self._current_device()
        if dev is None:
            self.log("خطا: میکروفونی انتخاب نشده.")
            return
        fm = self.ff_var.get().strip() or find_ffmpeg("")
        if not fm or not Path(fm).exists() and not shutil.which(fm):
            # shutil.which covers PATH case
            if not shutil.which(fm or "ffmpeg"):
                self.log("خطا: ffmpeg پیدا نشد.")
                return
        # fresh attempt: back to blue until we know the outcome
        self.start_btn.configure(text="▶  شروع استریم", style="Big.Accent.TButton")

        def worker():
            try:
                self.msg_q.put(("busy", "در حال اتصال…"))
                # resolve fresh key each start (auto rotation support)
                api = self.api_var.get().strip().rstrip("/") or DEFAULT_API_BASE
                user = self.user_var.get().strip()
                pw = self._password()
                want = self.source_var.get().strip() or DEFAULT_SOURCE
                url = ""
                if user and pw:
                    try:
                        token = nevisar_login(api, user, pw)
                        sources = fetch_rtmp_sources(api, token)
                        src = pick_source(sources, want)
                        host = self.cfg.get("rtmp_host") or (urlsplit(api).hostname or DEFAULT_SERVER)
                        url = url_from_source(src, host)
                        self.cfg.update({"source": src.get("name"), "rtmp_host": host, "last_url": url})
                        save_config(self.cfg)
                        self.log(f'کلید تازه برای «{src.get("name")}» دریافت شد.')
                    except Exception as exc:
                        cached = self.cfg.get("last_url", "") or self.resolved_url
                        if cached:
                            url = cached
                            self.log(f"هشدار: {exc} — با آدرس کش‌شده ادامه می‌دهم.")
                        else:
                            self.log(f"❌ شروع ناممکن: {exc}")
                            self.msg_q.put(("error", "شروع ناممکن — گزارش را ببینید"))
                            return
                else:
                    url = self.resolved_url or self.cfg.get("last_url", "")
                    if not url:
                        self.log("❌ نام کاربری/رمز لازم است (یا تست اتصال بزنید).")
                        self.msg_q.put(("error", "لاگین لازم است"))
                        return
                    self.log("بدون لاگین تازه، با آدرس کش‌شده ادامه می‌دهم.")
                self.resolved_url = url
                try:
                    host, port = urlsplit(url).hostname or "", urlsplit(url).port or 1935
                    with socket.create_connection((host, int(port)), timeout=3):
                        self.log(f"پورت {port} در {host} باز است.")
                except Exception as exc:
                    self.log(f"⚠️ پورت RTMP در دسترس نیست ({exc}) — باز هم تلاش می‌کنم…")
                cmd = build_ffmpeg_cmd(fm, dev, url)
                try:
                    safe_url = urlsplit(url)
                    safe_cmd = " ".join(cmd).replace(url, f"rtmp://{safe_url.hostname or '...'}/{safe_url.path.strip('/')[:20]}…")
                except Exception:
                    safe_cmd = " ".join(cmd[:7]) + " …"
                self.log(f"میکروفون: {dev['label']}  (fmt={dev['fmt']})")
                self.log(f"فرمان: {safe_cmd}")
                try:
                    lf_dir = _local_ffmpeg_dir().parent
                    lf_dir.mkdir(parents=True, exist_ok=True)
                    self._ffmpeg_log_file = lf_dir / "last-ffmpeg.log"
                    self._ffmpeg_log_file.write_text(f"cmd: {' '.join(cmd)}\nmic: {dev}\nurl: {url}\n---\n", encoding="utf-8")
                except Exception:
                    self._ffmpeg_log_file = None
                self._ffmpeg_log = []
                self.log("در حال شروع استریم… صحبت کنید!")
                self.msg_q.put(("target", f"در حال پخش: {dev['label']}"))
                start_ts = __import__("time").time()
                try:
                    self._stop_reader.clear()
                    creationflags = 0
                    if sys.platform.startswith("win"):
                        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    self.stream_proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, bufsize=1, creationflags=creationflags,
                        encoding="utf-8", errors="replace")
                except Exception as exc:
                    self.log(f"❌ اجرای ffmpeg ناممکن: {exc}")
                    self.msg_q.put(("error", "اجرای ffmpeg ناممکن"))
                    return
                self.streaming = True
                self.root.after(0, lambda: self.start_btn.configure(text="⏹  توقف استریم",
                                                                     style="Big.Green.TButton"))
                self.msg_q.put(("ready", "🔴 در حال استریم — برای توقف دکمه را بزنید"))
                reader = threading.Thread(target=self._read_ffmpeg_output, daemon=True)
                reader.start()
                rc = self.stream_proc.wait()
                try:
                    reader.join(timeout=2)
                except Exception:
                    pass
                elapsed = __import__("time").time() - start_ts
                self.streaming = False
                self.stream_proc = None
                self.root.after(0, lambda: self.start_btn.configure(text="▶  شروع استریم",
                                                                     style="Big.Accent.TButton"))
                if self._stop_reader.is_set():
                    self.log(f"استریم متوقف شد (کد {rc}).")
                    self.msg_q.put(("ready", "متوقف شد — دکمه شروع را بزنید"))
                elif rc == 0:
                    self.log("استریم پایان یافت.")
                    self.msg_q.put(("ready", "آماده — دکمه شروع را بزنید"))
                else:
                    tail = self._ffmpeg_log[-25:] if self._ffmpeg_log else []
                    if tail:
                        self.log("—— خروجی ffmpeg (آخرین خطوط) ——")
                        for ln in tail:
                            self.log(f"[ffmpeg] {ln[:500]}")
                        self.log("—— پایان خروجی ——")
                    hint = self._diagnose_ffmpeg_tail(tail, elapsed)
                    self.log(f"❌ ffmpeg پایان یافت (کد {rc} پس از {elapsed:.1f} ثانیه). {hint}")
                    if self._ffmpeg_log_file:
                        self.log(f"فایل گزارش کامل: {self._ffmpeg_log_file}")
                    self.msg_q.put(("error", "استریم قطع شد — گزارش را ببینید"))
            except Exception as exc:
                import traceback
                self.log(f"❌ خطای داخلی در شروع استریم: {exc}")
                self.log(traceback.format_exc().strip()[-800:])
                self.msg_q.put(("error", f"خطای داخلی: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _diagnose_ffmpeg_tail(self, tail: list[str], elapsed: float) -> str:
        text = "\n".join(tail).lower()
        if elapsed < 4 and not tail:
            return "خروجی خالی بود — میکروفون یا ffmpeg ممکن است درست راه‌اندازی نشده باشد."
        if "access is denied" in text or "could not open" in text or "device busy" in text:
            return "میکروفون در دسترس نیست (برنامه دیگری آن را گرفته یا مجوز ندارد)."
        if "could not find audio" in text or "no such device" in text or "device not found" in text:
            return "نام میکروفون اشتباه است — «تازه‌سازی» بزنید و دوباره انتخاب کنید."
        if "connection refused" in text or "failed to connect" in text or "rtmp_connect" in text:
            return "اتصال RTMP رد شد — فقط یک نفر همزمان می‌تواند استریم کند، یا پورت 1935 بسته است."
        if "handshake" in text or "auth" in text or "403" in text or "401" in text:
            return "کلید استریم/احراز هویت رد شد — «تست اتصال» بزنید تا کلید تازه گرفته شود."
        if "broken pipe" in text or "connection reset" in text:
            return "ارتباط شبکه قطع شد — اینترنت/VPN را چک کنید."
        if "timeout" in text:
            return "تایم‌اوت شبکه — اتصال به سرور ناپایدار است."
        if elapsed < 5:
            return "خیلی زود قطع شد — گزارش بالا را کپی کنید تا دقیق‌تر بررسی کنم."
        return "تک‌پابلیشر بودن و شبکه را چک کنید؛ اگر تکرار شد گزارش بالا را بفرستید."

    def _read_ffmpeg_output(self):
        proc = self.stream_proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                raw = line.strip()
                if not raw:
                    continue
                self._ffmpeg_log.append(raw)
                if len(self._ffmpeg_log) > 400:
                    self._ffmpeg_log = self._ffmpeg_log[-400:]
                if self._ffmpeg_log_file:
                    try:
                        with open(self._ffmpeg_log_file, "a", encoding="utf-8", errors="replace") as fh:
                            fh.write(raw + "\n")
                    except Exception:
                        pass
                low = raw.lower()
                if any(k in low for k in ("error", "fail", "connection", "refused",
                                          "denied", "timeout", "could not", "access is denied",
                                          "bitrate", "frame=", "opening", "publish")):
                    self.log(f"[ffmpeg] {raw[:500]}")
                elif len(self._ffmpeg_log) <= 4:
                    self.log(f"[ffmpeg] {raw[:500]}")
        except Exception:
            pass

    def _stop_stream(self):
        self.log("در حال توقف…")
        self._stop_reader.set()
        proc = self.stream_proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception as exc:
                self.log(f"خطا در توقف: {exc}")

    def on_close(self):
        try:
            self._collect_cfg()
        except Exception:
            pass
        if self.streaming:
            try:
                self._stop_stream()
            except Exception:
                pass
        try:
            self._canvas.unbind_all("<MouseWheel>")
            self._canvas.unbind_all("<Button-4>")
            self._canvas.unbind_all("<Button-5>")
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


def main():
    import tkinter as tk

    _register_bundled_fonts()
    root = tk.Tk()
    # Slightly nicer default on Windows HiDPI
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    # Pick the best Persian font now that Tk (and bundled fonts) is ready.
    global FONT, FONT_BOLD, FONT_TITLE, FONT_BIG, FONT_SMALL
    _fam = _detect_fa_family(root)
    FONT = (_fam, 10)
    FONT_BOLD = (_fam, 10, "bold")
    FONT_TITLE = (_fam, 14, "bold")
    FONT_BIG = (_fam, 13, "bold")
    FONT_SMALL = (_fam, 9)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
