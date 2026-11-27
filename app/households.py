"""Households: several households per installation, invitations, the people of a household with their
preferences, and the household match on the swipe cards.

Households
  By default there is one household and nothing changes for it: household 1 is the installation as it always was
  (/data/food-tinder.db, /data/img, /data/rezeptbilder). An instance admin (an admin of household 1) can create more
  households. Each one gets a database file of its own with the same tables in /data/households/<id>/ (store.py), so
  the data of two households cannot mix. Every account belongs to exactly one household (users.household_id) and has
  its role there (admin | member). API tokens belong to the household they were created in (api_tokens.household_id),
  and the internal tokens of the AI runs and the PDF renderer exist once per household (new on every start).
  Accounts, sessions, API tokens, households, invitations and preferences are in the instance database (household 1's
  file), next to household 1's data.
Invitations
  A household admin creates an invitation (link or code, valid 1–30 days, single use, can be revoked). Whoever opens it
  chooses a username and password (or signs in with OIDC) and lands in that household with the invitation's role.
  Only the SHA-256 of the code is stored, so the code is shown once.
People and preferences
  Each member keeps a display name and preferences: diet (vegetarian, vegan, pescatarian), no spicy food, disliked
  ingredients, free notes. The number of people (servings) is the number of members unless the household sets it
  (children, someone without an account). The AI prompts (jobs.py) get the number and everybody's preferences.
Match
  With more than one member everybody swipes the stack themselves. A card becomes a like (saved, then imported) only
  when all current members liked it; ★ (this week) counts as a like and plans the card when someone starred it.
  One "no" ends it. Alone everything works as before. The votes are in the table card_votes of the household database;
  the external worker and the import job keep seeing only cards with status like.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import threading
from datetime import datetime, timedelta, timezone

import auth
import config
import store
from auth import AuthError

DIETS = ("", "vegetarian", "vegan", "pescatarian")
DIET_TEXT = {"vegetarian": "vegetarian", "vegan": "vegan", "pescatarian": "pescatarian (fish, no meat)"}
ROLES = ("admin", "member")
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
_lock = threading.Lock()
_internal = {}  # household id → (agent token, PDF token) for households other than 1 (household 1 keeps auth.AGENT_TOKEN, auth.INTERNAL_TOKEN)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(t):
    return hashlib.sha256(t.encode()).hexdigest()


def init():
    """Instance tables and columns (after auth.init). Existing accounts and tokens belong to household 1."""
    with store.lock, store.main() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS households (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL DEFAULT '',
              created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS invites (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              household_id INTEGER NOT NULL,
              code_hash TEXT UNIQUE NOT NULL,
              role TEXT NOT NULL DEFAULT 'member',  -- admin | member
              note TEXT NOT NULL DEFAULT '',
              created_by INTEGER, created TEXT NOT NULL, expires TEXT NOT NULL,
              used TEXT, used_by INTEGER,
              revoked INTEGER NOT NULL DEFAULT 0
            );
            -- preferences of a person (account); the AI runs respect them
            CREATE TABLE IF NOT EXISTS member_prefs (
              user_id INTEGER PRIMARY KEY,
              diet TEXT NOT NULL DEFAULT '',        -- '' | vegetarian | vegan | pescatarian
              no_spicy INTEGER NOT NULL DEFAULT 0,
              dislikes TEXT NOT NULL DEFAULT '[]',  -- JSON list of ingredients
              notes TEXT NOT NULL DEFAULT '',
              updated TEXT
            );
        """)
        for table in ("users", "api_tokens"):
            if "household_id" not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN household_id INTEGER NOT NULL DEFAULT 1")
        c.execute("INSERT OR IGNORE INTO households(id, name, created) VALUES(1, '', ?)", (now(),))
    auth.INVITE_HOOK = (_check_for_oidc, _bind)


