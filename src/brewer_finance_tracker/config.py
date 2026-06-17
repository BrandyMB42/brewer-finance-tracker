"""Application configuration sourced from environment variables at startup.

No secrets are stored here.  Values that require confidentiality are fetched
from GCP Secret Manager at runtime via :mod:`brewer_finance_tracker.secrets.manager`.
"""

from __future__ import annotations

import os


class Config:
    """Runtime configuration for the Flask application.

    All attributes read from environment variables so that the same image can
    be deployed to staging and production without code changes.
    """

    #: GCP project that owns the Secret Manager secrets and other resources.
    GCP_PROJECT_ID: str = os.environ.get("GCP_PROJECT_ID", "")

    #: Log verbosity — one of DEBUG, INFO, WARNING, ERROR, CRITICAL.
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

    #: Deployment environment label used in structured log fields.
    ENVIRONMENT: str = os.environ.get("ENVIRONMENT", "development")
