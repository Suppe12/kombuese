"""OpenAPI 3.1 description of Kombüse's HTTP API, served at GET /api/openapi.json, Swagger UI at /api/docs.

One op() call per route and method, grouped like docs/api.md. Paths and JSON fields are German and part of the API
(see docs/api.md, "Naming"); descriptions are English. tests/test_openapi.py keeps this honest in both directions:
every route server.py answers must be in here, and every operation in here must exist on a running server.
When you add a route to server.py, add it here too (the test names what is missing).

Public on purpose: the document holds no data of the installation (no addresses, no cookie name, no secrets), only
what the open source code shows anyway, plus the version that GET /api/version gives to everyone."""
import os
import re

TITLE = "Kombüse API"

# ---------- tags (areas), in the order Swagger UI shows them ----------
TAGS = [
    ("app", "Version, configuration, state of the app, this description."),
    ("account", "Sign-in, sign-up, invitations, own account."),
    ("admin", "Accounts, API tokens and invitations of the household (admins with a session)."),
    ("households", "Household settings, members' preferences and the households of the installation."),
    ("recipes", "Own recipes: list, detail, create, change, image, rate, plan, shop, print, PDF, import from a link or photo."),
    ("swipe cards", "The swipe stack (Food Tinder): cards, likes, noes with reasons, taste profile, dislikes, matches."),
    ("week", "Week plan, AI week plan proposals, calendar feed."),
    ("meal prep", "Meal prep schedules for a prep day."),
    ("shopping", "Shopping list, „always at home“ list, Bring!."),
    ("pantry", "Cupboard (Schrank), barcodes, freezer (Vorrat), cooking from the pantry."),
    ("nutrition", "Eaten meals, nutrition per person and day, goals, Yazio."),
    ("AI jobs", "AI runs: settings, and the endpoints a run (inside the app or an outside worker) reports to."),
    ("connections", "Bring!, Yazio, Telegram, ntfy and notifications."),
    ("backup", "Backup download and import (instance admins)."),
    ("push", "Web Push per device."),
    ("sharing", "Public links of single recipes and the public pages behind them."),
    ("images", "Card and recipe images."),
    ("MCP", "MCP server for AI clients (JSON-RPC over HTTP)."),
]

# ---------- who may call an operation (x-kombuese-access) ----------
MEMBER = [{"cookieAuth": []}, {"bearerAuth": ["api"]}]
SESSION = [{"cookieAuth": []}]
ACCESS = {
    "public": ([], "Public, no sign-in."),
    "member": (MEMBER, "Every signed-in member: session cookie or API token with scope `api`."),
    "session": (SESSION, "Browser session only, not with an API token."),
    "admin": (SESSION, "Admins of the household, signed in with a session (an API token is not enough)."),
    "instance": (SESSION, "Instance admins only: admins of household 1, signed in with a session."),
    "calendar": ([{"bearerAuth": ["api", "calendar"]}, {"calendarToken": []}, {"cookieAuth": []}],
                 "API token with scope `api` or `calendar` (header or `?token=`), a session, or nothing when "
                 "KOMBUESE_CALENDAR_PUBLIC is set."),
    "mcp": ([{"bearerAuth": ["api"]}], "API token with scope `api` only (no session)."),
    "push": ([{"pushSecret": []}], "No session: the device id and its secret from POST /api/push/subscribe."),
}

AUTH_TEXT = """
## Authentication

* **Browser session**: `POST /auth/login` sets the session cookie (named `kombuese_<installation id>`). Every `POST`
  with a session cookie must also send the header **`X-Kombuese: 1`** (CSRF protection), otherwise 403.
  This page adds that header to every request it sends, so *Try it out* works with your session right away.
* **API token**: header `Authorization: Bearer kmb_…`, created by admins under ⚙️ Settings → API tokens.
  Scope `api` opens the whole API except the admin settings; scope `calendar` only `GET /kalender.ics`
  (there also as `?token=`). A token wins over a session cookie sent with it. Use *Authorize* above to try one.
* **Admin-only** operations need an admin signed in with a session; an API token is never enough.
  Backup and the list of households need an *instance admin* (admin of household 1).
* Every request works in the household of whoever asks (the account of the session, or the household the token
  was created in).

## Errors

Errors come as `{"error": "…"}` with a 4xx/5xx status (texts are German). 401 answers also carry `"login": true`.
An unknown route answers 404 `{"error": "unbekannt"}`.

## Naming

Paths and JSON fields are German and stay stable (`rezepte` = recipes, `zutaten` = ingredients, `einkauf` = shopping,
`schrank` = cupboard, `vorrat` = freezer, `woche` = week). The app's own pages and static files (`/`, `/login`,
`/sw.js`, `/i18n.js`, icons, …) are not part of the API and not listed here.
"""


# ---------- small schema helpers ----------
def ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def s(desc="", **kw):
    return {"type": "string", **({"description": desc} if desc else {}), **kw}


def i(desc="", **kw):
    return {"type": "integer", **({"description": desc} if desc else {}), **kw}


def n(desc="", **kw):
    return {"type": "number", **({"description": desc} if desc else {}), **kw}


def b(desc="", **kw):
    return {"type": "boolean", **({"description": desc} if desc else {}), **kw}


def arr(items, desc="", **kw):
    return {"type": "array", "items": items, **({"description": desc} if desc else {}), **kw}


def obj(props=None, required=(), desc="", **kw):
    out = {"type": "object", "properties": props or {}}
    if required:
        out["required"] = list(required)
    if desc:
        out["description"] = desc
    out.update(kw)
    return out


def nullable(schema):
    t = schema.get("type")
    return {**schema, "type": [t, "null"]} if isinstance(t, str) else {"anyOf": [schema, {"type": "null"}]}


DATE = s("Date YYYY-MM-DD", format="date")
MEAL_TYPE = s("Meal of the day", enum=["breakfast", "lunch", "dinner", "side", "snack"])
CARD_MEAL = s("haupt = lunch/dinner, fruehstueck = breakfast", enum=["haupt", "fruehstueck"])
NUTRIENTS = obj({"kcal": nullable(n()), "protein": nullable(n()), "fett": nullable(n()), "kh": nullable(n("Carbohydrates"))})

