"""Test harness: start app/server.py as a subprocess with an empty data directory and talk to it over HTTP.

Only the HTTP interface is used (no imports from app/), so the tests survive renaming the backend modules.
"""
import http.client
import http.server
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "app", "server.py")
ADMIN = ("admin", "admin-passwort-123")


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body or b"null")

    def header(self, name):
        return self.headers.get(name.lower())

    def __repr__(self):
        return f"<Response {self.status} {self.text[:300]!r}>"


class Client:
    """Small HTTP client with a cookie for the session. One connection per request (the server may close it)."""

    def __init__(self, server, token=None, app_header=True, forwarded_for=None):
        self.server, self.token, self.app_header, self.forwarded_for = server, token, app_header, forwarded_for
        self.cookie = None

    def request(self, method, path, body=None, headers=None, raw=None, content_type=None):
        h = {}
        if self.cookie:  # (name, value); the name is per installation
            h["Cookie"] = f"{self.cookie[0]}={self.cookie[1]}"
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.app_header and method == "POST":
            h["X-Kombuese"] = "1"
        if self.forwarded_for:
            h["X-Forwarded-For"] = self.forwarded_for
        data = None
        if raw is not None:
            data = raw
            h["Content-Type"] = content_type or "application/octet-stream"
        elif body is not None or method == "POST":
            data = json.dumps(body if body is not None else {}).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        con = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=60)
        try:
            con.request(method, path, body=data, headers=h)
            r = con.getresponse()
            raw_headers = r.getheaders()
            resp = Response(r.status, {k.lower(): v for k, v in raw_headers}, r.read())
        finally:
            con.close()
        resp.set_cookies = [v for k, v in raw_headers if k.lower() == "set-cookie"]
        for sc in resp.set_cookies:
            if not sc.startswith("kombuese_"):
                continue
            name, value = sc.split(";", 1)[0].split("=", 1)
            if value and "max-age=0" not in sc.lower():
                self.cookie = (name, value)
            elif self.cookie and self.cookie[0] == name:
                self.cookie = None
        return resp

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body, **kw)

    def login(self, username, password):
        r = self.post("/auth/login", {"username": username, "password": password})
        if r.status != 200:
            raise AssertionError(f"login {username}: {r}")
        return r


class Server:
    """One Kombüse server process with its own temporary data directory."""

    def __init__(self, env=None):
        self.port = free_port()
        self.dir = tempfile.mkdtemp(prefix="kombuese-test-")
        self.data = os.path.join(self.dir, "data")
        os.makedirs(os.path.join(self.dir, "secrets"))
        self.secret_key = secrets.token_urlsafe(32)
        self.log_path = os.path.join(self.dir, "server.log")
        base = {k: v for k, v in os.environ.items() if not k.startswith(("KOMBUESE_", "FT_"))}
        base.update({
            "KOMBUESE_DATA": self.data,
            "KOMBUESE_PORT": str(self.port),
            "KOMBUESE_SECRETS": os.path.join(self.dir, "secrets"),
            "KOMBUESE_SECRET_KEY": self.secret_key,
            # nothing in a test may reach real services: PDF renderer and internal address point to this server
            "KOMBUESE_INTERNAL_URL": f"http://127.0.0.1:{self.port}",
            "KOMBUESE_GOTENBERG_URL": "http://127.0.0.1:9",
            "KOMBUESE_UPDATE_URL": "off",  # the default asks GitHub
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        })
        base.update(env or {})
        self.env = base
        self.proc = None

    def start(self, timeout=30):
        os.makedirs(self.data, exist_ok=True)
        self._log = open(self.log_path, "wb")
        python = os.environ.get("KOMBUESE_TEST_PYTHON") or sys.executable
        self.proc = subprocess.Popen([python, SERVER], cwd=os.path.dirname(SERVER), env=self.env,
                                     stdout=self._log, stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited with {self.proc.returncode}:\n{self.log()}")
            try:
                r = Client(self).get("/api/version")
                if r.status == 200:
                    return self
            except OSError:
                pass
            time.sleep(0.1)
        self.stop()
        raise RuntimeError(f"server did not answer /api/version within {timeout} s:\n{self.log()}")

    def log(self):
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
        if getattr(self, "_log", None):
            self._log.close()
        if os.environ.get("KOMBUESE_TEST_LOG") == "1":
            print(f"\n----- server log {self.dir} -----\n{self.log()}", file=sys.stderr)
        if os.environ.get("KOMBUESE_TEST_KEEP") == "1":
            print(f"kept test data: {self.dir}", file=sys.stderr)
        else:
            shutil.rmtree(self.dir, ignore_errors=True)

    def client(self, **kw):
        return Client(self, **kw)


class ServerTestCase(unittest.TestCase):
    """Starts one fresh server per test class. With SETUP_ADMIN the first admin exists and self.admin is signed in."""

    SETUP_ADMIN = True
    SERVER_ENV = None

    @classmethod
    def setUpClass(cls):
        cls.server = Server(cls.SERVER_ENV).start()
        cls.addClassCleanup(cls.server.stop)
        if cls.SETUP_ADMIN:
            cls.admin = cls.server.client()
            r = cls.admin.post("/auth/setup", {"username": ADMIN[0], "password": ADMIN[1]})
            if r.status != 200:
                raise RuntimeError(f"setup failed: {r}\n{cls.server.log()}")

    # ---- helpers ----
    def ok(self, r, status=200):
        """Assert the status and return the JSON body."""
        self.assertEqual(r.status, status, f"{r}\n--- server log (tail) ---\n{self.server.log()[-3000:]}")
        ctype = r.header("content-type") or ""
        return r.json() if "json" in ctype else r

    def token(self, scope="api", name=None):
        r = self.admin.post("/api/tokens", {"name": name or f"test-{scope}-{secrets.token_hex(3)}", "scope": scope})
        return self.ok(r)

    def member(self, username=None, password="mitglied-passwort-1"):
        username = username or f"member{secrets.token_hex(3)}"
        d = self.ok(self.admin.post("/api/users", {"username": username, "password": password, "role": "member"}))
        c = self.server.client()
        c.login(username, password)
        return c, d["id"]

    def recipe(self, name, **fields):
        d = self.ok(self.admin.post("/api/rezepte", {"name": name, **fields}))
        return d["slug"]


class FakeSite:
    """A tiny web server on 127.0.0.1 inside the test, e.g. a recipe page. Counts the requests it got."""

    def __init__(self, pages):
        self.pages, self.hits = pages, []
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                site.hits.append(self.path)
                page = site.pages.get(self.path)
                body = (page[1] if page else "not found").encode()
                self.send_response(200 if page else 404)
                self.send_header("Content-Type", page[0] if page else "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
