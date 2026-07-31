"""
Meta Careers Auto-Apply — India
===============================
Sibling of google_autoapply.py / apple_autoapply.py, for metacareers.com.
Drives Meta's application flow with Playwright over the roles the watcher
queued from the Meta India scraper's alert email.

Reads ONLY source=="meta" rows from the shared json/autoapply_queue.json and
preserves the other sources' rows on save, so google/apple/naukri/meta share
one queue without stepping on each other.

THE FORM (mapped by CI recon of an India posting, run 30594032167)
  /jobs/<id>/ → "Apply now" → /profile/create_application/<id>/, which is a
  SINGLE page ending in one "Submit" — there are no steps to walk. It hydrates
  client-side well after domcontentloaded (see wait_for_form).

  Its contents depend on whether we're logged in, which is why nothing here is
  required up front:
    • logged in (the intended path) — the Meta Career Profile supplies the
      résumé and contact details, so most fields are absent or pre-populated and
      only a few questions remain, exactly like the Google applier.
    • anonymous — Meta asks for everything: résumé upload, name, email, phone,
      location, self-ID, plus creating a Career Profile account on submit.
  Recon confirmed the anonymous path has no login wall, so both work; the
  logged-in one is preferred because it answers far less.

  Meta's ARIA role engine reports ZERO controls on these pages (get_by_role and
  get_by_label both see nothing) while CSS sees all of them, and its CTAs are
  <div role="button"> rather than real buttons. Everything here therefore goes
  through CSS attribute selectors — see the taggers above fill_application.

Modes:
  --recon   Open each queued job, screenshot + dump the DOM, reach the
            application form, and report: login wall? résumé upload? which
            fields? No submissions.
  --walk    Fill ONE job's form completely, dump it, and STOP before Submit.
            This is how a fill is validated before arming.
  --apply   Fill + submit. Submit only fires when META_ENABLE_SUBMIT=1; a
            disarmed run fills the form, says so, and leaves the job queued.
            A fill that can't answer a field the form asks for raises
            MissingAnswer and requeues rather than submitting partial data.

Env:
  META_SESSION_B64 / META_SESSION_FILE   captured Career Profile session (see
                         capture_meta_session.py). Optional but recommended: it
                         is what keeps the form short.
  META_RESUME_B64 / META_RESUME_FILE     résumé PDF, only needed when the form
                         asks for an upload and the profile hasn't attached one
  META_ANSWERS_JSON      answers for whatever the form still asks, any of:
                         first_name, last_name, email, phone, phone_code,
                         website, current_location, account_password
  META_ENABLE_SUBMIT     "1" to actually submit (default off)
  APPLY_LIMIT            max submissions per run (0 = whole meta queue)
  RECON_LIMIT            max jobs for --recon (default 3)
  RECON_JOB              job id or full URL to recon/walk INSTEAD of the queue.
                         Meta's India software queue is empty most of the time
                         (Meta staffs ~16 India roles, rarely any SWE), so the
                         form can't be mapped by waiting for a queued job —
                         point this at any live posting to map the DOM. recon
                         and walk never submit, so this applies to nothing.
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


FORM_WAIT_S = 30


def wait_for_form(page, timeout_s: int = FORM_WAIT_S) -> bool:
    """Poll until the application form's fields exist.

    /profile/create_application/<id>/ is client-rendered and hydrates well after
    domcontentloaded — a fixed sleep caught 14 inputs on one recon and 0 on the
    next against the same posting. Anything that reads or fills the form has to
    wait on the fields themselves.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if page.locator("input,textarea,select").count() > 0:
                print(f"  [form] fields rendered after "
                      f"{timeout_s - int(deadline - time.time())}s")
                return True
        except Exception:
            pass
        page.wait_for_timeout(1_000)
    print(f"  [form] no fields after {timeout_s}s")
    return False


