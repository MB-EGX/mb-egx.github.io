"""
post_state.py — decides which of today's 3 scheduled posts (market,
sectors, tickers) are due-and-not-yet-fully-posted RIGHT NOW, tracking
Instagram and Facebook status SEPARATELY per type.

Why separately: if Instagram succeeds for a type but Facebook then
fails, a retry must only redo Facebook — re-attempting Instagram would
duplicate-post it. Tracking both platforms independently for the same
type prevents that.

State lives in web_public/social/post_state.json and is committed by
the workflow the same way it already commits rendered images.

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
import subprocess
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
    isn't today (Cairo). Does NOT save — callers save if they mutate."""
    state = _load_state()
    if state.get("date") != _today_str():
        state = {"date": _today_str(), "posted": _empty_posted()}
    # Guard against a type missing from an older state file shape.
    for t in DUE_TIMES:
        state["posted"].setdefault(t, {"ig": False, "fb": False})
    return state


def _data_is_fresh_today(today_str):
    # fetch-depth: 0 in the workflow's checkout step is required for this
    # to reliably find the last commit that touched DATA_PATH — a shallow
    # clone would only see the single most recent commit overall, which
    # might not be the one that last changed this specific file.
    result = subprocess.run(
        ["git", "log", "-1", "--format=%cd", "--date=format-local:%F", "--", DATA_PATH],
        capture_output=True, text=True, env={**os.environ, "TZ": "Africa/Cairo"},
    )
    last_commit_date = result.stdout.strip()
    return last_commit_date == today_str, last_commit_date


def cmd_due(args):
    now = datetime.now(CAIRO)
    today_str = now.strftime("%Y-%m-%d")

    state = _current_state()
    fresh, last_commit_date = _data_is_fresh_today(today_str)

    due_types = []
    if fresh:
        for post_type, (hh, mm) in DUE_TIMES.items():
            due_time = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            status = state["posted"][post_type]
            still_needed = not status["ig"] or not status["fb"]
            if now >= due_time and still_needed:
                due_types.append(post_type)

    print(f"Cairo time now: {now.strftime('%Y-%m-%d %H:%M')}", file=sys.stderr)
    print(f"Data last updated (Cairo date): {last_commit_date or '(no commits found)'}", file=sys.stderr)
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
