"""Demo household: fills an EMPTY Kombüse installation with realistic example data, in English or German.

  docker exec kombuese python /app/tools/admin.py demo [--lang en|de] [--public-demo] [--no-images]
  or the environment variable KOMBUESE_DEMO=true (or en / de) on the first start of an empty installation.

What you get: 22 recipes (breakfasts, meal prep stews and curries, quick and vegetarian dishes, with nutrition and
meal prep notes and microwave reheating tips), swipe cards in the stack (two of them already liked by the second
member: swipe right for a match),
a week plan around today with a meal prep day and its ready step-by-step sequence, leftovers and a frozen portion,
a week plan proposal for the following week, a shopping list, cupboard and freezer (some of it expiring soon), two
members (Alex = admin „demo“, Sam) with preferences, daily goals and three days of nutrition history.

The admin password is random and printed once; --public-demo uses the documented password „kombuese-demo“ instead
(only for throwaway public demos). Sam's password is random and not shown (reset it under Settings → Accounts).
Photos: freely licensed images from Wikimedia Commons in tools/demo/images, see CREDITS.md there; each recipe with a
photo names its author and license in a note. Nothing is downloaded at run time.
"""
import json
import os
import re
import secrets
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(HERE, "demo")
for _p in ("/app", os.path.join(HERE, "..", "app")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
import auth  # noqa: E402
import household  # noqa: E402
import households  # noqa: E402
import nutrition  # noqa: E402
import pantry  # noqa: E402
import store  # noqa: E402

PUBLIC_PASSWORD = "kombuese-demo"
CATS = {"O": "Obst & Gemüse", "G": "Gewürze", "W": "Würzmittel & Saucen", "R": "Getreide", "K": "Konserven", "B": "Backzutaten",
        "M": "Milchprodukte", "F": "Fleisch", "BW": "Backwaren", "S": "Meeresfrüchte", "OL": "Öle", "T": "Tiefkühlware", "X": "Sonstiges"}


class Error(Exception):
    pass


def _pick(text, i):
    parts = [x.strip() for x in text.split(" | ")]
    return parts[i] if len(parts) > i else parts[0]


def load_recipes(lang):
    """Parse demo/recipes.txt into recipe dicts of one language (format: see the head of that file)."""
    i = 0 if lang == "en" else 1
    out, cur = [], None
    with open(os.path.join(DIR, "recipes.txt"), encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("## "):
                cur = {"key": line[3:].strip(), "zutaten": [], "schritte": [], "notizen": [], "img": ""}
                out.append(cur)
            elif not line.strip() or line.startswith("#"):
                continue
            elif line.startswith("- "):
                p = [x.strip() for x in line[2:].split("|")]
                cur["zutaten"].append({"menge": p[0], "einheit": p[1], "name": p[2 + i], "kategorie": CATS[p[4]],
                                       "notiz": p[5 + i] if len(p) > 6 else ""})
            elif line.startswith("> "):
                cur["schritte"].append({"titel": "", "text": _pick(line[2:], i)})
            else:
                k, v = (x.strip() for x in line.split(":", 1))
                if k == "name":
                    cur["name"] = _pick(v, i)
                elif k == "desc":
                    cur["beschreibung"] = _pick(v, i)
                elif k == "meta":
                    cat, tags, serv, total, prep, cook = (x.strip() for x in (v + ";;;;;").split(";")[:6])
                    cur.update(kategorien=[cat], tags=[t.strip() for t in tags.split(",") if t.strip()], portionen=serv,
                               zeiten={"gesamt": total, "vorbereitung": prep, "kochen": cook})
                elif k == "nw":
                    cur["naehrwerte"] = dict(zip(store.NUTRIENTS, (float(x) for x in v.split())))
                elif k == "prep":
                    cur["notizen"].append({"titel": ("Meal prep & leftovers", "Mealprep & Reste")[i], "text": _pick(v, i)})
                elif k == "heat":  # reheating per portion at 800 W (app/reheat.py)
                    nums, *txt = [x.strip() for x in v.split("|")]
                    hint, alt = (txt[i] if len(txt) > i else ""), (txt[2 + i] if len(txt) > 2 + i else "")
                    if nums == "cold":
                        cur["aufwaermen"] = {"kalt": True, "hinweis": hint}
                    else:
                        fridge, frozen, rest = (nums.split() + ["-", "0"])[:3]
                        a = {"kuehlschrank": {"watt": 800, "minuten": float(fridge)}, "ruhen": float(rest), "hinweis": hint, "alternative": alt}
                        if frozen != "-":
                            a["gefroren"] = {"watt": 800, "minuten": float(frozen)}
                        cur["aufwaermen"] = a
                elif k == "img":
                    cur["img"] = v
    return out


def credits():
    try:
        with open(os.path.join(DIR, "images", "credits.json"), encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return {}


def image(key):
    p = os.path.join(DIR, "images", f"{key}.webp")
    if key and os.path.isfile(p):
        with open(p, "rb") as f:
            return f.read()
    return None


def credit_text(c, lang):
    return (f"{'Photo' if lang == 'en' else 'Foto'}: {c['artist']}, {c['license']}, "
            f"{'via' if lang == 'en' else 'über'} Wikimedia Commons ({c['page']})"
            + (f" – {'cropped and resized' if lang == 'en' else 'zugeschnitten und verkleinert'}" if c.get("changed") else ""))


def empty():
    with store.in_household(1), store.con() as c:
        recipes = c.execute("SELECT COUNT(*) FROM rezept").fetchone()[0]
    return not auth.user_count() and not recipes


def fill(lang="en", public=False, images=True, out=print):
    """Fill household 1 of an empty installation. Returns the counts; prints the admin password once via out."""
    lang = lang if lang in ("en", "de") else "en"
    if not empty():
        raise Error("This installation is not empty (accounts or recipes exist). The demo only fills a new, empty installation.")
    L = (lambda en, de: en) if lang == "en" else (lambda en, de: de)  # noqa: E731
    T = date.today()
    day = lambda n: str(T + timedelta(days=n))  # noqa: E731
    left = household.LEFTOVER[lang]
    pics = credits()
    with store.in_household(1):
        households.rename(1, "Alex & Sam")
        household.save({"language": lang, "people": 2, "description": L(
            "Two adults, Alex and Sam. Both work in the office Monday to Thursday and take lunch boxes. "
            "Shopping on Saturday, meal prep on Sunday afternoon (one oven, four burners). "
            "Weekday dinners quick, at most 30 minutes; on weekends we like to cook properly.",
            "Zwei Erwachsene, Alex und Sam. Beide sind Montag bis Donnerstag im Büro und nehmen Mittagessen mit. "
            "Einkauf am Samstag, Mealprep am Sonntagnachmittag (ein Ofen, vier Platten). "
            "Abends unter der Woche schnell, höchstens 30 Minuten; am Wochenende kochen wir gern richtig.")})

        # ----- people -----
        password = PUBLIC_PASSWORD if public else secrets.token_urlsafe(12)
        alex = households.create_user(1, "demo", password, "Alex", "", "admin")
        sam = households.create_user(1, "sam", secrets.token_urlsafe(18), "Sam", "", "member")
        households.save_prefs(1, alex, {"notes": L("Trains three times a week, likes protein-rich lunches",
                                                   "Trainiert dreimal pro Woche, mag eiweißreiche Mittagessen")})
        households.save_prefs(1, sam, {"no_spicy": True, "dislikes": [L("coriander", "Koriander"), L("olives", "Oliven")],
                                       "notes": L("Mostly vegetarian at lunch", "Mittags am liebsten vegetarisch")})
        nutrition.save_goal(alex, {"kcal": 2400, "protein": 140})
        nutrition.save_goal(sam, {"kcal": 1900, "protein": 90})

        # ----- recipes -----
        recipes, slug = {}, {}
        stars = {"chili": 5, "chickpea": 5, "goulash": 4, "oats": 4, "burrito": 4, "pasta": 4, "dal": 5, "shakshuka": 4, "salmon": 3}
        for r in load_recipes(lang):
            key, img = r.pop("key"), r.pop("img")
            c = pics.get(img) if images else None
            data = image(img) if c else None
            if data:
                r["notizen"].append({"titel": L("Photo", "Foto"), "text": credit_text(c, lang)})
            saved = store.save({**r, "sprache": lang, "quelle": L("Kombüse demo recipe", "Kombüse-Demorezept")}, name=r["name"],
                               rating=stars.get(key))
            if data:
                store.save_image(saved["slug"], data)
            recipes[key], slug[key] = saved, saved["slug"]

        # ----- pantry: cupboard (names as in the recipes, so the shopping list subtracts them) and freezer -----
        n = lambda key, i: next(z["name"] for z in recipes[key]["zutaten"] if z["name"].lower().startswith(i.lower()))  # noqa: E731
        cupboard = [(n("chickpea", L("basmati", "Basmati")), 1500, "g"), (n("pasta", "spaghetti"), 500, "g"),
                    (n("oats", L("rolled", "Hafer")), 300, "g"), (n("dal", L("red len", "rote Lin")), 500, "g"),
                    (n("chickpea", L("chopped", "gehackte")), 3, "Dose"), (n("chickpea", L("coconut", "Kokos")), 1, "Dose"),
                    (n("chickpea", L("chickpeas", "Kichererbsen")), 1, "Dose"), (n("chili", L("kidney", "Kidney")), 2, "Dose"),
                    (n("pasta", L("olive", "Oliven")), 750, "ml"), (n("friedrice", L("soy", "Soja")), 250, "ml"),
                    (n("chickpea", L("curry", "Curry")), None, ""), (n("chili", L("ground cumin", "Kreuzk")), None, ""),
                    (n("chili", L("sweet paprika", "Paprikapulver")), None, ""), (n("dal", L("ground turm", "Kurkuma")), None, ""),
                    (n("chickpea", L("onion", "Zwiebel")), 4, "Stück"), (n("chickpea", L("garlic", "Knoblauch")), 1, "Knolle"),
                    (n("oats", L("honey", "Honig")), 1, "Glas"), (n("lasagne", L("flour", "Mehl")), 1, "kg")]
        for name, amount, unit in cupboard:
            pantry.add(name, amount, unit)
        freezer = [(recipes["chili"]["name"], 2, "Portion", "haupt", -40, 50, "chili"),
                   (recipes["minestrone"]["name"], 2, "Portion", "haupt", -85, 5, "minestrone"),
                   (recipes["burrito"]["name"], 5, "Stück", "fruehstueck", -20, 70, "burrito"),
                   (recipes["eggbites"]["name"], 6, "Stück", "fruehstueck", -50, 10, "eggbites"),
                   (L("Bread rolls", "Brötchen"), 4, "Stück", "sonstiges", -60, 3, ""),
                   (L("Frozen spinach", "TK-Spinat"), 1, "Packung", "sonstiges", -30, 240, "")]
        with store.lock, store.con() as c:
            for name, count, unit, cat, frozen, until, key in freezer:
                c.execute("INSERT INTO vorrat(name, anzahl, einheit, kategorie, eingefroren, haltbar_bis, notiz, recipe_slug, geaendert)"
                          " VALUES(?,?,?,?,?,?,?,?,?)", (name, count, unit, cat, day(frozen), day(until), "", slug.get(key, ""), store.now()))

        # ----- week plan around today: three days back (eaten), meal prep today, then the week from the prep -----
        rn = lambda key: recipes[key]["name"]  # noqa: E731
        prep_day = L("meal prep today", "Mealprep heute")
        plan = [
            (-3, "breakfast", "pancakes", "", "", 1), (-3, "lunch", None, f"{left}: {rn('lasagne')}", L("from the freezer, 25 min in the oven", "aus dem Tiefkühler, 25 Min. in den Ofen"), 1),
            (-3, "dinner", "salmon", "", "", 1),
            (-2, "breakfast", None, f"{left}: {rn('burrito')}", L("from the freezer", "aus dem Tiefkühler"), 1), (-2, "lunch", None, f"{left}: {rn('chili')}", "", 1),
            (-2, "dinner", "pasta", "", "", 1),
            (-1, "breakfast", "shakshuka", "", "", 1), (-1, "lunch", None, f"{left}: {rn('chili')}", "", 1), (-1, "dinner", "stirfry", "", "", 1),
            (0, "breakfast", "pancakes", "", "", 1), (0, "lunch", "wrap", "", "", 1),
            (0, "side", None, "Mealprep", L("Goulash first (it braises for 2 hours), meanwhile the chickpea curry and the overnight oats. "
                                                              "Portion: curry 6 boxes, goulash 6 boxes, oats 4 jars.",
                                                              "Zuerst das Gulasch (schmort 2 Stunden), währenddessen Kichererbsen-Curry und Overnight Oats. "
                                                              "Portionieren: Curry 6 Boxen, Gulasch 6 Boxen, Oats 4 Gläser."), 1),
            (0, "dinner", "friedrice", "", L("with the rice left over from Saturday", "mit dem Reis von Samstag"), 1),
            (1, "breakfast", "oats", "", f"{prep_day}: 4 {L('jars', 'Gläser')}", 0.5),
            (1, "lunch", "chickpea", "", f"{prep_day}: 6 {L('servings', 'Portionen')}", 1.5),
            (1, "dinner", "goulash", "", f"{prep_day}: 6 {L('servings', 'Portionen')}", 1),
            (2, "breakfast", None, f"{left}: {rn('oats')}", "", 1), (2, "lunch", None, f"{left}: {rn('chickpea')}", L("2 min in the microwave", "2 Min. in der Mikrowelle"), 1),
            (2, "dinner", None, f"{left}: {rn('goulash')}", L("heat slowly in a pot", "langsam im Topf erwärmen"), 1),
            (3, "breakfast", None, f"{left}: {rn('eggbites')}", L("from the freezer, 1 min in the microwave", "aus dem Tiefkühler, 1 Min. Mikrowelle"), 1),
            (3, "lunch", None, f"{left}: {rn('goulash')}", "", 1), (3, "dinner", "tacos", "", "", 1),
            (4, "breakfast", None, f"{left}: {rn('burrito')}", L("frozen: move to the fridge the evening before", "eingefroren: am Vorabend in den Kühlschrank"), 1),
            (4, "lunch", None, f"{left}: {rn('chickpea')}", "", 1),
            (4, "dinner", None, f"{left}: {rn('minestrone')} ({L('frozen', 'eingefroren')})",
             L("expires soon: thaw in the fridge the evening before", "läuft bald ab: am Vorabend im Kühlschrank auftauen"), 1),
        ]
        ids = []
        for d, typ, key, title, text, scale in plan:
            eid = store.add_plan_entry(day(d), typ, slug[key] if key else None, title, text, scale)
            ids.append((d, typ, key, title, eid))
        # what was eaten in the last three days, for both
        with store.lock, store.con() as c:
            for d, typ, key, title, eid in ids:
                if d >= 0:
                    continue
                r = recipes[key] if key else next((x for x in recipes.values() if x["name"] == household.strip_leftover(title)), None)
                if not r:
                    continue
                nw = r["naehrwerte"]
                for uid, f in ((alex, 1.2), (sam, 0.9)):
                    c.execute("INSERT INTO gegessen(mealplan_id, user_id, datum, typ, name, slug, portionen, kcal, protein, fett, kh, am)"
                              " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (eid, uid, day(d), typ, r["name"], r["slug"], f, round(nw["kcal"] * f),
                                                                  round(nw["protein"] * f, 1), round(nw["fett"] * f, 1), round(nw["kh"] * f, 1), store.now()))

        # ----- the meal prep sequence for today -----
        _meta(1, "mealprep", {day(0): mealprep_sequence(L, day(0), recipes)})
        _meta(1, "mealprep_job", {"state": "idle"})

        # ----- a week plan proposal for the week after (as an AI run would send it) -----
        start = T + timedelta(days=5)  # the first Monday after the planned days
        start += timedelta(days=(7 - start.weekday()) % 7)
        _meta(1, "wochenplan_vorschlag", proposal(L, left, start, recipes, slug))
        _meta(1, "wochenplan", {"state": "done", "start": str(start), "changed": store.now()})

        # ----- shopping list: what today's prep and the fresh dinners need, minus the cupboard -----
        always = {"namen": [n("eggbites", L("salt", "Salz")), n("eggbites", L("black pepper", "schwarzer"))], "kategorien": []}
        _meta(1, "immer_da", always)
        store.add_to_list([(slug["chickpea"], 1.5), (slug["goulash"], 1), (slug["oats"], 1), (slug["tacos"], 1)], always)
        for text in (L("Coffee beans", "Kaffeebohnen"), L("2 l milk", "2 l Milch"), L("Dish soap", "Spülmittel"), L("Bananas", "Bananen")):
            store.add_item(text)
        for it in store.shopping_list()["items"]:
            if it["name"] in (L("Bananas", "Bananen"), L("Dish soap", "Spülmittel")):
                store.check_item(it["id"], True)

        # ----- swipe cards -----
        cards = add_cards(L, lang, sam, pics if images else {})

    return {"password": password, "username": "demo", "recipes": len(recipes), "cards": cards, "plan": len(plan),
            "freezer": len(freezer), "cupboard": len(cupboard), "lang": lang, "public": public}


def _meta(hid, key, value):
    with store.in_household(hid), store.lock, store.con() as c:
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))


