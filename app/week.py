"""Week view for Kombüse: the week plan Monday to Sunday and the nutrients per meal (for Yazio).
The data comes from store.py (Mealie before)."""
import re
from datetime import date, timedelta

import household
import store
from store import Error

LEFTOVER = re.compile(r"(?:rest|reste|leftovers?)\s*:", re.I)
ORDER = {"breakfast": 0, "lunch": 1, "dinner": 2, "side": 3, "snack": 4, "dessert": 5, "drink": 6}


def plan(start=None):
    d = date.fromisoformat(start) if start and re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) else date.today()
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    out = []
    for e in store.plan_week(monday, sunday):
        title, text = (e["titel_roh"] or "").strip(), (e["text"] or "").strip()
        r = e["rezept"]
        out.append({
            "id": e["id"], "date": e["date"], "typ": e["typ"], "titel": (r or {}).get("name") or title or "?",
            "rezept": r, "scale": e["scale"], "text": text,
            "rest": bool(LEFTOVER.match(title)),  # "Rest: …" or "Leftovers: …" (English)
            "gefroren": bool(LEFTOVER.match(title)) and bool(re.search(r"eingefroren|frozen|❄", text + title, re.I)),
        })
    out.sort(key=lambda x: (x["date"], ORDER.get(x["typ"], 9)))
    return {"start": str(monday), "ende": str(sunday), "heute": str(date.today()), "eintraege": out}


def _number(v):
    try:
        return float(str(v).replace(",", ".").split()[0])
    except (ValueError, IndexError, TypeError):
        return None


def meal(entry_id):
    """One plan entry with its recipe and nutrients per serving. Leftover entries without a recipe are found
        by the recipe name in the title ("Rest: Chicken Teriyaki")."""
    e, r = _entry(entry_id)
    if not r:
        raise Error("Kein Rezept zu diesem Eintrag, bitte in Yazio selbst eintragen")
    return {"id": e["id"], "datum": e["datum"], "typ": e["typ"], **recipe(r["slug"])}


def _entry(entry_id):
    if not re.fullmatch(r"\d{1,9}", str(entry_id)):
        raise Error("ungültige ID")
    e = store.plan_entry(int(entry_id))
    r = store.recipe(e["rezept_id"]) if e["rezept_id"] else None
    entry_title = (e["titel"] or "").strip()
    if not r and entry_title:
        name = household.strip_leftover(entry_title)  # "Rest: …" or "Leftovers: …"
        r = store.find_recipe(name)
    return e, r


def entry_recipe(entry_id):
    """The recipe of a plan entry, leftovers found by name (for reheating)."""
    r = _entry(entry_id)[1]
    if not r:
        raise Error("Kein Rezept zu diesem Eintrag")
    return r


# side dish when the recipe mentions rice only in the notes: 60 g raw rice per serving
RICE = {"kcal": 210.0, "protein": 4.2, "fett": 0.4, "kh": 47.0}


def recipe(slug):
    """Nutrients per serving of a recipe, plus hints for a rice side dish, category and how long it keeps frozen."""
    if not re.fullmatch(r"[a-z0-9-]{1,120}", slug or ""):
        raise Error("ungültiges Rezept")
    full = store.recipe(slug)
    n = full["naehrwerte"]
    nw = {"kcal": n.get("kcal"), "protein": n.get("protein"), "fett": n.get("fett"), "kh": n.get("kh")}
    if not nw["kcal"]:
        raise Error(f"„{full['name']}“ hat keine Nährwerte")
    ingr = " ".join(f"{z['name']} {z['notiz']}" for z in full["zutaten"]).lower()
    notes_text = " ".join(x["text"] for x in full["notizen"]).lower()
    rice = "reis" in notes_text and not re.search(r"\breis\b|basmati|naturreis|jasminreis", ingr)
    m = re.search(r"(\d+)\s*(?:monat|month)", notes_text)  # English notes
    cat = "fruehstueck" if "Frühstück" in full["kategorien"] else "haupt"
    return {"name": full["name"], "slug": full["slug"], "pro_portion": nw, "beilage_reis": rice, "kategorie": cat,
            "einfrieren": not re.search(r"nicht einfrieren|do not freeze|don.t freeze|not suitable for freezing", notes_text), "monate": int(m.group(1)) if m else 2}
