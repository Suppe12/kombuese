"""Share a recipe by link: a public, read-only page of one recipe that anyone can open without an account.

A link is a random token (24 bytes, URL-safe) per recipe and household, kept in the instance database (table shares)
because the token alone has to find its household. One link per recipe: sharing again returns the same link until it is
revoked; revoking deletes the row, so the link is gone for good (sharing later makes a new one).
What the link opens (server.py, routes /s/<token>…): the recipe page (printing.recipe_page in share mode), this recipe's
photo and its PDF. Nothing else: no API, no other recipe, no other image. The page carries noindex, no referrer and a
strict Content-Security-Policy. Unknown tokens count against the asking address (rate limit), so guessing is pointless
on top of 192 random bits."""
import secrets
import threading
import time

import store
from store import Error

_lock = threading.Lock()
_seen = {}  # ip -> {"all": [timestamps], "bad": [...], "pdf": [...]}
WINDOW = 600  # seconds
LIMITS = {"all": 300, "bad": 20, "pdf": 10}  # per address and window


def now():
    return store.now()


def init():
    """Instance table (household 1's file, after households.init)."""
    with store.lock, store.main() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS shares (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              token TEXT UNIQUE NOT NULL,
              household_id INTEGER NOT NULL,
              recipe_id TEXT NOT NULL,              -- rezept.id: the link survives renaming the recipe
              created_by INTEGER,
              created TEXT NOT NULL,
              views INTEGER NOT NULL DEFAULT 0,
              last_view TEXT
            );
            CREATE INDEX IF NOT EXISTS shares_recipe ON shares(household_id, recipe_id);
        """)


def _out(r, recipe=None, base=""):
    return {"id": r["id"], "url": f"{base}/s/{r['token']}", "token": r["token"], "slug": (recipe or {}).get("slug"),
            "name": (recipe or {}).get("name"), "erstellt": r["created"], "aufrufe": r["views"], "zuletzt": r["last_view"],
            "von": r["created_by"]}


def for_recipe(slug, base=""):
    """The link of a recipe in the current household, or None."""
    r = store.recipe(slug)
    with store.main() as c:
        s = c.execute("SELECT * FROM shares WHERE household_id=? AND recipe_id=?", (store.household_id(), r["id"])).fetchone()
    return _out(s, r, base) if s else None


def create(slug, uid, base=""):
    """Link for a recipe (the existing one, if there is one)."""
    r = store.recipe(slug)
    hid = store.household_id()
    with store.lock, store.main() as c:
        s = c.execute("SELECT * FROM shares WHERE household_id=? AND recipe_id=?", (hid, r["id"])).fetchone()
        if not s:
            c.execute("INSERT INTO shares(token, household_id, recipe_id, created_by, created) VALUES(?,?,?,?,?)",
                      (secrets.token_urlsafe(24), hid, r["id"], uid or None, now()))
            s = c.execute("SELECT * FROM shares WHERE household_id=? AND recipe_id=?", (hid, r["id"])).fetchone()
    return _out(s, r, base)


def list_all(base=""):
    """All links of the current household, newest first. Links of deleted recipes are removed on the way."""
    hid = store.household_id()
    with store.main() as c:
        rows = c.execute("SELECT * FROM shares WHERE household_id=? ORDER BY id DESC", (hid,)).fetchall()
    out, gone = [], []
    for s in rows:
        try:
            out.append(_out(s, store.recipe(s["recipe_id"]), base))
        except Error:
            gone.append(s["id"])
    if gone:
        with store.lock, store.main() as c:
            c.executemany("DELETE FROM shares WHERE id=?", [(i,) for i in gone])
    return out


def get(sid):
    with store.main() as c:
        r = c.execute("SELECT * FROM shares WHERE id=? AND household_id=?", (int(sid), store.household_id())).fetchone()
    if not r:
        raise Error("Link nicht gefunden")
    return r


def revoke(sid):
    """Delete a link of the current household; it stops working at once."""
    with store.lock, store.main() as c:
        if not c.execute("DELETE FROM shares WHERE id=? AND household_id=?", (int(sid), store.household_id())).rowcount:
            raise Error("Link nicht gefunden")
    return {"ok": True}


def remove_household(hid):
    with store.lock, store.main() as c:
        c.execute("DELETE FROM shares WHERE household_id=?", (int(hid),))


def resolve(token, count=False):
    """Token → (household id, recipe) or None. The recipe has to exist in that household."""
    if not token or len(token) > 64:
        return None
    with store.main() as c:
        s = c.execute("SELECT * FROM shares WHERE token=?", (token,)).fetchone()
    if not s:
        return None
    with store.in_household(s["household_id"]):
        try:
            r = store.recipe(s["recipe_id"])
        except Error:
            return None
    if count:
        with store.lock, store.main() as c:
            c.execute("UPDATE shares SET views=views+1, last_view=? WHERE id=?", (now(), s["id"]))
    return s["household_id"], r


def allowed(ip, kind="all"):
    """Rate limit per address: False = too many requests (or too many unknown links) in the last 10 minutes."""
    t = time.time()
    with _lock:
        if len(_seen) > 5000:  # keep the table small
            for k in [k for k, v in _seen.items() if not any(t - x < WINDOW for x in v["all"])]:
                _seen.pop(k, None)
        d = _seen.setdefault(ip, {"all": [], "bad": [], "pdf": []})
        for k in d:
            d[k] = [x for x in d[k] if t - x < WINDOW]
        if len(d["bad"]) >= LIMITS["bad"] or len(d[kind]) >= LIMITS[kind]:
            return False
        d[kind].append(t)
        if kind != "all":
            d["all"].append(t)
        return True


def failed(ip):
    with _lock:
        _seen.setdefault(ip, {"all": [], "bad": [], "pdf": []})["bad"].append(time.time())
