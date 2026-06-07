#!/usr/bin/env python3
"""
SEA Consumer Electronics Intelligence Newsletter Generator
=========================================================
Runs 7 rounds of web searches via Tavily, then calls either
DeepSeek or Anthropic Claude to write the newsletter body,
and wraps it into a fully-styled HTML file.

Providers
---------
  deepseek   — default; uses openai-compatible API at api.deepseek.com
  anthropic  — uses the anthropic SDK

Usage
-----
  python generate_newsletter.py                      # normal run
  python generate_newsletter.py --dry-run            # search only, skip LLM
  python generate_newsletter.py --save-search f.txt  # persist search results
  python generate_newsletter.py --search-cache f.txt # re-use saved results
"""

import argparse
import json
import logging
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ── dependency guard ──────────────────────────────────────────────────────────
try:
    import requests
    from dotenv import load_dotenv
except ImportError as e:
    print(f"\n[ERROR] Missing package: {e}")
    print("Run:  pip install -r requirements.txt\n")
    sys.exit(1)

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

PROVIDER          = os.getenv("PROVIDER", "deepseek").lower()
DEEPSEEK_API_KEY  = os.getenv("DEEPSEEK_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TAVILY_API_KEY    = os.getenv("TAVILY_API_KEY", "")

_DEFAULT_MODELS = {"deepseek": "deepseek-chat", "anthropic": "claude-opus-4-7"}
MODEL      = os.getenv("MODEL") or _DEFAULT_MODELS.get(PROVIDER, "deepseek-chat")
MAX_TOKENS = int(os.getenv("MAX_TOKENS") or "7000")   # DeepSeek hard limit 8192; "or" handles empty string
SEARCH_N   = int(os.getenv("SEARCH_RESULTS_PER_QUERY") or "6")

# Default to ./output so GitHub Actions works out of the box;
# override with OUTPUT_DIR env var for local Windows paths.
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "./output"))

# ── Email configuration ───────────────────────────────────────────────────────
EMAIL_SMTP_HOST = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
EMAIL_SMTP_PORT = int(os.getenv("EMAIL_SMTP_PORT", "465"))   # 465=SSL, 587=STARTTLS
EMAIL_FROM      = os.getenv("EMAIL_FROM", "")
EMAIL_PASSWORD  = os.getenv("EMAIL_PASSWORD", "")            # Gmail: use App Password
EMAIL_TO        = os.getenv("EMAIL_TO", "")                  # comma-separated recipients
EMAIL_CC        = os.getenv("EMAIL_CC", "")                  # optional CC list

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path(__file__).parent / "newsletter_generator.log"),
    ],
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# EMBEDDED CSS  — matches the reference v4 newsletter design
# ══════════════════════════════════════════════════════════════════════════════

