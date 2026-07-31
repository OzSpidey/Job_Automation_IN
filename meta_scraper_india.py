"""
Meta Jobs Scraper — India
=========================
India edition of the US meta_scraper.py. Same strategy — metacareers.com is a
Relay/GraphQL SPA, so instead of parsing HTML we intercept the GraphQL response
the search page fires on load and read the structured job list out of it.

The intercept machinery here is a straight port of the US scraper's, because
each of its defences was earned by a silent failure:
  • bodies prefixed with Meta's `for (;;);` anti-hijacking guard, or split into
    several JSON objects by Relay @defer streaming — both make response.json()
    throw (see _parse_json_chunks)
  • the search field carrying a version suffix — it was
    job_search_with_featured_jobs, then _v2, and it will be _v3 one day. We
    don't hardcode it: we walk the payload for arrays of job-shaped objects and
    log the JSON path they came from, so a rename is self-diagnosing in the log
  • the search page moving path (/jobs/ 302s to /jobsearch/)

WHAT'S DIFFERENT FROM THE US VERSION
  1. India is a tiny slice of Meta's global postings, so one unfiltered sweep
     can't be trusted to surface it. We sweep the plain search page AND a set
     of India office / keyword filtered URLs, accumulate everything (deduped by
     job id), and post-filter by location. The post-filter is the source of
     truth — a wrong/ignored URL filter costs coverage, never correctness.
  2. Roles are India policy: software only (Software Engineer / Software
     Developer / SDE / new-grad-early-career). No data or BI roles — that's the
     deliberate difference from the US scrapers. Override with
     META_TARGET_ROLES (comma-separated) without touching this file; Meta's
     "Production Engineer" (their SRE-equivalent software title) is NOT in the
     default set, so add it there if you want it.
  3. NO posting dates. Meta publishes none anywhere — not in the list payload
     (fields are only id/locations/sub_teams/teams/title), not in the detail
     page's GraphQL, not in the rendered page. The US scraper proved this twice
     by probing detail pages; this one doesn't pay for those page loads at all
     and simply omits the column. New-vs-seen comes from the seen-jobs file.

A run that captures nothing, or captures jobs but finds no India posting at
all, raises ScrapeError and exits 1 — a broken payload shape surfaces as a red
Actions run instead of a silent "0 matches" email.

Run: python meta_scraper_india.py
"""

import asyncio
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright

IST_ZONE = ZoneInfo("Asia/Kolkata")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
TARGET_EMAIL    = os.environ.get("EMAIL_TO_INDIA", "")
SENDER_EMAIL    = os.environ.get("EMAIL_SENDER", "")
SENDER_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
SMTP_SERVER     = "smtp.gmail.com"
SMTP_PORT       = 465

BASE            = "https://www.metacareers.com"
# Swept in order, accumulating jobs. The first two are the plain search page
# (/jobs/ 302s to /jobsearch/); the rest narrow to India by office or keyword,
# in case the unfiltered payload doesn't reach India's postings.
SWEEP_URLS      = [
    f"{BASE}/jobs/",
    f"{BASE}/jobsearch/",
    f"{BASE}/jobs/?offices[0]=Bangalore%2C%20India",
    f"{BASE}/jobs/?offices[0]=Gurgaon%2C%20India",
    f"{BASE}/jobs/?offices[0]=Hyderabad%2C%20India",
    f"{BASE}/jobs/?q=India",
]
PAGE_TIMEOUT_MS   = 60_000
FIRST_WAIT_S      = 30   # how long to wait for the first URL's payload
FOLLOWUP_WAIT_S   = 15   # later URLs are supplementary — don't burn 30s each
SCROLL_ROUNDS     = 6    # lazy-load nudges once a payload lands
SEEN_JOBS_FILE    = os.path.join(os.path.dirname(__file__), "json", "meta_india_seen_jobs.json")
USER_AGENT        = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# India = software only (see docstring). Override via META_TARGET_ROLES.
DEFAULT_TARGET_ROLES = [
    "software engineer",
    "software developer",
    "early grad",
    "early career",
    "new grad",
    "university grad",
]
TARGET_ROLES = [
    r.strip().lower()
    for r in os.environ.get("META_TARGET_ROLES", ",".join(DEFAULT_TARGET_ROLES)).split(",")
    if r.strip()
]

