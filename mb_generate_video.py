"""
mb_generate_video.py
====================
Master orchestrator — runs the full MB video pipeline in one call:

  1. Read market_data.json  → pick the right script type for this run
  2. mb_video_script.py     → generate Arabic script
  3. mb_tts.py              → free edge-tts (default) → mb_voice.mp3
  4. [only for heygen/did backends] upload mp3 to GitHub Releases for a public URL
  5. mb_avatar.py           → local SadTalker (default, free) or HeyGen/D-ID → mb_video.mp4
  6. mb_video_post.py       → upload mp4 → post IG Reel + FB Reel

Designed to be called from the weekly GitHub Actions workflow:
  python mb_generate_video.py --type weekly

--type weekly : the video is a weekly Session Picks recap — celebrates
                achieved_today picks if any crossed target on the run date,
                otherwise falls back to a Session Picks snapshot so every
                weekly video still ties back to picks performance.
--type auto   : original daily logic (achievement > Friday weekly_wrap > top_mover),
                kept for anyone who still wants a daily run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# -- local modules (same repo root)
from mb_video_script import generate_script, SCRIPT_TYPES
from mb_tts import text_to_speech
from mb_avatar import generate_avatar_video
from mb_video_post import post_mb_video, upload_video_to_github_release

CAIRO = ZoneInfo("Africa/Cairo")
DATA_PATH = Path("web_public/data/market_data.json")


def _load_data() -> dict:
    if not DATA_PATH.exists():
        raise FileNotFoundError(f"market_data.json not found at {DATA_PATH.resolve()}")
    with DATA_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _auto_pick_type(market_data: dict) -> str:
    """
    Decide which script type to generate today.
    Priority: achievement > weekly_wrap (Friday) > top_mover
    """
    achieved = market_data.get("session_picks", {}).get("achieved_today", [])
    if achieved:
        print("🏆 Picks achieved today — using 'achievement' script.")
        return "achievement"

    today_cairo = datetime.now(CAIRO)
    if today_cairo.weekday() == 4:   # Friday
        print("📅 It's Friday — using 'weekly_wrap' script.")
        return "weekly_wrap"

    print("📈 Standard day — using 'top_mover' script.")
    return "top_mover"


def _pick_weekly_type(market_data: dict) -> str:
    """
    Weekly video selection: celebrate every Session Pick achieved across
    the trailing 7 days (market_data['session_picks']['achieved_this_week'],
    populated by the updated session_picks.py); otherwise fall back to a
    Session Picks snapshot so the weekly video always ties back to picks
    performance rather than general market commentary.

    Transparently falls back to 'achieved_today' (Friday-only) if the
    market_data.json in use predates the session_picks.py update that adds
    'achieved_this_week' — see script_weekly_achievement() for that fallback.
    """
    picks = market_data.get("session_picks", {})
    achieved = picks.get("achieved_this_week")
    if achieved is None:
        achieved = picks.get("achieved_today", [])
    if achieved:
        print(f"🏆 {len(achieved)} pick(s) achieved this week — using 'weekly_achievement' script.")
        return "weekly_achievement"
    print("📈 No achievements this week — using 'session_picks' snapshot.")
    return "session_picks"


def run(
    script_type: str = "auto",
    post_ig: bool = True,
    post_fb: bool = True,
    dry_run: bool = False,
    keep_files: bool = False,
):
    print("\n" + "="*60)
    print("  🤖 MB Video Pipeline — starting")
    print("="*60 + "\n")

    market_data = _load_data()

    # ── Step 1: Determine script type ────────────────────────────
    if script_type == "weekly":
        script_type = _pick_weekly_type(market_data)
    elif script_type == "auto":
        script_type = _auto_pick_type(market_data)

    # ── Step 2: Generate Arabic script ───────────────────────────
    print(f"📝 Generating '{script_type}' script…")
    script = generate_script(script_type, market_data=market_data)
    if not script:
        print("⚠️  No script generated — insufficient data. Exiting.")
        sys.exit(0)

    print(f"\n--- Script preview ---\n{script[:300]}{'…' if len(script)>300 else ''}\n")

    if dry_run:
        print("🔍 DRY RUN — stopping before TTS/video generation.")
        print("Script:\n", script)
        return

    # ── Temp directory for intermediate files ────────────────────
    tmpdir = Path(tempfile.mkdtemp(prefix="mb_video_"))
    mp3_path   = tmpdir / "mb_voice.mp3"
    mp4_path   = tmpdir / "mb_video.mp4"
    script_path = tmpdir / "script.txt"
    script_path.write_text(script, encoding="utf-8")

    backend = os.environ.get("MB_LIPSYNC_BACKEND", "local").strip().lower()

    try:
        # ── Step 3: TTS ──────────────────────────────────────────
        print("\n🎙️  Step 3: Text-to-Speech (free edge-tts by default)…")
        text_to_speech(script, output_path=str(mp3_path))

        # ── Step 4: Public audio URL — only needed for HeyGen/D-ID ─
        if backend in ("heygen", "did"):
            print("\n☁️   Step 4: Uploading mp3 to GitHub Releases (needed for HeyGen/D-ID)…")
            audio_source = upload_video_to_github_release(
                str(mp3_path),
                tag="mb-audio-latest",
            )
        else:
            print("\n☁️   Step 4: Skipped — local backend uses the mp3 file directly.")
            audio_source = str(mp3_path)

        # ── Step 5: Avatar lip-sync ───────────────────────────────
        print(f"\n🎬  Step 5: Generating MB avatar video (backend: {backend})…")
        generate_avatar_video(
            audio_source=audio_source,
            output_path=str(mp4_path),
            backend=backend,
        )

        # ── Step 6: Post to Instagram + Facebook ─────────────────
        print("\n📲  Step 6: Posting to Instagram Reels + Facebook Reels…")
        results = post_mb_video(
            video_path=str(mp4_path),
            arabic_script=script,
            post_instagram=post_ig,
            post_facebook=post_fb,
            upload_to_github=True,
        )

        print("\n" + "="*60)
        print("  ✅ MB Video Pipeline — COMPLETE")
        print("="*60)
        for k, v in results.items():
            print(f"  {k}: {v}")

    finally:
        if not keep_files:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            print(f"\n📁 Temp files kept at: {tmpdir}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="MB full video pipeline")
    ap.add_argument(
        "--type",
        choices=list(SCRIPT_TYPES) + ["auto", "weekly"],
        default="weekly",
        help="Script type (default: weekly — Session Picks achievement/snapshot)",
    )
    ap.add_argument("--no-ig",  action="store_true", help="Skip Instagram posting")
    ap.add_argument("--no-fb",  action="store_true", help="Skip Facebook posting")
    ap.add_argument("--dry-run", action="store_true", help="Generate script only, no TTS/video")
    ap.add_argument("--keep-files", action="store_true", help="Keep temp mp3/mp4 files")
    args = ap.parse_args()

    run(
        script_type=args.type,
        post_ig=not args.no_ig,
        post_fb=not args.no_fb,
        dry_run=args.dry_run,
        keep_files=args.keep_files,
    )