def init_household():
    """Folder and the household's own tables (card_votes) of the current household; before the other modules' init."""
    os.makedirs(store.data_dir(), exist_ok=True)
    with store.con() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS card_votes (
            card_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            vote TEXT NOT NULL,                     -- like | nope | plan
            at TEXT NOT NULL, PRIMARY KEY (card_id, user_id))""")


# ---------- households ----------
def ids():
    with store.main() as c:
        return [r["id"] for r in c.execute("SELECT id FROM households ORDER BY id")]


def info(hid=None):
    hid = store.household_id() if hid is None else int(hid)
    with store.main() as c:
        r = c.execute("SELECT * FROM households WHERE id=?", (hid,)).fetchone()
    return {"id": hid, "name": r["name"] if r else ""}


def overview():
    """All households with their member counts (instance admins)."""
    with store.main() as c:
        rows = c.execute("SELECT h.*, (SELECT COUNT(*) FROM users u WHERE u.household_id=h.id AND u.disabled=0) members,"
                         " (SELECT COUNT(*) FROM users u WHERE u.household_id=h.id AND u.disabled=0 AND u.role='admin') admins,"
                         " (SELECT COUNT(*) FROM invites i WHERE i.household_id=h.id AND i.used IS NULL AND i.revoked=0 AND i.expires>?) open_invites"
                         " FROM households h ORDER BY h.id", (now(),)).fetchall()
    return [dict(r) for r in rows]


def _clean_name(name):
    return re.sub(r"\s+", " ", str(name or "")).strip()[:80]


def create(name):
    """A new, empty household. → id (the caller creates its tables in store.in_household(id))."""
    name = _clean_name(name)
    if not name:
        raise AuthError("Name fehlt")
    with store.lock, store.main() as c:
        return c.execute("INSERT INTO households(name, created) VALUES(?,?)", (name, now())).lastrowid


def rename(hid, name):
    name = _clean_name(name)
    if not name and int(hid) != 1:
        raise AuthError("Name fehlt")
    with store.lock, store.main() as c:
        if not c.execute("UPDATE households SET name=? WHERE id=?", (name, int(hid))).rowcount:
            raise AuthError("Haushalt nicht gefunden", 404)
    return info(hid)


def delete(hid):
    """Delete an empty household (no accounts left) with its data folder. Household 1 stays."""
    hid = int(hid)
    if hid == 1:
        raise AuthError("Der erste Haushalt bleibt")
    with store.lock, store.main() as c:
        if not c.execute("SELECT 1 FROM households WHERE id=?", (hid,)).fetchone():
            raise AuthError("Haushalt nicht gefunden", 404)
        if c.execute("SELECT 1 FROM users WHERE household_id=?", (hid,)).fetchone():
            raise AuthError("Im Haushalt gibt es noch Konten", 409)
        c.execute("DELETE FROM invites WHERE household_id=?", (hid,))
        c.execute("DELETE FROM api_tokens WHERE household_id=?", (hid,))
        c.execute("DELETE FROM households WHERE id=?", (hid,))
    shutil.rmtree(store.data_dir(hid), ignore_errors=True)
    _internal.pop(hid, None)
    return {"ok": True}


# ---------- who is asking, in which household ----------
def agent_token(hid=None):
    """Token of the AI runs inside the app (jobs.py) for a household."""
    hid = store.household_id() if hid is None else int(hid)
    return auth.AGENT_TOKEN if hid == 1 else _internal.setdefault(hid, (secrets.token_urlsafe(32), secrets.token_urlsafe(32)))[0]


def internal_token(hid=None):
    """Token of the PDF renderer (printing.py) for a household: only the print view and images."""
    hid = store.household_id() if hid is None else int(hid)
    return auth.INTERNAL_TOKEN if hid == 1 else _internal.setdefault(hid, (secrets.token_urlsafe(32), secrets.token_urlsafe(32)))[1]


def identify(headers, query=None):
    """auth.identify plus the household: the same dict with household_id, or None."""
    a = headers.get("Authorization") or ""
    bearer = a[7:].strip() if a.startswith("Bearer ") else None
    if bearer:
        for hid, (agent, internal) in list(_internal.items()):
            if hmac.compare_digest(bearer, agent):
                return {"id": 0, "username": "ai", "role": "member", "via": "agent", "scope": "api", "household_id": hid}
            if hmac.compare_digest(bearer, internal):
                return {"id": 0, "username": "pdf", "role": "member", "via": "internal", "scope": "read", "household_id": hid}
    u = auth.identify(headers, query)
    if not u:
        return None
    if u["via"] in ("agent", "internal"):
        return {**u, "household_id": 1}
    with store.main() as c:
        if u["via"] == "token":
            t = bearer or ((query or {}).get("token") or [""])[0]
            r = c.execute("SELECT household_id FROM api_tokens WHERE token_hash=?", (_sha(t),)).fetchone()
        else:
            r = c.execute("SELECT household_id FROM users WHERE id=?", (u["id"],)).fetchone()
        if not r or not c.execute("SELECT 1 FROM households WHERE id=?", (r["household_id"],)).fetchone():
            return None
    return {**u, "household_id": r["household_id"]}


def instance_admin(u):
    """Admins of household 1 (signed in) manage the installation: households, backup."""
    return bool(u and u.get("via") == "session" and u.get("role") == "admin" and u.get("household_id") == 1)


# ---------- members, accounts and API tokens of a household ----------
def members(hid=None):
    """Active accounts of the household: [{id, username, name, role}]."""
    hid = store.household_id() if hid is None else int(hid)
    with store.main() as c:
        rows = c.execute("SELECT id, username, display_name, role FROM users WHERE household_id=? AND disabled=0 ORDER BY id", (hid,)).fetchall()
    return [{"id": r["id"], "username": r["username"], "name": r["display_name"] or r["username"], "role": r["role"]} for r in rows]


def member_ids(hid=None):
    return [m["id"] for m in members(hid)]


def _user_household(uid):
    with store.main() as c:
        r = c.execute("SELECT household_id FROM users WHERE id=?", (uid,)).fetchone()
    return r["household_id"] if r else None


def users(hid):
    with store.main() as c:
        mine = {r["id"] for r in c.execute("SELECT id FROM users WHERE household_id=?", (hid,))}
    return [u for u in auth.users() if u["id"] in mine]


def create_user(hid, username, password, display_name="", email="", role="member"):
    uid = auth.create_user(username, password, display_name, email, role)
    with store.lock, store.main() as c:
        c.execute("UPDATE users SET household_id=? WHERE id=?", (hid, uid))
    return uid


def update_user(hid, uid, actor, data):
    """Change or delete an account of the household. The household keeps at least one admin."""
    if _user_household(uid) != hid:
        raise AuthError("Konto nicht gefunden", 404)
    with store.main() as c:
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        admins = c.execute("SELECT COUNT(*) FROM users WHERE household_id=? AND role='admin' AND disabled=0", (hid,)).fetchone()[0]
    if u["role"] == "admin" and not u["disabled"] and admins <= 1:
        if data.get("delete"):
            raise AuthError("Das eigene Konto und den letzten Admin kann man nicht löschen")
        if data.get("role") == "member" or data.get("disabled"):
            raise AuthError("Es muss einen Admin geben")
    r = auth.update_user(uid, actor, data)
    if data.get("delete") or data.get("disabled"):
        with store.in_household(hid):
            if data.get("delete"):
                with store.lock, store.main() as c:
                    c.execute("DELETE FROM member_prefs WHERE user_id=?", (uid,))
                with store.lock, store.con() as c:
                    c.execute("DELETE FROM card_votes WHERE user_id=?", (uid,))
            settle()
    return r


def tokens(hid):
    return [t for t in auth.tokens() if t.get("household_id") == hid]


def create_token(hid, name, scope, user_id):
    t = auth.create_token(name, scope, user_id)
    with store.lock, store.main() as c:
        c.execute("UPDATE api_tokens SET household_id=? WHERE id=?", (hid, t["id"]))
    return t


def revoke_token(hid, tid):
    with store.main() as c:
        r = c.execute("SELECT household_id FROM api_tokens WHERE id=?", (tid,)).fetchone()
    if not r or r["household_id"] != hid:
        raise AuthError("Token nicht gefunden", 404)
    return auth.revoke_token(tid)


# ---------- invitations ----------
def _norm(code):
    return re.sub(r"[^A-Z0-9]", "", str(code or "").upper())[:40]


def _invite_out(r):
    state = "revoked" if r["revoked"] else "used" if r["used"] else "expired" if r["expires"] <= now() else "open"
    return {k: r[k] for k in ("id", "role", "note", "created", "expires", "used")} | {"state": state, "used_by": r["used_by_name"]}


def invites(hid):
    with store.main() as c:
        rows = c.execute("SELECT i.*, COALESCE(NULLIF(u.display_name, ''), u.username) used_by_name FROM invites i"
                         " LEFT JOIN users u ON u.id = i.used_by WHERE i.household_id=? ORDER BY i.id DESC LIMIT 50", (hid,)).fetchall()
    return [_invite_out(r) for r in rows]


def create_invite(hid, actor_id, role="member", days=7, note=""):
    """→ {id, code, role, expires}; the code is shown only this once."""
    if role not in ROLES:
        raise AuthError("Rolle admin oder member")
    try:
        days = max(1, min(30, int(days or 7)))
    except (TypeError, ValueError):
        days = 7
    raw = "".join(secrets.choice(CODE_CHARS) for _ in range(12))
    code = f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
    with store.lock, store.main() as c:
        if not c.execute("SELECT 1 FROM households WHERE id=?", (hid,)).fetchone():
            raise AuthError("Haushalt nicht gefunden", 404)
        iid = c.execute("INSERT INTO invites(household_id, code_hash, role, note, created_by, created, expires) VALUES(?,?,?,?,?,?,?)",
                        (hid, _sha(_norm(code)), role, str(note or "").strip()[:120], actor_id or None, now(), expires)).lastrowid
    return {"id": iid, "code": code, "role": role, "expires": expires}


def revoke_invite(hid, iid):
    with store.lock, store.main() as c:
        if not c.execute("UPDATE invites SET revoked=1 WHERE id=? AND household_id=? AND used IS NULL AND revoked=0", (iid, hid)).rowcount:
            raise AuthError("Einladung nicht gefunden", 404)
    return {"ok": True}


def _valid(code):
    with store.main() as c:
        r = c.execute("SELECT i.*, h.name household_name FROM invites i JOIN households h ON h.id = i.household_id"
                      " WHERE i.code_hash=?", (_sha(_norm(code)),)).fetchone()
    if not r or r["revoked"] or r["used"] or r["expires"] <= now():
        return None
    return dict(r)


def check_invite(code, ip):
    """What the invitation page shows: household name, role, expiry. Wrong codes count like wrong passwords."""
    if not auth._rate_ok(ip):
        raise AuthError("Zu viele Versuche, bitte in ein paar Minuten nochmal", 429)
    r = _valid(code)
    if not r:
        auth._failed(ip)
        raise AuthError("Einladung ungültig oder abgelaufen", 404)
    return {"household": r["household_name"], "household_id": r["household_id"], "role": r["role"], "expires": r["expires"]}


def _check_for_oidc(code):
    r = _valid(code)
    if not r:
        raise AuthError("Einladung ungültig oder abgelaufen", 403)
    return r


def _bind(inv, uid):
    """Put a fresh account into the invitation's household and use the invitation up."""
    with store.lock, store.main() as c:
        if not c.execute("UPDATE invites SET used=?, used_by=? WHERE id=? AND used IS NULL AND revoked=0", (now(), uid, inv["id"])).rowcount:
            c.execute("DELETE FROM users WHERE id=?", (uid,))
            raise AuthError("Einladung ungültig oder abgelaufen", 403)
        c.execute("UPDATE users SET household_id=?, role=? WHERE id=?", (inv["household_id"], inv["role"], uid))


