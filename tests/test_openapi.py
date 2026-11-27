"""The API description (GET /api/openapi.json) and the docs page (/api/docs) stay honest.

* SpecDocumentTest: the document is valid enough for tools (OpenAPI 3.1 fields, unique operationIds, known tags,
  every $ref resolves, every path parameter declared once with an example), public, without data of the installation.
* SpecProbeTest: every operation in the document exists on a running server (the answer is never the "unknown route"
  answer 404 {"error": "unbekannt"} or a 405), and GET/POST on a documented path that the document does not list
  answer as unknown.
* RoutesInSourceTest – the one documented exception to "HTTP only": it reads app/server.py as text (no import) and
  checks that every route the server matches (p == "…", p in (…), re.fullmatch(r"…", p)) is in the document and every
  documented path fits a route of the server. Probing alone cannot find a route that nobody documented.
* DocsPageTest: Swagger UI is served by the app with a CSP that allows only its own files.
"""
import base64
import json
import os
import re
import secrets
import unittest

try:
    from .harness import ROOT, Server, ServerTestCase, free_port
except ImportError:
    from harness import ROOT, Server, ServerTestCase, free_port

# the app's own pages and static files: not part of the API, not in the document
STATIC = re.compile(r"/(|index\.html|login|setup|manifest\.json|sw\.js|icon\.svg|icon-\d+\.png|i18n\.js|zxing\.min\.js|zxing-LIZENZ\.txt)")
# routes the server answers by prefix (no == / fullmatch in the source): (path, the source text that answers it)
PREFIX_ROUTES = {"/auth/invite": 'p.startswith("/auth/invite")'}
TAGS = {"app", "account", "admin", "households", "recipes", "swipe cards", "week", "meal prep", "shopping", "pantry",
        "nutrition", "AI jobs", "connections", "backup", "push", "sharing", "images", "MCP"}


def b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def path_regex(path):
    """A documented path as a regex: every {param} one path segment (or part of one, e.g. {slug}.pdf)."""
    return re.compile("".join("[^/]+" if part.startswith("{") else re.escape(part) for part in re.split(r"(\{\w+\})", path)))


def fill(path, values):
    return re.sub(r"\{(\w+)\}", lambda m: str(values[m.group(1)]), path)


def shapes(rx):
    """Sample paths for a route regex of server.py: every optional group with and without, every alternative,
    one sample character per character class. Enough for the patterns of the routes (no lookarounds, no nesting tricks)."""
    pos = 0

    def sample_char(cls):
        return "a" if ("a-z" in cls or "\\w" in cls) else "1"

    def alternatives():
        nonlocal pos
        alts, cur = [], [""]
        while pos < len(rx) and rx[pos] != ")":
            c = rx[pos]
            if c == "|":
                alts += cur
                cur = [""]
                pos += 1
                continue
            if c == "(":
                pos += 1
                if rx.startswith("?:", pos):
                    pos += 2
                pieces = alternatives()
                pos += 1  # the closing ")"
            elif c == "\\":
                pieces = [{"d": "1", "w": "a"}.get(rx[pos + 1], rx[pos + 1])]
                pos += 2
            elif c == "[":
                end = rx.index("]", pos + 1)
                pieces = [sample_char(rx[pos + 1:end])]
                pos = end + 1
            else:
                pieces = [c]
                pos += 1
            if pos < len(rx) and rx[pos] in "?+*{":
                q = rx[pos]
                if q == "{":
                    end = rx.index("}", pos)
                    lo = int(rx[pos + 1:end].split(",")[0] or 0)
                    pieces = [p * max(lo, 1) for p in pieces]
                    pos = end + 1
                else:
                    pieces = pieces + [""] if q in "?*" else pieces
                    pos += 1
            cur = [a + p for a in cur for p in pieces]
        return alts + cur

    return alternatives()


def server_routes():
    """(literal paths, route regexes) that app/server.py matches the request path against."""
    with open(os.path.join(ROOT, "app", "server.py"), encoding="utf-8") as f:
        src = f.read()
    literals = set(re.findall(r'\bp == "(/[^"]*)"', src))
    for group in re.findall(r"\bp in \(([^)]*)\)", src):
        literals |= set(re.findall(r'"(/[^"]*)"', group))
    for path, code in PREFIX_ROUTES.items():
        if code in src:
            literals.add(path)
    regexes = set(re.findall(r're\.fullmatch\(r"(/[^"]+)", p\)', src))
    return literals, regexes


