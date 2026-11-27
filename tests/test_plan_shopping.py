"""Week plan, iCal feed and the shopping list (by hand and from recipes, with rounding)."""
import datetime
import unittest

try:
    from .harness import ServerTestCase
except ImportError:
    from harness import ServerTestCase

LONG = "Sehr langer Rezeptname mit Kürbis, Kichererbsen und ganz viel Liebe gekocht"


def unfold(ics):
    return ics.replace("\r\n ", "")


class PlanTest(ServerTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        r = cls.admin.post("/api/rezepte", {"name": LONG, "portionen": 2, "zutaten_text": "200 g Kürbis"})
        cls.slug = r.json()["slug"]
        cls.today = str(datetime.date.today())

    def test_plan_entry_and_week(self):
        eid = self.ok(self.admin.post("/api/plan", {"datum": self.today, "typ": "dinner", "slug": self.slug, "scale": 2}))["id"]
        w = self.ok(self.admin.get(f"/api/woche?start={self.today}"))
        self.assertLessEqual(w["start"], self.today)
        self.assertGreaterEqual(w["ende"], self.today)
        e = next(x for x in w["eintraege"] if x["id"] == eid)
        self.assertEqual((e["date"], e["typ"], e["titel"], e["scale"]), (self.today, "dinner", LONG, 2))
        self.assertEqual(e["rezept"]["slug"], self.slug)
        # entries without a recipe (leftovers, canteen)
        rid = self.ok(self.admin.post("/api/plan", {"datum": self.today, "typ": "lunch", "titel": "Rest: " + LONG}))["id"]
        e = next(x for x in self.ok(self.admin.get(f"/api/woche?start={self.today}"))["eintraege"] if x["id"] == rid)
        self.assertTrue(e["rest"])
        self.assertIsNone(e["rezept"])
        self.assertEqual(self.ok(self.admin.post(f"/api/plan/{rid}/weg"))["geloescht"], 1)
        self.assertEqual(self.ok(self.admin.post(f"/api/plan/{rid}/weg"))["geloescht"], 0)

    def test_plan_from_recipe_scales_by_servings(self):
        d = self.ok(self.admin.post(f"/api/rezepte/{self.slug}/plan", {"datum": self.today, "typ": "breakfast", "portionen": 5}))
        self.assertEqual(d["datum"], self.today)
        e = [x for x in self.ok(self.admin.get(f"/api/woche?start={self.today}"))["eintraege"] if x["typ"] == "breakfast"]
        self.assertEqual(e[-1]["scale"], 2.5)

    def test_plan_validation(self):
        self.assertEqual(self.admin.post("/api/plan", {"datum": "morgen", "typ": "dinner"}).status, 400)
        r = self.admin.post("/api/plan", {"datum": self.today, "typ": "dinner", "slug": "gibt-es-nicht"})
        self.assertEqual(r.status, 400)

    def test_ical_feed(self):
        dinner = self.ok(self.admin.post("/api/plan", {"datum": self.today, "typ": "dinner", "slug": self.slug}))["id"]
        lunch = self.ok(self.admin.post("/api/plan", {"datum": self.today, "typ": "lunch", "titel": "Kantine", "text": "Zeile 1\nZeile 2"}))["id"]
        old = self.ok(self.admin.post("/api/plan", {"datum": "2001-01-01", "typ": "dinner", "titel": "Uralt"}))["id"]
        tok = self.token("calendar")["token"]
        r = self.server.client(token=tok).get("/kalender.ics")
        self.ok(r)
        self.assertTrue(r.header("content-type").startswith("text/calendar"))
        ics = r.text
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(ics.endswith("END:VCALENDAR\r\n"))
        for line in ics.split("\r\n"):
            self.assertLessEqual(len(line.encode()), 75, f"line not folded: {line!r}")
        flat = unfold(ics)
        day = self.today.replace("-", "")
        self.assertIn(f"UID:kombuese-plan-{dinner}@kombuese", flat)
        self.assertIn(f"UID:kombuese-plan-{lunch}@kombuese", flat)
        self.assertNotIn(f"UID:kombuese-plan-{old}@kombuese", flat, "only a window around today")
        self.assertIn(LONG.replace(",", "\\,"), flat, "commas are escaped")
        self.assertIn(f"DTSTART:{day}T183000", flat)
        self.assertIn("DESCRIPTION:Zeile 1\\nZeile 2", flat)
        only_lunch = unfold(self.server.client(token=tok).get("/kalender.ics?typ=lunch").text)
        self.assertIn("Kantine", only_lunch)
        self.assertNotIn(f"kombuese-plan-{dinner}@", only_lunch)
        self.ok(self.admin.post(f"/api/plan/{dinner}/weg"))
        self.assertNotIn(f"kombuese-plan-{dinner}@", unfold(self.server.client(token=tok).get("/kalender.ics").text))


class ShoppingTest(ServerTestCase):

    def items(self):
        return {i["name"]: i for i in self.ok(self.admin.get("/api/einkauf"))["items"]}

    def clear(self):
        ids = [i["id"] for i in self.ok(self.admin.get("/api/einkauf"))["items"]]
        if ids:
            self.ok(self.admin.post("/api/einkauf/weg", {"ids": ids}))

    def setUp(self):
        self.clear()

    def test_add_check_remove(self):
        d = self.ok(self.admin.get("/api/einkauf"))
        self.assertEqual(d["items"], [])
        self.assertFalse(d["zugang"]["bring"])
        iid = self.ok(self.admin.post("/api/einkauf/neu", {"text": "2 l Milch; frisch"}))["id"]
        self.ok(self.admin.post("/api/einkauf/neu", {"text": "Spülmittel"}))
        it = self.items()
        self.assertEqual((it["Milch"]["menge"], it["Milch"]["note"], it["Milch"]["checked"]), ("2 l", "frisch", False))
        self.assertEqual(it["Spülmittel"]["menge"], "")
        self.ok(self.admin.post("/api/einkauf/check", {"id": str(iid), "checked": True}))
        self.assertTrue(self.items()["Milch"]["checked"])
        self.ok(self.admin.post("/api/einkauf/check", {"id": str(iid), "checked": False}))
        self.assertFalse(self.items()["Milch"]["checked"])
        self.ok(self.admin.post("/api/einkauf/weg", {"ids": [iid]}))
        self.assertNotIn("Milch", self.items())
        self.assertIn("Spülmittel", self.items())

    def test_empty_text_refused(self):
        r = self.admin.post("/api/einkauf/neu", {"text": ""})
        self.assertGreaterEqual(r.status, 400)
        self.assertEqual(self.items(), {})

    def test_category_from_lexicon(self):
        self.ok(self.admin.post("/api/zutaten", {"name": "Joghurt", "kategorie": "Milchprodukte"}))
        self.ok(self.admin.post("/api/einkauf/neu", {"text": "500 g Joghurt"}))
        self.assertEqual(self.items()["Joghurt"]["kategorie"], "Milchprodukte")
        self.assertEqual(self.admin.post("/api/zutaten", {"name": "x"}).status, 400)

    def test_recipe_to_list_rounds(self):
        slug = self.recipe("Pfannkuchen Test", portionen=2, zutaten_text="\n".join([
            "180 g Mehl", "0,3 l Milch", "3 Zehen Knoblauch", "1 Prise Salz", "1 EL Sojasauce", "1,5 Zwiebeln", "## Deko", "2 Stück Eier"]))
        d = self.ok(self.admin.post(f"/api/rezepte/{slug}/einkauf", {"portionen": 3}))  # factor 1.5
        self.assertEqual(d["zeilen_neu"], 6)
        self.assertEqual(d["weggelassen"], 1, "salt is always at home")
        it = self.items()
        self.assertNotIn("Salz", it)
        self.assertEqual((it["Mehl"]["menge"], it["Mehl"]["note"]), ("300 g", "gebraucht 270 g"))
        self.assertEqual((it["Milch"]["menge"], it["Milch"]["note"]), ("450 ml", ""))
        self.assertEqual((it["Knoblauch"]["menge"], it["Knoblauch"]["note"]), ("1 Knolle", "gebraucht 4,5 Zehen"))
        self.assertEqual((it["Sojasauce"]["menge"], it["Sojasauce"]["note"]), ("1 Packung", "gebraucht 1,5 EL"))
        self.assertEqual((it["Zwiebeln"]["menge"], it["Zwiebeln"]["note"]), ("3", "gebraucht 2,25"))
        self.assertEqual(it["Eier"]["menge"], "3 Stück")
        self.assertEqual(it["Mehl"]["rezepte"], ["Pfannkuchen Test"])
        # the same recipe again: lines are merged and rounded once
        d = self.ok(self.admin.post("/api/einkauf/rezepte", {"rezepte": [{"slug": slug, "scale": 1}]}))
        self.assertEqual(d["zeilen_neu"], 0)
        it = self.items()
        self.assertEqual((it["Mehl"]["menge"], it["Mehl"]["note"]), ("450 g", ""))
        self.assertEqual((it["Milch"]["menge"], it["Milch"]["note"]), ("800 ml", "gebraucht 750 ml"))  # over 500: steps of 100
        self.assertEqual(it["Knoblauch"]["menge"], "1 Knolle")
        self.assertEqual(it["Eier"]["menge"], "5 Stück")
        # checked lines stay as they are, a new line is started
        self.ok(self.admin.post("/api/einkauf/check", {"id": it["Mehl"]["id"], "checked": True}))
        self.ok(self.admin.post("/api/einkauf/rezepte", {"rezepte": [{"slug": slug, "scale": 1}, {"slug": "gibt-es-nicht"}]}))
        mehl = [i for i in self.ok(self.admin.get("/api/einkauf"))["items"] if i["name"] == "Mehl"]
        self.assertEqual(sorted((i["checked"], i["menge"]) for i in mehl), [(False, "200 g"), (True, "450 g")])

    def test_kilograms(self):
        slug = self.recipe("Kartoffelsuppe Test", portionen=4, zutaten_text="800 g Kartoffeln mehligkochend")
        self.ok(self.admin.post(f"/api/rezepte/{slug}/einkauf", {"portionen": 5}))  # 1000 g
        self.assertEqual(self.items()["Kartoffeln mehligkochend"]["menge"], "1 kg")

    def test_always_at_home(self):
        im = self.ok(self.admin.post("/api/einkauf/immer", {"name": "Mehl", "aktion": "add"}))
        self.assertIn("Mehl", im["namen"])
        try:
            slug = self.recipe("Brot Test", portionen=1, zutaten_text="500 g Mehl\n300 ml Wasser\n10 g Hefe")
            d = self.ok(self.admin.post(f"/api/rezepte/{slug}/einkauf", {"portionen": 1}))
            self.assertEqual(d["weggelassen"], 2)  # Mehl and Wasser
            self.assertEqual(set(self.items()), {"Hefe"})
        finally:
            im = self.ok(self.admin.post("/api/einkauf/immer", {"name": "mehl", "aktion": "remove"}))
        self.assertNotIn("Mehl", im["namen"])


if __name__ == "__main__":
    unittest.main()
