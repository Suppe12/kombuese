"""Recipes from cooking videos (TikTok, Instagram, YouTube, Facebook …): download, transcript, stills (docs/video.md).

What read() does for a link:
  1. Checks the link: http(s) only, the host must resolve to a public address (netsafe.check), unless
     KOMBUESE_ALLOW_PRIVATE_URLS is set. Then yt-dlp reads the metadata only (-J): title, uploader, description,
     duration, thumbnail, and the format it would download. yt-dlp does its own networking (it is a separate program),
     so the app cannot check every connection it makes. To keep it to video sites, its generic extractor (which would
     read any page) is switched off and the format URL it chose is checked with netsafe before the download.
  2. Refuses videos longer than the limit (default 10 min) or larger than the size limit (default 200 MB); the
     description is still returned. Otherwise downloads that exact format (--load-info-json), at most 720p.
  3. ffmpeg extracts the audio (mono, 16 kHz) and up to 12 stills spread over the video (longer side 640 px).
  4. The audio goes to the Whisper service set up in Settings → Video & Whisper (or KOMBUESE_WHISPER_URL), nowhere
     else: an OpenAI-compatible /v1/audio/transcriptions (speaches, faster-whisper-server, the OpenAI API) or the
     whisper-asr-webservice /asr. When Whisper is down or not set up, description and stills come back anyway.
  5. Temporary files are deleted at once. The stills and the thumbnail are kept for a day in <data>/videoimport/ of
     the household, so the run can take one as the recipe photo (POST /api/videoimport/bild); then they go too.

Agents (inside the app and over MCP) never talk to Whisper themselves: they get the tool read_video(url) (jobs.py).
Settings per household (encrypted like the other connections); the defaults come from the environment:
  KOMBUESE_VIDEO              on (default) / off: video import switched on for new households
  KOMBUESE_WHISPER_URL        address of the Whisper service, e.g. http://whisper:9000
  KOMBUESE_WHISPER_API        auto (default), openai or asr
  KOMBUESE_WHISPER_KEY        API key (only for the OpenAI API or a service that wants one)
  KOMBUESE_WHISPER_MODEL      model name for the OpenAI-compatible API (default whisper-1)
  KOMBUESE_VIDEO_MAX_MINUTES  longest video (default 10), KOMBUESE_VIDEO_MAX_MB largest download (default 200)
  KOMBUESE_YTDLP              yt-dlp command (default: the yt_dlp package of the image, or a newer copy in
                              <data>/tools/yt-dlp after an update), KOMBUESE_FFMPEG ffmpeg command (default ffmpeg)
  KOMBUESE_YTDLP_AUTOUPDATE   true (default) / false: when a download fails, update yt-dlp (pip, at most every 12 h)
                              into <data>/tools/yt-dlp and try once more
"""
import base64
import io
import json
import math
import os
import re
import secrets
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave

import config
import household
import netsafe
import photos
import secretbox
import store
from store import Error

APIS = ("auto", "openai", "asr")
LANGS = ("auto", "household", "de", "en", "fr", "es", "it", "nl", "pl", "pt", "tr", "sv", "da", "cs", "ja", "ko", "zh")
UA = "Mozilla/5.0 (Kombuese; self-hosted kitchen app)"
MAX_FRAMES = 12
FRAME_SIDE = 640
KEEP = 24 * 3600  # stills for the recipe photo
TAG = {"de": "Aus Video", "en": "From video"}  # tag and note title of recipes from videos, in the household language
_sem = threading.BoundedSemaphore(2)  # downloads and ffmpeg at the same time
_cache = {}  # (household, url) → (time, result); a run that asks twice does not download twice
_detected = {}  # whisper url → api
_update = {"t": 0.0, "running": False, "last": None}
HOSTS = {  # host (without www.) → platform name; yt-dlp knows many more, these are the ones the prompt names
    "tiktok.com": "TikTok", "vm.tiktok.com": "TikTok", "vt.tiktok.com": "TikTok",
    "instagram.com": "Instagram", "instagr.am": "Instagram",
    "youtube.com": "YouTube", "m.youtube.com": "YouTube", "youtu.be": "YouTube", "youtube-nocookie.com": "YouTube",
    "facebook.com": "Facebook", "m.facebook.com": "Facebook", "fb.watch": "Facebook", "fb.com": "Facebook",
    "vimeo.com": "Vimeo", "pinterest.com": "Pinterest", "pin.it": "Pinterest", "x.com": "X", "twitter.com": "X",
    "threads.net": "Threads", "dailymotion.com": "Dailymotion", "twitch.tv": "Twitch", "reddit.com": "Reddit",
    "v.redd.it": "Reddit", "bilibili.com": "Bilibili", "rumble.com": "Rumble",
}


