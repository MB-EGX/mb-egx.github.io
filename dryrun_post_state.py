"""
dryrun_post_state.py — prove the "hold + drain on late publish" behavior
of post_state.py by injecting four scenarios against the REAL function
(no post_state.py edits).

Run from the repo folder containing post_state.py, freshness.py and
web_public/data/market_data.json. Prints the due list per scenario so
you can see exactly what the daily-instagram-post.yml workflow would
post in each case.
"""
import json, os, sys, importlib
from datetime import datetime
from zoneinfo import ZoneInfo

DATA = "web_public/data/market_data.json"
os.makedirs("web_public/data", exist_ok=True)
os.makedirs("web_public/social", exist_ok=True)

CAIRO = ZoneInfo("Africa/Cairo")
TODAY = "2026-08-18"          # whatever today_cairo() returns inside the patched run

def write_data(last_data_date, achieved_history=None, achieved_today=None):
    payload = {
        "last_data_date": last_data_date,
        "market_matrix": [],
        "session_picks": {
            "short": [], "medium": [], "long": [],
            "achieved_today":   achieved_today   or [],
            "achieved_history": achieved_history or [],
        },
    }
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(payload, f)

def reset_state():
    p = "web_public/social/post_state.json"
    if os.path.exists(p): os.remove(p)

def patch_now(year, month, day, hour, minute):
    """Monkey-patch the CAIRO-aware now() that post_state uses."""
    real = datetime
    class P(real):
        @classmethod
        def now(cls, tz=None):
            return real(2026, 8, day, hour, minute, tzinfo=CAIRO)
    import post_state as ps
    ps.datetime = P
    # also patch freshness.py (it also imports datetime directly)
    import freshness as fr
    fr.datetime = P
    # and post_state's today_cairo is import-bound — re-import fresh
    importlib.reload(ps)

def run_scenario(label, last_data_date, now_hh, now_mm, achieved_history=None, achieved_today=None):
    reset_state()
    write_data(last_data_date, achieved_history=achieved_history, achieved_today=achieved_today)
    patch_now(2026, 8, 18, now_hh, now_mm)
    import post_state as ps
    # clear GITHUB_OUTPUT so cmd_due returns via stderr/stdout, not the file
    os.environ.pop("GITHUB_OUTPUT", None)
    print(f"\n===== {label} =====")
    print(f"  last_data_date in market_data.json = {last_data_date}")
    print(f"  simulated Cairo clock time          = {now_hh:02d}:{now_mm:02d}")
    try:
        ps.cmd_due(None)
    except SystemExit as e:
        print(f"  [post_state exited: {e}]")
    # Re-read what was saved to see the post_state reset
    print(f"  state.json after run: {ps._current_state().get('posted')}")

# --- Scenarios that match real user behavior ---

# A. Morning, before user has fed (still yesterday's data committed):
run_scenario(
    "A. Yesterday's data still committed, Cairo 10:00",
    last_data_date="2026-08-17", now_hh=10, now_mm=0,
)

# B. User feeds at 17:00 Cairo with TODAY's CSVs (post_state said all 4 time gates now due):
run_scenario(
    "B. Today's data just published, Cairo 17:00 (post-feed)",
    last_data_date="2026-08-18", now_hh=17, now_mm=0,
)

# C. LATE feed at 20:00 Cairo (way past 16:00/16:20/19:00, before 19:30? no, after):
run_scenario(
    "C. Late feed at Cairo 20:00 — all 4 time-gated posts still pending",
    last_data_date="2026-08-18", now_hh=20, now_mm=0,
)

# D. Very late feed at 23:30 Cairo (still same day, push trigger should fire):
run_scenario(
    "D. Very late feed at Cairo 23:30 — same day, still drainable",
    last_data_date="2026-08-18", now_hh=23, now_mm=30,
)

# E. Accidentally published YESTERDAY's CSV at 12:00 today:
run_scenario(
    "E. Yesterday's CSV re-pushed at Cairo 12:00 — should HOLD",
    last_data_date="2026-08-17", now_hh=12, now_mm=0,
)

# F. track_record gate - data + history + time past, BUT no achieved history ever
run_scenario(
    "F. Fresh data, NEW account — track_record must NOT fire (no history)",
    last_data_date="2026-08-18", now_hh=20, now_mm=0,
    achieved_history=None, achieved_today=None,
)
run_scenario(
    "F'. Same as F but account has 1 historical achievement -> track_record IS due",
    last_data_date="2026-08-18", now_hh=20, now_mm=0,
    achieved_history=[{"ticker": "COMI", "achieved_date": "2026-08-10"}],
    achieved_today=None,
)