SCHEMAS = {
    "Error": obj({"error": s("What went wrong (German)"), "login": b("Only on 401: sign in first")}, ["error"]),
    "Ok": obj({"ok": b()}, ["ok"]),
    "JobState": obj({
        "state": s("idle, requested (waits for a run), running, done, failed; angenommen = week plan accepted"),
        "requested": s("When it was asked for (ISO time)"), "changed": s("Last change (ISO time)"),
        "note": s("Note of the run, e.g. why it failed"), "wunsch": s("The wish sent along")},
        desc="State of one AI job. More fields depending on the job (url, start, datum, slug, name, modus, personen …)."),
    "Ingredient": obj({
        "abschnitt": s("Section heading the ingredient belongs to, empty for none"),
        "menge": nullable(n("Amount")), "einheit": s("Unit, a key from GET /api/einheiten (German, e.g. EL = tbsp)"),
        "name": s("Ingredient"), "notiz": s("Note, e.g. „finely chopped“"), "kategorie": s("Supermarket category")}),
    "TextBlock": obj({"titel": s("Heading, may be empty"), "text": s()}),
    "Nutrition": obj({k: nullable(n(d)) for k, d in (
        ("kcal", "per serving"), ("protein", "g"), ("kh", "carbohydrates, g"), ("zucker", "sugar, g"), ("fett", "fat, g"),
        ("gesfett", "saturated fat, g"), ("ballaststoffe", "fibre, g"), ("natrium", "sodium, mg"))},
        desc="Nutrition per serving"),
    "ReheatMode": obj({"watt": i("Microwave power the time is for"), "minuten": n("Minutes for one portion (2.5 = 2:30; \"2:30\" is accepted)"),
                       "hinweis": s("e.g. „stir halfway“")}),
    "Reheat": obj({"kuehlschrank": ref("ReheatMode"), "gefroren": ref("ReheatMode"), "ruhen": n("Standing time, minutes"),
                   "alternative": s("Oven or pan instead"), "hinweis": s("For both"), "kalt": b("Eaten cold, no times")},
                  desc="Reheating in the microwave for one portion; {} = not set. Shape and rules: app/reheat.py."),
    "ReheatInfo": obj({"geschaetzt": b("Estimated from the kind of dish, the recipe has no field"), "portionen": n(),
                       "watt": nullable(i("The microwave the times are converted to")), "kalt": b(), "ruhen": nullable(n()),
                       "alternative": s(), "hinweis": s(),
                       **{k: nullable(obj({"watt": i(), "sekunden": i(), "zeit": s("m:ss"), "hinweis": s(),
                                           "empfohlen": obj({"watt": i(), "minuten": n()})})) for k in ("kuehlschrank", "gefroren")}},
                      desc="Reheating for this many portions: +70 % time per extra portion, × recommended W / own W, rounded to 15 s."),
    "RecipeInput": obj({
        "name": s("Name of the recipe (required when creating)"), "beschreibung": s("Short description"),
        "portionen": n("Servings"), "ertrag": s("Yield as text, e.g. „4 servings“"),
        "zeiten": obj({"gesamt": s(), "vorbereitung": s(), "kochen": s()}, desc="Times as text, e.g. „45 min“"),
        "kategorien": arr(s(enum=["Hauptgericht", "Frühstück"]), "Fixed keys: Hauptgericht (lunch/dinner), Frühstück (breakfast)"),
        "tags": arr(s()), "sprache": s("Language of the recipe", enum=["de", "en"]),
        "zutaten": arr(ref("Ingredient")), "schritte": arr(ref("TextBlock"), "Steps"),
        "naehrwerte": ref("Nutrition"), "notizen": arr(ref("TextBlock"), "Notes, e.g. „Mealprep / Reste“"),
        "quelle": s("Source address"), "aufwaermen": ref("Reheat"),
        "zutaten_text": s("Instead of zutaten: one ingredient per line `menge einheit name; notiz`, `## Title` for a section"),
        "schritte_text": s("Instead of schritte: blocks separated by an empty line, `## Title` optional"),
        "notizen_text": s("Instead of notizen: like schritte_text")},
        desc="Creating needs name; changing changes only the fields sent."),
    "RecipeSummary": obj({
        "id": i(), "slug": s(), "name": s(), "kategorien": arr(s()), "tags": arr(s()), "zeit": s("Total time as text"),
        "portionen": nullable(n()), "kcal": nullable(n()), "protein": nullable(n()), "bewertung": nullable(i("Stars 1–5")),
        "bild": b("Has an image"), "quelle": nullable(s()), "sprache": s(),
        "aufwaermen": b("Has its own reheating field")}),
    "RecipeDetail": {"allOf": [ref("RecipeInput"), obj({
        "id": i(), "slug": s(), "bewertung": nullable(i("Stars 1–5")), "bild": b("Has an image"),
        "erstellt": s("Created (ISO time)"), "geaendert": s("Changed (ISO time)"),
        "aufwaermen_info": ref("ReheatInfo"),
        "zutaten": arr({"allOf": [ref("Ingredient"), obj({"text": s("The line as the app shows it")})]})})]},
    "Card": obj({
        "id": i(), "url": s("Recipe page"), "title": s(), "description": s(), "reason": s("Why the AI suggests it"),
        "image": s("img/<file> or empty"), "tags": arr(s()), "minutes": nullable(i()), "servings": s(), "kcal": nullable(i()),
        "kcal_est": i("1 = calories estimated"), "ingredients": arr(s()), "zutaten": arr(s("Main ingredients (German)")),
        "source": s("Host of the page"), "batch": s(), "added": s(), "meal": CARD_MEAL,
        "status": s("neu = in the stack, like, nope, mealie = imported as a recipe, fehler = import failed"),
        "plan": i("1 = cook this week"), "swiped": nullable(s()), "mealie_slug": nullable(s("Slug of the imported recipe")),
        "imported": nullable(s()), "note": s(), "why": arr(s("Reason codes, see GET /api/gruende")),
        "why_zutaten": arr(s()), "why_text": s(), "why_at": nullable(s())}),
    "CardInput": obj({
        "url": s("Recipe page (unique)"), "title": s(), "description": s(), "reason": s(), "image_url": s("Image to download once"),
        "tags": arr(s()), "minutes": i(), "servings": s(), "kcal": i(), "kcal_est": b(), "ingredients": arr(s()),
        "zutaten": arr(s("Main ingredients in German, for the picker on a no")), "source": s(), "meal": CARD_MEAL}, ["url", "title"]),
    "PlanEntry": obj({
        "id": i(), "date": DATE, "typ": MEAL_TYPE, "titel": s(), "rezept": nullable(obj(desc="The recipe (summary) or null")),
        "scale": n("Factor of the recipe's servings"), "text": s(), "rest": b("Leftovers entry"), "gefroren": b("Leftovers from the freezer"),
        "gegessen": nullable(obj(desc="My eaten entry")), "gegessen_von": arr(obj({"id": i(), "name": s()}))}),
    "ShoppingItem": obj({
        "id": s(), "checked": b(), "name": s(), "menge": s("Amount and unit as text"), "note": s(), "kategorie": s(),
        "rezepte": arr(s("Recipes the item is for")), "menge_zahl": nullable(n()), "einheit": s(), "schrank": nullable(s()),
        "im_schrank": b("Ticked because the pantry has it")}),
    "PantryItem": obj({"id": i(), "name": s(), "menge": nullable(n()), "einheit": s(), "kategorie": s(), "notiz": s(),
                       "text": s("Amount as text")}),
    "FreezerItem": obj({
        "id": i(), "name": s(), "anzahl": n(), "einheit": s("e.g. Portion"),
        "kategorie": s(enum=["fruehstueck", "haupt", "sonstiges"]), "eingefroren": nullable(DATE), "haltbar_bis": nullable(DATE),
        "notiz": s(), "recipe_slug": s(), "geaendert": nullable(s())}),
    "FreezerInput": obj({
        "name": s(), "anzahl": n(), "einheit": s(), "kategorie": s(enum=["fruehstueck", "haupt", "sonstiges"]),
        "eingefroren": DATE, "haltbar_bis": DATE, "notiz": s(), "recipe_slug": s()}),
    "Me": obj({
        "id": i(), "username": s(), "display_name": nullable(s()), "email": nullable(s()), "role": s(enum=["admin", "member"]),
        "via": s("How you are signed in", enum=["session", "token", "agent", "internal"]), "oidc": b(),
        "household": obj({"id": i(), "name": s()}), "instance_admin": b(), "households": i("Only for instance admins"),
        "prefs": obj(desc="Own preferences (diet, no_spicy, dislikes, notes)")}),
    "User": obj({"id": i(), "username": s(), "display_name": nullable(s()), "email": nullable(s()),
                 "role": s(enum=["admin", "member"]), "disabled": i(), "created": s(), "last_login": nullable(s())}),
    "Token": obj({"id": i(), "name": s(), "scope": s(enum=["api", "calendar"]), "created": s(), "last_used": nullable(s()),
                  "revoked": i(), "token_hash": {"type": "null", "description": "Never sent"}}),
    "Invite": obj({"id": i(), "role": s(enum=["admin", "member"]), "note": s(), "created": s(), "expires": s(),
                   "used": nullable(s()), "state": s(), "used_by": nullable(s())}),
    "InviteCreated": obj({"id": i(), "code": s("Shown only once"), "link": s("Sign-up link with the code"),
                          "role": s(), "expires": s()}),
    "MemberPrefs": obj({"display_name": s(), "diet": s(), "no_spicy": b(), "dislikes": s(), "notes": s()},
                       desc="Preferences of a member, used by the AI runs"),
    "Share": obj({"id": i(), "url": s("Public address …/s/<token>"), "token": s(), "slug": s(), "name": s(),
                  "erstellt": s("Created"), "aufrufe": i("Views"), "zuletzt": nullable(s("Last view")), "von": nullable(i("Shared by (account id)"))}),
    "Version": obj({"version": s(), "autor": s(), "changelog": s("Markdown"), "update": nullable(obj(desc="Newer release, if known")),
                    "source_url": s(), "license": s()}),
    "BackupResult": obj({"ok": b(), "households": i(), "recipes": i()}, desc="What was imported; more counts per table"),
    "PushSubscription": obj({"endpoint": s(), "expirationTime": nullable(n()),
                             "keys": obj({"p256dh": s(), "auth": s()})}, desc="The browser's PushSubscription.toJSON()"),
    "JsonRpc": obj({"jsonrpc": s(enum=["2.0"]), "id": {"type": ["integer", "string", "null"]}, "method": s(),
                    "params": obj()}, ["jsonrpc", "method"]),
}

