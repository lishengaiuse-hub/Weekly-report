#!/usr/bin/env python3
"""
SEA Consumer Electronics Intelligence Newsletter Generator
=========================================================
Runs 7 rounds of web searches via Tavily, then calls either
DeepSeek, Anthropic Claude, or Google Gemini to write the
newsletter body, and wraps it into a fully-styled HTML file.

Providers
---------
  deepseek   — default; uses openai-compatible API at api.deepseek.com
  anthropic  — uses the anthropic SDK
  gemini     — uses Google Gemini via OpenAI-compatible endpoint

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
    import report_layout   # shared newspaper-style page layout (needs beautifulsoup4)
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
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
TAVILY_API_KEY    = os.getenv("TAVILY_API_KEY", "")

_DEFAULT_MODELS = {
    "deepseek": "deepseek-chat",
    "anthropic": "claude-opus-4-7",
    "gemini": "gemini-3.8-flash",
}
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
        body  = item.get("content", "")[:200]
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
        {"r": 3, "label": "Wistron Quanta Compal Inventec ODM Vietnam",
         "q": f"Wistron Quanta Computer Compal Inventec ODM factory Vietnam Thailand expansion {week_str}"},
        {"r": 3, "label": "Tata Dixon India EMS expansion",
         "q": f"Tata Electronics Dixon Technologies Hosur Noida factory expansion capacity {week_str}"},
        {"r": 3, "label": "Jabil Epack India EMS expansion",
         "q": f"Jabil Epack Durable Amber Enterprises India EMS contract manufacturing factory expansion PLI {week_str}"},
        {"r": 3, "label": "Longcheer Tinno Chinese ODM Vietnam India",
         "q": f"Longcheer Tinno ODM smartphone factory Vietnam India Indonesia expansion {week_str}"},

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
        {"r": 5, "label": "PCB Compeq Unimicron Tripod Zhen Ding SEA",
         "q": f"Compeq Unimicron Tripod Zhen Ding Flexium PCB FPC factory Vietnam Thailand Malaysia expansion {week_str}"},
        {"r": 5, "label": "PCB Chin Poon Meiko Olympic SEA",
         "q": f"Chin Poon Meiko Electronics Olympic Circuit PCB factory Vietnam Thailand expansion {week_str}"},
        {"r": 5, "label": "PCB Sihui Fuji Shennan Aohong Thailand",
         "q": f"Sihui Fuji Shennan Circuits Aohong Allfavor PCB factory Thailand Rayong Ayutthaya expansion {week_str}"},
        {"r": 5, "label": "CCL Doosan ITEQ TUC Shengyi Syrma SEA India",
         "q": f"Doosan ITEQ TUC Taiwan Union Shengyi Technology CCL copper clad laminate Syrma SGS PCB factory Thailand India expansion {week_str}"},
        {"r": 5, "label": "Polarizer Shanjin display film Vietnam",
         "q": f"Shanjin Optoelectronics polarizer film display component factory Vietnam Southeast Asia expansion {week_str}"},
        {"r": 5, "label": "Lens Technology glass casing Vietnam",
         "q": f"Lens Technology cover glass metal casing factory Vietnam Thailand expansion {week_str}"},
        {"r": 5, "label": "Camera module LG Innotek Sunny Optical",
         "q": f"LG Innotek Sunny Optical Largan camera module factory Vietnam {week_str}"},

        # ── Round 6 — Core Components: MLCC / Battery / Connector ───────
        {"r": 6, "label": "MLCC Murata Taiyo Yuden SEA India",
         "q": f"Murata Taiyo Yuden Samsung Electro-Mechanics MLCC factory Thailand Malaysia India {week_str}"},
        {"r": 6, "label": "Battery cell factory SEA India",
         "q": f"ATL Sunwoda Desay BYD battery cell factory Vietnam Thailand India {week_str}"},
        {"r": 6, "label": "EV battery CATL EVE Gotion REPT SEA",
         "q": f"CATL EVE Energy Gotion REPT Battero battery factory Vietnam Indonesia Thailand Malaysia expansion {week_str}"},
        {"r": 6, "label": "Battery Samsung SDI LG Energy Indonesia Malaysia",
         "q": f"Samsung SDI LG Energy Solution battery cell factory Indonesia Malaysia expansion {week_str}"},
        {"r": 6, "label": "Connector Amphenol Molex Lite-On SEA India",
         "q": f"Amphenol Molex Lite-On TD Connex connector factory Vietnam India expansion {week_str}"},
        {"r": 6, "label": "Connector Deren ECT JONHON Chinese Vietnam",
         "q": f"Deren Electronics ECT Electric Connector JONHON Kaiwang connector factory Vietnam Thailand expansion {week_str}"},
        {"r": 6, "label": "Compressor motor Kulthorn Nidec SEA",
         "q": f"Kulthorn Nidec Welling Embraco compressor motor factory Thailand Vietnam {week_str}"},
        {"r": 6, "label": "Nidec Embraco Mitsubishi compressor India",
         "q": f"Nidec Embraco Mitsubishi Electric compressor air conditioner factory India expansion {week_str}"},
        {"r": 6, "label": "Highly AUX compressor AC Thailand India",
         "q": f"Shanghai Highly Haili AUX Chunlan compressor air conditioner factory Thailand India expansion {week_str}"},
        {"r": 6, "label": "Battery material BTR Putailai Indonesia",
         "q": f"BTR Putailai anode material battery factory Indonesia Central Java expansion {week_str}"},
        {"r": 6, "label": "Motor Mabuchi MinebeaMitsumi GMCC SEA India",
         "q": f"Mabuchi Motor MinebeaMitsumi GMCC micro motor factory Vietnam Thailand India expansion {week_str}"},
        {"r": 6, "label": "Magnetic Sumida Kyocera AVX passive SEA",
         "q": f"Sumida inductor transformer Kyocera AVX passive component factory Vietnam Thailand expansion {week_str}"},
        {"r": 6, "label": "TDK inductor Chilisin Yageo passive SEA India",
         "q": f"TDK inductor Chilisin Yageo passive component factory Vietnam Thailand India expansion {week_str}"},
        {"r": 6, "label": "Walsin Knowles MLCC passive Malaysia",
         "q": f"Walsin Technology Knowles Inpaq MLCC passive MEMS factory Malaysia Penang expansion {week_str}"},
        {"r": 6, "label": "Electrolytic cap Nippon Chemi-Con Rubycon SEA India",
         "q": f"Nippon Chemi-Con Rubycon Nichicon aluminum electrolytic capacitor factory Thailand Indonesia India expansion {week_str}"},
        {"r": 6, "label": "Crystal oscillator TXC NDK Epson SEA",
         "q": f"TXC NDK Epson crystal oscillator quartz factory Southeast Asia expansion {week_str}"},
        {"r": 6, "label": "Connector Hirose JAE I-PEX SEA India",
         "q": f"Hirose Electric JAE I-PEX connector factory Vietnam India Thailand expansion {week_str}"},
        {"r": 6, "label": "Relay switch Omron Hongfa TE SEA",
         "q": f"Omron Hongfa TE Connectivity relay switch factory Vietnam Indonesia Thailand India expansion {week_str}"},

        # ── Round 7 — Thermal / Cooling / Power Supply ──────────────────
        {"r": 7, "label": "AVC Auras thermal cooling Vietnam",
         "q": f"AVC Asia Vital Components Auras cooling fan heat sink factory Vietnam {week_str}"},
        {"r": 7, "label": "Delta thermal cooling Thailand India",
         "q": f"Delta Electronics cooling thermal management factory Thailand India {week_str}"},
        {"r": 7, "label": "ebm-papst Sunon fan blower SEA India",
         "q": f"ebm-papst Sunon fan blower motor factory India Southeast Asia expansion {week_str}"},
        {"r": 7, "label": "Chicony power supply charger Thailand",
         "q": f"Chicony Power charger power supply adapter factory Thailand expansion {week_str}"},
        {"r": 7, "label": "Huntkey Haineng power supply Vietnam",
         "q": f"Huntkey Haineng CE-LINK power supply charger adapter factory Vietnam expansion {week_str}"},
        {"r": 7, "label": "Salcomp charger adapter India",
         "q": f"Salcomp charger power adapter factory India expansion {week_str}"},

        # ── Round 8 — Metal Casing / Glass Cover / Precision Parts ──────
        {"r": 8, "label": "BYD Electronic Catcher casing Vietnam",
         "q": f"BYD Electronic Catcher Technology Everwin metal casing factory Vietnam expansion {week_str}"},
        {"r": 8, "label": "Corning BIEL glass cover India Vietnam",
         "q": f"Corning Gorilla Glass BIEL Crystal cover glass factory India Vietnam {week_str}"},
        {"r": 8, "label": "Tongda Silver Basis precision mold Vietnam India",
         "q": f"Tongda Group Silver Basis precision mold structural parts factory Vietnam India expansion {week_str}"},

        # ── Round 9 — Backlight / LED / Acoustic / Adhesive ────────────
        {"r": 9, "label": "Radiant Coretronic backlight Vietnam",
         "q": f"Radiant Coretronic GLT Longli backlight LED module factory Vietnam {week_str}"},
        {"r": 9, "label": "AAC Knowles Foster BSE acoustic Vietnam",
         "q": f"AAC Technologies Knowles Foster Electric BSE speaker acoustic MEMS microphone factory Vietnam expansion {week_str}"},
        {"r": 9, "label": "O-film GIS touch sensor SEA India",
         "q": f"O-film Ofilm GIS touch sensor module factory Vietnam India Southeast Asia expansion {week_str}"},
        {"r": 9, "label": "Sunway Coxon antenna stamping SEA",
         "q": f"Shenzhen Sunway Xinwei antenna Coxon Precise stamping factory Vietnam Southeast Asia expansion {week_str}"},
        {"r": 9, "label": "Johnson Electric motor Cooler Master SEA India",
         "q": f"Johnson Electric motor Cooler Master cooling factory Vietnam Thailand India expansion {week_str}"},
        {"r": 9, "label": "GSEO Asia Optical Cowell Partron lens SEA",
         "q": f"GSEO Asia Optical Cowell Partron camera lens module factory Vietnam Southeast Asia expansion {week_str}"},
        {"r": 9, "label": "Ju Teng Rosti plastic molding Vietnam India",
         "q": f"Ju Teng Rosti plastic injection molding casing factory Vietnam India expansion {week_str}"},
        {"r": 9, "label": "Nitto Denko adhesive optical film SEA India",
         "q": f"Nitto Denko adhesive tape optical film factory Vietnam India Malaysia {week_str}"},
        {"r": 9, "label": "Hitachi Energy transformer Thailand Vietnam",
         "q": f"Hitachi Energy transformer power equipment factory Thailand Vietnam India expansion {week_str}"},
        {"r": 9, "label": "Seoul Semi MLS LED module Vietnam India",
         "q": f"Seoul Semiconductor MLS MuLinsen LED module lighting component factory Vietnam India expansion {week_str}"},

        # ── Round 10 — Wire Harness / Insulation / Stamping ─────────────
        {"r": 10, "label": "Motherson Yazaki Sumitomo Leoni wire harness SEA India",
         "q": f"Motherson Sumi Yazaki Sumitomo Electric Prysmian Leoni Delfingen wire harness cable factory Vietnam Thailand India expansion {week_str}"},
        {"r": 10, "label": "ZTT Hengtong cable fiber optic Indonesia India",
         "q": f"Zhongtian Technology ZTT Hengtong fiber optic cable factory Indonesia India Malaysia expansion {week_str}"},
        {"r": 10, "label": "Yuto Hexing packaging Vietnam Thailand",
         "q": f"Yuto Hexing packaging corrugated box color box factory Vietnam Thailand Indonesia expansion {week_str}"},
        {"r": 10, "label": "Nanshan aluminum alumina Indonesia",
         "q": f"Nanshan Aluminum alumina smelter factory Indonesia expansion {week_str}"},
        {"r": 10, "label": "SRF Armacell insulation India",
         "q": f"SRF refrigerant Armacell insulation material factory India expansion {week_str}"},
        {"r": 10, "label": "Wacker Momentive Shin-Etsu silicone SEA India",
         "q": f"Wacker Momentive Shin-Etsu Chemical BASF silicone rubber polyurethane factory Thailand India expansion {week_str}"},
        {"r": 10, "label": "Henkel Dow 3M adhesive TIM SEA India",
         "q": f"Henkel Dow 3M adhesive thermal interface material electronics factory India Southeast Asia expansion {week_str}"},
        {"r": 10, "label": "POSCO JSW UACJ steel aluminum SEA India",
         "q": f"POSCO JSW Steel UACJ Novelis Tata Steel aluminum sheet factory Vietnam Thailand India expansion {week_str}"},
        {"r": 10, "label": "Chemours Honeywell refrigerant India",
         "q": f"Chemours Honeywell Navin Fluorine refrigerant cooling fluid factory India expansion {week_str}"},

        # ── Round 11 — Chinese Supplier Migration to SEA ────────────────
        {"r": 11, "label": "Chinese electronics supplier Vietnam factory",
         "q": f"Chinese electronics supplier factory Vietnam relocation expansion investment {week_str}"},
        {"r": 11, "label": "Chinese supplier Thailand Indonesia factory",
         "q": f"Chinese manufacturer factory Thailand Indonesia electronics component investment {week_str}"},
        {"r": 11, "label": "DBG Lingyi BYD Electronic SEA factory",
         "q": f"DBG Technology Lingyi iTech BYD Electronic factory Southeast Asia Batam {week_str}"},

        # ── Round 12 — Indian PLI Electronics / Appliance ───────────────
        {"r": 12, "label": "Kaynes Amber PCB HDI India PLI",
         "q": f"Kaynes Technology Amber Enterprises Ascent Circuits HDI PCB factory India PLI ECMS expansion {week_str}"},
        {"r": 12, "label": "Midea Mitsubishi appliance factory SEA India",
         "q": f"Midea Mitsubishi Electric appliance refrigerator air conditioner factory Indonesia India expansion {week_str}"},

        # ── Round 13 — Policy & Regulatory ──────────────────────────────
        {"r": 13, "label": "Indonesia TKDN electronics policy",
         "q": f"Indonesia TKDN electronics regulation policy {week_str}"},
        {"r": 13, "label": "Malaysia MIDA electronics incentive",
         "q": f"Malaysia MIDA electronics investment incentive SIRIM MCMC {week_str}"},
        {"r": 13, "label": "Singapore IMDA regulation",
         "q": f"Singapore IMDA CSA cybersecurity certification electronics {week_str}"},
        {"r": 13, "label": "Vietnam FDI electronics policy",
         "q": f"Vietnam FDI electronics manufacturing policy regulation {week_str}"},
        {"r": 13, "label": "Thailand BOI EEC electronics",
         "q": f"Thailand BOI EEC electronics factory investment incentive {week_str}"},
        {"r": 13, "label": "India PLI BIS ECMS policy",
         "q": f"India PLI ECMS BIS electronics manufacturing regulation {week_str}"},

        # ── Round 14 — Global / Supply Chain Intelligence ───────────────
        {"r": 14, "label": "China+1 supply chain SEA India",
         "q": f"China plus one supply chain diversification Southeast Asia India electronics {week_str}"},
        {"r": 14, "label": "SEA India factory investment data",
         "q": f"Southeast Asia India electronics factory investment FDI data {week_str}"},
        {"r": 14, "label": "US tariff impact SEA India manufacturing",
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

── 1. NO MASTHEAD, NO FOOTER ────────────────────────────────
The page title, dates and footer are added by the publishing system.
Start directly with the key highlights. Do NOT use emoji or flag
characters anywhere. Write headings in sentence case, not ALL CAPS.

── 2. KEY HIGHLIGHTS ────────────────────────────────────────
<div class="highlights"><div class="wrap">
  <div class="highlights-label">Top stories</div>
  <div class="hl-grid"><!-- 4 × .hl-item with .hl-num (1–4) + .hl-text (≤20 words each) --></div>
</div></div>

── 3. GLOBAL INDUSTRY SHIFTS (.global-wrap, navy bg) ────────
Inside .wrap. Only items with a named direct SEA-country impact.
Each: .g-card > h4 + <p> + .g-link (→ downstream SEA impact) + .g-src (SPECIFIC date · <a href="URL">media name</a>)

── 4. CORE COMPONENT SUPPLY CHAIN UPDATE (.supply-wrap) ─────
Each: .s-card > h4 + <p> + .s-meta (location, status) + .s-impact > <p> + .s-src (SPECIFIC date · <a href="URL">media name</a>)
Sub-sections where newsworthy: PCB/FPC · Display · Camera · Battery ·
Passive · Cables · LED · Compressors/Motors · Touch/Glass · Steel

── 5. POLICY FOCUS (.policy-wrap, gold bg) ──────────────────
Each country with news: .p-card > h4 + <p>+ + .p-status + optional .p-alert
End with .ptable: Market | Policy | CE Impact | Effective Date

── 6–11. COUNTRY SECTIONS ───────────────────────────────────
For each: .section-rule (h2 country name + .section-sub; no flag or icon)
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
b-compeq b-unimicron b-tripod b-zhending b-flexium
b-ofilm b-gis b-sunway b-coxon
b-catl b-eve b-gotion b-rept b-sumida b-kyocera
b-doosan b-iteq b-tuc b-syrma b-johnson b-coolermaster
b-ats b-ase b-shanjin b-tdk b-yageo b-chemicom b-rubycon
b-txc b-omron b-hongfa b-gseo b-juteng b-rosti
b-wacker b-momentive b-basf b-posco b-uacj b-chemours
b-chinpoon b-meiko b-olympic b-lens b-walsin b-knowles
b-samsungsdi b-lges b-hirose b-jae b-mabuchi b-minebea
b-ebmpapst b-sunon b-chicony b-cowell b-partron b-foster
b-bse b-sumitomo b-prysmian b-jsw b-tatasteel b-henkel
b-dow b-3m b-shinei
b-policy b-event b-supply b-data b-ems

── 13. SOURCE INDEX (.src-index) ────────────────────────────
.itable: No. | Market (country name) | Story Topic | Source Media | Date
Row classes: r-global r-supply r-policy r-new
Every news item in the newsletter must have a row.

Output ONLY the HTML elements above. No markdown. No explanations.
"""


