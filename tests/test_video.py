"""Recipes from cooking videos (app/video.py, docs/video.md): settings, Whisper test, the tool read_video over MCP and in
an AI run of the link import, error paths. No internet: yt-dlp and ffmpeg are small stub scripts (KOMBUESE_YTDLP,
KOMBUESE_FFMPEG), Whisper, the thumbnail and the AI are fake servers on 127.0.0.1. With a real ffmpeg on PATH one more
class runs ffmpeg itself on a generated test video."""
import http.server
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

try:
    from .harness import ServerTestCase, free_port
    from .test_pantry_photo import FakeAI, api, call, say
except ImportError:
    from harness import ServerTestCase, free_port
    from test_pantry_photo import FakeAI, api, call, say

SECRET = "whisper-key-NICHT-ECHT-0123456789"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64 + b"\xff\xd9"
TRANSCRIPT = "Heute machen wir Ofen-Feta-Pasta mit 400 g Nudeln und 200 g Feta."

YTDLP = r'''
import json, os, sys, urllib.parse
args = sys.argv[1:]
with open(os.environ["VIDEO_STUB_LOG"], "a") as f:
    f.write(json.dumps(["yt-dlp"] + args) + "\n")
if "--version" in args:
    print("2099.01.01-stub"); sys.exit(0)
if "-J" in args:
    url = args[-1]
    u = urllib.parse.urlsplit(url)
    if u.path.endswith("/private"):
        sys.stderr.write("ERROR: [Instagram] DAbc: Requested content is not available, rate-limit reached or login required\n"); sys.exit(1)
    if "/v/" not in u.path:
        sys.stderr.write("ERROR: Unsupported URL: " + url + "\n"); sys.exit(1)
    base = u.scheme + "://" + u.netloc
    print(json.dumps({"id": "abc", "title": "Ofen-Feta-Pasta in 30 Sekunden", "uploader": "kochkanal",
                      "uploader_url": base + "/@kochkanal", "webpage_url": url, "extractor_key": "TikTok",
                      "duration": 1500 if u.path.endswith("/long") else (6 if os.environ.get("VIDEO_STUB_MP4") else 30), "description": "Zutaten: 200 g Feta, 400 g Nudeln, Tomaten #feta",
                      "thumbnail": base + "/thumb.jpg", "url": base + "/file.mp4", "ext": "mp4", "filesize": 1000}))
    sys.exit(0)
if "--load-info-json" in args:
    out = args[args.index("-o") + 1].replace("%(ext)s", "mp4")
    real = os.environ.get("VIDEO_STUB_MP4")
    data = open(real, "rb").read() if real else b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100
    open(out, "wb").write(data)
    sys.exit(0)
sys.exit(2)
'''

FFMPEG = r'''
import json, os, sys, wave
args = sys.argv[1:]
with open(os.environ["VIDEO_STUB_LOG"], "a") as f:
    f.write(json.dumps(["ffmpeg"] + args) + "\n")
if "-version" in args:
    print("ffmpeg version 9.9-stub"); sys.exit(0)
out = args[-1]
if out.endswith(".mp3"):
    open(out, "wb").write(b"ID3" + b"\x00" * 2000)
elif out.endswith(".wav"):
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\x00\x00" * 16000)
elif "%02d" in out:
    for i in range(1, 6):
        open(out.replace("%02d", "%02d" % i), "wb").write(b"\xff\xd8\xff\xe0" + bytes([i]) * 50 + b"\xff\xd9")
sys.exit(0)
'''


