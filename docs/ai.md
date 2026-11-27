# AI

Kombüse's AI jobs are agent runs that use the app's own API as tools:

| Job | Started by | What it does |
|---|---|---|
| **refill** | "Get more" on the swipe stack | learns the taste from swipes, reasons and stars, updates the taste profile, finds 15 new dishes on recipe sites and creates cards |
| **import** | 10 minutes after the last like | turns liked cards into full recipes (ingredients, steps, nutrition, meal prep note, photo) |
| **link** | "+ Link" in the recipes | creates a recipe from a pasted link |
| **week** | "Plan the week" | proposes a week plan (freezer first, starred dishes, nutrition goal, leftovers, meal prep day); you accept it in the app |
| **swap** | "Swap" in a proposal | replaces one dish in the proposal |
| **prep** | after accepting a plan, or by hand | builds the step-by-step sequence for the meal prep mode |
| **pantry** | "What can I cook?" in the pantry | proposes 3–5 dishes that use the cupboard and freezer (soon-expiring first): own recipes first, otherwise found recipes as swipe cards with a reason ("uses your chickpeas and the frozen spinach") |
| **photo** | "+ New" → photo or screenshot | creates a recipe from 1–6 images (cookbook page, screenshot, handwritten note), in the household language, tagged "From photo", nutrition estimated |
| **reheat** | "Add reheating tips" below the recipe list (shown while recipes lack them) | writes microwave watt and minutes from the fridge and frozen, standing time, a hint and an oven or pan alternative into the field `aufwaermen` of every recipe without it (`POST /api/aufwaermen/job {"alle": true}` also redoes the others); changes nothing else |

The AI never writes the plan or the shopping list directly: week plans are proposals you accept.

Without any AI, `GET /api/schrank/rezepte` ranks your own recipes by how much of them is in the cupboard and freezer
(names matched against the ingredient list, salt, pepper, oil, water and the "always at home" list left out), shows
what is missing or too little, and `POST /api/schrank/rezepte/einkauf` puts only the missing things on the list.

