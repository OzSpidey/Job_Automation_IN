"""
Weekly Auto-Apply Report — India engine
=======================================
Runs every Saturday. Consolidates the past 7 days of auto-applications across
ALL platforms into one email:
  - Google / Naukri / Apple  -> json/autoapply_applied.json  (has "source")
  - Instahyre                -> json/instahyre_applied.json
grouped by platform with a table (Role -> linked, Company, Location, Applied).
Plus a best-effort "Responses this week" section: emails that landed in the
auto-apply inbox from someone OTHER than our own scraper/confirmation sender
(i.e. likely recruiter/platform replies).

Emails to WEEKLY_REPORT_EMAIL.
Run: python weekly_report.py
"""

import email
import email.header
import imaplib
import json
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_IST = ZoneInfo("Asia/Kolkata")
HERE = os.path.dirname(__file__)
AUTOAPPLY_APPLIED = os.path.join(HERE, "json", "autoapply_applied.json")
INSTAHYRE_APPLIED = os.path.join(HERE, "json", "instahyre_applied.json")

WINDOW_DAYS = 7

REPORT_TO   = os.environ.get("WEEKLY_REPORT_EMAIL", "") or os.environ.get("EMAIL_TO_INDIA", "")
SENDER      = os.environ.get("EMAIL_SENDER", "")
PASSWORD    = os.environ.get("GMAIL_APP_PASSWORD", "")
INBOX_USER  = os.environ.get("AUTOAPPLY_GMAIL_USER", "")
INBOX_PASS  = os.environ.get("AUTOAPPLY_GMAIL_APP_PASSWORD", "")

PLATFORM_LABEL = {"google": "Google", "naukri": "Naukri", "apple": "Apple", "instahyre": "Instahyre"}
PLATFORM_ORDER = ["google", "naukri", "apple", "instahyre"]


# ──────────────────────────────────────────────────────────────────────────────
# LOAD + FILTER
# ──────────────────────────────────────────────────────────────────────────────

