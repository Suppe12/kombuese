"""Nutrition overview without Yazio: what each person ate, their goals and what the week plan still holds.

Eaten meals are kept in the household's table gegessen, one row per plan entry and person (older versions: one row per plan
entry). „Eaten“ (POST /api/gegessen) always writes here; Yazio gets the entry as well when it is connected (once per plan
entry, or only for the person set in meta yazio_person). Older entries belong to the household's first admin.
Goals per person (kcal and protein per day, empty = no bars) are in the table nutrition_goals of the household.
Planned nutrients: one serving per person of every plan entry of a day that has a recipe with nutrients
(leftover entries „Rest: …“ by the recipe name, like week.meal)."""
import json
from datetime import date, timedelta

import household
import households
import store

KEYS = ("kcal", "protein", "kh", "fett")
LIMITS = {"kcal": 10000, "protein": 1000}


def default_person(hid=None):
    """Whose meals they are when nobody signed in is asking (API tokens, AI runs, older entries): the first admin."""
    ms = households.members(hid)
    admins = [m["id"] for m in ms if m["role"] == "admin"]
    return admins[0] if admins else (ms[0]["id"] if ms else None)


def person(u):
    """The person a request speaks for: the signed-in account, the account an API token belongs to, else the default."""
    ids = households.member_ids()
    if u and u.get("via") in ("session", "token") and u.get("id") in ids:
        return u["id"]
    return default_person()


def init():
    """Tables of the current household; moves the table gegessen to one row per person (2.7)."""
    owner = default_person()
    with store.lock:
        c = store.con()
        try:
            c.isolation_level = None  # one explicit transaction, DDL included: the move happens completely or not at all
            c.execute("BEGIN IMMEDIATE")
            c.execute("""CREATE TABLE IF NOT EXISTS nutrition_goals (
                user_id INTEGER PRIMARY KEY,
                kcal REAL, protein REAL,             -- per day, NULL = no goal
                updated TEXT)""")
            c.execute(TABLE)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(gegessen)")}
            if "user_id" not in cols:
                c.execute("ALTER TABLE gegessen RENAME TO gegessen_vor_27")
                c.execute(TABLE)
                c.execute("INSERT INTO gegessen(mealplan_id, user_id, datum, name, portionen, kcal, protein, fett, kh, yazio_id, am)"
                          " SELECT mealplan_id, ?, datum, name, portionen, kcal, protein, fett, kh, yazio_id, am FROM gegessen_vor_27", (owner,))
                c.execute("DROP TABLE gegessen_vor_27")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS gegessen_person ON gegessen(mealplan_id, user_id)")
            c.execute("CREATE INDEX IF NOT EXISTS gegessen_datum ON gegessen(datum)")
            c.execute("COMMIT")
        except Exception:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise
        finally:
            c.close()


# eaten meals: one row per plan entry and person; recipe menu entries have a negative mealplan_id
TABLE = """CREATE TABLE IF NOT EXISTS gegessen (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              mealplan_id INTEGER NOT NULL,
              user_id INTEGER,                      -- who ate it
              datum TEXT NOT NULL,
              typ TEXT NOT NULL DEFAULT '',
              name TEXT NOT NULL,
              slug TEXT NOT NULL DEFAULT '',
              portionen REAL NOT NULL,
              kcal REAL, protein REAL, fett REAL, kh REAL,
              yazio_id TEXT,
              am TEXT NOT NULL
            )"""


# ---------- goals ----------
def goals(uids):
    if not uids:
        return {}
    with store.con() as c:
        rows = {r["user_id"]: r for r in c.execute(
            f"SELECT * FROM nutrition_goals WHERE user_id IN ({','.join('?' * len(uids))})", list(uids))}
    return {u: {"kcal": rows[u]["kcal"] if u in rows else None, "protein": rows[u]["protein"] if u in rows else None} for u in uids}


def _goal_value(v, key):
    if v in (None, "", 0, "0"):
        return None
    try:
        x = float(str(v).replace(",", "."))
    except ValueError:
        raise store.Error(f"Ungültiges Ziel: {key}") from None
    if not 0 < x <= LIMITS[key]:
        raise store.Error(f"Ungültiges Ziel: {key}")
    return round(x)


def save_goal(uid, data):
    if uid not in households.member_ids():
        raise store.Error("Person nicht gefunden")
    cur = goals([uid])[uid]
    new = {k: _goal_value(data[k], k) if k in data else cur[k] for k in ("kcal", "protein")}
    with store.lock, store.con() as c:
        c.execute("INSERT OR REPLACE INTO nutrition_goals(user_id, kcal, protein, updated) VALUES(?,?,?,?)",
                  (uid, new["kcal"], new["protein"], store.now()))
    return {"ok": True, "user_id": uid, "ziel": new}


