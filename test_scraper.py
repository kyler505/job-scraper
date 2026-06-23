import tempfile
import unittest
from pathlib import Path

from scraper import Job, PropertyMapping, build_discovery_notes, build_notion_page_properties, write_outputs


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
    def test_discovery_notes_include_ats_search_queries(self):
        notes = build_discovery_notes()
        self.assertIn("site:http://jobs.ashbyhq.com", notes)
        self.assertIn("jobs.lever.co", notes)
        self.assertIn("LinkedIn", notes)

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


if __name__ == "__main__":
    unittest.main()
