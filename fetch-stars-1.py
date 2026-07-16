import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone


DEFAULT_OUTFILE = "github_starred_repos.csv"

COLUMNS = [
    "starred_at",
    "repo_full_name",
    "repo_name",
    "owner_login",
    "html_url",
    "description",
    "primary_language",
    "topics",
    "is_fork",
    "fork_of_full_name",
    "license_spdx",
    "archived",
    "updated_at",
    "pushed_at",
    "stargazers_count",
    "forks_count",
    "fetched_at",
]

STARRED_REPOS_QUERY = """
query($first: Int!, $after: String, $topicLimit: Int!) {
  viewer {
    starredRepositories(
      first: $first
      after: $after
      orderBy: {field: STARRED_AT, direction: DESC}
    ) {
      edges {
        starredAt
        node {
          nameWithOwner
          name
          owner {
            login
          }
          url
          description
          primaryLanguage {
            name
          }
          repositoryTopics(first: $topicLimit) {
            nodes {
              topic {
                name
              }
            }
          }
          isFork
          parent {
            nameWithOwner
          }
          licenseInfo {
            spdxId
          }
          isArchived
          updatedAt
          pushedAt
          stargazerCount
          forkCount
        }
      }
      pageInfo {
        hasNextPage
        endCursor
      }
      totalCount
    }
  }
  rateLimit {
    cost
    remaining
    resetAt
  }
}
"""

STARRED_REPOS_NO_TOPICS_QUERY = """
query($first: Int!, $after: String) {
  viewer {
    starredRepositories(
      first: $first
      after: $after
      orderBy: {field: STARRED_AT, direction: DESC}
    ) {
      edges {
        starredAt
        node {
          nameWithOwner
          name
          owner {
            login
          }
          url
          description
          primaryLanguage {
            name
          }
          isFork
          parent {
            nameWithOwner
          }
          licenseInfo {
            spdxId
          }
          isArchived
          updatedAt
          pushedAt
          stargazerCount
          forkCount
        }
      }
      pageInfo {
        hasNextPage
        endCursor
      }
      totalCount
    }
  }
  rateLimit {
    cost
    remaining
    resetAt
  }
}
"""


class GraphQLFetchError(RuntimeError):
    def __init__(self, message, fetched_count):
        super().__init__(message)
        self.fetched_count = fetched_count


def parse_args():
    parser = argparse.ArgumentParser(
        description="Export GitHub starred repositories to CSV using gh authentication."
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTFILE,
        help=f"CSV output path. Defaults to {DEFAULT_OUTFILE}.",
    )
    parser.add_argument(
        "--mode",
        choices=("incremental", "full"),
        default="incremental",
        help="Sync from the existing CSV checkpoint or rebuild it fully. Defaults to incremental.",
    )
    parser.add_argument(
        "--topic-limit",
        type=int,
        default=10,
        help="Maximum topics to fetch per repository through GraphQL. Defaults to 10.",
    )
    return parser.parse_args()


def split_include_response(raw):
    parts = raw.split("\r\n\r\n", 1)
    if len(parts) != 2:
        parts = raw.split("\n\n", 1)
    if len(parts) != 2:
        raise RuntimeError("Unexpected response format from gh api --include")

    header_txt, body_txt = parts
    headers = {}
    for line in header_txt.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()

    status_line = header_txt.splitlines()[0].strip() if header_txt.splitlines() else ""
    match = re.search(r"\s(\d{3})\s", status_line)
    status = int(match.group(1)) if match else 200
    return status, headers, body_txt


def run_gh_api(cmd, retry=6):
    backoff = 1.0
    last_err = None

    for _ in range(retry):
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            last_err = RuntimeError((proc.stderr or "").strip() or f"gh api failed: {proc.returncode}")
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 60.0)
            continue

        status, headers, body_txt = split_include_response(proc.stdout)
        if status in (403, 429):
            retry_after = headers.get("retry-after")
            sleep_s = float(retry_after) if retry_after else backoff
            last_err = RuntimeError(f"HTTP {status} rate limit response from GitHub")
            time.sleep(sleep_s)
            backoff = min(backoff * 2.0, 60.0)
            continue

        if status >= 400:
            raise RuntimeError(f"GitHub API returned HTTP {status}: {body_txt.strip()}")

        data = json.loads(body_txt) if body_txt.strip() else None
        return data, headers

    raise last_err or RuntimeError("GitHub API request failed")


