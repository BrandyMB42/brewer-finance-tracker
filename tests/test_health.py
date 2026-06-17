"""Tests for the /health endpoint used by Cloud Run and pipeline smoke tests."""

from __future__ import annotations

from flask.testing import FlaskClient


def test_health_returns_200(client: FlaskClient) -> None:
    """The health endpoint returns 200 with a status body."""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ok"
    assert "environment" in body
