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

COMMANDS
--------
/start, /help    - what this bot can do
/strongbuy        - today's 🔥 STRONG BUY tickers
/breakout          - today's ⚡ BREAKOUT BUY tickers
/accumulate        - today's 📈 ACCUMULATE tickers
/dip               - today's ⏳ BUY ON DIP tickers
/sectors           - sector heatmap summary
/picks             - active Session Picks (all horizons)
/leaderboard        - top 10 leaderboard
/ticker SYMBOL     - full signal detail for one ticker
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from html import escape as _esc

import requests

from config import TELEGRAM_BOT_TOKEN, get_logger

logger = get_logger("telegram_bot")

DATA_PATH = os.path.join("web_public", "data", "market_data.json")
STATE_PATH = os.path.join("web_public", "social", "telegram_state.json")
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"
MAX_UPDATES_PER_POLL = 50  # a few minutes' worth of commands, never unbounded
MAX_ROWS_PER_REPLY = 15    # keep replies readable on a phone screen; matrix can be 200+ rows


def _api_url(method: str) -> str:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set - see this module's SETUP docstring.")
    return f"{TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN)}/{method}"


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


# ---------------------------------------------------------------------------
# Formatting - every formatter takes the already-loaded market_data.json
# dict and returns an HTML-formatted string. Kept small/composable so a
# new command is just one more formatter + one more dispatch entry.
# ---------------------------------------------------------------------------

def _rows_for_action(data: dict, needle: str) -> list:
    return [r for r in data.get("market_matrix", []) if needle in (r.get("Action") or "")]


def _format_row_line(row: dict) -> str:
    ticker = _esc(str(row.get("Ticker", "?")))
    price = row.get("Current Price", "-")
    score = row.get("Rank Score", "-")
    gain = row.get("Projected Gain (%)", "N/A")
    return f"• <b>{ticker}</b>  {price}  (Score {score}, Proj. {gain}%)"


def _format_action_list(data: dict, needle: str, title: str, empty_msg: str) -> str:
    rows = sorted(_rows_for_action(data, needle), key=lambda r: r.get("Rank Score", 0), reverse=True)
    header = f"<b>{title}</b> — {_esc(str(data.get('last_data_date', 'N/A')))}\n\n"
    if not rows:
        return header + empty_msg
    lines = [_format_row_line(r) for r in rows[:MAX_ROWS_PER_REPLY]]
    body = "\n".join(lines)
    if len(rows) > MAX_ROWS_PER_REPLY:
        body += f"\n… and {len(rows) - MAX_ROWS_PER_REPLY} more. See the full dashboard for the rest."
    return header + body


def format_strong_buy(data: dict) -> str:
    return _format_action_list(data, "STRONG BUY", "🔥 Strong Buy", "No STRONG BUY signals right now.")


def format_breakout(data: dict) -> str:
    return _format_action_list(data, "BREAKOUT BUY", "⚡ Breakout Buy", "No BREAKOUT BUY signals right now.")


def format_accumulate(data: dict) -> str:
    return _format_action_list(data, "ACCUMULATE", "📈 Accumulate", "No ACCUMULATE signals right now.")


def format_dip(data: dict) -> str:
    return _format_action_list(data, "BUY ON DIP", "⏳ Buy On Dip", "No BUY ON DIP signals right now.")


def format_sectors(data: dict) -> str:
    sectors = data.get("sectors", [])
    if not sectors:
        return "No sector data available."
    sectors = sorted(sectors, key=lambda s: s.get("1D Return (%)", s.get("Bullish Breadth (%)", 0)), reverse=True)
    lines = ["<b>🏢 Sector Heatmap</b>\n"]
    for s in sectors[:MAX_ROWS_PER_REPLY]:
        name = _esc(str(s.get("Sector", "?")))
        status = _esc(str(s.get("Sector Status", "")))
        chg = s.get("1D Return (%)", "-")
        lines.append(f"• <b>{name}</b>  {chg}%  {status}")
    return "\n".join(lines)


def format_picks(data: dict) -> str:
    sp = data.get("session_picks", {})
    horizon_titles = {"short": "🚀 Next Session", "medium": "📈 Medium-Term", "long": "🏛️ Long-Term"}
    lines = [f"<b>Session Picks</b> — {_esc(str(sp.get('session_date', 'N/A')))}\n"]
    any_picks = False
    for horizon, title in horizon_titles.items():
        picks = sp.get(horizon, [])
        if not picks:
            continue
        any_picks = True
        lines.append(f"\n<b>{title}</b>")
        for p in picks:
            lines.append(
                f"• {_esc(str(p.get('ticker', '?')))} — target +{p.get('expected_pct', '?')}% "
                f"(picked {_esc(str(p.get('pick_date', '')))})"
            )
    if not any_picks:
        lines.append("No active picks right now.")
    return "\n".join(lines)


