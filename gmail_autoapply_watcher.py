"""
Gmail Auto-Apply Watcher — India (Google + Apple)
=================================================
Polls the dedicated auto-apply inbox (oswin.autoapply@gmail.com) over IMAP,
finds new scraper-alert emails, extracts the apply/job links, and appends any
genuinely-new openings to json/autoapply_queue.json.

Two sources are handled, routed by the email subject:
  • "Google …"  → Google Careers apply links (jobId in the query string).
                  EARLY-level roles only (Google caps applications ~3/month).
  • "Apple …"   → Apple jobs.apple.com/details/<positionId>/<slug> links.
                  ALL software/SDE roles (Apple has no monthly cap), no level gate.

Every queued row carries a "source" so the matching applier
(google_autoapply.py / apple_autoapply.py) only picks up its own jobs.

Dedup is layered and source-aware:
  - json/autoapply_queue.json    roles waiting to be applied to
  - json/autoapply_applied.json  roles already applied to (never re-queue)
Keyed on (source, job_id), so a Google jobId can never collide with an Apple
positionId. Processed Google/Apple emails are marked \\Seen; a rescan is
harmless anyway because the (source, job_id) dedup catches it. Any email from
a different sender, or whose subject is neither Google nor Apple (e.g. Amazon
alerts, personal mail), is left completely untouched and unread.

Run: python gmail_autoapply_watcher.py
"""

import email
import email.header
from email.message import Message
import html
import imaplib
import json
import os
import re
import sys
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

HERE         = os.path.dirname(__file__)
QUEUE_FILE   = os.path.join(HERE, "json", "autoapply_queue.json")
APPLIED_FILE = os.path.join(HERE, "json", "autoapply_applied.json")

_TAG_RE = re.compile(r"<[^>]+>")
_TR_RE  = re.compile(r"<tr\b.*?</tr>", re.S | re.I)
_TD_RE  = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S | re.I)

# ── Google ────────────────────────────────────────────────────────────────────
# Google Careers apply/signin links, with a jobId in the query string.
GOOGLE_LINK_RE = re.compile(
    r"https://www\.google\.com/about/careers/applications/[^\s\"'<>]*?jobId=[^\s\"'<>&]+[^\s\"'<>]*",
    re.I,
)
# Google caps applications at ~3/month, so we ONLY auto-apply to EARLY roles.
# The scraper email carries Google's level in a dedicated cell; these are the
# exact strings it renders.
LEVEL_STRINGS = {"early", "mid", "advanced", "expert / director"}
ALLOW_LEVELS  = {"early"}   # Google: queue Early only; everything else skipped

# ── Apple ─────────────────────────────────────────────────────────────────────
# Apple job detail links carry the positionId: /details/<positionId>/<slug>
APPLE_LINK_RE = re.compile(
    r"https://jobs\.apple\.com/[^\s\"'<>]*?/details/\d+/[^\s\"'<>]*",
    re.I,
)
APPLE_ID_RE = re.compile(r"/details/(\d+)/([^/?\s\"'<>]*)", re.I)

# ── Naukri ──────────────────────────────────────────────────────────────────────
# Naukri job links: naukri.com/job-listings-<slug>-<trailing-digits>
NAUKRI_LINK_RE = re.compile(r"https://www\.naukri\.com/job-listings-[^\s\"'<>]+", re.I)
NAUKRI_ID_RE   = re.compile(r"-(\d+)(?:\?|#|$)")

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


def _decode_part(part) -> str:
    try:
        return part.get_payload(decode=True).decode(
            part.get_content_charset() or "utf-8", errors="replace")
    except (AttributeError, LookupError):
        return ""


def _bodies(msg: Message) -> tuple[str, str]:
    """(html_body, plain_body) from an email message."""
    html_body = plain_body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/html" and not html_body:
                html_body = _decode_part(part)
            elif ctype == "text/plain" and not plain_body:
                plain_body = _decode_part(part)
    else:
        html_body = _decode_part(msg)
    return html_body, plain_body


