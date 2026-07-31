"""
Meta Careers Auto-Apply — India
===============================
Sibling of google_autoapply.py / apple_autoapply.py, for metacareers.com.
Drives Meta's application flow with Playwright over the roles the watcher
queued from the Meta India scraper's alert email.

Reads ONLY source=="meta" rows from the shared json/autoapply_queue.json and
preserves the other sources' rows on save, so google/apple/naukri/meta share
one queue without stepping on each other.

TWO WAYS META DIFFERS FROM THE OTHER APPLIERS
  1. The session is OPTIONAL. Google, Naukri and Instahyre all require a
     replayed logged-in session; Meta's application form has historically been
     open to anonymous applicants (name + email + résumé, no account). So
     META_SESSION_B64 is used if present and simply skipped if not — recon
     reports whether a login wall actually appears, and that decides whether a
     session capture is needed at all.
  2. A résumé FILE is likely required. The other India appliers all lean on a
     résumé already saved in the platform profile (Google careers profile,
     Naukri profile, Instahyre profile) and upload nothing. Meta has no such
     profile to lean on, and this repo is PUBLIC so no résumé can be committed:
     supply it as META_RESUME_B64 (base64 of the PDF) and it's written to a
     runtime temp file. recon prints the file-input count so we know whether
     it's needed before wiring the secret.

Modes:
  --recon   Open each queued job, screenshot + dump the DOM, try to reach the
            application form, and report: login wall? résumé upload? which
            fields? No submissions. RUN THIS FIRST to map the flow.
  --walk    (mapping) Take ONE job as far through the form as we can, dumping
            every page, and STOP before submit.
  --apply   Real run: fill + submit — but fill_application() is deliberately a
            NotImplementedError stub until recon reveals Meta's real form, and
            submit additionally requires META_ENABLE_SUBMIT=1. So --apply
            cannot blind-submit a half-filled form.

Env:
  META_SESSION_B64 / META_SESSION_FILE   optional captured session
  META_RESUME_B64 / META_RESUME_FILE     résumé PDF for the upload field
  META_ANSWERS_JSON      screening answers (shape finalised after recon)
  META_ENABLE_SUBMIT     "1" to actually submit (default off)
  APPLY_LIMIT            max submissions per run (0 = whole meta queue)
  RECON_LIMIT            max jobs for --recon (default 3)
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

SOURCE   = "meta"
IST_ZONE = ZoneInfo("Asia/Kolkata")

HERE           = os.path.dirname(__file__)
QUEUE_FILE     = os.path.join(HERE, "json", "autoapply_queue.json")
APPLIED_FILE   = os.path.join(HERE, "json", "autoapply_applied.json")
SCREENSHOT_DIR = os.path.join(HERE, "screenshots")
RECON_DIR      = os.path.join(HERE, "recon")

SESSION_FILE  = os.environ.get("META_SESSION_FILE", os.path.join(HERE, "meta_session.json"))
RESUME_FILE   = os.environ.get("META_RESUME_FILE",  os.path.join(HERE, "meta_resume.pdf"))
ENABLE_SUBMIT = os.environ.get("META_ENABLE_SUBMIT", "") == "1"
RECON_LIMIT   = int(os.environ.get("RECON_LIMIT", "3"))
APPLY_LIMIT   = int(os.environ.get("APPLY_LIMIT", "0"))  # 0 = whole meta queue (per run)
MAX_STEPS     = 8  # safety cap on how many pages we'll click through

# Buttons that start / advance a Meta application (refined at recon).
APPLY_BTN_RE = re.compile(r"^\s*(apply to job|apply now|apply|submit application"
                          r"|submit|continue|next|save and continue)\s*$", re.I)
# The final submit — kept separate from "advance" so the walk can stop short.
SUBMIT_BTN_RE = re.compile(r"^\s*(submit application|submit)\s*$", re.I)

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


def maybe_session_file() -> str | None:
    """The captured session if we have one — Meta apply may not need one."""
    if os.path.exists(SESSION_FILE):
        return SESSION_FILE
    b64 = os.environ.get("META_SESSION_B64", "")
    if not b64:
        print("[session] none supplied — running anonymous "
              "(Meta's form has historically not required an account).")
        return None
    with open(SESSION_FILE, "wb") as f:
        f.write(base64.b64decode(b64))
    print("[session] replaying META_SESSION_B64.")
    return SESSION_FILE


def maybe_resume_file() -> str | None:
    """The résumé PDF for Meta's upload field, if one was supplied."""
    if os.path.exists(RESUME_FILE):
        return RESUME_FILE
    b64 = os.environ.get("META_RESUME_B64", "")
    if not b64:
        print("[resume] none supplied — set META_RESUME_B64 if recon shows an "
              "upload field is required.")
        return None
    with open(RESUME_FILE, "wb") as f:
        f.write(base64.b64decode(b64))
    print(f"[resume] wrote {os.path.getsize(RESUME_FILE)}B from META_RESUME_B64.")
    return RESUME_FILE


def answers() -> dict:
    return json.loads(os.environ.get("META_ANSWERS_JSON", "{}") or "{}")


