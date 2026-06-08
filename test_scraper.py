import unittest

from scraper import Job, PropertyMapping, build_notion_page_properties


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


if __name__ == "__main__":
    unittest.main()