def count_file_inputs(page) -> int:
    """How many résumé-style upload controls the current page exposes."""
    try:
        return page.locator("input[type=file]").count()
    except Exception:
        return 0


def dump_controls(page, tag: str) -> None:
    print(f"\n--- controls [{tag}]  url={page.url}")
    print(f"  file inputs: {count_file_inputs(page)}")
    # Meta builds its controls as <div role="button"> inside deeply-nested
    # obfuscated-class markup. Count them by CSS too: it doesn't depend on the
    # accessibility tree, so a divergence between these two numbers tells us the
    # role engine (and therefore get_by_role clicking) can't see the page.
    for css in ('[role="button"]', '[role="link"]', "button", "a", "input", "textarea"):
        try:
            print(f"  css {css}: {page.locator(css).count()}")
        except Exception as exc:
            print(f"  css {css}: ERROR {type(exc).__name__}: {exc}")
    for role in ("combobox", "radiogroup", "radio", "checkbox", "textbox",
                 "button", "link", "listbox"):
        try:
            loc = page.get_by_role(role)
            n = loc.count()
        except Exception as exc:
            # Never swallow this: a silent 0 here is what made the first Meta
            # recon look like an empty page when the DOM was fully rendered.
            print(f"  {role}: ERROR {type(exc).__name__}: {exc}")
            continue
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


# Meta's markup is obfuscated-class soup and its role engine reports nothing
# (see dump_controls), so field identity has to come from the elements' own
# attributes. One JS pass beats dozens of locator round-trips.
_FIELDS_JS = """() => Array.from(document.querySelectorAll(
    'input,textarea,select,[role=radio],[role=checkbox],[role=combobox],[role=button]'
)).map((el, i) => ({
    i,
    tag:   el.tagName.toLowerCase(),
    type:  el.getAttribute('type'),
    role:  el.getAttribute('role'),
    name:  el.getAttribute('name'),
    id:    el.id || null,
    ph:    el.getAttribute('placeholder'),
    aria:  el.getAttribute('aria-label'),
    req:   el.required || el.getAttribute('aria-required') || null,
    label: (el.labels && el.labels[0] && el.labels[0].innerText) || null,
    text:  (el.innerText || '').trim().slice(0, 40) || null,
}))"""


def dump_fields(page, tag: str) -> None:
    """Every form control with the attributes that identify it."""
    try:
        fields = page.evaluate(_FIELDS_JS)
    except Exception as exc:
        print(f"  [fields] extraction failed: {type(exc).__name__}: {exc}")
        return
    print(f"  [fields] {len(fields)} control(s) on {tag}:")
    for f in fields:
        bits = [f"{k}={v!r}" for k, v in f.items() if k != "i" and v not in (None, "", False)]
        print(f"    #{f['i']:>2} {' '.join(bits)}")


def dump_visible_text(page, tag: str, limit: int = 3500) -> None:
    """The rendered text — this is what names Meta's questions in order."""
    try:
        text = page.inner_text("body")
    except Exception as exc:
        print(f"  [text] unavailable: {type(exc).__name__}: {exc}")
        return
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    joined = " | ".join(lines)[:limit]
    print(f"  [text] {tag}: {joined}")


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
    """Click the first enabled control whose label matches.

    Meta renders its CTAs as <div role="button"> wrapping a <span> of text, not
    as real <button>/<a> elements, so this deliberately tries CSS attribute
    selectors alongside the ARIA-role engine rather than trusting either alone.
    """
    candidates: list[tuple[str, object]] = []
    for role in ("button", "link"):
        try:
            candidates.append((role, page.get_by_role(role)))
        except Exception:
            pass
    for css in ('[role="button"]', '[role="link"]', "button", "a"):
        try:
            candidates.append((css, page.locator(css)))
        except Exception:
            pass

    for how, loc in candidates:
        try:
            n = loc.count()
        except Exception:
            continue
        for i in range(min(n, 40)):
            el = loc.nth(i)
            try:
                label = (el.inner_text(timeout=300) or el.get_attribute("aria-label") or "").strip()
            except Exception:
                continue
            if not (label and pattern.match(label)):
                continue
            try:
                el.click(timeout=5_000)
                print(f"  [click] matched {label!r} via {how}")
                return label
            except Exception as exc:
                print(f"  [click] {label!r} via {how} failed: {type(exc).__name__}")
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