class VideoError(Exception):
    pass


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


def _on(v):
    return str(v).strip().lower() not in ("0", "off", "false", "no", "")


# ---------- settings ----------

def defaults():
    def num(name, d, lo, hi):
        try:
            return max(lo, min(hi, int(_env(name, str(d)))))
        except ValueError:
            return d
    api = _env("KOMBUESE_WHISPER_API", "auto").lower()
    return {"enabled": _on(_env("KOMBUESE_VIDEO", "on")), "whisper_url": _env("KOMBUESE_WHISPER_URL").rstrip("/"),
            "whisper_api": api if api in APIS else "auto", "whisper_model": _env("KOMBUESE_WHISPER_MODEL"),
            "language": "auto", "max_minutes": num("KOMBUESE_VIDEO_MAX_MINUTES", 10, 1, 60),
            "max_mb": num("KOMBUESE_VIDEO_MAX_MB", 200, 10, 2000),
            "autoupdate": _on(_env("KOMBUESE_YTDLP_AUTOUPDATE", "true"))}


def _stored():
    with store.con() as c:
        r = c.execute("SELECT data FROM integrations WHERE name='video'").fetchone()
    return json.loads(secretbox.decrypt(r["data"])) if r else {}


def settings(with_key=False):
    """Defaults from the environment, overridden by what the household saved. Without with_key: has_key instead of the key."""
    st = _stored()
    s = {**defaults(), **{k: v for k, v in st.items() if k != "whisper_key"}}
    key = st.get("whisper_key") or _env("KOMBUESE_WHISPER_KEY")
    if with_key:
        s["whisper_key"] = key
    else:
        s["has_key"] = bool(key)
        s["key_from_env"] = bool(not st.get("whisper_key") and _env("KOMBUESE_WHISPER_KEY"))
        s["url_from_env"] = bool(not st.get("whisper_url") and s["whisper_url"])
    return s


def save(b):
    st = _stored()
    if "enabled" in b:
        st["enabled"] = bool(b["enabled"])
    if "autoupdate" in b:
        st["autoupdate"] = bool(b["autoupdate"])
    if "whisper_url" in b:
        u = str(b["whisper_url"] or "").strip().rstrip("/")[:300]
        if u and not re.fullmatch(r"https?://[^\s/]+(/\S*)?", u):
            raise Error("Whisper: Adresse wie http://whisper:9000")
        if u:
            st["whisper_url"] = u
        else:  # empty = the address from KOMBUESE_WHISPER_URL (if any)
            st.pop("whisper_url", None)
        _detected.pop(u, None)
    if b.get("whisper_api") in APIS:
        st["whisper_api"] = b["whisper_api"]
    if "whisper_model" in b:
        st["whisper_model"] = re.sub(r"[^\w./:-]", "", str(b["whisper_model"] or ""))[:120]
    if b.get("language") in LANGS:
        st["language"] = b["language"]
    for k, lo, hi in (("max_minutes", 1, 60), ("max_mb", 10, 2000)):
        if k in b:
            try:
                st[k] = max(lo, min(hi, int(float(b[k]))))
            except (TypeError, ValueError):
                raise Error(f"{k}: Zahl") from None
    if b.get("whisper_key"):  # empty = keep the old key
        st["whisper_key"] = str(b["whisper_key"]).strip()[:400]
    if b.get("whisper_key_clear"):
        st.pop("whisper_key", None)
    with store.lock, store.con() as c:
        c.execute("INSERT INTO integrations(name, data, updated) VALUES('video',?,?) ON CONFLICT(name) DO UPDATE SET data=excluded.data, "
                  "updated=excluded.updated", (secretbox.encrypt(json.dumps(st)), store.now()))
    return settings()


