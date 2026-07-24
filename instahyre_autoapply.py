"""
Instahyre Auto-Apply — India
============================
Instahyre is login-gated + AI-matched: a logged-in "Opportunities" feed curated
to your profile. This runs ONE logged-in Playwright session (headful under Xvfb,
replays INSTAHYRE_SESSION_B64), reads the matched feed via Instahyre's own API,
and applies to new Undecided opportunities, then emails a summary table.

No keyword scraping / no shared queue / no watcher — the feed IS the queue, and
it's matched to your Instahyre profile (so keep that profile software-focused).

Feed API (Tastypie):
  GET /api/v1/candidate_opportunities/candidate_opportunity/  -> {objects:[...], meta}
  object: {id, interview_status(0=Undecided), employer:{company_name}, job:{title,
           candidate_title, locations, opportunity_url}, resource_uri}
Apply flow: open job's opportunity_url -> click "Apply"/"I'm Interested".

Modes:
  --recon   Read the feed via API, list opportunities + statuses. No browsing.
  --walk    Open ONE Undecided opportunity's page, screenshot + dump the controls
            (to map the Apply button). Never clicks Apply.
  --apply   Apply to new Undecided opportunities — but only click Apply when
            INSTAHYRE_ENABLE_SUBMIT=1. APPLY_LIMIT caps submissions/run.

Env: INSTAHYRE_SESSION_B64 / INSTAHYRE_SESSION_FILE, INSTAHYRE_ENABLE_SUBMIT,
     APPLY_LIMIT, INSTAHYRE_ANSWERS_JSON (reserved), EMAIL_SENDER/GMAIL_APP_PASSWORD/APPLY_NOTIFY_EMAIL
"""

import base64
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

BASE          = "https://www.instahyre.com"
FEED_API      = BASE + "/api/v1/candidate_opportunities/candidate_opportunity/?limit=50"
_IST          = ZoneInfo("Asia/Kolkata")

# --search recon: candidate category pages to probe (recon reveals which are valid
# + the real search API + whether an apply cap/paywall appears).
SEARCH_URLS = [
    BASE + "/software-engineer-jobs/",
    BASE + "/python-developer-jobs/",
    BASE + "/backend-developer-jobs/",
]
LIMIT_MARKERS = ["go premium", "upgrade to premium", "premium to apply", "applies left",
                 "application limit", "you have reached", "daily limit", "reached your limit",
                 "buy premium", "limit reached", "premium members"]

HERE          = os.path.dirname(__file__)
APPLIED_FILE  = os.path.join(HERE, "json", "instahyre_applied.json")
SCREENSHOT_DIR = os.path.join(HERE, "screenshots")
RECON_DIR     = os.path.join(HERE, "recon")

SESSION_FILE  = os.environ.get("INSTAHYRE_SESSION_FILE", os.path.join(HERE, "instahyre_session.json"))
ENABLE_SUBMIT = os.environ.get("INSTAHYRE_ENABLE_SUBMIT", "") == "1"
APPLY_LIMIT   = int(os.environ.get("APPLY_LIMIT", "0"))       # 0 = all Undecided this run
MAX_ATTEMPTS  = int(os.environ.get("INSTAHYRE_MAX_ATTEMPTS", "3"))

# interview_status: 0 = Undecided (not acted on). Non-zero = already interested/declined.
STATUS_UNDECIDED = 0
APPLY_BTN_RE = re.compile(r"^\s*(apply|apply now|i\s*am\s*interested|i'?m\s*interested|interested)\s*$", re.I)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ──────────────────────────────────────────────────────────────────────────────
# STATE
# ──────────────────────────────────────────────────────────────────────────────

def _load(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def _save(path: str, rows: list) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)


def ensure_session_file() -> str:
    if os.path.exists(SESSION_FILE):
        return SESSION_FILE
    b64 = os.environ.get("INSTAHYRE_SESSION_B64", "")
    if not b64:
        print("[error] No session: set INSTAHYRE_SESSION_B64 or INSTAHYRE_SESSION_FILE.")
        sys.exit(1)
    with open(SESSION_FILE, "wb") as f:
        f.write(base64.b64decode(b64))
    return SESSION_FILE


