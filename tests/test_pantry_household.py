"""Pantry (cupboard and freezer): add, consume by cooking, undo. Household settings and the language of /i18n.js."""
import datetime
import unittest

try:
    from .harness import ServerTestCase
except ImportError:
    from harness import ServerTestCase


class PantryTest(ServerTestCase):

    def cupboard(self):
        return {r["name"]: r for r in self.ok(self.admin.get("/api/schrank"))["schrank"]}

    def test_add_consume_undo(self):
        # rice counts as long-life food through its category in the lexicon
        self.ok(self.admin.post("/api/zutaten", {"name": "Reis", "kategorie": "Getreide"}))
        b1 = self.ok(self.admin.post("/api/schrank", {"name": "Reis", "menge": 1, "einheit": "kg"}))["buchung"]
        self.assertIsInstance(b1, int)
        r = self.cupboard()["Reis"]
        self.assertEqual((r["menge"], r["einheit"], r["text"]), (1, "kg", "1 kg"))
        self.ok(self.admin.post("/api/schrank", {"name": "reis", "menge": "500", "einheit": "g"}))  # added to the kg line
        self.assertAlmostEqual(self.cupboard()["Reis"]["menge"], 1.5)
        self.assertEqual(len(self.cupboard()), 1)

        slug = self.recipe("Reispfanne", portionen=2, zutaten_text="200 g Reis\n1 EL Sojasauce\n2 Eier")
        d = self.ok(self.admin.post("/api/schrank/kochen", {"slug": slug, "portionen": 4}))
        self.assertIn("400 g Reis", d["schrank"]["text"])
        cook = d["schrank"]["buchung"]
        self.assertAlmostEqual(self.cupboard()["Reis"]["menge"], 1.1)
        # cooking the same recipe again on the same day books nothing twice
        self.assertIsNone(self.ok(self.admin.post("/api/schrank/kochen", {"slug": slug, "portionen": 4}))["schrank"])
        self.assertAlmostEqual(self.cupboard()["Reis"]["menge"], 1.1)
        # undo
        self.ok(self.admin.post("/api/schrank/rueckgaengig", {"buchung": cook}))
        self.assertAlmostEqual(self.cupboard()["Reis"]["menge"], 1.5)
        self.assertEqual(self.admin.post("/api/schrank/rueckgaengig", {"buchung": cook}).status, 400)
        self.assertEqual(self.admin.post("/api/schrank/rueckgaengig", {"buchung": 99999}).status, 400)
        journal = self.ok(self.admin.get("/api/schrank"))["buchungen"]
        self.assertTrue(any(j["id"] == cook and j["rueckgaengig"] for j in journal))

        # a plan entry is consumed once
        eid = self.ok(self.admin.post("/api/plan", {"datum": str(datetime.date.today()), "typ": "dinner", "slug": slug}))["id"]
        d = self.ok(self.admin.post("/api/schrank/kochen", {"plan_id": eid}))
        self.assertIn("200 g Reis", d["schrank"]["text"])
        self.assertAlmostEqual(self.cupboard()["Reis"]["menge"], 1.3)
        self.assertIsNone(self.ok(self.admin.post("/api/schrank/kochen", {"plan_id": eid}))["schrank"])
        self.ok(self.admin.post("/api/schrank/rueckgaengig", {"buchung": d["schrank"]["buchung"]}))
        self.assertAlmostEqual(self.cupboard()["Reis"]["menge"], 1.5)

    def test_pantry_covers_shopping_list(self):
        self.ok(self.admin.post("/api/zutaten", {"name": "Linsen", "kategorie": "Getreide"}))
        self.ok(self.admin.post("/api/schrank", {"name": "Linsen", "menge": 500, "einheit": "g"}))
        slug = self.recipe("Linsensuppe", portionen=2, zutaten_text="250 g Linsen\n1 Möhre")
        self.ok(self.admin.post(f"/api/rezepte/{slug}/einkauf", {"portionen": 2}))
        items = {i["name"]: i for i in self.ok(self.admin.get("/api/einkauf"))["items"]}
        self.assertTrue(items["Linsen"]["checked"])
        self.assertTrue(items["Linsen"]["im_schrank"])
        self.assertFalse(items["Möhre"]["checked"])
        # cupboard empty → the list line is open again; undo brings it back
        row = self.cupboard()["Linsen"]
        b = self.ok(self.admin.post(f"/api/schrank/{row['id']}", {"leer": True}))["buchung"]
        self.assertNotIn("Linsen", self.cupboard())
        items = {i["name"]: i for i in self.ok(self.admin.get("/api/einkauf"))["items"]}
        self.assertFalse(items["Linsen"]["checked"])
        self.assertEqual(items["Linsen"]["menge"], "250 g")
        self.ok(self.admin.post("/api/schrank/rueckgaengig", {"buchung": b}))
        self.assertEqual(self.cupboard()["Linsen"]["menge"], 500)
        # partly there: only the rest is on the list
        self.ok(self.admin.post(f"/api/schrank/{row['id']}", {"menge": 100}))
        items = {i["name"]: i for i in self.ok(self.admin.get("/api/einkauf"))["items"]}
        self.assertFalse(items["Linsen"]["checked"])
        self.assertEqual(items["Linsen"]["menge"], "150 g")
        self.assertIn("100 g im Schrank", items["Linsen"]["note"])

    def test_validation(self):
        self.assertEqual(self.admin.post("/api/schrank", {"name": " "}).status, 400)
        self.assertEqual(self.admin.post("/api/schrank/999999", {"menge": 1}).status, 400)

    def test_unknown_amount(self):
        self.ok(self.admin.post("/api/schrank", {"name": "Zimt", "einheit": ""}))
        self.assertEqual(self.cupboard()["Zimt"]["text"], "da")

    def test_freezer(self):
        lst = self.ok(self.admin.post("/api/vorrat", {"name": "Chili  con  Carne", "anzahl": 3, "kategorie": "haupt",
                                                     "haltbar_bis": "2027-01-31", "eingefroren": "kein datum"}))
        row = next(x for x in lst if x["name"] == "Chili con Carne")
        self.assertEqual((row["anzahl"], row["einheit"], row["haltbar_bis"]), (3, "Portion", "2027-01-31"))
        self.assertIsNone(row["eingefroren"])
        lst = self.ok(self.admin.post(f"/api/vorrat/{row['id']}", {"delta": -1}))
        self.assertEqual(next(x for x in lst if x["id"] == row["id"])["anzahl"], 2)
        lst = self.ok(self.admin.post(f"/api/vorrat/{row['id']}", {"delta": -5}))
        self.assertNotIn(row["id"], [x["id"] for x in lst], "at zero the entry is gone")
        self.assertEqual(self.admin.post(f"/api/vorrat/{row['id']}", {"delta": 1}).status, 404)
        self.assertEqual(self.admin.post("/api/vorrat", {"name": "Leer", "anzahl": 0}).status, 400)


