import tempfile
import unittest
from pathlib import Path

from scraper import (
    Job,
    PropertyMapping,
    build_discovery_notes,
    build_notion_page_properties,
    build_search_matrix,
    canonicalize_url,
    filter_jobs,
    parse_frontmatter,
    sync_jobs_to_vault,
    write_outputs,
)


class BuildNotionPagePropertiesTests(unittest.TestCase):
    def test_company_labeled_title_gets_company_name(self):
        job = Job(
            company="OpenAI",
            title="Software Engineer Intern",
            location="San Francisco, CA",
            url="https://example.com/job",
            updated_at=None,
            source="source-board",
            score=42,
        )
        schema = {
            "properties": {
                "Company": {"type": "title"},
                "Role": {"type": "rich_text"},
                "Location": {"type": "rich_text"},
                "Link": {"type": "url"},
            }
        }
        mapping = PropertyMapping(title="Company", role="Role", location="Location", url="Link")

        props = build_notion_page_properties(job, schema, mapping)

        self.assertEqual(props["Company"]["title"][0]["text"]["content"], "OpenAI")
        self.assertEqual(props["Role"]["rich_text"][0]["text"]["content"], "Software Engineer Intern")

    def test_generic_title_falls_back_to_company_and_role(self):
        job = Job(
            company="OpenAI",
            title="Software Engineer Intern",
            location="San Francisco, CA",
            url="https://example.com/job",
            updated_at=None,
            source="source-board",
            score=42,
        )
        schema = {
            "properties": {
                "Job": {"type": "title"},
                "Role": {"type": "rich_text"},
            }
        }
        mapping = PropertyMapping(title="Job", role="Role")

        props = build_notion_page_properties(job, schema, mapping)

        self.assertEqual(props["Job"]["title"][0]["text"]["content"], "OpenAI — Software Engineer Intern")


class DiscoveryNotesTests(unittest.TestCase):
    def test_search_matrix_has_multiple_ats_families(self):
        matrix = build_search_matrix()
        self.assertIn("High-precision startup ATS", matrix)
        self.assertIn("Broader enterprise ATS", matrix)
        self.assertIn("Long-tail ATS / SMB boards", matrix)
        self.assertIn("Role families", matrix)

    def test_discovery_notes_include_ats_search_queries(self):
        notes = build_discovery_notes()
        self.assertIn("site:jobs.ashbyhq.com", notes)
        self.assertIn("site:wd1.myworkdayjobs.com", notes)
        self.assertIn("High-recall query examples", notes)

    def test_write_outputs_creates_discovery_note(self):
        job = Job(
            company="OpenAI",
            title="Software Engineer Intern",
            location="San Francisco, CA",
            url="https://example.com/job",
            updated_at=None,
            source="source-board",
            score=42,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            json_path, md_path, discovery_path = write_outputs([job], out_dir)

            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())
            self.assertTrue(discovery_path.exists())
            self.assertIn("Discovery tips", md_path.read_text(encoding="utf-8"))
            self.assertIn("Google", discovery_path.read_text(encoding="utf-8"))


class VaultSyncTests(unittest.TestCase):
    def test_filter_jobs_enriches_vault_fields(self):
        raw_jobs = [
            {
                "company": "ByteDance",
                "title": "Software Engineer Intern - Developer Infrastructure",
                "location": "San Jose, CA",
                "url": "https://jobs.bytedance.com/en/position/7595707875767699765/detail?utm_source=Simplify&ref=Simplify",
                "updated_at": "18d",
                "source": "SimplifyJobs/Summer2026-Internships",
                "text": "ByteDance Software Engineer Intern Developer Infrastructure San Jose, CA",
            }
        ]

        jobs = filter_jobs(raw_jobs, ["software engineer", "platform", "developer"], ["intern", "new grad"])

        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.category, "internship")
        self.assertEqual(job.discipline, "devops")
        self.assertEqual(job.terms, ["Summer 2026"])
        self.assertEqual(job.source, "simplify-internships")
        self.assertEqual(job.url, "https://jobs.bytedance.com/en/position/7595707875767699765/detail")
        self.assertIsNotNone(job.date_posted)

    def test_sync_jobs_to_vault_preserves_manual_fields_and_body(self):
        job = Job(
            company="ByteDance",
            title="Software Engineer Intern - Developer Infrastructure",
            location="San Jose, CA",
            url="https://jobs.bytedance.com/en/position/7595707875767699765/detail?utm_source=Simplify&ref=Simplify",
            updated_at="18d",
            source="simplify-internships",
            score=18,
            category="internship",
            discipline="devops",
            terms=["Summer 2026"],
            listing_id="listing-1",
            date_posted="2026-06-04",
            date_updated="2026-06-04",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            vault = Path(tmpdir) / "jb"
            jobs_dir = vault / "Jobs"
            jobs_dir.mkdir(parents=True)
            existing = jobs_dir / "ByteDance - Software Engineer Intern - Developer Infrastructure.md"
            existing.write_text(
                """---
company: ByteDance
role: Old Title
category: internship
discipline: swe
locations:
- Old City
terms: []
url: https://jobs.bytedance.com/en/position/7595707875767699765/detail
source: simplify-internships
listing_id: listing-1
active: false
date_posted: '2026-05-01'
date_updated: '2026-05-01'
status: applied
applied_date: '2026-06-10'
deadline: null
notes: keep me
priority: 42.5
---

## Research

keep body
""",
                encoding="utf-8",
            )

            sync_jobs_to_vault([job], vault)
            text = existing.read_text(encoding="utf-8")
            fm = parse_frontmatter(text)
            self.assertEqual(fm["role"], job.title)
            self.assertEqual(fm["locations"], ["San Jose, CA"])
            self.assertEqual(fm["listing_id"], "listing-1")
            self.assertEqual(fm["date_posted"], "2026-06-04")
            self.assertEqual(fm["status"], "applied")
            self.assertEqual(fm["applied_date"], "2026-06-10")
            self.assertEqual(fm["notes"], "keep me")
            self.assertEqual(fm["priority"], 42.5)
            self.assertTrue(fm["active"])
            self.assertIn("## Research", text)
            self.assertIn("keep body", text)

    def test_sync_jobs_to_vault_deactivates_missing_notes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault = Path(tmpdir) / "jb"
            jobs_dir = vault / "Jobs"
            jobs_dir.mkdir(parents=True)
            stale = jobs_dir / "Old.md"
            stale.write_text(
                """---
company: OldCo
role: Old Role
category: internship
discipline: swe
locations:
- Remote
terms: []
url: https://example.com/old?utm_source=x
source: simplify-internships
listing_id: old-1
active: true
status: to-apply
applied_date: null
deadline: null
notes: null
---
""",
                encoding="utf-8",
            )

            sync_jobs_to_vault([], vault, deactivate_missing=True)
            fm = parse_frontmatter(stale.read_text(encoding="utf-8"))
            self.assertFalse(fm["active"])
            self.assertEqual(fm["url"], canonicalize_url("https://example.com/old?utm_source=x"))


if __name__ == "__main__":
    unittest.main()