**Reheating**: new recipes from the AI come with `aufwaermen` (see [api.md](api.md#reheating)).
For recipes without it the app shows an estimate from the kind of dish, marked as estimated; the job **reheat** fills
the field once for existing recipes. With provider "External" the worker sees it as `aufwaermen` in `GET /api/work`;
or run the MCP prompt `reheat` by hand (in Claude Code `/mcp__kombuese__reheat`).

**Photos:** the images are stored on the server only while the photo job waits or runs (at most 6 images, 5 MB each,
JPEG/PNG/WebP/GIF, deleted when the job ends and after two days at the latest, not part of backups). They go only to
the configured provider: Anthropic as image blocks, OpenAI-compatible as `image_url` data URLs. The OpenAI-compatible
model needs vision; if the server refuses images, the job fails with "The AI model cannot see images" and the app
says so. An uploaded image becomes the recipe photo only if it shows the finished dish. With provider "External" the
worker gets the images with `GET /api/fotoimport/bild/<nr>` or the MCP tool `read_photos`.

**Videos:** for a TikTok, Instagram, YouTube or Facebook link the link job calls the tool `read_video(url)`: the app
downloads the video (yt-dlp), has the speech written down by your Whisper service and answers with description,
transcript and up to 12 stills (as images; a model without vision gets the text only). One still becomes the recipe
photo through `POST /api/videoimport/bild`. Setup, limits and privacy: [video.md](video.md).

Every run gets the household's language, description, number of people and each member's preferences (diet, nothing
spicy, dislikes, notes; ⚙️ Settings → Household). Dishes have to suit everybody and servings follow the number of
people; a week plan can be asked for a different number (guests). With several households on one installation the
scheduler goes through them in turn; every household has its own AI settings, cost limits and run history, and a run
only reaches its own household's data (its own internal token).

## Providers

Choose under ⚙️ Settings → AI (admins).

| Provider | What happens |
|---|---|
| **External** (default) | Nothing runs inside the app. Your own worker polls `GET /api/work` with an API token and does the jobs – for example Claude Code with the example worker below. |
| **Claude (Anthropic API)** | The app runs the jobs with the official `anthropic` SDK and your API key. Web search and fetch are Anthropic's server tools. Default model `claude-opus-5-5`, effort adjustable; `claude-sonnet-5-5` and `claude-haiku-5-5` are cheaper. A refusal by a safety classifier is retried on Anthropic's recommended fallback model (`fallbacks: "default"`, not available for Haiku). Prompt caching is on. |
| **OpenAI-compatible** | Any `/chat/completions` endpoint with tool calling: OpenAI, OpenRouter, Ollama, LM Studio, vLLM … Base URL, model and an optional key. Web search goes through your SearXNG (search URL, JSON output enabled) or DuckDuckGo; pages are read by the app. |
| **Off** | No AI runs. |

When a provider inside the app is active, `GET /api/work` reports nothing to do, so an external worker that is still
scheduled does not run jobs twice.

## Costs and limits

Settings → AI holds a **limit per run** and a **limit per day** in US dollars (default 2 and 5); a run stops when one
is reached, and the job is marked failed. Prices are built in for the Claude models (including web searches); for other
providers enter the price per million input and output tokens (0 for local models). There is also a turn limit per run.

The **run list** shows every run with model, turns, tokens, searches, cost and a log of each tool call and its result.
The **test button** makes one small call with a tool, to check key, model and tool support.

## Safety

- Each job only gets the API paths it needs (`ALLOW` in `app/jobs.py`); the run cannot touch accounts, tokens,
  connections, AI settings, the household or backups.
- Pages are fetched without access to your local network (no `localhost`, private or link-local addresses, also after
  redirects). `KOMBUESE_ALLOW_PRIVATE_URLS=true` lifts that for recipe sites in your own network.
- Prompts tell the model not to follow instructions found on web pages.
- The API key is stored encrypted and never sent back to the browser.

## Local models

Local models work if they support tool calling and have a large context:

- With Ollama set `OLLAMA_CONTEXT_LENGTH=32768` (or at least 16384). The default of 4096 is too small: the model
  forgets the task or never finishes.
- On a CPU expect a long wait: a small mini PC processed about 2–6 tokens per second, so one turn of a job took several
  minutes. A GPU makes the difference.
- Small models (3–4 B) pass the test button and simple jobs; a refill of 15 cards or a week plan wants a much bigger model.

## MCP server

The app is also an MCP server at `/mcp` (Streamable HTTP with plain JSON answers, no sessions; protocol versions
2024-11-05 to 2026-07-28), so any MCP client gets the same tools and job prompts as the runs inside the app. Sign in
with an API token with scope *api*:

```sh
claude mcp add --transport http kombuese http://kitchen.lan:9926/mcp --header "Authorization: Bearer kmb_…"
```

- **Tools:** `kombuese_api` (the app's JSON API, all paths any job may use), `read_recipe`, `add_card`, `notify`,
  `read_photos` (the images of the waiting photo import as MCP image content), `read_video` (a cooking video: description,
  transcript and stills, see [video.md](video.md); it can take a few minutes, so give the client a long tool timeout). With
  `/mcp?web=1` also `web_search` and `web_fetch`; most clients bring their own.
- **Prompts:** `refill` (arguments `wish`, `meal`), `import`, `link` (`url`, `hint`), `week` (`start`, `wish`, `people`),
  `swap` (`slug`, `name`, `wish`), `prep` (`datum`, `wish`), `pantry` (`wish`), `photo` (`hint`), `reheat`, written with the household language, description, number
  of people and preferences. In Claude Code they show up as `/mcp__kombuese__week` and so on.
- Everything works in the household of the API token.

## Example worker (provider "External")

[`tools/mcp_worker.py`](../tools/mcp_worker.py) (Python standard library) polls `GET /api/work`, claims the next
waiting job, fetches its prompt over MCP and runs `claude -p` with the MCP server; a job Claude left running is marked
failed afterwards. Run it every 5 minutes on a machine where Claude Code is signed in, with the token in
`KOMBUESE_TOKEN` or `~/.config/kombuese/token`:

```sh
*/5 * * * * KOMBUESE_URL=http://kitchen.lan:9926 python3 /path/to/tools/mcp_worker.py >> ~/kombuese-worker.log 2>&1
```

`--dry-run` shows the next job and its prompt without running it; arguments after `--` go to `claude`
(e.g. `-- --model claude-sonnet-5-5`).

You can also write your own worker against the plain HTTP API: poll `GET /api/work`, set a job's state to `running`
(`POST /api/nachschub/state`, `/api/wochenplan/state`, `/api/mealprep/state`, `/api/linkimport/state`,
`/api/fotoimport/state`, `/api/kochideen/state`, `/api/aufwaermen/state`), do the work
through the API, and set `done` or `failed`. See [api.md](api.md).
