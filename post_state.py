"""
post_state.py — decides which of today's 3 scheduled posts (market,
sectors, tickers) are due-and-not-yet-posted RIGHT NOW, and remembers
which ones have already gone out today.

Why this exists: if you feed the CSVs late, the fixed-time crons in
the workflow may have already fired and found no fresh data (and
skipped). Without something tracking "what's still owed today," a
late feed would mean that day's post for that slot is just gone.
This script is what lets the workflow catch up instead — whenever it
next runs (either via the push-trigger when you publish, or the
15-minute safety-net cron), it checks what's due and not yet posted,
and posts all of it then, regardless of the original schedule time.

State lives in web_public/social/post_state.json and is committed by
the workflow the same way it already commits rendered images.

Usage:
    python post_state.py due            # prints due types to GITHUB_OUTPUT
    python post_state.py mark-posted --type market
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

# Cairo LOCAL due times. Using the system tz database (via zoneinfo/TZ)
# means DST transitions are handled automatically here — unlike the raw
# UTC cron expressions in the workflow file, which still need manual
# edits twice a year since GitHub Actions cron has no timezone concept.
# These are the same times daily-instagram-post.yml originally targeted:
# market/sectors shortly after EGX's 14:30 close, tickers in the evening.
DUE_TIMES = {
    "market":  (16, 0),
    "sectors": (16, 20),
    "tickers": (19, 0),
}


def _load_state():
    if not os.path.exists(STATE_PATH):
        return {"date": None, "posted": []}
    with open(STATE_PATH) as f:
        return json.load(f)


def _save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


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

    state = _load_state()
    if state.get("date") != today_str:
        # New day — nothing posted yet today regardless of yesterday's state.
        state = {"date": today_str, "posted": []}

    fresh, last_commit_date = _data_is_fresh_today(today_str)

    due_types = []
    if fresh:
        for post_type, (hh, mm) in DUE_TIMES.items():
            due_time = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now >= due_time and post_type not in state["posted"]:
                due_types.append(post_type)

    print(f"Cairo time now: {now.strftime('%Y-%m-%d %H:%M')}", file=sys.stderr)
    print(f"Data last updated (Cairo date): {last_commit_date or '(no commits found)'}", file=sys.stderr)
    print(f"Already posted today: {state['posted']}", file=sys.stderr)
    print(f"Due now: {due_types or '(none)'}", file=sys.stderr)

    _save_state(state)  # persists the day-rollover reset, if one happened

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"types={' '.join(due_types)}\n")


def cmd_mark_posted(args):
    today_str = datetime.now(CAIRO).strftime("%Y-%m-%d")
    state = _load_state()
    if state.get("date") != today_str:
        state = {"date": today_str, "posted": []}
    if args.type not in state["posted"]:
        state["posted"].append(args.type)
    _save_state(state)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("due").set_defaults(func=cmd_due)

    mark = sub.add_parser("mark-posted")
    mark.add_argument("--type", required=True, choices=DUE_TIMES.keys())
    mark.set_defaults(func=cmd_mark_posted)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
