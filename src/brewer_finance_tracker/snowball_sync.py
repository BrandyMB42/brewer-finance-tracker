"""Sync live Plaid liability data into the snowball debt-tracking Google Sheet.

This module is deployed as a **scheduled Cloud Function**. On each run it:

1. Reads the per-institution Plaid access tokens from Secret Manager (using the
   shared :func:`get_secret` pattern) and calls Plaid's ``/liabilities/get``
   endpoint for each connected institution (Chase, Citi, Wells Fargo).
2. Matches every returned credit-card account to a creditor row in the
   "THEORETICAL PAYOFFS" section of the ``Sheet1`` worksheet via
   :data:`PLAID_TO_SHEET_ROW_MAP`.
3. Updates only the matched rows' *Amount Owed*, *Minimum Monthly Payment*, and
   *Updated* cells. Rows for debts Plaid is not connected to (Amazon, Music and
   Arts, Van Repairs, CareCredit, Navient/Aidvantage, Discover, ...) are never
   touched.

The sheet has a single ``Sheet1`` tab; "THEORETICAL PAYOFFS" is a section banner
(in the *Name of Creditor* column) partway down it, not a separate tab. Matching
is therefore anchored to the rows *below* that banner and compares only the
creditor column — several creditor names (e.g. "Stephen's Citi") also appear in
the paid-off section above, and those historical rows must never be overwritten.

Resilience: a Plaid failure for one institution is logged and skipped so the
remaining institutions still sync — one bad token never blocks the whole run.

Secrets required in Secret Manager
-----------------------------------
- ``plaid-client-id`` / ``plaid-secret``        Plaid API credentials
- ``plaid-access-token-{institution}-*``        One access-token secret per card
  (Plaid Item), e.g. ``plaid-access-token-chase-7254``. All secrets matching each
  institution prefix (see ``INSTITUTION_SECRET_PREFIXES``) are read.

Sheets access uses the function's own identity (Application Default Credentials),
not a stored key — share the spreadsheet with the runtime service account.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import functions_framework
from plaid.api import plaid_api
from plaid.model.liabilities_get_request import LiabilitiesGetRequest

from .config import Config
from .plaid_link import _build_client as _build_plaid_client
from .secrets.manager import get_secret
from .sheets_writer import _get_client as _get_sheets_client

logger = logging.getLogger(__name__)

#: The snowball tracking spreadsheet (from its share URL).
SPREADSHEET_ID = "1icoDUaECtr2272qWxeYntdgNrU6VpEcrqbU1aHJIdsw"

#: The (only) worksheet/tab in the spreadsheet.
WORKSHEET_NAME = "Sheet1"

#: Banner text (in the creditor column) marking the start of the section we
#: write to. Only rows below this banner are eligible for matching.
SECTION_BANNER = "THEORETICAL PAYOFFS"

#: Institution display name -> Secret Manager id *prefix* for its access tokens.
#: Each institution may have MULTIPLE tokens (one Plaid Item per card), stored by
#: the setup tool as ``plaid-access-token-{slug}-{disambiguator}``. We list every
#: secret matching the prefix rather than assuming a single token per institution
#: (a bare ``plaid-access-token-{slug}`` with no suffix is also matched).
INSTITUTION_SECRET_PREFIXES: dict[str, str] = {
    "Chase": "plaid-access-token-chase",
    "Citi": "plaid-access-token-citibank-online",
    "Wells Fargo": "plaid-access-token-wells-fargo",
}

# ---------------------------------------------------------------------------
# Plaid account name -> snowball-sheet creditor row.
#
# IMPORTANT: the keys below are PLACEHOLDERS. The exact account names Plaid
# returns for each card are not known until the first real sync. Run the
# function once and read the "Encountered Plaid account" debug logs (emitted by
# update_snowball_sheet) to see the actual `account.name` values Plaid reports,
# then replace the keys here with those exact strings so the matching works.
# Until the keys match, those accounts are logged as "unmapped" and skipped —
# the function never guesses and never writes to a wrong row.
# ---------------------------------------------------------------------------
PLAID_TO_SHEET_ROW_MAP: dict[str, str] = {
    "Chase - Disney Premier (label TBD)": "My Chase Again",
    "Chase - Disney Premier #2 (label TBD)": "Stephen's Chase (Again)",
    "Citi - Rewards (label TBD)": "My Citi Rewards #2",
    "Citi - Rewards #2 (label TBD)": "Stephen's Citi",
    "Wells Fargo - (label TBD)": "Wells Fargo Again",
}

#: Column header text (row 3 of the worksheet) used to locate columns.
CREDITOR_HEADER = "Name of Creditor"
AMOUNT_OWED_HEADER = "Amount Owed"
MIN_PAYMENT_HEADER = "Minimum Monthly Payment"
UPDATED_HEADER = "Updated"


class SnowballSyncError(RuntimeError):
    """Raised when the sync cannot proceed (e.g. missing config or sheet layout)."""


def _model_get(obj: Any, key: str, default: Any = None) -> Any:
    """Safely read *key* from a Plaid model (or dict), returning *default* if absent.

    Plaid model objects raise on unset optional attributes rather than returning
    ``None``, so optional fields are read through this helper.
    """
    try:
        value = obj[key]
    except (KeyError, AttributeError, TypeError):
        return default
    return default if value is None else value


def _extract_apr(credit: Any) -> float | None:
    """Pick the most relevant APR from a Plaid credit liability.

    Prefers the purchase APR; falls back to the first APR present.
    """
    aprs = _model_get(credit, "aprs", []) or []
    if not aprs:
        return None
    chosen = next(
        (apr for apr in aprs if _model_get(apr, "apr_type") == "purchase_apr"),
        aprs[0],
    )
    percentage = _model_get(chosen, "apr_percentage")
    return float(percentage) if percentage is not None else None


def fetch_liabilities(
    client: plaid_api.PlaidApi, access_token: str
) -> list[dict[str, Any]]:
    """Fetch credit-card liabilities for one Plaid item.

    Args:
        client: An authenticated Plaid API client.
        access_token: The item access token for the institution.

    Returns:
        A list of normalized account dicts with the keys ``name``, ``balance``,
        ``minimum_payment``, and ``apr`` — one per credit-card account.
    """
    response = client.liabilities_get(
        LiabilitiesGetRequest(access_token=access_token)
    )
    accounts_by_id = {a["account_id"]: a for a in response["accounts"]}
    credit_liabilities = _model_get(response["liabilities"], "credit", []) or []

    accounts: list[dict[str, Any]] = []
    for credit in credit_liabilities:
        account = accounts_by_id.get(_model_get(credit, "account_id"))
        if account is None:
            continue
        accounts.append(
            {
                "name": _model_get(account, "name", ""),
                "balance": _model_get(account["balances"], "current"),
                "minimum_payment": _model_get(credit, "minimum_payment_amount"),
                "apr": _extract_apr(credit),
            }
        )
    return accounts


def collect_plaid_accounts() -> list[dict[str, Any]]:
    """Gather credit-card accounts across all connected institutions.

    Each institution is fetched independently: a failure for one (bad token,
    Plaid outage, etc.) is logged and skipped so the others still sync.

    Returns:
        A combined list of normalized account dicts (see :func:`fetch_liabilities`).

    Raises:
        SnowballSyncError: If ``GCP_PROJECT_ID`` is not configured.
    """
    project_id = Config.GCP_PROJECT_ID
    if not project_id:
        raise SnowballSyncError("GCP_PROJECT_ID must be set to fetch Plaid credentials")

    client = _build_plaid_client()
    accounts: list[dict[str, Any]] = []

    for institution, prefix in INSTITUTION_SECRET_PREFIXES.items():
        secret_ids = _list_token_secret_ids(project_id, prefix)
        if not secret_ids:
            logger.warning(
                "No access-token secrets found for institution",
                extra={"institution": institution, "prefix": prefix},
            )
            continue

        # Each secret is an independent Plaid Item; isolate per-token failures so
        # one bad token never blocks the others (or the other institutions).
        for secret_id in secret_ids:
            try:
                access_token = get_secret(project_id, secret_id)
                item_accounts = fetch_liabilities(client, access_token)
            except Exception:  # noqa: BLE001 - isolate one token's failure
                logger.exception(
                    "Failed to fetch liabilities; skipping token",
                    extra={"institution": institution, "secret_id": secret_id},
                )
                continue

            logger.info(
                "Fetched liabilities",
                extra={
                    "institution": institution,
                    "secret_id": secret_id,
                    "account_count": len(item_accounts),
                },
            )
            accounts.extend(item_accounts)

    return accounts


def _list_token_secret_ids(project_id: str, prefix: str) -> list[str]:
    """Return the ids of all access-token secrets for an institution *prefix*.

    Matches both a bare ``{prefix}`` secret and disambiguated
    ``{prefix}-{suffix}`` secrets (one per Plaid Item / card).

    Args:
        project_id: GCP project that owns the secrets.
        prefix: The ``plaid-access-token-{slug}`` prefix for an institution.

    Returns:
        Sorted secret ids (short names, not full resource paths).
    """
    from google.cloud import secretmanager  # imported here to avoid top-level cost

    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{project_id}"
    ids: list[str] = []
    for secret in client.list_secrets(request={"parent": parent, "filter": f"name:{prefix}"}):
        short = secret.name.rsplit("/", 1)[-1]
        if short == prefix or short.startswith(f"{prefix}-"):
            ids.append(short)
    return sorted(ids)


def _locate_layout(rows: list[list[str]]) -> dict[str, int]:
    """Locate the 1-based column numbers we read/write, keyed by header text.

    Args:
        rows: All worksheet values (``worksheet.get_all_values()``).

    Returns:
        A dict mapping each known header to its 1-based column number. Always
        includes the creditor and amount-owed columns; minimum-payment and
        updated columns are included when present.

    Raises:
        SnowballSyncError: If the header row cannot be found.
    """
    for row in rows:
        normalized = [cell.strip() for cell in row]
        if CREDITOR_HEADER in normalized and AMOUNT_OWED_HEADER in normalized:
            return {
                header: normalized.index(header) + 1
                for header in (
                    CREDITOR_HEADER,
                    AMOUNT_OWED_HEADER,
                    MIN_PAYMENT_HEADER,
                    UPDATED_HEADER,
                )
                if header in normalized
            }
    raise SnowballSyncError(
        f"Could not find a header row containing {CREDITOR_HEADER!r} and "
        f"{AMOUNT_OWED_HEADER!r} in worksheet {WORKSHEET_NAME!r}"
    )


def _find_section_start(rows: list[list[str]]) -> int:
    """Return the 0-based index of the ``SECTION_BANNER`` row.

    Raises:
        SnowballSyncError: If the banner is not found.
    """
    for index, row in enumerate(rows):
        if any(cell.strip() == SECTION_BANNER for cell in row):
            return index
    raise SnowballSyncError(
        f"Could not find the {SECTION_BANNER!r} section banner in "
        f"worksheet {WORKSHEET_NAME!r}"
    )


def _find_creditor_row(
    rows: list[list[str]], section_start: int, creditor_col: int, creditor: str
) -> int | None:
    """Return the 1-based row whose creditor cell matches *creditor*.

    Only rows *below* ``section_start`` are considered, and only the creditor
    column is compared — so identically-named paid-off rows above the section
    banner are never matched (and never overwritten).
    """
    col = creditor_col - 1  # 0-based index into each row
    for index in range(section_start + 1, len(rows)):
        row = rows[index]
        if col < len(row) and row[col].strip() == creditor:
            return index + 1
    return None


def update_snowball_sheet(
    worksheet: Any,
    accounts: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> list[str]:
    """Update matched creditor rows with live Plaid balances and minimums.

    Only rows whose creditor name is the mapped target of a Plaid account are
    touched; every other row (manual / non-Plaid debts) is left untouched.

    Args:
        worksheet: A gspread worksheet (or compatible object).
        accounts: Normalized Plaid account dicts from :func:`collect_plaid_accounts`.
        now: Timestamp to stamp into the *Updated* column. Defaults to UTC now;
             injectable for deterministic tests.

    Returns:
        The list of sheet creditor names that were updated.
    """
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S UTC")
    stamp = f"Auto-synced {timestamp}"

    rows = worksheet.get_all_values()
    columns = _locate_layout(rows)
    section_start = _find_section_start(rows)
    creditor_col = columns[CREDITOR_HEADER]

    updated: list[str] = []
    for account in accounts:
        plaid_name = account.get("name", "")
        # Debug line the user reads after the first run to fill in the row map.
        logger.debug(
            "Encountered Plaid account",
            extra={
                "plaid_account_name": plaid_name,
                "balance": account.get("balance"),
                "minimum_payment": account.get("minimum_payment"),
                "apr": account.get("apr"),
            },
        )

        creditor = PLAID_TO_SHEET_ROW_MAP.get(plaid_name)
        if creditor is None:
            logger.warning(
                "Plaid account has no row mapping; skipping (update PLAID_TO_SHEET_ROW_MAP)",
                extra={"plaid_account_name": plaid_name},
            )
            continue

        row_num = _find_creditor_row(rows, section_start, creditor_col, creditor)
        if row_num is None:
            logger.warning(
                "Mapped creditor not found in section; skipping",
                extra={"plaid_account_name": plaid_name, "creditor": creditor},
            )
            continue

        worksheet.update_cell(row_num, columns[AMOUNT_OWED_HEADER], account.get("balance"))
        minimum = account.get("minimum_payment")
        # Treat a missing OR zero minimum as "no data" — Plaid reports 0 for some
        # cards, and writing it would clobber a real minimum kept in the sheet.
        if MIN_PAYMENT_HEADER in columns and minimum:
            worksheet.update_cell(row_num, columns[MIN_PAYMENT_HEADER], minimum)
        if UPDATED_HEADER in columns:
            worksheet.update_cell(row_num, columns[UPDATED_HEADER], stamp)

        updated.append(creditor)
        logger.info(
            "Updated snowball row",
            extra={"creditor": creditor, "row": row_num, "balance": account.get("balance")},
        )

    logger.info("Snowball sync complete", extra={"rows_updated": len(updated)})
    return updated


def _open_worksheet() -> Any:
    """Open the snowball worksheet using the shared Sheets credentials."""
    client = _get_sheets_client()
    return client.open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET_NAME)


def run_sync() -> dict[str, Any]:
    """Run the full sync: fetch Plaid liabilities and update the sheet.

    Returns:
        A summary dict with the number of accounts fetched and rows updated.
    """
    accounts = collect_plaid_accounts()
    worksheet = _open_worksheet()
    updated = update_snowball_sheet(worksheet, accounts)
    summary = {
        "accounts_fetched": len(accounts),
        "rows_updated": len(updated),
        "updated": updated,
    }
    logger.info("run_sync finished", extra=summary)
    return summary


@functions_framework.http
def sync_snowball_sheet(request: Any) -> tuple[str, int, dict[str, str]]:
    """HTTP Cloud Function entry point.

    Args:
        request: The incoming Flask request (unused; the sync takes no input).

    Returns:
        A ``(body, status, headers)`` tuple with a JSON summary.
    """
    summary = run_sync()
    return json.dumps(summary), 200, {"Content-Type": "application/json"}


def sync_snowball_sheet_scheduled(event: Any, context: Any) -> None:
    """Pub/Sub (Cloud Scheduler) background Cloud Function entry point.

    Deploy with ``--trigger-topic`` and ``--signature-type=event`` (or the
    equivalent gen-2 CloudEvent wiring). The trigger payload is ignored — the
    schedule itself is the only signal needed.

    Args:
        event: The Pub/Sub event payload (unused).
        context: The event metadata (unused).
    """
    run_sync()
