"""Reheating in the microwave: the recipe field aufwaermen, more portions, another microwave,
the estimate without the field, leftover entries, print and share page, and the AI job „add reheating tips“."""
import json
import unittest
from datetime import date

try:
    from .harness import ServerTestCase
except ImportError:
    from harness import ServerTestCase

FIELD = {"kuehlschrank": {"watt": 800, "minuten": "2:30", "hinweis": "nach der Hälfte umrühren"},
         "gefroren": {"watt": "900", "minuten": 6}, "ruhen": "1", "alternative": "Topf, 5 min", "hinweis": "etwas Wasser dazu",
         "unsinn": "weg damit"}


class ReheatTest(ServerTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.slug = cls.admin.post("/api/rezepte", {"name": "Chili zum Aufwärmen", "portionen": 4, "aufwaermen": FIELD}).json()["slug"]

    def tearDown(self):
        self.ok(self.admin.post("/api/household", {"microwave_watt": 800}))

    def info(self, slug=None, **q):
        qs = "&".join(f"{k}={v}" for k, v in {"slug": slug or self.slug, **q}.items())
        return self.ok(self.admin.get("/api/aufwaermen?" + qs))

    def test_field_round_trip(self):
        d = self.ok(self.admin.get(f"/api/rezepte/{self.slug}"))
        self.assertEqual(d["aufwaermen"], {
            "kuehlschrank": {"watt": 800, "minuten": 2.5, "hinweis": "nach der Hälfte umrühren"},
            "gefroren": {"watt": 900, "minuten": 6.0, "hinweis": ""}, "ruhen": 1.0,
            "alternative": "Topf, 5 min", "hinweis": "etwas Wasser dazu"})
        i = d["aufwaermen_info"]
        self.assertFalse(i["geschaetzt"])
        self.assertEqual((i["kuehlschrank"]["zeit"], i["kuehlschrank"]["watt"], i["watt"]), ("2:30", 800, 800))
        listed = next(r for r in self.ok(self.admin.get("/api/rezepte")) if r["slug"] == self.slug)
        self.assertTrue(listed["aufwaermen"])
        # changing another field keeps it
        self.ok(self.admin.post(f"/api/rezepte/{self.slug}", {"tags": ["Mealprep"]}))
        self.assertEqual(self.ok(self.admin.get(f"/api/rezepte/{self.slug}"))["aufwaermen"]["kuehlschrank"]["minuten"], 2.5)

    def test_invalid_values_and_removing(self):
        slug = self.recipe("Kaputte Angaben", aufwaermen={"kuehlschrank": {"watt": 99999, "minuten": "90 s"},
                                                           "gefroren": {"minuten": "viel"}, "kalt": "nein"})
        a = self.ok(self.admin.get(f"/api/rezepte/{slug}"))["aufwaermen"]
        self.assertEqual(a, {"kuehlschrank": {"watt": 800, "minuten": 1.5, "hinweis": ""}})
        self.ok(self.admin.post(f"/api/rezepte/{slug}", {"aufwaermen": {}}))
        d = self.ok(self.admin.get(f"/api/rezepte/{slug}"))
        self.assertEqual(d["aufwaermen"], {})
        self.assertTrue(d["aufwaermen_info"]["geschaetzt"])
        self.assertFalse(next(r for r in self.ok(self.admin.get("/api/rezepte")) if r["slug"] == slug)["aufwaermen"])
        cold = self.recipe("Kalt gegessen", aufwaermen={"kalt": True, "hinweis": "kalt essen"})
        self.assertEqual(self.ok(self.admin.get(f"/api/rezepte/{cold}"))["aufwaermen"], {"kalt": True, "hinweis": "kalt essen"})

    def test_more_portions(self):
        self.assertEqual(self.info(portionen=1)["kuehlschrank"]["sekunden"], 150)
        two = self.info(portionen=2)
        self.assertEqual((two["kuehlschrank"]["sekunden"], two["kuehlschrank"]["zeit"]), (255, "4:15"))  # 150 × 1.7
        self.assertEqual(self.info(portionen=3)["kuehlschrank"]["zeit"], "6:00")  # 150 × 2.4
        self.assertEqual(two["portionen"], 2)

    def test_other_microwave(self):
        i = self.info(watt=600)  # 150 s × 800 / 600 = 200 s → 195 s (15 s steps)
        self.assertEqual((i["kuehlschrank"]["watt"], i["kuehlschrank"]["sekunden"]), (600, 195))
        self.assertEqual(i["kuehlschrank"]["empfohlen"], {"watt": 800, "minuten": 2.5})
        self.assertEqual(self.info()["gefroren"]["zeit"], "6:45")  # 6 min at 900 W in an 800 W microwave
        # the household setting is used when no watt is asked for, also in the recipe detail
        self.assertEqual(self.ok(self.admin.post("/api/household", {"microwave_watt": 1000}))["microwave_watt"], 1000)
        self.assertEqual(self.ok(self.admin.get("/api/household"))["microwave_watt"], 1000)
        d = self.ok(self.admin.get(f"/api/rezepte/{self.slug}"))["aufwaermen_info"]
        self.assertEqual((d["watt"], d["kuehlschrank"]["zeit"], d["gefroren"]["zeit"]), (1000, "2:00", "5:30"))
        self.assertEqual(self.info(portionen=2)["kuehlschrank"]["zeit"], "3:30")  # 150 × 1.7 × 0.8 = 204 s → 210 s
        self.assertEqual(self.admin.get(f"/api/aufwaermen?slug={self.slug}&watt=viel").status, 400)

    def test_estimate_without_field(self):
        curry = self.info(self.recipe("Linsen-Dal", tags=["Curry", "Mealprep"]))
        self.assertTrue(curry["geschaetzt"])
        self.assertEqual((curry["kuehlschrank"]["empfohlen"]["minuten"], curry["gefroren"]["empfohlen"]["minuten"]), (2.5, 6))
        self.assertTrue(curry["kuehlschrank"]["hinweis"])
        oats = self.info(self.recipe("Overnight Oats mit Beeren", kategorien=["Frühstück"]))
        self.assertTrue(oats["kalt"])
        self.assertIsNone(oats["kuehlschrank"])
        bake = self.info(self.recipe("Gemüselasagne"))
        self.assertEqual(bake["kuehlschrank"]["zeit"], "3:00")
        self.assertIn("180", bake["alternative"])
        muffin = self.info(self.recipe("Eiermuffins", kategorien=["Frühstück"]))
        self.assertEqual(muffin["kuehlschrank"]["zeit"], "1:00")
        fresh = self.info(self.recipe("Gebratener Reis", notizen=[{"titel": "Mealprep / Reste", "text": "2 Tage im Kühlschrank, nicht einfrieren"}]))
        self.assertEqual(fresh["kuehlschrank"]["zeit"], "2:00")
        self.assertIsNone(fresh["gefroren"], "no frozen time for a dish that does not freeze")
        self.assertTrue(self.info(self.recipe("Sauerteig-Mischbrot"))["kalt"], "bread does not go into the microwave")
        self.assertEqual(self.info(self.recipe("Burrito-Bowl"))["kuehlschrank"]["zeit"], "2:00", "a bowl is a rice dish, not a wrap")
        other = self.info(self.recipe("Hähnchen mit Gemüse"))
        self.assertEqual(other["kuehlschrank"]["zeit"], "2:30")
        english = self.info(self.recipe("Beef stew", sprache="en"))
        self.assertIn("stir", english["kuehlschrank"]["hinweis"])
        # only a hint stored: the times still come from the estimate
        hint = self.info(self.recipe("Kürbissuppe", aufwaermen={"hinweis": "mit Kernen"}))
        self.assertTrue(hint["geschaetzt"])
        self.assertEqual((hint["kuehlschrank"]["zeit"], hint["hinweis"]), ("3:00", "mit Kernen"))

    def test_leftover_entry(self):
        today = str(date.today())
        rid = self.ok(self.admin.post("/api/plan", {"datum": today, "typ": "lunch", "titel": "Rest: Chili zum Aufwärmen (eingefroren)"}))["id"]
        i = self.ok(self.admin.get(f"/api/aufwaermen?eintrag={rid}&portionen=2"))
        self.assertEqual((i["slug"], i["kuehlschrank"]["zeit"], i["gefroren"]["zeit"]), (self.slug, "4:15", "11:30"))
        none = self.ok(self.admin.post("/api/plan", {"datum": today, "typ": "dinner", "titel": "Kantine"}))["id"]
        self.assertEqual(self.admin.get(f"/api/aufwaermen?eintrag={none}").status, 404)
        self.assertEqual(self.admin.get("/api/aufwaermen?eintrag=999999").status, 404)
        self.assertEqual(self.admin.get("/api/aufwaermen?slug=gibt-es-nicht").status, 404)
        self.assertEqual(self.admin.get("/api/aufwaermen").status, 404)

    def test_print_and_share_page(self):
        self.ok(self.admin.post("/api/household", {"microwave_watt": 1000}))
        page = self.ok(self.admin.get(f"/rezept/{self.slug}/druck")).text
        self.assertIn("Aufwärmen", page)
        self.assertIn("1000 W · 2:00 min", page)
        self.assertIn("1 min ruhen lassen", page)
        self.assertIn("70 %", page)
        self.assertIn("Reheating", self.ok(self.admin.get(f"/rezept/{self.slug}/druck?lang=en")).text)
        link = self.ok(self.admin.post(f"/api/rezepte/{self.slug}/teilen", {}))["link"]
        shared = self.ok(self.server.client().get("/s/" + link["token"])).text
        self.assertIn("800 W · 2:30 min", shared, "the public page keeps the recommended watt")
        estimated = self.ok(self.admin.get(f"/rezept/{self.recipe('Erbsensuppe')}/druck")).text
        self.assertIn("geschätzt", estimated)


class ReheatJobTest(ServerTestCase):
    """The AI job reheat: requested in the app, listed in /api/work, its prompt over MCP."""

    def test_job_for_outside_worker(self):
        self.recipe("Ohne Tipps")
        self.recipe("Mit Aufwärmen", aufwaermen={"kuehlschrank": {"watt": 800, "minuten": 2}})
        st = self.ok(self.admin.get("/api/aufwaermen/job"))
        self.assertEqual((st["state"], st["ohne"]), ("idle", 1))
        worker = self.server.client(token=self.token("api")["token"])
        self.assertEqual(self.ok(worker.get("/api/work"))["aufwaermen"]["state"], "idle")
        self.assertEqual(self.ok(self.admin.post("/api/aufwaermen/job", {}))["state"], "requested")
        self.assertEqual(self.ok(self.admin.post("/api/aufwaermen/job", {"alle": True}))["alle"], False, "a waiting job is not replaced")
        w = self.ok(worker.get("/api/work"))
        self.assertEqual(w["aufwaermen"]["state"], "requested")
        self.assertEqual(self.ok(self.admin.get("/api/status"))["aufwaermen"]["state"], "requested")
        # the worker reports back; the run may write the field through the recipe route
        self.ok(worker.post("/api/aufwaermen/state", {"state": "running"}))
        self.ok(worker.post("/api/rezepte/ohne-tipps", {"aufwaermen": {"kuehlschrank": {"watt": 800, "minuten": 3}}}))
        self.ok(worker.post("/api/aufwaermen/state", {"state": "done", "note": "1 recipe"}))
        st = self.ok(self.admin.get("/api/aufwaermen/job"))
        self.assertEqual((st["state"], st["note"], st["ohne"]), ("done", "1 recipe", 0))

    def test_mcp_prompt_and_recipe_rules(self):
        tok = self.token("api")["token"]
        c = self.server.client(token=tok)

        def rpc(method, params):
            r = self.ok(c.post("/mcp", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                               headers={"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}))
            self.assertNotIn("error", r, r)
            return r["result"]
        self.assertIn("reheat", {p["name"] for p in rpc("prompts/list", {})["prompts"]})
        text = rpc("prompts/get", {"name": "reheat", "arguments": {}})["messages"][0]["content"]["text"]
        self.assertIn("/api/aufwaermen/state", text)
        self.assertIn('"kuehlschrank"', text)
        link = rpc("prompts/get", {"name": "link", "arguments": {"url": "https://example.org/r"}})["messages"][0]["content"]["text"]
        self.assertIn("aufwaermen", link, "new recipes from the AI come with the reheating field")
        tools = rpc("tools/list", {})["tools"]
        api = next(t for t in tools if t["name"] == "kombuese_api")
        self.assertIn("/api/aufwaermen/state", api["description"])
        res = rpc("tools/call", {"name": "kombuese_api", "arguments": {"method": "GET", "path": "/api/aufwaermen/job"}})
        self.assertIn("ohne", json.loads(res["content"][0]["text"]), "the run may read the job state")


if __name__ == "__main__":
    unittest.main()