def enabled():
    try:
        return bool(settings()["enabled"])
    except Exception:  # noqa: BLE001  e.g. while a backup is imported
        return False


# ---------- links ----------

def platform(url):
    """Platform name for a known video site, else None."""
    try:
        host = (urllib.parse.urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return None
    host = host[4:] if host.startswith("www.") else host
    while host:
        if host in HOSTS:
            return HOSTS[host]
        host = host.partition(".")[2]
    return None


def is_video_url(url):
    return platform(url) is not None


def check_url(url):
    url = str(url or "").strip()
    if not re.fullmatch(r"https?://\S{4,800}", url):
        raise VideoError("only http(s) links")
    try:
        netsafe.check(url)
    except netsafe.Blocked as e:
        raise VideoError(f"link refused: {e}") from None
    return url


# ---------- programs ----------

def _tools_dir():
    return os.path.join(config.DATA, "tools", "yt-dlp")


def ytdlp_cmd():
    """→ (argv prefix, extra environment). An updated copy in <data>/tools/yt-dlp wins over the one in the image."""
    own = _env("KOMBUESE_YTDLP")
    if own:
        return shlex.split(own), {}
    if os.path.isdir(os.path.join(_tools_dir(), "yt_dlp")):
        return [sys.executable, "-m", "yt_dlp"], {"PYTHONPATH": _tools_dir()}
    try:
        import importlib.util
        if importlib.util.find_spec("yt_dlp"):
            return [sys.executable, "-m", "yt_dlp"], {}
    except (ImportError, ValueError):
        pass
    exe = shutil.which("yt-dlp")
    return ([exe], {}) if exe else (None, {})


def ffmpeg_cmd():
    own = _env("KOMBUESE_FFMPEG")
    if own:
        return shlex.split(own)
    exe = shutil.which("ffmpeg")
    return [exe] if exe else None


def _run(argv, timeout, env=None):
    e = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout, env=e, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise VideoError(f"{os.path.basename(argv[0])} took longer than {timeout} s") from None
    except OSError as ex:
        raise VideoError(f"{argv[0]} cannot be started: {ex}") from None


def ytdlp_version():
    cmd, env = ytdlp_cmd()
    if not cmd:
        return None
    try:
        r = _run(cmd + ["--version"], 30, env)
    except VideoError:
        return None
    return r.stdout.decode("utf-8", "replace").strip()[:40] or None if r.returncode == 0 else None


def ffmpeg_version():
    cmd = ffmpeg_cmd()
    if not cmd:
        return None
    try:
        r = _run(cmd + ["-hide_banner", "-version"], 20)
    except VideoError:
        return None
    m = re.search(r"ffmpeg version (\S+)", r.stdout.decode("utf-8", "replace"))
    return (m.group(1)[:40] if m else "?") if r.returncode == 0 else None


def update_ytdlp(force=False):
    """pip install -U yt-dlp into <data>/tools/yt-dlp (no root needed). → {ok, version, note}"""
    if _env("KOMBUESE_YTDLP"):
        return {"ok": False, "note": "KOMBUESE_YTDLP is set: update that command yourself", "version": ytdlp_version()}
    if _update["running"]:
        return {"ok": False, "note": "update already running", "version": ytdlp_version()}
    if not force and time.time() - _update["t"] < 12 * 3600:
        return {"ok": False, "note": "updated less than 12 h ago", "version": ytdlp_version()}
    _update.update(running=True, t=time.time())
    try:
        tmp = _tools_dir() + ".new"
        shutil.rmtree(tmp, ignore_errors=True)
        r = _run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir", "--quiet",
                  "--upgrade", "--target", tmp, "yt-dlp[default]"], 300)
        if r.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            note = r.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["pip failed"]
            _update["last"] = {"ok": False, "note": note[0][:300]}
            return {**_update["last"], "version": ytdlp_version()}
        old = _tools_dir() + ".old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.isdir(_tools_dir()):
            os.replace(_tools_dir(), old)
        os.makedirs(os.path.dirname(_tools_dir()), exist_ok=True)
        os.replace(tmp, _tools_dir())
        shutil.rmtree(old, ignore_errors=True)
        v = ytdlp_version()
        _update["last"] = {"ok": True, "note": f"yt-dlp {v}"}
        print(f"video: yt-dlp updated to {v}", flush=True)
        return {"ok": True, "version": v, "note": f"yt-dlp {v}"}
    finally:
        _update["running"] = False


