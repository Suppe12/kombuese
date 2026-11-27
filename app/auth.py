"""Login for Kombüse: local accounts, your own identity provider (OIDC), sessions and API tokens.

Several accounts per household (households, invitations and the household of each account: households.py).
Roles: "admin" (manages the household's accounts and API tokens) and "member".
Python standard library only:
- passwords: hashlib.scrypt with a random salt
- sessions: random token in an HttpOnly cookie, only its SHA-256 is stored
- API tokens (for automation such as AI runs or a calendar feed): Bearer header, only the hash is stored
- OIDC: authorization code flow with PKCE, state and nonce. The ID token comes straight from the token endpoint
  (back channel). RS256 signatures are verified against the provider's JWKS, other algorithms rely on the TLS
  connection to the token endpoint as OpenID Connect Core 3.1.3.7 allows.

Environment (see config.py): KOMBUESE_OIDC_ISSUER, KOMBUESE_OIDC_CLIENT_ID, KOMBUESE_OIDC_CLIENT_SECRET,
KOMBUESE_OIDC_NAME (button label), KOMBUESE_OIDC_SIGNUP (create accounts on first OIDC login),
KOMBUESE_SESSION_DAYS, KOMBUESE_CALENDAR_PUBLIC.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import config
import store as _store


class store:  # noqa: N801  accounts, sessions and API tokens live in the instance database, whatever household a request works in
    lock = _store.lock
    con = staticmethod(_store.main)


INVITE_HOOK = None  # (check(code) → invitation, redeem(invitation, user id)), set by households.py: OIDC sign-up through an invitation

COOKIE = "kombuese_session"
INTERNAL_TOKEN = secrets.token_urlsafe(32)  # only for the PDF renderer inside Docker, new on every start
AGENT_TOKEN = secrets.token_urlsafe(32)     # AI runs inside the app (jobs.py) calling the API, new on every start
_lock = threading.Lock()
_fails = {}       # ip -> [timestamps of failed logins]
_oidc_state = {}  # state -> {verifier, nonce, redirect, created}
_install = {}     # cached cookie name of this installation (cookie_name)
_oidc_meta = {}   # discovery document and JWKS cache


class AuthError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init():
    with store.con() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT UNIQUE NOT NULL COLLATE NOCASE,
              display_name TEXT NOT NULL DEFAULT '',
              email TEXT COLLATE NOCASE,
              password_hash TEXT,                 -- NULL = OIDC only
              role TEXT NOT NULL DEFAULT 'member', -- admin | member
              oidc_issuer TEXT, oidc_sub TEXT,
              disabled INTEGER NOT NULL DEFAULT 0,
              created TEXT NOT NULL, last_login TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS users_oidc ON users(oidc_issuer, oidc_sub);
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created TEXT NOT NULL,
              expires TEXT NOT NULL, last_seen TEXT, agent TEXT
            );
            CREATE TABLE IF NOT EXISTS api_tokens (
              id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
              scope TEXT NOT NULL DEFAULT 'api',   -- api (everything) | calendar (only the iCal feed)
              user_id INTEGER, created TEXT NOT NULL, last_used TEXT, revoked INTEGER NOT NULL DEFAULT 0
            );
        """)
        if "remember" not in {r[1] for r in c.execute("PRAGMA table_info(sessions)")}:
            c.execute("ALTER TABLE sessions ADD COLUMN remember INTEGER NOT NULL DEFAULT 1")


# ---------- passwords ----------
def hash_password(pw):
    if len(pw or "") < 8:
        raise AuthError("Passwort zu kurz (mindestens 8 Zeichen)")
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"