def _amount(z, f):
    q = (z["menge"] or 0) * f
    return " ".join(x for x in (store.fmt_number(round(q, 1)) if q else "", z["einheit"]) if x)


def mealprep_sequence(L, day, recipes):
    """The step-by-step sequence of today's prep day (the format POST /api/mealprep/ablauf takes, see jobs.py)."""
    g, c, o = recipes["goulash"], recipes["chickpea"], recipes["oats"]
    f = {g["slug"]: 1, c["slug"]: 1.5, o["slug"]: 1}

    def z(r, *names):  # ingredients of recipe r whose name starts with one of names, with the scaled amount
        out = []
        for want in names:
            for x in r["zutaten"]:
                if x["name"].lower().startswith(want.lower()):
                    out.append({"menge": _amount(x, f[r["slug"]]), "name": x["name"], "rezept": r["slug"]})
                    break
        return out

    S = lambda title, text, rs, ingr=(), timer=(), side="", mins="": {  # noqa: E731
        "titel": title, "text": text, "rezepte": [r["slug"] for r in rs] if rs != "alle" else ["alle"], "zutaten": list(ingr),
        "timer": [{"min": m, "label": lab} for m, lab in timer], "nebenbei": side, "minuten": mins}
    steps = [
        S(L("Brown the beef", "Rindfleisch anbraten"), L("Pat the beef dry and brown it in three batches in the oil in your biggest pot over high heat, 3–4 minutes per batch. Set aside.",
                                                         "Rindfleisch trocken tupfen und in drei Portionen im Öl im größten Topf bei starker Hitze je 3–4 Minuten anbraten. Herausnehmen."),
          [g], z(g, L("beef", "Rinder"), L("sunflower", "Sonnenblumen")), mins="12"),
        S(L("Chop all onions and garlic", "Alle Zwiebeln und den Knoblauch schneiden"), L("Slice 4 onions into strips for the goulash, finely chop 1 for the curry. Chop 4 cloves of garlic: 2 each.",
                                                                                          "4 Zwiebeln in Streifen für das Gulasch, 1 fein gewürfelt für das Curry. 4 Knoblauchzehen hacken: je 2."),
          [g, c], z(g, L("onion", "Zwiebel"), L("garlic", "Knoblauch")) + z(c, L("onion", "Zwiebel"), L("garlic", "Knoblauch")), mins="8"),
        S(L("Start the goulash", "Gulasch ansetzen"), L("Fry the onion strips in the goulash pot for 10 minutes until golden. Stir in garlic, tomato paste, paprika and caraway for 1 minute, return the meat, add the stock and cover.",
                                                        "Zwiebelstreifen im Gulaschtopf 10 Minuten goldbraun braten. Knoblauch, Tomatenmark, Paprikapulver und Kümmel 1 Minute mitrösten, Fleisch zurück, Brühe angießen, Deckel drauf."),
          [g], z(g, L("tomato", "Tomaten"), L("sweet paprika", "Paprikapulver"), L("caraway", "Kümmel"), L("beef stock", "Rinderbr")),
          [(90, L("goulash", "Gulasch"))], mins="14"),
        S(L("Overnight oats", "Overnight Oats"), L("While the goulash braises: stir oats, chia, honey, yogurt and milk together and fill four jars in layers with the frozen berries. Close and put in the fridge.",
                                                   "Während das Gulasch schmort: Haferflocken, Chia, Honig, Joghurt und Milch verrühren und mit den TK-Beeren abwechselnd in vier Gläser schichten. Verschließen und kalt stellen."),
          [o], z(o, L("rolled", "Hafer"), L("chia", "Chia"), L("honey", "Honig"), L("Greek", "griech"), L("milk", "Milch"), L("frozen", "TK")),
          side=L("Goulash is braising (about 75 min left)", "Gulasch schmort (noch ca. 75 Min.)"), mins="8"),
        S(L("Rice for the curry", "Reis für das Curry"), L("Rinse the rice and cook it in a second pot according to the packet.", "Reis waschen und in einem zweiten Topf nach Packungsangabe garen."),
          [c], z(c, L("basmati", "Basmati")), [(12, L("rice", "Reis"))], mins="3"),
        S(L("Curry base", "Curry-Basis"), L("Soften the chopped onion in the oil in a wide pan for 5 minutes, add the garlic, grated ginger and curry powder and fry for 1 minute.",
                                            "Gewürfelte Zwiebel im Öl in einer weiten Pfanne 5 Minuten dünsten, Knoblauch, geriebenen Ingwer und Currypulver 1 Minute mitrösten."),
          [c], z(c, L("olive", "Oliven"), L("ginger", "Ingwer"), L("curry", "Curry")), mins="7"),
        S(L("Simmer the curry", "Curry köcheln"), L("Add chickpeas, coconut milk and tomatoes and simmer gently.", "Kichererbsen, Kokosmilch und Tomaten dazugeben und sanft köcheln lassen."),
          [c], z(c, L("chickpeas", "Kichererbsen"), L("coconut", "Kokos"), L("chopped", "gehackte")), [(15, L("curry", "Curry"))],
          L("Rice done in about 5 minutes: drain and spread on a tray to cool", "Reis in ca. 5 Minuten fertig: abgießen und auf einem Blech abkühlen lassen"), "3"),
        S(L("Finish the curry", "Curry fertigstellen"), L("Stir the spinach into the curry until wilted, season with salt and lime juice. Take off the heat and let it cool uncovered.",
                                                          "Spinat unter das Curry rühren, bis er zusammenfällt, mit Salz und Limettensaft abschmecken. Vom Herd nehmen und offen abkühlen lassen."),
          [c], z(c, L("baby", "Baby"), L("lime", "Limette")), mins="4"),
        S(L("Tidy up while it braises", "Aufräumen, während es schmort"), L("Wash the curry pan and rice pot, set out 12 boxes and labels.",
                                                                           "Curry-Pfanne und Reistopf spülen, 12 Boxen und Etiketten bereitstellen."), "alle",
          side=L("Goulash: about 30 minutes left", "Gulasch: noch ca. 30 Minuten"), mins="10"),
        S(L("Potatoes and peppers into the goulash", "Kartoffeln und Paprika ins Gulasch"), L("Peel and cube the potatoes, dice the peppers and add both to the goulash. Cook covered until the potatoes are soft, then season.",
                                                                                             "Kartoffeln schälen und würfeln, Paprika würfeln und beides ins Gulasch geben. Zugedeckt garen, bis die Kartoffeln weich sind, dann abschmecken."),
          [g], z(g, L("red bell", "rote Paprika"), L("potatoes", "Kartoffel")), [(25, L("potatoes", "Kartoffeln"))], mins="10"),
        S(L("Portion and label", "Portionieren und beschriften"), L("Curry with rice into 6 boxes (2 of them into the freezer, labelled with today's date), goulash into 6 boxes for the fridge. Let everything cool for 30 minutes before closing the lids.",
                                                                   "Curry mit Reis in 6 Boxen (2 davon mit Datum in den Tiefkühler), Gulasch in 6 Boxen für den Kühlschrank. Alles 30 Minuten abkühlen lassen, bevor die Deckel draufkommen."),
          "alle", mins="10"),
    ]
    ingr = {}
    for s in steps:
        for x in s["zutaten"]:
            ingr.setdefault(x["name"], {"menge": [], "name": x["name"], "fuer": []})
            ingr[x["name"]]["menge"].append(x["menge"])
            if x["rezept"] not in ingr[x["name"]]["fuer"]:
                ingr[x["name"]]["fuer"].append(x["rezept"])
    return {"datum": day, "titel": L("Meal prep", "Mealprep") + f" {day[8:10]}.{day[5:7]}.", "dauer_min": "150",
            "hinweis": L("The goulash braises on its own most of the time; the curry and the oats fit into that time.",
                         "Das Gulasch schmort die meiste Zeit allein; Curry und Oats passen in diese Zeit."),
            "rezepte": [{"slug": g["slug"], "name": g["name"], "portionen": L("6 servings", "6 Portionen")},
                        {"slug": c["slug"], "name": c["name"], "portionen": L("6 servings", "6 Portionen")},
                        {"slug": o["slug"], "name": o["name"], "portionen": L("4 jars", "4 Gläser")}],
            "vorbereiten": {"geraete": [L("large pot with lid", "großer Topf mit Deckel"), L("second pot for rice", "zweiter Topf für Reis"),
                                        L("wide pan", "weite Pfanne"), L("4 jars", "4 Gläser"), L("12 boxes and labels", "12 Boxen und Etiketten")],
                            "zutaten": [{"menge": " + ".join(v["menge"]), "name": v["name"], "rezept": "", "fuer": v["fuer"]} for v in ingr.values()]},
            "schritte": steps, "erstellt": store.now()}


