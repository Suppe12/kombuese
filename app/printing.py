"""Print view of a recipe: an A4 page that Gotenberg turns into a PDF file (pdf() below).
Quantities are scaled to the requested servings. Routes in server.py: /rezept/<slug>/druck and /rezept/<slug>.pdf.

Layout (reworked 2026-11-22, page breaks): small photo to the right of the title,
nutrients as a bar in the header, ingredients in groups that never break across a page, steps never split,
notes across the full width at the bottom. **bold** in recipes is printed bold."""
import html
import json
import math
import re

_FRACTIONS = [(0, ""), (.25, "¼"), (1 / 3, "⅓"), (.5, "½"), (2 / 3, "⅔"), (.75, "¾"), (1, "")]


def quantity(q):
    """Like in the app: from 50 in steps of 5, from 10 whole numbers, below that with fractions."""
    if not q:
        return ""
    if q >= 50:
        return str(int(round(q / 5) * 5))
    if q >= 10:
        return str(int(round(q)))
    w = math.floor(q)
    v, s = min(_FRACTIONS, key=lambda b: abs(b[0] - (q - w)))
    w = w + 1 if v == 1 else w
    return (str(w) if w else "") + s or str(round(q, 1)).replace(".", ",")


def _e(s):
    return html.escape(str(s or ""))