CSS = """
:root{--navy:#1a3a5c;--red:#c0392b;--cream:#f5f0e8;--white:#ffffff;
--yellow-bg:#fdf6e3;--new-bg:#fff8f0;--ink:#0d0d0d;--muted:#6b5f50;
--rule:#d4c9b5;--supply-bg:#f0eaf8;--supply-accent:#6d28d9;
--supply-border:#c4b5fd;--gold:#b8860b;--gold-light:#fdf3d0;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{font-family:'Source Sans 3',sans-serif;background:var(--cream);color:var(--ink);font-size:15px;line-height:1.7}
a{color:var(--navy)}a:hover{text-decoration:underline}
.wrap{max-width:880px;margin:0 auto;padding:0 24px 80px}
.masthead{background:var(--navy);color:#fff;padding:52px 40px 40px;position:relative;overflow:hidden}
.masthead::after{content:'';position:absolute;inset:0;background:repeating-linear-gradient(-55deg,transparent,transparent 8px,rgba(255,255,255,.03) 8px,rgba(255,255,255,.03) 9px);pointer-events:none}
.masthead::before{content:'';position:absolute;bottom:0;left:0;right:0;height:5px;background:linear-gradient(90deg,var(--red) 0%,#e84040 50%,var(--red) 100%)}
.mast-eyebrow{font-family:'Space Mono',monospace;font-size:9.5px;letter-spacing:.22em;text-transform:uppercase;color:rgba(255,255,255,.5);margin-bottom:12px}
.mast-title{font-family:'Playfair Display',serif;font-size:36px;font-weight:700;line-height:1.1;letter-spacing:-.02em;margin-bottom:16px}
.mast-title em{font-style:italic;font-weight:400;color:rgba(255,255,255,.75)}
.mast-flags{font-size:22px;letter-spacing:3px;margin:14px 0 10px;opacity:.9}
.mast-tags{display:flex;flex-wrap:wrap;gap:7px;margin:14px 0 0}
.mast-tag{font-family:'Space Mono',monospace;font-size:9px;letter-spacing:.14em;text-transform:uppercase;border:1px solid rgba(255,255,255,.28);color:rgba(255,255,255,.68);padding:3px 9px;border-radius:2px}
.mast-meta{font-family:'Space Mono',monospace;font-size:10px;color:rgba(255,255,255,.6);margin-top:18px;display:flex;flex-wrap:wrap;gap:20px}
.highlights{background:var(--red);color:#fff;padding:30px 32px}
.highlights-label{font-family:'Space Mono',monospace;font-size:9.5px;letter-spacing:.2em;text-transform:uppercase;opacity:.75;margin-bottom:18px}
.hl-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:560px){.hl-grid{grid-template-columns:1fr}}
.hl-item{display:flex;gap:12px;align-items:flex-start}
.hl-num{font-family:'Playfair Display',serif;font-size:30px;font-weight:700;line-height:1;opacity:.3;flex-shrink:0;width:26px}
.hl-text{font-size:13.5px;font-weight:600;line-height:1.45}
.section-rule{display:flex;align-items:center;gap:12px;margin:40px 0 20px;padding-bottom:12px;border-bottom:2.5px solid var(--navy)}
.section-icon{font-size:22px}
.section-rule h2{font-family:'Playfair Display',serif;font-size:22px;font-weight:700;color:var(--navy);flex:1}
.section-sub{font-family:'Space Mono',monospace;font-size:9px;letter-spacing:.13em;text-transform:uppercase;color:var(--muted)}
.card{background:var(--white);border-top:3px solid var(--navy);padding:22px 24px;margin-bottom:20px;position:relative;box-shadow:0 1px 6px rgba(0,0,0,.05),0 0 0 1px rgba(0,0,0,.04)}
.card.breaking{border-top-color:var(--red)}
.card.new-item{background:var(--new-bg)}
.breaking-badge{position:absolute;top:0;right:0;background:var(--red);color:#fff;font-family:'Space Mono',monospace;font-size:8.5px;font-weight:700;letter-spacing:.14em;padding:3px 9px;text-transform:uppercase}
.card-header{display:flex;align-items:flex-start;gap:10px;margin-bottom:12px}
.btag{font-family:'Space Mono',monospace;font-size:8.5px;font-weight:700;letter-spacing:.13em;text-transform:uppercase;color:#fff;padding:3px 9px;border-radius:2px;flex-shrink:0;margin-top:3px}
.b-samsung{background:#1428a0}.b-apple{background:#555}.b-huawei{background:#c82333}
.b-oppo{background:#1e4a8c}.b-xiaomi{background:#f97316}.b-vivo{background:#415fff}
.b-honor{background:#b8000e}.b-realme{background:#d97706}.b-iqoo{background:#0050a0}
.b-transsion{background:#7c3aed}.b-motorola{background:#003087}.b-dyson{background:#c34a00}
.b-panasonic{background:#003087}.b-hisense{background:#0a3d6b}.b-haier{background:#00529b}
.b-tcl{background:#e31837}.b-policy{background:#374151}.b-event{background:#065f46}
.b-supply{background:#5b21b6}.b-data{background:#0369a1}.b-ems{background:#5b21b6}
.card h3{font-family:'Playfair Display',serif;font-size:17.5px;font-weight:700;line-height:1.3;color:var(--ink)}
.card p{font-size:14px;color:#2c2620;line-height:1.7;margin-top:10px}
.card p+p{margin-top:8px}
.impact{background:var(--yellow-bg);border-left:3px solid var(--gold);padding:11px 15px;margin-top:16px}
.impact-label{font-family:'Space Mono',monospace;font-size:8.5px;font-weight:700;letter-spacing:.16em;text-transform:uppercase;color:var(--gold);margin-bottom:5px}
.impact p{font-size:13px;color:#3d2c00;line-height:1.6;margin-top:0}
.src{font-family:'Space Mono',monospace;font-size:9.5px;color:var(--muted);margin-top:14px;padding-top:10px;border-top:1px solid var(--rule);line-height:1.6}
.src a{color:var(--navy);text-decoration:none}.src a:hover{text-decoration:underline}
.no-news{font-family:'Space Mono',monospace;font-size:11px;color:var(--muted);padding:14px 0 6px;font-style:italic}
.global-wrap{background:var(--navy);color:#fff;padding:34px 36px;margin:40px 0}
.global-wrap h2{font-family:'Playfair Display',serif;font-size:22px;font-weight:700;margin-bottom:22px;display:flex;align-items:center;gap:10px}
.g-card{border-left:3px solid rgba(255,255,255,.25);padding:13px 18px;margin-bottom:18px}
.g-card:last-child{margin-bottom:0}
.g-card h4{font-family:'Playfair Display',serif;font-size:15.5px;font-weight:700;margin-bottom:7px}
.g-card p{font-size:13.5px;color:rgba(255,255,255,.82)}
.g-link{display:inline-block;margin-top:8px;font-family:'Space Mono',monospace;font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:#fbbf24}
.g-src{font-family:'Space Mono',monospace;font-size:9px;color:rgba(255,255,255,.45);margin-top:8px}
.supply-wrap{background:var(--supply-bg);border:1px solid var(--supply-border);padding:28px 32px;margin:40px 0}
.supply-wrap h2{font-family:'Playfair Display',serif;font-size:22px;font-weight:700;color:#3b0764;margin-bottom:22px;display:flex;align-items:center;gap:10px}
.s-card{background:#fff;border-left:4px solid var(--supply-accent);padding:15px 18px;margin-bottom:15px}
.s-card h4{font-family:'Playfair Display',serif;font-size:15px;font-weight:700;color:#3b0764;margin-bottom:6px}
.s-card p{font-size:13.5px;color:#2e1065;line-height:1.65}
.s-card .s-meta{font-family:'Space Mono',monospace;font-size:9px;letter-spacing:.1em;color:var(--supply-accent);margin-top:8px;text-transform:uppercase}
.s-impact{background:#ede9fe;border-left:3px solid #7c3aed;padding:9px 13px;margin-top:10px}
.s-impact p{font-size:12.5px;color:#3b0764;margin:0}
.policy-wrap{background:var(--gold-light);border:1px solid #e8c44a;padding:28px 32px;margin:40px 0}
.policy-wrap h2{font-family:'Playfair Display',serif;font-size:22px;font-weight:700;color:#78350f;margin-bottom:22px;display:flex;align-items:center;gap:10px}
.p-card{background:#fff;border-left:4px solid #d97706;padding:16px 20px;margin-bottom:16px}
.p-card h4{font-family:'Playfair Display',serif;font-size:15.5px;font-weight:700;color:var(--navy);margin-bottom:8px}
.p-card p{font-size:13.5px;color:#2c1800;line-height:1.65}.p-card p+p{margin-top:6px}
.p-status{font-family:'Space Mono',monospace;font-size:9px;letter-spacing:.1em;color:#92400e;margin-top:9px;text-transform:uppercase}
.p-alert{background:#fef3c7;border:1px solid #fcd34d;padding:9px 13px;margin-top:10px;font-size:13px;color:#78350f}
.ptable{width:100%;border-collapse:collapse;margin-top:22px;font-size:12.5px}
.ptable th{font-family:'Space Mono',monospace;font-size:9px;letter-spacing:.1em;text-transform:uppercase;background:var(--navy);color:#fff;padding:9px 11px;text-align:left}
.ptable td{padding:9px 11px;border-bottom:1px solid var(--rule);vertical-align:top;line-height:1.5}
.ptable tr:nth-child(even) td{background:rgba(255,255,255,.6)}
.src-index{margin:40px 0}
.src-index h2{font-family:'Playfair Display',serif;font-size:20px;font-weight:700;color:var(--navy);margin-bottom:14px;padding-bottom:10px;border-bottom:2.5px solid var(--navy);display:flex;align-items:center;gap:8px}
.itable{width:100%;border-collapse:collapse;font-size:12px}
.itable th{font-family:'Space Mono',monospace;font-size:9px;letter-spacing:.1em;text-transform:uppercase;background:var(--navy);color:#fff;padding:9px 11px;text-align:left}
.itable td{padding:8px 11px;border-bottom:1px solid var(--rule);vertical-align:top}
.itable tr.r-supply td{background:#f5f0ff}.itable tr.r-policy td{background:#fff8e1}
.itable tr.r-global td{background:#eef2ff}.itable tr.r-new td{background:var(--new-bg)}
.itable a{color:var(--navy);font-size:11px}
.divider{height:1px;background:var(--rule);margin:28px 0}
footer{border-top:2.5px solid var(--navy);padding-top:26px;margin-top:44px;font-family:'Space Mono',monospace;font-size:10px;color:var(--muted);line-height:1.9}
footer strong{color:var(--ink)}
"""

