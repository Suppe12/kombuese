"""Reading a recipe page (replaces Mealie's scraper): schema.org/Recipe from JSON-LD.
Almost every recipe site (Chefkoch, blogs with WP Recipe Maker, Tasty, ...) provides it. Standard library only.
Used by GET /api/scrape (karte.py, outside workers) and the AI runs in jobs.py."""
import html
import json
import re

import netsafe

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


class Error(Exception):
    pass


def _languages():  # prefer recipe pages in the household language
    import household
    return "en-US,en;q=0.9,de;q=0.5" if household.language() == "en" else "de-DE,de;q=0.9,en;q=0.5"


def _fetch(url):
    if not re.match(r"https?://", url or ""):
        raise Error("Kein Link")
    try:  # without access to the local network (netsafe.py)
        raw, _, cs, _ = netsafe.open_url(url, {"User-Agent": UA, "Accept-Language": _languages(),
                                               "Accept": "text/html,application/xhtml+xml"})
        cs = cs or "utf-8"
    except Exception as e:  # noqa: BLE001
        raise Error(f"Seite nicht erreichbar: {e}") from None
    return raw.decode(cs, "replace")


def _types(o):
    t = o.get("@type")
    return t if isinstance(t, list) else [t]


def _recipes(o):
    if isinstance(o, list):
        for x in o:
            yield from _recipes(x)
    elif isinstance(o, dict):
        if "Recipe" in _types(o):
            yield o
        for k in ("@graph", "mainEntity", "itemListElement"):
            if k in o:
                yield from _recipes(o[k])


def _text(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", str(s or "")))
    return re.sub(r"\s+", " ", s).strip()


def _duration(s):
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?", str(s or "").strip())
    if not m or not (m.group(1) or m.group(2)):
        return _text(s) if s and not str(s).startswith("P") else ""
    h, mi = int(m.group(1) or 0), int(m.group(2) or 0)
    h, mi = h + mi // 60, mi % 60
    return " ".join(x for x in (f"{h} h" if h else "", f"{mi} min" if mi else "") if x)


def _steps(x):
    out = []
    if isinstance(x, str):
        return [{"titel": "", "text": t} for t in (_text(p) for p in re.split(r"\n+|<br\s*/?>|</p>", x)) if t]
    for s in x or []:
        if isinstance(s, str):
            out += _steps(s)
        elif isinstance(s, dict) and "HowToSection" in _types(s):
            sub = _steps(s.get("itemListElement"))
            if sub:
                sub[0]["titel"] = _text(s.get("name"))
            out += sub
        elif isinstance(s, dict):
            t = _text(s.get("text") or s.get("name"))
            if t:
                out.append({"titel": "", "text": t})
    return out


def _image(b):
    if isinstance(b, list):
        b = b[-1] if b and isinstance(b[-1], (str, dict)) else (b[0] if b else "")
    if isinstance(b, dict):
        b = b.get("url") or b.get("contentUrl") or ""
    return str(b or "")


def _number(v):
    m = re.search(r"\d+(?:[.,]\d+)?", str(v or ""))
    return float(m.group(0).replace(",", ".")) if m else None


def raw_recipe(url):
    """The page's schema.org/Recipe object unchanged (like Mealie's test-scrape-url used to be, for karte.py)."""
    page = _fetch(url)
    for block in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", page, flags=re.S | re.I):
        try:
            for r in _recipes(json.loads(block.strip().strip(";"))):
                return r
        except ValueError:
            continue
    raise Error("Kein Rezept auf der Seite gefunden (kein schema.org/Recipe)")


def read_recipe(url):
    """A page's recipe in Kombüse's shape (store.py). The ingredients are still text lines here (zutaten_roh);
        the AI run structures them when importing."""
    page = _fetch(url)
    recipes = []
    for block in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", page, flags=re.S | re.I):
        try:
            recipes += list(_recipes(json.loads(block.strip().strip(";"))))
        except ValueError:
            continue
    if not recipes:
        raise Error("Kein Rezept auf der Seite gefunden (kein schema.org/Recipe)")
    r = recipes[0]
    n = r.get("nutrition") or {}
    servings_text = r.get("recipeYield")
    servings_text = servings_text[0] if isinstance(servings_text, list) and servings_text else servings_text
    cat = r.get("recipeCategory")
    kw = r.get("keywords")
    return {
        "name": _text(r.get("name")), "beschreibung": _text(r.get("description"))[:1500], "quelle": url,
        "bild": _image(r.get("image")), "ertrag": _text(servings_text), "portionen": _number(servings_text),
        "zeiten": {"gesamt": _duration(r.get("totalTime")), "vorbereitung": _duration(r.get("prepTime")), "kochen": _duration(r.get("cookTime"))},
        "zutaten_roh": [_text(z) for z in r.get("recipeIngredient") or r.get("ingredients") or [] if _text(z)],
        "schritte": _steps(r.get("recipeInstructions")),
        "naehrwerte_quelle": {k: _text(v) for k, v in n.items() if not k.startswith("@")},
        "kcal_pro_portion": _number(n.get("calories")) if n.get("servingSize") is None or "100" not in str(n.get("servingSize")) else None,
        "kategorien_quelle": [_text(k) for k in (cat if isinstance(cat, list) else [cat]) if k],
        "stichworte_quelle": [t.strip() for t in (kw if isinstance(kw, list) else str(kw or "").split(",")) if t and str(t).strip()][:20],
    }


def download_image(url):
    try:
        return netsafe.open_url(url, {"User-Agent": UA, "Accept": "image/*"}, timeout=30, max_bytes=15 * 1024 * 1024)[0]
    except Exception as e:  # noqa: BLE001
        raise Error(f"Bild nicht ladbar: {e}") from None
