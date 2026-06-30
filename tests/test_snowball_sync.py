"""Tests for the snowball sheet sync.

Plaid and gspread are mocked so tests run without credentials or network.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from brewer_finance_tracker import snowball_sync

# A representative worksheet layout: a title row, a header row, then creditor
# rows — including non-Plaid debts that must never be written to.
SHEET_ROWS: list[list[str]] = [
    ["THEORETICAL PAYOFFS", "", "", "", ""],
    ["Creditor", "Amount Owed", "Minimum Monthly Payment", "APR", "Updated"],
    ["My Chase Again", "1000", "50", "22%", "2026-01-01"],
    ["Stephen's Chase (Again)", "2000", "75", "24%", "2026-01-01"],
    ["My Citi Rewards #2", "500", "35", "19%", "2026-01-01"],
    ["Stephen's Citi", "750", "40", "21%", "2026-01-01"],
    ["Wells Fargo Again", "1500", "60", "18%", "2026-01-01"],
    ["Discover", "300", "25", "17%", "2026-01-01"],
    ["CareCredit", "900", "45", "0%", "2026-01-01"],
    ["Navient/Aidvantage", "12000", "150", "5%", "2026-01-01"],
]

# 1-based column numbers matching the header row above.
COL_AMOUNT = 2
COL_MIN = 3
COL_UPDATED = 5

# Rows (1-based) of the non-Plaid debts that must remain untouched.
NON_PLAID_ROWS = {8, 9, 10}

FIXED_NOW = datetime(2026, 6, 30, 14, 30, 0, tzinfo=timezone.utc)
EXPECTED_STAMP = "Auto-synced 2026-06-30 14:30:00 UTC"


def _make_worksheet() -> MagicMock:
    """A mock worksheet returning the canned layout from get_all_values."""
    worksheet = MagicMock()
    worksheet.get_all_values.return_value = [list(row) for row in SHEET_ROWS]
    return worksheet


def _account(name: str, balance: float, minimum: float | None = 99.0) -> dict[str, Any]:
    return {"name": name, "balance": balance, "minimum_payment": minimum, "apr": 20.0}


def _updates_by_cell(worksheet: MagicMock) -> dict[tuple[int, int], Any]:
    """Collapse update_cell calls into a {(row, col): value} dict."""
    updates: dict[tuple[int, int], Any] = {}
    for call in worksheet.update_cell.call_args_list:
        row, col, value = call.args
        updates[(row, col)] = value
    return updates


def test_successful_sync_updates_correct_rows() -> None:
    """A matched Plaid account updates its row's amount, minimum, and timestamp."""
    worksheet = _make_worksheet()
    accounts = [_account("Chase - Disney Premier (label TBD)", 1234.56, 60.0)]

    updated = snowball_sync.update_snowball_sheet(worksheet, accounts, now=FIXED_NOW)

    assert updated == ["My Chase Again"]
    updates = _updates_by_cell(worksheet)
    # "My Chase Again" is row 3 in the layout above.
    assert updates[(3, COL_AMOUNT)] == 1234.56
    assert updates[(3, COL_MIN)] == 60.0
    assert updates[(3, COL_UPDATED)] == EXPECTED_STAMP


def test_non_plaid_rows_remain_untouched() -> None:
    """Rows for debts Plaid is not connected to are never written."""
    worksheet = _make_worksheet()
    # Provide accounts for every mapped institution.
    accounts = [
        _account("Chase - Disney Premier (label TBD)", 1000.0),
        _account("Citi - Rewards (label TBD)", 500.0),
        _account("Wells Fargo - (label TBD)", 1500.0),
    ]

    snowball_sync.update_snowball_sheet(worksheet, accounts, now=FIXED_NOW)

    touched_rows = {row for (row, _col) in _updates_by_cell(worksheet)}
    assert touched_rows.isdisjoint(NON_PLAID_ROWS)


def test_partial_failure_still_updates_others() -> None:
    """If one institution's Plaid call fails, the others still sync."""

    def fake_fetch(_client: Any, access_token: str) -> list[dict[str, Any]]:
        if access_token == "token-citi":
            raise RuntimeError("Plaid is down for Citi")
        if access_token == "token-chase":
            return [_account("Chase - Disney Premier (label TBD)", 1000.0)]
        return [_account("Wells Fargo - (label TBD)", 1500.0)]

    token_for = {
        "plaid-access-token-chase": "token-chase",
        "plaid-access-token-citi": "token-citi",
        "plaid-access-token-wells-fargo": "token-wells",
    }

    with (
        patch.object(snowball_sync.Config, "GCP_PROJECT_ID", "proj"),
        patch.object(snowball_sync, "_build_plaid_client", return_value=MagicMock()),
        patch.object(snowball_sync, "get_secret", side_effect=lambda _p, s: token_for[s]),
        patch.object(snowball_sync, "fetch_liabilities", side_effect=fake_fetch),
    ):
        accounts = snowball_sync.collect_plaid_accounts()

    names = {a["name"] for a in accounts}
    # Chase and Wells Fargo succeeded; Citi failed and was skipped.
    assert names == {
        "Chase - Disney Premier (label TBD)",
        "Wells Fargo - (label TBD)",
    }