GOOGLE_FONTS = (
    "https://fonts.googleapis.com/css2?"
    "family=Playfair+Display:ital,wght@0,400;0,600;0,700;1,400"
    "&family=Source+Sans+3:wght@300;400;600;700"
    "&family=Space+Mono:wght@400;700&display=swap"
)


# ══════════════════════════════════════════════════════════════════════════════
# DATE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def coverage_window() -> tuple[datetime, datetime]:
    today = datetime.now()
    return today - timedelta(days=6), today


def fmt(dt: datetime, spec: str = "%d %B %Y") -> str:
    return dt.strftime(spec)


def make_output_path(end: datetime) -> Path:
    name = f"newsletter_SEA_electronics_{end.strftime('%d%b%Y').lower()}_EN.html"
    return OUTPUT_DIR / name


# ══════════════════════════════════════════════════════════════════════════════
# WEB SEARCH  —  Tavily API
# ══════════════════════════════════════════════════════════════════════════════

def _parse_published_date(raw: str) -> datetime | None:
    """Best-effort parse of Tavily published_date strings."""
    if not raw or raw == "n/d":
        return None
    for pattern in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d", "%d %b %Y",
                    "%B %d, %Y", "%b %d, %Y"):
        try:
            dt = datetime.strptime(raw.strip()[:19] if "T" in raw else raw.strip(), pattern)
            return dt
        except ValueError:
            continue
    return None


