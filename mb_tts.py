"""
mb_tts.py
=========
Converts MB's Arabic script to audio.

DEFAULT ENGINE — Microsoft Edge TTS (via the free `edge-tts` package):
  - $0 cost, no API key, no monthly character cap
  - Good Egyptian Arabic male voice: ar-EG-ShakirNeural
  - Only requirement: `pip install edge-tts`
  - This is what runs by default — no setup needed beyond the pip install.

OPTIONAL ENGINE — ElevenLabs (set MB_TTS_ENGINE=elevenlabs):
  - More expressive/controllable prosody, but free tier caps at 10k chars/month
  - Kept as a drop-in upgrade path if budget opens up later; not used by default.

OUTPUT: mb_voice.mp3
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

OUTPUT_FILENAME     = "mb_voice.mp3"
DEFAULT_EDGE_VOICE  = "ar-EG-ShakirNeural"   # Egyptian Arabic, male — matches MB's voice


# ---------------------------------------------------------------------------
# Edge TTS (free, default)
# ---------------------------------------------------------------------------

async def _edge_tts(script: str, output_path: str, voice: str) -> None:
    import edge_tts  # pip install edge-tts

    communicate = edge_tts.Communicate(script, voice, rate="+0%", pitch="+0Hz")
    await communicate.save(output_path)


def _edge_text_to_speech(script: str, output_path: str, voice: str | None = None) -> str:
    voice = voice or os.environ.get("MB_EDGE_VOICE", DEFAULT_EDGE_VOICE)
    print(f"🎙️  (edge-tts, free) Sending {len(script)} chars — voice: {voice}…")
    asyncio.run(_edge_tts(script, output_path, voice))

    out = Path(output_path)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(
            "edge-tts produced an empty file — check the voice name and network access."
        )
    size_kb = out.stat().st_size // 1024
    print(f"✅ TTS audio saved: {out.resolve()}  ({size_kb} KB)")
    return str(out.resolve())


# ---------------------------------------------------------------------------
# ElevenLabs (optional — only used if MB_TTS_ENGINE=elevenlabs)
# ---------------------------------------------------------------------------

def _elevenlabs_text_to_speech(
    script: str,
    output_path: str,
    api_key: str | None = None,
    voice_id: str | None = None,
    stability: float = 0.45,
    similarity_boost: float = 0.80,
    style: float = 0.35,
    use_speaker_boost: bool = True,
) -> str:
    import requests

    api_key  = api_key  or os.environ.get("ELEVENLABS_API_KEY", "").strip()
    voice_id = voice_id or os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
    if not api_key or not voice_id:
        raise EnvironmentError(
            "ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID not set — "
            "required when MB_TTS_ENGINE=elevenlabs."
        )

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": api_key, "Content-Type": "application/json", "Accept": "audio/mpeg"}
    payload = {
        "text": script,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity_boost,
            "style": style,
            "use_speaker_boost": use_speaker_boost,
        },
    }
    print(f"🎙️  (ElevenLabs) Sending {len(script)} chars to voice {voice_id}…")
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"ElevenLabs API error {resp.status_code}: {resp.text[:500]}")

    out = Path(output_path)
    out.write_bytes(resp.content)
    size_kb = out.stat().st_size // 1024
    print(f"✅ TTS audio saved: {out.resolve()}  ({size_kb} KB)")
    return str(out.resolve())


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def text_to_speech(
    script: str,
    output_path: str = OUTPUT_FILENAME,
    engine: str | None = None,
    voice: str | None = None,
    **kwargs,
) -> str:
    """
    Convert `script` (Arabic text) to speech.

    engine: "edge" (default, free) or "elevenlabs" (paid past free tier).
    Falls back to the MB_TTS_ENGINE env var, defaulting to "edge".
    """
    engine = (engine or os.environ.get("MB_TTS_ENGINE", "edge")).strip().lower()

    if engine == "elevenlabs":
        return _elevenlabs_text_to_speech(script, output_path, **kwargs)

    return _edge_text_to_speech(script, output_path, voice=voice)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="MB Arabic TTS")
    ap.add_argument("--script", required=True, help="Path to .txt file with Arabic script")
    ap.add_argument("--out", default=OUTPUT_FILENAME, help="Output mp3 path")
    ap.add_argument("--engine", default=None, choices=["edge", "elevenlabs"], help="TTS engine")
    ap.add_argument("--voice", default=None, help="Edge voice name (default ar-EG-ShakirNeural)")
    args = ap.parse_args()

    text = Path(args.script).read_text(encoding="utf-8").strip()
    if not text:
        print("❌ Script file is empty.", file=sys.stderr)
        sys.exit(1)

    text_to_speech(text, output_path=args.out, engine=args.engine, voice=args.voice)
