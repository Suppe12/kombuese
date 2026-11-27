"""Kombüse administration from the command line (inside the container):

  docker exec kombuese python /app/tools/admin.py users
  docker exec kombuese python /app/tools/admin.py create-user <name> [--admin]       (asks for the password)
  docker exec kombuese python /app/tools/admin.py reset-password <name>              (asks for the password)
  docker exec kombuese python /app/tools/admin.py create-token <name> [--calendar]   (prints the token once)
  docker exec kombuese python /app/tools/admin.py tokens
  docker exec kombuese python /app/tools/admin.py export <file> [--secrets]          (backup, - = to stdout)
  docker exec kombuese python /app/tools/admin.py import <file> --yes                (replaces all data, - = from stdin)
  docker exec kombuese python /app/tools/admin.py demo [--lang en|de] [--public-demo]  (example household, empty installation only)

For an interactive password prompt use `docker exec -it`.
Backup to the host and restore on another one (README, "Backup and moving to a new server"):
  docker exec kombuese python /app/tools/admin.py export - > kombuese-backup.tar.gz
  docker exec -i kombuese python /app/tools/admin.py import - --yes < kombuese-backup.tar.gz   (then restart the container)
"""
import getpass
import os
import shutil
import sys
import tempfile

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import auth  # noqa: E402
import backup  # noqa: E402
import store  # noqa: E402


def ask_password():
    p = getpass.getpass("Password: ")
    if p != getpass.getpass("Again: "):
        sys.exit("The passwords do not match")
    return p


def main(a):
    store.init()
    auth.init()
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
    elif a[0] == "users":
        for u in auth.users():
            print(f"{u['id']:>3}  {u['username']:<20} {u['role']:<7} {'disabled' if u['disabled'] else ''} {u['last_login'] or ''}")
    elif a[0] == "create-user" and len(a) > 1:
        print("created, id", auth.create_user(a[1], ask_password(), role="admin" if "--admin" in a else "member"))
    elif a[0] == "reset-password" and len(a) > 1:
        u = next((x for x in auth.users() if x["username"].lower() == a[1].lower()), None)
        if not u:
            sys.exit("Account not found")
        auth.update_user(u["id"], {"id": -1}, {"password": ask_password()})
        print("Password set, all sessions of the account signed out")
    elif a[0] == "create-token" and len(a) > 1:
        print(auth.create_token(a[1], "calendar" if "--calendar" in a else "api")["token"])
    elif a[0] == "tokens":
        for t in auth.tokens():
            print(f"{t['id']:>3}  {t['name']:<24} {t['scope']:<8} {'revoked' if t['revoked'] else ''} {t['last_used'] or 'never used'}")
    elif a[0] == "export" and len(a) > 1:
        dest = a[1]
        if dest == "-":
            fd, dest = tempfile.mkstemp(suffix=".tar.gz", dir=backup.backups_dir())
            os.close(fd)
        try:
            m = backup.export(dest, with_secrets="--secrets" in a)
            if a[1] == "-":
                with open(dest, "rb") as f:
                    shutil.copyfileobj(f, sys.stdout.buffer)
                sys.stdout.buffer.flush()
        finally:
            if a[1] == "-" and os.path.exists(dest):
                os.remove(dest)
        print("Backup written: " + ", ".join(f"{k} {v}" for k, v in m["counts"].items()) + f", images {sum(m['images'].values())}"
              + (" (with credentials, treat it like a password!)" if m["with_credentials"] else ""), file=sys.stderr)
    elif a[0] == "import" and len(a) > 1:
        if "--yes" not in a:
            sys.exit("Replaces ALL data of this installation with the backup. Add --yes to confirm.")
        import server  # noqa: E402  (migrations as at start)
        source, tmp = a[1], None
        if source == "-":
            fd, tmp = tempfile.mkstemp(suffix=".tar.gz", dir=backup.backups_dir())
            with os.fdopen(fd, "wb") as f:
                n = 0
                while chunk := sys.stdin.buffer.read(1024 * 1024):
                    n += len(chunk)
                    if n > backup.MAX_BYTES:
                        os.remove(tmp)
                        sys.exit(f"Backup too large (at most {backup.MAX_BYTES // 1024 // 1024} MB, KOMBUESE_BACKUP_MAX_MB)")
                    f.write(chunk)
            source = tmp
        try:
            r = backup.restore(source, after_init=server.init_all, lock=store.lock)
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        print("Imported:", ", ".join(f"{k} {v}" for k, v in r["counts"].items()), "| images", sum(r["images"].values()))
        if r["integrations"]["left_out"]:
            print("Set up again (Settings → Connections):", ", ".join(r["integrations"]["left_out"]))
        print("Previous data:", os.path.join(backup.backups_dir(), r["safety_backup"]))
        print("Please restart the container so the app reads the new data.")
    elif a[0] == "demo":  # demo household (tools/demo.py), only into an empty installation
        import server  # noqa: E402  (tables and migrations as at start)
        import demo  # noqa: E402
        server.init_all()
        lang = a[a.index("--lang") + 1] if "--lang" in a and a.index("--lang") + 1 < len(a) else "en"
        if lang not in ("en", "de"):
            sys.exit("--lang en or --lang de")
        try:
            demo.print_result(demo.fill(lang, public="--public-demo" in a, images="--no-images" not in a))
        except demo.Error as e:
            sys.exit(str(e))
        if lang != "en":
            print("Restart the container once so the running app switches to the demo's language.")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (auth.AuthError, backup.Error) as e:
        sys.exit(str(e))
