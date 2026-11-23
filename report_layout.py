"""Shared page layout for the SIPC weekly reports (newspaper style).

Used by both report generators:
  - ce-movement-weekly-report/generate_report.py   (CE Movement)
  - Weekly-report/generate_newsletter.py           (SEA & India Manufacturing Watch)

Pipeline: LLM body HTML -> clean_body() -> restructure() -> wrap()
  clean_body()   removes what reads as machine-generated: emoji and flag glyphs, ALL-CAPS
                 headings, BREAKING badges, LLM-written mastheads and footers
  restructure()  regroups the LLM's (inconsistently nested) markup into sections and stories
  wrap()         adds the nameplate, dateline, stylesheet and colophon

Keep this file identical in both repositories.
"""
import html as _html
import re
from datetime import datetime

from bs4 import BeautifulSoup, Tag

# ── text clean-up ─────────────────────────────────────────────────────────────
_EMOJI = re.compile(
    "[\U0001F1E6-\U0001F1FF"      # regional indicators (flags)
    "\U0001F300-\U0001FAFF"       # pictographs, emoji
    "☀-➿"               # misc symbols, dingbats
    "⬀-⯿"               # arrows / stars block
    "\uFE0F\u200D\u20E3]+")      # variation selector, joiner, keycap
_ACRONYMS = {"CE", "CMF", "SEA", "OEM", "EMS", "PCB", "FPC", "PLI", "TKDN", "AI", "US", "UK", "EU",
             "ASEAN", "LED", "LCD", "OLED", "UTG", "MLCC", "IDC", "PCM", "VCM", "HA", "TV", "IT", "R&D",
             "EV", "BOI", "MIDA", "FDI"}


def _sentence_case(text: str) -> str:
    words = text.split()
    out = []
    for i, w in enumerate(words):
        bare = re.sub(r"[^A-Za-z&]", "", w)
        if bare.upper() in _ACRONYMS:
            out.append(w.upper() if bare.isupper() or bare.upper() == bare else w)
        elif i == 0:
            out.append(w[:1].upper() + w[1:].lower())
        else:
            out.append(w.lower())
    return " ".join(out)


def _remove_block(html: str, opening: str) -> str:
    """Remove every element whose opening tag starts with `opening` (handles nested divs)."""
    while True:
        i = html.find(opening)
        if i < 0:
            return html
        tag = re.match(r"<(\w+)", opening).group(1)
        depth, j = 0, i
        pat = re.compile(rf"<{tag}\b|</{tag}>")
        for m in pat.finditer(html, i):
            depth += 1 if m.group(0).startswith(f"<{tag}") else -1
            if depth == 0:
                j = m.end()
                break
        else:
            return html
        html = html[:i] + html[j:]


def clean_body(body: str) -> str:
    body = re.sub(r"^```(?:html)?|```$", "", body.strip(), flags=re.M)
    for opening in ('<div class="masthead"', "<footer"):
        body = _remove_block(body, opening)
    body = re.sub(r'<span class="(?:badge-breaking|breaking-badge)">.*?</span>', "", body, flags=re.S)
    body = re.sub(r'<(?:span|div) class="section-icon">.*?</(?:span|div)>', "", body, flags=re.S)
    body = re.sub(r'<div class="mast-flags">.*?</div>', "", body, flags=re.S)
    def _ent(m):
        v = m.group(1)
        code = int(v[1:], 16) if v[:1] in "xX" else int(v)
        return _html.unescape(m.group(0)) if code > 0x2000 else m.group(0)
    body = re.sub(r"&#([xX][0-9A-Fa-f]+|[0-9]+);", _ent, body)
    body = _EMOJI.sub("", body)
    body = body.replace("\U0001F539", "")

    def fix_heading(m):
        inner = m.group(2)
        text = re.sub(r"<[^>]+>", "", inner).strip()
        if text and text == text.upper() and re.search(r"[A-Z]{3}", text):
            inner = _sentence_case(text.replace("&AMP;", "&amp;"))
        return f"<{m.group(1)}>{inner.strip()}</{m.group(1).split()[0]}>"
    body = re.sub(r"<(h2|h3|h4|div class=\"highlights-label\")>(.*?)</(?:h2|h3|h4|div)>",
                  fix_heading, body, flags=re.S)
    # leftover "→ " arrows and doubled spaces from removed glyphs
    body = body.replace("→ ", "").replace(" → ", ", ")
    body = body.replace(" · ", ", ").replace(" • ", ", ").replace("•", ", ")
    body = re.sub(r'(<div class="highlights-label">).*?(</div>)', r"\1This week in brief\2", body, count=1, flags=re.S)
    body = re.sub(r"(<h[234][^>]*>)\s+", r"\1", body)
    body = re.sub(r"  +", " ", body)
    return body



