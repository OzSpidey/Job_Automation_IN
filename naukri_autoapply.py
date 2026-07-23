"""
Naukri Auto-Apply — India
=========================
Sibling of google/apple autoapply, for Naukri. Drives Naukri's logged-in site
with Playwright, replaying a captured session (NAUKRI_SESSION_B64). Naukri
blocks HEADLESS Chrome, so this runs HEADFUL under Xvfb (workflow sets
DISPLAY=:99). No monthly cap; APPLY_LIMIT bounds submissions per run.

Reads ONLY source=="naukri" rows from the shared json/autoapply_queue.json and
preserves other sources' rows on save.

Naukri apply flow (mapped at recon): a logged-in job page has an on-site
"Apply" button (sometimes followed by a chatbot Q&A), OR an "Apply on company
site" button that redirects off-platform — we SKIP those (can't auto-submit).

Modes:
  --recon   Open each queued job, screenshot + dump DOM, detect apply-button
            type (on-site vs company-site), and report auth. Click on-site
            Apply once to capture the next screen (chatbot?). No submission.
  --walk    Take ONE job as far as possible, screenshot each step, stop before
            final submit.
  --apply   Real run: apply — but only actually submit when NAUKRI_ENABLE_SUBMIT=1.
            fill_chatbot() is a stub until recon reveals the Q&A, so apply can't
            blind-submit a half-answered form.

Env:
  NAUKRI_SESSION_B64 / NAUKRI_SESSION_FILE   captured session
  NAUKRI_ANSWERS_JSON      chatbot answers (shape TBD after recon)
  NAUKRI_ENABLE_SUBMIT     "1" to actually submit (default off)
  APPLY_LIMIT              max submissions per run (0 = whole naukri queue)
  RECON_LIMIT             max jobs for --recon (default 4)
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

SOURCE = "naukri"

HERE           = os.path.dirname(__file__)
QUEUE_FILE     = os.path.join(HERE, "json", "autoapply_queue.json")
APPLIED_FILE   = os.path.join(HERE, "json", "autoapply_applied.json")
SCREENSHOT_DIR = os.path.join(HERE, "screenshots")
RECON_DIR      = os.path.join(HERE, "recon")

SESSION_FILE  = os.environ.get("NAUKRI_SESSION_FILE", os.path.join(HERE, "naukri_session.json"))
ENABLE_SUBMIT = os.environ.get("NAUKRI_ENABLE_SUBMIT", "") == "1"
RECON_LIMIT   = int(os.environ.get("RECON_LIMIT", "4"))
APPLY_LIMIT   = int(os.environ.get("APPLY_LIMIT", "0"))  # 0 = whole naukri queue (per run)
MAX_STEPS     = 8
MAX_ATTEMPTS  = int(os.environ.get("NAUKRI_MAX_ATTEMPTS", "3"))  # drop a stuck job after N tries

# On-site apply CTAs (we can complete these). Company-site = external redirect.
APPLY_BTN_RE   = re.compile(r"^\s*(apply|i am interested|apply now)\s*$", re.I)
COMPANY_BTN_RE = re.compile(r"apply on company site|company site|apply on", re.I)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


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


def ensure_session_file() -> str:
    if os.path.exists(SESSION_FILE):
        return SESSION_FILE
    b64 = os.environ.get("NAUKRI_SESSION_B64", "")
    if not b64:
        print("[error] No session: set NAUKRI_SESSION_B64 or NAUKRI_SESSION_FILE.")
        sys.exit(1)
    with open(SESSION_FILE, "wb") as f:
        f.write(base64.b64decode(b64))
    return SESSION_FILE


def answers() -> dict:
    return json.loads(os.environ.get("NAUKRI_ANSWERS_JSON", "{}") or "{}")


def send_run_summary_email(jobs: list[dict]) -> None:
    """One summary email after the run: a table of the roles applied to this run,
    each role name hyperlinked to its Naukri posting."""
    recipient = os.environ.get("APPLY_NOTIFY_EMAIL", "").strip()
    sender    = os.environ.get("EMAIL_SENDER", "")
    password  = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not jobs:
        return
    if not (recipient and sender and password):
        print("  [notify] APPLY_NOTIFY_EMAIL / EMAIL_SENDER / GMAIL_APP_PASSWORD not set — skipping summary.")
        return
    n = len(jobs)
    subject = f"Naukri Auto-Apply — {n} role(s) applied"
    rows = []
    for j in jobs:
        title = j.get("title") or "(role)"
        url   = j.get("url", "")
        rows.append(
            f'<tr>'
            f'<td style="padding:8px;border:1px solid #ddd;">'
            f'<a href="{url}" style="color:#0a66c2;text-decoration:none;font-weight:600">{title}</a></td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{j.get("company", "") or ""}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{j.get("location", "") or ""}</td>'
            f'</tr>'
        )
    html = f"""<html><body style="font-family:Arial,sans-serif;color:#333">
      <h2 style="color:#188038">&#10003; Naukri Auto-Apply — {n} role(s) applied</h2>
      <p>Applications submitted this run (role name links to the Naukri posting):</p>
      <table style="border-collapse:collapse;width:100%;max-width:900px">
        <tr style="background:#4a4a4a;color:#fff">
          <th style="padding:10px;border:1px solid #555;text-align:left;width:50%">Role</th>
          <th style="padding:10px;border:1px solid #555;text-align:left;width:25%">Company</th>
          <th style="padding:10px;border:1px solid #555;text-align:left;width:25%">Location</th>
        </tr>
        {chr(10).join(rows)}
      </table>
      <p style="font-size:12px;color:#888;margin-top:20px">Auto-applied via Job_Automation_IN &middot;
      {datetime.now(timezone.utc).strftime('%b %d, %Y %H:%M UTC')}</p>
    </body></html>"""
    plain = f"Naukri Auto-Apply — {n} role(s) applied:\n\n" + "\n".join(
        f"- {j.get('title', '(role)')} @ {j.get('company', '') or '?'} | {j.get('location', '') or '?'}\n  {j.get('url', '')}"
        for j in jobs
    )
    recipients = [a.strip() for a in recipient.split(",") if a.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(sender, password)
            srv.sendmail(sender, recipients, msg.as_string())
        print(f"  [notify] summary emailed to {', '.join(recipients)} ({n} role(s)).")
    except Exception as exc:
        print(f"  [notify] summary email failed: {exc}")


# ──────────────────────────────────────────────────────────────────────────────
# PAGE HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def looks_logged_out(page) -> bool:
    url = (page.url or "").lower()
    if "nlogin/login" in url or "/login" in url:
        return True
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    # Logged-in pages show the profile; logged-out show a prominent login/register.
    return ("login or register" in body or "login to apply" in body
            or "register to apply" in body)


def apply_succeeded(page) -> bool:
    """Naukri confirms an apply two ways: (1) a redirect to
    .../myapply/saveApply?...:200, or (2) an 'Apply Confirmation' page whose
    banner reads 'Applied to <role>'. Accept either."""
    u = (page.url or "").lower()
    if "saveapply" in u and ":200" in u:
        return True
    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""
    if "apply confirmation" in title:
        return True
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        body = ""
    return any(m in body for m in ("applied to", "successfully applied", "application sent",
                                   "you have applied", "applied successfully"))


def chatbot_open(page) -> bool:
    """Heuristic: Naukri sometimes opens a recruiter Q&A drawer after Apply."""
    for sel in ('[class*="chatbot" i]', '[class*="chatBot"]', 'div._chatBot',
                'div[class*="drawer"] textarea', 'div[class*="drawer"] input'):
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


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


def dump_controls(page, tag: str) -> None:
    print(f"\n--- controls [{tag}]  url={page.url}")
    for role in ("button", "link", "textbox", "radio", "checkbox", "combobox", "listbox"):
        try:
            loc = page.get_by_role(role)
            n = loc.count()
        except Exception:
            n = 0
        if not n:
            continue
        names = []
        for i in range(min(n, 18)):
            try:
                el = loc.nth(i)
                name = (el.get_attribute("aria-label") or (el.inner_text(timeout=300) or "").strip())
            except Exception:
                name = "?"
            names.append((name or "·").replace("\n", " ")[:40])
        print(f"  {role}({n}): {names}")


def find_apply(page) -> tuple[str, object] | None:
    """Return ('onsite'|'company', locator) for the apply CTA, or None."""
    for role in ("button", "link"):
        try:
            loc = page.get_by_role(role)
            n = loc.count()
        except Exception:
            n = 0
        for i in range(min(n, 40)):
            el = loc.nth(i)
            try:
                label = (el.inner_text(timeout=300) or el.get_attribute("aria-label") or "").strip()
            except Exception:
                continue
            if not label:
                continue
            if COMPANY_BTN_RE.search(label):
                return ("company", el)
            if APPLY_BTN_RE.match(label):
                return ("onsite", el)
    return None


# ──────────────────────────────────────────────────────────────────────────────
# CHATBOT FILL (STUB — mapped after recon)
# ──────────────────────────────────────────────────────────────────────────────

def fill_chatbot(page, ans: dict) -> None:
    """Answer Naukri's post-apply chatbot Q&A. NOT implemented until recon shows
    it; raising guarantees --apply can't blind-submit a half-answered form."""
    raise NotImplementedError(
        "Naukri chatbot not mapped yet — run --recon, inspect artifacts, then implement."
    )