EXCLUDE_SUBSTRINGS = [
    "senior", "sr.", "sr ", "staff", "lead", "principal",
    "manager", "director", "avp", "vice president", "president",
    "data center", "datacenter",
]

# Meta writes locations as "City, Country" ("Bengaluru, India", "Remote, US").
# Match on the country plus the cities Meta actually staffs in India, so an
# office label that omits the country still lands.
INDIA_CITIES = [
    "bengaluru", "bangalore", "gurgaon", "gurugram", "hyderabad",
    "mumbai", "new delhi", "delhi", "pune", "chennai", "noida",
]

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
    t = f" {title.lower()} "
    if any(x in t for x in EXCLUDE_SUBSTRINGS):
        return False
    return any(role in t for role in TARGET_ROLES)


# Field names Meta has used for these lists. Read in order, take the first that
# yields anything, so a rename doesn't silently blank the column.
LOCATION_FIELDS = ("locations", "office_locations", "offices", "locations_text", "location")
TEAM_FIELDS     = ("teams", "sub_teams", "team", "teams_text")


def _as_strings(value) -> list[str]:
    """Normalise a location/team field (str, dict, or list of either) to strings."""
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for s in [value.get("name") or value.get("text") or ""] if s]
    out: list[str] = []
    for item in value:
        out.extend(_as_strings(item))
    return out


def _field_strings(job: dict, fields: tuple[str, ...]) -> list[str]:
    for f in fields:
        vals = _as_strings(job.get(f))
        if vals:
            return vals
    return []


def extract_locations(job: dict) -> list[str]:
    return _field_strings(job, LOCATION_FIELDS)


def _is_india_location(loc: str) -> bool:
    if not loc:
        return False
    l = loc.lower()
    if "india" in l:
        return True
    return any(city in l for city in INDIA_CITIES)


def has_india_location(job: dict) -> bool:
    return any(_is_india_location(loc) for loc in extract_locations(job))


def format_locations(job: dict) -> str:
    """India offices only — a job listed in 5 countries shouldn't print all 5."""
    seen: set[str] = set()
    parts = []
    for s in extract_locations(job):
        if _is_india_location(s) and s not in seen:
            seen.add(s)
            parts.append(s)
    return " / ".join(parts)


def format_teams(job: dict) -> str:
    return ", ".join(_field_strings(job, TEAM_FIELDS))


def job_url(job: dict) -> str:
    return f"{BASE}/jobs/{job['id']}/"


# ──────────────────────────────────────────────────────────────────────────────
# PLAYWRIGHT FETCH
# ──────────────────────────────────────────────────────────────────────────────

class ScrapeError(RuntimeError):
    """The page loaded but produced no usable job payload — fail loudly."""


# Meta guards some JSON responses against hijacking with a `for (;;);` prefix,
# and Relay streams @defer'd chunks as several JSON objects in one body. Either
# makes response.json() throw — which is exactly how a working intercept ends
# up silently reporting 0 jobs.
JSON_GUARD_RE = re.compile(r"^\s*(?:for\s*\(\s*;\s*;\s*\)\s*;|\)\]\}',?)\s*")


def _parse_json_chunks(text: str) -> list:
    """Parse a body that may be plain JSON, guarded JSON, or NDJSON/concatenated."""
    text = JSON_GUARD_RE.sub("", text or "").strip()
    if not text:
        return []
    try:
        return [json.loads(text)]
    except json.JSONDecodeError:
        pass

    chunks: list = []
    for line in text.splitlines():
        line = JSON_GUARD_RE.sub("", line).strip()
        if not line:
            continue
        try:
            chunks.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if chunks:
        return chunks

    # Concatenated objects with no newline between them.
    decoder, idx = json.JSONDecoder(), 0
    while idx < len(text):
        try:
            obj, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            break
        chunks.append(obj)
        idx = end
        while idx < len(text) and text[idx].isspace():
            idx += 1
    return chunks


