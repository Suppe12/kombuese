"""Backup and moving house: all data of an installation in one .tar.gz file, and back.

Archive (kombuese-backup-YYYY-MM-DD.tar.gz):
  manifest.json       format, app version, date, counts, what was left out
  food-tinder.db      consistent SQLite snapshot (sqlite backup API, then VACUUM)
  integrations.json   connections and AI settings, see below
  secret.key          only in a backup "with credentials": a one-time transfer key (never the server's own key)
  img/…               swipe card images
  rezeptbilder/…      recipe images
  households/<id>/…  : every further household with its own food-tinder.db, img/ and rezeptbilder/

A backup is always the whole installation with all households (only instance admins, the admins of household 1,
download or import it). Household 1 sits at the top level as before, so older backups import as household 1
and a backup with one household looks exactly like before.

What travels:
- recipes, cards, week plan, meal prep, shopping list, freezer, cupboard, barcodes, AI run history, settings in meta
- accounts with their password hashes (scrypt) and API tokens (only their SHA-256 hashes are stored, so the archive holds
  no usable token; tokens keep working on the new server)
What does not:
- sessions (everybody signs in again; the admin who imports stays signed in)
- the server's secret.key. Stored credentials (table integrations) are encrypted with it, so the snapshot's table is
  emptied and integrations.json holds them instead:
  * default: only the AI settings (provider, model, limits, household description) without the API key. Bring!, Yazio,
    Telegram and ntfy are left out and set up again on the new server. The archive contains no secrets in plain text.
  * with credentials (secrets=True): all connections, encrypted with a random transfer key that is stored in the
    archive as secret.key. Whoever has the file can read them, so treat it like a password.
  * the automatic backup before an import (secrets="server") keeps the rows encrypted with this server's key and holds
    no key, so it only restores credentials on the same server (and does not weaken KOMBUESE_SECRET_KEY).
- old copies of the database next to it, the backups folder, files in KOMBUESE_SECRETS

Import: the archive is checked (manifest, format and app version, sizes, only expected names, regular files only),
unpacked into a temporary folder in the data directory, the database is checked (integrity_check), then an automatic
backup of the current data (with credentials, stays on the server) is written to <data>/backups/, then the database is
replaced through the sqlite backup API, the image folders are swapped and the normal start-up migrations run.
Connections are re-encrypted with this server's key; connections the archive does not bring along stay as they are.
"""
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
import zlib
from datetime import datetime, timedelta, timezone

import config
import secretbox
import store

FORMAT = "kombuese-backup"
FORMAT_VERSION = 1
MAX_BYTES = config.BACKUP_MAX_MB * 1024 * 1024    # upload (compressed)
MAX_UNPACKED = 4 * MAX_BYTES                      # sum of all files in the archive
MAX_FILES = 100000
KEEP_SAFETY = 3                                   # automatic backups before an import that are kept
IMAGE_DIRS = ("img", "rezeptbilder")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,150}")
SMALL = {"manifest.json": 1024 * 1024, "integrations.json": 1024 * 1024, "secret.key": 32}
INTEGRATIONS = ("ai", "bring", "yazio", "telegram", "ntfy")
COUNTS = {"recipes": "rezept", "cards": "cards", "plan": "plan", "shopping": "einkauf", "freezer": "vorrat",
          "cupboard": "schrank", "users": "users", "api_tokens": "api_tokens", "ai_runs": "ai_runs"}
_busy = threading.Lock()


class Error(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def _db_path(hid=1):
    return store.db_path(hid)


def _prefix(hid):
    """Path of a household inside the archive and below the data directory ('' for household 1)."""
    return "" if int(hid) == 1 else f"households/{int(hid)}/"


def _household_ids():
    """Further households (2, 3 …) of the live installation that have a database."""
    c = sqlite3.connect(_db_path(), timeout=30)
    try:
        if "households" not in _tables(c):
            return []
        hids = [r[0] for r in c.execute("SELECT id FROM households WHERE id<>1 ORDER BY id")]
    finally:
        c.close()
    return [h for h in hids if os.path.isfile(_db_path(h))]


def backups_dir():
    d = os.path.join(config.DATA, "backups")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def app_version():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "0"
    except OSError:
        return "0"


def _vtuple(v):
    try:
        return tuple(int(x) for x in str(v).strip().split("."))
    except ValueError:
        return None


def filename():
    return f"kombuese-backup-{datetime.now().strftime('%Y-%m-%d')}.tar.gz"


def _tables(c):
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def counts(c):
    have = _tables(c)
    return {k: (c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] if t in have else 0) for k, t in COUNTS.items()}