def redeem(code, username, password, display_name, ip, agent=""):
    """Create the account of an invited person. → session token"""
    if not auth._rate_ok(ip):
        raise AuthError("Zu viele Versuche, bitte in ein paar Minuten nochmal", 429)
    with _lock:
        inv = _valid(code)
        if not inv:
            auth._failed(ip)
            raise AuthError("Einladung ungültig oder abgelaufen", 404)
        uid = auth.create_user(username, password, str(display_name or "").strip()[:80] or username, "", inv["role"])
        _bind(inv, uid)
    return auth.new_session(uid, agent)


# ---------- people and preferences ----------
def _prefs_row(r):
    return {"diet": r["diet"] if r and r["diet"] in DIETS else "", "no_spicy": bool(r and r["no_spicy"]),
            "dislikes": json.loads(r["dislikes"]) if r else [], "notes": r["notes"] if r else ""}


def prefs(uids):
    if not uids:
        return {}
    with store.main() as c:
        rows = {r["user_id"]: r for r in c.execute(f"SELECT * FROM member_prefs WHERE user_id IN ({','.join('?' * len(uids))})", list(uids))}
    return {u: _prefs_row(rows.get(u)) for u in uids}


def save_prefs(hid, uid, data):
    """Display name and preferences of a member of household hid."""
    if _user_household(uid) != hid:
        raise AuthError("Konto nicht gefunden", 404)
    cur = prefs([uid])[uid]
    if "diet" in data:
        if data["diet"] not in DIETS:
            raise AuthError("diet: " + "|".join(d for d in DIETS if d))
        cur["diet"] = data["diet"]
    if "no_spicy" in data:
        cur["no_spicy"] = bool(data["no_spicy"])
    if "dislikes" in data:
        items = data["dislikes"]
        if isinstance(items, str):
            items = items.split(",")
        seen, out = set(), []
        for z in items or []:
            z = re.sub(r"\s+", " ", str(z)).strip()[:60]
            if z and z.lower() not in seen:
                seen.add(z.lower())
                out.append(z)
        cur["dislikes"] = out[:30]
    if "notes" in data:
        cur["notes"] = str(data["notes"] or "").strip()[:300]
    with store.lock, store.main() as c:
        c.execute("INSERT OR REPLACE INTO member_prefs(user_id, diet, no_spicy, dislikes, notes, updated) VALUES(?,?,?,?,?,?)",
                  (uid, cur["diet"], 1 if cur["no_spicy"] else 0, json.dumps(cur["dislikes"], ensure_ascii=False), cur["notes"], now()))
        if "display_name" in data:
            name = re.sub(r"\s+", " ", str(data["display_name"] or "")).strip()[:80]
            if name:
                c.execute("UPDATE users SET display_name=? WHERE id=?", (name, uid))
    return {"ok": True, **cur}


