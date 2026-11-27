"""Web Push to the devices of a household, Python standard library only.

How it works without any encryption library:
- One VAPID key pair per installation (vapid.py, ECDSA P-256), created on first use; the private key is stored
  encrypted like other credentials (secretbox.py) in the instance database.
- A device subscribes in the app (Settings → Notifications). The app sends the subscription (endpoint, keys) to
  POST /api/push/subscribe and gets an id and a secret back, which its service worker keeps.
- A notification (notify.py, channel "webpush") is stored per device in push_pending, then an EMPTY push message (no
  payload, so no RFC 8291 encryption) goes to the push service with a signed VAPID header.
- The push wakes the service worker, which fetches GET /api/push/pending?id=… with the header X-Push-Secret and shows
  what it got. Delivered entries are marked and cleaned up after a day.
- Subscriptions the push service reports as gone (404, 410) are deleted.

Push services are outside services: every request goes through netsafe.py (no local addresses unless
KOMBUESE_ALLOW_PRIVATE_URLS=true, used by the tests with a fake push service on 127.0.0.1).
Tables (instance database): push_vapid, push_subs, push_pending.
"""
import hashlib
import hmac
import re
import secrets
import threading
import urllib.error
from datetime import datetime, timedelta, timezone

import config
import netsafe
import secretbox
import store
import vapid
from store import Error