def status():
    """For the settings page: programs and versions."""
    s = settings()
    cmd, _ = ytdlp_cmd()
    return {"ytdlp": ytdlp_version() if cmd else None, "ytdlp_own": os.path.isdir(os.path.join(_tools_dir(), "yt_dlp")),
            "ffmpeg": ffmpeg_version(), "whisper_api": _detected.get(s["whisper_url"]) if s["whisper_url"] else None,
            "last_update": _update["last"]}


# ---------- yt-dlp ----------

def _ytdlp_error(stderr, url):
    """yt-dlp's error output → a clear message for the household and the AI."""
    t = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else str(stderr)
    lines = t.strip().splitlines()
    last = next((x for x in reversed(lines) if x.startswith("ERROR")), lines[-1] if lines else "")
    last = re.sub(r"^ERROR:\s*", "", last)[:300]
    low = t.lower()
    if re.search(r"login|log in|sign in|cookies|rate-limit|empty media response|authentication|checkpoint", low):
        why = "the site wants a login or blocks downloads without one (Instagram does this often)"
    elif re.search(r"private|this video is unavailable|content isn.t available|not available|removed|deleted|404", low):
        why = "the video is private, deleted or not available in this region"
    elif "unsupported url" in low:
        why = "this link is not a video yt-dlp knows"
    elif re.search(r"geo|country", low):
        why = "the video is blocked in this country"
    else:
        why = "yt-dlp could not read the video (an update of yt-dlp may help, Settings → Video & Whisper)"
    return f"{why}. yt-dlp: {last}" if last else why


def _base_args():
    # YouTube needs a JavaScript runtime for its challenges; the image brings the small QuickJS instead of Deno
    js = ["--js-runtimes", "quickjs"] if shutil.which("qjs") and not shutil.which("deno") else []
    return ["--ignore-config", "--no-playlist", "--no-cache-dir", "--no-warnings", "--no-progress", *js,
            "--use-extractors", "default,-generic", "--socket-timeout", "20", "--retries", "2",
            "-f", "b[height<=720]/bv*[height<=720]+ba/b/bv*+ba"]


def _info(url, attempt_update=True):
    cmd, env = ytdlp_cmd()
    if not cmd:
        raise VideoError("yt-dlp is not installed (it is part of the Docker image; see docs/video.md)")
    r = _run(cmd + _base_args() + ["-J", "--", url], 120, env)
    if r.returncode != 0:
        err = _ytdlp_error(r.stderr, url)
        if attempt_update and settings()["autoupdate"] and not _env("KOMBUESE_YTDLP") and "login" not in err and "private" not in err:
            if update_ytdlp().get("ok"):
                return _info(url, attempt_update=False)
        raise VideoError(err)
    try:
        info = json.loads(r.stdout)
    except ValueError:
        raise VideoError("yt-dlp gave no readable answer") from None
    if info.get("_type") == "playlist":
        entries = [e for e in info.get("entries") or [] if e]
        if not entries:
            raise VideoError("the link is a playlist without videos")
        info = entries[0]
    return info


def _size(info):
    fmts = info.get("requested_formats") or [info]
    total = 0
    for f in fmts:
        total += int(f.get("filesize") or f.get("filesize_approx") or 0)
    return total


def _format_urls(info):
    return [f.get("url") for f in (info.get("requested_formats") or [info]) if f.get("url")]


