"""Kombüse database (replaces Mealie): recipes, ingredient lexicon, week plan, shopping list.

Everything lives in /data/food-tinder.db (SQLite), recipe images in /data/rezeptbilder/<id>.<ext>.
The table and column names, and the JSON field names of a recipe, are German and part of the API, so they stay.
A recipe is a JSON document (column daten) with these fields:
  beschreibung (description), portionen (servings, a number), ertrag (yield), zeiten {gesamt, vorbereitung, kochen} (times),
  kategorien [..] (categories such as „Frühstück“, „Hauptgericht“), tags [..],
  zutaten [{abschnitt, menge, einheit, name, notiz}] (ingredients: section, quantity, unit, name, note), schritte [{titel, text}] (steps),
  naehrwerte {kcal, protein, kh, zucker, fett, gesfett, ballaststoffe, natrium} per serving (nutrients),
  notizen [{titel, text}] (notes), quelle (source), sprache (language),
  aufwaermen {kuehlschrank, gefroren, ruhen, alternative, hinweis, kalt} (reheating in the microwave, see reheat.py)
The supermarket category of an ingredient is kept in the lexicon (table zutat), not in the recipe.
Tables: rezept (recipes), zutat (lexicon), plan (week plan), einkauf (shopping list); the pantry (pantry.py) adds more."""
import json
import math
import os
import re
import sqlite3
import threading
import unicodedata
import uuid
from datetime import date, datetime, timedelta, timezone

import config  # noqa: E402
import reheat  # noqa: E402

DATA = config.DATA
DB = os.path.join(DATA, config.DB_FILE)
IMAGES = os.path.join(DATA, "rezeptbilder")  # household 1; images_dir() for the current household
lock = threading.Lock()


class Error(Exception):
    pass


# ---------- households (households.py) ----------
# Every household has a database file of its own with the same tables. Household 1 is the installation as it always
# was: /data/food-tinder.db with /data/img and /data/rezeptbilder. Further households live in /data/households/<id>/
# with the same layout. The instance database (accounts, sessions, API tokens, households, invitations) is household
# 1's file. A request works in the household of whoever is asking; the household is set per thread.
_ctx = threading.local()


def household_id():
    return getattr(_ctx, "hid", 1)


def set_household(hid):
    _ctx.hid = int(hid)


class in_household:  # noqa: N801  with store.in_household(2): … works in household 2, then back
    def __init__(self, hid):
        self.hid = int(hid)

    def __enter__(self):
        self.prev = household_id()
        _ctx.hid = self.hid
        return self.hid

    def __exit__(self, *exc):
        _ctx.hid = self.prev


def data_dir(hid=None):
    hid = household_id() if hid is None else int(hid)
    return DATA if hid == 1 else os.path.join(DATA, "households", str(hid))


def db_path(hid=None):
    return os.path.join(data_dir(hid), config.DB_FILE)


def images_dir(hid=None):
    return os.path.join(data_dir(hid), "rezeptbilder")


def spawn(fn, *args, name=None):
    """Run fn in a background thread in the current household."""
    hid = household_id()

    def run():
        set_household(hid)
        fn(*args)
    t = threading.Thread(target=run, daemon=True, name=name)
    t.start()
    return t


class PerHousehold:
    """A dict kept apart per household, for module-level caches (logins of Bring and Yazio, timers)."""

    def __init__(self):
        self._d = {}

    def _cur(self):
        return self._d.setdefault(household_id(), {})

    def __getitem__(self, k):
        return self._cur()[k]

    def __setitem__(self, k, v):
        self._cur()[k] = v

    def __contains__(self, k):
        return k in self._cur()

    def get(self, k, default=None):
        return self._cur().get(k, default)

    def update(self, *a, **kw):
        self._cur().update(*a, **kw)

    def pop(self, k, *default):
        return self._cur().pop(k, *default)

    def clear(self):
        self._cur().clear()

    def clear_all(self):
        self._d.clear()


def con():
    """Connection to the database of the current household."""
    c = sqlite3.connect(db_path(), timeout=10)
    c.row_factory = sqlite3.Row
    return c


