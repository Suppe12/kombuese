"""MCP server: the tools and prompts of the AI jobs for any MCP client (Claude Code, Claude Desktop …),
so an outside worker and the runs inside the app share one source.

Endpoint /mcp, Streamable HTTP transport with plain JSON answers: POST only, no SSE stream, no requests from the server
to the client. Sign-in: Bearer API token with scope api (Settings → Accounts). /mcp?web=1 also offers web_search and
web_fetch (most MCP clients bring their own).

Protocol versions: the 2025 era with the initialize handshake (this server keeps no state, so it hands out no
Mcp-Session-Id) and 2026-07-28 without a handshake (every request carries its version in _meta, server/discover tells
what the server offers).

Tools: jobs.tools_for("mcp", …), the same definitions and handlers as the runs inside the app; kombuese_api allows the
union of all job allowlists and calls go through jobs.call_api. Prompts: one per job, rendered by jobs.prompts with the
household settings (language, description, people and their preferences). Everything works in the household of
the API token.
"""
import json
import os
import re
import urllib.parse
from datetime import datetime, timedelta

import ai
import jobs

LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")  # initialize, newest first
MODERN = ("2026-07-28",)  # stateless, version per request
META = "io.modelcontextprotocol/"


def _version():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "dev"
    except OSError:
        return "dev"


SERVER = {"name": "kombuese", "title": "Kombüse", "version": _version()}
INSTRUCTIONS = ("Kombüse is a self-hosted kitchen app (recipes, swipe cards, week plan, meal prep, shopping list, pantry). "
                "The prompts refill, import, link, week, swap, prep, pantry, photo and reheat are the app's AI jobs with the household's settings. "
                "kombuese_api calls the app's JSON API; the allowed paths are listed in its description. "
                "read_photos shows the images of a waiting photo import. "
                "read_video reads a cooking video link (description, transcript, stills; the app runs yt-dlp and Whisper).")
WISH = "wish of the household (optional)"
PROMPTS = {  # job → (description, [(argument, description, required)])
    "refill": ("Find new dishes for the swipe stack and update the taste profile.",
               [("wish", WISH, False), ("meal", "fruehstueck, haupt or gemischt (default)", False)]),
    "import": ("Turn the liked swipe cards into full recipes.", []),
    "link": ("Create a recipe from a link.", [("url", "link to the recipe", True), ("hint", "hint from the household (optional)", False)]),
    "week": ("Propose a week plan; the household checks and accepts it in the app.",
             [("start", "Monday of the week, YYYY-MM-DD (default: next Monday)", False), ("wish", WISH, False),
              ("people", "number of people for this plan, guests included (default: the household's)", False)]),
    "swap": ("Swap one dish in the week plan proposal.",
             [("slug", "slug of the recipe to replace", True), ("name", "name of that recipe", False), ("wish", WISH, False)]),
    "prep": ("Build the meal prep sequence for a prep day.", [("datum", "prep day, YYYY-MM-DD", True), ("wish", WISH, False)]),
    "pantry": ("Suggest 3-5 dishes from what is in the cupboard and freezer (own recipes first, otherwise new swipe cards).",
               [("wish", WISH, False)]),
    "photo": ("Create a recipe from the photos or screenshots of the waiting photo import (tool read_photos).",
              [("hint", "hint from the household (optional)", False)]),
}
PROMPTS["reheat"] = ("Add reheating tips (microwave watt and minutes) to the recipes that have none.", [])
PARAM = {"wish": "wunsch", "hint": "hinweis", "people": "personen"}  # MCP argument → job parameter (as in the app's state)
CHECK = {"start": r"\d{4}-\d{2}-\d{2}", "datum": r"\d{4}-\d{2}-\d{2}", "slug": jobs.S, "url": r"https?://\S{4,800}",
         "meal": r"fruehstueck|haupt|gemischt", "people": r"[1-9]|[12]d|30"}


class RpcError(Exception):
    def __init__(self, code, message, http=200, data=None):
        super().__init__(message)
        self.code, self.http, self.data = code, http, data


def _settings(web):
    s = ai.settings()
    # the prompt's tool sentence: with web=1 our own web tools, otherwise the client's (like Anthropic's server tools)
    return {**s, "provider": "mcp" if web else "anthropic"}


def _tools(web):
    return jobs.tools_for("mcp", ai.settings(), web=web)


def render(job, args, web=False):
    """MCP prompt → (title, text). args: the prompt arguments (English names; the app's German names work too)."""
    if job not in PROMPTS:
        raise RpcError(-32602, f"Unknown prompt: {job}")
    params = {}
    for name, _, required in PROMPTS[job][1]:
        v = args.get(name)
        if v is None:
            v = args.get(PARAM.get(name, name))
        v = str(v or "").strip()[:500]
        if required and not v:
            raise RpcError(-32602, f"Missing required argument: {name}")
        if v and name in CHECK and not re.fullmatch(CHECK[name], v):
            raise RpcError(-32602, f"Invalid argument {name}: {v[:80]}")
        params[PARAM.get(name, name)] = v
    if job == "photo":  # the images come through the tool read_photos; their number from the job state
        params["bilder_via"] = "tool"
        params["bilder"] = (jobs._meta("fotoimport", {}) or {}).get("bilder") or []
    if job == "week" and not params.get("start"):
        today = datetime.now().date()
        params["start"] = str(today + timedelta(days=7 - today.weekday()))
    system, task = jobs.prompts(job, params, _settings(web))
    return jobs.TASKS[job][0], system + "\n\n" + task


