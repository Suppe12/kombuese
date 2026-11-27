# HTTP API

Everything the app does goes through a JSON API that scripts, AI workers and MCP clients can use too.

**Full reference:** every installation describes its API itself – interactive at **`/api/docs`** (Swagger UI, also
under ⚙️ Settings → API tokens → „API docs“) and machine-readable as OpenAPI 3.1 at **`/api/openapi.json`**. Both are
public: the description holds nothing of the installation, trying an operation out needs a session or a token.
*Try it out* uses your session in the same browser (the page adds the `X-Kombuese: 1` header itself); for an API
token click *Authorize*. The description is built by `app/openapi.py`; `tests/test_openapi.py` fails when a route of
the server is missing there or a documented path does not exist. This page is the short overview.

## Authentication

- **API token** (scripts, workers): header `Authorization: Bearer kmb_…`, created under ⚙️ Settings → API tokens or with
  `tools/admin.py create-token`. Scope *api* opens the whole API except the admin settings below; scope *calendar*
  only `/kalender.ics`.
- **Browser session:** the session cookie (`kombuese_<id>`, named per installation) from
  `POST /auth/login {"username", "password", "remember": true}`; `remember: false` gives a cookie that ends with the
  browser. POST requests with a session cookie must send the header `X-Kombuese: 1` (CSRF protection).
- Admin settings (`/api/users`, `/api/tokens`, `/api/invites`, `/api/integrations`, `/api/ai`, changing
  `/api/household`) only work for admins signed in with a session, not with a token. `/api/backup` and
  `/api/households` only for instance admins (admins of household 1).
- **Households**: every request works in the household of whoever asks – the account of the session or
  the household the API token was created in. Accounts, tokens and invitations are always those of that household.

Errors come as `{"error": "…"}` with a 4xx/5xx status. Error texts are German at the moment.

## Naming

Paths and JSON fields are German (`rezepte` = recipes, `zutaten` = ingredients, `einkauf` = shopping, `schrank` =
cupboard, `vorrat` = freezer, `woche`/`wochenplan` = week/week plan). They are kept stable so existing workers and
backups keep working; English aliases may come with a later major version.

## Overview