def _images(d):
    """(name, path) of the image files in a folder, without links and odd names."""
    if not os.path.isdir(d):
        return []
    return [(n, os.path.join(d, n)) for n in sorted(os.listdir(d))
            if NAME.fullmatch(n) and not os.path.islink(os.path.join(d, n)) and os.path.isfile(os.path.join(d, n))]


def _info(name, size, mtime=None):
    ti = tarfile.TarInfo(name)
    ti.size, ti.mode, ti.mtime = size, 0o600 if name == "secret.key" else 0o644, int(mtime or time.time())
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti


def _snapshot(tmpdir, with_secrets, key=None, hid=1):
    """Consistent copy of a household's live database without sessions and without encrypted rows.
    → (path, integrations part, counts)"""
    snap = os.path.join(tmpdir, f"{int(hid)}-{config.DB_FILE}")
    src, dst = sqlite3.connect(_db_path(hid), timeout=30), sqlite3.connect(snap)
    try:
        src.backup(dst)
    finally:
        src.close()
    dst.row_factory = sqlite3.Row
    dst.isolation_level = None
    have = _tables(dst)
    items, left_out, unreadable = {}, [], []
    if "integrations" in have:
        for r in dst.execute("SELECT name, data, updated FROM integrations ORDER BY name").fetchall():
            try:
                d = json.loads(secretbox.decrypt(r["data"]))
            except (ValueError, TypeError):
                unreadable.append(r["name"])
                continue
            if with_secrets == "server":
                items[r["name"]] = {"data": r["data"], "updated": r["updated"]}
            elif with_secrets:
                items[r["name"]] = {"data": secretbox.encrypt(json.dumps(d), secretbox.derive(key)), "updated": r["updated"]}
            elif r["name"] == "ai":
                d.pop("api_key", None)
                items["ai"] = {"data": d, "updated": r["updated"]}
            else:
                left_out.append(r["name"])
        dst.execute("DELETE FROM integrations")
    if "sessions" in have:
        dst.execute("DELETE FROM sessions")
    for t in ("push_pending", "push_subs", "push_vapid"):  # Web Push: the devices subscribe again after a restore
        if t in have:
            dst.execute(f"DELETE FROM {t}")
    dst.execute("VACUUM")  # deleted rows must not stay behind in free pages
    n = counts(dst)
    dst.close()
    return snap, {"items": items, "left_out": left_out, "unreadable": unreadable}, n