# ══════════════════════════════════════════════════════════════════════════════
# HTML DOCUMENT WRAPPER
# ══════════════════════════════════════════════════════════════════════════════

def wrap_html(body: str, start: datetime, end: datetime) -> str:
    """Lay the LLM body out as a newspaper-style page (see report_layout.py)."""
    return report_layout.wrap(
        body,
        title="SEA & India Manufacturing Watch",
        scope=("Factory investment, supply chain, OEM/EMS and policy in Singapore, "
               "Malaysia, Indonesia, Thailand, Vietnam and India"),
        accent="#1e5568", accent_soft="#eef4f6",
        start=start, end=end,
        sources_note="Compiled from public news, company and government sources.",
        method_note=("Each story gives its publication date and a link to the original source. "
                     "Only events inside the coverage week are included."),
    )


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


def generate_body_deepseek(messages_or_builder, search_results: str | None = None) -> str:
    """Call DeepSeek chat API (OpenAI-compatible) with streaming.

    When *search_results* is provided, *messages_or_builder* must be a callable
    that accepts a string and returns a message list.  On content-filter failure
    the search data is progressively shrunk before each retry.

    When *search_results* is None, *messages_or_builder* is a plain message list
    and retries only bump the temperature.
    """
    try:
        from openai import OpenAI
    except ImportError:
        log.error("openai package missing — run: pip install -r requirements.txt")
        sys.exit(1)

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

    can_shrink = search_results is not None and callable(messages_or_builder)
    if can_shrink:
        caps = [len(search_results), 80_000, 50_000]

    for attempt in range(1, 4):
        if can_shrink:
            cap = caps[attempt - 1]
            data = search_results[:cap] if len(search_results) > cap else search_results
            messages = messages_or_builder(data)
        else:
            messages = messages_or_builder

        chunks: list[str] = []
        try:
            with client.chat.completions.create(
                model=MODEL,
                messages=messages,
                max_tokens=MAX_TOKENS,
                temperature=0.2 + (attempt - 1) * 0.1,
                stream=True,
            ) as stream:
                for chunk in stream:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        chunks.append(delta)
                        print(delta, end="", flush=True)
            print()
            return "".join(chunks)
        except Exception as e:
            err_msg = str(e)
            if "Content Exists Risk" in err_msg and attempt < 3:
                if can_shrink:
                    log.warning(
                        f"DeepSeek content filter triggered (attempt {attempt}/3), "
                        f"reducing data to {caps[attempt]:,} chars and retrying..."
                    )
                else:
                    log.warning(f"DeepSeek content filter triggered (attempt {attempt}/3), retrying...")
                time.sleep(2)
                continue
            raise

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


