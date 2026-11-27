"""Yazio for Kombüse: log eaten meals as quick entries (name + nutrients),
delete them again and read the daily totals.

Yazio has no official API. We use the internal app interface (v15), like the
rebuilt clients fliptheweb/yazio-mcp and itzptk/yazio-go-cli. It can break at any time.
Login: Settings → Connections, otherwise yazio.json in the secrets directory, {"email": "...", "password": "..."}"""
import json
import threading
import time
import uuid

import shopping
import store
import integrations

BASE = "https://yzapi.yazio.com/v15"
# publicly known client id of the Yazio app (in every rebuilt client), not a personal secret
CLIENT = {"client_id": "1_4hiybetvfksgw40o0sog4s884kwc840wwso8go4k8c04goo4c",
          "client_secret": "6rok2m65xuskgkgogw40wkkk8sw0osg84s8cggsc4woos4s8o"}
DAYTIME = {"breakfast": "breakfast", "lunch": "lunch", "dinner": "dinner"}  # everything else: snack

_lock = threading.Lock()
_tok = store.PerHousehold()  # per household


def configured():
    return bool(integrations.get("yazio"))


def _login():
    c = integrations.get("yazio")
    if not c:
        raise shopping.Error("Yazio ist nicht verbunden (⚙️ → Verbindungen)")
    r = None
    if _tok.get("refresh") and time.time() < _tok.get("refresh_until", 0):
        try:
            r = shopping._http("POST", f"{BASE}/oauth/token", data={**CLIENT, "grant_type": "refresh_token",
                                                                    "refresh_token": _tok["refresh"]})
        except shopping.Error:
            r = None
    if not r:
        r = shopping._http("POST", f"{BASE}/oauth/token", data={**CLIENT, "grant_type": "password",
                                                                "username": c["email"], "password": c["password"]})
    _tok.update(access=r["access_token"], typ=(r.get("token_type") or "Bearer").capitalize(),
                until=time.time() + int(r.get("expires_in", 3600)) - 300,
                refresh=r.get("refresh_token"), refresh_until=time.time() + 30 * 86400)


def _y(method, path, data=None):
    with _lock:
        if not _tok.get("access") or time.time() > _tok.get("until", 0):
            _login()
        h = {"Authorization": f"{_tok['typ']} {_tok['access']}"}
    return shopping._http(method, BASE + path, h, data=data)


def log_meal(name, date, meal_type, nutrients):
    """Quick entry (simple_products). nutrients: kcal, protein, fett, kh as totals of the amount eaten."""
    nid = str(uuid.uuid4())
    n = {"energy.energy": round(nutrients["kcal"], 1)}
    for k, y in (("protein", "nutrient.protein"), ("fett", "nutrient.fat"), ("kh", "nutrient.carb")):
        if nutrients.get(k) is not None:
            n[y] = round(nutrients[k], 1)
    _y("POST", "/user/consumed-items", {"products": [], "recipe_portions": [], "simple_products": [
        {"id": nid, "date": date, "daytime": DAYTIME.get(meal_type, "snack"), "name": name[:100], "nutrients": n}]})
    return nid


def delete(ids):
    _y("DELETE", "/user/consumed-items", list(ids))


def day(date):
    """Eaten and goal for one day. Adds up the totals from the entries itself where possible."""
    out = {"datum": date}
    try:
        g = _y("GET", f"/user/goals?date={date}") or {}
        out["ziel"] = {"kcal": g.get("energy.energy"), "protein": g.get("nutrient.protein"),
                       "fett": g.get("nutrient.fat"), "kh": g.get("nutrient.carb")}
    except shopping.Error as e:
        out["ziel_fehler"] = str(e)
    try:
        d = _y("GET", f"/user/consumed-items/nutrients-daily?start={date}&end={date}")
        row = d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else {})
        out["gegessen"] = {"kcal": row.get("energy") or row.get("energy.energy"), "protein": row.get("protein") or row.get("nutrient.protein"),
                           "fett": row.get("fat") or row.get("nutrient.fat"), "kh": row.get("carb") or row.get("nutrient.carb")}
        out["roh"] = row if not out["gegessen"]["kcal"] else None
    except shopping.Error as e:
        out["gegessen_fehler"] = str(e)
    return out
