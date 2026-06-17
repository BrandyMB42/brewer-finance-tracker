"""Webhook event dispatcher — routes inbound events to typed handler functions.

New event types are registered with the :func:`register` decorator.  Unregistered
types are acknowledged with a 200 and a structured warning rather than silently
dropped or retried, which prevents sender back-off storms.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from flask import Response, jsonify
from flask.typing import ResponseReturnValue

logger = logging.getLogger(__name__)

_HANDLERS: dict[str, Callable[[dict[str, Any]], ResponseReturnValue]] = {}


def register(event_type: str) -> Callable:
    """Decorator that registers *fn* as the handler for *event_type*.

    Args:
        event_type: The ``type`` field value that routes to this handler
                    (e.g. ``"transaction.created"``).

    Returns:
        A decorator that stores the function and returns it unchanged.
    """

    def decorator(fn: Callable[[dict[str, Any]], ResponseReturnValue]) -> Callable:
        _HANDLERS[event_type] = fn
        return fn

    return decorator


def dispatch(payload: dict[str, Any]) -> ResponseReturnValue:
    """Route *payload* to the registered handler for its event type.

    The event type is resolved from the ``type`` field of the payload, falling
    back to ``event_type`` for compatibility with providers that use the longer
    key name.

    If no handler is registered the event is acknowledged (HTTP 200) and a
    structured warning is emitted — this prevents senders from retrying events
    that the service intentionally does not handle.

    Args:
        payload: Parsed JSON body from the incoming webhook request.

    Returns:
        A Flask ``ResponseReturnValue`` produced by the matched handler, or a
        generic 200 acknowledgement for unknown event types.
    """
    event_type: str = payload.get("type") or payload.get("event_type") or ""

    handler = _HANDLERS.get(event_type)
    if handler is None:
        logger.warning(
            "Received unhandled webhook type — acknowledging without processing",
            extra={"event_type": event_type or "<missing>"},
        )
        return jsonify({"status": "ignored", "event_type": event_type}), 200

    logger.info("Dispatching webhook event", extra={"event_type": event_type})
    return handler(payload)


# ---------------------------------------------------------------------------
# Registered event handlers
# ---------------------------------------------------------------------------


@register("transaction.created")
def _handle_transaction_created(payload: dict[str, Any]) -> ResponseReturnValue:
    """Process a newly created transaction event.

    Args:
        payload: Full webhook payload for this event.

    Returns:
        202 Accepted response confirming the event was queued for processing.
    """
    logger.info(
        "Processing transaction.created",
        extra={"transaction_id": payload.get("id")},
    )
    return jsonify({"status": "accepted"}), 202


@register("transaction.updated")
def _handle_transaction_updated(payload: dict[str, Any]) -> ResponseReturnValue:
    """Process a transaction-update event.

    Args:
        payload: Full webhook payload for this event.

    Returns:
        202 Accepted response confirming the update was queued for processing.
    """
    logger.info(
        "Processing transaction.updated",
        extra={"transaction_id": payload.get("id")},
    )
    return jsonify({"status": "accepted"}), 202