# ── regroup into sections and stories ───────────────────────────────────────────
STORY = {"card", "g-card", "s-card", "p-card", "tech-box", "trend-box", "overseas-box"}
FEATURE = {"tech-box", "trend-box", "overseas-box"}
WRAPPERS = {"global-wrap", "supply-wrap", "policy-wrap", "global-section", "src-index"}


def _cls(el):
    return set(el.get("class") or []) if isinstance(el, Tag) else set()


def _inside_story(el):
    return any(_cls(p) & STORY for p in el.parents)


def restructure(body: str) -> str:
    soup = BeautifulSoup(body, "html.parser")

    # top stories strip
    hl = [t.get_text(" ", strip=True) for t in soup.select(".highlights .hl-text")]
    top = ""
    if hl:
        items = "".join(f"<li>{t}</li>" for t in hl)
        top = f'<section class="topstories"><h2 class="topstories-label">Top stories</h2><ol>{items}</ol></section>'
    for h in soup.select(".highlights"):
        h.decompose()

    sections, cur = [], None

    def new_section(title, dek=""):
        nonlocal cur
        cur = {"title": title, "dek": dek, "blocks": []}
        sections.append(cur)

    def add(kind, html):
        if cur is None:
            new_section("")
        blocks = cur["blocks"]
        if kind == "story" and blocks and blocks[-1][0] == "grid":
            blocks[-1][1].append(html)
        elif kind == "story":
            blocks.append(("grid", [html]))
        elif kind == "feature" and blocks and blocks[-1][0] == "features":
            blocks[-1][1].append(html)
        elif kind == "feature":
            blocks.append(("features", [html]))
        else:
            blocks.append((kind, html))

    for el in soup.find_all(True):
        if _inside_story(el):
            continue
        c = _cls(el)
        if el.name == "h2" or ("highlights-label" in c and any(_cls(p) & WRAPPERS for p in el.parents)):
            title = el.get_text(" ", strip=True)
            if title:
                new_section(title)
            continue
        if "section-sub" in c:
            h3 = el.find("h3")
            text = el.get_text(" ", strip=True)
            if h3:
                add("sub", h3.get_text(" ", strip=True))
            elif cur is not None and not cur["dek"] and not cur["blocks"]:
                cur["dek"] = text
            elif text:
                add("sub", text)
            continue
        if c & STORY:
            kind = "feature" if c & FEATURE else "story"
            lead = "breaking" in c
            for b in el.select(".badge-breaking, .breaking-badge"):
                b.decompose()
            inner = el.decode_contents()
            klass = "story" + (" lead" if lead else "") + (" feature" if kind == "feature" else "")
            add(kind, f'<article class="{klass}">{inner}</article>')
            continue
        if el.name == "table" and not any(p.name == "table" for p in el.parents):
            add("table", str(el))
            continue
        if "no-news" in c:
            add("note", f'<p class="no-news">{el.decode_contents()}</p>')
            continue
        if el.name == "p" and el.parent is not None and el.parent.name in ("[document]", "div") \
                and not (_cls(el.parent) & ({"section-rule", "section-sub"} | WRAPPERS)) and cur is None:
            # free paragraph before the first section, e.g. an editor's note
            top += f'<p class="editors-note">{el.decode_contents()}</p>'

    out = [top]
    for s in sections:
        if not s["blocks"] and not s["title"]:
            continue
        head = ""
        if s["title"]:
            dek = f'<p class="desk-dek">{s["dek"]}</p>' if s["dek"] else ""
            head = f'<header class="desk-head"><h2>{s["title"]}</h2>{dek}</header>'
        parts = []
        for kind, val in s["blocks"]:
            if kind == "grid":
                leads = [v for v in val if 'class="story lead' in v]
                rest = [v for v in val if 'class="story lead' not in v]
                if len(rest) == 1:  # a lone story fills the width instead of leaving a gap
                    rest = [rest[0].replace('class="story', 'class="story solo', 1)]
                parts.append('<div class="desk-grid">' + "".join(leads + rest) + "</div>")
            elif kind == "features":
                parts.append('<div class="desk-features">' + "".join(val) + "</div>")
            elif kind == "sub":
                parts.append(f'<h3 class="desk-sub">{val}</h3>')
            elif kind == "table":
                parts.append(f'<div class="desk-table">{val}</div>')
            else:
                parts.append(val)
        if s["title"] and s["title"].lower().startswith("source index"):
            out.append(f'<section class="desk desk-index">{head}{"".join(parts)}</section>')
        else:
            out.append(f'<section class="desk">{head}{"".join(parts)}</section>')
    return "\n".join(out)