def export(path, with_secrets=False):
    """Write a backup of the whole installation to path (atomically through a temporary file next to it).
    with_secrets: False, True or "server", see above. → manifest"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix=".export-", dir=backups_dir())
    try:
        key = secrets.token_bytes(32) if with_secrets is True else None
        parts = {}  # household id → (snapshot, integrations part, counts, images)
        for hid in [1] + _household_ids():
            snap, integ_part, n = _snapshot(tmpdir, with_secrets, key, hid)
            parts[hid] = (snap, integ_part, n, {d: _images(os.path.join(store.data_dir(hid), d)) for d in IMAGE_DIRS})
        snap, top, n, imgs = parts[1]
        integ = {"encrypted": "server" if with_secrets == "server" else bool(with_secrets), **top}
        if len(parts) > 1:
            integ["households"] = {str(h): p[1] for h, p in parts.items() if h != 1}
        manifest = {"format": FORMAT, "format_version": FORMAT_VERSION, "app": "Kombüse", "app_version": app_version(),
                    "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "with_credentials": with_secrets if with_secrets == "server" else bool(with_secrets),
                    "counts": n, "images": {d: len(v) for d, v in imgs.items()},
                    "integrations": {"included": sorted(top["items"]), "left_out": top["left_out"] + top["unreadable"]}}
        if len(parts) > 1:
            manifest["households"] = {str(h): {"counts": p[2], "images": {d: len(v) for d, v in p[3].items()}} for h, p in parts.items() if h != 1}
        part = os.path.join(tmpdir, "archive.tar.gz")
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz", compresslevel=6) as tar:
            for name, data in (("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1).encode()),
                               ("integrations.json", json.dumps(integ, ensure_ascii=False).encode()),
                               *((("secret.key", key),) if key else ())):
                tar.addfile(_info(name, len(data)), io.BytesIO(data))
            for hid, (snap, _, _, imgs) in parts.items():
                with open(snap, "rb") as f:
                    tar.addfile(_info(_prefix(hid) + config.DB_FILE, os.path.getsize(snap)), f)
                for d, files in imgs.items():
                    for name, p in files:
                        try:
                            with open(p, "rb") as f:
                                st = os.fstat(f.fileno())
                                tar.addfile(_info(f"{_prefix(hid)}{d}/{name}", st.st_size, st.st_mtime), f)
                        except OSError:
                            pass  # deleted meanwhile
        shutil.move(part, path)
        return manifest
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- import ----------
def _household_part(parts):
    """['households', '2'] → True: the folder of a further household (ids from 2)."""
    return len(parts) == 2 and parts[0] == "households" and re.fullmatch(r"[1-9]\d{0,8}", parts[1]) is not None and parts[1] != "1"


def _dir_ok(name):
    parts = name.split("/")
    return (name in IMAGE_DIRS or name == "households" or _household_part(parts)
            or (len(parts) == 3 and _household_part(parts[:2]) and parts[2] in IMAGE_DIRS))


def _staged_households(staging):
    d = os.path.join(staging, "households")
    if not os.path.isdir(d):
        return []
    return sorted(int(h) for h in os.listdir(d) if _household_part(["households", h]))


def _check_db(p):
    with open(p, "rb") as f:
        if f.read(16) != b"SQLite format 3\x00":
            raise Error("Datenbank im Backup ist kaputt")
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise Error("Datenbank im Backup ist kaputt")
            if not _tables(c) & {"cards", "rezept"}:
                raise Error("Datenbank im Backup ist keine Datenbank der Kombüse")
        finally:
            c.close()
    except sqlite3.DatabaseError:
        raise Error("Datenbank im Backup ist kaputt")


def _unpack(src, staging):
    """Unpack the archive into staging. Everything not expected is refused. → set of names"""
    total, names = 0, set()
    try:
        tar = tarfile.open(src, mode="r:gz")
    except (tarfile.TarError, OSError, EOFError):
        raise Error("Keine Backup-Datei (erwartet wird eine .tar.gz der Kombüse)")
    for d in IMAGE_DIRS:
        os.makedirs(os.path.join(staging, d))
    try:
        with tar:
            for m in tar:  # lazily, so a gzip bomb stops at the first header beyond the limit
                name = m.name
                if name.startswith("./"):
                    name = name[2:]
                if m.isdir() and _dir_ok(name.rstrip("/")):
                    continue
                parts = name.split("/")
                ok = (m.isreg() and "\\" not in name and ".." not in parts and not name.startswith("/")
                      and (name in SMALL or name == config.DB_FILE
                           or (len(parts) == 2 and parts[0] in IMAGE_DIRS and NAME.fullmatch(parts[1]))
                           or (len(parts) >= 3 and _household_part(parts[:2])  # households/<id>/…
                               and (parts[2:] == [config.DB_FILE] or (len(parts) == 4 and parts[2] in IMAGE_DIRS and NAME.fullmatch(parts[3]))))))
                if not ok:
                    raise Error(f"Unerwartete Datei im Backup: {name[:120]}")
                if name in names:
                    raise Error(f"Doppelte Datei im Backup: {name[:120]}")
                if name in SMALL and m.size > SMALL[name]:
                    raise Error(f"Datei zu groß im Backup: {name}")
                names.add(name)
                total += m.size
                if total > MAX_UNPACKED:
                    raise Error("Backup ausgepackt zu groß")
                if len(names) > MAX_FILES:
                    raise Error("Zu viele Dateien im Backup")
                f = tar.extractfile(m)
                n = 0
                os.makedirs(os.path.join(staging, *parts[:-1]), exist_ok=True)
                with open(os.path.join(staging, *parts), "xb") as out:
                    while True:
                        chunk = f.read(1024 * 1024)
                        if not chunk:
                            break
                        n += len(chunk)
                        if n > m.size:
                            raise Error("Backup-Datei kaputt")
                        out.write(chunk)
    except (tarfile.TarError, EOFError, zlib.error, OSError) as e:
        raise Error(f"Backup-Datei kaputt ({type(e).__name__})")
    return names


def check(staging, names):
    """Manifest, version and database of an unpacked archive. → (manifest, integrations, transfer key)"""
    if "manifest.json" not in names:
        raise Error("manifest.json fehlt im Backup")
    try:
        with open(os.path.join(staging, "manifest.json"), encoding="utf-8") as f:
            man = json.load(f)
    except (ValueError, OSError):
        raise Error("manifest.json im Backup ist kaputt")
    if not isinstance(man, dict) or man.get("format") != FORMAT:
        raise Error("Das ist kein Backup der Kombüse")
    if not isinstance(man.get("format_version"), int) or not 1 <= man["format_version"] <= FORMAT_VERSION:
        raise Error(f"Backup-Format {str(man.get('format_version'))[:20]} wird nicht unterstützt")
    v, cur = _vtuple(man.get("app_version")), _vtuple(app_version())
    if v is None:
        raise Error("Version im Backup fehlt")
    if cur and v > cur:
        raise Error(f"Das Backup ist von einer neueren Version ({man['app_version']}). Bitte erst die Kombüse aktualisieren (jetzt {app_version()}).")
    if config.DB_FILE not in names:
        raise Error("Datenbank fehlt im Backup")
    _check_db(os.path.join(staging, config.DB_FILE))
    for hid in _staged_households(staging):  # further households: each folder needs its database
        p = os.path.join(staging, "households", str(hid), config.DB_FILE)
        if not os.path.isfile(p):
            raise Error(f"Datenbank von Haushalt {hid} fehlt im Backup")
        _check_db(p)
    integ = {"encrypted": False, "items": {}}
    if "integrations.json" in names:
        try:
            with open(os.path.join(staging, "integrations.json"), encoding="utf-8") as f:
                integ = json.load(f)
            assert isinstance(integ, dict) and isinstance(integ.get("items", {}), dict)
        except (ValueError, OSError, AssertionError):
            raise Error("integrations.json im Backup ist kaputt")
    key = None
    if integ.get("encrypted") is True:
        if "secret.key" not in names:
            raise Error("Schlüssel fehlt im Backup")
        with open(os.path.join(staging, "secret.key"), "rb") as f:
            key = f.read()
        if len(key) != 32:
            raise Error("Schlüssel im Backup ist kaputt")
    return man, integ, key


def _prune_safety():
    old = sorted(f for f in os.listdir(backups_dir()) if f.startswith("kombuese-before-import-") and f.endswith(".tar.gz"))
    for f in old[:-KEEP_SAFETY]:
        try:
            os.remove(os.path.join(backups_dir(), f))
        except OSError:
            pass


def _swap_images(staging):
    for d in IMAGE_DIRS:
        live, new, old = os.path.join(config.DATA, d), os.path.join(staging, d), os.path.join(staging, d + ".old")
        try:
            if os.path.isdir(live):
                os.replace(live, old)
            os.replace(new, live)
        except OSError:  # e.g. the folder is a mount point of its own: replace the files instead
            os.makedirs(live, exist_ok=True)
            for n in os.listdir(live):
                p = os.path.join(live, n)
                if os.path.isfile(p) or os.path.islink(p):
                    os.remove(p)
            for n, p in _images(new):
                shutil.copy2(p, os.path.join(live, n))


def _swap_households(staging):
    """Further households: the folder /data/households becomes the archive's (empty for older backups)."""
    live, new, old = os.path.join(config.DATA, "households"), os.path.join(staging, "households"), os.path.join(staging, "households.old")
    os.makedirs(new, exist_ok=True)
    try:
        if os.path.isdir(live):
            os.replace(live, old)
        os.replace(new, live)
    except OSError:  # a mount point of its own: replace the contents instead
        os.makedirs(live, exist_ok=True)
        for n in os.listdir(live):
            shutil.rmtree(os.path.join(live, n), ignore_errors=True)
        for n in os.listdir(new):
            shutil.copytree(os.path.join(new, n), os.path.join(live, n))


