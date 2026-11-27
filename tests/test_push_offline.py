"""Web Push (subscribe, empty push with VAPID, pending, unsubscribe, gone devices) and the offline helpers of the
shopping list (client_id, service worker), over HTTP. A fake push service runs on 127.0.0.1, which the server may only
reach with KOMBUESE_ALLOW_PRIVATE_URLS=true. The signature itself is checked in test_vapid.py."""
import base64
import http.server
import json
import os
import secrets
import threading
import time

from harness import Client, ServerTestCase


def b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64u_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class FakePush:
    """Push service: records every request; /gone answers 410, /missing 404, /broken 500, everything else 201."""

    def __init__(self):
        self.hits = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                fake.hits.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": self.rfile.read(n)})
                code = 410 if self.path.startswith("/gone") else 404 if self.path.startswith("/missing") else 500 if self.path.startswith("/broken") else 201
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def to(self, path):
        return [h for h in self.hits if h["path"] == path]


def subscription(endpoint):
    return {"endpoint": endpoint, "expirationTime": None,
            "keys": {"p256dh": b64u(b"\x04" + secrets.token_bytes(64)), "auth": b64u(secrets.token_bytes(16))}}


class PushTest(ServerTestCase):
    SERVER_ENV = {"KOMBUESE_ALLOW_PRIVATE_URLS": "true", "KOMBUESE_PUSH_CONTACT": "mailto:kitchen@example.org"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.push = FakePush()
        cls.addClassCleanup(cls.push.close)

    def subscribe(self, client, path):
        d = self.ok(client.post("/api/push/subscribe", subscription(self.push.url + path)))
        self.assertTrue(d["ok"])
        self.assertGreaterEqual(len(d["secret"]), 32)
        return d

    def pending(self, sid, secret):
        return Client(self.server).get(f"/api/push/pending?id={sid}", headers={"X-Push-Secret": secret})

    def test_key(self):
        d = self.ok(self.admin.get("/api/push/key"))
        raw = b64u_decode(d["key"])
        self.assertEqual((len(raw), raw[0]), (65, 4))
        self.assertEqual(d["key"], self.ok(self.admin.get("/api/push/key"))["key"])  # created once
        self.assertIn("no-store", self.admin.get("/api/push/key").header("cache-control"))

    def test_notify_sends_empty_push_and_pending_holds_the_text(self):
        sub = self.subscribe(self.admin, "/sub/notify")
        key = self.ok(self.admin.get("/api/push/key"))["key"]
        self.assertEqual(sub["key"], key)
        self.assertIn(sub["id"], [x["id"] for x in self.ok(self.admin.get("/api/push/key"))["devices"]])
        r = self.ok(self.admin.post("/api/notify", {"text": "Der Wochenplan wartet", "title": "Kombüse", "view": "week"}))
        self.assertIn("webpush", r["sent"])
        hits = self.push.to("/sub/notify")
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertEqual(h["body"], b"")  # no payload
        self.assertEqual(h["headers"].get("ttl"), "86400")
        auth = h["headers"]["authorization"]
        self.assertTrue(auth.startswith("vapid t="), auth)
        token, k = auth[len("vapid t="):].split(", k=")
        self.assertEqual(k, key)
        head, claims, sig = token.split(".")
        self.assertEqual(json.loads(b64u_decode(head)), {"typ": "JWT", "alg": "ES256"})
        c = json.loads(b64u_decode(claims))
        self.assertEqual(c["aud"], self.push.url)
        self.assertEqual(c["sub"], "mailto:kitchen@example.org")
        self.assertTrue(time.time() < c["exp"] <= time.time() + 24 * 3600)
        self.assertEqual(len(b64u_decode(sig)), 64)
        # the service worker fetches the text without a session, with the device's secret, once
        p = self.pending(sub["id"], sub["secret"])
        items = self.ok(p)["items"]
        self.assertIn("no-store", p.header("cache-control"))
        self.assertEqual([(i["title"], i["text"], i["view"]) for i in items], [("Kombüse", "Der Wochenplan wartet", "week")])
        self.assertEqual(self.ok(self.pending(sub["id"], sub["secret"]))["items"], [])

    def test_pending_needs_the_secret(self):
        sub = self.subscribe(self.admin, "/sub/secret")
        self.assertEqual(self.pending(sub["id"], "falsch").status, 404)
        self.assertEqual(Client(self.server).get(f"/api/push/pending?id={sub['id']}").status, 404)
        self.assertEqual(self.pending(sub["id"] + 1000, sub["secret"]).status, 404)
        self.assertEqual(self.pending("abc", sub["secret"]).status, 404)
        self.ok(self.pending(sub["id"], sub["secret"]))

    def test_subscribing_again_takes_over_and_changes_the_secret(self):
        a = self.subscribe(self.admin, "/sub/again")
        b = self.subscribe(self.admin, "/sub/again")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(self.pending(a["id"], a["secret"]).status, 404)
        self.ok(self.pending(b["id"], b["secret"]))

    def test_unsubscribe(self):
        sub = self.subscribe(self.admin, "/sub/off")
        self.assertEqual(self.ok(self.admin.post("/api/push/subscribe", {"endpoint": self.push.url + "/sub/off", "delete": True}))["removed"], 1)
        self.assertNotIn(sub["id"], [x["id"] for x in self.ok(self.admin.get("/api/push/key"))["devices"]])
        self.assertEqual(self.pending(sub["id"], sub["secret"]).status, 404)
        sub = self.subscribe(self.admin, "/sub/off2")
        self.assertEqual(self.ok(self.admin.post("/api/push/unsubscribe", {"id": sub["id"]}))["removed"], 1)

    def test_gone_devices_are_removed(self):
        gone = self.subscribe(self.admin, "/gone/1")
        missing = self.subscribe(self.admin, "/missing/1")
        broken = self.subscribe(self.admin, "/broken/1")
        self.ok(self.admin.post("/api/notify", {"text": "Hallo"}))
        ids = [x["id"] for x in self.ok(self.admin.get("/api/push/key"))["devices"]]
        self.assertNotIn(gone["id"], ids)
        self.assertNotIn(missing["id"], ids)
        self.assertIn(broken["id"], ids)  # a server error is kept and shown
        dev = next(x for x in self.ok(self.admin.get("/api/push/key"))["devices"] if x["id"] == broken["id"])
        self.assertIn("HTTP 500", dev["last_error"])
        self.ok(self.admin.post("/api/push/unsubscribe", {"id": broken["id"]}))

    def test_test_message_to_one_device(self):
        sub = self.subscribe(self.admin, "/sub/test")
        other = self.subscribe(self.admin, "/sub/test-other")
        before = len(self.push.to("/sub/test-other"))
        r = self.ok(self.admin.post("/api/push/test", {"id": sub["id"], "lang": "en"}))
        self.assertEqual((r["devices"], r["sent"]), (1, 1))
        self.assertEqual(len(self.push.to("/sub/test-other")), before)
        self.assertEqual(self.ok(self.pending(sub["id"], sub["secret"]))["items"][-1]["text"], "Test message from Kombüse ✓")
        self.assertEqual(self.admin.post("/api/push/test", {"id": 999999}).status, 400)
        self.ok(self.admin.post("/api/push/unsubscribe", {"id": other["id"]}))

    def test_members_subscribe_their_own_devices(self):
        member, _ = self.member()
        sub = self.subscribe(member, "/sub/member")
        self.assertEqual([x["id"] for x in self.ok(member.get("/api/push/key"))["devices"]], [sub["id"]])
        self.assertNotIn(sub["id"], [x["id"] for x in self.ok(self.admin.get("/api/push/key"))["devices"]])
        # the admin cannot remove or test the member's device
        self.assertEqual(self.ok(self.admin.post("/api/push/unsubscribe", {"id": sub["id"]}))["removed"], 0)
        self.assertEqual(self.admin.post("/api/push/test", {"id": sub["id"]}).status, 400)
        # household notifications reach every member's device
        self.ok(self.admin.post("/api/notify", {"text": "Für alle"}))
        self.assertIn("Für alle", [i["text"] for i in self.ok(self.pending(sub["id"], sub["secret"]))["items"]])

    def test_tokens_and_bad_endpoints(self):
        tok = self.token()
        api = self.server.client(token=tok["token"])
        self.assertEqual(api.get("/api/push/key").status, 403)
        self.assertEqual(api.post("/api/push/subscribe", subscription(self.push.url + "/sub/token")).status, 403)
        for bad in ("ftp://example.org/x", "", "javascript:alert(1)", "https://" + "a" * 1100):
            self.assertEqual(self.admin.post("/api/push/subscribe", subscription(bad)).status, 400, bad)
        self.assertEqual(Client(self.server).post("/api/push/subscribe", subscription(self.push.url + "/x")).status, 401)

    def test_connections_overview_counts_devices(self):
        sub = self.subscribe(self.admin, "/sub/overview")
        w = self.ok(self.admin.get("/api/integrations"))["webpush"]
        self.assertTrue(w["configured"])
        self.assertGreaterEqual(w["devices"], 1)
        self.ok(self.admin.post("/api/push/unsubscribe", {"id": sub["id"]}))


class PushPrivateAddressTest(ServerTestCase):
    """Without KOMBUESE_ALLOW_PRIVATE_URLS push endpoints in the local network are refused (SSRF)."""

    def test_local_endpoint_refused(self):
        for url in ("https://127.0.0.1:9/push", "http://127.0.0.1:9/push", "https://localhost/push"):
            r = self.admin.post("/api/push/subscribe", subscription(url))
            self.assertEqual(r.status, 400, url)

    def test_notify_without_devices_has_no_webpush(self):
        r = self.ok(self.admin.post("/api/notify", {"text": "Hallo"}))
        self.assertNotIn("webpush", r["sent"])
        self.assertNotIn("webpush", r["errors"])


class OfflineShoppingTest(ServerTestCase):
    def items(self):
        return self.ok(self.admin.get("/api/einkauf"))["items"]

    def test_client_id_makes_a_repeat_harmless(self):
        cid = secrets.token_hex(8)
        a = self.ok(self.admin.post("/api/einkauf/neu", {"text": "2 l Hafermilch", "client_id": cid}))
        b = self.ok(self.admin.post("/api/einkauf/neu", {"text": "2 l Hafermilch", "client_id": cid}))
        self.assertEqual(a["id"], b["id"])
        self.assertTrue(b.get("repeat"))
        self.assertEqual(len([i for i in self.items() if i["name"] == "Hafermilch"]), 1)
        c = self.ok(self.admin.post("/api/einkauf/neu", {"text": "2 l Hafermilch", "client_id": secrets.token_hex(8)}))
        self.assertNotEqual(c["id"], a["id"])
        d = self.ok(self.admin.post("/api/einkauf/neu", {"text": "Senf"}))  # without client_id as before
        self.assertTrue(d["ok"])

    def test_tick_on_removed_item_reports_not_found(self):
        iid = self.ok(self.admin.post("/api/einkauf/neu", {"text": "Kapern"}))["id"]
        self.ok(self.admin.post("/api/einkauf/weg", {"ids": [iid]}))
        r = self.admin.post("/api/einkauf/check", {"id": str(iid), "checked": True})
        self.assertGreaterEqual(r.status, 400)
        self.assertIn("nicht gefunden", r.json()["error"])  # the app's queue drops it as done

    def test_check_is_idempotent(self):
        iid = str(self.ok(self.admin.post("/api/einkauf/neu", {"text": "Zitronen"}))["id"])
        for _ in range(2):
            self.ok(self.admin.post("/api/einkauf/check", {"id": iid, "checked": True}))
        self.assertTrue(next(i for i in self.items() if i["id"] == iid)["checked"])

    def test_service_worker_is_public_and_versioned(self):
        r = Client(self.server).get("/sw.js")
        self.assertEqual(r.status, 200)
        self.assertIn("javascript", r.header("content-type"))
        self.assertIn("no-cache", r.header("cache-control"))
        self.assertNotIn("__KOMBUESE_VERSION__", r.text)
        version = self.ok(Client(self.server).get("/api/version"))["version"]
        self.assertIn(f"const VERSION = '{version}'", r.text)
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "static", "sw.js"), encoding="utf-8") as f:
            self.assertIn("addEventListener('push'", f.read())
