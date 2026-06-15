import os
import re
import datetime
import requests
import yaml

FEEDS = [
    {
        "url": "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
        "category": "internship",
        "source": "simplify-internships",
    },
    {
        "url": "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
        "category": "new-grad",
        "source": "simplify-new-grad",
    },
]

# Fields the user owns — preserved across every re-scrape
USER_FIELDS = {"status", "applied_date", "deadline", "notes"}


def sanitize_filename(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    return name.strip(". ")[:80]


def unix_to_date(ts):
    if not ts:
        return None
    return datetime.date.fromtimestamp(ts).isoformat()


_DISCIPLINE_RULES = [
    ("ml",       ["machine learning", "ml engineer", "deep learning", "nlp", "computer vision", "ai engineer", "ai/ml"]),
    ("data",     ["data engineer", "data analyst", "data scien", "analytics", "business intelligence"]),
    ("devops",   ["devops", "site reliability", "platform engineer", "infrastructure", "cloud engineer", " sre "]),
    ("security", ["security", "appsec", "infosec", "cryptograph"]),
    ("hardware", ["hardware", "embedded", "firmware", "fpga", "asic", "electrical engineer"]),
    ("mobile",   ["mobile", "ios engineer", "android engineer"]),
    ("frontend", ["frontend", "front-end", "front end", "ui engineer", "web develop"]),
    ("backend",  ["backend", "back-end", "back end"]),
    ("swe",      ["software eng", "software develop", "swe", "full stack", "fullstack", "programmer", "developer"]),
]


def classify_discipline(title):
    t = title.lower()
    for bucket, keywords in _DISCIPLINE_RULES:
        if any(k in t for k in keywords):
            return bucket
    return "other"


def parse_note(path):
    """Return (frontmatter_dict, body_str) or (None, '') if not parseable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return None, ""

    if not content.startswith("---"):
        return None, content

    parts = content[3:].split("\n---", 1)
    if len(parts) < 2:
        return None, content

    try:
        fm = yaml.safe_load(parts[0])
        body = parts[1].lstrip("\n")
        return fm or {}, body
    except yaml.YAMLError:
        return None, content


def write_note(path, listing, category, source, existing_fm=None, existing_body=""):
    user_vals = {f: existing_fm[f] for f in USER_FIELDS if existing_fm and f in existing_fm}

    fm = {
        "company": listing["company_name"],
        "role": listing["title"],
        "category": category,
        "discipline": classify_discipline(listing["title"]),
        "locations": listing.get("locations", []),
        "terms": listing.get("terms", []),
        "url": listing.get("url", ""),
        "source": source,
        "listing_id": listing["id"],
        "active": listing.get("active", True),
        "date_posted": unix_to_date(listing.get("date_posted")),
        "date_updated": unix_to_date(listing.get("date_updated")),
        # user-owned
        "status": user_vals.get("status", "to-apply"),
        "applied_date": user_vals.get("applied_date"),
        "deadline": user_vals.get("deadline"),
        "notes": user_vals.get("notes"),
    }

    fm_str = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
    note = f"---\n{fm_str}---\n"
    if existing_body:
        note += "\n" + existing_body

    with open(path, "w", encoding="utf-8") as f:
        f.write(note)


def main():
    output_dir = os.environ.get("OUTPUT_DIR", "Jobs")
    os.makedirs(output_dir, exist_ok=True)

    # Index existing notes by listing_id for idempotent merges
    id_to_path = {}
    for fname in os.listdir(output_dir):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(output_dir, fname)
        fm, _ = parse_note(fpath)
        if fm and "listing_id" in fm:
            id_to_path[fm["listing_id"]] = fpath

    seen_ids = set()

    for feed in FEEDS:
        print(f"Fetching {feed['source']}...")
        resp = requests.get(feed["url"], timeout=30)
        resp.raise_for_status()
        listings = resp.json()

        active = [l for l in listings if l.get("active") and l.get("is_visible", True)]
        print(f"  {len(active)} active visible listings")

        for listing in active:
            lid = listing["id"]
            seen_ids.add(lid)

            if lid in id_to_path:
                fpath = id_to_path[lid]
                existing_fm, existing_body = parse_note(fpath)
            else:
                safe = sanitize_filename(listing["company_name"]) + " - " + sanitize_filename(listing["title"])
                fpath = os.path.join(output_dir, f"{safe}.md")
                existing_fm, existing_body = None, ""

            write_note(fpath, listing, feed["category"], feed["source"], existing_fm, existing_body)

    # Mark removed listings as inactive (never delete — preserves application history)
    for lid, fpath in id_to_path.items():
        if lid not in seen_ids:
            fm, body = parse_note(fpath)
            if fm and fm.get("active", True):
                fm["active"] = False
                fm_str = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
                note = f"---\n{fm_str}---\n"
                if body:
                    note += "\n" + body
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(note)
                print(f"  Marked inactive: {os.path.basename(fpath)}")

    print("Done.")


if __name__ == "__main__":
    main()
