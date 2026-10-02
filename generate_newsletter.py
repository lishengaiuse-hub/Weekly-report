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

# Force UTF-8 stdout on Windows to avoid GBK encoding errors with emoji
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
    sys.stderr = open(sys.stderr.fileno(), mode="w", encoding="utf-8", closefd=False)

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

PROVIDER          = os.getenv("PROVIDER", "deepseek").lower()
DEEPSEEK_API_KEY  = os.getenv("DEEPSEEK_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TAVILY_API_KEY    = os.getenv("TAVILY_API_KEY", "")

_DEFAULT_MODELS = {"deepseek": "deepseek-chat", "anthropic": "claude-opus-4-7"}
MODEL      = os.getenv("MODEL") or _DEFAULT_MODELS.get(PROVIDER, "deepseek-chat")
MAX_TOKENS = int(os.getenv("MAX_TOKENS") or "12000")  # "or" handles empty string
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
        logging.FileHandler(
            Path(__file__).parent / "newsletter_generator.log", encoding="utf-8"
        ),
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
.b-tcl{background:#e31837}.b-tata{background:#486aae}.b-dixon{background:#1a6b3c}.b-voltas{background:#e65100}
.b-foxconn{background:#2d5f2e}.b-luxshare{background:#1a237e}.b-goertek{background:#00695c}
.b-pegatron{background:#4a148c}.b-murata{background:#1b5e20}.b-boe{background:#004d99}.b-jabil{background:#006838}
.b-avc{background:#0d47a1}.b-salcomp{background:#2e7d32}.b-corning{background:#c62828}
.b-catcher{background:#4e342e}.b-everwin{background:#37474f}.b-aac{background:#6a1b9a}
.b-radiant{background:#00838f}.b-coretronic{background:#1565c0}.b-amphenol{background:#ad1457}
.b-molex{background:#e65100}.b-nitto{background:#283593}.b-biel{background:#558b2f}
.b-changhong{background:#d32f2f}.b-huaqin{background:#00695c}.b-wingtech{background:#4527a0}
.b-bluestar{background:#1565c0}.b-inari{background:#00838f}.b-uwc{background:#4e342e}
.b-nidec{background:#2e7d32}.b-victorygiant{background:#1b5e20}.b-click{background:#37474f}
.b-suntak{background:#0d47a1}.b-shenghong{background:#1a237e}.b-kinwong{background:#004d40}.b-wus{background:#33691e}
.b-policy{background:#374151}.b-event{background:#065f46}
.b-supply{background:#5b21b6}.b-data{background:#0369a1}.b-ems{background:#5b21b6}
.card h3{font-family:'Playfair Display',serif;font-size:17.5px;font-weight:700;line-height:1.3;color:var(--ink)}
.card p{font-size:14px;color:#2c2620;line-height:1.7;margin-top:10px}
.card p+p{margin-top:8px}
.impact{background:var(--yellow-bg);border-left:3px solid var(--gold);padding:11px 15px;margin-top:16px}
.impact-label{font-family:'Space Mono',monospace;font-size:8.5px;font-weight:700;letter-spacing:.16em;text-transform:uppercase;color:var(--gold);margin-bottom:5px}
.impact p{font-size:13px;color:#3d2c00;line-height:1.6;margin-top:0}
.src{font-family:'Space Mono',monospace;font-size:9.5px;color:var(--muted);margin-top:14px;padding-top:10px;border-top:1px solid var(--rule);line-height:1.6}
.src a{color:var(--navy);text-decoration:none}.src a:hover{text-decoration:underline}
.no-news{display:none}
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
.s-src{font-family:'Space Mono',monospace;font-size:9px;color:var(--supply-accent);margin-top:8px}
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
    name = f"newsletter_SEA_India_manufacturing_{end.strftime('%d%b%Y').lower()}_EN.html"
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
    week_str = f"{fmt(start, '%d %B')} to {fmt(end, '%d %B %Y')}"
    return [
        # ── Round 1 — Brand Manufacturer Factories (SEA) ─────────────────
        {"r": 1, "label": "Samsung LG factory SEA",
         "q": f"Samsung LG factory expansion investment Vietnam Thailand Malaysia Indonesia {week_str}"},
        {"r": 1, "label": "Haier Hisense TCL Midea factory SEA",
         "q": f"Haier Hisense TCL Midea GREE factory expansion capacity Southeast Asia {week_str}"},
        {"r": 1, "label": "TCL Changhong Thailand Chonburi factory",
         "q": f"TCL Smart Home Changhong factory Chonburi Thailand refrigerator freezer battery {week_str}"},
        {"r": 1, "label": "Panasonic Daikin Electrolux appliance factory SEA",
         "q": f"Panasonic Daikin Sharp Electrolux Dyson appliance factory investment Southeast Asia {week_str}"},
        {"r": 1, "label": "Xiaomi OPPO vivo factory SEA",
         "q": f"Xiaomi OPPO vivo Realme factory manufacturing expansion Southeast Asia India {week_str}"},

        # ── Round 2 — Brand Manufacturer Factories (India) ───────────────
        {"r": 2, "label": "Samsung LG factory India",
         "q": f"Samsung LG factory expansion investment India Chennai Sri City {week_str}"},
        {"r": 2, "label": "Haier India third factory investment",
         "q": f"Haier India new factory investment third manufacturing facility expansion {week_str}"},
        {"r": 2, "label": "Voltas Daikin Blue Star appliance India",
         "q": f"Voltas Daikin Blue Star Godrej appliance factory India expansion {week_str}"},
        {"r": 2, "label": "Apple iPhone India manufacturing",
         "q": f"Apple iPhone India manufacturing export Foxconn Tata Pegatron {week_str}"},
        {"r": 2, "label": "vivo Dixon India JV",
         "q": f"vivo Dixon Technologies India joint venture smartphone factory capacity {week_str}"},

        # ── Round 3 — Tier-1 EMS/OEM (global players in SEA) ────────────
        {"r": 3, "label": "Foxconn Vietnam expansion",
         "q": f"Foxconn factory expansion Vietnam investment Bac Ninh Quang Ninh {week_str}"},
        {"r": 3, "label": "Foxconn India display Chennai",
         "q": f"Foxconn India factory display module Chennai investment {week_str}"},
        {"r": 3, "label": "Luxshare Goertek Vietnam factory",
         "q": f"Luxshare Goertek factory expansion Vietnam capacity production line {week_str}"},
        {"r": 3, "label": "Pegatron Jabil Flex SEA factory",
         "q": f"Pegatron Jabil Flex Celestica factory expansion Vietnam Malaysia Indonesia Batam {week_str}"},
        {"r": 3, "label": "Huaqin Wingtech ODM Vietnam Indonesia",
         "q": f"Huaqin Technology Wingtech factory Vietnam Indonesia expansion capacity {week_str}"},
        {"r": 3, "label": "Tata Dixon India EMS expansion",
         "q": f"Tata Electronics Dixon Technologies Hosur Noida factory expansion capacity {week_str}"},

        # ── Round 4 — Tier-1 EMS/OEM (SEA local players) ────────────────
        {"r": 4, "label": "VS Industry Nationgate Inari MY EMS",
         "q": f"VS Industry Nationgate Inari Amertron UWC factory expansion Malaysia {week_str}"},
        {"r": 4, "label": "Hana Cal-Comp Fabrinet Hi-P TH SG",
         "q": f"Hana Microelectronics Cal-Comp Fabrinet Hi-P Venture factory Thailand Singapore {week_str}"},

        # ── Round 5 — Core Components: PCB / Display / Camera ───────────
        {"r": 5, "label": "PCB factory Vietnam Thailand Wus Aoshikang Victory Giant",
         "q": f"PCB printed circuit board factory Vietnam Thailand Wus Aoshikang Victory Giant {week_str}"},
        {"r": 5, "label": "PCB Suntak Shenghong Kinwong SEA",
         "q": f"Suntak Chongda Shenghong Kinwong PCB factory Vietnam Thailand Southeast Asia expansion {week_str}"},
        {"r": 5, "label": "Samsung BOE display factory Vietnam",
         "q": f"Samsung Display LG Display BOE CSOT OLED display factory Vietnam India {week_str}"},
        {"r": 5, "label": "Camera module LG Innotek Sunny Optical",
         "q": f"LG Innotek Sunny Optical Largan camera module factory Vietnam {week_str}"},

        # ── Round 6 — Core Components: MLCC / Battery / Connector ───────
        {"r": 6, "label": "MLCC Murata Taiyo Yuden SEA India",
         "q": f"Murata Taiyo Yuden Samsung Electro-Mechanics MLCC factory Thailand Malaysia India {week_str}"},
        {"r": 6, "label": "Battery cell factory SEA India",
         "q": f"ATL Sunwoda Desay BYD battery cell factory Vietnam Thailand India {week_str}"},
        {"r": 6, "label": "Connector Amphenol Molex Lite-On SEA India",
         "q": f"Amphenol Molex Lite-On TD Connex connector factory Vietnam India expansion {week_str}"},
        {"r": 6, "label": "Compressor motor Kulthorn Nidec SEA",
         "q": f"Kulthorn Nidec Welling Embraco compressor motor factory Thailand Vietnam {week_str}"},

        # ── Round 7 — Thermal / Cooling / Power Supply ──────────────────
        {"r": 7, "label": "AVC Auras thermal cooling Vietnam",
         "q": f"AVC Asia Vital Components Auras cooling fan heat sink factory Vietnam {week_str}"},
        {"r": 7, "label": "Delta thermal cooling Thailand India",
         "q": f"Delta Electronics cooling thermal management factory Thailand India {week_str}"},
        {"r": 7, "label": "Salcomp charger adapter India",
         "q": f"Salcomp charger power adapter factory India expansion {week_str}"},

        # ── Round 8 — Metal Casing / Glass Cover / Precision Parts ──────
        {"r": 8, "label": "BYD Electronic Catcher casing Vietnam",
         "q": f"BYD Electronic Catcher Technology Everwin metal casing factory Vietnam expansion {week_str}"},
        {"r": 8, "label": "Corning BIEL glass cover India Vietnam",
         "q": f"Corning Gorilla Glass BIEL Crystal cover glass factory India Vietnam {week_str}"},

        # ── Round 9 — Backlight / LED / Acoustic / Adhesive ────────────
        {"r": 9, "label": "Radiant Coretronic backlight Vietnam",
         "q": f"Radiant Coretronic GLT Longli backlight LED module factory Vietnam {week_str}"},
        {"r": 9, "label": "AAC Technologies acoustic Vietnam",
         "q": f"AAC Technologies speaker acoustic component factory Vietnam expansion {week_str}"},
        {"r": 9, "label": "Nitto Denko adhesive optical film SEA India",
         "q": f"Nitto Denko adhesive tape optical film factory Vietnam India Malaysia {week_str}"},

        # ── Round 10 — Wire Harness / Insulation / Stamping ─────────────
        {"r": 10, "label": "Motherson Yazaki wire harness India",
         "q": f"Motherson Sumi Yazaki wire harness cable factory India expansion {week_str}"},
        {"r": 10, "label": "SRF Armacell insulation India",
         "q": f"SRF refrigerant Armacell insulation material factory India expansion {week_str}"},

        # ── Round 11 — Chinese Supplier Migration to SEA ────────────────
        {"r": 11, "label": "Chinese electronics supplier Vietnam factory",
         "q": f"Chinese electronics supplier factory Vietnam relocation expansion investment {week_str}"},
        {"r": 11, "label": "Chinese supplier Thailand Indonesia factory",
         "q": f"Chinese manufacturer factory Thailand Indonesia electronics component investment {week_str}"},
        {"r": 11, "label": "DBG Lingyi BYD Electronic SEA factory",
         "q": f"DBG Technology Lingyi iTech BYD Electronic factory Southeast Asia Batam {week_str}"},

        # ── Round 12 — Policy & Regulatory ──────────────────────────────
        {"r": 12, "label": "Indonesia TKDN electronics policy",
         "q": f"Indonesia TKDN electronics regulation policy {week_str}"},
        {"r": 12, "label": "Malaysia MIDA electronics incentive",
         "q": f"Malaysia MIDA electronics investment incentive SIRIM MCMC {week_str}"},
        {"r": 12, "label": "Singapore IMDA regulation",
         "q": f"Singapore IMDA CSA cybersecurity certification electronics {week_str}"},
        {"r": 12, "label": "Vietnam FDI electronics policy",
         "q": f"Vietnam FDI electronics manufacturing policy regulation {week_str}"},
        {"r": 12, "label": "Thailand BOI EEC electronics",
         "q": f"Thailand BOI EEC electronics factory investment incentive {week_str}"},
        {"r": 12, "label": "India PLI BIS ECMS policy",
         "q": f"India PLI ECMS BIS electronics manufacturing regulation {week_str}"},

        # ── Round 13 — Global / Supply Chain Intelligence ───────────────
        {"r": 13, "label": "China+1 supply chain SEA India",
         "q": f"China plus one supply chain diversification Southeast Asia India electronics {week_str}"},
        {"r": 13, "label": "SEA India factory investment data",
         "q": f"Southeast Asia India electronics factory investment FDI data {week_str}"},
        {"r": 13, "label": "US tariff impact SEA India manufacturing",
         "q": f"US tariff Section 301 impact Southeast Asia India electronics manufacturing {week_str}"},
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
    "You are a professional Southeast Asia and India manufacturing "
    "industry intelligence analyst. You write in direct, "
    "supply-chain-practitioner English. "
    "Output ONLY raw HTML elements — no <!DOCTYPE>, no <html>, no <head>, "
    "no <body> tags, no CSS, no markdown fences. "
    "Start your output directly with the first HTML element."
)

USER_PROMPT = """\
Generate the BODY CONTENT of a weekly SEA consumer electronics newsletter.
Use ONLY the news from the search results below.
Do NOT invent facts. If a section has no relevant results, OMIT that
section entirely — do not output it at all.

IMPORTANT — DATE FILTERING (strictly enforce):
1. Only include news where the EVENT ITSELF occurred within {start_date} – {end_date}.
2. An article may be recently published but describe old events (e.g. a 2026
   article summarising a policy announced in 2025). Such articles must be EXCLUDED
   because the underlying event is stale — the article publication date alone is
   NOT sufficient to qualify.
3. Check dates, years, and temporal language in each article. If the key facts
   (product launch, policy effective date, factory opening, deal signing) happened
   before {start_date}, discard it.
4. If an article has no date at all, include it only if the content clearly
   describes events from this specific week.

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
  <div class="mast-eyebrow">Southeast Asia &amp; India · Manufacturing Intelligence</div>
  <div class="mast-title">Southeast Asia &amp; India<br><em>Manufacturing Watch</em></div>
  <div class="mast-flags">🇸🇬 🇲🇾 🇮🇩 🇹🇭 🇻🇳 🇮🇳</div>
  <div class="mast-tags"><!-- 6 .mast-tag spans: Home Appliances | OEM/EMS | Supply Chain | Policy & Regulation | Market Intelligence | Manufacturing --></div>
  <div class="mast-meta"><!-- 📅 coverage period | 🗓 compiled date | 🎯 audience --></div>
</div></div>

── 2. KEY HIGHLIGHTS ────────────────────────────────────────
<div class="highlights"><div class="wrap">
  <div class="highlights-label">⚡ Key Highlights — This Week</div>
  <div class="hl-grid"><!-- 4 × .hl-item with .hl-num (1–4) + .hl-text (≤20 words each) --></div>
</div></div>

── 3. GLOBAL INDUSTRY SHIFTS (.global-wrap, navy bg) ────────
Inside .wrap. Only items with a named direct SEA-country impact.
Each: .g-card > h4 + <p> + .g-link (→ downstream SEA impact) + .g-src (SPECIFIC date · <a href="URL">media name</a>)

── 4. CORE COMPONENT SUPPLY CHAIN UPDATE (.supply-wrap) ─────
Each: .s-card > h4 + <p> + .s-meta (📍 location · status) + .s-impact > <p> + .s-src (SPECIFIC date · <a href="URL">media name</a>)
Sub-sections where newsworthy: PCB/FPC · Display · Camera · Battery ·
Passive · Cables · LED · Compressors/Motors · Touch/Glass · Steel

── 5. POLICY FOCUS (.policy-wrap, gold bg) ──────────────────
Each country with news: .p-card > h4 + <p>+ + .p-status + optional .p-alert
End with .ptable: Market | Policy | CE Impact | Effective Date

── 6–11. COUNTRY SECTIONS ───────────────────────────────────
For each: .section-rule (with .section-icon flag + h2 name + .section-sub)
then .card items. Use class="breaking" + .breaking-badge for top 2–3 stories.

EXCLUSION RULES (strictly enforce):
- DO NOT include product launch / smartphone launch news.
- DO NOT include home appliance sales events, warehouse sales, or retail promotions.
- DO NOT include Philippines, Cambodia, Myanmar, or Laos news.
- DO NOT include semiconductor fab, chip packaging, OSAT, memory chip, or semiconductor equipment news (e.g. Micron, Infineon, LAM Research, Amkor, TSMC).
- Focus ONLY on: manufacturing, factory investment, supply chain, OEM/EMS, and policy.

GEOGRAPHIC FILTER (strictly enforce):
- The EVENT itself must be physically located in or directly about
  Southeast Asia (Singapore, Malaysia, Indonesia, Thailand, Vietnam)
  or India.
- DO NOT include news where the event happens in Japan, Korea, China,
  Taiwan, US, or Europe, even if it has indirect supply-chain impact
  on SEA/India (e.g. a Japanese company's capex plan at HQ, a Korean
  company's pricing action, global market analysis).
- Exception: Section 3 "Global Industry Shifts" may include
  cross-border policy or trade actions (e.g. US tariffs) that
  DIRECTLY and specifically name SEA/India countries as targets.
- For Section 4 "Supply Chain Update", only include items where the
  factory, plant, or facility is IN Southeast Asia or India.

NO-DUPLICATION RULE (strictly enforce):
- Each news story must appear ONLY ONCE in the entire newsletter.
- If a story appears in "Core Component Supply Chain Update" (section 4),
  DO NOT repeat it in any country section (sections 6–11), and vice versa.
- Choose the MOST appropriate single section for each story.

6. 🇲🇾 Malaysia   — Manufacturing · OEM/EMS · Market Data · Policy
7. 🇸🇬 Singapore  — Manufacturing · Policy
8. 🇮🇩 Indonesia  — TKDN · Manufacturing
9. 🇹🇭 Thailand   — Manufacturing · Supply Chain
10. 🇻🇳 Vietnam    — Manufacturing & Supply Chain · OEM/EMS
11. 🇮🇳 India      — Manufacturing · PLI & Policy · Supply Chain · OEM/EMS

── EVERY .card MUST HAVE ────────────────────────────────────
.card-header: .btag (colour-coded pill, e.g. class="btag b-samsung") + h3 (10-20 words)
2 <p> paragraphs: core facts (price/date/figure) → context
.impact: .impact-label + <p> — label must be one of:
  "Sourcing Implication" / "Market Signal" / "Strategic Read" /
  "Brand Watch" / "Compliance Alert"
.src: SPECIFIC date (e.g. "28 Sep 2026", NOT just "2026") · media name · <a href="REAL-URL-FROM-SEARCH">source name</a>
  ── The date MUST be the article's publication date from the search result, as specific as possible.
  ── If only month/year is available, use that (e.g. "Sep 2026"). Never use just a year like "2026".
  ── The URL MUST be copied exactly from the search results — never fabricate URLs.

Brand tag classes (use exact names):
b-samsung b-apple b-huawei b-oppo b-xiaomi b-vivo b-honor b-realme
b-iqoo b-transsion b-motorola b-dyson b-panasonic b-hisense b-haier
b-tcl b-tata b-dixon b-voltas b-foxconn b-luxshare b-goertek
b-pegatron b-murata b-boe b-jabil b-avc b-salcomp b-corning
b-catcher b-everwin b-aac b-radiant b-coretronic b-amphenol
b-molex b-nitto b-biel b-changhong b-huaqin b-wingtech
b-bluestar b-inari b-uwc b-nidec b-victorygiant b-click
b-suntak b-shenghong b-kinwong b-wus
b-policy b-event b-supply b-data b-ems

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
    title = f"SEA & India Manufacturing Watch | {fmt(start, '%d')}–{fmt(end, '%d %b %Y')}"
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
# DATE VALIDATION PASS  — remove stale news items via a second LLM call
# ══════════════════════════════════════════════════════════════════════════════

VALIDATION_SYSTEM = (
    "You are a strict date-validation editor for a weekly newsletter. "
    "Your ONLY job is to remove news items whose underlying EVENT is outside "
    "the given coverage period. Output the cleaned HTML and nothing else."
)

VALIDATION_PROMPT = """\
Below is the HTML body of a weekly newsletter.
Coverage period: {start_date} – {end_date}

TASK — for EVERY news card (.card, .g-card, .s-card, .p-card) and every
row in the source index (.itable):

1. Read the card content and the .src date line. Identify when the
   EVENT ITSELF happened (not the article publication date).
2. REMOVE the card if ANY of these apply:
   a. The event happened BEFORE {start_date} (e.g. a policy signed
      months ago, a factory opened last quarter, an earnings report
      from a prior quarter).
   b. The source date (.src line) is before {start_date}.
   c. The card describes a general market overview, evergreen company
      profile, or product catalogue with no specific event this week.
   d. The card is about a home appliance sale, warehouse sale, retail
      promotion, product launch, or consumer event.
   e. The card is about Philippines, Cambodia, Myanmar, or Laos.
   f. The card is about semiconductor fab, chip packaging, OSAT, memory chips, or semiconductor equipment (e.g. Micron, Infineon, LAM Research, Amkor, TSMC).
   g. The event takes place in Japan, Korea, China, Taiwan, US, or Europe
      — not in Southeast Asia or India (exception: Section 301 or tariff
      actions in "Global Industry Shifts" that directly name SEA/India).
3. If a card only says a month/year (e.g. "July 2026") and that month
   ended before {start_date}, REMOVE it.
4. If you cannot determine ANY date at all AND the content reads like
   a general profile rather than breaking news, REMOVE it.
   Only KEEP dateless cards if the text explicitly describes a new
   development happening "this week" or "today".
5. Also remove the corresponding source-index row for every removed card.
6. After removing stale cards, if a section has no cards left, REMOVE
   that entire section from the output.
7. Update the KEY HIGHLIGHTS section to reflect only the remaining cards.
8. Re-number the source-index rows sequentially.
9. DEDUPLICATION: If the same news story appears in BOTH a thematic section
   (Global Industry Shifts, Supply Chain Update, Policy Focus) AND a country
   section, REMOVE the duplicate from the country section. Each story must
   appear only once.
10. SOURCE DATES: If any .src line shows only a year (e.g. "Date: 2026"),
    check the search result for a more specific date and update it.
    If no specific date is available, use the month and year at minimum.

Output ONLY the cleaned HTML body. No markdown fences. No explanations.

─── HTML BODY ───
{body}
"""


def validate_dates(body: str, start: datetime, end: datetime) -> str:
    """Run a second LLM pass to strip news items with stale event dates."""
    user_content = VALIDATION_PROMPT.format(
        start_date=fmt(start),
        end_date=fmt(end),
        body=body,
    )
    messages = [
        {"role": "system", "content": VALIDATION_SYSTEM},
        {"role": "user",   "content": user_content},
    ]

    log.info(f"Calling {PROVIDER.upper()} ({MODEL}) — date validation pass...\n" + "─" * 60)

    if PROVIDER == "anthropic":
        cleaned = generate_body_anthropic(messages)
    else:
        cleaned = generate_body_deepseek(messages)

    log.info("─" * 60)

    # Strip markdown fences if present
    cleaned = cleaned.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[-1] if cleaned.count("```") >= 2 else cleaned[3:]
        if cleaned.startswith("html\n"):
            cleaned = cleaned[5:]
    if cleaned.endswith("```"):
        cleaned = cleaned[: cleaned.rfind("```")]

    if len(cleaned) < len(body) * 0.5:
        log.warning("Validation removed >50%% of content — keeping original to be safe")
        return body

    return cleaned.strip()


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
        f"SEA & India Manufacturing Watch | "
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
        f"SEA & India Manufacturing Watch Weekly\n"
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

    # ── Step 1/5: searches ───────────────────────────────────────────────────
    if args.search_cache:
        cache = Path(args.search_cache)
        log.info(f"\n[1/5] Loading search cache from {cache} ...")
        search_results = cache.read_text(encoding="utf-8")
    else:
        log.info("\n[1/5] Running web searches...")
        search_results = run_all_searches(start, end)

    log.info(f"✓ Search data: {len(search_results):,} chars")

    if args.save_search:
        Path(args.save_search).write_text(search_results, encoding="utf-8")
        log.info(f"✓ Search results saved → {args.save_search}")

    if args.dry_run:
        log.info("\n--dry-run: skipping LLM generation. Done.")
        return

    # ── Step 2/5: generate ───────────────────────────────────────────────────
    log.info("\n[2/5] Generating newsletter body...")
    body = generate_body(search_results, start, end)

    # ── Step 3/5: validate dates ─────────────────────────────────────────────
    log.info("\n[3/5] Validating news dates (removing stale events)...")
    body = validate_dates(body, start, end)

    # ── Step 4/5: assemble & save ────────────────────────────────────────────
    log.info("\n[4/5] Assembling HTML document and saving...")
    html = wrap_html(body, start, end)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    kb = len(html) // 1024
    log.info(f"\n{'═'*50}")
    log.info("✓ Newsletter generated successfully!")
    log.info(f"  File : {out_path}")
    log.info(f"  Size : {len(html):,} bytes ({kb} KB)")
    log.info(f"{'═'*50}\n")

    # ── Step 5/5: send email ─────────────────────────────────────────────────
    log.info("[5/5] Sending newsletter by email...")
    send_email(html, out_path, start, end)


if __name__ == "__main__":
    main()
