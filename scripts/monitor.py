"""
Docs Impact Monitor

Monitors the MystenLabs/sui release-notes-monitor workflow for completed runs,
extracts release notes from processed PRs, uses Claude to identify documentation
impact, generates fixes, and opens PRs with the updates.
"""

import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import requests

# ---------------------------------------------------------------------------
# State tracking — prevents reprocessing the same PR across runs
# ---------------------------------------------------------------------------

STATE_DIR = Path(__file__).parent.parent / "state"
STATE_DIR.mkdir(exist_ok=True)


def _state_file_for_job() -> Path:
    """Return a per-job state file based on MONITORED_REPO."""
    repo_slug = os.environ.get("MONITORED_REPO", "unknown").replace("/", "_").lower()
    return STATE_DIR / f"processed_{repo_slug}.json"


def load_processed_prs() -> set[int]:
    """Load the set of PR numbers already processed by previous runs."""
    path = _state_file_for_job()
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        return set(data.get("processed_prs", []))
    except (json.JSONDecodeError, KeyError):
        return set()


def save_processed_prs(pr_numbers: set[int]):
    """Persist the set of processed PR numbers."""
    path = _state_file_for_job()
    # Merge with existing state in case another job wrote concurrently
    existing = load_processed_prs()
    merged = existing | pr_numbers
    # Keep only the most recent 500 PRs to prevent unbounded growth
    trimmed = sorted(merged, reverse=True)[:500]
    path.write_text(json.dumps({"processed_prs": trimmed}, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MONITORED_REPO = os.environ.get("MONITORED_REPO", "MystenLabs/sui")
MONITORED_WORKFLOW = os.environ.get("MONITORED_WORKFLOW", "release-notes-monitor.yml")
DOCS_LLMS_URL = os.environ.get("DOCS_LLMS_URL", "https://docs.sui.io/llms.txt")
DOCS_BASE_URL = os.environ.get("DOCS_BASE_URL", "https://docs.sui.io")
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "6"))
THIS_REPO = os.environ.get("THIS_REPO", "")

# Slack notification
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "")

# Second Slack channel where a bot posts release notes info.
# This monitor replies to those posts with its outcome (PR link or "no update needed").
SLACK_RELEASE_NOTES_CHANNEL_ID = os.environ.get("SLACK_RELEASE_NOTES_CHANNEL_ID", "")

# When set to "merged_prs", skip workflow run scanning and directly scan
# recently merged PRs. Useful for repos whose release-notes workflow runs
# on PRs (not on a schedule), like Walrus.
SCAN_MODE = os.environ.get("SCAN_MODE", "workflow_runs")  # "workflow_runs" or "merged_prs"

# GitHub Actions output file for passing data between steps
GITHUB_OUTPUT = os.environ.get("GITHUB_OUTPUT", "")

# PR creation config
DOCS_REPO = os.environ.get("DOCS_REPO", "MystenLabs/sui")
DOCS_REPO_PATH_PREFIX = os.environ.get("DOCS_REPO_PATH_PREFIX", "docs/content")
FORK_OWNER = os.environ.get("FORK_OWNER", "")  # GitHub user/org to fork under
CREATE_PRS = os.environ.get("CREATE_PRS", "true").lower() == "true"

# PAT for write operations (fork, push, create PRs) — falls back to GITHUB_TOKEN
DOCS_MONITOR_PAT = os.environ.get("DOCS_MONITOR_PAT", "")

GH_API = "https://api.github.com"

