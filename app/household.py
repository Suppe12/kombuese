"""Household settings (Settings → Household): the household language and a description.

language     language of recipes, suggestions and AI runs ("de" or "en"); also the starting language of the app on
             new devices (each device can still switch under Settings). New recipes are stored with it.
description  free text for the AI runs: people, office days, shopping and prep day, kitchen equipment …
people       number of people the household cooks for (servings); null = the number of members (households.py)
microwave_watt  power of the household's microwave: reheating times are converted to it (reheat.py), default 800

Stored as JSON in the meta table (key "household") of each household. Older versions kept language and description in
Settings → AI and are taken over from there.
"""
import json

import store

LANGS = ("de", "en")
NAMES = {"de": "German", "en": "English"}
LEFTOVER = {"de": "Rest", "en": "Leftovers"}  # title prefix of leftover entries in the plan ("Rest: Chili")


def _load():
    with store.con() as c:
        r = c.execute("SELECT value FROM meta WHERE key='household'").fetchone()
    return json.loads(r["value"]) if r else None


def init():
    if _load() is not None:
        return
    old = {}
    try:  # settings from Settings → AI (2.2)
        import ai
        s = ai.stored()
        old = {"language": s.get("language") or "", "description": s.get("household") or ""}
    except Exception:  # noqa: BLE001
        pass
    with store.con() as c:  # an installation with older recipes is German (the app was German only)
        has_recipes = c.execute("SELECT 1 FROM rezept LIMIT 1").fetchone() is not None
    lang = old.get("language") if old.get("language") in LANGS else ("de" if has_recipes else "en")
    save({"language": lang, "description": old.get("description") or ""})


def get():  # read every time: admin.py demo or a restore may change it from another process
    h = _load() or {}
    return {"language": h.get("language") if h.get("language") in LANGS else "en",
            "description": str(h.get("description") or ""), "people": _people(h.get("people")),
            "microwave_watt": _watt(h.get("microwave_watt"))}


def _people(v):
    try:
        return max(1, min(30, int(v))) if v not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        return None


def _watt(v):
    import reheat
    return reheat._watt(v) if v not in (None, "") else reheat.DEFAULT_WATT


def microwave_watt():
    return get()["microwave_watt"]


def language():
    return get()["language"]


def save(b):
    h = get() if _load() is not None else {"language": "en", "description": "", "people": None}
    if b.get("language") in LANGS:
        h["language"] = b["language"]
    if "description" in b:
        h["description"] = str(b["description"] or "").strip()[:3000]
    if "people" in b:
        h["people"] = _people(b["people"])
    if "microwave_watt" in b:
        h["microwave_watt"] = _watt(b["microwave_watt"])
    with store.con() as c:
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('household',?)", (json.dumps(h, ensure_ascii=False),))
    return get()


def leftover_prefix(lang=None):
    return LEFTOVER.get(lang or language(), "Rest")


def strip_leftover(title):
    """'Rest: Chili (eingefroren)' / 'Leftovers: Chili' → 'Chili'."""
    import re
    t = re.sub(r"^(?:rest|reste|leftovers?)\s*:\s*", "", str(title or "").strip(), flags=re.I)
    return re.sub(r"\s*\((?:eingefroren|frozen)\)\s*$", "", t, flags=re.I).strip()
