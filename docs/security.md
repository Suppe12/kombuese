# Security

How Kombüse protects a household's data, what the review before the first public release (2026-11-25) found, what was
fixed and which risks are accepted. How to report a problem: [SECURITY.md](../SECURITY.md).

## Threat model

Kombüse runs on a home server, often behind a reverse proxy, sometimes reachable from the internet. It is built for one
or a few households that know each other (family, flat share, friends on one server).

| Who | Can | Must not be able to |
|---|---|---|
| Anyone on the network or internet | open the login page, public recipe links (`/s/<token>`), sign in with a valid password or invitation | read or change data, guess passwords at speed, reach the local network through the server, run script in a member's browser |
| Member of a household | everything in their own household except managing accounts, tokens, connections, AI settings | see or change another household's data, become admin, read secrets (Bring!/Yazio passwords, AI key) |
| Admin of a household | manage their household's accounts, invitations, tokens, connections, AI | reach other households |
| Admin of household 1 (instance admin) | additionally households, backup and restore of the whole installation | – (trusted with everything) |
| API token (`api` / `calendar`) | the household's API (no accounts, tokens, connections, backup); `calendar` only the iCal feed | the routes above |
| AI run inside the app (agent token) | only the API paths of its job (`jobs.py` `ALLOW`), web fetch through `netsafe.py` | accounts, tokens, connections, backup, notifications settings, sharing |
| A web page, photo or feed the AI reads | influence the AI's answer | anything beyond what the job's paths allow |

Protections in place: scrypt passwords, random session and API tokens stored only as SHA-256, HttpOnly + SameSite=Lax
cookie with a per-installation name, rate limit on sign-in and invitation codes, CSRF header `X-Kombuese: 1` on all
cookie POSTs, every household in a database file of its own, OIDC with state, nonce and PKCE, SSRF filter for all fetches
on behalf of users and AI runs, backup import with name/size/type checks and a safety backup, credentials encrypted at
rest (encrypt-then-MAC), push messages without payload (the device fetches them with its own secret).

## Findings fixed in the review

Severity from the point of view of a self-hosted installation reachable from the internet.

