#!/usr/bin/env python3
"""nevisar_mic_stream always publishes to the "Mic Test" RTMP source:
   python nevisar_mic_stream.py
   python nevisar_mic_stream.py --mic 2
 The first run asks for a Nevisar API address + an admin login once and saves it
 in ~/.nevisar_mic_stream.json (file permission 0600). Every later run logs in,
 reads the current Mic Test stream_key from the gateway and streams to it, so it
 keeps working even after the key is rotated. Reconfigure with --setup.
 Overrides via env: NEVISAR_API_BASE, NEVISAR_USERNAME, NEVISAR_PASSWORD, NEVISAR_SOURCE.

 MANUAL MODE (skip auto-connect):
   python nevisar_mic_stream.py --url rtmp://HOST:1935/live/KEY
   python nevisar_mic_stream.py --host HOST --port 1935 --app live --key KEY
 otherwise the stream connects but nothing is written.
* The stream_key is only returned by the gateway to accounts that may manage live
  sources (admin). Log in with such an account during --setup.
* The RTMP subsystem serves one source at a time (single-publisher). Test one
  computer at a time.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

DEFAULT_SERVER = "192.168.19.54"
DEFAULT_API_BASE = f"http://{DEFAULT_SERVER}/api"
DEFAULT_RTMP_PORT = "1935"
DEFAULT_APP = "live"
DEFAULT_SOURCE = "Mic Test"
CONFIG_PATH = Path.home() / ".nevisar_mic_stream.json"


# small helpers


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def _die(msg: str) -> NoReturn:
    _err(msg)
    sys.exit(1)


def _ffmpeg_bin() -> str:
    exe = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    if not exe:
        _die("ffmpeg not found. Install ffmpeg or point the FFMPEG env var at it.")
    return exe


def _run(args, timeout: int = 25):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        _die(f"command not found: {args[0]}")
    except subprocess.TimeoutExpired:
        _die(f"command timed out after {timeout}s: {' '.join(args)}")


def _read_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass
    except Exception as exc:
        print(f"note: could not save config {CONFIG_PATH}: {exc}", file=sys.stderr)


def _ask(prompt: str, default: str = "", secret: bool = False) -> str:
    if default:
        prompt += f" [{default}]"
    prompt += ": "
    try:
        answer = (getpass.getpass(prompt) if secret else input(prompt)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(130)
    return answer or default


# platform detection


def _os_key() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return "other"


# device enumeration (each item: idx, label, fmt, value)


def _list_windows(fm: str) -> list:
    res = _run([fm, "-hide_banner", "-f", "dshow", "-list_devices", "true", "-i", "dummy"])
    text = res.stderr or ""
    devices: list = []
    # New ffmpeg (7.x/8.x) format: `"Name" (audio)` / `"Name" (video)`, no
    # "DirectShow audio devices" header. Old ffmpeg used section headers.
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
                current = None  # video device: don't attach its alt to audio
                continue
            am = re.search(r'Alternative name\s+"([^"]+)"', body)
            if am and current is not None and current["alt"] is None:
                current["alt"] = am.group(1)
    else:
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


def _list_darwin(fm: str) -> list:
    res = _run([fm, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""])
    in_audio = False
    audio: list = []
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


def _list_linux(fm: str) -> list:
    devices: list = []
    pactl = shutil.which("pactl")
    if pactl:
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

    arecord = shutil.which("arecord")
    if arecord:
        res = _run([arecord, "-l"], timeout=10)
        out = []
        for line in (res.stdout or "").splitlines():
            m = re.match(r"\s*card\s+(\d+):\s+([^\[,]+?)\s*\[[^\]]*\]\s*,\s*device\s+(\d+):\s*([^\[]+)", line)
            if m:
                card, device, name = m.group(1), m.group(3), m.group(4).strip()
                label = f"{name} (hw:{card},{device})"
                out.append({"idx": len(out) + 1, "label": label, "fmt": "alsa", "value": f"hw:{card},{device}"})
        if out:
            # Prepend a default entry for convenience.
            default = {"idx": 1, "label": "System default", "fmt": "alsa", "value": "default"}
            rest = [{"idx": i + 2, **{k: v for k, v in d.items() if k != "idx"}} for i, d in enumerate(out)]
            return [default] + rest
        return out

    # Last resort: let ffmpeg try pulse/alsa defaults.
    return [{"idx": 1, "label": "System default source", "fmt": "pulse", "value": "default"}]


def _list_devices(fm: str) -> list:
    key = _os_key()
    if key == "windows":
        return _list_windows(fm)
    if key == "darwin":
        return _list_darwin(fm)
    if key == "linux":
        return _list_linux(fm)
    # Unknown platform: try pulse/alsa listing, fall back to empty.
    try:
        return _list_linux(fm)
    except Exception:
        return []


def _http(method: str, url: str, token: str = "", form: dict | None = None,
         payload: dict | None = None, timeout: int = 15):
    headers = {"User-Agent": "nevisar-mic-stream"}
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


def _nevisar_login(api: str, username: str, password: str) -> str:
    code, body = _http("POST", f"{api}/auth/token",
                       form={"username": username, "password": password})
    if code == 200:
        try:
            token = json.loads(body).get("access_token")
        except Exception:
            token = None
        if token:
            return token
        raise RuntimeError("login succeeded but no access_token in the response")
    raise RuntimeError(f"login failed (HTTP {code}): {body.strip()[:200]}")


def _fetch_rtmp_sources(api: str, token: str) -> list:
    code, body = _http("GET", f"{api}/live/sources", token=token)
    if code != 200:
        raise RuntimeError(f"could not list live sources (HTTP {code}): {body.strip()[:200]}")
    try:
        sources = json.loads(body)
    except Exception:
        raise RuntimeError("live sources response was not valid JSON")
    if not isinstance(sources, list):
        raise RuntimeError("live sources response was not a list")
    return [s for s in sources if s.get("kind") == "rtmp"]


# RTMP target resolution


def _pick_source(sources: list, want: str) -> dict:
    want_name = (want or DEFAULT_SOURCE).strip()
    want_lower = want_name.lower()
    for s in sources:
        if str(s.get("name", "")).strip().lower() == want_lower:
            return s
    avail = ", ".join(f'"{s.get("name", "?")}"' for s in sources) or "(none)"
    raise RuntimeError(f'source "{want_name}" not found. Available RTMP sources: {avail}')


def _url_from_source(source: dict, rtmp_host: str) -> str:
    params = source.get("params") or {}
    stream_key = (
        source.get("stream_key")
        or params.get("stream_key")
        or params.get("key")
        or params.get("streamKey")
    )
    if not stream_key:
        raise RuntimeError(
            f'source "{source.get("name", "?")}" has no stream_key. Log in with an '
            "account that can manage live sources (admin)"
        )
    app = ((source.get("params") or {}).get("app")) or DEFAULT_APP
    return f"rtmp://{rtmp_host}:{DEFAULT_RTMP_PORT}/{app}/{stream_key}"


def _run_setup(cfg: dict, args) -> dict:
    print("\nNevisar auto-connect setup (saved to %s)" % CONFIG_PATH)
    api = _ask("Nevisar API address", cfg.get("api_base") or DEFAULT_API_BASE).rstrip("/")
    username = _ask("Nevisar username (admin, can manage live sources)",
                    os.environ.get("NEVISAR_USERNAME", cfg.get("username", "")))
    password = _ask("Nevisar password", secret=True)
    want = _ask("RTMP source name", os.environ.get("NEVISAR_SOURCE", cfg.get("source") or DEFAULT_SOURCE))

    print("\ncontacting gateway...")
    try:
        token = _nevisar_login(api, username, password)
        sources = _fetch_rtmp_sources(api, token)
    except RuntimeError as exc:
        _err(str(exc))
        print("check the API address and credentials, then retry.")
        sys.exit(1)

    if not sources:
        _err("no RTMP live sources exist on this Nevisar server")
        sys.exit(1)

    try:
        source = _pick_source(sources, want)
    except RuntimeError as exc:
        _err(str(exc))
        sys.exit(1)

    host = urlsplit(api).hostname or DEFAULT_SERVER
    url = _url_from_source(source, host)
    print(f"\nOK: found source \"{source.get('name')}\"")
    print(f"RTMP URL: {url}")

    cfg.update({
        "api_base": api,
        "username": username,
        "source": source.get("name"),
        "rtmp_host": host,
        "last_url": url,
    })
    # Only store password if explicitly provided via env/args? Never store
    # password in plain config by default.
    _write_config(cfg)
    return cfg


def _resolve_target(cfg: dict, args) -> str:
    api = args.api or os.environ.get("NEVISAR_API_BASE") or cfg.get("api_base") or DEFAULT_API_BASE
    api = api.rstrip("/")
    username = args.login or os.environ.get("NEVISAR_USERNAME") or cfg.get("username") or ""
    password = args.password or os.environ.get("NEVISAR_PASSWORD") or ""
    want = args.source or os.environ.get("NEVISAR_SOURCE") or cfg.get("source") or DEFAULT_SOURCE
    cached = cfg.get("last_url") or ""

    if not username or not password:
        if cached:
            print(f"note: no login configured, using cached URL (it may be stale if the key was rotated).",
                  file=sys.stderr)
            return cached
        _die("no Nevisar login stored. Run with --setup first, or pass --login/--password.")

    try:
        token = _nevisar_login(api, username, password)
        sources = _fetch_rtmp_sources(api, token)
        source = _pick_source(sources, want)
    except RuntimeError as exc:
        if cached:
            print(f"warning: {exc}; falling back to cached URL.", file=sys.stderr)
            print("OK it may be stale if the key was rotated.", file=sys.stderr)
            return cached
        _err(str(exc))
        print("hint: re-run with --setup to reconfigure the Nevisar login.")
        sys.exit(1)

    host = cfg.get("rtmp_host") or (urlsplit(api).hostname or DEFAULT_SERVER)
    url = _url_from_source(source, host)
    if cfg.get("source") != source["name"] or cfg.get("last_url") != url:
        cfg.update({"source": source["name"], "rtmp_host": host, "last_url": url})
        _write_config(cfg)
    return url


# manual RTMP URL handling


def _manual_url(cfg: dict, args) -> str:
    if args.url:
        return args.url.strip()
    host = args.host or cfg.get("rtmp_host") or os.environ.get("NEVISAR_RTMP_HOST") or DEFAULT_SERVER
    port = args.port or DEFAULT_RTMP_PORT
    app = args.app or DEFAULT_APP
    key = (args.key or "").strip()
    if not key:
        _die("manual mode needs --url or --key")
    return f"rtmp://{host}:{port}/{app}/{key}"


def _url_parts(url: str):
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or int(DEFAULT_RTMP_PORT)
        return host, port
    except Exception:
        return "", DEFAULT_RTMP_PORT


def _build_ffmpeg_cmd(fm: str, device: dict, url: str) -> list:
    cmd = [fm, "-hide_banner", "-nostdin", "-loglevel", "info"]
    if device["fmt"] == "dshow":
        cmd += ["-rtbufsize", "64M"]
    cmd += ["-f", device["fmt"], "-i", device["value"]]
    cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "1", "-f", "flv", url]
    return cmd


def _check_reachable(host: str, port: str) -> None:
    try:
        with socket.create_connection((host, int(port)), timeout=3):
            print(f"RTMP listener reachable at {host}:{port}")
    except Exception as exc:
        print(f"warning: could not reach rtmp://{host}:{port} ({exc})", file=sys.stderr)
        print("         make sure the Nevisar server is up and the RTMP port (1935) is open.", file=sys.stderr)


def _run_stream(cmd: list) -> int:
    print("\nStarting stream. Speak into the mic. Press Ctrl+C to stop.")
    print("Running: " + " ".join(cmd))
    try:
        proc = subprocess.run(cmd)
        return proc.returncode
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="nevisar_mic_stream",
        description="Stream a local microphone into the Nevisar \"Mic Test\" RTMP live source via ffmpeg.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--list", "-l", action="store_true", help="list microphones and exit")
    parser.add_argument("--mic", "-m", type=int, help="microphone number (from --list)")
    parser.add_argument("--setup", action="store_true", help="(re)configure the Nevisar auto-connect login")
    parser.add_argument("--source", help="auto-connect source name (default: Mic Test)")
    parser.add_argument("--api", help=f"Nevisar API base (default {DEFAULT_API_BASE})")
    parser.add_argument("--login", help="Nevisar username (override stored login)")
    parser.add_argument("--password", help="Nevisar password (override stored login)")
    parser.add_argument("--url", help="manual mode: full RTMP publish URL")
    parser.add_argument("--host", help=f"manual mode: RTMP host (default {DEFAULT_SERVER})")
    parser.add_argument("--port", help=f"manual mode: RTMP port (default {DEFAULT_RTMP_PORT})")
    parser.add_argument("--app", help=f"manual mode: RTMP app (default {DEFAULT_APP})")
    parser.add_argument("--key", help="manual mode: RTMP stream key (alternative to --url)")
    parser.add_argument("--no-check", action="store_true", help="skip the TCP reachability check")
    args = parser.parse_args()

    fm = _ffmpeg_bin()
    cfg = _read_config()

    print(f"platform: {_os_key()}  ffmpeg: {os.path.basename(fm)}")
    print("listing microphones...")
    devices = _list_devices(fm)
    if not devices:
        _err("no microphone input found on this machine")
        print("Linux: make sure PulseAudio/PipeWire is running (`pactl info`) or run `arecord -l`.")
        print("Windows: run `ffmpeg -f dshow -list_devices true -i dummy` to see devices.")
        print("macOS: grant Terminal microphone permission in System Settings -> Privacy.")
        return 1

    print()
    for d in devices:
        print(f"  [{d['idx']}]  {d['label']}")
    print()

    if args.list:
        return 0

    if args.mic is not None:
        if args.mic < 1 or args.mic > len(devices):
            _die(f"mic number out of range: {args.mic} (1..{len(devices)})")
        device = next(d for d in devices if d["idx"] == args.mic)
    else:
        answer = input(f"select microphone [1..{len(devices)}] (Enter = 1): ").strip()
        choice = int(answer) if answer.isdigit() else 1
        if choice < 1 or choice > len(devices):
            _die(f"invalid selection: {choice}")
        device = next(d for d in devices if d["idx"] == choice)

    if args.setup:
        cfg = _run_setup(cfg, args)
        print(f"\nsetup complete. target saved: {cfg.get('source')}")
        return 0

    if args.url or args.key:
        url = _manual_url(cfg, args)
        label = "manual target"
    else:
        url = _resolve_target(cfg, args)
        label = f'source "{args.source or cfg.get("source") or DEFAULT_SOURCE}"'
    host, port = _url_parts(url)
    if not host:
        _die("could not parse the RTMP URL")

    print(f"\nmicrophone : {device['label']}")
    print(f"publishing to {label}: {url}")

    if not args.no_check:
        _check_reachable(host, str(port))

    cmd = _build_ffmpeg_cmd(fm, device, url)
    return _run_stream(cmd)


if __name__ == "__main__":
    sys.exit(main())
