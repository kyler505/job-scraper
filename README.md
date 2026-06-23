# job-scraper

Daily GitHub Action scraper for internship and new-grad SWE roles.

## What it does

- polls curated GitHub internship / new-grad repositories by default
- also supports public ATS sources if you pass them in manually
- filters for internship / new-grad roles
- keeps only SWE-like titles by default
- writes results to:
  - `outputs/jobs.json`
  - `outputs/jobs.md`
  - `outputs/discovery.md`
- syncs filtered listings into the `kyler505/jb` Obsidian vault when `JB_VAULT_DIR` points at a checked-out vault
- can post a run summary to Discord when a Discord webhook or bot token is configured

## Local test

```bash
python -m pip install -r requirements.txt
python scraper.py --max-results 50 --vault-dir /path/to/jb
```

If you only want the scrape outputs and do not want to touch the vault:

```bash
python scraper.py --max-results 50
```

## Vault sync

Required for vault sync:

- `JB_VAULT_DIR`

Optional behavior:

- `JB_DEACTIVATE_MISSING=true` to mark unmatched existing vault notes as `active: false`

By default, deactivation is off to avoid mass churn when source coverage changes.

## Discord notifications

The GitHub Actions workflow can post a completion message to Discord. Configure one of:

- `DISCORD_WEBHOOK_URL`
- `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID`

The workflow defaults `DISCORD_CHANNEL_ID` to `1519008020250492989`.

## GitHub Action

The workflow runs on a daily schedule and can also be triggered manually.
You can override the search terms through `workflow_dispatch` inputs:

- `sources_json`
- `role_terms_json`
- `cycle_terms_json`
- `max_results`

## Extending it

Edit `DEFAULT_SOURCES` in `scraper.py` to add more GitHub repos or ATS boards.
If you want broader results, pass custom source / role / cycle terms through the workflow inputs.
The generated `outputs/discovery.md` contains Google ATS search queries you can reuse manually.
