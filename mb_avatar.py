"""
mb_avatar.py
============
Turns MB's audio into a lip-synced .mp4 of the MB character speaking.

DEFAULT BACKEND — "local" (SadTalker, free, self-hosted):
  - $0 per video, no API key, no monthly cap
  - Runs on the GitHub Actions runner itself (CPU) — slow, but free forever
  - Needs: a single front-facing MB reference image checked into the repo
    (e.g. assets/mb_reference.png), and the SadTalker repo + checkpoints set
    up in the same job (see the "Set up SadTalker" step in
    mb-weekly-video.yml — only downloads once per run, ~2GB)
  - Quality note: SadTalker/Wav2Lip-style models are tuned on real human
    faces. MB's stylized anime look (esp. the glowing white eyes) may sync
    less crisply than a photorealistic avatar would on HeyGen/D-ID — worth
    reviewing the first couple of outputs before trusting it unattended.
  - Speed note: runs with "--preprocess crop" (face-only, not full frame)
    and "--size 256" (lower render resolution) to keep CPU render time
    manageable in CI. "--preprocess full" + default 512px size can take
    well over an hour for a ~30s clip on a GitHub Actions CPU runner —
    crop+256 is meaningfully faster at a modest quality cost.

OPTIONAL BACKENDS (set MB_LIPSYNC_BACKEND=heygen or MB_LIPSYNC_BACKEND=did):
  - HeyGen: ~$29/mo Starter (120 credits). Needs HEYGEN_API_KEY + HEYGEN_AVATAR_ID.
  - D-ID: ~$5.90/mo Lite, or a small free trial credit allotment. Needs
    DID_API_KEY + MB_AVATAR_IMAGE_URL (a *public* URL to MB's reference
    image — different from the local file path used by the "local" backend).
  Both are async: submit → poll → download, and both need the audio hosted
  at a public URL first (mb_generate_video.py handles that only when one
  of these backends is selected).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# HeyGen constants
# ---------------------------------------------------------------------------
HEYGEN_API_BASE = "https://api.heygen.com"
HEYGEN_VIDEO_EP = f"{HEYGEN_API_BASE}/v2/video/generate"
HEYGEN_STATUS_EP = f"{HEYGEN_API_BASE}/v1/video_status.get"
HEYGEN_DOWNLOAD_EP = f"{HEYGEN_API_BASE}/v1/asset"   # signed URL returned in status

POLL_INTERVAL_S  = 15   # seconds between status polls
MAX_POLL_TRIES   = 40   # 40 × 15s = 10 minutes max wait

# ---------------------------------------------------------------------------
# D-ID constants (fallback)
# ---------------------------------------------------------------------------
DID_API_BASE = "https://api.d-id.com"
DID_TALKS_EP = f"{DID_API_BASE}/talks"

OUTPUT_FILENAME = "mb_video.mp4"


# ===========================================================================
# HeyGen implementation
# ===========================================================================

def _heygen_submit(audio_url: str, api_key: str, avatar_id: str) -> str:
    """
    Submit a video generation job to HeyGen.
    audio_url must be a publicly accessible .mp3 URL.
    Returns the video_id for polling.
    """
    headers = {
        "X-Api-Key": api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "video_inputs": [
            {
                "character": {
                    "type": "avatar",
                    "avatar_id": avatar_id,
                    "avatar_style": "normal",
                },
                "voice": {
                    "type": "audio",
                    "audio_url": audio_url,
                },
                "background": {
                    "type": "color",
                    "value": "#0B1018",   # matches MB's dark trading-room aesthetic
                },
            }
        ],
        "dimension": {"width": 1080, "height": 1920},   # 9:16 vertical for Reels
        "aspect_ratio": "9:16",
        "caption": False,                               # we add our own Arabic caption
    }
    resp = requests.post(HEYGEN_VIDEO_EP, headers=headers, json=payload, timeout=30)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"HeyGen submit error {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    video_id = data.get("data", {}).get("video_id") or data.get("video_id")
    if not video_id:
        raise RuntimeError(f"HeyGen: no video_id in response: {data}")
    print(f"✅ HeyGen job submitted. video_id={video_id}")
    return video_id


def _heygen_poll(video_id: str, api_key: str) -> str:
    """
    Poll HeyGen until the video is ready. Returns the download URL.
    """
    headers = {"X-Api-Key": api_key}
    for attempt in range(1, MAX_POLL_TRIES + 1):
        resp = requests.get(
            HEYGEN_STATUS_EP,
            headers=headers,
            params={"video_id": video_id},
            timeout=20,
        )
        if resp.status_code != 200:
            print(f"  ⚠️  Poll attempt {attempt}: HTTP {resp.status_code}", flush=True)
        else:
            data = resp.json().get("data", {})
            status = data.get("status", "")
            print(f"  ⏳ [{attempt}/{MAX_POLL_TRIES}] HeyGen status: {status}", flush=True)
            if status == "completed":
                video_url = data.get("video_url") or data.get("url")
                if not video_url:
                    raise RuntimeError(f"HeyGen: completed but no video_url in: {data}")
                return video_url
            if status in ("failed", "error"):
                raise RuntimeError(f"HeyGen video generation failed: {data}")
        time.sleep(POLL_INTERVAL_S)

    raise TimeoutError(
        f"HeyGen video not ready after {MAX_POLL_TRIES * POLL_INTERVAL_S}s. "
        f"Check https://app.heygen.com for video_id={video_id}"
    )


def _heygen_generate(
    audio_url: str,
    output_path: str,
    api_key: str,
    avatar_id: str,
) -> str:
    video_id = _heygen_submit(audio_url, api_key, avatar_id)
    video_url = _heygen_poll(video_id, api_key)
    return _download_video(video_url, output_path)


# ===========================================================================
# D-ID fallback
# ===========================================================================

def _did_generate(
    audio_url: str,
    output_path: str,
    api_key: str,
    presenter_image_url: str,
) -> str:
    """
    D-ID /talks endpoint — alternative to HeyGen.
    presenter_image_url: publicly accessible URL of MB's front-facing avatar image.
    Set env var MB_AVATAR_IMAGE_URL to your hosted MB PNG.
    """
    headers = {
        "Authorization": f"Basic {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "source_url": presenter_image_url,
        "script": {
            "type": "audio",
            "audio_url": audio_url,
        },
        "config": {
            "fluent": True,
            "pad_audio": 0.0,
            "result_format": "mp4",
        },
    }
    resp = requests.post(DID_TALKS_EP, headers=headers, json=payload, timeout=30)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"D-ID submit error {resp.status_code}: {resp.text[:500]}")

    talk_id = resp.json().get("id")
    if not talk_id:
        raise RuntimeError(f"D-ID: no talk id in response: {resp.json()}")
    print(f"✅ D-ID job submitted. talk_id={talk_id}")

    # Poll
    for attempt in range(1, MAX_POLL_TRIES + 1):
        time.sleep(POLL_INTERVAL_S)
        poll = requests.get(
            f"{DID_TALKS_EP}/{talk_id}",
            headers=headers,
            timeout=20,
        )
        data = poll.json()
        status = data.get("status", "")
        print(f"  ⏳ [{attempt}] D-ID status: {status}", flush=True)
        if status == "done":
            video_url = data.get("result_url")
            if not video_url:
                raise RuntimeError(f"D-ID: done but no result_url: {data}")
            return _download_video(video_url, output_path)
        if status in ("error", "rejected"):
            raise RuntimeError(f"D-ID failed: {data}")

    raise TimeoutError(f"D-ID talk not ready after {MAX_POLL_TRIES * POLL_INTERVAL_S}s")


# ===========================================================================
# Local / self-hosted implementation (SadTalker) — free, default
# ===========================================================================

def _local_generate(
    audio_path: str,
    output_path: str,
    source_image: str | None = None,
    sadtalker_dir: str | None = None,
) -> str:
    """
    Free lip-sync using a self-hosted SadTalker checkout — no API key,
    no per-video cost. `audio_path` is a LOCAL file path (not a public URL;
    unlike the HeyGen/D-ID backends, nothing needs to be uploaded first).

    Expects SadTalker to already be cloned + checkpoints downloaded at
    `sadtalker_dir` (falls back to SADTALKER_DIR env var, default "SadTalker").
    See the "Set up SadTalker" step in mb-weekly-video.yml.

    Runs with "--preprocess crop" (face-only crop, not the full frame) and
    "--size 256" (lower resolution) — both meaningfully cut CPU render time
    compared to "--preprocess full" at default 512px, which can take well
    over an hour for a longer clip on a GitHub Actions CPU runner.
    """
    source_image = (
        source_image
        or os.environ.get("MB_AVATAR_IMAGE_PATH", "assets/mb_reference.png")
    )
    sadtalker_dir = sadtalker_dir or os.environ.get("SADTALKER_DIR", "SadTalker")

    if not Path(source_image).exists():
        raise FileNotFoundError(
            f"MB reference image not found at '{source_image}'. "
            "Check MB_AVATAR_IMAGE_PATH / commit the image into the repo."
        )
    if not Path(sadtalker_dir).exists():
        raise FileNotFoundError(
            f"SadTalker not found at '{sadtalker_dir}'. "
            "Make sure the 'Set up SadTalker' workflow step ran first."
        )

    result_dir = Path(output_path).parent / "sadtalker_out"
    result_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, str(Path(sadtalker_dir) / "inference.py"),
        "--driven_audio", audio_path,
        "--source_image", source_image,
        "--result_dir", str(result_dir),
        "--still",
        "--preprocess", "crop",
        "--size", "256",
    ]
    print(f"🎬 (SadTalker, free/local, crop+256 for speed) Running lip-sync — this can take a while…")
    subprocess.run(cmd, check=True)

    produced = sorted(result_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    if not produced:
        raise RuntimeError(f"SadTalker did not produce an .mp4 in {result_dir}")

    out = Path(output_path)
    produced[-1].replace(out)
    size_mb = out.stat().st_size / 1_048_576
    print(f"✅ Video saved: {out.resolve()}  ({size_mb:.1f} MB)")
    return str(out.resolve())


# ===========================================================================
# Shared helpers
# ===========================================================================

def _download_video(video_url: str, output_path: str) -> str:
    print(f"📥 Downloading video from {video_url[:80]}…")
    resp = requests.get(video_url, timeout=120, stream=True)
    resp.raise_for_status()
    out = Path(output_path)
    with out.open("wb") as fh:
        for chunk in resp.iter_content(chunk_size=65536):
            fh.write(chunk)
    size_mb = out.stat().st_size / 1_048_576
    print(f"✅ Video saved: {out.resolve()}  ({size_mb:.1f} MB)")
    return str(out.resolve())


# ===========================================================================
# Public entry point
# ===========================================================================

def generate_avatar_video(
    audio_source: str,
    output_path: str = OUTPUT_FILENAME,
    backend: str | None = None,
    api_key: str | None = None,
    avatar_id: str | None = None,
    did_presenter_image_url: str | None = None,
    local_source_image: str | None = None,
) -> str:
    """
    Generate MB's lip-synced video.

    Args:
        audio_source : LOCAL file path when backend="local" (the default),
                       or a PUBLIC URL to the .mp3 when backend is "heygen"/"did".
        output_path  : Where to save the resulting .mp4
        backend      : "local" (free, default) | "heygen" | "did".
                       Falls back to MB_LIPSYNC_BACKEND env var, default "local".
        api_key      : HeyGen or D-ID key (falls back to env vars)
        avatar_id    : HeyGen avatar ID (falls back to HEYGEN_AVATAR_ID)
        did_presenter_image_url: D-ID presenter image URL (falls back to MB_AVATAR_IMAGE_URL)
        local_source_image: local MB reference image for the "local" backend
                       (falls back to MB_AVATAR_IMAGE_PATH)

    Returns:
        Absolute path to the resulting .mp4 file.
    """
    backend = (backend or os.environ.get("MB_LIPSYNC_BACKEND", "local")).strip().lower()

    if backend == "did":
        did_key = api_key or os.environ.get("DID_API_KEY", "").strip()
        presenter = did_presenter_image_url or os.environ.get("MB_AVATAR_IMAGE_URL", "").strip()
        if not did_key:
            raise EnvironmentError("DID_API_KEY not set.")
        if not presenter:
            raise EnvironmentError(
                "MB_AVATAR_IMAGE_URL not set — D-ID needs a public URL "
                "to MB's front-facing avatar image."
            )
        return _did_generate(audio_source, output_path, did_key, presenter)

    if backend == "heygen":
        hg_key = api_key or os.environ.get("HEYGEN_API_KEY", "").strip()
        hg_avatar = avatar_id or os.environ.get("HEYGEN_AVATAR_ID", "").strip()
        if not hg_key:
            raise EnvironmentError("HEYGEN_API_KEY not set.")
        if not hg_avatar:
            raise EnvironmentError(
                "HEYGEN_AVATAR_ID not set. "
                "Create the MB avatar at app.heygen.com and copy its ID."
            )
        return _heygen_generate(audio_source, output_path, hg_key, hg_avatar)

    # Default: local / free (SadTalker)
    return _local_generate(audio_source, output_path, source_image=local_source_image)
