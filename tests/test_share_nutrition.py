"""Shared recipe links and the nutrition overview without Yazio."""
import unittest
from datetime import date, timedelta

try:
    from .harness import ServerTestCase
except ImportError:
    from harness import ServerTestCase


class ShareTest(ServerTestCase):

    def test_share_link_opens_only_this_recipe(self):
        slug = self.recipe("Geteilte Suppe", portionen=2, zutaten_text="200 g Linsen", schritte_text="Kochen",
                           naehrwerte={"kcal": 400, "protein": 20}, quelle="https://example.invalid/suppe", sprache="de")
        self.recipe("Geheimes Rezept")
        self.assertIsNone(self.ok(self.admin.get(f"/api/rezepte/{slug}/teilen"))["link"])
        link = self.ok(self.admin.post(f"/api/rezepte/{slug}/teilen", {}))["link"]
        self.assertEqual(self.ok(self.admin.post(f"/api/rezepte/{slug}/teilen", {}))["link"]["token"], link["token"], "one link per recipe")
        self.assertGreaterEqual(len(link["token"]), 32)
        path = "/s/" + link["token"]
        guest = self.server.client(app_header=False)
        r = self.ok(guest.get(path))
        html = r.text
        self.assertIn("Geteilte Suppe", html)
        self.assertIn("200 g", html)
        self.assertIn("noindex", html)
        self.assertIn("noindex", r.header("x-robots-tag"))
        self.assertEqual(r.header("referrer-policy"), "no-referrer")
        self.assertIn("default-src 'none'", r.header("content-security-policy"))
        self.assertNotIn("Geheimes Rezept", html)
        self.assertIn("400 g", self.ok(guest.get(path + "?portionen=4")).text, "servings adjustable")
        self.assertEqual(guest.get(path + "/bild").status, 404, "no photo, nothing to show")
        # the link is no login: nothing else is reachable with it
        for p in ("/api/rezepte", f"/api/rezepte/{slug}", "/api/woche", "/api/me", "/api/freigaben", f"/rezept/{slug}/druck",
                  path + "/../api/rezepte", "/s/" + link["token"] + "/api/rezepte"):
            self.assertNotEqual(guest.get(p).status, 200, p)
        self.assertIn(guest.get(f"/api/rezepte?token={link['token']}").status, (401, 403))
        self.assertIn(guest.request("GET", "/api/rezepte", headers={"Authorization": "Bearer " + link["token"]}).status, (401, 403))
        self.assertEqual(guest.post(path, {}).status, 405)
        self.assertEqual(guest.get("/s/" + "x" * 32).status, 404)
        # listed for admins, revoked = gone
        listed = self.ok(self.admin.get("/api/freigaben"))
        self.assertEqual([(x["slug"], x["aufrufe"]) for x in listed], [(slug, 2)])
        member, _ = self.member()
        self.assertEqual(member.get("/api/freigaben").status, 403)
        self.ok(self.admin.post(f"/api/freigaben/{link['id']}/weg", {}))
        self.assertEqual(guest.get(path).status, 404)
        self.assertEqual(guest.get(path + ".pdf").status, 404)
        self.assertIsNone(self.ok(self.admin.get(f"/api/rezepte/{slug}/teilen"))["link"])
        self.assertEqual(self.ok(self.admin.get("/api/freigaben")), [])
        new = self.ok(self.admin.post(f"/api/rezepte/{slug}/teilen", {}))["link"]
        self.assertNotEqual(new["token"], link["token"], "sharing again makes a new link")

    def test_unknown_tokens_are_rate_limited(self):
        guest = self.server.client(app_header=False, forwarded_for=None)
        codes = [guest.get("/s/" + f"{i:032d}").status for i in range(25)]
        self.assertEqual(codes[0], 404)
        self.assertEqual(codes[-1], 429)