def restore(src, actor=None, session=None, after_init=None, lock=None):
    """Replace all data with the backup in file src.

        actor: the signed-in admin (dict with id) who keeps their account, password and session (token) afterwards.
        after_init: runs the start-up migrations (server.init_all). lock: the database write lock (store.lock).
        → summary for the app
    """
    if not _busy.acquire(blocking=False):
        raise Error("Ein Import läuft schon", 409)
    staging = tempfile.mkdtemp(prefix=".import-", dir=config.DATA)
    try:
        names = _unpack(src, staging)
        man, integ, key = check(staging, names)
        live = sqlite3.connect(_db_path(), timeout=30)
        live.row_factory = sqlite3.Row
        have = _tables(live)
        me = live.execute("SELECT * FROM users WHERE id=?", (actor["id"],)).fetchone() if actor and "users" in have else None
        kept = {r["name"]: (r["data"], r["updated"]) for r in live.execute("SELECT * FROM integrations")} if "integrations" in have else {}
        live.close()
        safety = os.path.join(backups_dir(), f"kombuese-before-import-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.tar.gz")
        export(safety, with_secrets="server")
        _prune_safety()
        try:
            staged = sqlite3.connect(os.path.join(staging, config.DB_FILE))
            live = sqlite3.connect(_db_path(), timeout=30)
            try:
                if lock:
                    with lock:
                        staged.backup(live)
                else:
                    staged.backup(live)
            finally:
                staged.close()
                live.close()
            _swap_images(staging)
            _swap_households(staging)
            if after_init:
                after_init()
            result = _after(integ, key, kept, me, session, lock)
            result["households"] = _after_households(integ, key, lock)
        except Exception as e:  # noqa: BLE001
            raise Error(f"Import fehlgeschlagen: {e}. Die vorigen Daten liegen in backups/{os.path.basename(safety)}", 500)
        c = sqlite3.connect(_db_path(), timeout=30)
        n = counts(c)
        c.close()
        for hid in _household_ids():
            c = sqlite3.connect(_db_path(hid), timeout=30)
            result["households"].setdefault(str(hid), {})["counts"] = counts(c)
            c.close()
        return {"ok": True, "backup": {k: man.get(k) for k in ("app_version", "created", "with_credentials")}, "counts": n,
                "images": {d: len(_images(os.path.join(config.DATA, d))) for d in IMAGE_DIRS},
                "safety_backup": os.path.basename(safety), **result}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        _busy.release()