# ──────────────────────────────────────────────────────────────────────────────
# MODES
# ──────────────────────────────────────────────────────────────────────────────

def recon(page, jobs: list[dict]) -> None:
    authed = True
    for i, job in enumerate(jobs[:RECON_LIMIT]):
        jid = job["job_id"]
        print(f"\n[recon {i+1}] {jid} {job.get('title','')}")
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
        except Exception as exc:
            print(f"  [warn] nav: {exc}")
        snap(page, f"naukri_job_{jid}")
        dump_controls(page, f"job_{jid}")
        out = looks_logged_out(page)
        authed = authed and not out
        print(f"  url: {page.url}\n  session: {'LOGGED OUT' if out else 'authenticated'}")

        cta = find_apply(page)
        if not cta:
            print("  apply CTA: none found")
            continue
        kind, _ = cta
        print(f"  apply CTA: {kind}")
        if kind == "company":
            print("  -> company-site redirect; SKIP in apply mode.")
        else:
            # A click here SUBMITS immediately (one-click), so recon never clicks.
            print("  -> on-site ONE-CLICK Apply available (recon does NOT click — a click submits).")
    print("\nRECON RESULT:", "session survives." if authed else "session did NOT hold (recapture).")


def walk(page, jobs: list[dict], ans: dict) -> None:
    # Naukri Apply is one-click (a click submits), so walk NEVER clicks it — it
    # only inspects. Use --apply (armed) to actually submit.
    job = jobs[0]
    print(f"\n[walk] {job.get('title','')}  {job['job_id']}")
    page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)
    snap(page, "naukri_walk0")
    dump_controls(page, "walk0")
    if looks_logged_out(page):
        print("  session challenged."); return
    cta = find_apply(page)
    print(f"  apply CTA: {cta[0] if cta else 'none'}")
    print("  walk does NOT click (Naukri Apply is one-click submit). Use --apply when armed.")