# ──────────────────────────────────────────────────────────────────────────────
# GOOGLE PARSE (unchanged behaviour: Early-only, row-aware level gate)
# ──────────────────────────────────────────────────────────────────────────────

def parse_google_link(url: str) -> dict | None:
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
    title = urllib.parse.unquote_plus((params.get("title") or [""])[0]).strip()
    return {"job_id": job_id, "title": title, "url": url, "source": "google"}


def _row_level(row_html: str) -> str:
    """Level from the row's dedicated Level cell. Exact match so a title like
    'Software Engineer, Early Career' isn't mistaken for the 'Early' level."""
    for cell in _TD_RE.findall(row_html):
        text = html.unescape(_TAG_RE.sub("", cell)).strip()
        if text.lower() in LEVEL_STRINGS:
            return text
    return ""


def extract_google(html_body: str) -> list[dict]:
    """Google apply openings, tagged with level. Only EARLY roles are kept."""
    found: dict[str, dict] = {}
    skipped = 0
    if not html_body:
        print("    (no HTML body — can't read levels; skipping all Google)")
        return []
    for row in _TR_RE.findall(html_body):
        m = GOOGLE_LINK_RE.search(row)
        if not m:
            continue
        rec = parse_google_link(m.group(0))
        if not rec:
            continue
        level = _row_level(row)
        if level.lower() not in ALLOW_LEVELS:
            skipped += 1
            continue
        rec["level"] = level
        found[rec["job_id"]] = rec
    if skipped:
        print(f"    (skipped {skipped} non-Early Google role(s) — Early only)")
    return list(found.values())


# ──────────────────────────────────────────────────────────────────────────────
# APPLE PARSE (all software/SDE roles — Apple has no monthly cap, no level gate)
# ──────────────────────────────────────────────────────────────────────────────

def _slug_to_title(slug: str) -> str:
    slug = urllib.parse.unquote(slug).strip()
    if not slug:
        return ""
    return re.sub(r"\s+", " ", slug.replace("-", " ")).strip().title()


def parse_apple_link(url: str, row_title: str = "") -> dict | None:
    """Pull positionId + title out of an Apple jobs.apple.com details URL."""
    url = html.unescape(url).replace("&amp;", "&")
    m = APPLE_ID_RE.search(url)
    if not m:
        return None
    position_id = m.group(1)
    title = (row_title or "").strip() or _slug_to_title(m.group(2))
    return {"job_id": position_id, "title": title, "url": url, "source": "apple"}


def _row_role_text(row_html: str) -> str:
    """First cell's text (the Role column). Strips a leading 'NEW' badge whose
    text sits in the same cell before the title (e.g. 'NEWSoftware Engineer')."""
    cells = _TD_RE.findall(row_html)
    if not cells:
        return ""
    text = html.unescape(_TAG_RE.sub("", cells[0])).strip()
    if text.startswith("NEW") and len(text) > 3 and text[3].isupper():
        text = text[3:].strip()
    return text


def extract_apple(html_body: str, plain_body: str) -> list[dict]:
    """Every Apple opening in the email (no level filtering)."""
    found: dict[str, dict] = {}
    if html_body:
        for row in _TR_RE.findall(html_body):
            m = APPLE_LINK_RE.search(row)
            if not m:
                continue
            rec = parse_apple_link(m.group(0), _row_role_text(row))
            if rec:
                found[rec["job_id"]] = rec
    # Fallback: scan the whole body (covers non-table layouts / plain text).
    if not found:
        for body in (html_body, plain_body):
            for m in APPLE_LINK_RE.finditer(body or ""):
                rec = parse_apple_link(m.group(0))
                if rec:
                    found.setdefault(rec["job_id"], rec)
    return list(found.values())


# ──────────────────────────────────────────────────────────────────────────────
# NAUKRI PARSE (all software roles — the scraper already filters to SWE/recent)
# ──────────────────────────────────────────────────────────────────────────────

