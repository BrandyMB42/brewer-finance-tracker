"""Google Sheets writer for debt balances, transactions, and monthly totals.

Authentication uses a GCP service account whose JSON key is stored in Secret
Manager.  The service account must be granted Editor (or a custom Sheets role)
on each spreadsheet it writes to.

Secret required in Secret Manager
-----------------------------------
- ``sheets-service-account-json``  Service account key JSON for the Sheets API

Sheet layout expected by this module
--------------------------------------
- Sheet ``Debt Balances``    — columns: Date | Account | Balance
- Sheet ``Transactions``     — columns: Date | Account | Name | Amount | Category
- Sheet ``Monthly Totals``   — columns: Month | Income | Expenses | Net
"""

from __future__ import annotations

import json
import logging
from typing import Any

import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import ValueInputOption

from .config import Config
from .secrets.manager import get_secret

logger = logging.getLogger(__name__)

_SCOPES: list[str] = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _get_client() -> gspread.Client:
    """Build an authenticated gspread client using credentials from Secret Manager.

    Returns:
        An authorised :class:`gspread.Client` instance.

    Raises:
        ValueError: If ``GCP_PROJECT_ID`` is not set.
        google.api_core.exceptions.GoogleAPICallError: If the secret cannot be
            accessed.
        json.JSONDecodeError: If the stored secret is not valid JSON.
    """
    project_id = Config.GCP_PROJECT_ID
    if not project_id:
        raise ValueError("GCP_PROJECT_ID must be set to fetch Sheets credentials")

    sa_json_str = get_secret(project_id, "sheets-service-account-json")
    sa_info: dict[str, Any] = json.loads(sa_json_str)
    credentials = Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
        sa_info, scopes=_SCOPES
    )
    return gspread.authorize(credentials)


def write_debt_balances(
    spreadsheet_id: str,
    balances: list[dict[str, Any]],
) -> None:
    """Append current debt balances to the *Debt Balances* sheet.

    Each row in *balances* must have the keys ``date``, ``account``, and
    ``balance``.  Rows are appended below any existing data so the sheet
    accumulates a full history.

    Args:
        spreadsheet_id: The Google Sheets document ID from its URL.
        balances: List of balance dicts, one per account.

    Raises:
        gspread.exceptions.SpreadsheetNotFound: If the spreadsheet ID is wrong.
        KeyError: If a balance dict is missing a required field.
    """
    if not balances:
        logger.info("write_debt_balances called with empty list — nothing to write")
        return

    client = _get_client()
    sheet = client.open_by_key(spreadsheet_id).worksheet("Debt Balances")

    rows = [[b["date"], b["account"], b["balance"]] for b in balances]
    sheet.append_rows(rows, value_input_option=ValueInputOption.user_entered)

    logger.info(
        "Wrote debt balances to Sheets",
        extra={"spreadsheet_id": spreadsheet_id, "row_count": len(rows)},
    )


def write_transactions(
    spreadsheet_id: str,
    transactions: list[dict[str, Any]],
) -> None:
    """Append transactions to the *Transactions* sheet.

    Each dict in *transactions* must have the keys ``date``, ``account``,
    ``name``, ``amount``, and ``category``.

    Args:
        spreadsheet_id: The Google Sheets document ID.
        transactions: List of transaction dicts from the Plaid Transactions API.

    Raises:
        gspread.exceptions.SpreadsheetNotFound: If the spreadsheet ID is wrong.
        KeyError: If a transaction dict is missing a required field.
    """
    if not transactions:
        logger.info("write_transactions called with empty list — nothing to write")
        return

    client = _get_client()
    sheet = client.open_by_key(spreadsheet_id).worksheet("Transactions")

    rows = [
        [
            t["date"],
            t["account"],
            t["name"],
            t["amount"],
            t.get("category", ""),
        ]
        for t in transactions
    ]
    sheet.append_rows(rows, value_input_option=ValueInputOption.user_entered)

    logger.info(
        "Wrote transactions to Sheets",
        extra={"spreadsheet_id": spreadsheet_id, "row_count": len(rows)},
    )


def write_monthly_totals(
    spreadsheet_id: str,
    totals: list[dict[str, Any]],
) -> None:
    """Upsert monthly income/expense summaries into the *Monthly Totals* sheet.

    Each dict in *totals* must have the keys ``month`` (``YYYY-MM`` format),
    ``income``, ``expenses``, and ``net``.  Existing rows whose ``month`` value
    matches are overwritten; new months are appended.

    Args:
        spreadsheet_id: The Google Sheets document ID.
        totals: List of monthly summary dicts.

    Raises:
        gspread.exceptions.SpreadsheetNotFound: If the spreadsheet ID is wrong.
        KeyError: If a totals dict is missing a required field.
    """
    if not totals:
        logger.info("write_monthly_totals called with empty list — nothing to write")
        return

    client = _get_client()
    sheet = client.open_by_key(spreadsheet_id).worksheet("Monthly Totals")

    existing: list[list[str]] = sheet.get_all_values()
    month_to_row: dict[str, int] = {
        row[0]: idx + 1 for idx, row in enumerate(existing) if row
    }

    for total in totals:
        row_data = [total["month"], total["income"], total["expenses"], total["net"]]
        if total["month"] in month_to_row:
            row_num = month_to_row[total["month"]]
            # gspread 6.x signature: update(values, range_name, ...).
            sheet.update(
                [row_data],
                f"A{row_num}:D{row_num}",
                value_input_option=ValueInputOption.user_entered,
            )
            logger.debug("Updated monthly total row", extra={"month": total["month"], "row": row_num})
        else:
            sheet.append_rows([row_data], value_input_option=ValueInputOption.user_entered)
            logger.debug("Appended monthly total row", extra={"month": total["month"]})

    logger.info(
        "Wrote monthly totals to Sheets",
        extra={"spreadsheet_id": spreadsheet_id, "row_count": len(totals)},
    )
