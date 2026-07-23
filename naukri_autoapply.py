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


def send_confirmation_email(job: dict) -> None:
    recipient = os.environ.get("APPLY_NOTIFY_EMAIL", "").strip()
    sender    = os.environ.get("EMAIL_SENDER", "")
    password  = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not (recipient and sender and password):
        print("  [notify] APPLY_NOTIFY_EMAIL / EMAIL_SENDER / GMAIL_APP_PASSWORD not set — skipping email.")
        return
    role = job.get("title") or "a role"
    url  = job.get("url", "")
    subject = f"Application submitted: {role} (Naukri)"
    html = f"""<html><body style="font-family:Arial,sans-serif;color:#333">
      <h2 style="color:#188038">&#10003; Application submitted</h2>
      <p>Your application has been <strong>submitted</strong> via Naukri to:</p>
      <p style="font-size:16px"><strong>{role}</strong></p>
      <p><a href="{url}">{url}</a></p>
      <p style="font-size:12px;color:#888">Auto-applied via Job_Automation_IN &middot;
      {datetime.now(timezone.utc).strftime('%b %d, %Y %H:%M UTC')}</p>
    </body></html>"""
    plain = f"Application submitted via Naukri to {role}\n{url}"
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
        print(f"  [notify] confirmation emailed to {', '.join(recipients)}")
    except Exception as exc:
        print(f"  [notify] email failed: {exc}")


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
        kind, el = cta
        print(f"  apply CTA: {kind}")
        if kind == "company":
            print("  -> company-site redirect; would SKIP in apply mode.")
            continue
        try:
            el.click(timeout=4000)
            page.wait_for_timeout(4000)
            snap(page, f"naukri_afterapply_{jid}")
            dump_controls(page, f"afterapply_{jid}")
            print(f"  after Apply url: {page.url}")
        except Exception as exc:
            print(f"  [warn] apply click: {exc}")
    print("\nRECON RESULT:", "session survives." if authed else "session did NOT hold (recapture).")


def walk(page, jobs: list[dict], ans: dict) -> None:
    job = jobs[0]
    print(f"\n[walk] {job.get('title','')}  {job['job_id']}")
    page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)
    for n in range(MAX_STEPS):
        snap(page, f"naukri_walk{n}")
        dump_controls(page, f"walk{n}")
        if looks_logged_out(page):
            print("  session challenged — stopping walk."); break
        cta = find_apply(page)
        if cta and cta[0] == "onsite":
            cta[1].click(timeout=4000)
            page.wait_for_timeout(3500)
            continue
        try:
            fill_chatbot(page, ans)
        except NotImplementedError as exc:
            print(f"  fill_chatbot stub: {exc}"); break


def apply(page, jobs: list[dict], ans: dict, others: list[dict]) -> None:
    applied = _load(APPLIED_FILE)
    remaining: list[dict] = []
    attempts_budget = APPLY_LIMIT or len(jobs)
    attempted = 0
    for job in jobs:
        if attempted >= attempts_budget:
            remaining.append(job); continue
        attempted += 1
        jid = job["job_id"]
        print(f"\n[apply {attempted}] {job.get('title','')}  {jid}")
        submitted = False
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            if looks_logged_out(page):
                print("  [abort] session challenged — leaving queued.")
                remaining.append(job); continue
            cta = find_apply(page)
            if not cta:
                print("  no apply CTA — leaving queued."); remaining.append(job); continue
            if cta[0] == "company":
                print("  company-site redirect — SKIP (can't auto-submit off-platform).")
                remaining.append(job); continue
            snap(page, f"naukri_review_{jid}")
            if not ENABLE_SUBMIT:
                print("  [dry-run] NAUKRI_ENABLE_SUBMIT!=1 — not clicking Apply.")
                remaining.append(job); continue
            cta[1].click(timeout=4000)
            page.wait_for_timeout(4000)
            try:
                fill_chatbot(page, ans)      # NotImplemented until mapped
            except NotImplementedError as exc:
                print(f"  [skip] {exc}")
                remaining.append(job); continue
            snap(page, f"naukri_applied_{jid}")
            submitted = True
        except Exception as exc:
            print(f"  [error] {exc}")

        if submitted:
            job["applied_at"] = datetime.now(timezone.utc).isoformat()
            job["status"] = "applied"
            applied.append(job)
            send_confirmation_email(job)
        else:
            remaining.append(job)

    _save(QUEUE_FILE, others + remaining)   # keep other-source rows intact
    _save(APPLIED_FILE, applied)
    print(f"\nApplied: {len(applied)} total | still queued: {len(others + remaining)} "
          f"(naukri={len(remaining)}, other={len(others)})")


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
