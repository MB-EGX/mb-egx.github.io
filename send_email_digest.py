"""
send_email_digest.py
=====================
N20: daily email digest — "3-bullet summary" of today's Session Picks
(short/medium/long active picks + anything achieved today), sent to
config.EMAIL_DIGEST_RECIPIENTS via plain SMTP.

WHY THIS IS FREE / NO NEW PAID SERVICE:
    Uses smtplib against whatever SMTP server you already have access to
    (a free Gmail App Password, your own domain's mailbox, etc.) — no
    third-party email API/SDK, no per-send cost beyond what your SMTP
    provider already lets you send for free. Nothing here requires
    signing up for anything new.

DATA SOURCE: reads the already-published, privacy-scrubbed
web_public/data/market_data.json (same trust boundary as
social_poster.py — no local DuckDB / desktop-app dependency, so this can
run unattended from CI or your own machine's cron independent of the app
being open). Its "session_picks" field is exactly the shape
session_picks.build_digest_payload() expects (short/medium/long/
achieved_today/session_date — see session_picks.refresh_session_picks
and export_json.py's export_market_matrix, which write that same shape
into the public JSON).

CONFIGURATION (env vars):
    MBEGX_EMAIL_DIGEST_TO   comma-separated recipient list (also read by
                             config.EMAIL_DIGEST_RECIPIENTS)
    SMTP_HOST               e.g. smtp.gmail.com
    SMTP_PORT               default 587 (STARTTLS)
    SMTP_USER               mailbox username (also used as the From: address
                             unless SMTP_FROM is set)
    SMTP_PASS               mailbox password / app password
    SMTP_FROM               optional override for the From: header

If MBEGX_EMAIL_DIGEST_TO or the SMTP_* vars aren't set, this exits
early with a message instead of failing loudly — same "no-op unless
configured" posture as alerts.py's Telegram channel.

USAGE
-----
    python send_email_digest.py
    python send_email_digest.py --market-data web_public/data/market_data.json
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
from email.mime.text import MIMEText

from config import EMAIL_DIGEST_RECIPIENTS, get_logger
from session_picks import build_digest_payload

logger = get_logger("send_email_digest")

# Same three links appended to social posts (see social_poster.py's
# _PROMO_FOOTER), plus the Telegram invite — kept here as plain text
# since Facebook/Telegram render URLs verbatim and this is a plain-text
# email (no HTML, so no clickable-vs-not concern like Instagram's).
_SOCIAL_LINKS_FOOTER = (
    "\n\n————\n"
    "Follow us:\n"
    "Instagram: https://www.instagram.com/mb_egx/\n"
    "Facebook Page: https://www.facebook.com/profile.php?id=61593012092507\n"
    "Telegram Channel: https://t.me/MBEGX\n"
    "Dashboard: https://mb-egx.github.io/\n"
)


def _recipients(subscribers_file: str | None) -> list[str]:
    """Combines two sources: EMAIL_DIGEST_RECIPIENTS (a small static
    allowlist - useful for a personal copy or a team inbox) and the
    per-user emails export_subscribers.py just pulled from Firestore for
    every account that completed the Follow/Join gate (see that script's
    docstring). Either source alone is fine — a fresh app with no
    subscribers yet still works off the static list, and the static list
    is entirely optional once real subscribers exist.
    """
    recipients = {r.strip() for r in EMAIL_DIGEST_RECIPIENTS.split(",") if r.strip()}
    if subscribers_file and os.path.exists(subscribers_file):
        with open(subscribers_file, "r", encoding="utf-8") as f:
            recipients.update(line.strip() for line in f if line.strip())
    return sorted(recipients)


def load_market_data(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def send_digest_email(payload: dict, recipients: list[str]) -> list[str]:
    host = os.environ.get("SMTP_HOST", "").strip()
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "")
    from_addr = os.environ.get("SMTP_FROM", "").strip() or user

    if not host or not user or not password:
        sys.exit(
            "SMTP_HOST / SMTP_USER / SMTP_PASS are required (env vars). "
            "See this file's module docstring for the full list."
        )

    body_text = payload["text"] + _SOCIAL_LINKS_FOOTER
    msg = MIMEText(body_text, "plain", "utf-8")
    msg["Subject"] = payload["subject"]
    msg["From"] = from_addr
    # BCC, not To: putting every subscriber's address in the To: header
    # (a) leaks every recipient's email to every other recipient, and
    # (b) is a strong spam-filter signal — a message with dozens of
    # addresses in To: looks like a bulk blast, not a real email, and
    # gets silently dropped/spam-foldered by most providers for anyone
    # who isn't the sender themself. The actual delivery list still goes
    # to every recipient — it's passed to smtplib's sendmail() as the
    # envelope recipient list below — it just never appears in the
    # message headers, which is what BCC means.
    msg["To"] = from_addr

    with smtplib.SMTP(host, port, timeout=20) as server:
        server.starttls()
        server.login(user, password)
        # sendmail() does NOT raise just because some recipients were
        # rejected — it only raises if EVERY recipient was refused. Any
        # partial failures come back as a dict {email: (code, message)}
        # instead, which the old version of this function threw away
        # entirely, making a mailbox getting silently rejected (e.g. a
        # fake/mistyped/unverified address — Firebase's default email/
        # password sign-up never confirms the address is real) look
        # identical to a successful send. Returning this to the caller
        # so it can be logged is what actually surfaces that difference.
        refused = server.sendmail(from_addr, recipients, msg.as_string())
    return list(refused.keys())


def main():
    parser = argparse.ArgumentParser(description="Send the daily Session Picks email digest (N20).")
    parser.add_argument(
        "--market-data",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_public", "data", "market_data.json"),
        help="Path to the published market_data.json (default: web_public/data/market_data.json)",
    )
    parser.add_argument(
        "--subscribers-file",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_public", "social", "subscriber_emails.txt"),
        help="Path written by export_subscribers.py (default: web_public/social/subscriber_emails.txt). "
             "Missing/absent file is fine — just means no Firestore subscribers yet.",
    )
    args = parser.parse_args()

    recipients = _recipients(args.subscribers_file)
    if not recipients:
        print("ℹ️  No recipients — MBEGX_EMAIL_DIGEST_TO is unset and no confirmed subscribers found. Skipping.")
        return

    market_data = load_market_data(args.market_data)
    state = market_data.get("session_picks", {})
    if not state:
        print("⚠️  No session_picks data in market_data.json yet — nothing to send.")
        return

    payload = build_digest_payload(state)
    refused = send_digest_email(payload, recipients)
    delivered = len(recipients) - len(refused)
    print(f"✅ Digest accepted for {delivered}/{len(recipients)} recipient(s): {payload['subject']}")
    if refused:
        print(f"⚠️  {len(refused)} recipient(s) were REJECTED by the mail server (likely fake/mistyped/")
        print("    unverified addresses — Firebase's default sign-up never confirms an email is real):")
        for addr in refused:
            print(f"    - {_mask_email(addr)}")
        print("    These accounts should be excluded from future sends, or prompted to re-verify their email.")
    print("    Note: this only catches recipients rejected immediately (e.g. 'mailbox does not exist').")
    print("    A mailbox that exists but silently spam-filters the message won't show up here — check")
    print(f"    {os.environ.get('SMTP_USER', '').strip() or '(SMTP_USER)'}'s own inbox for any")
    print("    'Mail Delivery Subsystem' bounce-back messages too.")


def _mask_email(addr: str) -> str:
    """user@domain.com -> us***@domain.com — enough for the account's
    owner to recognize which address failed without printing full
    addresses to a CI log that may be publicly readable (GitHub Actions
    logs are public on public repos)."""
    local, _, domain = addr.partition("@")
    if not domain:
        return addr
    visible = local[:2]
    return f"{visible}{'*' * max(len(local) - len(visible), 3)}@{domain}"


if __name__ == "__main__":
    main()
