# Docs Drift Alert

Automated CI pipeline that monitors multiple Mysten Labs repositories for code changes that affect documentation, generates fixes following the [Sui Style Guide](https://docs.sui.io/references/contribute/style-guide), and opens PRs.

## Monitored repositories

| Job | Source repo | Scan mode | Docs site | Docs source | PRs opened against |
|-----|-------------|-----------|-----------|-------------|-------------------|
| `sui-docs` | [MystenLabs/sui](https://github.com/MystenLabs/sui) | Workflow runs | [docs.sui.io](https://docs.sui.io) | `docs/content` | `MystenLabs/sui` |
| `move-book` | [MystenLabs/sui](https://github.com/MystenLabs/sui) | Workflow runs | [move-book.com](https://move-book.com) | `book/` | `MystenLabs/move-book` |
| `walrus-docs` | [MystenLabs/walrus](https://github.com/MystenLabs/walrus) | Merged PRs | [docs.wal.app](https://docs.wal.app) | `docs/content` | `MystenLabs/walrus` |
| `seal-docs` | [MystenLabs/seal](https://github.com/MystenLabs/seal) | Merged PRs | [seal-docs.wal.app](https://seal-docs.wal.app) | `docs/content` | `MystenLabs/seal` |
| `suins-docs` | [MystenLabs/suins-contracts](https://github.com/MystenLabs/suins-contracts) | Merged PRs | [docs.suins.io](https://docs.suins.io) | `documentation/content` | `MystenLabs/suins-contracts` |

All 5 jobs run in parallel every 6 hours.

## How it works

```
Source repo (e.g., MystenLabs/sui)
        │  new release notes or merged PRs
        ▼
docs-monitor.yml (this repo, every 6 hours)
        │
        ├─ 1. Detect new changes (workflow runs or merged PRs)
        ├─ 2. Extract PR numbers and fetch release notes
        ├─ 3. Claude triage → which changes affect docs?
        │     (API changes, deprecations, breaking changes, new features)
        ├─ 4. Fetch docs index (llms.txt or repo tree fallback)
        ├─ 5. Claude identifies affected doc pages
        ├─ 6. Claude reviews each page and generates edits (style-guide-compliant)
        ├─ 7a. Fork → branch → commit → open PR against docs repo
        ├─ 7b. (fallback) Create GitHub issues if PR creation fails
        └─ 8. Notify Slack
```

### Scan modes

- **`workflow_runs`** — Monitors a scheduled workflow (like Sui's `release-notes-monitor.yml`), extracts PR numbers from matrix job names.
- **`merged_prs`** — Directly scans recently merged PRs within the lookback window. Used for repos without a scheduled release notes workflow (Walrus, Seal, SuiNS).

### Docs index

The pipeline uses `llms.txt` when available (Sui, SuiNS) for fast, structured doc discovery. When `llms.txt` is unavailable (returns 403), it falls back to building an index by walking the docs directory tree via the GitHub API.

## Setup

### 1. Repository secrets

| Secret | Required | Description |
|--------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key for Claude |
| `DOCS_MONITOR_PAT` | For PRs | Classic GitHub PAT with `repo` scope. Must be [SSO-authorized](https://docs.github.com/en/enterprise-cloud@latest/authentication/authenticating-with-saml-single-sign-on/authorizing-a-personal-access-token-for-use-with-saml-single-sign-on) for MystenLabs. |
| `SLACK_WEBHOOK_URL` | For Slack | [Incoming webhook URL](https://api.slack.com/messaging/webhooks) for notifications |
| `FORK_OWNER` | For PRs | GitHub username or org to fork repos under (can also be set as a variable) |

The default `GITHUB_TOKEN` (provided automatically by GitHub Actions) handles all read operations against public repos. The PAT is only used for write operations (fork, push, open PRs).

### 2. Repository variables

Set in **Settings > Secrets and variables > Actions > Variables**:

| Variable | Description |
|----------|-------------|
| `FORK_OWNER` | GitHub username or org to create forks under (alternative to setting as a secret) |

### 3. Issue labels (optional)

For issue-only mode, create these labels:

`docs-review`, `api_change`, `deprecation`, `breaking_change`, `new_feature`, `config_change`, `framework_change`

## Configuration

Each job is configured through environment variables in the workflow file:

| Variable | Description |
|----------|-------------|
| `MONITORED_REPO` | GitHub repo to monitor for changes |
| `MONITORED_WORKFLOW` | Workflow file to watch (empty for `merged_prs` mode) |
| `SCAN_MODE` | `workflow_runs` or `merged_prs` |
| `DOCS_LLMS_URL` | URL to `llms.txt` docs index (falls back to repo tree if unavailable) |
| `DOCS_BASE_URL` | Base URL for the docs site |
| `DOCS_REPO` | Repo containing the doc source files (can differ from monitored repo) |
| `DOCS_REPO_PATH_PREFIX` | Path prefix for docs in the source repo |
| `LOOKBACK_HOURS` | How far back to check (default: 6) |
| `CREATE_PRS` | `true` to open PRs, `false` for issue-only mode |

## Usage

### Scheduled

Runs automatically every 6 hours. All 5 jobs run in parallel.

### Manual trigger

**Actions > Docs Impact Monitor > Run workflow**. Inputs:
- `lookback_hours` — Override the lookback window (e.g., `168` for 1 week)
- `create_prs` — Toggle PR creation on/off

### Local testing

```bash
# Install dependencies
pip install -r scripts/requirements.txt

# Dry run (no PRs, no issues)
export GITHUB_TOKEN=$(gh auth token)
export ANTHROPIC_API_KEY="sk-ant-..."
export LOOKBACK_HOURS=168
export CREATE_PRS=false
python scripts/monitor.py

# With PR creation
export CREATE_PRS=true
export FORK_OWNER="your-github-username"
export DOCS_MONITOR_PAT="ghp_..."
python scripts/monitor.py
```

Override which repo to monitor:

```bash
export MONITORED_REPO=MystenLabs/seal
export SCAN_MODE=merged_prs
export DOCS_LLMS_URL=https://seal-docs.wal.app/llms.txt
export DOCS_BASE_URL=https://seal-docs.wal.app
export DOCS_REPO=MystenLabs/seal
export DOCS_REPO_PATH_PREFIX=docs/content
python scripts/monitor.py
```

## PR mode vs issue mode

### PR mode (default)

When `CREATE_PRS=true` and `FORK_OWNER` is set:

1. Forks the target docs repo under `FORK_OWNER` (or reuses existing fork)
2. Syncs the fork with upstream
3. Creates a timestamped branch (`docs-monitor/auto-update-YYYYMMDD-HHMMSS`)
4. Claude generates updated file content for each affected doc
5. Commits each file via the GitHub Contents API
6. Opens a single PR with all changes, grouped by triggering PR
7. Sends a Slack notification with the PR link

### Issue mode (fallback)

When PRs are disabled or creation fails:

- Creates GitHub issues in this repo with the triggering PR, affected pages, urgency levels, and suggested changes
- Sends a Slack notification with a summary

## How Claude generates edits

The pipeline uses Claude (Sonnet) at 4 stages:

1. **Triage** — Classifies which release notes affect documentation (API changes, deprecations, breaking changes, new features, config changes, framework changes)
2. **Doc matching** — Cross-references changes against the docs index to identify affected pages
3. **Review** — Analyzes each page against the change and describes what needs updating
4. **Edit generation** — Produces the complete updated file content

All generated edits follow the [Sui Documentation Style Guide](https://docs.sui.io/references/contribute/style-guide), including:
- Active voice, second person ("you"), present tense
- Correct capitalization for Sui terminology (Mainnet, Testnet, onchain, gas, etc.)
- Proper use of admonitions (`:::caution` for deprecations, `:::tip` for best practices)
- Sentence case for headings, Oxford commas, no em dashes
- Inline code for function names, CLI commands, file paths, and object names

## Notifications

When `SLACK_WEBHOOK_URL` is set, the pipeline posts to Slack:

- **On PR creation**: Lists the updated files and links to the PR
- **On issue creation**: Summarizes the flagged changes and links to the issues

## Adding a new repository

To monitor a new repo, add a job to `.github/workflows/docs-monitor.yml`:

```yaml
  new-repo-docs:
    name: New Repo Docs Monitor
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install -r scripts/requirements.txt
      - name: Run monitor
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          DOCS_MONITOR_PAT: ${{ secrets.DOCS_MONITOR_PAT }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
          MONITORED_REPO: OrgName/repo-name
          SCAN_MODE: merged_prs          # or workflow_runs
          DOCS_LLMS_URL: https://docs.example.com/llms.txt
          DOCS_BASE_URL: https://docs.example.com
          DOCS_REPO: OrgName/repo-name   # repo with doc source files
          DOCS_REPO_PATH_PREFIX: docs/content
          LOOKBACK_HOURS: ${{ inputs.lookback_hours || '6' }}
          THIS_REPO: ${{ github.repository }}
          CREATE_PRS: ${{ inputs.create_prs || 'true' }}
          FORK_OWNER: ${{ vars.FORK_OWNER || secrets.FORK_OWNER }}
        run: python scripts/monitor.py
```