# ── stylesheet and page ─────────────────────────────────────────────────────────
FONTS = ("https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,600;"
         "0,6..72,700;0,6..72,800;1,6..72,400&family=Libre+Franklin:wght@400;500;600;700&display=swap")


def css(accent: str, accent_soft: str) -> str:
    return f"""
:root {{
  --paper: #ffffff; --surround: #e9eaec; --ink: #121212; --text: #2b2b2b; --muted: #6a6a6a;
  --rule: #d9d9d9; --alert: #b1251b; --accent: {accent}; --accent-soft: {accent_soft};
  --serif: "Newsreader", Georgia, "Times New Roman", serif;
  --sans: "Libre Franklin", "Helvetica Neue", Arial, sans-serif;
  color-scheme: light;
}}
* {{ box-sizing: border-box; }}
html, body {{ background: var(--surround); }}
body {{ margin: 0; color: var(--text); font: 17px/1.62 var(--serif); -webkit-text-size-adjust: 100%; }}
a {{ color: inherit; text-decoration: underline; text-decoration-color: #b9b9b9; text-underline-offset: 2px; }}
a:hover {{ text-decoration-color: var(--accent); }}
a:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
.page {{ max-width: 1180px; margin: 32px auto; background: var(--paper); padding: 0 64px 56px; }}

/* nameplate and dateline */
.nameplate {{ text-align: center; padding: 40px 0 18px; }}
.nameplate h1 {{ font: 800 62px/1 var(--serif); letter-spacing: -0.02em; margin: 0; color: var(--ink); }}
.nameplate .scope {{ font: 400 15px/1.5 var(--sans); color: var(--muted); margin: 14px auto 0; max-width: 72ch; }}
.dateline {{ display: flex; justify-content: space-between; gap: 16px; flex-wrap: wrap;
             border-top: 4px solid var(--ink); border-bottom: 1px solid var(--ink); padding: 8px 0;
             font: 500 13px/1.4 var(--sans); color: var(--ink); }}
.dateline .cov {{ color: var(--accent); font-weight: 700; }}

/* top stories strip */
.topstories {{ padding: 18px 0 22px; border-bottom: 1px solid var(--ink); }}
.topstories-label {{ font: 700 13px/1 var(--sans); color: var(--accent); margin: 0 0 14px; }}
.topstories ol {{ list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); }}
.topstories li {{ padding: 0 20px; border-left: 1px solid var(--rule); font: 600 17.5px/1.3 var(--serif); color: var(--ink); }}
.topstories li:first-child {{ padding-left: 0; border-left: 0; }}
.editors-note {{ font: italic 15px/1.55 var(--serif); color: var(--muted); margin: 14px 0 0; max-width: 80ch; }}

/* section fronts */
.desk {{ margin-top: 54px; }}
.desk-head {{ border-top: 3px solid var(--accent); padding-top: 0; margin-bottom: 4px;
              display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; }}
.desk-head h2 {{ font: 700 15px/1 var(--sans); color: #fff; background: var(--accent); margin: 0;
                 padding: 9px 14px 8px; }}
.desk-dek {{ font: 400 13.5px/1.4 var(--sans); color: var(--muted); margin: 0; }}
.desk-sub {{ font: 700 21px/1.25 var(--serif); color: var(--ink); margin: 30px 0 0; padding-bottom: 8px;
             border-bottom: 1px solid var(--ink); }}

/* story grid: two columns, vertical rule, lead spans both */
.desk-grid {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }}
.story {{ padding: 22px 30px 24px 0; border-top: 1px solid var(--rule); }}
.desk-grid > .story:nth-child(even of :not(.lead)) {{ padding: 22px 0 24px 30px; border-left: 1px solid var(--rule); }}
.story.lead {{ grid-column: 1 / -1; padding: 24px 0 28px; display: grid;
               grid-template-columns: minmax(0, 5fr) minmax(0, 4fr); column-gap: 36px; }}
.story.lead > * {{ grid-column: 1 / -1; }}
.story.solo {{ grid-column: 1 / -1; padding: 22px 0 24px !important; border-left: 0 !important; display: grid;
               grid-template-columns: minmax(0, 5fr) minmax(0, 4fr); column-gap: 36px; }}
.story.solo > * {{ grid-column: 1; }}
.story.solo:not(:has(.impact, .s-impact, .p-alert)) {{ display: block; }}
.story.solo:not(:has(.impact, .s-impact, .p-alert)) > p {{ columns: 2; column-gap: 40px; column-rule: 1px solid var(--rule); max-width: none; }}
.story.solo > .card-header, .story.solo > .btag {{ grid-column: 1 / -1; }}
.story.solo > .impact, .story.solo > .s-impact, .story.solo > .p-alert {{ grid-column: 2; grid-row: 2 / span 4;
               align-self: start; margin-top: 4px; }}
.story.lead h3 {{ font-size: 38px; line-height: 1.1; letter-spacing: -0.015em; max-width: 24ch; margin-bottom: 14px; }}
.story.lead > p {{ grid-column: 1; font-size: 18px; }}
.story.lead > .impact {{ grid-column: 2; grid-row: 3 / span 3; align-self: start; margin-top: 0; }}
.story.lead > .src {{ grid-column: 1; }}
.card-header {{ display: block; }}
.btag {{ display: block; background: none !important; color: var(--accent) !important; padding: 0;
         border-radius: 0; font: 700 13px/1.2 var(--sans); margin: 0 0 9px; text-transform: none; letter-spacing: 0; }}
.story.lead .btag::after {{ content: "  |  Lead story"; color: var(--muted); font-weight: 500; white-space: pre; }}
.story h3, .story h4 {{ font: 700 24px/1.2 var(--serif); color: var(--ink); margin: 0 0 10px; letter-spacing: -0.005em;
                       text-wrap: balance; }}
.story p {{ margin: 0 0 12px; }}
.s-meta, .g-link {{ font: 600 12.5px/1.45 var(--sans); color: var(--accent); margin: 0 0 9px; }}
.p-status {{ font: 600 12.5px/1.45 var(--sans); color: var(--muted); margin: 0 0 9px; }}

/* analysis box */
.impact, .s-impact {{ margin: 14px 0 12px; padding: 13px 16px; background: var(--accent-soft); border: 0;
                      border-top: 2px solid var(--accent); }}
.impact-label {{ display: block; font: 700 12.5px/1.2 var(--sans); color: var(--accent); margin: 0 0 5px;
                 text-transform: none; letter-spacing: 0; }}
.impact p, .s-impact p {{ font: italic 400 16px/1.55 var(--serif); margin: 0; color: var(--ink); }}
.p-alert {{ margin: 12px 0; padding: 10px 14px; background: #fbefee; color: var(--alert); border-top: 2px solid var(--alert);
            font: 500 14px/1.5 var(--sans); }}

/* credit lines */
.src, .g-src, .s-src, .supplier-basis {{ font: 400 12.5px/1.5 var(--sans); color: var(--muted); margin: 8px 0 0; }}
.no-news {{ font: italic 16px/1.5 var(--serif); color: var(--muted); margin: 16px 0 0; }}

/* long-form features (CE deep dive): full width, bullet facts in two columns */
.desk-features .story {{ padding: 26px 0 28px; border-left: 0; }}
.story.feature h4 {{ font-size: 29px; line-height: 1.15; max-width: 32ch; margin-bottom: 12px; }}
.story.feature > p {{ max-width: 76ch; }}
.story.feature ul {{ columns: 2; column-gap: 40px; column-rule: 1px solid var(--rule); margin: 8px 0 14px; padding-left: 18px; }}
.story.feature li {{ margin: 0 0 9px; break-inside: avoid; }}
.supplier-box {{ margin: 18px 0 12px; padding: 16px 20px; background: #f6f6f6; border-left: 4px solid var(--accent); }}
.supplier-label {{ font: 700 13px/1.2 var(--sans); color: var(--accent); margin: 0 0 8px; text-transform: none; letter-spacing: 0; }}
.supplier-box p {{ font: 400 15.5px/1.6 var(--serif); margin: 0 0 8px; }}

/* tables */
.desk-table {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; margin: 18px 0; font: 400 14px/1.45 var(--sans); }}
th {{ text-align: left; font-weight: 700; color: var(--ink); background: none !important; border-bottom: 2px solid var(--ink);
      padding: 8px 14px 8px 0; text-transform: none; letter-spacing: 0; font-size: 13px; }}
td {{ padding: 9px 14px 9px 0; border-bottom: 1px solid var(--rule); vertical-align: top; background: none !important; color: var(--text); }}
.desk-index table {{ font-size: 12.5px; }}

/* colophon */
.colophon {{ margin-top: 56px; border-top: 4px solid var(--ink); padding-top: 12px; display: flex; gap: 32px;
             justify-content: space-between; flex-wrap: wrap; font: 400 12.5px/1.55 var(--sans); color: var(--muted); }}
.colophon p {{ margin: 0; max-width: 62ch; }}

@media (max-width: 880px) {{
  .page {{ margin: 0; padding: 0 20px 36px; }}
  .nameplate h1 {{ font-size: 40px; }}
  .topstories ol {{ grid-template-columns: 1fr 1fr; row-gap: 16px; }}
  .topstories li:nth-child(3) {{ padding-left: 0; border-left: 0; }}
  .desk-grid {{ display: block; }}
  .story, .desk-grid > .story:nth-child(even of :not(.lead)) {{ padding: 20px 0; border-left: 0; }}
  .story.lead, .story.solo {{ display: block; }}
  .story.lead h3 {{ font-size: 29px; }}
  .story.feature ul {{ columns: 1; }}
}}
@media (max-width: 520px) {{
  .topstories ol {{ grid-template-columns: 1fr; }}
  .topstories li {{ padding-left: 0; border-left: 0; }}
}}
@media print {{
  html, body {{ background: #fff; }}
  .page {{ margin: 0; max-width: none; padding: 0; }}
  .story {{ break-inside: avoid; }}
}}
"""


def wrap(body: str, *, title: str, scope: str, accent: str, accent_soft: str,
         start: datetime, end: datetime, sources_note: str, method_note: str) -> str:
    week = end.isocalendar()
    coverage = f"{start.day} {start:%b} – {end.day} {end:%b %Y}"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} | Week {week[1]}, {week[0]}</title>
<link href="{FONTS}" rel="stylesheet">
<style>{css(accent, accent_soft)}</style>
</head>
<body>
<div class="page">
<header class="nameplate">
  <h1>{title}</h1>
  <p class="scope">{scope}</p>
</header>
<div class="dateline">
  <span>Week {week[1]}, {week[0]}</span>
  <span class="cov">Coverage: {coverage}</span>
  <span>{end:%A}, {end.day} {end:%B %Y}</span>
</div>
<main>
{restructure(clean_body(body))}
</main>
<footer class="colophon">
  <p><strong>{title}</strong>. Prepared by SIPC for internal sourcing use. {sources_note}</p>
  <p>{method_note}</p>
</footer>
</div>
</body>
</html>"""
