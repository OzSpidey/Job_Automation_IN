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

Both match the same target roles as the US versions (Data Engineer, Data Analyst,
Business Intelligence / Analyst, BI Engineer/Developer, AI Engineer, Early Grad),
excluding senior+ levels. Only the location filter, recipient, timezone, and
email labels differ from the US scripts.

## Running in Actions

Both workflows are `workflow_dispatch` (manual / external-cron triggered):

- **Google India Scraper** → `.github/workflows/google_scraper_india.yml`
- **Amazon India Scraper** → `.github/workflows/amazon_scraper_india.yml`

## Required secrets

| Secret | Purpose |
| ------ | ------- |
| `EMAIL_SENDER` | Gmail address that sends the alerts |
| `GMAIL_APP_PASSWORD` | Gmail app password for the sender |
| `EMAIL_TO_INDIA` | Recipient(s), comma-separated |

## Local run

```bash
pip install -r requirements.txt
# put EMAIL_SENDER / GMAIL_APP_PASSWORD / EMAIL_TO_INDIA in a .env file
python google_scraper_india.py
python amazon_scraper_india.py
```

Pass `--preview` to `google_scraper_india.py` to email the current recent roles
without touching the seen-file.
