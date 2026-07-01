"""Tests for the Google Sheets writer.

The gspread client is mocked so tests run without credentials or network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from brewer_finance_tracker import sheets_writer


def _mock_client_with_worksheet() -> tuple[MagicMock, MagicMock]:
    """Build a mock gspread client whose worksheet is a MagicMock.

    Returns:
        A ``(client, worksheet)`` tuple for assertions.
    """
    worksheet = MagicMock()
    client = MagicMock()
    client.open_by_key.return_value.worksheet.return_value = worksheet
    return client, worksheet


def test_write_debt_balances_appends_rows() -> None:
    """Debt balances are appended as rows to the Debt Balances sheet."""
    client, worksheet = _mock_client_with_worksheet()
    balances: list[dict[str, Any]] = [
        {"date": "2026-06-01", "account": "Visa", "balance": -1200.50},
    ]

    with patch.object(sheets_writer, "_get_client", return_value=client):
        sheets_writer.write_debt_balances("sheet-1", balances)

    client.open_by_key.assert_called_once_with("sheet-1")
    client.open_by_key.return_value.worksheet.assert_called_once_with("Debt Balances")
    worksheet.append_rows.assert_called_once()
    appended_rows = worksheet.append_rows.call_args[0][0]
    assert appended_rows == [["2026-06-01", "Visa", -1200.50]]


def test_write_debt_balances_empty_is_noop() -> None:
    """An empty balances list does not call the Sheets API."""
    with patch.object(sheets_writer, "_get_client") as mock_get_client:
        sheets_writer.write_debt_balances("sheet-1", [])
    mock_get_client.assert_not_called()


def test_write_transactions_appends_rows() -> None:
    """Transactions are appended with all five expected columns."""
    client, worksheet = _mock_client_with_worksheet()
    txns: list[dict[str, Any]] = [
        {
            "date": "2026-06-10",
            "account": "Checking",
            "name": "Coffee Shop",
            "amount": 4.75,
            "category": "Food and Drink",
        },
    ]

    with patch.object(sheets_writer, "_get_client", return_value=client):
        sheets_writer.write_transactions("sheet-1", txns)

    appended = worksheet.append_rows.call_args[0][0]
    assert appended == [["2026-06-10", "Checking", "Coffee Shop", 4.75, "Food and Drink"]]


def test_write_transactions_missing_category_defaults_blank() -> None:
    """A transaction without a category writes an empty string."""
    client, worksheet = _mock_client_with_worksheet()
    txns: list[dict[str, Any]] = [
        {"date": "2026-06-10", "account": "Checking", "name": "ATM", "amount": 40.0},
    ]

    with patch.object(sheets_writer, "_get_client", return_value=client):
        sheets_writer.write_transactions("sheet-1", txns)

    appended = worksheet.append_rows.call_args[0][0]
    assert appended[0][4] == ""


def test_write_monthly_totals_appends_new_month() -> None:
    """A month not yet present is appended."""
    client, worksheet = _mock_client_with_worksheet()
    worksheet.get_all_values.return_value = [["Month", "Income", "Expenses", "Net"]]
    totals: list[dict[str, Any]] = [
        {"month": "2026-06", "income": 5000, "expenses": 3200, "net": 1800},
    ]

    with patch.object(sheets_writer, "_get_client", return_value=client):
        sheets_writer.write_monthly_totals("sheet-1", totals)

    worksheet.append_rows.assert_called_once()
    worksheet.update.assert_not_called()


def test_write_monthly_totals_updates_existing_month() -> None:
    """A month already present is updated in place rather than appended."""
    client, worksheet = _mock_client_with_worksheet()
    worksheet.get_all_values.return_value = [
        ["Month", "Income", "Expenses", "Net"],
        ["2026-06", "100", "50", "50"],
    ]
    totals: list[dict[str, Any]] = [
        {"month": "2026-06", "income": 5000, "expenses": 3200, "net": 1800},
    ]

    with patch.object(sheets_writer, "_get_client", return_value=client):
        sheets_writer.write_monthly_totals("sheet-1", totals)

    worksheet.update.assert_called_once()
    worksheet.append_rows.assert_not_called()


def test_write_monthly_totals_empty_is_noop() -> None:
    """An empty totals list does not call the Sheets API."""
    with patch.object(sheets_writer, "_get_client") as mock_get_client:
        sheets_writer.write_monthly_totals("sheet-1", [])
    mock_get_client.assert_not_called()


def test_get_client_uses_application_default_credentials() -> None:
    """The client authenticates via ADC (runtime identity), not a stored key."""
    fake_creds = MagicMock()
    fake_client = MagicMock()
    with (
        patch("google.auth.default", return_value=(fake_creds, "proj")) as mock_default,
        patch.object(sheets_writer.gspread, "authorize", return_value=fake_client) as mock_auth,
    ):
        result = sheets_writer._get_client()

    mock_default.assert_called_once_with(scopes=sheets_writer._SCOPES)
    mock_auth.assert_called_once_with(fake_creds)
    assert result is fake_client