class FakeWhisper:
    """Fake Whisper and thumbnail host on 127.0.0.1. kind "asr" (whisper-asr-webservice) or "openai"."""

    def __init__(self, kind="asr"):
        self.kind, self.requests = kind, []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def answer(self, status, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                if self.path == "/openapi.json":
                    return self.answer(200, {"paths": {"/asr": {}, "/detect-language": {}} if fake.kind == "asr" else {"/v1/audio/transcriptions": {}}})
                if self.path == "/thumb.jpg":
                    return self.answer(200, JPEG, "image/jpeg")
                self.answer(404, {"detail": "Not Found"})

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                fake.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "ctype": self.headers.get("Content-Type"), "body": body})
                if (fake.kind == "asr" and self.path.startswith("/asr?")) or (fake.kind == "openai" and self.path == "/v1/audio/transcriptions"):
                    return self.answer(200, {"text": TRANSCRIPT, "language": "de"})
                self.answer(404, {"detail": "Not Found"})

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class VideoBase(ServerTestCase):
    PRIVATE = True
    REAL_FFMPEG = False

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="kombuese-vstub-")
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.log_file = os.path.join(cls.tmp, "calls.log")
        open(cls.log_file, "w").close()
        for name, code in (("yt_dlp_stub.py", YTDLP), ("ffmpeg_stub.py", FFMPEG)):
            with open(os.path.join(cls.tmp, name), "w") as f:
                f.write(code)
        cls.whisper = FakeWhisper()
        cls.addClassCleanup(cls.whisper.close)
        py = shlex.quote(os.environ.get("KOMBUESE_TEST_PYTHON") or sys.executable)
        cls.SERVER_ENV = {"KOMBUESE_YTDLP": f"{py} {shlex.quote(os.path.join(cls.tmp, 'yt_dlp_stub.py'))}",
                          "VIDEO_STUB_LOG": cls.log_file, "KOMBUESE_WHISPER_URL": cls.whisper.url}
        if not cls.REAL_FFMPEG:
            cls.SERVER_ENV["KOMBUESE_FFMPEG"] = f"{py} {shlex.quote(os.path.join(cls.tmp, 'ffmpeg_stub.py'))}"
        if cls.PRIVATE:
            cls.SERVER_ENV["KOMBUESE_ALLOW_PRIVATE_URLS"] = "true"
        super().setUpClass()
        cls.tok = cls.admin.post("/api/tokens", {"name": "mcp", "scope": "api"}).json()["token"]

    def calls(self):
        with open(self.log_file) as f:
            return [json.loads(x) for x in f if x.strip()]

    def mcp_tool(self, name, arguments):
        r = self.server.client(token=self.tok).post("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                            "params": {"name": name, "arguments": arguments}})
        return self.ok(r)["result"]

    def read_video(self, path, **kw):
        return self.mcp_tool("read_video", {"url": self.whisper.url + path, **kw})


class VideoTest(VideoBase):

    def tearDown(self):
        self.ok(self.admin.post("/api/video", {"enabled": True, "whisper_url": "", "whisper_api": "auto", "whisper_key_clear": True, "language": "auto",
                                              "max_minutes": 10, "max_mb": 200}))

    def test_settings_key_never_sent_back_and_test_button(self):
        d = self.ok(self.admin.get("/api/video"))
        s = d["settings"]
        self.assertTrue(s["enabled"])  # on by default
        self.assertEqual((s["whisper_url"], s["url_from_env"], s["max_minutes"], s["max_mb"]), (self.whisper.url, True, 10, 200))
        self.assertEqual(d["status"]["ytdlp"], "2099.01.01-stub")
        self.assertEqual(d["status"]["ffmpeg"], "9.9-stub")
        s = self.ok(self.admin.post("/api/video", {"whisper_url": self.whisper.url + "/", "whisper_key": SECRET, "language": "de",
                                                   "max_minutes": 99}))["settings"]
        self.assertEqual((s["has_key"], s["whisper_url"], s["language"], s["max_minutes"]), (True, self.whisper.url, "de", 60))
        self.assertNotIn("whisper_key", s)
        m, _ = self.member()
        self.assertEqual(m.get("/api/video").status, 403)
        self.assertEqual(m.post("/api/video", {"enabled": False}).status, 403)
        self.assertEqual(self.server.client(token=self.tok).get("/api/video").status, 403)
        for path in ("/api/video", "/api/ai", "/api/integrations", "/api/config"):
            r = self.admin.get(path)
            self.assertEqual(r.status, 200, path)
            self.assertNotIn(SECRET, r.text, path)
        self.assertEqual(self.admin.post("/api/video", {"whisper_url": "ftp://whisper"}).status, 400)
        self.assertEqual(self.admin.post("/api/video", {"max_mb": "viel"}).status, 400)
        n = len(self.whisper.requests)
        r = self.ok(self.admin.post("/api/video/test", {}))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["api"], "asr")  # detected from /openapi.json
        req = self.whisper.requests[n]
        self.assertTrue(req["path"].startswith("/asr?"), req["path"])
        self.assertIn("language=de", req["path"])
        self.assertEqual(req["auth"], "Bearer " + SECRET)
        self.assertIn(b'name="audio_file"', req["body"])
        self.assertIn(b"RIFF", req["body"])  # the built-in test sound is a WAV
        self.assertEqual(self.ok(self.admin.get("/api/video"))["status"]["whisper_api"], "asr")
        self.assertNotIn(SECRET, self.server.log())
        self.assertEqual(self.admin.post("/api/video/update", {}).status, 400)  # never by accident
        self.assertFalse(self.ok(self.admin.post("/api/video", {"whisper_key_clear": True}))["settings"]["has_key"])

    def test_openai_compatible_whisper(self):
        fake = FakeWhisper("openai")
        self.addCleanup(fake.close)
        self.ok(self.admin.post("/api/video", {"whisper_url": fake.url + "/v1", "whisper_api": "auto", "whisper_model": "Systran/faster-whisper-small"}))
        r = self.ok(self.admin.post("/api/video/test", {}))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["api"], "openai")
        req = fake.requests[-1]
        self.assertEqual(req["path"], "/v1/audio/transcriptions")
        self.assertIn(b'name="model"\r\n\r\nSystran/faster-whisper-small', req["body"])
        self.assertIn(b'name="file"; filename=', req["body"])

    def test_read_video_over_mcp(self):
        tools = {t["name"] for t in self.ok(self.server.client(token=self.tok).post("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))["result"]["tools"]}
        self.assertIn("read_video", tools)
        n = len(self.calls())
        res = self.read_video("/v/ok")
        self.assertFalse(res["isError"], res)
        text = res["content"][0]["text"]
        for part in ("Ofen-Feta-Pasta in 30 Sekunden", "kochkanal", "Zutaten: 200 g Feta", TRANSCRIPT, "language de",
                     self.whisper.url + "/v/ok", "POST /api/videoimport/bild", "bild 0 = the video's cover image"):
            self.assertIn(part, text)
        images = [b for b in res["content"] if b["type"] == "image"]
        self.assertEqual(len(images), 5)
        self.assertEqual(images[0]["mimeType"], "image/jpeg")
        calls = self.calls()[n:]
        yt = [c for c in calls if c[0] == "yt-dlp"]
        self.assertIn("-J", yt[0])
        self.assertIn("default,-generic", yt[0])  # never the generic extractor that reads any page
        self.assertIn("--load-info-json", yt[1])
        audio = next(c for c in calls if c[0] == "ffmpeg" and c[-1].endswith(".mp3"))
        self.assertEqual(audio[audio.index("-ac") + 1:audio.index("-ac") + 4], ["1", "-ar", "16000"])
        frames = next(c for c in calls if c[0] == "ffmpeg" and "%02d" in c[-1])
        self.assertEqual(frames[frames.index("-frames:v") + 1], "12")
        self.assertFalse(any(n.startswith("kombuese-video-") for n in os.listdir(tempfile.gettempdir())
                             if os.path.getmtime(os.path.join(tempfile.gettempdir(), n)) > time.time() - 60), "temp files left")
        # a second call within half an hour comes from the cache, without downloading again
        n = len(self.calls())
        self.assertFalse(self.read_video("/v/ok", frames=False)["isError"])
        self.assertEqual(self.calls()[n:], [])
        # the still as the recipe photo, with the key from the answer
        key = re.search(r'"video": "([0-9a-f]{16})"', text).group(1)
        slug = self.recipe("Feta Pasta aus dem Video")
        self.assertEqual(self.ok(self.server.client(token=self.tok).post("/api/videoimport/bild", {"slug": slug, "video": key, "bild": 2}))["bild"], "jpg")
        self.assertEqual(self.server.client(token=self.tok).post("/api/videoimport/bild", {"slug": slug, "video": key, "bild": 9}).status, 404)
        self.assertEqual(self.admin.post("/api/videoimport/bild", {"slug": slug, "video": "../../etc", "bild": 0}).status, 404)
        self.ok(self.admin.post("/api/videoimport/bild", {"slug": slug, "video": key, "bild": 0}))  # the thumbnail

    def test_whisper_down_still_gives_description_and_stills(self):
        self.ok(self.admin.post("/api/video", {"whisper_url": f"http://127.0.0.1:{free_port()}"}))
        res = self.read_video("/v/down")
        self.assertFalse(res["isError"], res)
        text = res["content"][0]["text"]
        self.assertIn("Whisper not reachable", text)
        self.assertIn("Zutaten: 200 g Feta", text)
        self.assertIn("Transcript: not available", text)
        self.assertEqual(sum(1 for b in res["content"] if b["type"] == "image"), 5)

    def test_too_long_is_not_downloaded(self):
        n = len(self.calls())
        res = self.read_video("/v/long")
        self.assertFalse(res["isError"], res)
        self.assertIn("not downloaded: the video is 25 min long, the limit is 10 min", res["content"][0]["text"])
        self.assertIn("Zutaten: 200 g Feta", res["content"][0]["text"])
        self.assertFalse(any("--load-info-json" in c for c in self.calls()[n:]))

    def test_login_wall_and_unsupported(self):
        res = self.read_video("/v/private")
        self.assertTrue(res["isError"])
        self.assertIn("login", res["content"][0]["text"])
        res = self.read_video("/irgendwas")
        self.assertTrue(res["isError"])
        self.assertIn("not a video", res["content"][0]["text"])

    def test_switched_off(self):
        self.ok(self.admin.post("/api/video", {"enabled": False}))
        c = self.server.client(token=self.tok)
        tools = {t["name"] for t in self.ok(c.post("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))["result"]["tools"]}
        self.assertNotIn("read_video", tools)
        text = self.ok(c.post("/mcp", {"jsonrpc": "2.0", "id": 2, "method": "prompts/get", "params": {
            "name": "link", "arguments": {"url": "https://www.tiktok.com/@x/video/1"}}}))["result"]["messages"][0]["content"]["text"]
        self.assertIn("video import is switched off", text)
        self.ok(self.admin.post("/api/video", {"enabled": True}))
        text = self.ok(c.post("/mcp", {"jsonrpc": "2.0", "id": 3, "method": "prompts/get", "params": {
            "name": "link", "arguments": {"url": "https://www.tiktok.com/@x/video/1"}}}))["result"]["messages"][0]["content"]["text"]
        self.assertIn("call read_video(url)", text)
        self.assertIn('"From video"', text)


class VideoLinkImportTest(VideoBase):
    """The link import inside the app with a fake OpenAI-compatible AI: read_video, recipe, still as photo, state."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ai = FakeAI()
        cls.addClassCleanup(cls.ai.close)

    def wait_link(self, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.ok(self.admin.get("/api/status"))["linkimport"]
            if st.get("state") in ("done", "failed"):
                return st
            time.sleep(0.3)
        self.fail(f"link import did not finish\n{self.server.log()[-3000:]}")

    def test_link_import_end_to_end(self):
        self.ok(self.admin.post("/api/ai", {"provider": "openai", "base_url": self.ai.url, "model": "fake-vision"}))
        self.addCleanup(lambda: self.admin.post("/api/ai", {"provider": "extern"}))
        url = self.whisper.url + "/v/feta"
        seen = {}

        def after_video(body):
            msgs = body["messages"]
            seen["tool"] = next(m["content"] for m in msgs if m.get("role") == "tool")
            seen["images"] = [p for p in msgs[-1]["content"] if p.get("type") == "image_url"] if isinstance(msgs[-1]["content"], list) else []
            seen["key"] = re.search(r'"video": "([0-9a-f]{16})"', seen["tool"]).group(1)
            return call(api("POST", "/api/rezepte", {"name": "Ofen-Feta-Pasta", "portionen": 2, "quelle": url, "tags": ["From video"],
                                                     "zutaten_text": "400 g Nudeln\n200 g Feta", "naehrwerte": {"kcal": 700},
                                                     "notizen": [{"titel": "From video", "text": "by kochkanal, amounts guessed"}]}))
        self.ai.turns = [
            lambda b: call(("read_video", {"url": url})),
            after_video,
            lambda b: call(api("POST", "/api/videoimport/bild", {"slug": "ofen-feta-pasta", "video": seen["key"], "bild": 3})),
            lambda b: call(api("POST", "/api/linkimport/state", {"state": "done", "slug": "ofen-feta-pasta", "name": "Ofen-Feta-Pasta"})),
            lambda b: say("ok"),
        ]
        n = len(self.ai.requests)
        self.ok(self.admin.post("/api/linkimport", {"url": url}))
        st = self.wait_link()
        self.assertEqual(st["state"], "done", st)
        first = self.ai.requests[n]
        self.assertIn("read_video", {t["function"]["name"] for t in first["tools"]})
        self.assertIn("call read_video(url)", first["messages"][1]["content"])
        self.assertIn(TRANSCRIPT, seen["tool"])
        self.assertEqual(len(seen["images"]), 5)
        self.assertTrue(seen["images"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        d = self.ok(self.admin.get("/api/rezepte/ofen-feta-pasta"))
        self.assertTrue(d["bild"])
        self.assertEqual(d["quelle"], url)

    def test_model_without_vision_goes_on_with_text(self):
        self.ok(self.admin.post("/api/ai", {"provider": "openai", "base_url": self.ai.url, "model": "blind"}))
        self.addCleanup(lambda: self.admin.post("/api/ai", {"provider": "extern"}))
        url = self.whisper.url + "/v/blind"
        seen = {}

        def refuse(body):
            return 400, {"error": {"message": "this model does not support image input"}}

        def after(body):
            seen["last"] = body["messages"][-1]["content"]
            return call(api("POST", "/api/linkimport/state", {"state": "failed", "note": "test"}))
        self.ai.turns = [lambda b: call(("read_video", {"url": url})), refuse, after, lambda b: say("ok")]
        self.ok(self.admin.post("/api/linkimport", {"url": url}))
        self.assertEqual(self.wait_link()["note"], "test")
        self.assertIn("cannot see images", seen["last"])


class VideoSsrfTest(VideoBase):
    """Without KOMBUESE_ALLOW_PRIVATE_URLS a link into the local network is refused before yt-dlp starts."""
    PRIVATE = False

    def test_local_addresses_refused(self):
        n = len(self.calls())
        for url in (self.whisper.url + "/v/ok", f"http://localhost:{self.server.port}/v/ok", "http://192.168.2.1/v/x",
                    "http://169.254.169.254/latest/", "file:///etc/passwd", "ftp://example.org/v.mp4"):
            res = self.mcp_tool("read_video", {"url": url})
            self.assertTrue(res["isError"], url)
            self.assertRegex(res["content"][0]["text"], r"link refused|only http", url)
        self.assertEqual(self.calls()[n:], [], "yt-dlp must not have been started")


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class VideoRealFfmpegTest(VideoBase):
    """The real ffmpeg on a generated 6 s test video: sound for Whisper and stills."""
    REAL_FFMPEG = True

    @classmethod
    def setUpClass(cls):
        mp4 = os.path.join(tempfile.mkdtemp(prefix="kombuese-vmp4-"), "test.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=6:size=320x568:rate=10",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-shortest", "-c:v", "mpeg4", "-c:a", "aac", mp4], check=True)
        cls.addClassCleanup(shutil.rmtree, os.path.dirname(mp4), True)
        os.environ["VIDEO_STUB_MP4"] = mp4  # passed on to the server, and from there to the yt-dlp stub
        cls.addClassCleanup(os.environ.pop, "VIDEO_STUB_MP4", None)
        super().setUpClass()

    def test_sound_and_stills(self):
        n = len(self.whisper.requests)
        res = self.read_video("/v/real")
        self.assertFalse(res["isError"], res)
        images = [b for b in res["content"] if b["type"] == "image"]
        self.assertGreaterEqual(len(images), 3)
        self.assertLessEqual(len(images), 12)
        req = self.whisper.requests[n]
        self.assertIn(b'name="audio_file"', req["body"])
        self.assertGreater(len(req["body"]), 5000)


if __name__ == "__main__":
    unittest.main()
