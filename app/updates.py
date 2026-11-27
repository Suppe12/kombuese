"""Update notice: asks a release feed at most every 12 hours whether a newer version exists, so the
version sheet in the app can show it.

KOMBUESE_UPDATE_URL  JSON of the latest release in GitHub's format ({"tag_name": "v2.7", "html_url": "…"}), e.g.
                     https://api.github.com/repos/<owner>/<repo>/releases/latest. Empty or "off" switches it off.
The check runs in the background; a request never waits for it. Only the version number is sent out (User-Agent).
"""
import json
import re
import threading
import time

import config
import netsafe

_state = {"checked": 0.0, "latest": None, "url": ""}
_lock = threading.Lock()


def _vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v or ""))[:3])


def _fetch(url, current):
    try:
        raw = netsafe.open_url(url, {"User-Agent": f"Kombuese/{current} (update check)", "Accept": "application/json"},
                               timeout=15, max_bytes=1024 * 1024)[0]
        d = json.loads(raw)
        if isinstance(d, list):
            d = d[0] if d else {}
        tag = d.get("tag_name") or d.get("name")
        if tag and not d.get("prerelease") and not d.get("draft"):
            _state.update(latest=str(tag).lstrip("vV"), url=str(d.get("html_url") or ""))
    except Exception as e:  # noqa: BLE001  no notice is fine
        print(f"Update check: {e}", flush=True)


def check(current):
    """→ {"latest", "url"} when a newer version is known, else None. Starts a background check when due."""
    url = config.UPDATE_URL
    if not url or url.lower() == "off":
        return None
    with _lock:
        if time.time() - _state["checked"] > 12 * 3600:
            _state["checked"] = time.time()
            threading.Thread(target=_fetch, args=(url, current), daemon=True, name="update-check").start()
    if _state["latest"] and _vtuple(_state["latest"]) > _vtuple(current):
        return {"latest": _state["latest"], "url": _state["url"]}
    return None
