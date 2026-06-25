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
sensitive and is **never committed** — not in `values.yaml`, not as a templated
Secret manifest, not anywhere in this directory. The chart contains **no
delegate-token value and no token Secret template at all**.

Instead, the token lives only in a Kubernetes Secret named
`harness-delegate-token` that you create **manually on the cluster, out-of-band,
before the first ArgoCD sync**. The chart reads it via its `existingDelegateToken`
value (set in `harness-delegate/values.yaml`), which requires the token to be
stored under the data key **`DELEGATE_TOKEN`** — that key name is fixed by the
upstream chart and is not configurable. Get the token from
**Harness → Account/Project Settings → Delegates → Tokens**, then:

```bash
kubectl create namespace harness-delegate-ng --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic harness-delegate-token \
  --from-literal=DELEGATE_TOKEN="YOUR_REAL_TOKEN" \
  --namespace=harness-delegate-ng
```

The delegate reads the token from this Secret at runtime via
`existingDelegateToken: harness-delegate-token`. Because we point the chart at an
existing Secret, it does **not** create or manage its own (empty) token Secret.
The plaintext lives only in cluster `etcd` (encrypted at rest) — it is never
rendered by Helm, never tracked by ArgoCD, and never present in any committed
manifest.

> If you forget to create the Secret before syncing, the delegate pod will fail
> to authenticate and stay un-Connected. Create the Secret, then let ArgoCD
> re-sync (or restart the delegate deployment).

### This Secret is intentionally excluded from GitOps

Because `harness-delegate-token` holds a live credential, it is **deliberately
left out of GitOps management**. It is not in git, so ArgoCD has nothing to
render or reconcile for it. To guarantee ArgoCD never flags or prunes the
out-of-band Secret, annotate it so ArgoCD ignores it:

```bash
kubectl annotate secret harness-delegate-token \
  --namespace=harness-delegate-ng \
  argocd.argoproj.io/compare-options=IgnoreExtraneous \
  argocd.argoproj.io/sync-options=Prune=false
```

- `compare-options: IgnoreExtraneous` — ArgoCD won't report the Secret as
  out-of-sync just because it isn't in git.
- `sync-options: Prune=false` — even an automated/pruning sync will never delete
  it.

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
      # No token parameters here — the delegate token is supplied by the
      # out-of-band `harness-delegate-token` Secret, never by ArgoCD.
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
- The **delegate token never enters git** at all — it exists only as the
  manually-created `harness-delegate-token` Secret, which is intentionally
  excluded from GitOps management (see above).
