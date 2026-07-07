"""One-time cleanup: revoke a Plaid Item using a stored access token.

Given the id of a Secret Manager secret that holds a Plaid access token, this
script fetches the token and calls Plaid's ``/item/remove`` endpoint to formally
revoke the Item on Plaid's side (stopping further billing and webhooks for it).

It does **not** delete the Secret Manager secret — remove that separately with
``gcloud secrets delete <secret-id>`` once you've confirmed the Item is gone.

This is a standalone one-time utility, not part of the deployed app. Like the
rest of ``tools/plaid-link-setup``, it reads Plaid credentials from a local
``.env`` and reads the token from Secret Manager via the gcloud CLI.

Usage::

    pip install -r requirements.txt
    python remove_item.py plaid-access-token-chase-7254
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

import plaid
from dotenv import load_dotenv
from plaid.api import plaid_api
from plaid.api_client import ApiClient
from plaid.configuration import Configuration
from plaid.model.item_remove_request import ItemRemoveRequest

load_dotenv()

PLAID_CLIENT_ID = os.environ.get("PLAID_CLIENT_ID", "")
PLAID_SECRET = os.environ.get("PLAID_SECRET", "")
PLAID_ENV = os.environ.get("PLAID_ENV", "sandbox").lower()
# Optional. If unset, gcloud uses its active configured project.
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")

_PLAID_HOSTS = {
    "sandbox": plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}


def _require_credentials() -> None:
    """Exit early with a clear message if Plaid credentials are missing."""
    missing = [
        name
        for name, value in (
            ("PLAID_CLIENT_ID", PLAID_CLIENT_ID),
            ("PLAID_SECRET", PLAID_SECRET),
        )
        if not value
    ]
    if missing:
        sys.exit(
            "Missing required env var(s): "
            + ", ".join(missing)
            + ".\nCopy .env.example to .env and fill in your Plaid credentials."
        )


def _build_client() -> plaid_api.PlaidApi:
    """Construct an authenticated Plaid API client from local .env credentials."""
    host = _PLAID_HOSTS.get(PLAID_ENV)
    if host is None:
        sys.exit(f"PLAID_ENV must be 'sandbox' or 'production', got {PLAID_ENV!r}.")

    configuration = Configuration(
        host=host,
        api_key={"clientId": PLAID_CLIENT_ID, "secret": PLAID_SECRET},
    )
    return plaid_api.PlaidApi(ApiClient(configuration))


def _gcloud_path() -> str:
    """Resolve the gcloud executable, accounting for Windows' gcloud.cmd."""
    for candidate in ("gcloud", "gcloud.cmd"):
        path = shutil.which(candidate)
        if path:
            return path
    sys.exit(
        "gcloud CLI not found on PATH. Install the Google Cloud SDK and run "
        "`gcloud auth login` before using this tool."
    )


def _fetch_access_token(secret_id: str) -> str:
    """Read the latest version of *secret_id* from Secret Manager via gcloud."""
    cmd = [
        _gcloud_path(),
        "secrets", "versions", "access", "latest",
        f"--secret={secret_id}",
    ]
    if GCP_PROJECT_ID:
        cmd += ["--project", GCP_PROJECT_ID]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        sys.exit(f"Failed to read secret {secret_id!r}: {detail}")

    # gcloud does not add a trailing newline to the raw secret payload, but strip
    # defensively in case the token was stored with one.
    token = result.stdout.strip()
    if not token:
        sys.exit(f"Secret {secret_id!r} exists but its latest version is empty.")
    return token


def remove_item(secret_id: str) -> None:
    """Fetch the token behind *secret_id* and revoke its Item on Plaid."""
    _require_credentials()
    access_token = _fetch_access_token(secret_id)
    client = _build_client()

    try:
        client.item_remove(ItemRemoveRequest(access_token=access_token))
    except plaid.ApiException as exc:
        sys.exit(f"Plaid rejected item removal for {secret_id!r}:\n{exc.body}")

    print(f"Removed Plaid Item for secret {secret_id!r} (env: {PLAID_ENV}).")
    print(
        "The access token is now revoked on Plaid's side. To also delete the "
        f"stored secret, run:\n    gcloud secrets delete {secret_id}"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Revoke a Plaid Item using a token stored in Secret Manager.",
    )
    parser.add_argument(
        "secret_id",
        help="Secret Manager secret id holding the Plaid access token "
        "(e.g. plaid-access-token-chase-7254).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    remove_item(_parse_args().secret_id)