def _t(s):
    """Text with **bold** and line breaks."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", _e(s)).replace("\n", "<br>")


def _time(z):
    return re.sub(r"Minuten?", "min", str(z or ""))


def _number(x):
    return str(int(x)) if x == int(x) else str(round(x, 2)).replace(".", ",")


CSS = """
@page { size: A4; margin: 13mm 14mm 14mm; }
* { box-sizing: border-box; }
body { margin: 0; font: 10pt/1.45 -apple-system, "Segoe UI", Roboto, "Noto Sans", "Liberation Sans", Arial, sans-serif; color: #2a211b;
  background: #fff; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.blatt { max-width: 182mm; margin: 0 auto; padding: 10px 14px 70px; }
.serif { font-family: Georgia, "Noto Serif", "DejaVu Serif", "Liberation Serif", serif; }
.kopf { display: grid; grid-template-columns: 1fr 64mm; gap: 7mm; align-items: start; padding-bottom: 4mm; border-bottom: 2px solid #ef6c45; margin-bottom: 5mm; }
.kopf.ohnebild { grid-template-columns: 1fr; }
.foto { width: 64mm; height: 48mm; border-radius: 8px; background: #f0e7dc center/cover; }
h1 { font-size: 23pt; line-height: 1.12; margin: 0 0 2.5mm; letter-spacing: -.3px; }
.meta { color: #8a7c70; font-size: 9pt; }
.meta b { color: #2a211b; }
.tags { margin-top: 2mm; }
.tags span { display: inline-block; font-size: 7.5pt; background: #f0e7dc; border-radius: 4px; padding: 1px 6px; margin: 0 3px 3px 0; color: #6b5d51; }
.besch { margin: 2.5mm 0 0; font-style: italic; color: #4a3f36; font-size: 9.5pt; }
.nw { display: flex; gap: 1mm; margin-top: 2.5mm; flex-wrap: wrap; align-items: baseline; font-size: 7.5pt; color: #8a7c70; }
.nw span.l { margin-right: .5mm; }
.nw div { background: #faf6f1; border-radius: 4px; padding: .6mm 1.6mm; }
.nw b { color: #2a211b; font-size: 8.5pt; }
.spalten { display: grid; grid-template-columns: 60mm 1fr; gap: 8mm; align-items: start; }
h2 { font-size: 13pt; color: #ef6c45; margin: 0 0 2mm; break-after: avoid; page-break-after: avoid; }
.gruppe { break-inside: avoid; page-break-inside: avoid; margin-bottom: 2.5mm; }
.gruppe h3 { margin: 1.5mm 0 1mm; font-size: 8pt; text-transform: uppercase; letter-spacing: .4px; color: #8a7c70; }
.z { display: flex; gap: 2.5mm; padding: .8mm 0; border-bottom: 1px solid #eee3d6; font-size: 9.5pt; line-height: 1.35; }
.z .q { min-width: 15mm; text-align: right; font-weight: 700; color: #c4502c; flex: none; }
.z small { color: #8a7c70; }
.schritt { position: relative; padding: 0 0 3mm 8.5mm; break-inside: avoid; page-break-inside: avoid; }
.schritt i { position: absolute; left: 0; top: .2mm; width: 5.8mm; height: 5.8mm; border-radius: 50%; background: #ef6c45; color: #fff;
  font: normal 700 8pt/5.8mm sans-serif; text-align: center; }
.schritt b.t { display: block; }
.notizen { margin-top: 2mm; }
.notizen h2 { margin-bottom: 1.5mm; }
.note { background: #faf6f1; border-left: 3px solid #e0a422; border-radius: 4px; padding: 2.2mm 3.2mm; margin: 0 0 3mm; break-inside: avoid;
  page-break-inside: avoid; font-size: 8.8pt; }
.note b.t { display: block; margin-bottom: .8mm; }
.note.heat { border-left-color: #ef6c45; }
.hinweise { margin: 1mm 0 0; font-size: 7.5pt; line-height: 1.4; color: #a89a8c; }
.hinweise b { color: #8a7c70; }
.knopf { position: fixed; left: 0; right: 0; bottom: 0; display: flex; gap: 8px; justify-content: center; padding: 10px;
  background: rgba(255,255,255,.92); border-top: 1px solid #eee3d6; }
.knopf button { border: 0; border-radius: 999px; background: #ef6c45; color: #fff; font: 700 15px sans-serif; padding: 11px 18px; }
.knopf button.g { background: #f0e7dc; color: #2a211b; }
@media print { .knopf { display: none; } .blatt { padding: 0; max-width: none; } }
@media screen and (max-width: 560px) { .kopf, .spalten { grid-template-columns: 1fr; } .foto { width: 100%; height: 52vw; order: -1; }
 }
"""

# Only for the PDF file (?pdf=1): sheet at print width, then measure. Notes go into the right column if there is room
# below the steps (otherwise full width below). If the recipe is only a little longer than one page (up to 12 %), it is
# shrunk onto one page instead of sending two lines alone to page 2.
PDF_CSS = ".blatt { width: 182mm; max-width: none; padding: 0; margin: 0; } .knopf { display: none; }"
PDF_JS = """<script>
(function () {
  const sp = document.querySelector('.spalten'), L = sp.children[0], R = sp.children[1];
  const N = document.querySelector('.notizen'), H = document.querySelector('.hinweise');
  if (N) {
    R.appendChild(N); if (H) R.appendChild(H);
    if (R.offsetHeight > L.offsetHeight + 15) { sp.after(N); if (H) N.after(H); }
  } else if (H) {
    R.appendChild(H); if (R.offsetHeight > L.offsetHeight + 15) sp.after(H);
  }
  const mm = 96 / 25.4, seite = (297 - 13 - 14) * mm, b = document.querySelector('.blatt'), h = b.scrollHeight;
  if (h > seite && h < seite * 1.12) b.style.zoom = (seite / h * 0.985).toFixed(3);
})();
</script>"""


# English (?lang=en): headings, units and nutrients; the recipe text stays as it is
EN = {"Vorbereitung": "Prep", "Kochen": "Cook", "Gesamt": "Total", "Portion": "serving", "Portionen": "servings", "Original": "original",
      "Eiweiß": "Protein", "KH": "Carbs", "Fett": "Fat", "Zucker": "Sugar", "Ballaststoffe": "Fiber", "Pro Portion": "Per serving",
      "Zutaten": "Ingredients", "Zubereitung": "Method", "Notizen": "Notes", "Quelle": "Source", "Zurück": "Back",
      "Geteilt mit Kombüse": "Shared with Kombüse", "Weniger Portionen": "Fewer servings", "Mehr Portionen": "More servings"}

# Public page of a shared recipe (share.py): no script, the servings change with a plain GET form
SHARE_CSS = """
.knopf form { display: flex; align-items: center; gap: 6px; margin: 0; }
.knopf form button { padding: 9px 15px; background: #f0e7dc; color: #2a211b; }
.knopf form b { min-width: 92px; text-align: center; font: 700 14px sans-serif; }
.knopf a.b { border-radius: 999px; background: #ef6c45; color: #fff; font: 700 15px sans-serif; padding: 11px 18px; text-decoration: none; }
.hinweise a { color: inherit; }
.geteilt { margin-top: 6mm; font-size: 8pt; color: #a89a8c; text-align: center; }
"""
UNITS_EN = {"EL": "tbsp", "TL": "tsp", "Stück": "pc", "Zehe": "clove", "Prise": "pinch", "Packung": "pack", "Dose": "can", "Bund": "bunch",
           "Knolle": "bulb", "Becher": "tub", "Flasche": "bottle", "Glas": "jar", "Tube": "tube", "Zweig": "sprig", "Scheibe": "slice",
           "Blatt": "leaf", "Handvoll": "handful", "Portion": "serving", "Tasse": "cup", "Kopf": "head", "Stange": "stalk", "Würfel": "cube",
           "Schuss": "dash", "Spritzer": "splash", "Messerspitze": "pinch"}


def _share_bar(token, portions, orig, _):
    """Servings − / + and the PDF link for the public page (share mode)."""
    step = 1 if portions >= 2 else 0.5
    down, up = max(0.5, portions - step), min(50, portions + step)
    pdf_q = "" if abs(portions - orig) < 1e-6 else f"?portionen={_number(portions)}"
    label = f"{_number(portions)} {_('Portion') if portions == 1 else _('Portionen')}"
    return (f'<div class="knopf"><form method="get">'
            f'<button name="portionen" value="{_number(down).replace(",", ".")}" aria-label="{_e(_("Weniger Portionen"))}">−</button>'
            f'<b>{_e(label)}</b>'
            f'<button name="portionen" value="{_number(up).replace(",", ".")}" aria-label="{_e(_("Mehr Portionen"))}">+</button></form>'
            f'<a class="b" href="/s/{_e(token)}.pdf{pdf_q}">📄 PDF</a></div>')


def recipe_page(r, portions_arg=None, print_now=False, pdf=False, lang="de", share=None):
    """share: token of a public link (share.py) → public page: photo and PDF through /s/<token>, servings form, no script."""
    lang = "en" if lang == "en" else "de"  # ?lang= comes straight from the URL and goes into <html lang>: no reflected HTML (security review)
    _ =(lambda x: EN.get(x, x)) if lang == "en" else (lambda x: x)  # noqa: E731
    _u = (lambda x: UNITS_EN.get(x, x)) if lang == "en" else (lambda x: x)  # noqa: E731
    portions = float(portions_arg or r["portionen"] or 1)
    f = portions / float(r["portionen"] or 1)
    nw = r["naehrwerte"]
    times = " · ".join(f"{_(l)} {_time(r['zeiten'][k])}" for l, k in (("Vorbereitung", "vorbereitung"), ("Kochen", "kochen"), ("Gesamt", "gesamt"))
                        if r["zeiten"].get(k))
    orig = float(r["portionen"] or 1)
    portions_text = f"{_number(portions)} {_('Portion') if portions == 1 else _('Portionen')}" + (f" ({_('Original')} {_number(orig)})" if abs(portions - orig) > 1e-6 else "")

    # ingredients in groups (one per section), a group never breaks across pages
    groups, cur = [], {"titel": "", "zeilen": []}
    for z in r["zutaten"]:
        if z.get("abschnitt") and (cur["zeilen"] or cur["titel"]):
            groups.append(cur)
            cur = {"titel": "", "zeilen": []}
        if z.get("abschnitt"):
            cur["titel"] = z["abschnitt"]
        if z.get("name"):
            q = " ".join(x for x in (quantity((z["menge"] or 0) * f), _u(z["einheit"])) if x)
            note = " <small>" + _e(z["notiz"]) + "</small>" if z.get("notiz") else ""
            cur["zeilen"].append(f'<div class="z"><span class="q">{_e(q)}</span><span>{_e(z["name"])}{note}</span></div>')
    groups.append(cur)
    ingr = "".join('<div class="gruppe">' + (f"<h3>{_e(g['titel'])}</h3>" if g["titel"] else "") + "".join(g["zeilen"]) + "</div>"
                  for g in groups if g["zeilen"] or g["titel"])

    steps = "".join(f'<div class="schritt"><i>{n}</i>' + (f'<b class="t">{_t(s["titel"])}</b>' if s["titel"] else "") + _t(s["text"]) + "</div>"
                       for n, s in enumerate(r["schritte"], 1))
    facts = [("kcal", nw.get("kcal"), ""), ("Eiweiß", nw.get("protein"), " g"), ("KH", nw.get("kh"), " g"),
             ("Fett", nw.get("fett"), " g"), ("Zucker", nw.get("zucker"), " g"), ("Ballaststoffe", nw.get("ballaststoffe"), " g")]
    facts = "".join(f"<div><b>{round(v)}{u}</b> {_(l)}</div>" for l, v, u in facts if v is not None)
    facts = f'<span class="l">{_("Pro Portion")}</span>' + facts if facts else ""
    # notes that matter for cooking as boxes, bookkeeping (nutrients estimated, source/changes, photo) small at the end
    _small = lambda n: re.match(r"(?i)(nährwert|quelle|foto|bild)", n["titel"] or "")  # noqa: E731
    notes = "".join('<div class="note">' + (f'<b class="t">{_t(n["titel"])}</b>' if n["titel"] else "") + _t(n["text"]) + "</div>"
                      for n in r["notizen"] if not _small(n))
    # reheating: one portion, in the household's microwave; the public page keeps the recommended watt
    import reheat
    heat = reheat.info(r) if share else r.get("aufwaermen_info") or reheat.info(r)
    heat_title, heat_lines = reheat.lines(heat, lang)
    if heat_lines:
        notes = (f'<div class="note heat"><b class="t">{_e(heat_title)}</b>' + "<br>".join(_e(x) for x in heat_lines) + "</div>") + notes
    hints = [n for n in r["notizen"] if _small(n) and not n["titel"].lower().startswith(("foto", "bild"))]
    # „Nährwerte: Nährwerte geschätzt …“ → leave out the title if the text already starts with it
    hints = " ".join(("" if n["text"].lower().startswith(n["titel"].lower()[:8]) else f"<b>{_e(n['titel'])}:</b> ") + _t(n["text"])
                        for n in hints)
    img = f"/s/{share}/bild" if share else f"/mimg/{r['id']}/original.webp"
    photo = f'<div class="foto" style="background-image:url(\'{img}\')"></div>' if r.get("bild") else ""
    tags = "".join(f"<span>{_e(t)}</span>" for t in r["tags"])
    short = re.sub(r"^https?://(www\.)?", "", r.get("quelle") or "")[:90]
    source = _e(short)
    if share and short and re.fullmatch(r"https?://[^\s\"'<>]+", r.get("quelle") or ""):
        source = f'<a href="{_e(r["quelle"])}" rel="noopener noreferrer nofollow">{_e(short)}</a>'
    footer = " · ".join(x for x in (hints, f"<b>{_('Quelle')}:</b> " + source if short else "") if x)
    head = ('<meta name="robots" content="noindex, nofollow, noarchive"><meta name="referrer" content="no-referrer">'
            f"<style>{SHARE_CSS}</style>") if share else ""
    bar = (_share_bar(share, portions, orig, _) if share else
           f"""<div class="knopf"><button class="g" onclick="history.length > 1 ? history.back() : window.close()">{_("Zurück")}</button><button onclick="location.href = location.pathname.replace(/\\/druck$/, '.pdf') + location.search.replace(/&?drucken=1/, '')">📄 PDF</button></div>""")
    return f"""<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(r["name"])}</title><style>{CSS}</style>{head}</head><body><div class="blatt">
<div class="kopf{'' if photo else ' ohnebild'}"><div>
  <h1 class="serif">{_e(r["name"])}</h1>
  <div class="meta"><b>{_e(portions_text)}</b>{f" · {_e(times)}" if times else ""}</div>
  {f'<div class="nw">{facts}</div>' if facts else ""}
  {f'<div class="tags">{tags}</div>' if tags else ""}
  {f'<p class="besch">{_t(r["beschreibung"])}</p>' if r.get("beschreibung") else ""}
</div>{photo}</div>
<div class="spalten">
  <div><h2 class="serif">{_("Zutaten")}</h2>{ingr}</div>
  <div><h2 class="serif">{_("Zubereitung")}</h2>{steps}</div>
</div>
{f'<div class="notizen"><h2 class="serif">{_("Notizen")}</h2>{notes}</div>' if notes else ""}
{f'<p class="hinweise">{footer}</p>' if footer else ""}
{f'<p class="geteilt">{_("Geteilt mit Kombüse")}</p>' if share else ""}
</div>
{bar}
{"<script>window.addEventListener('load', () => setTimeout(() => window.print(), 600));</script>" if print_now else ""}
{f"<style>{PDF_CSS}</style>{PDF_JS}" if pdf else ""}
</body></html>"""


# ---------- real PDF file ----------
# On the iPhone window.print() does not work in the home screen app. So Gotenberg (Chromium, container
# kombuese-pdf in the same stack) renders the print view into a PDF file the iPhone can show, share and save.
import config  # noqa: E402

PDF_SERVICE = config.GOTENBERG_URL + "/forms/chromium/convert/url"
SELF_URL = config.INTERNAL_URL


class Error(Exception):
    pass


def pdf(slug, portions_arg=None, lang="de"):
    import urllib.error
    import urllib.request
    import uuid
    url = f"{SELF_URL}/rezept/{slug}/druck?pdf=1&lang={'en' if lang == 'en' else 'de'}" + (f"&portionen={portions_arg:g}" if portions_arg else "")
    import households  # internal key of the household: without a login Gotenberg may only load the print view and images
    fields = {"url": url, "printBackground": "true", "preferCssPageSize": "true", "waitDelay": "0.3s",
              "extraHttpHeaders": json.dumps({"Authorization": "Bearer " + households.internal_token()})}
    boundary = uuid.uuid4().hex
    body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items())
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(PDF_SERVICE, data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=70) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise Error(f"PDF-Dienst: HTTP {e.code} {e.read()[:200].decode('utf-8', 'replace')}") from None
    except urllib.error.URLError as e:
        raise Error(f"PDF-Dienst nicht erreichbar: {e.reason}") from None