# Read-only headers — uses the Actions-provided GITHUB_TOKEN (works on public repos)
GH_READ_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# Write headers — uses the PAT for fork/push/PR operations on external repos
_write_token = DOCS_MONITOR_PAT or GITHUB_TOKEN
GH_WRITE_HEADERS = {
    "Authorization": f"Bearer {_write_token}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------


def get_recent_workflow_runs() -> list[dict]:
    """Get completed runs of the monitored workflow within the lookback window.

    Paginates through results until we pass the cutoff time.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    url = f"{GH_API}/repos/{MONITORED_REPO}/actions/workflows/{MONITORED_WORKFLOW}/runs"

    recent = []
    page = 1
    while True:
        params = {
            "status": "completed",
            "per_page": 100,
            "branch": "main",
            "page": page,
        }
        resp = requests.get(url, headers=GH_READ_HEADERS, params=params)
        resp.raise_for_status()
        runs = resp.json().get("workflow_runs", [])

        if not runs:
            break

        past_cutoff = False
        for run in runs:
            run_time = datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
            if run_time < cutoff:
                past_cutoff = True
                break
            if run["conclusion"] == "success":
                recent.append(run)

        if past_cutoff:
            break

        page += 1
        # Safety cap — the API returns runs newest-first, so we stop
        # once we've gone past the cutoff or hit a reasonable page limit.
        if page > 20:
            break

    return recent


def get_workflow_run_jobs(run_id: int) -> list[dict]:
    """Get jobs for a workflow run to extract PR numbers from job names."""
    url = f"{GH_API}/repos/{MONITORED_REPO}/actions/runs/{run_id}/jobs"
    params = {"per_page": 100}
    resp = requests.get(url, headers=GH_READ_HEADERS, params=params)
    resp.raise_for_status()
    return resp.json().get("jobs", [])


def extract_pr_numbers_from_run(run_id: int) -> list[int]:
    """Extract PR numbers from the workflow run's matrix jobs."""
    jobs = get_workflow_run_jobs(run_id)
    pr_numbers = []
    for job in jobs:
        name = job.get("name", "")
        # Jobs are named like "Processing PR (12345)"
        match = re.search(r"Processing PR\s*\((\d+)\)", name)
        if match:
            pr_numbers.append(int(match.group(1)))
    return pr_numbers


def get_pr_details(pr_number: int) -> dict:
    """Fetch PR title, body, and author."""
    url = f"{GH_API}/repos/{MONITORED_REPO}/pulls/{pr_number}"
    resp = requests.get(url, headers=GH_READ_HEADERS)
    resp.raise_for_status()
    return resp.json()


def extract_release_notes(pr_body: str) -> str:
    """Extract the release notes section from a PR body."""
    if not pr_body:
        return ""

    patterns = [
        r"(?i)##?\s*release\s*notes?\s*\n(.*?)(?=\n##?\s|\Z)",
        r"(?i)release\s*notes?:\s*\n(.*?)(?=\n##?\s|\Z)",
    ]
    for pattern in patterns:
        match = re.search(pattern, pr_body, re.DOTALL)
        if match:
            notes = match.group(1).strip()
            if notes and notes.lower() not in ("none", "n/a", "no", ""):
                return notes

    return pr_body


def get_merged_prs_by_commits(head_sha: str) -> list[int]:
    """Fallback: find recently merged PRs associated with commits near head_sha."""
    url = f"{GH_API}/repos/{MONITORED_REPO}/commits/{head_sha}/pulls"
    resp = requests.get(url, headers=GH_READ_HEADERS)
    if resp.status_code == 200:
        return [pr["number"] for pr in resp.json() if pr.get("merged_at")]
    return []


def get_recently_merged_prs() -> list[int]:
    """Scan for recently merged PRs directly (used when SCAN_MODE=merged_prs).

    This is for repos like Walrus where the release-notes workflow runs on PRs
    rather than on a schedule, so we can't extract PR numbers from workflow runs.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    pr_numbers = []
    page = 1

    while True:
        url = f"{GH_API}/repos/{MONITORED_REPO}/pulls"
        params = {
            "state": "closed",
            "sort": "updated",
            "direction": "desc",
            "per_page": 100,
            "page": page,
        }
        resp = requests.get(url, headers=GH_READ_HEADERS, params=params)
        resp.raise_for_status()
        pulls = resp.json()

        if not pulls:
            break

        past_cutoff = False
        for pr in pulls:
            if not pr.get("merged_at"):
                continue
            merged_time = datetime.fromisoformat(pr["merged_at"].replace("Z", "+00:00"))
            if merged_time < cutoff:
                past_cutoff = True
                break
            pr_numbers.append(pr["number"])

        if past_cutoff:
            break
        page += 1
        if page > 10:
            break

    return pr_numbers


# ---------------------------------------------------------------------------
# Docs helpers
# ---------------------------------------------------------------------------


def fetch_docs_index() -> str:
    """Fetch the llms.txt docs index.

    Falls back to scanning the docs repo directory tree via the GitHub API
    if the llms.txt URL is unavailable (e.g. 403 for Walrus).
    """
    resp = requests.get(DOCS_LLMS_URL, timeout=30)
    if resp.status_code == 200:
        return resp.text

    print(f"  llms.txt unavailable ({resp.status_code}), building index from repo...")
    return build_docs_index_from_repo()


def build_docs_index_from_repo() -> str:
    """Build a docs index by listing files in the docs repo via GitHub API."""
    index_lines = [f"# {MONITORED_REPO} Documentation Index", ""]

    def list_tree(path: str):
        url = f"{GH_API}/repos/{DOCS_REPO}/contents/{path}"
        resp = requests.get(url, headers=GH_READ_HEADERS, params={"ref": "main"})
        if resp.status_code != 200:
            return
        for item in resp.json():
            if item["type"] == "dir":
                list_tree(item["path"])
            elif item["name"].endswith((".md", ".mdx")):
                # Build a browsable URL
                rel_path = item["path"].replace(DOCS_REPO_PATH_PREFIX + "/", "", 1)
                doc_url = f"{DOCS_BASE_URL}/{rel_path}"
                title = rel_path.replace("/", " > ").replace(".mdx", "").replace(".md", "")
                index_lines.append(f"- [{title}]({doc_url})")

    list_tree(DOCS_REPO_PATH_PREFIX)
    return "\n".join(index_lines)


def fetch_doc_page(url: str) -> str:
    """Fetch a single doc page as markdown."""
    resp = requests.get(url, timeout=30)
    if resp.status_code == 200:
        return resp.text
    return f"[Failed to fetch: {resp.status_code}]"


def doc_url_to_repo_path(doc_url: str) -> str:
    """Convert a docs.sui.io URL to a file path in the source repo.

    Example:
        https://docs.sui.io/develop/objects/dynamic-fields.md
        -> docs/content/develop/objects/dynamic-fields.mdx
    """
    # Strip the base URL to get the relative path
    path = doc_url.replace(DOCS_BASE_URL, "").lstrip("/")

    # The llms.txt URLs end in .md but source files are .mdx
    repo_path = f"{DOCS_REPO_PATH_PREFIX}/{path}"

    return repo_path


def get_repo_file(repo: str, path: str, ref: str = "main") -> dict | None:
    """Fetch a file from a GitHub repo. Returns dict with 'content', 'sha', 'path'."""
    url = f"{GH_API}/repos/{repo}/contents/{path}"
    params = {"ref": ref}
    resp = requests.get(url, headers=GH_READ_HEADERS, params=params)
    if resp.status_code == 200:
        data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return {"content": content, "sha": data["sha"], "path": data["path"]}
    # Try .mdx if .md failed
    if path.endswith(".md") and resp.status_code == 404:
        return get_repo_file(repo, path.replace(".md", ".mdx"), ref)
    return None


# ---------------------------------------------------------------------------
# Claude analysis
# ---------------------------------------------------------------------------

TRIAGE_SYSTEM = """You are a documentation impact analyst for the Sui blockchain project.

Given release notes from merged PRs, determine which changes could affect existing documentation. Focus on:

1. **API changes**: New, modified, or removed API methods/endpoints (JSON-RPC, GraphQL, SDK methods)
2. **Breaking changes**: Anything that changes existing behavior developers rely on
3. **New features**: Significant new capabilities that should be documented
4. **Configuration changes**: Changes to CLI flags, environment variables, config files
5. **Move framework changes**: Changes to standard library modules, object model, transaction types

Do NOT flag deprecations. Ignore any change that is purely a deprecation notice.

For each impactful change, output a JSON array of objects with:
- "pr_number": the PR number
- "change_summary": brief description of the doc-affecting change
- "change_type": one of "api_change", "breaking_change", "new_feature", "config_change", "framework_change"
- "search_terms": array of specific terms to search for in docs (function names, module names, API endpoints, etc.)

If NO changes affect documentation, return an empty array: []

Return ONLY valid JSON, no markdown fencing."""

DOCS_MATCH_SYSTEM = """You are a documentation reviewer for the Sui blockchain project.

Given a specific code change and a list of documentation pages (with URLs and titles from llms.txt), identify which documentation pages are MOST LIKELY to need updates.

Be selective — only flag pages that are clearly relevant. Consider:
- Pages that directly document the changed API/feature
- Tutorial pages that use the changed API
- Reference pages that list the changed items
- Concept pages that explain the changed behavior

Return a JSON array of objects:
- "doc_url": the URL of the affected doc page
- "doc_title": the title/description from the index
- "reason": why this page likely needs updating
- "urgency": "high" (incorrect/broken info), "medium" (missing new info), "low" (minor update needed)

Return ONLY valid JSON, no markdown fencing. If no docs are affected, return []."""

DOC_REVIEW_SYSTEM = """You are a documentation reviewer for the Sui blockchain project.

You are given:
1. A description of a code change from a PR
2. The current content of a documentation page

First, check whether the documentation page ALREADY covers the new feature, method, API, or behavior introduced by the change. If the page already documents it accurately, say "NO_UPDATE_NEEDED" and explain that it is already covered.

If the page does need updating, explain:
- What specific section(s) need updating
- What is currently wrong or missing
- What the update should say (brief suggestion)

Be precise — reference specific headings, code examples, or paragraphs.
If the page does NOT need updating for this change, say "NO_UPDATE_NEEDED" and briefly explain why.

IMPORTANT: Never suggest adding deprecation warnings, deprecation notices, or any mention of deprecations. Do not flag deprecated features or suggest marking anything as deprecated.
"""

DOC_EDIT_SYSTEM = """You are a technical writer for the Sui blockchain project.

You are given:
1. A description of a code change from a merged PR
2. A review explaining what needs to change in the documentation
3. The FULL current content of the source documentation file (mdx/md format)

Your job is to produce the COMPLETE updated file content with the necessary changes applied.

Rules:
- Make ONLY the changes needed to address the code change. Do not rewrite or reorganize unrelated content.
- Preserve all existing formatting, frontmatter, imports, and MDX components exactly.
- For new features: add documentation in the appropriate section, matching the style of surrounding content.
- For API changes: update signatures, parameters, return types, and examples as needed.
- NEVER add deprecation warnings, deprecation notices, :::caution blocks about deprecations, or any mention of deprecations.
- NEVER modify code blocks or code examples. Only update surrounding text, descriptions, and explanations.

Return ONLY the complete updated file content. No explanations, no markdown fencing around the whole file.

## Sui Documentation Style Guide

All edits MUST comply with the following style guide rules:

### Voice and Tone
- Use active voice, second person ("you"), and present tense.
- Use simple words and concise sentences. Prefer plain language over complex phrasing.
- Write for a global audience. Avoid jargon, slang, idioms, and cultural references.
- Use "might" instead of "may". Use "through" instead of "via". Use "because" instead of "since".
- Do not start sentences with "Please note" or "Note."
- Do not use exclamation marks.

### Spelling and Grammar
- Use US English spelling.
- Avoid Latin abbreviations (i.e., etc., et al.). Use "for example" or complete phrases.
- Use the Oxford (serial) comma.
- Do not write out numbers for quantities (use "24 files" not "twenty-four files").
- Do not use quotation marks except for "Hello, World!".
- Do not use ampersands (&) — write "and".
- Do not use em dashes. Rewrite using commas, parentheses, or split into two sentences.

### Capitalization
- Title case for page titles: capitalize all words except short conjunctions/prepositions (a, an, and, but, for, in, or, so, to, with, yet), unless first or last word.
- Sentence case for section headings, table cells, list items, captions.
- Always capitalize: Sui, SUI, CLI, SDK, Devnet, Testnet, Mainnet, Localnet, GraphQL RPC, ID, and other proper nouns/product names listed in the style guide.
- Always lowercase: gas, object, transaction, transfer, validator, wallet, smart contract, onchain, offchain, epoch, kiosk (instance).
- Never hyphenate: key pair, layer 1, offchain, onchain, open source, use case.

### Formatting
- Use bold for UI elements (buttons, menus, field labels).
- Use bold for term definitions in lists (term followed by colon).
- Do not use italic text.
- Use inline code (backticks) for: function names, object names, CLI commands, file names, file paths, variable names.
- Console commands in codeblocks must start with `$`.
- Use `:::info` for neutral context, `:::tip` for best practices. Do NOT add `:::caution` blocks for deprecation warnings.

### Structure
- H1 (#) is reserved for page title only.
- H2 (##) for top-level sections. H3 (###) for sub-topics. H4 (####) for short-form content.
- Do not stack headings without body text between them.
- Introduce lists with a short description ending in a colon.
- Use numbered lists for sequences, bulleted lists for related items.

### Links and References
- Use full relative links when linking to topics on docs.sui.io.
- Use descriptive text for links, never use a URL as link text.

### Terminology
- Use "app" rather than "dApp".
- Use "recovery passphrase" not "mnemonic".
- Spell out acronyms on first use, then use the acronym (except CLI and SDK, which are always acronyms).
"""


NO_UPDATE_REASON_SYSTEM = """You are a documentation impact analyst for the Sui blockchain project.

You have already determined that a PR's release notes do NOT require documentation updates. Now explain WHY in a concise Slack message (3-5 sentences max).

Your explanation must:
1. Reference the specific PR by number and title.
2. Briefly describe what the PR changed.
3. Explain why these changes do not affect any user-facing documentation (e.g., internal refactor, performance optimization with no API change, test-only change, backend plumbing with no developer-visible effect).
4. If relevant, mention which docs pages you considered and why they don't need updates.

Keep it concise and factual. No filler. Write in plain text suitable for a Slack thread reply."""


def generate_no_update_reason(pr: dict) -> str:
    """Ask Claude to explain why a PR does not require documentation updates."""
    try:
        content = (
            f"PR #{pr['number']}: {pr['title']}\n"
            f"URL: {pr.get('html_url', f'https://github.com/{MONITORED_REPO}/pull/{pr[\"number\"]}')}\n"
            f"Author: {pr.get('author', 'unknown')}\n"
            f"Release Notes:\n{pr.get('release_notes', '(none)')}\n"
        )
        message = client.messages.create(
            model="claude-sonnet-4-6",
            temperature=0,
            max_tokens=512,
            system=NO_UPDATE_REASON_SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
        return message.content[0].text.strip()
    except Exception as e:
        print(f"  [reason] Failed to generate reason for PR #{pr['number']}: {e}")
        return f"No documentation updates needed for PR #{pr['number']}."


def triage_release_notes(prs_with_notes: list[dict]) -> list[dict]:
    """Ask Claude which release notes affect documentation."""
    pr_summaries = []
    for pr in prs_with_notes:
        pr_summaries.append(
            f"PR #{pr['number']}: {pr['title']}\n"
            f"Author: {pr['author']}\n"
            f"Release Notes:\n{pr['release_notes']}\n"
        )

    content = "\n---\n".join(pr_summaries)
    if not content.strip():
        return []

    message = client.messages.create(
        model="claude-sonnet-4-6",
        temperature=0,
        max_tokens=4096,
        system=TRIAGE_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )

    response_text = message.content[0].text.strip()
    if response_text.startswith("```"):
        response_text = re.sub(r"^```(?:json)?\n?", "", response_text)
        response_text = re.sub(r"\n?```$", "", response_text)

    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        print(f"WARNING: Could not parse triage response:\n{response_text}")
        return []


def find_affected_docs(change: dict, docs_index: str) -> list[dict]:
    """Ask Claude which doc pages are affected by a specific change."""
    message = client.messages.create(
        model="claude-sonnet-4-6",
        temperature=0,
        max_tokens=4096,
        system=DOCS_MATCH_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"## Change\n"
                    f"PR #{change['pr_number']}: {change['change_summary']}\n"
                    f"Type: {change['change_type']}\n"
                    f"Search terms: {', '.join(change.get('search_terms', []))}\n\n"
                    f"## Documentation Index (llms.txt)\n{docs_index}"
                ),
            }
        ],
    )

    response_text = message.content[0].text.strip()
    if response_text.startswith("```"):
        response_text = re.sub(r"^```(?:json)?\n?", "", response_text)
        response_text = re.sub(r"\n?```$", "", response_text)

    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        print(f"WARNING: Could not parse docs match response:\n{response_text}")
        return []


def review_doc_page(change: dict, doc_url: str, doc_content: str) -> str:
    """Ask Claude to review a specific doc page against a change."""
    message = client.messages.create(
        model="claude-sonnet-4-6",
        temperature=0,
        max_tokens=2048,
        system=DOC_REVIEW_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"## Code Change\n"
                    f"PR #{change['pr_number']}: {change['change_summary']}\n"
                    f"Type: {change['change_type']}\n\n"
                    f"## Documentation Page: {doc_url}\n\n"
                    f"{doc_content[:15000]}"
                ),
            }
        ],
    )
    return message.content[0].text.strip()


def generate_doc_edit(change: dict, review: str, file_content: str) -> str:
    """Ask Claude to produce the updated file content."""
    message = client.messages.create(
        model="claude-sonnet-4-6",
        temperature=0,
        max_tokens=16384,
        system=DOC_EDIT_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"## Code Change\n"
                    f"PR #{change['pr_number']}: {change['change_summary']}\n"
                    f"Type: {change['change_type']}\n\n"
                    f"## Review Notes\n{review}\n\n"
                    f"## Current File Content\n{file_content}"
                ),
            }
        ],
    )
    return message.content[0].text.strip()


# ---------------------------------------------------------------------------
# GitHub PR creation
# ---------------------------------------------------------------------------


def ensure_fork(upstream_repo: str, fork_owner: str) -> str:
    """Ensure a fork exists. Returns the fork's full name (owner/repo)."""
    repo_name = upstream_repo.split("/")[1]
    fork_full = f"{fork_owner}/{repo_name}"

    # Check if fork already exists
    resp = requests.get(f"{GH_API}/repos/{fork_full}", headers=GH_READ_HEADERS)
    if resp.status_code == 200:
        print(f"    Fork exists: {fork_full}")
        return fork_full

    # Create fork — first try without organization (forks to the PAT owner's account).
    # If FORK_OWNER is an org, retry with the organization field.
    print(f"    Creating fork of {upstream_repo}...")
    resp = requests.post(
        f"{GH_API}/repos/{upstream_repo}/forks",
        headers=GH_WRITE_HEADERS,
        json={},
    )
    # If the PAT owner differs from FORK_OWNER (i.e. FORK_OWNER is an org), retry
    if resp.status_code == 422:
        resp = requests.post(
            f"{GH_API}/repos/{upstream_repo}/forks",
            headers=GH_WRITE_HEADERS,
            json={"organization": fork_owner},
        )
    if resp.status_code in (200, 202):
        fork_full = resp.json()["full_name"]
        print(f"    Fork created: {fork_full}")
        # Wait for fork to be ready
        for _ in range(10):
            time.sleep(3)
            check = requests.get(f"{GH_API}/repos/{fork_full}", headers=GH_READ_HEADERS)
            if check.status_code == 200:
                break
        return fork_full
    else:
        raise RuntimeError(f"Failed to create fork: {resp.status_code} {resp.text}")


def sync_fork(fork_repo: str, upstream_repo: str, branch: str = "main"):
    """Sync the fork's default branch with upstream."""
    resp = requests.post(
        f"{GH_API}/repos/{fork_repo}/merge-upstream",
        headers=GH_WRITE_HEADERS,
        json={"branch": branch},
    )
    if resp.status_code == 200:
        print(f"    Fork synced with upstream {branch}")
    else:
        print(f"    Fork sync response: {resp.status_code} (may already be up to date)")


def create_branch(repo: str, branch_name: str, from_branch: str = "main") -> bool:
    """Create a new branch in the repo."""
    # Get the SHA of the source branch
    resp = requests.get(
        f"{GH_API}/repos/{repo}/git/ref/heads/{from_branch}",
        headers=GH_READ_HEADERS,
    )
    if resp.status_code != 200:
        print(f"    Failed to get ref for {from_branch}: {resp.status_code}")
        return False

    sha = resp.json()["object"]["sha"]

    # Create the new branch
    resp = requests.post(
        f"{GH_API}/repos/{repo}/git/refs",
        headers=GH_WRITE_HEADERS,
        json={"ref": f"refs/heads/{branch_name}", "sha": sha},
    )
    if resp.status_code == 201:
        print(f"    Branch created: {branch_name}")
        return True
    elif resp.status_code == 422:
        # Branch already exists — update it to the latest SHA
        resp = requests.patch(
            f"{GH_API}/repos/{repo}/git/refs/heads/{branch_name}",
            headers=GH_WRITE_HEADERS,
            json={"sha": sha, "force": True},
        )
        if resp.status_code == 200:
            print(f"    Branch reset: {branch_name}")
            return True
    print(f"    Failed to create branch: {resp.status_code} {resp.text}")
    return False


def commit_file(
    repo: str,
    path: str,
    content: str,
    message: str,
    branch: str,
    file_sha: str | None = None,
) -> bool:
    """Create or update a file in the repo via the Contents API."""
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if file_sha:
        payload["sha"] = file_sha

    resp = requests.put(
        f"{GH_API}/repos/{repo}/contents/{path}",
        headers=GH_WRITE_HEADERS,
        json=payload,
    )
    if resp.status_code in (200, 201):
        print(f"    Committed: {path}")
        return True
    print(f"    Failed to commit {path}: {resp.status_code} {resp.text}")
    return False


def create_pull_request(
    upstream_repo: str,
    fork_owner: str,
    branch: str,
    title: str,
    body: str,
    base: str = "main",
) -> str | None:
    """Open a PR from fork:branch to upstream:base. Returns the PR URL."""
    resp = requests.post(
        f"{GH_API}/repos/{upstream_repo}/pulls",
        headers=GH_WRITE_HEADERS,
        json={
            "title": title,
            "body": body,
            "head": f"{fork_owner}:{branch}",
            "base": base,
        },
    )
    if resp.status_code == 201:
        pr_url = resp.json()["html_url"]
        print(f"    PR created: {pr_url}")
        return pr_url
    print(f"    Failed to create PR: {resp.status_code} {resp.text}")
    return None


# ---------------------------------------------------------------------------
# Issue creation (fallback when PRs are disabled)
# ---------------------------------------------------------------------------


def create_github_issue(title: str, body: str, labels: list[str] | None = None):
    """Create an issue in THIS repo."""
    if not THIS_REPO:
        print(f"ISSUE (dry-run, no THIS_REPO set):\n  Title: {title}\n  Body:\n{body}\n")
        return

    url = f"{GH_API}/repos/{THIS_REPO}/issues"
    payload = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    resp = requests.post(url, headers=GH_WRITE_HEADERS, json=payload)
    if resp.status_code == 201:
        issue_url = resp.json()["html_url"]
        print(f"Created issue: {issue_url}")
    else:
        print(f"Failed to create issue: {resp.status_code} {resp.text}")


# ---------------------------------------------------------------------------
# Slack & output helpers
# ---------------------------------------------------------------------------


def set_github_output(key: str, value: str):
    """Write a key=value pair to $GITHUB_OUTPUT for use in subsequent steps."""
    if GITHUB_OUTPUT:
        with open(GITHUB_OUTPUT, "a") as f:
            f.write(f"{key}={value}\n")


def send_slack_notification(pr_url: str, summary: str):
    """Post a notification to Slack via webhook."""
    if not SLACK_WEBHOOK_URL:
        print("  [slack] No SLACK_WEBHOOK_URL set, skipping notification")
        return

    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"Docs Impact Monitor — {MONITORED_REPO}",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": summary,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*<{pr_url}|View PR>*",
                },
            },
        ],
    }

    resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
    if resp.status_code == 200:
        print(f"  [slack] Notification sent")
    else:
        print(f"  [slack] Failed: {resp.status_code} {resp.text}")


