"""Demo household (tools/demo.py): KOMBUESE_DEMO=true on the first start and `admin.py demo` fill an empty installation,
and the app works with it. No network: the demo photos are files in the repository."""
import os
import re
import subprocess
import sys
from datetime import date

from tests.harness import ROOT, Server, ServerTestCase


class DemoMixin:
    def check_household(self, c, lang):
        today = str(date.today())
        recipes = self.ok(c.get("/api/rezepte"))
        self.assertGreaterEqual(len(recipes), 20)
        self.assertTrue(all(r["sprache"] == lang for r in recipes))
        self.assertTrue(all(r["kcal"] for r in recipes), "every demo recipe has nutrition")
        self.assertTrue(all(r["aufwaermen"] for r in recipes), "every demo recipe has reheating tips")
        with_photo = [r for r in recipes if r["bild"]]
        self.assertGreater(len(with_photo), 10)
        img = c.get(f"/mimg/{with_photo[0]['id']}/original.webp")
        self.assertEqual(img.status, 200)
        detail = self.ok(c.get(f"/api/rezepte/{with_photo[0]['slug']}"))
        self.assertIn("Wikimedia Commons", str(detail), "photo credit in the recipe note")

        week = self.ok(c.get("/api/woche"))
        self.assertGreater(len(week["eintraege"]), 5)
        self.assertTrue(any(e["rest"] for e in week["eintraege"]), "leftover entries are recognised")
        prep = self.ok(c.get(f"/api/mealprep?datum={today}"))
        self.assertGreater(len(prep["ablauf"]["schritte"]), 5)
        wp = self.ok(c.get("/api/wochenplan"))["vorschlag"]
        self.assertGreater(len(wp["eintraege"]), 10)
        self.assertGreater(wp["start"], today)

        shop = self.ok(c.get("/api/einkauf"))["items"]
        self.assertGreater(len(shop), 5)
        self.assertTrue(any(i["im_schrank"] for i in shop), "the cupboard covers part of the list")
        pantry = self.ok(c.get("/api/schrank"))
        self.assertGreater(len(pantry["schrank"]), 10)
        freezer = self.ok(c.get("/api/vorrat"))
        self.assertGreater(len(freezer), 3)

        status = self.ok(c.get("/api/status"))
        self.assertGreater(status["counts"].get("neu", 0), 5)
        stack = self.ok(c.get("/api/stack?meal=haupt"))
        self.assertGreater(len(stack), 3)
        nut = self.ok(c.get("/api/ernaehrung?tage=7&start=" + str(date.fromordinal(date.today().toordinal() - 3))))
        self.assertEqual(len(nut["personen"]), 2)
        self.assertTrue(any(p["summe"]["kcal"] > 0 for p in nut["personen"]), "nutrition history of the last days")
        # the installation is taken: nobody can create the first admin any more
        self.assertEqual(self.server.client().post("/auth/setup", {"username": "x", "password": "irgendein-passwort-1"}).status, 409)


class DemoOnFirstStart(DemoMixin, ServerTestCase):
    SETUP_ADMIN = False
    SERVER_ENV = {"KOMBUESE_DEMO": "true"}

    def test_env_fills_empty_installation(self):
        m = re.search(r"password: (\S+)", self.server.log())
        self.assertIsNotNone(m, self.server.log()[-2000:])
        self.assertNotEqual(m.group(1), "kombuese-demo", "the public password only with --public-demo")
        c = self.server.client()
        c.login("demo", m.group(1))
        self.check_household(c, "en")


class DemoCommand(DemoMixin, ServerTestCase):
    SETUP_ADMIN = False

    @classmethod
    def setUpClass(cls):
        # admin.py demo into an empty data directory before the server starts, as with docker exec
        cls.server = Server()
        os.makedirs(cls.server.data, exist_ok=True)
        python = os.environ.get("KOMBUESE_TEST_PYTHON") or sys.executable
        run = lambda *a: subprocess.run([python, os.path.join(ROOT, "tools", "admin.py"), *a], env=cls.server.env,  # noqa: E731
                                        capture_output=True, text=True, encoding="utf-8", timeout=120)
        cls.first = run("demo", "--lang", "de", "--public-demo")
        cls.second = run("demo")
        cls.server.start()
        cls.addClassCleanup(cls.server.stop)

    def test_command_fills_once(self):
        self.assertEqual(self.first.returncode, 0, self.first.stderr)
        self.assertIn("kombuese-demo", self.first.stdout)
        self.assertNotEqual(self.second.returncode, 0, "a second run must not touch a filled installation")
        self.assertIn("not empty", self.second.stderr)
        c = self.server.client()
        c.login("demo", "kombuese-demo")
        self.check_household(c, "de")
        self.assertEqual(self.ok(c.get("/api/household"))["language"], "de")