# Mapped from CI recon of create_application/<id>/ (run 30594032167). Meta's
# form is a SINGLE page — résumé upload, contact fields, two self-ID sections,
# an account-creation block, and one "Submit" — so there are no steps to walk.
#
# Both taggers stamp a data attribute in one JS pass and everything afterwards
# acts through plain CSS attribute selectors. That's deliberate: Meta's ARIA
# role engine reports zero controls (get_by_role/get_by_label see nothing) while
# CSS sees all of them, so label-based Playwright locators cannot be used here.
_TAG_INPUTS_JS = """() => {
  const out = [];
  document.querySelectorAll('input,textarea,select').forEach((el, i) => {
    const key = 'f' + i;
    el.setAttribute('data-oz', key);
    let label = '';
    if (el.labels && el.labels[0]) {
      label = (el.labels[0].innerText || '').trim();
      el.labels[0].setAttribute('data-oz-label', key);
    }
    out.push({key, label,
              type: (el.getAttribute('type') || el.tagName.toLowerCase()),
              val: (el.value || '')});
  });
  return out;
}"""

_TAG_ROLES_JS = """() => {
  const out = [];
  document.querySelectorAll('[role=radio],[role=button],[role=combobox]').forEach((el, i) => {
    const key = 'r' + i;
    el.setAttribute('data-ozr', key);
    out.push({key,
              role: el.getAttribute('role'),
              text: (el.innerText || '').trim().slice(0, 60),
              aria: el.getAttribute('aria-label') || ''});
  });
  return out;
}"""

class MissingAnswer(RuntimeError):
    """A field the form is asking for has no answer — fill aborts rather than
    submit something incomplete."""


def _find_input(fields: list[dict], *patterns: str, type_: str | None = None) -> dict | None:
    for f in fields:
        if type_ and f["type"] != type_:
            continue
        for p in patterns:
            if re.search(p, f["label"], re.I):
                return f
    return None


def _find_role(roles: list[dict], role: str, *patterns: str) -> dict | None:
    for r in roles:
        if r["role"] != role:
            continue
        blob = f"{r['text']} {r['aria']}"
        for p in patterns:
            if re.search(p, blob, re.I):
                return r
    return None


def _fill_text(page, field: dict | None, value: str, what: str) -> None:
    if not field or not value:
        print(f"  [fill] {what}: skipped (field={bool(field)}, value={bool(value)})")
        return
    try:
        page.locator(f'[data-oz="{field["key"]}"]').fill(value, timeout=8_000)
        print(f"  [fill] {what}: ok")
    except Exception as exc:
        print(f"  [fill] {what}: FAILED {type(exc).__name__}: {exc}")


def _click_radio(page, page_key: str, what: str, label_key: str | None = None) -> None:
    """Radios are visually-replaced inputs, so click the styled label when there
    is one and fall back to a forced click on the input itself."""
    for sel in ([f'[data-oz-label="{label_key}"]'] if label_key else []) + [f'[data-oz="{page_key}"]']:
        try:
            page.locator(sel).click(timeout=5_000)
            print(f"  [fill] {what}: selected")
            return
        except Exception:
            continue
    try:
        page.locator(f'[data-oz="{page_key}"]').click(timeout=5_000, force=True)
        print(f"  [fill] {what}: selected (forced)")
    except Exception as exc:
        print(f"  [fill] {what}: FAILED {type(exc).__name__}")


