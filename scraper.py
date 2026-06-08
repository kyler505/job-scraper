#!/usr/bin/env python3
"""Daily job scraper for internship/new-grad SWE roles.

This script polls public ATS endpoints (default: Greenhouse boards APIs),
filters for internship / new-grad / SWE-related roles, deduplicates the
results, writes a JSON + Markdown summary, and optionally syncs the matches
into a Notion database.

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
from typing import Iterable, Mapping

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

NOTION_API_VERSION = "2022-06-28"
NOTION_DEFAULT_STATUS = os.getenv("NOTION_STATUS_VALUE", "")
NOTION_SOURCE_LABEL = os.getenv("NOTION_SOURCE_LABEL", "job-scraper")

TITLE_CANDIDATES = ["name", "title", "job", "role", "position"]
COMPANY_CANDIDATES = ["company", "employer", "organization", "org", "company name"]
LOCATION_CANDIDATES = ["location", "city", "region", "office"]
URL_CANDIDATES = ["url", "link", "job url", "application url", "source url", "website"]
SCORE_CANDIDATES = ["score", "rank", "priority"]
UPDATED_CANDIDATES = ["updated at", "updated", "last updated", "scraped at", "date"]
SOURCE_CANDIDATES = ["source", "source board", "board", "origin"]
STATUS_CANDIDATES = ["status", "stage", "application status"]


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


@dataclass(frozen=True)
class PropertyMapping:
    title: str | None = None
    company: str | None = None
    location: str | None = None
    url: str | None = None
    score: str | None = None
    updated_at: str | None = None
    source: str | None = None
    status: str | None = None


def load_json_value(raw: str | None, default, label: str):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {label}: {exc}") from exc


def env_truthy(name: str) -> bool:
    value = os.getenv(name)
    return bool(value) and value.strip().lower() not in {"0", "false", "no", "off"}


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
    parser.add_argument(
        "--notion-sync",
        action=argparse.BooleanOptionalAction,
        default=env_truthy("NOTION_SYNC") if os.getenv("NOTION_SYNC") is not None else True,
        help="Sync matches into Notion when NOTION_TOKEN and NOTION_DATABASE_ID are present.",
    )
    return parser.parse_args()


def notion_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def notion_api(session: requests.Session, token: str, method: str, path: str, payload: dict | None = None) -> dict:
    response = session.request(
        method,
        f"https://api.notion.com/v1{path}",
        headers=notion_headers(token),
        json=payload,
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"Notion API {method} {path} failed: {response.status_code} {response.text}")
    return response.json() if response.content else {}


def get_database_schema(session: requests.Session, token: str, database_id: str) -> dict:
    return notion_api(session, token, "GET", f"/databases/{database_id}")


def find_property_name(properties: dict, candidates: list[str], type_name: str | None = None) -> str | None:
    normalized_candidates = [normalize(candidate) for candidate in candidates]

    def matches(name: str) -> bool:
        name_norm = normalize(name)
        return any(name_norm == candidate or candidate in name_norm or name_norm in candidate for candidate in normalized_candidates)

    # Exact or fuzzy name match first
    for name, prop in properties.items():
        if matches(name) and (type_name is None or prop.get("type") == type_name):
            return name

    # Type fallback if no name match was found
    if type_name is not None:
        for name, prop in properties.items():
            if prop.get("type") == type_name:
                return name

    return None


def detect_property_mapping(properties: dict, env: Mapping[str, str]) -> PropertyMapping:
    def explicit_or_detect(env_key: str, candidates: list[str], type_name: str | None = None) -> str | None:
        explicit = env.get(env_key, "").strip()
        if explicit:
            return explicit if explicit in properties else None
        return find_property_name(properties, candidates, type_name=type_name)

    return PropertyMapping(
        title=explicit_or_detect("NOTION_TITLE_PROPERTY", TITLE_CANDIDATES, type_name="title"),
        company=explicit_or_detect("NOTION_COMPANY_PROPERTY", COMPANY_CANDIDATES),
        location=explicit_or_detect("NOTION_LOCATION_PROPERTY", LOCATION_CANDIDATES),
        url=explicit_or_detect("NOTION_URL_PROPERTY", URL_CANDIDATES),
        score=explicit_or_detect("NOTION_SCORE_PROPERTY", SCORE_CANDIDATES, type_name="number"),
        updated_at=explicit_or_detect("NOTION_UPDATED_AT_PROPERTY", UPDATED_CANDIDATES),
        source=explicit_or_detect("NOTION_SOURCE_PROPERTY", SOURCE_CANDIDATES),
        status=explicit_or_detect("NOTION_STATUS_PROPERTY", STATUS_CANDIDATES),
    )


def prop_value(prop_type: str, value) -> dict:
    if prop_type == "title":
        return {"title": [{"type": "text", "text": {"content": str(value)}}]}
    if prop_type == "rich_text":
        return {"rich_text": [{"type": "text", "text": {"content": str(value)}}]}
    if prop_type == "url":
        return {"url": str(value)}
    if prop_type == "number":
        return {"number": value}
    if prop_type == "date":
        return {"date": {"start": str(value)}}
    if prop_type in {"select", "status"}:
        return {prop_type: {"name": str(value)}}
    if prop_type == "checkbox":
        return {"checkbox": bool(value)}
    if prop_type == "email":
        return {"email": str(value)}
    if prop_type == "phone_number":
        return {"phone_number": str(value)}
    return {}


def build_notion_page_properties(job: Job, schema: dict, mapping: PropertyMapping) -> dict:
    properties = schema.get("properties", {})
    page_props: dict[str, dict] = {}

    if not mapping.title or mapping.title not in properties:
        raise RuntimeError(
            "Could not find a title property in the Notion database. "
            "Set NOTION_TITLE_PROPERTY to the correct column name."
        )

    title_type = properties[mapping.title]["type"]
    title_value = job.title
    if mapping.company and mapping.company == mapping.title:
        title_value = f"{job.company} — {job.title}"
    page_props[mapping.title] = prop_value(title_type, title_value)

    field_values = [
        ("company", job.company),
        ("location", job.location),
        ("url", job.url),
        ("score", job.score),
        ("updated_at", job.updated_at),
        ("source", job.source),
    ]
    for field_name, field_value in field_values:
        if field_value in (None, ""):
            continue
        prop_name = getattr(mapping, field_name)
        if not prop_name or prop_name not in properties:
            continue
        if field_name == "company" and prop_name == mapping.title:
            continue
        prop_type = properties[prop_name]["type"]
        if field_name == "updated_at" and prop_type == "date":
            page_props[prop_name] = prop_value(prop_type, field_value)
        elif field_name == "score" and prop_type == "number":
            page_props[prop_name] = prop_value(prop_type, field_value)
        elif prop_type in {"title", "rich_text", "url", "number", "date", "select", "status", "checkbox", "email", "phone_number"}:
            page_props[prop_name] = prop_value(prop_type, field_value)

    if mapping.status and mapping.status in properties and NOTION_DEFAULT_STATUS:
        status_type = properties[mapping.status]["type"]
        if status_type in {"select", "status"}:
            page_props[mapping.status] = prop_value(status_type, NOTION_DEFAULT_STATUS)

    return page_props


def build_notion_dedupe_filter(job: Job, schema: dict, mapping: PropertyMapping) -> dict | None:
    properties = schema.get("properties", {})
    if mapping.url and mapping.url in properties:
        prop_type = properties[mapping.url]["type"]
        if prop_type in {"url", "rich_text", "title"}:
            return {"property": mapping.url, prop_type: {"equals": job.url}}

    clauses = []
    if mapping.title and mapping.title in properties:
        title_type = properties[mapping.title]["type"]
        if title_type in {"title", "rich_text"}:
            clauses.append({"property": mapping.title, title_type: {"equals": job.title}})
    if mapping.company and mapping.company in properties:
        company_type = properties[mapping.company]["type"]
        if company_type in {"rich_text", "select", "status", "title"}:
            clauses.append({"property": mapping.company, company_type: {"equals": job.company}})

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"and": clauses}


def query_existing_pages(session: requests.Session, token: str, database_id: str, filter_payload: dict) -> list[dict]:
    payload = {"filter": filter_payload, "page_size": 10}
    data = notion_api(session, token, "POST", f"/databases/{database_id}/query", payload)
    return data.get("results", [])


def sync_jobs_to_notion(jobs: list[Job]) -> None:
    token = os.getenv("NOTION_TOKEN", "").strip()
    database_id = os.getenv("NOTION_DATABASE_ID", "").strip()
    if not token or not database_id:
        print("Notion sync skipped: NOTION_TOKEN or NOTION_DATABASE_ID not set")
        return

    session = requests.Session()
    schema = get_database_schema(session, token, database_id)
    mapping = detect_property_mapping(schema.get("properties", {}), os.environ)

    print(
        "Notion mapping: "
        f"title={mapping.title or '-'} company={mapping.company or '-'} location={mapping.location or '-'} "
        f"url={mapping.url or '-'} score={mapping.score or '-'} updated_at={mapping.updated_at or '-'} "
        f"source={mapping.source or '-'} status={mapping.status or '-'}"
    )

    created = 0
    updated = 0
    skipped = 0

    for job in jobs:
        dedupe_filter = build_notion_dedupe_filter(job, schema, mapping)
        existing_pages = []
        if dedupe_filter is not None:
            existing_pages = query_existing_pages(session, token, database_id, dedupe_filter)

        page_props = build_notion_page_properties(job, schema, mapping)
        if not page_props:
            print(f"[warn] No usable Notion properties for {job.company} — {job.title}; skipping", file=sys.stderr)
            skipped += 1
            continue

        if existing_pages:
            page_id = existing_pages[0]["id"]
            notion_api(session, token, "PATCH", f"/pages/{page_id}", {"properties": page_props})
            updated += 1
        else:
            notion_api(
                session,
                token,
                "POST",
                "/pages",
                {
                    "parent": {"database_id": database_id},
                    "properties": page_props,
                },
            )
            created += 1

    print(f"Notion sync complete: created={created} updated={updated} skipped={skipped}")


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

    if args.notion_sync:
        sync_jobs_to_notion(jobs)
    else:
        print("Notion sync disabled via --no-notion-sync or NOTION_SYNC=false")

    print()

    for job in jobs[:25]:
        print(f"{job.company}\t{job.title}\t{job.location or 'Unknown'}\t{job.url}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