def _rows(items, mode, key):
    """Connections of the archive, encrypted with this server's key. → ({name: (data, updated)}, skipped names)"""
    tk = secretbox.derive(key) if key else None  # transfer key; with mode "server" the rows are encrypted with this server's key
    rows, skipped = {}, []
    for name, it in (items or {}).items():
        if name not in INTEGRATIONS or not isinstance(it, dict):
            continue
        d = it.get("data")
        try:
            if isinstance(d, str):
                d = json.loads(secretbox.decrypt(d, tk)) if tk or mode == "server" else None
        except (ValueError, TypeError):
            d = None
        if not isinstance(d, dict):
            skipped.append(name)
            continue
        rows[name] = (secretbox.encrypt(json.dumps(d)), str(it.get("updated") or "")[:40] or None)
    return rows, skipped


def _after_households(integ, key, lock):
    """Connections of the further households. Connections of households the archive does not bring along
    are not kept: a household with the same number may be a different one."""
    out = {}
    parts = integ.get("households") if isinstance(integ.get("households"), dict) else {}
    for hid in _household_ids():
        part = parts.get(str(hid)) if isinstance(parts.get(str(hid)), dict) else {}
        rows, skipped = _rows(part.get("items"), integ.get("encrypted"), key)
        c = sqlite3.connect(_db_path(hid), timeout=30)
        try:
            if lock:
                lock.acquire()
            with c:
                c.execute("DELETE FROM integrations")
                c.executemany("INSERT INTO integrations(name, data, updated) VALUES(?,?,?)", [(n, *v) for n, v in rows.items()])
        finally:
            if lock:
                lock.release()
            c.close()
        out[str(hid)] = {"integrations": {"restored": sorted(rows),
                                          "left_out": sorted((set(part.get("left_out") or []) | set(skipped)) - set(rows))}}
    return out