def _pick_combobox(page, roles: list[dict], aria: str, value: str, what: str) -> None:
    """Open one of Meta's <button role=combobox> pickers and choose a value.

    These are typeaheads whose option list is rendered on open, so the option is
    matched by its visible text after typing. Best-effort by design: a failure
    here is logged and left to the walk to refine rather than guessed at.
    """
    if not value:
        print(f"  [fill] {what}: no value supplied — leaving Meta's default")
        return
    combo = _find_role(roles, "combobox", re.escape(aria))
    if not combo:
        print(f"  [fill] {what}: combobox {aria!r} not found")
        return
    try:
        page.locator(f'[data-ozr="{combo["key"]}"]').click(timeout=8_000)
        page.wait_for_timeout(1_200)
        # The opened picker exposes a text box; type, then take the first option.
        typed = False
        for sel in ('[role=dialog] input', '[role=listbox] input', 'input[type=text]:focus', 'input:focus'):
            try:
                box = page.locator(sel).first
                if box.count():
                    box.fill(value, timeout=4_000)
                    typed = True
                    break
            except Exception:
                continue
        page.wait_for_timeout(1_500)
        for sel in (f'[role=option]:has-text("{value}")', f'[role=listbox] :text("{value}")',
                    f'li:has-text("{value}")', f'div[role=button]:has-text("{value}")'):
            try:
                opt = page.locator(sel).first
                if opt.count():
                    opt.click(timeout=4_000)
                    print(f"  [fill] {what}: picked {value!r} (typed={typed})")
                    return
            except Exception:
                continue
        print(f"  [fill] {what}: opened but no option matched {value!r} (typed={typed})")
    except Exception as exc:
        print(f"  [fill] {what}: FAILED {type(exc).__name__}: {exc}")


def _resume_already_attached(page) -> bool:
    """Whether the form already shows an attached résumé (from the profile)."""
    try:
        body = page.inner_text("body") or ""
    except Exception:
        return False
    return bool(re.search(r"\.pdf\b|\.docx\b|replace resume|remove resume", body, re.I))


