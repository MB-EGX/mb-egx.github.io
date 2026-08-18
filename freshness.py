"""
freshness.py
============
Single source of truth for "is the committed market_data.json the session
we are supposed to be publishing right now?" — the root-cause fix for the
recurring "posts / emails / bot replies carry the previous session" bug.

WHY THIS FILE EXISTS
--------------------
Every publishing channel used to decide freshness on its own — and three of
the four had NO check at all:

  * post_state.py        — gates the Instagram/Facebook/Telegram-channel
                           workflow (the ONLY channel that had a gate).
  * send_email_digest.py — had no gate: a stale market_data.json was emailed
                           as if it were today's digest.
  * telegram_bot.py      — had no gate: /strongbuy, /picks etc. answered with
                           the previous session's numbers.
  * social_poster.py     — render step trusted the fetched file
                           unconditionally, so any run that bypassed
                           post_state (manual workflow_dispatch, a future
                           refactor) silently posted stale cards.

The result: whenever the committed market_data.json still held the previous
session (publish.py not run after that day's close, or the feed lagged),
three of four channels published the previous session's results as if they
were today's.

THE CONTRACT
------------
The "target session" for a daily publish is today's date in Cairo
(Africa/Cairo), because the EGX trading day and every due time in
post_state.py are Cairo-local. A data file is FRESH only when its
``last_data_date`` equals today's Cairo date. Anything else is STALE
(previous session, or today's data not caught up yet) and MUST NOT be
published.

FAIL-LOUD, NEVER SILENT
-----------------------
Callers use :func:`assert_fresh` (or check :func:`is_fresh` /
:func:`is_fresh_data`) and refuse to render/send when stale — a stale file
is never silently posted. A missed publish (because data wasn't ready) is a
visible, actionable gap; a wrong-session publish is a silent, recurring lie.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

CAIRO = ZoneInfo("Africa/Cairo")

# Where the published, privacy-scrubbed payload lives (same path every
# channel already reads).
DEFAULT_DATA_PATH = os.path.join("web_public", "data", "market_data.json")


def today_cairo() -> str:
    """Today's date in Cairo local time, as YYYY-MM-DD — the single
    definition of 'which session are we publishing for'."""
    return datetime.now(CAIRO).strftime("%Y-%m-%d")


def load_last_data_date(path: str | None = None) -> str | None:
    """The session date the committed market_data.json actually represents
    (its ``last_data_date`` field), or None if missing/unreadable."""
    path = path or DEFAULT_DATA_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("last_data_date")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def is_fresh(path: str | None = None, target: str | None = None) -> tuple[bool, str | None, str]:
    """File-based freshness check.

    Returns ``(fresh, last_data_date, target)`` where ``fresh`` is True
    only when the file's session date equals ``target`` (default: today
    Cairo). Never raises — a missing/unreadable file is simply not fresh.
    """
    target = target or today_cairo()
    last = load_last_data_date(path)
    return (last == target), last, target


def is_fresh_data(data: dict | None, target: str | None = None) -> tuple[bool, str | None, str]:
    """Dict-based freshness check — for channels that already hold the
    parsed payload (e.g. social_poster.py after fetch_market_data). Same
    contract as :func:`is_fresh`."""
    target = target or today_cairo()
    last = (data or {}).get("last_data_date")
    return (last == target), last, target


def assert_fresh(path: str | None = None, target: str | None = None,
                 channel: str = "publish") -> str:
    """Fail-loud freshness gate. Returns the fresh session date on success;
    raises SystemExit with a clear message when the data is stale, so no
    channel can silently publish the previous session."""
    fresh, last, today = is_fresh(path, target)
    if fresh:
        return last  # guaranteed == target
    if last is None:
        raise SystemExit(
            f"[{channel}] market_data.json is missing or unreadable at "
            f"{path or DEFAULT_DATA_PATH} — refusing to publish. "
            f"Run publish.py first."
        )
    raise SystemExit(
        f"[{channel}] REFUSING to publish: market_data.json is for session "
        f"{last}, but today's session is {today}. The committed data is "
        f"stale (publish.py hasn't ingested today's feed yet). "
        f"Run publish.py after today's session closes, then re-run this step. "
        f"Nothing was published."
    )
