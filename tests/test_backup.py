"""Backup and moving house: export from one server, import into a second fresh one, refuse manipulated archives."""
import gzip
import io
import json
import tarfile
import unittest

try:
    from .harness import ADMIN, Server, ServerTestCase, free_port
except ImportError:
    from harness import ADMIN, Server, ServerTestCase, free_port

SECRET = "sk-test-NICHT-ECHT-backup-4711"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 100


def members(data):
    out = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar:
            out[m.name] = tar.extractfile(m).read() if m.isreg() else None
    return out


def build(files, extra=()):
    """files: {name: bytes}; extra: ready TarInfo objects (e.g. symlinks) → .tar.gz bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
        for ti in extra:
            tar.addfile(ti)
    return buf.getvalue()


class BackupTest(ServerTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        a = cls.admin
        for body in ({"name": "Gulasch", "portionen": 4, "zutaten_text": "1 kg Rindfleisch\n3 Zwiebeln", "sprache": "de"},
                     {"name": "Porridge", "portionen": 1, "zutaten_text": "50 g Haferflocken\n1 cup milk"}):
            assert a.post("/api/rezepte", body).status == 200
        cls.recipe_id = a.get("/api/rezepte/gulasch").json()["id"]
        assert a.post("/api/rezepte/gulasch/bild", raw=PNG, content_type="image/png").status == 200
        assert a.post("/api/plan", {"datum": "2030-01-07", "typ": "dinner", "slug": "gulasch"}).status == 200
        assert a.post("/api/einkauf/neu", {"text": "2 l Milch"}).status == 200
        assert a.post("/api/vorrat", {"name": "Chili", "anzahl": 2}).status == 200
        assert a.post("/api/schrank", {"name": "Reis", "menge": 1, "einheit": "kg"}).status == 200
        assert a.post("/api/cards", {"url": "https://example.org/a", "title": "Karte"}).status == 200
        assert a.post("/api/users", {"username": "mitbewohner", "password": "mitbewohner-pw", "role": "member"}).status == 200
        cls.api_token = a.post("/api/tokens", {"name": "umzug", "scope": "api"}).json()["token"]
        assert a.post("/api/household", {"language": "de", "description": "Testhaushalt"}).status == 200
        assert a.post("/api/ai", {"provider": "openai", "base_url": f"http://127.0.0.1:{free_port()}/v1",
                                  "model": "fake-model", "api_key": SECRET}).status == 200

    def export(self, query=""):
        r = self.admin.get("/api/backup" + query)
        self.ok(r)
        self.assertEqual(r.header("content-type"), "application/gzip")
        self.assertIn("attachment", r.header("content-disposition"))
        return r.body

    def new_server(self):
        s = Server().start()
        self.addCleanup(s.stop)
        return s

    def test_export_contents(self):
        data = self.export()
        files = members(data)
        self.assertIn("manifest.json", files)
        self.assertIn("food-tinder.db", files)
        self.assertIn(f"rezeptbilder/{self.recipe_id}.png", files)
        self.assertNotIn("secret.key", files)
        man = json.loads(files["manifest.json"])
        self.assertEqual(man["format"], "kombuese-backup")
        self.assertFalse(man["with_credentials"])
        self.assertEqual({k: man["counts"][k] for k in ("recipes", "plan", "shopping", "freezer", "cupboard", "users", "api_tokens", "cards")},
                         {"recipes": 2, "plan": 1, "shopping": 1, "freezer": 1, "cupboard": 1, "users": 2, "api_tokens": 1, "cards": 1})
        everything = gzip.decompress(data)
        self.assertNotIn(SECRET.encode(), everything, "no API key in a backup without credentials")
        self.assertIn("ai", json.loads(files["integrations.json"])["items"], "AI settings travel without the key")

    def test_move_to_fresh_server(self):
        data = self.export()
        man = json.loads(members(data)["manifest.json"])
        b = self.new_server()
        guest = b.client()
        self.assertTrue(guest.get("/auth/status").json()["setup"])
        self.assertEqual(guest.post("/auth/setup/import", raw=data, content_type="application/gzip",
                                    headers={"X-Kombuese": None}).status, 403)
        r = guest.post("/auth/setup/import", raw=data, content_type="application/gzip")
        d = self.ok(r)
        self.assertEqual(d["counts"], man["counts"])
        self.assertEqual(d["images"]["rezeptbilder"], 1)
        # a second import through the start page is refused now that accounts exist
        self.assertEqual(b.client().post("/auth/setup/import", raw=data, content_type="application/gzip").status, 409)

        # the admin of the old server signs in on the new one; old sessions do not travel
        old = b.client()
        old.cookie = self.admin.cookie
        self.assertEqual(old.get("/api/me").status, 401)
        nb = b.client()
        nb.login(*ADMIN)
        self.assertEqual(nb.get("/api/me").json()["role"], "admin")
        b.client().login("mitbewohner", "mitbewohner-pw")
        self.assertEqual(sorted(x["name"] for x in nb.get("/api/rezepte").json()), ["Gulasch", "Porridge"])
        g = nb.get("/api/rezepte/gulasch").json()
        self.assertEqual(([(z["menge"], z["einheit"], z["name"]) for z in g["zutaten"]], g["sprache"]),
                         ([(1, "kg", "Rindfleisch"), (3, "", "Zwiebeln")], "de"))
        self.assertEqual(nb.get(f"/mimg/{self.recipe_id}/original.webp").body, PNG)
        self.assertEqual([i["name"] for i in nb.get("/api/einkauf").json()["items"]], ["Milch"])
        self.assertEqual([e["titel"] for e in nb.get("/api/woche?start=2030-01-07").json()["eintraege"]], ["Gulasch"])
        h = nb.get("/api/household").json()  # with more fields (people, members), compare the old ones
        self.assertEqual({k: h[k] for k in ("language", "description")}, {"language": "de", "description": "Testhaushalt"})
        self.assertTrue(b.client().get("/i18n.js").text.startswith('window.KOMBUESE_LANG = "de";'))
        self.assertEqual(b.client(token=self.api_token).get("/api/rezepte").status, 200, "API tokens move along")
        ai = nb.get("/api/ai").json()["settings"]
        self.assertEqual((ai["provider"], ai["model"], ai["has_key"]), ("openai", "fake-model", False))

        # with credentials: the key arrives, encrypted again with the new server's own key, and the admin stays signed in
        data2 = self.export("?secrets=1")
        files = members(data2)
        self.assertIn("secret.key", files, "one-time transfer key")
        self.assertTrue(json.loads(files["manifest.json"])["with_credentials"])
        self.assertNotIn(SECRET.encode(), gzip.decompress(data2))
        d = self.ok(nb.post("/api/backup/import", raw=data2, content_type="application/gzip"))
        self.assertEqual(d["account"], ADMIN[0])
        self.assertIn("ai", d["integrations"]["restored"])
        self.assertTrue(d["safety_backup"].startswith("kombuese-before-import-"))
        self.assertTrue(self.ok(nb.get("/api/ai"))["settings"]["has_key"])
        self.assertNotIn(SECRET, nb.get("/api/ai").text)

    def test_manipulated_archives_refused(self):
        good = self.export()
        files = members(good)
        man = json.loads(files["manifest.json"])
        before = man["counts"]

        def with_manifest(**change):
            return build({**files, "manifest.json": json.dumps({**man, **change}).encode()})

        link = tarfile.TarInfo("img/1.jpg")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        cases = {
            "no archive": (b"das ist kein Backup" * 10, "Keine Backup-Datei"),
            "path traversal": (build({**files, "../boese.txt": b"x"}), "Unerwartete Datei"),
            "absolute path": (build({**files, "/etc/cron.d/boese": b"x"}), "Unerwartete Datei"),
            "traversal in image dir": (build({**files, "img/../../boese.py": b"x"}), "Unerwartete Datei"),
            "unknown file": (build({**files, "server.py": b"print(1)"}), "Unerwartete Datei"),
            "symlink": (build(files, [link]), "Unerwartete Datei"),
            "newer version": (with_manifest(app_version="99.0"), "neueren Version"),
            "foreign format": (with_manifest(format="etwas-anderes"), "kein Backup der Kombüse"),
            "unknown format version": (with_manifest(format_version=2), "nicht unterstützt"),
            "no manifest": (build({k: v for k, v in files.items() if k != "manifest.json"}), "manifest.json fehlt"),
            "broken database": (build({**files, "food-tinder.db": b"SQLite format 3\x00" + b"kaputt" * 200}), "Datenbank"),
            "not a database": (build({**files, "food-tinder.db": b"kaputt" * 200}), "Datenbank im Backup ist kaputt"),
            "no database": (build({k: v for k, v in files.items() if k != "food-tinder.db"}), "Datenbank fehlt"),
            "oversized key": (build({**files, "secret.key": b"k" * 64}), "zu groß"),
            "credentials without key": (build({**files, "integrations.json": json.dumps({"encrypted": True, "items": {}}).encode()}),
                                        "Schlüssel fehlt"),
        }
        for label, (data, msg) in cases.items():
            with self.subTest(label):
                r = self.admin.post("/api/backup/import", raw=data, content_type="application/gzip")
                self.assertEqual(r.status, 400, f"{label}: {r}")
                self.assertIn(msg, r.json()["error"], label)
        self.assertEqual(self.admin.post("/api/backup/import", raw=b"", content_type="application/gzip").status, 400)
        # nothing changed and the admin is still signed in
        self.assertEqual(json.loads(members(self.export())["manifest.json"])["counts"], before)
        self.assertEqual(self.ok(self.admin.get("/api/me"))["username"], ADMIN[0])
        self.assertFalse(self.server.client().get("/auth/status").json()["setup"])
        # also refused on the start page of a fresh server
        b = self.new_server()
        r = b.client().post("/auth/setup/import", raw=with_manifest(app_version="99.0"), content_type="application/gzip")
        self.assertEqual(r.status, 400)
        self.assertTrue(b.client().get("/auth/status").json()["setup"], "still a fresh installation")


if __name__ == "__main__":
    unittest.main()
