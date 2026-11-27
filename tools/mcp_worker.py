#!/usr/bin/env python3
"""Example worker for the AI provider "External": does the app's AI jobs with Claude Code and the app's MCP server.

Run it every 5 minutes (cron) on a machine with Claude Code (`claude`) signed in:

  */5 * * * * KOMBUESE_URL=http://kitchen.lan:9926 python3 /path/to/mcp_worker.py >> ~/kombuese-worker.log 2>&1

What it does, once per call:
  1. GET /api/work. If the app runs the AI itself (provider Anthropic or OpenAI-compatible) there is nothing to do.
  2. Picks the next waiting job in the app's order: refill, week plan (or swap), meal prep, recipe link, recipe from a
     photo, dishes from the pantry, reheating tips, and the import of liked cards (likes pending and the last swipe at least 10 min ago,
     at most every 30 min). The photo job reads its images with the MCP tool read_photos.
  3. Claims it by setting its state to running, fetches the job prompt from the MCP server (prompts/get, rendered with
     the household language and description), and runs `claude -p` with an MCP config pointing at /mcp.
  4. Afterwards marks a job that is still running as failed (refill: done when Claude ended without error).

Environment:
  KOMBUESE_URL     address of the app (default http://localhost:9926)
  KOMBUESE_TOKEN   API token with scope api (Settings → Accounts), or the file ~/.config/kombuese/token
  KOMBUESE_CLAUDE  the Claude Code command (default claude)

Options: --dry-run shows the job and its prompt without claiming or running it; --web lets Claude use the app's
web_search/web_fetch instead of its own WebSearch/WebFetch; everything after -- goes to claude, e.g.
  python3 mcp_worker.py -- --model claude-sonnet-5-5 --permission-mode auto
Standard library only.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime

URL = (os.environ.get("KOMBUESE_URL") or "http://localhost:9926").rstrip("/")
CLAUDE = os.environ.get("KOMBUESE_CLAUDE") or "claude"
PROTOCOL = "2025-11-25"
STAMP = os.path.join(os.path.expanduser("~"), ".cache", "kombuese-worker", "last-import")
# job → (key in /api/work, state route, timeout in s, arguments of the MCP prompt from the job state)
JOBS = {
    "refill": ("refill", "/api/nachschub/state", 2400, {"wish": "wunsch", "meal": "meal"}),
    "week": ("wochenplan", "/api/wochenplan/state", 3000, {"start": "start", "wish": "wunsch"}),
    "swap": ("wochenplan", "/api/wochenplan/state", 2400, {"slug": "slug", "name": "name", "wish": "wunsch"}),
    "prep": ("mealprep", "/api/mealprep/state", 3000, {"datum": "datum", "wish": "wunsch"}),
    "link": ("linkimport", "/api/linkimport/state", 3000, {"url": "url", "hint": "hinweis"}),
    "photo": ("fotoimport", "/api/fotoimport/state", 1800, {"hint": "hinweis"}),  # images via the MCP tool read_photos
    "pantry": ("kochideen", "/api/kochideen/state", 2400, {"wish": "wunsch"}),
    "reheat": ("aufwaermen", "/api/aufwaermen/state", 2400, {}),
    "import": (None, None, 3000, {}),
}
ORDER = (("refill", "refill"), ("week", "wochenplan"), ("prep", "mealprep"), ("link", "linkimport"),
         ("photo", "fotoimport"), ("pantry", "kochideen"), ("reheat", "aufwaermen"))


def log(msg):
    print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def token():
    t = os.environ.get("KOMBUESE_TOKEN")
    if not t:
        try:
            with open(os.path.expanduser("~/.config/kombuese/token"), encoding="utf-8") as f:
                t = f.read().strip()
        except OSError:
            t = ""
    if not t:
        sys.exit("No API token: set KOMBUESE_TOKEN or write it to ~/.config/kombuese/token")
    return t


def http(method, path, body=None, headers=None, tok=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method, headers={
        "Authorization": "Bearer " + tok, "Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else None


def mcp_prompt(job, args, tok, web):
    """The job prompt from the MCP server: initialize, notifications/initialized, prompts/get."""
    path = "/mcp" + ("?web=1" if web else "")
    hdr = {"Accept": "application/json, text/event-stream"}
    init = http("POST", path, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "kombuese-mcp-worker", "version": "1"}}}, hdr, tok)
    hdr["MCP-Protocol-Version"] = init["result"]["protocolVersion"]
    http("POST", path, {"jsonrpc": "2.0", "method": "notifications/initialized"}, hdr, tok)
    r = http("POST", path, {"jsonrpc": "2.0", "id": 2, "method": "prompts/get", "params": {"name": job, "arguments": args}}, hdr, tok)
    if "error" in r:
        raise RuntimeError(r["error"]["message"])
    return "\n\n".join(m["content"]["text"] for m in r["result"]["messages"] if m["content"].get("type") == "text")


def pick(w):
    """Next waiting job → (job, state) or (None, None). Same order as the app's own scheduler (jobs._claim)."""
    for job, key in ORDER:
        st = w.get(key) or {}
        if st.get("state") == "requested":
            return ("swap" if job == "week" and st.get("modus") == "tauschen" else job), st
    age = w.get("last_swipe_age_s")
    try:
        last = os.path.getmtime(STAMP)
    except OSError:
        last = 0
    if w.get("likes_pending") and age is not None and age >= 600 and time.time() - last > 1800:
        return "import", {"pending": w["likes_pending"]}
    return None, None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="show the job and its prompt, claim and run nothing")
    ap.add_argument("--web", action="store_true", help="offer the app's web_search/web_fetch over MCP")
    ap.add_argument("claude_args", nargs="*", help="after --: extra arguments for claude")
    a = ap.parse_args()
    tok = token()
    try:  # one worker at a time (cron every 5 min, runs take longer)
        import fcntl
        lock = open(os.path.join(tempfile.gettempdir(), "kombuese-worker.lock"), "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        pass  # Windows: no lock
    except OSError:
        return

    w = http("GET", "/api/work", tok=tok)
    if w.get("ai") == "app":
        return  # the app runs the AI jobs itself
    job, st = pick(w)
    if not job:
        return
    key, route, timeout, argmap = JOBS[job]
    args = {m: str(st[k]) for m, k in argmap.items() if st.get(k)}
    if a.dry_run:
        log(f"would run {job} with {json.dumps(args, ensure_ascii=False)}")
        print(mcp_prompt(job, args, tok, a.web))
        return
    if not shutil.which(CLAUDE):
        sys.exit(f"Claude Code not found ({CLAUDE}); set KOMBUESE_CLAUDE")

    if route:
        http("POST", route, {"state": "running"}, tok=tok)
    else:
        os.makedirs(os.path.dirname(STAMP), exist_ok=True)
        open(STAMP, "w").close()
    log(f"{job} starts {json.dumps(args, ensure_ascii=False)}")
    rc, note = 1, ""
    fd, cfg = tempfile.mkstemp(prefix="kombuese-mcp-", suffix=".json")  # holds the token: only for us, deleted afterwards
    try:
        prompt = mcp_prompt(job, args, tok, a.web)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"kombuese": {"type": "http", "url": URL + "/mcp" + ("?web=1" if a.web else ""),
                                                   "headers": {"Authorization": "Bearer " + tok}}}}, f)
        tools = "mcp__kombuese" + ("" if a.web else ",WebSearch,WebFetch")
        cmd = [CLAUDE, "-p", prompt, "--mcp-config", cfg, "--strict-mcp-config", "--allowedTools", tools, *a.claude_args]
        try:
            rc = subprocess.run(cmd, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            rc, note = 124, f"timeout after {timeout // 60} min"
    except (OSError, RuntimeError, ValueError, KeyError, urllib.error.URLError) as e:
        note = f"worker: {e}"[:300]
    finally:
        try:
            os.unlink(cfg)
        except OSError:
            pass
    if route:  # the run did not set its state itself
        now = (http("GET", "/api/work", tok=tok) or {}).get(key) or {}
        if now.get("state") == "running":
            state = "done" if job == "refill" and rc == 0 else "failed"
            http("POST", route, {"state": state, "note": note or f"no result (rc={rc})"}, tok=tok)
    log(f"{job} finished (rc={rc}{', ' + note if note else ''})")


if __name__ == "__main__":
    main()