def _download(info, folder, max_mb):
    cmd, env = ytdlp_cmd()
    for u in _format_urls(info):  # the file itself must come from the internet too (only http(s), public addresses)
        if u.startswith(("http://", "https://")):
            try:
                netsafe.check(u)
            except netsafe.Blocked as e:
                raise VideoError(f"download address refused: {e}") from None
    path = os.path.join(folder, "info.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(info, f)
    r = _run(cmd + _base_args() + ["--load-info-json", path, "--max-filesize", f"{max_mb}M", "--no-part",
                                   "--merge-output-format", "mp4", "-o", os.path.join(folder, "video.%(ext)s")], 600, env)
    files = [n for n in os.listdir(folder) if n.startswith("video.") and not n.endswith((".json", ".part", ".ytdl"))]
    if r.returncode != 0 or not files:
        out = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
        if "max-filesize" in out or "larger than max" in out.lower():
            raise VideoError(f"the video is larger than {max_mb} MB")
        raise VideoError(_ytdlp_error(r.stderr or r.stdout, ""))
    p = os.path.join(folder, sorted(files)[0])
    if os.path.getsize(p) > max_mb * 1024 * 1024:
        raise VideoError(f"the video is larger than {max_mb} MB")
    return p


# ---------- ffmpeg ----------

def _audio(video, folder):
    """→ (bytes, filename, mime): mono 16 kHz; MP3 if ffmpeg can, otherwise WAV."""
    ff = ffmpeg_cmd()
    if not ff:
        raise VideoError("ffmpeg is not installed")
    for name, mime, codec in (("audio.mp3", "audio/mpeg", ["-c:a", "libmp3lame", "-b:a", "48k"]),
                              ("audio.wav", "audio/wav", ["-c:a", "pcm_s16le"])):
        out = os.path.join(folder, name)
        r = _run(ff + ["-nostdin", "-hide_banner", "-v", "error", "-y", "-i", video, "-vn", "-ac", "1", "-ar", "16000", *codec, out], 300)
        if r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
            with open(out, "rb") as f:
                return f.read(), name, mime
        err = r.stderr.decode("utf-8", "replace")
        if re.search(r"does not contain any stream|matches no streams|Output file .* does not contain", err):
            raise VideoError("the video has no sound")
    raise VideoError("ffmpeg could not extract the sound: " + r.stderr.decode("utf-8", "replace").strip()[-200:])


def _frames(video, folder, duration):
    """Up to MAX_FRAMES stills spread over the video → [(seconds, jpeg bytes)]."""
    ff = ffmpeg_cmd()
    if not ff:
        return []
    d = float(duration or 0)
    step = max(1.5, d / MAX_FRAMES) if d else 3.0
    scale = f"scale='if(gt(iw,ih),{FRAME_SIDE},-2)':'if(gt(iw,ih),-2,{FRAME_SIDE})'"
    r = _run(ff + ["-nostdin", "-hide_banner", "-v", "error", "-y", "-ss", f"{step / 2:.2f}", "-i", video, "-an",
                   "-vf", f"fps=1/{step:.3f},{scale}", "-frames:v", str(MAX_FRAMES), "-q:v", "5",
                   os.path.join(folder, "f_%02d.jpg")], 240)
    out = []
    for i in range(1, MAX_FRAMES + 1):
        p = os.path.join(folder, f"f_{i:02d}.jpg")
        if not os.path.isfile(p):
            break
        with open(p, "rb") as f:
            data = f.read()
        if photos.mime(data) == "image/jpeg":
            out.append((round(step / 2 + (i - 1) * step, 1), data))
    if not out and r.returncode != 0:
        print(f"video: no stills: {r.stderr.decode('utf-8', 'replace')[-200:]}", flush=True)
    return out


# ---------- Whisper ----------

def _multipart(fields, files):
    boundary = "kombuese" + uuid.uuid4().hex
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, (name, mime, data) in files.items():
        buf.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\nContent-Type: {mime}\r\n\r\n'.encode())
        buf.write(data)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def _openai_url(base):
    base = base.rstrip("/")
    if base.endswith("/audio/transcriptions"):
        return base
    return base + ("/audio/transcriptions" if base.endswith("/v1") else "/v1/audio/transcriptions")


def detect_api(s):
    """auto: look at the service's OpenAPI description (both kinds are FastAPI apps). → "openai" or "asr"."""
    url = s["whisper_url"]
    if s["whisper_api"] in ("openai", "asr"):
        return s["whisper_api"]
    if url in _detected:
        return _detected[url]
    api = "openai"
    if not re.search(r"/v1/?$|/audio/transcriptions$|api\.openai\.com", url):
        try:
            with urllib.request.urlopen(urllib.request.Request(url + "/openapi.json", headers={"User-Agent": UA}), timeout=10) as r:
                paths = (json.loads(r.read(2 * 1024 * 1024)) or {}).get("paths") or {}
            if "/asr" in paths and "/v1/audio/transcriptions" not in paths:
                api = "asr"
        except (OSError, ValueError, urllib.error.URLError):
            pass  # not reachable now: guess openai, ask again next time
        else:
            _detected[url] = api
    else:
        _detected[url] = api
    return api


def transcribe(audio, name, mime, s, language=None, timeout=900):
    """→ {text, language, api}. Raises VideoError when the service is not set up, down or answers with an error."""
    url = s["whisper_url"]
    if not url:
        raise VideoError("no Whisper service set up (Settings → Video & Whisper)")
    api = detect_api(s)
    lang = language if language and language != "auto" else None
    h = {"User-Agent": UA}
    if s.get("whisper_key"):
        h["Authorization"] = "Bearer " + s["whisper_key"]
    if api == "asr":
        q = {"task": "transcribe", "output": "json", "encode": "true", **({"language": lang} if lang else {})}
        target = url + "/asr?" + urllib.parse.urlencode(q)
        body, ctype = _multipart({}, {"audio_file": (name, mime, audio)})
    else:
        target = _openai_url(url)
        fields = {"model": s.get("whisper_model") or "whisper-1", "response_format": "json"}
        if lang:
            fields["language"] = lang
        body, ctype = _multipart(fields, {"file": (name, mime, audio)})
    req = urllib.request.Request(target, data=body, method="POST", headers={**h, "Content-Type": ctype, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(8 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        msg = e.read(300).decode("utf-8", "replace")
        if e.code in (401, 403):
            raise VideoError(f"Whisper refused the key (HTTP {e.code})") from None
        if e.code in (404, 405) and s["whisper_api"] == "auto":
            _detected.pop(url, None)
        raise VideoError(f"Whisper answered HTTP {e.code}: {msg}") from None
    except (urllib.error.URLError, OSError) as e:
        raise VideoError(f"Whisper not reachable at {url}: {getattr(e, 'reason', e)}") from None
    try:
        d = json.loads(raw)
        text = d.get("text") if isinstance(d, dict) else None
        if text is None and isinstance(d, dict) and d.get("segments"):
            text = " ".join(str(x.get("text") or "") for x in d["segments"])
        detected = d.get("language") if isinstance(d, dict) else None
    except ValueError:
        text, detected = raw.decode("utf-8", "replace"), None
    return {"text": re.sub(r"[ \t]+", " ", str(text or "")).strip(), "language": detected or lang, "api": api}


def test_sound():
    """The built-in test: 2 s WAV, mono 16 kHz (a soft rising tone), made here instead of shipping a file."""
    rate, out = 16000, io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * (300 + 100 * i / rate) * i / rate)))
                               for i in range(2 * rate)))
    return out.getvalue()


