#!/usr/bin/env python3
"""Daily job scraper for internship/new-grad SWE roles.

This script polls public ATS endpoints (default: Greenhouse boards APIs),
filters for internship / new-grad / SWE-related roles, deduplicates the
results, writes a JSON + Markdown summary, and syncs the matches into the
jb Obsidian vault when configured.

It is intentionally configurable so you can swap in your own company boards
without changing the code.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup


DEFAULT_SOURCES = [
    {
        "name": "simplify-summer-2026",
        "provider": "github_readme",
        "repo": "SimplifyJobs/Summer2026-Internships",
    },
    {
        "name": "simplify-new-grad-positions",
        "provider": "github_readme",
        "repo": "SimplifyJobs/New-Grad-Positions",
    },
    {
        "name": "vansh-summer-2027",
        "provider": "github_readme",
        "repo": "vanshb03/Summer2027-Internships",
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
    "co-op",
    "coop",
]

ATS_MATRIX = [
    {
        "label": "High-precision startup ATS",
        "domains": ["jobs.ashbyhq.com", "boards.greenhouse.io", "jobs.lever.co"],
        "template": 'site:{domain} ({role} OR {role_alt}) {cycle}',
        "use_when": "You want fewer false positives and fresher startup postings.",
    },
    {
        "label": "Broader enterprise ATS",
        "domains": ["careers.icims.com", "jobs.jobvite.com", "wd1.myworkdayjobs.com"],
        "template": 'site:{domain} ({role} OR {role_alt} OR {team}) {cycle}',
        "use_when": "You want wider company coverage and are willing to sort noise.",
    },
    {
        "label": "Long-tail ATS / SMB boards",
        "domains": ["jobs.bamboohr.com", "jobs.smartrecruiters.com", "apply.jazz.co", "careers.workable.com"],
        "template": 'site:{domain} ({role} OR {team}) {cycle}',
        "use_when": "You want smaller-company openings that LinkedIn often misses.",
    },
]

ROLE_FAMILIES = [
    {"label": "core SWE", "terms": ["software engineer", "software developer", "swe"]},
    {"label": "platform / infra", "terms": ["platform", "infrastructure", "infra", "systems"]},
    {"label": "product / full stack", "terms": ["full stack", "frontend", "backend", "product engineer"]},
    {"label": "specialist", "terms": ["security engineer", "mobile engineer", "api engineer", "developer experience"]},
]

DISCOVERY_NOTES = """# How to find jobs

Most jobs are not posted on LinkedIn. Use Google with ATS site searches to find
them earlier and with less competition.

## Search matrix

{matrix}

## Query construction

1. Pick one ATS family from the matrix.
2. Pick a role family.
3. Add the cycle term (`intern`, `internship`, `new grad`, etc.).
4. Add a location or team keyword only if the query is too broad.
5. Run the same query across multiple domains and dedupe the results.

### High-precision query examples

- `site:jobs.ashbyhq.com ("software engineer" OR swe) internship`
- `site:boards.greenhouse.io ("platform" OR infra) "new grad"`
- `site:jobs.lever.co ("full stack" OR frontend) intern`

### High-recall query examples

- `site:wd1.myworkdayjobs.com (software OR engineer) (intern OR "new grad")`
- `site:jobs.smartrecruiters.com (backend OR platform) entry level`
- `site:careers.icims.com (developer OR "software engineer") graduate`

