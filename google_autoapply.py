"""
Google Careers Auto-Apply — India
==================================
Drives Google's own logged-in careers UI with Playwright. Google is NOT a
standard ATS (no form endpoint), so applying means replaying a captured Google
session and operating the real application form.

Session: we cannot log in fresh from CI (2FA / reCAPTCHA / device checks), so a
browser session captured once from a real login is imported via the
GOOGLE_SESSION_B64 secret (base64 of a Playwright storageState JSON) and replayed.

Modes:
  --recon   (default) For each queued job: open the apply page, detect whether
            the session is still authenticated, screenshot it, and dump the DOM
            to recon/. Never fills or submits. This is how we learn Google's
            real form and confirm the cloud session survives.
  --apply   Fill the form from the answers profile and (only if submit is
            explicitly enabled) submit. Field-fill is wired up AFTER recon shows
            us the real form — see fill_application().

Env:
  GOOGLE_SESSION_FILE        path to decoded storageState JSON (set by workflow)
  AUTOAPPLY_ANSWERS_JSON     JSON string: answer profile (name, phone, work auth…)
  RESUME_PATH                path to the resume PDF (fetched from private repo)
  AUTOAPPLY_ENABLE_SUBMIT    "1" to actually click Submit (default off -> dry run)
  RECON_LIMIT                max jobs to recon per run (default 3)

Run: python google_autoapply.py --recon
"""

import base64
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE         = os.path.dirname(__file__)
QUEUE_FILE   = os.path.join(HERE, "json", "autoapply_queue.json")
APPLIED_FILE = os.path.join(HERE, "json", "autoapply_applied.json")
SCREENSHOT_DIR = os.path.join(HERE, "screenshots")
RECON_DIR      = os.path.join(HERE, "recon")

SESSION_FILE = os.environ.get("GOOGLE_SESSION_FILE", os.path.join(HERE, "google_session.json"))
ENABLE_SUBMIT = os.environ.get("AUTOAPPLY_ENABLE_SUBMIT", "") == "1"
RECON_LIMIT   = int(os.environ.get("RECON_LIMIT", "3"))

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
    """Materialise the Playwright storageState from GOOGLE_SESSION_B64 if present."""
    if os.path.exists(SESSION_FILE):
        return SESSION_FILE
    b64 = os.environ.get("GOOGLE_SESSION_B64", "")
    if not b64:
        print("[error] No session: set GOOGLE_SESSION_B64 or provide GOOGLE_SESSION_FILE.")
        sys.exit(1)
    with open(SESSION_FILE, "wb") as f:
        f.write(base64.b64decode(b64))
    return SESSION_FILE


def looks_logged_out(page) -> bool:
    """Heuristic: did Google bounce us to a sign-in / verification screen?"""
    url = (page.url or "").lower()
    if "accounts.google.com" in url or "signin/v2" in url or "/challenge" in url:
        return True
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    markers = ["sign in", "verify it's you", "couldn't sign you in", "use your google account"]
    return any(m in body for m in markers)


# ──────────────────────────────────────────────────────────────────────────────
# RECON
# ──────────────────────────────────────────────────────────────────────────────

