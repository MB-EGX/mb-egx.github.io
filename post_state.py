"""
post_state.py — decides which of today's posts are due-and-not-yet-fully-
posted RIGHT NOW, tracking Instagram and Facebook status SEPARATELY per
type.

Three kinds of "due":
  * market / sectors / tickers — fixed Cairo LOCAL due times (see
    DUE_TIMES below), same as always.
  * achievement — NOT time-gated. It's due the moment fresh data shows at
    least one Session Pick crossed its horizon's target gain today
    (market_data.json's "session_picks.achieved_today" — see
    session_picks.py / export_json.py; the target itself is per-horizon,
    see config.SESSION_PICKS_EXPECTED_PCT) and it hasn't been posted yet.
    A pick achieving its target is itself the trigger, not a clock —
    "automatic" means it goes out as soon as the data says it happened,
    same posture as the push trigger already gets you for the other 3
    types on a normal day.
  * track_record — fixed Cairo LOCAL due time (see TRACK_RECORD_DUE_TIME)
    like market/sectors/tickers, PLUS an extra data-presence gate: it
    only actually becomes due once today's fresh data has at least one
    entry in "session_picks.achieved_history" (see session_picks.py /
    export_json.py) — i.e. "daily, if present". A brand-new account with
    no achieved picks yet simply never posts an empty track record; once
    the first pick is ever achieved, this starts firing daily like the
    other 3 timed posts.

Why track ig/fb separately: if Instagram succeeds for a type but Facebook
then fails, a retry must only redo Facebook — re-attempting Instagram
would duplicate-post it.

Freshness is judged by the "last_data_date" field INSIDE
market_data.json (the actual trading session the data represents),
never by when the file was committed to git — a same-day git commit
does not guarantee the data itself is for today's session.

State shape:
    {
      "date": "2026-08-09",
      "posted": {
        "market":        {"ig": true,  "fb": false},
        "sectors":       {"ig": false, "fb": false},
        "tickers":       {"ig": false, "fb": false},
        "achievement":   {"ig": false, "fb": false},
        "track_record":  {"ig": false, "fb": false}
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

# track_record's own fixed due time — after "tickers" so the day's other
# 3 posts always go out first. Kept separate from DUE_TIMES (rather than
# just adding a 4th entry there) because, unlike those 3, it ALSO needs
# the data-presence gate below — see cmd_due().
TRACK_RECORD_DUE_TIME = (19, 30)

# "achievement" has no fixed clock time (see module docstring) - it's
# tracked in the same per-day/per-platform `posted` shape as the other
# types, just checked differently in cmd_due(). "track_record" does have
# a fixed time (TRACK_RECORD_DUE_TIME above) but isn't in DUE_TIMES
# because its due-check needs the extra data-presence condition.
ALL_TYPES = (*DUE_TIMES.keys(), "achievement", "track_record")


def _today_str():
    return datetime.now(CAIRO).strftime("%Y-%m-%d")


def _empty_posted():
    return {t: {"ig": False, "fb": False} for t in ALL_TYPES}


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
    for t in ALL_TYPES:
        state["posted"].setdefault(t, {"ig": False, "fb": False})
    return state


def _achieved_today(today_str):
    """Session Picks marked achieved for today's session (see
    session_picks.py / export_json.py's "session_picks.achieved_today").
    Only meaningful when the data itself is fresh for today — caller
    already gates on that via _data_is_fresh_today()."""
    try:
        with open(DATA_PATH) as f:
            data = json.load(f)
        return data.get("session_picks", {}).get("achieved_today", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _achieved_history():
    """Full recent track record of achieved Session Picks (see
    session_picks.py / export_json.py's "session_picks.achieved_history"),
    NOT just today's — this is what gates the track_record post's "daily,
    if present" rule: an empty list here means nothing has ever been
    achieved yet, so track_record stays not-due regardless of the clock."""
    try:
        with open(DATA_PATH) as f:
            data = json.load(f)
        return data.get("session_picks", {}).get("achieved_history", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


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

        # achievement — due immediately (no clock gate) whenever today's
        # fresh data shows at least one newly-achieved Session Pick and it
        # hasn't gone out yet. See module docstring.
        status = state["posted"]["achievement"]
        still_needed = not status["ig"] or not status["fb"]
        achievements = _achieved_today(today_str)
        if achievements and still_needed:
            due_types.append("achievement")

        # track_record — fixed due time like market/sectors/tickers, PLUS
        # gated on "if present": only due once achieved_history actually
        # has at least one entry. See module docstring.
        hh, mm = TRACK_RECORD_DUE_TIME
        due_time = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        status = state["posted"]["track_record"]
        still_needed = not status["ig"] or not status["fb"]
        history_present = bool(_achieved_history())
        if now >= due_time and still_needed and history_present:
            due_types.append("track_record")

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
    needs.add_argument("--type", required=True, choices=ALL_TYPES)
    needs.add_argument("--platform", required=True, choices=PLATFORMS)
    needs.set_defaults(func=cmd_needs)

    mark = sub.add_parser("mark-posted")
    mark.add_argument("--type", required=True, choices=ALL_TYPES)
    mark.add_argument("--platform", required=True, choices=PLATFORMS)
    mark.set_defaults(func=cmd_mark_posted)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
