import csv
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "fetch-stars-1.py"
SPEC = importlib.util.spec_from_file_location("fetch_stars", SCRIPT)
fetch_stars = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch_stars)


def graphql_page(edges, *, has_next=False, cursor=None):
    return {
        "data": {
            "viewer": {
                "starredRepositories": {
                    "edges": edges,
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    "totalCount": len(edges),
                }
            },
            "rateLimit": {"cost": 1, "remaining": 4999, "resetAt": "later"},
        }
    }


def edge(starred_at, full_name):
    owner, name = full_name.split("/", 1)
    return {
        "starredAt": starred_at,
        "node": {
            "nameWithOwner": full_name,
            "name": name,
            "owner": {"login": owner},
            "url": f"https://github.com/{full_name}",
        },
    }


def complete_row(starred_at, full_name):
    row = {column: "" for column in fetch_stars.COLUMNS}
    row.update(
        starred_at=starred_at,
        repo_full_name=full_name,
        repo_name=full_name.split("/", 1)[1],
        owner_login=full_name.split("/", 1)[0],
        html_url=f"https://github.com/{full_name}",
    )
    return row


class ParseArgsTests(unittest.TestCase):
    def test_incremental_is_the_default_mode(self):
        with mock.patch.object(sys, "argv", [str(SCRIPT)]):
            args = fetch_stars.parse_args()

        self.assertEqual(args.mode, "incremental")