def parse_naukri_link(url: str, row_title: str = "") -> dict | None:
    """Pull job_id (trailing digits) + title out of a Naukri job-listings URL."""
    url = html.unescape(url).replace("&amp;", "&")
    path = urllib.parse.urlparse(url).path
    m = NAUKRI_ID_RE.search(path)
    job_id = m.group(1) if m else path  # fall back to the path if no id
    title = (row_title or "").strip()
    return {"job_id": job_id, "title": title, "url": url, "source": "naukri"}


def extract_naukri(html_body: str, plain_body: str) -> list[dict]:
    """Every Naukri opening in the email (no level gate — scraper filtered already)."""
    found: dict[str, dict] = {}
    if html_body:
        for row in _TR_RE.findall(html_body):
            m = NAUKRI_LINK_RE.search(row)
            if not m:
                continue
            rec = parse_naukri_link(m.group(0), _row_role_text(row))
            if rec:
                found[rec["job_id"]] = rec
    if not found:
        for body in (html_body, plain_body):
            for m in NAUKRI_LINK_RE.finditer(body or ""):
                rec = parse_naukri_link(m.group(0))
                if rec:
                    found.setdefault(rec["job_id"], rec)
    return list(found.values())


# ──────────────────────────────────────────────────────────────────────────────
# DISPATCH
# ──────────────────────────────────────────────────────────────────────────────

def source_from_subject(subject: str) -> str | None:
    s = (subject or "").lower()
    if "google" in s:
        return "google"
    if "apple" in s:
        return "apple"
    if "naukri" in s:
        return "naukri"
    return None


def extract_from_email(msg: Message, source: str) -> list[dict]:
    html_body, plain_body = _bodies(msg)
    if source == "google":
        return extract_google(html_body)
    if source == "apple":
        return extract_apple(html_body, plain_body)
    if source == "naukri":
        return extract_naukri(html_body, plain_body)
    return []


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
        source = source_from_subject(subject)
        if not source:
            continue  # leave non-Google/Apple mail untouched & unread
        found = extract_from_email(msg, source)
        print(f"[imap] [{source}] '{subject[:60]}' -> {len(found)} opening(s)")
        openings += found
        box.store(num, "+FLAGS", "\\Seen")  # mark this alert processed

    box.logout()

    # dedup across all processed emails by (source, job_id), keep first seen
    dedup: dict[tuple, dict] = {}
    for o in openings:
        dedup.setdefault((o["source"], o["job_id"]), o)
    return list(dedup.values())


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def _key(row: dict) -> tuple:
    return (row.get("source", "google"), row.get("job_id"))


def main() -> None:
    print("=" * 60)
    print("Gmail Auto-Apply Watcher — Google + Apple (India)")
    print("=" * 60)

    openings = fetch_new_openings()
    print(f"\nUnique openings pulled from inbox: {len(openings)}")

    queue   = _load(QUEUE_FILE)
    applied = _load(APPLIED_FILE)
    known   = {_key(r) for r in queue} | {_key(r) for r in applied}

    now = datetime.now(timezone.utc).isoformat()
    added = 0
    for o in openings:
        if _key(o) in known:
            continue
        o["queued_at"] = now
        o["status"] = "queued"
        queue.append(o)
        known.add(_key(o))
        added += 1
        tag = f"{o['source']}/{o.get('level') or 'n/a'}"
        print(f"  QUEUED [{tag}]: {o['title'] or '(untitled)'}")

    _save(QUEUE_FILE, queue)
    n_google = sum(1 for r in queue if r.get("source", "google") == "google")
    n_apple  = sum(1 for r in queue if r.get("source") == "apple")
    n_naukri = sum(1 for r in queue if r.get("source") == "naukri")
    print(f"\nAdded {added} new opening(s). Queue depth: {len(queue)} "
          f"(google={n_google}, apple={n_apple}, naukri={n_naukri}).")
    print("Done.")


if __name__ == "__main__":
    main()
