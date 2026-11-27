"""AI inside the app: settings, providers, the agent loop, run log and cost limits.

Providers (Settings → AI):
  extern     an outside worker does the AI runs (e.g. Claude Code with an API token, polling GET /api/work).
             Nothing runs inside the app. This is the default, so older installations keep working.
  anthropic  Claude through the official Anthropic SDK (`pip install anthropic`), with Anthropic's own web search
             and web fetch. Default model claude-opus-5-5.
  openai     any OpenAI-compatible chat API with tool calling: OpenAI, Ollama, LM Studio, vLLM, OpenRouter …
             Web search goes through your SearXNG (search URL) or, without one, DuckDuckGo's HTML page.
  off        AI features are switched off.

The agent loop is our own (not the SDK's tool runner) because it runs the same way for both providers and checks
the cost limits (per run and per day) after every turn, which the runner does not offer.
Settings including the API key are stored encrypted (secretbox.py); the key never goes back to the browser.
"""
import base64
import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import household
import store
import secretbox

PROVIDERS = ("extern", "anthropic", "openai", "off")
DEFAULTS = {"provider": "extern", "model": "", "base_url": "", "effort": "medium", "search_url": "",
            "run_budget": 2.0, "day_budget": 5.0, "price_in": 0.0, "price_out": 0.0, "max_turns": 120}
