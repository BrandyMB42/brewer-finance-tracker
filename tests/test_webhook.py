"""Tests for the /webhook endpoint and the Plaid event dispatcher.

Coverage:
- HTTP contract: 405 (wrong method), 400 (bad body), 200 (ignored), 500 (error)
- Plaid routing: TRANSACTIONS / LIABILITIES / ITEM type+code combinations
- Dispatcher unit behaviour independent of the Flask layer
"""

from __future__ import annotations

from typing import Any

import pytest
from flask import jsonify
from flask.testing import FlaskClient

from brewer_finance_tracker.webhook import dispatcher
from brewer_finance_tracker.webhook.dispatcher import dispatch, register


# ---------------------------------------------------------------------------
# HTTP contract
# ---------------------------------------------------------------------------


def test_get_method_returns_405(client: FlaskClient) -> None:
    """A non-POST method must be rejected with 405."""
    response = client.get("/webhook")
    assert response.status_code == 405
    assert response.get_json() == {"error": "Method not allowed"}


def test_put_method_returns_405(client: FlaskClient) -> None:
    """PUT, like all non-POST verbs, must return 405."""
    response = client.put("/webhook", json={"webhook_type": "ITEM"})
    assert response.status_code == 405


def test_missing_body_returns_400(client: FlaskClient) -> None:
    """A POST without a JSON body must return 400."""
    response = client.post("/webhook", data="", content_type="application/json")
    assert response.status_code == 400
    assert "error" in response.get_json()


def test_non_json_content_type_returns_400(client: FlaskClient) -> None:
    """A POST with a non-JSON content type must return 400."""
    response = client.post("/webhook", data="hello", content_type="text/plain")
    assert response.status_code == 400


def test_malformed_json_returns_400(client: FlaskClient) -> None:
    """A POST with a JSON content type but invalid JSON must return 400."""
    response = client.post(
        "/webhook", data="{not valid json", content_type="application/json"
    )
    assert response.status_code == 400


def test_unhandled_webhook_type_returns_200(client: FlaskClient) -> None:
    """An unregistered webhook type+code must be acknowledged with 200."""
    response = client.post(
        "/webhook",
        json={"webhook_type": "INCOME", "webhook_code": "SOMETHING_NEW"},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ignored"


def test_missing_type_fields_returns_200_ignored(client: FlaskClient) -> None:
    """A payload lacking webhook_type/code is ignored, not errored."""
    response = client.post("/webhook", json={"unrelated": "data"})
    assert response.status_code == 200
    assert response.get_json()["status"] == "ignored"


def test_handler_exception_returns_500(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception raised inside dispatch must surface as a 500."""

    def _boom(_payload: dict[str, Any]) -> None:
        raise RuntimeError("simulated handler failure")

    # Patch the dispatch symbol imported into the handler module.
    monkeypatch.setattr(
        "brewer_finance_tracker.webhook.handler.dispatch", _boom
    )
    response = client.post(
        "/webhook",
        json={"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE"},
    )
    assert response.status_code == 500
    assert response.get_json() == {"error": "Internal server error"}


# ---------------------------------------------------------------------------
# Plaid routing via the HTTP layer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("webhook_type", "webhook_code"),
    [
        ("TRANSACTIONS", "SYNC_UPDATES_AVAILABLE"),
        ("TRANSACTIONS", "DEFAULT_UPDATE"),
        ("TRANSACTIONS", "INITIAL_UPDATE"),
        ("TRANSACTIONS", "HISTORICAL_UPDATE"),
        ("TRANSACTIONS", "TRANSACTIONS_REMOVED"),
        ("LIABILITIES", "DEFAULT_UPDATE"),
    ],
)
def test_data_webhooks_return_202(
    client: FlaskClient, webhook_type: str, webhook_code: str
) -> None:
    """Data-bearing Plaid webhooks are accepted for processing with 202."""
    response = client.post(
        "/webhook",
        json={"webhook_type": webhook_type, "webhook_code": webhook_code, "item_id": "itm_1"},
    )
    assert response.status_code == 202
    assert response.get_json()["status"] == "accepted"


@pytest.mark.parametrize(
    "webhook_code",
    [
        "ERROR",
        "PENDING_EXPIRATION",
        "USER_PERMISSION_REVOKED",
        "LOGIN_REPAIRED",
        "WEBHOOK_UPDATE_ACKNOWLEDGED",
    ],
)
def test_item_webhooks_return_200(client: FlaskClient, webhook_code: str) -> None:
    """ITEM-level webhooks are acknowledged with 200 (no data to fetch)."""
    payload: dict[str, Any] = {
        "webhook_type": "ITEM",
        "webhook_code": webhook_code,
        "item_id": "itm_1",
    }
    if webhook_code == "ERROR":
        payload["error"] = {"error_code": "ITEM_LOGIN_REQUIRED", "error_message": "bad"}
    response = client.post("/webhook", json=payload)
    assert response.status_code == 200


def test_webhook_type_is_case_insensitive(client: FlaskClient) -> None:
    """Lower-case webhook_type/code values still route correctly."""
    response = client.post(
        "/webhook",
        json={"webhook_type": "transactions", "webhook_code": "sync_updates_available"},
    )
    assert response.status_code == 202


# ---------------------------------------------------------------------------
# Dispatcher unit tests (no Flask client)
# ---------------------------------------------------------------------------


def test_register_and_dispatch_custom_handler(app: Any) -> None:
    """A handler registered via the decorator is invoked by dispatch()."""

    @register("CUSTOM_TYPE", "CUSTOM_CODE")
    def _custom(_payload: dict[str, Any]) -> Any:
        return jsonify({"status": "custom"}), 218

    try:
        with app.app_context():
            _body, status = dispatch(
                {"webhook_type": "CUSTOM_TYPE", "webhook_code": "CUSTOM_CODE"}
            )
        assert status == 218
    finally:
        dispatcher._HANDLERS.pop("CUSTOM_TYPE:CUSTOM_CODE", None)


def test_dispatch_unknown_returns_ignored(app: Any) -> None:
    """dispatch() returns a 200 'ignored' tuple for unknown combinations."""
    with app.app_context():
        _body, status = dispatch(
            {"webhook_type": "NOPE", "webhook_code": "NOPE"}
        )
    assert status == 200