class SpecDocumentTest(ServerTestCase):
    SETUP_ADMIN = True

    def test_public_and_without_installation_data(self):
        r = self.server.client().get("/api/openapi.json")
        spec = self.ok(r)
        self.assertIn("json", r.header("content-type"))
        self.assertEqual(spec["info"]["version"], self.ok(self.server.client().get("/api/version"))["version"])
        text = r.text
        self.assertNotIn(self.admin.cookie[0], text)  # the cookie name of this installation
        self.assertNotIn(self.server.secret_key, text)
        self.assertNotIn("127.0.0.1", text)
        signed_in = self.ok(self.admin.get("/api/openapi.json"))
        self.assertEqual(spec, signed_in)

    def test_valid_document(self):
        spec = self.ok(self.server.client().get("/api/openapi.json"))
        self.assertRegex(spec["openapi"], r"^3\.1\.\d+$")
        for k in ("title", "version", "description", "license"):
            self.assertTrue(spec["info"].get(k), k)
        self.assertTrue(spec["paths"])
        self.assertEqual({t["name"] for t in spec["tags"]}, TAGS)
        schemes = spec["components"]["securitySchemes"]
        self.assertEqual(set(schemes), {"cookieAuth", "bearerAuth", "calendarToken", "pushSecret"})

        refs = set()

        def walk(x):
            if isinstance(x, dict):
                if isinstance(x.get("$ref"), str):
                    refs.add(x["$ref"])
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(spec)
        for r in refs:
            node = spec
            self.assertTrue(r.startswith("#/"), r)
            for part in r[2:].split("/"):
                self.assertIn(part, node, f"$ref {r} does not resolve")
                node = node[part]

        ids, access = set(), {"public", "member", "session", "admin", "instance", "calendar", "mcp", "push"}
        for path, ops in spec["paths"].items():
            self.assertTrue(path.startswith("/"), path)
            names = re.findall(r"\{(\w+)\}", path)
            for method, o in ops.items():
                where = f"{method.upper()} {path}"
                self.assertIn(method, ("get", "post"), where)
                self.assertTrue(o.get("summary"), where)
                self.assertNotIn(o["operationId"], ids, where)
                ids.add(o["operationId"])
                self.assertTrue(set(o["tags"]) <= TAGS, where)
                self.assertIn(o["x-kombuese-access"], access, where)
                self.assertIn("200", o["responses"], where)
                for code, resp in o["responses"].items():
                    self.assertRegex(code, r"^[1-5]\d\d$", where)
                    self.assertTrue("$ref" in resp or resp.get("description"), where)
                for req in o["security"]:
                    self.assertTrue(set(req) <= set(schemes), where)
                params = o.get("parameters", [])
                self.assertEqual(len(params), len({(p["name"], p["in"]) for p in params}), where)
                in_path = [p for p in params if p["in"] == "path"]
                self.assertEqual(sorted(p["name"] for p in in_path), sorted(names), where)
                for p in in_path:
                    self.assertTrue(p["required"], where)
                    self.assertIn("example", p, where)
                    if "pattern" in p["schema"]:
                        self.assertRegex(str(p["example"]), p["schema"]["pattern"], where)
                    if "enum" in p["schema"]:
                        self.assertIn(p["example"], p["schema"]["enum"], where)
                if o["x-kombuese-access"] != "public":
                    self.assertTrue(o["security"], where)
                    self.assertIn("401", o["responses"], where)