def check_password(pw, stored):
    try:
        _, n, r, p, salt, h = (stored or "").split("$")
        calc = hashlib.scrypt((pw or "").encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


def _sha(t):
    return hashlib.sha256(t.encode()).hexdigest()


# ---------- users ----------
def user_count():
    with store.con() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def _public(u):
    return {k: u[k] for k in ("id", "username", "display_name", "email", "role", "disabled", "created", "last_login")} | {
        "password": bool(u["password_hash"]), "oidc": bool(u["oidc_sub"])}


def users():
    with store.con() as c:
        return [_public(u) for u in c.execute("SELECT * FROM users ORDER BY username")]


def create_user(username, password=None, display_name="", email="", role="member", oidc=None):
    username = re.sub(r"\s+", "", str(username or "")).strip()[:60]
    if not re.fullmatch(r"[\w.@+-]{2,60}", username):
        raise AuthError("Benutzername: 2 bis 60 Zeichen, Buchstaben, Zahlen, . _ - @ +")
    if role not in ("admin", "member"):
        raise AuthError("Rolle admin oder member")
    ph = hash_password(password) if password else None
    if not ph and not oidc:
        raise AuthError("Passwort fehlt")
    with store.lock, store.con() as c:
        if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise AuthError("Benutzername schon vergeben")
        uid = c.execute("INSERT INTO users(username, display_name, email, password_hash, role, oidc_issuer, oidc_sub, created) "
                        "VALUES(?,?,?,?,?,?,?,?)", (username, str(display_name or username)[:80], str(email or "")[:120] or None, ph, role,
                                                    oidc[0] if oidc else None, oidc[1] if oidc else None, now())).lastrowid
    return uid


def update_user(uid, actor, data):
    with store.con() as c:
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        raise AuthError("Konto nicht gefunden", 404)
    admins = sum(1 for x in users() if x["role"] == "admin" and not x["disabled"])
    last_admin = u["role"] == "admin" and not u["disabled"] and admins <= 1
    if data.get("delete"):
        if last_admin or uid == actor["id"]:
            raise AuthError("Das eigene Konto und den letzten Admin kann man nicht löschen")
        with store.lock, store.con() as c:
            c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            c.execute("DELETE FROM users WHERE id=?", (uid,))
        return {"ok": True}
    sets = {}
    if "role" in data and data["role"] in ("admin", "member") and data["role"] != u["role"]:
        if last_admin:
            raise AuthError("Es muss einen Admin geben")
        sets["role"] = data["role"]
    if "disabled" in data:
        if last_admin and data["disabled"]:
            raise AuthError("Es muss einen Admin geben")
        sets["disabled"] = 1 if data["disabled"] else 0
    if data.get("password"):
        sets["password_hash"] = hash_password(data["password"])
    for k in ("display_name", "email"):
        if k in data:
            sets[k] = str(data[k] or "")[:120] or (None if k == "email" else "")
    if sets:
        with store.lock, store.con() as c:
            c.execute(f"UPDATE users SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), uid))
            if sets.get("disabled") or "password_hash" in sets:
                c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    return {"ok": True}


def change_own_password(user, old, new):
    with store.con() as c:
        u = c.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    if u["password_hash"] and not check_password(old, u["password_hash"]):
        raise AuthError("Altes Passwort stimmt nicht", 403)
    with store.lock, store.con() as c:
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(new), u["id"]))
        # security review: other devices signed in with the old password are signed out, this one stays
        c.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (u["id"], _sha(user.get("session") or "")))
    return {"ok": True}


# ---------- sessions ----------
def _rate_ok(ip):
    t = time.time()
    with _lock:
        _fails[ip] = [x for x in _fails.get(ip, []) if t - x < 600]
        return len(_fails[ip]) < 10


def _failed(ip):
    with _lock:
        _fails.setdefault(ip, []).append(time.time())


SHORT_SESSION = timedelta(hours=12)  # without "stay signed in": until the browser closes, at most 12 h idle


def _lifetime(remember):
    return timedelta(days=int(config.SESSION_DAYS)) if remember else SHORT_SESSION


def new_session(uid, agent="", remember=True):
    token = secrets.token_urlsafe(32)
    with store.lock, store.con() as c:
        c.execute("INSERT INTO sessions(token_hash, user_id, created, expires, last_seen, agent, remember) VALUES(?,?,?,?,?,?,?)",
                  (_sha(token), uid, now(), (datetime.now(timezone.utc) + _lifetime(remember)).isoformat(timespec="seconds"),
                   now(), str(agent or "")[:200], 1 if remember else 0))
        c.execute("UPDATE users SET last_login=? WHERE id=?", (now(), uid))
        c.execute("DELETE FROM sessions WHERE expires < ?", (now(),))
    return token


def cookie_name():
    """Unique per installation: cookies are per host, not per port, so two Kombüse instances on one host
    used to sign each other out with the shared name "kombuese_session"."""
    if "cookie" not in _install:
        f = os.path.join(config.DATA, "install_id")  # a file, so a restored backup keeps this installation's name
        with _lock:
            try:
                with open(f, encoding="utf-8") as fh:
                    iid = re.sub(r"[^0-9a-f]", "", fh.read())[:16]
            except OSError:
                iid = ""
            if not iid:
                iid = secrets.token_hex(4)
                with open(f, "w", encoding="utf-8") as fh:
                    fh.write(iid + "\n")
        _install["cookie"] = f"kombuese_{iid}"
    return _install["cookie"]