VIEWS = ("home", "deck", "recipes", "week", "pantry", "shop", "likes")  # where a tap on the notification opens the app
MAX_PER_USER = 20     # devices per account
MAX_PENDING = 20      # waiting notifications per device
TTL = 24 * 3600       # how long the push service keeps an undelivered push (seconds)
_lock = threading.Lock()
_keys = {}  # cache: "d" (private key as int), "pub" (b64url), "contact"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init():
    _keys.clear()  # after a backup import the key may be another one
    with store.main() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS push_vapid (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              public TEXT NOT NULL,          -- uncompressed P-256 point, base64url
              private TEXT NOT NULL,         -- encrypted with secretbox
              contact TEXT,                  -- sub claim: https address of the installation (or KOMBUESE_PUSH_CONTACT)
              created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS push_subs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              endpoint TEXT UNIQUE NOT NULL,
              p256dh TEXT NOT NULL DEFAULT '', auth TEXT NOT NULL DEFAULT '',   -- kept, but unused: messages have no payload
              user_id INTEGER NOT NULL,
              household_id INTEGER NOT NULL,
              secret_hash TEXT NOT NULL,     -- sha256 of the secret the service worker sends to fetch its notifications
              agent TEXT NOT NULL DEFAULT '',
              created TEXT NOT NULL,
              last_ok TEXT, last_error TEXT, errors INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS push_subs_household ON push_subs(household_id);
            CREATE TABLE IF NOT EXISTS push_pending (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sub_id INTEGER NOT NULL,
              title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL, view TEXT NOT NULL DEFAULT '',
              created TEXT NOT NULL, fetched TEXT
            );
            CREATE INDEX IF NOT EXISTS push_pending_sub ON push_pending(sub_id);
        """)


def _sha(s):
    return hashlib.sha256(str(s).encode()).hexdigest()


def _key():
    """(private key, public key b64url, contact). Created once; a key that no longer decrypts (other secret key) is
    replaced, and the subscriptions made with the old one are dropped, the devices subscribe again on their own."""
    with _lock:
        if _keys:
            return _keys["d"], _keys["pub"], _keys["contact"]
        with store.main() as c:
            r = c.execute("SELECT * FROM push_vapid WHERE id=1").fetchone()
            d = None
            if r:
                try:
                    d = vapid.private_from_bytes(vapid.b64u_decode(secretbox.decrypt(r["private"])))
                except (ValueError, TypeError):
                    print("Web Push: stored VAPID key cannot be decrypted (other KOMBUESE_SECRET_KEY?), creating a new one", flush=True)
                    d = None
            if d is None:
                d = vapid.new_private_key()
                pub = vapid.b64u(vapid.encode_point(vapid.public_key(d)))
                c.execute("DELETE FROM push_pending")
                c.execute("DELETE FROM push_subs")
                c.execute("INSERT OR REPLACE INTO push_vapid(id, public, private, contact, created) VALUES(1,?,?,?,?)",
                          (pub, secretbox.encrypt(vapid.b64u(vapid.private_to_bytes(d))), r["contact"] if r else None, _now()))
                contact = r["contact"] if r else None
            else:
                pub, contact = r["public"], r["contact"]
        _keys.update(d=d, pub=pub, contact=contact)
        return d, pub, contact


def public_key():
    return _key()[1]


def _contact():
    c = config.PUSH_CONTACT or config.https_url() or _key()[2]
    if c and re.fullmatch(r"(mailto:\S+@\S+|https://\S+)", c):
        return c
    return "mailto:kombuese@example.invalid"


def remember_contact(base_url):
    """The https address the app is used with, as contact for the push services (the sub claim), if none is configured."""
    if not base_url.startswith("https://") or config.PUSH_CONTACT or config.https_url():
        return
    _key()
    with _lock, store.main() as c:
        c.execute("UPDATE push_vapid SET contact=? WHERE id=1 AND (contact IS NULL OR contact != ?)", (base_url, base_url))
        _keys["contact"] = base_url


def _check_endpoint(endpoint):
    endpoint = str(endpoint or "").strip()
    if len(endpoint) > 1000 or not re.fullmatch(r"https://[^\s]+", endpoint) and not (config.ALLOW_PRIVATE_URLS and re.fullmatch(r"http://[^\s]+", endpoint)):
        raise Error("Push-Adresse ungültig")
    try:
        netsafe.check(endpoint)
    except netsafe.Blocked as e:
        raise Error(f"Push-Adresse abgelehnt: {e}") from None
    return endpoint


def _b64key(v, n):
    v = str(v or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}={0,2}", v):
        return ""
    try:
        return v if len(vapid.b64u_decode(v)) == n else ""
    except ValueError:
        return ""


def subscribe(user, sub, agent="", base_url=""):
    """Store a device's subscription for this account (an existing endpoint is taken over). → {ok, id, secret, key}"""
    sub = sub if isinstance(sub, dict) else {}
    endpoint = _check_endpoint(sub.get("endpoint"))
    keys = sub.get("keys") if isinstance(sub.get("keys"), dict) else {}
    p256dh, auth_key = _b64key(keys.get("p256dh"), 65), _b64key(keys.get("auth"), 16)
    secret = secrets.token_urlsafe(32)
    _, pub, _ = _key()
    with store.main() as c:
        n = c.execute("SELECT COUNT(*) FROM push_subs WHERE user_id=? AND endpoint!=?", (user["id"], endpoint)).fetchone()[0]
        if n >= MAX_PER_USER:
            raise Error(f"Höchstens {MAX_PER_USER} Geräte pro Konto")
        old = c.execute("SELECT id FROM push_subs WHERE endpoint=?", (endpoint,)).fetchone()
        if old:
            c.execute("DELETE FROM push_pending WHERE sub_id=?", (old["id"],))
            c.execute("UPDATE push_subs SET p256dh=?, auth=?, user_id=?, household_id=?, secret_hash=?, agent=?, created=?, last_ok=NULL,"
                      " last_error=NULL, errors=0 WHERE id=?",
                      (p256dh, auth_key, user["id"], user["household_id"], _sha(secret), str(agent or "")[:200], _now(), old["id"]))
            sid = old["id"]
        else:
            sid = c.execute("INSERT INTO push_subs(endpoint, p256dh, auth, user_id, household_id, secret_hash, agent, created) VALUES(?,?,?,?,?,?,?,?)",
                            (endpoint, p256dh, auth_key, user["id"], user["household_id"], _sha(secret), str(agent or "")[:200], _now())).lastrowid
    if base_url:
        remember_contact(base_url)
    return {"ok": True, "id": sid, "secret": secret, "key": pub}


def unsubscribe(user, endpoint=None, sid=None):
    """Remove this account's subscription by endpoint or id. → {ok, removed}"""
    with store.main() as c:
        if sid:
            rows = c.execute("SELECT id FROM push_subs WHERE id=? AND user_id=?", (int(sid), user["id"])).fetchall()
        else:
            rows = c.execute("SELECT id FROM push_subs WHERE endpoint=? AND user_id=?", (str(endpoint or ""), user["id"])).fetchall()
        for r in rows:
            c.execute("DELETE FROM push_pending WHERE sub_id=?", (r["id"],))
            c.execute("DELETE FROM push_subs WHERE id=?", (r["id"],))
    return {"ok": True, "removed": len(rows)}


def devices(user):
    """Devices of this account (without endpoints and secrets)."""
    with store.main() as c:
        return [dict(r) for r in c.execute("SELECT id, agent, created, last_ok, last_error FROM push_subs WHERE user_id=? AND household_id=?"
                                           " ORDER BY id", (user["id"], user["household_id"]))]


def _active(c, hid):
    """Subscriptions of a household whose account still exists, is active and still belongs to the household."""
    return c.execute("SELECT s.* FROM push_subs s JOIN users u ON u.id = s.user_id AND u.disabled = 0 AND u.household_id = s.household_id"
                     " WHERE s.household_id=? ORDER BY s.id", (int(hid),)).fetchall()


def count(hid=None):
    with store.main() as c:
        return len(_active(c, store.household_id() if hid is None else hid))


def overview(hid=None):
    """For Settings → Connections: how many devices of the household get notifications."""
    n = count(hid)
    return {"configured": n > 0, "devices": n}


def pending(sid, secret):
    """Notifications waiting for one device (service worker, X-Push-Secret). Marks them as fetched. → {items}"""
    try:
        sid = int(sid)
    except (TypeError, ValueError):
        raise Error("unbekannt") from None
    with store.main() as c:
        s = c.execute("SELECT s.secret_hash FROM push_subs s JOIN users u ON u.id = s.user_id AND u.disabled = 0 AND u.household_id = s.household_id"
                      " WHERE s.id=?", (sid,)).fetchone()
        if not s or not secret or not hmac.compare_digest(s["secret_hash"], _sha(secret)):
            raise Error("unbekannt")
        rows = c.execute("SELECT id, title, body, view, created FROM push_pending WHERE sub_id=? AND fetched IS NULL ORDER BY id", (sid,)).fetchall()
        if rows:
            c.execute(f"UPDATE push_pending SET fetched=? WHERE id IN ({','.join('?' * len(rows))})", (_now(), *[r["id"] for r in rows]))
        day = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
        c.execute("DELETE FROM push_pending WHERE (fetched IS NOT NULL AND fetched < ?) OR created < ?", (day, week))
    return {"items": [{"id": r["id"], "title": r["title"], "text": r["body"], "view": r["view"], "created": r["created"]} for r in rows]}


def _deliver(c, s, d, contact):
    """Send one empty push message. → None (ok), "gone" or an error text."""
    headers = {"TTL": str(TTL), "Urgency": "normal", "Content-Length": "0", "User-Agent": "Kombuese (self-hosted kitchen app)",
               "Authorization": vapid.authorization(d, s["endpoint"], contact)}
    try:
        netsafe.open_url(s["endpoint"], headers, timeout=10, max_bytes=4096, data=b"", method="POST")
        c.execute("UPDATE push_subs SET last_ok=?, last_error=NULL, errors=0 WHERE id=?", (_now(), s["id"]))
        return None
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):  # the subscription is gone (app removed, permission revoked, expired)
            c.execute("DELETE FROM push_pending WHERE sub_id=?", (s["id"],))
            c.execute("DELETE FROM push_subs WHERE id=?", (s["id"],))
            return "gone"
        try:
            detail = e.read(200).decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            detail = ""
        err = f"HTTP {e.code}" + (f" {detail[:150]}" if detail else "")
    except netsafe.Blocked as e:
        err = f"abgelehnt: {e}"
    except (urllib.error.URLError, OSError) as e:
        err = f"nicht erreichbar: {getattr(e, 'reason', e)}"
    except vapid.Error as e:
        err = str(e)
    c.execute("UPDATE push_subs SET last_error=?, errors=errors+1 WHERE id=?", (f"{_now()} {err}"[:300], s["id"]))
    return err


