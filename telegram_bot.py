"""
telegram_bot.py
================
N2 — interactive Telegram bot: replies to commands like /strongbuy,
/ticker COMI, /picks. This is the QUERY side of N2; the PUSH side
("a Session Pick just hit its target") is already handled by alerts.py,
which fires synchronously from session_picks.py's alert hook the moment
refresh_session_picks() detects an achievement - see alerts.py's own
docstring. This module does NOT duplicate that: broadcasting achievements
here again would double-post the same event through two different code
paths with two different dedup mechanisms. If you're looking for the
achievement push, that's alerts.py + config.SESSION_PICKS_TELEGRAM_WEBHOOK,
not this file.

WHY A POLL-ONCE-AND-EXIT SCRIPT (no server, no budget)
---------------------------------------------------------
Telegram's Bot API queues updates (messages) server-side until something
calls getUpdates and acknowledges them by offset - it does NOT require a
bot to be listening continuously. That means a short-lived script invoked
on a tight GitHub Actions schedule (see the companion telegram-bot.yml)
gets the same effective behavior as a always-on bot, for free: a command
sent between two scheduled runs simply waits in Telegram's queue until
the next run picks it up. This is the same "catch-up model" already used
throughout this codebase (daily-instagram-post.yml's schedule trigger,
post_state.py's due-check) - nothing here needs a paid host.

NOTE ON REPLY LATENCY: this script only runs when the GitHub Actions
schedule trigger fires (every 5 minutes as of telegram-bot.yml - GitHub
Actions doesn't reliably run tighter than that anyway). That cadence is
the practical floor for a free, schedule-only bot regardless of language.
For sub-second replies you'd need Telegram's webhook mode (Telegram pushes
to a URL of yours the instant a message arrives) behind a small always-on
endpoint (Cloudflare Worker / Vercel function / etc.) instead of this
poll-and-exit script - GitHub Actions itself can't receive inbound
webhooks.

WHAT IT READS
-------------
Only web_public/data/market_data.json - the exact same public,
already-privacy-scrubbed payload the web dashboard reads (see
export_json.py's privacy-fix comments: no cash balance, no real
positions, no per-user data). This script never touches the DuckDB
database directly - a bot reply can never leak more than the public
website already shows.

STATE
-----
web_public/social/telegram_state.json:
    { "update_offset": 123456789 }
Same small-JSON-file-committed-by-the-workflow pattern as post_state.py -
just the last processed Telegram update_id, so a re-run never re-replies
to the same message twice.

SETUP
-----
1. If you haven't already (for alerts.py's push alerts), message
   @BotFather on Telegram, /newbot, get a bot token.
2. Add it as the TELEGRAM_BOT_TOKEN repository secret (see config.py's
   comment on why this is separate from MBEGX_TELEGRAM_WEBHOOK).
3. Add the companion workflow (telegram-bot.yml) to .github/workflows/.
4. Message your bot /start on Telegram to try it.

COMMANDS (English)
-------------------
/start, /help    - what this bot can do
/strongbuy        - today's 🔥 STRONG BUY tickers
/breakout          - today's ⚡ BREAKOUT BUY tickers
/accumulate        - today's 📈 ACCUMULATE tickers
/dip               - today's ⏳ BUY ON DIP tickers
/sectors           - sector heatmap summary
/picks             - active Session Picks (all horizons)
/leaderboard        - top 10 leaderboard
/ticker SYMBOL     - full signal detail for one ticker

ARABIC SUPPORT
---------------
Any incoming message containing Arabic script is answered fully in
Arabic - no need to know the slash-command names. Slash commands still
work exactly as before too (e.g. an Arabic speaker can still type
/sectors and get an Arabic-labelled reply, since the reply language is
decided per-message, not per-command).

Natural-language Arabic triggers (see ARABIC_KEYWORDS below):
    قوي / شراء قوي        -> /strongbuy
    اختراق                -> /breakout
    تجميع / تراكم          -> /accumulate
    انخفاض / هبوط          -> /dip
    قطاع / قطاعات          -> /sectors
    توصيات / فرص           -> /picks
    متصدر / الأفضل         -> /leaderboard
    مساعدة / مرحبا / أهلا   -> /help
    سهم SYMBOL / سعر SYMBOL -> /ticker SYMBOL   (e.g. "سعر COMI")

Anything Arabic that doesn't match a known trigger gets a friendly
Arabic "didn't understand, here's what I can do" reply instead of the
generic English fallback.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from html import escape as _esc

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_STALE_POLICY, get_logger
from freshness import is_fresh

logger = get_logger("telegram_bot")

DATA_PATH = os.path.join("web_public", "data", "market_data.json")
STATE_PATH = os.path.join("web_public", "social", "telegram_state.json")
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"
MAX_UPDATES_PER_POLL = 50  # a few minutes' worth of commands, never unbounded
MAX_ROWS_PER_REPLY = 15    # keep replies readable on a phone screen; matrix can be 200+ rows

# ---------------------------------------------------------------------------
# Arabic detection + natural-language command matching
# ---------------------------------------------------------------------------

# Arabic (+ Arabic Supplement / Presentation Forms) unicode block - any hit
# anywhere in the message is enough to switch the reply language to Arabic.
_ARABIC_RE = re.compile(r'[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]')

# "سهم COMI" / "سعر comi" -> ticker lookup. Ticker symbols on EGX are Latin
# letters, so this still works fine inside an otherwise-Arabic sentence.
_ARABIC_TICKER_RE = re.compile(r'(?:سهم|سعر|تيكر)\s+([A-Za-z]{2,10})')

# Keyword -> command. Checked as a plain substring match against the raw
# message (Arabic has no case-folding concern here), first keyword that
# hits wins its command. Order matters only when a message could plausibly
# contain two of these - kept deliberately short/specific to avoid that.
ARABIC_KEYWORDS = {
    "/strongbuy":   ["شراء قوي", "توصية قوية", "قوي"],
    "/breakout":    ["اختراق"],
    "/accumulate":  ["تجميع", "تراكم"],
    "/dip":         ["انخفاض", "هبوط"],
    "/hold":        ["محايد", "احتفاظ"],
    "/sell":        ["بيع", "تجنب"],
    "/sectors":     ["قطاعات", "قطاع"],
    "/picks":       ["توصيات", "الفرص", "فرص اليوم"],
    "/leaderboard": ["المتصدرين", "متصدر", "الأفضل"],
    "/help":        ["مساعدة", "مرحبا", "أهلا", "السلام عليكم"],
}


def _is_arabic(text: str) -> bool:
    return bool(_ARABIC_RE.search(text or ""))


def _match_arabic_intent(text: str) -> str | None:
    for cmd, keywords in ARABIC_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return cmd
    return None


# ---------------------------------------------------------------------------
# Localized strings/labels - one dict per language, same keys throughout so
# every formatter just does LABELS[lang]["some_key"] instead of branching.
# ---------------------------------------------------------------------------

LABELS = {
    "en": {
        "score": "Score", "proj": "Proj.",
        "strong_buy_title": "🔥 Strong Buy",
        "strong_buy_empty": "No STRONG BUY signals right now.",
        "breakout_title": "⚡ Breakout Buy",
        "breakout_empty": "No BREAKOUT BUY signals right now.",
        "accumulate_title": "📈 Accumulate",
        "accumulate_empty": "No ACCUMULATE signals right now.",
        "dip_title": "⏳ Buy On Dip",
        "dip_empty": "No BUY ON DIP signals right now.",
        "hold_title": "🟡 Hold / Neutral",
        "hold_empty": "No HOLD / NEUTRAL signals right now.",
        "sell_title": "🛑 Sell / Avoid",
        "sell_empty": "No SELL / AVOID signals right now.",
        "sectors_title": "🏢 Sector Heatmap",
        "sectors_empty": "No sector data available.",
        "picks_title": "Session Picks",
        "picks_empty": "No active picks right now.",
        "horizon_short": "🚀 Next Session",
        "horizon_medium": "📈 Medium-Term",
        "horizon_long": "🏛️ Long-Term",
        "picked": "picked",
        "live": "live",
        "target": "target",
        "leaderboard_title": "🥇 Leaderboard",
        "leaderboard_empty": "No leaderboard data yet.",
        "hits": "hit(s)",
        "total": "total",
        "ticker_not_found": "No signal found for <b>{sym}</b>. Check the symbol and try again (e.g. /ticker COMI).",
        "ticker_price": "Price", "ticker_score_lbl": "Score",
        "ticker_entry": "Entry (VWAP)", "ticker_stop": "Stop-Loss",
        "ticker_tp": "Take-Profit", "ticker_proj": "Proj. Gain",
        "ticker_rsi": "RSI-14", "ticker_adx": "ADX-14", "ticker_trend": "Trend",
        "ticker_confidence": "Data Confidence",
        "more_rows": "… and {n} more. See the full dashboard for the rest.",
        "unknown": "Unknown command. Send /help to see what I can do.",
        "no_text_hint": "Send /help to see what I can do.",
        "no_data": "Market data isn't available right now - try again in a moment.",
        "stale_refuse": "🚫 Market data is from session {d} — not today's session. I won't answer with stale numbers.\n\nRun `python publish.py` after today's feed is captured, then message me again.",
        "ticker_usage": "Usage: /ticker SYMBOL (e.g. /ticker COMI)",
        "help": (
            "<b>MB-EGX Signal Bot</b>\n\n"
            "/strongbuy — today's 🔥 STRONG BUY tickers\n"
            "/breakout — today's ⚡ BREAKOUT BUY tickers\n"
            "/accumulate — today's 📈 ACCUMULATE tickers\n"
            "/dip — today's ⏳ BUY ON DIP tickers\n"
            "/hold — today's 🟡 HOLD / NEUTRAL tickers\n"
            "/sell — today's 🛑 SELL / AVOID tickers\n"
            "/sectors — sector heatmap summary\n"
            "/picks — active Session Picks\n"
            "/leaderboard — top 10 leaderboard\n"
            "/ticker SYMBOL — full detail for one ticker (e.g. /ticker COMI)\n\n"
            "Data is the same public feed as the web dashboard, refreshed on the same schedule. "
            "Achievement alerts (a pick hitting its target) are pushed automatically - no command needed.\n\n"
            "بتتكلم عربي؟ اكتب رسالتك بالعربي وهرد عليك عربي (مثال: \"قطاعات\"، \"توصيات\"، \"سعر COMI\")."
        ),
    },
    "ar": {
        "score": "التقييم", "proj": "العائد المتوقع",
        "strong_buy_title": "🔥 شراء قوي",
        "strong_buy_empty": "لا توجد إشارات شراء قوية حالياً.",
        "breakout_title": "⚡ اختراق للشراء",
        "breakout_empty": "لا توجد إشارات اختراق حالياً.",
        "accumulate_title": "📈 تجميع",
        "accumulate_empty": "لا توجد إشارات تجميع حالياً.",
        "dip_title": "⏳ شراء عند الانخفاض",
        "dip_empty": "لا توجد إشارات شراء عند الانخفاض حالياً.",
        "hold_title": "🟡 احتفاظ / محايد",
        "hold_empty": "لا توجد إشارات احتفاظ / محايد حالياً.",
        "sell_title": "🛑 بيع / تجنب",
        "sell_empty": "لا توجد إشارات بيع / تجنب حالياً.",
        "sectors_title": "🏢 خريطة القطاعات",
        "sectors_empty": "لا توجد بيانات قطاعات متاحة.",
        "picks_title": "الفرص المختارة (Session Picks)",
        "picks_empty": "لا توجد فرص نشطة حالياً.",
        "horizon_short": "🚀 الجلسة القادمة",
        "horizon_medium": "📈 متوسط المدى",
        "horizon_long": "🏛️ طويل المدى",
        "picked": "تاريخ الاختيار",
        "live": "الحالي",
        "target": "الهدف",
        "leaderboard_title": "🥇 المتصدرين",
        "leaderboard_empty": "لا توجد بيانات متصدرين بعد.",
        "hits": "إصابة/إصابات",
        "total": "الإجمالي",
        "ticker_not_found": "لا توجد إشارة للسهم <b>{sym}</b>. تأكد من الرمز وحاول مرة أخرى (مثال: سعر COMI).",
        "ticker_price": "السعر", "ticker_score_lbl": "التقييم",
        "ticker_entry": "سعر الدخول (VWAP)", "ticker_stop": "وقف الخسارة",
        "ticker_tp": "هدف جني الأرباح", "ticker_proj": "العائد المتوقع",
        "ticker_rsi": "مؤشر RSI-14", "ticker_adx": "مؤشر ADX-14", "ticker_trend": "الاتجاه",
        "ticker_confidence": "مستوى الثقة في البيانات",
        "more_rows": "… و{n} سهم إضافي. راجع لوحة البيانات الكاملة لباقي النتائج.",
        "unknown": "لم أفهم طلبك. اكتب \"مساعدة\" لمعرفة الأوامر المتاحة.",
        "no_text_hint": "اكتب \"مساعدة\" لمعرفة ما يمكنني فعله.",
        "no_data": "بيانات السوق غير متاحة حالياً - حاول مرة أخرى بعد قليل.",
        "stale_refuse": "🚫 بيانات السوق من جلسة {d} — وليست جلسة اليوم. لن أرد بأرقام قديمة.\n\nشغّل publish.py بعد تحديث بيانات اليوم ثم راسلني مجددًا.",
        "ticker_usage": "الاستخدام: سعر SYMBOL (مثال: سعر COMI)",
        "help": (
            "<b>بوت إشارات MB-EGX</b>\n\n"
            "شراء قوي — أسهم 🔥 الشراء القوي اليوم\n"
            "اختراق — أسهم ⚡ الاختراق اليوم\n"
            "تجميع — أسهم 📈 التجميع اليوم\n"
            "انخفاض — أسهم ⏳ الشراء عند الانخفاض اليوم\n"
            "محايد — أسهم 🟡 الاحتفاظ / المحايد اليوم\n"
            "بيع — أسهم 🛑 البيع / التجنب اليوم\n"
            "قطاعات — ملخص خريطة القطاعات\n"
            "توصيات — الفرص المختارة النشطة\n"
            "متصدرين — قائمة أفضل 10 أسهم\n"
            "سعر SYMBOL — تفاصيل كاملة لسهم معين (مثال: سعر COMI)\n\n"
            "البيانات هي نفس البيانات العامة المعروضة على الموقع، ويتم تحديثها بنفس الجدول الزمني. "
            "تنبيهات تحقيق الأهداف تُرسل تلقائياً بدون الحاجة لأي أمر."
        ),
    },
}


# ---------------------------------------------------------------------------
# Formatting - every formatter takes the already-loaded market_data.json
# dict (+ the arg it needs, + lang) and returns an HTML-formatted string.
# Kept small/composable so a new command is just one more formatter + one
# more dispatch entry, and localization is just LABELS[lang][...] lookups
# rather than a second parallel set of functions.
# ---------------------------------------------------------------------------

def _rows_for_action(data: dict, needle: str) -> list:
    return [r for r in data.get("market_matrix", []) if needle in (r.get("Action") or "")]


def _format_row_line(row: dict, lang: str) -> str:
    L = LABELS[lang]
    ticker = _esc(str(row.get("Ticker", "?")))
    price = row.get("Current Price", "-")
    score = row.get("Rank Score", "-")
    gain = row.get("Projected Gain (%)", "N/A")
    return f"• <b>{ticker}</b>  {price}  ({L['score']} {score}, {L['proj']} {gain}%)"


def _format_action_list(data: dict, needle: str, title_key: str, empty_key: str, lang: str) -> str:
    L = LABELS[lang]
    rows = sorted(_rows_for_action(data, needle), key=lambda r: r.get("Rank Score", 0), reverse=True)
    header = f"<b>{L[title_key]}</b> — {_esc(str(data.get('last_data_date', 'N/A')))}\n\n"
    if not rows:
        return header + L[empty_key]
    lines = [_format_row_line(r, lang) for r in rows[:MAX_ROWS_PER_REPLY]]
    body = "\n".join(lines)
    if len(rows) > MAX_ROWS_PER_REPLY:
        body += "\n" + L["more_rows"].format(n=len(rows) - MAX_ROWS_PER_REPLY)
    return header + body


def format_strong_buy(data: dict, arg: str, lang: str) -> str:
    return _format_action_list(data, "STRONG BUY", "strong_buy_title", "strong_buy_empty", lang)


def format_breakout(data: dict, arg: str, lang: str) -> str:
    return _format_action_list(data, "BREAKOUT BUY", "breakout_title", "breakout_empty", lang)


def format_accumulate(data: dict, arg: str, lang: str) -> str:
    return _format_action_list(data, "ACCUMULATE", "accumulate_title", "accumulate_empty", lang)


def format_dip(data: dict, arg: str, lang: str) -> str:
    return _format_action_list(data, "BUY ON DIP", "dip_title", "dip_empty", lang)


def format_hold(data: dict, arg: str, lang: str) -> str:
    return _format_action_list(data, "HOLD / NEUTRAL", "hold_title", "hold_empty", lang)


def format_sell(data: dict, arg: str, lang: str) -> str:
    return _format_action_list(data, "SELL / AVOID", "sell_title", "sell_empty", lang)


def format_sectors(data: dict, arg: str, lang: str) -> str:
    L = LABELS[lang]
    sectors = data.get("sectors", [])
    if not sectors:
        return L["sectors_empty"]
    sectors = sorted(sectors, key=lambda s: s.get("1D Return (%)", s.get("Bullish Breadth (%)", 0)), reverse=True)
    lines = [f"<b>{L['sectors_title']}</b>\n"]
    for s in sectors[:MAX_ROWS_PER_REPLY]:
        name = _esc(str(s.get("Sector", "?")))
        status = _esc(str(s.get("Sector Status", "")))
        chg = s.get("1D Return (%)", "-")
        lines.append(f"• <b>{name}</b>  {chg}%  {status}")
    return "\n".join(lines)


def _best_chart_pattern(stock_history: dict | None) -> str | None:
    """Best (highest-quality, bullish-preferred) chart pattern for a
    ticker from chart_history.stocks.<ticker>.patterns, as a short label
    like 'Double Bottom (bullish, 0.88)'. None if the ticker has no
    patterns (or no chart history)."""
    if not stock_history:
        return None
    patterns = stock_history.get("patterns") or []
    if not patterns:
        return None

    def _key(p):
        bull = 1 if str(p.get("direction", "")).lower() == "bullish" else 0
        q = p.get("quality")
        return (bull, float(q) if isinstance(q, (int, float)) else 0.0)

    best = max(patterns, key=_key)
    name = best.get("pattern")
    if not name:
        return None
    direction = str(best.get("direction", "")).lower()
    q = best.get("quality")
    q_str = f", {q:.2f}" if isinstance(q, (int, float)) else ""
    dir_str = f" ({direction}{q_str})" if direction else ""
    return f"{name}{dir_str}"


def format_picks(data: dict, arg: str, lang: str) -> str:
    L = LABELS[lang]
    sp = data.get("session_picks", {})
    # Live prices come from the same market_matrix the dashboard uses, so
    # the reply shows the CURRENT move since each pick was made (updated
    # info) rather than a stale fixed pick date.
    matrix = {r.get("Ticker"): r for r in data.get("market_matrix", [])}
    stocks = (data.get("chart_history") or {}).get("stocks", {})
    horizon_titles = {
        "short": L["horizon_short"],
        "medium": L["horizon_medium"],
        "long": L["horizon_long"],
    }
    lines = [f"<b>{L['picks_title']}</b> — {_esc(str(sp.get('session_date', 'N/A')))}\n"]
    any_picks = False
    for horizon, title in horizon_titles.items():
        picks = sp.get(horizon, [])
        if not picks:
            continue
        any_picks = True
        lines.append(f"\n<b>{title}</b>")
        for p in picks:
            ticker = str(p.get("ticker", "?"))
            ref = p.get("ref_price")
            cur = matrix.get(ticker, {}).get("Current Price")
            live = None
            if cur is not None and ref:
                live = (cur / ref - 1.0) * 100.0
            live_str = f" · {L['live']} {live:+.2f}%" if live is not None else ""
            pat = _best_chart_pattern(stocks.get(ticker))
            pat_str = f" · {_esc(pat)}" if pat else ""
            lines.append(
                f"• <b>{_esc(ticker)}</b> — {L['target']} +{p.get('expected_pct', '?')}%"
                f"{live_str}{pat_str}"
            )
    if not any_picks:
        lines.append(L["picks_empty"])
    return "\n".join(lines)


def format_leaderboard(data: dict, arg: str, lang: str) -> str:
    L = LABELS[lang]
    board = data.get("leaderboard", [])
    if not board:
        return L["leaderboard_empty"]
    lines = [f"<b>{L['leaderboard_title']}</b>\n"]
    for i, r in enumerate(board[:10], start=1):
        ticker = _esc(str(r.get("ticker", "?")))
        hits = r.get("hits", 0)
        total = r.get("total_return_pct", 0)
        lines.append(f"{i}. <b>{ticker}</b> — {hits} {L['hits']}, {total}% {L['total']}")
    return "\n".join(lines)


def format_ticker(data: dict, arg: str, lang: str) -> str:
    L = LABELS[lang]
    symbol = (arg or "").strip().upper()
    if not symbol:
        return L["ticker_usage"]
    row = next((r for r in data.get("market_matrix", []) if (r.get("Ticker") or "").upper() == symbol), None)
    if not row:
        return L["ticker_not_found"].format(sym=_esc(symbol))
    lines = [
        f"<b>{_esc(str(row.get('Ticker')))}</b> — {_esc(str(row.get('Action', '-')))}",
        f"{L['ticker_price']}: {row.get('Current Price', '-')}   {L['ticker_score_lbl']}: {row.get('Rank Score', '-')}",
        f"{L['ticker_entry']}: {row.get('Target Entry (VWAP)', '-')}   {L['ticker_stop']}: {row.get('Suggested Stop-Loss', '-')}",
        f"{L['ticker_tp']}: {row.get('Take-Profit Target', '-')}   {L['ticker_proj']}: {row.get('Projected Gain (%)', 'N/A')}%",
        f"{L['ticker_rsi']}: {row.get('RSI-14', '-')}   {L['ticker_adx']}: {row.get('ADX-14', '-')}   {L['ticker_trend']}: {_esc(str(row.get('Trend Class', '-')))}",
        f"{L['ticker_confidence']}: {_esc(str(row.get('Data Confidence', '-')))}",
    ]
    return "\n".join(lines)


def format_help(arg: str, lang: str) -> str:
    return LABELS[lang]["help"]


COMMAND_HANDLERS = {
    "/start": lambda data, arg, lang: format_help(arg, lang),
    "/help": lambda data, arg, lang: format_help(arg, lang),
    "/strongbuy": format_strong_buy,
    "/breakout": format_breakout,
    "/accumulate": format_accumulate,
    "/dip": format_dip,
    "/hold": format_hold,
    "/sell": format_sell,
    "/sectors": format_sectors,
    "/picks": format_picks,
    "/leaderboard": format_leaderboard,
    "/ticker": format_ticker,
}


def handle_command(text: str, data: dict | None, stale_date: str | None = None) -> str:
    """Resolves any incoming message - an English /slash command, a plain
    Arabic phrase, or an Arabic "سهم/سعر SYMBOL" ticker lookup - to a
    handler + arg + reply language, then dispatches.

    Reply language is decided per-message (Arabic script anywhere in the
    text => Arabic reply), independent of whether the command itself was
    typed as a slash command or matched via ARABIC_KEYWORDS - so an Arabic
    speaker who types "/sectors" still gets an Arabic-labelled reply, and
    "سعر COMI" resolves to the exact same handler as "/ticker COMI" does.
    """
    text = (text or "").strip()
    lang = "ar" if _is_arabic(text) else "en"

    cmd, arg = None, ""

    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()  # strip @BotName suffix (group chats append it)
        arg = parts[1] if len(parts) > 1 else ""
    else:
        ticker_match = _ARABIC_TICKER_RE.search(text)
        if ticker_match:
            cmd, arg = "/ticker", ticker_match.group(1)
        elif lang == "ar":
            cmd = _match_arabic_intent(text)
        # lang == "en" and no leading "/" and no ticker match: cmd stays None,
        # falls through to the English no-text-hint below.

    if cmd is None:
        return LABELS[lang]["no_text_hint"]

    handler = COMMAND_HANDLERS.get(cmd)
    if not handler:
        return LABELS[lang]["unknown"]
    if data is None and cmd not in ("/start", "/help"):
        return LABELS[lang]["no_data"]
    reply = handler(data, arg, lang)
    if stale_date and cmd not in ("/start", "/help") and TELEGRAM_STALE_POLICY == "REFUSE":
        return LABELS[lang]["stale_refuse"].format(d=_esc(stale_date))
    if stale_date and cmd not in ("/start", "/help"):
        warn = (
            f"⚠️ Data is from session {stale_date} — today's session isn't published yet "
            f"(run publish.py after market close).\n\n"
            if lang == "en" else
            f"⚠️ البيانات من جلسة {stale_date} — لم تُنشر بيانات جلسة اليوم بعد "
            f"(شغّل publish.py بعد إغلاق السوق).\n\n"
        )
        return warn + reply
    return reply


def _load_market_data() -> dict | None:
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"update_offset": 0}
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        state = {}
    state.setdefault("update_offset", 0)
    return state


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _api_url(method: str) -> str:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set - see this module's SETUP docstring.")
    return f"{TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN)}/{method}"


def send_message(chat_id, text: str) -> bool:
    try:
        resp = requests.post(
            _api_url("sendMessage"),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15,
        )
        if not resp.ok:
            logger.warning(f"Telegram sendMessage failed ({resp.status_code}): {resp.text[:300]}")
        return resp.ok
    except requests.RequestException as e:
        logger.warning(f"Telegram sendMessage error: {e}")
        return False


def poll_once() -> int:
    """Fetches any new messages since the last saved offset, replies to
    each, advances and saves the offset. Returns the number of updates
    processed (0 is normal - most polls will have nothing new)."""
    state = _load_state()
    data = _load_market_data()
    # ROOT-CAUSE FIX: never answer with the previous session's numbers.
    # The bot had no freshness gate - a stale market_data.json was
    # answered as if it were today's. Flag it loudly instead.
    _fresh, _last, _today = is_fresh()
    stale_date = None if _fresh else _last

    resp = requests.get(
        _api_url("getUpdates"),
        params={"offset": state["update_offset"], "timeout": 0, "limit": MAX_UPDATES_PER_POLL},
        timeout=20,
    )
    resp.raise_for_status()
    updates = resp.json().get("result", [])

    processed = 0
    for update in updates:
        state["update_offset"] = update["update_id"] + 1
        message = update.get("message") or update.get("edited_message")
        if not message or "text" not in message:
            continue
        chat_id = message["chat"]["id"]
        reply = handle_command(message["text"], data, stale_date=stale_date)
        send_message(chat_id, reply)
        processed += 1

    _save_state(state)
    return processed


def main():
    parser = argparse.ArgumentParser(description="Interactive Telegram bot for the MB-EGX dashboard (N2 - query side).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("poll", help="Fetch and reply to any new Telegram messages since the last run.")
    args = parser.parse_args()

    if args.cmd == "poll":
        n = poll_once()
        print(f"Processed {n} update(s).")


if __name__ == "__main__":
    main()
