# Sui Docs Impact Monitor

A CI pipeline that monitors the [MystenLabs/sui](https://github.com/MystenLabs/sui) release-notes-monitor workflow, detects documentation impact, generates fixes, and opens PRs.

## How it works

```
release-notes-monitor (MystenLabs/sui)
        │ completes
        ▼
docs-monitor.yml (this repo, runs every 6 hours)
        │
        ├─ 1. Checks for recent successful runs of release-notes-monitor
        ├─ 2. Extracts PR numbers from workflow run jobs
        ├─ 3. Fetches release notes from each PR
        ├─ 4. Claude triage → which changes affect docs?
        │     (API changes, deprecations, breaking changes, new features)
        ├─ 5. Fetches docs index from docs.sui.io/llms.txt
        ├─ 6. Claude identifies affected doc pages
        ├─ 7. Claude reviews each flagged page and generates edits
        ├─ 8a. Forks repo, commits edits, opens PR against MystenLabs/sui
        └─ 8b. (fallback) Creates GitHub issues if PR creation is disabled
```

## Setup

### 1. Repository secrets

| Secret | Required | Description |
|--------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key for Claude |
| `DOCS_MONITOR_PAT` | For PRs | GitHub PAT with `repo` + `workflow` scope (needed to fork, push, and open PRs on MystenLabs/sui) |

The default `GITHUB_TOKEN` is used automatically for reading public repos and creating issues in this repo. For PR creation against external repos, you need a PAT.

### 2. Repository variables

Set these in Settings > Secrets and variables > Actions > Variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `FORK_OWNER` | For PRs | GitHub username or org to create the fork under (e.g., `jessiemongeon`) |

### 3. Issue labels (optional)

For issue-only mode, create these labels:

- `docs-review`, `api_change`, `deprecation`, `breaking_change`, `new_feature`, `config_change`, `framework_change`

### 4. Configuration

All config is via environment variables in the workflow file:

| Variable | Default | Description |
|----------|---------|-------------|
| `MONITORED_REPO` | `MystenLabs/sui` | GitHub repo to monitor |
| `MONITORED_WORKFLOW` | `release-notes-monitor.yml` | Workflow file to watch |
| `DOCS_LLMS_URL` | `https://docs.sui.io/llms.txt` | Docs index URL |
| `DOCS_BASE_URL` | `https://docs.sui.io` | Base URL for doc pages |
| `DOCS_REPO` | `MystenLabs/sui` | Repo containing the doc source files |
| `DOCS_REPO_PATH_PREFIX` | `docs/content` | Path prefix for docs in the source repo |
| `LOOKBACK_HOURS` | `6` | How far back to check for runs |
| `CREATE_PRS` | `true` | Set to `false` for issue-only mode |

## Usage

### Automatic (scheduled)

Runs every 6 hours. If `FORK_OWNER` and `DOCS_MONITOR_PAT` are set, it opens PRs. Otherwise, it creates issues.

### Manual trigger

Actions > "Docs Impact Monitor" > Run workflow. You can toggle PR creation on/off.

### Local testing

```bash
export GITHUB_TOKEN="ghp_..."
export ANTHROPIC_API_KEY="sk-ant-..."
export LOOKBACK_HOURS=168            # 1 week
export CREATE_PRS=false              # issue-only / dry-run
python scripts/monitor.py
```

To test PR creation locally:

```bash
export CREATE_PRS=true
export FORK_OWNER="your-github-username"
export DOCS_MONITOR_PAT="ghp_..."    # PAT with repo scope
export GITHUB_TOKEN="$DOCS_MONITOR_PAT"
python scripts/monitor.py
```

## Modes

### PR mode (default)

When `CREATE_PRS=true` and `FORK_OWNER` is set:

1. Forks `MystenLabs/sui` under `FORK_OWNER` (if not already forked)
2. Syncs the fork with upstream
3. Creates a timestamped branch
4. Claude generates the actual updated file content for each affected doc
5. Commits each file via the GitHub Contents API
6. Opens a single PR against `MystenLabs/sui` with all changes

### Issue mode (fallback)

When `CREATE_PRS=false` or `FORK_OWNER` is not set:

Creates GitHub issues in this repo with:
- The triggering PR and change type
- Affected doc pages with urgency levels
- Specific suggestions for what to update

## How Claude generates edits

The pipeline uses Claude at multiple stages:

1. **Triage** (Sonnet) — classifies which release notes affect documentation
2. **Doc matching** (Sonnet) — identifies which specific pages are affected
3. **Review** (Sonnet) — analyzes each page and describes what needs changing
4. **Edit generation** (Sonnet) — produces the complete updated file content

The edit generation step receives the full source file (mdx/md) and produces a minimal diff — only changing what's needed for the specific code change, preserving all formatting, frontmatter, and MDX components.