def send(text, title="", view="", hid=None, only=None):
    """Notification to every device of the household (or only to subscription id `only`).
    → {devices, sent, removed, errors: [..]}; raises Error when nothing could be delivered."""
    text = str(text or "").strip()[:2000]
    if not text:
        raise Error("Text fehlt")
    view = view if view in VIEWS else ""
    d, _, _ = _key()
    contact = _contact()
    hid = store.household_id() if hid is None else int(hid)
    out = {"devices": 0, "sent": 0, "removed": 0, "errors": []}
    with store.main() as c:
        subs = [s for s in _active(c, hid) if only is None or s["id"] == only]
        out["devices"] = len(subs)
        for s in subs:
            c.execute("INSERT INTO push_pending(sub_id, title, body, view, created) VALUES(?,?,?,?,?)",
                      (s["id"], str(title or "")[:120], text, view, _now()))
            c.execute("DELETE FROM push_pending WHERE sub_id=? AND id NOT IN (SELECT id FROM push_pending WHERE sub_id=? ORDER BY id DESC LIMIT ?)",
                      (s["id"], s["id"], MAX_PENDING))
        c.commit()  # stored before the push goes out: the service worker may ask right away
        for s in subs:
            r = _deliver(c, s, d, contact)
            c.commit()
            if r is None:
                out["sent"] += 1
            elif r == "gone":
                out["removed"] += 1
            else:
                out["errors"].append(r)
    if not out["sent"]:
        if not subs:
            raise Error("kein Gerät angemeldet")
        raise Error("; ".join(out["errors"]) or "Gerät abgemeldet")
    return out


def test(user, endpoint=None, sid=None, text="Testnachricht aus der Kombüse ✓"):
    """Test message to one device of this account."""
    with store.main() as c:
        if sid:
            r = c.execute("SELECT id FROM push_subs WHERE id=? AND user_id=?", (int(sid), user["id"])).fetchone()
        else:
            r = c.execute("SELECT id FROM push_subs WHERE endpoint=? AND user_id=?", (str(endpoint or ""), user["id"])).fetchone()
    if not r:
        raise Error("Dieses Gerät ist nicht angemeldet")
    return {"ok": True, **send(text, "Kombüse", "home", hid=user["household_id"], only=r["id"])}
