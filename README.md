# Docs Changelog Monitor

<img src="icon.jpeg" alt="Docs Changelog Monitor" />

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

