#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "joblib", "python-dotenv"]
# ///

import os
import re
import sys
import time
from datetime import UTC, datetime, timedelta

import dotenv
import joblib
import requests

dotenv.load_dotenv()

README_PATH = "README.md"
TABLE_SECTION = "## UN Organizations with Open Source Repositories"

memory = joblib.Memory(".cache/fetch_stats", verbose=0)

# Set in main() before cached functions are called
_headers: dict = {}


class RateLimitError(Exception):
    pass


def check_token() -> str | None:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print(
            "Warning: GITHUB_TOKEN not set. Using unauthenticated API (60 req/hr). "
            "Set GITHUB_TOKEN for higher limits.",
            file=sys.stderr,
        )
    return token


def make_headers(token: str | None) -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": "awesome-united-nations/fetch_stats",
        "X-GitHub-Api-Version": "2022-11-28",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    }


def read_readme() -> str:
    with open(README_PATH) as f:
        return f.read()


def extract_table(content: str) -> tuple[str, str, str]:
    lines = content.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith(TABLE_SECTION):
            start = i + 1
            break
    if start is None:
        raise ValueError(f"Section '{TABLE_SECTION}' not found in README")

    while start < len(lines) and lines[start].strip() == "":
        start += 1

    end = start
    while end < len(lines):
        stripped = lines[end].strip()
        if stripped == "" or (stripped.startswith("##") and end > start):
            break
        end += 1

    pre = "".join(lines[:start])
    table = "".join(lines[start:end])
    post = "".join(lines[end:])
    return table, pre, post


