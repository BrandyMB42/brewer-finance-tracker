"""Flask Blueprint providing the /webhook HTTP endpoint.

Response contract
-----------------
200 / 202   Event accepted or intentionally ignored.
400         Request body is absent or not valid JSON.
405         HTTP method is not POST.
500         An unexpected server-side error occurred.

All error bodies are JSON so callers can parse them uniformly.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request
from flask.typing import ResponseReturnValue

from .dispatcher import dispatch
from .validators import get_validated_json

logger = logging.getLogger(__name__)

webhook_bp = Blueprint("webhook", __name__)

_ALLOWED_METHODS: frozenset[str] = frozenset({"POST"})

# Register all common verbs so Flask does not short-circuit with its own 405
# before our handler can emit a consistently formatted JSON error body.
_ALL_METHODS: list[str] = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]


@webhook_bp.route("/webhook", methods=_ALL_METHODS)
def handle_webhook() -> ResponseReturnValue:
    """Receive, validate, and dispatch inbound webhook events.

    Returns:
        A Flask response with the appropriate status code and a JSON body
        describing the outcome.  See the module docstring for the full
        response contract.
    """
    if request.method not in _ALLOWED_METHODS:
        logger.warning(
            "Webhook received with disallowed HTTP method",
            extra={"method": request.method, "path": request.path},
        )
        return jsonify({"error": "Method not allowed"}), 405

    body, validation_error = get_validated_json(request)
    if validation_error is not None:
        return jsonify({"error": validation_error}), 400

    try:
        return dispatch(body)  # type: ignore[arg-type]
    except Exception:
        logger.exception(
            "Unhandled exception while processing webhook",
            extra={"event_type": (body or {}).get("type")},
        )
        return jsonify({"error": "Internal server error"}), 500
