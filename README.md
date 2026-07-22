# GitHub Stars Export

Python tooling for incrementally exporting the authenticated user's GitHub
starred repositories to CSV and syncing them to Google Sheets.

The main script uses GitHub GraphQL through the GitHub CLI (`gh`) so large star
lists are fetched in 100-repository pages. GraphQL or checkpoint failures stop
the run without replacing the existing CSV.

## Requirements

- Python 3.10 or newer
- GitHub CLI (`gh`)
- An authenticated `gh` session with access to `viewer.starredRepositories`

Check authentication:

```sh
gh auth status
```

## Export Stars

Run:

```sh
python fetch-stars-1.py \
  --output github_starred_repos.csv \
  --mode incremental
```

`--mode` accepts `incremental` or `full` and defaults to `incremental`.
Incremental mode compares the existing CSV's exact top checkpoint
`(starred_at, repo_full_name)` with GraphQL results, stops pagination when it
finds that checkpoint, and prepends only newer stars. A missing, empty,
incomplete, or duplicate checkpoint is a hard failure and leaves the CSV
unchanged.

Useful option:

```sh
python fetch-stars-1.py --output github_starred_repos.csv --topic-limit 10
```

The output columns are:

```text
starred_at, repo_full_name, repo_name, owner_login, html_url, description,
primary_language, topics, is_fork, fork_of_full_name, license_spdx, archived,
updated_at, pushed_at, stargazers_count, forks_count, fetched_at
```

`topics` are semicolon-delimited. Empty GitHub values are written as empty CSV cells.

## Google Sheets Sync

The project includes `sync_to_sheets.py` to upload the generated CSV file directly to a Google Sheet.

### Sync Manually

Make sure you've installed the Google API packages listed in `requirements.txt`:
```sh
pip install -r requirements.txt
```

Set the `GOOGLE_SERVICE_ACCOUNT_KEY` environment variable to your Google service account JSON key content, or pass the path via `--credentials`:

```sh
python sync_to_sheets.py \
  --csv github_starred_repos.csv \
  --spreadsheet-id "YOUR_SPREADSHEET_ID" \
  --sheet-name "Sheet1" \
  --mode incremental
```

`incremental` is the default. It reads the Sheet's top data row, finds the
exact `(starred_at, repo_full_name)` checkpoint in the CSV, and atomically
inserts only newer rows beneath the header. If there are no newer rows, the
Sheet is left unchanged. A missing checkpoint, an empty Sheet, or a header
mismatch fails without changing the Sheet.

Use full mode to initialize or recover both artifacts. First rebuild the entire
CSV from GraphQL, then replace the Sheet from that CSV:

```sh
python fetch-stars-1.py \
  --output github_starred_repos.csv \
  --mode full

python sync_to_sheets.py \
  --csv github_starred_repos.csv \
  --spreadsheet-id "YOUR_SPREADSHEET_ID" \
  --sheet-name "Sheet1" \
  --mode full
```

Full mode clears existing cell values and rewrites the complete CSV in one
atomic Sheets request.

The public template ships with a header-only CSV. After configuring the
required secrets and `SPREADSHEET_ID` variable, run the workflow manually in
`full` mode once to initialize both the CSV and Sheet. Scheduled incremental
runs will fail safely until that initialization succeeds.

## Personal Knowledge Base & Agentic Context

Exporting and syncing your starred repositories serves two primary purposes beyond simple backup:

### 1. Personal Knowledge Base (PKB) Context
Your starred repositories represent a curated, high-quality stream of tools, frameworks, and reference implementations that you have personally evaluated or found interesting. By keeping this list in a structured format (like `github_starred_repos.csv` or Google Sheets), you can:
- Integrate this list directly with PKB tools like **Obsidian** or **Logseq** (using local CSV import plugins or APIs).
- Search and query your curated favorites locally without relying on GitHub's web interface or search API.
- Use topics and primary languages to automatically organize, tag, and cluster technical resources in your personal notes.

### 2. Agentic Context Layer
When working with agentic coding assistants (such as Antigravity or local developer agents), a major hurdle is "context bootstrapping." By providing your structured star database:
- The agent can immediately read the CSV to understand your technical stack, favorite developer tools, and preferred dependencies.
- It acts as a customized developer profile, allowing the agent to recommend technologies, libraries, or architectures that align with tools you already trust.
- In multi-agent systems, agents can query this local database as a custom knowledge tool to discover how you solved similar problems or what packages you prefer for specific tasks.

