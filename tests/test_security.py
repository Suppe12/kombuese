"""Regression tests for the findings of the security review before the open-source release (docs/security.md).

HTTP only, like the other tests; the DNS rebinding check of netsafe.py is in test_netsafe.py (needs a fake resolver).
"""
import json
import socket
import time
import urllib.parse

from harness import ADMIN, ServerTestCase


def raw_request(port, data, timeout=10):
    """Send bytes as they are (http.client refuses odd headers) → first bytes of the answer, b"" when closed."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        if data:
            s.sendall(data)
        try:
            return s.recv(4096)
        except socket.timeout:
            return None


class SecurityTest(ServerTestCase):

    def test_print_page_does_not_reflect_lang(self):
        """/rezept/<slug>/druck?lang= went unescaped into <html lang="…">: reflected XSS for anyone signed in."""
        slug = self.recipe("Reflektiert", zutaten_text="1 Ei", schritte_text="Kochen")
        evil = '"><script>alert(1)</script>'
        r = self.ok(self.admin.get(f"/rezept/{slug}/druck?lang={urllib.parse.quote(evil)}"))
        self.assertNotIn("<script>alert(1)", r.text)
        self.assertIn('<html lang="de">', r.text)
        self.assertIn('<html lang="en">', self.ok(self.admin.get(f"/rezept/{slug}/druck?lang=en")).text)

    def test_card_numbers_are_numbers(self):
        """kcal and minutes of a card went as text into INTEGER columns and from there unescaped into the swipe stack."""
        evil = "<img src=x onerror=alert(1)>"
        d = self.ok(self.admin.post("/api/cards", [
            {"url": "https://example.org/sec-1", "title": "Karte 1", "kcal": evil, "minutes": evil},
            {"url": "https://example.org/sec-2", "title": "Karte 2", "kcal": "450", "minutes": 35.0}]))
        cards = {c["id"]: c for c in self.ok(self.admin.get("/api/stack?limit=100"))}
        bad, good = cards[d["added"][0]], cards[d["added"][1]]
        self.assertIsNone(bad["kcal"])
        self.assertIsNone(bad["minutes"])
        self.assertEqual((good["kcal"], good["minutes"]), (450, 35))
        self.assertNotIn("onerror", self.admin.get("/api/stack?limit=100").text)

    def test_mealie_slug_must_be_a_slug(self):
        """The slug of an imported card goes into an onclick of the app; an AI run on a hostile page could set any text."""
        cid = self.ok(self.admin.post("/api/cards", {"url": "https://example.org/sec-3", "title": "Karte 3"}))["added"][0]
        self.assertEqual(self.admin.post(f"/api/cards/{cid}/mealie", {"slug": "x');alert(1);//"}).status, 400)
        self.ok(self.admin.post(f"/api/cards/{cid}/mealie", {"slug": "karte-3"}))

    def test_security_headers(self):
        for path in ("/", "/api/me", "/login"):
            r = self.admin.get(path)
            self.assertEqual(r.header("x-content-type-options"), "nosniff", path)
            self.assertEqual(r.header("referrer-policy"), "same-origin", path)
            self.assertIn("frame-ancestors 'self'", r.header("content-security-policy") or "", path)
            self.assertEqual(r.header("x-frame-options"), "SAMEORIGIN", path)
        # the public recipe page keeps its own, stricter policy
        slug = self.recipe("Geteilt sicher", zutaten_text="1 Ei", schritte_text="Kochen")
        url = self.ok(self.admin.post(f"/api/rezepte/{slug}/teilen"))["link"]["url"]
        r = self.ok(self.server.client().get(urllib.parse.urlsplit(url).path))
        self.assertIn("default-src 'none'", r.header("content-security-policy"))
        self.assertEqual(r.header("referrer-policy"), "no-referrer")

    def test_negative_content_length_refused(self):
        """Content-Length: -1 made the handler read until the client closed the connection."""
        t = time.time()
        answer = raw_request(self.server.port, b"POST /auth/login HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                                               b"X-Kombuese: 1\r\nContent-Length: -1\r\n\r\n{}")
        self.assertTrue(answer and answer.startswith(b"HTTP/1.0 400"), answer)
        self.assertLess(time.time() - t, 5)

    def test_auth_posts_only_from_the_app(self):
        """Login CSRF: a text/plain POST from another site (no preflight) could sign the victim in to the attacker's account."""
        body = json.dumps({"username": "admin", "password": "admin-passwort-123"}).encode()
        c = self.server.client(app_header=False)
        r = c.request("POST", "/auth/login", raw=body, content_type="text/plain")
        self.assertEqual(r.status, 403)
        self.assertFalse(r.set_cookies)
        self.assertEqual(c.request("POST", "/auth/logout", raw=b"", content_type="text/plain").status, 403)
        self.ok(self.server.client(app_header=False).post("/auth/login", {"username": "admin", "password": "admin-passwort-123"}))

    def test_query_token_not_in_log(self):
        tok = self.token("calendar")["token"]
        self.ok(self.server.client().get(f"/kalender.ics?token={tok}"))
        log = self.server.log()
        self.assertNotIn(tok, log)
        self.assertIn("token=[redacted]", log)

    def test_password_change_signs_out_other_devices(self):
        phone, _ = self.member("zweigeraete", "erstes-passwort")
        laptop = self.server.client()
        laptop.login("zweigeraete", "erstes-passwort")
        self.ok(phone.post("/api/me/password", {"old": "erstes-passwort", "new": "zweites-passwort"}))
        self.ok(phone.get("/api/me"))
        self.assertEqual(laptop.get("/api/me").status, 401)

    def test_password_change_needs_a_session(self):
        # an API token knowing the old password must not change its creator's password
        api = self.server.client(token=self.token("api")["token"])
        r = api.post("/api/me/password", {"old": ADMIN[1], "new": "vom-token-gesetzt-123"})
        self.assertEqual(r.status, 403)
        self.server.client().login(*ADMIN)  # the old password still works

    def test_unknown_user_login_is_refused_like_wrong_password(self):
        r = self.server.client().post("/auth/login", {"username": "gibtsnicht", "password": "irgendwas-langes"})
        self.assertEqual((r.status, r.json()["error"]), (403, "Benutzername oder Passwort falsch"))


class SlowClientTest(ServerTestCase):
    """A connection that sends nothing is dropped after KOMBUESE_SOCKET_TIMEOUT seconds (it held a thread forever)."""
    SETUP_ADMIN = False
    SERVER_ENV = {"KOMBUESE_SOCKET_TIMEOUT": "2"}

    def test_silent_connection_closed(self):
        t = time.time()
        answer = raw_request(self.server.port, b"", timeout=15)
        self.assertEqual(answer, b"")  # closed by the server, not by our timeout (None)
        self.assertLess(time.time() - t, 10)

    def test_slow_request_line_closed(self):
        answer = raw_request(self.server.port, b"GET /api/vers", timeout=15)
        self.assertEqual(answer, b"")
