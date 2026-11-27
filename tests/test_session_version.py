"""Stay signed in, the cookie name per installation (and taking over the old shared name),
the update notice and the source link in /api/version."""
import json
import time
import unittest

try:
    from .harness import ADMIN, FakeSite, ServerTestCase
except ImportError:
    from harness import ADMIN, FakeSite, ServerTestCase


class SessionCookieTest(ServerTestCase):

    def session_cookie(self, r):
        return next(c for c in r.set_cookies if c.startswith("kombuese_") and "max-age=0" not in c.lower())

    def test_cookie_name_is_per_installation(self):
        c = self.server.client()
        r = c.login(*ADMIN)
        name = c.cookie[0]
        self.assertRegex(name, r"^kombuese_[0-9a-f]{8}$")
        self.assertNotEqual(name, "kombuese_session")
        self.assertIn("Max-Age=", self.session_cookie(r))
        c2 = self.server.client()
        c2.login(*ADMIN)
        self.assertEqual(c2.cookie[0], name, "the name stays the same")

    def test_without_stay_signed_in_the_cookie_ends_with_the_browser(self):
        c = self.server.client()
        r = c.post("/auth/login", {"username": ADMIN[0], "password": ADMIN[1], "remember": False})
        self.ok(r)
        self.assertNotIn("Max-Age", self.session_cookie(r))
        self.assertEqual(self.ok(c.get("/api/me"))["username"], ADMIN[0])

    def test_old_shared_cookie_name_is_taken_over(self):
        c = self.server.client()
        c.login(*ADMIN)
        name, token = c.cookie
        old = self.server.client()
        old.cookie = ("kombuese_session", token)  # as a browser with the older cookie sends it
        r = old.get("/api/me")
        self.assertEqual(self.ok(r)["username"], ADMIN[0])
        self.assertEqual(old.cookie, (name, token), "moved to the new name")
        self.assertTrue(any(s.startswith("kombuese_session=;") and "Max-Age=0" in s for s in r.set_cookies), r.set_cookies)
        self.assertEqual(self.ok(old.get("/api/me"))["username"], ADMIN[0])

    def test_logout_clears_both_names(self):
        c = self.server.client()
        c.login(*ADMIN)
        r = c.post("/auth/logout")
        self.ok(r)
        cleared = [s.split("=", 1)[0] for s in r.set_cookies if "Max-Age=0" in s]
        self.assertIn("kombuese_session", cleared)
        self.assertEqual(len(cleared), 2)
        self.assertIsNone(c.cookie)


class VersionDefaultsTest(ServerTestCase):

    def test_defaults(self):
        d = self.ok(self.server.client().get("/api/version"))
        self.assertIsNone(d["update"], "no update check in tests (feed off)")
        self.assertNotIn("support_url", d, "no donation link")
        self.assertEqual((d["source_url"], d["license"]), ("https://github.com/Suppe12/kombuese", "AGPL-3.0-or-later"))


class VersionExtrasTest(ServerTestCase):
    """A fake release feed on 127.0.0.1 (allowed with KOMBUESE_ALLOW_PRIVATE_URLS) announces a newer version."""

    @classmethod
    def setUpClass(cls):
        cls.site = FakeSite({"/latest": ("application/json", json.dumps({"tag_name": "v99.1", "html_url": "https://example.org/r/99.1"})),
                             "/old": ("application/json", json.dumps({"tag_name": "v0.1"}))})
        cls.SERVER_ENV = {"KOMBUESE_UPDATE_URL": cls.site.url + "/latest", "KOMBUESE_ALLOW_PRIVATE_URLS": "true",
                          "KOMBUESE_SOURCE_URL": "https://example.org/kombuese"}
        super().setUpClass()
        cls.addClassCleanup(cls.site.close)

    def test_update_notice_and_source(self):
        c = self.server.client()
        d = self.ok(c.get("/api/version"))  # starts the check in the background
        self.assertEqual(d["source_url"], "https://example.org/kombuese", "AGPL source link")
        for _ in range(50):
            d = self.ok(c.get("/api/version"))
            if d["update"]:
                break
            time.sleep(0.1)
        self.assertEqual(d["update"], {"latest": "99.1", "url": "https://example.org/r/99.1"})
        self.assertEqual(self.site.hits.count("/latest"), 1, "checked once, not on every request")


class VersionOffTest(ServerTestCase):
    SERVER_ENV = {"KOMBUESE_UPDATE_URL": "off"}

    def test_switched_off(self):
        d = self.ok(self.server.client().get("/api/version"))
        self.assertIsNone(d["update"])


if __name__ == "__main__":
    unittest.main()
