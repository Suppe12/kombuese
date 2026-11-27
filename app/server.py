#!/usr/bin/env python3
"""Kombüse (formerly Food Tinder): self-hosted kitchen app. HTTP server and all routes.

Swipe cards ("Food Tinder"), recipes, week plan, meal prep, shopping list, pantry, freezer, Yazio, AI runs, MCP, backup.
Python standard library only (plus the optional anthropic SDK in ai.py). Data in /data (SQLite + images), web app in static/.
This file owns the request handler H (login gate, GET and POST routes) and the tables of the swipe cards (cards),
the dislikes (abneigung), eaten meals (gegessen), the freezer (vorrat) and the meta key/value table.
The other modules: store.py (recipes, plan, shopping list), pantry.py, shopping.py (Bring!), week.py, recipes.py,
printing.py, scrape.py, yazio.py, auth.py, households.py, ai.py, jobs.py, mcpserver.py, backup.py, household.py, integrations.py,
notify.py, push.py, vapid.py, netsafe.py, secretbox.py, config.py.
API paths, JSON fields, table and column names and the error texts are German and part of the API; they stay."""
import json
import mimetypes
import os
import re
import struct
import threading
import time
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import ai
import auth
import backup
import config
import printing
import shopping
import household
import households
import integrations
import jobs
import store
import mcpserver
import netsafe
import notify
import nutrition
import openapi
import push
import recipes
import pantry
import photos
import scrape
import share
import updates
import video
import week
import yazio

DATA = config.DATA
PORT = config.PORT
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
IMG = os.path.join(DATA, "img")  # household 1; img_dir() for the current household
DB = os.path.join(DATA, config.DB_FILE)
UA = "Mozilla/5.0 (Kombuese; self-hosted kitchen app)"
os.makedirs(IMG, exist_ok=True)
lock = threading.Lock()


def check_purchases_in_background():
    """Look in Bring whether something we sent was checked off, and book it into the pantry (at most every 10 min)."""
    def run():
        try:
            r = pantry.check_purchases(shopping.bring_state)
            if r and r.get("gebucht"):
                print("Pantry, bought:", ", ".join(r["gebucht"]), flush=True)
        except Exception as e:  # noqa: BLE001
            print("Pantry/Bring:", e, flush=True)
    store.spawn(run)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_ms():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def db():
    """Database of the current household (store.py)."""
    return store.con()


def img_dir():
    return os.path.join(store.data_dir(), "img")


def init_db():
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS cards (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              url TEXT UNIQUE NOT NULL,
              title TEXT NOT NULL,
              description TEXT DEFAULT '',
              reason TEXT DEFAULT '',
              image TEXT DEFAULT '',
              tags TEXT DEFAULT '[]',
              minutes INTEGER,
              servings TEXT DEFAULT '',
              kcal INTEGER,
              ingredients TEXT DEFAULT '[]',
              source TEXT DEFAULT '',
              batch TEXT DEFAULT '',
              added TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'neu',   -- neu (new) | like | nope | mealie (imported) | fehler (import failed)
              plan INTEGER NOT NULL DEFAULT 0,      -- 1 = cook this week
              swiped TEXT,
              mealie_slug TEXT,
              imported TEXT,
              note TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            -- dislikes: kept by the user (manuell) or removed; the counts come from cards.why_zutaten
            CREATE TABLE IF NOT EXISTS abneigung (
              key TEXT PRIMARY KEY,                 -- ingredient in lower case
              name TEXT NOT NULL,
              manuell INTEGER NOT NULL DEFAULT 0,   -- 1 = entered by hand
              entfernt TEXT                         -- set = deleted by the user; only later noes count
            );
            -- eaten meals: table gegessen, see nutrition.py (one row per plan entry and person)
            -- freezer. At 0 the entry disappears
            CREATE TABLE IF NOT EXISTS vorrat (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              anzahl REAL NOT NULL DEFAULT 0,
              einheit TEXT NOT NULL DEFAULT 'Portion',
              kategorie TEXT NOT NULL DEFAULT 'haupt',   -- fruehstueck | haupt | sonstiges
              eingefroren TEXT,                          -- YYYY-MM-DD
              haltbar_bis TEXT,                          -- YYYY-MM-DD
              notiz TEXT DEFAULT '',
              recipe_slug TEXT DEFAULT '',
              geaendert TEXT
            );
            """
        )
        cols = {r["name"] for r in con.execute("PRAGMA table_info(cards)")}
        for col, ddl in (("kcal_est", "INTEGER NOT NULL DEFAULT 0"),   # 1 = calories estimated by the AI
                         ("why", "TEXT DEFAULT ''"),                   # reasons for swiping left, codes separated by commas
                         ("meal", "TEXT NOT NULL DEFAULT 'haupt'"),    # haupt (lunch/dinner) | fruehstueck (breakfast)
                         ("zutaten", "TEXT DEFAULT '[]'"),             # main ingredients in German (for the picker on a no)
                         ("why_zutaten", "TEXT DEFAULT '[]'"),         # which ingredient the user does not like
                         ("why_text", "TEXT DEFAULT ''"),              # free-text reason
                         ("why_at", "TEXT")):                          # when the reason was set
            if col not in cols:
                con.execute(f"ALTER TABLE cards ADD COLUMN {col} {ddl}")


MEALS = ("haupt", "fruehstueck")
WHY = {"zutat": "mag eine Zutat nicht", "viele": "zu viele Zutaten", "aufwand": "zu aufwendig",
       "lang": "dauert zu lange", "schwer": "zu schwer/fettig", "scharf": "zu scharf", "suess": "zu süß",
       "exotisch": "zu exotisch/ungewohnt", "fleisch": "mir fehlt Fleisch", "mahlzeit": "passt nicht zur Mahlzeit",
       "optik": "sieht nicht lecker aus", "kenn": "kenne/hatte ich zu oft", "grad": "nur gerade keine Lust"}


def codes(s):
    return [c for c in (s or "").split(",") if c]


def jl(s):
    return json.loads(s or "[]")


def dislikes(con):
    """Ingredients the user tapped on a no or entered themselves. Only this list counts as dislikes."""
    tab = {r["key"]: dict(r) for r in con.execute("SELECT * FROM abneigung")}
    out = {}
    for r in con.execute("SELECT why_zutaten, why_at, swiped FROM cards WHERE status='nope' AND why_zutaten NOT IN ('', '[]')"):
        for z in jl(r["why_zutaten"]):
            k = z.strip().lower()
            if not k:
                continue
            t = tab.get(k)
            if t and t["entfernt"] and (r["why_at"] or r["swiped"] or "") <= t["entfernt"]:
                continue
            d = out.setdefault(k, {"zutat": t["name"] if t else z.strip(), "n": 0, "manuell": False})
            d["n"] += 1
    for k, t in tab.items():
        if t["manuell"] and not t["entfernt"]:
            out.setdefault(k, {"zutat": t["name"], "n": 0, "manuell": False})["manuell"] = True
    return sorted(out.values(), key=lambda x: (-x["n"], x["zutat"].lower()))


def taste(con):
    """Taste profile from all swipe decisions: like ratio per tag, plus calories and time."""
    rows = con.execute("SELECT tags, status, plan, kcal, minutes, why, meal FROM cards WHERE status!='neu'").fetchall()
    tags, why = {}, {}
    meals = {m: {"likes": 0, "nopes": 0, "kcal": []} for m in MEALS}
    agg = {"like": {"n": 0, "kcal": [], "min": []}, "nope": {"n": 0, "kcal": [], "min": []}}
    for r in rows:
        liked = r["status"] != "nope"
        a = agg["like" if liked else "nope"]
        a["n"] += 1
        m = meals.setdefault(r["meal"], {"likes": 0, "nopes": 0, "kcal": []})
        m["likes" if liked else "nopes"] += 1
        if liked and r["kcal"]:
            m["kcal"].append(r["kcal"])
        if r["kcal"]:
            a["kcal"].append(r["kcal"])
        if r["minutes"]:
            a["min"].append(r["minutes"])
        if not liked:
            for w in codes(r["why"]):
                why[w] = why.get(w, 0) + 1
        for t in json.loads(r["tags"] or "[]"):
            d = tags.setdefault(t, {"like": 0, "nope": 0, "plan": 0})
            d["like" if liked else "nope"] += 1
            d["plan"] += 1 if r["plan"] else 0
    avg = lambda xs: round(sum(xs) / len(xs)) if xs else None  # noqa: E731
    tag_list = sorted(({"tag": t, **d, "quote": round(d["like"] / (d["like"] + d["nope"]), 2)} for t, d in tags.items()),
                      key=lambda x: (-x["quote"], -(x["like"] + x["nope"])))
    return {
        "entscheidungen": len(rows),
        "likes": agg["like"]["n"], "nopes": agg["nope"]["n"],
        "kcal_like": avg(agg["like"]["kcal"]), "kcal_nope": avg(agg["nope"]["kcal"]),
        "minuten_like": avg(agg["like"]["min"]), "minuten_nope": avg(agg["nope"]["min"]),
        "tags": tag_list,
        "mahlzeiten": {k: {"likes": v["likes"], "nopes": v["nopes"], "kcal_like": avg(v["kcal"])} for k, v in meals.items()},
        "nein_gruende": {WHY.get(k, k): v for k, v in sorted(why.items(), key=lambda x: -x[1])},
        "abneigungen": dislikes(con),
        "nein_freitext": [{"titel": r["title"], "text": r["why_text"]} for r in con.execute(
            "SELECT title, why_text FROM cards WHERE status='nope' AND why_text!='' ORDER BY why_at DESC LIMIT 30")],
        "profil": meta_get(con, "profil", {"text": "", "stand": None}),
    }


FREEZER_CATEGORIES = ("fruehstueck", "haupt", "sonstiges")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def freezer(con):
    return [dict(r) for r in con.execute(
        "SELECT * FROM vorrat WHERE anzahl > 0 ORDER BY CASE WHEN haltbar_bis IS NULL OR haltbar_bis='' THEN 1 ELSE 0 END, haltbar_bis, name")]


def freezer_fields(b):
    """Check and clean what the app or the AI sends for a freezer entry."""
    out = {}
    if "name" in b:
        out["name"] = re.sub(r"\s+", " ", str(b["name"] or "")).strip()[:80]
    if "anzahl" in b:
        out["anzahl"] = max(0.0, round(float(b["anzahl"]), 2))
    for k, n in (("einheit", 20), ("notiz", 200), ("recipe_slug", 120)):
        if k in b:
            out[k] = str(b[k] or "").strip()[:n]
    if "kategorie" in b:
        out["kategorie"] = b["kategorie"] if b["kategorie"] in FREEZER_CATEGORIES else "sonstiges"
    for k in ("eingefroren", "haltbar_bis"):
        if k in b:
            v = str(b[k] or "")
            out[k] = v if DATE_RE.fullmatch(v) else None
    return out


def mealprep_plan(b):
    """Check the AI's meal prep schedule and cut everything to sensible lengths."""
    t = lambda x, n=300: str(x or "").strip()[:n]
    day = t(b.get("datum"), 10)
    if not DATE_RE.fullmatch(day):
        raise ValueError("datum fehlt")
    slug_ok = lambda x: bool(re.fullmatch(r"[a-z0-9-]{1,120}", x or ""))
    recipes = [{"slug": t(r.get("slug"), 120), "name": t(r.get("name"), 120), "portionen": t(r.get("portionen"), 60)}
               for r in (b.get("rezepte") or [])[:12] if slug_ok(r.get("slug"))]
    if not recipes:
        raise ValueError("rezepte fehlen")
    vb = b.get("vorbereiten") or {}
    ingr = lambda z: {"menge": t(z.get("menge"), 40), "name": t(z.get("name"), 120), "rezept": t(z.get("rezept"), 120)}
    steps = []
    for x in (b.get("schritte") or [])[:120]:
        if not (x.get("text") or x.get("titel")):
            continue
        timer = []
        for tm in (x.get("timer") or [])[:4]:
            try:
                m = float(tm.get("min"))
            except (TypeError, ValueError):
                continue
            if 0.5 <= m <= 360:
                timer.append({"min": m, "label": t(tm.get("label"), 60)})
        steps.append({"titel": t(x.get("titel"), 120), "text": t(x.get("text"), 1500),
                         "rezepte": [t(r, 120) for r in (x.get("rezepte") or [])[:6] if slug_ok(r) or r == "alle"],
                         "zutaten": [ingr(z) for z in (x.get("zutaten") or [])[:30] if z.get("name")],
                         "timer": timer, "nebenbei": t(x.get("nebenbei"), 300), "minuten": t(x.get("minuten"), 10)})
    if not steps:
        raise ValueError("schritte fehlen")
    return {"datum": day, "titel": t(b.get("titel"), 120) or f"Mealprep {day}", "dauer_min": t(b.get("dauer_min"), 10),
            "hinweis": t(b.get("hinweis"), 1000), "rezepte": recipes,
            "vorbereiten": {"geraete": [t(g, 120) for g in (vb.get("geraete") or [])[:20]],
                            "zutaten": [{**ingr(z), "fuer": [t(f, 120) for f in (z.get("fuer") or [])[:6]]}
                                        for z in (vb.get("zutaten") or [])[:80] if z.get("name")]},
            "schritte": steps, "erstellt": now()}


def meta_get(con, key, default=None):
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def meta_set(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, json.dumps(value)))


def card_out(r):
    d = dict(r)
    d["tags"] = json.loads(d["tags"] or "[]")
    d["ingredients"] = json.loads(d["ingredients"] or "[]")
    for k in ("zutaten", "why_zutaten"):
        if k in d:
            d[k] = jl(d[k])
    if "why" in d:
        d["why"] = codes(d["why"])
    for k in ("kcal", "minutes"):  # also cards stored with text in these columns before the security review
        if k in d:
            d[k] = int_or_none(d[k])
    d["image"] =f"img/{d['image']}" if d["image"] else ""
    return d


def fetch_image(card_id, url):
    """Download the card image once, so the app does not load images from other sites."""
    if not url:
        return ""
    try:
        data, ctype, _, _ = netsafe.open_url(url, {"User-Agent": UA, "Accept": "image/*"}, max_bytes=8 * 1024 * 1024 + 1)
        if not ctype.startswith("image/"):
            return ""
        if len(data) > 8 * 1024 * 1024:
            return ""
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif", "image/avif": "avif"}.get(ctype, "jpg")
        name = f"{card_id}.{ext}"
        with open(os.path.join(img_dir(), name), "wb") as f:
            f.write(data)
        return name
    except Exception as e:  # noqa: BLE001
        print(f"Image {url}: {e}", flush=True)
        return ""


