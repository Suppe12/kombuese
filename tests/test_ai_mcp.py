"""AI settings, /api/work for an outside worker, SSRF protection of /api/scrape, the MCP server at /mcp."""
import json
import unittest

try:
    from .harness import FakeSite, ServerTestCase, free_port
except ImportError:
    from harness import FakeSite, ServerTestCase, free_port

SECRET = "sk-test-NICHT-ECHT-0123456789abcdef"
RECIPE_PAGE = """<html><head><title>Testrezept</title>
<script type="application/ld+json">{"@context": "https://schema.org", "@graph": [{"@type": "WebPage"},
 {"@type": "Recipe", "name": "Fake Lasagne", "recipeYield": "4 Portionen", "totalTime": "PT1H",
  "recipeIngredient": ["500 g Hackfleisch", "1 Dose Tomaten"],
  "recipeInstructions": [{"@type": "HowToStep", "text": "Alles schichten."}],
  "nutrition": {"@type": "NutritionInformation", "calories": "650 kcal"}}]}</script></head><body>Lasagne</body></html>"""


def dead_url():
    """An address on this machine where nothing listens: if a test ever sends something, it goes nowhere."""
    return f"http://127.0.0.1:{free_port()}/v1"


class AiSettingsTest(ServerTestCase):

    def tearDown(self):
        self.ok(self.admin.post("/api/ai", {"provider": "extern", "api_key_clear": True}))

    def test_key_is_never_sent_back(self):
        d = self.ok(self.admin.post("/api/ai", {"provider": "openai", "base_url": dead_url(), "model": "fake-model",
                                                "api_key": SECRET, "run_budget": 1.5, "max_turns": 9999}))
        s = d["settings"]
        self.assertTrue(s["has_key"])
        self.assertNotIn("api_key", s)
        self.assertEqual((s["provider"], s["model"], s["run_budget"], s["max_turns"]), ("openai", "fake-model", 1.5, 400))
        tok = self.token("api")["token"]
        m, _ = self.member()
        for c, path in ((self.admin, "/api/ai"), (self.admin, "/api/ai/status"), (m, "/api/ai/status"),
                        (self.server.client(token=tok), "/api/work"), (self.admin, "/api/integrations"),
                        (self.admin, "/api/config")):
            r = c.get(path)
            self.ok(r)
            self.assertNotIn(SECRET, r.text, path)
            self.assertNotIn(SECRET[:12], r.text, path)
        self.assertTrue(self.ok(self.admin.get("/api/ai"))["settings"]["has_key"])
        # saving without a key keeps it, api_key_clear removes it
        self.assertTrue(self.ok(self.admin.post("/api/ai", {"model": "other"}))["settings"]["has_key"])
        self.assertFalse(self.ok(self.admin.post("/api/ai", {"api_key_clear": True}))["settings"]["has_key"])
        self.assertNotIn(SECRET, self.server.log())

    def test_validation(self):
        r = self.admin.post("/api/ai", {"base_url": "ftp://example.org"})
        self.assertEqual(r.status, 400)
        self.assertEqual(self.admin.post("/api/ai", {"run_budget": "viel"}).status, 400)
        s = self.ok(self.admin.post("/api/ai", {"provider": "gibtsnicht"}))["settings"]
        self.assertEqual(s["provider"], "extern")

    def test_work_for_outside_worker(self):
        tok = self.token("api")["token"]
        worker = self.server.client(token=tok)
        w = self.ok(worker.get("/api/work"))
        self.assertNotIn("ai", w)
        self.assertEqual(w["refill"]["state"], "idle")
        self.assertEqual(w["stack"], 0)
        # a job requested in the app shows up for the worker
        self.assertEqual(self.ok(self.admin.post("/api/nachschub", {"wunsch": "etwas mit Linsen", "meal": "haupt"}))["state"], "requested")
        self.ok(self.admin.post("/api/wochenplan", {"wunsch": "leicht", "start": "2030-01-07"}))
        w = self.ok(worker.get("/api/work"))
        self.assertEqual((w["refill"]["state"], w["refill"]["wunsch"], w["refill"]["meal"]), ("requested", "etwas mit Linsen", "haupt"))
        self.assertEqual((w["wochenplan"]["state"], w["wochenplan"]["start"]), ("requested", "2030-01-07"))
        # the worker reports back
        self.ok(worker.post("/api/nachschub/state", {"state": "idle", "note": "fertig"}))
        self.ok(worker.post("/api/wochenplan/state", {"state": "idle"}))
        w = self.ok(worker.get("/api/work"))
        self.assertEqual((w["refill"]["state"], w["wochenplan"]["state"]), ("idle", "idle"))
        # swipe cards from the worker are counted
        self.ok(worker.post("/api/cards", [{"url": "https://example.org/r/1", "title": "Karte 1"},
                                           {"url": "https://example.org/r/2", "title": "Karte 2", "meal": "fruehstueck"}]))
        self.assertEqual(self.ok(worker.get("/api/work"))["stack"], 2)

    def test_active_provider_makes_work_idle(self):
        # an OpenAI-compatible provider pointing to a dead local port: active, but nothing is requested
        self.ok(self.admin.post("/api/ai", {"provider": "openai", "base_url": dead_url(), "model": "fake-model"}))
        tok = self.token("api")["token"]
        w = self.ok(self.server.client(token=tok).get("/api/work"))
        self.assertEqual(w["ai"], "app")
        for k in ("refill", "wochenplan", "linkimport", "mealprep", "kochideen", "fotoimport", "aufwaermen"):
            self.assertEqual(w[k], {"state": "idle"}, k)
        self.assertEqual(w["likes_pending"], 0)
        self.assertEqual(self.ok(self.admin.get("/api/ai/status"))["provider"], "openai")
        # "off" is not active either: the outside worker sees the jobs again
        self.ok(self.admin.post("/api/ai", {"provider": "off"}))
        self.assertNotIn("ai", self.ok(self.server.client(token=tok).get("/api/work")))