def test(s=None):
    """Settings → Video & Whisper → Test: sends the 2 s test sound to Whisper. → {ok, api, seconds, text | error}"""
    s = s or settings(with_key=True)
    t0 = time.time()
    lang = household.language() if s["language"] == "household" else s["language"]
    try:
        r = transcribe(test_sound(), "kombuese-test.wav", "audio/wav", s, lang, timeout=120)
    except VideoError as e:
        return {"ok": False, "error": str(e), "seconds": round(time.time() - t0, 1)}
    return {"ok": True, "api": r["api"], "seconds": round(time.time() - t0, 1), "text": r["text"][:200]}


# ---------- stills for the recipe photo ----------

def _dir():
    return os.path.join(store.data_dir(), "videoimport")


def _cleanup():
    d = _dir()
    if not os.path.isdir(d):
        return
    for n in os.listdir(d):
        p = os.path.join(d, n)
        try:
            if time.time() - os.path.getmtime(p) > KEEP:
                os.remove(p)
        except OSError:
            pass


def _keep(frames, thumb):
    """Store thumbnail (0) and stills (1 …) → key."""
    _cleanup()
    os.makedirs(_dir(), exist_ok=True)
    key = secrets.token_hex(8)
    for nr, data in ([(0, thumb)] if thumb else []) + [(i, d) for i, (_, d) in enumerate(frames, 1)]:
        ext = photos.EXT.get(photos.mime(data) or "", "")
        if ext:
            with open(os.path.join(_dir(), f"{key}-{nr}.{ext}"), "wb") as f:
                f.write(data)
    return key