def proposal(L, left, start, recipes, slug):
    d = lambda n: str(start + timedelta(days=n))  # noqa: E731
    rn = lambda key: recipes[key]["name"]  # noqa: E731
    e = lambda n, typ, key=None, title="", text="", scale=1.0: {  # noqa: E731
        "datum": d(n), "typ": typ, "slug": slug[key] if key else None, "titel": title, "text": text, "scale": scale, "einkaufen": bool(key)}
    entries = [
        e(-1, "side", title="Mealprep", text=L("Chili and minestrone side by side, then the burritos while both simmer.",
                                                                 "Chili und Minestrone nebeneinander, dann die Burritos, während beides köchelt.")),
        e(0, "breakfast", "burrito", text=L("meal prep Sunday: 8 burritos, 4 into the freezer", "Mealprep Sonntag: 8 Burritos, 4 in den Tiefkühler")),
        e(0, "lunch", "chili", text=L("meal prep Sunday: 6 servings", "Mealprep Sonntag: 6 Portionen")),
        e(0, "dinner", "salmon"),
        e(1, "breakfast", title=f"{left}: {rn('burrito')}"), e(1, "lunch", "minestrone", text=L("meal prep Sunday: 6 servings", "Mealprep Sonntag: 6 Portionen")),
        e(1, "dinner", "peppers", scale=0.5),
        e(2, "breakfast", title=f"{left}: {rn('burrito')}"), e(2, "lunch", title=f"{left}: {rn('chili')}"), e(2, "dinner", "stirfry"),
        e(3, "breakfast", title=f"{left}: {rn('burrito')}"), e(3, "lunch", title=f"{left}: {rn('minestrone')}"), e(3, "dinner", title=f"{left}: {rn('peppers')}"),
        e(4, "breakfast", "pancakes"), e(4, "lunch", title=f"{left}: {rn('chili')}"), e(4, "dinner", "falafel", scale=0.5),
        e(5, "breakfast", "shakshuka"), e(5, "lunch", title=f"{left}: {rn('minestrone')}"), e(5, "dinner", "dal", scale=0.5),
        e(6, "breakfast", "pancakes"), e(6, "lunch", title=f"{left}: {rn('falafel')}"), e(6, "dinner", "lasagne",
                                                                                           text=L("cook once, 4 servings into the freezer", "einmal kochen, 4 Portionen in den Tiefkühler")),
    ]
    return {"start": d(0), "ende": d(6), "personen": 2, "erstellt": store.now(),
            "zusammenfassung": L("A prep week: chili and minestrone cover the lunches, burritos the breakfasts. Fresh dinners stay under 30 minutes, "
                                 "Saturday's lasagne fills the freezer for later. About 2,100 kcal and 110 g protein per person and day.",
                                 "Eine Mealprep-Woche: Chili und Minestrone decken die Mittagessen, Burritos die Frühstücke. Frische Abendessen bleiben unter 30 Minuten, "
                                 "die Lasagne am Samstag füllt den Tiefkühler für später. Etwa 2.100 kcal und 110 g Eiweiß pro Person und Tag."),
            "prep": L("1. Chili on the stove (55 min). 2. Minestrone vegetables, then simmer. 3. Burrito filling and eggs, roll, wrap, freeze 4.",
                      "1. Chili auf den Herd (55 Min.). 2. Gemüse für die Minestrone, dann köcheln. 3. Burrito-Füllung und Eier, rollen, einwickeln, 4 einfrieren."),
            "eintraege": entries,
            "vorrat_neu": [{"name": rn("burrito"), "anzahl": 4, "einheit": "Stück", "kategorie": "fruehstueck", "eingefroren": d(-1),
                            "haltbar_bis": str(start + timedelta(days=89)), "recipe_slug": slug["burrito"], "notiz": ""},
                           {"name": rn("lasagne"), "anzahl": 4, "einheit": "Portion", "kategorie": "haupt", "eingefroren": d(6),
                            "haltbar_bis": str(start + timedelta(days=96)), "recipe_slug": slug["lasagne"], "notiz": ""}]}