class ScrapeTest(ServerTestCase):
    """Recipe import must not reach into the local network (SSRF)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.site = FakeSite({"/rezept": ("text/html; charset=utf-8", RECIPE_PAGE)})
        cls.addClassCleanup(cls.site.close)

    def test_local_addresses_refused(self):
        hits = len(self.site.hits)
        for url in (f"{self.site.url}/rezept", f"http://localhost:{self.site.port}/rezept", "http://127.0.0.1/",
                    "http://[::1]/", "http://169.254.169.254/latest/meta-data/", "http://10.0.0.1/", "http://192.168.2.1/",
                    "http://0.0.0.0/", f"http://127.1:{self.site.port}/rezept", "http://2130706433/"):
            r = self.admin.get("/api/scrape?url=" + url)
            self.assertEqual(r.status, 422, url)
            self.assertIn("error", r.json())
        self.assertEqual(len(self.site.hits), hits, "the fake site must not have been contacted")
        for url in ("file:///etc/passwd", "ftp://example.org/x", "javascript:alert(1)", ""):
            self.assertEqual(self.admin.get("/api/scrape?url=" + url).status, 422, url)

    def test_raw_mode_and_images_refused_too(self):
        hits = len(self.site.hits)
        self.assertEqual(self.admin.get(f"/api/scrape?roh=1&url={self.site.url}/rezept").status, 422)
        slug = self.recipe("Bild von lokal")
        r = self.admin.post(f"/api/rezepte/{slug}/bild", {"url": f"{self.site.url}/bild.png"})
        self.assertEqual(r.status, 400)
        self.assertEqual(len(self.site.hits), hits)

    def test_card_image_from_local_address_not_fetched(self):
        hits = len(self.site.hits)
        d = self.ok(self.admin.post("/api/cards", {"url": "https://example.org/karte", "title": "Karte",
                                                   "image_url": f"{self.site.url}/bild.jpg"}))
        self.assertEqual(len(d["added"]), 1)
        self.assertEqual(len(self.site.hits), hits)
        card = next(c for c in self.ok(self.admin.get("/api/stack")) if c["id"] == d["added"][0])
        self.assertEqual(card["image"], "")


class ScrapePrivateAllowedTest(ServerTestCase):
    """With KOMBUESE_ALLOW_PRIVATE_URLS=true the same local page is read (shows the refusal above is the SSRF check)."""

    SERVER_ENV = {"KOMBUESE_ALLOW_PRIVATE_URLS": "true"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.site = FakeSite({"/rezept": ("text/html; charset=utf-8", RECIPE_PAGE)})
        cls.addClassCleanup(cls.site.close)

    def test_reads_local_recipe(self):
        d = self.ok(self.admin.get(f"/api/scrape?url={self.site.url}/rezept"))
        self.assertEqual(d["name"], "Fake Lasagne")
        self.assertEqual(d["portionen"], 4)
        self.assertEqual(d["zutaten_roh"], ["500 g Hackfleisch", "1 Dose Tomaten"])
        self.assertEqual(d["kcal_pro_portion"], 650)
        self.assertEqual(d["zeiten"]["gesamt"], "1 h")
        self.assertIn("/rezept", self.site.hits)
        r = self.admin.get(f"/api/scrape?url={self.site.url}/gibtsnicht")
        self.assertEqual(r.status, 422)


class McpTest(ServerTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tok = cls.admin.post("/api/tokens", {"name": "mcp", "scope": "api"}).json()["token"]
        cls.n = 0

    def rpc(self, method, params=None, client=None, headers=None, path="/mcp"):
        McpTest.n += 1
        c = client or self.server.client(token=self.tok)
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            msg["params"] = params
        return c.post(path, msg, headers=headers)

    def result(self, method, params=None, **kw):
        r = self.rpc(method, params, **kw)
        d = self.ok(r)
        self.assertNotIn("error", d, d)
        return d["result"]

    def test_needs_api_token(self):
        for c in (self.server.client(), self.admin, self.server.client(token=self.token("calendar")["token"]),
                  self.server.client(token="kmb_falsch")):
            r = self.rpc("tools/list", {}, client=c)
            self.assertEqual(r.status, 401)
            self.assertIn("Bearer", r.header("www-authenticate"))
        self.assertEqual(self.server.client(token=self.tok).get("/mcp").status, 405)
        self.assertEqual(self.server.client(token=self.tok).request("DELETE", "/mcp").status, 405)

    def test_initialize_and_lists(self):
        res = self.result("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "test", "version": "1"}})
        self.assertEqual(res["protocolVersion"], "2025-06-18")
        self.assertEqual(res["serverInfo"]["name"], "kombuese")
        self.assertIn("tools", res["capabilities"])
        self.assertEqual(self.result("initialize", {"protocolVersion": "1999-01-01"})["protocolVersion"], "2025-11-25")
        r = self.server.client(token=self.tok).post("/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(r.status, 202)
        tools = {t["name"]: t for t in self.result("tools/list", {})["tools"]}
        self.assertTrue({"kombuese_api", "notify", "read_recipe", "add_card"} <= set(tools), tools.keys())
        self.assertNotIn("web_fetch", tools)
        self.assertIn("inputSchema", tools["kombuese_api"])
        self.assertIn("web_fetch", {t["name"] for t in self.result("tools/list", {}, path="/mcp?web=1")["tools"]})
        prompts = {p["name"]: p for p in self.result("prompts/list", {})["prompts"]}
        self.assertEqual(set(prompts), {"refill", "import", "link", "week", "swap", "prep", "pantry", "photo", "reheat"})
        self.assertTrue(any(a["name"] == "url" and a["required"] for a in prompts["link"]["arguments"]))
        self.assertEqual(self.result("ping", {}), {})

    def test_prompts_get(self):
        res = self.result("prompts/get", {"name": "week", "arguments": {"start": "2030-01-07", "wish": "viel Gemüse"}})
        text = res["messages"][0]["content"]["text"]
        self.assertEqual(res["messages"][0]["role"], "user")
        self.assertIn("2030-01-07", text)
        self.assertIn("viel Gemüse", text)
        for params in ({"name": "link", "arguments": {}}, {"name": "week", "arguments": {"start": "morgen"}},
                       {"name": "gibtsnicht", "arguments": {}}, {"name": "link", "arguments": {"url": "javascript:x"}}):
            d = self.ok(self.rpc("prompts/get", params))
            self.assertEqual(d["error"]["code"], -32602, params)

    def test_tool_call_allowed_path(self):
        self.recipe("Über MCP gelesen")
        res = self.result("tools/call", {"name": "kombuese_api", "arguments": {"method": "GET", "path": "/api/rezepte?suche=mcp"}})
        self.assertFalse(res["isError"], res)
        self.assertEqual([r["name"] for r in json.loads(res["content"][0]["text"])], ["Über MCP gelesen"])
        res = self.result("tools/call", {"name": "kombuese_api", "arguments": {
            "method": "POST", "path": "/api/rezepte", "body": {"name": "Von MCP angelegt", "zutaten_text": "1 cup rice"}}})
        self.assertFalse(res["isError"], res)
        d = self.ok(self.admin.get("/api/rezepte/von-mcp-angelegt"))
        self.assertEqual(d["zutaten"][0]["einheit"], "Tasse")

    def test_tool_call_forbidden_path(self):
        for method, path in (("GET", "/api/users"), ("GET", "/api/tokens"), ("POST", "/api/ai"), ("GET", "/api/backup"),
                             ("GET", "/api/rezepte/../users"), ("GET", "/api/einkauf"), ("POST", "/api/household"),
                             ("DELETE", "/api/rezepte")):
            res = self.result("tools/call", {"name": "kombuese_api", "arguments": {"method": method, "path": path, "body": {}}})
            self.assertTrue(res["isError"], (method, path, res))
            self.assertIn("not allowed", res["content"][0]["text"])
        self.assertEqual(self.ok(self.admin.get("/api/household"))["language"], "en")
        d = self.ok(self.rpc("tools/call", {"name": "gibtsnicht", "arguments": {}}))
        self.assertEqual(d["error"]["code"], -32602)

    def test_tool_read_recipe_respects_ssrf(self):
        res = self.result("tools/call", {"name": "read_recipe", "arguments": {"url": f"http://127.0.0.1:{self.server.port}/api/version"}})
        self.assertTrue(res["isError"])

    def test_protocol_errors(self):
        c = self.server.client(token=self.tok)
        self.assertEqual(c.post("/mcp", raw=b"{kein json", content_type="application/json").status, 400)
        self.assertEqual(c.post("/mcp", [{"jsonrpc": "2.0", "id": 1, "method": "ping"}]).status, 400)
        self.assertEqual(c.post("/mcp", {"id": 1, "method": "ping"}).status, 400)
        d = self.ok(self.rpc("gibt/es/nicht", {}))
        self.assertEqual(d["error"]["code"], -32601)
        r = self.rpc("tools/list", {}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        self.result("tools/list", {}, headers={"Origin": f"http://127.0.0.1:{self.server.port}"})
        r = self.rpc("tools/list", {}, headers={"MCP-Protocol-Version": "1999-01-01"})
        self.assertEqual(r.status, 400)

    def test_stateless_protocol(self):
        v = "2026-07-28"
        meta = {"_meta": {"io.modelcontextprotocol/protocolVersion": v}}
        res = self.result("server/discover", meta, headers={"MCP-Protocol-Version": v, "Mcp-Method": "server/discover"})
        self.assertIn(v, res["supportedVersions"])
        self.assertEqual(res["resultType"], "complete")
        r = self.rpc("tools/list", meta, headers={"MCP-Protocol-Version": v})  # Mcp-Method header missing
        self.assertEqual(r.status, 400)
        self.assertEqual(r.json()["error"]["code"], -32020)


if __name__ == "__main__":
    unittest.main()