Startup roles are often never posted publicly on LinkedIn, so going where the
jobs actually live tends to surface better openings faster.
"""


def build_search_matrix() -> str:
    lines: list[str] = []
    for section in ATS_MATRIX:
        domains = ", ".join(f"`site:{domain}`" for domain in section["domains"])
        lines.append(f"- **{section['label']}**")
        lines.append(f"  - Domains: {domains}")
        lines.append(f"  - Pattern: `{section['template']}`")
        lines.append(f"  - When to use: {section['use_when']}")
    lines.append("")
    lines.append("### Role families")
    for family in ROLE_FAMILIES:
        terms = ", ".join(f"`{term}`" for term in family["terms"])
        lines.append(f"- **{family['label']}**: {terms}")
    return "\n".join(lines)




USER_AGENT = (
    "job-scraper/1.0 (+https://github.com/kyler505/job-scraper) "
    "requests"
)

GITHUB_API_BASE = "https://api.github.com"


@dataclass(frozen=True)
class Job:
    company: str
    title: str
    location: str
    url: str
    updated_at: str | None
    source: str
    score: int
    category: str = ""
    discipline: str = "other"
    terms: list[str] = field(default_factory=list)
    listing_id: str = ""
    date_posted: str | None = None
    date_updated: str | None = None

    def key(self) -> tuple[str, str, str]:
        return (self.company.lower(), self.title.lower(), self.url.lower())


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


def github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    token = os.getenv("GITHUB_TOKEN", "").strip() or os.getenv("GH_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_api_get(session: requests.Session, path: str) -> dict:
    response = session.get(f"{GITHUB_API_BASE}{path}", headers=github_headers(), timeout=30)
    response.raise_for_status()
    return response.json()


def source_label(source: dict) -> str:
    return source.get("repo") or source.get("name") or source.get("url") or "unknown-source"


def clean_text(value: str) -> str:
    value = re.sub(r"\s+", " ", (value or "").replace("\u00a0", " ")).strip()
    value = re.sub(r"^[^\w\d]+", "", value).strip()
    return value


def html_to_text(value: str) -> str:
    if not value:
        return ""
    if "<" in value and ">" in value:
        value = BeautifulSoup(value, "html.parser").get_text(" • ", strip=True)
    return clean_text(value)


def extract_first_url(value: str) -> str:
    if not value:
        return ""
    if "<a" in value.lower():
        soup = BeautifulSoup(value, "html.parser")
        link = soup.find("a", href=True)
        if link and link.get("href"):
            return link["href"].strip()
    md_link = re.search(r"\[[^\]]+\]\(([^)]+)\)", value)
    if md_link:
        return md_link.group(1).strip()
    href_link = re.search(r'href=["\']([^"\']+)["\']', value, re.I)
    if href_link:
        return href_link.group(1).strip()
    raw_url = re.search("https?://[^\\s<>\"]+", value)
    if raw_url:
        return raw_url.group(0).strip()
    return ""


def split_markdown_row(row: str) -> list[str]:
    row = row.strip().strip("|")
    return [part.strip() for part in row.split("|")]


def is_markdown_table_separator(row: str) -> bool:
    row = row.strip()
    if "-" not in row:
        return False
    return bool(re.fullmatch(r"\|?[\s:\-]+(?:\|[\s:\-]+)+\|?", row))


def is_markdown_table_row(row: str) -> bool:
    return "|" in row and not row.lstrip().startswith("<")


def parse_date_value(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%B %d, %Y",
        "%b %d, %Y",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def parse_relative_age(value: str | None, now: datetime | None = None) -> str | None:
    if not value:
        return None
    text = value.strip().lower()
    match = re.fullmatch(r"(\d+)\s*([dwmy])", text)
    if not match:
        return None
    qty = int(match.group(1))
    unit = match.group(2)
    days = {
        "d": qty,
        "w": qty * 7,
        "m": qty * 30,
        "y": qty * 365,
    }[unit]
    anchor = now or datetime.now(timezone.utc)
    return (anchor.date() - timedelta(days=days)).isoformat()


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    filtered_query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"ref", "gh_src", "gh_jid"}
    ]
    query = urlencode(filtered_query, doseq=True)
    cleaned = urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, query, ""))
    return cleaned.rstrip("?")


def make_listing_id(company: str, title: str, url: str) -> str:
    basis = canonicalize_url(url) or f"{normalize(company)}::{normalize(title)}"
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()
    return "-".join([
        digest[:8],
        digest[8:12],
        digest[12:16],
        digest[16:20],
        digest[20:32],
    ])


def infer_category(title: str, source: str) -> str:
    title_norm = normalize(title)
    source_norm = normalize(source)
    if any(term in title_norm for term in ["intern", "internship", "co-op", "coop"]):
        return "internship"
    if any(term in title_norm for term in ["new grad", "new graduate", "graduate", "entry level", "junior", "rotation program"]):
        return "new-grad"
    if "summer" in source_norm or "intern" in source_norm:
        return "internship"
    return "new-grad" if "new-grad" in source_norm or "new grad" in source_norm else "other"


def infer_terms(title: str, source: str) -> list[str]:
    source_norm = normalize(source)
    if infer_category(title, source) == "new-grad":
        return []
    if "summer2027" in source_norm or "summer 2027" in source_norm:
        return ["Summer 2027"]
    if "summer2026" in source_norm or "summer 2026" in source_norm:
        return ["Summer 2026"]
    return []


def normalize_source_label(source: str) -> str:
    source_norm = normalize(source)
    if "new-grad-positions" in source_norm or "new grad positions" in source_norm:
        return "simplify-new-grad"
    if "summer2026" in source_norm or "summer 2026" in source_norm:
        return "simplify-internships"
    if "summer2027" in source_norm or "summer 2027" in source_norm:
        return "vansh-internships"
    return source.replace("/", "-").replace(" ", "-").lower()


def infer_discipline(title: str) -> str:
    title_norm = normalize(title)
    checks = [
        ("ml", ["machine learning", "ml ", " ai", "artificial intelligence", "applied ai"]),
        ("data", ["data engineer", "data scientist", "data analyst", "analytics", "business intelligence"]),
        ("security", ["security", "cyber", "application security", "product security"]),
        ("devops", ["devops", "platform", "infrastructure", "infra", "sre", "site reliability", "developer infrastructure"]),
        ("mobile", ["ios", "android", "mobile"]),
        ("frontend", ["frontend", "front-end", "ui engineer", "web engineer"]),
        ("backend", ["backend", "back-end", "api engineer"]),
        ("hardware", ["hardware", "embedded", "firmware", "electrical", "systems"]),
    ]
    for label, needles in checks:
        if any(needle in title_norm for needle in needles):
            return label
    if any(needle in title_norm for needle in ["software", "developer", "engineer", "full stack", "full-stack", "swe"]):
        return "swe"
    return "other"


PRESERVED_VAULT_FIELDS = {
    "status",
    "applied_date",
    "deadline",
    "notes",
    "priority",
    "apply_method",
    "apply_result",
    "apply_error",
    "confirmation",
    "resume_used",
    "needs_review",
}

VAULT_FIELD_ORDER = [
    "company",
    "role",
    "category",
    "discipline",
    "locations",
    "terms",
    "url",
    "source",
    "listing_id",
    "active",
    "date_posted",
    "date_updated",
    "status",
    "applied_date",
    "deadline",
    "notes",
    "priority",
    "apply_method",
    "apply_result",
    "apply_error",
    "confirmation",
    "resume_used",
    "needs_review",
]


FM_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.S)


def split_note(text: str) -> tuple[str, str]:
    match = FM_RE.match(text)
    if not match:
        return "", text
    return match.group(1), match.group(2)


def parse_scalar(value: str) -> Any:
    text = value.strip()
    if text in {"null", "Null", "NULL", "~", ""}:
        return None
    if text in {"true", "True", "TRUE"}:
        return True
    if text in {"false", "False", "FALSE"}:
        return False
    if (text.startswith("'") and text.endswith("'")) or (text.startswith('"') and text.endswith('"')):
        return text[1:-1]
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [parse_scalar(part.strip()) for part in inner.split(",")]
    return text


def parse_frontmatter(text: str) -> dict[str, Any]:
    fm_text, _ = split_note(text)
    if not fm_text:
        return {}
    data: dict[str, Any] = {}
    current_list_key: str | None = None
    for raw_line in fm_text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") and current_list_key:
            data.setdefault(current_list_key, []).append(parse_scalar(stripped[2:]))
            continue
        current_list_key = None
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value == "":
            data[key] = []
            current_list_key = key
        else:
            data[key] = parse_scalar(raw_value)
    return data


def format_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".") if not value.is_integer() else str(int(value))
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if text == "" or re.search(r"[:#\[\]{}&*!|>'\"%@`\n]", text) or text.strip() != text:
        return f"'{text}'"
    return text


def dump_frontmatter(data: dict[str, Any]) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for key in VAULT_FIELD_ORDER + sorted(key for key in data if key not in VAULT_FIELD_ORDER):
        if key in seen or key not in data:
            continue
        seen.add(key)
        value = data[key]
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                for item in value:
                    lines.append(f"- {format_scalar(item)}")
        else:
            lines.append(f"{key}: {format_scalar(value)}")
    return "\n".join(lines)


def write_note(path: Path, frontmatter: dict[str, Any], body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = body.lstrip("\n")
    fm_text = dump_frontmatter(frontmatter)
    text = f"---\n{fm_text}\n---\n"
    if body:
        text += f"\n{body.rstrip()}\n"
    path.write_text(text, encoding="utf-8")


def slugify_filename(value: str) -> str:
    sanitized = re.sub(r"[\\/:*?\"<>|]", "", value)
    sanitized = re.sub(r"\s+", " ", sanitized).strip().rstrip(".")
    return sanitized or "Untitled"


def split_locations(location: str) -> list[str]:
    parts = [part.strip() for part in location.split(" • ") if part.strip()]
    cleaned = [part for part in parts if not re.fullmatch(r"\d+\s+locations?", part.lower())]
    if cleaned:
        return cleaned
    return [location.strip()] if location.strip() else []


def build_vault_frontmatter(job: Job, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    existing_listing_id = str(existing.get("listing_id") or "").strip()
    frontmatter = {key: value for key, value in existing.items() if key not in {
        "company", "role", "category", "discipline", "locations", "terms", "url", "source", "listing_id", "active", "date_posted", "date_updated"
    }}
    frontmatter.update({
        "company": job.company,
        "role": job.title,
        "category": job.category or infer_category(job.title, job.source),
        "discipline": job.discipline or infer_discipline(job.title),
        "locations": split_locations(job.location),
        "terms": job.terms,
        "url": canonicalize_url(job.url),
        "source": job.source,
        "listing_id": existing_listing_id or job.listing_id or make_listing_id(job.company, job.title, job.url),
        "active": True,
        "date_posted": job.date_posted or existing.get("date_posted"),
        "date_updated": job.date_updated or existing.get("date_updated"),
    })
    frontmatter.setdefault("status", "to-apply")
    frontmatter.setdefault("applied_date", None)
    frontmatter.setdefault("deadline", None)
    frontmatter.setdefault("notes", None)
    return frontmatter


def sync_jobs_to_vault(jobs: list[Job], vault_dir: Path, deactivate_missing: bool = False) -> None:
    jobs_dir = vault_dir / "Jobs"
    if not jobs_dir.exists():
        raise RuntimeError(f"Vault Jobs directory not found: {jobs_dir}")

    existing_by_url: dict[str, Path] = {}
    existing_by_listing_id: dict[str, Path] = {}
    existing_frontmatter: dict[Path, dict[str, Any]] = {}
    existing_bodies: dict[Path, str] = {}

    for path in jobs_dir.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        _, body = split_note(text)
        existing_frontmatter[path] = fm
        existing_bodies[path] = body
        url = canonicalize_url(str(fm.get("url") or ""))
        if url:
            existing_by_url[url] = path
        listing_id = str(fm.get("listing_id") or "").strip()
        if listing_id:
            existing_by_listing_id[listing_id] = path

    seen_paths: set[Path] = set()
    created = 0
    updated = 0
    reactivated = 0

    for job in jobs:
        listing_id = job.listing_id or make_listing_id(job.company, job.title, job.url)
        canonical_url = canonicalize_url(job.url)
        path = existing_by_url.get(canonical_url) or existing_by_listing_id.get(listing_id)
        if path is None:
            filename = slugify_filename(f"{job.company} - {job.title}") + ".md"
            path = jobs_dir / filename
            suffix = 2
            while path.exists():
                path = jobs_dir / f"{slugify_filename(f'{job.company} - {job.title}')} ({suffix}).md"
                suffix += 1
            fm = build_vault_frontmatter(job)
            write_note(path, fm, "")
            created += 1
        else:
            existing_fm = existing_frontmatter.get(path, {})
            existing_body = existing_bodies.get(path, "")
            fm = build_vault_frontmatter(job, existing_fm)
            previous_active = existing_fm.get("active")
            if previous_active is False:
                reactivated += 1
            if fm != existing_fm:
                write_note(path, fm, existing_body)
                updated += 1
        seen_paths.add(path)

    deactivated = 0
    skipped_deactivation = 0
    for path, fm in existing_frontmatter.items():
        if path in seen_paths:
            continue
        if str(fm.get("url") or "").strip() == "":
            continue
        if not deactivate_missing:
            skipped_deactivation += 1
            continue
        if fm.get("active") is False:
            continue
        fm = dict(fm)
        fm["url"] = canonicalize_url(str(fm.get("url") or ""))
        fm["active"] = False
        before = path.read_text(encoding="utf-8")
        write_note(path, fm, existing_bodies.get(path, ""))
        after = path.read_text(encoding="utf-8")
        if after != before:
            deactivated += 1

    print(
        f"Vault sync complete: created={created} updated={updated} reactivated={reactivated} deactivated={deactivated} skipped_deactivation={skipped_deactivation} vault={vault_dir}"
    )


def parse_table_schema(headers: list[str]) -> dict[str, int | None]:
    normalized = [normalize(header) for header in headers]

    def pick(*needles: str) -> int | None:
        for idx, header in enumerate(normalized):
            if any(needle in header for needle in needles):
                return idx
        return None

    return {
        "company": pick("company", "employer", "organization"),
        "title": pick("role", "title", "job"),
        "location": pick("location", "city", "region", "office"),
        "url": pick("application", "apply", "link", "url", "job"),
        "updated_at": pick("age", "date", "posted", "added", "updated"),
    }


def build_raw_job(company: str, title: str, location: str, url: str, updated_at: str | None, source: dict, text: str) -> dict:
    source_name = source_label(source)
    return {
        "company": company,
        "title": title,
        "location": location,
        "url": url,
        "updated_at": updated_at,
        "source": source_name,
        "text": text,
    }


def fetch_github_repo_jobs(session: requests.Session, source: dict) -> list[dict]:
    repo = source.get("repo", "").strip()
    if not repo:
        raise ValueError("GitHub source is missing repo")
    readme_path = source.get("path", "").strip()
    if readme_path:
        data = github_api_get(session, f"/repos/{repo}/contents/{readme_path}")
    else:
        data = github_api_get(session, f"/repos/{repo}/readme")

    content = data.get("content", "")
    if data.get("encoding", "base64") == "base64":
        readme = base64.b64decode(content).decode("utf-8", errors="replace")
    else:
        readme = content

    source_url = data.get("html_url") or f"https://github.com/{repo}"
    source_text = f"{repo} {source_url}"
    jobs: list[dict] = []

    soup = BeautifulSoup(readme, "html.parser")
    for table in soup.find_all("table"):
        headers = [html_to_text(th.get_text(" ", strip=True)) for th in table.find_all("th")]
        schema = parse_table_schema(headers)
        prev_company = ""
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"], recursive=False)
            if not cells or all(cell.name == "th" for cell in cells):
                continue
            cell_texts = [html_to_text(cell.get_text(" • ", strip=True)) for cell in cells]

            def get_cell(idx: int | None) -> str:
                if idx is None or idx >= len(cell_texts):
                    return ""
                return cell_texts[idx]

            company = get_cell(schema["company"])
            if not company or company == "↳":
                company = prev_company
            else:
                prev_company = company

            title = get_cell(schema["title"])
            location = get_cell(schema["location"])
            updated_at = get_cell(schema["updated_at"]) or None

            url = ""
            url_idx = schema["url"]
            if url_idx is not None and url_idx < len(cells):
                url = extract_first_url(cells[url_idx].decode_contents()) or extract_first_url(cell_texts[url_idx])
            if not url and schema["title"] is not None and schema["title"] < len(cells):
                url = extract_first_url(cells[schema["title"]].decode_contents()) or extract_first_url(cell_texts[schema["title"]])
            if not url:
                for cell in cells:
                    url = extract_first_url(cell.decode_contents())
                    if url:
                        break

            if not company or not title or not url:
                continue

            text = " ".join(part for part in [source_text, company, title, location, updated_at or ""] if part)
            jobs.append(build_raw_job(company, title, location, url, updated_at, source, text))

    lines = readme.splitlines()
    i = 0
    prev_company = ""
    while i < len(lines) - 1:
        header_line = lines[i].rstrip()
        sep_line = lines[i + 1].rstrip()
        if is_markdown_table_row(header_line) and is_markdown_table_separator(sep_line):
            headers = [html_to_text(cell) for cell in split_markdown_row(header_line)]
            schema = parse_table_schema(headers)
            j = i + 2
            while j < len(lines) and is_markdown_table_row(lines[j].rstrip()):
                row = lines[j].rstrip()
                cells = [html_to_text(cell) for cell in split_markdown_row(row)]
                if not cells:
                    j += 1
                    continue

                def get_cell(idx: int | None) -> str:
                    if idx is None or idx >= len(cells):
                        return ""
                    return cells[idx]

                company = get_cell(schema["company"])
                if not company or company == "↳":
                    company = prev_company
                else:
                    prev_company = company

                title = get_cell(schema["title"])
                location = get_cell(schema["location"])
                updated_at = get_cell(schema["updated_at"]) or None

                url = ""
                url_idx = schema["url"]
                if url_idx is not None and url_idx < len(cells):
                    url = extract_first_url(split_markdown_row(row)[url_idx])
                if not url and schema["title"] is not None and schema["title"] < len(cells):
                    url = extract_first_url(split_markdown_row(row)[schema["title"]])
                if not url:
                    for cell in split_markdown_row(row):
                        url = extract_first_url(cell)
                        if url:
                            break

                if company and title and url:
                    text = " ".join(part for part in [source_text, company, title, location, updated_at or ""] if part)
                    jobs.append(build_raw_job(company, title, location, url, updated_at, source, text))
                j += 1
            i = j
            continue
        i += 1

    return jobs


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
        out.append(build_raw_job(company, title, location, url, updated_at, source, text))
    return out


def fetch_sources(session: requests.Session, sources: list[dict]) -> list[dict]:
    jobs: list[dict] = []
    for source in sources:
        provider = source.get("provider")
        if not provider:
            provider = "github_readme" if source.get("repo") else "greenhouse" if source.get("url") else ""
        if provider == "github_readme":
            try:
                jobs.extend(fetch_github_repo_jobs(session, source))
            except Exception as exc:
                print(f"[warn] Failed source {source.get('name') or source.get('repo')}: {exc}", file=sys.stderr)
        elif provider == "greenhouse":
            try:
                jobs.extend(fetch_greenhouse_board(session, source))
            except Exception as exc:
                print(f"[warn] Failed source {source.get('name')}: {exc}", file=sys.stderr)
        else:
            print(f"[warn] Skipping unsupported source provider: {provider}", file=sys.stderr)
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
        source = item.get("source", "")
        normalized_source = normalize_source_label(source)
        resolved_date = parse_date_value(item.get("updated_at")) or parse_relative_age(item.get("updated_at"))
        job = Job(
            company=item.get("company", ""),
            title=title.strip(),
            location=item.get("location", "").strip(),
            url=canonicalize_url(item.get("url", "")),
            updated_at=item.get("updated_at"),
            source=normalized_source,
            score=score,
            category=infer_category(title, source),
            discipline=infer_discipline(title),
            terms=infer_terms(title, source),
            listing_id=make_listing_id(item.get("company", ""), title, item.get("url", "")),
            date_posted=resolved_date,
            date_updated=resolved_date,
        )
        key = job.key()
        existing = results.get(key)
        if existing is None or job.score > existing.score:
            results[key] = job

    return sorted(
        results.values(),
        key=lambda j: (-j.score, j.company.lower(), j.title.lower(), j.location.lower()),
    )


def build_discovery_notes() -> str:
    return DISCOVERY_NOTES.format(matrix=build_search_matrix()).strip() + "\n"


def write_outputs(jobs: list[Job], output_dir: Path) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "jobs.json"
    md_path = output_dir / "jobs.md"
    discovery_path = output_dir / "discovery.md"

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
    lines.extend([
        "",
        "## Discovery tips",
        "See `discovery.md` for the ATS search strategy that complements the scraped boards.",
    ])
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    discovery_path.write_text(build_discovery_notes(), encoding="utf-8")

    return json_path, md_path, discovery_path


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
        "--vault-dir",
        default=os.getenv("JB_VAULT_DIR", ""),
        help="Optional path to the jb Obsidian vault root. When provided, syncs Jobs/*.md notes.",
    )
    parser.add_argument(
        "--deactivate-missing-vault-jobs",
        action="store_true",
        default=os.getenv("JB_DEACTIVATE_MISSING", "false").lower() == "true",
        help="Mark unmatched existing vault notes active:false. Default is off to avoid mass churn when source coverage changes.",
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
    all_jobs = filter_jobs(raw_jobs, role_terms=role_terms, cycle_terms=cycle_terms)
    jobs = all_jobs[: args.max_results]

    output_dir = Path(args.output_dir)
    json_path, md_path, discovery_path = write_outputs(jobs, output_dir)

    print(f"Sources checked: {len(sources)}")
    print(f"Raw jobs fetched: {len(raw_jobs)}")
    print(f"Filtered matches: {len(all_jobs)}")
    print(f"Emitted matches: {len(jobs)}")
    print(f"Wrote: {json_path}")
    print(f"Wrote: {md_path}")
    print(f"Wrote: {discovery_path}")

    if args.vault_dir:
        sync_jobs_to_vault(
            all_jobs,
            Path(args.vault_dir),
            deactivate_missing=args.deactivate_missing_vault_jobs,
        )
    else:
        print("Vault sync skipped: JB_VAULT_DIR/--vault-dir not set")

    print()

    for job in jobs[:25]:
        print(f"{job.company}\t{job.title}\t{job.location or 'Unknown'}\t{job.url}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
