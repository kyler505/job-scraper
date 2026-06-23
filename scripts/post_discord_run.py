#!/usr/bin/env python3
"""Post GitHub Actions scrape results to Discord.

The script is intentionally dependency-free so it can run inside GitHub Actions
without extra packages. It supports either:
- DISCORD_WEBHOOK_URL, or
- DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID

If no Discord credentials are configured, it exits quietly.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

MAX_DISCORD_CHARS = 1900


def env(name: str) -> str:
    return os.getenv(name, "").strip()


def truncate(text: str, limit: int = MAX_DISCORD_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def load_jobs_summary(output_dir: Path) -> tuple[int | None, list[dict], str | None]:
    json_path = output_dir / "jobs.json"
    if not json_path.exists():
        return None, [], None
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, [], None
    count = payload.get("count")
    jobs = payload.get("jobs") or []
    generated_at = payload.get("generated_at")
    return count if isinstance(count, int) else None, jobs if isinstance(jobs, list) else [], generated_at


def build_message() -> str:
    run_status = env("RUN_STATUS") or "unknown"
    run_url = env("RUN_URL")
    repo = env("REPO_NAME") or env("GITHUB_REPOSITORY") or "job-scraper"
    workflow = env("WORKFLOW_NAME") or "job-scraper"
    output_dir = Path(env("OUTPUT_DIR") or "outputs")

    count, jobs, generated_at = load_jobs_summary(output_dir)
    status_emoji = {
        "success": "✅",
        "failure": "❌",
        "cancelled": "⚪",
        "skipped": "⚪",
    }.get(run_status, "ℹ️")

    lines = [f"{status_emoji} **{repo}** — {workflow} finished with **{run_status}**"]
    if run_url:
        lines.append(run_url)
    if generated_at:
        lines.append(f"Generated: {generated_at}")
    if count is not None:
        lines.append(f"Matches: {count}")
    elif run_status != "success":
        lines.append("No results file was produced.")

    if jobs:
        lines.append("")
        lines.append("Top matches:")
        for job in jobs[:8]:
            company = str(job.get("company", "Unknown"))
            title = str(job.get("title", "Unknown role"))
            location = str(job.get("location", "")).strip() or "Unknown location"
            url = str(job.get("url", "")).strip()
            lines.append(f"- {company} — {title} ({location})")
            if url:
                lines.append(f"  {url}")

    discovery_path = output_dir / "discovery.md"
    if discovery_path.exists():
        lines.append("")
        lines.append("Discovery tips are in the vault artifact: `discovery.md`.")

    return truncate("\n".join(lines))


def post_webhook(url: str, message: str) -> None:
    payload = json.dumps({"content": message, "allowed_mentions": {"parse": []}}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "job-scraper/discord-notify"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status >= 300:
            raise RuntimeError(f"Discord webhook returned HTTP {response.status}")


def post_bot_message(token: str, channel_id: str, message: str) -> None:
    payload = json.dumps({"content": message, "allowed_mentions": {"parse": []}}).encode("utf-8")
    request = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "job-scraper/discord-notify",
            "Authorization": f"Bot {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status >= 300:
            raise RuntimeError(f"Discord API returned HTTP {response.status}")


def main() -> int:
    webhook_url = env("DISCORD_WEBHOOK_URL")
    bot_token = env("DISCORD_BOT_TOKEN")
    channel_id = env("DISCORD_CHANNEL_ID")

    if not webhook_url and not (bot_token and channel_id):
        print("Discord notification skipped: no webhook or bot token configured")
        return 0

    message = build_message()
    if webhook_url:
        post_webhook(webhook_url, message)
    else:
        post_bot_message(bot_token, channel_id, message)

    print("Discord notification sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
