"""Recipes for the app's screens: list, detail with the edit texts, saving, rating, planning and shopping.
A thin layer over store.py (Mealie before)."""
import re
from datetime import date

import household
import reheat
import store
from store import Error

SLUG = re.compile(r"[a-z0-9-]{1,120}")


def _time(r):
    z = r.get("zeiten") or {}
    return z.get("gesamt") or z.get("kochen") or z.get("vorbereitung") or ""


def list_all():
    out = []
    for r in store.all_recipes():
        n = r["naehrwerte"]
        out.append({"id": r["id"], "slug": r["slug"], "name": r["name"], "kategorien": r["kategorien"], "tags": r["tags"],
                    "zeit": _time(r), "portionen": r["portionen"], "kcal": n.get("kcal"), "protein": n.get("protein"),
                    "bewertung": r["bewertung"], "bild": bool(r["bild"]), "quelle": r["quelle"], "sprache": r["sprache"],
                    "aufwaermen": bool(r.get("aufwaermen"))})  # has its own reheating field
    return out


def detail(slug):
    if not SLUG.fullmatch(slug or ""):
        raise Error("ungültiges Rezept")
    r = store.recipe(slug)
    ingr = [{**z, "text": store.format_line(z).replace("; ", ", "), "kategorie": store.category(z["name"]) if z["name"] else ""}
           for z in r["zutaten"]]
    return {**r, "bild": bool(r["bild"]), "zutaten": ingr,
            "zutaten_text": "\n".join(store.format_line(z) for z in r["zutaten"]),
            "schritte_text": _write_blocks(r["schritte"]), "notizen_text": _write_blocks(r["notizen"]),
            # reheating for one portion in the household microwave, the estimate if the recipe has no field
            "aufwaermen_info": reheat.info(r, household.microwave_watt())}


def reheating(slug=None, entry=None, portions=1, watt=None):
    """GET /api/aufwaermen: reheating of a recipe or of a plan entry (leftovers „Rest: Chili“, frozen portions) for
        this many portions; watt = the own microwave (default the household setting)."""
    if entry:
        import week
        r = week.entry_recipe(entry)
    elif SLUG.fullmatch(slug or ""):
        r = store.recipe(slug)
    else:
        raise Error("ungültiges Rezept")
    return {"slug": r["slug"], "name": r["name"], **reheat.info(r, watt or household.microwave_watt(), portions)}


def _write_blocks(blocks):
    return "\n\n".join((f"## {s['titel']}\n" if s["titel"] else "") + s["text"] for s in blocks)


def _read_blocks(text):
    """„## Title“ (optional) and text, blocks separated by blank lines (steps, notes)."""
    out = []
    for block in re.split(r"\n\s*\n", str(text or "").strip()):
        lines = block.strip().splitlines()
        if not lines:
            continue
        title = lines[0].lstrip("# ").strip() if lines[0].startswith("#") else ""
        t = "\n".join(lines[1:] if title else lines).strip()
        if t:
            out.append({"titel": title, "text": t})
    return out


def save(slug, data):
    """From the app (edit) or from AI runs: zutaten_text, schritte_text and notizen_text are read like the
        edit fields; alternatively zutaten/schritte/notizen as lists (shape see store.py)."""
    d = dict(data)
    if "zutaten_text" in d:
        d["zutaten"] = [store.parse_line(t) for t in str(d.pop("zutaten_text")).splitlines() if t.strip()]
    if "schritte_text" in d:
        d["schritte"] = _read_blocks(d.pop("schritte_text"))
    if "notizen_text" in d:
        d["notizen"] = _read_blocks(d.pop("notizen_text"))
    return store.save(d, slug=slug)


def to_shopping_list(slug, portions, always):
    r = store.recipe(slug)
    f = store.add_to_list([(slug, max(0.1, float(portions)) / float(r["portionen"] or 1))], always)
    return {"ok": True, "name": r["name"], "portionen": portions, **f}


def schedule(slug, day, meal_type, portions):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
        day = str(date.today())
    r = store.recipe(slug)
    store.add_plan_entry(day, meal_type, slug, scale=max(0.1, float(portions)) / float(r["portionen"] or 1))
    return {"ok": True, "name": r["name"], "datum": day}


def rating(slug):
    """The user's stars for a recipe (None = not rated yet)."""
    return store.recipe(slug)["bewertung"]


def rate(slug, stars):
    if not SLUG.fullmatch(slug or ""):
        raise Error("ungültiges Rezept")
    return {"ok": True, "sterne": store.rate(slug, stars)}


def add_plan_entry(day, meal_type, slug=None, title="", text="", scale=1.0):
    """One week plan entry: with a recipe (counts for shopping) or only title/text (leftovers, canteen)."""
    return store.add_plan_entry(day, meal_type, slug or None, title, text, scale)


def add_to_list(entries, always):
    """Several recipes [(slug, factor)] on the list in one go, ready for shopping. Returns (count, errors)."""
    r = store.add_to_list([(s, float(f or 1)) for s, f in entries], always)
    return len(entries) - len(r["fehler"]), [f"Liste {x}" for x in r["fehler"]]
