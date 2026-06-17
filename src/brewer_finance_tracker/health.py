"""Liveness/readiness endpoint used by Cloud Run and pipeline smoke tests.

The check is intentionally dependency-free — it confirms the process is up and
serving HTTP without reaching out to Secret Manager, Plaid, or Sheets.  This
keeps the probe fast and avoids cascading failures when a downstream is slow.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify
from flask.typing import ResponseReturnValue

from .config import Config

logger = logging.getLogger(__name__)

health_bp = Blueprint("health", __name__)


@health_bp.route("/health", methods=["GET"])
def health() -> ResponseReturnValue:
    """Report service liveness.

    Returns:
        A 200 response with a small JSON body identifying the running
        environment.  Smoke-test stages assert on the status code.
    """
    return jsonify({"status": "ok", "environment": Config.ENVIRONMENT}), 200
