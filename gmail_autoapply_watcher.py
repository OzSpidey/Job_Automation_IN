"""
Gmail Auto-Apply Watcher — India / Google
=========================================
Polls the dedicated auto-apply inbox (oswin.autoapply@gmail.com) over IMAP,
finds new Google-India-Scraper alert emails, extracts the Google Careers apply
links, and appends any genuinely-new openings to json/autoapply_queue.json.

The Google apply URL carries everything we need in its query string:

    https://www.google.com/about/careers/applications/.../signin?jobId=<ID>&loc=IN&title=<encoded>

so we pull job_id + title straight from the URL — no fragile HTML-row parsing.

Dedup is two-layered:
  - json/autoapply_queue.json    roles waiting to be applied to
  - json/autoapply_applied.json  roles already applied to (never re-queue)

Processed emails are marked \\Seen so we don't rescan them, but even a rescan is
harmless because job_id dedup catches it.

We only look at Google emails for now (subject contains "Google"); Amazon alerts
are left untouched in the inbox.

Run: python gmail_autoapply_watcher.py
"""

import email
import html
import imaplib
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
IMAP_HOST   = "imap.gmail.com"
IMAP_PORT   = 993
GMAIL_USER  = os.environ.get("AUTOAPPLY_GMAIL_USER", "")          # the inbox to watch
GMAIL_PASS  = os.environ.get("AUTOAPPLY_GMAIL_APP_PASSWORD", "")  # app password for it
FROM_SENDER = os.environ.get("EMAIL_SENDER", "")                  # who the scrapers send as
SUBJECT_MUST_CONTAIN = "Google"   # Google-only for now

HERE        = os.path.dirname(__file__)
QUEUE_FILE   = os.path.join(HERE, "json", "autoapply_queue.json")
APPLIED_FILE = os.path.join(HERE, "json", "autoapply_applied.json")

# Google Careers apply/signin links, with a jobId in the query string.
APPLY_LINK_RE = re.compile(
    r"https://www\.google\.com/about/careers/applications/[^\s\"'<>]*?jobId=[^\s\"'<>&]+[^\s\"'<>]*",
    re.I,
)

# ──────────────────────────────────────────────────────────────────────────────
# STATE
# ──────────────────────────────────────────────────────────────────────────────

def _load(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def _save(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)


# ──────────────────────────────────────────────────────────────────────────────
# PARSE
# ──────────────────────────────────────────────────────────────────────────────

def parse_apply_link(url: str) -> dict | None:
    """Pull job_id + human title out of a Google Careers apply URL."""
    url = html.unescape(url).replace("&amp;", "&")
    try:
        q = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(q.query)
    except ValueError:
        return None
    job_id = (params.get("jobId") or [None])[0]
    if not job_id:
        return None
    title = (params.get("title") or [""])[0]
    title = urllib.parse.unquote_plus(title).strip()
    return {"job_id": job_id, "title": title, "url": url, "source": "google"}


def extract_from_email(msg: email.message.Message) -> list[dict]:
    """Return all Google apply openings found in one email (HTML or plain)."""
    bodies = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/html", "text/plain"):
                try:
                    bodies.append(part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"))
                except (AttributeError, LookupError):
                    continue
    else:
        try:
            bodies.append(msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", errors="replace"))
        except (AttributeError, LookupError):
            pass

    found = {}
    for body in bodies:
        for m in APPLY_LINK_RE.finditer(body):
            rec = parse_apply_link(m.group(0))
            if rec:
                found[rec["job_id"]] = rec   # dedup within the email by job_id
    return list(found.values())


# ──────────────────────────────────────────────────────────────────────────────
# IMAP
# ──────────────────────────────────────────────────────────────────────────────

def fetch_new_openings() -> list[dict]:
    if not (GMAIL_USER and GMAIL_PASS):
        print("[error] AUTOAPPLY_GMAIL_USER / AUTOAPPLY_GMAIL_APP_PASSWORD not set.")
        sys.exit(1)

    print(f"[imap] connecting to {IMAP_HOST} as {GMAIL_USER} ...")
    box = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    box.login(GMAIL_USER, GMAIL_PASS)
    box.select("INBOX")

    criteria = ["UNSEEN"]
    if FROM_SENDER:
        criteria += ["FROM", FROM_SENDER]
    typ, data = box.search(None, *criteria)
    ids = data[0].split() if data and data[0] else []
    print(f"[imap] {len(ids)} unseen message(s) from {FROM_SENDER or 'anyone'}")

    openings: list[dict] = []
    for num in ids:
        typ, msg_data = box.fetch(num, "(RFC822)")
        if typ != "OK" or not msg_data or not msg_data[0]:
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
        if SUBJECT_MUST_CONTAIN.lower() not in subject.lower():
            continue  # leave non-Google (e.g. Amazon) mail untouched & unread
        found = extract_from_email(msg)
        print(f"[imap] '{subject[:60]}' -> {len(found)} opening(s)")
        openings += found
        box.store(num, "+FLAGS", "\\Seen")  # mark this Google alert processed

    box.logout()

    # dedup across all processed emails by job_id, keep first title seen
    dedup = {}
    for o in openings:
        dedup.setdefault(o["job_id"], o)
    return list(dedup.values())


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("Gmail Auto-Apply Watcher — Google (India)")
    print("=" * 60)

    openings = fetch_new_openings()
    print(f"\nUnique openings pulled from inbox: {len(openings)}")

    queue   = _load(QUEUE_FILE)
    applied = _load(APPLIED_FILE)
    known   = {r.get("job_id") for r in queue} | {r.get("job_id") for r in applied}

    now = datetime.now(timezone.utc).isoformat()
    added = 0
    for o in openings:
        if o["job_id"] in known:
            continue
        o["queued_at"] = now
        o["status"] = "queued"
        queue.append(o)
        known.add(o["job_id"])
        added += 1
        print(f"  QUEUED: {o['title'] or '(untitled)'}  [{o['job_id']}]")

    _save(QUEUE_FILE, queue)
    print(f"\nAdded {added} new opening(s). Queue depth: {len(queue)}.")
    print("Done.")


if __name__ == "__main__":
    main()
