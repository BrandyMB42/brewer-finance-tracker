"""Local-only Flask backend for the one-time Plaid Link account-setup tool.

This server is **not deployed**. It runs on your laptop to walk through Plaid
Link once per institution and stash the resulting long-lived access tokens in
GCP Secret Manager, where the deployed app reads them at runtime.

Unlike the deployed application (which pulls Plaid credentials *from* Secret
Manager), this tool reads ``PLAID_CLIENT_ID`` / ``PLAID_SECRET`` from a local
``.env`` file. That keeps the setup flow self-contained and lets you test in
Plaid's sandbox before pointing it at production.

Endpoints
---------
``POST /create-link-token``  Create a short-lived Link token for the browser.
``POST /exchange-token``     Exchange a public token, store the access token in
                             Secret Manager as ``plaid-access-{slug}``.
``GET  /status``             List institutions already connected (by inspecting
                             which ``plaid-access-*`` secrets exist).

Run with::

    pip install -r requirements.txt
    python server.py

then open ``index.html`` in your browser.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import plaid
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from plaid.api import plaid_api
from plaid.api_client import ApiClient
from plaid.configuration import Configuration
from plaid.model.country_code import CountryCode
from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_public_token_exchange_request import (
    ItemPublicTokenExchangeRequest,
)
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()

PLAID_CLIENT_ID = os.environ.get("PLAID_CLIENT_ID", "")
PLAID_SECRET = os.environ.get("PLAID_SECRET", "")
PLAID_ENV = os.environ.get("PLAID_ENV", "sandbox").lower()
# Optional. If unset, gcloud uses its active configured project.
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")

# The Secret Manager id prefix for stored Plaid access tokens.
SECRET_PREFIX = "plaid-access-"

_PLAID_HOSTS = {
    "sandbox": plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}

CLIENT_USER_ID = "brewer-family-setup"
CLIENT_NAME = "Brewer Finance Tracker"
PRODUCTS = ["transactions", "liabilities", "balance"]
COUNTRY_CODES = ["US"]


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


def _slugify(name: str) -> str:
    """Turn an institution name into a Secret-Manager-safe slug.

    Secret ids may contain only letters, digits, hyphens and underscores.
    e.g. ``"Chase Bank, N.A."`` -> ``"chase-bank-n-a"``.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "unknown"


# ---------------------------------------------------------------------------
# gcloud helpers (Secret Manager via the CLI, not the client library)
# ---------------------------------------------------------------------------
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