def test_row_matching_by_creditor_name() -> None:
    """_find_creditor_row resolves the right 1-based row, regardless of column."""
    rows = [list(r) for r in SHEET_ROWS]
    # Header is at index 1 (0-based).
    assert snowball_sync._find_creditor_row(rows, 1, "Wells Fargo Again") == 7
    assert snowball_sync._find_creditor_row(rows, 1, "Stephen's Citi") == 6
    assert snowball_sync._find_creditor_row(rows, 1, "Not A Creditor") is None


def test_unmapped_account_is_skipped() -> None:
    """An account whose name isn't in the map writes nothing."""
    worksheet = _make_worksheet()
    accounts = [_account("Some Unknown Card", 42.0)]

    updated = snowball_sync.update_snowball_sheet(worksheet, accounts, now=FIXED_NOW)

    assert updated == []
    worksheet.update_cell.assert_not_called()


def test_missing_minimum_payment_skips_min_cell() -> None:
    """A null minimum payment leaves the minimum cell untouched."""
    worksheet = _make_worksheet()
    accounts = [_account("Citi - Rewards (label TBD)", 600.0, minimum=None)]

    snowball_sync.update_snowball_sheet(worksheet, accounts, now=FIXED_NOW)

    updates = _updates_by_cell(worksheet)
    # "Citi - Rewards" maps to "My Citi Rewards #2" — row 5.
    assert updates[(5, COL_AMOUNT)] == 600.0
    assert (5, COL_MIN) not in updates
    assert updates[(5, COL_UPDATED)] == EXPECTED_STAMP


def test_missing_header_raises() -> None:
    """A worksheet without the expected headers raises a clear error."""
    worksheet = MagicMock()
    worksheet.get_all_values.return_value = [["nope", "still nope"]]

    with pytest.raises(snowball_sync.SnowballSyncError):
        snowball_sync.update_snowball_sheet(worksheet, [_account("x", 1.0)], now=FIXED_NOW)


def test_fetch_liabilities_normalizes_and_joins_accounts() -> None:
    """fetch_liabilities joins accounts to credit liabilities and picks purchase APR."""
    response = {
        "accounts": [
            {"account_id": "a1", "name": "Chase Card", "balances": {"current": 1000.0}},
            # An account with no matching credit liability is ignored.
            {"account_id": "a2", "name": "Checking", "balances": {"current": 50.0}},
        ],
        "liabilities": {
            "credit": [
                {
                    "account_id": "a1",
                    "minimum_payment_amount": 35.0,
                    "aprs": [
                        {"apr_type": "balance_transfer_apr", "apr_percentage": 0.0},
                        {"apr_type": "purchase_apr", "apr_percentage": 22.99},
                    ],
                },
            ]
        },
    }
    client = MagicMock()
    client.liabilities_get.return_value = response

    result = snowball_sync.fetch_liabilities(client, "tok")

    assert result == [
        {"name": "Chase Card", "balance": 1000.0, "minimum_payment": 35.0, "apr": 22.99}
    ]


def test_extract_apr_falls_back_to_first_without_purchase_apr() -> None:
    """When no purchase APR is present, the first APR is used."""
    credit = {"aprs": [{"apr_type": "cash_apr", "apr_percentage": 25.0}]}
    assert snowball_sync._extract_apr(credit) == 25.0


def test_extract_apr_none_when_no_aprs() -> None:
    """No APRs yields None rather than raising."""
    assert snowball_sync._extract_apr({"aprs": []}) is None


def test_collect_requires_project_id() -> None:
    """collect_plaid_accounts fails fast when GCP_PROJECT_ID is unset."""
    with (
        patch.object(snowball_sync.Config, "GCP_PROJECT_ID", ""),
        pytest.raises(snowball_sync.SnowballSyncError),
    ):
        snowball_sync.collect_plaid_accounts()


def test_run_sync_orchestrates_fetch_and_update() -> None:
    """run_sync wires collect -> update and returns a summary."""
    worksheet = _make_worksheet()
    accounts = [_account("Chase - Disney Premier (label TBD)", 1000.0)]

    with (
        patch.object(snowball_sync, "collect_plaid_accounts", return_value=accounts),
        patch.object(snowball_sync, "_open_worksheet", return_value=worksheet),
    ):
        summary = snowball_sync.run_sync()

    assert summary == {
        "accounts_fetched": 1,
        "rows_updated": 1,
        "updated": ["My Chase Again"],
    }


def test_http_entry_point_returns_json_summary() -> None:
    """The HTTP entry point returns a 200 JSON response."""
    with patch.object(snowball_sync, "run_sync", return_value={"rows_updated": 2}):
        body, status, headers = snowball_sync.sync_snowball_sheet(MagicMock())

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"rows_updated": 2}


def test_scheduled_entry_point_runs_sync() -> None:
    """The Pub/Sub entry point triggers a sync and ignores its payload."""
    with patch.object(snowball_sync, "run_sync") as mock_run:
        snowball_sync.sync_snowball_sheet_scheduled({"data": "ignored"}, None)
    mock_run.assert_called_once_with()
