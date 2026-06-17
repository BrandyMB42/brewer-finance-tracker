#!/usr/bin/env bash
# One-time GCP bootstrap for brewer-finance-tracker.
#
# Usage:
#   bash scripts/setup.sh <GCP_PROJECT_ID> [REGION]
#
# The script is idempotent — safe to re-run if a resource already exists.

set -euo pipefail

PROJECT_ID="${1:?Usage: setup.sh <GCP_PROJECT_ID> [REGION]}"
REGION="${2:-us-central1}"
SA_NAME="finance-tracker-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
REPO_NAME="brewer-finance-tracker"

log() { echo "==> $*"; }

log "Setting active project to ${PROJECT_ID}"
gcloud config set project "${PROJECT_ID}"

# ---------------------------------------------------------------------------
# APIs
# ---------------------------------------------------------------------------

log "Enabling required APIs"
gcloud services enable \
  secretmanager.googleapis.com \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudresourcemanager.googleapis.com \
  iam.googleapis.com

# ---------------------------------------------------------------------------
# Service account
# ---------------------------------------------------------------------------

log "Creating service account ${SA_NAME}"
gcloud iam service-accounts create "${SA_NAME}" \
  --display-name="Brewer Finance Tracker runtime SA" \
  --project="${PROJECT_ID}" 2>/dev/null || \
  log "  Service account already exists — skipping"

log "Granting IAM roles to ${SA_EMAIL}"
for ROLE in \
  roles/secretmanager.secretAccessor \
  roles/run.invoker; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${ROLE}" \
    --condition=None \
    --quiet
done

# ---------------------------------------------------------------------------
# Artifact Registry
# ---------------------------------------------------------------------------

log "Creating Artifact Registry repository ${REPO_NAME}"
gcloud artifacts repositories create "${REPO_NAME}" \
  --repository-format=docker \
  --location="${REGION}" \
  --project="${PROJECT_ID}" 2>/dev/null || \
  log "  Repository already exists — skipping"

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

# Each secret is created as a placeholder.
# Run the commented command below each block to populate the real value.

SECRETS=(
  "plaid-client-id"
  "plaid-secret"
  "sheets-service-account-json"
  "webhook-signing-secret"
)

for SECRET in "${SECRETS[@]}"; do
  log "Ensuring secret exists: ${SECRET}"
  if ! gcloud secrets describe "${SECRET}" --project="${PROJECT_ID}" &>/dev/null; then
    printf "PLACEHOLDER" | gcloud secrets create "${SECRET}" \
      --data-file=- \
      --replication-policy=automatic \
      --project="${PROJECT_ID}"
    log "  Created '${SECRET}' with PLACEHOLDER value — update before deploying"
  else
    log "  Secret '${SECRET}' already exists — skipping"
  fi

  gcloud secrets add-iam-policy-binding "${SECRET}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor" \
    --project="${PROJECT_ID}" \
    --quiet
done

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

cat <<DONE

Bootstrap complete.  Update each secret before running the first deploy:

  # Plaid credentials (from console.plaid.com)
  echo -n "<plaid-client-id>"  | gcloud secrets versions add plaid-client-id  --data-file=- --project=${PROJECT_ID}
  echo -n "<plaid-secret>"     | gcloud secrets versions add plaid-secret      --data-file=- --project=${PROJECT_ID}

  # Google service account JSON for Sheets access
  gcloud secrets versions add sheets-service-account-json --data-file=sa.json --project=${PROJECT_ID}

  # Random string used to verify Plaid webhook HMAC signatures
  openssl rand -hex 32 | gcloud secrets versions add webhook-signing-secret --data-file=- --project=${PROJECT_ID}

DONE
