"""Tests for the Secret Manager wrapper.

The GCP client is fully mocked — these tests never touch the network.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from brewer_finance_tracker.secrets import manager


@pytest.fixture(autouse=True)
def _clear_secret_cache() -> None:
    """Reset the lru_cache before and after each test to avoid cross-test leaks."""
    manager.get_secret.cache_clear()
    yield
    manager.get_secret.cache_clear()


def test_get_secret_returns_decoded_payload() -> None:
    """get_secret returns the UTF-8 decoded secret payload."""
    fake_client = MagicMock()
    fake_client.access_secret_version.return_value.payload.data = b"super-secret"

    with patch.object(
        manager.secretmanager, "SecretManagerServiceClient", return_value=fake_client
    ):
        value = manager.get_secret("proj-1", "my-secret")

    assert value == "super-secret"
    fake_client.access_secret_version.assert_called_once_with(
        request={"name": "projects/proj-1/secrets/my-secret/versions/latest"}
    )


def test_get_secret_uses_requested_version() -> None:
    """A non-default version is reflected in the resource name."""
    fake_client = MagicMock()
    fake_client.access_secret_version.return_value.payload.data = b"v3-value"

    with patch.object(
        manager.secretmanager, "SecretManagerServiceClient", return_value=fake_client
    ):
        manager.get_secret("proj-1", "my-secret", version="3")

    fake_client.access_secret_version.assert_called_once_with(
        request={"name": "projects/proj-1/secrets/my-secret/versions/3"}
    )


def test_get_secret_is_cached() -> None:
    """Repeated calls with the same args hit the cache, not the API."""
    fake_client = MagicMock()
    fake_client.access_secret_version.return_value.payload.data = b"cached"

    with patch.object(
        manager.secretmanager, "SecretManagerServiceClient", return_value=fake_client
    ):
        manager.get_secret("proj-1", "my-secret")
        manager.get_secret("proj-1", "my-secret")

    # Client constructed and queried only once despite two calls.
    fake_client.access_secret_version.assert_called_once()