def fill_application(page, ans: dict, resume: str | None) -> None:
    """Fill Meta's application form. Never clicks Submit — the caller decides
    that, so a disarmed run can fill and stop.

    Written to suit BOTH shapes of the form, because they differ a lot:
      • logged in  — the Career Profile supplies the résumé and contact details,
        so most fields are absent or already populated and only a few questions
        remain (same situation as the Google applier).
      • anonymous  — Meta asks for everything, including creating an account.
    So nothing is required up front. Each field is handled only if the form
    actually renders it AND it isn't already filled, and a field that is present,
    empty and unanswerable aborts the fill instead of submitting a partial form.
    """
    fields = page.evaluate(_TAG_INPUTS_JS)
    roles  = page.evaluate(_TAG_ROLES_JS)
    print(f"  [fill] tagged {len(fields)} input(s), {len(roles)} role element(s)")

    def needs(field: dict | None) -> bool:
        """Present on the form and still empty (i.e. the profile didn't fill it)."""
        return bool(field) and not (field.get("val") or "").strip()

    # 1) Résumé. Setting the file input directly avoids the OS file chooser the
    #    "Upload resume" button opens. When logged in the profile résumé is
    #    normally already attached, so an upload field alone isn't a blocker.
    file_field = _find_input(fields, r".*", type_="file")
    if file_field and resume:
        try:
            page.locator(f'[data-oz="{file_field["key"]}"]').set_input_files(resume, timeout=15_000)
            print(f"  [fill] résumé: uploaded {os.path.basename(resume)}")
            page.wait_for_timeout(4_000)   # Meta parses the file and may re-render
            fields = page.evaluate(_TAG_INPUTS_JS)   # re-tag: parse rebuilds the form
            roles  = page.evaluate(_TAG_ROLES_JS)
        except Exception as exc:
            raise MissingAnswer(f"résumé upload failed: {type(exc).__name__}: {exc}")
    elif file_field and not _resume_already_attached(page):
        raise MissingAnswer(
            "the form asks for a résumé, none is attached from the profile, and "
            "META_RESUME_B64 isn't set (.pdf/.docx, max 2MB)"
        )
    elif file_field:
        print("  [fill] résumé: already attached from the Career Profile — leaving it")

    # 2) Contact fields — only the ones this form renders empty.
    for label_re, key, what in (
        (r"^first name",   "first_name", "first name"),
        (r"^last name",    "last_name",  "last name"),
        (r"^email",        "email",      "email"),
        (r"^phone number", "phone",      "phone"),
        (r"^website",      "website",    "website"),
    ):
        field = _find_input(fields, label_re)
        if not field:
            continue
        if not needs(field):
            print(f"  [fill] {what}: already filled by the profile — leaving it")
            continue
        value = str(ans.get(key, "") or "")
        if not value:
            if key == "website":       # genuinely optional on Meta's form
                print(f"  [fill] {what}: empty and no answer — optional, skipping")
                continue
            raise MissingAnswer(
                f"the form asks for {what} and it is empty, but META_ANSWERS_JSON "
                f"has no {key!r}"
            )
        _fill_text(page, field, value, what)

    # 3) Pickers. Only touched when an answer is supplied explicitly — Meta's
    #    phone code defaults to +1, but when logged in the profile's own value is
    #    already right and must not be clobbered.
    _pick_combobox(page, roles, "Code", str(ans.get("phone_code", "")), "phone code")
    _pick_combobox(page, roles, "Current location", ans.get("current_location", ""), "current location")

    # 4) Self-ID: decline both, matching the India policy already used for
    #    Google's voluntary self-ID step. Gender is never inferred.
    gender_decline = _find_input(fields, r"choose not to disclose", type_="radio")
    if gender_decline:
        _click_radio(page, gender_decline["key"], "gender (decline)", gender_decline["key"])
    disability_decline = _find_input(fields, r"do not want to answer", type_="radio")
    if disability_decline:
        _click_radio(page, disability_decline["key"], "disability (decline)", disability_decline["key"])

    # 5) Career Profile account. Meta creates one on submit regardless, so pick
    #    password mode and set it — one-time-code mode would need inbox access
    #    mid-submit, which this run can't do.
    password = ans.get("account_password", "")
    if password:
        pw_mode = _find_role(roles, "radio", r"use a password")
        if pw_mode:
            try:
                page.locator(f'[data-ozr="{pw_mode["key"]}"]').click(timeout=5_000)
                print("  [fill] account mode: use a password")
                page.wait_for_timeout(1_000)
                fields = page.evaluate(_TAG_INPUTS_JS)
            except Exception as exc:
                print(f"  [fill] account mode: FAILED {type(exc).__name__}")
        _fill_text(page, _find_input(fields, r"^password", type_="password"), password, "password")
        _fill_text(page, _find_input(fields, r"^confirm password", type_="password"),
                   password, "confirm password")
    else:
        print("  [fill] account: no account_password supplied — leaving Meta's default mode")


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

        wait_for_form(page)
        snap(page, f"meta_form_{jid}")
        dump_controls(page, f"form_{jid}")
        dump_fields(page, f"form_{jid}")
        dump_visible_text(page, f"form_{jid}")
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


def open_form(page, job: dict) -> bool:
    """Posting page → click "Apply now" → the hydrated create_application form."""
    page.goto(job["url"], wait_until="domcontentloaded", timeout=45_000)
    page.wait_for_timeout(4_000)
    dismiss_cookie_banner(page)
    if "create_application" not in (page.url or ""):
        clicked = click_apply_or_next(page)
        if not clicked:
            print("  [abort] no apply CTA on the posting page")
            return False
        page.wait_for_timeout(2_500)
    if already_applied(page):
        return False
    return wait_for_form(page)