ANTHROPIC_MODELS = {  # USD per million tokens: input, output, cache read (cache writes cost 1.25 x input)
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
WEB_SEARCH_USD = 0.01  # Anthropic web search, per search
UA = "Mozilla/5.0 (Kombuese; self-hosted kitchen app)"
NO_VISION = "The AI model cannot see images"  # start of the error when an OpenAI-compatible model refuses image input
_lock = threading.Lock()


class Error(Exception):
    pass


class Budget(Error):
    pass


# ---------- settings ----------

def init():
    with store.con() as c:
        c.execute("CREATE TABLE IF NOT EXISTS integrations (name TEXT PRIMARY KEY, data TEXT NOT NULL, updated TEXT)")
        c.execute("""CREATE TABLE IF NOT EXISTS ai_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, started TEXT NOT NULL, ended TEXT,
            state TEXT NOT NULL DEFAULT 'running', provider TEXT, model TEXT, turns INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, cache_read INTEGER DEFAULT 0,
            cache_write INTEGER DEFAULT 0, searches INTEGER DEFAULT 0, cost REAL DEFAULT 0,
            summary TEXT DEFAULT '', log TEXT DEFAULT '[]')""")
        # runs that were interrupted by a restart
        c.execute("UPDATE ai_runs SET state='failed', summary='interrupted (restart)', ended=started WHERE state='running'")


def stored():
    """The saved AI settings as they are in the database (older versions also held language and household)."""
    with store.con() as c:
        r = c.execute("SELECT data FROM integrations WHERE name='ai'").fetchone()
    return json.loads(secretbox.decrypt(r["data"])) if r else {}


def settings(with_key=False):
    """AI settings plus the household language and description (Settings → Household) for the prompts."""
    h = household.get()
    s = {**DEFAULTS, **{k: v for k, v in stored().items() if k not in ("language", "household")},
         "language": h["language"], "household": h["description"]}
    if not with_key:
        s["has_key"] = bool(s.pop("api_key", ""))
    return s


def save(b):
    old = settings(with_key=True)
    s = dict(old)
    if b.get("provider") in PROVIDERS:
        s["provider"] = b["provider"]
    for k in ("model", "base_url", "search_url"):
        if k in b:
            s[k] = str(b[k] or "").strip()[:300]
    if b.get("effort") in ("low", "medium", "high", "xhigh", "max"):
        s["effort"] = b["effort"]
    for k, lo, hi in (("run_budget", 0, 100), ("day_budget", 0, 500), ("price_in", 0, 1000), ("price_out", 0, 1000), ("max_turns", 5, 400)):
        if k in b:
            try:
                s[k] = max(lo, min(hi, float(b[k] or 0)))
            except (TypeError, ValueError):
                raise Error(f"{k}: number expected") from None
    s["max_turns"] = int(s["max_turns"])
    if "language" in b or "household" in b:  # older clients: these belong to the household
        household.save({k2: v for k2, v in (("language", b.get("language")), ("description", b.get("household"))) if v is not None})
    for k in ("language", "household"):
        s.pop(k, None)
    if b.get("api_key"):  # empty = keep the old key
        s["api_key"] = str(b["api_key"]).strip()[:400]
    if b.get("api_key_clear"):
        s.pop("api_key", None)
    for k in ("base_url", "search_url"):
        if s[k] and not re.fullmatch(r"https?://\S+", s[k]):
            raise Error(f"{k}: http(s) address expected")
    with store.con() as c:
        c.execute("INSERT OR REPLACE INTO integrations(name,data,updated) VALUES('ai',?,?)",
                  (secretbox.encrypt(json.dumps(s)), datetime.now().isoformat(timespec="seconds")))
    return settings()


def active():
    """Does the app run AI jobs itself?"""
    return settings()["provider"] in ("anthropic", "openai")


# ---------- runs and costs ----------

def runs(limit=20):
    with store.con() as c:
        rows = c.execute("SELECT id, job, started, ended, state, provider, model, turns, input_tokens, output_tokens,"
                         " cache_read, cache_write, searches, cost, summary FROM ai_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def run_log(rid):
    with store.con() as c:
        r = c.execute("SELECT * FROM ai_runs WHERE id=?", (rid,)).fetchone()
    if not r:
        raise Error("run not found")
    d = dict(r)
    d["log"] = json.loads(d["log"] or "[]")
    return d


def spent_today():
    with store.con() as c:
        r = c.execute("SELECT COALESCE(SUM(cost),0) s FROM ai_runs WHERE started >= ?", (datetime.now().strftime("%Y-%m-%d"),)).fetchone()
    return float(r["s"])


def _price(s, model):
    if s["provider"] == "anthropic":
        base = next((v for k, v in ANTHROPIC_MODELS.items() if model.startswith(k)), ANTHROPIC_MODELS["claude-opus-5-5"])
        return base[0], base[1], base[2], base[0] * 1.25
    return s["price_in"], s["price_out"], s["price_in"], s["price_in"]


class Run:
    """One AI run: counts tokens and money, keeps a short log, enforces the limits."""

    def __init__(self, job, s):
        self.s, self.job = s, job
        self.model = s["model"] or ("claude-opus-5-5" if s["provider"] == "anthropic" else "")
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "searches": 0}
        self.cost, self.turns, self.log = 0.0, 0, []
        self.day_before = spent_today()
        with store.con() as c:
            self.id = c.execute("INSERT INTO ai_runs(job, started, provider, model) VALUES(?,?,?,?)",
                                (job, datetime.now().isoformat(timespec="seconds"), s["provider"], self.model)).lastrowid

    def add_usage(self, u):
        for k in self.usage:
            self.usage[k] += int(u.get(k) or 0)
        pin, pout, pcr, pcw = _price(self.s, self.model)
        self.cost = (self.usage["input_tokens"] * pin + self.usage["output_tokens"] * pout + self.usage["cache_read"] * pcr
                     + self.usage["cache_write"] * pcw) / 1e6
        if self.s["provider"] == "anthropic":
            self.cost += self.usage["searches"] * WEB_SEARCH_USD
        self.turns += 1
        self._store()

    def check(self):
        if self.s["run_budget"] and self.cost >= self.s["run_budget"]:
            raise Budget(f"cost limit per run reached ({self.cost:.2f} of {self.s['run_budget']:.2f} USD)")
        if self.s["day_budget"] and self.day_before + self.cost >= self.s["day_budget"]:
            raise Budget(f"daily cost limit reached ({self.day_before + self.cost:.2f} of {self.s['day_budget']:.2f} USD)")
        if self.turns >= self.s["max_turns"]:
            raise Budget(f"turn limit reached ({self.turns})")

    def note(self, kind, text):
        self.log.append({"t": datetime.now().strftime("%H:%M:%S"), "kind": kind, "text": str(text)[:600]})
        del self.log[:-400]
        self._store()  # visible in Settings → AI while the run is going

    def _store(self, state=None, summary=None):
        with store.con() as c:
            c.execute("UPDATE ai_runs SET turns=?, input_tokens=?, output_tokens=?, cache_read=?, cache_write=?, searches=?, cost=?,"
                      " log=?, model=? WHERE id=?",
                      (self.turns, *self.usage.values(), round(self.cost, 4), json.dumps(self.log, ensure_ascii=False), self.model, self.id))
            if state:
                c.execute("UPDATE ai_runs SET state=?, summary=?, ended=? WHERE id=?",
                          (state, str(summary or "")[:1000], datetime.now().isoformat(timespec="seconds"), self.id))

    def finish(self, state, summary):
        self._store(state, summary)


# ---------- providers ----------

class AnthropicProvider:
    """Claude via the official SDK. Messages are kept in the API's own format."""
    server_tools = True

    def __init__(self, s, model):
        try:
            import anthropic  # only needed when this provider is used
        except ImportError:
            raise Error("The Python package 'anthropic' is missing (pip install anthropic)") from None
        if not s.get("api_key"):
            raise Error("No API key saved")
        self.sdk = anthropic
        self.client = anthropic.Anthropic(api_key=s["api_key"], max_retries=4, timeout=900)
        self.model, self.effort = model, s["effort"]

    def _create(self, system, messages, tools):
        kw = {"model": self.model, "max_tokens": 16000, "system": system, "messages": messages, "tools": tools,
              "cache_control": {"type": "ephemeral"}, "output_config": {"effort": self.effort}}
        try:
            if not self.model.startswith("claude-haiku"):
                # a refusal by a safety classifier is retried on the fallback model Anthropic recommends
                return self.client.beta.messages.create(**kw, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            return self.client.messages.create(**kw)
        except self.sdk.AuthenticationError:
            raise Error("API key was rejected") from None
        except self.sdk.BadRequestError as e:
            raise Error(f"Request rejected: {getattr(e, 'message', e)}") from None
        except self.sdk.RateLimitError:
            raise Error("Rate limit at Anthropic, try again later") from None
        except self.sdk.APIStatusError as e:
            raise Error(f"Anthropic API error {e.status_code}: {getattr(e, 'message', e)}") from None
        except self.sdk.APIConnectionError:
            raise Error("Anthropic API not reachable") from None

    def tool_defs(self, tools, web):
        out = [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]} for t in tools]
        if web:
            out += [{"type": "web_search_20260209", "name": "web_search", "max_uses": 40},
                    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 60}]
        return out

    def turn(self, system, messages, tools):
        """→ (text, [(id, name, input)], stop, usage). Appends the assistant turn to messages."""
        r = self._create(system, messages, tools)
        u = r.usage
        stu = getattr(u, "server_tool_use", None)
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                 "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
                 "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
                 "searches": (getattr(stu, "web_search_requests", 0) or 0) if stu else 0}
        messages.append({"role": "assistant", "content": r.content})
        text = "\n".join(b.text for b in r.content if b.type == "text").strip()
        calls = [(b.id, b.name, b.input) for b in r.content if b.type == "tool_use"]
        stop = r.stop_reason
        if stop == "refusal":
            d = getattr(r, "stop_details", None)
            text = f"refused ({getattr(d, 'category', None) or 'no category'})"
        return text, calls, stop, usage

    def results(self, messages, results):
        """c is text, or a list of MCP-style blocks (text, image) from tools such as read_video (since video import)."""
        def blocks(c):
            if isinstance(c, str):
                return c
            return [{"type": "text", "text": b["text"][:40000]} if b.get("type") == "text" else
                    {"type": "image", "source": {"type": "base64", "media_type": b["mimeType"], "data": b["data"]}} for b in c]
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": i, "content": blocks(c), **({"is_error": True} if err else {})} for i, c, err in results]})

    def say(self, messages, text, images=None):
        """images: [(mime, bytes)] go in front of the text as image blocks (recipe from a photo)."""
        if not images:
            return messages.append({"role": "user", "content": text})
        blocks = [{"type": "image", "source": {"type": "base64", "media_type": m, "data": base64.b64encode(d).decode()}} for m, d in images]
        messages.append({"role": "user", "content": blocks + [{"type": "text", "text": text}]})


