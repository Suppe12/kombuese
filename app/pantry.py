"""Pantry (Schrank): shelf-stable stock with amounts, next to the freezer (table vorrat in server.py).

- Bought: what the app sends to Bring is remembered (table unterwegs). Once it shows up in Bring under
  „recently bought“ (checked off in the shop), the amount is booked into the pantry, but only for shelf-stable ingredients.
- Shopping list: store.py weighs the pantry against the need; what is covered shows as „im Schrank“ on the list.
- Cooking: „Fertig“ in cooking mode, „Gegessen“ on a cooking day and „Mealprep fertig“ subtract the recipe amounts,
  only once per plan entry (key plan:<id>). Every booking (table schrank_buchung) can be undone.
- Barcode: Open Food Facts gives name, brand and pack size; the app remembers which ingredient a code belongs to.
Amounts are kept in the base of the ingredient: grams, millilitres or piece/pack/can …
Table and column names are German and stay (schrank, unterwegs, schrank_buchung, barcode)."""
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

import store
from store import Error

# shelf-stable = goes into the pantry. Fresh produce, meat, dairy and frozen food do not.
SHELF_STABLE_CATEGORIES = {"Gewürze", "Würzmittel & Saucen", "Getreide", "Konserven", "Backzutaten", "Snacks", "Alkohol",
                      "Süßigkeiten", "Öle", "Getränke"}
SHELF_STABLE_NAMES = {"zwiebel", "rote zwiebel", "knoblauch", "kartoffel", "süßkartoffel", "ingwerwurzel"}
_SPOON_UNITS = {"EL", "TL", "Prise", "Messerspitze", "Schuss", "Spritzer"}
_lock = threading.Lock()
_last_checked = store.PerHousehold()  # last Bring check, per household