def _load(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def _parse(iso: str) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except ValueError:
        return None


def _fmt(dt: datetime | None) -> str:
    if not dt:
        return ""
    ist = dt.astimezone(_IST)
    return f"{ist.strftime('%b %d')}, {ist.strftime('%I:%M %p').lstrip('0')} IST"


def collect(cutoff: datetime) -> dict[str, list[dict]]:
    """All applications with applied_at within the window, grouped by platform."""
    rows: list[dict] = []
    for j in _load(AUTOAPPLY_APPLIED):
        j = dict(j); j["source"] = j.get("source", "google")
        rows.append(j)
    for j in _load(INSTAHYRE_APPLIED):
        j = dict(j); j["source"] = "instahyre"
        rows.append(j)

    grouped: dict[str, list[dict]] = {}
    for j in rows:
        dt = _parse(j.get("applied_at", ""))
        if not dt or dt < cutoff:
            continue
        j["_dt"] = dt
        grouped.setdefault(j["source"], []).append(j)
    for src in grouped:
        grouped[src].sort(key=lambda x: x["_dt"], reverse=True)
    return grouped


# ──────────────────────────────────────────────────────────────────────────────
# RESPONSES (best-effort: non-self emails in the auto-apply inbox this week)
# ──────────────────────────────────────────────────────────────────────────────

def fetch_responses(cutoff: datetime) -> list[dict]:
    if not (INBOX_USER and INBOX_PASS):
        return []
    out: list[dict] = []
    try:
        box = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        box.login(INBOX_USER, INBOX_PASS)
        box.select("INBOX")
        since = cutoff.strftime("%d-%b-%Y")
        typ, data = box.search(None, "SINCE", since)
        ids = data[0].split() if data and data[0] else []
        self_sender = (SENDER or "").lower()
        for num in ids[-80:]:
            typ, md = box.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ != "OK" or not md or not md[0]:
                continue
            msg = email.message_from_bytes(md[0][1])
            frm = str(email.header.make_header(email.header.decode_header(msg.get("From", ""))))
            if self_sender and self_sender in frm.lower():
                continue  # our own scraper/confirmation emails — not a response
            subj = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
            out.append({"from": frm, "subject": subj, "date": msg.get("Date", "")})
        box.logout()
    except Exception as exc:
        print(f"[responses] inbox scan failed (non-fatal): {exc}")
    return out


# ──────────────────────────────────────────────────────────────────────────────
# EMAIL
# ──────────────────────────────────────────────────────────────────────────────

def _rows_html(jobs: list[dict]) -> str:
    out = []
    for j in jobs:
        title = j.get("title") or "(role)"
        url = j.get("url") or ""
        role = (f'<a href="{url}" style="color:#0a66c2;text-decoration:none;font-weight:600">{title}</a>'
                if url else title)
        out.append(
            f'<tr>'
            f'<td style="padding:7px;border:1px solid #ddd;">{role}</td>'
            f'<td style="padding:7px;border:1px solid #ddd;">{j.get("company","") or ""}</td>'
            f'<td style="padding:7px;border:1px solid #ddd;">{j.get("location","") or ""}</td>'
            f'<td style="padding:7px;border:1px solid #ddd;white-space:nowrap;">{_fmt(j.get("_dt"))}</td>'
            f'</tr>'
        )
    return "\n".join(out)


def build_html(grouped: dict, responses: list[dict], start: datetime, end: datetime) -> tuple[str, str, int]:
    total = sum(len(v) for v in grouped.values())
    rng = f"{start.astimezone(_IST).strftime('%b %d')} – {end.astimezone(_IST).strftime('%b %d, %Y')}"
    counts = " · ".join(f"{PLATFORM_LABEL.get(s, s)}: {len(grouped[s])}"
                        for s in PLATFORM_ORDER if grouped.get(s)) or "no applications"

    sections = []
    for s in PLATFORM_ORDER:
        jobs = grouped.get(s)
        if not jobs:
            continue
        sections.append(f"""
        <h3 style="margin:22px 0 6px;color:#202124">{PLATFORM_LABEL.get(s, s)} — {len(jobs)} application(s)</h3>
        <table style="border-collapse:collapse;width:100%;max-width:1000px;font-size:14px">
          <tr style="background:#4a4a4a;color:#fff">
            <th style="padding:8px;border:1px solid #555;text-align:left;width:40%">Role</th>
            <th style="padding:8px;border:1px solid #555;text-align:left;width:22%">Company</th>
            <th style="padding:8px;border:1px solid #555;text-align:left;width:20%">Location</th>
            <th style="padding:8px;border:1px solid #555;text-align:left;width:18%">Applied</th>
          </tr>
          {_rows_html(jobs)}
        </table>""")

    if responses:
        rrows = "\n".join(
            f'<tr><td style="padding:7px;border:1px solid #ddd;">{r["from"][:60]}</td>'
            f'<td style="padding:7px;border:1px solid #ddd;">{r["subject"][:80]}</td>'
            f'<td style="padding:7px;border:1px solid #ddd;white-space:nowrap;">{r["date"][:16]}</td></tr>'
            for r in responses[:40])
        resp_html = f"""
        <h3 style="margin:26px 0 6px;color:#188038">Responses this week — {len(responses)} (best-effort, auto-apply inbox)</h3>
        <table style="border-collapse:collapse;width:100%;max-width:1000px;font-size:13px">
          <tr style="background:#188038;color:#fff">
            <th style="padding:8px;border:1px solid #555;text-align:left;width:32%">From</th>
            <th style="padding:8px;border:1px solid #555;text-align:left;width:50%">Subject</th>
            <th style="padding:8px;border:1px solid #555;text-align:left;width:18%">When</th>
          </tr>{rrows}
        </table>"""
    else:
        resp_html = ('<h3 style="margin:26px 0 6px;color:#188038">Responses this week</h3>'
                     '<p style="color:#666">None detected in the auto-apply inbox.</p>')

    body = sections and "\n".join(sections) or '<p style="color:#666">No applications submitted this week.</p>'
    html = f"""<html><body style="font-family:Arial,sans-serif;color:#333">
      <h2 style="color:#202124">Weekly Auto-Apply Report</h2>
      <p style="color:#555">{rng} &nbsp;·&nbsp; <strong>{total}</strong> application(s) &nbsp;—&nbsp; {counts}</p>
      {body}
      {resp_html}
      <p style="font-size:12px;color:#888;margin-top:24px">Job_Automation_IN &middot; auto-generated {datetime.now(timezone.utc).strftime('%b %d, %Y %H:%M UTC')}</p>
    </body></html>"""

    plain_lines = [f"Weekly Auto-Apply Report ({rng}) — {total} application(s): {counts}", ""]
    for s in PLATFORM_ORDER:
        for j in grouped.get(s, []):
            plain_lines.append(f"[{PLATFORM_LABEL.get(s,s)}] {j.get('title','(role)')} @ {j.get('company','') or '?'} "
                               f"| {j.get('location','') or '?'} | {_fmt(j.get('_dt'))}\n  {j.get('url','')}")
    plain_lines.append(f"\nResponses this week (best-effort): {len(responses)}")
    return html, "\n".join(plain_lines), total


def send_email(html: str, plain: str, total: int) -> None:
    if not (REPORT_TO and SENDER and PASSWORD):
        print("[error] WEEKLY_REPORT_EMAIL / EMAIL_SENDER / GMAIL_APP_PASSWORD not set."); return
    recipients = [a.strip() for a in REPORT_TO.split(",") if a.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Weekly Auto-Apply Report — {total} application(s)"
    msg["From"] = SENDER
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
        srv.login(SENDER, PASSWORD)
        srv.sendmail(SENDER, recipients, msg.as_string())
    print(f"[email] Weekly report sent to {', '.join(recipients)} — {total} application(s).")


def main() -> None:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=WINDOW_DAYS)
    print(f"Weekly report window: {start.date()} .. {end.date()}")
    grouped = collect(start)
    for s in PLATFORM_ORDER:
        print(f"  {PLATFORM_LABEL.get(s,s)}: {len(grouped.get(s, []))}")
    responses = fetch_responses(start)
    print(f"  responses (best-effort): {len(responses)}")
    html, plain, total = build_html(grouped, responses, start, end)
    send_email(html, plain, total)
    print("Done.")


if __name__ == "__main__":
    main()