# ---------- app icon as PNG, without Pillow ----------
def make_icon(size):
    def px(x, y):
        # background: warm gradient
        t = (x + y) / (2 * size)
        bg = (int(255 - 40 * t), int(122 - 50 * t), int(89 - 30 * t))
        # heart: (x^2 + y^2 - 1)^3 - x^2 y^3 <= 0
        u = (x - size / 2) / (size * 0.30)
        v = -(y - size * 0.53) / (size * 0.30)
        h = (u * u + v * v - 1) ** 3 - u * u * v ** 3
        if h <= 0:
            return (255, 248, 240)
        return bg

    raw = b"".join(b"\x00" + b"".join(bytes(px(x, y)) for x in range(size)) for y in range(size))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


ICONS = {}


def int_or_none(v):
    """A whole number or None. Numbers from clients must not land as text in INTEGER columns: SQLite keeps the text,
    and the app puts kcal and minutes into its pages as numbers (security review: stored XSS through a card's kcal)."""
    try:
        return None if v in (None, "") else int(float(v))
    except (TypeError, ValueError, OverflowError):
        return None


class H(BaseHTTPRequestHandler):
    server_version = "kombuese"
    timeout = config.SOCKET_TIMEOUT or None  # security review: a client that sends nothing no longer holds a thread forever

    # Security review: on every answer unless the route sets its own value (the public recipe page has a stricter CSP).
    # The app needs inline scripts, so the CSP only covers frames, plugins and <base>; frame-ancestors: KOMBUESE_FRAME_ANCESTORS
    SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
                        "Content-Security-Policy": f"frame-ancestors {config.FRAME_ANCESTORS}; object-src 'none'; base-uri 'self'",
                        **({"X-Frame-Options": "SAMEORIGIN"} if config.FRAME_ANCESTORS == "'self'" else {})}

    def log_message(self, fmt, *args):
        if not self.path.startswith(("/api/status", "/img/", "/api/version")):
            line = re.sub(r"([?&]token=)[^&\s\"]+", r"\1[redacted]", fmt % args)  # calendar feeds carry their API token in the URL
            print(f"{self.address_string()} {line}", flush=True)

    # ----- helpers -----
    def send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        given = {k.lower() for k in (extra or {})}
        for k, v in self.SECURITY_HEADERS.items():
            if k.lower() not in given:
                self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        for c in getattr(self, "set_cookies", None) or []:  # more Set-Cookie headers (session cookie takeover)
            self.send_header("Set-Cookie", c)
        self.set_cookies = []
        self.end_headers()
        self.wfile.write(body)

    def body(self, limit=2 * 1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("zu gross")
        return json.loads(self.rfile.read(n) or b"{}")

    # ----- login (auth.py) -----
    PUBLIC = {"/login", "/setup", "/manifest.json", "/icon.svg", "/i18n.js", "/api/version", "/favicon.ico", "/sw.js"}

    def do_GET(self):
        if not self._gate("GET"):
            self._get()

    def do_POST(self):
        if not self._gate("POST"):
            self._post()

    def do_DELETE(self):  # MCP clients end sessions this way; /mcp has none (stateless)
        self.send(405, {"error": "unbekannt"}, extra={"Allow": "GET, POST"})

    def _base_url(self):
        proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
        host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or f"localhost:{PORT}").split(",")[0].strip()
        return f"{proto}://{host}"

    def _ip(self):
        """Client address for the sign-in rate limit. X-Forwarded-For only counts when the request comes from a proxy
        listed in KOMBUESE_TRUSTED_PROXIES; then the rightmost address that is not a trusted proxy wins.
        Otherwise anyone could send a new made-up address with every attempt."""
        peer = self.client_address[0]
        if not config.trusted_proxy(peer):
            return peer
        chain = [x.strip() for x in (self.headers.get("X-Forwarded-For") or "").split(",") if x.strip()]
        for addr in reversed(chain):
            if not config.trusted_proxy(addr):
                return addr
        return chain[0] if chain else peer

    def _redirect(self, url, cookie=None):
        self.send(302, b"", "text/plain", {"Location": url, **({"Set-Cookie": cookie} if cookie else {}), "Cache-Control": "no-store"})

    def _gate(self, method):
        """Check the login. True = the request is already answered (login pages, auth routes, accounts, refused)."""
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        secure = self._base_url().startswith("https://")
        agent = self.headers.get("User-Agent") or ""
        self.user = None
        store.set_household(1)  # until we know who is asking (a connection may serve several requests)
        self.set_cookies = []
        try:
            # Security review: a negative Content-Length made rfile.read(-1) wait for the end of the connection (memory, a thread)
            cl = self.headers.get("Content-Length")
            if cl is not None and not re.fullmatch(r"\d{1,12}", cl.strip()):
                self.close_connection = True
                self.send(400, {"error": "Content-Length ungültig"})
                return True
            # Security review: sign-in, sign-up and sign-out only from the app (JSON or the app header), not from a
            # form or text/plain request of another site (login CSRF); a JSON request from elsewhere needs CORS, which we never allow
            if (p.startswith("/auth/") and method == "POST" and self.headers.get("X-Kombuese") != "1"
                    and not (self.headers.get("Content-Type") or "").lower().startswith("application/json")):
                self.send(403, {"error": "Anfrage ohne App-Kennung abgelehnt (CSRF-Schutz)"})
                return True
            if p.startswith("/auth/invite"):  # invitation link or code: show it, create the account
                self._invite(method, q, secure, agent)
                return True
            if p.startswith("/auth/"):
                if p == "/auth/status" and method == "GET":
                    self.send(200, {"setup": auth.user_count() == 0, "oidc": auth.oidc_enabled(), "oidc_name": config.OIDC_NAME})
                elif p == "/auth/login" and method == "POST":
                    b = self.body()
                    remember = b.get("remember", True) is not False  # "stay signed in", default yes
                    t = auth.login(b.get("username"), b.get("password"), self._ip(), agent, remember)
                    self.send(200, {"ok": True}, extra={"Set-Cookie": auth.cookie_header(t, secure, remember=remember)})
                elif p == "/auth/setup" and method == "POST":
                    b = self.body()
                    t = auth.setup(b.get("username"), b.get("password"), self._ip(), agent)
                    self.send(200, {"ok": True}, extra={"Set-Cookie": auth.cookie_header(t, secure)})
                elif p == "/auth/setup/import" and method == "POST":  # new installation from a backup
                    if self.headers.get("X-Kombuese") != "1":
                        self.send(403, {"error": "Anfrage ohne App-Kennung abgelehnt (CSRF-Schutz)"})
                    else:
                        with auth._lock:
                            if auth.user_count():
                                raise auth.AuthError("Es gibt schon ein Konto", 409)
                            self._backup("POST", p, setup=True)
                elif p == "/auth/logout" and method == "POST":
                    auth.logout(auth._cookie(self.headers))
                    self.set_cookies = [auth.cookie_header("", secure, clear=True, name=auth.COOKIE)]
                    self.send(200, {"ok": True}, extra={"Set-Cookie": auth.cookie_header("", secure, clear=True)})
                elif p == "/auth/oidc/start" and auth.oidc_enabled():
                    self._redirect(auth.oidc_start(self._base_url(), q.get("invite", [None])[0]))
                elif p == "/auth/oidc/callback" and auth.oidc_enabled():
                    try:
                        t = auth.oidc_callback(q, self._ip(), agent)
                        self._redirect("/", auth.cookie_header(t, secure))
                    except auth.AuthError as e:
                        self._redirect("/login?fehler=" + quote(str(e)))
                else:
                    self.send(404, {"error": "unbekannt"})
                return True
            if p.startswith("/s/"):  # public link of one shared recipe (share.py), no account
                self._share(method, p, q)
                return True
            if method == "GET" and p.startswith(("/api/openapi.json", "/api/docs")):  # API description and Swagger UI, public
                self._docs(p)
                return True
            if method == "GET" and (p in self.PUBLIC or re.fullmatch(r"/icon-\d+\.png", p)):
                if p in ("/login", "/setup"):
                    self.static("login.html", cache=False)
                    return True
                if p == "/i18n.js":  # starting language of the signed-in household
                    u = households.identify(self.headers)
                    store.set_household(u["household_id"] if u else 1)
                return False
            if p == "/api/push/pending" and method == "GET":  # service worker after a push: the device's secret, no session
                try:
                    self.send(200, push.pending(q.get("id", [""])[0], self.headers.get("X-Push-Secret") or ""), extra={"Cache-Control": "no-store"})
                except store.Error:
                    self.send(404, {"error": "unbekannt"}, extra={"Cache-Control": "no-store"})
                return True
            self.user = households.identify(self.headers, q)  # auth.identify plus the household
            if self.user:
                store.set_household(self.user["household_id"])
            if self.user and self.user.get("cookie_legacy"):  # session with the older cookie name: move it to the new cookie name
                self.set_cookies = [auth.cookie_header(self.user["session"], secure, remember=self.user["remember"]),
                                    auth.cookie_header("", secure, clear=True, name=auth.COOKIE)]
            if p == "/mcp":  # MCP server for AI clients (mcpserver.py), API token only
                mcpserver.handle(self, method)
                return True
            if p == "/kalender.ics" and method == "GET" and (config.CALENDAR_PUBLIC or (self.user and self.user["scope"] in ("api", "calendar"))):
                return False
            if self.user and self.user["scope"] == "calendar":
                self.send(403, {"error": "Dieses Token gilt nur für den Kalender"})
                return True
            if self.user and self.user["via"] == "internal" and not (method == "GET" and p.startswith(("/rezept/", "/mimg/"))):
                self.send(403, {"error": "nicht erlaubt"})
                return True
            if self.user and self.user["via"] == "agent" and (not p.startswith("/api/") or p.startswith(("/api/me", "/api/users", "/api/tokens", "/api/integrations", "/api/ai", "/api/notify", "/api/backup", "/api/household", "/api/invites", "/api/members", "/api/push"))):
                self.send(403, {"error": "nicht erlaubt"})  # AI runs inside the app (jobs.py) also check their paths themselves
                return True
            if not self.user:
                if method == "GET" and not p.startswith("/api/"):
                    self._redirect("/login")
                else:
                    self.send(401, {"error": "Nicht angemeldet", "login": True})
                return True
            if method == "POST" and self.user["via"] == "session" and self.headers.get("X-Kombuese") != "1":
                self.send(403, {"error": "Anfrage ohne App-Kennung abgelehnt (CSRF-Schutz)"})
                return True
            return self._accounts(method, p)
        except auth.AuthError as e:
            self.send(e.code, {"error": str(e)})
            return True
        except ValueError as e:
            self.send(400, {"error": str(e)})
            return True

    def _invite(self, method, q, secure, agent):
        """GET /auth/invite?code= → household, role, expiry; POST /auth/invite {code, username, password, display_name}
        → new account in that household, signed in. Wrong codes count like wrong passwords."""
        if method == "GET":
            self.send(200, {**households.check_invite(q.get("code", [""])[0], self._ip()), "oidc": auth.oidc_enabled(),
                            "oidc_name": config.OIDC_NAME})
        elif method == "POST":
            b = self.body()
            t = households.redeem(b.get("code"), b.get("username"), b.get("password"), b.get("display_name"), self._ip(), agent)
            self.send(200, {"ok": True}, extra={"Set-Cookie": auth.cookie_header(t, secure)})
        else:
            self.send(404, {"error": "unbekannt"})

    def _households(self, method, p):
        """Households of the installation, instance admins only: list, create (with an invitation for its first
        admin), rename, delete when empty, new invitation."""
        if not households.instance_admin(self.user):
            self.send(403, {"error": "Nur für Admins"})
            return True
        m = re.fullmatch(r"/api/households(?:/(\d+)(/invites)?)?", p)
        if not m:
            self.send(404, {"error": "unbekannt"})
            return True
        hid = int(m.group(1)) if m.group(1) else None
        if method == "GET" and not hid:
            self.send(200, households.overview())
        elif method == "POST" and not hid:
            b = self.body()
            lang = b.get("language") if b.get("language") in household.LANGS else household.language()
            hid = households.create(b.get("name"))
            with store.in_household(hid):
                init_household()
                household.save({"language": lang, "description": "", "people": None})
            inv = households.create_invite(hid, self.user["id"], "admin", b.get("days") or 7)
            self.send(200, {"ok": True, "id": hid, "name": households.info(hid)["name"], "invite": self._invite_out(inv)})
        elif method == "POST" and m.group(2):
            b = self.body()
            self.send(200, self._invite_out(households.create_invite(hid, self.user["id"], b.get("role") or "admin", b.get("days") or 7, b.get("note") or "")))
        elif method == "POST":
            b = self.body()
            if b.get("delete"):
                out = households.delete(hid)
                share.remove_household(hid)  # its public recipe links
                self.send(200, out)
            else:
                self.send(200, {"ok": True, **households.rename(hid, b.get("name"))})
        else:
            self.send(404, {"error": "unbekannt"})
        return True

    def _invite_out(self, inv):
        return {**inv, "link": f"{self._base_url()}/login?invite={inv['code']}"}

    SHARE_HEADERS = {"X-Robots-Tag": "noindex, nofollow, noarchive", "Referrer-Policy": "no-referrer",
                     "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-cache"}
    SHARE_CSP = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"

    def _share(self, method, p, q):
        """Public page of a shared recipe: /s/<token> (page, ?portionen=), /s/<token>/bild (its photo),
        /s/<token>.pdf (PDF). The token only opens this one recipe; there is no user, so no other route is reachable."""
        h = self.SHARE_HEADERS
        if method != "GET":
            return self.send(405, {"error": "unbekannt"}, extra={**h, "Allow": "GET"})
        m = re.fullmatch(r"/s/([A-Za-z0-9_-]{20,64})(\.pdf|/bild)?", p)
        ip = self._ip()
        if not share.allowed(ip, "pdf" if m and m.group(2) == ".pdf" else "all"):
            return self.send(429, "Zu viele Anfragen, bitte später noch einmal. / Too many requests, please try again later.",
                             "text/plain; charset=utf-8", {**h, "Retry-After": "600"})
        found = share.resolve(m.group(1), count=not m.group(2)) if m else None
        if not found:
            share.failed(ip)
            return self.send(404, "Dieser Link gilt nicht (mehr). / This link is not valid (any more).", "text/plain; charset=utf-8", h)
        hid, r = found
        store.set_household(hid)
        try:
            portions = float(q.get("portionen", ["0"])[0] or 0) or None
        except ValueError:
            portions = None
        portions = portions and max(0.5, min(50.0, portions))
        lang = r.get("sprache") or household.language()
        if m.group(2) == "/bild":
            f = store.image_file(r["id"]) if r.get("bild") else None
            if not f:
                return self.send(404, {"error": "kein Bild"}, extra=h)
            with open(f[0], "rb") as fh:
                return self.send(200, fh.read(), f[1], {**h, "Cache-Control": "private, max-age=3600"})
        if m.group(2) == ".pdf":
            try:
                data = printing.pdf(r["slug"], portions, lang)
            except printing.Error as e:
                return self.send(502, str(e), "text/plain; charset=utf-8", h)
            name = re.sub(r"[^A-Za-z0-9-]+", "-", store.slugify(r["name"]).title()) + ".pdf"
            return self.send(200, data, "application/pdf", {**h, "Content-Disposition": f'inline; filename="{name}"'})
        page = printing.recipe_page(recipes.detail(r["slug"]), portions, lang=lang, share=m.group(1))
        return self.send(200, page, "text/html; charset=utf-8", {**h, "Content-Security-Policy": self.SHARE_CSP})

    # Swagger UI (static/apidocs, Apache-2.0): only its own files, scripts from this server, requests to this server
    DOCS_FILES = {"index.html": "text/html; charset=utf-8", "swagger-ui-bundle.js": "application/javascript; charset=utf-8",
                  "swagger-ui.css": "text/css; charset=utf-8", "init.js": "application/javascript; charset=utf-8",
                  "docs.css": "text/css; charset=utf-8", "LICENSE": "text/plain; charset=utf-8", "NOTICE": "text/plain; charset=utf-8",
                  "swagger-ui-bundle.js.LICENSE.txt": "text/plain; charset=utf-8"}
    DOCS_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; "
                f"form-action 'none'; base-uri 'none'; frame-ancestors {config.FRAME_ANCESTORS}")

    def _docs(self, p):
        """GET /api/openapi.json (openapi.py) and the API documentation /api/docs[/<file>]. Public: the
        description holds nothing of this installation; trying an operation out needs a session or a token."""
        if p == "/api/openapi.json":
            return self.send(200, openapi.document(), extra={"Cache-Control": "no-cache"})
        m = re.fullmatch(r"/api/docs/([\w.-]+)", p)
        name = "index.html" if p == "/api/docs" else m.group(1) if m else ""
        if name not in self.DOCS_FILES or (m and name == "index.html"):
            return self.send(404, {"error": "unbekannt"})
        with open(os.path.join(STATIC, "apidocs", name), "rb") as fh:
            data = fh.read()
        return self.send(200, data, self.DOCS_FILES[name], {"Content-Security-Policy": self.DOCS_CSP,
                                                           "Cache-Control": "no-cache" if name in ("index.html", "init.js") else "max-age=86400"})

    def _shares(self, method, p):
        """Links of shared recipes. Everyone in the household shares and stops sharing a recipe;
        the list of all links is for admins (⚙️). AI runs inside the app cannot share."""
        me, base = self.user, self._base_url()
        if me["via"] == "agent":
            self.send(403, {"error": "nicht erlaubt"})
            return True
        try:
            m = re.fullmatch(r"/api/rezepte/([a-z0-9-]{1,120})/teilen", p)
            if m and method == "GET":
                self.send(200, {"link": share.for_recipe(m.group(1), base)})
            elif m and method == "POST":
                self.send(200, {"ok": True, "link": share.create(m.group(1), me["id"], base)})
            elif p == "/api/freigaben" and method == "GET":
                if me["role"] != "admin" or me["via"] != "session":
                    self.send(403, {"error": "Nur für Admins"})
                else:
                    self.send(200, share.list_all(base))
            elif re.fullmatch(r"/api/freigaben/\d+/weg", p) and method == "POST":
                self.send(200, share.revoke(p.split("/")[3]))
            else:
                self.send(404, {"error": "unbekannt"})
        except store.Error as e:
            self.send(404, {"error": str(e)})
        return True

    def _integrations(self, method, p):
        """Set up and test Bring, Yazio, Telegram, ntfy. Secrets never go back to the app."""
        if self.user["role"] != "admin" or self.user["via"] != "session":
            self.send(403, {"error": "Nur für Admins"})
            return True
        m = re.fullmatch(r"/api/integrations(?:/(bring|yazio|telegram|ntfy)(/test)?)?", p)
        if not m:
            self.send(404, {"error": "unbekannt"})
            return True
        name = m.group(1)
        try:
            if method == "GET" and not name:
                self.send(200, {**integrations.overview(), "webpush": push.overview()})  # webpush: devices
            elif method == "POST" and name and m.group(2):
                if name == "bring":
                    self.send(200, shopping.bring_test())
                elif name == "yazio":
                    yazio._tok.clear()
                    self.send(200, {"ok": True, "heute": yazio.day(now()[:10])})
                else:
                    self.send(200, notify.test(name))
            elif method == "POST" and name:
                b = self.body()
                r = integrations.delete(name) if b.get("delete") else integrations.save(name, b)
                shopping._bring.clear()  # new credentials: log in again
                yazio._tok.clear()
                self.send(200, r)
            else:
                self.send(404, {"error": "unbekannt"})
        except store.Error as e:
            self.send(400, {"error": str(e)})
        return True

    def _push(self, method, p):
        """Web Push per device (push.py): public key and own devices, subscribe/unsubscribe, test message."""
        me = self.user
        if me["via"] != "session":
            self.send(403, {"error": "nicht erlaubt"})
            return True
        try:
            if p == "/api/push/key" and method == "GET":
                self.send(200, {"key": push.public_key(), "devices": push.devices(me), "household_devices": push.count(me["household_id"])},
                          extra={"Cache-Control": "no-store"})
            elif p == "/api/push/subscribe" and method == "POST":  # {endpoint, keys: {p256dh, auth}} | {endpoint or id, delete: true}
                b = self.body()
                if b.get("delete"):
                    self.send(200, push.unsubscribe(me, b.get("endpoint"), b.get("id")))
                else:
                    self.send(200, push.subscribe(me, b, self.headers.get("User-Agent") or "", self._base_url()))
            elif p == "/api/push/unsubscribe" and method == "POST":  # {endpoint or id}
                b = self.body()
                self.send(200, push.unsubscribe(me, b.get("endpoint"), b.get("id")))
            elif p == "/api/push/test" and method == "POST":  # {endpoint or id, lang}
                b = self.body()
                text = {"text": "Test message from Kombüse ✓"} if b.get("lang") == "en" else {}
                self.send(200, push.test(me, b.get("endpoint"), b.get("id"), **text))
            else:
                self.send(404, {"error": "unbekannt"})
        except store.Error as e:
            self.send(400, {"error": str(e)})
        return True

    def _ai(self, method, p):
        """Settings for AI runs inside the app: provider, model, key, cost limits, household, runs."""
        me = self.user
        if p == "/api/ai/status" and method == "GET":  # for everyone: does the AI run inside the app, which language?
            s = ai.settings()
            self.send(200, {"provider": s["provider"], "language": s["language"], "running": jobs.current()})
            return True
        if me["role"] != "admin" or me["via"] != "session":
            self.send(403, {"error": "Nur für Admins"})
            return True
        try:
            m = re.fullmatch(r"/api/ai/runs/(\d+)", p)
            if p == "/api/ai" and method == "GET":
                self.send(200, {"settings": ai.settings(), "runs": ai.runs(), "today": round(ai.spent_today(), 4),
                                "running": jobs.current(), "anthropic_models": list(ai.ANTHROPIC_MODELS), "sdk": ai.sdk_version()})
            elif p == "/api/ai" and method == "POST":
                self.send(200, {"ok": True, "settings": ai.save(self.body())})
            elif p == "/api/ai/test" and method == "POST":
                self.send(200, ai.test())
            elif m and method == "GET":
                self.send(200, ai.run_log(int(m.group(1))))
            else:
                self.send(404, {"error": "unbekannt"})
        except ai.Error as e:
            self.send(400, {"error": str(e)})
        return True

    def _video(self, method, p):
        """Settings → Video & Whisper (video.py): on/off, Whisper address, API, key (never sent back), language, limits;
        test with the built-in test sound; update yt-dlp."""
        me = self.user
        if me["role"] != "admin" or me["via"] != "session":
            self.send(403, {"error": "Nur für Admins"})
            return True
        try:
            if p == "/api/video" and method == "GET":
                self.send(200, {"settings": video.settings(), "status": video.status()})
            elif p == "/api/video" and method == "POST":
                self.send(200, {"ok": True, "settings": video.save(self.body())})
            elif p == "/api/video/test" and method == "POST":
                self.send(200, video.test())
            elif p == "/api/video/update" and method == "POST":
                if self.body().get("confirm") is not True:  # it installs from PyPI: only on purpose
                    self.send(400, {"error": "confirm: true fehlt"})
                else:
                    self.send(200, video.update_ytdlp(force=True))
            else:
                self.send(404, {"error": "unbekannt"})
        except store.Error as e:
            self.send(400, {"error": str(e)})
        return True

    def _backup(self, method, p, setup=False):
        """Download a backup (GET /api/backup[?secrets=1]) and import one (POST /api/backup/import, raw .tar.gz body).
                setup=True: import on the start page of a new installation, as long as there is no account (/auth/setup/import)."""
        if not setup and not households.instance_admin(self.user):  # the whole installation, all households
            self.send(403, {"error": "Nur für Admins"})
            return True
        q = parse_qs(urlparse(self.path).query)
        tmp, sent = None, False
        try:
            if p == "/api/backup" and method == "GET" and not setup:
                tmp = os.path.join(backup.backups_dir(), f".download-{os.getpid()}-{threading.get_ident()}.tar.gz")
                backup.export(tmp, with_secrets=q.get("secrets", [""])[0] == "1")
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Length", str(os.path.getsize(tmp)))
                self.send_header("Content-Disposition", f'attachment; filename="{backup.filename()}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                sent = True
                with open(tmp, "rb") as f:
                    while chunk := f.read(1024 * 1024):
                        self.wfile.write(chunk)
            elif (p == "/api/backup/import" or setup) and method == "POST":
                if jobs.busy():
                    raise backup.Error("Gerade läuft ein KI-Lauf, bitte warten", 409)
                tmp = backup.receive(self.rfile, int(self.headers.get("Content-Length") or 0))
                r = backup.restore(tmp, actor=None if setup else self.user, session=None if setup else self.user.get("session"),
                                   after_init=init_all, lock=store.lock)
                shopping._bring.clear_all()  # credentials may have changed
                yazio._tok.clear_all()
                self.send(200, r)
            else:
                self.send(404, {"error": "unbekannt"})
        except backup.Error as e:
            self.close_connection = True  # a refused upload may still be unread on the connection
            self.send(e.code, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            print("Backup:", repr(e), flush=True)
            self.close_connection = True
            if not sent:
                self.send(500, {"error": f"Backup fehlgeschlagen: {e}"})
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        return True

    def _accounts(self, method, p):
        """Own account, managing accounts and API tokens (admins). True = answered."""
        me = self.user
        hid = me["household_id"]
        if p == "/api/me" and method == "GET":
            out = {**{k: me.get(k) for k in ("id", "username", "display_name", "email", "role", "via")}, "oidc": auth.oidc_enabled(),
                   "household": households.info(hid), "instance_admin": households.instance_admin(me)}
            if households.instance_admin(me):
                out["households"] = len(households.ids())
            if households.voter(me):
                out["prefs"] = households.prefs([me["id"]])[me["id"]]
            self.send(200, out)
            return True
        m = re.fullmatch(r"/api/members/(\d+)", p)
        if m and method == "POST":  # display name and preferences: your own, or any member's as admin
            uid = int(m.group(1))
            if not (households.voter(me) == uid or (me["role"] == "admin" and me["via"] == "session")):
                self.send(403, {"error": "Nur für Admins"})
                return True
            self.send(200, households.save_prefs(hid, uid, self.body()))
            return True
        if p.startswith("/api/households"):  # several households, instance admins only
            return self._households(method, p)
        if p.startswith("/api/freigaben") or re.fullmatch(r"/api/rezepte/[a-z0-9-]{1,120}/teilen", p):  # shared links
            return self._shares(method, p)
        if p.startswith("/api/invites"):  # invitations into this household, its admins
            if me["role"] != "admin" or me["via"] != "session":
                self.send(403, {"error": "Nur für Admins"})
                return True
            m = re.fullmatch(r"/api/invites(?:/(\d+)/revoke)?", p)
            if m and method == "GET" and not m.group(1):
                self.send(200, households.invites(hid))
            elif m and method == "POST" and m.group(1):
                self.send(200, households.revoke_invite(hid, int(m.group(1))))
            elif m and method == "POST":
                b = self.body()
                self.send(200, self._invite_out(households.create_invite(hid, me["id"], b.get("role") or "member", b.get("days") or 7, b.get("note") or "")))
            else:
                self.send(404, {"error": "unbekannt"})
            return True
        if p == "/api/me/password" and method == "POST":
            if me["via"] != "session":  # an API token must not change its creator's password (a token without account crashed here)
                self.send(403, {"error": "nicht erlaubt"})
                return True
            b = self.body()
            self.send(200, auth.change_own_password(me, b.get("old"), b.get("new")))
            return True
        if p == "/api/notify" and method == "POST":  # notification to every configured channel
            b = self.body()
            try:
                self.send(200, notify.send(b.get("text"), b.get("title") or "", view=str(b.get("view") or "")))
            except store.Error as e:
                self.send(400, {"error": str(e)})
            return True
        if p == "/api/household":  # household: language and description; everyone reads, admins change
            if method == "POST":  # also name and people (number of servings, null = number of members)
                if me["role"] != "admin" or me["via"] != "session":
                    self.send(403, {"error": "Nur für Admins"})
                    return True
                b = self.body()
                if "name" in b:
                    households.rename(hid, b["name"])
                self.send(200, {"ok": True, **household.save(b), **households.people(), "name": households.info(hid)["name"]})
            else:
                self.send(200, {**household.get(), **households.people(), "id": hid, "name": households.info(hid)["name"]})
            return True
        if p.startswith("/api/push/"):  # Web Push on this device, every signed-in member
            return self._push(method, p)
        if p.startswith("/api/integrations"):  # connections, admins only
            return self._integrations(method, p)
        if p.startswith("/api/ai"):  # AI inside the app, admins only
            return self._ai(method, p)
        if re.fullmatch(r"/api/video(/test|/update)?", p):  # video import and Whisper (video.py), admins only
            return self._video(method, p)
        if p.startswith("/api/backup"):  # backup and moving house, admins only
            return self._backup(method, p)
        if not p.startswith(("/api/users", "/api/tokens")):
            return False
        if me["role"] != "admin" or me["via"] != "session":
            self.send(403, {"error": "Nur für Admins"})
            return True
        # exactly the documented routes (openapi.py): /api/users[/<id>], /api/tokens[/<id>/revoke]
        m = re.fullmatch(r"/api/users(?:/(\d+))?", p) or re.fullmatch(r"/api/tokens(?:/(\d+)/revoke)?", p)
        if not m:
            self.send(404, {"error": "unbekannt"})
            return True
        resource, iid = p.split("/")[2], int(m.group(1)) if m.group(1) else None
        # always the accounts and tokens of the admin's own household (households.py)
        if method == "GET" and not iid:
            self.send(200, households.users(hid) if resource == "users" else households.tokens(hid))
        elif method == "POST" and resource == "users" and not iid:
            b = self.body()
            self.send(200, {"ok": True, "id": households.create_user(hid, b.get("username"), b.get("password"), b.get("display_name") or "",
                                                                    b.get("email") or "", b.get("role") or "member")})
        elif method == "POST" and resource == "users":
            self.send(200, households.update_user(hid, iid, me, self.body()))
        elif method == "POST" and resource == "tokens" and not iid:
            b = self.body()
            self.send(200, households.create_token(hid, b.get("name"), b.get("scope") or "api", me["id"]))
        elif method == "POST" and resource == "tokens":
            self.send(200, households.revoke_token(hid, iid))
        else:
            self.send(404, {"error": "unbekannt"})
        return True

    # ----- GET -----
    def _get(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        try:
            if p in ("/", "/index.html"):
                return self.static("index.html", cache=False)
            if p == "/manifest.json":
                return self.static("manifest.json", ctype="application/manifest+json")
            if p == "/sw.js":  # service worker: the version in it renews the offline cache with every release
                with open(os.path.join(STATIC, "sw.js"), encoding="utf-8") as fh:
                    js = fh.read()
                vf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
                version = open(vf, encoding="utf-8").read().strip() if os.path.isfile(vf) else "0"
                return self.send(200, js.replace("__KOMBUESE_VERSION__", re.sub(r"[^\w.+-]", "", version)), "application/javascript; charset=utf-8",
                                 {"Cache-Control": "no-cache"})
            m = re.fullmatch(r"/icon-(\d+)\.png", p)
            if p == "/icon.svg":
                return self.static("icon.svg", ctype="image/svg+xml")
            if m and int(m.group(1)) in (180, 192, 512):
                s = int(m.group(1))
                if os.path.isfile(os.path.join(STATIC, f"icon-{s}.png")):  # Kombüse logo (pot with anchor), from static/icon.svg
                    return self.static(f"icon-{s}.png", ctype="image/png")
                if s not in ICONS:
                    ICONS[s] = make_icon(s)
                return self.send(200, ICONS[s], "image/png", {"Cache-Control": "max-age=604800"})
            m = re.fullmatch(r"/img/([0-9]+\.[a-z]+)", p)
            if m:
                f = os.path.join(img_dir(), m.group(1))
                if os.path.isfile(f):
                    with open(f, "rb") as fh:
                        return self.send(200, fh.read(), mimetypes.guess_type(f)[0] or "image/jpeg",
                                         {"Cache-Control": "max-age=2592000"})
                return self.send(404, {"error": "kein Bild"})
            m = re.fullmatch(r"/mimg/([0-9a-f-]{36})/(original|min-original|tiny-original)\.webp", p)
            if m:  # recipe images (local in /data/rezeptbilder)
                f = store.image_file(m.group(1), small=m.group(2) != "original")
                if not f:
                    return self.send(404, {"error": "kein Bild"})
                with open(f[0], "rb") as fh:
                    return self.send(200, fh.read(), f[1], {"Cache-Control": "max-age=3600"})
            m = re.fullmatch(r"/rezept/([a-z0-9-]{1,120})\.pdf", p)
            if m:  # real PDF file (Gotenberg), ?portionen=4
                try:
                    portions = float(q.get("portionen", ["0"])[0] or 0) or None
                    r = store.recipe(m.group(1))
                    data = printing.pdf(m.group(1), portions and max(0.5, min(50.0, portions)), q.get("lang", ["de"])[0])
                    name = re.sub(r"[^A-Za-z0-9-]+", "-", store.slugify(r["name"]).title()) + ".pdf"
                    return self.send(200, data, "application/pdf", {"Content-Disposition": f'inline; filename="{name}"', "Cache-Control": "no-cache"})
                except store.Error as e:
                    return self.send(404, {"error": str(e)})
                except printing.Error as e:
                    return self.send(502, {"error": str(e)})
            m = re.fullmatch(r"/rezept/([a-z0-9-]{1,120})/druck", p)
            if m:  # print view / PDF (?portionen=4&drucken=1 opens the print dialog right away)
                try:
                    portions = float(q.get("portionen", ["0"])[0] or 0) or None
                    page = printing.recipe_page(recipes.detail(m.group(1)), portions and max(0.5, min(50.0, portions)), bool(q.get("drucken")), pdf=bool(q.get("pdf")), lang=q.get("lang", ["de"])[0])
                    return self.send(200, page, "text/html; charset=utf-8", {"Cache-Control": "no-cache"})
                except store.Error as e:
                    return self.send(404, {"error": str(e)})
            if p == "/kalender.ics":  # week plan for Home Assistant (Remote Calendar)
                meal_types = [t for t in q.get("typ", [""])[0].split(",") if t in store.MEAL_TYPES]  # ?typ=lunch,dinner
                return self.send(200, store.ical(meal_types or None), "text/calendar; charset=utf-8")
            if p == "/api/scrape":  # read a recipe page (AI runs, karte.py)
                try:
                    u = q.get("url", [""])[0]
                    return self.send(200, scrape.raw_recipe(u) if q.get("roh") else scrape.read_recipe(u))
                except scrape.Error as e:
                    return self.send(422, {"error": str(e)})
            if p == "/api/zutaten":
                return self.send(200, store.ingredients(q.get("suche", [""])[0]))
            if p == "/api/einheiten":
                return self.send(200, store.UNITS)
            if p == "/api/config":  # addresses of the installation for the app (KOMBUESE_URLS)
                return self.send(200, {**config.public(), "language": household.language()})
            if p == "/api/version":  # VERSION and CHANGELOG.md next to server.py
                d = os.path.dirname(os.path.abspath(__file__))
                read_file = lambda n: open(os.path.join(d, n), encoding="utf-8").read().strip() if os.path.isfile(os.path.join(d, n)) else ""  # noqa: E731
                en = q.get("lang", [""])[0] == "en"  # English changelog in CHANGELOG.en.md
                version = read_file("VERSION") or "?"
                return self.send(200, {"version": version, "autor": config.AUTHOR,
                                       "changelog": (en and read_file("CHANGELOG.en.md")) or read_file("CHANGELOG.md"),
                                       "update": updates.check(version),  # newer release, if known
                                       "source_url": config.SOURCE_URL if re.fullmatch(r"https?://\S+", config.SOURCE_URL) else "",
                                       "license": "AGPL-3.0-or-later"}, extra={"Cache-Control": "no-cache"})
            if p == "/i18n.js":  # starting language of new devices = household language
                with open(os.path.join(STATIC, "i18n.js"), "rb") as fh:
                    js = f"window.KOMBUESE_LANG = {json.dumps(household.language())};\n".encode() + fh.read()
                return self.send(200, js, "application/javascript; charset=utf-8", {"Cache-Control": "no-cache"})
            if p in ("/zxing.min.js", "/zxing-LIZENZ.txt", "/i18n.js"):  # barcode scanner (ZXing, Apache-2.0), language
                return self.static(p[1:], ctype="application/javascript" if p.endswith(".js") else "text/plain", cache=p != "/i18n.js")
            if p == "/api/schrank":
                check_purchases_in_background()
                return self.send(200, pantry.overview())
            if p == "/api/schrank/rezepte":  # own recipes by what is in cupboard and freezer (no AI)
                try:
                    return self.send(200, pantry.cookable(int(q.get("limit", ["30"])[0] or 30)))
                except ValueError:
                    return self.send(400, {"error": "limit: Zahl"})
            if p == "/api/fotoimport":  # state of the recipe from a photo
                with db() as con:
                    return self.send(200, photos.public(meta_get(con, "fotoimport", {"state": "idle"})))
            m = re.fullmatch(r"/api/fotoimport/bild/(\d{1,2})", p)
            if m:  # the image for an outside worker (or the app), only while the import waits or runs
                with db() as con:
                    st = meta_get(con, "fotoimport", {})
                f = photos.file(st, int(m.group(1))) if st.get("state") in ("requested", "running") else None
                if not f:
                    return self.send(404, {"error": "kein Bild"})
                with open(f[0], "rb") as fh:
                    return self.send(200, fh.read(), f[1], {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
            if p == "/api/kochideen":  # AI proposals what to cook from the pantry
                with db() as con:
                    return self.send(200, {"status": meta_get(con, "kochideen", {"state": "idle"}), "vorschlag": cook_ideas(con)})
            m = re.fullmatch(r"/api/barcode/(\d{6,14})", p)
            if m:
                try:
                    return self.send(200, pantry.barcode(m.group(1)))
                except store.Error as e:
                    return self.send(400, {"error": str(e)})
            if p.startswith("/api/einkauf"):
                try:
                    if p == "/api/einkauf":
                        with db() as con:
                            always = meta_get(con, "immer_da", shopping.ALWAYS_DEFAULT)
                        check_purchases_in_background()
                        store.reconcile()  # weigh the pantry against the list
                        return self.send(200, {**shopping.shopping_list(), "zugang": shopping.configured(), "immer": always})
                    if p == "/api/einkauf/test":
                        return self.send(200, shopping.bring_test())
                except shopping.Error as e:
                    return self.send(502, {"error": str(e), "zugang": shopping.configured()})
            if p == "/api/woche":
                try:
                    w = week.plan(q.get("start", [""])[0])
                    me = nutrition.person(self.user)
                    with db() as con:
                        rows = [dict(r) for r in con.execute("SELECT * FROM gegessen WHERE datum BETWEEN ? AND ?", (w["start"], w["ende"]))]
                    names = {m["id"]: m["name"] for m in households.members()}
                    for e in w["eintraege"]:  # gegessen = my entry (one per person), gegessen_von = everybody who ate it
                        mine = [r for r in rows if r["mealplan_id"] == e["id"]]
                        e["gegessen"] = next((r for r in mine if r["user_id"] == me), None)
                        e["gegessen_von"] = [{"id": r["user_id"], "name": names.get(r["user_id"], "?")} for r in mine]
                    w["yazio"] = yazio.configured()
                    return self.send(200, w)
                except shopping.Error as e:
                    return self.send(502, {"error": str(e)})
            if p.startswith("/api/rezepte"):
                try:
                    if p == "/api/rezepte":
                        su = q.get("suche", [""])[0].lower()
                        return self.send(200, [r for r in recipes.list_all() if not su or su in r["name"].lower() or su in (r["quelle"] or "").lower()])
                    m = re.fullmatch(r"/api/rezepte/([a-z0-9-]{1,120})", p)
                    if m:
                        return self.send(200, recipes.detail(m.group(1)))
                except shopping.Error as e:  # unknown or invalid recipe (a 404)
                    return self.send(404, {"error": str(e)})
            if p == "/api/rezept":
                try:
                    return self.send(200, week.recipe(q.get("slug", [""])[0]))
                except shopping.Error as e:
                    return self.send(502, {"error": str(e)})
            if p == "/api/mahlzeit":
                try:
                    return self.send(200, week.meal(q.get("id", [""])[0]))
                except shopping.Error as e:
                    return self.send(502, {"error": str(e)})
            if p == "/api/aufwaermen":  # reheating of a recipe (?slug=) or a plan entry (?eintrag=), ?portionen= &watt=
                try:
                    portions = float(q.get("portionen", ["1"])[0] or 1)
                    watt = int(q.get("watt", ["0"])[0] or 0) or None
                except ValueError:
                    return self.send(400, {"error": "ungültige Zahl"})
                try:
                    return self.send(200, recipes.reheating(q.get("slug", [""])[0], q.get("eintrag", [""])[0], portions, watt))
                except store.Error as e:
                    return self.send(404, {"error": str(e)})
            if p == "/api/aufwaermen/job":  # the AI job "add reheating tips": state and how many recipes lack the field
                with db() as con:
                    return self.send(200, {**meta_get(con, "aufwaermen", {"state": "idle"}), "ohne": sum(1 for r in recipes.list_all() if not r["aufwaermen"])})
            if p == "/api/ernaehrung":  # nutrition per person and day, ?start=YYYY-MM-DD&tage=7
                d = nutrition.overview(q.get("start", [""])[0], q.get("tage", ["7"])[0] or 7, nutrition.person(self.user))
                return self.send(200, {**d, "yazio": yazio.configured()})
            if p == "/api/yazio/tag":
                try:
                    d = q.get("date", [""])[0]
                    return self.send(200, yazio.day(d if DATE_RE.fullmatch(d) else now()[:10]))
                except shopping.Error as e:
                    return self.send(502, {"error": str(e), "zugang": shopping.configured()})
            with db() as con:
                if p == "/api/stack":
                    lim = min(int(q.get("limit", ["20"])[0]), 100)
                    meal = q.get("meal", [""])[0]
                    mine, args = self._not_voted()  # several members: only the cards this member has not swiped yet
                    if meal in MEALS:
                        rows = con.execute(f"SELECT * FROM cards WHERE status='neu' AND meal=?{mine} ORDER BY id LIMIT ?", (meal, *args, lim)).fetchall()
                    else:
                        rows = con.execute(f"SELECT * FROM cards WHERE status='neu'{mine} ORDER BY id LIMIT ?", (*args, lim)).fetchall()
                    return self.send(200, [card_out(r) for r in rows])
                if p == "/api/match":  # cards I liked that wait for the other members
                    ms, uid = households.members(), households.voter(self.user)
                    if not uid or len(ms) < 2:
                        return self.send(200, {"members": len(ms), "waiting": []})
                    w = households.waiting(con, uid, ms)
                    cards = {r["id"]: card_out(r) for r in con.execute(
                        f"SELECT * FROM cards WHERE id IN ({','.join('?' * len(w))})", [x["id"] for x in w])} if w else {}
                    return self.send(200, {"members": len(ms), "waiting": [{**cards[x["id"]], **x} for x in w if x["id"] in cards]})
                if p == "/api/likes":
                    where = "status IN ('like','mealie','fehler')"
                    if q.get("pending"):
                        where = "status='like'"
                    if q.get("plan"):
                        where += " AND plan=1"
                    rows = con.execute(f"SELECT * FROM cards WHERE {where} ORDER BY swiped DESC").fetchall()
                    return self.send(200, [card_out(r) for r in rows])
                if p == "/api/status":
                    return self.send(200, self.status(con))
                if p == "/api/work":  # every 5 min from the worker: also check whether something was checked off in Bring
                    check_purchases_in_background()
                    if ai.active():  # the app does the AI runs itself, an outside worker has nothing to do
                        idle = {"state": "idle"}
                        return self.send(200, {"likes_pending": 0, "last_swipe_age_s": None, "refill": idle, "stack": 0, "wochenplan": idle,
                                               "linkimport": idle, "mealprep": idle, "kochideen": idle, "fotoimport": idle, "aufwaermen": idle, "ai": "app"})
                    st = self.status(con)
                    last = meta_get(con, "last_swipe")
                    age = int(time.time() - datetime.fromisoformat(last).timestamp()) if last else None
                    return self.send(200, {"likes_pending": st["counts"].get("like", 0), "last_swipe_age_s": age,
                                           "refill": st["refill"], "stack": st["counts"].get("neu", 0),
                                           "wochenplan": st["wochenplan"], "linkimport": st["linkimport"], "mealprep": st["mealprep"],
                                           "kochideen": st["kochideen"], "fotoimport": st["fotoimport"],
                                           "aufwaermen": st["aufwaermen"],
                                           "language": household.language()})
                if p == "/api/wochenplan":
                    return self.send(200, {"status": meta_get(con, "wochenplan", {"state": "idle"}),
                                           "vorschlag": meta_get(con, "wochenplan_vorschlag")})
                if p == "/api/mealprep":  # ?datum= -> {status, ablauf, daten}
                    stored = meta_get(con, "mealprep", {})
                    return self.send(200, {"status": meta_get(con, "mealprep_job", {"state": "idle"}), "daten": sorted(stored),
                                           "ablauf": stored.get(q.get("datum", [""])[0])})
                if p == "/api/geschmack":
                    return self.send(200, taste(con))
                if p == "/api/history":
                    rows = con.execute(
                        "SELECT id, title, meal, tags, reason, status, plan, swiped, source, kcal, minutes, why, why_zutaten, why_text"
                        " FROM cards WHERE status!='neu' ORDER BY swiped DESC LIMIT ?",
                        (int(q.get("limit", ["200"])[0]),)).fetchall()
                    out = []
                    for r in rows:
                        d = dict(r)
                        d["tags"] = jl(d["tags"])
                        d["kcal"], d["minutes"] = int_or_none(d["kcal"]), int_or_none(d["minutes"])
                        d["status"] = "nope" if d["status"] == "nope" else "like"
                        d["why"] = ", ".join(WHY.get(c, c) for c in codes(d["why"]))
                        d["why_zutaten"] = jl(d["why_zutaten"])
                        out.append(d)
                    return self.send(200, out)
                if p == "/api/abneigungen":
                    return self.send(200, dislikes(con))
                if p == "/api/nopes":  # noes to fill in: reason "zutat" (ingredient), but no ingredient and no text
                    where = "status='nope'"
                    if q.get("offen"):
                        where += " AND ','||why||',' LIKE '%,zutat,%' AND why_zutaten IN ('', '[]') AND why_text=''"
                    rows = con.execute(f"SELECT * FROM cards WHERE {where} ORDER BY swiped DESC LIMIT ?",
                                       (min(int(q.get("limit", ["50"])[0]), 200),)).fetchall()
                    return self.send(200, [card_out(r) for r in rows])
                if p == "/api/gruende":
                    return self.send(200, WHY)
                if p == "/api/vorrat":
                    return self.send(200, freezer(con))
                if p == "/api/known-urls":
                    return self.send(200, [r["url"] for r in con.execute("SELECT url FROM cards")])
            return self.send(404, {"error": "unbekannt"})
        except Exception as e:  # noqa: BLE001
            return self.send(500, {"error": str(e)})

    def _add_item(self, text, client_id=None):
        """Add to the shopping list. With client_id (the offline queue of the app) the same id sent again
        returns the first answer instead of adding the item twice (e.g. when the answer got lost in a dead spot)."""
        cid = str(client_id or "")
        if not re.fullmatch(r"[\w-]{8,64}", cid):
            return store.add_item(text)
        with lock:
            with db() as con:
                seen = meta_get(con, "einkauf_client_ids", {})
            if cid in seen:
                return {**seen[cid]["r"], "repeat": True}
            r = store.add_item(text)
            seen[cid] = {"r": r, "at": now()}
            if len(seen) > 300:  # the last 300 are plenty, the queue syncs within days at most
                seen = dict(sorted(seen.items(), key=lambda x: x[1]["at"])[-300:])
            with db() as con:
                meta_set(con, "einkauf_client_ids", seen)
            return r

    def _not_voted(self):
        """SQL condition for cards this member has not swiped yet, when the household has several members (match)."""
        uid = households.voter(self.user)
        if uid and len(households.member_ids()) > 1:
            return " AND id NOT IN (SELECT card_id FROM card_votes WHERE user_id=?)", (uid,)
        return "", ()

    def status(self, con):
        counts = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM cards GROUP BY status")}
        plan = con.execute("SELECT COUNT(*) n FROM cards WHERE plan=1 AND status!='nope'").fetchone()["n"]
        mine, args = self._not_voted()
        stack = {r["meal"]: r["n"] for r in con.execute(f"SELECT meal, COUNT(*) n FROM cards WHERE status='neu'{mine} GROUP BY meal", args)}
        out = {"counts": counts, "plan": plan, "stack": stack, "refill": meta_get(con, "refill", {"state": "idle"}),
               "wochenplan": meta_get(con, "wochenplan", {"state": "idle"}), "linkimport": meta_get(con, "linkimport", {"state": "idle"}),
               "mealprep": meta_get(con, "mealprep_job", {"state": "idle"}),
               "kochideen": meta_get(con, "kochideen", {"state": "idle"}),
               "fotoimport": photos.public(meta_get(con, "fotoimport", {"state": "idle"})),
               "aufwaermen": meta_get(con, "aufwaermen", {"state": "idle"})}
        if mine:  # several members: how many of my likes wait for the others
            out["match"] = {"members": len(households.member_ids()), "waiting": con.execute(
                "SELECT COUNT(*) FROM card_votes v JOIN cards k ON k.id = v.card_id WHERE v.user_id=? AND v.vote IN ('like','plan') AND k.status='neu'",
                args).fetchone()[0]}
        return out

    def static(self, name, ctype=None, cache=True):
        f = os.path.join(STATIC, name)
        with open(f, "rb") as fh:
            data = fh.read()
        self.send(200, data, ctype or (mimetypes.guess_type(f)[0] or "text/plain") + ("; charset=utf-8" if name.endswith((".html", ".js", ".css")) else ""),
                  {"Cache-Control": "max-age=3600" if cache else "no-cache"})

    def _photo_import(self, p):
        """Recipe from a photo. POST /api/fotoimport {bilder: [{daten: base64}], hinweis} queues the job,
        /api/fotoimport/state {state, note, slug, name} from the run, /api/fotoimport/bild {slug, bild} uses image nr as
        the recipe photo, /api/fotoimport/abbrechen drops a waiting import. Images are deleted when the job ends."""
        n = int(self.headers.get("Content-Length") or 0)
        if n > 32 * 1024 * 1024:
            self.close_connection = True
            return self.send(413, {"error": "Bilder zu groß"})
        try:
            b = self.body(32 * 1024 * 1024)
        except ValueError:
            return self.send(400, {"error": "kein JSON"})
        if not isinstance(b, dict):
            return self.send(400, {"error": "kein JSON"})
        lang = household.language()
        try:
            if p == "/api/fotoimport":
                with db() as con:
                    cur = meta_get(con, "fotoimport", {"state": "idle"})
                if cur.get("state") in ("requested", "running"):
                    return self.send(409, {"error": "Es läuft schon ein Foto-Import", **photos.public(cur)})
                images = photos.decode(b.get("bilder"))
                key, meta = photos.save(images)
                st = {"state": "requested", "requested": now(), "hinweis": str(b.get("hinweis") or "").strip()[:300],
                      "bilder": meta, "bilder_key": key}
                with lock, db() as con:
                    meta_set(con, "fotoimport", st)
                jobs.wake()
                return self.send(200, photos.public(st))
            if p == "/api/fotoimport/state":
                with lock, db() as con:
                    cur = meta_get(con, "fotoimport", {"state": "idle"})
                    cur.update({k: str(b.get(k) or "")[:300] for k in ("state", "note", "slug", "name")}, changed=now())
                    note = cur["note"]
                    if cur["state"] == "failed" and (note.startswith("NO_IMAGE") or ai.NO_VISION in note):
                        cur["grund"], cur["note"] = "vision", re.sub(r"^NO_IMAGE[:\s]*", "", note)
                    elif cur["state"] == "failed" and note.startswith("NO_RECIPE"):
                        cur["grund"], cur["note"] = "kein_rezept", re.sub(r"^NO_RECIPE[:\s]*", "", note)
                    meta_set(con, "fotoimport", cur)
                if cur["state"] not in ("requested", "running"):
                    if cur["state"] == "done" and cur.get("slug"):
                        photos.mark_recipe(cur["slug"], len(cur.get("bilder") or []) or 1, lang)
                    photos.clear()
                return self.send(200, photos.public(cur))
            if p == "/api/fotoimport/bild":
                with db() as con:
                    cur = meta_get(con, "fotoimport", {})
                try:
                    nr = int(b.get("bild"))
                except (TypeError, ValueError):
                    return self.send(400, {"error": "bild: Nummer des Bildes (1 = erstes)"})
                f = photos.file(cur, nr) if cur.get("state") in ("requested", "running") else None
                if not f:
                    return self.send(404, {"error": "kein Bild mit dieser Nummer"})
                with open(f[0], "rb") as fh:
                    return self.send(200, {"ok": True, "bild": store.save_image(str(b.get("slug") or ""), fh.read())})
            if p == "/api/fotoimport/abbrechen":
                with lock, db() as con:
                    cur = meta_get(con, "fotoimport", {"state": "idle"})
                    since = cur.get("changed") or cur.get("requested") or ""
                    try:
                        recent = datetime.now(timezone.utc) - datetime.fromisoformat(since) < timedelta(minutes=30)
                    except ValueError:
                        recent = False
                    if cur.get("state") == "running" and recent:  # a run that hangs for more than 30 min may be dropped
                        return self.send(409, {"error": "Der Import läuft schon"})
                    meta_set(con, "fotoimport", {"state": "idle", "changed": now()})
                photos.clear()
                return self.send(200, {"ok": True})
        except store.Error as e:
            return self.send(400, {"error": str(e)})
        return self.send(404, {"error": "unbekannt"})

    # ----- POST -----
    def _post(self):
        p = urlparse(self.path).path
        try:
            m = re.fullmatch(r"/api/rezepte/([a-z0-9-]{1,120})/bild", p)
            if m and not (self.headers.get("Content-Type") or "").startswith("application/json"):
                n = int(self.headers.get("Content-Length") or 0)  # image directly as the body (image/jpeg, image/png, image/webp)
                if n > 15 * 1024 * 1024:
                    return self.send(413, {"error": "Bild zu groß"})
                try:
                    return self.send(200, {"ok": True, "bild": store.save_image(m.group(1), self.rfile.read(n))})
                except store.Error as e:
                    return self.send(400, {"error": str(e)})
            if p.startswith("/api/fotoimport"):  # recipe from a photo, images up to 20 MB as base64
                return self._photo_import(p)
            b = self.body()
            if m:  # {"url": "..."}: fetch the image and save it
                try:
                    return self.send(200, {"ok": True, "bild": store.save_image(m.group(1), scrape.download_image(str(b.get("url") or "")))})
                except (store.Error, scrape.Error) as e:
                    return self.send(400, {"error": str(e)})
            if p == "/api/videoimport/bild":  # {slug, video, bild}: a still of read_video as the recipe photo (video.py)
                try:
                    return self.send(200, {"ok": True, "bild": video.save_frame(b)})
                except store.Error as e:
                    return self.send(404 if "Standbild" in str(e) else 400, {"error": str(e)})
            if p == "/api/rezepte":  # new recipe (AI runs, app)
                try:
                    r = recipes.save(None, b)
                    return self.send(200, {"ok": True, "slug": r["slug"], "id": r["id"]})
                except store.Error as e:
                    return self.send(400, {"error": str(e)})
            m = re.fullmatch(r"/api/rezepte/([a-z0-9-]{1,120})", p)
            if m:  # change a recipe: only the fields sent
                try:
                    r = recipes.save(m.group(1), b)
                    return self.send(200, {"ok": True, "slug": r["slug"]})
                except store.Error as e:
                    return self.send(400, {"error": str(e)})
            if p == "/api/plan":  # {datum, typ, slug | titel, text, scale}
                try:
                    return self.send(200, {"ok": True, "id": store.add_plan_entry(str(b.get("datum") or ""), str(b.get("typ") or ""), b.get("slug") or None,
                                                                         b.get("titel") or "", b.get("text") or "", b.get("scale") or 1)})
                except store.Error as e:
                    return self.send(400, {"error": str(e)})
            m = re.fullmatch(r"/api/plan/(\d+)/weg", p)
            if m:
                return self.send(200, {"ok": True, "geloescht": store.delete_plan_entry(int(m.group(1)))})
            if p == "/api/zutaten":  # {name, kategorie}: set the supermarket category
                n, k = str(b.get("name") or "").strip()[:120], str(b.get("kategorie") or "").strip()[:60]
                if not n or not k:
                    return self.send(400, {"error": "name und kategorie"})
                store.set_category(n, k)
                return self.send(200, {"ok": True})
            m = re.fullmatch(r"/api/gegessen(?:/(-?\d+)/weg)?", p)
            if m:
                try:
                    # kept in Kombüse for every person (nutrition.py), Yazio only in addition when connected
                    me = nutrition.person(self.user)
                    member_ids = households.member_ids()
                    if m.group(1):  # undo: my entry ({"person": id} someone else's, {"alle": true} everybody's), in Yazio too
                        mid = int(m.group(1))
                        who = b.get("person", me)
                        with db() as con:
                            rows = con.execute("SELECT * FROM gegessen WHERE mealplan_id=?" + ("" if b.get("alle") else " AND user_id IS ?"),
                                               (mid,) if b.get("alle") else (mid, who)).fetchall()
                        yazio_error = None
                        ids = [r["yazio_id"] for r in rows if r["yazio_id"]]
                        if ids and yazio.configured():
                            try:
                                yazio.delete(ids)
                            except shopping.Error as e:
                                yazio_error = str(e)
                        with lock, db() as con:
                            con.executemany("DELETE FROM gegessen WHERE id=?", [(r["id"],) for r in rows])
                        return self.send(200, {"ok": True, "geloescht": len(rows), **({"yazio_fehler": yazio_error} if yazio_error else {})})
                    people = b.get("personen")  # who ate it: ids of members, default the person asking
                    if people:
                        try:
                            people = list(dict.fromkeys(int(x) for x in people))
                        except (TypeError, ValueError):
                            people = [0]
                        if any(x not in member_ids for x in people):
                            return self.send(400, {"error": "Person nicht gefunden"})
                    else:
                        people = [me]
                    portions = max(0.25, min(10.0, float(b.get("portionen") or 1)))
                    if b.get("slug"):  # without a plan entry, e.g. from the recipe menu: own negative ID
                        mid = -int(time.time() * 1000)
                        day = str(b.get("datum") or "")
                        kind = b.get("typ") if b.get("typ") in ("breakfast", "lunch", "dinner", "snack") else "snack"
                        meal_info = {**week.recipe(str(b["slug"])), "datum": day if DATE_RE.fullmatch(day) else now()[:10], "typ": kind}
                    else:
                        mid = int(b.get("mealplan_id") or 0)
                        meal_info = week.meal(mid)
                        day = str(b.get("datum") or "")  # a planned meal eaten on another day (e.g. tomorrow's breakfast today)
                        if DATE_RE.fullmatch(day):
                            meal_info["datum"] = day
                    with db() as con:
                        before = con.execute("SELECT user_id, yazio_id FROM gegessen WHERE mealplan_id=?", (mid,)).fetchall()
                    people = [x for x in people if x not in {r["user_id"] for r in before}]
                    if not people:
                        return self.send(409, {"error": "Schon eingetragen"})
                    pp = dict(meal_info["pro_portion"])
                    if b.get("reis"):
                        pp = {k: (v or 0) + week.RICE[k] for k, v in pp.items()}
                    nw = {k: (v * portions if v is not None else None) for k, v in pp.items()}
                    name = meal_info["name"] + (" mit Reis" if b.get("reis") else "") + ("" if portions == 1 else f" ({str(portions).rstrip('0').rstrip('.').replace('.', ',')} Portionen)")
                    # Yazio is one account: the person set for it (meta yazio_person), else the first one per plan entry
                    yazio_for, yid, yazio_error = None, None, None
                    if yazio.configured():
                        target = nutrition.yazio_person()
                        if target in people:
                            yazio_for = target
                        elif target is None and not any(r["yazio_id"] for r in before):
                            yazio_for = people[0]
                    if yazio_for is not None or (yazio.configured() and people == [None]):
                        try:
                            yid = yazio.log_meal(name, meal_info["datum"], meal_info["typ"], nw)
                        except shopping.Error as e:
                            yazio_error = str(e)
                    with lock, db() as con:
                        for who in people:
                            con.execute("INSERT INTO gegessen(mealplan_id,user_id,datum,typ,name,slug,portionen,kcal,protein,fett,kh,yazio_id,am)"
                                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                        (mid, who, meal_info["datum"], meal_info["typ"], meal_info["name"], meal_info["slug"], portions,
                                         nw["kcal"], nw["protein"], nw["fett"], nw["kh"], yid if who == yazio_for else None, now()))
                    try:
                        stars = recipes.rating(meal_info["slug"])
                    except shopping.Error:
                        stars = None
                    try:  # cooking day in the plan: ingredients out of the pantry (once per entry, not twice after meal prep either)
                        pantry_result = pantry.cook_plan_entry(mid) if mid > 0 else None
                    except Exception:  # noqa: BLE001
                        pantry_result = None
                    return self.send(200, {"ok": True, "id": mid, "name": name, "slug": meal_info["slug"], "sterne": stars, "schrank": pantry_result,
                                           "personen": people, "yazio": bool(yid), **({"yazio_fehler": yazio_error} if yazio_error else {}),
                                           **{k: round(v) if v else v for k, v in nw.items()}})
                except shopping.Error as e:
                    return self.send(502, {"error": str(e)})
            if p in ("/api/ernaehrung/ziel", "/api/ernaehrung/yazio"):  # goals per person, whose meals go to Yazio
                me = self.user
                admin = me["via"] == "session" and me["role"] == "admin"
                try:
                    if p.endswith("/yazio"):
                        if not admin:
                            return self.send(403, {"error": "Nur für Admins"})
                        return self.send(200, nutrition.set_yazio_person(int(b["user_id"]) if b.get("user_id") else None))
                    uid = int(b.get("user_id") or nutrition.person(me) or 0)
                    if not (admin or (me["via"] in ("session", "token") and me["id"] == uid)):
                        return self.send(403, {"error": "Nur für Admins"})
                    return self.send(200, nutrition.save_goal(uid, b))
                except (store.Error, ValueError) as e:
                    return self.send(400, {"error": str(e)})
            m = re.fullmatch(r"/api/rezepte/([a-z0-9-]{1,120})/(einkauf|plan|sterne)", p)
            if m:
                try:
                    if m.group(2) == "sterne":
                        return self.send(200, recipes.rate(m.group(1), b.get("sterne") or 0))
                    portions = max(0.5, min(20.0, float(b.get("portionen") or 2)))
                    if m.group(2) == "einkauf":
                        with db() as con:
                            always = meta_get(con, "immer_da", shopping.ALWAYS_DEFAULT)
                        return self.send(200, recipes.to_shopping_list(m.group(1), portions, always))
                    return self.send(200, recipes.schedule(m.group(1), str(b.get("datum") or ""), str(b.get("typ") or ""), portions))
                except shopping.Error as e:
                    return self.send(502, {"error": str(e)})
            if p.startswith(("/api/schrank", "/api/barcode")):
                try:
                    if p == "/api/schrank":  # {name, menge, einheit, kategorie, notiz}
                        return self.send(200, pantry.add(b.get("name"), b.get("menge"), b.get("einheit") or "", b.get("kategorie"), b.get("notiz") or ""))
                    m = re.fullmatch(r"/api/schrank/(\d+)", p)
                    if m:  # {menge, einheit, leer, notiz}
                        return self.send(200, pantry.set_amount(int(m.group(1)), b.get("menge"), b.get("einheit"), bool(b.get("leer")), b.get("notiz")))
                    if p == "/api/schrank/rueckgaengig":
                        return self.send(200, pantry.undo(int(b.get("buchung") or 0)))
                    if p == "/api/schrank/kochen":  # {plan_id} or {slug, portionen}
                        if b.get("plan_id"):
                            return self.send(200, {"ok": True, "schrank": pantry.cook_plan_entry(int(b["plan_id"]))})
                        return self.send(200, {"ok": True, "schrank": pantry.cook_free(str(b.get("slug") or ""), b.get("portionen"))})
                    if p == "/api/schrank/mealprep":  # {datum, slugs}
                        return self.send(200, {"ok": True, "schrank": pantry.cook_mealprep(str(b.get("datum") or ""), [str(s) for s in b.get("slugs") or []])})
                    if p == "/api/schrank/rezepte/einkauf":  # {slug, portionen}: only what is missing onto the list
                        with db() as con:
                            always = meta_get(con, "immer_da", shopping.ALWAYS_DEFAULT)
                        return self.send(200, pantry.missing_to_list(str(b.get("slug") or ""), b.get("portionen"), always))
                    if p == "/api/schrank/kauf":  # check Bring right now
                        return self.send(200, pantry.check_purchases(shopping.bring_state, force=True) or {"gebucht": []})
                    if p == "/api/barcode":  # {code, zutat, menge, einheit, aktion: rein|leer|merken, kategorie, produkt, marke}
                        return self.send(200, pantry.book_barcode(b.get("code"), b.get("zutat"), b.get("menge"), b.get("einheit"),
                                                                     b.get("aktion") or "merken", b.get("kategorie"), b.get("produkt") or "", b.get("marke") or ""))
                except (store.Error, ValueError) as e:
                    return self.send(400, {"error": str(e)})
                except shopping.Error as e:
                    return self.send(502, {"error": str(e)})
            if p == "/api/einkauf/immer":  # {"name": "Zimt", "aktion": "add"|"remove"}
                n = re.sub(r"\s+", " ", str(b.get("name") or "")).strip()[:60]
                with lock, db() as con:
                    im = meta_get(con, "immer_da", shopping.ALWAYS_DEFAULT)
                    if n and b.get("aktion") == "add" and n.lower() not in {x.lower() for x in im["namen"]}:
                        im["namen"].append(n)
                    if n and b.get("aktion") == "remove":
                        im["namen"] = [x for x in im["namen"] if x.lower() != n.lower()]
                        im["kategorien"] = [x for x in im["kategorien"] if x.lower() != n.lower()]
                    meta_set(con, "immer_da", im)
                return self.send(200, im)
            if p.startswith("/api/einkauf/"):
                try:
                    if p == "/api/einkauf/neu":  # {text: "2 l Milch", client_id}: client_id makes a repeat harmless
                        return self.send(200, self._add_item(str(b.get("text") or "")[:200], b.get("client_id")))
                    if p == "/api/einkauf/weg":  # {ids: [..]}
                        store.remove_items([int(i) for i in b.get("ids") or []])
                        return self.send(200, {"ok": True})
                    if p == "/api/einkauf/rezepte":  # {rezepte: [{slug, scale}]} added ready for shopping
                        with db() as con:
                            always = meta_get(con, "immer_da", shopping.ALWAYS_DEFAULT)
                        return self.send(200, store.add_to_list([(str(x.get("slug")), float(x.get("scale") or 1)) for x in b.get("rezepte") or []], always))
                    if p == "/api/einkauf/check":
                        return self.send(200, shopping.check_item(str(b.get("id")), b.get("checked")))
                    if p == "/api/einkauf/bring":
                        return self.send(200, shopping.send_to_bring())
                except shopping.Error as e:
                    return self.send(502, {"error": str(e)})
            with lock, db() as con:
                if p == "/api/swipe":
                    action = b.get("action")
                    if action not in ("like", "nope", "plan"):
                        return self.send(400, {"error": "action like|nope|plan"})
                    r = con.execute("SELECT status FROM cards WHERE id=?", (b.get("id"),)).fetchone()
                    if not r:
                        return self.send(404, {"error": "Karte fehlt"})
                    if r["status"] != "neu":
                        return self.send(409, {"error": "schon gewischt"})
                    uid = households.voter(self.user)
                    ms = households.members() if uid else []
                    if uid and len(ms) > 1 and uid in {x["id"] for x in ms}:  # match: a like only when all members liked it
                        res = households.vote(con, int(b["id"]), uid, action, ms)
                        if res is None:
                            return self.send(409, {"error": "schon gewischt"})
                        meta_set(con, "last_swipe", now())
                        return self.send(200, {"ok": True, **res})
                    con.execute("UPDATE cards SET status=?, plan=?, swiped=? WHERE id=?",
                                ("nope" if action == "nope" else "like", 1 if action == "plan" else 0, now(), b["id"]))
                    if uid:
                        households.record(con, int(b["id"]), uid, action)
                    meta_set(con, "last_swipe", now())
                    return self.send(200, {"ok": True})
                if p == "/api/undo":
                    r = con.execute("SELECT status FROM cards WHERE id=?", (b.get("id"),)).fetchone()
                    uid = households.voter(self.user)
                    mine = uid and r and con.execute("SELECT 1 FROM card_votes WHERE card_id=? AND user_id=?", (b["id"], uid)).fetchone()
                    if r and r["status"] == "neu" and mine:  # match: take back my vote, the others' stay
                        con.execute("DELETE FROM card_votes WHERE card_id=? AND user_id=?", (b["id"], uid))
                        return self.send(200, {"ok": True})
                    if not r or r["status"] not in ("like", "nope", "fehler"):
                        return self.send(409, {"error": "nicht rueckgaengig zu machen (schon in Mealie?)"})
                    con.execute("UPDATE cards SET status='neu', plan=0, swiped=NULL, why='', why_zutaten='[]', why_text='', why_at=NULL"
                                " WHERE id=?", (b["id"],))
                    if uid:
                        con.execute("DELETE FROM card_votes WHERE card_id=? AND user_id=?", (b["id"], uid))
                    return self.send(200, {"ok": True})
                m = re.fullmatch(r"/api/cards/(\d+)/plan", p)
                if m:
                    con.execute("UPDATE cards SET plan=? WHERE id=? AND status IN ('like','mealie','fehler')",
                                (1 if b.get("plan") else 0, int(m.group(1))))
                    return self.send(200, {"ok": True})
                m = re.fullmatch(r"/api/cards/(\d+)/why", p)
                if m:
                    # {"why": ["zutat","schwer"] or "zutat", "zutaten": ["Austernpilze"], "text": "..."}
                    w = b.get("why", [])
                    w = codes(w) if isinstance(w, str) else [str(x) for x in w or []]
                    bad = [x for x in w if x not in WHY]
                    if bad:
                        return self.send(400, {"error": "why: " + "|".join(WHY)})
                    ingr, seen = [], set()
                    for z in b.get("zutaten") or []:
                        z = re.sub(r"\s+", " ", str(z)).strip()[:60]
                        if z and z.lower() not in seen:
                            seen.add(z.lower())
                            ingr.append(z)
                    if ingr and "zutat" not in w:
                        w.insert(0, "zutat")
                    con.execute("UPDATE cards SET why=?, why_zutaten=?, why_text=?, why_at=? WHERE id=? AND status='nope'",
                                (",".join(dict.fromkeys(w)), json.dumps(ingr[:10], ensure_ascii=False),
                                 (b.get("text") or "").strip()[:300], now_ms(), int(m.group(1))))
                    return self.send(200, {"ok": True})
                if p == "/api/vorrat":  # new: {name, anzahl, einheit, kategorie, eingefroren, haltbar_bis, notiz, recipe_slug}
                    f = freezer_fields(b)
                    if not f.get("name") or not f.get("anzahl"):
                        return self.send(400, {"error": "name und anzahl > 0"})
                    f.setdefault("eingefroren", now()[:10])
                    f["geaendert"] = now()
                    con.execute(f"INSERT INTO vorrat({','.join(f)}) VALUES({','.join('?' * len(f))})", tuple(f.values()))
                    return self.send(200, freezer(con))
                m = re.fullmatch(r"/api/vorrat/(\d+)", p)
                if m:  # {"delta": -1} or change fields; at 0 the entry is gone
                    vid = int(m.group(1))
                    r = con.execute("SELECT anzahl FROM vorrat WHERE id=?", (vid,)).fetchone()
                    if not r:
                        return self.send(404, {"error": "Eintrag fehlt"})
                    f = freezer_fields(b)
                    if "delta" in b:
                        f["anzahl"] = max(0.0, round(r["anzahl"] + float(b["delta"]), 2))
                    if f.get("name") == "":
                        f.pop("name")
                    if f:
                        f["geaendert"] = now()
                        con.execute(f"UPDATE vorrat SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), vid))
                    con.execute("DELETE FROM vorrat WHERE id=? AND anzahl <= 0", (vid,))
                    return self.send(200, freezer(con))
                if p == "/api/abneigungen":  # {"zutat": "Kichererbsen", "aktion": "add"|"remove"}
                    z = re.sub(r"\s+", " ", str(b.get("zutat") or "")).strip()[:60]
                    if not z or b.get("aktion") not in ("add", "remove"):
                        return self.send(400, {"error": "zutat und aktion add|remove"})
                    if b["aktion"] == "add":
                        con.execute("INSERT INTO abneigung(key,name,manuell,entfernt) VALUES(?,?,1,NULL)"
                                    " ON CONFLICT(key) DO UPDATE SET manuell=1, entfernt=NULL, name=excluded.name", (z.lower(), z))
                    else:
                        con.execute("INSERT INTO abneigung(key,name,manuell,entfernt) VALUES(?,?,0,?)"
                                    " ON CONFLICT(key) DO UPDATE SET manuell=0, entfernt=excluded.entfernt", (z.lower(), z, now_ms()))
                    return self.send(200, dislikes(con))
                m = re.fullmatch(r"/api/cards/(\d+)/edit", p)
                if m:  # correct a card (title, reason, kcal …) so nobody has to edit the database
                    allowed = {"title": str, "reason": str, "description": str, "tags": list, "kcal": int,
                               "kcal_est": int, "minutes": int, "servings": str, "meal": str, "zutaten": list}
                    sets, vals = [], []
                    for k, kind in allowed.items():
                        if k in b:
                            v = b[k]
                            if k == "meal" and v not in MEALS:
                                return self.send(400, {"error": "meal: " + "|".join(MEALS)})
                            v = json.dumps(v, ensure_ascii=False) if kind is list else (kind(v) if v is not None else None)
                            sets.append(f"{k}=?")
                            vals.append(v)
                    if not sets:
                        return self.send(400, {"error": "Felder: " + ", ".join(allowed)})
                    con.execute(f"UPDATE cards SET {', '.join(sets)} WHERE id=?", (*vals, int(m.group(1))))
                    return self.send(200, {"ok": True})
                m = re.fullmatch(r"/api/cards/(\d+)/meal", p)
                if m:
                    if b.get("meal") not in MEALS:
                        return self.send(400, {"error": "meal: " + "|".join(MEALS)})
                    con.execute("UPDATE cards SET meal=? WHERE id=?", (b["meal"], int(m.group(1))))
                    return self.send(200, {"ok": True})
                m = re.fullmatch(r"/api/cards/(\d+)/kcal", p)
                if m:
                    con.execute("UPDATE cards SET kcal=?, kcal_est=? WHERE id=?",
                                (int(b["kcal"]) if b.get("kcal") else None, 1 if b.get("geschaetzt", True) else 0, int(m.group(1))))
                    return self.send(200, {"ok": True})
                if p == "/api/geschmack":
                    meta_set(con, "profil", {"text": (b.get("text") or "").strip()[:1500], "stand": now()})
                    return self.send(200, {"ok": True})
                m = re.fullmatch(r"/api/cards/(\d+)/(mealie|fehler|nope)", p)
                if m:
                    cid, what = int(m.group(1)), m.group(2)
                    if what == "mealie":
                        # security review: the slug goes into the app's links; an AI run reading a hostile page sends it
                        if not re.fullmatch(r"[a-z0-9-]{1,120}", str(b.get("slug") or "")):
                            return self.send(400, {"error": "slug ungültig"})
                        con.execute("UPDATE cards SET status='mealie', mealie_slug=?, imported=?, note='' WHERE id=?",
                                    (b.get("slug"), now(), cid))
                    elif what == "fehler":
                        con.execute("UPDATE cards SET status='fehler', note=? WHERE id=?", (b.get("note", ""), cid))
                    else:  # "Doch nicht" (changed my mind) in the saved list
                        con.execute("UPDATE cards SET status='nope', plan=0 WHERE id=? AND status IN ('like','fehler')", (cid,))
                    return self.send(200, {"ok": True})
                # ----- recipe from a link (AI run via the worker, like via Telegram) -----
                if p == "/api/linkimport":  # {url, hinweis}
                    cur = meta_get(con, "linkimport", {"state": "idle"})
                    if cur.get("state") in ("requested", "running"):
                        return self.send(200, cur)
                    url = str(b.get("url") or "").strip()
                    if not re.fullmatch(r"https?://\S{4,500}", url):
                        return self.send(400, {"error": "Bitte einen Link (http…) einfügen"})
                    st = {"state": "requested", "requested": now(), "url": url, "hinweis": (b.get("hinweis") or "").strip()[:300]}
                    meta_set(con, "linkimport", st)
                    return self.send(200, st)
                if p == "/api/linkimport/state":  # from the run: {state, note, slug, name}
                    cur = meta_get(con, "linkimport", {"state": "idle"})
                    cur.update({k: str(b.get(k) or "")[:300] for k in ("state", "note", "slug", "name")}, changed=now())
                    meta_set(con, "linkimport", cur)
                    return self.send(200, cur)
                # ----- what to cook from the pantry (AI run) -----
                if p == "/api/kochideen":  # {wunsch}
                    cur = meta_get(con, "kochideen", {"state": "idle"})
                    if cur.get("state") in ("requested", "running"):
                        return self.send(200, cur)
                    st = {"state": "requested", "requested": now(), "wunsch": (b.get("wunsch") or "").strip()[:300]}
                    meta_set(con, "kochideen", st)
                    jobs.wake()
                    return self.send(200, st)
                if p == "/api/kochideen/state":  # from the run: {state, note}
                    cur = meta_get(con, "kochideen", {"state": "idle"})
                    cur.update({"state": str(b.get("state") or "idle")[:20], "note": str(b.get("note") or "")[:300], "changed": now()})
                    meta_set(con, "kochideen", cur)
                    return self.send(200, cur)
                if p == "/api/kochideen/vorschlag":  # from the AI: {zusammenfassung, ideen: [{slug | karte, titel, grund, nutzt, fehlt}]}
                    try:
                        v = cook_ideas_check(con, b)
                    except ValueError as x:
                        return self.send(400, {"error": str(x)})
                    cur = meta_get(con, "kochideen", {"state": "idle"})
                    v["wunsch"] = cur.get("wunsch", "")
                    meta_set(con, "kochideen_vorschlag", v)
                    cur.update({"state": "done", "changed": now(), "note": ""})
                    meta_set(con, "kochideen", cur)
                    return self.send(200, {"ok": True, "ideen": len(v["ideen"]), "fehler": v["fehler"]})
                # ----- reheating tips for the recipes that have none (AI run) -----
                if p == "/api/aufwaermen/job":  # {alle: true} = also overwrite recipes that already have the field
                    cur = meta_get(con, "aufwaermen", {"state": "idle"})
                    if cur.get("state") in ("requested", "running"):
                        return self.send(200, cur)
                    st = {"state": "requested", "requested": now(), "alle": b.get("alle") is True}
                    meta_set(con, "aufwaermen", st)
                    jobs.wake()
                    return self.send(200, st)
                if p == "/api/aufwaermen/state":  # from the run: {state, note}
                    cur = meta_get(con, "aufwaermen", {"state": "idle"})
                    cur.update({"state": str(b.get("state") or "idle")[:20], "note": str(b.get("note") or "")[:300], "changed": now()})
                    meta_set(con, "aufwaermen", cur)
                    return self.send(200, cur)
                if p == "/api/kochideen/verwerfen":
                    meta_set(con, "kochideen_vorschlag", None)
                    meta_set(con, "kochideen", {"state": "idle", "changed": now()})
                    return self.send(200, {"ok": True})
                # ----- week plan on request (AI run via the worker, proposal to approve) -----
                if p == "/api/wochenplan":  # {wunsch, start}
                    cur = meta_get(con, "wochenplan", {"state": "idle"})
                    if cur.get("state") in ("requested", "running"):
                        return self.send(200, cur)
                    start = str(b.get("start") or "")
                    if not DATE_RE.fullmatch(start):
                        today = datetime.now().date()
                        start = str(today + timedelta(days=7 - today.weekday()))
                    st = {"state": "requested", "requested": now(), "wunsch": (b.get("wunsch") or "").strip()[:500], "start": start}
                    people = household._people(b.get("personen"))  # number of people for this plan (guests)
                    if people:
                        st["personen"] = people
                    meta_set(con, "wochenplan", st)
                    return self.send(200, st)
                if p == "/api/wochenplan/personen":  # {personen}: scale the proposal to another number of people
                    v = meta_get(con, "wochenplan_vorschlag")
                    people = household._people(b.get("personen"))
                    if not v:
                        return self.send(409, {"error": "Kein Vorschlag da"})
                    if not people:
                        return self.send(400, {"error": "personen: 1 bis 30"})
                    old = v.get("personen") or households.people()["people_count"]
                    for e in v["eintraege"]:
                        if e.get("slug") and e.get("scale"):
                            e["scale"] = round(e["scale"] * people / old, 3)
                    v["personen"] = people
                    meta_set(con, "wochenplan_vorschlag", v)
                    return self.send(200, {"ok": True, "vorschlag": v})
                if p == "/api/wochenplan/state":
                    cur = meta_get(con, "wochenplan", {"state": "idle"})
                    cur.update({"state": b.get("state", "idle"), "note": (b.get("note") or "")[:300], "changed": now()})
                    meta_set(con, "wochenplan", cur)
                    return self.send(200, cur)
                if p == "/api/wochenplan/vorschlag":  # from the AI: {start, ende, zusammenfassung, prep, eintraege[], vorrat_neu[]}
                    entries = []
                    for e in b.get("eintraege") or []:
                        if not DATE_RE.fullmatch(str(e.get("datum") or "")) or e.get("typ") not in ("breakfast", "lunch", "dinner", "side", "snack"):
                            return self.send(400, {"error": f"Eintrag ungültig: {e}"})
                        slug = str(e.get("slug") or "")
                        if slug and not re.fullmatch(r"[a-z0-9-]{1,120}", slug):
                            return self.send(400, {"error": f"slug ungültig: {slug}"})
                        entries.append({"datum": e["datum"], "typ": e["typ"], "slug": slug or None,
                                          "titel": str(e.get("titel") or "")[:200], "text": str(e.get("text") or "")[:1000],
                                          "scale": float(e.get("scale") or 1), "einkaufen": bool(e.get("einkaufen", bool(slug)))})
                    if not entries:
                        return self.send(400, {"error": "keine Eintraege"})
                    v = {"start": str(b.get("start") or entries[0]["datum"]), "ende": str(b.get("ende") or entries[-1]["datum"]),
                         "zusammenfassung": str(b.get("zusammenfassung") or "")[:1500], "prep": str(b.get("prep") or "")[:3000],
                         "eintraege": entries, "vorrat_neu": [freezer_fields(x) for x in (b.get("vorrat_neu") or [])][:30],
                         "erstellt": now()}
                    cur = meta_get(con, "wochenplan", {"state": "idle"})
                    old = meta_get(con, "wochenplan_vorschlag") or {}
                    v["personen"] = (household._people(b.get("personen")) or (old.get("personen") if cur.get("modus") == "tauschen" else None)
                                     or cur.get("personen") or households.people()["people_count"])
                    meta_set(con, "wochenplan_vorschlag", v)
                    cur.update({"state": "done", "changed": now()})
                    meta_set(con, "wochenplan", cur)
                    return self.send(200, {"ok": True, "eintraege": len(entries)})
                if p == "/api/wochenplan/tauschen":  # {slug, neu} swaps right away, {slug, wunsch} lets the AI swap
                    v = meta_get(con, "wochenplan_vorschlag")
                    old = str(b.get("slug") or "")
                    if not v or not any(e["slug"] == old for e in v["eintraege"]):
                        return self.send(409, {"error": "Gericht steht nicht im Vorschlag"})
                    cur = meta_get(con, "wochenplan", {"state": "idle"})
                    if cur.get("state") in ("requested", "running"):
                        return self.send(409, {"error": "Claude ist gerade schon dran"})
                    stored = {r["slug"]: r for r in recipes.list_all()}
                    old_name = (stored.get(old) or {}).get("name") or old
                    new = str(b.get("neu") or "")
                    if not new:
                        st = {"state": "requested", "requested": now(), "modus": "tauschen", "slug": old, "name": old_name,
                              "wunsch": (b.get("wunsch") or "").strip()[:500], "start": v["start"],
                              **({"personen": v["personen"]} if v.get("personen") else {})}
                        meta_set(con, "wochenplan", st)
                        return self.send(200, st)
                    if new not in stored:
                        return self.send(400, {"error": "Rezept nicht gefunden"})
                    if new == old:
                        return self.send(400, {"error": "Das ist dasselbe Gericht"})
                    try:
                        info = week.recipe(new)
                    except shopping.Error:
                        info = {"einfrieren": True, "monate": 2}
                    new_name, p_old, p_new = stored[new]["name"], week._number(stored[old].get("portionen")) if old in stored else None, week._number(stored[new].get("portionen"))
                    hints = []
                    for e in v["eintraege"]:
                        if e["slug"] == old:  # keep the same number of servings
                            e["slug"] = new
                            if p_old and p_new:
                                e["scale"] = round(e["scale"] * p_old / p_new, 3)
                        elif not e["slug"] and old_name in (e["titel"] or ""):
                            e["titel"] = e["titel"].replace(old_name, new_name)
                            if not info["einfrieren"] and "eingefroren" in (e["text"] or "").lower():
                                hints.append(f"{new_name} lässt sich laut Notiz nicht einfrieren, die Reste am {e['datum']} bitte prüfen")
                    for f in v.get("vorrat_neu") or []:
                        if f.get("recipe_slug") == old or f.get("name") == old_name:
                            f.update(recipe_slug=new, name=new_name)
                            if f.get("eingefroren") and DATE_RE.fullmatch(f["eingefroren"]):
                                f["haltbar_bis"] = str(datetime.strptime(f["eingefroren"], "%Y-%m-%d").date() + timedelta(days=30 * info["monate"]))
                            if not info["einfrieren"]:
                                hints.append(f"{new_name} lässt sich laut Notiz nicht einfrieren")
                    for k in ("prep", "zusammenfassung"):
                        v[k] = (v.get(k) or "").replace(old_name, new_name)
                    v.setdefault("getauscht", []).append({"alt": old_name, "neu": new_name})
                    meta_set(con, "wochenplan_vorschlag", v)
                    return self.send(200, {"ok": True, "alt": old_name, "neu": new_name, "hinweise": list(dict.fromkeys(hints))})
                # ----- meal prep mode: the AI interleaves the recipes of the prep day into one schedule -----
                if p == "/api/mealprep":  # {datum, wunsch}
                    day = str(b.get("datum") or "")
                    if not DATE_RE.fullmatch(day):
                        return self.send(400, {"error": "Datum fehlt"})
                    cur = meta_get(con, "mealprep_job", {"state": "idle"})
                    if cur.get("state") in ("requested", "running"):
                        return self.send(200, cur)
                    st = {"state": "requested", "requested": now(), "datum": day, "wunsch": (b.get("wunsch") or "").strip()[:500]}
                    meta_set(con, "mealprep_job", st)
                    return self.send(200, st)
                if p == "/api/mealprep/state":
                    cur = meta_get(con, "mealprep_job", {"state": "idle"})
                    cur.update({"state": b.get("state", "idle"), "note": (b.get("note") or "")[:300], "changed": now()})
                    meta_set(con, "mealprep_job", cur)
                    return self.send(200, cur)
                if p == "/api/mealprep/ablauf":  # from the AI, format see mealprep.md
                    try:
                        a = mealprep_plan(b)
                    except ValueError as x:
                        return self.send(400, {"error": str(x)})
                    stored = meta_get(con, "mealprep", {})
                    stored[a["datum"]] = a
                    meta_set(con, "mealprep", dict(sorted(stored.items())[-8:]))
                    cur = meta_get(con, "mealprep_job", {"state": "idle"})
                    cur.update({"state": "done", "changed": now(), "datum": a["datum"]})
                    meta_set(con, "mealprep_job", cur)
                    return self.send(200, {"ok": True, "schritte": len(a["schritte"]), "rezepte": len(a["rezepte"])})
                if p == "/api/wochenplan/verwerfen":
                    meta_set(con, "wochenplan_vorschlag", None)
                    meta_set(con, "wochenplan", {"state": "idle", "changed": now()})
                    return self.send(200, {"ok": True})
                if p == "/api/wochenplan/annehmen":
                    v = meta_get(con, "wochenplan_vorschlag")
                    if not v:
                        return self.send(409, {"error": "Kein Vorschlag da"})
                    always = meta_get(con, "immer_da", shopping.ALWAYS_DEFAULT)
                    errors, plan_n, list_n = [], 0, 0
                    for e in v["eintraege"]:
                        try:
                            recipes.add_plan_entry(e["datum"], e["typ"], e["slug"], e["titel"], e["text"], e["scale"])
                            plan_n += 1
                        except shopping.Error as x:
                            errors.append(f"Plan {e['datum']} {e['titel'] or e['slug']}: {x}")
                    try:  # all recipes together, so the same ingredients are rounded only once
                        list_n, list_errors = recipes.add_to_list([(e["slug"], e["scale"]) for e in v["eintraege"] if e["slug"] and e["einkaufen"]], always)
                        errors += list_errors
                    except shopping.Error as x:
                        errors.append(f"Einkaufsliste: {x}")
                    for f in v.get("vorrat_neu") or []:
                        if f.get("name") and f.get("anzahl"):
                            f = {**f, "geaendert": now()}
                            con.execute(f"INSERT INTO vorrat({','.join(f)}) VALUES({','.join('?' * len(f))})", tuple(f.values()))
                    slugs = [e["slug"] for e in v["eintraege"] if e["slug"]]
                    if slugs:  # stars from the swipe cards are planned with this
                        con.execute(f"UPDATE cards SET plan=0 WHERE mealie_slug IN ({','.join('?' * len(slugs))})", slugs)
                    meta_set(con, "wochenplan_vorschlag", None)
                    meta_set(con, "wochenplan", {"state": "angenommen", "changed": now(), "start": v["start"]})
                    prep = next((e["datum"] for e in v["eintraege"] if e["typ"] == "side" and "mealprep" in (e["titel"] or "").lower()), None)
                    if prep and meta_get(con, "mealprep_job", {}).get("state") not in ("requested", "running"):
                        meta_set(con, "mealprep_job", {"state": "requested", "requested": now(), "datum": prep, "wunsch": ""})
                    return self.send(200, {"ok": True, "plan": plan_n, "rezepte_auf_liste": list_n, "fehler": errors})
                if p == "/api/nachschub":
                    cur = meta_get(con, "refill", {"state": "idle"})
                    if cur.get("state") in ("requested", "running"):
                        return self.send(200, cur)
                    meal = b.get("meal") if b.get("meal") in MEALS else "gemischt"
                    st = {"state": "requested", "requested": now(), "wunsch": (b.get("wunsch") or "").strip()[:300], "meal": meal}
                    meta_set(con, "refill", st)
                    return self.send(200, st)
                if p == "/api/nachschub/state":
                    cur = meta_get(con, "refill", {"state": "idle"})
                    cur.update({"state": b.get("state", "idle"), "note": b.get("note", ""), "changed": now()})
                    meta_set(con, "refill", cur)
                    return self.send(200, cur)
                if p == "/api/cards":
                    items = b if isinstance(b, list) else [b]
                    added, dup = [], []
                    batch = now()
                    for c in items:
                        if not c.get("url") or not c.get("title"):
                            continue
                        if con.execute("SELECT 1 FROM cards WHERE url=?", (c["url"],)).fetchone():
                            dup.append(c["url"])
                            continue
                        cur = con.execute(
                            "INSERT INTO cards(url,title,description,reason,tags,minutes,servings,kcal,kcal_est,ingredients,source,batch,added,meal,zutaten)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (c["url"], c["title"][:200], (c.get("description") or "")[:600], (c.get("reason") or "")[:300],
                             json.dumps(c.get("tags") or [], ensure_ascii=False), int_or_none(c.get("minutes")), str(c.get("servings") or ""),
                             int_or_none(c.get("kcal")), 1 if c.get("kcal_est") else 0, json.dumps((c.get("ingredients") or [])[:40], ensure_ascii=False),
                             c.get("source") or urlparse(c["url"]).hostname or "", batch, now(),
                             c.get("meal") if c.get("meal") in MEALS else "haupt",
                             json.dumps([str(z).strip()[:60] for z in (c.get("zutaten") or []) if str(z).strip()][:12], ensure_ascii=False)))
                        added.append((cur.lastrowid, c.get("image_url")))
                    con.commit()
            # fetch images outside the lock
            if p == "/api/cards":
                for cid, iurl in added:
                    name = fetch_image(cid, iurl)
                    if name:
                        with db() as con:
                            con.execute("UPDATE cards SET image=? WHERE id=?", (name, cid))
                return self.send(200, {"added": [a[0] for a in added], "duplicates": dup})
            return self.send(404, {"error": "unbekannt"})
        except Exception as e:  # noqa: BLE001
            return self.send(500, {"error": str(e)})


def cook_ideas_check(con, b):
    """Check the AI's pantry proposals: every idea is an own recipe (slug) or a swipe card (karte)."""
    t = lambda x, n: re.sub(r"\s+", " ", str(x or "")).strip()[:n]  # noqa: E731
    words = lambda xs: [t(x, 60) for x in (xs if isinstance(xs, list) else [])[:12] if t(x, 60)]  # noqa: E731
    ideas, errors = [], []
    raw = b.get("ideen") if isinstance(b.get("ideen"), list) else []
    for i, x in enumerate(raw[:8], 1):
        if not isinstance(x, dict):
            continue
        slug, card = str(x.get("slug") or ""), x.get("karte")
        idea = {"titel": t(x.get("titel"), 120), "grund": t(x.get("grund"), 300), "nutzt": words(x.get("nutzt")), "fehlt": words(x.get("fehlt"))}
        if slug:
            r = con.execute("SELECT name FROM rezept WHERE slug=?", (slug,)).fetchone() if re.fullmatch(r"[a-z0-9-]{1,120}", slug) else None
            if not r:
                errors.append(f"Idee {i}: Rezept {slug[:60]} nicht gefunden")
                continue
            ideas.append({**idea, "slug": slug, "titel": idea["titel"] or r["name"]})
        elif card not in (None, ""):
            try:
                cid = int(card)
            except (TypeError, ValueError):
                cid = 0
            r = con.execute("SELECT title FROM cards WHERE id=?", (cid,)).fetchone()
            if not r:
                errors.append(f"Idee {i}: Karte {str(card)[:20]} nicht gefunden")
                continue
            ideas.append({**idea, "karte": cid, "titel": idea["titel"] or r["title"]})
        else:
            errors.append(f"Idee {i}: slug oder karte fehlt")
    if not ideas:
        raise ValueError("keine gültigen Ideen" + (": " + "; ".join(errors) if errors else ""))
    return {"zusammenfassung": t(b.get("zusammenfassung"), 1000), "ideen": ideas, "fehler": errors, "erstellt": now()}


def cook_ideas(con):
    """The stored pantry proposals with what the app shows: recipe name, image, kcal; the swipe card with its state."""
    v = meta_get(con, "kochideen_vorschlag")
    if not v:
        return None
    stored = {r["slug"]: r for r in recipes.list_all()}
    out = []
    for x in v.get("ideen") or []:
        if x.get("slug"):
            r = stored.get(x["slug"])
            if r:
                out.append({**x, "rezept": {k: r[k] for k in ("id", "name", "bild", "kcal", "zeit", "portionen", "bewertung")}})
        elif x.get("karte"):
            c = con.execute("SELECT * FROM cards WHERE id=?", (x["karte"],)).fetchone()
            if c:
                d = card_out(c)
                out.append({**x, "karte_daten": {k: d.get(k) for k in ("id", "title", "image", "url", "source", "reason", "status",
                                                                       "kcal", "kcal_est", "minutes", "meal", "mealie_slug")}})
    return {**v, "ideen": out}


def init_household():
    """Tables and migrations of the current household (each household has a database of its own)."""
    households.init_household()
    os.makedirs(img_dir(), exist_ok=True)
    init_db()
    nutrition.init()
    store.init()
    integrations.init()
    pantry.init()
    ai.init()
    household.init()
    with db() as con:  # images of a photo import that no longer waits are removed (also leftovers older than 2 days)
        st = meta_get(con, "fotoimport", {})
    photos.clear(st.get("bilder_key") if st.get("state") in ("requested", "running") else None)


def init_all():
    """Create tables and run migrations, like at start. Also after importing a backup (backup.py).
    Instance tables first (accounts, households), then every household; household 1 is the old installation."""
    with store.in_household(1):
        init_db()
        store.init()
        auth.init()
        households.init()
        share.init()
        push.init()
    for hid in households.ids():
        with store.in_household(hid):
            init_household()


def demo_on_first_start():
    """KOMBUESE_DEMO=true (or en / de): an empty installation starts with the demo household (tools/demo.py).
    The password of the demo account is printed once to the log."""
    v = os.environ.get("KOMBUESE_DEMO", "").strip().lower()
    if v not in ("1", "true", "yes", "en", "de"):
        return
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.join(here, "tools"), os.path.join(here, "..", "tools")):  # /app/tools in Docker, ../tools in the repository
        if os.path.isfile(os.path.join(d, "demo.py")):
            sys.path.insert(0, d)
            break
    import demo
    if not demo.empty():
        return
    lang = v if v in ("en", "de") else os.environ.get("KOMBUESE_DEMO_LANG", "en")
    demo.print_result(demo.fill(lang), out=lambda s: print("Demo:", s, flush=True))


if __name__ == "__main__":
    init_all()
    demo_on_first_start()
    jobs.start()
    print(f"Kombüse on port {PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