def ensure_bot_in_channel(channel_id: str, label: str = ""):
    """Join a Slack channel if the bot is not already a member."""
    if not SLACK_BOT_TOKEN or not channel_id:
        return
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        "https://slack.com/api/conversations.join",
        headers=headers,
        json={"channel": channel_id},
        timeout=10,
    )
    data = resp.json()
    if data.get("ok"):
        print(f"  [slack{label}] Joined channel {channel_id}")
    elif data.get("error") == "already_in_channel":
        pass  # already joined, no action needed
    else:
        print(f"  [slack{label}] Failed to join channel {channel_id}: {data.get('error', resp.text)}")


def find_slack_message_for_pr(pr_number: int) -> str | None:
    """Search the Slack channel for a message about a merged PR. Returns the message ts."""
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL_ID:
        return None

    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}

    # Ensure bot is in the channel before reading history
    ensure_bot_in_channel(SLACK_CHANNEL_ID)

    # Search recent history for a message mentioning this PR number
    resp = requests.get(
        "https://slack.com/api/conversations.history",
        headers=headers,
        params={"channel": SLACK_CHANNEL_ID, "limit": 200},
        timeout=15,
    )
    if resp.status_code != 200 or not resp.json().get("ok"):
        print(f"  [slack] Failed to fetch channel history: {resp.text}")
        return None

    pr_patterns = [f"#{pr_number}", f"/{pr_number}", f"({pr_number})"]
    for msg in resp.json().get("messages", []):
        text = msg.get("text", "")
        for block in msg.get("blocks", []):
            if block.get("type") == "section":
                txt = block.get("text", {})
                if isinstance(txt, dict):
                    text += " " + txt.get("text", "")
        # Also check attachments
        for att in msg.get("attachments", []):
            text += " " + att.get("text", "") + " " + att.get("pretext", "")
            text += " " + att.get("title", "")

        if any(p in text for p in pr_patterns):
            return msg["ts"]

    return None


