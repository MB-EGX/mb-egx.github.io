"""
export_subscribers.py
======================
N21: pulls every Firestore `users/{uid}` doc that has a non-empty `email`
field and writes those emails to a plain text file, one per line.

Every account is auto-subscribed at creation (see login.html's
account-creation writes) — there is no separate Follow/Join confirmation
step anymore (Instagram/Facebook follows can't be verified via their
public APIs, so that honor-system gate was removed in favor of plain,
always-visible social links). This also means any account created before
that change — which may still have `subscription.subscribed_active ==
false` from the old gate — is included here too, with no migration
needed.

WHY A SEPARATE STEP, NOT INSIDE send_email_digest.py DIRECTLY:
    send_email_digest.py deliberately has zero dependency on Firebase / the
    local DuckDB (see its own docstring) so it can run from anywhere with
    nothing but the public market_data.json and SMTP creds. Keeping the
    Firestore query here, as its own small step, means send_email_digest.py
    stays that simple - it just reads whatever file this script wrote.

AUTH: uses the Firebase Admin SDK with a service-account key, NOT the
public Firebase Web SDK config already in login.html/index.html (that
config has no privilege to list every user's data - by design, an
individual user's ID token can only read/write their OWN doc, per the
Firestore security rules such an app should have). Get a service-account
key from:
    Firebase Console → Project Settings → Service Accounts →
    "Generate new private key" → downloads a JSON file.
NEVER commit that file to the repo. In GitHub Actions, base64-encode it
and store as the FIREBASE_SERVICE_ACCOUNT_JSON secret (see the companion
workflow step / this file's __main__ block below for how it's decoded).

USAGE
-----
    python export_subscribers.py
    python export_subscribers.py --out web_public/social/subscriber_emails.txt
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys

import firebase_admin
from firebase_admin import credentials, firestore

DEFAULT_OUT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "web_public", "social", "subscriber_emails.txt"
)


def _init_firebase_admin():
    """Accepts the service-account JSON either as a raw JSON string or
    base64-encoded (base64 is friendlier for pasting into a GitHub Actions
    secret box, which can be finicky about multi-line values / quoting)."""
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        sys.exit(
            "FIREBASE_SERVICE_ACCOUNT_JSON is not set. Generate a service-account "
            "key in Firebase Console -> Project Settings -> Service Accounts, "
            "then store its JSON (raw or base64) as this env var / GH secret."
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        try:
            parsed = json.loads(base64.b64decode(raw).decode("utf-8"))
        except Exception as e:
            sys.exit(f"FIREBASE_SERVICE_ACCOUNT_JSON is neither valid JSON nor valid base64-encoded JSON: {e}")
    cred = credentials.Certificate(parsed)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()


def get_confirmed_subscriber_emails(db) -> list[str]:
    """Every user doc with a non-empty email — every signed-up account is
    a subscriber now, no separate confirmation flag to filter on (see
    module docstring). Kept as its own function/name (rather than being
    inlined into export_subscribers()) since send_email_digest.py and any
    future caller only care about "get me the list", not how it's
    derived."""
    emails = []
    for doc in db.collection("users").stream():
        data = doc.to_dict() or {}
        email = (data.get("email") or "").strip()
        if email:
            emails.append(email)
    return sorted(set(emails))


def export_subscribers(out_path: str) -> int:
    db = _init_firebase_admin()
    emails = get_confirmed_subscriber_emails(db)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(emails))
        if emails:
            f.write("\n")
    print(f"✅ Exported {len(emails)} confirmed subscriber email(s) → {out_path}")
    return len(emails)


def main():
    parser = argparse.ArgumentParser(description="Export confirmed (Follow+Join) subscriber emails from Firestore.")
    parser.add_argument("--out", default=DEFAULT_OUT_PATH, help=f"Output path (default: {DEFAULT_OUT_PATH})")
    args = parser.parse_args()
    export_subscribers(args.out)


if __name__ == "__main__":
    main()
