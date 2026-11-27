"""Sign-in: first admin, login/logout, CSRF, 401, rate limit, API tokens, member vs admin."""
import unittest

try:
    from .harness import ADMIN, ServerTestCase
except ImportError:
    from harness import ADMIN, ServerTestCase


class SetupTest(ServerTestCase):
    """A fresh installation: the first visitor creates the admin, and only once."""

    SETUP_ADMIN = False

    def test_setup_flow(self):
        c = self.server.client()
        self.assertTrue(self.ok(c.get("/auth/status"))["setup"])
        r = c.get("/api/me")
        self.assertEqual(r.status, 401)
        self.assertTrue(r.json().get("login"))
        r = c.get("/")
        self.assertEqual(r.status, 302)
        self.assertEqual(r.header("location"), "/login")
        self.assertEqual(c.get("/login").status, 200)

        self.assertEqual(c.post("/auth/setup", {"username": "admin", "password": "kurz"}).status, 400)
        self.assertEqual(c.post("/auth/setup", {"username": "x", "password": "langes-passwort"}).status, 400)
        r = c.post("/auth/setup", {"username": ADMIN[0], "password": ADMIN[1]})
        self.ok(r)
        cookie = r.header("set-cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("Secure", cookie)  # plain http
        me = self.ok(c.get("/api/me"))
        self.assertEqual((me["username"], me["role"], me["via"]), (ADMIN[0], "admin", "session"))
        self.assertFalse(self.ok(c.get("/auth/status"))["setup"])

        other = self.server.client()
        self.assertEqual(other.post("/auth/setup", {"username": "zweiter", "password": "langes-passwort"}).status, 409)
        self.assertEqual(other.get("/api/me").status, 401)



class LoginTest(ServerTestCase):

    def test_secure_cookie_behind_https_proxy(self):
        c = self.server.client()
        r = c.post("/auth/login", {"username": ADMIN[0], "password": ADMIN[1]}, headers={"X-Forwarded-Proto": "https"})
        self.ok(r)
        self.assertIn("Secure", r.header("set-cookie"))

    def test_login_logout(self):
        c = self.server.client()
        self.assertEqual(c.post("/auth/login", {"username": ADMIN[0], "password": "falsch-falsch"}).status, 403)
        self.assertEqual(c.post("/auth/login", {"username": "niemand", "password": "falsch-falsch"}).status, 403)
        self.assertIsNone(c.cookie)
        c.login(ADMIN[0], ADMIN[1])
        self.assertEqual(self.ok(c.get("/api/me"))["username"], ADMIN[0])
        old = c.cookie
        self.ok(c.post("/auth/logout"))
        self.assertIsNone(c.cookie)
        self.assertEqual(c.get("/api/me").status, 401)
        c.cookie = old  # the old session cookie is invalid on the server, not only deleted in the browser
        self.assertEqual(c.get("/api/me").status, 401)

    def test_username_case_insensitive(self):
        c = self.server.client()
        c.login(ADMIN[0].upper(), ADMIN[1])
        self.assertEqual(self.ok(c.get("/api/me"))["username"], ADMIN[0])

    def test_401_without_login(self):
        c = self.server.client()
        for path in ("/api/rezepte", "/api/einkauf", "/api/woche", "/api/schrank", "/api/household", "/api/users",
                     "/api/backup", "/kalender.ics", "/api/work"):
            r = c.get(path)
            if path == "/kalender.ics":
                self.assertIn(r.status, (302, 401), path)
            else:
                self.assertEqual(r.status, 401, path)
        self.assertEqual(c.post("/api/rezepte", {"name": "x"}).status, 401)
        self.assertEqual(self.server.client(token="kmb_gibtesnicht").get("/api/rezepte").status, 401)
        # open routes
        for path in ("/api/version", "/i18n.js", "/manifest.json", "/icon.svg", "/auth/status"):
            self.assertEqual(c.get(path).status, 200, path)

    def test_csrf_header_required_for_session_posts(self):
        c = self.server.client(app_header=False)
        c.login(ADMIN[0], ADMIN[1])  # /auth/login itself needs no header
        r = c.post("/api/einkauf/neu", {"text": "Milch"})
        self.assertEqual(r.status, 403)
        self.assertIn("CSRF", r.json()["error"])
        r = c.post("/api/household", {"description": "x"})
        self.assertEqual(r.status, 403)
        self.assertEqual(c.get("/api/einkauf").status, 200)  # GET needs no header
        self.ok(c.post("/api/einkauf/neu", {"text": "Milch"}, headers={"X-Kombuese": "1"}))
        self.assertEqual(c.post("/auth/setup/import", raw=b"x", content_type="application/gzip").status, 403)

    def test_token_posts_need_no_csrf_header(self):
        t = self.token("api")
        c = self.server.client(token=t["token"], app_header=False)
        self.ok(c.post("/api/einkauf/neu", {"text": "Brot"}))


class RateLimitBehindProxyTest(ServerTestCase):
    """The test client is a trusted reverse proxy here, so the limit counts per X-Forwarded-For address."""

    SERVER_ENV = {"KOMBUESE_TRUSTED_PROXIES": "127.0.0.1,::1"}

    def test_too_many_failed_logins(self):
        c = self.server.client(forwarded_for="203.0.113.7")
        for i in range(10):
            self.assertEqual(c.post("/auth/login", {"username": ADMIN[0], "password": f"falsch-{i}"}).status, 403, i)
        r = c.post("/auth/login", {"username": ADMIN[0], "password": ADMIN[1]})
        self.assertEqual(r.status, 429, "even the right password is refused while the limit holds")
        self.assertIsNone(c.cookie)
        # other addresses are not affected
        self.server.client(forwarded_for="203.0.113.8").login(ADMIN[0], ADMIN[1])



class RateLimitTest(ServerTestCase):
    """No trusted proxy: every request counts for its direct sender, whatever X-Forwarded-For says."""

    def test_rate_limit_not_bypassed_by_spoofed_forwarded_for(self):
        # regression: X-Forwarded-For only counts from KOMBUESE_TRUSTED_PROXIES
        for i in range(10):
            c = self.server.client(forwarded_for=f"198.51.100.{i + 1}")
            self.assertEqual(c.post("/auth/login", {"username": ADMIN[0], "password": f"falsch-{i}"}).status, 403)
        c = self.server.client(forwarded_for="198.51.100.200")
        self.assertEqual(c.post("/auth/login", {"username": ADMIN[0], "password": "noch-falscher"}).status, 429)


class TokenTest(ServerTestCase):

    def test_api_token(self):
        t = self.token("api", "worker")
        self.assertTrue(t["token"].startswith("kmb_"))
        c = self.server.client(token=t["token"])
        self.assertEqual(self.ok(c.get("/api/me"))["via"], "token")
        self.ok(c.get("/api/rezepte"))
        self.ok(c.get("/kalender.ics"))
        # a token is never an admin
        for path in ("/api/users", "/api/tokens", "/api/integrations", "/api/ai", "/api/backup"):
            self.assertEqual(c.get(path).status, 403, path)
        self.assertEqual(c.post("/api/household", {"language": "de"}).status, 403)
        # the list of tokens shows neither the token nor its hash
        listed = self.ok(self.admin.get("/api/tokens"))
        row = next(x for x in listed if x["id"] == t["id"])
        self.assertIsNone(row["token_hash"])
        self.assertNotIn(t["token"], self.admin.get("/api/tokens").text)

    def test_token_as_query_parameter(self):
        t = self.token("api")
        self.ok(self.server.client().get(f"/api/rezepte?token={t['token']}"))

    def test_calendar_token_only_calendar(self):
        t = self.token("calendar")
        c = self.server.client(token=t["token"])
        r = c.get("/kalender.ics")
        self.ok(r)
        self.assertTrue(r.text.startswith("BEGIN:VCALENDAR"))
        self.ok(self.server.client().get(f"/kalender.ics?token={t['token']}"))
        for path in ("/api/rezepte", "/api/me", "/api/einkauf", "/"):
            r = c.get(path)
            self.assertEqual(r.status, 403, path)
        self.assertEqual(c.post("/api/einkauf/neu", {"text": "x"}).status, 403)

    def test_revoked_token_refused(self):
        t = self.token("api")
        c = self.server.client(token=t["token"])
        self.ok(c.get("/api/rezepte"))
        self.ok(self.admin.post(f"/api/tokens/{t['id']}/revoke"))
        self.assertEqual(c.get("/api/rezepte").status, 401)
        # without a valid token the feed behaves like any page without login (redirect to /login)
        self.assertIn(self.server.client().get(f"/kalender.ics?token={t['token']}").status, (302, 401))
        self.assertEqual(self.admin.post("/api/tokens/999999/revoke").status, 404)

    def test_token_validation(self):
        self.assertEqual(self.admin.post("/api/tokens", {"name": "", "scope": "api"}).status, 400)
        self.assertEqual(self.admin.post("/api/tokens", {"name": "x", "scope": "admin"}).status, 400)


class RoleTest(ServerTestCase):

    def test_member_refused_on_admin_routes(self):
        m, _ = self.member()
        self.assertEqual(self.ok(m.get("/api/me"))["role"], "member")
        for path in ("/api/users", "/api/tokens", "/api/integrations", "/api/ai", "/api/backup"):
            r = m.get(path)
            self.assertEqual(r.status, 403, path)
        for path, body in (("/api/users", {"username": "neu", "password": "langes-passwort"}),
                           ("/api/tokens", {"name": "x"}), ("/api/household", {"language": "de"}),
                           ("/api/ai", {"provider": "off"}), ("/api/integrations/ntfy", {"url": "http://x"})):
            self.assertEqual(m.post(path, body).status, 403, path)
        self.assertEqual(m.post("/api/backup/import", raw=b"x", content_type="application/gzip").status, 403)
        # everyday routes work
        for path in ("/api/rezepte", "/api/einkauf", "/api/household", "/api/ai/status", "/api/schrank"):
            self.ok(m.get(path))
        # and the admin may do all of it
        for path in ("/api/users", "/api/tokens", "/api/integrations", "/api/ai"):
            self.ok(self.admin.get(path))

    def test_disabled_member_loses_session(self):
        m, uid = self.member()
        self.ok(m.get("/api/me"))
        self.ok(self.admin.post(f"/api/users/{uid}", {"disabled": True}))
        self.assertEqual(m.get("/api/me").status, 401)
        self.ok(self.admin.post(f"/api/users/{uid}", {"disabled": False}))
        self.ok(self.admin.post(f"/api/users/{uid}", {"delete": True}))
        self.assertNotIn(uid, [u["id"] for u in self.ok(self.admin.get("/api/users"))])

    def test_last_admin_protected(self):
        me = self.ok(self.admin.get("/api/me"))
        self.assertEqual(self.admin.post(f"/api/users/{me['id']}", {"role": "member"}).status, 400)
        self.assertEqual(self.admin.post(f"/api/users/{me['id']}", {"delete": True}).status, 400)
        self.assertEqual(self.admin.post(f"/api/users/{me['id']}", {"disabled": True}).status, 400)
        self.assertEqual(self.ok(self.admin.get("/api/me"))["role"], "admin")

    def test_duplicate_username(self):
        self.member("doppelt")
        r = self.admin.post("/api/users", {"username": "DOPPELT", "password": "langes-passwort"})
        self.assertEqual(r.status, 400)

    def test_change_own_password(self):
        m, _ = self.member("passwortwechsel", "erstes-passwort")
        self.assertEqual(m.post("/api/me/password", {"old": "falsch-falsch", "new": "zweites-passwort"}).status, 403)
        self.ok(m.post("/api/me/password", {"old": "erstes-passwort", "new": "zweites-passwort"}))
        self.server.client().login("passwortwechsel", "zweites-passwort")
        self.assertEqual(self.server.client().post("/auth/login", {"username": "passwortwechsel", "password": "erstes-passwort"}).status, 403)

    def test_users_list_has_no_password_hash(self):
        self.member()
        r = self.admin.get("/api/users")
        self.ok(r)
        self.assertNotIn("scrypt$", r.text)
        self.assertNotIn("password_hash", r.text)


if __name__ == "__main__":
    unittest.main()
