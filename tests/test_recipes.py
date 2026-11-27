"""Recipes: create, read, edit, ingredient lines (German and English units), recipe language."""
import unittest

try:
    from .harness import ServerTestCase
except ImportError:
    from harness import ServerTestCase


ZUTATEN = "\n".join([
    "200 g Mehl; gesiebt",
    "2 tbsp olive oil",
    "1 1/2 cups milk",
    "3 cloves garlic",
    "½ TL Salz",
    "1,5 Liter Wasser",
    "## Sauce",
    "2-3 Zwiebeln",
    "8-Kräuter-Mischung",
    "1 pinch pepper",
    "2 Esslöffel Honig",
    "4 Stk. Eier",
])


class RecipeTest(ServerTestCase):

    def zutaten(self, slug):
        return self.ok(self.admin.get(f"/api/rezepte/{slug}"))["zutaten"]

    def test_create_and_read(self):
        r = self.ok(self.admin.post("/api/rezepte", {
            "name": "Test Curry", "portionen": 4, "beschreibung": "Ein Testrezept", "zutaten_text": ZUTATEN,
            "schritte_text": "## Vorbereiten\nZwiebeln schneiden\n\nAlles kochen", "tags": ["schnell", "schnell", "vegetarisch"],
            "zeiten": {"gesamt": "PT1H20M"}, "naehrwerte": {"kcal": "450 kcal", "protein": "20,5"}}))
        self.assertEqual(r["slug"], "test-curry")
        d = self.ok(self.admin.get("/api/rezepte/test-curry"))
        self.assertEqual(d["name"], "Test Curry")
        self.assertEqual(d["portionen"], 4)
        self.assertEqual(d["tags"], ["schnell", "vegetarisch"])
        self.assertEqual(d["zeiten"]["gesamt"], "1 h 20 min")
        self.assertEqual(d["naehrwerte"]["kcal"], 450)
        self.assertEqual(d["naehrwerte"]["protein"], 20.5)
        self.assertEqual(d["schritte"], [{"titel": "Vorbereiten", "text": "Zwiebeln schneiden"}, {"titel": "", "text": "Alles kochen"}])
        got = [(z["abschnitt"], z.get("menge"), z.get("einheit"), z["name"], z.get("notiz", "")) for z in d["zutaten"]]
        self.assertEqual(got, [
            ("", 200, "g", "Mehl", "gesiebt"),
            ("", 2, "EL", "olive oil", ""),
            ("", 1.5, "Tasse", "milk", ""),
            ("", 3, "Zehe", "garlic", ""),
            ("", 0.5, "TL", "Salz", ""),
            ("", 1.5, "l", "Wasser", ""),
            ("Sauce", 0, "", "", ""),
            ("", 2.5, "", "Zwiebeln", ""),
            ("", 0, "", "8-Kräuter-Mischung", ""),
            ("", 1, "Prise", "pepper", ""),
            ("", 2, "EL", "Honig", ""),
            ("", 4, "Stück", "Eier", ""),
        ])
        # the edit field text is written back in the normalized form
        self.assertIn("200 g Mehl; gesiebt", d["zutaten_text"])
        self.assertIn("## Sauce", d["zutaten_text"])
        self.assertIn("1,5 Tasse milk", d["zutaten_text"])
        # list and search
        names = [x["name"] for x in self.ok(self.admin.get("/api/rezepte?suche=curry"))]
        self.assertIn("Test Curry", names)
        self.assertEqual(self.ok(self.admin.get("/api/rezepte?suche=gibtsnicht")), [])
        # new ingredients land in the lexicon
        lex = self.ok(self.admin.get("/api/zutaten?suche=Mehl"))
        self.assertIn("Mehl", [z["name"] for z in lex["zutaten"]])

    def test_structured_ingredients(self):
        slug = self.recipe("Strukturiert", zutaten=[{"menge": "250", "einheit": "Gramm", "name": "Nudeln"},
                                                    "1 can tomatoes", {"abschnitt": "Topping", "name": ""}])
        z = self.zutaten(slug)
        self.assertEqual((z[0]["menge"], z[0]["einheit"], z[0]["name"]), (250, "g", "Nudeln"))
        self.assertEqual((z[1]["menge"], z[1]["einheit"], z[1]["name"]), (1, "Dose", "tomatoes"))
        self.assertEqual(z[2]["abschnitt"], "Topping")

    def test_edit_only_sent_fields(self):
        slug = self.recipe("Zum Bearbeiten", portionen=2, zutaten_text="100 g Reis", beschreibung="alt")
        self.ok(self.admin.post(f"/api/rezepte/{slug}", {"beschreibung": "neu"}))
        d = self.ok(self.admin.get(f"/api/rezepte/{slug}"))
        self.assertEqual(d["beschreibung"], "neu")
        self.assertEqual(d["portionen"], 2)
        self.assertEqual([(z["menge"], z["einheit"], z["name"]) for z in d["zutaten"]], [(100, "g", "Reis")])
        r = self.ok(self.admin.post(f"/api/rezepte/{slug}", {"name": "Neuer Name", "zutaten_text": "2 tsp sugar\n1 bunch parsley"}))
        self.assertEqual(r["slug"], slug, "the slug stays when the name changes")
        d = self.ok(self.admin.get(f"/api/rezepte/{slug}"))
        self.assertEqual(d["name"], "Neuer Name")
        self.assertEqual([(z["menge"], z["einheit"], z["name"]) for z in d["zutaten"]], [(2, "TL", "sugar"), (1, "Bund", "parsley")])
        self.assertEqual(self.admin.post("/api/rezepte/gibt-es-nicht", {"beschreibung": "x"}).status, 400)

    def test_slug_unique_and_name_required(self):
        a = self.recipe("Doppelt Gekocht")
        b = self.recipe("Doppelt Gekocht")
        self.assertEqual((a, b), ("doppelt-gekocht", "doppelt-gekocht-2"))
        self.assertEqual(self.recipe("Käsespätzle über Nacht"), "kasespatzle-uber-nacht")
        r = self.admin.post("/api/rezepte", {"beschreibung": "ohne Namen"})
        self.assertEqual(r.status, 400)
        self.assertIn("Name", r.json()["error"])

    def test_language(self):
        lang = self.ok(self.admin.get("/api/household"))["language"]
        self.assertEqual(lang, "en", "a fresh installation without recipes starts in English")
        self.assertEqual(self.ok(self.admin.get(f"/api/rezepte/{self.recipe('Pancakes')}"))["sprache"], "en")
        de = self.recipe("Pfannkuchen", sprache="de")
        self.assertEqual(self.ok(self.admin.get(f"/api/rezepte/{de}"))["sprache"], "de")
        self.assertEqual(self.ok(self.admin.get(f"/api/rezepte/{self.recipe('Crêpes', sprache='fr')}"))["sprache"], "en")
        # editing keeps the language, unless it is sent
        self.ok(self.admin.post(f"/api/rezepte/{de}", {"beschreibung": "lecker"}))
        self.assertEqual(self.ok(self.admin.get(f"/api/rezepte/{de}"))["sprache"], "de")
        self.ok(self.admin.post(f"/api/rezepte/{de}", {"sprache": "en"}))
        self.assertEqual(self.ok(self.admin.get(f"/api/rezepte/{de}"))["sprache"], "en")
        listed = {x["slug"]: x["sprache"] for x in self.ok(self.admin.get("/api/rezepte"))}
        self.assertEqual(listed[de], "en")
        # new recipes follow the household language
        self.ok(self.admin.post("/api/household", {"language": "de"}))
        try:
            self.assertEqual(self.ok(self.admin.get(f"/api/rezepte/{self.recipe('Bratkartoffeln')}"))["sprache"], "de")
        finally:
            self.ok(self.admin.post("/api/household", {"language": "en"}))

    def test_rating(self):
        slug = self.recipe("Bewertet")
        self.assertEqual(self.ok(self.admin.post(f"/api/rezepte/{slug}/sterne", {"sterne": 4}))["sterne"], 4)
        self.assertEqual(self.ok(self.admin.post(f"/api/rezepte/{slug}/sterne", {"sterne": 9}))["sterne"], 5)
        self.assertEqual(self.ok(self.admin.get(f"/api/rezepte/{slug}"))["bewertung"], 5)

    def test_unknown_recipe_is_an_error(self):
        r = self.admin.get("/api/rezepte/gibt-es-nicht")
        self.assertGreaterEqual(r.status, 400)
        self.assertIn("nicht gefunden", r.json()["error"])

    def test_unknown_recipe_is_404(self):
        # regression (was a 502)
        self.assertEqual(self.admin.get("/api/rezepte/gibt-es-nicht").status, 404)

    def test_image_upload(self):
        slug = self.recipe("Mit Bild")
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        self.assertEqual(self.ok(self.admin.post(f"/api/rezepte/{slug}/bild", raw=png, content_type="image/png"))["bild"], "png")
        d = self.ok(self.admin.get(f"/api/rezepte/{slug}"))
        self.assertTrue(d["bild"])
        r = self.admin.get(f"/mimg/{d['id']}/original.webp")
        self.ok(r)
        self.assertEqual(r.body, png)
        self.assertEqual(r.header("content-type"), "image/png")
        self.assertEqual(self.admin.post(f"/api/rezepte/{slug}/bild", raw=b"kein bild", content_type="image/png").status, 400)


if __name__ == "__main__":
    unittest.main()