def reply_to_slack_thread(thread_ts: str, text: str):
    """Post a threaded reply to a Slack message."""
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL_ID:
        return

    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "channel": SLACK_CHANNEL_ID,
        "thread_ts": thread_ts,
        "text": text,
    }
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers=headers,
        json=payload,
        timeout=10,
    )
    data = resp.json()
    if data.get("ok"):
        print(f"  [slack] Thread reply sent")
    else:
        print(f"  [slack] Thread reply failed: {data.get('error', resp.text)}")


def post_slack_webhook_message(text: str, channel_context: str = ""):
    """Post a standalone message via the Slack webhook as a fallback when bot token is unavailable."""
    if not SLACK_WEBHOOK_URL:
        return
    payload = {"text": text}
    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code == 200:
            print(f"  [slack] Webhook fallback message sent{f' ({channel_context})' if channel_context else ''}")
        else:
            print(f"  [slack] Webhook fallback failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"  [slack] Webhook fallback error: {e}")


def notify_slack_for_pr(pr_number: int, docs_pr_url: str | None, reason: str = ""):
    """Find the Slack message for a source PR and reply with the outcome in-thread."""
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL_ID:
        return

    if docs_pr_url:
        text = f"📝 Docs update PR opened for PR #{pr_number}: {docs_pr_url}"
    elif reason:
        text = f"✅ No docs updates needed for PR #{pr_number}.\n\n{reason}"
    else:
        text = f"✅ No docs updates needed for PR #{pr_number}."

    thread_ts = find_slack_message_for_pr(pr_number)
    if not thread_ts:
        print(f"  [slack] No message found for PR #{pr_number}, skipping thread reply")
        return

    react_to_slack_message(SLACK_CHANNEL_ID, thread_ts, "white_check_mark")
    reply_to_slack_thread(thread_ts, text)


# ---------------------------------------------------------------------------
# Release notes channel — reply to bot posts in a second Slack channel
# ---------------------------------------------------------------------------


def find_release_notes_message_for_pr(pr_number: int) -> str | None:
    """Search the release notes Slack channel for a bot post about a PR.

    Returns the message ts for threading, or None if not found.
    """
    if not SLACK_BOT_TOKEN or not SLACK_RELEASE_NOTES_CHANNEL_ID:
        return None

    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}

    # Ensure bot is in the channel before reading history
    ensure_bot_in_channel(SLACK_RELEASE_NOTES_CHANNEL_ID, label="-rn")

    resp = requests.get(
        "https://slack.com/api/conversations.history",
        headers=headers,
        params={"channel": SLACK_RELEASE_NOTES_CHANNEL_ID, "limit": 200},
        timeout=15,
    )
    if resp.status_code != 200 or not resp.json().get("ok"):
        print(f"  [slack-rn] Failed to fetch release notes channel history: {resp.text}")
        return None

    pr_patterns = [f"#{pr_number}", f"/{pr_number}", f"({pr_number})", f"PR {pr_number}"]
    for msg in resp.json().get("messages", []):
        text = msg.get("text", "")
        # Check blocks
        for block in msg.get("blocks", []):
            if block.get("type") == "section":
                txt = block.get("text", {})
                if isinstance(txt, dict):
                    text += " " + txt.get("text", "")
            elif block.get("type") == "rich_text":
                for element in block.get("elements", []):
                    for sub in element.get("elements", []):
                        if sub.get("type") == "text":
                            text += " " + sub.get("text", "")
                        elif sub.get("type") == "link":
                            text += " " + sub.get("url", "")
        # Check attachments
        for att in msg.get("attachments", []):
            text += " " + att.get("text", "")
            text += " " + att.get("pretext", "")
            text += " " + att.get("title", "")
            text += " " + att.get("fallback", "")

        if any(p in text for p in pr_patterns):
            return msg["ts"]

    return None


