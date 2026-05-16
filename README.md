# SEA Consumer Electronics Intelligence — Newsletter Generator

Automatically searches for weekly news across 11 Southeast Asian markets and generates a fully-styled HTML newsletter using **DeepSeek** (default) or Anthropic Claude.

Runs every **Friday at 08:00 UTC** via GitHub Actions and commits the HTML file back to this repository.

---

## Architecture

```
Weekly trigger (GitHub Actions)
        │
        ▼
  Tavily Web Search          ← 28 queries across 7 topic rounds
        │
        ▼
  DeepSeek / Claude          ← generates HTML body only (fits within token limits)
        │
        ▼
  Python wraps body          ← injects CSS + Google Fonts → complete HTML doc
        │
        ▼
  output/newsletter_SEA_electronics_DDMONYYYY_EN.html
        │
        ▼
  git commit + push          ← file is version-controlled in this repo
```

---

## Quick Start (GitHub)

### Step 1 — Fork or create this repository

Push all files to a new GitHub repository (public or private).

```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

### Step 2 — Add GitHub Secrets

Go to your repository → **Settings → Secrets and variables → Actions → Secrets → New repository secret**

#### API keys (required)

| Secret name | Value | Where to get it |
|---|---|---|
| `DEEPSEEK_API_KEY` | `sk-...` | [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) |
| `TAVILY_API_KEY` | `tvly-...` | [app.tavily.com](https://app.tavily.com/) — free 1,000/month |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | Optional — only if you switch `PROVIDER` to `anthropic` |

#### Email delivery (required for auto-send)

| Secret name | Value |
|---|---|
| `EMAIL_FROM` | Your Gmail address, e.g. `yourname@gmail.com` |
| `EMAIL_PASSWORD` | **Gmail App Password** (16 chars, not your login password) |
| `EMAIL_TO` | Recipient email(s), comma-separated: `a@x.com,b@x.com` |
| `EMAIL_CC` | CC recipients (optional, can leave empty) |

> **How to create a Gmail App Password:**
> 1. Go to your Google Account → **Security**
> 2. Enable **2-Step Verification** (required)
> 3. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
> 4. Select **Mail** + **Windows Computer** → click **Generate**
> 5. Copy the 16-character password (e.g. `abcd efgh ijkl mnop`) into `EMAIL_PASSWORD`

### Step 3 — (Optional) Set GitHub Variables

Go to **Settings → Secrets and variables → Actions → Variables**

| Variable | Default | Description |
|---|---|---|
| `PROVIDER` | `deepseek` | `deepseek` or `anthropic` |
| `MODEL` | `deepseek-chat` | Override model name |
| `MAX_TOKENS` | `7000` | Max output tokens |

### Step 4 — Trigger your first run

- Go to **Actions → Generate SEA Electronics Newsletter → Run workflow**
- Click **Run workflow** (green button)
- Watch the logs — generation takes ~3–5 minutes

The HTML file will appear in `output/` and be committed automatically.

---

## Manual / Local Run

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO

# 2. Create virtual environment
python -m venv .venv
source .venv/bin/activate        # Linux/Mac
# or: .venv\Scripts\activate     # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure API keys
cp .env.example .env
# Edit .env and fill in DEEPSEEK_API_KEY and TAVILY_API_KEY

# 5. Run
python generate_newsletter.py
```

### Useful flags

```bash
# Test searches only (no LLM cost)
python generate_newsletter.py --dry-run

# Save search results to a file (for reuse / debugging)
python generate_newsletter.py --save-search search_cache.txt

# Regenerate from saved results (skips Tavily — saves API calls)
python generate_newsletter.py --search-cache search_cache.txt
```

---

## Schedule

The workflow runs automatically every **Monday at 08:00 SGT** (00:00 UTC). Output filename convention:

```
newsletter_SEA_electronics_DDMONYYYY_EN.html
e.g. newsletter_SEA_electronics_23may2026_EN.html
```

To change the schedule, edit `.github/workflows/generate_newsletter.yml`:

```yaml
schedule:
  - cron: '0 1 * * 5'   # 01:00 UTC = 09:00 SGT/MYT (UTC+8), every Friday
```

Common time conversions (Friday morning):

| Local time | UTC cron |
|---|---|
| 08:00 SGT/MYT | `0 0 * * 5` |
| **09:00 SGT/MYT** ← current | **`0 1 * * 5`** |
| 10:00 SGT/MYT | `0 2 * * 5` |
| 09:00 CST (China) | `0 1 * * 5` |

---

## Full Automated Flow (every Friday 09:00 SGT)

```
GitHub Actions wakes up
   │
   ├─[1/4] Tavily: 28 web searches across SEA news sources
   ├─[2/4] DeepSeek V3: writes newsletter body HTML
   ├─[3/4] Python: wraps body → complete styled HTML file saved to output/
   └─[4/4] SMTP: sends HTML email + .html attachment to all recipients
              │
              └─ git commit + push → file archived in repo
```

## API Costs (estimated per weekly run)

| Service | Usage | Estimated cost |
|---|---|---|
| Tavily Search | ~28 queries | **Free** (≤1,000/month tier) |
| DeepSeek-V3 input | ~40K tokens | ~$0.011 |
| DeepSeek-V3 output | ~7K tokens | ~$0.008 |
| Email (Gmail SMTP) | 1 send | **Free** |
| **Total per issue** | | **~$0.02** |

---

## File Structure

```
.
├── generate_newsletter.py        # Main script
├── requirements.txt              # Python dependencies
├── .env.example                  # API key template
├── .env                          # Your keys (git-ignored)
├── .gitignore
├── run_weekly.bat                # Windows double-click runner
├── .github/
│   └── workflows/
│       └── generate_newsletter.yml   # GitHub Actions schedule
└── output/
    ├── .gitkeep
    └── newsletter_SEA_electronics_*.html   # Generated files
```

---

## Providers

| Provider | Model | Input limit | Output limit | Cost |
|---|---|---|---|---|
| **DeepSeek** (default) | `deepseek-chat` (V3) | 128K tokens | 8K tokens | Very cheap |
| Anthropic | `claude-opus-4-7` | 200K tokens | 16K tokens | Higher |

The script generates **body HTML only** and wraps the document in Python — this keeps output within DeepSeek's 8K limit comfortably. A truncation-continuation retry is built in as a safety net.
