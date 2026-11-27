"""Configuration of a Kombüse installation, read from environment variables.

KOMBUESE_DATA           data directory (SQLite database, images)                  default /data
KOMBUESE_PORT           HTTP port                                                  default 9926
KOMBUESE_SECRETS        old way to provide Bring!/Yazio logins (bring.json, yazio.json), default /secrets.
                        Connections are set up in the app (Settings → Connections).
KOMBUESE_SECRET_KEY     key for encrypting stored credentials; without it a key file is created in the data
                        directory (then a copy of the data directory includes the key)
KOMBUESE_URLS           comma separated addresses users open the app with, e.g.
                        "http://kitchen.lan:9926,https://kitchen.example.org". The first https
                        address is suggested for camera, screen wake lock and sharing, which
                        browsers only allow over HTTPS. A second address is used to open PDFs
                        outside the home screen app.
KOMBUESE_INTERNAL_URL   how the PDF renderer reaches the app inside Docker           default http://kombuese:9926
KOMBUESE_GOTENBERG_URL  Gotenberg (PDF renderer)                                    default http://gotenberg:3000

Login: the first visitor creates the admin account. Optional OIDC for your own identity provider:
KOMBUESE_OIDC_ISSUER, KOMBUESE_OIDC_CLIENT_ID, KOMBUESE_OIDC_CLIENT_SECRET, KOMBUESE_OIDC_NAME (button label),
KOMBUESE_OIDC_SIGNUP=true (accounts are created on first login, otherwise an admin creates them with the same e-mail).
The redirect URI to register at the provider is <address of the app>/auth/oidc/callback.
KOMBUESE_SESSION_DAYS (default 180), KOMBUESE_CALENDAR_PUBLIC=true (iCal feed without token, only in trusted networks).

KOMBUESE_ALLOW_PRIVATE_URLS=true lets recipe import and AI runs fetch pages in your own network (off by default).
AI runs inside the app are set up in Settings → AI; see ai.py.
KOMBUESE_BACKUP_MAX_MB (default 2048): largest backup file the import accepts (Settings → Backup, see backup.py).
KOMBUESE_UPDATE_URL: release feed for the update notice (GitHub "releases/latest" JSON), empty or "off" = no check.
KOMBUESE_PUSH_CONTACT: contact for the Web Push services (sub claim of VAPID), "mailto:you@example.org" or an
https address; default: the first https address in KOMBUESE_URLS, else the https address the app was opened with.
KOMBUESE_TRUSTED_PROXIES: addresses or networks of your reverse proxy (e.g. "172.18.0.0/16"); only from them the
sign-in rate limit believes X-Forwarded-For. Without it every request counts for its direct sender.
KOMBUESE_FRAME_ANCESTORS (default 'self'): who may embed the app in a frame, e.g. "'self' https://ha.example.org".
KOMBUESE_SOCKET_TIMEOUT (default 60): seconds a silent connection is kept open, 0 = no limit.

The older FT_DATA, FT_PORT and FT_SECRETS names (from the Food Tinder days) still work.
"""
import ipaddress
import os


def _env(name, default="", old=None):
    return os.environ.get(name) or (os.environ.get(old) if old else None) or default


DATA = _env("KOMBUESE_DATA", "/data", "FT_DATA")
PORT = int(_env("KOMBUESE_PORT", "9926", "FT_PORT"))
SECRETS = _env("KOMBUESE_SECRETS", "/secrets", "FT_SECRETS")
URLS = [u.strip().rstrip("/") for u in _env("KOMBUESE_URLS").split(",") if u.strip()]
INTERNAL_URL = _env("KOMBUESE_INTERNAL_URL", "http://kombuese:9926").rstrip("/")
GOTENBERG_URL = _env("KOMBUESE_GOTENBERG_URL", "http://gotenberg:3000").rstrip("/")
# Login, see auth.py
OIDC_ISSUER = _env("KOMBUESE_OIDC_ISSUER")            # e.g. https://auth.example.org/application/o/kombuese/
OIDC_CLIENT_ID = _env("KOMBUESE_OIDC_CLIENT_ID")
OIDC_CLIENT_SECRET = _env("KOMBUESE_OIDC_CLIENT_SECRET")
OIDC_NAME = _env("KOMBUESE_OIDC_NAME", "SSO")         # label of the login button
OIDC_SIGNUP = _env("KOMBUESE_OIDC_SIGNUP", "false").lower() in ("1", "true", "yes")  # new accounts on first OIDC login
SESSION_DAYS = int(_env("KOMBUESE_SESSION_DAYS", "180"))
CALENDAR_PUBLIC = _env("KOMBUESE_CALENDAR_PUBLIC", "false").lower() in ("1", "true", "yes")  # /kalender.ics without token
# Fetching other sites (recipes, images, AI runs) refuses addresses in local networks (SSRF) unless this is true
ALLOW_PRIVATE_URLS = _env("KOMBUESE_ALLOW_PRIVATE_URLS", "false").lower() in ("1", "true", "yes")
BACKUP_MAX_MB = int(_env("KOMBUESE_BACKUP_MAX_MB", "2048"))  # import limit for backups (backup.py)
# Contact for Web Push services (push.py): mailto: or https address
PUSH_CONTACT = _env("KOMBUESE_PUSH_CONTACT", "").strip()
# Reverse proxies whose X-Forwarded-For is believed: addresses or networks, comma separated
TRUSTED_PROXIES = []
for _p in _env("KOMBUESE_TRUSTED_PROXIES").split(","):
    if _p.strip():
        try:
            TRUSTED_PROXIES.append(ipaddress.ip_network(_p.strip(), strict=False))
        except ValueError:
            print(f"KOMBUESE_TRUSTED_PROXIES: not an address or network: {_p.strip()}", flush=True)


# Security review 2026-11-25: who may show the app in a frame (CSP frame-ancestors), e.g. "'self' https://ha.example.org"
# for a Home Assistant dashboard on another address; default only the app itself (clickjacking)
FRAME_ANCESTORS = _env("KOMBUESE_FRAME_ANCESTORS", "'self'").strip()
# Seconds a connection may stay silent before the server drops it (slow clients holding threads), 0 = no limit
SOCKET_TIMEOUT = int(_env("KOMBUESE_SOCKET_TIMEOUT", "60"))


def trusted_proxy(addr):
    try:
        ip = ipaddress.ip_address(str(addr).split("%")[0])
    except ValueError:
        return False
    return any(ip in n for n in TRUSTED_PROXIES)
AUTHOR = "Suppe12"
# Update notice (updates.py): release feed to compare with, empty/"off" = no check
UPDATE_URL = _env("KOMBUESE_UPDATE_URL", "https://api.github.com/repos/Suppe12/kombuese/releases/latest")
# Where the source code of this installation is (AGPL-3.0 §13: users of a changed version over the network must be
# offered its source). Shown in the version sheet; whoever runs a changed version sets it to their own repository.
SOURCE_URL = _env("KOMBUESE_SOURCE_URL", "https://github.com/Suppe12/kombuese")
DB_FILE = "food-tinder.db"  # historical name, kept so existing installations keep their data


def https_url():
    return next((u for u in URLS if u.startswith("https://")), "")


def public():
    """What the web app may know about the installation (GET /api/config)."""
    return {"urls": URLS, "https_url": https_url(), "author": AUTHOR}
