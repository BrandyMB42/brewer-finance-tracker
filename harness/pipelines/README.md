# Harness Pipelines

Three pipelines gate every change from pull request through production.
They are defined in Harness CI/CD and reference secrets stored in GCP Secret Manager.

The runnable pipeline definitions live alongside this README:

| File | Pipeline | Trigger |
|------|----------|---------|
| [`pr-validation.yaml`](./pr-validation.yaml) | PR Validation | Pull request to `main` |
| [`staging-deploy.yaml`](./staging-deploy.yaml) | Staging Deploy | Push / merge to `main` |
| [`production-deploy.yaml`](./production-deploy.yaml) | Production Deploy | Manual |

The sections below describe each pipeline conceptually. See
[Importing into Harness](#importing-into-harness) at the end for step-by-step
setup of connectors, secrets, and triggers.

---

## 1. PR Validation

**Purpose:** Block merges that break tests, type checks, or linting.

**Trigger:** `On Pull Request` — fires on PR open, synchronize (new commits pushed), and reopen events targeting the `main` branch.

### Stages

| Stage | What it does |
|-------|-------------|
| **Lint** | `ruff check src tests` — enforces style and catches common bugs |
| **Type check** | `mypy src` with `strict = true` |
| **Unit tests** | `pytest tests/ --cov=src --cov-fail-under=80` |

### Required checks

All three stages must pass before GitHub allows the merge.  Configure this in
**GitHub → Settings → Branches → Branch protection rules** by adding the Harness
pipeline status check as a required status.

### Harness configuration notes

```yaml
trigger:
  type: PullRequest
  spec:
    actions:
      - Open
      - Synchronize
      - Reopen
    targetBranch: main
```

No environment-specific variables are needed.  The pipeline runs in an
ephemeral container and never touches GCP resources.

---

## 2. Staging Deploy

**Purpose:** Automatically deploy every merge to `main` to the staging Cloud Run service.

**Trigger:** `On Push` — fires when a commit is pushed (or a PR is merged) to `main`.

### Stages

| Stage | What it does |
|-------|-------------|
| **Build** | `docker build -t $IMAGE_TAG .` and pushes to Artifact Registry |
| **Deploy** | `gcloud run deploy` with `--image $IMAGE_TAG` |

### Environment variables

The deploy stage sets the following variables on the Cloud Run service:

| Variable | Value |
|----------|-------|
| `ENVIRONMENT` | `staging` |
| `GCP_PROJECT_ID` | `<your-gcp-project-id>` |
| `LOG_LEVEL` | `DEBUG` |

**No secrets are set as environment variables.**  The Cloud Run service account
must hold `roles/secretmanager.secretAccessor` so the app can call Secret
Manager at runtime.

### Harness configuration notes

```yaml
trigger:
  type: Push
  spec:
    branches:
      - main

stages:
  - stage:
      name: Build and Push Image
      type: CI
      spec:
        execution:
          steps:
            - step:
                type: BuildAndPushGCR
                spec:
                  connectorRef: gcp_connector
                  host: <region>-docker.pkg.dev
                  projectID: <your-gcp-project-id>
                  imageName: brewer-finance-tracker
                  tags:
                    - <+pipeline.sequenceId>
                    - latest

  - stage:
      name: Deploy to Staging
      type: Deployment
      spec:
        environment:
          name: staging
        execution:
          steps:
            - step:
                type: ShellScript
                spec:
                  shell: Bash
                  source:
                    type: Inline
                    spec:
                      script: |
                        gcloud run deploy brewer-finance-tracker-staging \
                          --image <region>-docker.pkg.dev/<project>/brewer-finance-tracker:<+pipeline.sequenceId> \
                          --region <region> \
                          --set-env-vars ENVIRONMENT=staging,GCP_PROJECT_ID=<project> \
                          --service-account finance-tracker-sa@<project>.iam.gserviceaccount.com \
                          --platform managed
```

---

## 3. Production Deploy

**Purpose:** Promote a validated staging image to production after chaos testing
passes and a human approves the release.

**Trigger:** Manual — an engineer initiates this pipeline from the Harness UI
after confirming the staging smoke tests and chaos validation report are green.

### Stages

| Stage | What it does |
|-------|-------------|
| **Chaos Validation** | Runs a Harness Chaos Engineering experiment against the staging service; the stage fails if the steady-state hypothesis is violated |
| **Approval Gate** | Pauses the pipeline and sends an email/Slack notification; a designated approver must click **Approve** in the Harness UI within 24 hours or the run times out |
| **Deploy to Production** | Promotes the same image tag that passed chaos validation to the production Cloud Run service |

### Approval gate

Configure the approval step with the following settings:

- **Approvers:** add the `finance-release-approvers` user group
- **Minimum approvals:** 1
- **Timeout:** 24 hours
- **Message:** Include the Harness chaos report URL and the staging deploy URL for the approver's reference

### Environment variables

| Variable | Value |
|----------|-------|
| `ENVIRONMENT` | `production` |
| `GCP_PROJECT_ID` | `<your-gcp-project-id>` |
| `LOG_LEVEL` | `INFO` |

### Harness configuration notes

```yaml
trigger:
  type: Manual

stages:
  - stage:
      name: Chaos Validation
      type: Custom
      spec:
        execution:
          steps:
            - step:
                type: ChaosExperiment
                spec:
                  experimentRef: brewer-finance-tracker-resiliency
                  expectedResiliencyScore: 90

  - stage:
      name: Approval Gate
      type: Approval
      spec:
        execution:
          steps:
            - step:
                type: HarnessApproval
                spec:
                  approvalMessage: |
                    Chaos score passed. Review staging at https://brewer-finance-tracker-staging-<hash>.run.app
                    Approve to promote to production.
                  includePipelineExecutionHistory: true
                  approvers:
                    userGroups:
                      - finance-release-approvers
                    minimumCount: 1
                  approverInputs: []
                timeout: 1d

  - stage:
      name: Deploy to Production
      type: Deployment
      spec:
        environment:
          name: production
        execution:
          steps:
            - step:
                type: ShellScript
                spec:
                  shell: Bash
                  source:
                    type: Inline
                    spec:
                      script: |
                        gcloud run deploy brewer-finance-tracker-prod \
                          --image <region>-docker.pkg.dev/<project>/brewer-finance-tracker:<+pipeline.sequenceId> \
                          --region <region> \
                          --set-env-vars ENVIRONMENT=production,GCP_PROJECT_ID=<project> \
                          --service-account finance-tracker-sa@<project>.iam.gserviceaccount.com \
                          --platform managed \
                          --traffic 100
```

---

## Secret Manager requirements

All three pipelines assume these secrets exist in GCP Secret Manager under the
project specified by `GCP_PROJECT_ID`:

| Secret name | Purpose |
|-------------|---------|
| `plaid-client-id` | Plaid API client ID |
| `plaid-secret` | Plaid API secret (environment-specific) |
| `plaid-access-token-<item-label>` | Per-item Plaid access token, created at link time by `plaid_link.exchange_public_token` |
| `webhook-signing-secret` | HMAC key used to verify inbound Plaid webhook payloads |

Google Sheets access does **not** use a stored key. `sheets_writer` / `snowball_sync`
authenticate as the runtime service account via Application Default Credentials;
share each target spreadsheet with `finance-tracker-sa` (Editor).

`scripts/setup.sh` creates the static secrets (everything except the
per-item access tokens) as placeholders and grants the runtime service account
access. Populate them with real values before the first deploy — see the
instructions printed at the end of that script.

The Cloud Run service account (`finance-tracker-sa`) must have
`roles/secretmanager.secretAccessor` on each secret. The same account also needs
`roles/secretmanager.secretVersionAdder`/`secretManager.admin` (or a custom role
granting `secrets.create` + `secrets.versions.add`) so that
`plaid_link.exchange_public_token` can create per-item access-token secrets at
runtime.

---

## Importing into Harness

### Prerequisites

1. A Harness account with a **Project** whose identifier matches the YAML:
   `projectIdentifier: brewer_finance_tracker` under `orgIdentifier: default`.
   Create these (or edit the identifiers in all three YAML files to match an
   existing project/org).
2. The GCP project bootstrapped via `scripts/setup.sh` (APIs enabled, runtime SA
   and Artifact Registry created, secrets populated).
3. A separate **CI/CD service account** with permission to build images and
   deploy Cloud Run. Grant it: `roles/run.admin`, `roles/cloudbuild.builds.editor`,
   `roles/artifactregistry.writer`, `roles/iam.serviceAccountUser` (to act as the
   runtime SA), and `roles/resourcemanager.projectIamAdmin` (required only by the
   production chaos gate, which toggles IAM bindings). Export a JSON key for it.

### Step 1 — Create connectors

| Connector | Identifier | Purpose | Notes |
|-----------|------------|---------|-------|
| **GitHub** | `github_connector` | Clone the repo, report PR status checks | Use a GitHub App or a PAT stored as a Harness secret. Enable **API Access** so PR validation can post status checks. |
| **GCP** | `gcp_connector` | (Optional) native Harness GCP steps | The pipelines authenticate with `gcloud` + a Harness secret key instead, so this is only needed if you switch to Harness-native GCR/Cloud Run steps. |

In Harness: **Project Settings → Connectors → New Connector**. The connector
identifiers must match `connectorRef` values in the YAML (`github_connector`).

### Step 2 — Create secrets

**Project Settings → Secrets → New Secret** (Text/File). These are Harness
platform secrets referenced as `<+secrets.getValue("name")>` — distinct from the
GCP Secret Manager secrets the *application* reads at runtime.

| Harness secret | Type | Value |
|----------------|------|-------|
| `gcp-cicd-sa-key` | File or Text | JSON key for the CI/CD service account |
| `gcp-project-id` | Text | Your GCP project ID |
| `staging-service-url` | Text | Cloud Run staging URL (fill in after first deploy) |
| `production-service-url` | Text | Cloud Run production URL (fill in after first prod deploy) |

> The service URLs are unknown until the first deploy. Run staging once, copy the
> URL Cloud Run prints, then set `staging-service-url` so the smoke-test stage
> can resolve it on subsequent runs.

### Step 3 — Create the approver user group

The production approval references `finance_release_approvers`. Create it under
**Account/Project Settings → Access Control → User Groups** with identifier
`finance_release_approvers` and add the engineers allowed to approve releases.

### Step 4 — Import the pipelines

For each YAML file:

1. **Pipelines → New Pipeline → Import From Git** (if the repo is connected via
   `github_connector`), pointing at `harness/pipelines/<file>.yaml`; or
2. **New Pipeline → Inline → YAML view**, then paste the file contents.

Importing from Git is preferred — the pipeline stays version-controlled and
edits flow through PRs.

### Step 5 — Configure triggers

Triggers are set in the Harness UI (**Pipeline → Triggers**), not in these YAML
files:

| Pipeline | Trigger type | Configuration |
|----------|--------------|---------------|
| PR Validation | **Pull Request** | Connector `github_connector`, events Open/Reopen/Updated, target branch `main` |
| Staging Deploy | **Push** | Connector `github_connector`, event Push, branch `main` |
| Production Deploy | *(none)* | Run manually from the UI |

### Step 6 — Wire PR validation as a required check

In GitHub **Settings → Branches → Branch protection rules** for `main`, add the
Harness PR Validation status check as **required**, so PRs cannot merge until
lint, tests (≥80% coverage), and the TruffleHog secret scan all pass.

### Notes on the YAML

- All three pipelines run on **Harness Cloud** (`runtime.type: Cloud`) for
  zero-setup infrastructure. To run on your own Kubernetes/Docker delegate,
  replace each `runtime` block with the appropriate `infrastructure` spec.
- Secrets are **always** referenced via `<+secrets.getValue("...")>` and written
  to disk only inside ephemeral build containers — never committed or echoed in
  full.
- The chaos gate restores the revoked IAM binding in a dedicated step so a
  failed assertion never leaves staging in a broken state; the recovery probe is
  the hard pass/fail gate for the stage.
