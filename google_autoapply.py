"""
Google Careers Auto-Apply — India
==================================
Drives Google's own logged-in careers UI with Playwright. Google is NOT a
standard ATS, so applying means replaying a captured Google session
(GOOGLE_SESSION_B64 secret) and operating the real application.

Because the Google Careers PROFILE is already created (name/phone/résumé saved
server-side), each application skips the profile step and lands on:
    Role Information  ->  Voluntary self-identification  ->  Review & apply

Modes:
  --recon   Open each queued apply URL, screenshot + dump DOM, report if the
            session is still authenticated. No fills, no advancing.
  --walk    (mapping) Take ONE queued job through the steps: fill Role Info,
            advance, screenshot + dump the controls on every page, and STOP at
            Review (never submits). Used to see Self-ID / Review before arming.
  --apply   Real run over the whole queue: fill each step and submit — but only
            click the final Submit when AUTOAPPLY_ENABLE_SUBMIT=1 (else dry-run).

Env:
  GOOGLE_SESSION_B64 / GOOGLE_SESSION_FILE   the captured session
  AUTOAPPLY_ANSWERS_JSON   {"work_eligible","needs_sponsorship","preferred_location"}
  AUTOAPPLY_ENABLE_SUBMIT  "1" to actually submit (default off)
  RECON_LIMIT              max jobs for --recon (default 3)
"""

import base64
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

HERE           = os.path.dirname(__file__)
QUEUE_FILE     = os.path.join(HERE, "json", "autoapply_queue.json")
APPLIED_FILE   = os.path.join(HERE, "json", "autoapply_applied.json")
SCREENSHOT_DIR = os.path.join(HERE, "screenshots")
RECON_DIR      = os.path.join(HERE, "recon")

SESSION_FILE  = os.environ.get("GOOGLE_SESSION_FILE", os.path.join(HERE, "google_session.json"))
ENABLE_SUBMIT = os.environ.get("AUTOAPPLY_ENABLE_SUBMIT", "") == "1"
RECON_LIMIT   = int(os.environ.get("RECON_LIMIT", "3"))
APPLY_LIMIT   = int(os.environ.get("APPLY_LIMIT", "0"))  # 0 = whole queue (per run)
MONTHLY_CAP   = int(os.environ.get("AUTOAPPLY_MONTHLY_CAP", "3"))  # Google's ~3/month
MAX_STEPS     = 6  # safety cap on how many pages we'll click through

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
    b64 = os.environ.get("GOOGLE_SESSION_B64", "")
    if not b64:
        print("[error] No session: set GOOGLE_SESSION_B64 or GOOGLE_SESSION_FILE.")
        sys.exit(1)
    with open(SESSION_FILE, "wb") as f:
        f.write(base64.b64decode(b64))
    return SESSION_FILE


def answers() -> dict:
    return json.loads(os.environ.get("AUTOAPPLY_ANSWERS_JSON", "{}") or "{}")


def send_confirmation_email(job: dict) -> None:
    """Notify the user that an application was fully submitted."""
    recipient = os.environ.get("APPLY_NOTIFY_EMAIL", "").strip()
    sender    = os.environ.get("EMAIL_SENDER", "")
    password  = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not (recipient and sender and password):
        print("  [notify] APPLY_NOTIFY_EMAIL / EMAIL_SENDER / GMAIL_APP_PASSWORD not set — skipping email.")
        return
    role    = job.get("title") or "a role"
    company = "Google"
    url     = job.get("url", "")
    subject = f"Application submitted: {role} at {company}"
    html = f"""<html><body style="font-family:Arial,sans-serif;color:#333">
      <h2 style="color:#188038">&#10003; Application submitted</h2>
      <p>Your application has been <strong>completely submitted</strong> to:</p>
      <p style="font-size:16px"><strong>{role}</strong> at <strong>{company}</strong></p>
      <p><a href="{url}">{url}</a></p>
      <p style="font-size:12px;color:#888">Auto-applied via Job_Automation_IN &middot;
      {datetime.now(timezone.utc).strftime('%b %d, %Y %H:%M UTC')}</p>
    </body></html>"""
    plain = f"Application completely submitted to {role} at {company}\n{url}"
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