def yazio_person():
    with store.con() as c:
        r = c.execute("SELECT value FROM meta WHERE key='yazio_person'").fetchone()
    try:
        return int(json.loads(r["value"])) if r and json.loads(r["value"]) else None
    except (ValueError, TypeError):
        return None


def set_yazio_person(uid):
    if uid is not None and uid not in households.member_ids():
        raise store.Error("Person nicht gefunden")
    with store.lock, store.con() as c:
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('yazio_person', ?)", (json.dumps(uid),))
    return {"ok": True, "yazio_person": uid}


# ---------- planned ----------
def _per_serving(e, cache):
    """Nutrients per serving of a plan entry (dict of KEYS) or None."""
    r = e["rezept"]
    key = r["slug"] if r else household.strip_leftover(e["titel_roh"] or "")
    if not key:
        return None
    if key not in cache:
        try:
            full = store.recipe(r["slug"]) if r else store.find_recipe(key)
            n = (full or {}).get("naehrwerte") or {}
            cache[key] = {k: n.get(k) for k in KEYS} if n.get("kcal") else None
        except store.Error:
            cache[key] = None
    return cache[key]


def planned(start, end):
    """Plan entries per day with their nutrients per serving: {datum: [{id, typ, titel, nw | None}]}."""
    out, cache = {}, {}
    for e in store.plan_week(start, end):
        nw = _per_serving(e, cache)
        title = (e["rezept"] or {}).get("name") or (e["titel_roh"] or "").strip() or "?"
        out.setdefault(e["date"], []).append({"id": e["id"], "typ": e["typ"], "titel": title, "nw": nw})
    return out


def _sum(rows, nw=lambda r: r):
    s = {k: 0.0 for k in KEYS}
    for r in rows:
        v = nw(r)
        for k in KEYS:
            s[k] += float((v or {}).get(k) or 0)
    return {k: round(v, 1) for k, v in s.items()}


# ---------- overview ----------
def overview(start=None, days=7, me=None):
    """Per person and day: eaten, planned and still open (planned but not eaten yet), goal; the household's plan."""
    try:
        first = date.fromisoformat(start) if start else None
    except ValueError:
        first = None
    today = date.today()
    if not first:
        first = today - timedelta(days=today.weekday())
    try:
        days = max(1, min(31, int(days or 7)))
    except ValueError:
        days = 7
    last = first + timedelta(days=days - 1)
    dates = [str(first + timedelta(days=i)) for i in range(days)]
    ms = households.members()
    if not ms:
        ms = []
    with store.con() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM gegessen WHERE datum BETWEEN ? AND ? ORDER BY datum, am", (str(first), str(last)))]
    plan = planned(first, last)
    g = goals([m["id"] for m in ms])
    people = []
    for m in ms:
        mine = [r for r in rows if r["user_id"] == m["id"]]
        logged = {r["mealplan_id"] for r in mine}
        per_day = []
        for d in dates:
            eaten = [r for r in mine if r["datum"] == d]
            open_ = [e for e in plan.get(d, []) if e["nw"] and e["id"] not in logged]
            per_day.append({"datum": d, "gegessen": _sum(eaten), "offen": _sum(open_, lambda e: e["nw"]),
                            "mahlzeiten": len(eaten), "offen_mahlzeiten": len(open_)})
        total = _sum([x["gegessen"] for x in per_day])
        with_food = [x for x in per_day if x["mahlzeiten"]]
        people.append({"id": m["id"], "name": m["name"], "ich": m["id"] == me, "ziel": g[m["id"]], "tage": per_day,
                       "summe": total, "schnitt": {k: round(total[k] / len(with_food), 1) for k in KEYS} if with_food else None,
                       "eintraege": [{k: r[k] for k in ("id", "mealplan_id", "datum", "typ", "name", "portionen", *KEYS)} for r in mine]})
    geplant = []
    for d in dates:
        es = plan.get(d, [])
        geplant.append({"datum": d, **_sum([e for e in es if e["nw"]], lambda e: e["nw"]),
                        "mahlzeiten": len([e for e in es if e["nw"]]), "ohne_naehrwerte": len([e for e in es if not e["nw"]])})
    return {"start": str(first), "ende": str(last), "heute": str(today), "personen": people, "geplant": geplant,
            "yazio_person": yazio_person()}
