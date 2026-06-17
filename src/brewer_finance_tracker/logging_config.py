"""Structured logging configuration for the finance tracker service.

Emits log records as structured ``key=value`` lines on stdout so that GCP
Cloud Logging's structured-log ingestion can parse them automatically.
"""

from __future__ import annotations

import logging
import sys
from typing import Any


_SKIP_KEYS: frozenset[str] = frozenset(logging.LogRecord.__dict__)


class StructuredFormatter(logging.Formatter):
    """Format log records as flat key=value pairs suitable for Cloud Logging."""

    def format(self, record: logging.LogRecord) -> str:
        """Render *record* as a structured log line."""
        fields: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            fields["exception"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key not in _SKIP_KEYS and not key.startswith("_"):
                fields[key] = value

        return " ".join(f"{k}={v!r}" for k, v in fields.items())


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger with structured output on stdout.

    Args:
        level: Logging level name (e.g. ``"DEBUG"``, ``"INFO"``, ``"WARNING"``).
               Defaults to ``"INFO"``.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
