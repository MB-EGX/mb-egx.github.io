"""
alerts.py
=========
Push-alert channels for session_picks.py's existing ALERT_CHANNELS /
register_alert_channel() hook (see that module's docstring). The hook
already existed and already fires on every "pick_achieved" event from
refresh_session_picks() — nothing was ever actually listening. This adds
one real listener: Telegram, via config.SESSION_PICKS_TELEGRAM_WEBHOOK.

WHY TELEGRAM FIRST: it's the only channel in config.py (SESSION_PICKS_
TELEGRAM_WEBHOOK / MBEGX_TELEGRAM_WEBHOOK) that needs nothing but a bot
token — no paid API, no domain/SPF/DKIM setup like email, no app-store
review like a push notification. A Telegram bot is free to create via
@BotFather; SESSION_PICKS_TELEGRAM_WEBHOOK should be set to
``https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>`` (a
plain POST to that URL with a JSON body is all sendMessage needs).

Auto-registration: this module registers its Telegram channel with
session_picks.py the moment it's imported, IF the webhook is configured.
It is imported for its side effect (see the bottom of this file and
session_picks.py's own import of it) from wherever session_picks already
runs — the desktop app's "Execute Matrix" button and the nightly
publish.py pipeline both call DecisionMatrix.analyze_market(), which is
session_picks.py's own single entry point (see that module's docstring),
so importing it there is enough to cover both paths with one line, no
per-caller wiring needed.

If SESSION_PICKS_TELEGRAM_WEBHOOK is empty (the default), nothing is
registered and this module is a no-op import — no network calls, no
behavior change for anyone who hasn't set the env var.
"""
from __future__ import annotations

import json
from urllib import request as _urlrequest
from urllib.error import URLError

from config import SESSION_PICKS_TELEGRAM_WEBHOOK, get_logger

logger = get_logger("alerts")


def _post_telegram_message(text: str) -> bool:
    """POSTs ``text`` to SESSION_PICKS_TELEGRAM_WEBHOOK. Returns False
    (and logs a warning) on any failure instead of raising — an alert
    channel must never be able to break the matrix run that triggered
    it (see session_picks._emit_alert's own try/except around every
    callback, which this relies on as a second line of defense)."""
    if not SESSION_PICKS_TELEGRAM_WEBHOOK:
        return False
    body = json.dumps({"text": text, "parse_mode": "HTML", "disable_web_page_preview": True}).encode("utf-8")
    req = _urlrequest.Request(
        SESSION_PICKS_TELEGRAM_WEBHOOK, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with _urlrequest.urlopen(req, timeout=15) as resp:
            ok = 200 <= resp.status < 300
            if not ok:
                logger.warning(f"Telegram alert returned HTTP {resp.status}")
            return ok
    except URLError as e:
        logger.warning(f"Telegram alert failed: {e}")
        return False


def send_telegram_pick_achieved(event_type: str, payload: dict) -> None:
    """session_picks.py ALERT_CHANNELS callback signature: (event_type,
    payload). Only handles 'pick_achieved' for now — other event types
    are silently ignored so a future new event type doesn't crash an
    already-deployed channel that doesn't know about it yet."""
    if event_type != "pick_achieved":
        return
    ticker = payload.get("ticker", "?")
    horizon = payload.get("horizon", "?")
    achieved_pct = payload.get("achieved_pct")
    ref_price = payload.get("ref_price")
    achieved_price = payload.get("achieved_price")
    pct_str = f"+{achieved_pct}%" if achieved_pct is not None else "?"
    text = (
        f"🚀 <b>{ticker}</b> hit its {horizon}-term Session Pick target: {pct_str}\n"
        f"Picked at {ref_price} → achieved at {achieved_price}"
    )
    _post_telegram_message(text)


def send_telegram_concentration_breach(event_type: str, payload: dict) -> None:
    """session_picks.py ALERT_CHANNELS callback signature: (event_type,
    payload). Handles 'concentration_breach' - fired from
    decision_matrix.py the moment a position or sector crosses
    config.PORTFOLIO_RISK_THRESHOLDS (see session_picks.emit_alert's
    dedup, which keeps this from re-firing every run while the breach
    stays unresolved). Ignores any other event type for the same
    forward-compatibility reason as send_telegram_pick_achieved above.
    """
    if event_type != "concentration_breach":
        return
    message = payload.get("message", "")
    if not message:
        return
    text = f"⚠️ <b>Portfolio concentration alert</b>\n{message}"
    _post_telegram_message(text)


def send_telegram_pre_breakout_high_confidence(event_type: str, payload: dict) -> None:
    """session_picks.py ALERT_CHANNELS callback signature: (event_type,
    payload). Handles 'pre_breakout_high_confidence' - fired from
    decision_matrix.py for any still-coiling (not yet fired) name whose
    Pre-Breakout Watchlist score clears config.ACTION_THRESHOLDS[
    "breakout_watch_alert_score"]. This is the proactive half of the
    watchlist: a name can score well and just sit in a table nobody
    happens to open that session, which is exactly the kind of miss a
    push notification is for. Deduped by session_picks.emit_alert over
    config.PRE_BREAKOUT_ALERT_DEDUP_DAYS so a name sitting near the top
    of the list for a week doesn't re-push every single run.
    """
    if event_type != "pre_breakout_high_confidence":
        return
    ticker = payload.get("Ticker", "?")
    score = payload.get("Breakout Score", "?")
    price = payload.get("Current Price", "?")
    dist = payload.get("Dist. to Resistance (%)", "?")
    signals = payload.get("Signals", "")
    text = (
        f"🎯 <b>{ticker}</b> — new High-Confidence Pre-Breakout entrant (score {score})\n"
        f"Price {price} · {dist}% from resistance\n"
        f"{signals}"
    )
    _post_telegram_message(text)


def send_telegram_usd_divergence(event_type: str, payload: dict) -> None:
    """session_picks.py ALERT_CHANNELS callback signature: (event_type,
    payload). Handles 'usd_divergence_detected' - fired from
    decision_matrix.py the moment usd_divergence.py's live snapshot
    flips to a real "bearish" or "bullish" verdict between EGX30 (EGP)
    and EGX30 (USD) (see that module's docstring for what each means).
    Deduped by session_picks.emit_alert per DIRECTION (see the emit_alert
    call site in decision_matrix.py) so an unresolved divergence doesn't
    re-push every run, but a flip to the opposite direction does.
    """
    if event_type != "usd_divergence_detected":
        return
    divergence = payload.get("divergence", "?")
    note = payload.get("note", "")
    icon = "⚠️" if divergence == "bearish" else "💡"
    text = f"{icon} <b>EGX30 EGP/USD Divergence — {divergence.title()}</b>\n{note}"
    _post_telegram_message(text)


def register_default_channels() -> None:
    """Idempotent — safe to call from multiple import sites."""
    if not SESSION_PICKS_TELEGRAM_WEBHOOK:
        return
    import session_picks  # local import: avoids a hard import-time
    # dependency from session_picks.py -> alerts.py -> session_picks.py;
    # session_picks.py imports THIS module for its side effect instead.
    session_picks.register_alert_channel(send_telegram_pick_achieved)
    session_picks.register_alert_channel(send_telegram_concentration_breach)
    session_picks.register_alert_channel(send_telegram_pre_breakout_high_confidence)
    session_picks.register_alert_channel(send_telegram_usd_divergence)


register_default_channels()
