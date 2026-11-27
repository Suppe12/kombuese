#!/usr/bin/env python3
"""Creates a swipe card ("Food Tinder") from a recipe link (for AI runs that add cards).

  python3 karte.py <url> --reason "why it fits" --tags "Vegetarisch,Mealprep" --ingredients "Haehnchen,Paprika,Reis" [--title "Short name"]

The German option names of earlier versions still work: --grund (= --reason), --titel (= --title),
--mahlzeit (= --meal), --zutaten (= --ingredients). --meal also takes main/breakfast for haupt/fruehstueck.

Checks the link with Kombüse's recipe reader (/api/scrape, schema.org/Recipe, saves nothing). A card is only created
when the recipe can be read, so a like can be imported cleanly later.
Output: one line starting with OK, FEHLER (error) or DOPPELT (already known). These three words stay German,
AI runs and scripts look for them.
"""
import argparse
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request

FT = os.environ.get("KOMBUESE_API") or os.environ.get("FT_API") or "http://localhost:9926/api"
UA = "Mozilla/5.0 (Kombuese; self-hosted kitchen app)"
MEALS = {"haupt": "haupt", "main": "haupt", "fruehstueck": "fruehstueck", "breakfast": "fruehstueck"}


def _token():
    """API token: KOMBUESE_TOKEN or the file ~/.config/kombuese/token."""
    t = os.environ.get("KOMBUESE_TOKEN")
    if not t:
        try:
            t = open(os.path.expanduser("~/.config/kombuese/token"), encoding="utf-8").read().strip()
        except OSError:
            t = ""
    return t


def req(url, data=None, headers=None, timeout=90):
    h = {"User-Agent": UA, **(headers or {})}
    if url.startswith(FT.rsplit("/api", 1)[0]) and _token():
        h["Authorization"] = "Bearer " + _token()
    if data is not None:
        data = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout) as r:
        return r.read()


def iso_minutes(s):
    if not s or not isinstance(s, str):
        return None
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?", s.strip())
    if not m:
        return None
    v = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    return v or None


def pick_image(img):
    if isinstance(img, str):
        return img
    if isinstance(img, list) and img:
        return pick_image(img[0])
    if isinstance(img, dict):
        u = img.get("url") or img.get("contentUrl")
        return u if isinstance(u, str) and u.startswith("http") else None
    return None


def og_image(url):
    try:
        page = req(url, headers={"Accept": "text/html"}, timeout=30).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    for pat in (r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image'):
        m = re.search(pat, page, re.I)
        if m:
            return html.unescape(m.group(1))
    return None


def clean(s, n):
    s = html.unescape(re.sub(r"<[^>]+>", " ", str(s or "")))
    return re.sub(r"\s+", " ", s).strip()[:n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--reason", "--grund", dest="reason", default="")
    ap.add_argument("--tags", default="")
    ap.add_argument("--title", "--titel", dest="title", default="")
    ap.add_argument("--meal", "--mahlzeit", dest="meal", choices=sorted(MEALS), default="haupt",
                    help="haupt/main = lunch/dinner, fruehstueck/breakfast = breakfast")
    ap.add_argument("--kcal", type=int, default=None,
                    help="estimated kcal per serving, if the source has none")
    ap.add_argument("--ingredients", "--zutaten", dest="ingredients", default="",
                    help="4-8 main ingredients in German, comma separated (the user taps them on a no), e.g. 'Austernpilze,Sahne,Fettuccine'")
    a = ap.parse_args()

    known = set(json.loads(req(f"{FT}/known-urls", timeout=15)))
    if a.url in known:
        print(f"DOPPELT {a.url}")
        return 0

    try:  # Kombüse reads the page (schema.org/Recipe)
        r = json.loads(req(f"{FT}/scrape?roh=1&url=" + urllib.parse.quote(a.url, safe=""), timeout=40))
    except Exception as e:  # noqa: BLE001
        print(f"FEHLER {a.url}: page not readable ({e})")
        return 1
    if not isinstance(r, dict) or not r.get("name"):
        print(f"FEHLER {a.url}: no recipe found ({str(r)[:80]})")
        return 1
    ingredients = [clean(i, 120) for i in (r.get("recipeIngredient") or []) if clean(i, 120)]
    if len(ingredients) < 3:
        print(f"FEHLER {a.url}: too few ingredients found ({len(ingredients)})")
        return 1

    minutes = iso_minutes(r.get("totalTime"))
    if not minutes or minutes > 8 * 60:  # soaking or resting times distort the total time
        parts = [iso_minutes(r.get(k)) for k in ("prepTime", "cookTime", "performTime")]
        minutes = sum(p for p in parts if p and p <= 8 * 60) or None
    y = r.get("recipeYield")
    y = y[0] if isinstance(y, list) and y else y
    servings = re.match(r"\d+", str(y or "")).group(0) if re.match(r"\d+", str(y or "")) else ""
    kcal = None
    n = r.get("nutrition") or {}
    per_100 = isinstance(n, dict) and re.search(r"100\s*(g|ml)", str(n.get("servingSize", "")), re.I)
    if isinstance(n, dict) and n.get("calories") and not per_100:  # values per 100 g are no use per serving
        m = re.search(r"\d+", str(n["calories"]).replace(".", ""))
        kcal = int(m.group(0)) if m else None
    kcal_est = False
    if not kcal and a.kcal:
        kcal, kcal_est = a.kcal, True
    image = pick_image(r.get("image")) or og_image(a.url)

    card = {
        "url": a.url,
        "title": a.title or clean(r["name"], 120),
        "description": clean(r.get("description"), 500),
        "reason": a.reason,
        "tags": [t.strip() for t in a.tags.split(",") if t.strip()],
        "minutes": minutes,
        "servings": servings,
        "kcal": kcal,
        "kcal_est": kcal_est,
        "meal": MEALS[a.meal],
        "ingredients": ingredients,
        "zutaten": [z.strip() for z in a.ingredients.split(",") if z.strip()],
        "image_url": image,
    }
    res = json.loads(req(f"{FT}/cards", card))
    if res.get("added"):
        kc = f"{'~' if kcal_est else ''}{kcal} kcal" if kcal else "kcal MISSING, please estimate with --kcal"
        print(f"OK {card['title']} ({len(ingredients)} ingredients, {minutes or '?'} min, {kc}, image {'yes' if image else 'no'})")
        return 0
    print(f"DOPPELT {a.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