def apply(page, jobs: list[dict], ans: dict, others: list[dict]) -> None:
    applied = _load(APPLIED_FILE)
    remaining: list[dict] = []
    applied_now: list[dict] = []
    dropped = 0
    submit_budget = APPLY_LIMIT or len(jobs)   # APPLY_LIMIT = max SUBMISSIONS/run
    submitted_count = 0

    def requeue_or_drop(job: dict, reason: str) -> None:
        """Retry a stuck job up to MAX_ATTEMPTS, then drop it so the queue can't
        clog (fully-automated workflow, no manual cleanup)."""
        nonlocal dropped
        job["attempts"] = job.get("attempts", 0) + 1
        if job["attempts"] >= MAX_ATTEMPTS:
            dropped += 1
            print(f"  dropping after {job['attempts']} attempt(s): {reason}")
        else:
            remaining.append(job)

    for job in jobs:
        if submitted_count >= submit_budget:
            remaining.append(job); continue     # per-run submit cap — leave the rest queued
        jid = job["job_id"]
        print(f"\n[naukri {jid}] {job.get('title','')}")
        submitted = False
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            if looks_logged_out(page):
                # Session issue, not the job's fault — requeue without counting an attempt.
                print("  [abort] session challenged — leaving queued (will retry).")
                remaining.append(job); continue
            cta = find_apply(page)
            if cta and cta[0] == "company":
                # User policy: fully-automated apply IGNORES company-site jobs entirely.
                print("  company-site redirect — IGNORED (dropped from queue).")
                dropped += 1; continue
            if not cta:
                print("  no on-site Apply CTA (already applied / not applyable).")
                requeue_or_drop(job, "no apply CTA"); continue
            if not ENABLE_SUBMIT:
                # A click submits instantly, so never click in dry-run. Not an attempt.
                print("  [dry-run] NAUKRI_ENABLE_SUBMIT!=1 — on-site Apply available, not clicking.")
                remaining.append(job); continue

            cta[1].click(timeout=4000)   # one-click submit
            page.wait_for_timeout(5000)
            snap(page, f"naukri_applied_{jid}")

            if apply_succeeded(page):
                submitted = True
                print("  [submit] Apply succeeded.")
            elif chatbot_open(page):
                try:
                    fill_chatbot(page, ans)
                    page.wait_for_timeout(3000)
                    submitted = apply_succeeded(page)
                except NotImplementedError:
                    print("  [skip] recruiter chatbot appeared — not mapped yet.")
                if not submitted:
                    requeue_or_drop(job, "chatbot not mapped"); continue
            else:
                print("  [skip] no success signal after Apply.")
                requeue_or_drop(job, "no success signal"); continue
        except Exception as exc:
            print(f"  [error] {exc}")
            requeue_or_drop(job, f"error: {exc}"); continue

        if submitted:
            job["applied_at"] = datetime.now(timezone.utc).isoformat()
            job["status"] = "applied"
            applied.append(job)
            applied_now.append(job)
            submitted_count += 1

    _save(QUEUE_FILE, others + remaining)   # keep other-source rows intact
    _save(APPLIED_FILE, applied)
    send_run_summary_email(applied_now)      # one summary table after the run
    print(f"\nApplied this run: {len(applied_now)} | dropped: {dropped} | "
          f"still queued: {len(others + remaining)} (naukri={len(remaining)}, other={len(others)})")


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    mode = "recon"
    if "--walk" in sys.argv:  mode = "walk"
    if "--apply" in sys.argv: mode = "apply"

    print("=" * 60)
    print(f"Naukri Auto-Apply — India · mode={mode} · submit={'ON' if ENABLE_SUBMIT else 'off'}")
    print("=" * 60)

    all_jobs = _load(QUEUE_FILE)
    jobs   = [j for j in all_jobs if j.get("source") == SOURCE]
    others = [j for j in all_jobs if j.get("source") != SOURCE]
    print(f"Queue depth (naukri): {len(jobs)}  |  other-source rows kept intact: {len(others)}")
    if not jobs:
        print("Nothing queued for Naukri. Run the watcher first.")
        return

    session = ensure_session_file()
    ans = answers()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        # HEADFUL under Xvfb — Naukri blocks headless Chrome.
        browser = p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        context = browser.new_context(
            storage_state=session,
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
            viewport={"width": 1280, "height": 900},
        )
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = context.new_page()
        try:
            if mode == "recon":  recon(page, jobs)
            elif mode == "walk": walk(page, jobs, ans)
            else:                apply(page, jobs, ans, others)
        finally:
            context.close()
            browser.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