def _fmt(iso: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ist = dt.astimezone(_IST)
        return f"{ist.strftime('%b %d, %Y')}, {ist.strftime('%I:%M %p').lstrip('0')} IST"
    except ValueError:
        return iso


# ──────────────────────────────────────────────────────────────────────────────
# FEED (via Instahyre's own API, using the session cookies)
# ──────────────────────────────────────────────────────────────────────────────

def _opp_fields(o: dict) -> dict:
    job = o.get("job") or {}
    emp = o.get("employer") or {}
    locs = job.get("locations")
    if isinstance(locs, list):
        locs = ", ".join(str(x) for x in locs)
    return {
        "id":       str(o.get("id") or ""),
        "status":   o.get("interview_status"),
        "title":    job.get("candidate_title") or job.get("title") or "(role)",
        "company":  emp.get("company_name") or "",
        "location": locs or "",
        "url":      BASE + (job.get("opportunity_url") or ""),
    }


def fetch_feed(page) -> list[dict]:
    """GET the matched-opportunities feed via the session-authenticated API."""
    r = page.request.get(FEED_API)
    if not r.ok:
        print(f"[feed] API {r.status} — is the session valid?")
        return []
    data = r.json()
    objs = data.get("objects", []) if isinstance(data, dict) else []
    return [_opp_fields(o) for o in objs]


# ──────────────────────────────────────────────────────────────────────────────
# PAGE HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def looks_logged_out(page) -> bool:
    u = (page.url or "").lower()
    return "/login" in u or "/register" in u


def snap(page, name: str) -> None:
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    os.makedirs(RECON_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:60]
    try:
        page.screenshot(path=os.path.join(SCREENSHOT_DIR, f"{safe}.png"), full_page=True)
    except Exception as exc:
        print(f"  [warn] screenshot {safe}: {exc}")
    try:
        with open(os.path.join(RECON_DIR, f"{safe}.html"), "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception as exc:
        print(f"  [warn] dom {safe}: {exc}")


def dump_ctas(page, tag: str) -> None:
    print(f"\n--- controls [{tag}]  url={page.url}")
    for role in ("button", "link"):
        try:
            loc = page.get_by_role(role); n = loc.count()
        except Exception:
            n = 0
        names = []
        for i in range(min(n, 40)):
            try:
                t = (loc.nth(i).inner_text(timeout=250) or "").strip()
            except Exception:
                continue
            if t:
                names.append(t.replace("\n", " ")[:30])
        if names:
            print(f"  {role}({n}): {names}")


def find_apply(page):
    for role in ("button", "link"):
        try:
            loc = page.get_by_role(role); n = loc.count()
        except Exception:
            n = 0
        for i in range(min(n, 40)):
            el = loc.nth(i)
            try:
                label = (el.inner_text(timeout=250) or "").strip()
            except Exception:
                continue
            if label and APPLY_BTN_RE.match(label) and el.is_enabled():
                return el
    return None


def apply_succeeded(page) -> bool:
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        body = ""
    return any(m in body for m in ("you have applied", "application sent", "you're interested",
                                   "you are interested", "interest sent", "applied successfully",
                                   "we have notified"))


# ──────────────────────────────────────────────────────────────────────────────
# SUMMARY EMAIL (same format as Naukri: Role linked | Company | Location | Applied)
# ──────────────────────────────────────────────────────────────────────────────

def send_run_summary_email(jobs: list[dict]) -> None:
    recipient = os.environ.get("APPLY_NOTIFY_EMAIL", "").strip()
    sender    = os.environ.get("EMAIL_SENDER", "")
    password  = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not jobs:
        return
    if not (recipient and sender and password):
        print("  [notify] APPLY_NOTIFY_EMAIL / EMAIL_SENDER / GMAIL_APP_PASSWORD not set — skipping summary.")
        return
    n = len(jobs)
    subject = f"Instahyre Auto-Apply — {n} role(s) applied"
    rows = []
    for j in jobs:
        rows.append(
            f'<tr>'
            f'<td style="padding:8px;border:1px solid #ddd;">'
            f'<a href="{j.get("url","")}" style="color:#0a66c2;text-decoration:none;font-weight:600">{j.get("title","(role)")}</a></td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{j.get("company","") or ""}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{j.get("location","") or ""}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;white-space:nowrap;">{_fmt(j.get("applied_at",""))}</td>'
            f'</tr>'
        )
    html = f"""<html><body style="font-family:Arial,sans-serif;color:#333">
      <h2 style="color:#188038">&#10003; Instahyre Auto-Apply — {n} role(s) applied</h2>
      <p>Expressed interest this run (role name links to the Instahyre posting):</p>
      <table style="border-collapse:collapse;width:100%;max-width:1000px">
        <tr style="background:#4a4a4a;color:#fff">
          <th style="padding:10px;border:1px solid #555;text-align:left;width:38%">Role</th>
          <th style="padding:10px;border:1px solid #555;text-align:left;width:22%">Company</th>
          <th style="padding:10px;border:1px solid #555;text-align:left;width:20%">Location</th>
          <th style="padding:10px;border:1px solid #555;text-align:left;width:20%">Applied</th>
        </tr>
        {chr(10).join(rows)}
      </table>
      <p style="font-size:12px;color:#888;margin-top:20px">Auto-applied via Job_Automation_IN &middot;
      {datetime.now(timezone.utc).strftime('%b %d, %Y %H:%M UTC')}</p>
    </body></html>"""
    plain = f"Instahyre Auto-Apply — {n} role(s) applied:\n\n" + "\n".join(
        f"- {j.get('title','(role)')} @ {j.get('company','') or '?'} | {j.get('location','') or '?'}"
        f" | {_fmt(j.get('applied_at','')) or '?'}\n  {j.get('url','')}" for j in jobs)
    recipients = [a.strip() for a in recipient.split(",") if a.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject; msg["From"] = sender; msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(plain, "plain")); msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(sender, password)
            srv.sendmail(sender, recipients, msg.as_string())
        print(f"  [notify] summary emailed to {', '.join(recipients)} ({n} role(s)).")
    except Exception as exc:
        print(f"  [notify] summary email failed: {exc}")


# ──────────────────────────────────────────────────────────────────────────────
# MODES
# ──────────────────────────────────────────────────────────────────────────────

def recon(page) -> None:
    feed = fetch_feed(page)
    print(f"Feed opportunities: {len(feed)}")
    for o in feed:
        state = "Undecided" if o["status"] == STATUS_UNDECIDED else f"status={o['status']}"
        print(f"  [{state}] {o['title']} @ {o['company']} — {o['location']}  {o['url']}")


def search(page, captured: list) -> None:
    """Recon the keyword-search path: probe category pages, list job links, watch
    for an apply cap/paywall, and surface any search API XHR that was intercepted."""
    print("Search recon — probing category pages (no applies)")
    for url in SEARCH_URLS:
        print(f"\n[search] GET {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(5000)
        except Exception as exc:
            print(f"  [warn] nav: {exc}")
        snap(page, "instahyre_search_" + re.sub(r"\W+", "_", url)[-28:])
        try:
            links = page.eval_on_selector_all(
                "a[href*='/job-']", "els => els.map(e => e.getAttribute('href'))")
        except Exception:
            links = []
        links = sorted({l for l in links if l})
        print(f"  final url : {page.url}")
        print(f"  job links : {len(links)}  e.g. {links[:3]}")
        try:
            body = (page.inner_text("body") or "").lower()
        except Exception:
            body = ""
        hits = [m for m in LIMIT_MARKERS if m in body]
        print(f"  limit/paywall markers: {hits or 'none'}")
    print(f"\nIntercepted API XHR ({len(captured)}):")
    for u in captured[:15]:
        print(f"  {u}")


def walk(page) -> None:
    feed = [o for o in fetch_feed(page) if o["status"] == STATUS_UNDECIDED]
    if not feed:
        print("No Undecided opportunities to walk."); return
    o = feed[0]
    print(f"\n[walk] {o['title']} @ {o['company']}  {o['url']}")
    page.goto(o["url"], wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)
    snap(page, f"instahyre_walk_{o['id']}")
    dump_ctas(page, f"walk_{o['id']}")
    cta = find_apply(page)
    print(f"  apply CTA found: {bool(cta)} (walk does NOT click)")


def apply(page) -> None:
    applied = _load(APPLIED_FILE)
    applied_ids = {r.get("id") for r in applied}
    feed = fetch_feed(page)
    undecided = [o for o in feed if o["status"] == STATUS_UNDECIDED and o["id"] not in applied_ids]
    print(f"Feed: {len(feed)} | new Undecided to apply: {len(undecided)} | submit={'ON' if ENABLE_SUBMIT else 'off'}")

    budget = APPLY_LIMIT or len(undecided)
    applied_now: list[dict] = []
    for o in undecided:
        if len(applied_now) >= budget:
            break
        print(f"\n[apply] {o['title']} @ {o['company']}  ({o['id']})")
        try:
            page.goto(o["url"], wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            if looks_logged_out(page):
                print("  [abort] session challenged — stopping."); break
            cta = find_apply(page)
            if not cta:
                print("  no Apply CTA found — skipping."); continue
            if not ENABLE_SUBMIT:
                print("  [dry-run] INSTAHYRE_ENABLE_SUBMIT!=1 — Apply available, not clicking.")
                continue
            cta.click(timeout=4000)
            page.wait_for_timeout(4000)
            snap(page, f"instahyre_applied_{o['id']}")
            # confirm via page text OR by re-checking the feed status flipped off Undecided
            ok = apply_succeeded(page)
            if not ok:
                fresh = {x["id"]: x for x in fetch_feed(page)}
                ok = fresh.get(o["id"], {}).get("status", STATUS_UNDECIDED) != STATUS_UNDECIDED
            if ok:
                o["applied_at"] = datetime.now(timezone.utc).isoformat()
                o["status"] = "applied"
                applied.append(o)
                applied_now.append(o)
                print("  [submit] applied (interest expressed).")
            else:
                print("  [skip] no success signal after Apply — not recorded.")
        except Exception as exc:
            print(f"  [error] {exc}")

    _save(APPLIED_FILE, applied)
    send_run_summary_email(applied_now)
    print(f"\nApplied this run: {len(applied_now)} | total applied: {len(applied)}")


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    mode = "recon"
    if "--walk" in sys.argv:   mode = "walk"
    if "--apply" in sys.argv:  mode = "apply"
    if "--search" in sys.argv: mode = "search"
    print("=" * 60)
    print(f"Instahyre Auto-Apply — India · mode={mode} · submit={'ON' if ENABLE_SUBMIT else 'off'}")
    print("=" * 60)

    session = ensure_session_file()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,   # headful under Xvfb
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        context = browser.new_context(
            storage_state=session,
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            locale="en-IN", timezone_id="Asia/Kolkata",
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
            viewport={"width": 1280, "height": 900},
        )
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = context.new_page()
        captured: list = []
        if mode == "search":
            page.on("response", lambda r: captured.append(f"{r.status} {r.url}")
                    if ("instahyre.com" in r.url and ("/api/" in r.url or "job" in r.url.lower()
                        or "search" in r.url.lower())) else None)
        try:
            if mode == "recon":    recon(page)
            elif mode == "walk":   walk(page)
            elif mode == "search": search(page, captured)
            else:                  apply(page)
        finally:
            context.close(); browser.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
