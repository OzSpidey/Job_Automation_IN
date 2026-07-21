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
import sys
import time
from datetime import datetime, timezone

HERE           = os.path.dirname(__file__)
QUEUE_FILE     = os.path.join(HERE, "json", "autoapply_queue.json")
APPLIED_FILE   = os.path.join(HERE, "json", "autoapply_applied.json")
SCREENSHOT_DIR = os.path.join(HERE, "screenshots")
RECON_DIR      = os.path.join(HERE, "recon")

SESSION_FILE  = os.environ.get("GOOGLE_SESSION_FILE", os.path.join(HERE, "google_session.json"))
ENABLE_SUBMIT = os.environ.get("AUTOAPPLY_ENABLE_SUBMIT", "") == "1"
RECON_LIMIT   = int(os.environ.get("RECON_LIMIT", "3"))
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
    if "self" in u or "identification" in u:           return "selfid"
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
    combo = page.get_by_role("combobox")
    if combo.count() == 0:
        print("  location: no combobox found (maybe single fixed location)")
        return
    try:
        combo.first.click()
        page.wait_for_timeout(700)
        opts = page.get_by_role("option")
        if opts.count() > 0:
            label = (opts.first.inner_text(timeout=800) or "").strip()[:45]
            opts.first.click()
            print(f"  location: selected first option '{label}'")
        else:
            page.keyboard.press("Escape")
            print("  location: no options appeared (leaving as-is)")
    except Exception as exc:
        print(f"  location: error {exc}")


def answer_radio_group(page, name_re, choice: str) -> bool:
    grp = page.get_by_role("radiogroup", name=name_re)
    try:
        if grp.count() > 0:
            grp.first.get_by_role("radio", name=re.compile(rf"^\s*{choice}\s*$", re.I)).first.click(timeout=4000)
            return True
    except Exception as exc:
        print(f"    [radiogroup click failed: {exc}]")
    # Fallback: a radio whose accessible name embeds the question text + choice
    try:
        r = page.get_by_role("radio", name=name_re)
        if r.count() > 0:
            r.first.click(timeout=3000)
            return True
    except Exception:
        pass
    return False


def fill_role_info(page, ans: dict) -> None:
    select_first_location(page)
    elig  = _yn(ans.get("work_eligible"), "Yes")
    spons = _yn(ans.get("needs_sponsorship"), "No")
    ok1 = answer_radio_group(page, re.compile("legally eligible", re.I), elig)
    ok2 = answer_radio_group(page, re.compile("sponsor", re.I), spons)
    print(f"  eligible={elig} ({'ok' if ok1 else 'NOT FOUND'}) | "
          f"sponsorship={spons} ({'ok' if ok2 else 'NOT FOUND'})")


def handle_self_id(page) -> None:
    """Voluntary — decline where possible, otherwise just proceed."""
    for label in ("I don't wish to answer", "Decline to self-identify",
                  "I do not wish to answer", "Prefer not to say"):
        try:
            el = page.get_by_text(re.compile(label, re.I))
            if el.count() > 0:
                el.first.click(timeout=1500)
                print(f"  self-id: chose '{label}'")
                return
        except Exception:
            continue
    print("  self-id: left blank (voluntary)")


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
    remaining = []
    for job in jobs:
        jid = job["job_id"]
        print(f"\n[apply] {job.get('title','')}  {jid[:20]}…")
        submitted = False
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            if looks_logged_out(page):
                print("  [abort] session challenged — leaving queued.")
                remaining.append(job); continue

            for n in range(MAX_STEPS):
                step = step_of(page.url)
                print(f"  step {n}: {step}")
                if step == "review":
                    if ENABLE_SUBMIT:
                        btn = page.get_by_role("button",
                              name=re.compile(r"submit application|^submit$", re.I))
                        if btn.count() > 0:
                            btn.first.click()
                            page.wait_for_timeout(4000)
                            submitted = True
                            print("  [submit] submitted.")
                        else:
                            print("  [submit] submit button not found.")
                    else:
                        print("  [dry-run] at review; AUTOAPPLY_ENABLE_SUBMIT!=1, not submitting.")
                    break
                if step == "done":
                    submitted = True; break
                do_step(page, step, ans)
                page.wait_for_timeout(600)
                if not click_next(page):
                    print("  no advance button — stopping.")
                    break
                page.wait_for_timeout(3500)
        except Exception as exc:
            print(f"  [error] {exc}")

        if submitted:
            job["applied_at"] = datetime.now(timezone.utc).isoformat()
            job["status"] = "applied"
            applied.append(job)
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
