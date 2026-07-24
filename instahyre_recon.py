"""
Instahyre Recon — India (map the logged-in Opportunities feed + apply flow)
===========================================================================
Instahyre is login-gated and AI-matches jobs to your profile (an "Opportunities"
feed), with no public API. This probe replays a captured session
(INSTAHYRE_SESSION_B64), opens the candidate feed, screenshots + dumps the DOM,
lists the Apply / Interested / View / Not-Interested controls, and INTERCEPTS
any XHR the page makes (to discover Instahyre's internal feed endpoint + shape).

Runs HEADFUL under Xvfb by default (safe against bot checks, like Naukri); the
workflow sets DISPLAY=:99.

Run:  python instahyre_recon.py
Env:  INSTAHYRE_SESSION_B64 / INSTAHYRE_SESSION_FILE
"""

import base64
import json
import os
import re
import sys

HERE           = os.path.dirname(__file__)
SCREENSHOT_DIR = os.path.join(HERE, "screenshots")
RECON_DIR      = os.path.join(HERE, "recon")
SESSION_FILE   = os.environ.get("INSTAHYRE_SESSION_FILE", os.path.join(HERE, "instahyre_session.json"))

# Logged-in candidate pages to probe (whichever renders the matched feed).
CANDIDATE_URLS = [
    "https://www.instahyre.com/candidate/opportunities/",
    "https://www.instahyre.com/candidate/",
    "https://www.instahyre.com/",
]

BLOCK_MARKERS = ["access denied", "unusual traffic", "are you a human", "captcha",
                 "request unsuccessful", "cloudflare", "attention required"]
# Controls we care about on the feed / job view.
CTA_RE = re.compile(r"^\s*(apply|i am interested|interested|view|not interested|save)\s*$", re.I)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def maybe_session() -> str | None:
    if os.path.exists(SESSION_FILE):
        return SESSION_FILE
    b64 = os.environ.get("INSTAHYRE_SESSION_B64", "")
    if not b64:
        return None
    with open(SESSION_FILE, "wb") as f:
        f.write(base64.b64decode(b64))
    return SESSION_FILE


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
            loc = page.get_by_role(role)
            n = loc.count()
        except Exception:
            n = 0
        hits = []
        for i in range(min(n, 60)):
            try:
                label = (loc.nth(i).inner_text(timeout=250) or "").strip()
            except Exception:
                continue
            if label and CTA_RE.match(label):
                hits.append(label)
        if hits:
            from collections import Counter
            print(f"  {role} CTAs: {dict(Counter(hits))}")


def looks_logged_out(page) -> bool:
    url = (page.url or "").lower()
    if "/login" in url or "/register" in url:
        return True
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    return "log in to continue" in body or "sign in to your account" in body


def main() -> None:
    session = maybe_session()
    print("=" * 60)
    print(f"Instahyre Recon — India · mode={'LOGGED-IN' if session else 'ANONYMOUS'}")
    print("=" * 60)

    from playwright.sync_api import sync_playwright
    captured: list[dict] = []
    verdict = {"reached": False, "blocked": False, "authed": False, "api_hits": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,   # headful under Xvfb
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        context = browser.new_context(
            storage_state=session if session else None,
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
            viewport={"width": 1280, "height": 900},
        )
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

        def on_response(resp):
            try:
                u = resp.url
                if "instahyre.com" in u and ("/api/" in u or "opportunit" in u.lower()
                                             or "employer" in u.lower()):
                    body = None
                    try:
                        if "application/json" in (resp.headers.get("content-type") or ""):
                            body = resp.json()
                    except Exception:
                        pass
                    captured.append({"url": u, "status": resp.status,
                                     "keys": list(body.keys()) if isinstance(body, dict) else None})
                    if body is not None:
                        with open(os.path.join(RECON_DIR, f"insta_api_{len(captured)}.json"),
                                  "w", encoding="utf-8") as f:
                            json.dump(body, f, indent=2)
            except Exception:
                pass

        page = context.new_page()
        page.on("response", on_response)

        for i, url in enumerate(CANDIDATE_URLS):
            print(f"\n[{i+1}] GET {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(6000)
                verdict["reached"] = True
            except Exception as exc:
                print(f"  [warn] nav: {exc}")
            snap(page, f"instahyre_{i}")
            try:
                title = page.title()
                body = (page.inner_text("body") or "")[:4000].lower()
            except Exception:
                title, body = "", ""
            blocked = any(m in (title.lower() + " " + body) for m in BLOCK_MARKERS)
            out = looks_logged_out(page)
            verdict["blocked"] = verdict["blocked"] or blocked
            verdict["authed"] = verdict["authed"] or (session and not out)
            print(f"  final url: {page.url}")
            print(f"  title    : {title[:80]}")
            print(f"  bot-block: {blocked} | session: {'LOGGED OUT' if out else 'authenticated'}")
            dump_ctas(page, f"page{i}")

        verdict["api_hits"] = len(captured)
        context.close()
        browser.close()

    print("\n" + "=" * 60)
    print("RECON VERDICT")
    for k, v in verdict.items():
        print(f"  {k}: {v}")
    print(f"  intercepted API XHR: {[c['url'][:80] for c in captured][:8]}")
    ok = verdict["reached"] and not verdict["blocked"]
    print(f"\n  => Instahyre from GitHub IP looks {'VIABLE' if ok else 'BLOCKED / not usable'}.")
    print("=" * 60)


if __name__ == "__main__":
    main()
