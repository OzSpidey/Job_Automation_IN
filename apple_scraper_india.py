"""
Apple Jobs Scraper — India
==========================
India edition of the US apple_scraper.py. Same strategy — hit Apple's internal
JSON search endpoint directly:

    POST https://jobs.apple.com/api/v1/search

— but filtered to India postings and to Software / SDE roles only, then emailed
to the India recipient(s) (EMAIL_TO_INDIA). Dates are shown in IST.

India filtering is defensive. Apple's location facet token isn't publicly
documented, so we try a few candidates (postLocation-IND / postLocation-India);
whichever returns India results wins. If none do, we fall back to the global
"newest" feed and post-filter by country. Either way, the country post-filter
below is the source of truth — a wrong facet only costs coverage depth, never
correctness. The run FAILS LOUDLY (non-zero exit) if it can't find a single
India posting, so a broken facet/layout surfaces as a red Actions run rather
than a silent empty email.

Run: python apple_scraper_india.py
"""

import http.cookiejar
import json
import os
import re
import smtplib
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
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

API_URL         = "https://jobs.apple.com/api/v1/search"
SEARCH_PAGE_URL = "https://jobs.apple.com/en-in/search"
DETAILS_LOCALE  = "en-in"
PAGE_SIZE       = 20        # Apple returns exactly 20 per page
KW_PAGES        = 10        # pages fetched per keyword query (10 * 20 = 200 each)
MAX_AGE_DAYS    = 14        # ignore jobs posted more than 14 days ago
REQUEST_DELAY_S = 0.4
SEEN_JOBS_FILE  = os.path.join(os.path.dirname(__file__), "json", "apple_india_api_seen_jobs.json")
USER_AGENT      = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# India is only ~0.4% of Apple's global postings, so a "newest global" sweep
# can't surface India software roles. Instead we search by keyword (Apple's
# search box) and post-filter to India — this targets exactly what we want.
SOFTWARE_QUERIES = [
    "software engineer",
    "software developer",
    "software development engineer",
    "sde",
    "backend engineer",
    "full stack engineer",
]

# India = software only (matches the Google India scraper's SWE-only policy).
TARGET_ROLES = [
    "software engineer",
    "software developer",
    "software development engineer",
    "software development",
    "software engineering",
    "sde",
    "full stack",
    "back end",
    "backend",
    "front end",
    "frontend",
    "new grad",
    "university graduate",
    "early career",
]

# Skip senior+ levels — we want entry/mid software roles.
EXCLUDE_LEVELS = ["senior", "sr.", "principal", "lead", "staff", "manager",
                  "director", "architect"]

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
    if any(level in t for level in EXCLUDE_LEVELS):
        return False
    return any(role in t for role in TARGET_ROLES)


def parse_gmt_date(s: str) -> datetime:
    """Parse Apple's postDateInGMT (nanosecond ISO like '2026-05-14T18:01:47.961313540Z')."""
    if not s:
        return datetime.min.replace(tzinfo=timezone.utc)
    s = s.rstrip("Z")
    if "." in s:
        base, frac = s.split(".", 1)
        s = base + "." + frac[:6]   # Python can't parse >6 fractional digits
    try:
        return datetime.fromisoformat(s + "+00:00")
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def is_within_max_age(gmt_str: str) -> bool:
    dt = parse_gmt_date(gmt_str)
    if dt == datetime.min.replace(tzinfo=timezone.utc):
        return True  # keep if date unknown
    age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    return age_days <= MAX_AGE_DAYS


def format_date_ist(gmt_str: str) -> str:
    """Apple's GMT post date rendered in IST. e.g. 'May 14, 2026 11:31:47 PM IST'."""
    dt = parse_gmt_date(gmt_str)
    if dt == datetime.min.replace(tzinfo=timezone.utc):
        return ""
    ist = dt.astimezone(IST_ZONE)
    time_str = ist.strftime("%I:%M:%S %p").lstrip("0")
    return f"{ist.strftime('%b %d, %Y')} {time_str} IST"


def _loc_is_india(loc: dict) -> bool:
    country = (loc.get("countryName") or "").strip().lower()
    code    = (loc.get("countryCode") or loc.get("countryID") or "").strip().upper()
    if country == "india":
        return True
    if code in {"IND", "IN"}:
        return True
    return False


def is_india_job(job: dict) -> bool:
    return any(_loc_is_india(loc) for loc in (job.get("locations") or []))