class HouseholdTest(ServerTestCase):

    def lang_js(self):
        r = self.server.client().get("/i18n.js")  # open route, no login
        self.ok(r)
        self.assertTrue(r.header("content-type").startswith("application/javascript"))
        first = r.text.split("\n", 1)[0]
        self.assertRegex(first, r'^window\.KOMBUESE_LANG = "(de|en)";$')
        self.assertGreater(len(r.body), 1000, "the translation table follows")
        return first.split('"')[1]

    def test_household(self):
        h = self.ok(self.admin.get("/api/household"))
        self.assertEqual({k: h[k] for k in ("language", "description")}, {"language": "en", "description": ""})  # more fields
        self.assertEqual(self.lang_js(), "en")
        d = self.ok(self.admin.post("/api/household", {"language": "de", "description": "  Zwei Personen, Mealprep am Sonntag  "}))
        self.assertTrue(d["ok"])
        self.assertEqual((d["language"], d["description"]), ("de", "Zwei Personen, Mealprep am Sonntag"))
        self.assertEqual(self.lang_js(), "de")
        self.assertEqual(self.ok(self.admin.get("/api/config"))["language"], "de")
        tok = self.token("api")["token"]
        self.assertEqual(self.ok(self.server.client(token=tok).get("/api/work"))["language"], "de")
        self.assertEqual(self.ok(self.admin.get("/api/ai/status"))["language"], "de")
        # unknown languages are ignored, the description stays when it is not sent
        d = self.ok(self.admin.post("/api/household", {"language": "fr"}))
        self.assertEqual((d["language"], d["description"]), ("de", "Zwei Personen, Mealprep am Sonntag"))
        # members read, only admins change
        m, _ = self.member()
        self.assertEqual(self.ok(m.get("/api/household"))["language"], "de")
        self.assertEqual(m.post("/api/household", {"language": "en"}).status, 403)
        self.assertEqual(self.server.client(token=tok).post("/api/household", {"language": "en"}).status, 403)
        self.assertEqual(self.lang_js(), "de")
        self.ok(self.admin.post("/api/household", {"language": "en", "description": ""}))
        self.assertEqual(self.lang_js(), "en")


if __name__ == "__main__":
    unittest.main()
