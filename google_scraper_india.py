"""
Google Jobs Scraper — India
===========================
India edition of the US google_scraper.py. Same Boq-scraping strategy, but
filtered to India postings and emailed to the India recipient(s).

Google's careers site is server-rendered (no public JSON API as of 2026).
This scrapes the embedded Boq framework state from the HTML response:

    https://www.google.com/about/careers/applications/jobs/results?sort_by=date&location=India&page=N

The page HTML contains an `AF_initDataCallback({key: 'ds:1', ..., data:[...]})`
block carrying the rendered jobs as a positional array. We regex it out and
parse the JSON.

Strategy: poll-shallow. Fetch only the first MAX_PAGES (newest by date),
diff against a seen-file, email on new matches. A secondary keyword pass
catches roles that don't surface in the date-sorted feed.

Boq positional fields (mapped from a live response — may shift):
  job[0]  = id
  job[1]  = title
  job[2]  = apply URL   (".../signin?jobId=...&loc=IN&title=...")
  job[9]  = locations   list of [display, [display], city, null, state, country_code]
  job[12] = [unix_sec, nanos]  posted timestamp
  job[20] = experience level code (1=Early, 2=Mid, 3=Advanced, 4=Expert/Director)

Run: python google_scraper_india.py
"""

import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

IST_ZONE = ZoneInfo("Asia/Kolkata")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
TARGET_EMAIL    = os.environ.get("EMAIL_TO_INDIA", "")
SENDER_EMAIL    = os.environ.get("EMAIL_SENDER", "")
SENDER_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
SMTP_SERVER     = "smtp.gmail.com"
SMTP_PORT       = 465

SEARCH_URL      = "https://www.google.com/about/careers/applications/jobs/results"
LOCATION_FILTER = "India"    # URL-level filter, smaller universe than unfiltered
MAX_PAGES       = 10         # 10 pages × ~20 jobs = 200 newest India postings per run
REQUEST_DELAY_S = 0.6        # polite pause between page fetches

# Secondary keyword search pass — catches roles that don't surface in the
# date-sorted feed (Google's sort_by=date is not strictly chronological).
KEYWORD_SEARCHES = [
    "software engineer",
    "software developer",
    "early career",
]
SEEN_JOBS_FILE  = os.path.join(os.path.dirname(__file__), "json", "google_india_seen_jobs.json")
USER_AGENT      = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

TARGET_ROLES = [
    "software engineer",
    "software developer",
    "early grad",
    "early career",
    "new grad",
]

# Exclude senior+ levels — we want entry/mid only.
EXCLUDE_SUBSTRINGS = [
    "senior", "sr.", "sr ", "staff", "lead", "principal",
    "manager", "director", "avp", "vice president", "president",
    "data center", "datacenter",
]

# Matches the Boq initial-state block carrying the jobs list.
DS1_PATTERN = re.compile(
    r"AF_initDataCallback\(\{key:\s*'ds:1'.*?data:(\[.*?\])\s*,\s*sideChannel",
    re.S,
)

# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def load_seen_urls() -> set[str]:
    if not os.path.exists(SEEN_JOBS_FILE):
        return set()
    with open(SEEN_JOBS_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_seen_urls(urls: set[str]) -> None:
    os.makedirs(os.path.dirname(SEEN_JOBS_FILE), exist_ok=True)
    with open(SEEN_JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(urls), f, indent=2)


def is_target_role(title: str) -> bool:
    t = title.lower()
    if any(x in t for x in EXCLUDE_SUBSTRINGS):
        return False
    return any(role in t for role in TARGET_ROLES)


_DUMP_FIELDS_DONE = False  # print raw indices for first job once per run

# Google's internal level codes (observed from live responses — may shift)
_LEVEL_MAP = {
    1: "Early",
    2: "Mid",
    3: "Advanced",
    4: "Expert / Director",
}

def parse_job(raw: list) -> dict | None:
    """Pull (id, title, url, locations, posted_ts, level) from a Boq job array.

    Indices are stable as of mapping but we double-check by structural cues
    (apply URL must contain '/signin?jobId='). If structure looks wrong we
    return None rather than emit garbage.
    """
    global _DUMP_FIELDS_DONE
    if not isinstance(raw, list) or len(raw) < 10:
        return None
    job_id = raw[0] if isinstance(raw[0], str) else None
    title  = raw[1] if isinstance(raw[1], str) else None
    url    = raw[2] if isinstance(raw[2], str) and "/signin?jobId=" in raw[2] else None
    if not (job_id and title and url):
        return None

    if "--dump-fields" in sys.argv and not _DUMP_FIELDS_DONE:
        _DUMP_FIELDS_DONE = True
        print(f"\n[dump-fields] Raw Boq array for: {title}")
        for i, v in enumerate(raw):
            print(f"  [{i}] {repr(v)[:120]}")
        print()

    # Locations: list of [display, [display], city, null, state, country_code]
    locations = raw[9] if len(raw) > 9 and isinstance(raw[9], list) else []

    # Posted timestamp: [unix_seconds, nanos]
    posted_ts = None
    if len(raw) > 12 and isinstance(raw[12], list) and raw[12] and isinstance(raw[12][0], int):
        posted_ts = raw[12][0]

    # Experience level: index 20 holds a single int.
    level = None
    if len(raw) > 20 and isinstance(raw[20], int):
        level = _LEVEL_MAP.get(raw[20])

    return {
        "id":        job_id,
        "title":     title,
        "url":       url,
        "locations": locations,
        "posted_ts": posted_ts,
        "level":     level,
    }


def is_india_job(job: dict) -> bool:
    """True if any location has country_code 'IN'."""
    for loc in (job.get("locations") or []):
        if isinstance(loc, list) and len(loc) >= 6 and (loc[5] or "").upper() == "IN":
            return True
        # fallback: display string ending in ", India"
        if isinstance(loc, list) and loc and isinstance(loc[0], str) and loc[0].upper().endswith(", INDIA"):
            return True
    # extra fallback: apply URL's loc param
    return "loc=IN" in (job.get("url") or "")


def format_locations(job: dict) -> str:
    """Render a multi-city posting as 'Bengaluru, Karnataka, India / Hyderabad, ...'."""
    cities = []
    seen = set()
    for loc in (job.get("locations") or []):
        if isinstance(loc, list) and loc and isinstance(loc[0], str):
            disp = loc[0]
            if disp not in seen:
                seen.add(disp)
                cities.append(disp)
    return " / ".join(cities)


def format_date(ts: int | None) -> str:
    """IST wallclock with seconds. e.g. 'May 14, 2026 6:30:23 PM IST'."""
    if not ts:
        return ""
    try:
        dt = datetime.fromtimestamp(ts, tz=IST_ZONE)
    except (OverflowError, OSError, ValueError):
        return ""
    # %I gives zero-padded 12-hour; strip the leading zero for readability
    time_str = dt.strftime("%I:%M:%S %p").lstrip("0")
    return f"{dt.strftime('%b %d, %Y')} {time_str} IST"


def format_ago(ts: int | None) -> str:
    """Relative posting age. e.g. 'Posted 25 minutes ago'."""
    if not ts:
        return ""
    delta = int(time.time()) - ts
    if delta < 0:
        return "Posted just now"
    if delta < 60:
        return f"Posted {delta} second{'s' if delta != 1 else ''} ago"
    if delta < 3600:
        m = delta // 60
        return f"Posted {m} minute{'s' if m != 1 else ''} ago"
    if delta < 86400:
        h = delta // 3600
        return f"Posted {h} hour{'s' if h != 1 else ''} ago"
    d = delta // 86400
    return f"Posted {d} day{'s' if d != 1 else ''} ago"


# ──────────────────────────────────────────────────────────────────────────────
# FETCH
# ──────────────────────────────────────────────────────────────────────────────

def fetch_page(page: int) -> list[dict]:
    """Fetch one results page, extract the ds:1 Boq block, return parsed jobs."""
    params = {"sort_by": "date", "page": str(page), "location": LOCATION_FILTER}
    url    = SEARCH_URL + "?" + urllib.parse.urlencode(params)
    req    = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", errors="replace")
    m = DS1_PATTERN.search(html)
    if not m:
        print(f"  [page {page}] WARN: ds:1 block not found — Google may have changed layout")
        return []
    try:
        ds1 = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        print(f"  [page {page}] WARN: ds:1 parse failed: {e}")
        return []
    raw_jobs = ds1[0] if ds1 and isinstance(ds1[0], list) else []
    parsed   = [j for j in (parse_job(r) for r in raw_jobs) if j]
    if page == 1 and len(ds1) > 2:
        print(f"  [page 1] Google reports {ds1[2]} total matches; page_size={ds1[3] if len(ds1) > 3 else '?'}")
    return parsed


def fetch_recent_jobs(max_pages: int = MAX_PAGES) -> list[dict]:
    results = []
    for page in range(1, max_pages + 1):
        jobs = fetch_page(page)
        if not jobs:
            print(f"  [page {page}] empty — stopping")
            break
        results.extend(jobs)
        print(f"  [page {page}]  fetched={len(jobs)}  cumulative={len(results)}")
        if page < max_pages:
            time.sleep(REQUEST_DELAY_S)
    return results


def fetch_keyword_jobs(keywords: list[str]) -> list[dict]:
    """Fetch page 1 of a keyword search for each term, return all parsed jobs."""
    results = []
    for kw in keywords:
        params = {"q": kw, "location": LOCATION_FILTER}
        url    = SEARCH_URL + "?" + urllib.parse.urlencode(params)
        req    = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                html = r.read().decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"  [kw:{kw!r}] ERROR: {exc}")
            time.sleep(REQUEST_DELAY_S)
            continue
        m = DS1_PATTERN.search(html)
        if not m:
            print(f"  [kw:{kw!r}] WARN: ds:1 block not found")
            time.sleep(REQUEST_DELAY_S)
            continue
        try:
            ds1 = json.loads(m.group(1))
        except json.JSONDecodeError:
            print(f"  [kw:{kw!r}] WARN: JSON parse failed")
            time.sleep(REQUEST_DELAY_S)
            continue
        raw_jobs = ds1[0] if ds1 and isinstance(ds1[0], list) else []
        parsed   = [j for j in (parse_job(r) for r in raw_jobs) if j]
        matched  = [j for j in parsed if is_india_job(j) and is_target_role(j["title"])]
        print(f"  [kw:{kw!r}]  fetched={len(parsed)}  matched={len(matched)}")
        results.extend(matched)
        time.sleep(REQUEST_DELAY_S)
    return results