def people(hid=None):
    """Members with their preferences and the number of people the household cooks for."""
    import household
    ms = members(hid)
    p = prefs([m["id"] for m in ms])
    stored = household.get().get("people")
    return {"people": stored, "people_count": stored or len(ms) or 1,
            "members": [{**m, "prefs": p[m["id"]]} for m in ms]}


def people_text(count=None):
    """The people of the household for the AI prompts (jobs.py)."""
    pp = people()
    n = int(count or pp["people_count"])
    out = [f"{n} {'person' if n == 1 else 'people'}" + (" for this plan (guests included)" if count else "")
           + f"; plan servings for {n} unless the wish says otherwise."]
    for m in pp["members"]:
        f = m["prefs"]
        parts = ([DIET_TEXT[f["diet"]]] if f["diet"] else []) + (["no spicy food"] if f["no_spicy"] else []) \
            + ([f"dislikes {', '.join(f['dislikes'])}"] if f["dislikes"] else []) + ([f"notes: {f['notes']}"] if f["notes"] else [])
        out.append(f"- {m['name']}: {'; '.join(parts) if parts else 'no special preferences'}")
    if n > len(pp["members"]) and pp["members"]:
        out.append(f"- {n - len(pp['members'])} more without an account (e.g. children or guests)")
    return "\n".join(out)


