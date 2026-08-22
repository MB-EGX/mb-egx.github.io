"""
offline_pin.py
==============
Creator-only second factor for MB-EGX offline mode: a locally-stored PIN.

WHY: the "Continue Offline" button is already gated by an env var + secret
key hash, but that secret lives in the private launcher file. This adds a
second layer that the creator types at the keyboard, so a copied launcher
alone is no longer enough to open the dashboard offline.

STORAGE: only a salted PBKDF2-HMAC-SHA256 hash is written to disk - never
the PIN. Comparison uses hmac.compare_digest (constant-time).

LOCKOUT: after MAX_ATTEMPTS wrong PINs the PIN is locked for
LOCKOUT_SECONDS. The lockout is persisted, so restarting the app does not
reset it.

FORGOT YOUR PIN? Delete the PIN file (it is stored in your HOME dir, NOT
in the repo, so publish.py's git push can never commit the hash):
    Windows:     del "%USERPROFILE%\\.mbegx_offline_pin.json"
    macOS/Linux: rm ~/.mbegx_offline_pin.json
The next offline launch will then ask you to set a new PIN.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Stored OUTSIDE the repo (home dir) so git push can never commit the hash.
# chmod 600 on POSIX; a no-op on Windows.
PIN_FILE = Path.home() / ".mbegx_offline_pin.json"

ITERATIONS = 600_000        # PBKDF2-HMAC-SHA256 work factor
MAX_ATTEMPTS = 5            # wrong PINs before lockout
LOCKOUT_SECONDS = 300       # 5 minutes


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_record() -> dict | None:
    try:
        with open(PIN_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, TypeError):
        return None


def save_record(record: dict) -> None:
    PIN_FILE.write_text(json.dumps(record, indent=2), encoding="utf-8")
    try:
        os.chmod(PIN_FILE, 0o600)
    except OSError:
        pass  # Windows has no chmod; the file is still user-scoped


def _hash_pin(pin: str, salt_hex: str, iterations: int) -> str:
    salt = bytes.fromhex(salt_hex)
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations).hex()


def pin_is_set() -> bool:
    rec = load_record()
    return bool(rec and isinstance(rec, dict) and rec.get("hash"))


def set_pin(pin: str) -> None:
    salt = secrets.token_bytes(16).hex()
    save_record({
        "salt": salt,
        "iterations": ITERATIONS,
        "hash": _hash_pin(pin, salt, ITERATIONS),
        "created": _now().isoformat(),
        "failed_attempts": 0,
        "locked_until": None,
    })


def lockout_remaining() -> float:
    """Seconds left in the lockout (0.0 = not locked)."""
    rec = load_record()
    if not rec or not rec.get("locked_until"):
        return 0.0
    try:
        until = datetime.fromisoformat(rec["locked_until"])
    except (ValueError, TypeError):
        return 0.0
    return max(0.0, (until - _now()).total_seconds())


def verify_pin(pin: str) -> bool:
    if lockout_remaining() > 0:
        return False
    rec = load_record()
    if not rec or not isinstance(rec, dict):
        return False
    try:
        expected = rec["hash"]
        actual = _hash_pin(pin, rec["salt"], int(rec["iterations"]))
    except (KeyError, TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def register_failed_attempt() -> int:
    """Record one wrong PIN. Returns attempts remaining before lockout (0 = locked)."""
    rec = load_record() or {}
    fails = int(rec.get("failed_attempts", 0) or 0) + 1
    rec["failed_attempts"] = fails
    if fails >= MAX_ATTEMPTS:
        rec["locked_until"] = (_now() + timedelta(seconds=LOCKOUT_SECONDS)).isoformat()
    save_record(rec)
    return max(0, MAX_ATTEMPTS - fails)


def reset_attempts() -> None:
    rec = load_record()
    if not rec:
        return
    rec["failed_attempts"] = 0
    rec["locked_until"] = None
    save_record(rec)