def tavily_search(query: str, days_back: int = 7) -> str:
    """Run one Tavily search; returns a formatted text block.

    Results with a published_date older than ``days_back`` are discarded
    so that the newsletter only contains news from the past week.
    """
    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": "advanced",
        "max_results": SEARCH_N,
        "include_answer": True,
        "include_raw_content": False,
        "days": days_back,
    }
    try:
        r = requests.post("https://api.tavily.com/search", json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning(f"    ⚠ Search failed: {exc}")
        return f"[Search unavailable for: {query}]"

    cutoff = datetime.now() - timedelta(days=days_back)

    lines = []
    if data.get("answer"):
        lines.append(f"SUMMARY: {data['answer']}")
    skipped = 0
    for item in data.get("results", []):
        pub_raw = item.get("published_date") or "n/d"
        pub_dt  = _parse_published_date(pub_raw)
        if pub_dt and pub_dt < cutoff:
            skipped += 1
            continue
        title = item.get("title", "(no title)")
        url   = item.get("url", "")
        body  = item.get("content", "")[:480]
        lines.append(f"• [{pub_raw}] {title}\n  URL: {url}\n  {body}")

    if skipped:
        log.info(f"    ↳ filtered out {skipped} result(s) older than {days_back} days")

    return "\n".join(lines) if lines else "[No results]"


def build_queries(start: datetime, end: datetime) -> list[dict]:
    my = end.strftime("%B %Y")
    return [
        # Round 1 — Home Appliance Manufacturing
        {"r": 1, "label": "Home Appliance SG/MY/ID factories",
         "q": f"home appliance factory Singapore Malaysia Indonesia {my}"},
        {"r": 1, "label": "Home Appliance TH/VN/PH factories",
         "q": f"home appliance factory Thailand Vietnam Philippines {my}"},
        {"r": 1, "label": "Samsung LG Panasonic Daikin SEA appliance",
         "q": f"Samsung LG Panasonic Sharp Hitachi Daikin Carrier appliance factory Southeast Asia {my}"},
        {"r": 1, "label": "Haier Hisense TCL Midea SEA factory",
         "q": f"Haier Hisense TCL Midea GREE home appliance factory Southeast Asia {my}"},
        {"r": 1, "label": "Electrolux Dyson Bosch SEA",
         "q": f"Electrolux Whirlpool Dyson Bosch Philips home appliance Southeast Asia {my}"},

        # Round 2 — Smartphone Launches
        {"r": 2, "label": "Smartphone launches MY/SG/ID",
         "q": f"smartphone launch Malaysia Singapore Indonesia {my} price specs"},
        {"r": 2, "label": "Smartphone launches TH/VN/PH",
         "q": f"smartphone launch Thailand Vietnam Philippines {my} price specs"},
        {"r": 2, "label": "Apple Samsung Huawei OPPO Xiaomi vivo HONOR SEA",
         "q": f"Apple Samsung Huawei OPPO Xiaomi vivo HONOR iQOO phone launch Southeast Asia {my}"},
        {"r": 2, "label": "realme Motorola Nothing Tecno Infinix SEA",
         "q": f"realme OnePlus Motorola Nothing Tecno Infinix itel phone launch Southeast Asia {my}"},
        {"r": 2, "label": "Foldable flagship SEA debut",
         "q": f"foldable flagship phone launch Southeast Asia premiere {my}"},

        # Round 3 — OEM / EMS
        {"r": 3, "label": "Foxconn Pegatron Jabil EMS SEA",
         "q": f"Foxconn Pegatron Wistron Jabil Flex Celestica factory Vietnam Malaysia Thailand {my}"},
        {"r": 3, "label": "Luxshare BYD Goertek Chinese EMS Vietnam",
         "q": f"Luxshare BYD Goertek AAC Lingyi Changying Foxlink factory Vietnam Southeast Asia {my}"},
        {"r": 3, "label": "VS Industry Nationgate Inari Hana MY EMS",
         "q": f"VS Industry Nationgate Inari Amertron UWC Hana Microelectronics earnings Malaysia {my}"},
        {"r": 3, "label": "Cal-Comp Fabrinet Venture Hi-P SEA EMS",
         "q": f"Cal-Comp Fabrinet Venture Corporation Hi-P electronics manufacturing Southeast Asia {my}"},

        # Round 4 — Core Component Supply Chain
        {"r": 4, "label": "PCB FPC Vietnam Malaysia",
         "q": f"PCB FPC printed circuit board factory Vietnam Malaysia consumer electronics {my}"},
        {"r": 4, "label": "Display panels SEA Samsung BOE LG",
         "q": f"Samsung Display LG Display BOE CSOT OLED display panel Vietnam Southeast Asia {my}"},
        {"r": 4, "label": "Camera modules LG Innotek Largan Sunny Optical",
         "q": f"LG Innotek Largan Sunny Optical camera module factory Vietnam Southeast Asia {my}"},
        {"r": 4, "label": "MLCC passive Yageo Murata TDK SEA",
         "q": f"Yageo Murata TDK Samsung Electro-Mechanics MLCC passive component Malaysia Thailand Philippines {my}"},
        {"r": 4, "label": "Battery ATL Amperex Sunwoda SEA",
         "q": f"ATL Amperex Sunwoda Desay battery cell factory Malaysia Vietnam Southeast Asia {my}"},
        {"r": 4, "label": "Compressors Kulthorn Nidec motor SEA",
         "q": f"Kulthorn Nidec Welling Embraco compressor motor home appliance Thailand Vietnam {my}"},

        # Round 5 — Brand Structure
        {"r": 5, "label": "Brand merger acquisition SEA",
         "q": f"smartphone brand merger acquisition restructure distributor Southeast Asia {my}"},

        # Round 6 — Policy & Regulatory
        {"r": 6, "label": "Indonesia TKDN ecommerce policy",
         "q": f"Indonesia TKDN electronics ecommerce regulation policy {my}"},
        {"r": 6, "label": "Malaysia SIRIM tax incentive",
         "q": f"Malaysia consumer electronics policy SIRIM MCMC regulation tax MIDA {my}"},
        {"r": 6, "label": "Singapore IMDA CSA regulation",
         "q": f"Singapore IMDA CSA consumer device cybersecurity certification {my}"},
        {"r": 6, "label": "Vietnam Thailand Philippines CE policy",
         "q": f"Vietnam Thailand Philippines consumer electronics import tariff regulation {my}"},

        # Round 7 — Global Context
        {"r": 7, "label": "China+1 supply chain SEA",
         "q": f"consumer electronics supply chain China plus one Southeast Asia manufacturing {my}"},
        {"r": 7, "label": "SEA smartphone market share",
         "q": f"smartphone market share Southeast Asia {my} IDC Canalys Omdia"},
        {"r": 7, "label": "Global brand shifts affecting SEA",
         "q": f"consumer electronics brand globalisation Southeast Asia Japanese Korean Chinese exit enter {my}"},
    ]


def run_all_searches(start: datetime, end: datetime) -> str:
    queries = build_queries(start, end)
    blocks: list[str] = []
    log.info(f"Running {len(queries)} searches via Tavily...")
    for i, item in enumerate(queries, 1):
        log.info(f"  [{i:02d}/{len(queries)}] R{item['r']} · {item['label']}")
        result = tavily_search(item["q"], days_back=7)
        blocks.append(
            f"══ ROUND {item['r']} · {item['label']} ══\n"
            f"Query: {item['q']}\n\n{result}"
        )
        time.sleep(0.35)
    log.info("✓ All searches complete")
    sep = "\n\n" + "─" * 70 + "\n\n"
    return sep.join(blocks)


# ══════════════════════════════════════════════════════════════════════════════
# GENERATION PROMPTS
# (model generates BODY HTML only — Python wraps with full doc + CSS)
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = (
    "You are a professional Southeast Asia consumer electronics and "
    "smartphone industry intelligence analyst. You write in direct, "
    "supply-chain-practitioner English. "
    "Output ONLY raw HTML elements — no <!DOCTYPE>, no <html>, no <head>, "
    "no <body> tags, no CSS, no markdown fences. "
    "Start your output directly with the first HTML element."
)

USER_PROMPT = """\
Generate the BODY CONTENT of a weekly SEA consumer electronics newsletter.
Use ONLY the news from the search results below.
Do NOT invent facts. If a section has no relevant results, write:
<p class="no-news">No significant developments reported this week.</p>

IMPORTANT: Only include news published within the coverage period below.
Discard any article whose date falls outside {start_date} – {end_date}.
If an article has no date, include it only if the content clearly refers to this week's events.

COVERAGE : {start_date} – {end_date}
COMPILED  : {end_date}

═══════════════════════════════════════
SEARCH RESULTS (sole content source)
═══════════════════════════════════════
{search_results}

═══════════════════════════════════════
OUTPUT ORDER (HTML elements only, no wrapping tags)
═══════════════════════════════════════

── 1. MASTHEAD ──────────────────────────────────────────────
<div class="masthead"><div class="wrap">
  <div class="mast-eyebrow">Southeast Asia · Consumer Electronics &amp; Smartphone Intelligence</div>
  <div class="mast-title">Industry Intelligence<br><em>Weekly Briefing</em></div>
  <div class="mast-flags">🇸🇬 🇲🇾 🇮🇩 🇹🇭 🇻🇳 🇵🇭 🇲🇲 🇰🇭 🇱🇦 🇧🇳 🇹🇱</div>
  <div class="mast-tags"><!-- 6 .mast-tag spans: Smartphones | Home Appliances | OEM/EMS | Supply Chain | Policy & Regulation | Market Intelligence --></div>
  <div class="mast-meta"><!-- 📅 coverage period | 🗓 compiled date | 🎯 audience --></div>
</div></div>

── 2. KEY HIGHLIGHTS ────────────────────────────────────────
<div class="highlights"><div class="wrap">
  <div class="highlights-label">⚡ Key Highlights — This Week</div>
  <div class="hl-grid"><!-- 4 × .hl-item with .hl-num (1–4) + .hl-text (≤20 words each) --></div>
</div></div>

── 3. GLOBAL INDUSTRY SHIFTS (.global-wrap, navy bg) ────────
Inside .wrap. Only items with a named direct SEA-country impact.
Each: .g-card > h4 + <p> + .g-link (→ downstream SEA impact) + .g-src

── 4. CORE COMPONENT SUPPLY CHAIN UPDATE (.supply-wrap) ─────
Each: .s-card > h4 + <p> + .s-meta (📍 location · status) + .s-impact > <p>
Sub-sections where newsworthy: PCB/FPC · Display · Camera · Battery ·
Passive · Cables · LED · Compressors/Motors · Touch/Glass · Steel

── 5. POLICY FOCUS (.policy-wrap, gold bg) ──────────────────
Each country with news: .p-card > h4 + <p>+ + .p-status + optional .p-alert
End with .ptable: Market | Policy | CE Impact | Effective Date

── 6–12. COUNTRY SECTIONS ───────────────────────────────────
For each: .section-rule (with .section-icon flag + h2 name + .section-sub)
then .card items. Use class="breaking" + .breaking-badge for top 2–3 stories.

6. 🇲🇾 Malaysia   — Manufacturing · Smartphones · Market Data · Policy
7. 🇸🇬 Singapore  — Flagship Launches · Home Appliances · Events
8. 🇮🇩 Indonesia  — Smartphones · TKDN · Home Appliances
9. 🇹🇭 Thailand   — Manufacturing · Smartphones
10. 🇻🇳 Vietnam    — Manufacturing & Supply Chain · Smartphones
11. 🇵🇭 Philippines — Smartphone Launches · Consumer Electronics
12. 🌏 Other SEA  — Myanmar/Cambodia/Laos/Brunei/Timor-Leste (if newsworthy)

── EVERY .card MUST HAVE ────────────────────────────────────
.card-header: .btag (colour-coded pill, e.g. class="btag b-samsung") + h3 (10-20 words)
2 <p> paragraphs: core facts (price/date/figure) → context
.impact: .impact-label + <p> — label must be one of:
  "Sourcing Implication" / "Market Signal" / "Strategic Read" /
  "Brand Watch" / "Compliance Alert"
.src: date · media · <a href="REAL-URL-FROM-SEARCH">source name</a>

Brand tag classes (use exact names):
b-samsung b-apple b-huawei b-oppo b-xiaomi b-vivo b-honor b-realme
b-iqoo b-transsion b-motorola b-dyson b-panasonic b-hisense b-haier
b-tcl b-policy b-event b-supply b-data b-ems

── 13. SOURCE INDEX (.src-index) ────────────────────────────
.itable: No. | Market (flag emoji) | Story Topic | Source Media | Date
Row classes: r-global r-supply r-policy r-new
Every news item in the newsletter must have a row.

── 14. FOOTER ───────────────────────────────────────────────
<footer> with publication name, period, compiled date, disclaimer, source list.
End with </div><!-- /wrap --> after footer.

Output ONLY the HTML elements above. No markdown. No explanations.
"""


# ══════════════════════════════════════════════════════════════════════════════
# HTML DOCUMENT WRAPPER
# ══════════════════════════════════════════════════════════════════════════════

def wrap_html(body: str, start: datetime, end: datetime) -> str:
    """Inject body HTML into a complete document with embedded CSS and fonts."""
    title = f"SEA Consumer Electronics Intelligence | {fmt(start, '%d')}–{fmt(end, '%d %b %Y')}"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<link href="{GOOGLE_FONTS}" rel="stylesheet">
<style>
{CSS}
</style>
</head>
<body>
{body}
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# LLM BACKENDS
# ══════════════════════════════════════════════════════════════════════════════

def _stream_to_str(stream_iter) -> str:
    chunks: list[str] = []
    for text in stream_iter:
        chunks.append(text)
        print(text, end="", flush=True)
    print()
    return "".join(chunks)


def generate_body_deepseek(messages: list[dict]) -> str:
    """Call DeepSeek chat API (OpenAI-compatible) with streaming."""
    try:
        from openai import OpenAI
    except ImportError:
        log.error("openai package missing — run: pip install -r requirements.txt")
        sys.exit(1)

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
    chunks: list[str] = []

    with client.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=MAX_TOKENS,
        temperature=0.2,
        stream=True,
    ) as stream:
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                chunks.append(delta)
                print(delta, end="", flush=True)

    print()
    return "".join(chunks)


