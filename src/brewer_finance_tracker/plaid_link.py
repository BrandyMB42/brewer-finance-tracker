"""Plaid Link token creation and public-token exchange.

All Plaid credentials are fetched from GCP Secret Manager at call time —
never from environment variables or config files.

Secrets required in Secret Manager
-----------------------------------
- ``plaid-client-id``          Plaid API client ID
- ``plaid-secret``             Plaid API secret (environment-specific)

The Plaid environment is derived from the ``ENVIRONMENT`` config value:
- ``development``  → Plaid Sandbox
- ``staging``      → Plaid Sandbox
- ``production``   → Plaid Production
"""

from __future__ import annotations

import logging

import plaid
from plaid.api import plaid_api
from plaid.api_client import ApiClient
from plaid.configuration import Configuration
from plaid.model.country_code import CountryCode
from plaid.model.item_public_token_exchange_request import (
    ItemPublicTokenExchangeRequest,
)
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products

from .config import Config
from .secrets.manager import get_secret

logger = logging.getLogger(__name__)

_PLAID_ENV_MAP: dict[str, str] = {
    "production": plaid.Environment.Production,
    "staging": plaid.Environment.Sandbox,
    "development": plaid.Environment.Sandbox,
}


def _build_client() -> plaid_api.PlaidApi:
    """Construct an authenticated Plaid API client.

    Credentials are pulled from Secret Manager on every call so that secret
    rotation takes effect without a service restart.  The underlying
    ``get_secret`` function caches results in-process, so the network round-trip
    only happens once per worker lifetime unless the cache is cleared.

    Returns:
        A configured :class:`plaid_api.PlaidApi` instance.

    Raises:
        ValueError: If ``GCP_PROJECT_ID`` is not set in the environment.
    """
    project_id = Config.GCP_PROJECT_ID
    if not project_id:
        raise ValueError("GCP_PROJECT_ID must be set to fetch Plaid credentials")

    client_id = get_secret(project_id, "plaid-client-id")
    secret = get_secret(project_id, "plaid-secret")
    host = _PLAID_ENV_MAP.get(Config.ENVIRONMENT, plaid.Environment.Sandbox)

    configuration = Configuration(
        host=host,
        api_key={"clientId": client_id, "secret": secret},
    )
    return plaid_api.PlaidApi(ApiClient(configuration))


def create_link_token(user_id: str) -> str:
    """Create a Plaid Link token for a given user.

    The token is short-lived (30 minutes) and must be passed to the Plaid Link
    SDK on the client side to initiate the account-linking flow.

    Args:
        user_id: An opaque, stable identifier for the end user (e.g. a database
                 primary key).  Plaid uses this to de-duplicate Link sessions.

    Returns:
        A ``link_token`` string that can be passed to ``Plaid.create()``.

    Raises:
        plaid.ApiException: If the Plaid API returns an error response.
    """
    client = _build_client()
    request = LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id=user_id),
        client_name="Brewer Finance Tracker",
        products=[Products("transactions"), Products("liabilities")],
        country_codes=[CountryCode("US")],
        language="en",
    )
    response = client.link_token_create(request)
    logger.info("Created Plaid Link token", extra={"user_id": user_id})
    return str(response["link_token"])


def exchange_public_token(public_token: str, item_label: str) -> str:
    """Exchange a Plaid Link public token for a permanent access token.

    The resulting access token is stored in Secret Manager under the key
    ``plaid-access-token-{item_label}`` so that it survives service restarts
    without ever appearing in logs, environment variables, or source code.

    Args:
        public_token: The temporary token returned by Plaid Link on the client
                      after the user successfully connects their account.
        item_label:   A short, URL-safe label identifying this Plaid Item
                      (e.g. ``"chase-checking"``).  Used as the Secret Manager
                      key suffix.

    Returns:
        The permanent Plaid access token string.

    Raises:
        plaid.ApiException: If Plaid rejects the token exchange.
        ValueError: If ``GCP_PROJECT_ID`` is not set.
    """
    client = _build_client()
    request = ItemPublicTokenExchangeRequest(public_token=public_token)
    response = client.item_public_token_exchange(request)
    access_token: str = response["access_token"]
    item_id: str = response["item_id"]

    _store_access_token(access_token, item_label)

    logger.info(
        "Exchanged Plaid public token and stored access token",
        extra={"item_id": item_id, "item_label": item_label},
    )
    return access_token


def _store_access_token(access_token: str, item_label: str) -> None:
    """Persist *access_token* to Secret Manager.

    Creates the secret if it does not exist, then adds a new version.

    Args:
        access_token: Plaid access token to store.
        item_label:   Label used to construct the secret resource name.
    """
    from google.cloud import secretmanager  # imported here to avoid top-level cost

    project_id = Config.GCP_PROJECT_ID
    secret_id = f"plaid-access-token-{item_label}"
    parent = f"projects/{project_id}"

    sm_client = secretmanager.SecretManagerServiceClient()

    try:
        sm_client.get_secret(request={"name": f"{parent}/secrets/{secret_id}"})
    except Exception:
        sm_client.create_secret(
            request={
                "parent": parent,
                "secret_id": secret_id,
                "secret": {"replication": {"automatic": {}}},
            }
        )

    sm_client.add_secret_version(
        request={
            "parent": f"{parent}/secrets/{secret_id}",
            "payload": {"data": access_token.encode("utf-8")},
        }
    )
    # Clear the lru_cache so the next get_secret call reads the new version.
    get_secret.cache_clear()
    logger.info("Stored Plaid access token in Secret Manager", extra={"secret_id": secret_id})