# ---------- household match on the swipe cards ----------
def voter(u):
    """Account id whose swipe is a personal vote (signed-in members); API tokens and AI runs decide for the household."""
    return u["id"] if u and u.get("via") == "session" and u.get("id") else None


def _name_map(ms):
    return {m["id"]: m["name"] for m in ms}


def _settle_card(c, cid, mids):
    """Status of a card from the votes of the current members: one no → nope, all liked → like, else stays neu."""
    if not mids:
        return None
    votes = {r["user_id"]: r["vote"] for r in c.execute("SELECT user_id, vote FROM card_votes WHERE card_id=?", (cid,)) if r["user_id"] in mids}
    if any(v == "nope" for v in votes.values()):
        c.execute("UPDATE cards SET status='nope', plan=0, swiped=? WHERE id=? AND status='neu'", (now(), cid))
        return "nope"
    if all(m in votes for m in mids):
        c.execute("UPDATE cards SET status='like', plan=?, swiped=? WHERE id=? AND status='neu'",
                  (1 if any(v == "plan" for v in votes.values()) else 0, now(), cid))
        return "like"
    return "neu"


def settle():
    """After a member left: decide the cards that only waited for them (current household)."""
    mids = member_ids()
    if not mids:
        return
    with store.lock, store.con() as c:
        for r in c.execute("SELECT DISTINCT v.card_id FROM card_votes v JOIN cards k ON k.id = v.card_id WHERE k.status='neu'").fetchall():
            _settle_card(c, r["card_id"], mids)