# ──────────────────────────────────────────────────────────────────────────────
# EMAIL
# ──────────────────────────────────────────────────────────────────────────────

def send_email(jobs: list[dict], previously_seen: set[str]) -> None:
    new_count = sum(1 for j in jobs if j["url"] not in previously_seen)
    count     = len(jobs)
    subject   = f"Google India Jobs Scraper — {count} Matching Role(s) Found ({new_count} NEW)"

    if not jobs:
        plain = "No matching jobs found."
        html  = "<p>No matching jobs found.</p>"
    else:
        NEW_BADGE = '<span style="background:#4285F4;color:#fff;font-size:11px;font-weight:bold;padding:2px 6px;border-radius:3px;margin-right:6px;">NEW</span>'
        rows = []
        for j in jobs:
            is_new   = j["url"] not in previously_seen
            is_early = (j.get("level") or "").strip().lower() == "early"
            # Green wins over the new-role blue: an Early-career role is the
            # signal we most want to spot at a glance.
            if is_early:
                row_bg = 'background:#d6f5d6;'
            elif is_new:
                row_bg = 'background:#f0f6ff;'
            else:
                row_bg = ''
            badge  = NEW_BADGE if is_new else ''
            rows.append(
                f'<tr style="{row_bg}">'
                f'<td style="padding:8px;border:1px solid #ddd;">{badge}{j["title"]}</td>'
                f'<td style="padding:8px;border:1px solid #ddd;">{j.get("level", "")}</td>'
                f'<td style="padding:8px;border:1px solid #ddd;">{j.get("location", "")}</td>'
                f'<td style="padding:8px;border:1px solid #ddd;"><a href="{j["url"]}">Apply</a></td>'
                f'<td style="padding:8px;border:1px solid #ddd;white-space:nowrap;">'
                f'{j.get("date", "")}'
                f'<br><span style="font-size:11px;color:#666">({j.get("ago", "")})</span>'
                f'</td>'
                f'</tr>'
            )
        html = f"""
        <html><body style="font-family:Arial,sans-serif;color:#333">
        <h2 style="color:#202124">Google Jobs — India — Matching Roles</h2>
        <p>Found <strong>{count}</strong> role(s) matching:
           <em>Software Engineer &nbsp;|&nbsp; Software Developer &nbsp;|&nbsp; Early Grad / Early Career</em>
        </p>
        <table style="border-collapse:collapse;width:100%;max-width:1100px">
          <tr style="background:#202124;color:#FBBC04">
            <th style="padding:10px;border:1px solid #555;text-align:left;width:36%">Role</th>
            <th style="padding:10px;border:1px solid #555;text-align:left;width:8%">Level</th>
            <th style="padding:10px;border:1px solid #555;text-align:left;width:28%">Location</th>
            <th style="padding:10px;border:1px solid #555;text-align:left;width:10%">Link</th>
            <th style="padding:10px;border:1px solid #555;text-align:left;width:13%">Date Posted</th>
          </tr>
          {chr(10).join(rows)}
        </table>
        <p style="font-size:12px;color:#888;margin-top:20px">
          Source: google.com/about/careers · India · Most Recent · top {MAX_PAGES} page(s)
        </p>
        </body></html>
        """
        plain = f"Found {count} matching role(s) ({new_count} NEW):\n\n" + "\n".join(
            f"- {'[NEW] ' if j['url'] not in previously_seen else ''}{j['title']} — {j.get('location', '?')}\n  {j.get('date', '?')} ({j.get('ago', '?')})\n  {j['url']}"
            for j in jobs
        )

    recipients = [a.strip() for a in TARGET_EMAIL.split(",") if a.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as srv:
        srv.login(SENDER_EMAIL, SENDER_PASSWORD)
        srv.sendmail(SENDER_EMAIL, recipients, msg.as_string())

    print(f"[email] Sent to {', '.join(recipients)} — {count} job(s).")


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def scan() -> list[dict]:
    print(f"[1] Fetching first {MAX_PAGES} page(s) of google.com/about/careers (sort_by=date, location=India)...")
    raw = fetch_recent_jobs(MAX_PAGES)
    print(f"  Total raw jobs: {len(raw)}")

    print("[2] Filtering for India locations...")
    india_jobs = [j for j in raw if is_india_job(j)]
    print(f"  India jobs: {len(india_jobs)}")

    print("[3] Filtering by target role title...")
    matched = []
    seen_urls = set()
    for j in india_jobs:
        if not is_target_role(j["title"]):
            continue
        if j["url"] in seen_urls:
            continue
        seen_urls.add(j["url"])
        matched.append({
            "title":    j["title"],
            "url":      j["url"],
            "location": format_locations(j),
            "date":     format_date(j["posted_ts"]),
            "ago":      format_ago(j["posted_ts"]),
            "posted_ts": j["posted_ts"] or 0,
            "level":    j.get("level") or "",
        })
        print(f"  MATCH: {j['title']}  [{matched[-1]['location']}]")

    print(f"[4] Keyword search pass ({len(KEYWORD_SEARCHES)} queries)...")
    for j in fetch_keyword_jobs(KEYWORD_SEARCHES):
        if j["url"] in seen_urls:
            continue
        seen_urls.add(j["url"])
        matched.append({
            "title":     j["title"],
            "url":       j["url"],
            "location":  format_locations(j),
            "date":      format_date(j["posted_ts"]),
            "ago":       format_ago(j["posted_ts"]),
            "posted_ts": j["posted_ts"] or 0,
            "level":     j.get("level") or "",
        })
        print(f"  MATCH (kw): {j['title']}  [{format_locations(j)}]")

    matched.sort(key=lambda j: j["posted_ts"], reverse=True)
    return matched


def main():
    preview = "--preview" in sys.argv

    print("=" * 60)
    print("Google Jobs Scanner — India · Most Recent" + ("  [PREVIEW MODE]" if preview else ""))
    print("=" * 60)

    t0 = time.time()
    jobs = scan()
    elapsed = time.time() - t0

    print("\n" + "=" * 60)
    print(f"Total matches: {len(jobs)} | elapsed: {elapsed:.1f}s")
    for j in jobs:
        print(f"  • {j['title']}")
        print(f"    {j['url']}")
    print("=" * 60)

    previously_seen = load_seen_urls()

    # Email only includes roles posted in the last 7 days. Jobs without a
    # parseable timestamp are kept (better to over-include than silently drop).
    week_cutoff = int(time.time()) - 7 * 24 * 3600
    email_jobs = [j for j in jobs if not j["posted_ts"] or j["posted_ts"] >= week_cutoff]

    # Sort: new (not in seen) at top, then by recency desc within each group.
    email_jobs.sort(key=lambda j: (j["url"] in previously_seen, -j["posted_ts"]))

    new_jobs = [j for j in email_jobs if j["url"] not in previously_seen]
    print(f"New roles (not seen before, <=7d): {len(new_jobs)}")
    print(f"Roles in email body (<=7d): {len(email_jobs)}")

    if preview:
        # Don't update seen file so the next normal run still flags real "new"s.
        if not email_jobs:
            print("Preview: no recent roles to show.")
        else:
            print(f"\n[PREVIEW] Sending email with all {len(email_jobs)} recent role(s)...")
            send_email(email_jobs, previously_seen)
        print("Done. (Seen-file not updated in preview mode.)")
        return

    # Normal mode: update seen file (covers ALL matched, including >7d, so an
    # 8-day-old role doesn't keep re-flagging as new on each run), then email
    # only if there's at least one new recent role.
    save_seen_urls(previously_seen | {j["url"] for j in jobs})

    if not new_jobs:
        print("No new recent roles — skipping email.")
    else:
        print(f"\nSending email ({len(new_jobs)} new role(s))...")
        send_email(email_jobs, previously_seen)
    print("Done.")


if __name__ == "__main__":
    main()
