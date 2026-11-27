"""Connections to other services, set up in the web UI under Settings → Connections.

bring     Bring! shopping list (unofficial API): email, password, list (part of the list name, optional)
yazio     Yazio food diary (unofficial API): email, password
telegram  notifications via a Telegram bot: bot_token, chat_id
ntfy      notifications via ntfy: server (default https://ntfy.sh), topic, token (optional)

Stored encrypted in the database (secretbox.py). For older installations, bring.json and yazio.json in
KOMBUESE_SECRETS still work as long as nothing is saved in the UI (household 1 only). Passwords and tokens never leave the server.
Each household keeps its own connections (in its own database).
"""
import json
import os
import re

import config
import store
import secretbox
from store import Error

FIELDS = {
    "bring": {"email": True, "password": True, "list": False},
    "yazio": {"email": True, "password": True},
    "telegram": {"bot_token": True, "chat_id": True},
    "ntfy": {"server": False, "topic": True, "token": False},
}
SECRET = {"password", "bot_token", "token"}
FILES = {"bring": "bring.json", "yazio": "yazio.json"}


def init():
    with store.con() as c:
        c.execute("CREATE TABLE IF NOT EXISTS integrations (name TEXT PRIMARY KEY, data TEXT NOT NULL, updated TEXT)")


def _db(name):
    with store.con() as c:
        r = c.execute("SELECT data FROM integrations WHERE name=?", (name,)).fetchone()
    return json.loads(secretbox.decrypt(r["data"])) if r else None


def _file(name):
    f = FILES.get(name)
    if not f or store.household_id() != 1:  # the old files belong to the first household
        return None
    try:
        with open(os.path.join(config.SECRETS, f), encoding="utf-8-sig") as fh:
            return json.loads(fh.read())
    except (OSError, ValueError):
        return None


def get(name):
    """Settings of a connection (with secrets) or None."""
    return _db(name) or _file(name)


def overview():
    out = {}
    for name, fields in FIELDS.items():
        d, src = _db(name), "db"
        if not d:
            d, src = _file(name), "file"
        out[name] = {"configured": bool(d), "source": src if d else None,
                     **{k: (bool((d or {}).get(k)) if k in SECRET else (d or {}).get(k, "")) for k in fields}}
    return out


def save(name, data):
    if name not in FIELDS:
        raise Error("Unbekannte Verbindung")
    old = _db(name) or _file(name) or {}
    new = {}
    for k, required in FIELDS[name].items():
        v = str(data.get(k) or "").strip()[:500]
        if not v and k in SECRET:
            v = old.get(k, "")  # empty = unchanged
        if required and not v:
            raise Error(f"{k} fehlt")
        if v:
            new[k] = v
    if name == "ntfy":
        new["server"] = (new.get("server") or "https://ntfy.sh").rstrip("/")
        if not re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", new["server"]):
            raise Error("Server: Adresse wie https://ntfy.sh")
        if not re.fullmatch(r"[\w-]{1,64}", new["topic"]):
            raise Error("Topic: Buchstaben, Zahlen, - und _")
    with store.lock, store.con() as c:
        c.execute("INSERT INTO integrations(name, data, updated) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET data=excluded.data, "
                  "updated=excluded.updated", (name, secretbox.encrypt(json.dumps(new)), store.now()))
    return {"ok": True}


def delete(name):
    with store.lock, store.con() as c:
        c.execute("DELETE FROM integrations WHERE name=?", (name,))
    return {"ok": True, "file_fallback": bool(_file(name))}