def gh_graphql(query, variables):
    cmd = ["gh", "api", "graphql", "--include", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is None:
            continue
        cmd.extend(["-F", f"{key}={value}"])
    data, _ = run_gh_api(cmd)

    errors = data.get("errors") if isinstance(data, dict) else None
    if errors:
        messages = "; ".join(error.get("message", str(error)) for error in errors)
        raise RuntimeError(f"GraphQL error: {messages}")

    return data


def bool_str(value):
    if value is None:
        return ""
    return "true" if value else "false"


def parse_dt(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_required_dt(value):
    if not value:
        raise ValueError("starred_at must not be empty")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"invalid starred_at timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"starred_at timestamp must include a timezone: {value!r}")
    return parsed


def clean_cell(value):
    return "" if value is None else value


def topics_from_graphql(repo):
    topic_nodes = ((repo.get("repositoryTopics") or {}).get("nodes")) or []
    names = []
    for node in topic_nodes:
        name = ((node or {}).get("topic") or {}).get("name")
        if name:
            names.append(name)
    return ";".join(names)


def row_from_graphql_edge(edge):
    repo = edge.get("node") or {}
    owner = repo.get("owner") or {}
    parent = repo.get("parent") or {}
    language = repo.get("primaryLanguage") or {}
    license_info = repo.get("licenseInfo") or {}

    return {
        "starred_at": clean_cell(edge.get("starredAt")),
        "repo_full_name": clean_cell(repo.get("nameWithOwner")),
        "repo_name": clean_cell(repo.get("name")),
        "owner_login": clean_cell(owner.get("login")),
        "html_url": clean_cell(repo.get("url")),
        "description": clean_cell(repo.get("description")),
        "primary_language": clean_cell(language.get("name")),
        "topics": topics_from_graphql(repo),
        "is_fork": bool_str(repo.get("isFork")),
        "fork_of_full_name": clean_cell(parent.get("nameWithOwner")),
        "license_spdx": clean_cell(license_info.get("spdxId")),
        "archived": bool_str(repo.get("isArchived")),
        "updated_at": clean_cell(repo.get("updatedAt")),
        "pushed_at": clean_cell(repo.get("pushedAt")),
        "stargazers_count": clean_cell(repo.get("stargazerCount")),
        "forks_count": clean_cell(repo.get("forkCount")),
    }


def fetch_stars_graphql(topic_limit, checkpoint=None):
    rows = []
    cursor = None
    page_num = 0
    final_rate_limit = None
    total_count = None
    query = STARRED_REPOS_QUERY if topic_limit > 0 else STARRED_REPOS_NO_TOPICS_QUERY
    checkpoint_dt = parse_required_dt(checkpoint[0]) if checkpoint else None

    while True:
        try:
            page_num += 1
            variables = {"first": 100, "after": cursor}
            if topic_limit > 0:
                variables["topicLimit"] = topic_limit
            data = gh_graphql(query, variables)
        except Exception as exc:
            raise GraphQLFetchError(str(exc), len(rows)) from exc

        starred = (((data.get("data") or {}).get("viewer") or {}).get("starredRepositories")) or {}
        total_count = starred.get("totalCount", total_count)
        edges = starred.get("edges") or []
        checkpoint_found = False
        for edge in edges:
            row = row_from_graphql_edge(edge or {})
            if checkpoint:
                identity = (row["starred_at"], row["repo_full_name"])
                if identity == checkpoint:
                    checkpoint_found = True
                    break
                if parse_required_dt(row["starred_at"]) < checkpoint_dt:
                    raise RuntimeError(
                        "Incremental checkpoint was not found before older stars were reached; "
                        "run again with --mode full."
                    )
            rows.append(row)

        page_info = starred.get("pageInfo") or {}
        final_rate_limit = ((data.get("data") or {}).get("rateLimit")) or final_rate_limit
        fetched_text = f"{len(rows)}/{total_count}" if total_count is not None else str(len(rows))
        print(f"GraphQL page {page_num}: fetched {len(edges)} repos ({fetched_text})", flush=True)

        if checkpoint_found:
            break
        if not page_info.get("hasNextPage"):
            if checkpoint:
                raise RuntimeError(
                    "Incremental checkpoint was not found in GitHub starred repositories; "
                    "run again with --mode full."
                )
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            raise RuntimeError("GraphQL pagination expected an endCursor but none was returned")

    return rows, final_rate_limit


def sort_rows(rows):
    rows.sort(key=lambda row: parse_dt(row.get("starred_at") or ""), reverse=True)


def write_csv_atomic(rows, output):
    output = os.path.abspath(output)
    output_dir = os.path.dirname(output)
    os.makedirs(output_dir, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{os.path.basename(output)}.", dir=output_dir)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=COLUMNS, quoting=csv.QUOTE_MINIMAL)
            writer.writeheader()
            for row in rows:
                writer.writerow({column: row.get(column, "") for column in COLUMNS})
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, output)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def print_summary(rows, output, rate_limit=None, wrote=True):
    newest = rows[0].get("starred_at") if rows else ""
    oldest = rows[-1].get("starred_at") if rows else ""
    print(f"{len(rows)} starred repos; newest={newest}; oldest={oldest}", flush=True)
    action = "Wrote" if wrote else "Output unchanged"
    print(f"{action}: {output}", flush=True)

    if rate_limit:
        print(
            "GraphQL rate limit: "
            f"cost={rate_limit.get('cost')}; "
            f"remaining={rate_limit.get('remaining')}; "
            f"reset_at={rate_limit.get('resetAt')}",
            flush=True,
        )


def incremental_csv_error(message):
    return ValueError(f"Invalid incremental CSV: {message}; run again with --mode full.")


def read_incremental_csv(path):
    if not os.path.exists(path):
        raise incremental_csv_error(f"{path} does not exist")

    try:
        file = open(path, newline="", encoding="utf-8")
    except OSError as exc:
        raise incremental_csv_error(f"could not read {path}: {exc}") from exc

    with file:
        try:
            reader = csv.DictReader(file, strict=True)
            if reader.fieldnames != COLUMNS:
                raise incremental_csv_error(
                    f"header must exactly match {','.join(COLUMNS)}"
                )
            rows = list(reader)
        except (csv.Error, UnicodeError) as exc:
            raise incremental_csv_error(f"could not parse {path}: {exc}") from exc
    if not rows:
        raise incremental_csv_error("the file has no data rows")

    seen_repos = set()
    previous_dt = None
    for line_number, row in enumerate(rows, start=2):
        if set(row) != set(COLUMNS) or None in row.values():
            raise incremental_csv_error(f"row {line_number} does not match the CSV header")
        repo_full_name = row["repo_full_name"]
        if not repo_full_name:
            raise incremental_csv_error(f"row {line_number} has no repo_full_name")
        if repo_full_name in seen_repos:
            raise incremental_csv_error(
                f"row {line_number} duplicates repo_full_name {repo_full_name!r}"
            )
        seen_repos.add(repo_full_name)
        try:
            row_dt = parse_required_dt(row["starred_at"])
        except ValueError as exc:
            raise incremental_csv_error(f"row {line_number} has {exc}") from exc
        if previous_dt is not None and row_dt > previous_dt:
            raise incremental_csv_error(
                f"row {line_number} is not sorted by starred_at descending"
            )
        previous_dt = row_dt

    first = rows[0]
    return rows, (first["starred_at"], first["repo_full_name"])

def main():
    args = parse_args()
    if args.topic_limit < 0:
        print("--topic-limit must be 0 or greater", file=sys.stderr)
        return 2

    if args.mode == "incremental":
        existing_rows, checkpoint = read_incremental_csv(args.output)
    else:
        existing_rows, checkpoint = [], None

    rows, rate_limit = fetch_stars_graphql(args.topic_limit, checkpoint=checkpoint)
    if args.mode == "incremental":
        existing_repos = {row["repo_full_name"] for row in existing_rows}
        rows = [row for row in rows if row["repo_full_name"] not in existing_repos]
    if args.mode == "incremental" and not rows:
        print("No new starred repositories; output left unchanged.", flush=True)
        print_summary(existing_rows, args.output, rate_limit, wrote=False)
        return 0

    fetched_at = datetime.now(timezone.utc).isoformat()
    for row in rows:
        row["fetched_at"] = fetched_at
    combined_rows = rows + existing_rows
    sort_rows(combined_rows)
    write_csv_atomic(combined_rows, args.output)
    print_summary(combined_rows, args.output, rate_limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
