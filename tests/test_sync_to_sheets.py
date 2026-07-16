import sys
import types
import unittest
from pathlib import Path


google = types.ModuleType("google")
google.oauth2 = types.ModuleType("google.oauth2")
google.oauth2.service_account = types.ModuleType("google.oauth2.service_account")
googleapiclient = types.ModuleType("googleapiclient")
googleapiclient.discovery = types.ModuleType("googleapiclient.discovery")
googleapiclient.discovery.build = lambda *args, **kwargs: None
googleapiclient.errors = types.ModuleType("googleapiclient.errors")
googleapiclient.errors.HttpError = type("HttpError", (Exception,), {})
sys.modules.setdefault("google", google)
sys.modules.setdefault("google.oauth2", google.oauth2)
sys.modules.setdefault("google.oauth2.service_account", google.oauth2.service_account)
sys.modules.setdefault("googleapiclient", googleapiclient)
sys.modules.setdefault("googleapiclient.discovery", googleapiclient.discovery)
sys.modules.setdefault("googleapiclient.errors", googleapiclient.errors)

import sync_to_sheets as syncer


def make_row(starred_at, repo_full_name):
    return [starred_at, repo_full_name] + [""] * (len(syncer.COLUMNS) - 2)


class FakeRequest:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class FakeValues:
    def __init__(self, response):
        self.response = response
        self.get_calls = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return FakeRequest(self.response)


class FakeSpreadsheets:
    def __init__(self, sheet_values, row_count=100, column_count=26):
        self.values_api = FakeValues({"values": sheet_values})
        self.metadata = {
            "sheets": [
                {
                    "properties": {
                        "sheetId": 123,
                        "title": "Sheet1",
                        "gridProperties": {
                            "rowCount": row_count,
                            "columnCount": column_count,
                        },
                    }
                }
            ]
        }
        self.batch_calls = []

    def get(self, **kwargs):
        return FakeRequest(self.metadata)

    def values(self):
        return self.values_api

    def batchUpdate(self, **kwargs):
        self.batch_calls.append(kwargs)
        return FakeRequest({})


class FakeService:
    def __init__(self, spreadsheets):
        self.api = spreadsheets

    def spreadsheets(self):
        return self.api


class ParseArgsTests(unittest.TestCase):
    def test_mode_defaults_to_incremental(self):
        args = syncer.parse_args(["--spreadsheet-id", "spreadsheet"])
        self.assertEqual(args.mode, "incremental")

    def test_full_mode_is_available(self):
        args = syncer.parse_args(
            ["--spreadsheet-id", "spreadsheet", "--mode", "full"]
        )
        self.assertEqual(args.mode, "full")