def _looks_like_job(d) -> bool:
    """Meta job ids are long numeric strings; that plus a title is enough."""
    if not isinstance(d, dict) or not d.get("title"):
        return False
    jid = str(d.get("id") or "")
    return jid.isdigit() and len(jid) >= 8


def _harvest_jobs(node, path: str = "", depth: int = 0, found=None) -> list:
    """Walk a decoded payload for any array of job-shaped objects.

    Returns (json_path, jobs) pairs so the log names the field the jobs came
    from — that's what identifies the new field name if Meta renames one.
    """
    if found is None:
        found = []
    if depth > 10:
        return found
    if isinstance(node, dict):
        for k, v in node.items():
            _harvest_jobs(v, f"{path}.{k}" if path else k, depth + 1, found)
    elif isinstance(node, list):
        jobs  = [d for d in node if _looks_like_job(d)]
        dicts = [d for d in node if isinstance(d, dict)]
        if jobs and len(jobs) == len(dicts):
            found.append((path or "<root>", jobs))
            return found
        for v in node:
            _harvest_jobs(v, f"{path}[]", depth + 1, found)
    return found


async def _dismiss_cookie_banner(page) -> None:
    for label in ("Allow all cookies", "Accept all", "Allow all", "Accept"):
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            if await btn.count():
                await btn.first.click(timeout=3_000)
                print(f"  [browser] dismissed cookie banner ({label!r})")
                return
        except Exception:
            continue


async def _fetch_jobs_playwright() -> list[dict]:
    all_jobs: list[dict] = []
    seen_ids: set[str]   = set()
    graphql_diag: list[str] = []   # graphql responses that yielded nothing
    other_json: set[str]    = set()  # other API endpoints, in case jobs moved

    def add(jobs: list, source: str) -> int:
        added = 0
        for j in jobs:
            jid = str(j.get("id"))
            if jid in seen_ids:
                continue
            seen_ids.add(jid)
            all_jobs.append(j)
            added += 1
        if added:
            print(f"  [capture] +{added} from {source} (total: {len(all_jobs)})")
        return added

    async def on_response(resp):
        url = resp.url
        if "graphql" not in url.lower():
            if "/api/" in url.lower():
                other_json.add(url.split("?")[0])
            return
        try:
            text = await resp.text()
        except Exception as e:
            graphql_diag.append(f"{url} — body unreadable ({type(e).__name__})")
            return

        chunks = _parse_json_chunks(text)
        if not chunks:
            graphql_diag.append(f"{url} — {len(text)}B, unparseable")
            return

        hits = 0
        for chunk in chunks:
            data = chunk.get("data") if isinstance(chunk, dict) else None
            # Fast path: the field name as of the last US fix. The suffix moves
            # (_v2 → _v3 …), so a miss here just falls through to the walk.
            for field in ("job_search_with_featured_jobs_v2", "job_search_with_featured_jobs"):
                js = (data or {}).get(field) or {}
                if isinstance(js, dict) and (js.get("all_jobs") or js.get("featured_jobs")):
                    hits += add(js.get("all_jobs") or [], f"{field}.all_jobs")
                    hits += add(js.get("featured_jobs") or [], f"{field}.featured_jobs")
                    break
            else:
                for jpath, jobs in _harvest_jobs(chunk):
                    hits += add(jobs, jpath)

        if not hits:
            first = chunks[0] if isinstance(chunks[0], dict) else {}
            keys  = sorted((first.get("data") or {}).keys()) if isinstance(first.get("data"), dict) else []
            graphql_diag.append(
                f"{url} — {len(text)}B, {len(chunks)} chunk(s), data keys={keys or 'none'}"
            )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
        )
        page = await context.new_page()
        page.on("response", on_response)

        for i, url in enumerate(SWEEP_URLS):
            print(f"  [browser] navigating to {url} ...")
            before = len(all_jobs)
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            except Exception as e:
                print(f"  [warn] navigation failed: {type(e).__name__}: {e}")
                continue
            print(f"  [browser] status={resp.status if resp else '?'} → landed on {page.url}")
            await _dismiss_cookie_banner(page)

            # networkidle fires before Relay issues the search query, so poll
            # for this URL's payload rather than trusting a single load event.
            wait_s   = FIRST_WAIT_S if i == 0 else FOLLOWUP_WAIT_S
            deadline = time.time() + wait_s
            while len(all_jobs) == before and time.time() < deadline:
                await page.wait_for_timeout(1_000)

            if len(all_jobs) > before:
                for _ in range(SCROLL_ROUNDS):   # nudge lazy-loaded result pages
                    prev = len(all_jobs)
                    await page.mouse.wheel(0, 20_000)
                    await page.wait_for_timeout(1_500)
                    if len(all_jobs) == prev:
                        break
            else:
                print(f"  [warn] no new job payload from {url}")
                if not all_jobs:
                    try:
                        print(f"  [diag] page title: {await page.title()!r}")
                        body = (await page.inner_text("body"))[:300].replace("\n", " ")
                        print(f"  [diag] body starts: {body!r}")
                    except Exception:
                        print("  [diag] page text unavailable")

            india_so_far = sum(1 for j in all_jobs if has_india_location(j))
            print(f"  [sweep] captured {len(all_jobs)} job(s) so far, "
                  f"{india_so_far} with an India location")

        await browser.close()

    if all_jobs:
        print(f"  [diag] sample job fields: {sorted(all_jobs[0].keys())}")
    else:
        print("  [diag] graphql responses observed:")
        for line in graphql_diag[:20] or ["    (none — no graphql request was made at all)"]:
            print(f"    {line}")
        if other_json:
            print("  [diag] other API endpoints seen:")
            for u in sorted(other_json)[:20]:
                print(f"    {u}")

    return all_jobs