class NutritionTest(ServerTestCase):

    def test_planned_meal_eaten_on_another_day(self):
        # tomorrow's breakfast eaten today is booked for today, not for the day it was planned
        today, tomorrow = str(date.today()), str(date.today() + timedelta(days=1))
        slug = self.recipe("Overnight Oats", portionen=1, naehrwerte={"kcal": 400, "protein": 20, "kh": 50, "fett": 10})
        self.ok(self.admin.post("/api/plan", {"datum": tomorrow, "typ": "breakfast", "slug": slug, "scale": 1}))
        w = self.ok(self.admin.get(f"/api/woche?start={tomorrow}"))
        e = next(x for x in w["eintraege"] if x["date"] == tomorrow and x["typ"] == "breakfast")
        self.ok(self.admin.post("/api/gegessen", {"mealplan_id": e["id"], "datum": today}))
        n = self.ok(self.admin.get(f"/api/ernaehrung?start={today}&tage=2"))
        me = next(p for p in n["personen"] if p["ich"])
        eaten = {d["datum"]: d["gegessen"]["kcal"] for d in me["tage"]}
        self.assertEqual((eaten.get(today), eaten.get(tomorrow)), (400, 0))

    def test_eaten_without_yazio_per_person(self):
        today = str(date.today())
        slug = self.recipe("Proteinbowl", portionen=2, naehrwerte={"kcal": 600, "protein": 40, "kh": 50, "fett": 20})
        self.ok(self.admin.post("/api/plan", {"datum": today, "typ": "lunch", "slug": slug, "scale": 1}))
        self.ok(self.admin.post("/api/plan", {"datum": today, "typ": "dinner", "slug": slug, "scale": 1}))
        member, mid = self.member()
        w = self.ok(self.admin.get("/api/woche"))
        self.assertFalse(w["yazio"])
        lunch, dinner = sorted((e for e in w["eintraege"] if e["date"] == today), key=lambda e: e["typ"] != "lunch")
        # before eating: planned and open
        n = self.ok(self.admin.get("/api/ernaehrung"))
        me = next(p for p in n["personen"] if p["ich"])
        day = next(d for d in me["tage"] if d["datum"] == today)
        self.assertEqual((day["gegessen"]["kcal"], day["offen"]["kcal"]), (0, 1200))
        self.assertEqual(next(d for d in n["geplant"] if d["datum"] == today)["kcal"], 1200)
        self.assertIsNone(me["ziel"]["kcal"])
        # eaten without Yazio: kept locally
        r = self.ok(self.admin.post("/api/gegessen", {"mealplan_id": lunch["id"], "portionen": 1.5}))
        self.assertEqual((r["kcal"], r["yazio"]), (900, False))
        self.assertEqual(self.admin.post("/api/gegessen", {"mealplan_id": lunch["id"]}).status, 409)
        self.ok(member.post("/api/gegessen", {"mealplan_id": lunch["id"]}))  # the other person ate it too
        w = self.ok(self.admin.get("/api/woche"))
        e = next(x for x in w["eintraege"] if x["id"] == lunch["id"])
        self.assertEqual(e["gegessen"]["portionen"], 1.5)
        self.assertEqual(len(e["gegessen_von"]), 2)
        n = self.ok(self.admin.get("/api/ernaehrung"))
        by = {p["id"]: next(d for d in p["tage"] if d["datum"] == today) for p in n["personen"]}
        self.assertEqual((by[mid]["gegessen"]["kcal"], by[mid]["offen"]["kcal"]), (600, 600))
        mine = next(p for p in n["personen"] if p["ich"])
        self.assertEqual(next(d for d in mine["tage"] if d["datum"] == today)["gegessen"]["protein"], 60)
        # several people at once, ad hoc from a recipe
        r = self.ok(self.admin.post("/api/gegessen", {"slug": slug, "datum": today, "personen": [mid]}))
        self.assertEqual(r["personen"], [mid])
        self.assertEqual(self.admin.post("/api/gegessen", {"slug": slug, "personen": [99999]}).status, 400)
        # undo only my entry
        self.ok(self.admin.post(f"/api/gegessen/{lunch['id']}/weg", {}))
        w = self.ok(self.admin.get("/api/woche"))
        e = next(x for x in w["eintraege"] if x["id"] == lunch["id"])
        self.assertIsNone(e["gegessen"])
        self.assertEqual(len(e["gegessen_von"]), 1)
        self.assertEqual(dinner["typ"], "dinner")

    def test_goals_per_person(self):
        member, mid = self.member()
        me = self.ok(self.admin.get("/api/me"))["id"]
        self.ok(member.post("/api/ernaehrung/ziel", {"kcal": 2000, "protein": "120"}))
        self.assertEqual(member.post("/api/ernaehrung/ziel", {"user_id": me, "kcal": 1800}).status, 403)
        self.ok(self.admin.post("/api/ernaehrung/ziel", {"user_id": mid, "protein": ""}))
        self.assertEqual(self.admin.post("/api/ernaehrung/ziel", {"kcal": 99999}).status, 400)
        n = self.ok(member.get("/api/ernaehrung?tage=1"))
        goals = {p["id"]: p["ziel"] for p in n["personen"]}
        self.assertEqual(goals[mid], {"kcal": 2000, "protein": None})
        self.assertEqual(goals[me], {"kcal": None, "protein": None})
        self.assertEqual(len(n["personen"][0]["tage"]), 1)
        self.assertEqual(member.post("/api/ernaehrung/yazio", {"user_id": mid}).status, 403)
        self.assertEqual(self.ok(self.admin.post("/api/ernaehrung/yazio", {"user_id": me}))["yazio_person"], me)


if __name__ == "__main__":
    unittest.main()