class WorkflowContractTests(unittest.TestCase):
    def test_one_sync_mode_is_passed_to_fetch_and_sheet_steps(self):
        workflow = Path(".github/workflows/sync-stars.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("sync_mode:", workflow)
        self.assertIn("inputs.sync_mode", workflow)
        self.assertNotIn("inputs.mode", workflow)
        self.assertEqual(workflow.count('--mode "$SYNC_MODE"'), 2)


class IncrementalSyncTests(unittest.TestCase):
    def test_inserts_only_rows_before_exact_top_checkpoint_in_one_batch(self):
        current_top = make_row("2026-07-14T10:00:00Z", "owner/current")
        values = [
            syncer.COLUMNS,
            make_row("2026-07-16T10:00:00Z", "owner/newest"),
            make_row("2026-07-15T10:00:00Z", "owner/newer"),
            current_top,
            make_row("2026-07-13T10:00:00Z", "owner/older"),
        ]
        spreadsheets = FakeSpreadsheets([syncer.COLUMNS, current_top])

        changed = syncer.sync(
            FakeService(spreadsheets), "spreadsheet", "Sheet1", values, "incremental"
        )

        self.assertEqual(changed, 2)
        self.assertEqual(len(spreadsheets.batch_calls), 1)
        requests = spreadsheets.batch_calls[0]["body"]["requests"]
        self.assertEqual(len(requests), 2)
        self.assertEqual(
            requests[0]["insertDimension"]["range"],
            {
                "sheetId": 123,
                "dimension": "ROWS",
                "startIndex": 1,
                "endIndex": 3,
            },
        )
        written_rows = requests[1]["updateCells"]["rows"]
        self.assertEqual(
            written_rows[0]["values"][1]["userEnteredValue"]["stringValue"],
            "owner/newest",
        )
        self.assertEqual(
            written_rows[1]["values"][1]["userEnteredValue"]["stringValue"],
            "owner/newer",
        )

    def test_checkpoint_uses_exact_identity_tuple(self):
        top = make_row("2026-07-14T10:00:00Z", "owner/current")
        values = [syncer.COLUMNS, make_row(top[0], "owner/different")]
        spreadsheets = FakeSpreadsheets([syncer.COLUMNS, top])

        with self.assertRaisesRegex(ValueError, "not found exactly once"):
            syncer.sync(
                FakeService(spreadsheets),
                "spreadsheet",
                "Sheet1",
                values,
                "incremental",
            )

        self.assertEqual(spreadsheets.batch_calls, [])

    def test_matching_top_row_is_a_noop(self):
        top = make_row("2026-07-14T10:00:00Z", "owner/current")
        spreadsheets = FakeSpreadsheets([syncer.COLUMNS, top])

        changed = syncer.sync(
            FakeService(spreadsheets),
            "spreadsheet",
            "Sheet1",
            [syncer.COLUMNS, top],
            "incremental",
        )

        self.assertEqual(changed, 0)
        self.assertEqual(spreadsheets.batch_calls, [])

    def test_header_mismatch_fails_without_mutation(self):
        top = make_row("2026-07-14T10:00:00Z", "owner/current")
        wrong_header = list(syncer.COLUMNS)
        wrong_header[-1] = "unexpected"
        spreadsheets = FakeSpreadsheets([wrong_header, top])

        with self.assertRaisesRegex(ValueError, "header"):
            syncer.sync(
                FakeService(spreadsheets),
                "spreadsheet",
                "Sheet1",
                [syncer.COLUMNS, top],
                "incremental",
            )

        self.assertEqual(spreadsheets.batch_calls, [])

    def test_header_only_sheet_requires_full_mode_without_mutation(self):
        spreadsheets = FakeSpreadsheets([syncer.COLUMNS])

        with self.assertRaisesRegex(ValueError, "--mode full"):
            syncer.sync(
                FakeService(spreadsheets),
                "spreadsheet",
                "Sheet1",
                [syncer.COLUMNS, make_row("2026-07-16", "owner/repo")],
                "incremental",
            )

        self.assertEqual(spreadsheets.batch_calls, [])


class FullSyncTests(unittest.TestCase):
    def test_full_mode_clears_and_rewrites_grid_in_one_atomic_batch(self):
        values = [syncer.COLUMNS, make_row("2026-07-16", "owner/repo")]
        spreadsheets = FakeSpreadsheets([], row_count=10, column_count=20)

        changed = syncer.sync(
            FakeService(spreadsheets), "spreadsheet", "Sheet1", values, "full"
        )

        self.assertEqual(changed, 1)
        self.assertEqual(len(spreadsheets.batch_calls), 1)
        requests = spreadsheets.batch_calls[0]["body"]["requests"]
        self.assertEqual(len(requests), 1)
        update = requests[0]["updateCells"]
        self.assertEqual(
            update["range"],
            {
                "sheetId": 123,
                "startRowIndex": 0,
                "endRowIndex": 10,
                "startColumnIndex": 0,
                "endColumnIndex": 20,
            },
        )
        self.assertEqual(len(update["rows"]), 2)
        self.assertEqual(update["fields"], "userEnteredValue")

    def test_full_mode_expands_small_grid_in_same_batch(self):
        values = [syncer.COLUMNS, make_row("2026-07-16", "owner/repo")]
        spreadsheets = FakeSpreadsheets([], row_count=1, column_count=2)

        syncer.sync(
            FakeService(spreadsheets), "spreadsheet", "Sheet1", values, "full"
        )

        requests = spreadsheets.batch_calls[0]["body"]["requests"]
        self.assertEqual(requests[0]["appendDimension"]["dimension"], "ROWS")
        self.assertEqual(requests[1]["appendDimension"]["dimension"], "COLUMNS")
        self.assertIn("updateCells", requests[2])


if __name__ == "__main__":
    unittest.main()
