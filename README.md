# job-scraper

Daily GitHub Action scraper for internship and new-grad SWE roles.

## What it does

- polls public ATS boards (default: Greenhouse)
- filters for internship / new-grad roles
- keeps only SWE-like titles by default
- writes results to:
  - `outputs/jobs.json`
  - `outputs/jobs.md`
- syncs the matches into a Notion database when `NOTION_TOKEN` and `NOTION_DATABASE_ID` are available

## Local test

```bash
python -m pip install -r requirements.txt
python scraper.py --max-results 50
```

To skip Notion sync locally:

```bash
python scraper.py --max-results 50 --no-notion-sync
```

## Notion sync

The workflow reads these GitHub Actions secrets:

- `NOTION_TOKEN`
- `NOTION_DATABASE_ID`

The script auto-detects the Notion database columns by name and type. If your database uses different column names, you can override them with environment variables:

- `NOTION_TITLE_PROPERTY`
- `NOTION_COMPANY_PROPERTY`
- `NOTION_LOCATION_PROPERTY`
- `NOTION_URL_PROPERTY`
- `NOTION_SCORE_PROPERTY`
- `NOTION_UPDATED_AT_PROPERTY`
- `NOTION_SOURCE_PROPERTY`
- `NOTION_STATUS_PROPERTY`
- `NOTION_STATUS_VALUE`

The sync is idempotent when a URL property is available; otherwise it falls back to title/company matching.

## GitHub Action

The workflow runs on a daily schedule and can also be triggered manually.
You can override the search terms through `workflow_dispatch` inputs:

- `sources_json`
- `role_terms_json`
- `cycle_terms_json`
- `max_results`

## Extending it

Edit `DEFAULT_SOURCES` in `scraper.py` to add more company boards.
If you want broader results, pass custom role terms through the workflow inputs.
