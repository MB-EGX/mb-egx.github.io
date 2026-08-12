"""
post_state.py — decides which of today's 3 scheduled posts (market,
sectors, tickers) are due-and-not-yet-fully-posted RIGHT NOW, tracking
Instagram and Facebook status SEPARATELY per type.

Why separately: if Instagram succeeds for a type but Facebook then
fails, a retry must only redo Facebook — re-attempting Instagram would
duplicate-post it. Tracking both platforms independently for the same
type prevents that.

Freshness is judged by the "last_data_date" field INSIDE
market_data.json (the actual trading session the data represents),
never by when the file was committed to git — a same-day git commit
does not guarantee the data itself is for today's session.

State shape:
    {
      "date": "2026-08-09",
      "posted": {
        "market":  {"ig": true,  "fb": false},
        "sectors": {"ig": false, "fb": false},
        "tickers": {"ig": false, "fb": false}
      }
    }

Usage:
    python post_state.py due                              # -> types=... in GITHUB_OUTPUT
    python post_state.py needs --type market --platform ig # exit 0 if still needed, 1 if already done
    python post_state.py mark-posted --type market --platform ig
"""
import argparse
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

CAIRO = ZoneInfo("Africa/Cairo")
STATE_PATH = "web_public/social/post_state.json"
DATA_PATH = "web_public/data/market_data.json"
PLATFORMS = ("ig", "fb")

# Cairo LOCAL due times. Using the system tz database (via zoneinfo) means
# DST transitions are handled automatically here — unlike the raw UTC cron
# expressions in the workflow file, which still need manual edits twice a
# year since GitHub Actions cron has no timezone concept.
DUE_TIMES = {
    "market":  (16, 0),
    "sectors": (16, 20),
    "tickers": (19, 0),
}


def _today_str():
    return datetime.now(CAIRO).strftime("%Y-%m-%d")


def _empty_posted():
    return {t: {"ig": False, "fb": False} for t in DUE_TIMES}


def _load_state():
    if not os.path.exists(STATE_PATH):
        return {"date": None, "posted": {}}
    with open(STATE_PATH) as f:
        return json.load(f)


def _save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def _current_state():
    """Loads state, resetting to a fresh empty day if the stored date
    isn't today (Cairo) OR if the file is in the old pre-per-platform
    format (posted as a list of type names rather than a dict of
    {type: {ig, fb}}) — that old shape was committed by an earlier
    version of this script and would otherwise crash here. Does NOT
    save — callers save if they mutate."""
    state = _load_state()
    posted = state.get("posted")
    if state.get("date") != _today_str() or not isinstance(posted, dict):
        state = {"date": _today_str(), "posted": _empty_posted()}
    for t in DUE_TIMES:
        state["posted"].setdefault(t, {"ig": False, "fb": False})
    return state


def _data_is_fresh_today(today_str):
    """Freshness is judged by the DATA's own reported session date
    (market_data.json's "last_data_date" field), NOT by when the file
    happened to get committed to git. Those are different things:
    publish.py can push a fresh git commit today even though the CSVs
    fed into it — and therefore last_data_date inside the JSON — still
    represent yesterday's session (source data not caught up yet, wrong
    file fed by mistake, etc). Checking the field itself is what
    actually answers "is this today's session," which is what matters
    for what gets posted — a same-day git commit is not proof of that.
    """
    try:
        with open(DATA_PATH) as f:
            data = json.load(f)
        last_data_date = data.get("last_data_date")
    except (FileNotFoundError, json.JSONDecodeError):
        last_data_date = None
    return last_data_date == today_str, last_data_date


def cmd_due(args):
    now = datetime.now(CAIRO)
    today_str = now.strftime("%Y-%m-%d")

    state = _current_state()
    fresh, last_data_date = _data_is_fresh_today(today_str)

    due_types = []
    if fresh:
        for post_type, (hh, mm) in DUE_TIMES.items():
            due_time = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            status = state["posted"][post_type]
            still_needed = not status["ig"] or not status["fb"]
            if now >= due_time and still_needed:
                due_types.append(post_type)

    print(f"Cairo time now: {now.strftime('%Y-%m-%d %H:%M')}", file=sys.stderr)
    print(f"market_data.json last_data_date: {last_data_date or '(missing/unreadable)'}", file=sys.stderr)
    print(f"Today (Cairo date): {today_str}", file=sys.stderr)
    print(f"Status today: {json.dumps(state['posted'])}", file=sys.stderr)
    print(f"Due now: {due_types or '(none)'}", file=sys.stderr)

    _save_state(state)  # persists the day-rollover reset, if one happened

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"types={' '.join(due_types)}\n")


def cmd_needs(args):
    state = _current_state()
    already_done = state["posted"][args.type][args.platform]
    sys.exit(1 if already_done else 0)


def cmd_mark_posted(args):
    state = _current_state()
    state["posted"][args.type][args.platform] = True
    _save_state(state)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("due").set_defaults(func=cmd_due)

    needs = sub.add_parser("needs")
    needs.add_argument("--type", required=True, choices=DUE_TIMES.keys())
    needs.add_argument("--platform", required=True, choices=PLATFORMS)
    needs.set_defaults(func=cmd_needs)

    mark = sub.add_parser("mark-posted")
    mark.add_argument("--type", required=True, choices=DUE_TIMES.keys())
    mark.add_argument("--platform", required=True, choices=PLATFORMS)
    mark.set_defaults(func=cmd_mark_posted)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
