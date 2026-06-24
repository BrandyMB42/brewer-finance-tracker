# Kubernetes / GitOps

This directory holds the Kubernetes manifests that ArgoCD applies to our cluster.
It is managed **GitOps-style**: the desired state lives in git, and ArgoCD
continuously reconciles the cluster to match it. Nobody runs `kubectl apply` or
`helm install` by hand for these resources — you change a file, open a PR, merge,
and ArgoCD syncs.

## Layout

| Path | What it is |
|------|------------|
| [`namespace.yaml`](./namespace.yaml) | Creates the `harness-delegate-ng` namespace. |
| [`harness-delegate/`](./harness-delegate/) | Umbrella Helm chart that deploys the self-hosted Harness delegate. See its [README](./harness-delegate/README.md). |

## Secrets are never stored in git

The Harness delegate authenticates with a **delegate token**. That token is
sensitive and is **never committed** — not in `values.yaml`, not in any manifest
in this directory.

What lives in git is only a placeholder. `harness-delegate/values.yaml` sets:

```yaml
delegateTokenValue: HARNESS_DELEGATE_TOKEN_PLACEHOLDER
```

and `harness-delegate/templates/delegate-token-secret.yaml` renders that value
into a Kubernetes Secret:

```yaml
kind: Secret
metadata:
  name: harness-delegate-token
  namespace: harness-delegate-ng
stringData:
  delegateToken: {{ .Values.delegateTokenValue | quote }}
```

The **real** token is created directly on the cluster, out-of-band, and ArgoCD
injects it over the placeholder at sync time (via a parameter override or
`valuesObject` on the Application). Get the token from
**Harness → Account/Project Settings → Delegates → Tokens**, then create the
override secret on the cluster:

```bash
kubectl create namespace harness-delegate-ng --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic harness-delegate-token-override \
  --namespace harness-delegate-ng \
  --from-literal=delegateTokenValue='<paste-token-here>'
```

Point the ArgoCD Application at that secret so it overrides
`delegateTokenValue` at render time — the plaintext token lives only in cluster
`etcd` (encrypted at rest), never in the manifests ArgoCD reads from git.

> For a fully GitOps-friendly token, manage it with
> [Sealed Secrets](https://github.com/bitnami-labs/sealed-secrets) or the
> [External Secrets Operator](https://external-secrets.io/) backed by GCP Secret
> Manager (where this project already stores its other secrets). Either lets the
> *encrypted* reference live in git while the plaintext never does.

## How ArgoCD watches this directory

ArgoCD is configured with an `Application` whose source points at this repo and
this path. On every commit to `main`, ArgoCD detects the change, renders the Helm
chart, and syncs the result into the cluster.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: harness-delegate
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/<org>/brewer-finance-tracker.git
    targetRevision: main
    path: kubernetes/harness-delegate
    helm:
      valueFiles:
        - values.yaml
      # Override the placeholder with the real token at sync time.
      parameters:
        - name: delegateTokenValue
          value: $ARGOCD_ENV_DELEGATE_TOKEN   # sourced from a cluster secret
  destination:
    server: https://kubernetes.default.svc
    namespace: harness-delegate-ng
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

Key points:

- **`path: kubernetes/harness-delegate`** — ArgoCD watches exactly that folder.
  A push that changes `Chart.yaml`, `values.yaml`, or a template triggers a sync.
- **`automated` + `selfHeal`** — manual drift in the cluster is reverted back to
  what's in git.
- **`namespace.yaml`** can be applied as its own Application or relied upon via
  `CreateNamespace=true`; either way the `harness-delegate-ng` namespace exists
  before the delegate is deployed.
- The **real delegate token never enters git** — only the placeholder does, and
  ArgoCD overrides it from a cluster secret during rendering.
