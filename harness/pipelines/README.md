# Harness Pipelines

Three pipelines gate every change from pull request through production.
They are defined in Harness CI/CD and reference secrets stored in GCP Secret Manager.

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
                          --service-account brewer-finance-tracker-sa@<project>.iam.gserviceaccount.com \
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
                          --service-account brewer-finance-tracker-sa@<project>.iam.gserviceaccount.com \
                          --platform managed \
                          --traffic 100
```

---

## Secret Manager requirements

All three pipelines assume these secrets exist in GCP Secret Manager under the
project specified by `GCP_PROJECT_ID`:

| Secret name | Purpose |
|-------------|---------|
| `webhook-signing-secret` | HMAC key used to verify inbound webhook payloads |

Create secrets with:

```bash
echo -n "<value>" | gcloud secrets create webhook-signing-secret \
  --data-file=- \
  --replication-policy=automatic \
  --project=<your-gcp-project-id>
```

The Cloud Run service account (`brewer-finance-tracker-sa`) must have
`roles/secretmanager.secretAccessor` on each secret.