def _apps_this_month(applied: list[dict]) -> int:
    """How many applications were submitted in the current (UTC) calendar month."""
    now = datetime.now(timezone.utc)
    n = 0
    for j in applied:
        try:
            d = datetime.fromisoformat(j.get("applied_at", ""))
        except ValueError:
            continue
        if (d.year, d.month) == (now.year, now.month):
            n += 1
    return n


# ──────────────────────────────────────────────────────────────────────────────
# PAGE HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def looks_logged_out(page) -> bool:
    url = (page.url or "").lower()
    if "accounts.google.com" in url or "signin/v2" in url or "/challenge" in url:
        return True
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    return any(m in body for m in
               ("verify it's you", "couldn't sign you in", "use your google account"))


def step_of(url: str) -> str:
    u = (url or "").lower()
    if u.endswith("/form") or "/form" in u:            return "profile"
    if "/role" in u:                                   return "role"
    if "/vsi" in u or "self" in u or "identification" in u: return "selfid"
    if "/review" in u:                                 return "review"
    if "confirm" in u or "success" in u or "thank" in u: return "done"
    return "unknown"


def dump_controls(page, tag: str) -> None:
    print(f"\n--- controls [{tag}]  url={page.url}")
    for role in ("combobox", "radiogroup", "radio", "checkbox", "textbox", "button", "link"):
        try:
            loc = page.get_by_role(role)
            n = loc.count()
        except Exception:
            n = 0
        if not n:
            continue
        names = []
        for i in range(min(n, 14)):
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
    try:
        page.screenshot(path=os.path.join(SCREENSHOT_DIR, f"{name}.png"), full_page=True)
    except Exception as exc:
        print(f"  [warn] screenshot {name}: {exc}")
    try:
        with open(os.path.join(RECON_DIR, f"{name}.html"), "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception as exc:
        print(f"  [warn] dom {name}: {exc}")


# ──────────────────────────────────────────────────────────────────────────────
# STEP FILLERS
# ──────────────────────────────────────────────────────────────────────────────

def _yn(val: str, default: str) -> str:
    v = (val or default).strip().lower()
    return "Yes" if v.startswith("y") else "No"


def select_first_location(page) -> None:
    """Pick a preferred location only if none is pre-filled (don't clobber it)."""
    combo = page.get_by_role("combobox")
    if combo.count() == 0:
        print("  location: no combobox (fixed single location)")
        return
    try:
        current = combo.first.inner_text(timeout=800) or ""
    except Exception:
        current = ""
    cur = re.sub(r"(?i)preferred location\*?", "", current).strip(" *\n\t")
    if cur:
        print(f"  location: already set to '{cur[:40]}' — leaving it")
        return
    try:
        combo.first.click()
        page.wait_for_timeout(700)
        opts = page.get_by_role("option")
        for i in range(min(opts.count(), 12)):
            label = (opts.nth(i).inner_text(timeout=500) or "").strip()
            if label:
                opts.nth(i).click()
                print(f"  location: selected '{label[:40]}'")
                return
        page.keyboard.press("Escape")
        print("  location: no non-empty option found")
    except Exception as exc:
        print(f"  location: error {exc}")


def _click_group_radio(group_loc, index: int, label: str) -> bool:
    """Click option at `index` (Yes=0, No=1, Not sure=2) in a radiogroup. The
    radios expose no accessible name, so we select positionally by DOM order."""
    try:
        group_loc.get_by_role("radio").nth(index).click(timeout=3000)
        print(f"    {label}: option #{index} clicked")
        return True
    except Exception as exc:
        print(f"    {label}: FAILED ({exc})")
        return False


def fill_role_info(page, ans: dict) -> None:
    select_first_location(page)

    # Minimum qualifications — user policy: answer "Yes" (option 0) to every one.
    mq = page.get_by_role("radiogroup", name=re.compile("minimum qualif", re.I))
    nmq = mq.count()
    done = sum(_click_group_radio(mq.nth(i), 0, f"min-qual[{i}]=Yes") for i in range(nmq))
    print(f"  min-quals: {done}/{nmq} answered Yes")

    # Work authorization (Yes=0, No=1)
    elig  = _yn(ans.get("work_eligible"), "Yes")
    spons = _yn(ans.get("needs_sponsorship"), "No")
    eg = page.get_by_role("radiogroup", name=re.compile("legally eligible", re.I))
    if eg.count():
        _click_group_radio(eg.first, 0 if elig == "Yes" else 1, f"eligible={elig}")
    sg = page.get_by_role("radiogroup", name=re.compile("sponsor", re.I))
    if sg.count():
        _click_group_radio(sg.first, 0 if spons == "Yes" else 1, f"sponsorship={spons}")


def handle_self_id(page) -> None:
    """Voluntary self-ID (/vsi): decline every question, then tick the required
    consent box so Next enables. Controls carry no accessible names, so select
    positionally. In each radio question 'I choose not to disclose' is the LAST
    option; among the checkboxes the race 'I choose not to disclose' is the
    second-to-last and the consent box is the last."""
    groups = page.get_by_role("radiogroup")
    ng = groups.count()
    for i in range(ng):
        try:
            radios = groups.nth(i).get_by_role("radio")
            radios.nth(radios.count() - 1).click(timeout=2500)  # last = decline
        except Exception as exc:
            print(f"    vsi radiogroup[{i}]: {exc}")
    print(f"  self-id: declined {ng} radio question(s)")

    cbs = page.get_by_role("checkbox")
    ncb = cbs.count()
    print(f"  self-id: {ncb} checkbox(es) present")
    if ncb >= 2:
        try:
            cbs.nth(ncb - 2).click(timeout=2500)   # race: 'I choose not to disclose'
        except Exception as exc:
            print(f"    vsi race-cb: {exc}")
    if ncb >= 1:
        try:
            cbs.nth(ncb - 1).click(timeout=2500)   # consent (required)
            print("  self-id: consent ticked")
        except Exception as exc:
            print(f"    vsi consent-cb: {exc}")


def click_next(page) -> str | None:
    for name in ("Next", "Continue", "Save and continue", "Review", "Review & apply"):
        try:
            btn = page.get_by_role("button", name=re.compile(rf"^\s*{re.escape(name)}\s*$", re.I))
            if btn.count() > 0 and btn.first.is_enabled():
                btn.first.click()
                return name
        except Exception:
            continue
    return None


def do_step(page, step: str, ans: dict) -> None:
    if step == "role":
        fill_role_info(page, ans)
    elif step == "selfid":
        handle_self_id(page)
    # profile/review handled by caller


# ──────────────────────────────────────────────────────────────────────────────
# MODES
# ──────────────────────────────────────────────────────────────────────────────

def recon(page, jobs: list[dict]) -> None:
    authed = True
    for i, job in enumerate(jobs[:RECON_LIMIT]):
        jid = job["job_id"]
        print(f"\n[recon {i+1}] {jid[:20]}… {job.get('title','')}")
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
        except Exception as exc:
            print(f"  [warn] nav: {exc}")
        snap(page, jid)
        out = looks_logged_out(page)
        authed = authed and not out
        print(f"  final url: {page.url}\n  session: {'LOGGED OUT' if out else 'authenticated'} | step={step_of(page.url)}")
    print("\nRECON RESULT:", "session survives." if authed else "session did NOT hold.")


def walk(page, jobs: list[dict], ans: dict) -> None:
    """Map the flow on ONE job: fill + advance, screenshot every page, never submit."""
    job = jobs[0]
    print(f"\n[walk] {job.get('title','')}  {job['job_id'][:20]}…")
    page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)

    for n in range(MAX_STEPS):
        step = step_of(page.url)
        print(f"\n[walk step {n}] detected='{step}'")
        dump_controls(page, f"walk{n}_{step}")
        snap(page, f"walk{n}_{step}")

        if step == "review":
            print("  reached REVIEW — stopping before submit (walk never submits).")
            break
        if step == "done":
            print("  reached a confirmation page — stopping.")
            break

        do_step(page, step, ans)
        page.wait_for_timeout(600)
        clicked = click_next(page)
        print(f"  advance: clicked {clicked!r}")
        if not clicked:
            print("  no advance button found — stopping walk here.")
            break
        page.wait_for_timeout(3500)


