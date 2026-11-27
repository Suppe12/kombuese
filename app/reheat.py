"""Reheating in the microwave: the optional recipe field aufwaermen, the rules for more portions and
another microwave, and an estimate from the kind of dish when a recipe has no field yet.

Field (per portion, about one box of 350–450 g), everything optional:
  aufwaermen = {"kuehlschrank": {"watt": 800, "minuten": 2.5, "hinweis": "stir halfway"},   from the fridge
                "gefroren":     {"watt": 800, "minuten": 6,   "hinweis": "covered, stir twice"}, straight from the freezer
                "ruhen": 1,                       minutes standing time after the microwave
                "alternative": "pot over medium heat, 5 min with a splash of water",   oven or pan instead
                "hinweis": "add a little water",  for both
                "kalt": false}                    eaten cold (overnight oats, salads): no times
  minuten may be sent as a number (2.5), "2:30" or "2,5"; it is stored as a number of minutes.

Rules (the same in the app's frontend, index.html rhTime):
  more portions at once: every extra portion +70 % time (2 portions = 1.7 ×, 3 = 2.4 ×); from 3 portions on a pot
      or the oven is usually better.
  another microwave:     time × recommended watt / own watt (household setting microwave_watt, default 800 W).
  rounded to 15 seconds, at least 15 seconds.
"""
import math
import re

DEFAULT_WATT = 800
WATTS = (600, 700, 750, 800, 850, 900, 1000, 1100, 1200)
EXTRA = 0.7  # time per extra portion
MODES = ("kuehlschrank", "gefroren")