def cookie_header(token, secure, clear=False, remember=True, name=None):
    """Set-Cookie for a session. remember=False: a browser-session cookie without Max-Age."""
    age = "; Max-Age=0" if clear else (f"; Max-Age={int(config.SESSION_DAYS) * 86400}" if remember else "")
    return (f"{name or cookie_name()}={'' if clear else token}; Path=/; HttpOnly; SameSite=Lax{age}"
            + ("; Secure" if secure else ""))


def _dummy_hash():
    if "dummy" not in _install:
        _install["dummy"] = hash_password(secrets.token_urlsafe(16))
    return _install["dummy"]


def login(username, password, ip, agent="", remember=True):
    if not _rate_ok(ip):
        raise AuthError("Zu viele Versuche, bitte in ein paar Minuten nochmal", 429)
    with store.con() as c:
        u = c.execute("SELECT * FROM users WHERE username=?", (str(username or "").strip(),)).fetchone()
    if not u or not u["password_hash"]:
        check_password(password, _dummy_hash())  # security review: as slow as a real check, the timing does not tell which names exist
        _failed(ip)
        raise AuthError("Benutzername oder Passwort falsch", 403)
    if u["disabled"] or not check_password(password, u["password_hash"]):
        _failed(ip)
        raise AuthError("Benutzername oder Passwort falsch", 403)
    return new_session(u["id"], agent, remember)


def logout(token):
    if token:
        with store.lock, store.con() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (_sha(token),))


def setup(username, password, ip, agent=""):
    """First start: the first account becomes admin. Only possible while there is no account."""
    with _lock:
        if user_count():
            raise AuthError("Es gibt schon ein Konto", 409)
        uid = create_user(username, password, role="admin")
    return new_session(uid, agent)


# ---------- requests ----------
def _cookies(headers):
    out = {}
    for part in (headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        out.setdefault(k, v)
    return out


def _cookie(headers):
    """Session token: the installation's cookie, else the older shared name (taken over on the next answer)."""
    c = _cookies(headers)
    return c.get(cookie_name()) or c.get(COOKIE)


def identify(headers, query=None):
    """Who is asking? → {"id", "username", "role", ..., "via": "session"|"token"|"internal"|"agent", "scope"} or None."""
    a = headers.get("Authorization") or ""
    if a.startswith("Bearer "):
        t = a[7:].strip()
        if hmac.compare_digest(t, INTERNAL_TOKEN):
            return {"id": 0, "username": "pdf", "role": "member", "via": "internal", "scope": "read"}
        if hmac.compare_digest(t, AGENT_TOKEN):
            return {"id": 0, "username": "ai", "role": "member", "via": "agent", "scope": "api"}
        return _token_user(t)
    if query and query.get("token"):
        return _token_user(query["token"][0])
    jar = _cookies(headers)
    t, legacy = jar.get(cookie_name()), False
    if not t and jar.get(COOKIE):
        t, legacy = jar[COOKIE], True
    if not t:
        return None
    with store.con() as c:
        s = c.execute("SELECT s.*, u.username, u.display_name, u.role, u.disabled, u.email FROM sessions s "
                      "JOIN users u ON u.id = s.user_id WHERE s.token_hash=?", (_sha(t),)).fetchone()
    if not s or s["disabled"] or s["expires"] < now():
        return None
    remember = bool(s["remember"])
    if not s["last_seen"] or s["last_seen"][:13] != now()[:13]:  # at most hourly: extend the session (rolling)
        with store.lock, store.con() as c:
            c.execute("UPDATE sessions SET last_seen=?, expires=? WHERE token_hash=?",
                      (now(), (datetime.now(timezone.utc) + _lifetime(remember)).isoformat(timespec="seconds"), _sha(t)))
    return {"id": s["user_id"], "username": s["username"], "display_name": s["display_name"], "email": s["email"],
            "role": s["role"], "via": "session", "scope": "api", "session": t, "remember": remember,
            "cookie_legacy": legacy}


def _token_user(t):
    with store.con() as c:
        r = c.execute("SELECT * FROM api_tokens WHERE token_hash=? AND revoked=0", (_sha(t),)).fetchone()
    if not r:
        return None
    if not r["last_used"] or r["last_used"][:13] != now()[:13]:
        with store.lock, store.con() as c:
            c.execute("UPDATE api_tokens SET last_used=? WHERE id=?", (now(), r["id"]))
    return {"id": r["user_id"] or 0, "username": f"token:{r['name']}", "role": "member", "via": "token", "scope": r["scope"]}


# ---------- API tokens ----------
def tokens():
    with store.con() as c:
        return [dict(r) | {"token_hash": None} for r in c.execute("SELECT * FROM api_tokens ORDER BY revoked, name")]


def create_token(name, scope="api", user_id=None):
    name = str(name or "").strip()[:60]
    if not name:
        raise AuthError("Name fehlt")
    if scope not in ("api", "calendar"):
        raise AuthError("Bereich api oder calendar")
    t = "kmb_" + secrets.token_urlsafe(32)
    with store.lock, store.con() as c:
        tid = c.execute("INSERT INTO api_tokens(name, token_hash, scope, user_id, created) VALUES(?,?,?,?,?)",
                        (name, _sha(t), scope, user_id, now())).lastrowid
    return {"id": tid, "name": name, "scope": scope, "token": t}


def revoke_token(tid):
    with store.lock, store.con() as c:
        if not c.execute("UPDATE api_tokens SET revoked=1 WHERE id=?", (tid,)).rowcount:
            raise AuthError("Token nicht gefunden", 404)
    return {"ok": True}


# ---------- OIDC ----------
def oidc_enabled():
    return bool(config.OIDC_ISSUER and config.OIDC_CLIENT_ID)


def _http_json(url, data=None, headers=None):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"Accept": "application/json", **(headers or {})},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise AuthError(f"Anmeldedienst: HTTP {e.code} {e.read()[:200].decode('utf-8', 'replace')}", 502) from None
    except (urllib.error.URLError, ValueError) as e:
        raise AuthError(f"Anmeldedienst nicht erreichbar: {e}", 502) from None