def init():
    with store.con() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS schrank (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              menge REAL,                          -- in einheit; NULL = there, amount unknown
              einheit TEXT NOT NULL DEFAULT '',    -- g, ml, Stück, Packung, Dose …
              kategorie TEXT NOT NULL DEFAULT 'Sonstiges',
              notiz TEXT NOT NULL DEFAULT '',
              geaendert TEXT
            );
            -- sent to Bring, not seen as bought yet
            CREATE TABLE IF NOT EXISTS unterwegs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, menge REAL, einheit TEXT NOT NULL DEFAULT '', gesendet TEXT NOT NULL
            );
            -- every change to the pantry, for undo. aenderungen: [{id, name, einheit, kategorie, alt, neu}]
            CREATE TABLE IF NOT EXISTS schrank_buchung (
              id INTEGER PRIMARY KEY AUTOINCREMENT, art TEXT NOT NULL, schluessel TEXT, text TEXT NOT NULL,
              aenderungen TEXT NOT NULL, am TEXT NOT NULL, rueckgaengig INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS schrank_buchung_schluessel ON schrank_buchung(schluessel);
            CREATE TABLE IF NOT EXISTS barcode (
              code TEXT PRIMARY KEY, produkt TEXT, marke TEXT, zutat TEXT, menge REAL, einheit TEXT, geaendert TEXT
            );
        """)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(einkauf)")}
        if "manuell" not in cols:  # check mark set/removed by hand: the pantry reconciliation leaves it alone
            c.execute("ALTER TABLE einkauf ADD COLUMN manuell INTEGER NOT NULL DEFAULT 0")
        if "schrank" not in cols:  # how much of it is in the pantry (text for the note)
            c.execute("ALTER TABLE einkauf ADD COLUMN schrank TEXT NOT NULL DEFAULT ''")


def shelf_stable(name, cat=None):
    if (name or "").lower() in SHELF_STABLE_NAMES:
        return True
    return (cat if cat is not None else store.category(name)) in SHELF_STABLE_CATEGORIES


def _base(e, name):
    return store.base_unit(e or "", name)


def _text(amount, e):
    return "da" if amount is None else " ".join(x for x in (store.fmt_number(amount), e) if x)


# ---------- stock ----------
def overview():
    with store.con() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM schrank ORDER BY kategorie, name COLLATE NOCASE")]
        bookings = [dict(r) for r in c.execute("SELECT id, art, text, am, rueckgaengig FROM schrank_buchung ORDER BY id DESC LIMIT 8")]
        in_transit = [dict(r) for r in c.execute("SELECT name, menge, einheit, gesendet FROM unterwegs ORDER BY name")]
    for r in rows:
        r["text"] = _text(r["menge"], r["einheit"])
    return {"schrank": rows, "buchungen": bookings, "unterwegs": in_transit, "kategorien": sorted(SHELF_STABLE_CATEGORIES),
            "haltbar_namen": sorted(SHELF_STABLE_NAMES)}


def _find(c, name, e):
    """Existing row for the ingredient with a matching base (g to kg, Stück to no unit …)."""
    b = _base(e, name)[0]
    for r in c.execute("SELECT * FROM schrank WHERE name=? COLLATE NOCASE", (name,)):
        if _base(r["einheit"], name)[0] == b:
            return r
    return None


def stock_for(c, name, e):
    """For the shopping list: (available in the base unit of the need, text), or (None, text) = surely covered
        (spoonfuls from an opened pack), or (0, "") = nothing there."""
    rows = list(c.execute("SELECT * FROM schrank WHERE name=? COLLATE NOCASE", (name,)))
    if not rows:
        return 0, ""
    b, _ = _base(e, name)
    for r in rows:
        rb, rf = _base(r["einheit"], name)
        if rb == b and r["menge"] is not None:
            return r["menge"] * rf, _text(r["menge"], r["einheit"])
    if b == "el" or e in _SPOON_UNITS:  # spices, sauces: any pack in the pantry is enough for spoonfuls
        return None, _text(rows[0]["menge"], rows[0]["einheit"])
    if any(r["menge"] is None for r in rows):
        return None, "da"
    return 0, ""


def _apply(c, changes):
    """changes: [{name, einheit, kategorie, delta}] or [{id, setzen}] → writes them and returns the log for undo."""
    log = []
    for a in changes:
        r = c.execute("SELECT * FROM schrank WHERE id=?", (a["id"],)).fetchone() if a.get("id") else _find(c, a["name"], a.get("einheit", ""))
        if r:
            old = dict(r)
            if a.get("leer"):
                new = 0.0
            elif "setzen" in a:
                new = a["setzen"]  # None = there, amount unknown
            elif r["menge"] is None:
                continue  # amount unknown: buying and using do not change that something is there
            else:  # delta in the unit of the change → unit of the row (same base, e.g. kg and g)
                fa, fr = _base(a.get("einheit") or r["einheit"], r["name"])[1], _base(r["einheit"], r["name"])[1]
                new = r["menge"] + a["delta"] * fa / fr
            if new is not None and new <= 1e-6:
                c.execute("DELETE FROM schrank WHERE id=?", (r["id"],))
                log.append({"alt": old, "neu": None})
            elif new != r["menge"]:
                c.execute("UPDATE schrank SET menge=?, geaendert=? WHERE id=?", (new, store.now(), r["id"]))
                log.append({"alt": old, "neu": {**old, "menge": new}})
        elif a.get("delta", 0) > 0 or "setzen" in a:
            amount = a["setzen"] if "setzen" in a else a["delta"]
            iid = c.execute("INSERT INTO schrank(name, menge, einheit, kategorie, notiz, geaendert) VALUES(?,?,?,?,?,?)",
                            (a["name"], amount, a.get("einheit", ""), a.get("kategorie") or store.category(a["name"]),
                             a.get("notiz", ""), store.now())).lastrowid
            log.append({"alt": None, "neu": dict(c.execute("SELECT * FROM schrank WHERE id=?", (iid,)).fetchone())})
    return log


def book(changes, kind, text, once_key=None):
    with store.lock, store.con() as c:
        if once_key and c.execute("SELECT 1 FROM schrank_buchung WHERE schluessel=? AND rueckgaengig=0", (once_key,)).fetchone():
            return None
        log = _apply(c, changes)
        if not log:
            return None
        return c.execute("INSERT INTO schrank_buchung(art, schluessel, text, aenderungen, am) VALUES(?,?,?,?,?)",
                         (kind, once_key, text, json.dumps(log, ensure_ascii=False), store.now())).lastrowid


def undo(bid):
    with store.lock, store.con() as c:
        b = c.execute("SELECT * FROM schrank_buchung WHERE id=? AND rueckgaengig=0", (bid,)).fetchone()
        if not b:
            raise Error("Buchung nicht gefunden oder schon zurückgenommen")
        for p in reversed(json.loads(b["aenderungen"])):
            if p["neu"] and not p["alt"]:
                c.execute("DELETE FROM schrank WHERE id=?", (p["neu"]["id"],))
            elif p["alt"]:
                a = p["alt"]
                c.execute("INSERT OR REPLACE INTO schrank(id, name, menge, einheit, kategorie, notiz, geaendert) VALUES(?,?,?,?,?,?,?)",
                          (a["id"], a["name"], a["menge"], a["einheit"], a["kategorie"], a["notiz"], store.now()))
        c.execute("UPDATE schrank_buchung SET rueckgaengig=1 WHERE id=?", (bid,))
    return {"ok": True}


def set_amount(iid, amount=None, unit=None, empty=False, note=None):
    """By hand: set the amount or empty (row goes away)."""
    with store.con() as c:
        r = c.execute("SELECT * FROM schrank WHERE id=?", (iid,)).fetchone()
    if not r:
        raise Error("Nicht im Schrank")
    if unit is not None and store.unit(unit) != r["einheit"]:
        with store.lock, store.con() as c:
            c.execute("UPDATE schrank SET einheit=? WHERE id=?", (store.unit(unit), iid))
    if note is not None:
        with store.lock, store.con() as c:
            c.execute("UPDATE schrank SET notiz=? WHERE id=?", (str(note)[:200], iid))
    bid = book([{"id": iid, "setzen": None if amount in (None, "") else float(amount), "leer": empty}], "hand",
                 f"{r['name']}: {'leer' if empty else _text(None if amount in (None, '') else float(amount), store.unit(unit) if unit else r['einheit'])}")
    return {"ok": True, "buchung": bid}


def add(name, amount=None, unit="", category=None, note=""):
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:120]
    if not name:
        raise Error("Was kommt in den Schrank?")
    e = store.unit(unit)
    m = None if amount in (None, "") else float(str(amount).replace(",", "."))
    cat = category or store.category(name)
    with store.con() as c:
        existing = _find(c, name, e)
    if existing and m is not None and existing["menge"] is not None:  # already there: add to it
        bid = book([{"name": name, "einheit": e, "delta": m}], "hand", f"+ {_text(m, e)} {name}")
    elif existing:
        bid = book([{"id": existing["id"], "setzen": m}], "hand", f"{name}: {_text(m, e)}")
    else:
        bid = book([{"name": name, "einheit": e, "kategorie": cat, "setzen": m, "notiz": note}], "hand", f"{name}: {_text(m, e)}")
    return {"ok": True, "buchung": bid}


# ---------- bought (Bring) ----------
def remember_sent(lines):
    """lines: [{name, menge, einheit}] from the list that just went to Bring (only shelf-stable items are kept)."""
    with store.lock, store.con() as c:
        for z in lines:
            if shelf_stable(z["name"]):
                c.execute("DELETE FROM unterwegs WHERE name=? COLLATE NOCASE", (z["name"],))
                c.execute("INSERT INTO unterwegs(name, menge, einheit, gesendet) VALUES(?,?,?,?)",
                          (z["name"], z.get("menge"), z.get("einheit") or "", store.now()))


def _pack_in_grams(c, name, amount, e):
    """1 pack of chia seeds → 250 g, if a barcode knows the pack size."""
    if e not in ("Packung", "Dose", "Glas", "Flasche", "Tube", "Becher") or not amount:
        return amount, e
    r = c.execute("SELECT menge, einheit FROM barcode WHERE zutat=? COLLATE NOCASE AND menge > 0 ORDER BY geaendert DESC", (name,)).fetchone()
    return (amount * r["menge"], r["einheit"]) if r else (amount, e)


def check_purchases(fetch_bring, force=False):
    """fetch_bring: function that fetches the Bring list ({purchase, recently}). Books what was checked off."""
    with _lock:
        if not force and time.time() - _last_checked.get("t", 0.0) < 600:
            return None
        _last_checked["t"] = time.time()
        with store.con() as c:
            pending = [dict(r) for r in c.execute("SELECT * FROM unterwegs")]
        if not pending:
            return {"gebucht": []}
        cur = fetch_bring()
        bought = {p["name"].lower() for p in cur.get("recently") or []}
        still_open = {p["name"].lower() for p in cur.get("purchase") or []}
        deltas, drop, texts = [], [], []
        cutoff = (datetime.now(timezone.utc) - timedelta(days=21)).isoformat()
        with store.con() as c:
            for u in pending:
                n = u["name"].lower()
                if n in bought and n not in still_open:
                    m, e = _pack_in_grams(c, u["name"], u["menge"], u["einheit"])
                    deltas.append({"name": u["name"], "einheit": e, "delta": m or 1, "kategorie": store.category(u["name"])})
                    texts.append(f"{_text(m or 1, e)} {u['name']}")
                    drop.append(u["id"])
                elif n not in still_open and n not in bought or u["gesendet"] < cutoff:
                    drop.append(u["id"])  # deleted in Bring or too old: not bought
        bid = book(deltas, "kauf", "Gekauft: " + ", ".join(texts)) if deltas else None
        with store.lock, store.con() as c:
            for i in drop:
                c.execute("DELETE FROM unterwegs WHERE id=?", (i,))
        return {"gebucht": texts, "buchung": bid}


# ---------- cooking ----------
def _usage(slug, factor):
    """Shelf-stable ingredients of a recipe with amount × factor that are in the pantry."""
    r = store.recipe(slug)
    out, texts = [], []
    with store.con() as c:
        for z in r["zutaten"]:
            if not z["name"] or not z["menge"] or not shelf_stable(z["name"]):
                continue
            if z["einheit"] in _SPOON_UNITS:
                continue  # spoonfuls from the pack: the stock stays „da“
            row = _find(c, z["name"], z["einheit"])
            if not row or row["menge"] is None:
                continue
            m = z["menge"] * factor
            out.append({"name": z["name"], "einheit": z["einheit"], "delta": -m})
            texts.append(f"{_text(round(m, 1), z['einheit'])} {z['name']}")
    return r, out, texts


def cook(slug, factor, once_key):
    r, deltas, texts = _usage(slug, factor)
    if not deltas:
        return None
    bid = book(deltas, "kochen", f"{r['name']}: " + ", ".join(texts), once_key)
    return {"buchung": bid, "text": "Aus dem Schrank: " + ", ".join(texts)} if bid else None


def cook_plan_entry(eid):
    """Cooking day from the plan (Gegessen or cooking mode): only once per entry."""
    try:
        e = store.plan_entry(int(eid))
    except (Error, ValueError):
        return None
    if not e["rezept_id"]:
        return None
    r = store.recipe(e["rezept_id"])
    return cook(r["slug"], e["scale"] or 1, f"plan:{e['id']}")


def cook_free(slug, portions):
    """Cooking mode without a plan entry: matching plan entry today or yesterday, otherwise by servings."""
    today = date.today()
    with store.con() as c:
        r = store.recipe(slug)
        e = c.execute("""SELECT p.id FROM plan p WHERE p.rezept_id=? AND p.datum BETWEEN ? AND ?
                         AND NOT EXISTS (SELECT 1 FROM schrank_buchung b WHERE b.schluessel = 'plan:' || p.id AND b.rueckgaengig=0)
                         ORDER BY p.datum DESC LIMIT 1""", (r["id"], str(today - timedelta(days=1)), str(today))).fetchone()
    if e:
        return cook_plan_entry(e["id"])
    return cook(slug, float(portions or r["portionen"]) / float(r["portionen"] or 1), f"koch:{slug}:{today}")


def cook_mealprep(day, slugs):
    """Meal prep done: all cooking days of these recipes from the prep day to 8 days later."""
    d = date.fromisoformat(day)
    with store.con() as c:
        ids = [r["id"] for r in c.execute("""SELECT p.id FROM plan p JOIN rezept r ON r.id = p.rezept_id
                                             WHERE p.datum BETWEEN ? AND ? AND r.slug IN (%s)""" % ",".join("?" * len(slugs)),
                                          (str(d), str(d + timedelta(days=8)), *slugs))]
    texts, bookings = [], []
    for i in ids:
        x = cook_plan_entry(i)
        if x:
            texts.append(x["text"].replace("Aus dem Schrank: ", ""))
            bookings.append(x["buchung"])
    return {"buchungen": bookings, "text": ("Aus dem Schrank: " + "; ".join(texts)) if texts else ""}


# ---------- what can I cook with what is there ----------
# never counted, neither as there nor as missing: basics every kitchen has. The household's „always at home“ list
# (meta immer_da, also used by the shopping list) is left out the same way.
BASICS = {"salz", "pfeffer", "schwarzer pfeffer", "wasser", "öl", "olivenöl", "rapsöl", "sonnenblumenöl", "pflanzenöl",
          "speiseöl", "salt", "pepper", "black pepper", "water", "oil", "olive oil", "vegetable oil", "sunflower oil",
          "rapeseed oil", "canola oil", "salt and pepper", "salz und pfeffer", "eis", "ice", "eiswürfel", "ice cubes"}
_NOISE = {"tk", "tiefgekühlt", "tiefgekühlte", "tiefgekühlter", "gefroren", "gefrorene", "gefrorener", "frozen", "bio",
          "frisch", "frische", "frischer", "fresh", "eingefroren", "portion", "portionen"}
SOON_DAYS = 14  # freezer items that run out within two weeks count as „soon“


def _key(name):
    """Comparable form of an ingredient name: lower case, without brackets, frozen/organic words and punctuation."""
    s = re.sub(r"\([^)]*\)", " ", str(name or "").lower())
    s = re.sub(r"[^\wäöüß ]+", " ", s)
    return " ".join(w for w in s.split() if w not in _NOISE)


def _stem(s):
    """Crude singular for German and English endings, enough to match „Kichererbsen“ with „Kichererbse“, „tomatoes“ with „tomato“."""
    for end in ("es", "en", "n", "e", "s"):
        if s.endswith(end) and len(s) - len(end) >= 4:
            return s[:-len(end)]
    return s


def _same(a, b):
    """Is the stock item a the ingredient b? Equal, or one ends with the other (German compounds: „Blattspinat“ is
    „Spinat“, „Rinderhackfleisch“ is „Hackfleisch“; English „baby spinach“ is „spinach“)."""
    if not a or not b:
        return False
    a, b = _stem(a), _stem(b)
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    if short in _STRICT or _stem(short) in _STRICT:  # „Kokosmilch“ is no „Milch“, „peanut butter“ no „butter“
        return False
    return len(short) >= 4 and long_.endswith(short)


_STRICT = {_stem(w) for w in ("milch", "butter", "mehl", "essig", "sauce", "soße", "sosse", "brühe", "creme", "sahne", "pulver",
                              "saft", "paste", "mark", "käse", "milk", "flour", "vinegar", "stock", "broth", "powder", "juice",
                              "cheese", "cream", "sugar", "zucker")}


def _stock(c):
    """Everything in the cupboard and freezer: [{key, name, quelle: schrank|vorrat, menge, einheit, bald, haltbar_bis, text}]."""
    out, today = [], date.today()
    for r in c.execute("SELECT * FROM schrank WHERE menge IS NULL OR menge > 0"):
        out.append({"key": _key(r["name"]), "name": r["name"], "quelle": "schrank", "menge": r["menge"], "einheit": r["einheit"],
                    "bald": False, "haltbar_bis": None, "text": _text(r["menge"], r["einheit"])})
    for r in c.execute("SELECT * FROM vorrat WHERE anzahl > 0"):
        soon = False
        if r["haltbar_bis"]:
            try:
                soon = (date.fromisoformat(r["haltbar_bis"]) - today).days <= SOON_DAYS
            except ValueError:
                pass
        out.append({"key": _key(r["name"]), "name": r["name"], "quelle": "vorrat", "menge": r["anzahl"], "einheit": r["einheit"],
                    "bald": soon, "haltbar_bis": r["haltbar_bis"], "slug": r["recipe_slug"] or "",
                    "text": f"{store.fmt_number(r['anzahl'])} {r['einheit']}".strip()})
    return out


def _always(c):
    import shopping  # late import: shopping imports pantry
    r = c.execute("SELECT value FROM meta WHERE key='immer_da'").fetchone()
    a = json.loads(r["value"]) if r else shopping.ALWAYS_DEFAULT
    return {n.lower() for n in a.get("namen") or []}, {k.lower() for k in a.get("kategorien") or []}


def _enough(item, z):
    """False if the cupboard amount is known, in the same base and clearly less than the recipe needs."""
    if item["quelle"] != "schrank" or item["menge"] is None or not z["menge"]:
        return True
    bi, fi = _base(item["einheit"], z["name"])
    bz, fz = _base(z["einheit"], z["name"])
    return bi != bz or item["menge"] * fi >= z["menge"] * fz * 0.8


def cookable(limit=30, slug=None):
    """Own recipes ranked by how much of them is in the cupboard and freezer, soon-expiring freezer items first.
    → {"rezepte": [{slug, name, …, da, fehlt, knapp, treffer, gesamt, quote, bald}], "bald": [stock items running out], "vorrat": n}.
    Salt, pepper, oil, water and the „always at home“ list are left out of the count."""
    with store.con() as c:
        stock = _stock(c)
        names, cats = _always(c)
        lexicon = {r["name"].lower(): r["kategorie"] for r in c.execute("SELECT name, kategorie FROM zutat")}
    ready = {s["slug"] for s in stock if s["quelle"] == "vorrat" and s.get("slug")}  # frozen portions of a recipe
    stock = [s for s in stock if s["key"] and not (s["quelle"] == "vorrat" and s.get("slug"))]
    out = []
    for r in store.all_recipes():
        if slug and r["slug"] != slug:
            continue
        have, missing, low, seen = [], [], [], set()
        for z in r["zutaten"]:
            k = _key(z["name"])
            if not k or k in seen:
                continue
            seen.add(k)
            low_name = z["name"].lower()
            if k in BASICS or low_name in BASICS or low_name in names or lexicon.get(low_name, "").lower() in cats:
                continue
            hit = next((s for s in stock if _same(s["key"], k)), None)
            if hit and _enough(hit, z):
                have.append({"name": z["name"], "quelle": hit["quelle"], "vorrat": hit["name"], "text": hit["text"], "bald": hit["bald"]})
            else:
                miss = {"name": z["name"], "menge": z["menge"] or None, "einheit": z["einheit"]}
                (low if hit else missing).append({**miss, **({"vorrat": hit["text"]} if hit else {})})
        total = len(have) + len(missing) + len(low)
        if not total or (not have and not low and not slug):
            continue
        n = r["naehrwerte"] or {}
        z = r.get("zeiten") or {}
        out.append({"slug": r["slug"], "name": r["name"], "bild": bool(r["bild"]), "id": r["id"], "kcal": n.get("kcal"),
                    "zeit": z.get("gesamt") or z.get("kochen") or "", "portionen": r["portionen"], "bewertung": r["bewertung"],
                    "kategorien": r["kategorien"], "da": have, "fehlt": missing, "knapp": low, "treffer": len(have), "gesamt": total,
                    "quote": round(len(have) / total, 2), "bald": sum(1 for h in have if h["bald"]),
                    "eingefroren": r["slug"] in ready})
    out.sort(key=lambda x: (-(x["quote"] + 0.15 * min(x["bald"], 2)), len(x["fehlt"]) + len(x["knapp"]), -(x["bewertung"] or 0), x["name"].lower()))
    soon = sorted((s for s in _stock_soon()), key=lambda s: s["haltbar_bis"] or "")
    return {"rezepte": out[:max(1, min(int(limit or 30), 200))], "bald": soon, "vorrat": len(stock)}


def _stock_soon():
    with store.con() as c:
        return [{k: s[k] for k in ("name", "quelle", "text", "haltbar_bis")} for s in _stock(c) if s["bald"]]


def missing_to_list(slug, portions, always):
    """Only what is missing (or too little) for a recipe onto the shopping list, scaled to the servings."""
    found = cookable(slug=slug)["rezepte"]
    if not found:
        raise Error("Rezept nicht gefunden")
    r = found[0]
    names = {z["name"].lower() for z in r["fehlt"] + r["knapp"]}
    if not names:
        return {"ok": True, "name": r["name"], "zeilen_neu": 0, "fehlt": [], "fehler": []}
    factor = max(0.1, float(portions or r["portionen"] or 1)) / float(r["portionen"] or 1)
    res = store.add_to_list([(slug, factor)], always, only=names)
    return {"ok": True, "name": r["name"], "fehlt": sorted(names), **res}


# ---------- barcode (Open Food Facts) ----------
OFF = "https://world.openfoodfacts.org/api/v2/product/{}.json?fields=product_name,product_name_de,generic_name_de,brands,quantity,product_quantity,product_quantity_unit"


def _guess_ingredient(product):
    """Longest lexicon name contained in the product name (ignoring spaces and hyphens)."""
    norm = lambda s: re.sub(r"[\s\-]+", "", s.lower())  # noqa: E731
    p = norm(product)
    with store.con() as c:
        names = [r["name"] for r in c.execute("SELECT name FROM zutat")]
    hits = [n for n in names if len(n) >= 3 and norm(n) in p]
    return max(hits, key=len) if hits else ""


def barcode(code):
    code = re.sub(r"\D", "", str(code or ""))
    if not 6 <= len(code) <= 14:
        raise Error("Ungültiger Barcode")
    with store.con() as c:
        r = c.execute("SELECT * FROM barcode WHERE code=?", (code,)).fetchone()
    if r:
        d = dict(r)
        return {**d, "kategorie": store.category(d["zutat"]) if d["zutat"] else "Sonstiges", "bekannt": True,
                "im_schrank": _in_pantry(d["zutat"])}
    req = urllib.request.Request(OFF.format(code), headers={"User-Agent": "Kombuese (self-hosted kitchen app)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            j = json.load(resp)
    except Exception:  # noqa: BLE001
        j = {}
    p = j.get("product") or {}
    product = (p.get("product_name_de") or p.get("product_name") or p.get("generic_name_de") or "").strip()
    amount, unit = None, ""
    try:
        if p.get("product_quantity"):
            amount = float(p["product_quantity"])
            unit = {"g": "g", "ml": "ml", "kg": "g", "l": "ml"}.get((p.get("product_quantity_unit") or "g").lower(), "g")
            if (p.get("product_quantity_unit") or "").lower() in ("kg", "l"):
                amount *= 1000
    except (TypeError, ValueError):
        pass
    if not amount:
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|g|ml|l|cl)\b", str(p.get("quantity") or ""), re.I)
        if m:
            amount = float(m.group(1).replace(",", ".")) * {"kg": 1000, "l": 1000, "cl": 10}.get(m.group(2).lower(), 1)
            unit = "g" if m.group(2).lower() in ("g", "kg") else "ml"
    ingredient = _guess_ingredient(product) if product else ""
    return {"code": code, "produkt": product, "marke": (p.get("brands") or "").split(",")[0].strip(), "zutat": ingredient or product,
            "menge": amount, "einheit": unit, "kategorie": store.category(ingredient) if ingredient else "Sonstiges",
            "bekannt": False, "gefunden": bool(product), "im_schrank": _in_pantry(ingredient or product)}


def _in_pantry(name):
    if not name:
        return []
    with store.con() as c:
        return [{**dict(r), "text": _text(r["menge"], r["einheit"])} for r in c.execute("SELECT * FROM schrank WHERE name=? COLLATE NOCASE", (name,))]


def book_barcode(code, ingredient, amount, unit, action, category=None, product="", brand=""):
    """action: rein (pack into the pantry) | leer (pack used up, subtract) | merken (only remember the mapping)."""
    code = re.sub(r"\D", "", str(code or ""))
    ingredient = re.sub(r"\s+", " ", str(ingredient or "")).strip()[:120]
    if not code or not ingredient:
        raise Error("Barcode und Zutat nötig")
    e = store.unit(unit) if unit else "Packung"
    m = float(str(amount).replace(",", ".")) if amount not in (None, "") else (1 if e == "Packung" else None)
    with store.lock, store.con() as c:
        c.execute("""INSERT INTO barcode(code, produkt, marke, zutat, menge, einheit, geaendert) VALUES(?,?,?,?,?,?,?)
                     ON CONFLICT(code) DO UPDATE SET zutat=excluded.zutat, menge=excluded.menge, einheit=excluded.einheit,
                     produkt=COALESCE(NULLIF(excluded.produkt,''), produkt), marke=COALESCE(NULLIF(excluded.marke,''), marke),
                     geaendert=excluded.geaendert""", (code, product, brand, ingredient, m, e, store.now()))
        if category:
            c.execute("INSERT INTO zutat(name, kategorie) VALUES(?,?) ON CONFLICT(name) DO NOTHING", (ingredient, category))
    if action == "rein":
        deltas, extra = [], 0.0
        if e in ("g", "ml") and m:  # „1 Packung Chiasamen“ from shopping: now the pack size is known → in grams
            with store.con() as c:
                for r in c.execute("SELECT * FROM schrank WHERE name=? COLLATE NOCASE AND einheit IN ('Packung','Dose','Glas','Flasche','Tube','Becher') "
                                   "AND menge IS NOT NULL", (ingredient,)):
                    deltas.append({"id": r["id"], "leer": True})
                    extra += r["menge"] * m
        deltas.append({"name": ingredient, "einheit": e, "delta": (m or 1) + extra, "kategorie": category or store.category(ingredient)})
        bid = book(deltas, "scan", f"Gescannt: + {_text(m, e)} {ingredient}" + (f" (Packung im Schrank jetzt in {e})" if extra else ""))
    elif action == "leer":
        bid = book([{"name": ingredient, "einheit": e, "delta": -(m or 1)}], "scan", f"Aufgebraucht: {_text(m, e)} {ingredient}")
    else:
        bid = None
    return {"ok": True, "buchung": bid}