def _minutes(v):
    """2.5, "2,5", "2:30", "2:30 min", "90 s", "150 sec" → minutes (float) or None."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        m = float(v)
    else:
        t = str(v).strip().lower().replace(",", ".")
        mm = re.fullmatch(r"(\d{1,2}):([0-5]\d)\s*(?:min\w*)?", t)
        ms = re.fullmatch(r"(\d{1,4})\s*(?:s|sec|sek|seconds?|sekunden)", t)
        mn = re.fullmatch(r"(\d{1,3}(?:\.\d+)?)\s*(?:m|min\w*)?", t)
        if mm:
            m = int(mm.group(1)) + int(mm.group(2)) / 60
        elif ms:
            m = int(ms.group(1)) / 60
        elif mn:
            m = float(mn.group(1))
        else:
            return None
    if not math.isfinite(m) or m <= 0:
        return None
    return round(min(m, 60.0), 2)


def _watt(v, default=DEFAULT_WATT):
    try:
        w = int(float(str(v).replace(",", ".").split()[0]))
    except (TypeError, ValueError, IndexError):
        return default
    return w if 100 <= w <= 2000 else default


def _text(v, n):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def normalize(v):
    """The stored shape; {} when nothing useful is in it."""
    if not isinstance(v, dict):
        return {}
    out = {}
    for k in MODES:
        m = v.get(k)
        if isinstance(m, dict):
            mins = _minutes(m.get("minuten") if m.get("minuten") not in (None, "") else m.get("zeit"))
            if mins:
                out[k] = {"watt": _watt(m.get("watt")), "minuten": mins, "hinweis": _text(m.get("hinweis"), 200)}
    rest = _minutes(v.get("ruhen"))
    if rest and out:
        out["ruhen"] = min(rest, 15.0)
    for k, n in (("alternative", 300), ("hinweis", 300)):
        if _text(v.get(k), n):
            out[k] = _text(v.get(k), n)
    if v.get("kalt") is True or str(v.get("kalt")).lower() in ("1", "true", "ja", "yes"):
        out["kalt"] = True
    return out


def seconds(minutes, portions=1, watt_from=DEFAULT_WATT, watt_to=DEFAULT_WATT):
    """Time for this many portions in a microwave with watt_to, from the recommendation per portion at watt_from."""
    try:
        n = max(1.0, float(portions or 1))
    except (TypeError, ValueError):
        n = 1.0
    s = float(minutes) * 60 * (1 + EXTRA * (n - 1)) * _watt(watt_from) / _watt(watt_to)
    return max(15, int(round(s / 15.0)) * 15)


def clock(sec):
    """150 → "2:30"."""
    return f"{sec // 60}:{sec % 60:02d}"


# ---------- estimate from the kind of dish (no AI) ----------
# (pattern on name, categories and tags, fridge minutes, frozen minutes, hint key, alternative key, standing minutes)
RULES = [
    (r"overnight|bircher|chia|müsli|muesli|granola|salat\b|salad|bowl.*kalt|gazpacho", None, None, "cold", None, 0),
    (r"muffin|egg ?bite|eiermuffin", 1.0, 2.0, "muffin", None, 0),
    (r"brot\b|brötchen|bread|bagel|scone|plätzchen|keks|cookie|kuchen|cake|brownie|riegel|\bbar\b|granola bar", None, None, "bakery", None, 0),
    (r"burrito(?![\s-]*bowl)|wrap|quesadilla|tortilla", 1.5, 3.5, "burrito", "pan_wrap", 1),
    (r"pizza|quiche|tarte|flammkuchen|strudel|frittata|blech|focaccia|empanada|börek", 1.0, 2.5, "crisp", "oven_crisp", 0),
    (r"lasagne|lasagna|auflauf|gratin|bake\b|baked|casserole|überbacken|moussaka|enchilada|cannelloni", 3.0, 7.0, "bake", "oven_bake", 1),
    (r"suppe|soup|eintopf|stew|brühe|broth|ramen|\bpho\b|minestrone|chowder", 3.0, 7.0, "soup", "pot_soup", 1),
    (r"pasta|nudel|noodle|spaghetti|penne|fusilli|tortellini|gnocchi|mac\b|spätzle", 2.0, 4.5, "pasta", "pan_pasta", 1),
    (r"curry|chili|dal\b|dhal|daal|gulasch|goulash|ragout|ragù|ragu|bolognese|stroganoff|tikka|korma|masala|shakshuka|sauce|geschnetzeltes|tajine|tagine",
     2.5, 6.0, "stew", "pot_stew", 1),
    (r"reis|rice|risotto|paella|pilaw|pilaf|biryani|nasi|congee|bowl|teriyaki|bibimbap", 2.0, 4.5, "rice", "pan_rice", 1),
    (r"porridge|oatmeal|haferbrei|grießbrei", 1.5, 3.0, "porridge", None, 0),
    (r"pancake|pfannkuchen|waffel|waffle|french toast|crêpe|crepe", 0.75, 1.5, "pancake", "pan_pancake", 0),
]
BREAKFAST = (1.0, 2.0, "breakfast", None, 0)
MAIN = (2.5, 5.5, "main", "pot_main", 1)

TEXT = {
    "de": {
        "cold": "Schmeckt kalt aus dem Kühlschrank, Aufwärmen ist nicht nötig.",
        "bakery": "Bei Raumtemperatur essen, nicht in die Mikrowelle (wird zäh). Gefroren: bei Raumtemperatur auftauen oder 5–10 min bei 180 °C aufbacken.",
        "muffin": "auf Küchenpapier, nicht zu lange (wird sonst gummiartig)",
        "burrito": "in feuchtes Küchenpapier wickeln, nach der Hälfte wenden",
        "crisp": "wird in der Mikrowelle weich, im Ofen oder in der Pfanne bleibt es knusprig",
        "bake": "abgedeckt, nach der Hälfte prüfen, die Mitte muss heiß sein",
        "soup": "abgedeckt, nach der Hälfte umrühren, bei Bedarf einen Schluck Wasser dazu",
        "stew": "abgedeckt, nach der Hälfte umrühren",
        "rice": "1 EL Wasser darüber, abgedeckt erhitzen",
        "pasta": "einen Schuss Wasser oder Milch dazu, nach der Hälfte umrühren",
        "porridge": "einen Schuss Milch dazu, zwischendurch umrühren",
        "pancake": "nebeneinander legen, nicht stapeln",
        "breakfast": "abgedeckt erhitzen",
        "main": "abgedeckt, nach der Hälfte umrühren",
        "frozen": "abgedeckt, zwischendurch umrühren oder wenden; oder über Nacht im Kühlschrank auftauen und wie aus dem Kühlschrank erwärmen",
        "pan_wrap": "Pfanne ohne Fett: 3–4 min je Seite bei mittlerer Hitze",
        "oven_crisp": "Ofen 180 °C: 8–10 min (gefroren 15–20 min)",
        "oven_bake": "Ofen 180 °C abgedeckt: 20 min (gefroren 40 min)",
        "pot_soup": "Topf bei mittlerer Hitze: 5–7 min, umrühren",
        "pot_stew": "Topf mit einem Schluck Wasser bei mittlerer Hitze: 5–8 min",
        "pan_rice": "Pfanne mit etwas Öl oder Wasser: 4–5 min unter Rühren",
        "pan_pasta": "Pfanne mit einem Schluck Wasser: 4–5 min unter Rühren",
        "pan_pancake": "Pfanne oder Toaster: 1–2 min",
        "pot_main": "Pfanne oder Topf mit einem Schluck Wasser: 5–8 min",
    },
    "en": {
        "cold": "Tastes good cold from the fridge, no need to reheat.",
        "bakery": "Eat at room temperature, not in the microwave (it turns chewy). Frozen: thaw at room temperature or crisp up 5–10 min at 180 °C.",
        "muffin": "on kitchen paper, not too long (they turn rubbery)",
        "burrito": "wrap in a damp paper towel, turn halfway",
        "crisp": "turns soft in the microwave, the oven or a pan keeps it crisp",
        "bake": "covered, check halfway, the middle must be hot",
        "soup": "covered, stir halfway, add a splash of water if needed",
        "stew": "covered, stir halfway",
        "rice": "sprinkle 1 tbsp water over it, heat covered",
        "pasta": "add a splash of water or milk, stir halfway",
        "porridge": "add a splash of milk, stir in between",
        "pancake": "side by side, not stacked",
        "breakfast": "heat covered",
        "main": "covered, stir halfway",
        "frozen": "covered, stir or turn in between; or thaw overnight in the fridge and reheat as from the fridge",
        "pan_wrap": "dry pan: 3–4 min per side over medium heat",
        "oven_crisp": "oven 180 °C: 8–10 min (frozen 15–20 min)",
        "oven_bake": "oven 180 °C covered: 20 min (frozen 40 min)",
        "pot_soup": "pot over medium heat: 5–7 min, stirring",
        "pot_stew": "pot with a splash of water over medium heat: 5–8 min",
        "pan_rice": "pan with a little oil or water: 4–5 min, stirring",
        "pan_pasta": "pan with a splash of water: 4–5 min, stirring",
        "pan_pancake": "pan or toaster: 1–2 min",
        "pot_main": "pan or pot with a splash of water: 5–8 min",
    },
}
NO_FREEZE = re.compile(r"nicht einfrieren|nicht zum einfrieren|do not freeze|don.t freeze|not suitable for freezing", re.I)


def estimate(r):
    """A rough recommendation from name, categories and tags of a recipe (always marked as estimated)."""
    lang = "en" if r.get("sprache") == "en" else "de"
    tx = TEXT[lang]
    hay = " ".join([r.get("name") or ""] + list(r.get("kategorien") or []) + list(r.get("tags") or [])).lower()
    rule = next((x[1:] for x in RULES if re.search(x[0], hay)), None)
    if rule is None:
        rule = BREAKFAST if "Frühstück" in (r.get("kategorien") or []) else MAIN
    fridge, frozen, hint, alt, rest = rule
    if fridge is None:
        return {"kalt": True, "hinweis": tx[hint]}
    notes = " ".join(n.get("text") or "" for n in r.get("notizen") or [])
    out = {"kuehlschrank": {"watt": DEFAULT_WATT, "minuten": fridge, "hinweis": tx[hint]}}
    if not NO_FREEZE.search(notes):
        out["gefroren"] = {"watt": DEFAULT_WATT, "minuten": frozen, "hinweis": tx["frozen"]}
    if rest:
        out["ruhen"] = rest
    if alt:
        out["alternative"] = tx[alt]
    return out


def info(r, watt=None, portions=1):
    """What to show: the recipe's field (or the estimate, geschaetzt true) with the times for this many portions in
    the household's microwave. watt None = keep the recommended watt (share page)."""
    stored = normalize(r.get("aufwaermen") or {})
    own = stored.get("kalt") or any(k in stored for k in MODES)
    d = stored if own else {**estimate(r), **stored}  # only a hint or an alternative: times from the estimate
    try:
        n = max(1.0, min(10.0, float(portions or 1)))
    except (TypeError, ValueError):
        n = 1.0
    out = {"geschaetzt": not own, "portionen": n if n != int(n) else int(n), "watt": _watt(watt) if watt else None,
           "kalt": bool(d.get("kalt")), "ruhen": d.get("ruhen"), "alternative": d.get("alternative", ""),
           "hinweis": d.get("hinweis", "")}
    for k in MODES:
        m = d.get(k)
        if m:
            w = _watt(watt) if watt else m["watt"]
            sec = seconds(m["minuten"], n, m["watt"], w)
            out[k] = {"watt": w, "sekunden": sec, "zeit": clock(sec), "hinweis": m.get("hinweis", ""),
                      "empfohlen": {"watt": m["watt"], "minuten": m["minuten"]}}
        else:
            out[k] = None
    return out