# Swipe cards: key, meal, minutes, kcal, servings, status, (title en, de), (reason en, de), (tags en, de), (main ingredients en, de)
CARDS = [
    ("pumpkin", "haupt", 40, 380, "4", "neu", ("Roasted pumpkin soup with ginger", "Geröstete Kürbissuppe mit Ingwer"),
     ("You liked the minestrone and the dal: another soup for the freezer, mild and creamy.", "Dir gefallen Minestrone und Dal: noch eine Suppe für den Tiefkühler, mild und cremig."),
     ("vegetarian, meal prep", "vegetarisch, Mealprep"), ("pumpkin, ginger, coconut milk", "Kürbis, Ingwer, Kokosmilch")),
    ("gnocchi", "haupt", 25, 590, "2", "neu", ("Crispy gnocchi with tomatoes and mozzarella", "Knusprige Gnocchi mit Tomaten und Mozzarella"),
     ("Quick weekday dinner from one pan, like your cherry tomato pasta.", "Schnelles Abendessen aus einer Pfanne, wie deine Kirschtomaten-Pasta."),
     ("quick, vegetarian", "schnell, vegetarisch"), ("gnocchi, cherry tomatoes, mozzarella", "Gnocchi, Kirschtomaten, Mozzarella")),
    ("bibimbap", "haupt", 45, 640, "2", "neu", ("Bibimbap bowl with fried egg", "Bibimbap-Bowl mit Spiegelei"),
     ("Rice bowls with lots of vegetables – the gochujang goes on the side for Sam.", "Reisbowl mit viel Gemüse – das Gochujang kommt für Sam extra dazu."),
     ("bowl", "Bowl"), ("rice, spinach, carrots, egg", "Reis, Spinat, Karotten, Ei")),
    ("cannelloni", "haupt", 70, 560, "4", "neu", ("Spinach and ricotta cannelloni", "Cannelloni mit Spinat und Ricotta"),
     ("Bakes like your lasagne and freezes well in portions.", "Wird gebacken wie deine Lasagne und lässt sich gut portionsweise einfrieren."),
     ("vegetarian, freezes", "vegetarisch, einfrierbar"), ("spinach, ricotta, tomatoes", "Spinat, Ricotta, Tomaten")),
    ("padthai", "haupt", 30, 610, "2", "neu", ("Pad thai with tofu", "Pad Thai mit Tofu"),
     ("Your noodle stir-fry was a hit: here is a sweet-sour variant with peanuts.", "Die Nudelpfanne kam gut an: hier eine süß-saure Variante mit Erdnüssen."),
     ("quick", "schnell"), ("rice noodles, tofu, peanuts, egg", "Reisnudeln, Tofu, Erdnüsse, Ei")),
    ("ratatouille", "haupt", 60, 320, "4", "neu", ("Oven ratatouille", "Ratatouille aus dem Ofen"),
     ("Lots of vegetables, keeps four days and goes with rice, pasta or bread.", "Viel Gemüse, hält vier Tage und passt zu Reis, Nudeln oder Brot."),
     ("vegetarian, meal prep", "vegetarisch, Mealprep"), ("aubergine, courgette, peppers", "Aubergine, Zucchini, Paprika")),
    ("granola", "fruehstueck", 35, 420, "8", "neu", ("Homemade nut granola", "Selbstgemachtes Nuss-Granola"),
     ("One tray lasts two weeks – with yogurt and fruit in two minutes.", "Ein Blech reicht zwei Wochen – mit Joghurt und Obst in zwei Minuten fertig."),
     ("breakfast, meal prep", "Frühstück, Mealprep"), ("oats, nuts, honey", "Haferflocken, Nüsse, Honig")),
    ("chia", "fruehstueck", 5, 330, "2", "neu", ("Mango chia pudding", "Mango-Chiapudding"),
     ("Prepared in the evening like your overnight oats, but lighter.", "Am Vorabend vorbereitet wie deine Overnight Oats, nur leichter."),
     ("breakfast, vegan", "Frühstück, vegan"), ("chia seeds, coconut milk, mango", "Chiasamen, Kokosmilch, Mango")),
    ("frenchtoast", "fruehstueck", 20, 480, "2", "neu", ("Cinnamon French toast with berries", "Arme Ritter mit Zimt und Beeren"),
     ("A weekend breakfast that uses up old bread.", "Ein Wochenendfrühstück, das altes Brot verwertet."),
     ("weekend", "Wochenende"), ("bread, eggs, milk, berries", "Brot, Eier, Milch, Beeren")),
    ("paella", "haupt", 50, 620, "4", "like", ("Vegetable paella", "Gemüsepaella"),
     ("One pan for four, with peppers and peas.", "Eine Pfanne für vier, mit Paprika und Erbsen."), ("vegetarian", "vegetarisch"), ("rice, peppers, peas", "Reis, Paprika, Erbsen")),
    ("moussaka", "haupt", 120, 720, "6", "nope", ("Moussaka", "Moussaka"),
     ("Greek classic for the weekend.", "Griechischer Klassiker fürs Wochenende."), ("weekend", "Wochenende"), ("aubergine, lamb mince, béchamel", "Aubergine, Lammhack, Béchamel")),
    ("jambalaya", "haupt", 45, 650, "4", "nope", ("Spicy jambalaya", "Scharfes Jambalaya"),
     ("Rice, sausage and chicken from one pot.", "Reis, Wurst und Hähnchen aus einem Topf."), ("one pot", "One Pot"), ("rice, chorizo, chicken", "Reis, Chorizo, Hähnchen")),
]