def generate_body_gemini(messages: list[dict]) -> str:
    """Call Google Gemini API via OpenAI-compatible endpoint with streaming."""
    try:
        from openai import OpenAI
    except ImportError:
        log.error("openai package missing — run: pip install -r requirements.txt")
        sys.exit(1)

    client = OpenAI(
        api_key=GEMINI_API_KEY,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )

    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        chunks: list[str] = []
        try:
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
        except Exception as e:
            err_msg = str(e)
            if ("503" in err_msg or "429" in err_msg or "UNAVAILABLE" in err_msg) and attempt < max_attempts:
                wait = 30 * attempt
                log.warning(f"Gemini temporarily unavailable (attempt {attempt}/{max_attempts}), retrying in {wait}s...")
                time.sleep(wait)
                continue
            raise

    return "".join(chunks)


def generate_body(search_results: str, start: datetime, end: datetime) -> str:
    """
    Ask the LLM to produce HTML body content only.
    If the response appears truncated, request a continuation (one retry).
    """
    max_search_chars = {"deepseek": 120_000, "gemini": 900_000, "anthropic": 900_000}.get(PROVIDER, 120_000)
    if len(search_results) > max_search_chars:
        log.warning(f"Search data too large ({len(search_results):,} chars), "
                    f"truncating to {max_search_chars:,} chars")
        search_results = search_results[:max_search_chars]

    def _build_messages(data: str) -> list[dict]:
        user_content = USER_PROMPT.format(
            start_date=fmt(start),
            end_date=fmt(end),
            search_results=data,
        )
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_content},
        ]

    log.info(f"Calling {PROVIDER.upper()} ({MODEL}) — streaming body HTML...\n" + "─" * 60)

    if PROVIDER == "anthropic":
        body = generate_body_anthropic(_build_messages(search_results))
    elif PROVIDER == "gemini":
        body = generate_body_gemini(_build_messages(search_results))
    else:
        body = generate_body_deepseek(_build_messages, search_results)

    log.info("─" * 60)

    # ── Truncation check & one continuation pass ──────────────────────────────
    if not body.rstrip().endswith("</html>") and "</footer>" not in body:
        log.warning("Output appears truncated — requesting continuation...")
        cont_messages = _build_messages(search_results) + [
            {"role": "assistant", "content": body},
            {"role": "user", "content":
             "The HTML was cut off. Continue from exactly where you stopped. "
             "Output only the remaining HTML elements — no repetition."},
        ]
        log.info("Continuation stream:\n" + "─" * 60)
        if PROVIDER == "anthropic":
            continuation = generate_body_anthropic(cont_messages)
        elif PROVIDER == "gemini":
            continuation = generate_body_gemini(cont_messages)
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
    elif PROVIDER == "gemini":
        cleaned = generate_body_gemini(messages)
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
    if PROVIDER == "gemini" and not GEMINI_API_KEY:
        errors.append("GEMINI_API_KEY missing  →  https://aistudio.google.com/apikey")
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
