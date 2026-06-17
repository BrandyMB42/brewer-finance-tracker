"""Flask application factory for the Brewer Finance Tracker service.

Usage
-----
Run locally::

    FLASK_APP=src/brewer_finance_tracker/app.py flask run

The Cloud Run entry-point is ``main.py`` at the repo root, which calls
:func:`create_app` and passes the resulting object to ``gunicorn``.
"""

from __future__ import annotations

import logging

from flask import Flask

from .config import Config
from .logging_config import configure_logging
from .webhook.handler import webhook_bp

logger = logging.getLogger(__name__)


def create_app(config: type[Config] = Config) -> Flask:
    """Create and configure the Flask application.

    Registers all blueprints and applies configuration from *config*.  Logging
    is initialised first so that any import-time log statements are captured.

    Args:
        config: A configuration class whose class attributes are loaded via
                ``app.config.from_object``.  Defaults to :class:`Config`.

    Returns:
        A fully initialised :class:`flask.Flask` application instance.
    """
    configure_logging(config.LOG_LEVEL)

    app = Flask(__name__)
    app.config.from_object(config)

    app.register_blueprint(webhook_bp)

    logger.info(
        "Application initialised",
        extra={
            "environment": config.ENVIRONMENT,
            "gcp_project": config.GCP_PROJECT_ID,
        },
    )
    return app
