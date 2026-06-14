# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A Python job scraper that runs on a daily GitHub Actions schedule and writes results to a Notion database.

## Setup

```bash
pip install -r requirements.txt
```

Requires two environment variables (set in GitHub Actions secrets, or locally via `.env`):
- `NOTION_TOKEN` — Notion integration token
- `NOTION_DATABASE_ID` — target database ID

## Running

```bash
python scraper.py
```

The GitHub Actions workflow (`.github/workflows/scrape.yml`) runs this automatically at 13:00 UTC daily and can also be triggered manually via `workflow_dispatch`.

## Architecture

`scraper.py` is the single entry point. It currently fetches a URL with `requests`, parses HTML with BeautifulSoup, and is expected to write structured job data to Notion via `notion-client`. The `pandas` and `python-dotenv` dependencies are in `requirements.txt` for use as the scraper logic is built out.

The intended data flow: scrape job listings → parse into structured records → push to Notion database.
