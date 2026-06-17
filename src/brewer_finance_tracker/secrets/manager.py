"""GCP Secret Manager wrapper that fetches secrets at runtime.

All secrets are retrieved here — never from environment variables, config
files, or hardcoded strings — to satisfy the secret-management requirements
enforced by the Harness pipeline security gate.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from google.cloud import secretmanager

logger = logging.getLogger(__name__)


@lru_cache(maxsize=None)
def get_secret(project_id: str, secret_id: str, version: str = "latest") -> str:
    """Fetch a secret value from GCP Secret Manager.

    Results are cached in-process for the lifetime of the worker to avoid
    repeated API round-trips on hot paths.  For secrets that rotate frequently,
    call ``get_secret.cache_clear()`` before the next access.

    The calling identity (Cloud Run service account) must hold the
    ``roles/secretmanager.secretAccessor`` IAM role on the target secret.

    Args:
        project_id: GCP project that owns the secret resource.
        secret_id: The name of the secret (not the full resource path).
        version: Secret version to access.  Defaults to ``"latest"``.

    Returns:
        The secret payload decoded as a UTF-8 string.

    Raises:
        google.api_core.exceptions.NotFound: If the secret or version does not
            exist in Secret Manager.
        google.api_core.exceptions.PermissionDenied: If the caller lacks the
            ``secretAccessor`` role.
    """
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version}"
    logger.info(
        "Fetching secret from Secret Manager",
        extra={"secret_id": secret_id, "version": version},
    )
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")