def _call(name, params, web):
    if name == "initialize":
        v = params.get("protocolVersion")
        return {"protocolVersion": v if v in LEGACY else LEGACY[0], "capabilities": {"tools": {}, "prompts": {}},
                "serverInfo": SERVER, "instructions": INSTRUCTIONS}
    if name == "server/discover":
        return {"supportedVersions": list(MODERN + LEGACY), "capabilities": {"tools": {}, "prompts": {}}, "instructions": INSTRUCTIONS}
    if name == "ping":
        return {}
    if name == "tools/list":
        defs, _ = _tools(web)
        return {"tools": [{"name": d["name"], "description": d["description"], "inputSchema": d["schema"]} for d in defs]}
    if name == "tools/call":
        defs, handlers = _tools(web)
        fn = handlers.get(params.get("name"))
        if not fn:
            raise RpcError(-32602, f"Unknown tool: {params.get('name')}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise RpcError(-32602, "arguments must be an object")
        try:
            out = fn(args)
            if isinstance(out, dict) and "_content" in out:  # e.g. read_photos: text and image blocks as they are
                return {"content": out["_content"], "isError": False}
            text, err = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False), False
        except Exception as e:  # noqa: BLE001  tool errors go back to the model, like in the app's runs
            text, err = f"{type(e).__name__}: {e}", True
        return {"content": [{"type": "text", "text": text}], "isError": err}
    if name == "prompts/list":
        return {"prompts": [{"name": job, "title": jobs.TASKS[job][0], "description": d,
                             "arguments": [{"name": a, "description": t, "required": r} for a, t, r in args]}
                            for job, (d, args) in PROMPTS.items()]}
    if name == "prompts/get":
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise RpcError(-32602, "arguments must be an object")
        title, text = render(params.get("name"), args, web)
        return {"description": title, "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
    raise RpcError(-32601, f"Method not found: {name}", 404)


def _hosts(h):
    return {x.split(",")[0].strip().lower() for x in (h.headers.get("Host"), h.headers.get("X-Forwarded-Host")) if x}


def handle(h, method):
    """The /mcp route. h = the server's request handler (send, headers, rfile, path, user)."""
    if method != "POST":
        return h.send(405, {"error": "MCP: only POST, no SSE stream"}, extra={"Allow": "POST"})
    u = h.user
    if not u or u.get("via") != "token" or u.get("scope") != "api":
        return h.send(401, {"error": "MCP needs a Bearer API token with scope api"}, extra={"WWW-Authenticate": 'Bearer realm="kombuese"'})
    origin = h.headers.get("Origin")
    if origin and origin != "null" and urllib.parse.urlsplit(origin).netloc.lower() not in _hosts(h):
        return h.send(403, {"error": "MCP: foreign Origin"})
    web = urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query).get("web") == ["1"]

    def reply(rid, result=None, err=None, modern=False):
        if err:
            e = {"code": err.code, "message": str(err)}
            if err.data is not None:
                e["data"] = err.data
            return h.send(err.http if modern or err.http == 400 else 200, {"jsonrpc": "2.0", "id": rid, "error": e})
        if modern:
            result = {"resultType": "complete", **result, "_meta": {META + "serverInfo": {k: SERVER[k] for k in ("name", "version")}}}
        return h.send(200, {"jsonrpc": "2.0", "id": rid, "result": result})

    try:
        n = int(h.headers.get("Content-Length") or 0)
        if n > 2 * 1024 * 1024:
            raise ValueError("too large")
        msg = json.loads(h.rfile.read(n) or b"null")
    except ValueError:
        return reply(None, err=RpcError(-32700, "Parse error", 400))
    if isinstance(msg, list):
        return reply(None, err=RpcError(-32600, "JSON-RPC batches are not supported", 400))
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return reply(None, err=RpcError(-32600, "Invalid Request", 400))
    if "method" not in msg or "id" not in msg:  # a notification (e.g. notifications/initialized) or a response to us
        return h.send(202, b"", "text/plain")
    rid, name, params = msg["id"], str(msg["method"]), msg.get("params") or {}
    if not isinstance(params, dict):
        return reply(rid, err=RpcError(-32602, "params must be an object", 400))
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    header = h.headers.get("MCP-Protocol-Version")
    modern = META + "protocolVersion" in meta
    if modern:
        v = meta[META + "protocolVersion"]
        if v not in MODERN:
            return reply(rid, err=RpcError(-32022, f"Unsupported protocol version: {v}", 400,
                                           {"supported": list(MODERN), "requested": v}), modern=True)
        want = {"MCP-Protocol-Version": v, "Mcp-Method": name}
        if name in ("tools/call", "prompts/get"):
            want["Mcp-Name"] = str(params.get("name") or "")
        for k, val in want.items():
            if h.headers.get(k) != val:
                return reply(rid, err=RpcError(-32020, f"Header {k} missing or not matching the body", 400), modern=True)
    elif name != "initialize" and header and header not in LEGACY:
        return reply(rid, err=RpcError(-32600, f"Unsupported MCP-Protocol-Version: {header}", 400, {"supported": list(LEGACY)}))
    try:
        return reply(rid, _call(name, params, web), modern=modern)
    except RpcError as e:
        return reply(rid, err=e, modern=modern)
    except Exception as e:  # noqa: BLE001
        return reply(rid, err=RpcError(-32603, f"Internal error: {e}", 500), modern=modern)