def _gcloud(*args: str, check: bool = True, capture: bool = True,
            stdin_text: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a gcloud command, appending --project when GCP_PROJECT_ID is set."""
    cmd = [_gcloud_path(), *args]
    if GCP_PROJECT_ID:
        cmd += ["--project", GCP_PROJECT_ID]
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
        input=stdin_text,
    )


def _secret_exists(secret_id: str) -> bool:
    """Return True if a Secret Manager secret with *secret_id* already exists."""
    result = _gcloud(
        "secrets", "describe", secret_id, "--format=value(name)",
        check=False,
    )
    return result.returncode == 0


def _store_access_token(secret_id: str, access_token: str) -> None:
    """Create *secret_id* if needed, then add *access_token* as a new version."""
    if not _secret_exists(secret_id):
        _gcloud(
            "secrets", "create", secret_id,
            "--replication-policy=automatic",
        )
    _gcloud(
        "secrets", "versions", "add", secret_id, "--data-file=-",
        stdin_text=access_token,
    )


def _list_connected_institutions() -> list[str]:
    """Return institution slugs for every existing plaid-access-* secret."""
    result = _gcloud(
        "secrets", "list",
        f"--filter=name:{SECRET_PREFIX}",
        "--format=value(name)",
        check=False,
    )
    if result.returncode != 0:
        return []
    slugs = []
    for line in result.stdout.splitlines():
        # gcloud may return the bare id or a full resource path; take the tail.
        secret_id = line.strip().rsplit("/", 1)[-1]
        if secret_id.startswith(SECRET_PREFIX):
            slugs.append(secret_id[len(SECRET_PREFIX):])
    return sorted(slugs)


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.after_request
def _allow_cors(response):  # type: ignore[no-untyped-def]
    """Allow the locally-opened index.html (file:// origin) to call this API."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.route("/create-link-token", methods=["POST", "OPTIONS"])
def create_link_token():  # type: ignore[no-untyped-def]
    """Create a short-lived Plaid Link token for the browser SDK."""
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        client = _build_client()
        link_request = LinkTokenCreateRequest(
            user=LinkTokenCreateRequestUser(client_user_id=CLIENT_USER_ID),
            client_name=CLIENT_NAME,
            products=[Products(p) for p in PRODUCTS],
            country_codes=[CountryCode(c) for c in COUNTRY_CODES],
            language="en",
        )
        response = client.link_token_create(link_request)
        return jsonify({"link_token": response["link_token"]})
    except plaid.ApiException as exc:
        return jsonify({"error": "plaid_error", "detail": exc.body}), 502
    except Exception as exc:  # noqa: BLE001 - surface any setup error to the UI
        return jsonify({"error": "server_error", "detail": str(exc)}), 500


@app.route("/exchange-token", methods=["POST", "OPTIONS"])
def exchange_token():  # type: ignore[no-untyped-def]
    """Exchange a public token and store the access token in Secret Manager."""
    if request.method == "OPTIONS":
        return ("", 204)
    payload = request.get_json(silent=True) or {}
    public_token = payload.get("public_token")
    if not public_token:
        return jsonify({"error": "missing_public_token"}), 400

    # Institution metadata from Plaid Link's onSuccess callback (preferred).
    institution = payload.get("institution") or {}
    institution_name = institution.get("name")

    try:
        client = _build_client()
        exchange = client.item_public_token_exchange(
            ItemPublicTokenExchangeRequest(public_token=public_token)
        )
        access_token = exchange["access_token"]
        item_id = exchange["item_id"]

        # Fall back to the API if the browser didn't pass an institution name.
        if not institution_name:
            institution_name = _lookup_institution_name(client, access_token)

        slug = _slugify(institution_name)
        secret_id = f"{SECRET_PREFIX}{slug}"
        _store_access_token(secret_id, access_token)

        return jsonify(
            {
                "institution_name": institution_name,
                "item_id": item_id,
                "secret_id": secret_id,
            }
        )
    except plaid.ApiException as exc:
        return jsonify({"error": "plaid_error", "detail": exc.body}), 502
    except subprocess.CalledProcessError as exc:
        return (
            jsonify(
                {
                    "error": "secret_manager_error",
                    "detail": (exc.stderr or str(exc)).strip(),
                }
            ),
            500,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": "server_error", "detail": str(exc)}), 500


def _lookup_institution_name(client: plaid_api.PlaidApi, access_token: str) -> str:
    """Resolve an institution's display name from an access token."""
    item = client.item_get(ItemGetRequest(access_token=access_token))
    institution_id = item["item"]["institution_id"]
    if not institution_id:
        return "unknown"
    details = client.institutions_get_by_id(
        InstitutionsGetByIdRequest(
            institution_id=institution_id,
            country_codes=[CountryCode(c) for c in COUNTRY_CODES],
        )
    )
    return str(details["institution"]["name"])


@app.route("/status", methods=["GET", "OPTIONS"])
def status():  # type: ignore[no-untyped-def]
    """List institutions already connected (existing plaid-access-* secrets)."""
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        return jsonify(
            {
                "plaid_env": PLAID_ENV,
                "connected": _list_connected_institutions(),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": "server_error", "detail": str(exc)}), 500


if __name__ == "__main__":
    _require_credentials()
    print(f"Plaid Link setup server running in '{PLAID_ENV}' mode.")
    print("Open tools/plaid-link-setup/index.html in your browser.")
    # Local-only: bind to localhost so nothing is exposed on the network.
    app.run(host="127.0.0.1", port=5000, debug=True)