def _fmt(iso: str) -> str:
    """UTC ISO timestamp → IST, for the summary email."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ist = dt.astimezone(IST_ZONE)
    return f"{ist.strftime('%b %d, %Y')} {ist.strftime('%I:%M %p').lstrip('0')} IST"


def send_run_summary_email(jobs: list[dict]) -> None:
    """One email per run listing what was submitted (same 4-column format as
    the Naukri / Instahyre appliers). Only sends when something was applied."""
    recipient = os.environ.get("APPLY_NOTIFY_EMAIL", "").strip()
    sender    = os.environ.get("EMAIL_SENDER", "")
    password  = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not jobs:
        return
    if not (recipient and sender and password):
        print("  [notify] APPLY_NOTIFY_EMAIL / EMAIL_SENDER / GMAIL_APP_PASSWORD not set — skipping summary.")
        return
    n = len(jobs)
    subject = f"Meta Auto-Apply — {n} role(s) applied"
    jobs = sorted(jobs, key=lambda j: j.get("applied_at", ""), reverse=True)  # newest first
    rows = []
    for j in jobs:
        rows.append(
            f'<tr>'
            f'<td style="padding:8px;border:1px solid #ddd;">'
            f'<a href="{j.get("url","")}" style="color:#0866ff;text-decoration:none;font-weight:600">'
            f'{j.get("title","(role)")}</a></td>'
            f'<td style="padding:8px;border:1px solid #ddd;">Meta</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{j.get("location","") or ""}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;white-space:nowrap;">{_fmt(j.get("applied_at",""))}</td>'
            f'</tr>'
        )
    html = f"""<html><body style="font-family:Arial,sans-serif;color:#333">
      <h2 style="color:#188038">&#10003; Meta Auto-Apply — {n} role(s) applied</h2>
      <p>Submitted this run (role name links to the Meta posting):</p>
      <table style="border-collapse:collapse;width:100%;max-width:1000px">
        <tr style="background:#0866ff;color:#fff">
          <th style="padding:10px;border:1px solid #1877f2;text-align:left;width:38%">Role</th>
          <th style="padding:10px;border:1px solid #1877f2;text-align:left;width:22%">Company</th>
          <th style="padding:10px;border:1px solid #1877f2;text-align:left;width:20%">Location</th>
          <th style="padding:10px;border:1px solid #1877f2;text-align:left;width:20%">Applied</th>
        </tr>
        {chr(10).join(rows)}
      </table>
      <p style="font-size:12px;color:#888;margin-top:20px">Auto-applied via Job_Automation_IN &middot;
      {datetime.now(timezone.utc).strftime('%b %d, %Y %H:%M UTC')}</p>
    </body></html>"""
    plain = f"Meta Auto-Apply — {n} role(s) applied:\n\n" + "\n".join(
        f"- {j.get('title','(role)')} @ Meta | {j.get('location','') or '?'}"
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
# PAGE HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def hit_login_wall(page) -> bool:
    """True if Meta is demanding a Facebook/Meta login instead of showing the form."""
    url = (page.url or "").lower()
    if any(s in url for s in ("facebook.com/login", "/login/", "accounts.google.com",
                              "facebook.com/checkpoint")):
        return True
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    return any(m in body for m in
               ("log in to facebook", "log into facebook", "create new account",
                "log in to continue", "sign in to continue"))


def count_file_inputs(page) -> int:
    """How many résumé-style upload controls the current page exposes."""
    try:
        return page.locator("input[type=file]").count()
    except Exception:
        return 0


def dump_controls(page, tag: str) -> None:
    print(f"\n--- controls [{tag}]  url={page.url}")
    print(f"  file inputs: {count_file_inputs(page)}")
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
                        or el.get_attribute("placeholder")
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


def _click_matching(page, pattern: re.Pattern) -> str | None:
    """Click the first enabled button (then link) whose label matches."""
    for role in ("button", "link"):
        try:
            loc = page.get_by_role(role)
            n = loc.count()
        except Exception:
            continue
        for i in range(min(n, 30)):
            el = loc.nth(i)
            try:
                label = (el.inner_text(timeout=300) or el.get_attribute("aria-label") or "").strip()
            except Exception:
                continue
            if not (label and pattern.match(label)):
                continue
            try:
                if role == "button" and not el.is_enabled():
                    continue
                el.click()
                return label
            except Exception:
                continue
    return None


def click_apply_or_next(page) -> str | None:
    return _click_matching(page, APPLY_BTN_RE)


def dismiss_cookie_banner(page) -> None:
    for label in ("Allow all cookies", "Accept all", "Allow all", "Accept"):
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            if btn.count():
                btn.first.click(timeout=3_000)
                print(f"  [browser] dismissed cookie banner ({label!r})")
                return
        except Exception:
            continue


# ──────────────────────────────────────────────────────────────────────────────
# STEP FILLING (STUB — mapped after recon)
# ──────────────────────────────────────────────────────────────────────────────

def fill_application(page, ans: dict, resume: str | None) -> None:
    """Fill Meta's application form fields for the current step.

    Intentionally NOT implemented until CI recon reveals Meta's real form DOM
    (field labels / roles / step structure / whether the résumé upload is
    required). Raising here guarantees --apply can never blind-submit a
    half-filled Meta form before we've mapped it.
    """
    raise NotImplementedError(
        "Meta form not mapped yet — run --recon, inspect the artifacts, then "
        "implement fill_application()."
    )


# ──────────────────────────────────────────────────────────────────────────────
# MODES
# ──────────────────────────────────────────────────────────────────────────────

def recon(page, jobs: list[dict]) -> None:
    """Answer the three questions that decide how Meta apply gets built:
    does a login wall appear, is a résumé upload required, what are the fields."""
    walled = False
    resume_needed = False
    reached_form = 0
    for i, job in enumerate(jobs[:RECON_LIMIT]):
        jid = job["job_id"]
        print(f"\n[recon {i+1}] {jid} {job.get('title','')}")
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
        except Exception as exc:
            print(f"  [warn] nav: {exc}")
        dismiss_cookie_banner(page)
        snap(page, f"meta_details_{jid}")
        dump_controls(page, f"details_{jid}")
        print(f"  details url: {page.url}")

        clicked = click_apply_or_next(page)
        print(f"  apply CTA: clicked {clicked!r}")
        if not clicked:
            print("  [note] no apply CTA found on the posting page")
            continue

        page.wait_for_timeout(4500)
        snap(page, f"meta_form_{jid}")
        dump_controls(page, f"form_{jid}")
        reached_form += 1
        if hit_login_wall(page):
            walled = True
            print(f"  form url: {page.url}\n  LOGIN WALL — a captured session will be required")
        else:
            print(f"  form url: {page.url}\n  no login wall — anonymous apply looks possible")
        files = count_file_inputs(page)
        if files:
            resume_needed = True
        print(f"  résumé upload fields on the form: {files}")

    print("\nRECON RESULT")
    print(f"  application form reached for {reached_form}/{min(len(jobs), RECON_LIMIT)} job(s)")
    print(f"  login wall: {'YES — capture META_SESSION_B64' if walled else 'no'}")
    print(f"  résumé upload: {'YES — set META_RESUME_B64' if resume_needed else 'not seen'}")
    print("  next: read the dumped controls above, then implement fill_application().")


def walk(page, jobs: list[dict], ans: dict, resume: str | None) -> None:
    job = jobs[0]
    print(f"\n[walk] {job.get('title','')}  {job['job_id']}")
    page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)
    dismiss_cookie_banner(page)
    for n in range(MAX_STEPS):
        snap(page, f"meta_walk{n}")
        dump_controls(page, f"walk{n}")
        if hit_login_wall(page):
            print("  login wall — stopping walk.")
            break
        try:
            fill_application(page, ans, resume)
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


def apply(page, jobs: list[dict], ans: dict, resume: str | None, others: list[dict]) -> None:
    applied = _load(APPLIED_FILE)
    submitted_now: list[dict] = []
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
            dismiss_cookie_banner(page)
            if hit_login_wall(page):
                print("  [abort] login wall — leaving queued.")
                remaining.append(job); continue

            for n in range(MAX_STEPS):
                snap(page, f"meta_apply{n}_{jid}")
                if hit_login_wall(page):
                    print("  [abort] login wall mid-flow — leaving queued.")
                    break
                try:
                    fill_application(page, ans, resume)
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
            submitted_now.append(job)
        else:
            remaining.append(job)

    _save(QUEUE_FILE, others + remaining)   # keep other-source rows intact
    _save(APPLIED_FILE, applied)
    send_run_summary_email(submitted_now)
    print(f"\nSubmitted this run: {len(submitted_now)} | applied ledger: {len(applied)} rows | "
          f"still queued: {len(others + remaining)} (meta={len(remaining)}, other={len(others)})")


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    mode = "recon"
    if "--walk" in sys.argv:  mode = "walk"
    if "--apply" in sys.argv: mode = "apply"

    print("=" * 60)
    print(f"Meta Auto-Apply — India · mode={mode} · submit={'ON' if ENABLE_SUBMIT else 'off'}")
    print("=" * 60)

    all_jobs = _load(QUEUE_FILE)
    jobs   = [j for j in all_jobs if j.get("source") == SOURCE]
    others = [j for j in all_jobs if j.get("source") != SOURCE]
    print(f"Queue depth (meta): {len(jobs)}  |  other-source rows kept intact: {len(others)}")
    if not jobs:
        print("Nothing queued for Meta. Run the scraper + watcher first.")
        return

    session = maybe_session_file()
    resume  = maybe_resume_file()
    ans     = answers()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx_kwargs = dict(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 1000},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
        )
        if session:
            ctx_kwargs["storage_state"] = session
        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()
        try:
            if mode == "recon":  recon(page, jobs)
            elif mode == "walk": walk(page, jobs, ans, resume)
            else:                apply(page, jobs, ans, resume, others)
        finally:
            context.close()
            browser.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