def format_locations(job: dict) -> str:
    parts, seen = [], set()
    for loc in (job.get("locations") or []):
        city    = loc.get("city") or ""
        state   = loc.get("stateProvince") or ""
        country = loc.get("countryName") or ""
        label   = ", ".join(filter(None, [city, state, country]))
        if label and label not in seen:
            seen.add(label)
            parts.append(label)
    return " / ".join(parts) if parts else ""


def job_url(job: dict) -> str:
    pos_id    = job.get("positionId") or ""
    slug      = job.get("transformedPostingTitle") or ""
    team_code = (job.get("team") or {}).get("teamCode") or ""
    url = f"https://jobs.apple.com/{DETAILS_LOCALE}/details/{pos_id}/{slug}"
    if team_code:
        url += f"?team={team_code}"
    return url


# ──────────────────────────────────────────────────────────────────────────────
# SESSION + API FETCH
# ──────────────────────────────────────────────────────────────────────────────

def make_opener() -> urllib.request.OpenerDirector:
    """Opener with a cookie jar seeded by the search page (Apple's API needs it)."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(SEARCH_PAGE_URL,
                                 headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with opener.open(req, timeout=20):
        pass
    return opener


def fetch_page(opener, page: int, query: str = "") -> dict:
    payload = json.dumps({
        "query":   query,
        "filters": {},
        "page":    page,
        "locale":  "en-in",
        "sort":    "newest",
        "format":  {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"},
    }).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "User-Agent":   USER_AGENT,
            "Content-Type": "application/json",
            "Accept":       "application/json",
            "Referer":      SEARCH_PAGE_URL,
            "Origin":       "https://jobs.apple.com",
        },
        method="POST",
    )
    with opener.open(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_query_jobs() -> list[dict]:
    """Search each software keyword, page through, keep the India postings.
    Deduped across keywords by positionId."""
    opener = make_opener()
    by_pos: dict[str, dict] = {}
    for kw in SOFTWARE_QUERIES:
        kept = 0
        for page in range(1, KW_PAGES + 1):
            try:
                data = fetch_page(opener, page, query=kw)
            except Exception as exc:
                print(f"  [kw:{kw!r}] page={page} error: {exc} — stopping this query.")
                break
            jobs = (data.get("res") or {}).get("searchResults") or []
            if not jobs:
                break
            for j in jobs:
                if is_india_job(j):
                    pos = j.get("positionId") or job_url(j)
                    if pos not in by_pos:
                        by_pos[pos] = j
                        kept += 1
            if len(jobs) < PAGE_SIZE:
                break
            time.sleep(REQUEST_DELAY_S)
        print(f"  [kw:{kw!r}] India postings kept so far: {len(by_pos)} (+{kept} from this query)")
    return list(by_pos.values())


# ──────────────────────────────────────────────────────────────────────────────
# EMAIL
# ──────────────────────────────────────────────────────────────────────────────

def send_email(jobs: list[dict], previously_seen: set[str]) -> None:
    new_count = sum(1 for j in jobs if j["url"] not in previously_seen)
    count     = len(jobs)
    subject   = f"Apple India Jobs Scraper — {count} Matching Role(s) Found ({new_count} NEW)"

    if not jobs:
        plain = "No matching jobs found."
        html  = "<p>No matching jobs found.</p>"
    else:
        NEW_BADGE = ('<span style="background:#0071e3;color:#fff;font-size:11px;'
                     'font-weight:bold;padding:2px 6px;border-radius:3px;margin-right:6px;">NEW</span>')
        rows = []
        for j in jobs:
            is_new = j["url"] not in previously_seen
            row_bg = "background:#f0f7ff;" if is_new else ""
            badge  = NEW_BADGE if is_new else ""
            rows.append(
                f'<tr style="{row_bg}">'
                f'<td style="padding:8px;border:1px solid #d2d2d7;">{badge}{j["title"]}</td>'
                f'<td style="padding:8px;border:1px solid #d2d2d7;">{j.get("location", "")}</td>'
                f'<td style="padding:8px;border:1px solid #d2d2d7;">'
                f'<a href="{j["url"]}" style="color:#0071e3">Apply</a></td>'
                f'<td style="padding:8px;border:1px solid #d2d2d7;white-space:nowrap;">{j.get("date", "")}</td>'
                f"</tr>"
            )
        html = f"""
        <html><body style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;color:#1d1d1f">
        <h2 style="color:#1d1d1f">Apple Jobs — India — Software Roles</h2>
        <p>Found <strong>{count}</strong> role(s) matching:
           <em>Software Engineer &nbsp;|&nbsp; Software Developer &nbsp;|&nbsp; SDE &nbsp;|&nbsp; New Grad / Early Career</em></p>
        <table style="border-collapse:collapse;width:100%;max-width:1100px">
          <tr style="background:#1d1d1f;color:#f5f5f7">
            <th style="padding:10px;border:1px solid #424245;text-align:left;width:38%">Role</th>
            <th style="padding:10px;border:1px solid #424245;text-align:left;width:30%">Location</th>
            <th style="padding:10px;border:1px solid #424245;text-align:left;width:12%">Link</th>
            <th style="padding:10px;border:1px solid #424245;text-align:left;width:20%">Date Posted</th>
          </tr>
          {chr(10).join(rows)}
        </table>
        <p style="font-size:12px;color:#86868b;margin-top:20px">
          Source: jobs.apple.com/api/v1/search &middot; India &middot; Newest First
        </p>
        </body></html>
        """
        plain = f"Found {count} matching role(s) ({new_count} NEW):\n\n" + "\n".join(
            f"- {'[NEW] ' if j['url'] not in previously_seen else ''}"
            f"{j['title']} — {j.get('location', 'location unknown')}\n"
            f"  {j.get('date', '')}\n  {j['url']}"
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

def scan() -> tuple[list[dict], int]:
    print(f"[1] Searching Apple by software keyword ({len(SOFTWARE_QUERIES)} queries), "
          f"India-filtered...")
    raw = fetch_query_jobs()   # already India-only
    india_seen = len(raw)
    print(f"  India software-search postings found: {india_seen}")

    # [debug] What does the API expose per job? Looking for qualifications /
    # experience text so we can classify level from requirements, not title.
    if raw:
        s = raw[0]
        print(f"[debug] job keys: {sorted(s.keys())}")
        for k in ("jobSummary", "minimumQualifications", "preferredQualifications",
                  "description", "keyQualifications", "educationExperience",
                  "reasonableAccommodation", "postingTitle"):
            v = s.get(k)
            if v:
                print(f"[debug] {k}: {str(v)[:400]}")

    print(f"[2] Filtering by target role title + recency (<= {MAX_AGE_DAYS} days)...")
    matched: list[dict] = []
    seen_urls: set[str] = set()
    for j in raw:
        title = j.get("postingTitle") or ""
        gmt = j.get("postDateInGMT") or ""
        role_ok = is_target_role(title)
        age_ok  = is_within_max_age(gmt)
        print(f"  [india] role_ok={role_ok} age_ok={age_ok} | {title!r} | {format_date_ist(gmt) or gmt or '?'}")
        if not role_ok:
            continue
        if not age_ok:
            continue
        url = job_url(j)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        matched.append({
            "title":    title,
            "url":      url,
            "location": format_locations(j),
            "date":     format_date_ist(gmt),
            "gmt":      gmt,
        })
        print(f"  MATCH: {title}  [{matched[-1]['location']}]")

    matched.sort(key=lambda j: parse_gmt_date(j["gmt"]), reverse=True)
    return matched, india_seen


def main():
    print("=" * 60)
    print("Apple Jobs Scraper — India · Newest First")
    print("=" * 60)

    t0 = time.time()
    jobs, india_seen = scan()
    elapsed = time.time() - t0

    print("\n" + "=" * 60)
    print(f"India jobs seen: {india_seen} | software matches: {len(jobs)} | elapsed: {elapsed:.1f}s")
    for j in jobs:
        print(f"  • {j['title']}\n    {j['url']}")
    print("=" * 60)

    # Loud failure: the software-keyword search returning zero India postings
    # means the country filter or Apple's API/response shape changed — surface
    # it as a red run instead of a silent empty email.
    if india_seen == 0:
        print("[FATAL] Software search returned 0 India postings — country filter/API likely broken.")
        sys.exit(1)

    previously_seen = load_seen_urls()
    new_jobs = [j for j in jobs if j["url"] not in previously_seen]
    print(f"New roles (not seen before): {len(new_jobs)}")

    jobs.sort(key=lambda j: parse_gmt_date(j["gmt"]), reverse=True)
    jobs.sort(key=lambda j: j["url"] in previously_seen)  # new first

    save_seen_urls(previously_seen | {j["url"] for j in jobs})

    if not new_jobs:
        print("No new roles — skipping email.")
    else:
        print(f"\nSending email ({len(new_jobs)} new role(s))...")
        send_email(jobs, previously_seen)
    print("Done.")


if __name__ == "__main__":
    main()