class OpenAIProvider:
    """OpenAI-compatible /chat/completions with function calling (OpenAI, Ollama, LM Studio, vLLM …)."""
    server_tools = False

    def __init__(self, s, model):
        if not s.get("base_url"):
            raise Error("No base URL saved (e.g. http://ollama:11434/v1)")
        if not model:
            raise Error("No model saved")
        self.url = s["base_url"].rstrip("/") + "/chat/completions"
        self.key, self.model = s.get("api_key") or "", model
        self.tool_images, self.no_vision = [], False  # messages with images from tools (read_video); dropped if the model is blind

    def tool_defs(self, tools, web):
        return [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["schema"]}}
                for t in tools]

    def turn(self, system, messages, tools):
        h = {"Content-Type": "application/json", "User-Agent": UA}
        if self.key:
            h["Authorization"] = "Bearer " + self.key
        attempt = 0
        while True:
            body = {"model": self.model, "messages": [{"role": "system", "content": system}, *messages], "tools": tools,
                    "tool_choice": "auto", "max_tokens": 8000}  # without a limit some local servers generate endlessly
            req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=h, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=900) as r:
                    d = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                msg = e.read()[:300].decode("utf-8", "replace")
                vision = self._has_images(messages) and e.code in (400, 404, 415, 422, 500, 501) and re.search(
                    r"(?i)image|vision|multi-?modal|modalit|content.*(type|part)|unsupported content", msg)
                if vision and self.tool_images:  # stills from a tool: go on without them, the text is enough to work with
                    for m in self.tool_images:
                        m["content"] = "(The images of the tool answer above cannot be shown: this AI model cannot see images. Work with the text.)"
                    self.tool_images, self.no_vision = [], True
                    continue
                if vision:
                    raise Error(f"{NO_VISION} (HTTP {e.code} from the AI server: {msg}). Choose a model with vision in Settings → AI.") from None
                if e.code in (429, 500, 502, 503) and attempt < 2:
                    attempt += 1
                    time.sleep(5 * attempt)
                    continue
                raise Error(f"HTTP {e.code} from the AI server: {msg}") from None
            except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError) as e:
                if attempt < 2:
                    attempt += 1
                    time.sleep(5)
                    continue
                raise Error(f"AI server not reachable: {getattr(e, 'reason', e)}") from None
        ch = (d.get("choices") or [{}])[0]
        m = ch.get("message") or {}
        calls = []
        for i, tc in enumerate(m.get("tool_calls") or []):
            f = tc.get("function") or {}
            args = f.get("arguments")
            try:
                args = json.loads(args) if isinstance(args, str) else (args or {})
            except ValueError:
                args = {"_invalid_json": str(args)[:500]}
            calls.append((tc.get("id") or f"call_{i}", f.get("name"), args))
        msg = {"role": "assistant", "content": m.get("content") or ""}
        if m.get("tool_calls"):
            msg["tool_calls"] = [{"id": c[0], "type": "function",
                                  "function": {"name": c[1], "arguments": json.dumps(c[2], ensure_ascii=False)}} for c in calls]
        messages.append(msg)
        u = d.get("usage") or {}
        usage = {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}
        fr = ch.get("finish_reason")
        stop = "tool_use" if calls else ("max_tokens" if fr == "length" else "end_turn")
        return (m.get("content") or "").strip(), calls, stop, usage

    def results(self, messages, results):
        """c is text, or a list of MCP-style blocks: tool messages carry text only, so the images (stills of read_video)
        follow in one user message after the tool answers; a model without vision gets a note instead (see turn)."""
        images = []
        for i, c, err in results:
            if not isinstance(c, str):
                images += [b for b in c if b.get("type") == "image"]
                c = "\n".join(b["text"] for b in c if b.get("type") == "text")[:40000]
            messages.append({"role": "tool", "tool_call_id": i, "content": ("ERROR: " if err else "") + c})
        if images and self.no_vision:
            messages.append({"role": "user", "content": "(The images of the tool answer above cannot be shown: this AI model cannot see images. Work with the text.)"})
        elif images:
            m = {"role": "user", "content": [{"type": "text", "text": "The images of the tool answer above, in order:"}] + [
                {"type": "image_url", "image_url": {"url": f"data:{b['mimeType']};base64,{b['data']}"}} for b in images]}
            messages.append(m)
            self.tool_images.append(m)

    def say(self, messages, text, images=None):
        """images: [(mime, bytes)] as image_url parts with data URLs; needs a model with vision."""
        if not images:
            return messages.append({"role": "user", "content": text})
        parts = [{"type": "text", "text": text}] + [
            {"type": "image_url", "image_url": {"url": f"data:{m};base64,{base64.b64encode(d).decode()}"}} for m, d in images]
        messages.append({"role": "user", "content": parts})

    @staticmethod
    def _has_images(messages):
        return any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"]) for m in messages)