def _after(integ, key, kept, me, session, lock):
    """Sessions, connections and the importing admin on the freshly imported database."""
    rows, skipped = _rows(integ.get("items"), integ.get("encrypted"), key)
    kept_names = [n for n in kept if n not in rows]
    for n in kept_names:
        rows[n] = kept[n]
    c = sqlite3.connect(_db_path(), timeout=30)
    c.row_factory = sqlite3.Row
    account = None
    try:
        if lock:
            lock.acquire()
        with c:
            c.execute("DELETE FROM sessions")
            c.execute("DELETE FROM integrations")
            c.executemany("INSERT INTO integrations(name, data, updated) VALUES(?,?,?)", [(n, *v) for n, v in rows.items()])
            if me:
                account = me["username"]
                if me["oidc_sub"]:
                    c.execute("UPDATE users SET oidc_issuer=NULL, oidc_sub=NULL WHERE oidc_issuer=? AND oidc_sub=? AND username<>?",
                              (me["oidc_issuer"], me["oidc_sub"], me["username"]))
                ex = c.execute("SELECT id FROM users WHERE username=?", (me["username"],)).fetchone()
                if ex:
                    uid = ex["id"]
                    if "household_id" in {r["name"] for r in c.execute("PRAGMA table_info(users)")}:  # stays an instance admin
                        c.execute("UPDATE users SET household_id=1 WHERE id=?", (uid,))
                    c.execute("UPDATE users SET password_hash=COALESCE(?, password_hash), role='admin', disabled=0, "
                              "oidc_issuer=COALESCE(?, oidc_issuer), oidc_sub=COALESCE(?, oidc_sub) WHERE id=?",
                              (me["password_hash"], me["oidc_issuer"], me["oidc_sub"], uid))
                else:
                    uid = c.execute("INSERT INTO users(username, display_name, email, password_hash, role, oidc_issuer, oidc_sub, created) "
                                    "VALUES(?,?,?,?, 'admin', ?,?,?)", (me["username"], me["display_name"], me["email"], me["password_hash"],
                                                                        me["oidc_issuer"], me["oidc_sub"], me["created"])).lastrowid
                if session:
                    now = datetime.now(timezone.utc)
                    c.execute("INSERT INTO sessions(token_hash, user_id, created, expires, last_seen, agent) VALUES(?,?,?,?,?,?)",
                              (hashlib.sha256(session.encode()).hexdigest(), uid, now.isoformat(timespec="seconds"),
                               (now + timedelta(days=int(config.SESSION_DAYS))).isoformat(timespec="seconds"),
                               now.isoformat(timespec="seconds"), "backup import"))
    finally:
        if lock:
            lock.release()
        c.close()
    return {"integrations": {"restored": sorted(n for n in rows if n not in kept_names), "kept": sorted(kept_names),
                             "left_out": sorted((set(integ.get("left_out") or []) | set(skipped)) - set(rows))},
            "account": account}


def receive(rfile, length, dest_dir=None):
    """Copy an upload of length bytes from rfile into a temporary file. → path (caller removes it)"""
    if length <= 0:
        raise Error("Keine Backup-Datei (erwartet wird eine .tar.gz der Kombüse)")
    if length > MAX_BYTES:
        raise Error(f"Backup-Datei zu groß (höchstens {config.BACKUP_MAX_MB} MB, KOMBUESE_BACKUP_MAX_MB)", 413)
    fd, path = tempfile.mkstemp(prefix=".upload-", suffix=".tar.gz", dir=dest_dir or backups_dir())
    try:
        with os.fdopen(fd, "wb") as f:
            left = length
            while left > 0:
                chunk = rfile.read(min(left, 1024 * 1024))
                if not chunk:
                    raise Error("Upload abgebrochen")
                f.write(chunk)
                left -= len(chunk)
    except BaseException:
        os.remove(path)
        raise
    return path