| # | Severity | Finding | Fix | Test |
|---|---|---|---|---|
| 1 | High | **Reflected XSS on the print page.** `/rezept/<slug>/druck?lang=…` put `lang` unescaped into `<html lang="…">`. A link sent to a signed-in member ran script as that member (an admin's browser could create tokens, accounts, download backups). | `printing.recipe_page` only takes `de`/`en`. | `test_security.test_print_page_does_not_reflect_lang` |
| 2 | Medium | **Stored XSS through card numbers.** `POST /api/cards` stored `kcal`/`minutes` as sent; SQLite keeps text in INTEGER columns and the swipe stack showed it as HTML. Any member or API token could plant script for the household's admin. | Server stores and returns whole numbers or null (`int_or_none`, also for old rows); the app escapes them. | `test_card_numbers_are_numbers` |
| 3 | Medium | **Script in onclick strings.** `esc()` does not protect a JS string inside `onclick="f('…')"` (the browser decodes `&#39;` before the script runs). The slug of an imported card (`POST /api/cards/<id>/mealie`, writable by the AI import run, i.e. by a hostile recipe page) ended up there. | New `jsq()` for all such places in `index.html`; `esc()` also escapes `'`; the server only accepts real slugs. | `test_mealie_slug_must_be_a_slug` |
| 4 | Medium | **`javascript:` links.** A recipe's source, a card's URL and the update feed's URL became `href` as they were; a click ran script. Recipes come from AI runs reading web pages. | `safeUrl()` in the app: only `http(s)` links. | (front end only, no HTTP test possible) |
| 5 | Medium | **DNS rebinding past the SSRF filter.** `netsafe.check()` resolved the host name, then urllib resolved it again to connect. A DNS server answering "public" and then "127.0.0.1"/"192.168.x.x" let recipe import, image download, push endpoints and AI web fetches reach the local network (Home Assistant, router, Docker services). | The connection goes to the address checked in the same step (`netsafe._connect`); with an HTTP(S) proxy in the environment the proxy resolves names as before. | `test_netsafe.py` (fake resolver) |
| 6 | Medium | **Negative `Content-Length`.** `rfile.read(-1)` read until the client closed the connection: unbounded memory per request, also before sign-in (`/auth/login`). | `_gate` refuses anything but a plain number (400). | `test_negative_content_length_refused` |
| 7 | Medium | **Silent connections kept threads forever** (slowloris). | Socket timeout, `KOMBUESE_SOCKET_TIMEOUT` (default 60 s). | `SlowClientTest` |
| 8 | Low | **Missing security headers.** No clickjacking protection, no `nosniff`, full referrer to recipe sites. | `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, CSP `frame-ancestors` (`KOMBUESE_FRAME_ANCESTORS`, default `'self'`) + `X-Frame-Options: SAMEORIGIN`, `object-src 'none'`, `base-uri 'self'` on every answer; the public recipe page keeps its stricter policy. | `test_security_headers` |
| 9 | Low | **Login CSRF.** `/auth/login`, `/auth/setup`, `/auth/invite`, `/auth/logout` accepted `text/plain` POSTs from other sites (no CORS preflight), so a page could sign a visitor in to the attacker's account. | `/auth/*` POSTs need JSON or the app header. | `test_auth_posts_only_from_the_app` |
| 10 | Low | **API tokens in the log.** Calendar feeds pass their token as `?token=`; the access log printed it. | Redacted in the log. | `test_query_token_not_in_log` |
| 11 | Low | **Password change kept other sessions.** A stolen session survived "change password". | Other sessions of the account are ended, the current one stays. | `test_password_change_signs_out_other_devices` |
| 12 | Low | **User enumeration by timing.** Unknown names skipped scrypt and answered faster. | A dummy scrypt check for unknown names. | `test_unknown_user_login_is_refused_like_wrong_password` (answer only; timing is not tested, it would be flaky) |

## Accepted risks

| Risk | Why accepted |
|---|---|
| No strict Content-Security-Policy for scripts (`'unsafe-inline'` would be needed) | The web app builds its pages with inline handlers; moving to a nonce-based CSP is a larger rewrite. All data is escaped (`esc`, `jsq`, `safeUrl`, `html.escape` on the server pages); the public recipe page has `default-src 'none'` and no script at all. |
| `/api/openapi.json` and the API docs `/api/docs` are public | They describe the open source code and nothing of the installation (no addresses, no cookie name, no data; the version is public at `/api/version` anyway). Trying an operation out needs a session or a token like any request. The docs page has its own CSP: `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; form-action 'none'; base-uri 'none'` plus `frame-ancestors` – Swagger UI is bundled (no CDN), no inline script, no outside validator. |
| `X-Forwarded-Proto` / `X-Forwarded-Host` are believed from any client | They only shape the answer to the same client (Secure flag of its own cookie, links it is shown, its OIDC redirect URI, which the identity provider checks against the registered one). Restricting them to `KOMBUESE_TRUSTED_PROXIES` would break existing installations behind a proxy without that setting. |
| First visitor becomes admin (or imports a backup) | Usual for self-hosted apps; documented: set up right after start, before exposing the port. |
| OIDC links an existing local account by verified e-mail; a missing `email_verified` counts as verified | The identity provider is the operator's own (Authentik, Authelia, Keycloak …) and trusted; some providers do not send the claim. Do not connect a provider where users choose unverified e-mail addresses. |
| ID tokens with algorithms other than RS256/HS* (e.g. ES256) are not signature-checked | They come straight from the token endpoint over the back channel, which OpenID Connect Core 3.1.3.7 allows. |
| Rate limit per address only (10 failures / 10 min), not per account; the in-memory tables are not bounded | A slow distributed guess against one account remains possible; use long passwords or OIDC. Memory grows only with distinct failing addresses within 10 minutes. |
| One thread per connection, no global connection limit | Fine for a household; put a reverse proxy in front for internet exposure. The socket timeout ends idle connections. |
| Members of a household trust each other | Every member can change recipes, the plan, the list, cards, and remove other members' "eaten" entries – by design of a shared kitchen. Script injection between members is what the fixes above prevent. |
| Pushing an endpoint already registered by another account takes it over | Endpoints are long secret URLs known only to the browser that subscribed; the message contents are only handed out with the device's own secret. |
| Backup import trusts the archive's SQLite database | Only instance admins (or the first visitor of an empty installation) can import; names, sizes, file types and the database integrity are checked. |
| Admin-configured addresses bypass the SSRF filter (ntfy server, AI base URL, SearXNG, OIDC issuer) | They are set by an admin on purpose and often live in the local network. |
| Credentials encryption is a construction of its own (HMAC-SHA256 as PRF in counter mode, encrypt-then-MAC, random 128-bit nonce) | Standard library only, no AES available; the construction is sound. The key is `KOMBUESE_SECRET_KEY` or `secret.key` in the data directory. |

## AI runs and prompt injection

AI runs read web pages, search results and photos chosen by users, so their content may try to steer the model. What a
hostile page can make an AI run do is limited to the API paths of the running job (`ALLOW` in `app/jobs.py`), for example
in an import: create or change recipes and their images, mark the card as imported or failed. It cannot reach accounts,
tokens, connections, backups, sharing, other households or the local network (web fetches go through `netsafe.py`). The
worst outcome is wrong or spammy kitchen data and a notification to the household. Values the AI writes are escaped
in the app like any user input (findings 2–4). The system prompt tells the model not to follow instructions found inside web pages
(`app/jobs.py`); that helps, but the path allowlist is the actual limit.

MCP clients (`/mcp`) use an API token and get the union of all job paths.
