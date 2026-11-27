r"""Import from Mealie into Kombüse (for moving over from Mealie). Runs inside the Kombüse container:
  docker cp tools/import_mealie.py kombuese:/tmp/ && docker exec -e MEALIE_API=http://mealie:9000/api \
    -e MEALIE_TOKEN=... kombuese python /tmp/import_mealie.py [--dry-run]
Takes recipes (IDs and slugs stay), images, the user's star ratings, foods with their shopping category, all meal plan
entries (IDs stay) and open shopping list items. Overwrites what was imported before, so it can be repeated as long as
nothing new was created in Kombüse. --dry-run (old name --trocken) = only fetch and show two converted recipes."""
import json
import os
import sys
import urllib.request

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

API = os.environ.get("MEALIE_API", "http://mealie:9000/api")
TOKEN = os.environ.get("MEALIE_TOKEN") or open(os.path.join(os.environ.get("KOMBUESE_SECRETS", "/secrets"), "mealie_token"),
                                                encoding="utf-8-sig").read().strip()
DRY_RUN = "--dry-run" in sys.argv or "--trocken" in sys.argv


def get(path, raw=False):
    req = urllib.request.Request(API + path, headers={"Authorization": "Bearer " + TOKEN})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read() if raw else json.load(r)


NW = {"calories": "kcal", "proteinContent": "protein", "carbohydrateContent": "kh", "sugarContent": "zucker",
      "fatContent": "fett", "saturatedFatContent": "gesfett", "fiberContent": "ballaststoffe", "sodiumContent": "natrium"}


def convert(m):
    ingr = []
    for i in m.get("recipeIngredient") or []:
        food, unit, note = (i.get("food") or {}).get("name") or "", (i.get("unit") or {}).get("name") or "", (i.get("note") or "").strip()
        name, note_text = (food, note) if food else (note or (i.get("originalText") or i.get("display") or "").strip(), "")
        ingr.append({"abschnitt": i.get("title") or "", "menge": i.get("quantity") or 0, "einheit": store.unit(unit),
                    "name": name, "notiz": note_text})
    n = m.get("nutrition") or {}
    return store.normalize({
        "beschreibung": m.get("description"), "portionen": m.get("recipeServings") or store._f(m.get("recipeYield")) or 2,
        "ertrag": m.get("recipeYield"),
        "zeiten": {"gesamt": m.get("totalTime"), "vorbereitung": m.get("prepTime"), "kochen": m.get("performTime")},
        "kategorien": [c["name"] for c in m.get("recipeCategory") or []], "tags": [t["name"] for t in m.get("tags") or []],
        "zutaten": ingr,
        "schritte": [{"titel": s.get("title") or "", "text": s.get("text") or ""} for s in m.get("recipeInstructions") or []],
        "naehrwerte": {k: n.get(src) for src, k in NW.items()},
        "notizen": [{"titel": x.get("title") or "", "text": x.get("text") or ""} for x in m.get("notes") or []],
        "quelle": m.get("orgURL"),
    })


def main():
    store.init()
    ratings = {r["recipeId"]: r["rating"] for r in get("/users/self/ratings").get("ratings") or [] if r.get("rating")}
    foods = get("/foods?perPage=-1").get("items") or []
    recipe_list = get("/recipes?perPage=-1").get("items") or []
    plans = get("/households/mealplans?perPage=-1&start_date=2025-01-01&end_date=2030-12-31").get("items") or []
    recipes, images = [], {}
    for x in recipe_list:
        m = get(f"/recipes/{x['slug']}")
        recipes.append((m, convert(m)))
        if m.get("image"):
            for size, filename in (("original", f"{m['id']}.webp"), ("min-original", f"{m['id']}-min.webp")):
                try:
                    images[filename] = get(f"/media/recipes/{m['id']}/images/{size}.webp", raw=True)
                except Exception as e:  # noqa: BLE001
                    print("Image missing:", m["slug"], size, e)
    lists = get("/households/shopping/lists?perPage=-1").get("items") or []
    open_items = []
    for l in lists:
        for i in get(f"/households/shopping/lists/{l['id']}").get("listItems") or []:
            if not i.get("checked"):
                open_items.append(i)
    print(f"Mealie: {len(recipes)} recipes, {len(images)} image files, {len(ratings)} ratings, {len(foods)} foods, "
          f"{len(plans)} plan entries, {len(open_items)} open list items")
    if DRY_RUN:
        for m, d in recipes[:2]:
            print(json.dumps({"slug": m["slug"], **d}, ensure_ascii=False)[:1500])
        return
    with store.lock, store.con() as c:
        for f in foods:
            c.execute("INSERT INTO zutat(name, kategorie) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET kategorie=excluded.kategorie",
                      (f["name"], ((f.get("label") or {}).get("name")) or "Sonstiges"))
        for m, d in recipes:
            for z in d["zutaten"]:
                if z["name"]:
                    c.execute("INSERT OR IGNORE INTO zutat(name) VALUES(?)", (z["name"],))
            c.execute("""INSERT INTO rezept(id, slug, name, daten, bewertung, bild, erstellt, geaendert) VALUES(?,?,?,?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET slug=excluded.slug, name=excluded.name, daten=excluded.daten,
                         bewertung=excluded.bewertung, bild=excluded.bild, geaendert=excluded.geaendert""",
                      (m["id"], m["slug"], m["name"], json.dumps(d, ensure_ascii=False), ratings.get(m["id"]),
                       "webp" if f"{m['id']}.webp" in images else "", m.get("createdAt") or m.get("dateAdded"), store.now()))
        c.execute("DELETE FROM plan")
        for e in plans:
            c.execute("INSERT INTO plan(id, datum, typ, rezept_id, scale, titel, text) VALUES(?,?,?,?,?,?,?)",
                      (e["id"], e["date"], e.get("entryType") or "dinner", e.get("recipeId"),
                       e.get("recipeScale") if e.get("recipeId") else None, e.get("title") or "", e.get("text") or ""))
        if not c.execute("SELECT 1 FROM einkauf").fetchone():
            for i in open_items:
                food = (i.get("food") or {})
                name = food.get("name") or (i.get("note") or i.get("display") or "").strip()
                if name and not name.startswith("mealie-"):  # not Mealie's internal device entries
                    c.execute("INSERT INTO einkauf(name, kategorie, menge, einheit, notiz, geaendert) VALUES(?,?,?,?,?,?)",
                              (name, (food.get("label") or {}).get("name") or "Sonstiges", i.get("quantity") or None,
                               store.unit((i.get("unit") or {}).get("name")), (i.get("note") or "").strip() if food.get("name") else "", store.now()))
    for filename, data in images.items():
        with open(os.path.join(store.IMAGES, filename), "wb") as fh:
            fh.write(data)
    print("imported")


if __name__ == "__main__":
    main()
