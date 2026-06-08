#!/usr/bin/env python3
"""Daily job scraper for internship/new-grad SWE roles.

This script polls public ATS endpoints (default: Greenhouse boards APIs),
filters for internship / new-grad / SWE-related roles, deduplicates the
results, and writes a JSON + Markdown summary for GitHub Actions artifacts.

It is intentionally configurable so you can swap in your own company boards
without changing the code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests


DEFAULT_SOURCES = [
    {
        "name": "airbnb",
        "provider": "greenhouse",
        "url": "https://boards-api.greenhouse.io/v1/boards/airbnb/jobs?content=true",
    },
    {
        "name": "stripe",
        "provider": "greenhouse",
        "url": "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true",
    },
    {
        "name": "figma",
        "provider": "greenhouse",
        "url": "https://boards-api.greenhouse.io/v1/boards/figma/jobs?content=true",
    },
    {
        "name": "datadog",
        "provider": "greenhouse",
        "url": "https://boards-api.greenhouse.io/v1/boards/datadog/jobs?content=true",
    },
    {
        "name": "pinterest",
        "provider": "greenhouse",
        "url": "https://boards-api.greenhouse.io/v1/boards/pinterest/jobs?content=true",
    },
]

DEFAULT_ROLE_TERMS = [
    "software engineer",
    "software engineering",
    "software developer",
    "swe",
    "backend",
    "full stack",
    "frontend",
    "platform",
    "infrastructure",
    "infra",
    "developer",
    "developer experience",
    "api engineer",
    "security engineer",
    "systems engineer",
    "product engineer",
    "mobile engineer",
]

DEFAULT_CYCLE_TERMS = [
    "intern",
    "internship",
    "new grad",
    "new graduate",
    "graduate",
    "entry level",
    "new college grad",
]

USER_AGENT = (
    "job-scraper/1.0 (+https://github.com/kyler505/job-scraper) "
    "requests"
)


@dataclass(frozen=True)
class Job:
    company: str
    title: str
    location: str
    url: str
    updated_at: str | None
    source: str
    score: int

    def key(self) -> tuple[str, str, str]:
        return (self.company.lower(), self.title.lower(), self.url.lower())


def load_json_value(raw: str | None, default, label: str):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {label}: {exc}") from exc


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def compile_terms(terms: Iterable[str]) -> list[re.Pattern]:
    patterns = []
    for term in terms:
        if not term:
            continue
        if term.lower() == "intern":
            patterns.append(re.compile(r"\bintern(?:ship)?\b", re.I))
        else:
            patterns.append(re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.I))
    return patterns


def match_score(text: str, title: str, role_terms: list[re.Pattern], cycle_terms: list[re.Pattern]) -> int:
    score = 0
    title_norm = normalize(title)
    text_norm = normalize(text)

    for pattern in cycle_terms:
        if pattern.search(title_norm):
            score += 6
        elif pattern.search(text_norm):
            score += 3

    for pattern in role_terms:
        if pattern.search(title_norm):
            score += 4
        elif pattern.search(text_norm):
            score += 1

    return score


def fetch_greenhouse_board(session: requests.Session, source: dict) -> list[dict]:
    response = session.get(source["url"], timeout=30)
    response.raise_for_status()
    payload = response.json()
    jobs = payload.get("jobs", [])
    company = source["name"]
    out: list[dict] = []
    for job in jobs:
        location = (job.get("location") or {}).get("name") or ""
        title = job.get("title") or ""
        url = job.get("absolute_url") or ""
        content = job.get("content") or ""
        updated_at = job.get("updated_at")
        text = " ".join([company, title, location, content])
        out.append(
            {
                "company": company,
                "title": title,
                "location": location,
                "url": url,
                "updated_at": updated_at,
                "source": source["url"],
                "text": text,
            }
        )
    return out


def fetch_sources(session: requests.Session, sources: list[dict]) -> list[dict]:
    jobs: list[dict] = []
    for source in sources:
        provider = source.get("provider", "greenhouse")
        if provider != "greenhouse":
            print(f"[warn] Skipping unsupported source provider: {provider}", file=sys.stderr)
            continue
        try:
            jobs.extend(fetch_greenhouse_board(session, source))
        except Exception as exc:
            print(f"[warn] Failed source {source.get('name')}: {exc}", file=sys.stderr)
    return jobs


def filter_jobs(raw_jobs: list[dict], role_terms: list[str], cycle_terms: list[str]) -> list[Job]:
    role_patterns = compile_terms(role_terms)
    cycle_patterns = compile_terms(cycle_terms)

    results: dict[tuple[str, str, str], Job] = {}
    for item in raw_jobs:
        title = item.get("title", "")
        text = item.get("text", "")
        title_norm = normalize(title)
        if not any(pattern.search(title_norm) for pattern in cycle_patterns):
            continue
        score = match_score(text=text, title=title, role_terms=role_patterns, cycle_terms=cycle_patterns)
        if score <= 0:
            continue
        if not any(pattern.search(title_norm) for pattern in role_patterns):
            continue
        job = Job(
            company=item.get("company", ""),
            title=title.strip(),
            location=item.get("location", "").strip(),
            url=item.get("url", ""),
            updated_at=item.get("updated_at"),
            source=item.get("source", ""),
            score=score,
        )
        key = job.key()
        existing = results.get(key)
        if existing is None or job.score > existing.score:
            results[key] = job

    return sorted(
        results.values(),
        key=lambda j: (-j.score, j.company.lower(), j.title.lower(), j.location.lower()),
    )


def write_outputs(jobs: list[Job], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "jobs.json"
    md_path = output_dir / "jobs.md"

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(jobs),
        "jobs": [asdict(job) for job in jobs],
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        "# Job scrape results",
        "",
        f"Generated: {payload['generated_at']}",
        f"Matches: {len(jobs)}",
        "",
    ]
    if jobs:
        for job in jobs:
            lines.append(
                f"- **{job.company}** — {job.title}  \n"
                f"  {job.location or 'Unknown location'}  \n"
                f"  {job.url}  \n"
                f"  _score={job.score}, updated={job.updated_at or 'unknown'}_"
            )
    else:
        lines.append("No matches found.")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return json_path, md_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape internship/new-grad SWE jobs from public ATS boards")
    parser.add_argument(
        "--sources",
        default=os.getenv("SOURCES_JSON", ""),
        help="JSON array of source objects. If omitted, uses built-in defaults.",
    )
    parser.add_argument(
        "--role-terms",
        default=os.getenv("ROLE_TERMS_JSON", ""),
        help="JSON array of role-related keywords to match.",
    )
    parser.add_argument(
        "--cycle-terms",
        default=os.getenv("CYCLE_TERMS_JSON", ""),
        help="JSON array of cycle-related keywords to match (intern/new grad).",
    )
    parser.add_argument(
        "--output-dir",
        default=os.getenv("OUTPUT_DIR", "outputs"),
        help="Directory where jobs.json and jobs.md are written.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=int(os.getenv("MAX_RESULTS", "200")),
        help="Maximum number of jobs to emit after filtering.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    sources = load_json_value(os.getenv("SOURCES_JSON"), DEFAULT_SOURCES, "SOURCES_JSON")
    if args.sources:
        sources = load_json_value(args.sources, sources, "--sources")

    role_terms = load_json_value(os.getenv("ROLE_TERMS_JSON"), DEFAULT_ROLE_TERMS, "ROLE_TERMS_JSON")
    if args.role_terms:
        role_terms = load_json_value(args.role_terms, role_terms, "--role-terms")

    cycle_terms = load_json_value(os.getenv("CYCLE_TERMS_JSON"), DEFAULT_CYCLE_TERMS, "CYCLE_TERMS_JSON")
    if args.cycle_terms:
        cycle_terms = load_json_value(args.cycle_terms, cycle_terms, "--cycle-terms")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json,text/plain,*/*"})

    raw_jobs = fetch_sources(session, sources)
    jobs = filter_jobs(raw_jobs, role_terms=role_terms, cycle_terms=cycle_terms)
    jobs = jobs[: args.max_results]

    output_dir = Path(args.output_dir)
    json_path, md_path = write_outputs(jobs, output_dir)

    print(f"Sources checked: {len(sources)}")
    print(f"Raw jobs fetched: {len(raw_jobs)}")
    print(f"Matches: {len(jobs)}")
    print(f"Wrote: {json_path}")
    print(f"Wrote: {md_path}")
    print()

    for job in jobs[:25]:
        print(f"{job.company}\t{job.title}\t{job.location or 'Unknown'}\t{job.url}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
