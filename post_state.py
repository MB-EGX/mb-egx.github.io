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
        "market":        {"ig": true,  "fb": false, "tg": false},
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
from freshness import is_fresh, today_cairo
from config import ACHIEVEMENT_REBROADCAST_MAX_AGE_DAYS, TRACK_RECORD_FREQUENCY, TRACK_RECORD_WEEKLY_WEEKDAY

CAIRO = ZoneInfo("Africa/Cairo")
STATE_PATH = "web_public/social/post_state.json"
DATA_PATH = "web_public/data/market_data.json"
PLATFORMS = ("ig", "fb", "tg")
JITTER_MINUTES = 5

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

# "weekly" — Friday recap (top gainers/losers, best/worst sector,
# EGX30/EGX70 EWI trend) of the week that closed Thursday. Also kept out
# of DUE_TIMES because, unlike market/sectors/tickers, its due-check
# needs an extra day-of-week gate (see cmd_due()) instead of firing every
# day. Egypt's weekend is Friday-Saturday (EGX trades Sun-Thu), so Friday
# — the day right after the week's last session — is the natural day to
# post it; no urgency about the exact hour, so this is set comfortably
# after market-related posts would have gone out on a normal trading day.
WEEKLY_DUE_TIME = (13, 0)
WEEKLY_DUE_WEEKDAY = 4  # Python weekday(): Monday=0 ... Friday=4, Sunday=6

# How many days old market_data.json's last_data_date is allowed to be
# on Friday and still count as "this week's" data (Thursday's close is
# normally 1 day old by Friday; a couple of extra days of slack covers a
# missed Thursday publish without ever posting a truly stale, weeks-old
# recap).
WEEKLY_MAX_DATA_AGE_DAYS = 6

# "achievement" has no fixed clock time (see module docstring) - it's
# tracked in the same per-day/per-platform `posted` shape as the other
# types, just checked differently in cmd_due(). "track_record" and
# "weekly" do have fixed times (TRACK_RECORD_DUE_TIME / WEEKLY_DUE_TIME
# above) but aren't in DUE_TIMES because their due-checks need extra
# conditions beyond the plain clock-time gate.
ALL_TYPES = (*DUE_TIMES.keys(), "achievement", "track_record", "weekly")


def _today_str():
    return today_cairo()


def _empty_posted():
    return {t: {p: False for p in PLATFORMS} for t in ALL_TYPES}


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
        state["posted"].setdefault(t, {p: False for p in PLATFORMS})
    return state


