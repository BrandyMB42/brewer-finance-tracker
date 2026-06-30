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

# A representative worksheet, mirroring the live sheet's real shape: a single
# tab whose row 3 is the header, a paid-off section, then a "THEORETICAL PAYOFFS"
# banner, then the active rows we sync. Note "Stephen's Citi" appears BOTH in the
# paid-off section (row 5) and the theoretical section (row 12) — only the latter
# may ever be written.
SHEET_ROWS: list[list[str]] = [
    ["Snowball Tracker", "", "", "", ""],                                    # 1 title
    ["", "", "", "", ""],                                                    # 2 blank
    ["Name of Creditor", "Amount Owed", "Minimum Monthly Payment", "Interest Rate", "Updated"],  # 3 header
    ["Chase Bank", "$0.00", "$0.00", "20.49%", "Updated 3/17"],             # 4 paid-off
    ["Stephen's Citi", "$0.00", "$0.00", "28.24%", "Updated 12/11"],        # 5 paid-off DUP
    ["Discover", "$0.00", "$0.00", "0%", "Updated 12/27"],                  # 6 paid-off non-plaid
    ["THEORETICAL PAYOFFS", "", "", "", ""],                                # 7 section banner
    ["My Chase Again", "100", "10", "19.49%", "Updated 6/29"],              # 8
    ["Stephen's Chase (Again)", "200", "20", "19.49%", "Updated 6/29"],     # 9
    ["My Citi Rewards #2", "300", "30", "26.49%", "Updated 6/29"],          # 10
    ["Wells Fargo Again", "400", "25", "28.49%", "Updated 6/29"],           # 11
    ["Stephen's Citi", "14000", "223", "0%", "Updated 6/29"],               # 12 theoretical DUP
    ["Music and Arts", "5530", "250", "0%", "Updated 6/29"],                # 13 non-plaid
    ["Van Repairs", "2682", "135", "0%", "Updated 6/29"],                   # 14 non-plaid
]

# 1-based column numbers matching the header row above.
COL_AMOUNT = 2
COL_MIN = 3
COL_UPDATED = 5

# Rows (1-based) that must remain untouched: every paid-off row (incl. the
# duplicate Stephen's Citi at row 5) and the non-Plaid theoretical rows.
NON_PLAID_ROWS = {4, 5, 6, 13, 14}

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
    # "My Chase Again" is row 8 in the layout above.
    assert updates[(8, COL_AMOUNT)] == 1234.56
    assert updates[(8, COL_MIN)] == 60.0
    assert updates[(8, COL_UPDATED)] == EXPECTED_STAMP


def test_non_plaid_rows_remain_untouched() -> None:
    """Rows for debts Plaid is not connected to are never written."""
    worksheet = _make_worksheet()
    # Provide accounts for every mapped institution.
    accounts = [
        _account("Chase - Disney Premier (label TBD)", 1000.0),
        _account("Citi - Rewards (label TBD)", 500.0),
        _account("Wells Fargo - (label TBD)", 1500.0),
    ]

    accounts.append(_account("Citi - Rewards #2 (label TBD)", 14000.0))  # Stephen's Citi
    accounts.append(_account("Chase - Disney Premier #2 (label TBD)", 2000.0))  # Stephen's Chase

    snowball_sync.update_snowball_sheet(worksheet, accounts, now=FIXED_NOW)

    touched_rows = {row for (row, _col) in _updates_by_cell(worksheet)}
    assert touched_rows.isdisjoint(NON_PLAID_ROWS)


def test_duplicate_creditor_updates_theoretical_not_paid_off() -> None:
    """A creditor that also exists in the paid-off section updates only the
    theoretical-section row, never the historical $0.00 one."""
    worksheet = _make_worksheet()
    # Maps to "Stephen's Citi", which appears at row 5 (paid off) and row 12.
    accounts = [_account("Citi - Rewards #2 (label TBD)", 14000.0, 223.0)]

    snowball_sync.update_snowball_sheet(worksheet, accounts, now=FIXED_NOW)

    updates = _updates_by_cell(worksheet)
    assert updates[(12, COL_AMOUNT)] == 14000.0
    touched_rows = {row for (row, _col) in updates}
    assert 5 not in touched_rows


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
        "plaid-access-token-citibank-online": "token-citi",
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
    """_find_creditor_row resolves the right 1-based row within the section."""
    rows = [list(r) for r in SHEET_ROWS]
    section_start = 6  # 0-based index of the "THEORETICAL PAYOFFS" banner (row 7)
    assert snowball_sync._find_creditor_row(rows, section_start, 1, "Wells Fargo Again") == 11
    # Resolves the theoretical row (12), not the paid-off duplicate (5).
    assert snowball_sync._find_creditor_row(rows, section_start, 1, "Stephen's Citi") == 12
    assert snowball_sync._find_creditor_row(rows, section_start, 1, "Not A Creditor") is None


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
    # "Citi - Rewards" maps to "My Citi Rewards #2" — row 10.
    assert updates[(10, COL_AMOUNT)] == 600.0
    assert (10, COL_MIN) not in updates
    assert updates[(10, COL_UPDATED)] == EXPECTED_STAMP


def test_missing_header_raises() -> None:
    """A worksheet without the expected headers raises a clear error."""
    worksheet = MagicMock()
    worksheet.get_all_values.return_value = [["nope", "still nope"]]

    with pytest.raises(snowball_sync.SnowballSyncError):
        snowball_sync.update_snowball_sheet(worksheet, [_account("x", 1.0)], now=FIXED_NOW)


def test_missing_section_banner_raises() -> None:
    """Headers present but no section banner raises a clear error."""
    worksheet = MagicMock()
    worksheet.get_all_values.return_value = [
        ["Name of Creditor", "Amount Owed", "Minimum Monthly Payment", "Interest Rate", "Updated"],
        ["My Chase Again", "1", "2", "3", "x"],
    ]

    with pytest.raises(snowball_sync.SnowballSyncError):
        snowball_sync.update_snowball_sheet(
            worksheet,
            [_account("Chase - Disney Premier (label TBD)", 1.0)],
            now=FIXED_NOW,
        )


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
