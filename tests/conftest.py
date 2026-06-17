"""Shared pytest fixtures for the test suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient

from brewer_finance_tracker.app import create_app
from brewer_finance_tracker.config import Config


@pytest.fixture
def app() -> Flask:
    """Create a Flask application instance configured for testing.

    Returns:
        A Flask app with ``TESTING`` enabled.
    """
    application = create_app(Config)
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app: Flask) -> Iterator[FlaskClient]:
    """Provide a Flask test client for issuing requests.

    Args:
        app: The application fixture.

    Yields:
        A :class:`flask.testing.FlaskClient` bound to the test app.
    """
    with app.test_client() as test_client:
        yield test_client