def fetch_all_jobs() -> list[dict]:
    return asyncio.run(_fetch_jobs_playwright())


# ──────────────────────────────────────────────────────────────────────────────
# EMAIL
# ──────────────────────────────────────────────────────────────────────────────

def send_email(jobs: list[dict], previously_seen: set[str]) -> None:
    new_count = sum(1 for j in jobs if j["url"] not in previously_seen)
    count     = len(jobs)
    # Subject MUST say "Meta" — the auto-apply watcher routes on it.
    subject   = f"Meta India Jobs Scraper — {count} Matching Role(s) Found ({new_count} NEW)"

    if not jobs:
        plain = "No matching jobs found."
        html  = "<p>No matching jobs found.</p>"
    else:
        NEW_BADGE = (
            '<span style="background:#0866ff;color:#fff;font-size:11px;'
            'font-weight:bold;padding:2px 6px;border-radius:3px;margin-right:6px;">NEW</span>'
        )
        display_jobs = sorted(jobs, key=lambda j: j["url"] in previously_seen)  # new first
        rows = []
        for j in display_jobs:
            is_new = j["url"] not in previously_seen
            row_bg = "background:#eef2ff;" if is_new else ""
            badge  = NEW_BADGE if is_new else ""
            rows.append(
                f'<tr style="{row_bg}">'
                f'<td style="padding:8px;border:1px solid #ddd;">{badge}{j["title"]}</td>'
                f'<td style="padding:8px;border:1px solid #ddd;">{j.get("team", "")}</td>'
                f'<td style="padding:8px;border:1px solid #ddd;">{j.get("location", "")}</td>'
                f'<td style="padding:8px;border:1px solid #ddd;">'
                f'<a href="{j["url"]}" style="color:#0866ff">Apply</a></td>'
                f"</tr>"
            )
        role_labels = " &nbsp;|&nbsp; ".join(r.title() for r in TARGET_ROLES)
        html = f"""
        <html><body style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;color:#1c1e21">
        <h2 style="color:#0866ff">Meta Jobs — India — Software Roles</h2>
        <p>Found <strong>{count}</strong> India role(s) matching: <em>{role_labels}</em></p>
        <table style="border-collapse:collapse;width:100%;max-width:1100px">
          <tr style="background:#0866ff;color:#fff">
            <th style="padding:10px;border:1px solid #1877f2;text-align:left;width:38%">Role</th>
            <th style="padding:10px;border:1px solid #1877f2;text-align:left;width:22%">Team</th>
            <th style="padding:10px;border:1px solid #1877f2;text-align:left;width:26%">Location</th>
            <th style="padding:10px;border:1px solid #1877f2;text-align:left;width:14%">Link</th>
          </tr>
          {chr(10).join(rows)}
        </table>
        <p style="font-size:12px;color:#65676b;margin-top:20px">
          Source: metacareers.com &middot; India &middot; NEW roles first — Meta publishes no posting dates
          &middot; {datetime.now(IST_ZONE).strftime('%b %d, %Y %I:%M %p IST')}
        </p>
        </body></html>
        """
        plain = f"Found {count} India role(s) ({new_count} NEW):\n\n" + "\n".join(
            f"- {'[NEW] ' if j['url'] not in previously_seen else ''}"
            f"{j['title']} | {j.get('team', '')} | {j.get('location', '')}\n  {j['url']}"
            for j in display_jobs
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
    print("[1] Launching browser and intercepting Meta GraphQL (India sweep) ...")
    raw = fetch_all_jobs()
    print(f"  Total raw jobs captured: {len(raw)}")

    if not raw:
        raise ScrapeError(
            "no jobs captured from metacareers.com — see the [diag] lines above "
            "for the graphql responses that were seen"
        )

    if not any(extract_locations(j) for j in raw):
        raise ScrapeError(
            f"{len(raw)} jobs captured but none carry a recognised location field "
            f"(looked for {LOCATION_FIELDS}); sample fields: {sorted(raw[0].keys())}"
        )

    india_raw = [j for j in raw if has_india_location(j)]
    print(f"  India postings among them: {len(india_raw)}")
    # List them regardless of whether they match. Meta's India presence is tiny,
    # so a 0-match run is the normal case — printing the rejected titles is what
    # makes that explainable (role filter too narrow?) instead of mysterious.
    for j in india_raw:
        print(f"    [india] {j.get('title','')}  ({format_locations(j)})  {job_url(j)}")

    print("[2] Filtering: India only, software roles, excluding senior/staff/lead/manager ...")
    matched: list[dict] = []
    seen_urls: set[str] = set()
    for j in india_raw:
        title = j.get("title") or ""
        if not is_target_role(title):
            continue
        url = job_url(j)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        matched.append({
            "id":       j["id"],
            "title":    title,
            "url":      url,
            "location": format_locations(j),
            "team":     format_teams(j),
        })
        print(f"  MATCH: {title}  [{matched[-1]['location']}]")

    return matched, len(india_raw)


def main():
    print("=" * 60)
    print("Meta Jobs Scraper — India · Playwright GraphQL Intercept")
    print("=" * 60)

    t0 = time.time()
    jobs, india_seen = scan()
    elapsed = time.time() - t0

    print("\n" + "=" * 60)
    print(f"India jobs seen: {india_seen} | software matches: {len(jobs)} | elapsed: {elapsed:.1f}s")
    for j in jobs:
        print(f"  • {j['title']}  [{j.get('location','')}]")
        print(f"    {j['url']}")
    print("=" * 60)

    # Loud failure: capturing jobs but zero India postings means the sweep URLs
    # or the location field/shape changed. A red run beats a silent empty email.
    if india_seen == 0:
        raise ScrapeError(
            "jobs were captured but NONE carried an India location — the India "
            "sweep URLs or Meta's location field have changed"
        )

    previously_seen = load_seen_urls()
    new_jobs = [j for j in jobs if j["url"] not in previously_seen]
    print(f"New roles (not seen before): {len(new_jobs)}")

    save_seen_urls(previously_seen | {j["url"] for j in jobs})

    if not new_jobs:
        print("No new roles — skipping email.")
    elif not TARGET_EMAIL:
        print("[warn] EMAIL_TO_INDIA not configured — skipping email.")
    else:
        print(f"\nSending email ({len(new_jobs)} new role(s))...")
        send_email(jobs, previously_seen)
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except ScrapeError as e:
        print(f"\n[FATAL] scrape failed loudly (no silent skip): {e}", flush=True)
        sys.exit(1)