def reply_to_release_notes_thread(thread_ts: str, text: str):
    """Post a threaded reply in the release notes Slack channel."""
    if not SLACK_BOT_TOKEN or not SLACK_RELEASE_NOTES_CHANNEL_ID:
        return

    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "channel": SLACK_RELEASE_NOTES_CHANNEL_ID,
        "thread_ts": thread_ts,
        "text": text,
    }
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers=headers,
        json=payload,
        timeout=10,
    )
    data = resp.json()
    if data.get("ok"):
        print(f"  [slack-rn] Release notes thread reply sent")
    else:
        print(f"  [slack-rn] Release notes thread reply failed: {data.get('error', resp.text)}")


def react_to_slack_message(channel_id: str, timestamp: str, emoji: str, label: str = ""):
    """Add an emoji reaction to a Slack message."""
    if not SLACK_BOT_TOKEN:
        return
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        "https://slack.com/api/reactions.add",
        headers=headers,
        json={"channel": channel_id, "timestamp": timestamp, "name": emoji},
        timeout=10,
    )
    data = resp.json()
    if data.get("ok"):
        print(f"  [slack{label}] Reacted with :{emoji}:")
    elif data.get("error") == "already_reacted":
        pass  # already reacted, no action needed
    else:
        print(f"  [slack{label}] Reaction failed: {data.get('error', resp.text)}")