def generate_body_anthropic(messages: list[dict]) -> str:
    """Call Anthropic Claude API with streaming."""
    try:
        import anthropic as ant
    except ImportError:
        log.error("anthropic package missing — run: pip install -r requirements.txt")
        sys.exit(1)

    client = ant.Anthropic(api_key=ANTHROPIC_API_KEY)
    chunks: list[str] = []

    # Convert OpenAI-style messages to Anthropic format
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    user_messages = [m for m in messages if m["role"] != "system"]

    with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=user_messages,
    ) as stream:
        for text in stream.text_stream:
            chunks.append(text)
            print(text, end="", flush=True)

    print()
    return "".join(chunks)


def generate_body(search_results: str, start: datetime, end: datetime) -> str:
    """
    Ask the LLM to produce HTML body content only.
    If the response appears truncated, request a continuation (one retry).
    """
    user_content = USER_PROMPT.format(
        start_date=fmt(start),
        end_date=fmt(end),
        search_results=search_results,
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": user_content},
    ]

    log.info(f"Calling {PROVIDER.upper()} ({MODEL}) — streaming body HTML...\n" + "─" * 60)

    if PROVIDER == "anthropic":
        body = generate_body_anthropic(messages)
    else:
        body = generate_body_deepseek(messages)

    log.info("─" * 60)

    # ── Truncation check & one continuation pass ──────────────────────────────
    if not body.rstrip().endswith("</html>") and "</footer>" not in body:
        log.warning("Output appears truncated — requesting continuation...")
        cont_messages = messages + [
            {"role": "assistant", "content": body},
            {"role": "user", "content":
             "The HTML was cut off. Continue from exactly where you stopped. "
             "Output only the remaining HTML elements — no repetition."},
        ]
        log.info("Continuation stream:\n" + "─" * 60)
        if PROVIDER == "anthropic":
            continuation = generate_body_anthropic(cont_messages)
        else:
            continuation = generate_body_deepseek(cont_messages)
        log.info("─" * 60)
        body = body + continuation

    # Strip stray markdown fences if model added them
    body = body.strip()
    if body.startswith("```"):
        body = body.split("```", 2)[-1] if body.count("```") >= 2 else body[3:]
        if body.startswith("html\n"):
            body = body[5:]
    if body.endswith("```"):
        body = body[: body.rfind("```")]

    return body.strip()


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL DELIVERY
# ══════════════════════════════════════════════════════════════════════════════