def format_leaderboard(data: dict) -> str:
    board = data.get("leaderboard", [])
    if not board:
        return "No leaderboard data yet."
    lines = ["<b>🥇 Leaderboard</b>\n"]
    for i, r in enumerate(board[:10], start=1):
        ticker = _esc(str(r.get("ticker", "?")))
        hits = r.get("hits", 0)
        total = r.get("total_return_pct", 0)
        lines.append(f"{i}. <b>{ticker}</b> — {hits} hit(s), {total}% total")
    return "\n".join(lines)


def format_ticker(data: dict, symbol: str) -> str:
    symbol = symbol.strip().upper()
    row = next((r for r in data.get("market_matrix", []) if (r.get("Ticker") or "").upper() == symbol), None)
    if not row:
        return f"No signal found for <b>{_esc(symbol)}</b>. Check the symbol and try again (e.g. /ticker COMI)."
    lines = [
        f"<b>{_esc(str(row.get('Ticker')))}</b> — {_esc(str(row.get('Action', '-')))}",
        f"Price: {row.get('Current Price', '-')}   Score: {row.get('Rank Score', '-')}",
        f"Entry (VWAP): {row.get('Target Entry (VWAP)', '-')}   Stop-Loss: {row.get('Suggested Stop-Loss', '-')}",
        f"Take-Profit: {row.get('Take-Profit Target', '-')}   Proj. Gain: {row.get('Projected Gain (%)', 'N/A')}%",
        f"RSI-14: {row.get('RSI-14', '-')}   ADX-14: {row.get('ADX-14', '-')}   Trend: {_esc(str(row.get('Trend Class', '-')))}",
        f"Data Confidence: {_esc(str(row.get('Data Confidence', '-')))}",
    ]
    return "\n".join(lines)


def format_help() -> str:
    return (
        "<b>MB-EGX Signal Bot</b>\n\n"
        "/strongbuy — today's 🔥 STRONG BUY tickers\n"
        "/breakout — today's ⚡ BREAKOUT BUY tickers\n"
        "/accumulate — today's 📈 ACCUMULATE tickers\n"
        "/dip — today's ⏳ BUY ON DIP tickers\n"
        "/sectors — sector heatmap summary\n"
        "/picks — active Session Picks\n"
        "/leaderboard — top 10 leaderboard\n"
        "/ticker SYMBOL — full detail for one ticker (e.g. /ticker COMI)\n\n"
        "Data is the same public feed as the web dashboard, refreshed on the same schedule. "
        "Achievement alerts (a pick hitting its target) are pushed automatically - no command needed."
    )


COMMAND_HANDLERS = {
    "/start": lambda data, arg: format_help(),
    "/help": lambda data, arg: format_help(),
    "/strongbuy": lambda data, arg: format_strong_buy(data),
    "/breakout": lambda data, arg: format_breakout(data),
    "/accumulate": lambda data, arg: format_accumulate(data),
    "/dip": lambda data, arg: format_dip(data),
    "/sectors": lambda data, arg: format_sectors(data),
    "/picks": lambda data, arg: format_picks(data),
    "/leaderboard": lambda data, arg: format_leaderboard(data),
    "/ticker": lambda data, arg: format_ticker(data, arg) if arg else "Usage: /ticker SYMBOL (e.g. /ticker COMI)",
}


def handle_command(text: str, data: dict | None) -> str:
    text = (text or "").strip()
    if not text.startswith("/"):
        return "Send /help to see what I can do."
    parts = text.split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower()  # strip @BotName suffix (group chats append it)
    arg = parts[1] if len(parts) > 1 else ""
    handler = COMMAND_HANDLERS.get(cmd)
    if not handler:
        return "Unknown command. Send /help to see what I can do."
    if data is None and cmd not in ("/start", "/help"):
        return "Market data isn't available right now - try again in a moment."
    return handler(data, arg)


def poll_once() -> int:
    """Fetches any new messages since the last saved offset, replies to
    each, advances and saves the offset. Returns the number of updates
    processed (0 is normal - most polls will have nothing new)."""
    state = _load_state()
    data = _load_market_data()

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
        reply = handle_command(message["text"], data)
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
