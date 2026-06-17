"""Webhook event dispatcher — routes Plaid webhook events to typed handlers.

Plaid webhooks carry ``webhook_type`` and ``webhook_code`` fields rather than a
single ``type`` string.  The dispatcher builds a composite routing key of the
form ``"{WEBHOOK_TYPE}:{WEBHOOK_CODE}"`` (e.g. ``"TRANSACTIONS:SYNC_UPDATES_AVAILABLE"``).

Unregistered type+code combinations are acknowledged with HTTP 200 and a
structured WARNING so that Plaid does not retry them — this is intentional.

Plaid webhook_type values handled here
----------------------------------------
- TRANSACTIONS  — new/updated/removed transaction data
- LIABILITIES   — updated debt balances
- ITEM          — item-level status changes and errors

Reference: https://plaid.com/docs/api/webhooks/
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from flask import jsonify
from flask.typing import ResponseReturnValue

logger = logging.getLogger(__name__)

#: Signature shared by every registered webhook handler.
Handler = Callable[[dict[str, Any]], ResponseReturnValue]

_HANDLERS: dict[str, Handler] = {}


def register(webhook_type: str, webhook_code: str) -> Callable[[Handler], Handler]:
    """Decorator that registers *fn* as the handler for a Plaid event.

    Args:
        webhook_type: Plaid ``webhook_type`` value (e.g. ``"TRANSACTIONS"``).
        webhook_code: Plaid ``webhook_code`` value (e.g. ``"SYNC_UPDATES_AVAILABLE"``).

    Returns:
        A decorator that stores the function and returns it unchanged.
    """
    key = f"{webhook_type}:{webhook_code}"

    def decorator(fn: Handler) -> Handler:
        _HANDLERS[key] = fn
        return fn

    return decorator


def dispatch(payload: dict[str, Any]) -> ResponseReturnValue:
    """Route *payload* to the registered handler for its Plaid event type+code.

    Builds the routing key from the ``webhook_type`` and ``webhook_code`` fields
    in *payload*.  If no handler is registered for that combination, the event
    is acknowledged with HTTP 200 and a structured WARNING is logged — callers
    should not retry acknowledged events.

    Args:
        payload: Parsed JSON body from the incoming Plaid webhook request.

    Returns:
        A Flask ``ResponseReturnValue`` from the matched handler, or a 200
        acknowledgement for unhandled event combinations.
    """
    webhook_type: str = (payload.get("webhook_type") or "").upper()
    webhook_code: str = (payload.get("webhook_code") or "").upper()
    key = f"{webhook_type}:{webhook_code}"

    handler = _HANDLERS.get(key)
    if handler is None:
        logger.warning(
            "Received unhandled Plaid webhook — acknowledging without processing",
            extra={"webhook_type": webhook_type or "<missing>", "webhook_code": webhook_code or "<missing>"},
        )
        return jsonify({"status": "ignored", "webhook_type": webhook_type, "webhook_code": webhook_code}), 200

    logger.info(
        "Dispatching Plaid webhook",
        extra={"webhook_type": webhook_type, "webhook_code": webhook_code},
    )
    return handler(payload)


# ---------------------------------------------------------------------------
# TRANSACTIONS handlers
# ---------------------------------------------------------------------------


@register("TRANSACTIONS", "SYNC_UPDATES_AVAILABLE")
def _handle_transactions_sync(payload: dict[str, Any]) -> ResponseReturnValue:
    """New transactions are ready to be pulled via /transactions/sync.

    Args:
        payload: Plaid webhook payload containing ``item_id`` and
                 ``new_transactions`` count.

    Returns:
        202 Accepted — caller should fetch and write the new transactions.
    """
    logger.info(
        "Transactions sync update available",
        extra={
            "item_id": payload.get("item_id"),
            "new_transactions": payload.get("new_transactions"),
        },
    )
    return jsonify({"status": "accepted", "action": "sync_transactions"}), 202


@register("TRANSACTIONS", "DEFAULT_UPDATE")
def _handle_transactions_default_update(payload: dict[str, Any]) -> ResponseReturnValue:
    """Default (incremental) transaction update for an item.

    Args:
        payload: Plaid webhook payload.

    Returns:
        202 Accepted.
    """
    logger.info(
        "Transactions default update received",
        extra={
            "item_id": payload.get("item_id"),
            "new_transactions": payload.get("new_transactions"),
            "removed_transactions": payload.get("removed_transactions"),
        },
    )
    return jsonify({"status": "accepted", "action": "sync_transactions"}), 202


@register("TRANSACTIONS", "INITIAL_UPDATE")
def _handle_transactions_initial_update(payload: dict[str, Any]) -> ResponseReturnValue:
    """First batch of transactions after a new item is linked.

    Args:
        payload: Plaid webhook payload.

    Returns:
        202 Accepted.
    """
    logger.info(
        "Transactions initial update received",
        extra={"item_id": payload.get("item_id"), "new_transactions": payload.get("new_transactions")},
    )
    return jsonify({"status": "accepted", "action": "sync_transactions"}), 202


@register("TRANSACTIONS", "HISTORICAL_UPDATE")
def _handle_transactions_historical_update(payload: dict[str, Any]) -> ResponseReturnValue:
    """Full historical transaction pull is complete for a new item.

    Args:
        payload: Plaid webhook payload.

    Returns:
        202 Accepted.
    """
    logger.info(
        "Transactions historical update complete",
        extra={"item_id": payload.get("item_id"), "new_transactions": payload.get("new_transactions")},
    )
    return jsonify({"status": "accepted", "action": "sync_transactions"}), 202


@register("TRANSACTIONS", "TRANSACTIONS_REMOVED")
def _handle_transactions_removed(payload: dict[str, Any]) -> ResponseReturnValue:
    """Previously returned transactions have been removed by Plaid.

    Args:
        payload: Plaid webhook payload containing a list of removed transaction IDs.

    Returns:
        202 Accepted.
    """
    removed_ids: list[str] = payload.get("removed_transactions") or []
    logger.info(
        "Transactions removed",
        extra={"item_id": payload.get("item_id"), "removed_count": len(removed_ids)},
    )
    return jsonify({"status": "accepted", "action": "remove_transactions", "count": len(removed_ids)}), 202


# ---------------------------------------------------------------------------
# LIABILITIES handlers
# ---------------------------------------------------------------------------


@register("LIABILITIES", "DEFAULT_UPDATE")
def _handle_liabilities_default_update(payload: dict[str, Any]) -> ResponseReturnValue:
    """Debt balance data has been updated for one or more accounts.

    Args:
        payload: Plaid webhook payload.

    Returns:
        202 Accepted — caller should fetch updated liabilities and write to Sheets.
    """
    logger.info(
        "Liabilities default update received",
        extra={"item_id": payload.get("item_id"), "account_ids": payload.get("account_ids")},
    )
    return jsonify({"status": "accepted", "action": "sync_liabilities"}), 202


# ---------------------------------------------------------------------------
# ITEM handlers
# ---------------------------------------------------------------------------


@register("ITEM", "ERROR")
def _handle_item_error(payload: dict[str, Any]) -> ResponseReturnValue:
    """A Plaid Item has entered an error state and requires user action.

    Args:
        payload: Plaid webhook payload including an ``error`` object.

    Returns:
        200 OK — the error is logged; no retry is needed.
    """
    error = payload.get("error") or {}
    logger.error(
        "Plaid item error",
        extra={
            "item_id": payload.get("item_id"),
            "error_code": error.get("error_code"),
            "error_message": error.get("error_message"),
        },
    )
    return jsonify({"status": "acknowledged", "action": "alert_user"}), 200


@register("ITEM", "PENDING_EXPIRATION")
def _handle_item_pending_expiration(payload: dict[str, Any]) -> ResponseReturnValue:
    """The item's access consent is about to expire and needs re-linking.

    Args:
        payload: Plaid webhook payload including ``consent_expiration_time``.

    Returns:
        200 OK.
    """
    logger.warning(
        "Plaid item access expiring soon",
        extra={
            "item_id": payload.get("item_id"),
            "consent_expiration_time": payload.get("consent_expiration_time"),
        },
    )
    return jsonify({"status": "acknowledged", "action": "prompt_relink"}), 200


@register("ITEM", "USER_PERMISSION_REVOKED")
def _handle_item_permission_revoked(payload: dict[str, Any]) -> ResponseReturnValue:
    """The user revoked Plaid's access to their financial institution.

    Args:
        payload: Plaid webhook payload.

    Returns:
        200 OK.
    """
    logger.warning(
        "Plaid item permission revoked by user",
        extra={"item_id": payload.get("item_id")},
    )
    return jsonify({"status": "acknowledged", "action": "remove_item"}), 200


@register("ITEM", "LOGIN_REPAIRED")
def _handle_item_login_repaired(payload: dict[str, Any]) -> ResponseReturnValue:
    """A previously broken item login has been repaired.

    Args:
        payload: Plaid webhook payload.

    Returns:
        200 OK.
    """
    logger.info("Plaid item login repaired", extra={"item_id": payload.get("item_id")})
    return jsonify({"status": "acknowledged"}), 200


@register("ITEM", "WEBHOOK_UPDATE_ACKNOWLEDGED")
def _handle_item_webhook_acknowledged(payload: dict[str, Any]) -> ResponseReturnValue:
    """Plaid confirming it received a webhook URL update request.

    Args:
        payload: Plaid webhook payload.

    Returns:
        200 OK.
    """
    logger.info(
        "Plaid acknowledged webhook URL update",
        extra={"item_id": payload.get("item_id"), "new_webhook_url": payload.get("new_webhook_url")},
    )
    return jsonify({"status": "acknowledged"}), 200
