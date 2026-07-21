# Job Automation — India

India-region job scrapers, split off from the US `Job_Automation` project. Each
scraper polls a company careers source, filters for India-located roles matching
a target-role list, dedupes against a committed seen-file, and emails new matches.

Self-contained: scripts and workflows live in this repo and run directly in
GitHub Actions (no external private-repo checkout).

## Scrapers

| Source | Script | Method | Seen-file |
| ------ | ------ | ------ | --------- |
| Google Careers | `google_scraper_india.py` | Scrapes the embedded Boq `ds:1` state from the server-rendered results page, `location=India` | `json/google_india_seen_jobs.json` |
| Amazon Jobs | `amazon_scraper_india.py` | Public `search.json` API, `sort=recent`, India filtered in Python | `json/amazon_india_api_seen_jobs.json` |

India tracks a **software-only** role set — Software Engineer, Software Developer /
SDE, and Early Grad / Early Career roles, excluding senior+ levels. No data or BI
roles (that's the key difference from the US scrapers, which also match Data
Engineer / Analyst / BI).

India alerts are sent to the dedicated auto-apply inbox `oswin.autoapply@gmail.com`.

## Auto-apply pipeline (Google, India)

The goal is hands-off applying to new Google openings. Flow:

1. `gmail_autoapply_watcher.py` — polls the auto-apply inbox over IMAP, extracts
   Google Careers apply links (job_id + title come straight from the URL), and
   appends new openings to `json/autoapply_queue.json` (deduped against
   `json/autoapply_applied.json`).
2. `google_autoapply.py` — replays a captured Google session (Playwright) against
   each queued job.
   - `--recon` (default): screenshots + dumps the form DOM to artifacts, and
     reports whether the session survives the runner IP. Never fills/submits.
   - `--apply`: fills the form from the answers profile, uploads the resume, and
     submits **only** when `AUTOAPPLY_ENABLE_SUBMIT=1`.

> Google is not a standard ATS — applying drives its own logged-in UI, which has
> IP-tied bot detection. The recon run is the feasibility check for running this
> from GitHub Actions vs. a residential/self-hosted runner.

## Running in Actions

All workflows are `workflow_dispatch`:

- **Google India Scraper** → `.github/workflows/google_scraper_india.yml`
- **Amazon India Scraper** → `.github/workflows/amazon_scraper_india.yml`
- **Gmail Auto-Apply Watcher** → `.github/workflows/gmail_watcher.yml`
- **Google Auto-Apply** → `.github/workflows/google_autoapply.yml` (mode: recon/apply)

## Required secrets / vars

| Secret | Purpose |
| ------ | ------- |
| `EMAIL_SENDER` | Gmail address that sends the alerts |
| `GMAIL_APP_PASSWORD` | Gmail app password for the sender |
| `EMAIL_TO_INDIA` | Recipient(s), comma-separated (the auto-apply inbox) |
| `AUTOAPPLY_GMAIL_APP_PASSWORD` | App password for the auto-apply inbox (IMAP read) |
| `GOOGLE_SESSION_B64` | base64 of a Playwright storageState JSON (captured Google login) |
| `AUTOAPPLY_ANSWERS_JSON` | JSON answer profile (name, phone, work auth, …) |
| `PRIVATE_REPO_PAT` | fetches the resume PDF from `Job_Automation_Private` at runtime |

Repo variable `AUTOAPPLY_ENABLE_SUBMIT=1` arms real submission (off by default).

## Local run

```bash
pip install -r requirements.txt
# put EMAIL_SENDER / GMAIL_APP_PASSWORD / EMAIL_TO_INDIA in a .env file
python google_scraper_india.py
python amazon_scraper_india.py
```

Pass `--preview` to `google_scraper_india.py` to email the current recent roles
without touching the seen-file.