| Area | Endpoints |
|---|---|
| App | `GET /api/version` (version, changelog), `GET /api/config`, `GET /api/status`, `GET /api/work` (waiting AI jobs) |
| Account | `/auth/status`, `/auth/setup`, `/auth/setup/import`, `/auth/login`, `/auth/logout`, `/auth/oidc/start[?invite=]`, `GET /api/me` (with `household`, `instance_admin`, own `prefs`), `POST /api/me/password` |
| Invitations | `GET /auth/invite?code=` (household, role, expiry), `POST /auth/invite {"code", "username", "password", "display_name"}` (creates the account, signs in), admins: `GET/POST /api/invites {"role", "days", "note"}` (answer with `code` and `link`, shown once), `POST /api/invites/<id>/revoke` |
| People | `GET /api/household` (also `name`, `people`, `people_count`, `members` with `prefs`), admins `POST /api/household {"name", "people"}`, `POST /api/members/<id> {"display_name", "diet", "no_spicy", "dislikes", "notes"}` (own account, admins any member) |
| Households | instance admins: `GET /api/households`, `POST /api/households {"name", "language"}` (answer with `invite` for the first admin), `POST /api/households/<id> {"name"} \| {"delete": true}`, `POST /api/households/<id>/invites` |
| Admin | `/api/users[/<id>]`, `/api/tokens[/<id>/revoke]`, `/api/integrations[/<name>[/test]]`, `/api/ai`, `/api/ai/test`, `/api/ai/runs/<id>`, `/api/ai/status`, `/api/household`, `GET /api/backup`, `POST /api/backup/import` |
| Recipes | `GET/POST /api/rezepte` (list with `?suche=`, create), `GET/POST /api/rezepte/<slug>` (detail, change only the fields sent), `POST /api/rezepte/<slug>/bild` (image as body or `{"url"}`), `POST /api/rezepte/<slug>/einkauf\|plan\|sterne`, `GET /api/zutaten?suche=`, `GET /api/einheiten`, `GET /api/scrape?url=` (read a recipe page), `GET /api/aufwaermen?slug=|eintrag=&portionen=&watt=` (reheating, see below), `GET/POST /api/aufwaermen/job` (AI: add reheating tips) |
| Swipe cards | `GET /api/stack` (with several members only the cards you have not swiped), `POST /api/swipe` (several members: answer with `match`, `status`, `waiting`), `POST /api/undo`, `GET /api/match` (your likes that wait for others), `GET /api/likes`, `GET /api/history`, `GET/POST /api/geschmack` (taste evaluation and profile), `GET/POST /api/abneigungen`, `POST /api/cards` (create), `POST /api/cards/<id>/edit\|kcal\|meal\|plan\|why\|mealie\|fehler\|nope`, `GET /api/known-urls`, `POST /api/nachschub` (request a refill) |
| Week | `GET /api/woche?start=`, `POST /api/plan`, `POST /api/plan/<id>/weg`, `GET/POST /api/wochenplan` (proposal, `{"start", "wunsch", "personen"}`), `POST /api/wochenplan/vorschlag\|annehmen\|verwerfen\|tauschen\|personen`, `GET/POST /api/mealprep`, `POST /api/mealprep/ablauf`, `GET /kalender.ics` |
| Shopping | `GET /api/einkauf`, `POST /api/einkauf/neu\|weg\|check\|rezepte\|bring\|immer`; `neu` takes an optional `client_id` (8–64 letters, digits, `-`, `_`): sending the same id again returns the first answer with `"repeat": true` instead of adding the item twice (offline queue of the app) |
| Web Push | session only: `GET /api/push/key` (public VAPID key, own `devices`, `household_devices`), `POST /api/push/subscribe` with the browser's `PushSubscription.toJSON()` (answer `{id, secret, key}`) or `{"endpoint" or "id", "delete": true}`, `POST /api/push/unsubscribe {"endpoint" or "id"}`, `POST /api/push/test {"endpoint" or "id", "lang"}`. Service worker: `GET /api/push/pending?id=<id>` with header `X-Push-Secret: <secret>` (no session; `{items: [{id, title, text, view, created}]}`, each item delivered once) |
| Pantry | `GET/POST /api/schrank`, `POST /api/schrank/<id>\|kochen\|mealprep\|kauf\|rueckgaengig`, `GET /api/barcode/<code>`, `POST /api/barcode`, `GET/POST /api/vorrat[/<id>]` (freezer) |
| Cook from the pantry | `GET /api/schrank/rezepte?limit=` (own recipes by stock: `da`, `fehlt`, `knapp`, `treffer`, `gesamt`, `quote`, `bald`; top-level `bald` = stock running out within 14 days), `POST /api/schrank/rezepte/einkauf {"slug", "portionen"}` (only the missing things onto the list), AI: `POST /api/kochideen {"wunsch"}`, `GET /api/kochideen` (`status`, `vorschlag.ideen` with `rezept` or `karte_daten`), `POST /api/kochideen/vorschlag\|state\|verwerfen` |
| Recipe from a photo | `POST /api/fotoimport {"bilder": [{"daten": "<base64 or data URL>"}], "hinweis"}` (1–6 images, 5 MB each, 409 while one waits), `GET /api/fotoimport` (state; `grund` = `vision` or `kein_rezept` on failure), `GET /api/fotoimport/bild/<nr>` (while the job waits or runs), `POST /api/fotoimport/state\|bild {"slug", "bild"}\|abbrechen` |
| Food diary | `POST /api/gegessen {"mealplan_id" | "slug", "portionen", "reis", "personen": [ids]}` (kept in Kombüse per person; also to Yazio when connected, answer with `yazio`, `yazio_fehler`), `POST /api/gegessen/<id>/weg {"person" | "alle"}`, `GET /api/ernaehrung?start=&tage=` (per person and day: `gegessen`, `offen` = planned and not eaten yet, `ziel`; `geplant` per day), `POST /api/ernaehrung/ziel {"user_id", "kcal", "protein"}` (own goal, admins any member; empty = no goal), admins `POST /api/ernaehrung/yazio {"user_id" | null}` (whose meals go to Yazio), `GET /api/yazio/tag?date=`, `GET /api/mahlzeit?id=` |
| Shared recipes | `GET/POST /api/rezepte/<slug>/teilen` (the public link of a recipe or `null`; POST creates it or returns the existing one), `POST /api/freigaben/<id>/weg` (delete = invalid at once), admins `GET /api/freigaben`. Public without an account: `GET /s/<token>[?portionen=]`, `/s/<token>/bild`, `/s/<token>.pdf` – see below |
| Other | `POST /api/notify {"text", "title", "view"}` (all channels incl. Web Push; `view` = `home`, `deck`, `recipes`, `week`, `pantry`, `shop` or `likes` opens the app there on a tap), `POST /api/linkimport`, `GET /rezept/<slug>/druck` (print view), `GET /rezept/<slug>.pdf` |
| AI jobs | `POST /api/nachschub/state`, `/api/wochenplan/state`, `/api/mealprep/state`, `/api/linkimport/state`, `/api/kochideen/state`, `/api/fotoimport/state`, `/api/aufwaermen/state` (`{"state": "running\|done\|failed", "note"}`) |
| MCP | `POST /mcp` – see [ai.md](ai.md#mcp-server) |

## Recipe format

`POST /api/rezepte` and `/api/rezepte/<slug>` take:

```json
{"name": "Chili con Carne", "beschreibung": "…", "portionen": 4, "ertrag": "4 servings",
 "zeiten": {"gesamt": "45 min", "vorbereitung": "15 min", "kochen": "30 min"},
 "kategorien": ["Hauptgericht"], "tags": ["Mealprep"], "sprache": "en",
 "zutaten": [{"abschnitt": "", "menge": 500, "einheit": "g", "name": "ground beef", "notiz": "", "kategorie": ""}],
 "schritte": [{"titel": "", "text": "…"}],
 "naehrwerte": {"kcal": 520, "protein": 34, "kh": 40, "zucker": 8, "fett": 22, "gesfett": 8, "ballaststoffe": 9, "natrium": 900},
 "notizen": [{"titel": "Mealprep / Reste", "text": "keeps 4 days, freezes for 3 months"}], "quelle": "https://…",
 "aufwaermen": {"kuehlschrank": {"watt": 800, "minuten": 3, "hinweis": "covered, stir halfway"},
                "gefroren": {"watt": 800, "minuten": 7, "hinweis": ""}, "ruhen": 1,
                "alternative": "pot over medium heat, 5–8 min", "hinweis": "add a little water"}}
```

Instead of lists, `zutaten_text`, `schritte_text` and `notizen_text` take the same text as the editor in the app
(one ingredient per line `menge einheit name; notiz`, blocks separated by an empty line, `## Title` for a heading).
Units are the keys from `GET /api/einheiten` (German, e.g. `EL` = tbsp); English units such as `tbsp`, `tsp`, `cup`
are recognised in text. `kategorien` uses the fixed keys `Hauptgericht` (lunch/dinner) and `Frühstück` (breakfast).
Nutrition is per serving.

### Reheating

`aufwaermen` is optional: microwave watt and minutes for **one portion** (one box, about 350–450 g) from the fridge
(`kuehlschrank`) and straight from the freezer (`gefroren`), each with a short `hinweis`; `ruhen` = standing
minutes, `alternative` = oven or pan instead, `hinweis` = for both, `kalt: true` = eaten cold (no times). `minuten`
takes a number (`2.5`) or `"2:30"`; leave out `gefroren` for dishes that do not freeze. `{}` removes the field.

What the app shows comes from `aufwaermen_info` in `GET /api/rezepte/<slug>` and from
`GET /api/aufwaermen?slug=<slug>|eintrag=<plan entry id>&portionen=<n>&watt=<W>` (leftover entries „Rest: Chili“ find
their recipe by name): `kuehlschrank`/`gefroren` with `watt`, `sekunden`, `zeit` (`m:ss`), `hinweis` and the
recommendation (`empfohlen`), plus `geschaetzt`. The rules (app/reheat.py):

* **More portions at once**: every extra portion +70 % time (2 portions 1.7 ×, 3 portions 2.4 ×).
* **Another microwave**: time × recommended watt / own watt. The own watt is the household setting
  `microwave_watt` (`GET/POST /api/household`, default 800).
* Rounded to 15 seconds, at least 15 seconds.
* **Without the field** the app estimates from name, categories and tags (stews and curries 2:30 / 6 min, soups 3 / 7,
  pasta and rice dishes 2 / 4:30, bakes 3 / 7 with the oven as alternative, egg muffins 1 / 2, burritos 1:30 / 3:30,
  pizza and quiche better in the oven, overnight oats and salads cold …) and marks it `geschaetzt: true`. No frozen
  time when the notes say „do not freeze“. The AI job „Add reheating tips“ fills the field ([ai.md](ai.md)).

`GET /api/rezepte` has `aufwaermen: true|false` per recipe (own field or not).

## Shared recipes

A shared link opens one recipe as a read-only page for anyone, without an account: photo, ingredients (servings
adjustable with `?portionen=`), steps, notes, nutrition, source and a PDF, in the language of the recipe. The token
(24 random bytes) opens nothing else: no API, no other recipe, no other image. The page sends `X-Robots-Tag: noindex`,
`Referrer-Policy: no-referrer` and a Content-Security-Policy without scripts. Per address at most 300 requests, 10 PDFs
and 20 unknown links in 10 minutes (then 429). One link per recipe; deleting it makes it invalid for good, sharing again
creates a new one. AI runs inside the app cannot share.

## Eaten meals and goals

The table `gegessen` holds one row per plan entry and person (`user_id`); older rows belong to the first admin. Without
`personen` a meal counts for whoever asks (an API token: the account it belongs to). Planned nutrition counts one serving
per person of every plan entry of a day whose recipe has nutrition (leftovers by the recipe name). Goals are per person
and day (`kcal`, `protein`).
