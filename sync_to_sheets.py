#!/usr/bin/env python3
import argparse
import csv
import json
import os
import sys

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


DEFAULT_CSV = "github_starred_repos.csv"
DEFAULT_SHEET = "Sheet1"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
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


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Sync a local CSV file containing GitHub starred repositories to Google Sheets."
    )
    parser.add_argument(
        "--csv",
        default=DEFAULT_CSV,
        help=f"Path to input CSV. Defaults to {DEFAULT_CSV}.",
    )
    parser.add_argument(
        "--spreadsheet-id",
        required=True,
        help="The Google Sheets spreadsheet ID to sync to.",
    )
    parser.add_argument(
        "--sheet-name",
        default=DEFAULT_SHEET,
        help=f"Tab name in the spreadsheet. Defaults to {DEFAULT_SHEET}.",
    )
    parser.add_argument(
        "--credentials",
        help="Path to the Google service account JSON key file. Can also be supplied via GOOGLE_SERVICE_ACCOUNT_KEY env var.",
    )
    parser.add_argument(
        "--mode",
        choices=("incremental", "full"),
        default="incremental",
        help="Sync only rows newer than the Sheet checkpoint, or replace all rows. Defaults to incremental.",
    )
    return parser.parse_args(argv)


def load_credentials(creds_path=None):
    env_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY")
    if env_json:
        try:
            info = json.loads(env_json)
            return service_account.Credentials.from_service_account_info(
                info, scopes=SCOPES
            )
        except Exception as exc:
            raise ValueError(
                f"Failed to parse GOOGLE_SERVICE_ACCOUNT_KEY env var as JSON: {exc}"
            ) from exc

    if creds_path:
        if not os.path.exists(creds_path):
            raise FileNotFoundError(f"Credentials file not found at: {creds_path}")
        return service_account.Credentials.from_service_account_file(
            creds_path, scopes=SCOPES
        )

    env_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if env_file:
        if not os.path.exists(env_file):
            raise FileNotFoundError(
                "Credentials file not found at GOOGLE_APPLICATION_CREDENTIALS "
                f"path: {env_file}"
            )
        return service_account.Credentials.from_service_account_file(
            env_file, scopes=SCOPES
        )

    raise ValueError(
        "No Google credentials provided. Please supply credentials file via --credentials, "
        "or set either the GOOGLE_SERVICE_ACCOUNT_KEY or "
        "GOOGLE_APPLICATION_CREDENTIALS environment variable."
    )


def read_csv(csv_path):
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV file not found at: {csv_path}")

    with open(csv_path, "r", newline="", encoding="utf-8") as file:
        values = list(csv.reader(file))

    if not values:
        raise ValueError("CSV is empty")
    if values[0] != COLUMNS:
        raise ValueError("CSV header does not match the expected GitHub stars columns")

    width = len(COLUMNS)
    for line_number, row in enumerate(values[1:], start=2):
        if len(row) != width:
            raise ValueError(
                f"CSV row {line_number} has {len(row)} columns; expected {width}"
            )
    return values


def column_letter(index):
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def a1_sheet_name(sheet_name):
    return "'" + sheet_name.replace("'", "''") + "'"


def normalize_row(row, width):
    return list(row[:width]) + [""] * max(0, width - len(row))


def checkpoint(row):
    return tuple(row[:2])


def cell_data(value):
    if value == "":
        return {}
    return {"userEnteredValue": {"stringValue": value}}


def row_data(row):
    return {"values": [cell_data(value) for value in row]}


def get_sheet_properties(spreadsheets, spreadsheet_id, sheet_name):
    metadata = spreadsheets.get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))",
    ).execute()
    for sheet in metadata.get("sheets", []):
        properties = sheet.get("properties", {})
        if properties.get("title") == sheet_name:
            return properties
    raise ValueError(f"Sheet tab not found: {sheet_name}")