def send_email(html: str, out_path: Path, start: datetime, end: datetime) -> None:
    """
    Send the newsletter as an HTML email with the .html file attached.

    Requires EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO to be set.
    If any are missing the function logs a warning and returns silently
    so that a missing email config never blocks newsletter generation.

    Gmail users: create a 16-character App Password at
    https://myaccount.google.com/apppasswords  (2FA must be enabled first).
    """
    if not all([EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO]):
        log.info(
            "Email skipped — set EMAIL_FROM / EMAIL_PASSWORD / EMAIL_TO "
            "to enable delivery."
        )
        return

    subject = (
        f"SEA Electronics Intelligence Weekly | "
        f"{fmt(start, '%d')}–{fmt(end, '%d %b %Y')}"
    )
    recipients = [r.strip() for r in EMAIL_TO.split(",") if r.strip()]
    cc_list    = [r.strip() for r in EMAIL_CC.split(",") if r.strip()]

    # ── Build message ─────────────────────────────────────────────────────────
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = ", ".join(recipients)
    if cc_list:
        msg["Cc"]  = ", ".join(cc_list)

    # Part 1: alternative (plain-text + HTML body)
    alt = MIMEMultipart("alternative")

    plain_body = (
        f"SEA Consumer Electronics Intelligence Weekly\n"
        f"Coverage: {fmt(start)} – {fmt(end)}\n\n"
        f"Please view this email in an HTML-capable client,\n"
        f"or open the attached HTML file in a browser.\n\n"
        f"Attachment: {out_path.name}"
    )
    alt.attach(MIMEText(plain_body, "plain", "utf-8"))
    alt.attach(MIMEText(html,       "html",  "utf-8"))
    msg.attach(alt)

    # Part 2: HTML file as attachment (opens perfectly in any browser)
    attachment = MIMEBase("text", "html", charset="utf-8")
    attachment.set_payload(html.encode("utf-8"))
    encoders.encode_base64(attachment)
    attachment.add_header(
        "Content-Disposition", "attachment", filename=out_path.name
    )
    msg.attach(attachment)

    # ── Send ─────────────────────────────────────────────────────────────────
    all_recipients = recipients + cc_list
    try:
        if EMAIL_SMTP_PORT == 465:
            # SSL (recommended for Gmail)
            with smtplib.SMTP_SSL(EMAIL_SMTP_HOST, EMAIL_SMTP_PORT) as server:
                server.login(EMAIL_FROM, EMAIL_PASSWORD)
                server.sendmail(EMAIL_FROM, all_recipients, msg.as_bytes())
        else:
            # STARTTLS (port 587)
            with smtplib.SMTP(EMAIL_SMTP_HOST, EMAIL_SMTP_PORT) as server:
                server.ehlo()
                server.starttls()
                server.login(EMAIL_FROM, EMAIL_PASSWORD)
                server.sendmail(EMAIL_FROM, all_recipients, msg.as_bytes())

        log.info(f"✓ Email sent → {', '.join(all_recipients)}")

    except smtplib.SMTPAuthenticationError:
        log.error(
            "✗ Email authentication failed.\n"
            "  Gmail users: make sure you are using a 16-character App Password,\n"
            "  not your regular Gmail password.\n"
            "  Generate one at: https://myaccount.google.com/apppasswords"
        )
    except Exception as exc:
        log.error(f"✗ Email sending failed: {exc}")
        # Newsletter file is already saved — do not abort the process.


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