def sdk_version():
    try:
        import anthropic
        return getattr(anthropic, "__version__", "?")
    except ImportError:
        return None


def provider(s, model):
    if s["provider"] == "anthropic":
        return AnthropicProvider(s, model)
    if s["provider"] == "openai":
        return OpenAIProvider(s, model)
    raise Error("AI runs inside the app are not switched on (Settings → AI)")


# ---------- agent loop ----------

def agent(job, system, task, tools, handlers, web=True, s=None, images=None):
    """Runs the agent until it stops calling tools.
    tools: [{name, description, schema}], handlers: {name: fn(input) -> str}. images: [(mime, bytes)] sent with the task
. → (state, summary, run id)."""
    s = s or settings(with_key=True)
    run = Run(job, s)
    try:
        p = provider(s, run.model)
        defs = p.tool_defs(tools, web)
        messages = []
        p.say(messages, task, images)
        run.note("start", f"{s['provider']} {run.model}" + (f", {len(images)} image(s)" if images else ""))
        nudges = 0
        while True:
            run.check()
            run.note("wait", f"turn {run.turns + 1}: waiting for the model")  # shows that a slow model is still working
            text, calls, stop, usage = p.turn(system, messages, defs)
            run.add_usage(usage)
            if text:
                run.note("text", text)
            if stop == "refusal":
                raise Error(text)
            if stop == "pause_turn":  # a long server tool turn (web search); send it back so the server continues
                continue
            if not calls:
                if stop == "max_tokens" and nudges < 3:
                    nudges += 1
                    p.say(messages, "Your answer was cut off. Continue, and keep each step shorter.")
                    continue
                run.finish("done", text)
                return "done", text, run.id
            results = []
            for cid, name, inp in calls:
                if stop == "max_tokens":  # the input of this call may be cut off
                    results.append((cid, "Your message was cut off by the output limit, so this call was not run. Send it again, shorter.", True))
                    continue
                fn = handlers.get(name)
                run.note("tool", f"{name} {json.dumps(inp, ensure_ascii=False)[:300]}")
                if not fn:
                    results.append((cid, f"unknown tool {name}", True))
                    continue
                if "_invalid_json" in (inp or {}):
                    results.append((cid, "The arguments were not valid JSON. Send the call again.", True))
                    continue
                try:
                    out, err = fn(inp or {}), False
                except Exception as e:  # noqa: BLE001  the model gets the error and can react
                    out, err = f"{type(e).__name__}: {e}", True
                if isinstance(out, dict) and isinstance(out.get("_content"), list):  # text and images (read_video, read_photos)
                    blocks = out["_content"]
                    n = sum(1 for b in blocks if b.get("type") == "image")
                    run.note("result", next((b["text"] for b in blocks if b.get("type") == "text"), "")[:300] + (f" (+{n} image(s))" if n else ""))
                    results.append((cid, blocks, err))
                    continue
                out = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                run.note("result" if not err else "error", out[:300])
                results.append((cid, out[:40000], err))
            p.results(messages, results)
    except Budget as e:
        run.note("stop", str(e))
        run.finish("failed", str(e))
        return "failed", str(e), run.id
    except Exception as e:  # noqa: BLE001
        run.note("error", str(e))
        run.finish("failed", str(e))
        return "failed", str(e), run.id


def test(s=None):
    """Small check of the saved settings: one turn with a tool. → {ok, text, tool, cost}."""
    s = s or settings(with_key=True)
    called = []
    tools = [{"name": "ping", "description": "Answers pong. Call it once.", "schema": {
        "type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"]}}]
    state, summary, rid = agent("test", "You are testing a tool connection. Be brief.",
                                "Call the ping tool once with word 'hello', then reply with the single word OK.", tools,
                                {"ping": lambda i: called.append(i) or "pong"}, web=False, s={**s, "max_turns": 4})
    r = run_log(rid)
    return {"ok": state == "done" and bool(called), "tool": bool(called), "text": summary[:300], "cost": r["cost"],
            "model": r["model"], "run": rid}