# ---------- path parameters: schema, description, example (the example must fit the server's route) ----------
PARAMS = {
    "slug": (s(pattern="^[a-z0-9-]{1,120}$"), "Recipe slug", "chili-con-carne"),
    "card_id": (i(), "Swipe card id", 1),
    "plan_id": (i(), "Week plan entry id", 1),
    "mealplan_id": (i(), "Plan entry id (negative for meals eaten without a plan entry)", 1),
    "user_id": (i(), "Account id", 2),
    "token_id": (i(), "API token id", 1),
    "invite_id": (i(), "Invitation id", 1),
    "household_id": (i(), "Household id", 2),
    "share_id": (i(), "Shared link id", 1),
    "pantry_id": (i(), "Cupboard entry id", 1),
    "freezer_id": (i(), "Freezer entry id", 1),
    "run_id": (i(), "AI run id", 1),
    "nr": (i(minimum=1, maximum=6), "Number of the image, 1 = first", 1),
    "name": (s(enum=["bring", "yazio", "telegram", "ntfy"]), "Connection", "ntfy"),
    "code": (s(pattern="^[0-9]{6,14}$"), "EAN/GTIN barcode", "4000417025005"),
    "file": (s(pattern="^[0-9]+\\.[a-z]+$"), "Image file of a card", "1.jpg"),
    "image_id": (s(pattern="^[0-9a-f-]{36}$"), "Image id of a recipe", "00000000-0000-0000-0000-000000000000"),
    "size": (s(enum=["original", "min-original", "tiny-original"]), "Image size", "original"),
    "token": (s(pattern="^[A-Za-z0-9_-]{20,64}$"), "Token of the shared link", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
    "asset": (s(enum=["swagger-ui-bundle.js", "swagger-ui.css", "init.js", "docs.css", "LICENSE", "NOTICE",
                      "swagger-ui-bundle.js.LICENSE.txt"]), "File of this documentation page", "init.js"),
}

ERRORS = {400: "Invalid input", 401: "Not signed in", 403: "Not allowed (role, scope or missing X-Kombuese header)",
          404: "Not found", 409: "Conflict with the current state", 413: "Too large", 422: "Could not be read",
          429: "Too many requests", 500: "Unexpected error", 502: "An outside service (Bring!, Yazio, Gotenberg …) failed"}

PATHS = {}


def op(method, path, tag, summary, access="member", desc="", query=(), body=None, example=None, raw=None, ok=None,
       ok_type="application/json", ok_desc="OK", errors=(), requires=None, headers=None):
    """Add one operation. query: (name, schema, description) tuples. body: JSON schema of the request;
    raw: {media type: schema} for a non-JSON body. ok: schema of the 200 answer. errors: extra error statuses."""
    security, who = ACCESS[access]
    params = []
    for name in re.findall(r"{(\w+)}", path):
        schema, d, ex = PARAMS[name]
        params.append({"name": name, "in": "path", "required": True, "description": d, "schema": schema, "example": ex})
    for name, schema, d in query:
        params.append({"name": name, "in": "query", "required": False, "description": d, "schema": schema})
    for name, d in (headers or {}).items():
        params.append({"name": name, "in": "header", "required": True, "description": d, "schema": s()})
    o = {"tags": [tag], "summary": summary, "operationId": re.sub(r"\W+", "_", f"{method} {path}").strip("_").lower(),
         "description": (desc + "\n\n" if desc else "") + f"**Access:** {who}", "x-kombuese-access": access, "security": security}
    if requires:
        o["x-kombuese-requires"] = requires
    if params:
        o["parameters"] = params
    if body is not None or raw:
        content = dict(raw or {})
        if body is not None:
            content["application/json"] = {"schema": body, **({"example": example} if example is not None else {})}
        o["requestBody"] = {"required": False, "content": content}
    codes = set(errors) | {500}
    if access != "public":
        codes |= {401} if access in ("member", "calendar", "mcp") else {401, 403}
    if access == "member" and method == "post":
        codes.add(403)
    if method == "post" and body is not None:
        codes.add(400)
    resp = {"200": {"description": ok_desc, **({"content": {ok_type: {"schema": ok if ok is not None else obj()}}} if ok_type else {})}}
    for c in sorted(codes):
        resp[str(c)] = {"$ref": f"#/components/responses/E{c}"}
    o["responses"] = resp
    PATHS.setdefault(path, {})[method] = o


def get(path, tag, summary, **kw):
    op("get", path, tag, summary, **kw)


def post(path, tag, summary, **kw):
    op("post", path, tag, summary, **kw)


OK = ref("Ok")
STATE = ref("JobState")
STATE_BODY = obj({"state": s(enum=["running", "done", "failed"]), "note": s("Short note, e.g. why it failed")}, ["state"])
IMAGE = {"image/*": {"schema": s(contentMediaType="image/*")}}
HTML = "text/html"


# ================= account =================
get("/auth/status", "account", "Is there an account yet, is OIDC on?", access="public",
    ok=obj({"setup": b("true = no account yet, POST /auth/setup creates the first admin"), "oidc": b(), "oidc_name": s()}))
post("/auth/login", "account", "Sign in (sets the session cookie)", access="public",
     desc="Needs `Content-Type: application/json` or `X-Kombuese: 1`. `remember: false` gives a cookie that ends with the "
          "browser. Wrong passwords are rate-limited per address.",
     body=obj({"username": s(), "password": s(format="password"), "remember": b("Stay signed in (default true)")}, ["username", "password"]),
     example={"username": "admin", "password": "…", "remember": True}, ok=OK, errors=(401, 403, 429))
post("/auth/setup", "account", "Create the first admin (only while there is no account)", access="public",
     body=obj({"username": s(), "password": s(format="password")}, ["username", "password"]),
     example={"username": "admin", "password": "…"}, ok=OK, errors=(403, 409))
post("/auth/setup/import", "account", "New installation from a backup (only while there is no account)", access="public",
     desc="Body: the .tar.gz from GET /api/backup. Needs the header `X-Kombuese: 1`.",
     raw={"application/gzip": {"schema": s(contentMediaType="application/gzip")}}, ok=ref("BackupResult"), errors=(400, 403, 409, 413))
post("/auth/logout", "account", "Sign out (ends this session)", access="public", ok=OK, errors=(403,))
get("/auth/oidc/start", "account", "Start sign-in with OIDC (redirect)", access="public", requires="oidc",
    desc="Only when OIDC is configured (KOMBUESE_OIDC_*), else 404.",
    query=[("invite", s(), "Invitation code to redeem with the OIDC account")], ok_desc="—", ok_type=None, errors=(404,))
op("get", "/auth/oidc/callback", "account", "OIDC callback (redirect to the app)", access="public", requires="oidc",
   desc="Called by the identity provider; signs in and redirects to `/` (or to `/login?fehler=` on failure).",
   query=[("code", s(), ""), ("state", s(), "")], ok_desc="—", ok_type=None, errors=(404,))
get("/auth/invite", "account", "Look at an invitation", access="public", query=[("code", s(), "Invitation code")],
    ok=obj({"household": s(), "household_id": i(), "role": s(), "expires": s(), "oidc": b(), "oidc_name": s()}), errors=(400, 404, 429))
post("/auth/invite", "account", "Redeem an invitation: create the account and sign in", access="public",
     body=obj({"code": s(), "username": s(), "password": s(format="password"), "display_name": s()}, ["code", "username", "password"]),
     example={"code": "ABCD1234", "username": "kim", "password": "…", "display_name": "Kim"}, ok=OK, errors=(403, 404, 409, 429))
get("/api/me", "account", "Own account, household and preferences", ok=ref("Me"))
post("/api/me/password", "account", "Change own password (ends your other sessions)", access="session",
     body=obj({"old": s(format="password"), "new": s(format="password")}, ["old", "new"]), example={"old": "…", "new": "…"}, ok=OK)

# ================= admin =================
get("/api/users", "admin", "Accounts of the household", access="admin", ok=arr(ref("User")))
post("/api/users", "admin", "Create an account", access="admin",
     body=obj({"username": s(), "password": s(format="password"), "display_name": s(), "email": s(), "role": s(enum=["admin", "member"])},
              ["username", "password"]),
     example={"username": "kim", "password": "a-long-password", "role": "member"}, ok=obj({"ok": b(), "id": i()}), errors=(409,))
post("/api/users/{user_id}", "admin", "Change, disable or delete an account", access="admin",
     body=obj({"display_name": s(), "email": s(), "role": s(enum=["admin", "member"]), "password": s(format="password"),
               "disabled": b(), "delete": b()}), example={"display_name": "Kim"}, ok=obj(), errors=(404,))
get("/api/tokens", "admin", "API tokens of the household", access="admin", ok=arr(ref("Token")))
post("/api/tokens", "admin", "Create an API token (the token is shown only in this answer)", access="admin",
     body=obj({"name": s(), "scope": s(enum=["api", "calendar"])}, ["name"]), example={"name": "Script", "scope": "api"},
     ok=obj({"id": i(), "name": s(), "scope": s(), "token": s("kmb_…, shown only once")}))
post("/api/tokens/{token_id}/revoke", "admin", "Revoke an API token", access="admin", ok=OK, errors=(404,))
get("/api/invites", "admin", "Invitations into this household", access="admin", ok=arr(ref("Invite")))
post("/api/invites", "admin", "Create an invitation (code and link shown once)", access="admin",
     body=obj({"role": s(enum=["admin", "member"]), "days": i("Valid for (default 7)"), "note": s()}),
     example={"role": "member", "days": 7, "note": "Kim"}, ok=ref("InviteCreated"))
post("/api/invites/{invite_id}/revoke", "admin", "Revoke an invitation", access="admin", ok=OK, errors=(404,))

# ================= households =================
get("/api/household", "households", "Household: language, description, people, members",
    ok=obj({"id": i(), "name": s(), "language": s(enum=["de", "en"]), "description": s(), "people": nullable(i("Servings, null = members")),
            "people_count": i(), "microwave_watt": i("Own microwave, W (default 800)"), "members": arr(obj({"id": i(), "name": s(), "prefs": ref("MemberPrefs")}))}))
post("/api/household", "households", "Change the household", access="admin",
     body=obj({"name": s(), "language": s(enum=["de", "en"]), "description": s("For the AI runs"),
               "people": nullable(i("Servings 1–30, null = number of members")),
               "microwave_watt": i("Own microwave, W: reheating times are converted to it")}),
     example={"description": "Two adults, little time on weekdays"}, ok=obj())
post("/api/members/{user_id}", "households", "Display name and preferences of a member",
     desc="Your own; admins with a session also any member's.", body=ref("MemberPrefs"), example={"diet": "vegetarian"}, ok=obj())
get("/api/households", "households", "All households of the installation", access="instance",
    ok=arr(obj({"id": i(), "name": s(), "members": i(), "admins": i(), "open_invites": i()})))
post("/api/households", "households", "Create a household (answer with an invitation for its first admin)", access="instance",
     body=obj({"name": s(), "language": s(enum=["de", "en"]), "days": i()}, ["name"]), example={"name": "Flat share"},
     ok=obj({"ok": b(), "id": i(), "name": s(), "invite": ref("InviteCreated")}))
post("/api/households/{household_id}", "households", "Rename (`{name}`) or delete an empty household (`{delete: true}`)",
     access="instance", body=obj({"name": s(), "delete": b()}), example={"name": "Flat share"}, ok=obj(), errors=(404,))
post("/api/households/{household_id}/invites", "households", "New invitation into a household", access="instance",
     body=obj({"role": s(enum=["admin", "member"]), "days": i(), "note": s()}), example={"role": "admin"}, ok=ref("InviteCreated"), errors=(404,))

# ================= app =================
get("/api/version", "app", "Version and changelog", access="public", query=[("lang", s(enum=["de", "en"]), "Changelog language")],
    ok=ref("Version"))
get("/api/config", "app", "Addresses of the installation and language", ok=obj({"urls": arr(s()), "https_url": s(), "author": s(),
                                                                                 "language": s()}))
get("/api/status", "app", "Counts and the state of every AI job",
    ok=obj({"counts": obj(desc="Cards per status"), "plan": i(), "stack": obj(desc="Cards in the stack per meal"),
            "refill": STATE, "wochenplan": STATE, "linkimport": STATE, "mealprep": STATE, "kochideen": STATE, "fotoimport": STATE, "aufwaermen": STATE,
            "match": obj({"members": i(), "waiting": i()}, desc="Only with several members")}))
get("/api/work", "AI jobs", "Waiting AI jobs (for an outside worker, polled every few minutes)",
    desc="`ai: \"app\"` = the app runs AI jobs itself, an outside worker has nothing to do.",
    ok=obj({"likes_pending": i(), "last_swipe_age_s": nullable(i()), "refill": STATE, "stack": i(), "wochenplan": STATE,
            "linkimport": STATE, "mealprep": STATE, "kochideen": STATE, "fotoimport": STATE, "aufwaermen": STATE,
            "language": s(), "ai": s()}))
get("/api/openapi.json", "app", "This OpenAPI description", access="public", ok=obj(desc="OpenAPI 3.1 document"))
get("/api/docs", "app", "API documentation (Swagger UI)", access="public", ok_type=HTML, ok=s())
get("/api/docs/{asset}", "app", "Files of the documentation page", access="public", ok_type="*/*", ok=s(), errors=(404,))

# ================= recipes =================
get("/api/rezepte", "recipes", "List recipes", query=[("suche", s(), "Search in name and source")], ok=arr(ref("RecipeSummary")))
post("/api/rezepte", "recipes", "Create a recipe", body=ref("RecipeInput"),
     example={"name": "Chili con Carne", "portionen": 4, "zutaten_text": "500 g ground beef\n1 can tomatoes",
              "schritte_text": "Brown the meat.\n\nAdd the tomatoes.", "naehrwerte": {"kcal": 520}},
     ok=obj({"ok": b(), "slug": s(), "id": i()}))
get("/api/rezepte/{slug}", "recipes", "One recipe with the edit texts", ok=ref("RecipeDetail"), errors=(404,))
post("/api/rezepte/{slug}", "recipes", "Change a recipe (only the fields sent)", body=ref("RecipeInput"),
     example={"tags": ["Mealprep"]}, ok=obj({"ok": b(), "slug": s()}))
get("/api/aufwaermen", "recipes", "Reheating of a recipe or a plan entry for some portions",
    desc="Leftover entries („Rest: Chili“) find their recipe by name. Without a reheating field the answer is an estimate (geschaetzt).",
    query=[("slug", s(), "Recipe slug"), ("eintrag", i(), "Instead of slug: plan entry id"), ("portionen", n(), "Default 1"),
           ("watt", i(), "Own microwave, default the household setting")],
    ok={"allOf": [ref("ReheatInfo"), obj({"slug": s(), "name": s()})]}, errors=(400, 404))
get("/api/aufwaermen/job", "AI jobs", "State of the job „add reheating tips“ and how many recipes have no field",
    ok={"allOf": [STATE, obj({"ohne": i("Recipes without the field"), "alle": b()})]})
post("/api/aufwaermen/job", "AI jobs", "Add reheating tips to the recipes without them (queues the AI job reheat)",
     body=obj({"alle": b("Also recipes that already have the field")}), example={}, ok=STATE)
post("/api/aufwaermen/state", "AI jobs", "State of the reheating job (from the run)", body=STATE_BODY, example={"state": "running"}, ok=STATE)
post("/api/rezepte/{slug}/bild", "recipes", "Set the recipe image",
     desc="The image as the body (image/jpeg, image/png, image/webp, up to 15 MB), or JSON `{\"url\"}` to download it.",
     raw=IMAGE, body=obj({"url": s()}), example={"url": "https://example.org/chili.jpg"}, ok=obj({"ok": b(), "bild": s()}), errors=(413,))
post("/api/rezepte/{slug}/einkauf", "recipes", "Put the ingredients on the shopping list",
     body=obj({"portionen": n("Servings (default 2)")}), example={"portionen": 4}, ok=obj(), errors=(502,))
post("/api/rezepte/{slug}/plan", "recipes", "Put the recipe into the week plan",
     body=obj({"datum": DATE, "typ": MEAL_TYPE, "portionen": n()}), example={"datum": "2026-12-01", "typ": "dinner", "portionen": 2},
     ok=obj({"ok": b(), "name": s(), "datum": s()}), errors=(502,))
post("/api/rezepte/{slug}/sterne", "recipes", "Rate a recipe", body=obj({"sterne": i(minimum=0, maximum=5)}), example={"sterne": 5},
     ok=obj({"ok": b(), "sterne": nullable(i())}), errors=(502,))
get("/api/zutaten", "recipes", "Known ingredients with their supermarket category", query=[("suche", s(), "Part of the name")], ok=obj())
post("/api/zutaten", "recipes", "Set the supermarket category of an ingredient", body=obj({"name": s(), "kategorie": s()}, ["name", "kategorie"]),
     example={"name": "Zimt", "kategorie": "Gewürze"}, ok=OK)
get("/api/einheiten", "recipes", "Units (German keys, e.g. EL = tbsp)", ok=arr(s()))
get("/api/scrape", "recipes", "Read a recipe page (schema.org Recipe)",
    query=[("url", s(), "Address of the page"), ("roh", s(), "1 = the raw data of the page")], ok=obj(), errors=(422,))
post("/api/linkimport", "recipes", "Recipe from a link: queue the AI job", body=obj({"url": s(), "hinweis": s("Note for the AI")}, ["url"]),
     example={"url": "https://example.org/chili", "hinweis": ""}, ok=STATE)
post("/api/linkimport/state", "AI jobs", "State of the link import (from the run)",
     body=obj({"state": s(enum=["running", "done", "failed"]), "note": s(), "slug": s("The new recipe"), "name": s()}, ["state"]),
     example={"state": "done", "slug": "chili-con-carne", "name": "Chili con Carne"}, ok=STATE)
get("/api/fotoimport", "recipes", "State of the recipe from a photo",
    ok=obj({"state": s(), "grund": s("On failure: vision (model cannot read images) or kein_rezept"), "bilder": arr(obj()),
            "slug": s(), "name": s(), "note": s()}))
post("/api/fotoimport", "recipes", "Recipe from photos: queue the AI job",
     desc="1–6 images, 5 MB each, base64 or data URL. 409 while one waits or runs.",
     body=obj({"bilder": arr(obj({"daten": s("base64 or data URL")}), minItems=1, maxItems=6), "hinweis": s()}, ["bilder"]),
     example={"bilder": [{"daten": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="}]},
     ok=STATE, errors=(409, 413))
get("/api/fotoimport/bild/{nr}", "recipes", "One image of the photo import (only while the job waits or runs)", ok_type="image/*",
    ok=s(contentMediaType="image/*"), errors=(404,))
post("/api/fotoimport/bild", "AI jobs", "Use an image of the photo import as the recipe photo",
     body=obj({"slug": s(), "bild": i("Number of the image, 1 = first")}, ["slug", "bild"]), example={"slug": "chili-con-carne", "bild": 1},
     ok=obj({"ok": b(), "bild": s()}), errors=(404,))
post("/api/fotoimport/abbrechen", "recipes", "Drop a waiting photo import", body=obj(), example={}, ok=OK, errors=(409,))
post("/api/fotoimport/state", "AI jobs", "State of the photo import (from the run)",
     desc="`note` starting with NO_IMAGE or NO_RECIPE sets `grund`.",
     body=obj({"state": s(), "note": s(), "slug": s(), "name": s()}), example={"state": "failed", "note": "NO_RECIPE: not a recipe"}, ok=STATE)
get("/rezept/{slug}/druck", "recipes", "Print view of a recipe (HTML)",
    query=[("portionen", n(), "Servings"), ("drucken", s(), "1 = open the print dialog"), ("pdf", s(), "1 = layout for the PDF"),
           ("lang", s(enum=["de", "en"]), "Language")], ok_type=HTML, ok=s(), errors=(404,))
get("/rezept/{slug}.pdf", "recipes", "Recipe as a PDF (needs Gotenberg)",
    query=[("portionen", n(), "Servings"), ("lang", s(enum=["de", "en"]), "Language")], ok_type="application/pdf",
    ok=s(contentMediaType="application/pdf"), errors=(404, 502))

# ================= images =================
get("/img/{file}", "images", "Image of a swipe card", ok_type="image/*", ok=s(contentMediaType="image/*"), errors=(404,))
get("/mimg/{image_id}/{size}.webp", "images", "Image of a recipe", ok_type="image/*", ok=s(contentMediaType="image/*"), errors=(404,))

# ================= swipe cards =================
get("/api/stack", "swipe cards", "Cards to swipe",
    desc="With several members only the cards you have not swiped yet.",
    query=[("limit", i(maximum=100), "Default 20"), ("meal", CARD_MEAL, "Only this meal")], ok=arr(ref("Card")))
post("/api/swipe", "swipe cards", "Swipe a card",
     desc="With several members a like only counts when everybody liked it: answer with `match`, `status`, `waiting`.",
     body=obj({"id": i(), "action": s(enum=["like", "nope", "plan"])}, ["id", "action"]), example={"id": 1, "action": "like"},
     ok=obj({"ok": b(), "match": b(), "status": s(), "waiting": arr(s())}), errors=(404, 409))
post("/api/undo", "swipe cards", "Take a swipe back", body=obj({"id": i()}, ["id"]), example={"id": 1}, ok=OK, errors=(409,))
get("/api/match", "swipe cards", "Your likes that wait for the other members",
    ok=obj({"members": i(), "waiting": arr(ref("Card"))}))
get("/api/likes", "swipe cards", "Liked cards",
    query=[("pending", s(), "1 = only not imported yet"), ("plan", s(), "1 = only marked for this week")], ok=arr(ref("Card")))
get("/api/history", "swipe cards", "Swipe history", query=[("limit", i(), "Default 200")], ok=arr(obj()))
get("/api/nopes", "swipe cards", "Noes",
    query=[("offen", s(), "1 = only noes with reason „ingredient“ but no ingredient or text"), ("limit", i(maximum=200), "Default 50")],
    ok=arr(ref("Card")))
get("/api/gruende", "swipe cards", "Reason codes for a no", ok=obj(additionalProperties=s()))
get("/api/geschmack", "swipe cards", "Taste evaluation and profile", ok=obj())
post("/api/geschmack", "swipe cards", "Save the taste profile text", body=obj({"text": s()}), example={"text": "Likes spicy food"}, ok=OK)
get("/api/abneigungen", "swipe cards", "Dislikes (ingredients)", ok=arr(obj({"zutat": s(), "n": i("Noes because of it"), "manuell": b()})))
post("/api/abneigungen", "swipe cards", "Add or remove a dislike",
     body=obj({"zutat": s(), "aktion": s(enum=["add", "remove"])}, ["zutat", "aktion"]), example={"zutat": "Koriander", "aktion": "add"},
     ok=arr(obj()))
post("/api/cards", "swipe cards", "Add cards to the stack (one or a list)",
     body={"oneOf": [ref("CardInput"), arr(ref("CardInput"))]},
     example={"url": "https://example.org/rezept/test", "title": "Test", "meal": "haupt"},
     ok=obj({"added": arr(i()), "duplicates": arr(s())}))
post("/api/cards/{card_id}/edit", "swipe cards", "Correct a card",
     body=obj({k: v for k, v in SCHEMAS["Card"]["properties"].items()
               if k in ("title", "reason", "description", "tags", "kcal", "kcal_est", "minutes", "servings", "meal", "zutaten")}),
     example={"title": "Test"}, ok=OK)
post("/api/cards/{card_id}/kcal", "swipe cards", "Set the calories of a card",
     body=obj({"kcal": nullable(i()), "geschaetzt": b("Estimated (default true)")}), example={"kcal": 600}, ok=OK)
post("/api/cards/{card_id}/meal", "swipe cards", "Set the meal of a card", body=obj({"meal": CARD_MEAL}, ["meal"]), example={"meal": "haupt"}, ok=OK)
post("/api/cards/{card_id}/plan", "swipe cards", "Mark a liked card for this week", body=obj({"plan": b()}), example={"plan": True}, ok=OK)
post("/api/cards/{card_id}/why", "swipe cards", "Reasons for a no",
     body=obj({"why": {"oneOf": [arr(s()), s()], "description": "Codes from GET /api/gruende"}, "zutaten": arr(s()), "text": s()}),
     example={"why": ["zutat"], "zutaten": ["Koriander"]}, ok=OK)
post("/api/cards/{card_id}/mealie", "swipe cards", "Mark a card as imported (with the recipe slug)",
     body=obj({"slug": s(pattern="^[a-z0-9-]{1,120}$")}, ["slug"]), example={"slug": "chili-con-carne"}, ok=OK)
post("/api/cards/{card_id}/fehler", "swipe cards", "Mark a card's import as failed", body=obj({"note": s()}), example={"note": "page gone"}, ok=OK)
post("/api/cards/{card_id}/nope", "swipe cards", "Changed my mind: a like becomes a no", body=obj(), example={}, ok=OK)
get("/api/known-urls", "swipe cards", "Addresses of all cards (to skip known ones)", ok=arr(s()))
post("/api/nachschub", "swipe cards", "Ask for new cards (queues the AI job)",
     body=obj({"wunsch": s(), "meal": s(enum=["haupt", "fruehstueck", "gemischt"])}), example={"wunsch": "", "meal": "gemischt"}, ok=STATE)
post("/api/nachschub/state", "AI jobs", "State of the refill job (from the run)", body=STATE_BODY, example={"state": "done"}, ok=STATE)

# ================= week =================
get("/api/woche", "week", "Week plan Monday to Sunday", query=[("start", DATE, "Any day of the week (default today)")],
    ok=obj({"start": DATE, "ende": DATE, "heute": DATE, "eintraege": arr(ref("PlanEntry")), "yazio": b()}))
post("/api/plan", "week", "Add a plan entry (with a recipe or only title/text)",
     body=obj({"datum": DATE, "typ": MEAL_TYPE, "slug": s(), "titel": s(), "text": s(), "scale": n()}, ["datum", "typ"]),
     example={"datum": "2026-12-01", "typ": "dinner", "titel": "Leftovers: Chili"}, ok=obj({"ok": b(), "id": i()}))
post("/api/plan/{plan_id}/weg", "week", "Delete a plan entry", body=obj(), example={}, ok=obj({"ok": b(), "geloescht": b()}))
get("/api/wochenplan", "week", "State of the AI week plan and its proposal", ok=obj({"status": STATE, "vorschlag": nullable(obj())}))
post("/api/wochenplan", "week", "Ask for a week plan proposal (queues the AI job)",
     body=obj({"start": DATE, "wunsch": s(), "personen": i("1–30, for guests")}), example={"wunsch": "two vegetarian days"}, ok=STATE)
post("/api/wochenplan/annehmen", "week", "Accept the proposal: plan, shopping list, freezer",
     body=obj(), example={}, ok=obj({"ok": b(), "plan": i(), "rezepte_auf_liste": i(), "fehler": arr(s())}), errors=(409,))
post("/api/wochenplan/verwerfen", "week", "Drop the proposal", body=obj(), example={}, ok=OK)
post("/api/wochenplan/tauschen", "week", "Swap a dish of the proposal",
     desc="`{slug, neu}` swaps right away, `{slug, wunsch}` lets the AI swap.",
     body=obj({"slug": s(), "neu": s(), "wunsch": s()}, ["slug"]), example={"slug": "chili-con-carne", "neu": "lasagne"},
     ok=obj(), errors=(409,))
post("/api/wochenplan/personen", "week", "Scale the proposal to another number of people",
     body=obj({"personen": i(minimum=1, maximum=30)}, ["personen"]), example={"personen": 3}, ok=obj({"ok": b(), "vorschlag": obj()}), errors=(409,))
post("/api/wochenplan/vorschlag", "AI jobs", "The AI's week plan proposal",
     body=obj({"start": DATE, "ende": DATE, "zusammenfassung": s(), "prep": s(), "personen": i(),
               "eintraege": arr(obj({"datum": DATE, "typ": MEAL_TYPE, "slug": s(), "titel": s(), "text": s(), "scale": n(),
                                     "einkaufen": b()})), "vorrat_neu": arr(ref("FreezerInput"))}, ["eintraege"]),
     example={"eintraege": [{"datum": "2026-12-01", "typ": "dinner", "titel": "Pasta"}]}, ok=obj({"ok": b(), "eintraege": i()}))
post("/api/wochenplan/state", "AI jobs", "State of the week plan job (from the run)", body=STATE_BODY, example={"state": "running"}, ok=STATE)
get("/kalender.ics", "week", "Week plan as an iCal feed (e.g. for Home Assistant)", access="calendar",
    query=[("typ", s(), "Only these meals, comma-separated, e.g. lunch,dinner"), ("token", s(), "API token (instead of the header)")],
    ok_type="text/calendar", ok=s())

# ================= meal prep =================
get("/api/mealprep", "meal prep", "Meal prep job and schedules", query=[("datum", DATE, "Prep day of the schedule to return")],
    ok=obj({"status": STATE, "daten": arr(DATE, "Days with a schedule"), "ablauf": nullable(obj(desc="Schedule of `datum`"))}))
post("/api/mealprep", "meal prep", "Ask for a meal prep schedule (queues the AI job)",
     body=obj({"datum": DATE, "wunsch": s()}, ["datum"]), example={"datum": "2026-11-29"}, ok=STATE)
post("/api/mealprep/ablauf", "AI jobs", "The AI's meal prep schedule",
     body=obj({"datum": DATE, "titel": s(), "dauer_min": s(), "hinweis": s(),
               "rezepte": arr(obj({"slug": s(), "name": s(), "portionen": s()})),
               "vorbereiten": obj({"geraete": arr(s()), "zutaten": arr(obj())}),
               "schritte": arr(obj({"titel": s(), "text": s(), "rezepte": arr(s()), "zutaten": arr(obj()),
                                    "timer": arr(obj({"min": n(), "label": s()})), "nebenbei": s(), "minuten": s()}))},
              ["datum", "rezepte", "schritte"]),
     example={"datum": "2026-11-29", "rezepte": [{"slug": "chili-con-carne"}], "schritte": [{"text": "Chop the onions"}]},
     ok=obj({"ok": b(), "schritte": i(), "rezepte": i()}))
post("/api/mealprep/state", "AI jobs", "State of the meal prep job (from the run)", body=STATE_BODY, example={"state": "running"}, ok=STATE)

# ================= shopping =================
get("/api/einkauf", "shopping", "Shopping list",
    ok=obj({"items": arr(ref("ShoppingItem")), "zugang": b("Bring! is connected"), "immer": obj({"namen": arr(s()), "kategorien": arr(s())})}),
    errors=(502,))
get("/api/einkauf/test", "shopping", "Test the Bring! connection", ok=obj(), errors=(502,))
post("/api/einkauf/neu", "shopping", "Add an item",
     desc="`client_id` (8–64 letters, digits, `-`, `_`): sending the same id again returns the first answer with "
          "`\"repeat\": true` instead of adding the item twice (offline queue of the app).",
     body=obj({"text": s("e.g. „2 l Milch“"), "client_id": s()}, ["text"]), example={"text": "2 l Milch"},
     ok=obj({"ok": b(), "id": i(), "repeat": b()}), errors=(502,))
post("/api/einkauf/weg", "shopping", "Remove items", body=obj({"ids": arr(i())}, ["ids"]), example={"ids": [999999]}, ok=OK)
post("/api/einkauf/check", "shopping", "Tick or untick an item", body=obj({"id": s(), "checked": b()}, ["id"]),
     example={"id": "999999", "checked": True}, ok=OK, errors=(502,))
post("/api/einkauf/rezepte", "shopping", "Put several recipes on the list",
     body=obj({"rezepte": arr(obj({"slug": s(), "scale": n()}))}, ["rezepte"]), example={"rezepte": [{"slug": "chili-con-carne", "scale": 1}]},
     ok=obj())
post("/api/einkauf/bring", "shopping", "Send the list to Bring!", body=obj(), example={}, ok=obj(), errors=(502,))
post("/api/einkauf/immer", "shopping", "„Always at home“ list: add or remove a name",
     body=obj({"name": s(), "aktion": s(enum=["add", "remove"])}, ["name", "aktion"]), example={"name": "Zimt", "aktion": "add"},
     ok=obj({"namen": arr(s()), "kategorien": arr(s())}))

# ================= pantry =================
get("/api/schrank", "pantry", "Cupboard, last bookings, items on their way",
    ok=obj({"schrank": arr(ref("PantryItem")), "buchungen": arr(obj()), "unterwegs": arr(obj()), "kategorien": arr(s()),
            "haltbar_namen": arr(s())}))
post("/api/schrank", "pantry", "Add to the cupboard",
     body=obj({"name": s(), "menge": nullable(n()), "einheit": s(), "kategorie": s(), "notiz": s()}, ["name"]),
     example={"name": "Reis", "menge": 1, "einheit": "kg"}, ok=obj())
post("/api/schrank/{pantry_id}", "pantry", "Change an amount or mark as empty",
     body=obj({"menge": nullable(n()), "einheit": s(), "leer": b(), "notiz": s()}), example={"menge": 500, "einheit": "g"}, ok=obj())
post("/api/schrank/rueckgaengig", "pantry", "Undo a booking", body=obj({"buchung": i()}, ["buchung"]), example={"buchung": 1}, ok=obj())
post("/api/schrank/kochen", "pantry", "Cooked: take the ingredients out of the cupboard",
     body=obj({"plan_id": i(), "slug": s(), "portionen": n()}), example={"slug": "chili-con-carne", "portionen": 2},
     ok=obj({"ok": b(), "schrank": obj()}))
post("/api/schrank/mealprep", "pantry", "Meal prep cooked: take the ingredients of these recipes out",
     body=obj({"datum": DATE, "slugs": arr(s())}), example={"datum": "2026-11-29", "slugs": ["chili-con-carne"]}, ok=obj({"ok": b(), "schrank": obj()}))
post("/api/schrank/kauf", "pantry", "Check Bring! for bought items now", body=obj(), example={}, ok=obj({"gebucht": arr(s())}), errors=(502,))
get("/api/schrank/rezepte", "pantry", "Own recipes by what is in cupboard and freezer (no AI)",
    query=[("limit", i(), "Default 30")],
    ok=obj({"rezepte": arr(obj({"slug": s(), "name": s(), "da": arr(obj()), "fehlt": arr(obj()), "knapp": arr(obj()), "treffer": i(),
                                "gesamt": i(), "quote": n(), "bald": arr(obj())})), "bald": arr(obj(), "Stock running out within 14 days"),
            "vorrat": i()}))
post("/api/schrank/rezepte/einkauf", "pantry", "Only the missing things of a recipe onto the list",
     body=obj({"slug": s(), "portionen": n()}, ["slug"]), example={"slug": "chili-con-carne", "portionen": 2}, ok=obj())
get("/api/barcode/{code}", "pantry", "Look up a barcode (own list, then Open Food Facts)", ok=obj(), errors=(400,))
post("/api/barcode", "pantry", "Book a scanned product",
     body=obj({"code": s(), "zutat": s(), "menge": n(), "einheit": s(), "aktion": s(enum=["rein", "leer", "merken"]),
               "kategorie": s(), "produkt": s(), "marke": s()}, ["code"]),
     example={"code": "4000417025005", "zutat": "Schokolade", "aktion": "merken"}, ok=obj())
get("/api/vorrat", "pantry", "Freezer", ok=arr(ref("FreezerItem")))
post("/api/vorrat", "pantry", "Add to the freezer", body=ref("FreezerInput"), example={"name": "Chili", "anzahl": 2, "einheit": "Portion"},
     ok=arr(ref("FreezerItem")))
post("/api/vorrat/{freezer_id}", "pantry", "Change a freezer entry (`{delta: -1}` or fields; at 0 it is gone)",
     body={"allOf": [ref("FreezerInput"), obj({"delta": n()})]}, example={"delta": -1}, ok=arr(ref("FreezerItem")), errors=(404,))
get("/api/kochideen", "pantry", "AI ideas what to cook from the pantry",
    ok=obj({"status": STATE, "vorschlag": nullable(obj({"zusammenfassung": s(), "ideen": arr(obj({
        "titel": s(), "grund": s(), "nutzt": arr(s()), "fehlt": arr(s()), "slug": s(), "rezept": obj(), "karte": i(),
        "karte_daten": obj()}))}))}))
post("/api/kochideen", "pantry", "Ask for cooking ideas (queues the AI job)", body=obj({"wunsch": s()}), example={"wunsch": "quick"}, ok=STATE)
post("/api/kochideen/verwerfen", "pantry", "Drop the cooking ideas", body=obj(), example={}, ok=OK)
post("/api/kochideen/vorschlag", "AI jobs", "The AI's cooking ideas (every idea an own recipe or a swipe card)",
     body=obj({"zusammenfassung": s(), "ideen": arr(obj({"slug": s(), "karte": i(), "titel": s(), "grund": s(),
                                                          "nutzt": arr(s()), "fehlt": arr(s())}))}, ["ideen"]),
     example={"ideen": [{"slug": "chili-con-carne", "grund": "uses the beans"}]}, ok=obj({"ok": b(), "ideen": i(), "fehler": arr(s())}))
post("/api/kochideen/state", "AI jobs", "State of the cooking ideas job (from the run)", body=STATE_BODY, example={"state": "running"}, ok=STATE)

# ================= nutrition =================
post("/api/gegessen", "nutrition", "Log an eaten meal (per person; also to Yazio when connected)",
     body=obj({"mealplan_id": i(), "slug": s("Instead of mealplan_id: a recipe without a plan entry"), "datum": DATE, "typ": MEAL_TYPE,
               "portionen": n(), "reis": b("With a rice side dish"), "personen": arr(i("Member ids, default you"))}),
     example={"slug": "chili-con-carne", "portionen": 1},
     ok=obj({"ok": b(), "id": i(), "name": s(), "slug": s(), "sterne": nullable(i()), "schrank": nullable(obj()), "personen": arr(i()),
             "yazio": b(), "yazio_fehler": s(), "kcal": nullable(n()), "protein": nullable(n()), "fett": nullable(n()), "kh": nullable(n())}),
     errors=(409, 502))
post("/api/gegessen/{mealplan_id}/weg", "nutrition", "Undo an eaten meal (yours, `{person}` someone's, `{alle: true}` everybody's)",
     body=obj({"person": i(), "alle": b()}), example={}, ok=obj({"ok": b(), "geloescht": i(), "yazio_fehler": s()}), errors=(502,))
get("/api/ernaehrung", "nutrition", "Nutrition per person and day",
    query=[("start", DATE, "First day (default Monday of this week)"), ("tage", i(minimum=1, maximum=31), "Days (default 7)")],
    ok=obj({"start": DATE, "ende": DATE, "heute": DATE, "personen": arr(obj()), "geplant": arr(obj()), "yazio_person": nullable(i()),
            "yazio": b()}))
post("/api/ernaehrung/ziel", "nutrition", "Daily goal of a person (own; admins any member's; empty = no goal)",
     body=obj({"user_id": i(), "kcal": nullable(n()), "protein": nullable(n())}), example={"kcal": 2000, "protein": 100},
     ok=obj({"ok": b(), "user_id": i(), "ziel": NUTRIENTS}))
post("/api/ernaehrung/yazio", "nutrition", "Whose meals go to Yazio", access="admin",
     body=obj({"user_id": nullable(i())}), example={"user_id": None}, ok=obj())
get("/api/yazio/tag", "nutrition", "One day in Yazio", query=[("date", DATE, "Default today")], ok=obj(), errors=(502,))
get("/api/mahlzeit", "nutrition", "Nutrients per serving of a plan entry's recipe", query=[("id", i(), "Plan entry id")], ok=obj(), errors=(502,))
get("/api/rezept", "nutrition", "Nutrients per serving of a recipe", query=[("slug", s(), "Recipe slug")],
    ok=obj({"name": s(), "slug": s(), "pro_portion": NUTRIENTS, "beilage_reis": b(), "kategorie": s(), "einfrieren": b(), "monate": i()}),
    errors=(502,))

# ================= AI settings =================
get("/api/ai/status", "AI jobs", "Does the app run AI jobs itself, in which language, what runs now",
    ok=obj({"provider": s(), "language": s(), "running": nullable(obj())}))
get("/api/ai", "AI jobs", "AI settings and the last runs (the key never comes back)", access="admin",
    ok=obj({"settings": obj({"provider": s(enum=["extern", "anthropic", "openai"]), "model": s(), "has_key": b()}), "runs": arr(obj()),
            "today": n("Cost today"), "running": nullable(obj()), "anthropic_models": arr(s()), "sdk": nullable(s())}))
post("/api/ai", "AI jobs", "Change the AI settings", access="admin",
     body=obj({"provider": s(enum=["extern", "anthropic", "openai"]), "model": s(), "base_url": s(), "api_key": s(format="password"),
               "api_key_clear": b(), "run_budget": n(), "day_budget": n(), "max_turns": i(), "effort": s(), "search_url": s()}),
     example={"provider": "extern"}, ok=obj({"ok": b(), "settings": obj()}))
post("/api/ai/test", "AI jobs", "Test the AI settings", access="admin", body=obj(), example={}, ok=obj())
get("/api/ai/runs/{run_id}", "AI jobs", "Log of one AI run", access="admin", ok=obj(), errors=(400,))

# ================= video import (docs/video.md) =================
get("/api/video", "AI jobs", "Video import and Whisper settings (the key never comes back), versions of yt-dlp and ffmpeg", access="admin",
    ok=obj({"settings": obj({"enabled": b(), "whisper_url": s(), "whisper_api": s(enum=["auto", "openai", "asr"]), "whisper_model": s(),
                             "language": s(), "max_minutes": i(), "max_mb": i(), "autoupdate": b(), "has_key": b()}),
            "status": obj({"ytdlp": nullable(s()), "ffmpeg": nullable(s()), "whisper_api": nullable(s())})}))
post("/api/video", "AI jobs", "Change the video import and Whisper settings", access="admin",
     body=obj({"enabled": b(), "whisper_url": s(), "whisper_api": s(enum=["auto", "openai", "asr"]), "whisper_model": s(),
               "whisper_key": s(format="password"), "whisper_key_clear": b(), "language": s("auto, household or a language code"),
               "max_minutes": i(minimum=1, maximum=60), "max_mb": i(minimum=10, maximum=2000), "autoupdate": b()}),
     example={"enabled": True, "language": "auto"}, ok=obj({"ok": b(), "settings": obj()}))
post("/api/video/test", "AI jobs", "Send the built-in 2 s test sound to Whisper", access="admin", body=obj(), example={},
     ok=obj({"ok": b(), "api": s(), "seconds": n(), "text": s(), "error": s()}))
post("/api/video/update", "AI jobs", "Update yt-dlp (pip, into the data directory)", access="admin",
     desc="Installs the newest yt-dlp from PyPI; needs `{confirm: true}`.", body=obj({"confirm": b()}, ["confirm"]),
     example={"confirm": True}, ok=obj({"ok": b(), "version": nullable(s()), "note": s()}))
post("/api/videoimport/bild", "AI jobs", "Use a still of a read video (tool read_video) as the recipe photo",
     body=obj({"slug": s(), "video": s("Key from the read_video answer"), "bild": i("Number of the still, 0 = the cover image")},
              ["slug", "video", "bild"]),
     example={"slug": "chili-con-carne", "video": "0123456789abcdef", "bild": 1}, ok=obj({"ok": b(), "bild": s()}), errors=(404,))

# ================= connections =================
get("/api/integrations", "connections", "Connections (secrets only as true/false)", access="admin",
    ok=obj({"bring": obj(), "yazio": obj(), "telegram": obj(), "ntfy": obj(), "webpush": obj({"configured": b(), "devices": i()})}))
post("/api/integrations/{name}", "connections", "Save (`{field: value}`) or remove (`{delete: true}`) a connection", access="admin",
     desc="Fields: bring `email, password, list`; yazio `email, password`; telegram `bot_token, chat_id`; ntfy `server, topic, token`.",
     body=obj(desc="The fields of the connection", properties={"delete": b()}),
     example={"server": "https://ntfy.example.org", "topic": "kombuese-test"},
     ok=obj())
post("/api/integrations/{name}/test", "connections", "Test a connection", access="admin", body=obj(), example={}, ok=obj(), errors=(502,))
post("/api/notify", "connections", "Send a notification to every channel (incl. Web Push)",
     body=obj({"text": s(), "title": s(), "view": s("Opens the app there on a tap",
                                                    enum=["home", "deck", "recipes", "week", "pantry", "shop", "likes"])}, ["text"]),
     example={"text": "Dinner is ready", "view": "week"}, ok=obj())

# ================= backup =================
get("/api/backup", "backup", "Download a backup of the whole installation (.tar.gz)", access="instance",
    query=[("secrets", s(enum=["1"]), "1 = with the connections' secrets")], ok_type="application/gzip",
    ok=s(contentMediaType="application/gzip"))
post("/api/backup/import", "backup", "Import a backup (replaces everything)", access="instance",
     desc="Body: the .tar.gz from GET /api/backup. 409 while an AI run is going.",
     raw={"application/gzip": {"schema": s(contentMediaType="application/gzip")}}, ok=ref("BackupResult"), errors=(400, 409, 413))

# ================= push =================
get("/api/push/key", "push", "Public VAPID key, own devices", access="session",
    ok=obj({"key": s(), "devices": arr(obj({"id": i(), "agent": s(), "created": s(), "last_ok": nullable(s()), "last_error": nullable(s())})),
            "household_devices": i()}))
post("/api/push/subscribe", "push", "Subscribe this device (or `{endpoint or id, delete: true}`)", access="session",
     body={"allOf": [ref("PushSubscription"), obj({"id": i(), "delete": b()})]},
     example={"endpoint": "https://push.example.org/sub/1", "keys": {"p256dh": "…", "auth": "…"}},
     ok=obj({"ok": b(), "id": i(), "secret": s("For GET /api/push/pending"), "key": s(), "removed": i()}))
post("/api/push/unsubscribe", "push", "Unsubscribe a device", access="session", body=obj({"endpoint": s(), "id": i()}),
     example={"id": 999999}, ok=obj({"ok": b(), "removed": i()}))
post("/api/push/test", "push", "Send a test message to a device", access="session",
     body=obj({"endpoint": s(), "id": i(), "lang": s(enum=["de", "en"])}), example={"id": 999999, "lang": "en"}, ok=obj())
get("/api/push/pending", "push", "Messages waiting for a device (service worker after a push; each delivered once)", access="push",
    query=[("id", i(), "Device id from /api/push/subscribe")],
    ok=obj({"items": arr(obj({"id": i(), "title": s(), "text": s(), "view": s(), "created": s()}))}), errors=(404,))

# ================= sharing =================
get("/api/rezepte/{slug}/teilen", "sharing", "The public link of a recipe, or null", ok=obj({"link": nullable(ref("Share"))}), errors=(404,))
post("/api/rezepte/{slug}/teilen", "sharing", "Share a recipe (creates the link or returns the existing one)",
     desc="AI runs inside the app cannot share.", body=obj(), example={}, ok=obj({"ok": b(), "link": ref("Share")}), errors=(404,))
get("/api/freigaben", "sharing", "All shared links of the household", access="admin", ok=arr(ref("Share")))
post("/api/freigaben/{share_id}/weg", "sharing", "Stop sharing (the link is invalid at once, for good)", body=obj(), example={},
     ok=obj(), errors=(404,))
SHARE_DESC = ("The token opens only this one recipe. Answers carry `X-Robots-Tag: noindex`, `Referrer-Policy: no-referrer`; "
              "per address at most 300 requests, 10 PDFs and 20 unknown links in 10 minutes (then 429).")
get("/s/{token}", "sharing", "Public page of a shared recipe", access="public", desc=SHARE_DESC,
    query=[("portionen", n(), "Servings")], ok_type=HTML, ok=s(), errors=(404, 429))
get("/s/{token}/bild", "sharing", "Photo of a shared recipe", access="public", desc=SHARE_DESC, ok_type="image/*",
    ok=s(contentMediaType="image/*"), errors=(404, 429))
get("/s/{token}.pdf", "sharing", "PDF of a shared recipe", access="public", desc=SHARE_DESC, query=[("portionen", n(), "Servings")],
    ok_type="application/pdf", ok=s(contentMediaType="application/pdf"), errors=(404, 429, 502))

# ================= MCP =================
post("/mcp", "MCP", "MCP server (JSON-RPC 2.0, stateless, no SSE)", access="mcp",
     desc="Methods `initialize`, `tools/list`, `tools/call` and more, see docs/ai.md. `?web=1` adds web tools. "
          "Requests from a foreign `Origin` are refused.",
     query=[("web", s(enum=["1"]), "1 = also offer the web tools")], body=ref("JsonRpc"),
     example={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, ok=obj({"jsonrpc": s(), "id": {}, "result": obj(), "error": obj()}))


def _version():
    f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
    try:
        with open(f, encoding="utf-8") as fh:
            return fh.read().strip() or "0"
    except OSError:
        return "0"


def document():
    """The whole OpenAPI document (a new dict every time)."""
    return {
        "openapi": "3.1.0",
        "info": {"title": TITLE, "version": _version(),
                 "summary": "HTTP API of Kombüse, the self-hosted kitchen app",
                 "description": "Everything the app does goes through this JSON API; scripts, AI workers and MCP clients use it too."
                                + AUTH_TEXT,
                 "license": {"name": "AGPL-3.0-or-later", "identifier": "AGPL-3.0-or-later"}},
        "servers": [{"url": "/", "description": "This installation"}],
        "tags": [{"name": t, "description": d} for t, d in TAGS],
        "security": MEMBER,
        "paths": {p: dict(ops) for p, ops in PATHS.items()},
        "components": {
            "schemas": SCHEMAS,
            "responses": {f"E{c}": {"description": d, "content": {"application/json": {"schema": ref("Error")}}}
                          for c, d in ERRORS.items()},
            "securitySchemes": {
                "cookieAuth": {"type": "apiKey", "in": "cookie", "name": "kombuese_<installation id>",
                               "description": "Session cookie from POST /auth/login. POST requests also need the header "
                                              "`X-Kombuese: 1` (this page adds it). The browser sends the cookie by itself."},
                "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "kmb_…",
                               "description": "API token (⚙️ Settings → API tokens). Roles = token scopes: api, calendar."},
                "calendarToken": {"type": "apiKey", "in": "query", "name": "token",
                                  "description": "API token in the address, for calendar apps (GET /kalender.ics only)."},
                "pushSecret": {"type": "apiKey", "in": "header", "name": "X-Push-Secret",
                               "description": "Secret of one device from POST /api/push/subscribe."},
            },
        },
    }