def already_applied(page) -> bool:
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    if any(m in body for m in ("you have already applied", "already applied to this",
                               "application already submitted")):
        print("  [note] Meta says this role was already applied to")
        return True
    return False


def submit_succeeded(page) -> bool:
    """Positive confirmation that Meta accepted the application.

    NOT yet verified against a real submission — nothing has been submitted to
    Meta from here. Both signals are checked and an unconfirmed submit requeues
    the job rather than being recorded, so the failure mode is a retry (which
    already_applied() then catches), never a silently-lost application. Confirm
    and tighten this on the first armed run.
    """
    url = (page.url or "").lower()
    if "create_application" not in url:
        print(f"  [submit] navigated away to {page.url}")
        return True
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    for marker in ("thank you for applying", "application submitted",
                   "we received your application", "application received",
                   "thanks for applying"):
        if marker in body:
            print(f"  [submit] confirmation text: {marker!r}")
            return True
    return False


def walk(page, jobs: list[dict], ans: dict, resume: str | None) -> None:
    """Fill the form for ONE job and stop before Submit."""
    job = jobs[0]
    print(f"\n[walk] {job.get('title','')}  {job['job_id']}")
    if not open_form(page, job):
        print("  form not reached — nothing to fill.")
        return
    snap(page, "meta_walk_form")
    dump_controls(page, "walk_form")
    dump_fields(page, "walk_form")
    try:
        fill_application(page, ans, resume)
    except MissingAnswer as exc:
        print(f"  [walk] cannot fill: {exc}")
        return
    snap(page, "meta_walk_filled")
    dump_fields(page, "walk_filled")
    print("  [walk] form filled — STOPPING before Submit (walk never submits).")


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
            if not open_form(page, job):
                print("  [abort] form not reached — leaving queued.")
                remaining.append(job); continue
            if hit_login_wall(page):
                print("  [abort] login wall — leaving queued.")
                remaining.append(job); continue

            snap(page, f"meta_apply_form_{jid}")
            fill_application(page, ans, resume)
            snap(page, f"meta_apply_filled_{jid}")

            # The form is one page, so "advance" IS the final submit. Gate it on
            # the arming flag rather than on the generic advance helper, which
            # matches "Submit" and would otherwise fire while disarmed.
            if not ENABLE_SUBMIT:
                print("  [dry] form filled; META_ENABLE_SUBMIT!=1 so NOT submitting "
                      "— leaving queued.")
                remaining.append(job); continue

            clicked = _click_matching(page, SUBMIT_BTN_RE)
            if not clicked:
                print("  [abort] no Submit control found — leaving queued.")
                remaining.append(job); continue
            page.wait_for_timeout(6_000)
            snap(page, f"meta_apply_after_submit_{jid}")
            submitted = submit_succeeded(page)
            print(f"  [submit] clicked {clicked!r} → "
                  f"{'confirmed' if submitted else 'NO confirmation — leaving queued'}")
        except MissingAnswer as exc:
            print(f"  [skip] {exc}")
        except Exception as exc:
            print(f"  [error] {type(exc).__name__}: {exc}")

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

    # Mapping override: recon/walk a specific posting instead of the queue, so
    # the form can be mapped while the India queue is empty. Never for --apply,
    # which must only ever touch genuinely queued+deduped roles.
    target = os.environ.get("RECON_JOB", "").strip()
    if target and mode in ("recon", "walk"):
        jid = target.rsplit("/jobs/", 1)[-1].strip("/") if "/" in target else target
        url = target if target.startswith("http") else f"https://www.metacareers.com/jobs/{jid}/"
        jobs = [{"job_id": jid, "url": url, "title": "(RECON_JOB override)", "source": SOURCE}]
        print(f"[recon-override] using RECON_JOB → {url}")
    elif not jobs:
        print("Nothing queued for Meta. Run the scraper + watcher first, "
              "or set RECON_JOB=<job id|url> to map the form now.")
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