def main():
    """Connection to the instance database (accounts, sessions, API tokens, households, invitations)."""
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init():
    os.makedirs(images_dir(), exist_ok=True)
    with con() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS rezept (
              id TEXT PRIMARY KEY,
              slug TEXT UNIQUE NOT NULL,
              name TEXT NOT NULL,
              daten TEXT NOT NULL,                 -- JSON, see above
              bewertung REAL,                      -- the user's stars 1-5
              bild TEXT NOT NULL DEFAULT '',       -- file extension of the image (webp, jpg, png) or empty
              erstellt TEXT, geaendert TEXT
            );
            -- ingredient lexicon with supermarket category (started from Mealie's 576 foods)
            CREATE TABLE IF NOT EXISTS zutat (name TEXT PRIMARY KEY COLLATE NOCASE, kategorie TEXT NOT NULL DEFAULT 'Sonstiges');
            -- week plan: with a recipe (counts for shopping) or only title/text (leftovers, canteen, meal prep day)
            CREATE TABLE IF NOT EXISTS plan (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              datum TEXT NOT NULL,
              typ TEXT NOT NULL,                   -- breakfast | lunch | dinner | side | snack
              rezept_id TEXT,
              scale REAL,
              titel TEXT NOT NULL DEFAULT '',
              text TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS plan_datum ON plan(datum);
            -- shopping list. roh: {"teile": [{"m": quantity, "e": unit, "r": recipe}], "notizen": [..]} for lines from recipes,
            -- quantity, unit and note are calculated from it. Lines entered by hand have roh NULL.
            CREATE TABLE IF NOT EXISTS einkauf (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              kategorie TEXT NOT NULL DEFAULT 'Sonstiges',
              menge REAL,
              einheit TEXT NOT NULL DEFAULT '',
              notiz TEXT NOT NULL DEFAULT '',
              checked INTEGER NOT NULL DEFAULT 0,
              roh TEXT,
              geaendert TEXT
            );
        """)


# ---------- units ----------
UNITS = ["g", "kg", "ml", "l", "EL", "TL", "Prise", "Messerspitze", "Schuss", "Spritzer", "Stück", "Zehe", "Knolle",
             "Bund", "Dose", "Packung", "Becher", "Flasche", "Tube", "Glas", "Zweig", "Kopf", "Scheibe", "Blatt",
             "Handvoll", "Portion", "Tasse", "Stange", "Würfel", "cl", "mg"]
_ALIASES = {"gramm": "g", "gr": "g", "kilogramm": "kg", "milligramm": "mg", "milliliter": "ml", "liter": "l",
          "centiliter": "cl", "esslöffel": "EL", "essl.": "EL", "el.": "EL", "teelöffel": "TL", "teel.": "TL", "tl.": "TL",
          "stk": "Stück", "stk.": "Stück", "stck": "Stück", "stück": "Stück", "zehen": "Zehe", "knollen": "Knolle",
          "prisen": "Prise", "dosen": "Dose", "packungen": "Packung", "pck.": "Packung", "pkg.": "Packung", "pck": "Packung",
          "päckchen": "Packung", "flaschen": "Flasche", "tuben": "Tube", "gläser": "Glas", "zweige": "Zweig", "köpfe": "Kopf",
          "scheiben": "Scheibe", "blätter": "Blatt", "portionen": "Portion", "tassen": "Tasse", "stangen": "Stange",
          "bünde": "Bund", "becher": "Becher", "würfel": "Würfel",
          # English units (the app speaks English)
          "tbsp": "EL", "tbs": "EL", "tablespoon": "EL", "tablespoons": "EL", "tsp": "TL", "teaspoon": "TL", "teaspoons": "TL",
          "pc": "Stück", "pcs": "Stück", "piece": "Stück", "pieces": "Stück", "clove": "Zehe", "cloves": "Zehe", "pinch": "Prise",
          "pack": "Packung", "packs": "Packung", "package": "Packung", "can": "Dose", "cans": "Dose", "tin": "Dose", "bunch": "Bund",
          "bulb": "Knolle", "jar": "Glas", "jars": "Glas", "bottle": "Flasche", "cup": "Tasse", "cups": "Tasse", "slice": "Scheibe",
          "slices": "Scheibe", "handful": "Handvoll", "sprig": "Zweig", "sprigs": "Zweig", "tub": "Becher", "leaf": "Blatt",
          "leaves": "Blatt", "head": "Kopf", "stalk": "Stange", "cube": "Würfel", "dash": "Schuss", "splash": "Spritzer",
          "serving": "Portion", "servings": "Portion", "tube": "Tube"}
_UNITS_LOWER = {e.lower(): e for e in UNITS}


def unit(e):
    """Normalize a unit: „Esslöffel“ → „EL“, „Zehen“ → „Zehe“. Anything unknown stays as it is."""
    s = re.sub(r"\s+", " ", str(e or "")).strip()
    if not s:
        return ""
    k = s.lower()
    return _UNITS_LOWER.get(k) or _ALIASES.get(k) or s


# base for adding up on the shopping list: (base, factor)
_BASE = {"g": ("g", 1), "kg": ("g", 1000), "mg": ("g", .001), "ml": ("ml", 1), "cl": ("ml", 10), "l": ("ml", 1000),
          "EL": ("el", 1), "TL": ("el", 1 / 3), "Prise": ("el", .05), "Messerspitze": ("el", .05), "Schuss": ("el", 1),
          "Spritzer": ("el", .2), "Zehe": ("zehe", 1), "Stück": ("stk", 1), "": ("stk", 1)}


def base_unit(e, name=""):
    if e == "Knolle" and "knoblauch" in name.lower():
        return "zehe", 10
    return _BASE.get(e, (e, 1))


def fmt_number(q):
    if q is None or q == "":
        return ""
    q = round(float(q), 2)
    return str(int(q)) if q == int(q) else str(q).replace(".", ",")


# ---------- recipes ----------
def slugify(name):
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:100] or "rezept"


def _f(v):
    try:
        if v is None or v == "":
            return None
        return float(str(v).replace(",", ".").split()[0])
    except (ValueError, IndexError):
        return None


def _duration(v):
    """Readable times: ISO duration „PT1H20M“ → „1 h 20 min“, „20 Minuten“ stays."""
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?", str(v or "").strip())
    if not m or not (m.group(1) or m.group(2)):
        return re.sub(r"\s+", " ", str(v or "")).strip()[:60]
    h, mins = int(m.group(1) or 0), int(m.group(2) or 0)
    h, mins = h + mins // 60, mins % 60
    return " ".join(x for x in (f"{h} h" if h else "", f"{mins} min" if mins else "") if x)


NUTRIENTS = ("kcal", "protein", "kh", "zucker", "fett", "gesfett", "ballaststoffe", "natrium")


def normalize(d):
    """Check recipe data and bring it into the fixed shape (for saving from the app, AI runs and the Mealie import)."""
    s = lambda v, n=2000: re.sub(r"[ \t]+", " ", str(v or "")).strip()[:n]  # noqa: E731
    ingr = []
    for z in d.get("zutaten") or []:
        if isinstance(z, str):
            z = parse_line(z)
        name, section = s(z.get("name"), 200), s(z.get("abschnitt"), 120)
        if not name and not section:
            continue
        ingr.append({"abschnitt": section, "menge": _f(z.get("menge")) or 0, "einheit": unit(z.get("einheit")),
                    "name": name, "notiz": s(z.get("notiz"), 300), **({"kategorie": s(z["kategorie"], 60)} if z.get("kategorie") else {})})
    steps = []
    for x in d.get("schritte") or []:
        x = {"text": x} if isinstance(x, str) else x
        if s(x.get("text"), 5000):
            steps.append({"titel": s(x.get("titel"), 200), "text": s(x.get("text"), 5000)})
    nw = {k: _f((d.get("naehrwerte") or {}).get(k)) for k in NUTRIENTS}
    return {
        "beschreibung": s(d.get("beschreibung"), 3000),
        "portionen": _f(d.get("portionen")) or 2,
        "ertrag": s(d.get("ertrag"), 100),
        "zeiten": {k: _duration((d.get("zeiten") or {}).get(k)) for k in ("gesamt", "vorbereitung", "kochen")},
        "kategorien": [s(k, 60) for k in d.get("kategorien") or [] if s(k)],
        "tags": list(dict.fromkeys(s(t, 60) for t in d.get("tags") or [] if s(t))),
        "zutaten": ingr, "schritte": steps, "naehrwerte": nw,
        "notizen": [{"titel": s(n.get("titel"), 200), "text": s(n.get("text"), 5000)} for n in d.get("notizen") or [] if s(n.get("text"))],
        "quelle": s(d.get("quelle"), 1000),
        "aufwaermen": reheat.normalize(d.get("aufwaermen")),  #, reheat.py; {} = not set
        "sprache": d.get("sprache") if d.get("sprache") in ("de", "en") else "",  #, empty = household language
    }


def _row(r):
    if not r:
        return None
    d = json.loads(r["daten"])
    d.setdefault("sprache", "de")  # older recipes
    d.setdefault("aufwaermen", {})  # older recipes
    return {"id": r["id"], "slug": r["slug"], "name": r["name"], "bewertung": r["bewertung"], "bild": r["bild"],
            "erstellt": r["erstellt"], "geaendert": r["geaendert"], **d}


def recipe(slug_or_id):
    with con() as c:
        r = c.execute("SELECT * FROM rezept WHERE slug=? OR id=?", (slug_or_id, slug_or_id)).fetchone()
    if not r:
        raise Error("Rezept nicht gefunden")
    return _row(r)


def find_recipe(name):
    """Recipe by its name (for leftover entries „Rest: Chicken Teriyaki“)."""
    with con() as c:
        r = c.execute("SELECT * FROM rezept WHERE name=? COLLATE NOCASE", (name,)).fetchone()
        if not r:
            rs = c.execute("SELECT * FROM rezept WHERE name LIKE ? COLLATE NOCASE", (f"%{name}%",)).fetchall()
            r = rs[0] if len(rs) == 1 else None
    return _row(r)


def all_recipes(search=""):
    with con() as c:
        rows = c.execute("SELECT * FROM rezept ORDER BY name COLLATE NOCASE").fetchall()
    out = [_row(r) for r in rows]
    if search:
        q = search.lower()
        out = [r for r in out if q in r["name"].lower() or q in (r.get("quelle") or "").lower()]
    return out


def save(recipe_data, slug=None, name=None, rid=None, rating=None, created=None):
    """Create a new recipe (slug None) or replace an existing one. data in the shape above (partial data is fine:
        for an existing recipe only the fields sent are replaced). New ingredients go into the lexicon."""
    with lock, con() as c:
        old = c.execute("SELECT * FROM rezept WHERE slug=?", (slug,)).fetchone() if slug else None
        if slug and not old:
            raise Error("Rezept nicht gefunden")
        base_data = json.loads(old["daten"]) if old else {}
        d = normalize({**base_data, **{k: v for k, v in recipe_data.items() if k not in ("id", "slug", "name", "bewertung", "bild")}})
        if not d["sprache"]:  # new recipes in the household language, old recipes were German
            import household
            d["sprache"] = household.language() if not old else "de"
        name = re.sub(r"\s+", " ", str(name or recipe_data.get("name") or (old["name"] if old else ""))).strip()[:200]
        if not name:
            raise Error("Name fehlt")
        for z in d["zutaten"]:  # lexicon: take in new ingredients with their category, the category is not stored in the recipe
            cat = z.pop("kategorie", "")
            if z["name"]:
                if cat:
                    c.execute("INSERT INTO zutat(name, kategorie) VALUES(?,?) ON CONFLICT(name) DO NOTHING", (z["name"], cat))
                else:
                    c.execute("INSERT OR IGNORE INTO zutat(name) VALUES(?)", (z["name"],))
        if old:
            c.execute("UPDATE rezept SET name=?, daten=?, geaendert=? WHERE id=?",
                      (name, json.dumps(d, ensure_ascii=False), now(), old["id"]))
            s = old["slug"]
        else:
            s, n = slugify(name), 2
            while c.execute("SELECT 1 FROM rezept WHERE slug=?", (s,)).fetchone():
                s, n = f"{slugify(name)}-{n}", n + 1
            c.execute("INSERT INTO rezept(id, slug, name, daten, bewertung, erstellt, geaendert) VALUES(?,?,?,?,?,?,?)",
                      (rid or str(uuid.uuid4()), s, name, json.dumps(d, ensure_ascii=False), rating, created or now(), now()))
    return recipe(s)


def rate(slug, stars):
    s = max(1.0, min(5.0, float(stars)))
    with lock, con() as c:
        if not c.execute("UPDATE rezept SET bewertung=? WHERE slug=?", (s, slug)).rowcount:
            raise Error("Rezept nicht gefunden")
    return s


_IMAGE_TYPES = {b"\xff\xd8": "jpg", b"\x89P": "png", b"RI": "webp", b"GI": "gif"}


def save_image(slug, data):
    ext = _IMAGE_TYPES.get(data[:2])
    if not ext or (ext == "webp" and data[8:12] != b"WEBP"):
        raise Error("Kein Bild (JPEG, PNG, WebP oder GIF)")
    if len(data) > 15 * 1024 * 1024:
        raise Error("Bild zu groß")
    r = recipe(slug)
    for old in os.listdir(images_dir()):
        if old.startswith(r["id"]):
            os.remove(os.path.join(images_dir(), old))
    with open(os.path.join(images_dir(), f"{r['id']}.{ext}"), "wb") as f:
        f.write(data)
    with lock, con() as c:
        c.execute("UPDATE rezept SET bild=?, geaendert=? WHERE id=?", (ext, now(), r["id"]))
    return ext


def image_file(rid, small=False):
    """Path and MIME type of the recipe image; small = thumbnail if there is one (taken over from Mealie)."""
    if not re.fullmatch(r"[0-9a-f-]{36}", rid or ""):
        return None
    for name in ([f"{rid}-min.webp"] if small else []) + [f"{rid}.{e}" for e in ("webp", "jpg", "png", "gif")]:
        p = os.path.join(images_dir(), name)
        if os.path.isfile(p):
            return p, {"webp": "image/webp", "jpg": "image/jpeg", "png": "image/png", "gif": "image/gif"}[name.rsplit(".", 1)[1]]
    return None


# ---------- reading ingredient lines („200 g Mehl; gesiebt“) ----------
_FRACTIONS = {"½": .5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": .25, "¾": .75, "⅛": .125}


def _quantity(t):
    t = t.strip()
    for k, v in _FRACTIONS.items():
        if t.endswith(k):
            whole = t[:-1].strip()
            return (float(whole.replace(",", ".")) if whole else 0) + v
    m = re.fullmatch(r"(?:(\d+)\s+)?(\d+)\s*/\s*(\d+)", t)  # 1/2 and 1 1/2
    if m:
        return int(m.group(1) or 0) + int(m.group(2)) / int(m.group(3))
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*[-–]\s*(\d+(?:[.,]\d+)?)", t)  # range: mean value
    if m:
        return (float(m.group(1).replace(",", ".")) + float(m.group(2).replace(",", "."))) / 2
    return float(t.replace(",", "."))


# quantity first (not for „8-Kräuter-Mischung“: a number with a hyphen and a letter is part of the name)
_LINE = re.compile(r"^\s*(?:((?:\d+(?:[.,]\d+)?\s*)?[½⅓⅔¼¾⅛]|(?:\d+\s+)?\d+\s*/\s*\d+|\d+(?:[.,]\d+)?(?:\s*[-–]\s*\d+(?:[.,]\d+)?)?)(?!-\D))?\s*(.*)$")


def parse_line(t):
    """One ingredient line from the edit field: „quantity unit name; note“. „## Title“ is a section."""
    t = (t or "").strip()
    if t.startswith("#"):
        return {"abschnitt": t.lstrip("# ").strip(), "name": ""}
    note = ""
    if ";" in t:
        t, note = (x.strip() for x in t.split(";", 1))
    m = _LINE.match(t)
    amount, rest = 0, t
    if m and m.group(1):
        try:
            amount, rest = _quantity(m.group(1)), m.group(2)
        except ValueError:
            amount, rest = 0, t
    e = ""
    w = rest.split(" ", 1)
    if len(w) == 2 and (w[0].lower() in _UNITS_LOWER or w[0].lower() in _ALIASES):
        e, rest = unit(w[0]), w[1]
    return {"abschnitt": "", "menge": amount, "einheit": e, "name": rest.strip(), "notiz": note}


def format_line(z):
    if z.get("abschnitt") and not z.get("name"):
        return "## " + z["abschnitt"]
    out = " ".join(x for x in (fmt_number(z.get("menge")) if z.get("menge") else "", z.get("einheit") or "", z.get("name") or "") if x)
    return out + (f"; {z['notiz']}" if z.get("notiz") else "")


# ---------- ingredient lexicon ----------
def category(name):
    with con() as c:
        r = c.execute("SELECT kategorie FROM zutat WHERE name=?", (name,)).fetchone()
    return r["kategorie"] if r else "Sonstiges"


def ingredients(search=""):
    with con() as c:
        if search:
            rows = c.execute("SELECT * FROM zutat WHERE name LIKE ? ORDER BY length(name), name LIMIT 30", (f"%{search}%",)).fetchall()
        else:
            rows = c.execute("SELECT * FROM zutat ORDER BY name").fetchall()
        cats = [r[0] for r in c.execute("SELECT DISTINCT kategorie FROM zutat ORDER BY kategorie")]
    return {"zutaten": [dict(r) for r in rows], "kategorien": cats}


def set_category(name, cat):
    with lock, con() as c:
        c.execute("INSERT INTO zutat(name, kategorie) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET kategorie=excluded.kategorie", (name, cat))
        c.execute("UPDATE einkauf SET kategorie=? WHERE name=? COLLATE NOCASE", (cat, name))


# ---------- week plan ----------
MEAL_TYPES = ("breakfast", "lunch", "dinner", "side", "snack")


def plan_week(monday, sunday):
    with con() as c:
        rows = c.execute("""SELECT p.*, r.slug, r.name AS rname, r.daten FROM plan p LEFT JOIN rezept r ON r.id = p.rezept_id
                            WHERE p.datum BETWEEN ? AND ? ORDER BY p.datum, p.id""", (str(monday), str(sunday))).fetchall()
    out = []
    for e in rows:
        out.append({"id": e["id"], "date": e["datum"], "typ": e["typ"], "titel_roh": e["titel"], "text": e["text"], "scale": e["scale"],
                    "rezept": {"id": e["rezept_id"], "slug": e["slug"], "name": e["rname"],
                               "portionen": json.loads(e["daten"])["portionen"]} if e["slug"] else None})
    return out


def plan_entry(eid):
    with con() as c:
        e = c.execute("SELECT * FROM plan WHERE id=?", (eid,)).fetchone()
    if not e:
        raise Error("Plan-Eintrag nicht gefunden")
    return dict(e)


def add_plan_entry(day, meal_type, slug=None, title="", text="", scale=1.0, eid=None):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
        raise Error("Datum fehlt")
    if meal_type not in MEAL_TYPES:
        meal_type = "dinner"
    rid = recipe(slug)["id"] if slug else None
    with lock, con() as c:
        cur = c.execute("INSERT INTO plan(id, datum, typ, rezept_id, scale, titel, text) VALUES(?,?,?,?,?,?,?)",
                        (eid, day, meal_type, rid, round(float(scale or 1), 3) if rid else None,
                         str(title or "")[:200], str(text or "")[:1000]))
        return cur.lastrowid


def delete_plan_entry(eid):
    with lock, con() as c:
        return c.execute("DELETE FROM plan WHERE id=?", (eid,)).rowcount


# ---------- shopping list ----------
def _to_buy(parts, name):
    """Bring the total need of a line to what you buy: (quantity, unit, „gebraucht …“ note or None)."""
    b = base_unit(parts[0]["e"], name)[0]
    total = sum(t["m"] * base_unit(t["e"], name)[1] for t in parts)
    units = {t["e"] for t in parts}
    if total <= 0:
        return None, "", None
    if b in ("g", "ml"):
        step = 50 if total <= 500 else 100
        q = math.ceil(total / step - 1e-9) * step
        if q >= 1000 and q % 1000 == 0 and b == "g":
            return q / 1000, "kg", (f"gebraucht {fmt_number(total)} g" if abs(q - total) > 1e-6 else None)
        return q, b, (f"gebraucht {fmt_number(total)} {b}" if abs(q - total) > 1e-6 else None)
    if b == "el":  # spice and sauce amounts: one pack
        if len(units) == 1:
            return 1, "Packung", f"gebraucht {fmt_number(sum(t['m'] for t in parts))} {parts[0]['e']}"
        return 1, "Packung", f"gebraucht {fmt_number(total)} EL"
    if b == "zehe":  # garlic: whole bulbs, about 10 cloves per bulb
        q = max(1, math.ceil(total / 10 - 1e-9))
        return q, "Knolle", (None if units == {"Knolle"} and abs(q * 10 - total) < 1e-6 else f"gebraucht {fmt_number(total)} Zehen")
    q = math.ceil(total - 1e-9)
    e = "Stück" if b == "stk" and "Stück" in units else ("" if b == "stk" else b)
    return q, e, (f"gebraucht {fmt_number(total)}{'' if b == 'stk' else ' ' + b}" if abs(q - total) > 1e-6 else None)


def _note(notes, needed_note=None):
    rest = list(dict.fromkeys(n for n in notes if n))
    rest = [t for t in rest if not any(t != o and t.lower() in o.lower() for o in rest)]
    return "; ".join(([needed_note] if needed_note else []) + rest)[:200]


def _recalculate(c, iid):
    """Quantity, unit and note of a recipe line from the raw need, minus the pantry:
        if the pantry covers it, the line is checked as „im Schrank“; if it covers part of it, only the rest is listed.
        If the user set or removed the check mark by hand (manuell), it stays."""
    import pantry  # late import, pantry imports store
    it = c.execute("SELECT * FROM einkauf WHERE id=?", (iid,)).fetchone()
    raw = json.loads(it["roh"])
    parts, name = raw["teile"], it["name"]
    amount, e, needed = _to_buy(parts, name)
    have, have_text, covered = 0, "", False
    if parts and pantry.shelf_stable(name, it["kategorie"]):
        b, f0 = base_unit(parts[0]["e"], name)
        total = sum(t["m"] * base_unit(t["e"], name)[1] for t in parts)
        have, have_text = pantry.stock_for(c, name, parts[0]["e"])
        if have is None or (total > 0 and have >= total - 1e-6):
            covered = True
        elif have and total > 0:
            amount, e, _ = _to_buy([{"m": (total - have) / f0, "e": parts[0]["e"]}], name)
            needed = f"gebraucht {fmt_number(total)}{'' if b == 'stk' else ' ' + (b if b in ('g', 'ml') else parts[0]['e'])}, {have_text} im Schrank"
    checked = it["checked"] if it["manuell"] else (1 if covered else 0)
    c.execute("UPDATE einkauf SET menge=?, einheit=?, notiz=?, checked=?, schrank=?, geaendert=? WHERE id=?",
              (amount, e, _note(raw["notizen"], needed), checked, have_text if (covered or have) else "", now(), iid))


def reconcile():
    """Recalculate the recipe lines of the list against the current pantry (when the shopping page opens)."""
    with lock, con() as c:
        for r in c.execute("SELECT id FROM einkauf WHERE roh IS NOT NULL").fetchall():
            _recalculate(c, r["id"])


def add_to_list(recipe_factors, always, only=None):
    """Put recipes [(slug, factor)] on the list ready for shopping: leave out what is always at home, merge each
        ingredient with a matching unit into one line (also with what is already there) and round once. Checked lines and
        lines entered by hand stay untouched. only: set of ingredient names in lower case, the rest is left out (only what
        is missing in the pantry)."""
    names = {n.lower() for n in always.get("namen") or []}
    cats = {k.lower() for k in always.get("kategorien") or []}
    skipped = new = 0
    errors = []
    with lock, con() as c:
        lexicon = {r["name"].lower(): r["kategorie"] for r in c.execute("SELECT * FROM zutat")}
        open_rows = {}
        for it in c.execute("SELECT * FROM einkauf WHERE checked=0 AND roh IS NOT NULL"):
            t0 = json.loads(it["roh"])["teile"]
            open_rows[(it["name"].lower(), base_unit(t0[0]["e"], it["name"])[0] if t0 else "stk")] = it["id"]
        changed = set()
        for slug, factor in recipe_factors:
            r = c.execute("SELECT * FROM rezept WHERE slug=?", (slug,)).fetchone()
            if not r:
                errors.append(f"{slug}: nicht gefunden")
                continue
            d = json.loads(r["daten"])
            for z in d["zutaten"]:
                if not z["name"] or (only is not None and z["name"].lower() not in only):
                    continue
                cat = lexicon.get(z["name"].lower(), "Sonstiges")
                if z["name"].lower() in names or cat.lower() in cats:
                    skipped += 1
                    continue
                part = {"m": round((z["menge"] or 0) * float(factor), 4), "e": z["einheit"], "r": r["name"]}
                key = (z["name"].lower(), base_unit(z["einheit"], z["name"])[0])
                if key in open_rows:
                    iid = open_rows[key]
                    raw = json.loads(c.execute("SELECT roh FROM einkauf WHERE id=?", (iid,)).fetchone()["roh"])
                    raw["teile"].append(part)
                    raw["notizen"].append(z["notiz"])
                    c.execute("UPDATE einkauf SET roh=? WHERE id=?", (json.dumps(raw, ensure_ascii=False), iid))
                else:
                    iid = c.execute("INSERT INTO einkauf(name, kategorie, roh) VALUES(?,?,?)",
                                    (z["name"], cat, json.dumps({"teile": [part], "notizen": [z["notiz"]]}, ensure_ascii=False))).lastrowid
                    open_rows[key] = iid
                    new += 1
                changed.add(iid)
        for iid in changed:
            _recalculate(c, iid)
    return {"zeilen_neu": new, "weggelassen": skipped, "fehler": errors}


def _quantity_text(amount, e):
    return " ".join(x for x in (fmt_number(amount) if amount else "", e or "") if x)


def shopping_list():
    with con() as c:
        rows = c.execute("SELECT * FROM einkauf").fetchall()
    items = [{"id": str(r["id"]), "checked": bool(r["checked"]), "name": r["name"], "menge": _quantity_text(r["menge"], r["einheit"]),
              "note": r["notiz"], "kategorie": r["kategorie"], "rezepte": sorted({t.get("r") for t in json.loads(r["roh"])["teile"]} - {None, ""})
              if r["roh"] else [], "menge_zahl": r["menge"], "einheit": r["einheit"], "schrank": r["schrank"],
              "im_schrank": bool(r["checked"] and not r["manuell"] and r["schrank"])} for r in rows]
    items.sort(key=lambda x: (x["kategorie"].lower(), x["name"].lower()))
    return {"items": items}


def check_item(iid, checked):
    with lock, con() as c:
        if not c.execute("UPDATE einkauf SET checked=?, manuell=1, geaendert=? WHERE id=?", (1 if checked else 0, now(), int(iid))).rowcount:
            raise Error("Eintrag nicht gefunden")
    return {"ok": True}


def add_item(text):
    """By hand: „2 Liter Milch“ or „Spülmittel“."""
    z = parse_line(text)
    if not z["name"]:
        raise Error("Was soll auf die Liste?")
    with lock, con() as c:
        r = c.execute("SELECT kategorie FROM zutat WHERE name=?", (z["name"],)).fetchone()
        iid = c.execute("INSERT INTO einkauf(name, kategorie, menge, einheit, notiz, geaendert) VALUES(?,?,?,?,?,?)",
                        (z["name"], r["kategorie"] if r else "Sonstiges", z["menge"] or None, z["einheit"], z["notiz"], now())).lastrowid
    return {"ok": True, "id": iid}


def remove_items(ids):
    with lock, con() as c:
        for i in ids:
            c.execute("DELETE FROM einkauf WHERE id=?", (int(i),))


# ---------- calendar (iCal for Home Assistant) ----------
TYPE_NAMES = {"breakfast": "Frühstück", "lunch": "Mittagessen", "dinner": "Abendessen", "side": "Beilage", "snack": "Snack"}
TYPE_TIMES = {"breakfast": ("0730", "0800"), "lunch": ("1230", "1300"), "dinner": ("1830", "1930"), "side": ("1000", "1100"), "snack": ("1600", "1615")}


def ical(meal_types=None, days_back=14, days_ahead=42):
    """Week plan as iCal, optionally only some meals (meal_types, e.g. ["lunch", "dinner"] for the HA overview)."""
    today = date.today()
    with con() as c:
        rows = c.execute("""SELECT p.*, r.name AS rname FROM plan p LEFT JOIN rezept r ON r.id = p.rezept_id
                            WHERE p.datum BETWEEN ? AND ?""", (str(today - timedelta(days=days_back)), str(today + timedelta(days=days_ahead)))).fetchall()
    esc = lambda s: str(s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")  # noqa: E731
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Kombuese//Wochenplan//DE", "X-WR-CALNAME:Kombüse", "CALSCALE:GREGORIAN"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for e in rows:
        if meal_types and e["typ"] not in meal_types:
            continue
        d = e["datum"].replace("-", "")
        start, until = TYPE_TIMES.get(e["typ"], ("1200", "1230"))
        title = e["rname"] or e["titel"] or "?"
        out += ["BEGIN:VEVENT", f"UID:kombuese-plan-{e['id']}@kombuese", f"DTSTAMP:{stamp}",
                f"DTSTART:{d}T{start}00", f"DTEND:{d}T{until}00",  # local time without time zone
                f"SUMMARY:{esc(TYPE_NAMES.get(e['typ'], '') + ': ' + title)}", f"DESCRIPTION:{esc(e['text'])}",
                f"CATEGORIES:{e['typ']}", "END:VEVENT"]
    out.append("END:VCALENDAR")
    return "\r\n".join(_fold(z) for z in out) + "\r\n"


def _fold(z):
    """iCal: lines of at most 75 bytes, continued with a space at the start."""
    if len(z.encode()) <= 75:
        return z
    parts, cur = [], ""
    for ch in z:
        if len((cur + ch).encode()) > (75 if not parts else 74):
            parts.append(cur)
            cur = ch
        else:
            cur += ch
    return "\r\n ".join(parts + [cur])