class SpecProbeTest(ServerTestCase):
    SERVER_ENV = {"KOMBUESE_ALLOW_PRIVATE_URLS": "true", "KOMBUESE_PUSH_CONTACT": "mailto:kitchen@example.org"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t = cls()
        cls.spec = t.ok(cls.server.client().get("/api/openapi.json"))
        cls.api = cls.server.client(token=t.token("api")["token"])
        slug = t.recipe("Probe Chili", portionen=2, naehrwerte={"kcal": 500})
        share = t.ok(cls.admin.post(f"/api/rezepte/{slug}/teilen"))["link"]
        card = t.ok(cls.admin.post("/api/cards", {"url": "https://example.org/probe", "title": "Probe"}))["added"][0]
        plan = t.ok(cls.admin.post("/api/plan", {"datum": "2026-12-01", "typ": "dinner", "slug": slug}))["id"]
        _, member = t.member()
        revoke = t.token("api", name="probe-revoke")["id"]
        invite = t.ok(cls.admin.post("/api/invites", {"role": "member"}))["id"]
        household = t.ok(cls.admin.post("/api/households", {"name": "Probe"}))["id"]
        t.ok(cls.admin.post("/api/schrank", {"name": "Reis", "menge": 1, "einheit": "kg"}))
        pantry = next(x["id"] for x in t.ok(cls.admin.get("/api/schrank"))["schrank"] if x["name"] == "Reis")
        freezer = t.ok(cls.admin.post("/api/vorrat", {"name": "Chili", "anzahl": 2}))[0]["id"]
        sub = {"endpoint": f"http://127.0.0.1:{free_port()}/sub/probe",
               "keys": {"p256dh": b64u(b"\x04" + secrets.token_bytes(64)), "auth": b64u(secrets.token_bytes(16))}}
        device = t.ok(cls.admin.post("/api/push/subscribe", sub))
        cls.push = (device["id"], device["secret"])
        examples = {p["name"]: p["example"] for ops in cls.spec["paths"].values() for o in ops.values()
                    for p in o.get("parameters", []) if p["in"] == "path"}
        cls.values = {**examples, "slug": slug, "token": share["token"], "share_id": share["id"], "card_id": card,
                      "plan_id": plan, "mealplan_id": plan, "user_id": member, "token_id": revoke, "invite_id": invite,
                      "household_id": household, "pantry_id": pantry, "freezer_id": freezer}
        t.ok(cls.admin.post("/api/barcode", {"code": examples["code"], "zutat": "Schokolade", "aktion": "merken"}))  # no lookup outside
        cls.query = {"/api/rezept": f"?slug={slug}", "/api/mahlzeit": f"?id={plan}", "/api/push/pending": f"?id={device['id']}",
                     "/api/scrape": "?url=http://127.0.0.1:9/"}
        # nothing in a test may reach real services: these examples would, so the probe sends addresses where nothing listens
        cls.bodies = {"/api/rezepte/{slug}/bild": {"url": "http://127.0.0.1:9/x.jpg"},
                      "/api/video/update": {},  # the example would install yt-dlp from PyPI
                      "/api/integrations/{name}": {"server": "http://127.0.0.1:9", "topic": "probe"},
                      "/api/push/subscribe": {**sub, "endpoint": f"http://127.0.0.1:{free_port()}/sub/probe2"}}

    @staticmethod
    def unknown(r):
        if r.status == 405:
            return True
        if r.status == 404 and "json" in (r.header("content-type") or ""):
            try:
                return r.json().get("error") == "unbekannt"
            except ValueError:
                return False
        return False

    def call(self, method, path, o):
        access = o["x-kombuese-access"]
        url = fill(path, self.values) + self.query.get(path, "")
        headers = {}
        if access == "push":
            client, headers["X-Push-Secret"] = self.server.client(), self.push[1]
        elif access == "public":
            client = self.server.client()
        elif access in ("member", "calendar", "mcp"):
            client = self.api
        else:
            client = self.admin
        if method == "get":
            return client.get(url, headers=headers)
        content = o.get("requestBody", {}).get("content", {})
        if path in self.bodies:
            return client.post(url, self.bodies[path], headers=headers)
        if "application/json" in content:
            return client.post(url, content["application/json"].get("example", {}), headers=headers)
        if content:
            return client.request("POST", url, raw=b"", content_type=next(iter(content)), headers=headers)
        return client.post(url, {}, headers=headers)

    def test_every_operation_exists(self):
        ops = [(m, p, o) for p, d in self.spec["paths"].items() for m, o in d.items() if not o.get("x-kombuese-requires")]
        self.assertGreater(len(ops), 150)
        failed = []
        for method in ("get", "post"):  # reads first, then the changes (revoking, deleting) at the end
            for m, path, o in ops:
                if m != method:
                    continue
                r = self.call(m, path, o)
                if self.unknown(r) or r.status == 500:
                    failed.append(f"{m.upper()} {path}: {r}")
        self.assertEqual(failed, [], f"\n--- server log (tail) ---\n{self.server.log()[-3000:]}")

    def test_undocumented_methods_are_unknown(self):
        answered = []
        for path, d in self.spec["paths"].items():
            for m in {"get", "post"} - set(d):
                url = fill(path, self.values)
                r = self.admin.get(url) if m == "get" else self.admin.post(url, {})
                if not self.unknown(r):
                    answered.append(f"{m.upper()} {path}: {r}")
        self.assertEqual(answered, [], "the server answers these, the document does not list them")

    def test_operations_that_need_oidc_answer_404_without_it(self):
        for path, d in self.spec["paths"].items():
            for m, o in d.items():
                if o.get("x-kombuese-requires") == "oidc":
                    self.assertTrue(self.unknown(self.server.client().get(fill(path, self.values))), path)


class RoutesInSourceTest(unittest.TestCase):
    """Reads app/server.py as text (see the module docstring), and the document through a server like the others."""

    @classmethod
    def setUpClass(cls):
        server = Server().start()
        cls.spec = server.client().get("/api/openapi.json").json()
        server.stop()
        cls.literals, cls.regexes = server_routes()
        cls.documented = [(p, path_regex(p)) for p in cls.spec["paths"]]

    def test_source_scan_finds_routes(self):  # guards the scanner itself
        self.assertIn("/api/rezepte", self.literals)
        self.assertIn("/kalender.ics", self.literals)
        self.assertIn(r"/api/rezepte/([a-z0-9-]{1,120})", self.regexes)
        self.assertGreater(len(self.literals) + len(self.regexes), 120)
        self.assertEqual(sorted(shapes(r"/api/households(?:/(\d+)(/invites)?)?")),
                         ["/api/households", "/api/households/1", "/api/households/1/invites"])

    def test_every_server_route_is_documented(self):
        missing = []
        samples = [(x, x) for x in self.literals] + [(rx, s) for rx in self.regexes for s in shapes(rx)]
        for route, sample in samples:
            if STATIC.fullmatch(sample):
                continue
            if not any(r.fullmatch(sample) for _, r in self.documented):
                missing.append(f"{route} (e.g. {sample})")
        self.assertEqual(sorted(set(missing)), [], "routes of server.py missing in app/openapi.py")

    def test_every_documented_path_is_a_server_route(self):
        values = {p["name"]: p["example"] for ops in self.spec["paths"].values() for o in ops.values()
                  for p in o.get("parameters", []) if p["in"] == "path"}
        compiled = [re.compile(rx) for rx in self.regexes]
        unknown = []
        for path in self.spec["paths"]:
            concrete = fill(path, values)
            if concrete not in self.literals and not any(rx.fullmatch(concrete) for rx in compiled):
                unknown.append(f"{path} (as {concrete})")
        self.assertEqual(unknown, [], "documented paths that no route of server.py matches")


class DocsPageTest(ServerTestCase):
    SETUP_ADMIN = False

    def test_page_and_files(self):
        c = self.server.client()
        r = c.get("/api/docs")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.header("content-type").startswith("text/html"))
        csp = r.header("content-security-policy")
        directives = {d.split()[0]: d.split()[1:] for d in csp.split(";") if d.strip()}
        self.assertEqual(directives["default-src"], ["'none'"])
        self.assertEqual(directives["script-src"], ["'self'"])  # no inline scripts, no eval, no CDN
        self.assertEqual(directives["connect-src"], ["'self'"])
        self.assertNotIn("'unsafe-inline'", directives.get("style-src", []))
        self.assertIn("frame-ancestors", directives)
        self.assertEqual(directives["base-uri"], ["'none'"])
        self.assertNotRegex(r.text, r"<script>|<script [^>]*>[^<]|https?://")  # only external scripts of this server
        for src in re.findall(r'(?:src|href)="(/[^"]+)"', r.text):
            self.assertEqual(c.get(src).status, 200, src)
        for name, ctype in (("swagger-ui-bundle.js", "application/javascript"), ("swagger-ui.css", "text/css"),
                            ("init.js", "application/javascript"), ("LICENSE", "text/plain"), ("NOTICE", "text/plain")):
            f = c.get(f"/api/docs/{name}")
            self.assertEqual(f.status, 200, name)
            self.assertTrue(f.header("content-type").startswith(ctype), name)
            self.assertEqual(f.header("x-content-type-options"), "nosniff")
        self.assertIn("Apache License", c.get("/api/docs/LICENSE").text)
        init = c.get("/api/docs/init.js").text
        self.assertIn("X-Kombuese", init)
        self.assertIn("validatorUrl: null", init)
        for bad in ("/api/docs/../server.py", "/api/docs/index.html", "/api/docs/nope.js", "/api/docs/%2e%2e%2fserver.py"):
            self.assertEqual(c.get(bad).status, 404, bad)

    def test_app_keeps_its_own_csp(self):
        r = self.server.client().get("/api/version")
        self.assertNotIn("script-src", r.header("content-security-policy"))


if __name__ == "__main__":
    unittest.main()