def validate_config() -> None:
    errors: list[str] = []
    if not TAVILY_API_KEY:
        errors.append("TAVILY_API_KEY missing  →  free key at https://app.tavily.com")
    if PROVIDER == "deepseek" and not DEEPSEEK_API_KEY:
        errors.append("DEEPSEEK_API_KEY missing  →  https://platform.deepseek.com/api_keys")
    if PROVIDER == "anthropic" and not ANTHROPIC_API_KEY:
        errors.append("ANTHROPIC_API_KEY missing  →  https://console.anthropic.com")
    if errors:
        log.error("\nConfiguration errors:")
        for e in errors:
            log.error(f"  ✗ {e}")
        log.error("\nCopy .env.example → .env and fill in your keys.\n")
        sys.exit(1)
    log.info(f"✓ Config OK  provider={PROVIDER}  model={MODEL}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="SEA Electronics Newsletter Generator")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run searches only — skip LLM call")
    parser.add_argument("--save-search", metavar="FILE",
                        help="Save raw search results to FILE")
    parser.add_argument("--search-cache", metavar="FILE",
                        help="Load search results from FILE instead of querying Tavily")
    args = parser.parse_args()

    log.info("══ SEA Consumer Electronics Newsletter Generator ══")
    validate_config()

    start, end = coverage_window()
    out_path   = make_output_path(end)
    log.info(f"Coverage : {fmt(start)} – {fmt(end)}")
    log.info(f"Output   : {out_path}")

    # ── Step 1/4: searches ───────────────────────────────────────────────────
    if args.search_cache:
        cache = Path(args.search_cache)
        log.info(f"\n[1/4] Loading search cache from {cache} ...")
        search_results = cache.read_text(encoding="utf-8")
    else:
        log.info("\n[1/4] Running web searches...")
        search_results = run_all_searches(start, end)

    log.info(f"✓ Search data: {len(search_results):,} chars")

    if args.save_search:
        Path(args.save_search).write_text(search_results, encoding="utf-8")
        log.info(f"✓ Search results saved → {args.save_search}")

    if args.dry_run:
        log.info("\n--dry-run: skipping LLM generation. Done.")
        return

    # ── Step 2: generate ─────────────────────────────────────────────────────
    log.info("\n[2/4] Generating newsletter body...")
    body = generate_body(search_results, start, end)

    # ── Step 3: assemble & save ───────────────────────────────────────────────
    log.info("\n[3/4] Assembling HTML document and saving...")
    html = wrap_html(body, start, end)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    kb = len(html) // 1024
    log.info(f"\n{'═'*50}")
    log.info("✓ Newsletter generated successfully!")
    log.info(f"  File : {out_path}")
    log.info(f"  Size : {len(html):,} bytes ({kb} KB)")
    log.info(f"{'═'*50}\n")

    # ── Step 4: send email ────────────────────────────────────────────────────
    log.info("[4/4] Sending newsletter by email...")
    send_email(html, out_path, start, end)


if __name__ == "__main__":
    main()