def _achieved_today(today_str):
    """Session Picks marked achieved for today's session (see
    session_picks.py / export_json.py's "session_picks.achieved_today").
    Only meaningful when the data itself is fresh for today — caller
    already gates on that via _data_is_fresh_today().

    NEW: only achievements whose achieved_date is within
    config.ACHIEVEMENT_REBROADCAST_MAX_AGE_DAYS of today count as due —
    anything older is *history* and must never trigger the achievement
    post. This kills the recurring "historical achievement re-posted
    every day" bug (a stale committed market_data.json re-listed the
    same old day's achievements forever)."""
    try:
        with open(DATA_PATH) as f:
            data = json.load(f)
        achievements = data.get("session_picks", {}).get("achieved_today", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    try:
        today = datetime.strptime(today_str, "%Y-%m-%d").date()
    except ValueError:
        return []
    max_age = max(0, int(ACHIEVEMENT_REBROADCAST_MAX_AGE_DAYS))
    out = []
    for a in achievements:
        try:
            ad = str(a.get("achieved_date") or "")[:10]
            if ad:
                age = (today - datetime.strptime(ad, "%Y-%m-%d").date()).days
                if age > max_age:
                    continue
        except ValueError:
            pass
        out.append(a)
    return out


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


def _data_age_days(last_data_date, today_str):
    """How many calendar days old is last_data_date compared to today
    (Cairo)? Returns None if last_data_date is missing/unparseable —
    callers treat that as "not recent enough" rather than crashing."""
    if not last_data_date:
        return None
    try:
        last = datetime.strptime(last_data_date, "%Y-%m-%d").date()
        today = datetime.strptime(today_str, "%Y-%m-%d").date()
        return (today - last).days
    except ValueError:
        return None


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
    # Single source of truth now lives in freshness.py (same contract:
    # data is fresh only when its last_data_date == the target session
    # date). Keeps the return shape (bool, last_data_date) unchanged.
    return is_fresh(DATA_PATH, target=today_str)[:2]


def cmd_due(args):
    now = datetime.now(CAIRO)
    today_str = now.strftime("%Y-%m-%d")

    state = _current_state()
    fresh, last_data_date = _data_is_fresh_today(today_str)

    due_types = []
    if fresh:
        for post_type, (hh, mm) in DUE_TIMES.items():
            due_time = now.replace(hour=hh, minute=max(0, mm - JITTER_MINUTES), second=0, microsecond=0)
            status = state["posted"][post_type]
            still_needed = any(not status[p] for p in PLATFORMS)
            if now >= due_time and still_needed:
                due_types.append(post_type)

        # achievement — due immediately (no clock gate) whenever today's
        # fresh data shows at least one newly-achieved Session Pick and it
        # hasn't gone out yet. See module docstring.
        status = state["posted"]["achievement"]
        still_needed = any(not status[p] for p in PLATFORMS)
        achievements = _achieved_today(today_str)
        if achievements and still_needed:
            due_types.append("achievement")

        # track_record — fixed due time like market/sectors/tickers, PLUS
        # gated on "if present": only due once achieved_history actually
        # has at least one entry. See module docstring.
        hh, mm = TRACK_RECORD_DUE_TIME
        due_time = now.replace(hour=hh, minute=max(0, mm - JITTER_MINUTES), second=0, microsecond=0)
        status = state["posted"]["track_record"]
        still_needed = any(not status[p] for p in PLATFORMS)
        history_present = bool(_achieved_history())
        # WEEKLY (default): track_record is a Friday recap - right after
        # Thursday's last EGX session - never a daily post. Set
        # config.TRACK_RECORD_FREQUENCY="DAILY" to restore the old
        # every-day cadence.
        weekly_gate = (
            TRACK_RECORD_FREQUENCY != "DAILY"
            and now.weekday() != TRACK_RECORD_WEEKLY_WEEKDAY
        )
        if now >= due_time and still_needed and history_present and not weekly_gate:
            due_types.append("track_record")

    # weekly — deliberately OUTSIDE the `if fresh:` block above: "fresh"
    # means last_data_date == today, which never happens on a Friday
    # (EGX doesn't trade Fri/Sat, so Friday's market_data.json is still
    # dated Thursday). This has its own staleness check instead — the
    # data just needs to be recent enough to be "this week's" (see
    # WEEKLY_MAX_DATA_AGE_DAYS), not dated exactly today.
    if now.weekday() == WEEKLY_DUE_WEEKDAY:
        hh, mm = WEEKLY_DUE_TIME
        due_time = now.replace(hour=hh, minute=max(0, mm - JITTER_MINUTES), second=0, microsecond=0)
        status = state["posted"]["weekly"]
        still_needed = any(not status[p] for p in PLATFORMS)
        data_age_days = _data_age_days(last_data_date, today_str)
        data_recent_enough = data_age_days is not None and data_age_days <= WEEKLY_MAX_DATA_AGE_DAYS
        if now >= due_time and still_needed and data_recent_enough:
            due_types.append("weekly")
        elif now >= due_time and still_needed and not data_recent_enough:
            print(
                f"HOLDING weekly: market_data.json last_data_date {last_data_date or '(missing)'} "
                f"is more than {WEEKLY_MAX_DATA_AGE_DAYS} day(s) old — looks like this week's "
                f"publish.py run never happened. Run publish.py, then re-run this workflow.",
                file=sys.stderr,
            )

    print(f"Cairo time now: {now.strftime('%Y-%m-%d %H:%M')}", file=sys.stderr)
    print(f"market_data.json last_data_date: {last_data_date or '(missing/unreadable)'}", file=sys.stderr)
    print(f"Today (Cairo date): {today_str}", file=sys.stderr)
    print(f"Status today: {json.dumps(state['posted'])}", file=sys.stderr)
    # W13: make the HOLD visible to the operator. Before this line, a stale-data
    # run printed "Due now: (none)" which is technically correct but doesn't
    # explain WHY nothing posts — easy to misread as "the schedule hasn't fired
    # yet" rather than "I'm holding because your data is from yesterday's
    # session, not today's." Print a single explicit line on the stale branch so
    # the operator can see the gate tripping in CI logs.
    if not fresh:
        print(
            f"HOLDING: market_data.json is for session {last_data_date or '(missing)'}, "
            f"NOT today's {today_str} — NOTHING will post until you run publish.py "
            f"with today's CSV. Run publish.py, then re-run this workflow.",
            file=sys.stderr,
        )
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
    if state["posted"][args.type][args.platform]:
        return
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