## Automated Sync with GitHub Actions

The repository is configured with a GitHub Actions workflow (`.github/workflows/sync-stars.yml`) that automatically runs daily to fetch your latest starred repositories, incrementally sync them to Google Sheets, and commit the updated `github_starred_repos.csv` back to the repository. Scheduled runs always use incremental mode. A manual workflow run offers an `incremental` or `full` choice and passes that same mode to both the CSV fetch and Sheet sync. Select `full` to rebuild both artifacts for initialization or recovery. Concurrency protection prevents overlapping sync runs.

### Required Secrets

To configure the workflow, configure the following secrets and variables under **Settings > Secrets and variables > Actions**:

1. **Secrets**:
   - `PERSONAL_GITHUB_TOKEN`: A GitHub Personal Access Token (PAT) with `read:user` scope (to read your starred repos).
   - `GOOGLE_SERVICE_ACCOUNT_KEY`: The JSON key string of your Google Cloud service account.
2. **Variables**:
   - `SPREADSHEET_ID`: The Google Sheets spreadsheet ID to sync to.

### Sharing the Google Sheet

Make sure the Google Sheet is shared with your service account email (e.g., `your-service-account-name@your-project-id.iam.gserviceaccount.com`) as an **Editor**.

## Agent Commit Memory

This project includes a versioned `post-commit` hook that captures commit-time context for future coding agents, onboarding agents, and meta/process/memory analysis agents.

Install it after this directory is a Git repository:

```sh
git init
python tools/install_git_hooks.py
```

The installer sets:

```sh
git config core.hooksPath .githooks
```

After each commit, `.githooks/post-commit` runs `tools/agent_commit_hook.py`.

### What It Captures

The hook writes a structured JSON payload and a readable Markdown report containing:

- commit hash, branch, subject, author, remotes, and status
- changed paths, numstat, shortstat, and selected Git command outputs
- selected Git config and `gh auth status`
- bounded snapshots and SHA-256 hashes of configured context files
- optional ignored session notes from `.agent/session-notes/`
- optional ignored local logs from `.agent/logs/`
- runtime context such as Python version, platform, selected environment values, and hook argv
- decisions, learnings, outcomes, and metrics intended for future agents

The hook is a `post-commit` hook, so it does not block or rewrite commits. If collection fails, the commit still exists and the hook reports the failure.

### Where Memory Is Stored

By default, durable memory is written outside the repo:

```text
~/.codex/memories/projects/github-stars
```

The repo also gets a small ignored pointer file under:

```text
.agent/commit-reports/
```

This keeps rich process memory available to external analysis agents without committing local logs, generated reports, or personal export data into the project.

### Configuration

Edit `.agent-commit-hook.json` to change capture behavior:

- `memory_dir`: outside-repo destination for JSON, Markdown, and `events.jsonl`
- `repo_report_dir`: ignored local pointer report directory
- `include_globs`: files whose text may be snapshotted
- `exclude_globs`: files and directories never captured
- `session_globs`: ignored local session notes to preserve outside the repo
- `log_globs`: ignored local logs to preserve outside the repo
- `max_file_bytes`: maximum text snapshot size per file
- `max_log_bytes`: maximum complete log size before only the tail is captured
- `capture_repo_snapshot`, `capture_sessions`, `capture_logs`, `capture_git_config`, `capture_environment`, `capture_gh_status`: feature toggles

Generated CSV exports are excluded by default because they can contain personal account history.

### Privacy Boundary

The hook redacts common token, password, API key, authorization, cookie, and credential-in-URL shapes before writing captured text. It is still a local process-memory system, not a secret scanner or compliance boundary.

Private chat transcripts, terminal scrollback, and agent session internals are not automatically available to Git hooks. To preserve those, write or export them into `.agent/session-notes/` or `.agent/logs/`, or extend `tools/agent_commit_hook.py` with an explicit source.

## Validation

Syntax check:

```sh
python -m py_compile fetch-stars-1.py tools/agent_commit_hook.py tools/install_git_hooks.py
```

Smoke test the export:

```sh
python fetch-stars-1.py --output github_starred_repos.csv --mode full
```

Expected behavior:

- GraphQL page progress is printed.
- The final row count matches the account's starred repo count.
- The CSV remains sorted by `starred_at` descending.
- The final summary includes newest star, oldest star, output path, and GraphQL rate-limit usage.

## License

This project is open-source and licensed under the [MIT License](LICENSE).
