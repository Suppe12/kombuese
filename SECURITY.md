# Security policy

## Reporting a vulnerability

Please do **not** open a public issue for a security problem. Report it privately through GitHub: open the
repository's **Security** tab and choose **Report a vulnerability** (private security advisory). Include a
description, the affected version (⚙️ → What's new) and, if you can, the steps to reproduce it.

You get an answer within a week. When the problem is confirmed, a fixed release follows as soon as possible and the
release notes name the issue once the fix is out. Credit is given if you want it.

## Supported versions

Kombüse is a small project maintained by one person. Only the **latest release** gets security fixes; please update
before reporting. The app shows a notice in ⚙️ → What's new when a newer release is out.

## Scope

In scope: the code in this repository – the server (`app/*.py`), the web app (`app/static/`), the Docker image and the
example compose file – in its default configuration.

Out of scope:

- your reverse proxy, TLS setup, Docker host, identity provider (OIDC) and the outside services you connect
  (Bring!, Yazio, Telegram, ntfy, the AI provider, Gotenberg)
- settings that lift a protection on purpose: `KOMBUESE_ALLOW_PRIVATE_URLS=true`, `KOMBUESE_CALENDAR_PUBLIC=true`,
  `KOMBUESE_OIDC_SIGNUP=true`, an AI base URL or SearXNG address in your own network
- what admins can do by design (they manage accounts, tokens, connections and backups of their household; admins of the
  first household manage the whole installation)
- denial of service by flooding (see the deployment advice), missing rate limits on endpoints that need a sign-in
- the AI following instructions from a web page or photo within the API paths its job is allowed to use
  (see [docs/security.md](docs/security.md#ai-runs-and-prompt-injection))

The threat model, the findings of the review before the first public release and the accepted risks are in
[docs/security.md](docs/security.md).

## Deployment advice

- **HTTPS.** Run Kombüse behind a reverse proxy with TLS (Caddy, Traefik, nginx …) when anyone reaches it over a network
  you do not fully trust. The session cookie gets the `Secure` flag when the proxy sends `X-Forwarded-Proto: https`.
  Camera, push notifications and the screen wake lock need HTTPS anyway.
- **Trusted proxies.** Set `KOMBUESE_TRUSTED_PROXIES` to the address or network of your proxy (e.g. `172.18.0.0/16`).
  Only then the sign-in rate limit counts per real client address; without it every client behind the proxy shares one limit.
- **Not public unless needed.** For a household that only uses the app at home and on the road, a VPN
  (WireGuard, Tailscale …) is the safer choice than opening the port to the internet.
- **First start.** The first visitor creates the admin account (or imports a backup). Do this right after starting a new
  installation, before the port is reachable from outside.
- **Secret key.** Set `KOMBUESE_SECRET_KEY` (a long random string) so a copy of the data directory alone does not contain
  the key of the stored credentials. Keep backups "with credentials" like a password.
- **Keep the data directory private.** It holds the database (password hashes, token hashes), images and `secret.key`.
- **Do not forward Gotenberg.** The PDF renderer only needs to be reachable from the Kombüse container.
- **Frames.** The app may only be framed by itself. To show it in a Home Assistant dashboard on another address, set
  `KOMBUESE_FRAME_ANCESTORS="'self' https://your-home-assistant"`.
