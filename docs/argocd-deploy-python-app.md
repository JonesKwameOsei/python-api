# Deploying python-app with Argo CD (GitOps)

How to stop running `helm install` by hand and let Argo CD deploy [charts/python-app](../charts/python-app/) from GitHub instead. Git becomes the source of truth: you change the chart or its values, push, and Argo CD makes the cluster match.

This builds on [argocd-deploy-with-helm.md](argocd-deploy-with-helm.md) (Argo CD itself) and [k8s-manifest-to-helm.md](k8s-manifest-to-helm.md) (the python-app chart).

## Contents

1. [What changes with GitOps](#1-what-changes-with-gitops)
2. [Starting point](#2-starting-point)
3. [Push the repo to GitHub](#3-push-the-repo-to-github)
4. [The Application manifest](#4-the-application-manifest)
5. [Create and sync the Application](#5-create-and-sync-the-application)
6. [Verifying](#6-verifying)
7. [Turning on automated sync](#7-turning-on-automated-sync)
8. [Day-to-day: the GitOps loop](#8-day-to-day-the-gitops-loop)
9. [Troubleshooting](#9-troubleshooting)
10. [Cheat sheet](#10-cheat-sheet)
11. [Next steps](#11-next-steps)

---

## 1. What changes with GitOps

```text
Before:   you ──helm upgrade──► cluster

After:    you ──git push──► GitHub ◄──polls── Argo CD ──applies──► cluster
```

| Helm by hand | Argo CD |
|---|---|
| The cluster has whatever you last ran | The cluster has whatever is in Git |
| `helm upgrade ... --set image.tag=v2` | Edit `values.yaml`, commit, push |
| `kubectl edit` changes stick silently | Argo CD shows the drift and can undo it (self-heal) |
| `helm history` / `helm rollback` | `git log` / `git revert` (and `argocd app rollback`) |

### Helm is still used, but differently

Argo CD doesn't run `helm install`. It runs the equivalent of `helm template` to render your chart into plain YAML, then applies that YAML itself. So:

- the chart and `values.yaml` work exactly as before;
- **`helm list` will not show python-app**. Argo CD owns the release, not Helm. Use `argocd app get` instead;
- `NOTES.txt` isn't printed anywhere.

---

## 2. Starting point

Check the state before you begin:

```bash
helm list -A                      # python-app should NOT be listed (or uninstall it first)
kubectl get ns python             # doesn't exist yet; Argo CD will create it
kubectl -n argocd get applications
```

If python-app is still installed by Helm, uninstall it first so two tools aren't fighting over the same objects:

```bash
helm uninstall python-app -n python
```

What you need:

| Item | Value |
|---|---|
| Git repo | `https://github.com/JonesKwameOsei/python-api.git` (public, so no credentials needed) |
| Chart path in the repo | `charts/python-app` |
| Branch | `main` |
| Target namespace | `python` (needs the `gateway-access: "true"` label) |
| Image | `kwameds/python-app:v1`. `image.tag` is empty, so the chart uses `appVersion: "v1"`. |

---

## 3. Push the repo to GitHub

Argo CD reads from GitHub, not from your laptop. Anything not pushed doesn't exist as far as Argo CD is concerned.

```bash
git status                 # check nothing unexpected is staged (.venv/ and .vscode/ are ignored)
git add .
git commit -m "Add python-app Helm chart and Argo CD config"
git push -u origin main
```

Confirm the chart is visible on GitHub at `charts/python-app/Chart.yaml`.

**Never commit secrets.** Passwords, tokens and kubeconfigs don't belong in the repo. That includes the Argo CD admin password. Everything in this repo so far is safe to commit.

---

## 4. The Application manifest

An **Application** is an Argo CD custom resource that says: *take this path from this repo, render it, and keep it applied to this cluster and namespace.*

Create `argocd/python-app.yaml` at the repo root:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: python-app
  namespace: argocd              # Applications always live in Argo CD's namespace
spec:
  project: default

  source:
    repoURL: https://github.com/JonesKwameOsei/python-api.git
    targetRevision: main
    path: charts/python-app
    helm:
      releaseName: python-app
      valueFiles:
        - values.yaml

  destination:
    server: https://kubernetes.default.svc
    namespace: python

  syncPolicy:
    syncOptions:
      - CreateNamespace=true
    managedNamespaceMetadata:
      labels:
        gateway-access: "true"
```

### Field by field

| Field | Why |
|---|---|
| `metadata.namespace: argocd` | Argo CD only watches Applications in its own namespace. This is **not** where the app is deployed. |
| `project: default` | Argo CD Projects restrict which repos and namespaces an app can use. `default` allows everything; fine for learning. |
| `repoURL` | Use the **HTTPS** URL. The SSH URL (`git@github.com:...`) would need an SSH key registered in Argo CD. |
| `targetRevision: main` | Branch, tag or commit to follow. |
| `path` | Folder in the repo containing `Chart.yaml`. Argo CD detects it's a Helm chart automatically. |
| `helm.releaseName` | Keeps object names as `python-app`. Otherwise the release name would default to the Application name, which happens to be the same here. |
| `helm.valueFiles` | Paths are **relative to `path`**. Add more files (e.g. `values-prod.yaml`) to layer overrides. |
| `destination.server` | `https://kubernetes.default.svc` means "the cluster Argo CD itself runs in". |
| `destination.namespace` | The release namespace (same as `helm install -n python`). |
| `CreateNamespace=true` | Argo CD creates `python` if it's missing. |
| `managedNamespaceMetadata.labels` | Puts `gateway-access: "true"` on the namespace it creates, so the Gateway accepts the HTTPRoute. This replaces [deploy/namespace-python.yaml](../deploy/namespace-python.yaml). It only works together with `CreateNamespace=true`. |

There's **no `automated` block yet**. Start with manual sync so you see each step. Section 7 turns automation on.

### Where the Argo CD files live

You now have two Argo CD files:

- `charts/python-app/argocd/values-argo.yaml`, the values for installing **Argo CD itself**;
- `argocd/python-app.yaml`, the Application that deploys **python-app**.

The first one is inside the python-app chart folder, so it gets bundled into the chart even though it has nothing to do with python-app. Consider moving it to `argocd/values-argo.yaml` next to the Application. If you do, update the `-f` path in the Argo CD install command.

---

## 5. Create and sync the Application

Commit and push the Application file so it lives in Git too, then apply it:

```bash
git add argocd/python-app.yaml
git commit -m "Add Argo CD Application for python-app"
git push

kubectl apply -f argocd/python-app.yaml
```

> The CLI equivalent is `argocd app create python-app --repo ... --path ... --dest-namespace python ...`, but a YAML file in Git is better: it's reviewable and reproducible.

Check its status:

```bash
argocd app get python-app
```

Expect `Sync Status: OutOfSync` and `Health Status: Missing`. Argo CD has rendered the chart and sees that nothing exists in the cluster yet. In the UI at `http://argocd.test.com:8081`, the app tile is yellow.

Look at what it would do, then do it:

```bash
argocd app diff python-app        # the objects that would be created
argocd app sync python-app        # create them
argocd app wait python-app --health --timeout 120
```

You can also press **Sync** in the UI.

### Sync vs health

| Status | Question it answers | Values |
|---|---|---|
| **Sync** | Does the cluster match Git? | `Synced`, `OutOfSync` |
| **Health** | Is what's running actually working? | `Healthy`, `Progressing`, `Degraded`, `Missing` |

An app can be `Synced` but `Degraded`. For example, the Deployment matches Git but the pod is crash-looping.

---

## 6. Verifying

```bash
argocd app get python-app                 # Synced + Healthy

kubectl get ns python --show-labels       # gateway-access=true
kubectl -n python get deploy,svc,pods,httproute

kubectl -n python describe httproute python-app   # Accepted=True, ResolvedRefs=True

curl http://python-app.test.com:8081/api/v1/healthz
```

In the UI, click the app tile to see the resource tree: Application → Deployment → ReplicaSet → Pod, plus Service, ConfigMap and HTTPRoute.

---

## 7. Turning on automated sync

Once a manual sync works, let Argo CD sync on its own. Add `automated` to `syncPolicy`:

```yaml
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
    managedNamespaceMetadata:
      labels:
        gateway-access: "true"
```

| Option | Effect |
|---|---|
| `automated` | When Git changes, sync without anyone pressing Sync. |
| `prune: true` | If a resource is removed from the chart, delete it from the cluster. Without this, removed resources are left behind and the app shows `OutOfSync`. |
| `selfHeal: true` | If someone changes a resource in the cluster (e.g. `kubectl edit`), put it back to what Git says. |

Commit, push and re-apply:

```bash
git commit -am "Enable automated sync for python-app"
git push
kubectl apply -f argocd/python-app.yaml
```

### Try self-heal

```bash
kubectl -n python scale deploy python-app --replicas=3
kubectl -n python get deploy python-app -w      # watch it go back to 1 within seconds
```

That's GitOps in one command: the cluster can't drift from Git.

---

## 8. Day-to-day: the GitOps loop

Every change now goes through Git. To run two replicas:

```bash
# charts/python-app/values.yaml
replicaCount: 2
```

```bash
git commit -am "Scale python-app to 2 replicas"
git push
```

Argo CD checks the repo about **every 3 minutes**. To skip the wait:

```bash
argocd app get python-app --refresh     # or the Refresh button in the UI
```

### Releasing a new image

1. Build and push a new image, e.g. `kwameds/python-app:v2`.
2. Set `image.tag: v2` in `values.yaml`, or bump `appVersion` in `Chart.yaml`.
3. Commit and push. Argo CD rolls out the new Deployment.

### Rolling back

The GitOps way is to revert the commit:

```bash
git revert HEAD
git push
```

`argocd app rollback python-app <history-id>` also works, but only while auto-sync is off. Otherwise Argo CD immediately syncs back to what Git says.

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ComparisonError: repository not found` | Repo not pushed yet, or SSH URL used | Push to GitHub; use the `https://` URL |
| `app path does not exist` | `path` wrong, or the chart isn't on `targetRevision` yet | Check the folder exists on GitHub on that branch |
| `namespaces "python" not found` during sync | `CreateNamespace=true` missing | Add it under `syncOptions` |
| App `Synced`/`Healthy` but `curl` returns 404 | Namespace missing the `gateway-access` label, so the route is rejected | Check `managedNamespaceMetadata`; `kubectl describe httproute -n python` |
| Pod `ImagePullBackOff`, app `Degraded` | Tag doesn't exist on Docker Hub | Check `image.tag` / `appVersion` |
| App flips to `OutOfSync`, or self-heal fights the HPA | A Deployment with an HPA also sets `spec.replicas`. Your chart already avoids this: [deployment.yaml](../charts/python-app/templates/deployment.yaml) skips `replicas` when `autoscaling.enabled` is true. | For charts that don't, add `ignoreDifferences` for `/spec/replicas` on the Deployment |
| Pushed a change but nothing happens | Still within the 3-minute poll | `argocd app get python-app --refresh` |
| `helm list` doesn't show python-app | Expected | Argo CD owns it; use `argocd app get` |

Read the details with:

```bash
argocd app get python-app            # conditions and per-resource status
kubectl -n argocd describe application python-app
```

---

## 10. Cheat sheet

```bash
# Create / update the Application
kubectl apply -f argocd/python-app.yaml

# Status
argocd app list
argocd app get python-app
argocd app get python-app --refresh      # re-read Git now

# Inspect and sync
argocd app diff python-app
argocd app sync python-app
argocd app wait python-app --health

# History
argocd app history python-app

# Delete the Application AND everything it deployed
argocd app delete python-app
```

`argocd app delete` removes the deployed resources as well as the Application. Deleting the Application with `kubectl delete` only cascades if the Application has Argo CD's `resources-finalizer.argocd.argoproj.io` finalizer.

---

## 11. Next steps

- **App of apps:** one parent Application that points at the `argocd/` folder, so adding a new Application YAML to Git is enough to deploy it.
- **Argo CD manages itself:** an Application for the `argo/argo-cd` chart using your `values-argo.yaml`, so Argo CD upgrades also go through Git.
- **Environments:** `values-dev.yaml` / `values-prod.yaml` and one Application per environment (or an ApplicationSet).
- **Argo CD Image Updater:** bumps `image.tag` in Git automatically when a new image is pushed.