def frame_file(key, nr):
    """(path, mime) of still nr (0 = thumbnail) of a read video, or None."""
    if not re.fullmatch(r"[0-9a-f]{16}", str(key or "")):
        return None
    for mime, ext in photos.EXT.items():
        p = os.path.join(_dir(), f"{key}-{int(nr)}.{ext}")
        if os.path.isfile(p):
            return p, mime
    return None


def _thumbnail(info):
    urls = [info.get("thumbnail")] + [t.get("url") for t in sorted(info.get("thumbnails") or [], key=lambda t: -(t.get("preference") or 0))]
    for u in [u for u in urls if isinstance(u, str) and u.startswith("http")][:3]:
        try:
            data = netsafe.open_url(u, {"User-Agent": UA, "Accept": "image/*"}, timeout=20, max_bytes=photos.MAX_BYTES)[0]
        except (netsafe.Blocked, urllib.error.URLError, OSError, ValueError):
            continue
        if photos.mime(data) in ("image/jpeg", "image/png", "image/webp"):
            return data
    return None


# ---------- the whole thing ----------

def read(url, s=None, household_lang="en"):
    """A video link → dict with title, uploader, link, duration, description, transcript, notes, frames
    [(seconds, jpeg)], thumbnail (bytes or None) and key (for POST /api/videoimport/bild). Raises VideoError."""
    s = s or settings(with_key=True)
    if not s["enabled"]:
        raise VideoError("video import is switched off (Settings → Video & Whisper)")
    url = check_url(url)
    ck = (store.household_id(), url)
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < 1800 and frame_file(hit[1]["key"], 1 if hit[1]["frames"] else 0) is not None:
        return hit[1]
    if not _sem.acquire(timeout=600):
        raise VideoError("too many videos at the same time, try again in a few minutes")
    folder = tempfile.mkdtemp(prefix="kombuese-video-")
    try:
        info = _info(url)
        dur = info.get("duration")
        res = {"title": str(info.get("title") or info.get("fulltitle") or "")[:300],
               "uploader": str(info.get("uploader") or info.get("channel") or info.get("creator") or info.get("uploader_id") or "")[:200],
               "uploader_url": str(info.get("uploader_url") or info.get("channel_url") or "")[:300],
               "link": str(info.get("webpage_url") or url)[:800], "platform": platform(url) or info.get("extractor_key") or "",
               "duration": round(float(dur)) if isinstance(dur, (int, float)) else None,
               "description": str(info.get("description") or "")[:15000], "transcript": None, "language": None,
               "notes": [], "frames": [], "thumbnail": None, "key": None}
        res["thumbnail"] = _thumbnail(info)
        video = None
        size = _size(info)
        if res["duration"] and res["duration"] > s["max_minutes"] * 60:
            res["notes"].append(f"not downloaded: the video is {res['duration'] // 60} min long, the limit is {s['max_minutes']} min")
        elif size and size > s["max_mb"] * 1024 * 1024:
            res["notes"].append(f"not downloaded: the video has about {size // (1024 * 1024)} MB, the limit is {s['max_mb']} MB")
        elif info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            res["notes"].append("not downloaded: live stream")
        else:
            try:
                video = _download(info, folder, s["max_mb"])
            except VideoError as e:
                res["notes"].append(f"download failed: {e}")
        if video:
            res["frames"] = _frames(video, folder, res["duration"])
            try:
                audio, name, mime = _audio(video, folder)
                lang = s["language"]
                lang = household_lang if lang == "household" else lang
                t = transcribe(audio, name, mime, s, lang)
                res["transcript"], res["language"] = t["text"], t["language"]
                if not t["text"]:
                    res["notes"].append("the transcript is empty (music only, or nobody speaks)")
            except VideoError as e:
                res["notes"].append(f"no transcript: {e}")
        res["key"] = _keep(res["frames"], res["thumbnail"])
        _cache[ck] = (time.time(), res)
        for k in [k for k, v in _cache.items() if time.time() - v[0] > 1800]:
            _cache.pop(k, None)
        return res
    finally:
        _sem.release()
        shutil.rmtree(folder, ignore_errors=True)


