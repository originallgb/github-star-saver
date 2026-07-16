# Project Rules for github-stars

This repository contains automated tooling to sync the user's GitHub stars to a CSV and a Google Sheet.

## Python Dependency & Execution Guidelines
- Always use the local `.venv` environment for running python scripts:
  ```bash
  .venv/bin/python fetch-stars-1.py --output github_starred_repos.csv
  .venv/bin/python sync_to_sheets.py --spreadsheet-id <id>
  ```
- Always update `requirements.txt` when adding new packages.

## Git & File System Integrity
- **Do NOT ignore `github_starred_repos.csv`:** The workflow automatically commits this file back to the repository. Keep it tracked and do not re-add it to `.gitignore`.
- **Ignore `.venv/`:** Ensure `.venv/` remains ignored in `.gitignore`.

## Google Sheets & Sync Protocol
- **Sheets API Authentication:** `sync_to_sheets.py` supports authenticating via `GOOGLE_SERVICE_ACCOUNT_KEY` env var (raw JSON string) or `--credentials` (filepath).
- **Google Sheets Permissions:** Permissions are managed at the spreadsheet sharing level. Share target sheets as "Editor" with your service account email (for example, `service-account@your-project.iam.gserviceaccount.com`). No GCP IAM roles are required.
- **Workflow Secrets:** The GitHub Actions workflow relies on:
  - `PERSONAL_GITHUB_TOKEN`: Classic GitHub PAT with `read:user` scope.
  - `GOOGLE_SERVICE_ACCOUNT_KEY`: Service account JSON key contents.
