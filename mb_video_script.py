"""
mb_video_script.py
==================
Auto-generates MB's Arabic video script from live market_data.json.

MB is a friendly, slightly cheeky Egyptian trading buddy who speaks
colloquial Egyptian Arabic (عامية) — never formal MSA.
Catchphrase open : "اهلا يا صحبي!"
Catchphrase close : "ودايمًا فاكر قبل ما تدوس!"

Script types (mirrors social_poster.py post_types):
  - weekly_wrap   : Friday recap — top movers, EGX30 trend, MB verdict
  - top_mover     : Single best performing ticker today with evidence
  - achievement   : A Session Pick just crossed its target gain
  - session_picks : Today's active picks snapshot (short/medium/long)

All returned scripts are plain Arabic strings, ready for TTS.
Kept import-free of heavy dependencies — only stdlib + json.
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pct(val) -> str:
    """Format a float as e.g. '+5.2%' or '-3.1%'."""
    if val is None:
        return "—"
    sign = "+" if float(val) >= 0 else ""
    return f"{sign}{float(val):.1f}%"


def _ticker_arabic(ticker: str) -> str:
    """
    Strip the .CA suffix so it sounds natural when read aloud in Arabic.
    e.g.  'COMI.CA'  ->  'كومي'  (just returns the root, TTS handles pronunciation)
    We keep it Latin; ElevenLabs Arabic voices handle mixed-script fine.
    """
    return ticker.replace(".CA", "").replace(".EGX", "EGX")


def _regime_ar(regime: str) -> str:
    mapping = {
        "bull": "صاعد",
        "bear": "هابط",
        "neutral": "محايد",
        "sideways": "جانبي",
    }
    return mapping.get(str(regime).lower(), regime)


def _horizon_ar(horizon: str) -> str:
    return {"short": "قصير المدى", "medium": "متوسط المدى", "long": "طويل المدى"}.get(
        horizon, horizon
    )


# ---------------------------------------------------------------------------
# Script builders  (each returns a single Arabic string ≤ ~200 words)
# ---------------------------------------------------------------------------

def script_top_mover(market_data: dict) -> str:
    """
    'MB reacts to today's top mover' — auto-generated from market_matrix.
    Picks the ticker with the highest rank_score that has a BUY-class action.
    """
    matrix = market_data.get("market_matrix", [])
    if not matrix:
        return ""

    BUY_ACTIONS = {"STRONG BUY", "BREAKOUT BUY", "BUY ON DIP", "ACCUMULATE"}
    buys = [
        r for r in matrix
        if any(b in str(r.get("action", "")).upper() for b in BUY_ACTIONS)
    ]
    if not buys:
        return ""

    top = max(buys, key=lambda r: float(r.get("rank_score", 0) or 0))
    ticker  = _ticker_arabic(top.get("ticker", ""))
    score   = top.get("rank_score", 0)
    rsi     = top.get("rsi14", "—")
    adx     = top.get("adx", "—")
    action  = top.get("action", "")
    pattern = top.get("pattern", "")
    sector  = top.get("sector", "")

    # build Arabic evidence line
    evidence_parts = []
    if rsi:
        evidence_parts.append(f"RSI عنده {rsi:.0f}" if isinstance(rsi, float) else f"RSI {rsi}")
    if adx:
        evidence_parts.append(f"ADX {adx:.0f}" if isinstance(adx, float) else f"ADX {adx}")
    if pattern:
        evidence_parts.append(f"نمط {pattern}")
    evidence = "، ".join(evidence_parts) if evidence_parts else "إشارات متعددة مؤكدة"

    script = (
        f"اهلا يا صحبي! 👋\n\n"
        f"النهارده MB شايف إن {ticker} هو الأبرز في الجلسة.\n\n"
        f"الـ action بتاعته '{action}'، وسكوره {score:.0f} نقطة — "
        f"وده بيجي من {evidence}.\n\n"
    )

    if sector:
        script += f"القطاع بتاعه '{sector}' كمان بيديه دعم إضافي.\n\n"

    script += (
        f"طبعًا دي مش نصيحة استثمارية — دي بس قراءة الماتريكس.\n"
        f"اتفرج على الشارت وابحث كويس قبل ما تقرر.\n\n"
        f"ودايمًا فاكر قبل ما تدوس! 🚀"
    )
    return script


def script_weekly_wrap(market_data: dict) -> str:
    """
    Friday weekly wrap — top gainers, worst losers, EGX30 regime, MB verdict.
    """
    matrix  = market_data.get("market_matrix", [])
    sectors = market_data.get("sectors", [])
    regime_data = market_data.get("market_regime", {})
    session_date = market_data.get("last_data_date", "")

    # top 3 gainers / losers by rank_score
    sorted_m = sorted(matrix, key=lambda r: float(r.get("rank_score", 0) or 0), reverse=True)
    top3  = sorted_m[:3]
    bot3  = sorted(matrix, key=lambda r: float(r.get("rank_score", 0) or 0))[:3]

    top_names = "، ".join(_ticker_arabic(r["ticker"]) for r in top3 if r.get("ticker"))
    bot_names = "، ".join(_ticker_arabic(r["ticker"]) for r in bot3 if r.get("ticker"))

    # EGX30 regime
    egx30_regime = "—"
    benchmarks = regime_data.get("benchmarks", {})
    for bm in benchmarks.values() if isinstance(benchmarks, dict) else []:
        if isinstance(bm, dict) and ".EGX30" in str(bm.get("ticker", "")):
            egx30_regime = _regime_ar(bm.get("regime", ""))
            break
    # fallback: flat regime field
    if egx30_regime == "—":
        egx30_regime = _regime_ar(regime_data.get("regime", "محايد"))

    # best sector
    best_sector = ""
    if sectors:
        best = max(sectors, key=lambda s: float(s.get("avg_return", 0) or 0), default=None)
        if best:
            best_sector = best.get("sector", "")

    script = (
        f"اهلا يا صحبي! 👋 — ملخص الأسبوع مع MB!\n\n"
        f"الأسبوع ده جلسة {session_date}، والـ EGX30 جو {egx30_regime}.\n\n"
        f"أقوى الأسهم على الماتريكس الأسبوع ده: {top_names}.\n"
        f"والأضعف في التقييم: {bot_names}.\n\n"
    )

    if best_sector:
        script += f"أحسن قطاع أداءً كان '{best_sector}'.\n\n"

    script += (
        f"خد بالك إن الأرقام دي بتتغير كل جلسة — السوق مش ثابت.\n"
        f"تابع MB كل يوم عشان تبقى دايمًا على علم.\n\n"
        f"ودايمًا فاكر قبل ما تدوس! 📊"
    )
    return script


def script_achievement(market_data: dict) -> str:
    """
    An 'achievement' post: a Session Pick just crossed its target gain.
    """
    picks   = market_data.get("session_picks", {})
    achieved = picks.get("achieved_today", [])
    if not achieved:
        return ""

    # celebrate up to 3
    lines = []
    for p in achieved[:3]:
        ticker = _ticker_arabic(p.get("ticker", ""))
        pct    = _pct(p.get("achieved_pct"))
        horizon = _horizon_ar(p.get("horizon", ""))
        lines.append(f"{ticker} حقق {pct} ({horizon})")

    celebrates = "\n".join(f"✅ {l}" for l in lines)
    extra = ""
    if len(achieved) > 3:
        extra = f"\n...وأكتر من كده! إجمالي {len(achieved)} أسهم وصلوا هدفهم النهارده."

    script = (
        f"اهلا يا صحبي! 👋\n\n"
        f"خبر كويس النهارده — Session Picks بتاعت MB وصلت هدفها!\n\n"
        f"{celebrates}{extra}\n\n"
        f"ده مش صدفة — ده الماتريكس بيشتغل!\n"
        f"وطبعًا التاريخ مش ضمان للمستقبل — بس الأرقام بتتكلم.\n\n"
        f"ودايمًا فاكر قبل ما تدوس! 🎯"
    )
    return script


def script_weekly_achievement(market_data: dict) -> str:
    """
    Weekly video: celebrates every Session Pick achieved in the trailing
    7 days (market_data['session_picks']['achieved_this_week'], populated
    by the updated session_picks.py). Falls back to 'achieved_today' if the
    market_data.json wasn't regenerated with the newer session_picks.py yet
    (older exports won't have the 'achieved_this_week' field at all).
    """
    picks = market_data.get("session_picks", {})
    achieved = picks.get("achieved_this_week")
    if achieved is None:
        achieved = picks.get("achieved_today", [])
    if not achieved:
        return ""

    # celebrate up to 5 — a week can reasonably have more hits than a day
    lines = []
    for p in achieved[:5]:
        ticker  = _ticker_arabic(p.get("ticker", ""))
        pct     = _pct(p.get("achieved_pct"))
        horizon = _horizon_ar(p.get("horizon", ""))
        lines.append(f"{ticker} حقق {pct} ({horizon})")

    celebrates = "\n".join(f"✅ {l}" for l in lines)
    extra = ""
    if len(achieved) > 5:
        extra = f"\n...وأكتر من كده! إجمالي {len(achieved)} أسهم وصلوا هدفهم الأسبوع ده."

    script = (
        f"اهلا يا صحبي! 👋\n\n"
        f"ملخص الأسبوع — Session Picks بتاعت MB وصلت هدفها!\n\n"
        f"{celebrates}{extra}\n\n"
        f"ده مش صدفة — ده الماتريكس بيشتغل!\n"
        f"وطبعًا التاريخ مش ضمان للمستقبل — بس الأرقام بتتكلم.\n\n"
        f"ودايمًا فاكر قبل ما تدوس! 🎯"
    )
    return script


def script_session_picks(market_data: dict) -> str:
    """
    Today's active Session Picks snapshot — short / medium / long.
    """
    picks = market_data.get("session_picks", {})
    session_date = picks.get("session_date") or market_data.get("last_data_date", "")

    short_picks  = picks.get("short",  [])[:3]
    medium_picks = picks.get("medium", [])[:2]
    long_picks   = picks.get("long",   [])[:2]

    def fmt_pick(p):
        return (
            f"{_ticker_arabic(p['ticker'])} "
            f"(هدف +{p.get('expected_pct', '?')}%)"
        )

    lines = []
    if short_picks:
        names = "، ".join(fmt_pick(p) for p in short_picks)
        lines.append(f"قصير المدى: {names}")
    if medium_picks:
        names = "، ".join(fmt_pick(p) for p in medium_picks)
        lines.append(f"متوسط المدى: {names}")
    if long_picks:
        names = "، ".join(fmt_pick(p) for p in long_picks)
        lines.append(f"طويل المدى: {names}")

    if not lines:
        return ""

    body = "\n".join(f"• {l}" for l in lines)
    script = (
        f"اهلا يا صحبي! 👋\n\n"
        f"دي Session Picks بتاعة MB ليوم {session_date}:\n\n"
        f"{body}\n\n"
        f"الأسهم دي اتختارت بناءً على الماتريكس — سكور عالي، "
        f"تحليل فني قوي، وتأكيد قطاعي.\n"
        f"شوف التفاصيل في التطبيق.\n\n"
        f"ودايمًا فاكر قبل ما تدوس! 📈"
    )
    return script


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

SCRIPT_TYPES = {
    "top_mover":          script_top_mover,
    "weekly_wrap":        script_weekly_wrap,
    "achievement":        script_achievement,
    "weekly_achievement": script_weekly_achievement,
    "session_picks":      script_session_picks,
}


def generate_script(
    script_type: str,
    market_data_path: str = "web_public/data/market_data.json",
    market_data: Optional[dict] = None,
) -> str:
    """
    Public entry point.

    Args:
        script_type: one of 'top_mover', 'weekly_wrap', 'achievement', 'session_picks'
        market_data_path: path to market_data.json (ignored if market_data supplied)
        market_data: pre-loaded dict (for testing / when called from another module)

    Returns:
        Arabic script string, or '' if data insufficient.
    """
    if market_data is None:
        path = Path(market_data_path)
        if not path.exists():
            raise FileNotFoundError(f"market_data.json not found at {path}")
        with path.open("r", encoding="utf-8") as fh:
            market_data = json.load(fh)

    builder = SCRIPT_TYPES.get(script_type)
    if builder is None:
        raise ValueError(
            f"Unknown script_type '{script_type}'. "
            f"Choose from: {list(SCRIPT_TYPES)}"
        )
    return builder(market_data)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Generate MB Arabic video script")
    ap.add_argument(
        "--type",
        choices=list(SCRIPT_TYPES),
        default="top_mover",
        help="Script type to generate",
    )
    ap.add_argument(
        "--data",
        default="web_public/data/market_data.json",
        help="Path to market_data.json",
    )
    ap.add_argument("--out", default=None, help="Write script to file instead of stdout")
    args = ap.parse_args()

    text = generate_script(args.type, market_data_path=args.data)
    if not text:
        print("⚠️  Insufficient data to generate this script type.", file=sys.stderr)
        sys.exit(1)

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"✅ Script written to {args.out}")
    else:
        print(text)