def _split_row(line: str) -> list[str]:
    s = line.strip().strip("|")
    return [c.strip() for c in s.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    return len(cells) >= 2 and all(re.fullmatch(r"-+", c) for c in cells if c)


def _extract_url(cell: str) -> str:
    """Strip [:octocat:](url) formatting back to a raw URL."""
    m = re.match(r'\[:octocat:\]\(([^)]+)\)', cell.strip())
    return m.group(1).strip() if m else cell.strip()


def parse_table(table_text: str) -> list[dict]:
    rows = []
    link_col = 2  # default: old format (Org | Full Name | Link | ...)
    for i, line in enumerate(ln for ln in table_text.splitlines() if ln.strip()):
        cells = _split_row(line)
        if _is_separator_row(cells):
            row_type, cells = "separator", []
        elif i == 0:
            row_type = "header"
            # Detect new format (Link is last col): Org | Full Name | Repos | Stars | Commits | Link
            if len(cells) == 6 and cells[5].strip().lower() == "link":
                link_col = 5
            cells = [cells[0], cells[1], "Link"]
        else:
            row_type = "data"
            if len(cells) == 6:
                raw_link = _extract_url(cells[link_col])
                cells = [cells[0], cells[1], raw_link]
            else:
                cells = [cells[0], cells[1], _extract_url(cells[2]) if len(cells) > 2 else ""]
        rows.append({"type": row_type, "cells": cells})
    return rows


def classify_link(link: str) -> tuple[str, str | None]:
    link = link.strip()
    if not link:
        return "none", None
    if link.startswith("https://github.com/"):
        org = link.removeprefix("https://github.com/").strip("/")
        if org:
            return "github", org
    return "other", None


def handle_rate_limit(response: requests.Response) -> None:
    if response.status_code not in (403, 429):
        return
    reset_ts = int(response.headers.get("X-RateLimit-Reset", 0))
    wait = reset_ts - time.time() + 1
    if 0 < wait <= 300:
        print(f"  Rate limited; sleeping {wait:.0f}s...")
        time.sleep(wait)
    elif wait > 300:
        raise RateLimitError(
            f"Rate limit resets in {wait:.0f}s. Set GITHUB_TOKEN to avoid this."
        )


def _get(url: str, params: dict | None = None) -> requests.Response:
    for attempt in range(2):
        try:
            r = requests.get(url, headers=_headers, params=params, timeout=20)
            handle_rate_limit(r)
            return r
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 0:
                time.sleep(2)
            else:
                raise
    raise RuntimeError("unreachable")


@memory.cache
def _fetch_org_repos(org: str) -> list[dict]:
    url = f"https://api.github.com/orgs/{org}/repos"
    params: dict = {"per_page": 100, "type": "public"}
    repos = []
    while url:
        r = _get(url, params)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        repos.extend(r.json())
        url = r.links.get("next", {}).get("url", "")
        params = {}
    return repos


@memory.cache
def _fetch_commits_1m(org: str, cutoff: str) -> int:
    r = _get(
        "https://api.github.com/search/commits",
        {"q": f"org:{org} committer-date:>{cutoff}", "per_page": 1},
    )
    if r.status_code in (404, 422):
        return 0
    r.raise_for_status()
    return r.json()["total_count"]


def fetch_stats_for_org(org: str) -> tuple[int, int, int]:
    try:
        repos = _fetch_org_repos(org)
        stars = sum(r.get("stargazers_count", 0) for r in repos)
        cutoff = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%d")
        commits = _fetch_commits_1m(org, cutoff)
        return len(repos), stars, commits
    except RateLimitError as e:
        print(f"  Skipping {org}: {e}", file=sys.stderr)
        return -1, -1, -1
    except Exception as e:
        print(f"  Error fetching {org}: {e}", file=sys.stderr)
        return -1, -1, -1


def fetch_all_stats(rows: list[dict]) -> list[dict]:
    github_rows = [
        r
        for r in rows
        if r["type"] == "data"
        and len(r["cells"]) >= 3
        and classify_link(r["cells"][2])[0] == "github"
    ]
    total = len(github_rows)
    done = 0

    for row in rows:
        if row["type"] in ("header", "separator"):
            row["stats"] = None
            continue
        if len(row["cells"]) < 3:
            row["stats"] = ("", "", "")
            continue

        link_type, org = classify_link(row["cells"][2])
        if link_type == "github" and org:
            done += 1
            print(f"Fetching {org} ({done}/{total})...")
            n_repos, stars, commits = fetch_stats_for_org(org)
            row["stats"] = (format_number(n_repos), format_number(stars), format_number(commits))
            time.sleep(0.5)
        else:
            row["stats"] = ("", "", "")

    return rows


def format_number(n: int) -> str:
    if n < 0:
        return "-"
    if n < 1_000:
        return str(n)
    if n < 10_000:
        return f"{n:,}"
    if n < 1_000_000:
        return f"{n // 1000}k"
    return f"{n / 1_000_000:.1f}M"


def _format_link(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    return f"[:octocat:]({url})"


def serialize_table(rows: list[dict]) -> str:
    lines = []
    for row in rows:
        if row["type"] == "separator":
            lines.append("---|---|---|---|---|---")
        elif row["type"] == "header":
            org, full_name, _ = (row["cells"] + ["", "", ""])[:3]
            lines.append(" | ".join([org, full_name, "Repos", "Stars", "Commits (1m)", "Link"]))
        else:
            cells = (list(row["cells"]) + ["", "", ""])[:3]
            org, full_name, link = cells
            stats = list(row["stats"])
            lines.append(" | ".join([org, full_name] + stats + [_format_link(link)]))
    return "\n".join(lines)


def write_readme(content: str) -> None:
    with open(README_PATH, "w") as f:
        f.write(content)


def main() -> None:
    global _headers
    token = check_token()
    _headers = make_headers(token)
    content = read_readme()
    table_text, pre, post = extract_table(content)
    rows = parse_table(table_text)
    rows = fetch_all_stats(rows)
    new_table = serialize_table(rows)
    write_readme(pre + new_table + "\n" + post)
    print("README.md updated.")


if __name__ == "__main__":
    main()
