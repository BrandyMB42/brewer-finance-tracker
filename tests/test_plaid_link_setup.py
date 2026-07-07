"""Tests for the standalone Plaid Link setup tool (tools/plaid-link-setup).

The tool is local-only and lives outside the ``src`` package, so it is loaded
here by file path. Its ``python-dotenv`` dependency (declared in the tool's own
requirements.txt) is not installed in the app's test/CI environment, so we stub
it before import rather than adding an extra dependency just to import the file.

Plaid API calls and the gcloud subprocess wrapper are mocked, so these tests
touch neither the network nor Secret Manager.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import plaid

# Stub python-dotenv (only a dependency of the standalone tool) so server.py
# imports cleanly under the app's test environment.
if "dotenv" not in sys.modules:
    _dotenv_stub = types.ModuleType("dotenv")
    _dotenv_stub.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = _dotenv_stub

_SERVER_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "plaid-link-setup" / "server.py"
)
_spec = importlib.util.spec_from_file_location("plaid_link_setup_server", _SERVER_PATH)
assert _spec is not None and _spec.loader is not None
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


def test_slugify_normalizes_punctuation_and_case() -> None:
    """Slugify lowercases and collapses non-alphanumerics into hyphens."""
    assert server._slugify("Chase Bank, N.A.") == "chase-bank-n-a"


def test_slugify_empty_returns_unknown() -> None:
    """A name with no usable characters slugifies to 'unknown'."""
    assert server._slugify("   ") == "unknown"


def test_account_disambiguator_prefers_first_account_mask() -> None:
    """The first account with a mask (last-4) is used as the disambiguator."""
    fake_client = MagicMock()
    fake_client.accounts_get.return_value = {
        "accounts": [{"mask": None}, {"mask": "4321"}]
    }

    result = server._account_disambiguator(fake_client, "access-1", "item-xyz")

    assert result == "4321"


def test_account_disambiguator_falls_back_to_item_id_without_mask() -> None:
    """With no mask on any account, a suffix of the item_id is used."""
    fake_client = MagicMock()
    fake_client.accounts_get.return_value = {"accounts": [{"mask": None}]}

    result = server._account_disambiguator(fake_client, "access-1", "item-1234567890")

    assert result == "34567890"  # last 8 chars of the item_id


def test_account_disambiguator_falls_back_on_api_error() -> None:
    """If accounts_get raises, the item_id suffix is used instead."""
    fake_client = MagicMock()
    fake_client.accounts_get.side_effect = plaid.ApiException()

    result = server._account_disambiguator(fake_client, "access-1", "item-1234567890")

    assert result == "34567890"


def test_list_connected_institutions_parses_and_sorts_slugs() -> None:
    """Both bare ids and full resource paths are reduced to sorted slugs."""
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=(
            "plaid-access-token-chase-7254\n"
            "projects/p/secrets/plaid-access-token-amex-1001\n"
            "unrelated-secret\n"
        ),
    )

    with patch.object(server, "_gcloud", return_value=completed):
        result = server._list_connected_institutions()

    assert result == ["amex-1001", "chase-7254"]


def test_list_connected_institutions_empty_on_gcloud_failure() -> None:
    """A non-zero gcloud exit yields an empty list rather than raising."""
    completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="")

    with patch.object(server, "_gcloud", return_value=completed):
        assert server._list_connected_institutions() == []


def test_exchange_token_builds_disambiguated_secret_id() -> None:
    """POST /exchange-token stores under plaid-access-token-{slug}-{mask}."""
    fake_client = MagicMock()
    fake_client.item_public_token_exchange.return_value = {
        "access_token": "access-xyz",
        "item_id": "item-1",
    }
    fake_client.accounts_get.return_value = {"accounts": [{"mask": "7254"}]}

    with (
        patch.object(server, "_build_client", return_value=fake_client),
        patch.object(server, "_store_access_token") as mock_store,
    ):
        response = server.app.test_client().post(
            "/exchange-token",
            json={
                "public_token": "public-abc",
                "institution": {"name": "Chase Bank"},
            },
        )

    assert response.status_code == 200
    assert response.get_json()["secret_id"] == "plaid-access-token-chase-bank-7254"
    mock_store.assert_called_once_with(
        "plaid-access-token-chase-bank-7254", "access-xyz"
    )


def test_exchange_token_requires_public_token() -> None:
    """A request without a public_token is rejected with 400."""
    response = server.app.test_client().post("/exchange-token", json={})

    assert response.status_code == 400
    assert response.get_json()["error"] == "missing_public_token"


def test_exchange_token_looks_up_institution_when_absent() -> None:
    """When the browser omits the institution, it is resolved from the API."""
    fake_client = MagicMock()
    fake_client.item_public_token_exchange.return_value = {
        "access_token": "access-1",
        "item_id": "item-1",
    }
    fake_client.accounts_get.return_value = {"accounts": [{"mask": "1001"}]}

    with (
        patch.object(server, "_build_client", return_value=fake_client),
        patch.object(
            server, "_lookup_institution_name", return_value="American Express"
        ) as mock_lookup,
        patch.object(server, "_store_access_token") as mock_store,
    ):
        response = server.app.test_client().post(
            "/exchange-token", json={"public_token": "public-abc"}
        )

    mock_lookup.assert_called_once()
    assert response.get_json()["secret_id"] == (
        "plaid-access-token-american-express-1001"
    )
    mock_store.assert_called_once_with(
        "plaid-access-token-american-express-1001", "access-1"
    )
