"""
mb_video_post.py
================
Posts MB's generated .mp4 as an Instagram Reel + Facebook Reel.

HOW INSTAGRAM REELS PUBLISHING WORKS (Graph API v21):
  Step 1 — Create a media container:
    POST /{ig-user-id}/media
      media_type=REELS, video_url=<public mp4 url>,
      caption=..., share_to_feed=true
  Step 2 — Wait for container to be ready (status_code = FINISHED)
    GET /{container-id}?fields=status_code
  Step 3 — Publish:
    POST /{ig-user-id}/media_publish
      creation_id={container-id}

HOW FACEBOOK REELS PUBLISHING WORKS (Graph API v21):
  Step 1 — Upload video bytes → get upload handle
    POST /me/video_reels  (start upload session)
  Step 2 — Binary transfer of mp4 bytes
    POST <upload_url> with raw bytes
  Step 3 — Publish
    POST /{page-id}/video_reels  (status=PUBLISHED)

REQUIRED GITHUB SECRETS (all already used by social_poster.py):
  IG_USER_ID            — numeric Instagram user ID
  IG_ACCESS_TOKEN       — long-lived / rotated token
  FB_PAGE_ID            — Facebook page numeric ID
  FB_PAGE_ACCESS_TOKEN  — page token with pages_manage_posts + pages_read_engagement

NEW SECRETS (only for the MB video step):
  MB_VIDEO_PUBLIC_URL   — public URL of the hosted mp4 (see upload_video_to_cdn())
  (optional) PAGES_BASE_URL — already set; reused to determine hosting base

This module is self-contained so it can be imported by social_poster.py
or run directly from the GitHub Actions workflow step.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests

GRAPH_IG_BASE = "https://graph.instagram.com/v21.0"
GRAPH_FB_BASE = "https://graph.facebook.com/v21.0"

POLL_INTERVAL_S = 10
MAX_POLL_TRIES  = 30   # 30 × 10s = 5 minutes max


# ===========================================================================
# Upload mp4 to a public CDN (GitHub Releases asset — free, no extra service)
# ===========================================================================

def upload_video_to_github_release(
    video_path: str,
    gh_pat: str | None = None,
    github_repo: str | None = None,
    tag: str = "mb-video-latest",
) -> str:
    """
    Uploads the mp4 as a GitHub Release asset (public, permanent URL).
    This is free and requires no external CDN.

    The release tagged `mb-video-latest` is recreated on each run so the
    URL stays predictable and doesn't accumulate old assets.

    Args:
        video_path   : local path to mb_video.mp4
        gh_pat       : GitHub PAT with `repo` scope (falls back to GH_PAT env var)
        github_repo  : "owner/repo" (falls back to GITHUB_REPO env var)
        tag          : release tag to (re)use

    Returns:
        Public download URL of the uploaded asset.
    """
    gh_pat      = gh_pat      or os.environ.get("GH_PAT", "").strip()
    github_repo = github_repo or os.environ.get("GITHUB_REPO", "").strip()

    if not gh_pat:
        raise EnvironmentError("GH_PAT not set — needed to upload video to GitHub Releases.")
    if not github_repo:
        raise EnvironmentError("GITHUB_REPO not set (format: owner/repo).")

    api = "https://api.github.com"
    headers = {
        "Authorization": f"token {gh_pat}",
        "Accept": "application/vnd.github+json",
    }

    # ---- delete existing release with same tag (idempotent) ----
    r = requests.get(f"{api}/repos/{github_repo}/releases/tags/{tag}", headers=headers, timeout=15)
    if r.status_code == 200:
        release_id = r.json()["id"]
        requests.delete(f"{api}/repos/{github_repo}/releases/{release_id}", headers=headers, timeout=15)
        # Also delete the tag itself
        requests.delete(f"{api}/repos/{github_repo}/git/refs/tags/{tag}", headers=headers, timeout=15)
        print(f"  🗑️  Deleted old release '{tag}'")

    # ---- create fresh release ----
    create = requests.post(
        f"{api}/repos/{github_repo}/releases",
        headers=headers,
        json={
            "tag_name": tag,
            "name": "MB Video (auto-updated)",
            "body": "Auto-generated MB video. Updated on every workflow run.",
            "draft": False,
            "prerelease": False,
        },
        timeout=20,
    )
    create.raise_for_status()
    upload_url_template = create.json()["upload_url"]  # has {?name,label} suffix
    upload_url = upload_url_template.split("{")[0]      # strip template part

    # ---- upload the mp4 ----
    file_path = Path(video_path)
    filename  = file_path.name
    upload_headers = {
        "Authorization": f"token {gh_pat}",
        "Content-Type": "video/mp4",
        "Accept": "application/vnd.github+json",
    }
    with file_path.open("rb") as fh:
        video_bytes = fh.read()

    up = requests.post(
        upload_url,
        headers=upload_headers,
        params={"name": filename},
        data=video_bytes,
        timeout=120,
    )
    up.raise_for_status()
    browser_url = up.json()["browser_download_url"]
    print(f"✅ Video uploaded to GitHub Releases: {browser_url}")
    return browser_url


# ===========================================================================
# Instagram Reels
# ===========================================================================

def _ig_create_reel_container(
    ig_user_id: str,
    access_token: str,
    video_url: str,
    caption: str,
) -> str:
    """Step 1: Create IG Reels media container. Returns creation_id."""
    resp = requests.post(
        f"{GRAPH_IG_BASE}/{ig_user_id}/media",
        data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true",
            "access_token": access_token,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"IG container create error {resp.status_code}: {resp.text[:400]}")
    creation_id = resp.json().get("id")
    if not creation_id:
        raise RuntimeError(f"IG: no creation_id in response: {resp.json()}")
    print(f"  📦 IG container created: {creation_id}")
    return creation_id


def _ig_wait_for_container(
    ig_user_id: str,
    creation_id: str,
    access_token: str,
) -> None:
    """Step 2: Poll until container status == FINISHED."""
    for attempt in range(1, MAX_POLL_TRIES + 1):
        time.sleep(POLL_INTERVAL_S)
        resp = requests.get(
            f"{GRAPH_IG_BASE}/{creation_id}",
            params={"fields": "status_code", "access_token": access_token},
            timeout=20,
        )
        status = resp.json().get("status_code", "")
        print(f"  ⏳ [{attempt}/{MAX_POLL_TRIES}] IG container status: {status}", flush=True)
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"IG container failed with status: {status}")
    raise TimeoutError("IG container did not become FINISHED in time.")


def _ig_publish_reel(ig_user_id: str, creation_id: str, access_token: str) -> str:
    """Step 3: Publish the reel. Returns the media ID."""
    resp = requests.post(
        f"{GRAPH_IG_BASE}/{ig_user_id}/media_publish",
        data={"creation_id": creation_id, "access_token": access_token},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"IG publish error {resp.status_code}: {resp.text[:400]}")
    media_id = resp.json().get("id", "")
    print(f"✅ Instagram Reel published! media_id={media_id}")
    return media_id


def publish_instagram_reel(
    video_url: str,
    caption: str,
    ig_user_id: str | None = None,
    access_token: str | None = None,
) -> str:
    ig_user_id   = ig_user_id   or os.environ.get("IG_USER_ID", "").strip()
    access_token = access_token or os.environ.get("IG_ACCESS_TOKEN", "").strip()

    if not ig_user_id or not access_token:
        raise EnvironmentError("IG_USER_ID or IG_ACCESS_TOKEN not set.")

    creation_id = _ig_create_reel_container(ig_user_id, access_token, video_url, caption)
    _ig_wait_for_container(ig_user_id, creation_id, access_token)
    return _ig_publish_reel(ig_user_id, creation_id, access_token)


# ===========================================================================
# Facebook Reels
# ===========================================================================

def publish_facebook_reel(
    video_path: str,
    caption: str,
    page_id: str | None = None,
    page_access_token: str | None = None,
) -> str:
    """
    Facebook Reels uses a 3-step resumable upload protocol:
      1. Start upload session  → get video_id + upload_url
      2. Binary PUT of mp4 bytes
      3. POST to publish as Reel
    """
    page_id            = page_id            or os.environ.get("FB_PAGE_ID", "").strip()
    page_access_token  = page_access_token  or os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()

    if not page_id or not page_access_token:
        raise EnvironmentError("FB_PAGE_ID or FB_PAGE_ACCESS_TOKEN not set.")

    video_bytes = Path(video_path).read_bytes()
    file_size   = len(video_bytes)

    # Step 1 — Start upload session
    start_resp = requests.post(
        f"{GRAPH_FB_BASE}/{page_id}/video_reels",
        data={
            "upload_phase": "start",
            "access_token": page_access_token,
        },
        timeout=20,
    )
    if start_resp.status_code != 200:
        raise RuntimeError(f"FB Reels start error {start_resp.status_code}: {start_resp.text[:400]}")
    start_data = start_resp.json()
    video_id   = start_data.get("video_id")
    upload_url = start_data.get("upload_url")
    if not video_id or not upload_url:
        raise RuntimeError(f"FB Reels start: missing video_id or upload_url: {start_data}")
    print(f"  📦 FB Reels upload session started. video_id={video_id}")

    # Step 2 — Upload binary
    transfer_resp = requests.post(
        upload_url,
        headers={
            "Authorization": f"OAuth {page_access_token}",
            "Content-Type": "video/mp4",
            "offset": "0",
            "file_size": str(file_size),
        },
        data=video_bytes,
        timeout=180,
    )
    if transfer_resp.status_code not in (200, 201):
        raise RuntimeError(f"FB Reels upload error {transfer_resp.status_code}: {transfer_resp.text[:400]}")
    print(f"  ✅ FB Reels video bytes uploaded ({file_size // 1024} KB)")

    # Step 3 — Publish
    publish_resp = requests.post(
        f"{GRAPH_FB_BASE}/{page_id}/video_reels",
        data={
            "video_id": video_id,
            "upload_phase": "finish",
            "video_state": "PUBLISHED",
            "description": caption,
            "access_token": page_access_token,
        },
        timeout=30,
    )
    if publish_resp.status_code not in (200, 201):
        raise RuntimeError(f"FB Reels publish error {publish_resp.status_code}: {publish_resp.text[:400]}")
    print(f"✅ Facebook Reel published! video_id={video_id}")
    return video_id


# ===========================================================================
# Caption builder
# ===========================================================================

def build_reel_caption(arabic_script: str, hashtags: bool = True) -> str:
    """
    Build a social-ready caption from MB's script.
    Takes the first 200 chars of the script as the caption preview,
    then appends Arabic hashtags.
    """
    # Trim to a punchy caption (Instagram shows ~125 chars before 'more')
    preview = arabic_script.strip()
    if len(preview) > 200:
        # cut at last space before 200 chars
        cut = preview[:200].rfind(" ")
        preview = preview[: cut if cut > 100 else 200] + "…"

    tags = (
        "\n\n#بورصة_مصر #EGX #تحليل_فني #سوق_الأسهم "
        "#MB_EGX #استثمار #تداول #أسهم_مصر"
    ) if hashtags else ""

    return preview + tags


# ===========================================================================
# Orchestrator
# ===========================================================================

def post_mb_video(
    video_path: str,
    arabic_script: str,
    post_instagram: bool = True,
    post_facebook:  bool = True,
    upload_to_github: bool = True,
    public_video_url: str | None = None,
) -> dict:
    """
    Full orchestration: upload mp4 → post to IG Reels → post to FB Reels.

    Args:
        video_path        : local path to mb_video.mp4
        arabic_script     : the MB Arabic script (used to build caption)
        post_instagram    : whether to post to Instagram
        post_facebook     : whether to post to Facebook
        upload_to_github  : upload mp4 to GitHub Releases to get a public URL
        public_video_url  : if already hosted publicly, skip GitHub upload

    Returns:
        dict with keys 'ig_media_id', 'fb_video_id', 'video_url'
    """
    caption = build_reel_caption(arabic_script)
    results: dict = {}

    # ---- Get a public URL for the video ----
    if public_video_url:
        video_url = public_video_url
    elif upload_to_github:
        video_url = upload_video_to_github_release(video_path)
    else:
        raise ValueError(
            "Either provide public_video_url or set upload_to_github=True."
        )
    results["video_url"] = video_url

    # ---- Instagram Reels ----
    if post_instagram:
        try:
            ig_id = publish_instagram_reel(video_url, caption)
            results["ig_media_id"] = ig_id
        except Exception as exc:
            print(f"⚠️  Instagram Reel failed: {exc}", file=sys.stderr)
            results["ig_error"] = str(exc)

    # ---- Facebook Reels ----
    if post_facebook:
        try:
            fb_id = publish_facebook_reel(video_path, caption)
            results["fb_video_id"] = fb_id
        except Exception as exc:
            print(f"⚠️  Facebook Reel failed: {exc}", file=sys.stderr)
            results["fb_error"] = str(exc)

    return results


# ===========================================================================
# CLI
# ===========================================================================

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Post MB video to Instagram & Facebook Reels")
    ap.add_argument("--video",  required=True, help="Path to mb_video.mp4")
    ap.add_argument("--script", required=True, help="Path to Arabic script .txt")
    ap.add_argument("--no-ig",  action="store_true", help="Skip Instagram")
    ap.add_argument("--no-fb",  action="store_true", help="Skip Facebook")
    ap.add_argument("--url",    default=None, help="Already-public video URL (skip GitHub upload)")
    args = ap.parse_args()

    script_text = Path(args.script).read_text(encoding="utf-8")
    result = post_mb_video(
        video_path       = args.video,
        arabic_script    = script_text,
        post_instagram   = not args.no_ig,
        post_facebook    = not args.no_fb,
        upload_to_github = args.url is None,
        public_video_url = args.url,
    )
    print("\n📊 Results:", result)
