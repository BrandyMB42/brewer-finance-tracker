"""Cloud Run entry-point for the Brewer Finance Tracker service.

Gunicorn invocation (set in the Harness deploy stage)::

    gunicorn --bind :$PORT --workers 2 main:app

Secrets referenced at startup (fetched from Secret Manager, not env vars):
  - ``webhook-signing-secret``  — used to verify HMAC signatures on inbound events.
"""

from __future__ import annotations

from src.brewer_finance_tracker.app import create_app

app = create_app()
