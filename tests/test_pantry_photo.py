"""What can I cook with what is there (ranking without AI, AI job pantry) and recipe from a photo (AI job photo):
API, /api/work and MCP for an outside worker, and runs inside the app against a fake OpenAI-compatible server."""
import base64
import http.server
import json
import struct
import threading
import time
import zlib
from datetime import date, timedelta

try:
    from .harness import ServerTestCase
except ImportError:
    from harness import ServerTestCase


def png(w=2, h=2):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\x80\x40" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class FakeAI:
    """OpenAI-compatible /v1/chat/completions on 127.0.0.1. turns: list of functions(request body) → (status, JSON answer)."""

    def __init__(self):
        self.turns, self.requests = [], []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                fake.requests.append(body)
                fn = fake.turns.pop(0) if fake.turns else (lambda b: say("done"))
                status, answer = fn(body)
                data = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def say(text):
    return 200, {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def call(*calls):
    """A turn with tool calls: (name, arguments) pairs."""
    return 200, {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]},
        "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def api(method, path, body=None):
    return ("kombuese_api", {"method": method, "path": path, **({"body": body} if body is not None else {})})


def last_tool_results(body):
    return [m["content"] for m in body["messages"] if m.get("role") == "tool"]


class PantryPhotoTest(ServerTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ai = FakeAI()
        cls.addClassCleanup(cls.ai.close)
        cls.tok = cls.admin.post("/api/tokens", {"name": "worker", "scope": "api"}).json()["token"]
        cls.worker = cls.server.client(token=cls.tok)
        soon = str(date.today() + timedelta(days=5))
        cls.hummus = cls.admin.post("/api/rezepte", {"name": "Kichererbsen-Spinat-Pfanne", "portionen": 2, "zutaten_text":
                                                     "1 Dose Kichererbsen\n200 g Spinat\n1 Zitrone\nSalz\n2 EL Olivenöl\nWasser"}).json()["slug"]
        cls.pasta = cls.admin.post("/api/rezepte", {"name": "Tomatennudeln", "portionen": 2, "zutaten_text": "250 g Nudeln\n3 Tomaten"}).json()["slug"]
        cls.curry = cls.admin.post("/api/rezepte", {"name": "Kichererbsencurry", "portionen": 4, "zutaten_text":
                                                    "2 Dosen Kichererbsen\n400 ml Kokosmilch\n1 Zwiebel"}).json()["slug"]
        cls.admin.post("/api/schrank", {"name": "Kichererbsen", "menge": 1, "einheit": "Dose"})
        cls.admin.post("/api/schrank", {"name": "Milch", "menge": 1000, "einheit": "ml"})
        cls.admin.post("/api/vorrat", {"name": "Blattspinat (TK)", "anzahl": 1, "einheit": "Packung", "kategorie": "sonstiges",
                                       "haltbar_bis": soon})

    def use_ai(self):
        self.ok(self.admin.post("/api/ai", {"provider": "openai", "base_url": self.ai.url, "model": "fake-vision"}))
        self.addCleanup(lambda: self.admin.post("/api/ai", {"provider": "extern"}))

    def wait(self, path, key, states=("done", "failed"), timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            d = self.ok(self.admin.get(path))
            st = d[key] if key else d
            if st.get("state") in states:
                return d
            time.sleep(0.3)
        self.fail(f"{path} did not finish: {d}\n{self.server.log()[-3000:]}")

    # ---------- level 1: without AI ----------
    def test_cookable_ranking(self):
        d = self.ok(self.admin.get("/api/schrank/rezepte"))
        names = [r["slug"] for r in d["rezepte"]]
        self.assertEqual(names[0], self.hummus, d)
        self.assertNotIn(self.pasta, names)  # nothing of it is there
        top = d["rezepte"][0]
        self.assertEqual((top["treffer"], top["gesamt"]), (2, 3))  # salt, oil and water are not counted
        self.assertEqual([z["name"] for z in top["fehlt"]], ["Zitrone"])
        self.assertEqual(top["bald"], 1)  # the frozen spinach runs out soon
        self.assertEqual({z["quelle"] for z in top["da"]}, {"schrank", "vorrat"})
        self.assertIn("Blattspinat (TK)", [x["name"] for x in d["bald"]])
        curry = next(r for r in d["rezepte"] if r["slug"] == self.curry)
        self.assertEqual([z["name"] for z in curry["knapp"]], ["Kichererbsen"])  # 1 can there, 2 needed
        self.assertIn("Kokosmilch", [z["name"] for z in curry["fehlt"]])  # milk in the cupboard is no coconut milk
        self.assertEqual(self.admin.get("/api/schrank/rezepte?limit=x").status, 400)

    def test_missing_onto_shopping_list(self):
        r = self.ok(self.admin.post("/api/schrank/rezepte/einkauf", {"slug": self.hummus, "portionen": 4}))
        self.assertEqual(r["fehlt"], ["zitrone"])
        items = {i["name"]: i for i in self.ok(self.admin.get("/api/einkauf"))["items"]}
        self.assertIn("Zitrone", items)
        self.assertEqual(items["Zitrone"]["menge_zahl"], 2)  # doubled for 4 servings
        for n in ("Kichererbsen", "Spinat", "Salz", "Olivenöl"):
            self.assertNotIn(n, items)
        self.ok(self.admin.post("/api/einkauf/weg", {"ids": [items["Zitrone"]["id"]]}))
        self.assertEqual(self.admin.post("/api/schrank/rezepte/einkauf", {"slug": "gibtsnicht"}).status, 400)

    # ---------- outside worker: /api/work, image endpoint, MCP ----------
    def rpc(self, method, params):
        r = self.worker.post("/mcp", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        d = self.ok(r)
        self.assertNotIn("error", d, d)
        return d["result"]

    def test_worker_sees_jobs_and_images(self):
        self.ok(self.admin.post("/api/kochideen", {"wunsch": "etwas Schnelles"}))
        w = self.ok(self.worker.get("/api/work"))
        self.assertEqual((w["kochideen"]["state"], w["kochideen"]["wunsch"]), ("requested", "etwas Schnelles"))
        prompts = {p["name"] for p in self.rpc("prompts/list", {})["prompts"]}
        self.assertTrue({"pantry", "photo"} <= prompts)
        text = self.rpc("prompts/get", {"name": "pantry", "arguments": {"wish": "etwas Schnelles"}})["messages"][0]["content"]["text"]
        self.assertIn("/api/schrank/rezepte", text)
        self.assertIn("etwas Schnelles", text)
        self.ok(self.worker.post("/api/kochideen/state", {"state": "idle"}))

        img = png()
        b64 = base64.b64encode(img).decode()
        self.assertEqual(self.admin.post("/api/fotoimport", {"bilder": [{"daten": base64.b64encode(b"kein bild").decode()}]}).status, 400)
        self.assertEqual(self.admin.post("/api/fotoimport", {"bilder": [{"daten": b64}] * 7}).status, 400)
        self.assertEqual(self.admin.post("/api/fotoimport", {"bilder": []}).status, 400)
        st = self.ok(self.admin.post("/api/fotoimport", {"bilder": [{"daten": "data:image/png;base64," + b64}, {"daten": b64}], "hinweis": "Omas Rezept"}))
        self.assertEqual((st["state"], len(st["bilder"])), ("requested", 2))
        self.assertNotIn("bilder_key", st)
        self.assertEqual(self.admin.post("/api/fotoimport", {"bilder": [{"daten": b64}]}).status, 409)
        w = self.ok(self.worker.get("/api/work"))
        self.assertEqual((w["fotoimport"]["state"], w["fotoimport"]["hinweis"]), ("requested", "Omas Rezept"))
        self.assertNotIn("bilder_key", json.dumps(w))
        r = self.worker.get("/api/fotoimport/bild/1")
        self.assertEqual((r.status, r.header("content-type"), r.body), (200, "image/png", img))
        self.assertEqual(self.worker.get("/api/fotoimport/bild/3").status, 404)
        self.assertEqual(self.server.client().get("/api/fotoimport/bild/1").status, 401)
        res = self.rpc("tools/call", {"name": "read_photos", "arguments": {}})
        self.assertFalse(res["isError"], res)
        images = [c for c in res["content"] if c["type"] == "image"]
        self.assertEqual([(c["mimeType"], c["data"]) for c in images], [("image/png", b64)] * 2)
        text = self.rpc("prompts/get", {"name": "photo", "arguments": {"hint": "Omas Rezept"}})["messages"][0]["content"]["text"]
        self.assertIn("read_photos", text)
        self.assertIn("2 image(s)", text)
        # the worker creates the recipe, uses image 2 as the dish photo and reports done: images are deleted
        self.ok(self.worker.post("/api/fotoimport/state", {"state": "running"}))
        slug = self.ok(self.worker.post("/api/rezepte", {"name": "Omas Linsensuppe", "zutaten_text": "200 g Linsen"}))["slug"]
        self.ok(self.worker.post("/api/fotoimport/bild", {"slug": slug, "bild": 2}))
        self.assertEqual(self.worker.post("/api/fotoimport/bild", {"slug": slug, "bild": 9}).status, 404)
        self.ok(self.worker.post("/api/fotoimport/state", {"state": "done", "slug": slug, "name": "Omas Linsensuppe"}))
        d = self.ok(self.admin.get(f"/api/rezepte/{slug}"))
        self.assertIn("From photo", d["tags"])
        self.assertTrue(d["bild"])
        self.assertTrue(any(n["titel"] == "From photo" for n in d["notizen"]))
        self.assertEqual(self.worker.get("/api/fotoimport/bild/1").status, 404)
        res = self.worker.post("/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "read_photos", "arguments": {}}}).json()
        self.assertTrue(res["result"]["isError"])
        # a waiting import can be dropped
        self.ok(self.admin.post("/api/fotoimport", {"bilder": [{"daten": b64}]}))
        self.ok(self.admin.post("/api/fotoimport/abbrechen", {}))
        self.assertEqual(self.ok(self.admin.get("/api/fotoimport"))["state"], "idle")
        self.assertEqual(self.worker.get("/api/fotoimport/bild/1").status, 404)

    # ---------- runs inside the app (fake provider) ----------
    def test_pantry_job_in_app(self):
        self.use_ai()
        self.ai.turns = [
            lambda b: call(api("GET", "/api/schrank/rezepte?limit=40"), api("POST", "/api/einkauf/neu", {"text": "Butter"})),
            lambda b: call(api("POST", "/api/kochideen/vorschlag", {"zusammenfassung": "Weg mit dem Spinat", "ideen": [
                {"slug": self.hummus, "grund": "nutzt deine Kichererbsen und den TK-Spinat", "nutzt": ["Kichererbsen", "Spinat"], "fehlt": ["Zitrone"]},
                {"slug": "gibtsnicht", "grund": "x"}]})),
            lambda b: say("4 Ideen"),
        ]
        n = len(self.ai.requests)
        self.ok(self.admin.post("/api/kochideen", {"wunsch": "vegetarisch"}))
        d = self.wait("/api/kochideen", "status")
        self.assertEqual(d["status"]["state"], "done", d)
        v = d["vorschlag"]
        self.assertEqual([(i["slug"], i["rezept"]["name"]) for i in v["ideen"]], [(self.hummus, "Kichererbsen-Spinat-Pfanne")])
        self.assertEqual(len(v["fehler"]), 1)
        reqs = self.ai.requests[n:]
        self.assertIn("vegetarisch", reqs[0]["messages"][1]["content"])
        results = last_tool_results(reqs[1])
        self.assertIn(self.hummus, results[0])
        self.assertIn("not allowed", results[1])  # the pantry job may not touch the shopping list
        self.assertNotIn("Butter", [i["name"] for i in self.ok(self.admin.get("/api/einkauf"))["items"]])
        self.ok(self.admin.post("/api/kochideen/verwerfen", {}))
        self.assertIsNone(self.ok(self.admin.get("/api/kochideen"))["vorschlag"])

    def test_photo_job_in_app(self):
        self.use_ai()
        img = png(3, 3)
        b64 = base64.b64encode(img).decode()
        seen = {}

        def first(body):
            parts = body["messages"][1]["content"]
            seen["parts"] = parts
            return call(api("POST", "/api/rezepte", {"name": "Zettel-Pfannkuchen", "portionen": 2, "zutaten_text": "200 g Mehl\n2 Eier",
                                                     "naehrwerte": {"kcal": 450}, "notizen": [{"titel": "From photo", "text": "handwritten, estimated"}]}))
        self.ai.turns = [first, lambda b: call(api("POST", "/api/fotoimport/state", {"state": "done", "slug": "zettel-pfannkuchen", "name": "Zettel-Pfannkuchen"})),
                         lambda b: say("ok")]
        self.ok(self.admin.post("/api/fotoimport", {"bilder": [{"daten": b64}], "hinweis": "vom Zettel"}))
        st = self.wait("/api/fotoimport", None)
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(seen["parts"][0]["type"], "text")
        self.assertIn("attached to this message", seen["parts"][0]["text"])
        self.assertEqual(seen["parts"][1], {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}})
        d = self.ok(self.admin.get("/api/rezepte/zettel-pfannkuchen"))
        self.assertIn("From photo", d["tags"])
        self.assertEqual(sum(1 for n in d["notizen"] if n["titel"] == "From photo"), 1)  # the AI's note, not a second one
        self.assertFalse(d["bild"])  # a photo of a note is no recipe photo
        self.assertEqual(self.admin.get("/api/fotoimport/bild/1").status, 404)  # deleted after the job

    def test_photo_job_model_without_vision(self):
        self.use_ai()
        self.ai.turns = [lambda b: (400, {"error": {"message": "model does not support image input"}})]
        self.ok(self.admin.post("/api/fotoimport", {"bilder": [{"daten": base64.b64encode(png()).decode()}]}))
        st = self.wait("/api/fotoimport", None)
        self.assertEqual((st["state"], st.get("grund")), ("failed", "vision"), st)
        self.assertIn("cannot see images", st["note"])
        self.assertEqual(self.admin.get("/api/fotoimport/bild/1").status, 404)


if __name__ == "__main__":
    import unittest
    unittest.main()
