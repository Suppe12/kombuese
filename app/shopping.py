"""Shopping for Kombüse: the list itself lives in store.py; this module sends the rest to Bring! and reads it back.

Login: Settings → Connections (integrations.py), or the old file in the secrets directory (see config.py):
  /secrets/bring.json     {"email": "...", "password": "..."}
Bring has no official API. We use the same interface as the web app and the
Home Assistant integration (package bring-api).
ALWAYS_DEFAULT: staples that never go on the list (meta key immer_da holds the household's own version)."""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import config
import integrations
import store
import pantry

SECRETS = config.SECRETS
BRING = "https://api.getbring.com/rest/v2"
BRING_HEADERS = {  # like the Bring web app
    "X-BRING-API-KEY": "cof4Nc6D8saplXjE3h3HXqHH8m7VU2i1Gs0g85Sp",
    "X-BRING-CLIENT": "webApp",
    "X-BRING-APPLICATION": "bring",
    "X-BRING-COUNTRY": "DE",
    "User-Agent": "Mozilla/5.0 (kueche-app; private home server)",
}

_lock = threading.Lock()
_bring = store.PerHousehold()  # uuid, token, expires, list_uuid, list_name (per household)


Error = store.Error


def _secret(name):
    try:
        with open(os.path.join(SECRETS, name), encoding="utf-8-sig") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def configured():
    return {"mealie": True, "bring": bool(integrations.get("bring"))}  # "mealie" = list available (old name kept for the app)


def _http(method, url, headers=None, data=None, form=None, timeout=20):
    h = dict(headers or {})
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    elif data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        raise Error(f"{method} {url.split('?')[0]}: HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise Error(f"{method} {url.split('?')[0]}: {e.reason}") from None


# ---------- list ----------
shopping_list = store.shopping_list
check_item = store.check_item
# ---------- Bring ----------
def _bring_login():
    c = integrations.get("bring")  # from the settings (Connections), otherwise bring.json
    if not c:
        raise Error("Bring ist nicht verbunden (⚙️ → Verbindungen)")
    r = _http("POST", f"{BRING}/bringauth", BRING_HEADERS, form={"email": c["email"], "password": c["password"]})
    _bring.update(uuid=r["uuid"], token=r["access_token"], expires=time.time() + int(r.get("expires_in", 3600)) - 300,
                  default_list=r.get("bringListUUID"))
    lists = _http("GET", f"{BRING}/bringusers/{r['uuid']}/lists", _bh())["lists"]
    want = (c.get("list") or "einkauf").lower()
    pick = next((l for l in lists if want in l["name"].lower()), None) \
        or next((l for l in lists if l["listUuid"] == _bring["default_list"]), lists[0] if lists else None)
    if not pick:
        raise Error("Bring: keine Liste gefunden")
    _bring.update(list_uuid=pick["listUuid"], list_name=pick["name"], lists=[l["name"] for l in lists])


def _bh():
    return {**BRING_HEADERS, "Authorization": f"Bearer {_bring['token']}", "X-BRING-USER-UUID": _bring["uuid"]}


def _bring_ready():
    if not _bring.get("token") or time.time() > _bring.get("expires", 0):
        _bring_login()


def bring_test():
    with _lock:
        _bring.clear()
        _bring_ready()
        cur = _http("GET", f"{BRING}/bringlists/{_bring['list_uuid']}", _bh())
        return {"ok": True, "liste": _bring["list_name"], "alle_listen": _bring["lists"],
                "artikel": len(cur.get("purchase") or [])}


def send_to_bring():
    """Send all unchecked items to Bring and empty the list afterwards.
        Whatever did not reach Bring stays on the list."""
    with _lock:
        all_items = store.shopping_list()["items"]
        todo = [i for i in all_items if not i["checked"]]
        _bring_ready()
        cur = _http("GET", f"{BRING}/bringlists/{_bring['list_uuid']}", _bh())
        in_bring = {p["name"].lower(): p.get("specification") or "" for p in cur.get("purchase") or []}
        changes = []
        for it in todo:
            if not it["name"]:
                continue
            spec = ", ".join(x for x in (it["menge"], it["note"]) if x)
            old = in_bring.get(it["name"].lower())
            if old and spec and spec not in old:
                spec = f"{old} + {spec}"
            elif old and not spec:
                spec = old
            in_bring[it["name"].lower()] = spec
            changes.append({"accessibility": "", "itemId": it["name"], "spec": spec[:250], "uuid": str(uuid.uuid4()),
                            "operation": "TO_PURCHASE"})
        if changes:
            _http("PUT", f"{BRING}/bringlists/{_bring['list_uuid']}/items", _bh(), data={"changes": changes, "sender": ""})
        # check: is everything in Bring?
        now_listed = {p["name"].lower() for p in (_http("GET", f"{BRING}/bringlists/{_bring['list_uuid']}", _bh()).get("purchase") or [])}
        missing = [c["itemId"] for c in changes if c["itemId"].lower() not in now_listed]
        removed = [i["id"] for i in all_items if i["checked"] or i["name"] not in missing]
        # pantry: remember shelf-stable items; they are booked once checked off in Bring (pantry.check_purchases)
        pantry.remember_sent([{"name": i["name"], "menge": i["menge_zahl"], "einheit": i["einheit"]}
                                  for i in todo if i["name"] and i["name"] not in missing])
        store.remove_items(removed)
        return {"gesendet": len(changes) - len(missing), "fehlt": missing, "hatte_ich": sum(1 for i in all_items if i["checked"]),
                "geloescht": len(removed), "liste": _bring["list_name"]}


# ---------- always at home, never goes on the list ----------
ALWAYS_DEFAULT = {"namen": ["Salz", "Schwarzer Pfeffer", "Pfeffer", "Wasser", "Zucker", "Backpulver", "Maisstärke",
                            "Speisestärke", "Gemüsebrühe"],
                  "kategorien": ["Gewürze", "Öle"]}


def bring_state():
    """Bring list with purchase (open) and recently (recently checked off), for the pantry."""
    with _lock:
        _bring_ready()
        return _http("GET", f"{BRING}/bringlists/{_bring['list_uuid']}", _bh())
