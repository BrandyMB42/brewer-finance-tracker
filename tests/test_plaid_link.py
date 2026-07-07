"""Tests for Plaid Link token creation and public-token exchange.

The Plaid API client and Secret Manager calls are mocked so no network or
credentials are required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import plaid
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


def test_exchange_uses_institution_name_and_mask_for_secret_id() -> None:
    """Secret id is plaid-access-token-{slug}-{mask} when a name is supplied."""
    fake_client = MagicMock()
    fake_client.item_public_token_exchange.return_value = {
        "access_token": "access-sandbox-xyz",
        "item_id": "item-abcdefgh",
    }
    fake_client.accounts_get.return_value = {"accounts": [{"mask": "7254"}]}

    with (
        patch.object(plaid_link, "_build_client", return_value=fake_client),
        patch.object(plaid_link, "_store_access_token") as mock_store,
    ):
        token = plaid_link.exchange_public_token(
            "public-token-abc", "Chase Bank, N.A."
        )

    assert token == "access-sandbox-xyz"
    mock_store.assert_called_once_with(
        "access-sandbox-xyz", "plaid-access-token-chase-bank-n-a-7254"
    )


def test_exchange_looks_up_institution_name_when_omitted() -> None:
    """When no name is passed, it is resolved from the item via the API."""
    fake_client = MagicMock()
    fake_client.item_public_token_exchange.return_value = {
        "access_token": "access-1",
        "item_id": "item-abcdefgh",
    }
    fake_client.accounts_get.return_value = {"accounts": [{"mask": "1001"}]}

    with (
        patch.object(plaid_link, "_build_client", return_value=fake_client),
        patch.object(
            plaid_link, "_lookup_institution_name", return_value="Amex"
        ) as mock_lookup,
        patch.object(plaid_link, "_store_access_token") as mock_store,
    ):
        plaid_link.exchange_public_token("public-token-abc")

    mock_lookup.assert_called_once()
    mock_store.assert_called_once_with("access-1", "plaid-access-token-amex-1001")


def test_account_disambiguator_prefers_first_account_mask() -> None:
    """The first account with a mask (last-4) is used as the disambiguator."""
    fake_client = MagicMock()
    fake_client.accounts_get.return_value = {
        "accounts": [{"mask": None}, {"mask": "4321"}]
    }

    result = plaid_link._account_disambiguator(fake_client, "access-1", "item-xyz")

    assert result == "4321"


def test_account_disambiguator_falls_back_to_item_id_without_mask() -> None:
    """With no mask on any account, a suffix of the item_id is used."""
    fake_client = MagicMock()
    fake_client.accounts_get.return_value = {"accounts": [{"mask": None}]}

    result = plaid_link._account_disambiguator(
        fake_client, "access-1", "item-1234567890"
    )

    assert result == "34567890"  # last 8 chars of the item_id


def test_account_disambiguator_falls_back_on_api_error() -> None:
    """If accounts_get raises, the item_id suffix is used instead."""
    fake_client = MagicMock()
    fake_client.accounts_get.side_effect = plaid.ApiException()

    result = plaid_link._account_disambiguator(
        fake_client, "access-1", "item-1234567890"
    )

    assert result == "34567890"


def test_slugify_normalizes_punctuation_and_case() -> None:
    """Slugify lowercases and collapses non-alphanumerics into hyphens."""
    assert plaid_link._slugify("Chase Bank, N.A.") == "chase-bank-n-a"


def test_slugify_empty_returns_unknown() -> None:
    """A name with no usable characters slugifies to 'unknown'."""
    assert plaid_link._slugify("!!!") == "unknown"


def test_lookup_institution_name_resolves_via_api() -> None:
    """The institution display name is resolved from the item's institution_id."""
    fake_client = MagicMock()
    fake_client.item_get.return_value = {"item": {"institution_id": "ins_1"}}
    fake_client.institutions_get_by_id.return_value = {
        "institution": {"name": "Chase"}
    }

    name = plaid_link._lookup_institution_name(fake_client, "access-1")

    assert name == "Chase"


def test_lookup_institution_name_unknown_when_no_institution_id() -> None:
    """When Plaid reports no institution_id, the name defaults to 'unknown'."""
    fake_client = MagicMock()
    fake_client.item_get.return_value = {"item": {"institution_id": None}}

    name = plaid_link._lookup_institution_name(fake_client, "access-1")

    assert name == "unknown"
    fake_client.institutions_get_by_id.assert_not_called()


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
