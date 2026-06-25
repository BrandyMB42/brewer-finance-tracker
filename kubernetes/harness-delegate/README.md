# Harness Delegate (Helm chart)

This directory is an **umbrella Helm chart** that deploys a self-hosted
[Harness delegate](https://developer.harness.io/docs/platform/delegates/delegate-concepts/delegate-overview/)
to our Kubernetes cluster. The delegate is the agent that lets the pipelines in
[`harness/pipelines/`](../../harness/pipelines/) execute on our own
infrastructure instead of Harness Cloud.

| File | Purpose |
|------|---------|
| [`Chart.yaml`](./Chart.yaml) | Declares the official `harness-delegate-ng` chart as a dependency. |
| [`values.yaml`](./values.yaml) | Our delegate configuration (name, account, image, resources). |

For the repo-wide GitOps story and the delegate-token security model, see
[`../README.md`](../README.md).

---

## Why an umbrella chart?

`Chart.yaml` lists the upstream delegate chart as a dependency:

```yaml
dependencies:
  - name: harness-delegate-ng
    version: "1.0.0"
    repository: https://app.harness.io/storage/harness-download/delegate-helm-chart/
```

This pins the exact upstream version in git, keeps our overrides in one
reviewable `values.yaml`, and lets ArgoCD render everything with a single chart
reference. To resolve the dependency locally:

```bash
helm dependency update kubernetes/harness-delegate
```

---

## The delegate token

This chart carries **no delegate token** — there is no token value in
`values.yaml` and no Secret template. The token lives only in a Kubernetes
Secret named `harness-delegate-token` (key `delegateToken`) that you create
**manually on the cluster** before the first ArgoCD sync. That Secret is
intentionally excluded from GitOps management.

See [`../README.md`](../README.md#secrets-are-never-stored-in-git) for the exact
`kubectl create secret` command and the ArgoCD exclusion annotations.

---

## Verifying the delegate

After the first sync:

```bash
kubectl get pods -n harness-delegate-ng
kubectl logs -n harness-delegate-ng deploy/gcp-delegate
```

Then confirm it shows **Connected** in
**Harness → Account/Project Settings → Delegates**.
