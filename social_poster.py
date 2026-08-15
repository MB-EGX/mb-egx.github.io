"""
social_poster.py
=================
Generates and publishes daily Instagram/Facebook posts: the best ticker
for the next session, the best medium-term setup, and the best
long-term/sector pick (post-type "tickers"); a market overview and a
sectors overview; an "achievement" post the moment a Session Pick crosses
its horizon's target; and a "track_record" post showing the recent
history of picks that actually hit their target ("daily, if present" —
see post_state.py). Each with the evidence (score, RSI/ADX, pattern,
sector strength) straight from the same market_data.json the web
dashboard already reads.

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
# 2. PICK THE DAILY HIGHLIGHTS
# =============================================================================
# The horizons/quotas here come straight from the app's own Session Picks
# watchlist (session_picks.py / config.SESSION_PICKS_QUOTA) — up to 5
# next-session, 3 medium-term, 3 long-term tickers, already auto-picked
# and auto-refilled by DecisionMatrix.analyze_market(). This no longer
# re-derives its own "best 1" pick from top10/sectors like the old
# single-ticker version did — it just reads the SAME picks the desktop
# app's Session Picks tab shows, so the social post and the app can never
# disagree about what's currently on the watchlist.
_HORIZON_LABELS = {"short": "next_session", "medium": "medium_term", "long": "long_term"}


def _enrich_pick(pick: dict, by_ticker: dict) -> dict:
    """Merge a stored pick (ticker/pick_date/ref_price) with that ticker's
    current matrix row (score, RSI, price, ...) plus the % move since it
    was picked, for display purposes."""
    row = dict(by_ticker.get(pick.get("ticker"), {}))
    ref_price = pick.get("ref_price")
    current_price = row.get("Current Price")
    pct_since_pick = None
    if current_price is not None and ref_price:
        pct_since_pick = round((current_price / ref_price - 1.0) * 100.0, 2)
    row.update(pick)
    row["pct_since_pick"] = pct_since_pick
    return row


def pick_daily_highlights(market_data: dict) -> dict:
    matrix = market_data.get("market_matrix", [])
    by_ticker = {r["Ticker"]: r for r in matrix}
    session_picks = market_data.get("session_picks", {})

    result = {
        _HORIZON_LABELS[h]: [_enrich_pick(p, by_ticker) for p in session_picks.get(h, [])]
        for h in ("short", "medium", "long")
    }
    result["last_data_date"] = market_data.get("last_data_date")
    return result


# =============================================================================
# 2a2. TRACK RECORD (history of achieved picks — "correct expectations")
# =============================================================================
# Reads session_picks.achieved_history (see export_json.py / session_picks.py
# / db_manager.get_recent_achieved_picks) — the full recent list of Session
# Picks that actually hit their horizon's target, not just today's. This is
# what powers the "daily, if present" track_record post: post_state.py
# already refuses to mark it due at all when this list is empty (see that
# module's docstring), so by the time this runs there's guaranteed to be at
# least one entry.
def pick_track_record_highlights(market_data: dict, limit: int = 10) -> list[dict]:
    history = market_data.get("session_picks", {}).get("achieved_history", [])
    return history[:limit]


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


def _draw_pick_row(draw, x, y, w, row_h, pick, fonts):
    f_ticker, f_body = fonts
    draw.rounded_rectangle([x, y + 4, x + w, y + row_h - 6], radius=8, fill=PANEL)
    ticker = pick.get("Ticker") or pick.get("ticker", "—")
    draw.text((x + 20, y + 12), ticker, font=f_ticker, fill=TEXT_MAIN)

    bits = []
    score = pick.get("Rank Score")
    if score is not None:
        bits.append(f"score {score}/100")
    price = pick.get("Current Price")
    if price is not None:
        bits.append(f"{price} EGP")
    pct = pick.get("pct_since_pick")
    if pct is not None:
        bits.append(f"{pct:+.2f}% since picked")
    draw.text((x + 240, y + 16), "  ·  ".join(bits) or "—", font=f_body, fill=TEXT_MUTED)


def render_post_image(highlights: dict, out_path: Path) -> Path:
    """One card listing every active Session Pick, grouped by horizon —
    up to 5 next-session + 3 medium-term + 3 long-term rows (see
    config.SESSION_PICKS_QUOTA). Height grows with however many picks are
    actually active so a thin watchlist doesn't leave a mostly-empty card
    and a full one never gets cut off. Each section header shows that
    horizon's own target % gain (config.SESSION_PICKS_EXPECTED_PCT), read
    off the first pick in that bucket since every pick in a bucket shares
    the same horizon-wide target — falls back to config directly if the
    bucket is empty."""
    from config import SESSION_PICKS_EXPECTED_PCT

    def _section_title(base_title, horizon_key, rows):
        pct = (rows[0].get("expected_pct") if rows else None) or SESSION_PICKS_EXPECTED_PCT.get(horizon_key)
        return f"{base_title} (target +{pct:.0f}%)" if pct is not None else base_title

    sections = [
        (_section_title("🚀 NEXT SESSION", "short", highlights.get("next_session", [])), highlights.get("next_session", []), GREEN),
        (_section_title("📈 MEDIUM TERM", "medium", highlights.get("medium_term", [])), highlights.get("medium_term", []), ACCENT),
        (_section_title("🏛️ LONG TERM", "long", highlights.get("long_term", [])), highlights.get("long_term", []), AMBER),
    ]
    ROW_H, HEADER_H, SECTION_H, FOOTER_H = 62, 200, 46, 70
    total_rows = sum(max(len(rows), 1) for _, rows, _ in sections)
    height = HEADER_H + SECTION_H * len(sections) + ROW_H * total_rows + FOOTER_H + 30

    img = Image.new("RGB", (1080, height), BG)
    draw = ImageDraw.Draw(img)

    f_brand = _font("DejaVuSans-Bold.ttf", 44)
    f_tagline = _font("DejaVuSans.ttf", 24)
    f_date = _font("DejaVuSans.ttf", 22)
    f_section = _font("DejaVuSans-Bold.ttf", 26)
    f_row_ticker = _font("DejaVuSans-Bold.ttf", 26)
    f_row_body = _font("DejaVuSans.ttf", 20)

    draw.text((40, 40), "MB-EGX", font=f_brand, fill=ACCENT)
    draw.text((40, 96), "Session Picks", font=f_tagline, fill=TEXT_MUTED)
    date_str = highlights.get("last_data_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    draw.text((40, 130), f"Market data as of {date_str}", font=f_date, fill=TEXT_MUTED)

    y = HEADER_H
    for title, rows, accent in sections:
        draw.text((40, y), title, font=f_section, fill=accent)
        y += SECTION_H
        if not rows:
            draw.text((60, y + 10), "No active picks right now", font=f_row_body, fill=TEXT_MUTED)
            y += ROW_H
            continue
        for pick in rows:
            _draw_pick_row(draw, 40, y, 1000, ROW_H, pick, (f_row_ticker, f_row_body))
            y += ROW_H

    f_disclaimer = _font("DejaVuSans.ttf", 18)
    draw.text(
        (40, height - 44),
        "Educational content, not investment advice. Not financial advice.",
        font=f_disclaimer, fill=TEXT_MUTED,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


def render_achievement_image(achievements: list[dict], last_data_date: str | None, out_path: Path) -> Path:
    """One card announcing every Session Pick that crossed its horizon's
    own target % gain this run (config.SESSION_PICKS_EXPECTED_PCT — see
    session_picks.py)."""
    ROW_H, HEADER_H, FOOTER_H = 74, 200, 70
    height = HEADER_H + ROW_H * max(len(achievements), 1) + FOOTER_H + 30

    img = Image.new("RGB", (1080, height), BG)
    draw = ImageDraw.Draw(img)

    f_brand = _font("DejaVuSans-Bold.ttf", 44)
    f_tagline = _font("DejaVuSans.ttf", 24)
    f_date = _font("DejaVuSans.ttf", 22)
    f_ticker = _font("DejaVuSans-Bold.ttf", 30)
    f_body = _font("DejaVuSans.ttf", 22)
    f_pct = _font("DejaVuSans-Bold.ttf", 30)

    draw.text((40, 40), "MB-EGX", font=f_brand, fill=ACCENT)
    draw.text((40, 96), "🎯 Session Pick Achieved!", font=f_tagline, fill=GREEN)
    date_str = last_data_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    draw.text((40, 130), f"Market data as of {date_str}", font=f_date, fill=TEXT_MUTED)

    y = HEADER_H
    horizon_labels = {"short": "Next Session", "medium": "Medium-Term", "long": "Long-Term"}
    for a in achievements:
        draw.rounded_rectangle([40, y + 4, 1040, y + ROW_H - 10], radius=10, fill=PANEL)
        draw.text((60, y + 12), a["ticker"], font=f_ticker, fill=TEXT_MAIN)
        label = horizon_labels.get(a.get("horizon"), a.get("horizon", ""))
        draw.text((280, y + 10), label, font=f_body, fill=ACCENT)
        draw.text((280, y + 38), f"picked {a['pick_date']} @ {a['ref_price']} EGP", font=f_body, fill=TEXT_MUTED)
        draw.text((820, y + 20), f"+{a['achieved_pct']:.2f}%", font=f_pct, fill=GREEN)
        y += ROW_H

    f_disclaimer = _font("DejaVuSans.ttf", 18)
    draw.text(
        (40, height - 44),
        "Educational content, not investment advice. Not financial advice.",
        font=f_disclaimer, fill=TEXT_MUTED,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


def render_track_record_image(history: list[dict], last_data_date: str | None, out_path: Path) -> Path:
    """One card listing our recent track record — Session Picks that
    actually hit their horizon's target (session_picks.achieved_history,
    already capped to `limit` by pick_track_record_highlights). Visually
    close to render_achievement_image (same row style) since both show
    "picked X, hit target Y%", but this one is a rolling history rather
    than a single run's fresh crossings."""
    ROW_H, HEADER_H, FOOTER_H = 74, 200, 70
    height = HEADER_H + ROW_H * max(len(history), 1) + FOOTER_H + 30

    img = Image.new("RGB", (1080, height), BG)
    draw = ImageDraw.Draw(img)

    f_brand = _font("DejaVuSans-Bold.ttf", 44)
    f_tagline = _font("DejaVuSans.ttf", 24)
    f_date = _font("DejaVuSans.ttf", 22)
    f_ticker = _font("DejaVuSans-Bold.ttf", 30)
    f_body = _font("DejaVuSans.ttf", 22)
    f_pct = _font("DejaVuSans-Bold.ttf", 30)

    draw.text((40, 40), "MB-EGX", font=f_brand, fill=ACCENT)
    draw.text((40, 96), "📜 Track Record — Calls That Hit", font=f_tagline, fill=GREEN)
    date_str = last_data_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    draw.text((40, 130), f"Market data as of {date_str}", font=f_date, fill=TEXT_MUTED)

    y = HEADER_H
    horizon_labels = {"short": "Next Session", "medium": "Medium-Term", "long": "Long-Term"}
    for a in history:
        draw.rounded_rectangle([40, y + 4, 1040, y + ROW_H - 10], radius=10, fill=PANEL)
        draw.text((60, y + 12), a["ticker"], font=f_ticker, fill=TEXT_MAIN)
        label = horizon_labels.get(a.get("horizon"), a.get("horizon", ""))
        draw.text((280, y + 10), label, font=f_body, fill=ACCENT)
        draw.text((280, y + 38), f"picked {a['pick_date']} → hit {a['achieved_date']}", font=f_body, fill=TEXT_MUTED)
        draw.text((820, y + 20), f"+{a['achieved_pct']:.2f}%", font=f_pct, fill=GREEN)
        y += ROW_H

    f_disclaimer = _font("DejaVuSans.ttf", 18)
    draw.text(
        (40, height - 44),
        "Past calls hitting target does not guarantee future results. Educational content, not investment advice.",
        font=f_disclaimer, fill=TEXT_MUTED,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


# =============================================================================
# 4. CAPTION 
# =============================================================================
def _maybe_override_caption(kind: str, text_block: str) -> str:
    override = os.environ.get(f"MBEGX_{kind.upper()}_CAPTION_OVERRIDE", "").strip()
    return override if override else text_block


def append_promotional_footer(text_block: str) -> str:
    footer = (
        "\n\n"
        "Follow Facebook Page: https://www.facebook.com/profile.php?id=61593012092507\n"
        "Follow Instagram: https://www.instagram.com/mb_egx/\n"
        "Login App: https://mb-egx.github.io/"
    )
    return text_block + footer


def _pick_lines_en(rows: list[dict]) -> list[str]:
    if not rows:
        return ["  (no active picks right now)"]
    return [
        f"  • {r.get('Ticker') or r.get('ticker', '—')} — score {r.get('Rank Score', '—')}/100, RSI {r.get('RSI-14', '—')}"
        for r in rows
    ]


def _pick_lines_ar(rows: list[dict]) -> list[str]:
    if not rows:
        return ["  (لا توجد ترشيحات نشطة حاليًا)"]
    return [
        f"  • {r.get('Ticker') or r.get('ticker', '—')} — الدرجة {r.get('Rank Score', '—')}/100، RSI {r.get('RSI-14', '—')}"
        for r in rows
    ]


def _target_pct(rows: list[dict], horizon_key: str) -> float | None:
    """Horizon-wide target % gain (config.SESSION_PICKS_EXPECTED_PCT),
    read off the first pick if the bucket has one, else straight from
    config — so an empty bucket still shows its target."""
    from config import SESSION_PICKS_EXPECTED_PCT
    if rows and rows[0].get("expected_pct") is not None:
        return rows[0]["expected_pct"]
    return SESSION_PICKS_EXPECTED_PCT.get(horizon_key)


def build_caption(highlights: dict) -> str:
    ns, mt, lt = highlights["next_session"], highlights["medium_term"], highlights["long_term"]
    pct_ns, pct_mt, pct_lt = _target_pct(ns, "short"), _target_pct(mt, "medium"), _target_pct(lt, "long")

    en = [
        "📊 MB-EGX Session Picks",
        "",
        f"🔥 Next session (target +{pct_ns:.0f}%):" if pct_ns is not None else "🔥 Next session:",
        *_pick_lines_en(ns),
        "",
        f"📈 Medium term (target +{pct_mt:.0f}%):" if pct_mt is not None else "📈 Medium term:",
        *_pick_lines_en(mt),
        "",
        f"🏛️ Long term (target +{pct_lt:.0f}%):" if pct_lt is not None else "🏛️ Long term:",
        *_pick_lines_en(lt),
        "",
        "This is educational content generated by a mechanical multi-factor model reading price, volume, and trend data. It is NOT investment advice — always do your own research and apply your own judgment.",
        "",
        "#EGX #MBEGX #EgyptStockMarket #StockMarket #Investing #Trading #EGYPT",
    ]

    ar = [
        "📊 ترشيحات الجلسة — MB-EGX",
        "",
        f"🔥 الجلسة القادمة (الهدف +{pct_ns:.0f}%):" if pct_ns is not None else "🔥 الجلسة القادمة:",
        *_pick_lines_ar(ns),
        "",
        f"📈 متوسط المدى (الهدف +{pct_mt:.0f}%):" if pct_mt is not None else "📈 متوسط المدى:",
        *_pick_lines_ar(mt),
        "",
        f"🏛️ طويل المدى (الهدف +{pct_lt:.0f}%):" if pct_lt is not None else "🏛️ طويل المدى:",
        *_pick_lines_ar(lt),
        "",
        "هذا محتوى تعليمي من نموذج آلي متعدد العوامل يحلل بيانات السعر والحجم والاتجاه، وليس نصيحة استثمارية — قم دائمًا بأبحاثك الخاصة واستخدم حكمك الشخصي.",
    ]

    combined_caption = "\n".join(en) + "\n\n————\n\n" + "\n".join(ar)
    return append_promotional_footer(_maybe_override_caption("session", combined_caption))


def build_achievement_caption(achievements: list[dict]) -> str:
    from config import SESSION_PICKS_EXPECTED_PCT
    horizon_labels_en = {"short": "Next Session", "medium": "Medium-Term", "long": "Long-Term"}
    horizon_labels_ar = {"short": "الجلسة القادمة", "medium": "متوسط المدى", "long": "طويل المدى"}

    en = ["🎯 MB-EGX Session Pick(s) Achieved!", ""]
    for a in achievements:
        label = horizon_labels_en.get(a.get("horizon"), a.get("horizon", ""))
        target = SESSION_PICKS_EXPECTED_PCT.get(a.get("horizon"))
        target_str = f", target was +{target:.0f}%" if target is not None else ""
        en.append(
            f"✅ {a['ticker']} ({label}{target_str}): +{a['achieved_pct']:.2f}% since picked on "
            f"{a['pick_date']} @ {a['ref_price']} EGP"
        )
    en += [
        "",
        "Flagged on our Session Picks watchlist before the move. Educational content generated by a mechanical multi-factor model — NOT investment advice.",
        "",
        "#EGX #MBEGX #EgyptStockMarket #StockMarket #Investing #Trading #EGYPT",
    ]

    ar = ["🎯 تحقق هدف ترشيح! — MB-EGX", ""]
    for a in achievements:
        label = horizon_labels_ar.get(a.get("horizon"), a.get("horizon", ""))
        target = SESSION_PICKS_EXPECTED_PCT.get(a.get("horizon"))
        target_str = f"، الهدف كان +{target:.0f}%" if target is not None else ""
        ar.append(
            f"✅ {a['ticker']} ({label}{target_str}): +{a['achieved_pct']:.2f}% منذ الترشيح بتاريخ "
            f"{a['pick_date']} بسعر {a['ref_price']} جنيه"
        )
    ar += [
        "",
        "تم رصد هذه الفرصة في قائمة ترشيحات الجلسة قبل الحركة. محتوى تعليمي من نموذج آلي — وليس نصيحة استثمارية.",
    ]

    combined_caption = "\n".join(en) + "\n\n————\n\n" + "\n".join(ar)
    return append_promotional_footer(_maybe_override_caption("achievement", combined_caption))


def build_track_record_caption(history: list[dict]) -> str:
    horizon_labels_en = {"short": "Next Session", "medium": "Medium-Term", "long": "Long-Term"}
    horizon_labels_ar = {"short": "الجلسة القادمة", "medium": "متوسط المدى", "long": "طويل المدى"}

    en = ["📜 MB-EGX Track Record — Calls That Hit", ""]
    for a in history:
        label = horizon_labels_en.get(a.get("horizon"), a.get("horizon", ""))
        en.append(
            f"✅ {a['ticker']} ({label}): +{a['achieved_pct']:.2f}% — picked "
            f"{a['pick_date']} @ {a['ref_price']} EGP, hit target on {a['achieved_date']} @ {a['achieved_price']} EGP"
        )
    en += [
        "",
        "A rolling look back at recent Session Picks that reached their target. Past calls hitting target does not guarantee future results — educational content generated by a mechanical multi-factor model, NOT investment advice.",
        "",
        "#EGX #MBEGX #EgyptStockMarket #StockMarket #Investing #Trading #EGYPT",
    ]

    ar = ["📜 سجل الأداء — MB-EGX", ""]
    for a in history:
        label = horizon_labels_ar.get(a.get("horizon"), a.get("horizon", ""))
        ar.append(
            f"✅ {a['ticker']} ({label}): +{a['achieved_pct']:.2f}% — تم الترشيح بتاريخ "
            f"{a['pick_date']} بسعر {a['ref_price']} جنيه، وتحقق الهدف بتاريخ {a['achieved_date']} بسعر {a['achieved_price']} جنيه"
        )
    ar += [
        "",
        "نظرة على أحدث ترشيحات الجلسة التي حققت هدفها. تحقق الهدف سابقًا لا يضمن نتائج مماثلة مستقبلًا — محتوى تعليمي من نموذج آلي، وليس نصيحة استثمارية.",
    ]

    combined_caption = "\n".join(en) + "\n\n————\n\n" + "\n".join(ar)
    return append_promotional_footer(_maybe_override_caption("track_record", combined_caption))


def build_market_caption(overview: dict) -> str:
    if overview.get("total", 0) == 0:
        en = ["📊 MB-EGX Market Overview", "", "No data available today."]
        ar = ["📊 نظرة عامة على السوق — MB-EGX", "", "لا توجد بيانات متاحة اليوم."]
        combined_caption = "\n".join(en) + "\n\n————\n\n" + "\n".join(ar)
        return append_promotional_footer(_maybe_override_caption("market", combined_caption))

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
    return append_promotional_footer(_maybe_override_caption("sectors", combined_caption))


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


def build_telegram_caption(highlights: dict) -> str:
    rows = []
    for title, bucket in (("Next session", highlights.get("next_session", [])), ("Medium term", highlights.get("medium_term", [])), ("Long term", highlights.get("long_term", []))):
        if bucket:
            rows.append(title + ': ' + ', '.join((r.get("Ticker") or r.get("ticker", "—")) for r in bucket[:3]))
    return "\n".join(rows) if rows else "No active highlights."


def build_shorts_payload(post_type: str, caption: str) -> dict:
    return {
        "post_type": post_type,
        "aspect_ratio": "9:16",
        "headline": caption.splitlines()[0] if caption else "MB-EGX",
        "caption": caption,
    }

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
        "--post-type", choices=["tickers", "market", "sectors", "achievement", "track_record"], default="tickers",
        help="Which post to render",
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
        elif args.post_type == "sectors":
            sectors = pick_sector_highlights(market_data)
            last_data_date = market_data.get("last_data_date")
            render_sectors_image(sectors, last_data_date, out_dir / f"{date_str}_sectors.png")
            render_sectors_image(sectors, last_data_date, out_dir / "latest_sectors.png")
            caption = build_sectors_caption(sectors)
        elif args.post_type == "achievement":
            achievements = market_data.get("session_picks", {}).get("achieved_today", [])
            last_data_date = market_data.get("last_data_date")
            if not achievements:
                print("⚠️  No achieved Session Picks in market_data.json — nothing to render.")
                return
            render_achievement_image(achievements, last_data_date, out_dir / f"{date_str}_achievement.png")
            render_achievement_image(achievements, last_data_date, out_dir / "latest_achievement.png")
            caption = build_achievement_caption(achievements)
        else:  # track_record
            history = pick_track_record_highlights(market_data)
            last_data_date = market_data.get("last_data_date")
            if not history:
                print("⚠️  No achieved_history in market_data.json yet — nothing to render.")
                return
            render_track_record_image(history, last_data_date, out_dir / f"{date_str}_track_record.png")
            render_track_record_image(history, last_data_date, out_dir / "latest_track_record.png")
            caption = build_track_record_caption(history)

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