def vote(c, cid, uid, action, ms):
    """A member's swipe with several members. c: open connection of the household database. → answer for the app or None (already swiped)"""
    if c.execute("SELECT 1 FROM card_votes WHERE card_id=? AND user_id=?", (cid, uid)).fetchone():
        return None
    c.execute("INSERT INTO card_votes(card_id, user_id, vote, at) VALUES(?,?,?,?)", (cid, uid, action, now()))
    mids = [m["id"] for m in ms]
    status = _settle_card(c, cid, mids)
    names = _name_map(ms)
    voted = {r["user_id"] for r in c.execute("SELECT user_id FROM card_votes WHERE card_id=?", (cid,))}
    return {"match": status == "like", "decided": status != "neu", "status": status, "members": len(mids),
            "waiting": [names[m] for m in mids if m not in voted] if status == "neu" else []}


def record(c, cid, uid, action):
    """With one member the swipe decides right away; the vote is kept anyway, so a second member later sees the history."""
    c.execute("INSERT OR REPLACE INTO card_votes(card_id, user_id, vote, at) VALUES(?,?,?,?)", (cid, uid, action, now()))


def waiting(c, uid, ms):
    """Cards this member liked that still wait for others: [{card id, waiting: [names], liked_by: [names]}]."""
    names, mids = _name_map(ms), [m["id"] for m in ms]
    out = []
    for r in c.execute("SELECT k.id FROM cards k JOIN card_votes v ON v.card_id = k.id AND v.user_id=? AND v.vote IN ('like','plan')"
                       " WHERE k.status='neu' ORDER BY v.at DESC LIMIT 100", (uid,)).fetchall():
        voted = {x["user_id"]: x["vote"] for x in c.execute("SELECT user_id, vote FROM card_votes WHERE card_id=?", (r["id"],))}
        out.append({"id": r["id"], "waiting": [names[m] for m in mids if m not in voted],
                    "liked_by": [names[m] for m in mids if voted.get(m) in ("like", "plan")]})
    return out
