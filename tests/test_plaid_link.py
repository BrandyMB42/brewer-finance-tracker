"""Tests for Plaid Link token creation and public-token exchange.

The Plaid API client and Secret Manager calls are mocked so no network or
credentials are required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from brewer_finance_tracker import plaid_link


def test_create_link_token_returns_token() -> None:
    """create_link_token returns the link_token from the Plaid response."""
    fake_client = MagicMock()
    fake_client.link_token_create.return_value = {"link_token": "link-sandbox-123"}

    with patch.object(plaid_link, "_build_client", return_value=fake_client):
        token = plaid_link.create_link_token("user-42")

    assert token == "link-sandbox-123"
    fake_client.link_token_create.assert_called_once()


def test_exchange_public_token_stores_and_returns_access_token() -> None:
    """exchange_public_token returns the access token and persists it."""
    fake_client = MagicMock()
    fake_client.item_public_token_exchange.return_value = {
        "access_token": "access-sandbox-xyz",
        "item_id": "item-1",
    }

    with (
        patch.object(plaid_link, "_build_client", return_value=fake_client),
        patch.object(plaid_link, "_store_access_token") as mock_store,
    ):
        token = plaid_link.exchange_public_token("public-token-abc", "chase-checking")

    assert token == "access-sandbox-xyz"
    mock_store.assert_called_once_with("access-sandbox-xyz", "chase-checking")


def test_build_client_requires_project_id() -> None:
    """_build_client raises ValueError when GCP_PROJECT_ID is unset."""
    with patch.object(plaid_link.Config, "GCP_PROJECT_ID", ""):
        with pytest.raises(ValueError, match="GCP_PROJECT_ID"):
            plaid_link._build_client()


def test_build_client_fetches_credentials_from_secret_manager() -> None:
    """_build_client pulls both Plaid secrets via get_secret."""
    with (
        patch.object(plaid_link.Config, "GCP_PROJECT_ID", "proj-1"),
        patch.object(plaid_link.Config, "ENVIRONMENT", "staging"),
        patch.object(
            plaid_link, "get_secret", side_effect=["client-id-val", "secret-val"]
        ) as mock_get_secret,
        patch.object(plaid_link, "ApiClient"),
        patch.object(plaid_link.plaid_api, "PlaidApi") as mock_api,
    ):
        plaid_link._build_client()

    assert mock_get_secret.call_count == 2
    mock_get_secret.assert_any_call("proj-1", "plaid-client-id")
    mock_get_secret.assert_any_call("proj-1", "plaid-secret")
    mock_api.assert_called_once()