def notify_release_notes_channel_for_pr(pr_number: int, docs_pr_url: str | None, reason: str = ""):
    """Find the release notes bot post for a PR and reply with the docs monitor outcome in-thread."""
    if not SLACK_BOT_TOKEN or not SLACK_RELEASE_NOTES_CHANNEL_ID:
        return

    if docs_pr_url:
        text = f"📝 Docs update PR opened for PR #{pr_number}: {docs_pr_url}"
    elif reason:
        text = f"✅ No docs updates needed for PR #{pr_number}.\n\n{reason}"
    else:
        text = f"✅ No docs updates needed for PR #{pr_number}."

    thread_ts = find_release_notes_message_for_pr(pr_number)
    if not thread_ts:
        print(f"  [slack-rn] No release notes message found for PR #{pr_number}")
        return

    react_to_slack_message(SLACK_RELEASE_NOTES_CHANNEL_ID, thread_ts, "white_check_mark", label="-rn")
    reply_to_release_notes_thread(thread_ts, text)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("=== Docs Impact Monitor ===")
    print(f"Monitoring: {MONITORED_REPO}")
    print(f"Scan mode: {SCAN_MODE}")
    if SCAN_MODE == "workflow_runs":
        print(f"Workflow: {MONITORED_WORKFLOW}")
    print(f"Lookback: {LOOKBACK_HOURS} hours")
    print(f"PR creation: {'enabled' if CREATE_PRS else 'disabled (issue-only mode)'}")
    if CREATE_PRS:
        print(f"Fork owner: {FORK_OWNER or '(not set — PR creation will be skipped)'}")
    print()

    # Step 1-2: Get PR numbers (mode-dependent)
    all_pr_numbers = set()

    if SCAN_MODE == "merged_prs":
        print("Step 1: Scanning recently merged PRs...")
        pr_nums = get_recently_merged_prs()
        all_pr_numbers.update(pr_nums)
        print(f"Found {len(pr_nums)} recently merged PR(s)")
    else:
        print("Step 1: Checking for recent workflow runs...")
        runs = get_recent_workflow_runs()
        if not runs:
            print("No recent successful runs found. Nothing to do.")
            return

        print(f"Found {len(runs)} recent run(s)")

        print("\nStep 2: Extracting PR numbers from workflow runs...")
        for run in runs:
            pr_nums = extract_pr_numbers_from_run(run["id"])
            if not pr_nums:
                pr_nums = get_merged_prs_by_commits(run["head_sha"])
            all_pr_numbers.update(pr_nums)
            print(f"  Run #{run['id']}: {len(pr_nums)} PRs")

    if not all_pr_numbers:
        print("No PRs found. Nothing to do.")
        return

    print(f"Total unique PRs: {len(all_pr_numbers)}")

    # Filter out PRs already processed in previous runs
    already_processed = load_processed_prs()
    new_pr_numbers = all_pr_numbers - already_processed
    if not new_pr_numbers:
        print(f"All {len(all_pr_numbers)} PR(s) already processed in previous runs. Nothing to do.")
        return
    if len(new_pr_numbers) < len(all_pr_numbers):
        print(f"Skipping {len(all_pr_numbers) - len(new_pr_numbers)} already-processed PR(s), {len(new_pr_numbers)} new")
    all_pr_numbers = new_pr_numbers

    # Step 3: Fetch PR details and release notes
    print("\nStep 3: Fetching PR details and release notes...")
    prs_with_notes = []
    for pr_num in sorted(all_pr_numbers):
        try:
            pr = get_pr_details(pr_num)
            notes = extract_release_notes(pr.get("body", "") or "")
            if notes:
                prs_with_notes.append(
                    {
                        "number": pr_num,
                        "title": pr.get("title", ""),
                        "author": pr.get("user", {}).get("login", "unknown"),
                        "release_notes": notes,
                        "html_url": pr.get("html_url", ""),
                    }
                )
                print(f"  PR #{pr_num}: {pr.get('title', '')[:60]}...")
        except Exception as e:
            print(f"  PR #{pr_num}: Failed to fetch ({e})")

    if not prs_with_notes:
        print("No PRs with release notes found. Nothing to do.")
        no_notes_reason = "This PR does not contain a release notes section, so no documentation impact assessment is needed."
        if SLACK_BOT_TOKEN and SLACK_CHANNEL_ID:
            for pr_num in sorted(all_pr_numbers):
                notify_slack_for_pr(pr_num, None, reason=no_notes_reason)
        for pr_num in sorted(all_pr_numbers):
            notify_release_notes_channel_for_pr(pr_num, None, reason=no_notes_reason)
        save_processed_prs(all_pr_numbers)
        return

    print(f"\nFound {len(prs_with_notes)} PR(s) with release notes")

    # Step 4: Triage with Claude — which changes affect docs?
    print("\nStep 4: Triaging release notes with Claude...")
    impactful_changes = triage_release_notes(prs_with_notes)

    if not impactful_changes:
        print("No doc-affecting changes found. All clear!")
        # Generate a detailed reason for each PR explaining why no docs update is needed
        pr_reasons: dict[int, str] = {}
        for pr in prs_with_notes:
            print(f"  Generating reason for PR #{pr['number']}...")
            pr_reasons[pr["number"]] = generate_no_update_reason(pr)
        if SLACK_BOT_TOKEN and SLACK_CHANNEL_ID:
            for pr_num in sorted(all_pr_numbers):
                reason = pr_reasons.get(pr_num, "No release notes with documentation impact found.")
                notify_slack_for_pr(pr_num, None, reason=reason)
        for pr_num in sorted(all_pr_numbers):
            reason = pr_reasons.get(pr_num, "No release notes with documentation impact found.")
            notify_release_notes_channel_for_pr(pr_num, None, reason=reason)
        save_processed_prs(all_pr_numbers)
        return

    print(f"Found {len(impactful_changes)} potentially doc-affecting change(s)")
    for change in impactful_changes:
        print(f"  - PR #{change['pr_number']}: {change['change_summary']} [{change['change_type']}]")

    # Step 5: Fetch the docs index
    print("\nStep 5: Fetching docs index...")
    docs_index = fetch_docs_index()
    print(f"Docs index: {len(docs_index)} chars, ~{docs_index.count(chr(10))} lines")

    # Step 6: For each impactful change, find affected docs and generate edits
    print("\nStep 6: Identifying affected documentation pages...")
    all_file_edits = []  # list of {change, source_pr, doc_url, repo_path, review, new_content, file_sha}
    issues_to_create = []

    for change in impactful_changes:
        print(f"\n  Analyzing PR #{change['pr_number']}: {change['change_summary']}")
        affected_docs = find_affected_docs(change, docs_index)

        if not affected_docs:
            print("    No docs affected")
            continue

        print(f"    Found {len(affected_docs)} potentially affected page(s)")

        reviewed_docs = []
        for doc in affected_docs[:5]:
            doc_url = doc["doc_url"]
            print(f"    Reviewing: {doc_url}")
            try:
                content = fetch_doc_page(doc_url)
                review = review_doc_page(change, doc_url, content)
                if "NO_UPDATE_NEEDED" not in review:
                    reviewed_docs.append(
                        {
                            "url": doc_url,
                            "title": doc.get("doc_title", ""),
                            "urgency": doc.get("urgency", "medium"),
                            "reason": doc.get("reason", ""),
                            "review": review,
                        }
                    )
                else:
                    print(f"      -> No update needed")
            except Exception as e:
                print(f"      -> Failed to review ({e})")

        if not reviewed_docs:
            continue

        source_pr = next(
            (p for p in prs_with_notes if p["number"] == change["pr_number"]),
            None,
        )

        # Step 7: Generate actual file edits if PR creation is enabled
        if CREATE_PRS and FORK_OWNER:
            print(f"\n  Step 7: Generating file edits for PR #{change['pr_number']}...")
            for doc in reviewed_docs:
                repo_path = doc_url_to_repo_path(doc["url"])
                print(f"    Fetching source: {repo_path}")

                try:
                    file_data = get_repo_file(DOCS_REPO, repo_path)
                except Exception as e:
                    print(f"      -> Error fetching file: {e}")
                    continue

                if not file_data:
                    print(f"      -> Source file not found in repo (tried .md and .mdx), skipping")
                    continue

                print(f"      Found: {file_data['path']} ({len(file_data['content'])} chars)")
                print(f"      Generating updated content with Claude...")
                try:
                    new_content = generate_doc_edit(
                        change, doc["review"], file_data["content"]
                    )
                except Exception as e:
                    print(f"      -> Claude edit generation failed: {e}")
                    continue

                if new_content and new_content != file_data["content"]:
                    all_file_edits.append(
                        {
                            "change": change,
                            "source_pr": source_pr,
                            "doc_url": doc["url"],
                            "repo_path": file_data["path"],
                            "review": doc["review"],
                            "urgency": doc["urgency"],
                            "new_content": new_content,
                            "file_sha": file_data["sha"],
                        }
                    )
                    print(f"      -> Edit generated ({len(new_content)} chars)")
                else:
                    print(f"      -> No changes produced (content identical)")
        else:
            print(f"\n  [skip Step 7] CREATE_PRS={CREATE_PRS}, FORK_OWNER='{FORK_OWNER}'")

        # Collect for issue creation (used as fallback or alongside PRs)
        issues_to_create.append(
            {
                "change": change,
                "source_pr": source_pr,
                "docs": reviewed_docs,
            }
        )

    # Step 8: Create PR or fall back to issues
    pr_created = False
    pr_url = None
    committed_files = []

    if CREATE_PRS and FORK_OWNER:
        # Deduplicate: if multiple changes affect the same file, keep only the last
        # edit (which was generated with the most recent file state).
        seen_paths = {}
        for edit in all_file_edits:
            path = edit["repo_path"]
            if path in seen_paths:
                print(f"  [dedup] {path} edited by multiple PRs — merging into single edit")
            seen_paths[path] = edit
        all_file_edits = list(seen_paths.values())

        print(f"\n  [debug] all_file_edits count: {len(all_file_edits)}")
        print(f"  [debug] issues_to_create count: {len(issues_to_create)}")
        if all_file_edits:
            print(f"\nStep 8: Creating PR with {len(all_file_edits)} file edit(s)...")

            try:
                fork_repo = ensure_fork(DOCS_REPO, FORK_OWNER)
                sync_fork(fork_repo, DOCS_REPO)

                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                branch_name = f"docs-monitor/auto-update-{timestamp}"
                if not create_branch(fork_repo, branch_name):
                    raise RuntimeError("Failed to create branch")

                committed_files = []
                for edit in all_file_edits:
                    fork_file = get_repo_file(fork_repo, edit["repo_path"], ref=branch_name)
                    file_sha = fork_file["sha"] if fork_file else edit["file_sha"]

                    success = commit_file(
                        repo=fork_repo,
                        path=edit["repo_path"],
                        content=edit["new_content"],
                        message=(
                            f"docs: update {edit['repo_path']} for PR #{edit['change']['pr_number']}\n\n"
                            f"Automated update based on: {edit['change']['change_summary']}"
                        ),
                        branch=branch_name,
                        file_sha=file_sha,
                    )
                    if success:
                        committed_files.append(edit)

                if committed_files:
                    pr_body_parts = [
                        "## Summary",
                        "",
                        "Automated documentation updates triggered by recent release notes.",
                        "",
                        "### Changes",
                        "",
                    ]

                    by_pr: dict[int, list] = {}
                    for edit in committed_files:
                        pr_num = edit["change"]["pr_number"]
                        by_pr.setdefault(pr_num, []).append(edit)

                    for pr_num, edits in by_pr.items():
                        source = edits[0]["source_pr"]
                        pr_link = source["html_url"] if source else f"#{pr_num}"
                        pr_title = source["title"] if source else edits[0]["change"]["change_summary"]
                        pr_body_parts.append(
                            f"**Triggered by [{DOCS_REPO}#{pr_num}]({pr_link})**: {pr_title}"
                        )
                        for edit in edits:
                            urgency_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(
                                edit["urgency"], "⚪"
                            )
                            pr_body_parts.append(
                                f"- {urgency_emoji} `{edit['repo_path']}`"
                            )
                        pr_body_parts.append("")

                    # Full review notes in collapsible sections
                    pr_body_parts.append("### Review details\n")
                    for _pr_num, edits in by_pr.items():
                        for edit in edits:
                            pr_body_parts.append(
                                f"<details>\n<summary><code>{edit['repo_path']}</code></summary>\n"
                            )
                            pr_body_parts.append(edit["review"])
                            pr_body_parts.append("\n</details>\n")

                    pr_body_parts.extend(
                        [
                            "---",
                            "",
                            "This PR was automatically generated by the **Docs Impact Monitor**. "
                            "Please review the changes carefully before merging.",
                            "",
                            "Each file edit was generated by Claude based on the release notes "
                            "and a review of the existing documentation content.",
                        ]
                    )

                    change_types = list({e["change"]["change_type"] for e in committed_files})
                    pr_title = f"docs: automated updates for {', '.join(change_types)}"
                    if len(pr_title) > 70:
                        pr_title = f"docs: automated updates ({len(committed_files)} files)"

                    pr_url = create_pull_request(
                        upstream_repo=DOCS_REPO,
                        fork_owner=FORK_OWNER,
                        branch=branch_name,
                        title=pr_title,
                        body="\n".join(pr_body_parts),
                    )

                    if pr_url:
                        print(f"\n    PR created successfully: {pr_url}")
                        pr_created = True
                        set_github_output("pr_url", pr_url)

                        # Build Slack summary
                        file_list = "\n".join(
                            f"• `{e['repo_path']}`" for e in committed_files
                        )
                        slack_summary = (
                            f"*{len(committed_files)} doc(s) updated* "
                            f"for `{MONITORED_REPO}`\n{file_list}"
                        )
                        send_slack_notification(pr_url, slack_summary)
                    else:
                        print("    PR creation API call failed.")
                else:
                    print("    No files were committed.")

            except Exception as e:
                print(f"\n    PR creation failed: {e}")
                print("    Falling back to issue creation...")
        else:
            print("\nStep 8: No file edits were generated. Falling back to issue creation...")

    elif CREATE_PRS and not FORK_OWNER:
        print("\nStep 8: FORK_OWNER not set — cannot create PRs. Falling back to issues...")

    # Step 8b: Create issues as fallback
    if not pr_created:
        if not issues_to_create:
            print("\nNo documentation updates needed after deep review. All clear!")
            return

        print(f"\nStep 8b: Creating {len(issues_to_create)} GitHub issue(s)...")

        for item in issues_to_create:
            change = item["change"]
            source_pr = item["source_pr"]
            docs = item["docs"]

            pr_link = source_pr["html_url"] if source_pr else f"#{change['pr_number']}"
            title = f"Docs review needed: {change['change_summary'][:80]}"

            urgency_emoji = {"high": "\U0001f534", "medium": "\U0001f7e1", "low": "\U0001f7e2"}

            body_parts = [
                "## Triggered by",
                f"**PR**: [{MONITORED_REPO}#{change['pr_number']}]({pr_link})",
                f"**Change type**: `{change['change_type']}`",
                f"**Summary**: {change['change_summary']}",
                "",
                "## Documentation pages to review",
                "",
            ]

            for doc in docs:
                emoji = urgency_emoji.get(doc["urgency"], "\u26aa")
                body_parts.append(f"### {emoji} [{doc['title'] or doc['url']}]({doc['url']})")
                body_parts.append(f"**Urgency**: {doc['urgency']}")
                body_parts.append(f"**Why**: {doc['reason']}")
                body_parts.append(f"\n<details><summary>Suggested changes</summary>\n")
                body_parts.append(doc["review"])
                body_parts.append(f"\n</details>\n")

            body_parts.append("---")
            body_parts.append(
                "*This issue was automatically generated by the Docs Impact Monitor.*"
            )

            body = "\n".join(body_parts)
            labels = ["docs-review", change["change_type"]]
            create_github_issue(title, body, labels)

        # Notify Slack about issues created
        issue_summaries = []
        for item in issues_to_create:
            change = item["change"]
            doc_list = ", ".join(d["url"].split("/")[-1] for d in item["docs"][:3])
            issue_summaries.append(
                f"• PR #{change['pr_number']}: {change['change_summary'][:80]} ({doc_list})"
            )
        slack_summary = (
            f"*{len(issues_to_create)} docs issue(s) created* "
            f"for `{MONITORED_REPO}`\n" + "\n".join(issue_summaries)
        )
        repo_url = f"https://github.com/{THIS_REPO}/issues" if THIS_REPO else ""
        send_slack_notification(repo_url, slack_summary)

    # Step 9: Reply to Slack threads for each source PR
    # Determine which source PRs got docs edits in the created PR
    pr_numbers_with_edits: set[int] = set()
    if pr_created:
        for edit in committed_files:
            pr_numbers_with_edits.add(edit["change"]["pr_number"])

    # Generate reasons for PRs that did NOT get edits
    pr_no_edit_reasons: dict[int, str] = {}
    prs_without_edits = sorted(all_pr_numbers - pr_numbers_with_edits)
    if prs_without_edits:
        print("\nGenerating reasons for PRs without docs edits...")
        for pr_num in prs_without_edits:
            pr_data = next((p for p in prs_with_notes if p["number"] == pr_num), None)
            if pr_data:
                pr_no_edit_reasons[pr_num] = generate_no_update_reason(pr_data)
            else:
                pr_no_edit_reasons[pr_num] = "No release notes with documentation impact found."

    if SLACK_BOT_TOKEN and SLACK_CHANNEL_ID:
        print("\nStep 9a: Replying to Slack threads for source PRs...")
        for pr_num in sorted(all_pr_numbers):
            if pr_num in pr_numbers_with_edits and pr_url:
                notify_slack_for_pr(pr_num, pr_url)
            else:
                notify_slack_for_pr(pr_num, None, reason=pr_no_edit_reasons.get(pr_num, ""))

    # Step 9b: Reply to release notes bot posts in the second channel
    print("\nStep 9b: Replying to release notes channel...")
    for pr_num in sorted(all_pr_numbers):
        if pr_num in pr_numbers_with_edits and pr_url:
            notify_release_notes_channel_for_pr(pr_num, pr_url)
        else:
            notify_release_notes_channel_for_pr(pr_num, None, reason=pr_no_edit_reasons.get(pr_num, ""))

    # Mark all PRs from this run as processed so they aren't picked up again
    save_processed_prs(all_pr_numbers)
    print(f"\nSaved {len(all_pr_numbers)} PR(s) to processed state.")
    print("Done!")


if __name__ == "__main__":
    main()