def recon(page, jobs: list[dict]) -> None:
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    os.makedirs(RECON_DIR, exist_ok=True)
    authed = None
    for i, job in enumerate(jobs[:RECON_LIMIT]):
        jid = job["job_id"]
        print(f"\n[recon {i+1}/{min(len(jobs), RECON_LIMIT)}] job {jid}: {job.get('title','')}")
        print(f"  -> {job['url']}")
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)  # let SPA settle
        except Exception as exc:
            print(f"  [warn] navigation error: {exc}")
        out_png  = os.path.join(SCREENSHOT_DIR, f"{jid}.png")
        out_html = os.path.join(RECON_DIR, f"{jid}.html")
        try:
            page.screenshot(path=out_png, full_page=True)
        except Exception as exc:
            print(f"  [warn] screenshot failed: {exc}")
        try:
            with open(out_html, "w", encoding="utf-8") as f:
                f.write(page.content())
        except Exception as exc:
            print(f"  [warn] dom dump failed: {exc}")
        logged_out = looks_logged_out(page)
        authed = (authed is not False) and (not logged_out)
        print(f"  final url : {page.url}")
        print(f"  session   : {'LOGGED OUT / challenged' if logged_out else 'looks authenticated'}")
        print(f"  saved     : {os.path.relpath(out_png, HERE)}, {os.path.relpath(out_html, HERE)}")

    print("\n" + "=" * 60)
    if authed:
        print("RECON RESULT: session appears to survive from this runner. "
              "Next: wire fill_application() to the captured form.")
    else:
        print("RECON RESULT: session did NOT hold (Google challenged the login). "
              "Cloud auto-apply likely needs a residential/self-hosted runner.")
    print("=" * 60)


# ──────────────────────────────────────────────────────────────────────────────
# APPLY  (wired up after recon reveals the real form)
# ──────────────────────────────────────────────────────────────────────────────

def fill_application(page, job: dict, answers: dict, resume_path: str) -> bool:
    """Fill Google's application form for one job.

    Intentionally not implemented until recon shows the real DOM. Once we have
    the field selectors, this fills from `answers`, uploads `resume_path`, and
    returns True when the form is complete and ready to submit.
    """
    raise NotImplementedError(
        "fill_application() is wired after the recon run reveals Google's form. "
        "Run --recon first."
    )


def apply(page, jobs: list[dict]) -> None:
    answers = json.loads(os.environ.get("AUTOAPPLY_ANSWERS_JSON", "{}") or "{}")
    resume_path = os.environ.get("RESUME_PATH", "")
    if not answers:
        print("[error] AUTOAPPLY_ANSWERS_JSON is empty — nothing to fill with.")
        sys.exit(1)
    if not (resume_path and os.path.exists(resume_path)):
        print(f"[error] RESUME_PATH not found: {resume_path!r}")
        sys.exit(1)

    queue, applied = jobs, _load(APPLIED_FILE)
    still_queued = []
    for job in queue:
        jid = job["job_id"]
        print(f"\n[apply] job {jid}: {job.get('title','')}")
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            if looks_logged_out(page):
                print("  [abort] session challenged — leaving job queued.")
                still_queued.append(job)
                continue
            ready = fill_application(page, job, answers, resume_path)
            if ready and ENABLE_SUBMIT:
                # SUBMIT step goes here once fills are verified.
                print("  [submit] AUTOAPPLY_ENABLE_SUBMIT=1 — submitting…")
                # page.click("<submit selector>")
                job["applied_at"] = datetime.now(timezone.utc).isoformat()
                job["status"] = "applied"
                applied.append(job)
            else:
                print("  [dry-run] filled, submit disabled — leaving job queued.")
                still_queued.append(job)
        except NotImplementedError as exc:
            print(f"  [skip] {exc}")
            still_queued.append(job)
        except Exception as exc:
            print(f"  [error] {exc} — leaving job queued.")
            still_queued.append(job)

    _save(QUEUE_FILE, still_queued)
    _save(APPLIED_FILE, applied)


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    mode = "apply" if "--apply" in sys.argv else "recon"
    print("=" * 60)
    print(f"Google Careers Auto-Apply — India · mode={mode} · submit={'ON' if ENABLE_SUBMIT else 'off'}")
    print("=" * 60)

    jobs = _load(QUEUE_FILE)
    print(f"Queue depth: {len(jobs)}")
    if not jobs:
        print("Nothing queued. Run the watcher first.")
        return

    session = ensure_session_file()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            storage_state=session,
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            viewport={"width": 1440, "height": 1000},
        )
        page = context.new_page()
        try:
            if mode == "recon":
                recon(page, jobs)
            else:
                apply(page, jobs)
        finally:
            context.close()
            browser.close()

    print("Done.")


if __name__ == "__main__":
    main()
