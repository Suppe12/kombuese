"""Notifications: Telegram bot and/or ntfy, configured under Settings → Connections; also Web Push to the
devices of the household that switched it on (Settings → Notifications, push.py).

POST /api/notify {"text": "...", "title": "...", "view": "week"} sends to every configured channel (view: where a tap on a
Web Push notification opens the app, optional). AI runs and scripts use it with an
API token, so they don't need their own messenger credentials.
"""
import urllib.error
import urllib.parse
import urllib.request

import integrations
import push
from store import Error

UA = "Kombuese (self-hosted kitchen app)"


def channels():
    return [n for n in ("telegram", "ntfy") if integrations.get(n)] + (["webpush"] if push.count() else [])


def _post(url, data, headers):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise Error(f"HTTP {e.code} {e.read()[:150].decode('utf-8', 'replace')}") from None
    except urllib.error.URLError as e:
        raise Error(f"nicht erreichbar: {e.reason}") from None


def _telegram(cfg, text, title):
    msg = f"{title}\n{text}" if title else text
    _post(f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
          urllib.parse.urlencode({"chat_id": cfg["chat_id"], "text": msg[:4000], "disable_web_page_preview": "true"}).encode(),
          {"Content-Type": "application/x-www-form-urlencoded"})


def _ntfy(cfg, text, title):
    h = {"Content-Type": "text/plain; charset=utf-8"}
    if title:
        h["Title"] = title.encode("utf-8").decode("latin-1", "ignore") or "Kombuese"
    if cfg.get("token"):
        h["Authorization"] = "Bearer " + cfg["token"]
    _post(f"{cfg.get('server') or 'https://ntfy.sh'}/{cfg['topic']}", text.encode()[:4000], h)


SENDERS = {"telegram": _telegram, "ntfy": _ntfy}


def send(text, title="", only=None, view=""):
    text = str(text or "").strip()
    if not text:
        raise Error("Text fehlt")
    sent, errors = [], {}
    for name in [only] if only else channels():
        if name == "webpush":  # devices of this household (push.py); errors of single devices stay with the device
            try:
                push.send(text, str(title or "")[:120], view)
                sent.append(name)
            except Error as e:
                errors[name] = str(e)
            continue
        cfg = integrations.get(name)
        if not cfg:
            errors[name] = "nicht eingerichtet"
            continue
        try:
            SENDERS[name](cfg, text, str(title or "")[:120])
            sent.append(name)
        except (Error, KeyError) as e:
            errors[name] = str(e)
    return {"sent": sent, "errors": errors}


def test(name):
    r = send("Testnachricht aus der Kombüse ✓", "Kombüse", only=name)
    if r["errors"]:
        raise Error(f"{name}: {r['errors'][name]}")
    return {"ok": True}
