"""Request validation helpers for the webhook endpoint.

Validation is intentionally kept separate from routing so that each concern
can be tested in isolation and swapped without touching the Flask view.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Request

logger = logging.getLogger(__name__)


def get_validated_json(
    request: Request,
) -> tuple[dict[str, Any] | None, str | None]:
    """Parse and validate that *request* carries a well-formed JSON body.

    Checks ``Content-Type`` first, then attempts to decode the body.  Both
    failure modes map to HTTP 400 at the call site.

    Args:
        request: The active Flask request object.

    Returns:
        A two-tuple ``(body, error)``.  On success, ``body`` is the decoded
        dict and ``error`` is ``None``.  On failure, ``body`` is ``None`` and
        ``error`` is a human-readable description of the problem.
    """
    content_type: str = request.content_type or ""
    if "application/json" not in content_type:
        logger.warning(
            "Webhook request missing JSON content-type",
            extra={"content_type": content_type},
        )
        return None, "Content-Type must be application/json"

    body: dict[str, Any] | None = request.get_json(silent=True, force=False)
    if body is None:
        logger.warning("Webhook request body is not valid JSON")
        return None, "Request body must be valid JSON"

    return body, None
