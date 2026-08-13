"""
social_poster.py
=================
Generates and publishes a daily Instagram post: the best ticker for the
next session, the best medium-term setup, and the best long-term/sector
pick — each with the evidence (score, RSI/ADX, pattern, sector strength)
straight from the same market_data.json the web dashboard already reads.

DESIGN — why this reads market_data.json instead of the local DuckDB:
    market_data.json is already the PUBLIC, privacy-scrubbed payload
    export_json.py produces (see that file's privacy-fix notes — no cash
    balance, no account P&L, no owned-position data). Posting to
    Instagram is inherently public, so this script only ever touches
    data that has already passed that privacy boundary. It also means
    this script has ZERO dependency on the local DuckDB file or the
    desktop app being open, so it can run unattended on a schedule
    (e.g. a GitHub Actions cron job) with nothing but network access —
    see .github/workflows/daily-instagram-post.yml.

TWO-STEP WORKFLOW (render, then publish) instead of one shot:
    Instagram's Graph API requires a publicly reachable image URL — it
    fetches the image itself, you cannot upload bytes directly. So:
      1. `render`  — fetch market_data.json, pick the 3 highlights,
         draw the card PNG, write the caption text. Exits.
         (Then the CI workflow commits+pushes that PNG to the repo.)
      2. `publish` — now that the PNG is live at a public raw.githack /
         raw.githubusercontent URL, call the Graph API with that URL.
    Splitting it this way means this script never needs its own image
    hosting — it reuses the repo you already publish from.

REQUIRED SETUP (one-time, cannot be done from code):
    Uses the "Instagram API with Instagram Login" flow (graph.instagram.com),
    not the older Facebook Page-token flow — no Facebook Page token
    exchange needed; the token generated in the App Dashboard's
    "API setup with Instagram login" → "Generate access tokens" step is
    already a 60-day long-lived token.
    1. Convert the Instagram account to a Business or Creator account
       (Instagram app → Settings → Account type and tools).
    2. Create a Meta developer app at https://developers.facebook.com,
       add the "Instagram" (Manage messaging & content on Instagram)
       use case.
    3. In the app's "Permissions and features" page, add
       instagram_business_content_publish (on top of the default
       instagram_business_basic bundle).
    4. Under App roles → Roles, add your Instagram account as an
       "Instagram Tester", then accept the invite from the Instagram
       account itself at https://www.instagram.com/accounts/manage_access/
       → "Tester Invites" tab.
    5. In "API setup with Instagram login" → "2. Generate access
       tokens", click "Add account", approve the permissions, then
       "Generate token". The numeric ID shown next to the account name
       on that same page is your Instagram User ID (IG_USER_ID).
       Long-lived tokens last ~60 days — you will need to refresh it
       periodically (see refresh_reminder() below) or automate the
       refresh call in the same cron job.
    6. Store as environment variables / GitHub Actions secrets (NEVER
       commit these to the repo):
         IG_USER_ID       = the numeric Instagram User ID from step 5
         IG_ACCESS_TOKEN  = the long-lived token from step 5
         PAGES_BASE_URL   = e.g. https://<user>.github.io/<repo>  (used
                             only to fetch market_data.json; the image
                             itself is served from raw.githubusercontent
                             so it doesn't wait on a Pages rebuild)
         GITHUB_REPO      = "<owner>/<repo>"  (for building the raw
                             image URL after the CI commit step)
    7. For fully automatic token rotation (recommended — otherwise you
       must manually refresh & update IG_ACCESS_TOKEN every ~60 days):
       create a GitHub Personal Access Token scoped ONLY to this repo
       with "Secrets: Read and write" permission (Settings → Developer
       settings → Fine-grained tokens → Generate new token → Repository
       access: only this repo → Permissions → Secrets: Read and write).
       Save it as a repo secret named GH_PAT. The workflow then rotates
       IG_ACCESS_TOKEN itself whenever Instagram issues a refreshed
       token — you never need to touch it again.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

GRAPH_API_VERSION = "v21.0"
GRAPH_BASE = "https://graph.instagram.com"
FB_GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

OUT_DIR = Path(__file__).parent / "web_public" / "social"
FONT_DIR = "/usr/share/fonts/truetype/dejavu"  # adjust if your CI image differs

# ---- Brand palette (matches app_gui.py / index.html dark theme) ----
BG = (11, 16, 24)
PANEL = (18, 26, 38)
ACCENT = (147, 204, 255)   # #93ccff
GREEN = (72, 187, 120)
AMBER = (237, 137, 54)
RED = (245, 101, 101)
TEXT_MAIN = (237, 242, 247)
TEXT_MUTED = (160, 174, 192)


# =============================================================================
# 1. DATA FETCH
# =============================================================================
def fetch_market_data(pages_base_url: str) -> dict:
    url = pages_base_url.rstrip("/") + "/data/market_data.json"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


# =============================================================================
# 2. PICK THE 3 DAILY HIGHLIGHTS
# =============================================================================
def pick_daily_highlights(market_data: dict) -> dict:
    matrix = market_data.get("market_matrix", [])
    top10 = market_data.get("top_10", {})
    sectors = market_data.get("sectors", [])
    by_ticker = {r["Ticker"]: r for r in matrix}
    used: set = set()

    # --- Next session ---
    next_pool = list(top10.get("🔥 STRONG BUY", []))
    for cat, rows in top10.items():
        if "BREAKOUT BUY" in cat:
            next_pool.extend(rows)
    next_pool.sort(key=lambda r: r.get("Rank Score", 0), reverse=True)
    next_session = next_pool[0] if next_pool else None
    if next_session:
        used.add(next_session["Ticker"])

    # --- Medium term ---
    med_pool = [
        r for r in (top10.get("📈 ACCUMULATE", []) + top10.get("⏳ BUY ON DIP", []))
        if r["Ticker"] not in used
    ]
    med_pool.sort(key=lambda r: r.get("Rank Score", 0), reverse=True)
    medium_term = med_pool[0] if med_pool else None
    if medium_term:
        used.add(medium_term["Ticker"])

    # --- Long term ---
    long_term, sector_context = None, None
    strong_sectors = [s for s in sectors if "STRONG INFLOW" in s.get("Sector Status", "")]
    strong_sectors.sort(key=lambda s: s.get("Bullish Breadth (%)", 0), reverse=True)
    for s in strong_sectors:
        leader = s.get("Sector Leader")
        if leader in by_ticker and leader not in used:
            long_term = by_ticker[leader]
            sector_context = s
            break
    if long_term is None:
        fallback_pool = [r for r in matrix if r["Ticker"] not in used]
        fallback_pool.sort(key=lambda r: r.get("Rank Score", 0), reverse=True)
        long_term = fallback_pool[0] if fallback_pool else None

    return {
        "next_session": next_session,
        "medium_term": medium_term,
        "long_term": long_term,
        "long_term_sector": sector_context,
        "last_data_date": market_data.get("last_data_date"),
    }


def build_evidence(row: dict | None, sector_context: dict | None = None) -> list[str]:
    if row is None:
        return []
    ev = [
        f"Composite score {row.get('Rank Score', '—')}/100",
        f"RSI-14: {row.get('RSI-14', '—')}  ·  ADX-14: {row.get('ADX-14', '—')} ({row.get('Trend Class', '—')})",
    ]
    pat_conf = row.get("Pattern Conf (%)")
    if pat_conf not in (None, "N/A"):
        ev.append(
            f"Chart pattern confidence {pat_conf}% · projected +{row.get('Projected Gain (%)', '—')}%"
        )
    if row.get("Data Confidence"):
        ev.append(f"Data confidence: {row['Data Confidence']}")
    if sector_context:
        ev.append(
            f"Sector leader — {sector_context['Sector']} ({sector_context['Sector Status']}, "
            f"{sector_context['Bullish Breadth (%)']}% of sector bullish)"
        )
    return ev


# =============================================================================
# 2b. MARKET OVERVIEW
# =============================================================================
def compute_market_overview(market_data: dict) -> dict:
    stocks = market_data.get("chart_history", {}).get("stocks", {})
    movers = []
    for ticker, hist in stocks.items():
        closes = [c for c in hist.get("close", []) if c is not None]
        if len(closes) < 2 or not closes[-2]:
            continue
        ret_1d = (closes[-1] / closes[-2] - 1) * 100
        ret_5d = None
        if len(closes) >= 6 and closes[-6]:
            ret_5d = (closes[-1] / closes[-6] - 1) * 100
        movers.append({"ticker": ticker, "ret_1d": ret_1d, "ret_5d": ret_5d})

    last_data_date = market_data.get("last_data_date")
    if not movers:
        return {"total": 0, "last_data_date": last_data_date}

    avg_1d = sum(m["ret_1d"] for m in movers) / len(movers)
    fivers = [m["ret_5d"] for m in movers if m["ret_5d"] is not None]
    avg_5d = sum(fivers) / len(fivers) if fivers else None
    advancers = sum(1 for m in movers if m["ret_1d"] > 0)
    decliners = sum(1 for m in movers if m["ret_1d"] < 0)
    unchanged = len(movers) - advancers - decliners

    return {
        "total": len(movers),
        "avg_1d": avg_1d,
        "avg_5d": avg_5d,
        "advancers": advancers,
        "decliners": decliners,
        "unchanged": unchanged,
        "top_gainer": max(movers, key=lambda m: m["ret_1d"]),
        "top_loser": min(movers, key=lambda m: m["ret_1d"]),
        "last_data_date": last_data_date,
    }


def render_market_overview_image(overview: dict, out_path: Path) -> Path:
    img = Image.new("RGB", (1080, 1350), BG)
    draw = ImageDraw.Draw(img)
    f_brand = _font("DejaVuSans-Bold.ttf", 44)
    f_tagline = _font("DejaVuSans.ttf", 24)
    f_date = _font("DejaVuSans.ttf", 22)
    f_big = _font("DejaVuSans-Bold.ttf", 88)
    f_big_small = _font("DejaVuSans-Bold.ttf", 50)
    f_label = _font("DejaVuSans.ttf", 28)
    f_section = _font("DejaVuSans-Bold.ttf", 30)
    f_body = _font("DejaVuSans.ttf", 26)

    draw.text((40, 40), "MB-EGX", font=f_brand, fill=ACCENT)
    draw.text((40, 96), "Market Overview — Tracked Stocks", font=f_tagline, fill=TEXT_MUTED)
    date_str = overview.get("last_data_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    draw.text((40, 130), f"Market data as of {date_str}", font=f_date, fill=TEXT_MUTED)

    if overview.get("total", 0) == 0:
        draw.text((40, 250), "No data available today", font=f_body, fill=TEXT_MUTED)
        img.save(out_path, "PNG")
        return out_path

    avg_1d = overview["avg_1d"]
    color = GREEN if avg_1d >= 0 else RED
    draw.rounded_rectangle([40, 200, 1040, 480], radius=18, fill=PANEL)
    draw.text((70, 230), "AVERAGE 1-DAY RETURN", font=f_label, fill=TEXT_MUTED)
    draw.text((70, 265), f"{avg_1d:+.2f}%", font=f_big, fill=color)
    if overview.get("avg_5d") is not None:
        draw.text((70, 400), f"5-day average: {overview['avg_5d']:+.2f}%", font=f_body, fill=TEXT_MAIN)

    draw.rounded_rectangle([40, 510, 1040, 680], radius=18, fill=PANEL)
    draw.text((70, 535), "BREADTH", font=f_label, fill=TEXT_MUTED)
    total = overview["total"]
    draw.text(
        (70, 575),
        f"{overview['advancers']} up  ·  {overview['decliners']} down  ·  {overview['unchanged']} flat",
        font=f_body, fill=TEXT_MAIN,
    )
    draw.text((70, 615), f"out of {total} tracked stocks", font=f_body, fill=TEXT_MUTED)

    gainer, loser = overview.get("top_gainer"), overview.get("top_loser")
    y = 710
    for title, mover, color in [("TOP GAINER", gainer, GREEN), ("TOP LOSER", loser, RED)]:
        draw.rounded_rectangle([40, y, 1040, y + 160], radius=18, fill=PANEL)
        draw.text((70, y + 24), title, font=f_section, fill=color)
        if mover:
            draw.text((70, y + 62), mover["ticker"], font=f_section, fill=TEXT_MAIN)
            draw.text((70, y + 104), f"{mover['ret_1d']:+.2f}%", font=f_big_small, fill=color)
        y += 190

    f_disclaimer = _font("DejaVuSans.ttf", 18)
    draw.text(
        (40, 1300),
        "Average of tracked stocks only — not the official EGX30/EGX70 index. Educational content, not investment advice.",
        font=f_disclaimer, fill=TEXT_MUTED,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


# =============================================================================
# 2c. SECTORS OVERVIEW
# =============================================================================
def pick_sector_highlights(market_data: dict, limit: int = 5) -> list[dict]:
    sectors = market_data.get("sectors", [])
    return sorted(sectors, key=lambda s: s.get("1D Return (%)", 0), reverse=True)[:limit]


def render_sectors_image(sectors: list[dict], last_data_date: str | None, out_path: Path) -> Path:
    img = Image.new("RGB", (1080, 1350), BG)
    draw = ImageDraw.Draw(img)
    f_brand = _font("DejaVuSans-Bold.ttf", 44)
    f_tagline = _font("DejaVuSans.ttf", 24)
    f_date = _font("DejaVuSans.ttf", 22)
    f_name = _font("DejaVuSans-Bold.ttf", 34)
    f_pct = _font("DejaVuSans-Bold.ttf", 40)
    f_small = _font("DejaVuSans.ttf", 22)

    draw.text((40, 40), "MB-EGX", font=f_brand, fill=ACCENT)
    draw.text((40, 96), "Sectors Overview — Top Movers", font=f_tagline, fill=TEXT_MUTED)
    date_str = last_data_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    draw.text((40, 130), f"Market data as of {date_str}", font=f_date, fill=TEXT_MUTED)

    y = 190
    if not sectors:
        draw.text((40, y), "No sector data available today", font=f_small, fill=TEXT_MUTED)
    for sec in sectors:
        ret_1d = sec.get("1D Return (%)", 0)
        color = GREEN if ret_1d >= 0 else RED
        draw.rounded_rectangle([40, y, 1040, y + 200], radius=18, fill=PANEL)
        draw.text((70, y + 24), sec.get("Sector", "—"), font=f_name, fill=TEXT_MAIN)
        draw.text((70, y + 74), f"{ret_1d:+.2f}% today", font=f_pct, fill=color)
        draw.text(
            (70, y + 130),
            f"Breadth {sec.get('Bullish Breadth (%)', '—')}% bullish · Leader {sec.get('Sector Leader', '—')}",
            font=f_small, fill=TEXT_MUTED,
        )
        status_clean = "".join(ch for ch in sec.get("Sector Status", "") if ch.isascii()).strip()
        draw.text((70, y + 160), status_clean, font=f_small, fill=color)
        y += 220

    f_disclaimer = _font("DejaVuSans.ttf", 18)
    draw.text(
        (40, 1300),
        "Educational content, not investment advice.",
        font=f_disclaimer, fill=TEXT_MUTED,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


# =============================================================================
# 3. RENDER THE IMAGE CARD (1080x1350 — Instagram portrait)
# =============================================================================
def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    path = os.path.join(FONT_DIR, name)
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _draw_section(draw, x, y, w, title, row, evidence, accent):
    draw.rounded_rectangle([x, y, x + w, y + 330], radius=18, fill=PANEL)
    f_title = _font("DejaVuSans-Bold.ttf", 30)
    f_ticker = _font("DejaVuSans-Bold.ttf", 56)
    f_body = _font("DejaVuSans.ttf", 26)
    f_small = _font("DejaVuSans.ttf", 22)

    draw.text((x + 30, y + 24), title, font=f_title, fill=accent)
    if row is None:
        draw.text((x + 30, y + 90), "No qualifying setup today", font=f_body, fill=TEXT_MUTED)
        return
    ticker = row.get("Ticker", "—")
    draw.text((x + 30, y + 76), ticker, font=f_ticker, fill=TEXT_MAIN)

    price = row.get("Current Price")
    if price is not None:
        draw.text((x + 30, y + 150), f"{price} EGP", font=f_body, fill=TEXT_MUTED)

    ey = y + 195
    for line in evidence[:3]:
        draw.text((x + 30, ey), f"• {line}", font=f_small, fill=TEXT_MAIN)
        ey += 32


def render_post_image(highlights: dict, out_path: Path) -> Path:
    img = Image.new("RGB", (1080, 1350), BG)
    draw = ImageDraw.Draw(img)

    f_brand = _font("DejaVuSans-Bold.ttf", 44)
    f_tagline = _font("DejaVuSans.ttf", 24)
    f_date = _font("DejaVuSans.ttf", 22)

    draw.text((40, 40), "MB-EGX", font=f_brand, fill=ACCENT)
    draw.text((40, 96), "Daily Ticker Highlights", font=f_tagline, fill=TEXT_MUTED)
    date_str = highlights.get("last_data_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    draw.text((40, 130), f"Market data as of {date_str}", font=f_date, fill=TEXT_MUTED)

    sections = [
        ("NEXT SESSION", highlights["next_session"], GREEN,
         build_evidence(highlights["next_session"])),
        ("MEDIUM TERM", highlights["medium_term"], ACCENT,
         build_evidence(highlights["medium_term"])),
        ("LONG TERM / SECTOR LEADER", highlights["long_term"], AMBER,
         build_evidence(highlights["long_term"], highlights.get("long_term_sector"))),
    ]
    y = 200
    for title, row, accent, evidence in sections:
        draw.rounded_rectangle([40, y, 46, y + 330], radius=3, fill=accent)
        _draw_section(draw, 40, y, 1000, title, row, evidence, accent)
        y += 360

    f_disclaimer = _font("DejaVuSans.ttf", 18)
    draw.text(
        (40, 1300),
        "Educational content, not investment advice. Not financial advice.",
        font=f_disclaimer, fill=TEXT_MUTED,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


# =============================================================================
# 4. CAPTION 
# =============================================================================
def append_promotional_footer(text_block: str) -> str:
    footer = (
        "\n\n"
        "Follow Facebook Page: https://www.facebook.com/profile.php?id=61593012092507\n"
        "Follow Instagram: https://www.instagram.com/mb_egx/\n"
        "Login App: https://mb-egx.github.io/"
    )
    return text_block + footer


def _row_line_en(label: str, row: dict | None) -> str:
    if row is None:
        return f"{label}: no qualifying setup today"
    return f"{label}: {row['Ticker']} — score {row.get('Rank Score', '—')}/100, RSI {row.get('RSI-14', '—')}"


def _row_line_ar(label: str, row: dict | None) -> str:
    if row is None:
        return f"{label}: لا يوجد إعداد مؤهل اليوم"
    return f"{label}: {row['Ticker']} — الدرجة {row.get('Rank Score', '—')}/100، RSI {row.get('RSI-14', '—')}"


def build_caption(highlights: dict) -> str:
    ns, mt, lt = highlights["next_session"], highlights["medium_term"], highlights["long_term"]
    lt_sector = highlights.get("long_term_sector")

    en = [
        "📊 MB-EGX Daily Ticker Highlights",
        "",
        _row_line_en("🔥 Next session", ns),
        _row_line_en("📈 Medium term", mt),
        _row_line_en("🏛️ Long term / sector leader", lt),
    ]
    if lt_sector:
        en.append(f"   Sector: {lt_sector['Sector']} ({lt_sector['Sector Status']})")
    en += [
        "",
        "This is educational content generated by a mechanical multi-factor model reading price, volume, and trend data. It is NOT investment advice — always do your own research and apply your own judgment.",
        "",
        "#EGX #MBEGX #EgyptStockMarket #StockMarket #Investing #Trading #EGYPT",
    ]

    ar = [
        "📊 أبرز الأسهم اليومية — MB-EGX",
        "",
        _row_line_ar("🔥 الجلسة القادمة", ns),
        _row_line_ar("📈 متوسط المدى", mt),
        _row_line_ar("🏛️ طويل المدى / رائد القطاع", lt),
    ]
    if lt_sector:
        ar.append(f"   القطاع: {lt_sector['Sector']} ({lt_sector['Sector Status']})")
    ar += [
        "",
        "هذا محتوى تعليمي من نموذج آلي متعدد العوامل يحلل بيانات السعر والحجم والاتجاه، وليس نصيحة استثمارية — قم دائمًا بأبحاثك الخاصة واستخدم حكمك الشخصي.",
    ]

    combined_caption = "\n".join(en) + "\n\n————\n\n" + "\n".join(ar)
    return append_promotional_footer(combined_caption)


def build_market_caption(overview: dict) -> str:
    if overview.get("total", 0) == 0:
        en = ["📊 MB-EGX Market Overview", "", "No data available today."]
        ar = ["📊 نظرة عامة على السوق — MB-EGX", "", "لا توجد بيانات متاحة اليوم."]
        combined_caption = "\n".join(en) + "\n\n————\n\n" + "\n".join(ar)
        return append_promotional_footer(combined_caption)

    gainer, loser = overview["top_gainer"], overview["top_loser"]
    en = [
        "📊 MB-EGX Market Overview (tracked stocks, NOT the official EGX30/EGX70 index)",
        "",
        f"Average 1-day return: {overview['avg_1d']:+.2f}%",
        f"Breadth: {overview['advancers']} up / {overview['decliners']} down / {overview['unchanged']} flat (of {overview['total']} tracked)",
        f"Top gainer: {gainer['ticker']} {gainer['ret_1d']:+.2f}%",
        f"Top loser: {loser['ticker']} {loser['ret_1d']:+.2f}%",
        "",
        "This is an average across the individual stocks this account tracks — it is NOT the official EGX30 or EGX70 index value. Educational content, not investment advice.",
        "",
        "#EGX #MBEGX #EgyptStockMarket #StockMarket #EGYPT",
    ]
    ar = [
        "📊 نظرة عامة على السوق — MB-EGX (متوسط الأسهم المتابَعة، وليس مؤشر EGX30/EGX70 الرسمي)",
        "",
        f"متوسط عائد اليوم: {overview['avg_1d']:+.2f}%",
        f"الاتساع: {overview['advancers']} صاعد / {overview['decliners']} هابط / {overview['unchanged']} مستقر (من أصل {overview['total']} سهمًا متابَعًا)",
        f"الأعلى ارتفاعًا: {gainer['ticker']} {gainer['ret_1d']:+.2f}%",
        f"الأعلى انخفاضًا: {loser['ticker']} {loser['ret_1d']:+.2f}%",
        "",
        "هذا متوسط لأسهم متابَعة فرديًا وليس القيمة الرسمية لمؤشر EGX30 أو EGX70. محتوى تعليمي وليس نصيحة استثمارية.",
    ]
    
    combined_caption = "\n".join(en) + "\n\n————\n\n" + "\n".join(ar)
    return append_promotional_footer(combined_caption)


def build_sectors_caption(sectors: list[dict]) -> str:
    if not sectors:
        en = ["📊 MB-EGX Sectors Overview", "", "No sector data available today."]
        ar = ["📊 نظرة عامة على القطاعات — MB-EGX", "", "لا توجد بيانات قطاعات متاحة اليوم."]
        combined_caption = "\n".join(en) + "\n\n————\n\n" + "\n".join(ar)
        return append_promotional_footer(combined_caption)

    en = ["📊 MB-EGX Sectors Overview — today's top movers", ""]
    ar = ["📊 نظرة عامة على القطاعات — MB-EGX — أبرز تحركات اليوم", ""]
    for sec in sectors:
        en.append(f"{sec.get('Sector', '—')}: {sec.get('1D Return (%)', 0):+.2f}% ({sec.get('Sector Status', '—')})")
        ar.append(f"{sec.get('Sector', '—')}: {sec.get('1D Return (%)', 0):+.2f}% ({sec.get('Sector Status', '—')})")
    en += [
        "",
        "Educational content generated by a mechanical model reading price, volume, and trend data. Not investment advice.",
        "",
        "#EGX #MBEGX #EgyptStockMarket #StockMarket #EGYPT",
    ]
    ar += ["", "محتوى تعليمي من نموذج آلي يحلل بيانات السعر والحجم والاتجاه. ليس نصيحة استثمارية."]
    
    combined_caption = "\n".join(en) + "\n\n————\n\n" + "\n".join(ar)
    return append_promotional_footer(combined_caption)


# =============================================================================
# 5. GRAPH API PUBLISH
# =============================================================================
def publish_to_instagram(ig_user_id: str, access_token: str, image_url: str, caption: str) -> str:
    create_resp = requests.post(
        f"{GRAPH_BASE}/{ig_user_id}/media",
        data={"image_url": image_url, "caption": caption, "access_token": access_token},
        timeout=30,
    )
    create_resp.raise_for_status()
    creation_id = create_resp.json()["id"]

    for _ in range(10):
        status_resp = requests.get(
            f"{GRAPH_BASE}/{creation_id}",
            params={"fields": "status_code", "access_token": access_token},
            timeout=15,
        )
        status_resp.raise_for_status()
        if status_resp.json().get("status_code") == "FINISHED":
            break
        time.sleep(2)

    publish_resp = requests.post(
        f"{GRAPH_BASE}/{ig_user_id}/media_publish",
        data={"creation_id": creation_id, "access_token": access_token},
        timeout=30,
    )
    publish_resp.raise_for_status()
    return publish_resp.json()["id"]


def publish_to_facebook(page_id: str, page_access_token: str, image_url: str, caption: str) -> str:
    resp = requests.post(
        f"{FB_GRAPH_BASE}/{page_id}/photos",
        data={"url": image_url, "caption": caption, "access_token": page_access_token},
        timeout=30,
    )
    if not resp.ok:
        print(f"❌ Facebook API error {resp.status_code}: {resp.text}", file=sys.stderr)
    resp.raise_for_status()
    return resp.json()["post_id"]


def refresh_reminder(access_token: str) -> str | None:
    try:
        resp = requests.get(
            f"{GRAPH_BASE}/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": access_token},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        new_token = data.get("access_token")
        expires_in_days = data.get("expires_in", 0) / 86400
        if new_token and new_token != access_token:
            print(f"🔄 Token refreshed (new one expires in ~{expires_in_days:.0f} days).")
            github_output = os.environ.get("GITHUB_OUTPUT")
            if github_output:
                with open(github_output, "a", encoding="utf-8") as f:
                    f.write(f"new_token={new_token}\n")
            return new_token
        elif expires_in_days and expires_in_days < 10:
            print(f"⚠️  WARNING: IG_ACCESS_TOKEN expires in ~{expires_in_days:.0f} days.")
    except Exception as e:
        print(f"(token-refresh check skipped: {e})")
    return None


# =============================================================================
# CLI
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description="Daily MB-EGX Instagram poster")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_render = sub.add_parser("render", help="Fetch data, pick highlights, write image + caption")
    p_render.add_argument(
        "--post-type", choices=["tickers", "market", "sectors"], default="tickers",
        help="Which of the 3 daily posts to render",
    )
    p_render.add_argument("--pages-base-url", default=os.environ.get("PAGES_BASE_URL"))
    p_render.add_argument("--out-dir", default=str(OUT_DIR))

    p_publish = sub.add_parser("publish", help="Publish an already-rendered image to Instagram")
    p_publish.add_argument("--image-url", required=True, help="Public URL of the rendered PNG")
    p_publish.add_argument("--caption-file", required=True)
    p_publish.add_argument("--ig-user-id", default=os.environ.get("IG_USER_ID"))
    p_publish.add_argument("--access-token", default=os.environ.get("IG_ACCESS_TOKEN"))

    p_publish_fb = sub.add_parser("publish-fb", help="Publish an already-rendered image to a Facebook Page")
    p_publish_fb.add_argument("--image-url", required=True, help="Public URL of the rendered PNG")
    p_publish_fb.add_argument("--caption-file", required=True)
    p_publish_fb.add_argument("--fb-page-id", default=os.environ.get("FB_PAGE_ID"))
    p_publish_fb.add_argument("--fb-access-token", default=os.environ.get("FB_PAGE_ACCESS_TOKEN"))

    args = parser.parse_args()

    if args.cmd == "render":
        if not args.pages_base_url:
            sys.exit("PAGES_BASE_URL is required (env var or --pages-base-url)")
        market_data = fetch_market_data(args.pages_base_url)
        out_dir = Path(args.out_dir)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if args.post_type == "tickers":
            highlights = pick_daily_highlights(market_data)
            render_post_image(highlights, out_dir / f"{date_str}_tickers.png")
            render_post_image(highlights, out_dir / "latest_tickers.png")
            caption = build_caption(highlights)
        elif args.post_type == "market":
            overview = compute_market_overview(market_data)
            render_market_overview_image(overview, out_dir / f"{date_str}_market.png")
            render_market_overview_image(overview, out_dir / "latest_market.png")
            caption = build_market_caption(overview)
        else:  # sectors
            sectors = pick_sector_highlights(market_data)
            last_data_date = market_data.get("last_data_date")
            render_sectors_image(sectors, last_data_date, out_dir / f"{date_str}_sectors.png")
            render_sectors_image(sectors, last_data_date, out_dir / "latest_sectors.png")
            caption = build_sectors_caption(sectors)

        caption_path = out_dir / f"latest_{args.post_type}_caption.txt"
        caption_path.write_text(caption, encoding="utf-8")
        print(f"✅ Rendered {args.post_type} post → {out_dir / f'latest_{args.post_type}.png'} and {caption_path}")

    elif args.cmd == "publish":
        if not args.ig_user_id or not args.access_token:
            sys.exit("IG_USER_ID and IG_ACCESS_TOKEN are required (env vars or --ig-user-id/--access-token)")
        refresh_reminder(args.access_token)
        caption = Path(args.caption_file).read_text(encoding="utf-8")
        media_id = publish_to_instagram(args.ig_user_id, args.access_token, args.image_url, caption)
        print(f"✅ Published to Instagram — media ID {media_id}")

    elif args.cmd == "publish-fb":
        if not args.fb_page_id or not args.fb_access_token:
            sys.exit("FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN are required (env vars or --fb-page-id/--fb-access-token)")
        caption = Path(args.caption_file).read_text(encoding="utf-8")
        post_id = publish_to_facebook(args.fb_page_id, args.fb_access_token, args.image_url, caption)
        print(f"✅ Published to Facebook — post ID {post_id}")


if __name__ == "__main__":
    main()