def _discovery():
    m = _oidc_meta.get("disc")
    if not m or time.time() - m[0] > 3600:
        d = _http_json(config.OIDC_ISSUER.rstrip("/") + "/.well-known/openid-configuration")
        _oidc_meta["disc"] = m = (time.time(), d)
    return m[1]


def _b64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def oidc_start(base_url, invite=None):
    d = _discovery()
    state, verifier, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(48), secrets.token_urlsafe(24)
    redirect = base_url.rstrip("/") + "/auth/oidc/callback"
    with _lock:
        for k in [k for k, v in _oidc_state.items() if time.time() - v["created"] > 600]:
            del _oidc_state[k]
        _oidc_state[state] = {"verifier": verifier, "nonce": nonce, "redirect": redirect, "created": time.time(),
                              "invite": str(invite or "")[:40] or None}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = {"response_type": "code", "client_id": config.OIDC_CLIENT_ID, "redirect_uri": redirect, "scope": "openid email profile",
         "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
    return d["authorization_endpoint"] + ("&" if "?" in d["authorization_endpoint"] else "?") + urllib.parse.urlencode(q)


def _rs256_ok(signing_input, sig, jwk):
    n = int.from_bytes(_b64(jwk["n"]), "big")
    e = int.from_bytes(_b64(jwk["e"]), "big")
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    m = pow(int.from_bytes(sig, "big"), e, n).to_bytes(k, "big")
    info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(signing_input).digest()
    return hmac.compare_digest(m, b"\x00\x01" + b"\xff" * (k - len(info) - 3) + b"\x00" + info)


def _verify_id_token(id_token, nonce):
    try:
        h64, p64, s64 = id_token.split(".")
        head, claims = json.loads(_b64(h64)), json.loads(_b64(p64))
    except ValueError:
        raise AuthError("Ungültiges ID-Token", 403) from None
    if head.get("alg") == "RS256":
        d = _discovery()
        keys = _http_json(d["jwks_uri"]).get("keys") or []
        jwk = next((k for k in keys if k.get("kid") == head.get("kid") and k.get("kty") == "RSA"), None) \
            or next((k for k in keys if k.get("kty") == "RSA" and not head.get("kid")), None)
        if not jwk or not _rs256_ok(f"{h64}.{p64}".encode(), _b64(s64), jwk):
            raise AuthError("Signatur des ID-Tokens ungültig", 403)
    elif head.get("alg") in ("HS256", "HS384", "HS512") and config.OIDC_CLIENT_SECRET:
        fn = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}[head["alg"]]
        if not hmac.compare_digest(hmac.new(config.OIDC_CLIENT_SECRET.encode(), f"{h64}.{p64}".encode(), fn).digest(), _b64(s64)):
            raise AuthError("Signatur des ID-Tokens ungültig", 403)
    elif head.get("alg") in (None, "none") or str(head.get("alg")).startswith("HS"):
        raise AuthError("ID-Token ohne gültige Signatur", 403)
    # other algorithms (e.g. ES256): the token came straight from the token endpoint over TLS (OIDC Core 3.1.3.7)
    aud = claims.get("aud")
    aud = aud if isinstance(aud, list) else [aud]
    if claims.get("iss", "").rstrip("/") != config.OIDC_ISSUER.rstrip("/") or config.OIDC_CLIENT_ID not in aud:
        raise AuthError("ID-Token ist nicht für diese Kombüse", 403)
    if claims.get("exp", 0) < time.time() - 60 or claims.get("nonce") != nonce:
        raise AuthError("ID-Token abgelaufen oder nicht passend", 403)
    return claims


