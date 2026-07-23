"""
Naukri Recon — India (make-or-break IP / bot-block test)
========================================================
Naukri's search API (/jobapi/v3/search) needs a session-signed `nkparam` token
minted by its own JS, and Naukri bot-blocks datacenter IPs. So before building
anything, this probe answers ONE question: can a GitHub Actions runner load
Naukri and read jobs, or does it get bot-blocked?

It loads a Naukri search results page with Playwright — LOGGED IN if
NAUKRI_SESSION_B64 is set, else ANONYMOUS — screenshots + dumps the DOM, and
INTERCEPTS any jobapi/v3/search XHR (that response is exactly what the scraper
would consume). It then reports: page reached? bot-block challenge? job cards
visible? jobapi intercepted? session authenticated?

Run:  python naukri_recon.py
Env:  NAUKRI_SESSION_B64 / NAUKRI_SESSION_FILE   (optional — omit for anon probe)
"""

import base64
import json
import os
import re
import sys

HERE           = os.path.dirname(__file__)
SCREENSHOT_DIR = os.path.join(HERE, "screenshots")
RECON_DIR      = os.path.join(HERE, "recon")
SESSION_FILE   = os.environ.get("NAUKRI_SESSION_FILE", os.path.join(HERE, "naukri_session.json"))

# Proven search-results URLs (this exact ?k=...&experience= form worked in the
# retired cloud scraper). These render job cards server-side + trigger the XHR.
SEARCH_URLS = [
    "https://www.naukri.com/software-engineer-jobs?k=software+engineer&experience=1",
    "https://www.naukri.com/python-jobs?k=python&experience=1",
]

# Text that betrays an Akamai / bot-block / challenge page.
BLOCK_MARKERS = [
    "access denied", "reference #", "unusual traffic", "are you a human",
    "verify you are", "captcha", "request unsuccessful", "bot detected",
    "edgesuite", "akamai", "blocked",
]
JOB_CARD_SELECTORS = [
    "div.srp-jobtuple-wrapper", "article.jobTuple", "[data-job-id]",
    "div.cust-job-tuple", "a.title",
]

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def maybe_session() -> str | None:
    if os.path.exists(SESSION_FILE):
        return SESSION_FILE
    b64 = os.environ.get("NAUKRI_SESSION_B64", "")
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


def count_cards(page) -> tuple[str, int]:
    for sel in JOB_CARD_SELECTORS:
        try:
            n = page.locator(sel).count()
        except Exception:
            n = 0
        if n:
            return sel, n
    return "", 0


def main() -> None:
    session = maybe_session()
    mode = "LOGGED-IN" if session else "ANONYMOUS"
    print("=" * 60)
    print(f"Naukri Recon — India · mode={mode}")
    print("=" * 60)

    from playwright.sync_api import sync_playwright

    captured: list[dict] = []
    verdict = {"reached": False, "blocked": False, "cards": 0, "api_hits": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        context = browser.new_context(
            storage_state=session if session else None,
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            locale="en-IN",
            timezone_id="Asia/Kolkata",                              # match proven scraper
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},  # missing this = bot tell
            viewport={"width": 1280, "height": 900},
        )
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

        def on_response(resp):
            try:
                if "jobapi" in resp.url and "search" in resp.url:
                    body = None
                    try:
                        body = resp.json()
                    except Exception:
                        pass
                    captured.append({"url": resp.url, "status": resp.status,
                                     "n": len((body or {}).get("jobDetails", []) or []) if body else 0})
                    if body:
                        with open(os.path.join(RECON_DIR, f"naukri_api_{len(captured)}.json"),
                                  "w", encoding="utf-8") as f:
                            json.dump(body, f, indent=2)
            except Exception:
                pass

        page = context.new_page()
        page.on("response", on_response)

        for i, url in enumerate(SEARCH_URLS):
            print(f"\n[{i+1}] GET {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3500)
                try:
                    page.wait_for_selector("a.title", timeout=15000)  # like the proven scraper
                except Exception:
                    pass
                verdict["reached"] = True
            except Exception as exc:
                print(f"  [warn] nav: {exc}")
            snap(page, f"naukri_{mode.lower()}_{i}")

            title = ""
            body_txt = ""
            try:
                title = page.title()
                body_txt = (page.inner_text("body") or "")[:4000].lower()
            except Exception:
                pass
            blocked = any(m in (title.lower() + " " + body_txt) for m in BLOCK_MARKERS)
            verdict["blocked"] = verdict["blocked"] or blocked
            sel, n = count_cards(page)
            verdict["cards"] = max(verdict["cards"], n)
            logged_out = ("login" in body_txt and "register" in body_txt)
            print(f"  final url : {page.url}")
            print(f"  title     : {title[:80]}")
            print(f"  bot-block : {blocked}")
            print(f"  job cards : {n} (selector={sel or 'none'})")
            if session:
                print(f"  session   : {'looks LOGGED OUT' if logged_out else 'looks authenticated'}")

        verdict["api_hits"] = len(captured)
        context.close()
        browser.close()

    print("\n" + "=" * 60)
    print("RECON VERDICT")
    print(f"  reached page      : {verdict['reached']}")
    print(f"  bot-blocked       : {verdict['blocked']}")
    print(f"  max job cards seen : {verdict['cards']}")
    print(f"  jobapi XHR hits   : {verdict['api_hits']}  {[c['url'][:70] for c in captured]}")
    ok = verdict["reached"] and not verdict["blocked"] and (verdict["cards"] > 0 or verdict["api_hits"] > 0)
    print(f"\n  => Naukri from GitHub IP looks {'VIABLE' if ok else 'BLOCKED / not usable'}.")
    print("=" * 60)


if __name__ == "__main__":
    main()