def add_cards(L, lang, sam, pics):
    i = 0 if lang == "en" else 1
    why = {"moussaka": ("aufwand,lang", L("Too much work for a weekday", "Zu viel Arbeit unter der Woche")), "jambalaya": ("scharf", "")}
    img_dir = os.path.join(store.data_dir(), "img")
    os.makedirs(img_dir, exist_ok=True)
    n = 0
    with store.lock, store.con() as c:
        for k, (key, meal, minutes, kcal, servings, status, title, reason, tags, ingr) in enumerate(CARDS):
            swiped = store.now() if status != "neu" else None
            cid = c.execute(
                "INSERT INTO cards(url, title, description, reason, tags, minutes, servings, kcal, ingredients, source, batch, added, status, plan,"
                " swiped, meal, zutaten, why, why_text, why_at, kcal_est) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"https://example.org/kombuese-demo/{key}", title[i], "", reason[i], json.dumps([t.strip() for t in tags[i].split(",")], ensure_ascii=False),
                 minutes, servings, kcal, json.dumps([x.strip() for x in ingr[i].split(",")], ensure_ascii=False), L("Kombüse demo", "Kombüse-Demo"),
                 "demo", store.now(), status, 1 if status == "like" else 0, swiped, meal,
                 json.dumps([x.strip() for x in ingr[i].split(",")], ensure_ascii=False), why.get(key, ("", ""))[0], why.get(key, ("", ""))[1],
                 swiped if key in why else None, 1)).lastrowid
            data = image(key) if key in pics else None
            if data:
                with open(os.path.join(img_dir, f"{cid}.webp"), "wb") as f:
                    f.write(data)
                c.execute("UPDATE cards SET image=? WHERE id=?", (f"{cid}.webp", cid))
            if key in ("pumpkin", "gnocchi"):  # Sam liked these already: swiping right makes a match
                c.execute("INSERT INTO card_votes(card_id, user_id, vote, at) VALUES(?,?,?,?)", (cid, sam, "like", store.now()))
            n += 1
    return n


def main(argv):
    lang = "en"
    if "--lang" in argv:
        lang = argv[argv.index("--lang") + 1] if argv.index("--lang") + 1 < len(argv) else ""
        if lang not in ("en", "de"):
            sys.exit("--lang en or --lang de")
    r = fill(lang, public="--public-demo" in argv, images="--no-images" not in argv)
    print_result(r)


def print_result(r, out=print):
    out(f"Demo household filled ({r['lang']}): {r['recipes']} recipes, {r['cards']} swipe cards, {r['plan']} plan entries, "
        f"{r['cupboard']} cupboard and {r['freezer']} freezer items.")
    out(f"Sign in as „{r['username']}“ with the password: {r['password']}")
    out("This is the only time the password is shown." if not r["public"] else
        "Public demo password: anyone who knows the address can sign in. Use it only for a throwaway demo.")


if __name__ == "__main__":
    import server  # noqa: E402,F401  tables and migrations like at start
    server.init_all()
    try:
        main(sys.argv[1:])
    except Error as e:
        sys.exit(str(e))