def oidc_callback(query, ip, agent=""):
    if not _rate_ok(ip):
        raise AuthError("Zu viele Versuche, bitte in ein paar Minuten nochmal", 429)
    if query.get("error"):
        raise AuthError("Anmeldung abgebrochen: " + query["error"][0], 403)
    state = (query.get("state") or [""])[0]
    with _lock:
        st = _oidc_state.pop(state, None)
    if not st or time.time() - st["created"] > 600:
        _failed(ip)
        raise AuthError("Anmeldung abgelaufen, bitte nochmal", 403)
    d = _discovery()
    form = {"grant_type": "authorization_code", "code": (query.get("code") or [""])[0], "redirect_uri": st["redirect"],
            "client_id": config.OIDC_CLIENT_ID, "code_verifier": st["verifier"]}
    headers = {}
    if config.OIDC_CLIENT_SECRET:
        if "client_secret_basic" in (d.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]):
            cred = f"{urllib.parse.quote(config.OIDC_CLIENT_ID, safe='')}:{urllib.parse.quote(config.OIDC_CLIENT_SECRET, safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
        else:
            form["client_secret"] = config.OIDC_CLIENT_SECRET
    tok = _http_json(d["token_endpoint"], form, headers)
    claims = _verify_id_token(tok.get("id_token") or "", st["nonce"])
    iss, sub = claims["iss"].rstrip("/"), str(claims.get("sub") or "")
    email = claims.get("email") if claims.get("email_verified", True) else None
    with store.con() as c:
        u = c.execute("SELECT * FROM users WHERE oidc_issuer=? AND oidc_sub=?", (iss, sub)).fetchone()
        if not u and email:  # existing local account with the same e-mail: link it
            u = c.execute("SELECT * FROM users WHERE email=? AND oidc_sub IS NULL", (email,)).fetchone()
            if u:
                with store.lock:
                    c.execute("UPDATE users SET oidc_issuer=?, oidc_sub=? WHERE id=?", (iss, sub, u["id"]))
    invite = None
    if not u:
        if st.get("invite") and INVITE_HOOK:  # signed in through an invitation link: a new account in that household
            invite = INVITE_HOOK[0](st["invite"])
        if not (config.OIDC_SIGNUP or invite or not user_count()):
            _failed(ip)
            raise AuthError("Für dieses Login gibt es kein Konto. Ein Admin kann es anlegen (gleiche E-Mail).", 403)
        name = re.sub(r"[^\w.@+-]", "", claims.get("preferred_username") or (email or "").split("@")[0] or "user")[:40] or "user"
        base, i = name, 2
        while True:
            with store.con() as c:
                if not c.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone():
                    break
            name, i = f"{base}{i}", i + 1
        uid = create_user(name, None, claims.get("name") or name, email,
                          invite["role"] if invite else "admin" if not user_count() else "member", (iss, sub))
        if invite:
            INVITE_HOOK[1](invite, uid)
    else:
        if u["disabled"]:
            raise AuthError("Konto ist gesperrt", 403)
        uid = u["id"]
    return new_session(uid, agent)