def apply(page, jobs: list[dict], ans: dict) -> None:
    applied = _load(APPLIED_FILE)
    used = _apps_this_month(applied)
    print(f"This month: {used}/{MONTHLY_CAP} application(s) already used.")
    remaining = []
    attempts_budget = APPLY_LIMIT or len(jobs)
    attempted = 0
    for job in jobs:
        if attempted >= attempts_budget:            # per-run attempt cap
            remaining.append(job); continue
        if ENABLE_SUBMIT and used >= MONTHLY_CAP:    # hard monthly cap (Google 3/mo)
            print(f"  monthly cap {MONTHLY_CAP} reached — leaving job queued for next month.")
            remaining.append(job); continue
        attempted += 1
        jid = job["job_id"]
        print(f"\n[apply {attempted}] {job.get('title','')}  {jid[:20]}…")
        submitted = False
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            if looks_logged_out(page):
                print("  [abort] session challenged — leaving queued.")
                remaining.append(job); continue

            for n in range(MAX_STEPS):
                step = step_of(page.url)
                print(f"  step {n}: {step} ({(page.url or '').rsplit('/', 1)[-1]})")
                if step == "review":
                    # Final privacy-consent checkbox — 'Apply' stays disabled without it.
                    cbs = page.get_by_role("checkbox")
                    if cbs.count() > 0:
                        try:
                            cbs.last.click(timeout=2500)
                            print("  review: consent ticked")
                        except Exception as exc:
                            print(f"  review consent: {exc}")
                    page.wait_for_timeout(600)
                    snap(page, f"review_{jid[:16]}")   # evidence of what we submit
                    if ENABLE_SUBMIT:
                        btn = page.get_by_role("button", name=re.compile(r"^\s*Apply\s*$", re.I))
                        if btn.count() > 0 and btn.first.is_enabled():
                            btn.first.click()
                            page.wait_for_timeout(6000)
                            submitted = True
                            snap(page, f"applied_{jid[:16]}")   # confirmation page
                            print("  [submit] 'Apply' clicked — application submitted.")
                        else:
                            print("  [submit] 'Apply' button missing/disabled — not submitted.")
                    else:
                        print("  [dry-run] at review; AUTOAPPLY_ENABLE_SUBMIT!=1 — not submitting.")
                    break
                if step == "done":
                    submitted = True; break
                do_step(page, step, ans)
                page.wait_for_timeout(600)
                if not click_next(page):
                    print("  no advance button — stopping this job.")
                    break
                page.wait_for_timeout(3500)
        except Exception as exc:
            print(f"  [error] {exc}")

        if submitted:
            job["applied_at"] = datetime.now(timezone.utc).isoformat()
            job["status"] = "applied"
            applied.append(job)
            used += 1
            send_confirmation_email(job)
        else:
            remaining.append(job)

    _save(QUEUE_FILE, remaining)
    _save(APPLIED_FILE, applied)
    print(f"\nApplied: {len(applied)} total | still queued: {len(remaining)}")


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    mode = "recon"
    if "--walk" in sys.argv:  mode = "walk"
    if "--apply" in sys.argv: mode = "apply"

    print("=" * 60)
    print(f"Google Auto-Apply — India · mode={mode} · submit={'ON' if ENABLE_SUBMIT else 'off'}")
    print("=" * 60)

    jobs = _load(QUEUE_FILE)
    print(f"Queue depth: {len(jobs)}")
    if not jobs:
        print("Nothing queued. Run the watcher first.")
        return

    session = ensure_session_file()
    ans = answers()
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
            if mode == "recon":  recon(page, jobs)
            elif mode == "walk": walk(page, jobs, ans)
            else:                apply(page, jobs, ans)
        finally:
            context.close()
            browser.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