def tool_text(r, with_frames):
    """The tool answer for the AI (text part)."""
    dur = f"{r['duration'] // 60}:{r['duration'] % 60:02d} min" if r["duration"] else "unknown"
    lines = [f"Video: {r['title'] or '(no title)'}", f"Platform: {r['platform'] or '?'}, duration {dur}",
             f"From: {r['uploader'] or 'unknown'}" + (f" ({r['uploader_url']})" if r["uploader_url"] else ""),
             f"Link (use as quelle): {r['link']}", "", "Description (written by the uploader):", r["description"] or "(none)", ""]
    if r["transcript"]:
        lines += [f"Transcript of the spoken words (Whisper{', language ' + r['language'] if r['language'] else ''}; "
                  "names and amounts may be misheard):", r["transcript"][:30000], ""]
    else:
        lines += ["Transcript: not available.", ""]
    if r["notes"]:
        lines += ["Notes: " + "; ".join(r["notes"]), ""]
    n = len(r["frames"])
    if n:
        when = ", ".join(f"{i}: {t:g} s" for i, (t, _) in enumerate(r["frames"], 1))
        lines.append(f"Stills: {n} ({when})." + (" They follow as images, in order." if with_frames else
                                                 " Not shown in this answer."))
    else:
        lines.append("Stills: none.")
    if r["key"] and (n or r["thumbnail"]):
        lines.append(f"Recipe photo: POST /api/videoimport/bild {{\"slug\": \"<slug>\", \"video\": \"{r['key']}\", \"bild\": <still number>}}"
                     + (", bild 0 = the video's cover image (thumbnail)" if r["thumbnail"] else ", no cover image available")
                     + ". Valid for 24 h.")
    return "\n".join(lines)


def tool(i, household_lang="en"):
    """Handler of the tool read_video (jobs.py, mcpserver.py) → {"_content": [MCP text and image blocks]}."""
    with_frames = i.get("frames") is not False and str(i.get("frames")).lower() != "false"
    try:
        r = read(i.get("url"), household_lang=household_lang)
    except VideoError as e:
        raise RuntimeError(str(e)) from None
    blocks = [{"type": "text", "text": tool_text(r, with_frames)}]
    if with_frames:
        for nr, (t, data) in enumerate(r["frames"], 1):
            blocks += [{"type": "text", "text": f"Still {nr} ({t:g} s):"},
                       {"type": "image", "data": base64.b64encode(data).decode(), "mimeType": "image/jpeg"}]
    return {"_content": blocks}


def save_frame(b):
    """POST /api/videoimport/bild {slug, video, bild}: still bild (0 = thumbnail) of a read video becomes the recipe photo."""
    try:
        nr = int(b.get("bild"))
    except (TypeError, ValueError):
        raise Error("bild: Nummer des Standbilds (0 = Vorschaubild)") from None
    f = frame_file(b.get("video"), nr)
    if not f:
        raise Error("kein Standbild mit dieser Nummer (oder älter als 24 h)")
    with open(f[0], "rb") as fh:
        return store.save_image(str(b.get("slug") or ""), fh.read())