def incremental_requests(spreadsheets, spreadsheet_id, sheet_name, sheet_id, values):
    width = len(COLUMNS)
    checkpoint_range = f"{a1_sheet_name(sheet_name)}!A1:{column_letter(width)}2"
    response = (
        spreadsheets.values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=checkpoint_range,
            majorDimension="ROWS",
        )
        .execute()
    )
    sheet_values = response.get("values", [])

    if not sheet_values or normalize_row(sheet_values[0], width) != COLUMNS:
        raise ValueError(
            "Sheet header does not match the expected columns; run again with --mode full"
        )
    if len(sheet_values) < 2 or not any(sheet_values[1]):
        raise ValueError(
            "Incremental sync requires an existing top data row; initialize the Sheet "
            "with --mode full"
        )

    top_row = normalize_row(sheet_values[1], width)
    top_checkpoint = checkpoint(top_row)
    if not all(top_checkpoint):
        raise ValueError(
            "Sheet top data row has an incomplete checkpoint; recover with --mode full"
        )

    matching_indexes = [
        index
        for index, row in enumerate(values[1:])
        if checkpoint(row) == top_checkpoint
    ]
    if len(matching_indexes) != 1:
        raise ValueError(
            "Sheet top-row checkpoint was not found exactly once in the CSV; "
            "no changes were made. Recover with --mode full"
        )

    new_rows = values[1 : matching_indexes[0] + 1]
    if not new_rows:
        return [], 0

    requests = [
        {
            "insertDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": 1,
                    "endIndex": 1 + len(new_rows),
                },
                "inheritFromBefore": False,
            }
        },
        {
            "updateCells": {
                "start": {"sheetId": sheet_id, "rowIndex": 1, "columnIndex": 0},
                "rows": [row_data(row) for row in new_rows],
                "fields": "userEnteredValue",
            }
        },
    ]
    return requests, len(new_rows)


def full_requests(properties, values):
    sheet_id = properties["sheetId"]
    grid = properties.get("gridProperties", {})
    row_count = grid.get("rowCount", 0)
    column_count = grid.get("columnCount", 0)
    required_rows = len(values)
    required_columns = len(COLUMNS)
    requests = []

    if row_count < required_rows:
        requests.append(
            {
                "appendDimension": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "length": required_rows - row_count,
                }
            }
        )
    if column_count < required_columns:
        requests.append(
            {
                "appendDimension": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "length": required_columns - column_count,
                }
            }
        )

    requests.append(
        {
            "updateCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": max(row_count, required_rows),
                    "startColumnIndex": 0,
                    "endColumnIndex": max(column_count, required_columns),
                },
                "rows": [row_data(row) for row in values],
                "fields": "userEnteredValue",
            }
        }
    )
    return requests


def sync(service, spreadsheet_id, sheet_name, values, mode):
    spreadsheets = service.spreadsheets()
    properties = get_sheet_properties(spreadsheets, spreadsheet_id, sheet_name)

    if mode == "incremental":
        requests, inserted_rows = incremental_requests(
            spreadsheets,
            spreadsheet_id,
            sheet_name,
            properties["sheetId"],
            values,
        )
        if not requests:
            return 0
    else:
        requests = full_requests(properties, values)
        inserted_rows = len(values) - 1

    spreadsheets.batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests},
    ).execute()
    return inserted_rows


def main(argv=None):
    args = parse_args(argv)

    try:
        values = read_csv(args.csv)
        creds = load_credentials(args.credentials)
        service = build("sheets", "v4", credentials=creds)
        changed_rows = sync(
            service,
            args.spreadsheet_id,
            args.sheet_name,
            values,
            args.mode,
        )
    except HttpError as err:
        print(f"Google Sheets API HTTP error: {err}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Sync failed: {exc}", file=sys.stderr)
        return 1

    if args.mode == "incremental":
        print(f"Incremental sync complete: inserted {changed_rows} new rows.")
    else:
        print(f"Full sync complete: rewrote {changed_rows} data rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
