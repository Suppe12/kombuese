# Configuration

Most settings live in the app under ⚙️ Settings (admins). Environment variables cover what has to be known before the
app starts.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `KOMBUESE_URLS` | | Comma separated addresses you open the app with. The first `https://` one is suggested for camera, wake lock and sharing; a second address is used to open PDFs outside the home screen app |
| `KOMBUESE_DATA` | `/data` | Database, images, safety backups |
| `KOMBUESE_PORT` | `9926` | HTTP port |
| `KOMBUESE_SECRET_KEY` | | Key for encrypting stored credentials. Without it, `secret.key` is created in the data directory (then a copy of the data directory includes the credentials) |
| `KOMBUESE_INTERNAL_URL` | `http://kombuese:9926` | How the PDF renderer reaches the app |
| `KOMBUESE_GOTENBERG_URL` | `http://gotenberg:3000` | PDF renderer |
| `KOMBUESE_OIDC_ISSUER`, `_CLIENT_ID`, `_CLIENT_SECRET` | | Sign-in with your identity provider, see below |
| `KOMBUESE_OIDC_NAME` | `SSO` | Label of the sign-in button |
| `KOMBUESE_OIDC_SIGNUP` | `false` | Create accounts on the first OIDC sign-in |
| `KOMBUESE_SESSION_DAYS` | `180` | How long a sign-in lasts (extended while used) |
| `KOMBUESE_CALENDAR_PUBLIC` | `false` | iCal feed without token (only in trusted networks) |
| `KOMBUESE_ALLOW_PRIVATE_URLS` | `false` | Let recipe import and AI runs fetch pages in your own network |
| `KOMBUESE_BACKUP_MAX_MB` | `2048` | Largest backup file a restore accepts |
| `KOMBUESE_UPDATE_URL` | GitHub releases of Kombüse | Release feed for the update notice in ⚙️ → What's new (GitHub `releases/latest` JSON); empty or `off` = no check |
| `KOMBUESE_SOURCE_URL` | `https://github.com/Suppe12/kombuese` | Link to the source code shown in ⚙️ → What's new. If you run a changed version for others, set it to your repository (AGPL-3.0 §13) |
| `KOMBUESE_TRUSTED_PROXIES` | | Addresses or networks of your reverse proxy (e.g. `172.18.0.0/16`). Only from them the sign-in rate limit believes `X-Forwarded-For`; without it all requests through a proxy share one limit |
| `KOMBUESE_FRAME_ANCESTORS` | `'self'` | Who may show the app in a frame (CSP `frame-ancestors`), e.g. `'self' https://ha.example.org` for a Home Assistant dashboard on another address. See [security.md](security.md) |
| `KOMBUESE_SOCKET_TIMEOUT` | `60` | Seconds a connection may stay silent before the server drops it; `0` = no limit |
| `KOMBUESE_PUSH_CONTACT` | | Contact for the Web Push services (VAPID `sub`): `mailto:you@example.org` or an https address. Default: the first https address in `KOMBUESE_URLS`, else the https address the app was opened with when a device subscribed |
| `KOMBUESE_DEMO` | | `true`, `en` or `de`: an empty installation starts with the demo household (see [installation.md](installation.md#demo-household)); the password of the account `demo` is printed once to the log |
| `KOMBUESE_WHISPER_URL` | | Whisper service for the video import, e.g. `http://whisper:9000`; also `KOMBUESE_WHISPER_API` (`auto`, `asr`, `openai`), `KOMBUESE_WHISPER_MODEL`, `KOMBUESE_WHISPER_KEY`. See [video.md](video.md) |
| `KOMBUESE_VIDEO` | `on` | `off` switches the video import off; limits `KOMBUESE_VIDEO_MAX_MINUTES` (10), `KOMBUESE_VIDEO_MAX_MB` (200), yt-dlp self-update `KOMBUESE_YTDLP_AUTOUPDATE` (true), commands `KOMBUESE_YTDLP`, `KOMBUESE_FFMPEG`. See [video.md](video.md) |
| `KOMBUESE_SECRETS` | `/secrets` | Old way to provide `bring.json` / `yazio.json` (read only if nothing is set in the app) |

The older names `FT_DATA`, `FT_PORT` and `FT_SECRETS` still work.

## Accounts and sign-in

Any number of accounts with the roles admin and member; every account belongs to one household (see
[Households](#households)). The first account is the admin. Admins manage accounts, invitations, API tokens,
connections, AI and the household under ⚙️ Settings; members use everything else and keep their own preferences under
⚙️ Settings → Account. With **Stay signed in** (ticked by default) a sign-in lasts 180 days and is extended while used;
without it, it ends when the browser closes (at most 12 hours without use) – for devices of other people. The
session cookie is named per installation (`kombuese_<id>`, id in the file `install_id` in the data directory), so
several Kombüse instances on one host do not sign each other out.

**Invitations** (⚙️ Settings → Accounts → Invite): an admin creates a link or code (valid 1 to 30 days, single use,
can be revoked, role member or admin). The invited person opens the link (or enters the code under "I have an
invitation" on the sign-in page), chooses a username and password, or signs in with OIDC, and lands in that household.
Only a hash of the code is stored, so it is shown once. Creating an account with a start password still works.

**API tokens** (⚙️ Settings → API tokens, or `tools/admin.py create-token`) are for scripts, AI workers and MCP
clients: header `Authorization: Bearer kmb_…`. A token belongs to the household it was created in and only sees that
household's data. A token with the scope *calendar* only opens the iCal feed
(`/kalender.ics?token=…`). Tokens are shown once and stored as hashes.

**OpenID Connect** (Authentik, Keycloak, Pocket ID, Authelia …): register a confidential client with the redirect URI
`<your address>/auth/oidc/callback` and set `KOMBUESE_OIDC_ISSUER`, `KOMBUESE_OIDC_CLIENT_ID` and
`KOMBUESE_OIDC_CLIENT_SECRET`. Accounts are matched by e-mail (an admin creates the account with the same e-mail), or
created on the first sign-in with `KOMBUESE_OIDC_SIGNUP=true`, or through an invitation link (then
in the invitation's household, whatever `KOMBUESE_OIDC_SIGNUP` says). ID tokens are checked (RS256 via the provider's keys, or
HS256 with the client secret), with PKCE and nonce.

## Household

⚙️ Settings → Household:

- **Language** of recipes and suggestions (German or English). The AI searches recipe sites and writes cards,
  recipes, plans and messages in it; new recipes are stored with it; new devices start in it. Each device can switch
  the language of the interface itself at the top of ⚙️ Settings. Every recipe keeps its own language (editable), so a
  mixed collection works, and its PDF comes in that language.
- **Description for the AI:** free text that goes into every AI run – office days, shopping and prep day,
  equipment, how much time on weekdays, dislikes everyone shares.
- **Name** of the household and **people:** the members with their preferences (diet vegetarian, vegan or
  pescatarian, nothing spicy, disliked ingredients, notes; each member edits their own under ⚙️ Settings → Account,
  admins everybody's) and the number of people the household cooks for. Empty = one per account; set it when children
  or people without an account eat along. The week plan, the refill and the meal prep runs get both: dishes have to
  suit everybody, servings follow the number of people. When you ask for a week plan you can change the number for
  that plan (guests), and in the proposal ＋/− scales all dishes.
- **Match:** with more than one member everybody swipes the stack themselves. A dish is saved ("Saved", then imported
  as a recipe) only when all members liked it; ★ counts as a like and plans the dish for this week; one "no" ends it.
  The saved list shows which of your likes still wait for whom. Alone everything works as before.

## Households

By default an installation has one household and nothing about households shows up. An admin of the first household
(instance admin) can add more under ⚙️ Settings → Household → *More households on this Kombüse*: each household has
its own recipes, stack, week plan, shopping list, pantry, freezer, settings, AI settings, AI runs and connections
(Bring!, Yazio, Telegram, ntfy), and its members see only their own. Creating a household gives an invitation link for
its first account, which becomes that household's admin. One account belongs to one household. A household without
accounts can be deleted with its data.

Storage: household 1 is the installation as it always was (`/data/food-tinder.db`, `/data/img`, `/data/rezeptbilder`);
every further household has the same files in `/data/households/<id>/`. Accounts, sessions, API tokens, households,
invitations and preferences live in the first household's database. Upgrading from an older version changes nothing:
all data stays where it was and becomes household 1. Backups always contain the whole installation with all
households and only instance admins can download or import them (see [backup.md](backup.md)). The internal tokens of
the AI runs and the PDF renderer exist per household. `KOMBUESE_SECRETS` files (`bring.json`, `yazio.json`) only
count for household 1.

## Connections

⚙️ Settings → Connections, each with a test button. Credentials are stored encrypted and never sent back to the browser.

- **Bring!** (shopping list): e-mail, password, optionally part of the list name. The shopping list is sent to Bring!;
  items ticked off there move into the cupboard.
- **Yazio** (food diary): e-mail and password. "Eaten" in the week view logs a meal with its nutrition.
- **Telegram:** bot token (from @BotFather) and chat ID. **ntfy:** server, topic, optional access token.
  Notifications go to all set up channels; scripts and AI runs use `POST /api/notify {"text": "…", "title": "…"}`.

### Notifications on your devices (Web Push)

Every member can switch on ⚙️ Settings → Notifications → *Notifications on this device*, with a test
button. Web Push is then one more channel next to Telegram and ntfy: everything sent with `POST /api/notify` (and the
messages of AI runs) also reaches the household's devices that switched it on. A tap on the notification opens the
app at the matching view (week plan, new dishes …). ⚙️ → Connections shows how many devices of the household get it.

- It needs **HTTPS** (a browser rule) and a browser with Web Push (Chrome, Edge, Firefox, Safari 16+).
- **iPhone and iPad** (iOS/iPadOS 16.4 or later): only for Kombüse on the home screen. In Safari open the https
  address, Share → *Add to Home Screen*, open Kombüse from the home screen, then ⚙️ → Notifications → switch on and
  allow. Notifications can be changed later in the iPhone's Settings → Notifications → Kombüse.
- No dependencies: the messages carry no content. The server keeps the text and sends an empty push signed with its
  VAPID key (ECDSA P-256 in pure Python, `app/vapid.py`); the app's service worker then fetches the text with a
  per-device secret. Push services (Apple, Google, Mozilla) only see that there is a message, never its text.
- The VAPID key is created on first use and stored encrypted like other credentials. Devices that are gone (404/410
  from the push service) are removed automatically. Signing out removes this device's subscription.
- Backups leave subscriptions and the key out; after a restore the devices subscribe again by themselves the next
  time the app is opened.

## Offline shopping list

The app installs a service worker (needs HTTPS, or `localhost`). The app opens without network, and the
shopping list works in the supermarket without reception: the last list is shown from the device, ticking items off
and adding new ones works offline and is sent in order when the server answers again (a small notice in the shopping
list shows the state). Items ticked offline that were removed from the list meanwhile are simply dropped. Only the
shopping list is kept on the device (no other data); signing out or another account signing in removes it. Every
release gets a new offline cache, so updates arrive as before.

Bring! and Yazio have no official API: Kombüse uses the same interfaces as their apps, which may change without notice.

## Calendar

`/kalender.ics` is the week plan as an iCal feed (e.g. Home Assistant's remote calendar). `?typ=lunch,dinner` limits
the meal types. It needs a token with the scope *calendar* (`?token=…`) unless `KOMBUESE_CALENDAR_PUBLIC=true`.
