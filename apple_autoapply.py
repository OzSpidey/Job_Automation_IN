"""
Apple Careers Auto-Apply — India
=================================
Sibling of google_autoapply.py, for Apple (jobs.apple.com). Drives Apple's
logged-in careers UI with Playwright by replaying a captured Apple session
(APPLE_SESSION_B64 secret — see capture_apple_session.py).

Unlike Google, Apple has NO monthly application cap, so there is no monthly
guard here — only APPLY_LIMIT bounds how many we submit per run.

Reads ONLY source=="apple" rows from the shared json/autoapply_queue.json and
preserves the other sources' rows on save, so Google and Apple share one queue
without stepping on each other.

Modes:
  --recon   Open each queued job's details page, screenshot + dump DOM, try to
            reach the application form, and report whether the session is still
            authenticated. No submissions. THIS IS RUN FIRST to map the flow.
  --walk    (mapping) Take ONE job as far through the form as we can, screenshot
            + dump controls on every page, and STOP before submit.
  --apply   Real run: fill + submit — but only click the final submit when
            APPLE_ENABLE_SUBMIT=1. fill_application() is intentionally a stub
            until recon reveals Apple's form, so apply can't blind-submit.

Env:
  APPLE_SESSION_B64 / APPLE_SESSION_FILE   the captured session
  APPLE_ANSWERS_JSON       screening answers (shape TBD after recon)
  APPLE_ENABLE_SUBMIT      "1" to actually submit (default off)
  APPLY_LIMIT              max submissions per run (0 = whole apple queue)
  RECON_LIMIT              max jobs for --recon (default 3)
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

SOURCE = "apple"

HERE           = os.path.dirname(__file__)
QUEUE_FILE     = os.path.join(HERE, "json", "autoapply_queue.json")
APPLIED_FILE   = os.path.join(HERE, "json", "autoapply_applied.json")
SCREENSHOT_DIR = os.path.join(HERE, "screenshots")
RECON_DIR      = os.path.join(HERE, "recon")

SESSION_FILE  = os.environ.get("APPLE_SESSION_FILE", os.path.join(HERE, "apple_session.json"))
ENABLE_SUBMIT = os.environ.get("APPLE_ENABLE_SUBMIT", "") == "1"
RECON_LIMIT   = int(os.environ.get("RECON_LIMIT", "3"))
APPLY_LIMIT   = int(os.environ.get("APPLY_LIMIT", "0"))  # 0 = whole apple queue (per run)
MAX_STEPS     = 8  # safety cap on how many pages we'll click through

# Buttons that start / advance an Apple application (mapped/adjusted at recon).
APPLY_BTN_RE = re.compile(r"^\s*(apply|submit résumé|submit resume|submit application"
                          r"|submit|continue|next|save and continue)\s*$", re.I)

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
    b64 = os.environ.get("APPLE_SESSION_B64", "")
    if not b64:
        print("[error] No session: set APPLE_SESSION_B64 or APPLE_SESSION_FILE.")
        sys.exit(1)
    with open(SESSION_FILE, "wb") as f:
        f.write(base64.b64decode(b64))
    return SESSION_FILE


def answers() -> dict:
    return json.loads(os.environ.get("APPLE_ANSWERS_JSON", "{}") or "{}")


def send_confirmation_email(job: dict) -> None:
    recipient = os.environ.get("APPLY_NOTIFY_EMAIL", "").strip()
    sender    = os.environ.get("EMAIL_SENDER", "")
    password  = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not (recipient and sender and password):
        print("  [notify] APPLY_NOTIFY_EMAIL / EMAIL_SENDER / GMAIL_APP_PASSWORD not set — skipping email.")
        return
    role = job.get("title") or "a role"
    url  = job.get("url", "")
    subject = f"Application submitted: {role} at Apple"
    html = f"""<html><body style="font-family:-apple-system,Arial,sans-serif;color:#1d1d1f">
      <h2 style="color:#188038">&#10003; Application submitted</h2>
      <p>Your application has been <strong>completely submitted</strong> to:</p>
      <p style="font-size:16px"><strong>{role}</strong> at <strong>Apple</strong></p>
      <p><a href="{url}">{url}</a></p>
      <p style="font-size:12px;color:#888">Auto-applied via Job_Automation_IN &middot;
      {datetime.now(timezone.utc).strftime('%b %d, %Y %H:%M UTC')}</p>
    </body></html>"""
    plain = f"Application completely submitted to {role} at Apple\n{url}"
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
    if any(s in url for s in ("idmsa.apple.com", "appleid.apple.com", "/sign-in", "signin")):
        return True
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    return any(m in body for m in
               ("sign in with your apple", "manage your apple", "sign in to apple",
                "forgot apple id"))


def dump_controls(page, tag: str) -> None:
    print(f"\n--- controls [{tag}]  url={page.url}")
    for role in ("combobox", "radiogroup", "radio", "checkbox", "textbox",
                 "button", "link", "listbox"):
        try:
            loc = page.get_by_role(role)
            n = loc.count()
        except Exception:
            n = 0
        if not n:
            continue
        names = []
        for i in range(min(n, 16)):
            el = loc.nth(i)
            try:
                name = (el.get_attribute("aria-label")
                        or (el.inner_text(timeout=400) or "").strip())
            except Exception:
                name = "?"
            names.append((name or "·").replace("\n", " ")[:45])
        print(f"  {role}({n}): {names}")


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


def click_apply_or_next(page) -> str | None:
    """Click the most likely apply/advance button. Returns its label or None."""
    try:
        btns = page.get_by_role("button")
        for i in range(min(btns.count(), 30)):
            b = btns.nth(i)
            try:
                label = (b.inner_text(timeout=300) or b.get_attribute("aria-label") or "").strip()
            except Exception:
                continue
            if label and APPLY_BTN_RE.match(label) and b.is_enabled():
                b.click()
                return label
    except Exception as exc:
        print(f"  [warn] click_apply_or_next: {exc}")
    # Apple sometimes renders the apply CTA as a link, not a button.
    try:
        links = page.get_by_role("link")
        for i in range(min(links.count(), 30)):
            l = links.nth(i)
            try:
                label = (l.inner_text(timeout=300) or "").strip()
            except Exception:
                continue
            if label and APPLY_BTN_RE.match(label):
                l.click()
                return label
    except Exception:
        pass
    return None


# ──────────────────────────────────────────────────────────────────────────────
# STEP FILLING (STUB — mapped after recon)
# ──────────────────────────────────────────────────────────────────────────────

def fill_application(page, ans: dict) -> None:
    """Fill Apple's application form fields for the current step.

    Intentionally NOT implemented until CI recon reveals Apple's real form DOM
    (field labels / roles / step structure). Raising here guarantees --apply
    can never blind-submit a half-filled Apple form before we've mapped it.
    """
    raise NotImplementedError(
        "Apple form not mapped yet — run --recon, inspect the artifacts, then "
        "implement fill_application()."
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
        snap(page, f"apple_details_{jid}")
        dump_controls(page, f"details_{jid}")
        out = looks_logged_out(page)
        authed = authed and not out
        print(f"  details url: {page.url}\n  session: {'LOGGED OUT' if out else 'authenticated'}")

        # Try to reach the application form so recon captures it too.
        clicked = click_apply_or_next(page)
        print(f"  apply CTA: clicked {clicked!r}")
        if clicked:
            page.wait_for_timeout(4000)
            snap(page, f"apple_form_{jid}")
            dump_controls(page, f"form_{jid}")
            out2 = looks_logged_out(page)
            authed = authed and not out2
            print(f"  form url: {page.url}\n  session after CTA: {'LOGGED OUT' if out2 else 'authenticated'}")
    print("\nRECON RESULT:", "session survives." if authed else "session did NOT hold (capture a fresh one).")


def walk(page, jobs: list[dict], ans: dict) -> None:
    job = jobs[0]
    print(f"\n[walk] {job.get('title','')}  {job['job_id']}")
    page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)
    for n in range(MAX_STEPS):
        snap(page, f"apple_walk{n}")
        dump_controls(page, f"walk{n}")
        if looks_logged_out(page):
            print("  session challenged — stopping walk.")
            break
        try:
            fill_application(page, ans)
        except NotImplementedError as exc:
            print(f"  fill_application stub: {exc}")
            print("  (walk only screenshots/dumps until fill_application is mapped)")
            break
        clicked = click_apply_or_next(page)
        print(f"  advance: clicked {clicked!r}")
        if not clicked:
            print("  no advance control — stopping walk.")
            break
        page.wait_for_timeout(3500)


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

            for n in range(MAX_STEPS):
                snap(page, f"apple_apply{n}_{jid}")
                if looks_logged_out(page):
                    print("  [abort] session challenged mid-flow — leaving queued.")
                    break
                try:
                    fill_application(page, ans)
                except NotImplementedError as exc:
                    print(f"  [skip] {exc}")
                    break  # not mapped yet → never submits
                if not click_apply_or_next(page):
                    print("  no advance control — stopping this job.")
                    break
                page.wait_for_timeout(3500)
        except Exception as exc:
            print(f"  [error] {exc}")

        if submitted and ENABLE_SUBMIT:
            job["applied_at"] = datetime.now(timezone.utc).isoformat()
            job["status"] = "applied"
            applied.append(job)
            send_confirmation_email(job)
        else:
            remaining.append(job)

    _save(QUEUE_FILE, others + remaining)   # keep other-source rows intact
    _save(APPLIED_FILE, applied)
    print(f"\nApplied: {len(applied)} total | still queued: {len(others + remaining)} "
          f"(apple={len(remaining)}, other={len(others)})")


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    mode = "recon"
    if "--walk" in sys.argv:  mode = "walk"
    if "--apply" in sys.argv: mode = "apply"

    print("=" * 60)
    print(f"Apple Auto-Apply — India · mode={mode} · submit={'ON' if ENABLE_SUBMIT else 'off'}")
    print("=" * 60)

    all_jobs = _load(QUEUE_FILE)
    jobs   = [j for j in all_jobs if j.get("source") == SOURCE]
    others = [j for j in all_jobs if j.get("source") != SOURCE]
    print(f"Queue depth (apple): {len(jobs)}  |  other-source rows kept intact: {len(others)}")
    if not jobs:
        print("Nothing queued for Apple. Run the watcher first.")
        return

    session = ensure_session_file()
    ans = answers()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            storage_state=session,
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                        "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"),
            viewport={"width": 1440, "height": 1000},
        )
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