# ---------- lines for the print and share page ----------
LABELS = {"de": {"title": "Aufwärmen", "fridge": "Aus dem Kühlschrank", "frozen": "Gefroren", "rest": "{n} min ruhen lassen",
                 "alt": "Statt Mikrowelle", "est": "geschätzt nach Art des Gerichts", "more": "Je weitere Portion etwa 70 % länger.",
                 "per": "1 Portion", "portions": "{n} Portionen"},
          "en": {"title": "Reheating", "fridge": "From the fridge", "frozen": "Frozen", "rest": "let it stand {n} min",
                 "alt": "Instead of the microwave", "est": "estimated from the kind of dish", "more": "Each extra portion takes about 70 % longer.",
                 "per": "1 portion", "portions": "{n} portions"}}


def _num(x):
    return str(int(x)) if float(x) == int(x) else str(x).replace(".", ",")


def lines(i, lang="de"):
    """→ (title, [lines]) in plain text for print and share."""
    lb = LABELS["en" if lang == "en" else "de"]
    title = lb["title"] + (f" ({lb['est']})" if i["geschaetzt"] else "")
    out = []
    if i["kalt"]:
        out.append(i["hinweis"])
        return title, [x for x in out if x]
    n = i["portionen"]
    who = lb["per"] if n == 1 else lb["portions"].format(n=_num(n))
    for k, label in (("kuehlschrank", lb["fridge"]), ("gefroren", lb["frozen"])):
        m = i.get(k)
        if m:
            parts = [f"{m['watt']} W", f"{m['zeit']} min"] + ([m["hinweis"]] if m["hinweis"] else [])
            if k == "kuehlschrank" and i.get("ruhen"):
                parts.append(lb["rest"].format(n=_num(i["ruhen"])))
            out.append(f"{label} ({who}): " + " · ".join(parts))
    if i.get("hinweis"):
        out.append(i["hinweis"])
    if i.get("alternative"):
        out.append(f"{lb['alt']}: {i['alternative']}")
    if out:
        out.append(lb["more"])
    return title, out
