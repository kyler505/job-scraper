# job-scraper

Daily GitHub Action scraper for internship and new-grad SWE roles.

## What it does

- polls public ATS boards (default: Greenhouse)
- filters for internship / new-grad roles
- keeps only SWE-like titles by default
- writes results to:
  - `outputs/jobs.json`
  - `outputs/jobs.md`

## Local test

```bash
python -m pip install -r requirements.txt
python scraper.py --max-results 50
```

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
