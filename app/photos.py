"""Recipe from a photo or screenshot: the images of the AI job „photo“ (meta key fotoimport).

The app sends one to six images (cookbook page, screenshot, handwritten note, photo of the dish). They are kept in
<data>/fotoimport/ of the household only while the job waits or runs and are deleted when it ends (done or failed),
when a new import replaces them, and after two days at the latest. They are not part of backups.
Inside the app they go to the configured AI provider only (Anthropic image blocks, OpenAI-compatible data URLs);
an outside worker fetches them with its API token (GET /api/fotoimport/bild/<nr> or the MCP tool read_photos).
"""
import base64
import os
import re
import secrets
import time

import store
from store import Error

MAX_IMAGES = 6
MAX_BYTES = 5 * 1024 * 1024  # per image: the limit of the Anthropic API; the app shrinks photos before sending
MAX_TOTAL = 20 * 1024 * 1024
MAX_AGE = 2 * 24 * 3600
TAG = {"de": "Aus Foto", "en": "From photo"}  # tag of recipes made from photos, in the household language


def _dir():
    return os.path.join(store.data_dir(), "fotoimport")


def mime(data):
    """Image type from the first bytes (the browser's Content-Type is not trusted). None = not an image we take."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


def decode(items):
    """[{"daten": base64 or data URL}] from the app → [bytes], checked for type and size."""
    if not isinstance(items, list) or not items:
        raise Error("Bitte mindestens ein Bild auswählen")
    if len(items) > MAX_IMAGES:
        raise Error(f"Höchstens {MAX_IMAGES} Bilder")
    out, total = [], 0
    for i, x in enumerate(items, 1):
        raw = str((x or {}).get("daten") if isinstance(x, dict) else x or "")
        raw = re.sub(r"^data:[^,]*,", "", raw.strip())
        try:
            data = base64.b64decode(raw, validate=False)
        except (ValueError, TypeError):
            raise Error(f"Bild {i}: nicht lesbar") from None
        if not mime(data):
            raise Error(f"Bild {i}: kein Bild (JPEG, PNG, WebP oder GIF)")
        if len(data) > MAX_BYTES:
            raise Error(f"Bild {i}: zu groß (höchstens {MAX_BYTES // (1024 * 1024)} MB)")
        total += len(data)
        out.append(data)
    if total > MAX_TOTAL:
        raise Error(f"Bilder zusammen zu groß (höchstens {MAX_TOTAL // (1024 * 1024)} MB)")
    return out


def save(images):
    """Store the images of a new import (old ones go away) → (key, [{nr, typ, groesse}])."""
    clear()
    os.makedirs(_dir(), exist_ok=True)
    key, meta = secrets.token_hex(8), []
    for nr, data in enumerate(images, 1):
        t = mime(data)
        with open(os.path.join(_dir(), f"{key}-{nr}.{EXT[t]}"), "wb") as f:
            f.write(data)
        meta.append({"nr": nr, "typ": t, "groesse": len(data)})
    return key, meta


def file(state, nr):
    """(path, mime) of image nr of the import in state, or None."""
    key = str((state or {}).get("bilder_key") or "")
    if not re.fullmatch(r"[0-9a-f]{16}", key):
        return None
    for b in state.get("bilder") or []:
        if b.get("nr") == nr and b.get("typ") in EXT:
            p = os.path.join(_dir(), f"{key}-{nr}.{EXT[b['typ']]}")
            return (p, b["typ"]) if os.path.isfile(p) else None
    return None


def load(state):
    """All images of the import → [(mime, bytes)] (missing files are skipped)."""
    out = []
    for b in (state or {}).get("bilder") or []:
        f = file(state, b.get("nr"))
        if f:
            with open(f[0], "rb") as fh:
                out.append((f[1], fh.read()))
    return out


def clear(keep=None):
    """Delete the stored images (keep: key of the import that still waits or runs); old leftovers always go."""
    d = _dir()
    if not os.path.isdir(d):
        return
    for n in os.listdir(d):
        p = os.path.join(d, n)
        old = time.time() - os.path.getmtime(p) > MAX_AGE
        if old or not keep or not n.startswith(keep + "-"):
            try:
                os.remove(p)
            except OSError:
                pass


def public(state):
    """The job state for the app and workers, without the internal file key."""
    return {k: v for k, v in (state or {}).items() if k != "bilder_key"}


def mark_recipe(slug, count, lang):
    """A recipe made from photos gets the tag „Aus Foto“/„From photo“ (household language) and a note if the AI wrote none."""
    try:
        r = store.recipe(slug)
    except Error:
        return False
    tag = TAG.get(lang, TAG["en"])
    tags = list(r.get("tags") or [])
    notes = list(r.get("notizen") or [])
    changed = {}
    if not any(t.lower() in (x.lower() for x in TAG.values()) for t in tags):
        changed["tags"] = tags + [tag]
    if not any((n.get("titel") or "").lower() in (x.lower() for x in TAG.values()) for n in notes):
        text = (f"Aus {count} Foto(s) erstellt, Nährwerte geschätzt." if lang == "de"
                else f"Created from {count} photo(s), nutrition estimated.")
        changed["notizen"] = notes + [{"titel": tag, "text": text}]
    if changed:
        store.save(changed, slug=slug)
    return True