class ExistingCsvTests(unittest.TestCase):
    def write_csv(self, path, rows, fieldnames=None):
        with open(path, "w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=fieldnames or fetch_stars.COLUMNS,
                extrasaction="ignore",
            )
            writer.writeheader()
            writer.writerows(rows)

    def test_valid_csv_uses_exact_first_row_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stars.csv")
            rows = [
                complete_row("2026-07-16T12:00:00Z", "owner/newest"),
                complete_row("2026-07-15T12:00:00Z", "owner/older"),
            ]
            self.write_csv(path, rows)

            loaded, checkpoint = fetch_stars.read_incremental_csv(path)

        self.assertEqual(loaded, rows)
        self.assertEqual(checkpoint, ("2026-07-16T12:00:00Z", "owner/newest"))

    def test_missing_and_header_only_csvs_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "missing.csv")
            header_only = os.path.join(tmp, "empty.csv")
            self.write_csv(header_only, [])

            for path in (missing, header_only):
                with self.subTest(path=path):
                    with self.assertRaisesRegex(ValueError, "--mode full"):
                        fetch_stars.read_incremental_csv(path)

    def test_truncated_csv_row_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "truncated.csv")
            with open(path, "w", newline="", encoding="utf-8") as file:
                file.write(",".join(fetch_stars.COLUMNS) + "\n")
                file.write("2026-07-16T12:00:00Z,owner/repo\n")

            with self.assertRaisesRegex(ValueError, "does not match the CSV header"):
                fetch_stars.read_incremental_csv(path)

    def test_malformed_duplicate_and_unsorted_csvs_are_rejected(self):
        valid = complete_row("2026-07-16T12:00:00Z", "owner/newest")
        cases = {
            "wrong header": ([valid], fetch_stars.COLUMNS[:-1]),
            "malformed timestamp": (
                [complete_row("not-a-time", "owner/newest")],
                fetch_stars.COLUMNS,
            ),
            "duplicate": ([valid, valid], fetch_stars.COLUMNS),
            "unsorted": (
                [
                    complete_row("2026-07-15T12:00:00Z", "owner/older"),
                    complete_row("2026-07-16T12:00:00Z", "owner/newest"),
                ],
                fetch_stars.COLUMNS,
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, (rows, fieldnames) in cases.items():
                with self.subTest(name=name):
                    path = os.path.join(tmp, name.replace(" ", "-") + ".csv")
                    self.write_csv(path, rows, fieldnames)
                    with self.assertRaisesRegex(ValueError, "--mode full"):
                        fetch_stars.read_incremental_csv(path)


class GraphqlCheckpointTests(unittest.TestCase):
    def test_stops_at_exact_checkpoint_and_keeps_same_timestamp_other_repo(self):
        checkpoint = ("2026-07-16T12:00:00Z", "owner/checkpoint")
        page = graphql_page(
            [
                edge("2026-07-17T12:00:00Z", "owner/new"),
                edge("2026-07-16T12:00:00Z", "owner/same-time-new"),
                edge(*checkpoint),
                edge("2026-07-15T12:00:00Z", "owner/must-not-be-read"),
            ],
            has_next=True,
            cursor="unused",
        )
        with mock.patch.object(fetch_stars, "gh_graphql", return_value=page) as gh:
            rows, _ = fetch_stars.fetch_stars_graphql(10, checkpoint=checkpoint)

        self.assertEqual(
            [row["repo_full_name"] for row in rows],
            ["owner/new", "owner/same-time-new"],
        )
        gh.assert_called_once()

    def test_follows_cursor_until_checkpoint_is_found_on_later_page(self):
        checkpoint = ("2026-07-16T12:00:00Z", "owner/checkpoint")
        pages = [
            graphql_page(
                [edge("2026-07-18T12:00:00Z", "owner/newest")],
                has_next=True,
                cursor="page-two",
            ),
            graphql_page(
                [
                    edge("2026-07-17T12:00:00Z", "owner/newer"),
                    edge(*checkpoint),
                ]
            ),
        ]
        with mock.patch.object(fetch_stars, "gh_graphql", side_effect=pages) as gh:
            rows, _ = fetch_stars.fetch_stars_graphql(10, checkpoint=checkpoint)

        self.assertEqual(
            [row["repo_full_name"] for row in rows],
            ["owner/newest", "owner/newer"],
        )
        self.assertEqual(gh.call_count, 2)
        self.assertEqual(gh.call_args_list[1].args[1]["after"], "page-two")

    def test_fails_if_results_fall_below_checkpoint_before_exact_match(self):
        checkpoint = ("2026-07-16T12:00:00Z", "owner/checkpoint")
        page = graphql_page([edge("2026-07-15T12:00:00Z", "owner/older")])
        with mock.patch.object(fetch_stars, "gh_graphql", return_value=page):
            with self.assertRaisesRegex(RuntimeError, "--mode full"):
                fetch_stars.fetch_stars_graphql(10, checkpoint=checkpoint)

    def test_fails_if_pagination_ends_without_checkpoint(self):
        checkpoint = ("2026-07-16T12:00:00Z", "owner/checkpoint")
        page = graphql_page([edge("2026-07-17T12:00:00Z", "owner/new")])
        with mock.patch.object(fetch_stars, "gh_graphql", return_value=page):
            with self.assertRaisesRegex(RuntimeError, "--mode full"):
                fetch_stars.fetch_stars_graphql(10, checkpoint=checkpoint)


class MainTests(unittest.TestCase):
    def test_incremental_no_change_does_not_rewrite_output(self):
        args = mock.Mock(output="stars.csv", mode="incremental", topic_limit=10)
        existing = [complete_row("2026-07-16T12:00:00Z", "owner/checkpoint")]
        checkpoint = ("2026-07-16T12:00:00Z", "owner/checkpoint")
        with (
            mock.patch.object(fetch_stars, "parse_args", return_value=args),
            mock.patch.object(
                fetch_stars, "read_incremental_csv", return_value=(existing, checkpoint)
            ),
            mock.patch.object(
                fetch_stars, "fetch_stars_graphql", return_value=([], None)
            ),
            mock.patch.object(fetch_stars, "write_csv_atomic") as write,
        ):
            self.assertEqual(fetch_stars.main(), 0)

        write.assert_not_called()

    def test_full_mode_rebuilds_without_reading_checkpoint(self):
        args = mock.Mock(output="stars.csv", mode="full", topic_limit=10)
        fetched = [complete_row("2026-07-16T12:00:00Z", "owner/repo")]
        with (
            mock.patch.object(fetch_stars, "parse_args", return_value=args),
            mock.patch.object(fetch_stars, "read_incremental_csv") as read,
            mock.patch.object(
                fetch_stars, "fetch_stars_graphql", return_value=(fetched, None)
            ) as fetch,
            mock.patch.object(fetch_stars, "write_csv_atomic") as write,
        ):
            self.assertEqual(fetch_stars.main(), 0)

        read.assert_not_called()
        fetch.assert_called_once_with(10, checkpoint=None)
        write.assert_called_once()

    def test_incremental_filters_restarred_existing_repo_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "stars.csv")
            existing = [
                complete_row("2026-07-16T12:00:00Z", "owner/checkpoint"),
                complete_row("2026-07-14T12:00:00Z", "owner/restarred"),
            ]
            fetch_stars.write_csv_atomic(existing, output)
            args = mock.Mock(output=output, mode="incremental", topic_limit=10)
            page = graphql_page(
                [
                    edge("2026-07-18T12:00:00Z", "owner/new"),
                    edge("2026-07-17T12:00:00Z", "owner/restarred"),
                    edge("2026-07-16T12:00:00Z", "owner/checkpoint"),
                ]
            )

            with (
                mock.patch.object(fetch_stars, "parse_args", return_value=args),
                mock.patch.object(fetch_stars, "gh_graphql", return_value=page) as gh,
            ):
                self.assertEqual(fetch_stars.main(), 0)

            with open(output, newline="", encoding="utf-8") as file:
                rows = list(csv.DictReader(file))

        names = [row["repo_full_name"] for row in rows]
        self.assertEqual(names, ["owner/new", "owner/checkpoint", "owner/restarred"])
        self.assertEqual(names.count("owner/restarred"), 1)
        gh.assert_called_once()


if __name__ == "__main__":
    unittest.main()
