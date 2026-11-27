# Installation

Kombüse is one Python process (standard library, plus the optional `anthropic` package for Claude) with a SQLite
database, and an optional [Gotenberg](https://gotenberg.dev) container that renders recipe PDFs. It runs fine on a small
home server or a Raspberry Pi; plan for about 100–250 MB of memory.

## With Docker Compose

```sh
git clone https://github.com/Suppe12/kombuese.git && cd kombuese
cp docker-compose.example.yml docker-compose.yml
# edit KOMBUESE_URLS (the addresses you will open the app with), optionally KOMBUESE_SECRET_KEY
docker compose up -d --build
```

Open `http://<host>:9926`. The first visitor creates the first account, which becomes the admin – or chooses
**Restore backup** to start from a backup of another installation (see [backup.md](backup.md)).

Then, under ⚙️ Settings:

1. **Household:** language of recipes and suggestions, and a short description for the AI (people, office days,
   shopping and prep day, kitchen).
2. **Accounts:** accounts for the other people in the household.
3. **Connections** (optional): Bring!, Yazio, Telegram or ntfy.
4. **AI** (optional): who fills the swipe stack, imports recipes and plans the week – see [ai.md](ai.md).
5. **Video & Whisper:** recipes from TikTok, Instagram and YouTube links. The example starts its own Whisper
   container (CPU, about 1 GB RAM); press **Test** once. Using your own Whisper or none: see [video.md](video.md).

All data lives in the data directory (`./data` in the example): the database `food-tinder.db` (historical name), card
images in `img/`, recipe images in `rezeptbilder/`, safety backups in `backups/`, and `secret.key` unless you set
`KOMBUESE_SECRET_KEY`.

## Without Docker

Python 3.13 (the version the Docker image uses and the tests run on):

```sh
pip install "anthropic>=1,<2"        # only for Claude via the Anthropic API
pip install "yt-dlp[default]"         # only for recipes from videos, plus ffmpeg from your system (docs/video.md)
KOMBUESE_DATA=./data KOMBUESE_URLS=http://localhost:9926 python app/server.py
```

The PDF download needs Gotenberg (`KOMBUESE_GOTENBERG_URL`) that can reach the app (`KOMBUESE_INTERNAL_URL`);
everything else works without it.

## HTTPS in 5 minutes

Kombüse itself speaks plain HTTP on port 9926; HTTPS comes from something in front of it that also renews the
certificate. You want HTTPS: browsers only allow the camera (barcode scanner), the screen wake lock (cooking mode), the
share sheet, notifications (Web Push), the offline shopping list and installing the app on the iPhone home screen over
HTTPS with a trusted certificate. A self-signed certificate is not enough for these.

Pick one of the two ways. Both need the same three settings in the `kombuese` service afterwards:

```yaml
      - KOMBUESE_URLS=https://kitchen.example.org,http://kitchen.lan:9926   # the https address first
      - KOMBUESE_TRUSTED_PROXIES=172.16.0.0/12   # Docker's networks: the proxy reaches the app from there
```

(`KOMBUESE_TRUSTED_PROXIES` lets the sign-in rate limit count each visitor instead of the proxy as one.)

### Way 1: Tailscale (nothing exposed to the internet)

Best for one household: the app stays private, every phone with the Tailscale app reaches it with a real certificate.

1. Install [Tailscale](https://tailscale.com) on the server and on your phones, enable *HTTPS certificates* in the
   admin console (DNS → HTTPS Certificates).
2. On the server: `tailscale serve --bg 9926`
3. Your address is `https://<server>.<tailnet>.ts.net` – put it first in `KOMBUESE_URLS`.

### Way 2: Caddy with your own domain (reachable from the internet)

1. Point a domain (e.g. `kitchen.example.org`) at your server and forward ports 80 and 443 to it.
2. Uncomment the `caddy` service in `docker-compose.example.yml` and create a `Caddyfile` next to it:

   ```
   kitchen.example.org {
       reverse_proxy kombuese:9926
   }
   ```

3. `docker compose up -d` – Caddy fetches and renews a Let's Encrypt certificate by itself. You can then remove the
   `9926:9926` port of the `kombuese` service, so the app is only reachable through Caddy.

Other proxies (Traefik, nginx, Nginx Proxy Manager) work the same way; they must pass `X-Forwarded-Proto` and
`X-Forwarded-Host`, so sign-in cookies are marked secure and OIDC redirects use the right address.

If you open Kombüse without HTTPS from an address outside your home network, the app shows a short notice with a link
to this section.

**Security status:** Kombüse has sign-in, CSRF protection, a rate limit on sign-in, encrypted credentials and SSRF
protection, but it has not had an external review. Prefer a trusted network or VPN over exposing it directly to the
internet.

## Updating

```sh
git pull
docker compose up -d --build
```

Database changes are applied on start. Make a backup first (⚙️ Settings → Backup, or
`docker exec kombuese python /app/tools/admin.py export - > backup.tar.gz`). ⚙️ → *What's new* shows the changes of each
version.

## Command line

Inside the container:

```sh
docker exec kombuese python /app/tools/admin.py users
docker exec -it kombuese python /app/tools/admin.py create-user <name> [--admin]      # asks for the password
docker exec -it kombuese python /app/tools/admin.py reset-password <name>
docker exec kombuese python /app/tools/admin.py create-token <name> [--calendar]      # prints the token once
docker exec kombuese python /app/tools/admin.py tokens
docker exec kombuese python /app/tools/admin.py export - > backup.tar.gz
docker exec kombuese python /app/tools/admin.py demo [--lang en|de] [--public-demo]  # demo household, see below
```

## Demo household

To look around before you move in, fill an **empty** installation (no accounts yet) with example data: 22 recipes
with photos and nutrition, swipe cards, a week plan around today with a meal prep day and its ready step-by-step
sequence, a week plan proposal, shopping list, cupboard and freezer, and two members (Alex and Sam) with preferences
and a few days of nutrition history. Either set `KOMBUESE_DEMO=true` (or `en` / `de`) before the first start, or run
`admin.py demo` as above. Sign in as `demo`; the password is printed once (`docker compose logs kombuese | grep Demo`).
`--public-demo` uses the documented password `kombuese-demo` instead – only for a throwaway public demo. The data is
generated around today's date; to start over, stop the container and empty the data directory. The photos come from
Wikimedia Commons, see `tools/demo/images/CREDITS.md`.

## Troubleshooting

- **PDF button does nothing / error 502:** Gotenberg is not running or cannot reach `KOMBUESE_INTERNAL_URL`. The print
  view (`/rezept/<slug>/druck`) works without it.
- **Camera does not start:** open the app over its `https://` address.
- **Signed out unexpectedly:** each address (IP, host name, VPN name) has its own sign-in, and on iPhone the home
  screen app is separate from Safari. Two Kombüse instances on the same host but different ports share cookies, so
  signing in to one signs you out of the other.
- **Local AI model is very slow or never finishes:** see [ai.md](ai.md#local-models).
