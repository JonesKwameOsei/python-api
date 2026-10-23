# From Ingress to Gateway API on k3d

How `python-app` was moved from a Kubernetes Ingress to the Gateway API on a local k3d cluster: why, how it works, the migration steps, adding more apps, and what broke along the way.

For the Ingress/Service background this builds on, see [service-explanation.md](service-explanation.md).

## Contents

1. [Current setup](#1-current-setup)
2. [Why move off Ingress](#2-why-move-off-ingress)
3. [How Gateway API works](#3-how-gateway-api-works)
4. [Ingress vs Gateway API](#4-ingress-vs-gateway-api)
5. [Enabling Gateway API in k3s Traefik](#5-enabling-gateway-api-in-k3s-traefik)
6. [Migrating python-app](#6-migrating-python-app)
7. [Adding another app](#7-adding-another-app)
8. [Route precedence: why every app gets a hostname](#8-route-precedence-why-every-app-gets-a-hostname)
9. [Routes from other namespaces](#9-routes-from-other-namespaces)
10. [Features Ingress needed annotations for](#10-features-ingress-needed-annotations-for)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Current setup

Cluster (k3s v1.35, Traefik v3.6):

```bash
k3d cluster create --api-port 6550 -p "8081:80@loadbalancer" --agents 2
```

Manifests in [deploy/](deploy/):

| File | Kind(s) | Owner role | Purpose |
|---|---|---|---|
| [traefik-config.yaml](deploy/traefik-config.yaml) | HelmChartConfig | Cluster admin | Turns on Traefik's Gateway API provider |
| [gateway.yaml](deploy/gateway.yaml) | Gateway `web` | Platform | The entrance: HTTP on Traefik port 8000, routes from labelled namespaces only |
| [deployment.yaml](deploy/deployment.yaml) | Deployment | App team | `kwameds/python-app:v1`, Flask on port 5000 |
| [service.yaml](deploy/service.yaml) | Service | App team | `python-app:8080` → pod `5000` |
| [httproute.yaml](deploy/httproute.yaml) | HTTPRoute | App team | `python.localhost` → `python-app:8080` |
| [whoami.yaml](deploy/whoami.yaml) | Deployment + Service + HTTPRoute | App team | Second demo app at `whoami.localhost` |

Plus one setting that is **not in a file** (see [section 11](#11-troubleshooting)):

```bash
kubectl label namespace default gateway-access=true
```

**Access:**

```bash
curl python.localhost:8081/api/v1/details
curl whoami.localhost:8081/
```

```
localhost:8081 → k3d serverlb:80 → Traefik (Service :80 → pod :8000)
                                     └── Gateway "web" (listener :8000)
                                           ├── HTTPRoute python-app (python.localhost) → python-app:8080 → pod:5000
                                           └── HTTPRoute whoami     (whoami.localhost) → whoami:80      → pod:80
```

---

## 2. Why move off Ingress

- **Ingress is frozen.** It still works and isn't deprecated, but gets no new features. Gateway API is its official successor, developed by Kubernetes SIG Network.
- **ingress-nginx is retired.** The community controller's best-effort maintenance ended in March 2026, which pushed many teams to migrate.
- **Ingress only knows "host + path → Service".** Everything else (rewrites, redirects, header matching, canaries) was added through **controller-specific annotations**: untyped strings that Kubernetes doesn't validate, and that differ per controller:

  ```yaml
  # ingress-nginx
  nginx.ingress.kubernetes.io/rewrite-target: /
  # Traefik
  traefik.ingress.kubernetes.io/router.middlewares: default-strip@kubernetescrd
  ```

  Switching controllers meant rewriting them all.
- **One Ingress object mixes several people's decisions**: ports, TLS certificates, and app routing all in one place.

---

## 3. How Gateway API works

The single Ingress object is split into layers, each owned by a different role:

```
GatewayClass   "which controller runs this?"         ← cluster admin / provider
     │            (traefik, envoy, istio, cilium, ...)
     ▼
Gateway        "the entrance: ports, hostnames, TLS"  ← platform team
     │            listens on a port, decides which namespaces may attach
     ▼
HTTPRoute      "my app's routing rules"               ← app team
     │            hostnames, path/header matching, filters, weights
     ▼
Service → Pods (unchanged)
```

Other route types exist for other protocols: `GRPCRoute`, `TLSRoute`, and (experimental) `TCPRoute`/`UDPRoute`.

**Shopping-centre analogy** (continuing from [service-explanation.md](service-explanation.md#10-services-explained-for-a-primary-2-child)): with **Ingress**, there was one note on the receptionist's desk. With **Gateway API**, the building owner picks the receptionist company (GatewayClass), the centre manager opens the entrance and decides which shops may use it (Gateway), and each shop writes its own directions (HTTPRoute).

---

## 4. Ingress vs Gateway API

| | Ingress | Gateway API |
|---|---|---|
| Objects | `Ingress` + controller | `GatewayClass` → `Gateway` → `*Route` |
| Who owns what | All mixed in one object | Admin / platform / app team separated |
| Protocols | HTTP(S) only | HTTP, gRPC, TLS, TCP/UDP (experimental) |
| Rewrites, redirects, headers | Controller-specific annotations | Built-in, typed **filters** |
| Traffic splitting (canary) | Annotations, if supported | Built-in `weight` on backends |
| Header / query / method matching | Mostly no | Yes |
| Duplicate rules | Controller picks one, often unpredictably | Defined precedence order ([section 8](#8-route-precedence-why-every-app-gets-a-hostname)) |
| Cross-namespace | No | Yes, controlled by `allowedRoutes` and `ReferenceGrant` |
| Debugging | Little status info | Each route reports `Accepted` / `ResolvedRefs` conditions |
| Portability | Low (annotations) | High (same YAML across controllers) |
| Status | Frozen | Actively developed |

---

## 5. Enabling Gateway API in k3s Traefik

What the cluster had out of the box:

- ✅ Gateway API CRDs installed (k3s v1.35 ships them):
  ```bash
  kubectl get crd | grep gateway
  # gatewayclasses, gateways, httproutes, grpcroutes, referencegrants, backendtlspolicies
  ```
- ✅ Traefik v3.6, which supports Gateway API.
- ❌ Gateway provider **not enabled**: Traefik's args only had `--providers.kubernetescrd` and `--providers.kubernetesingress`, and there was no GatewayClass.

### How k3s manages Traefik

k3s installs Traefik as a **Helm chart**, managed by its built-in **helm-controller**. You don't redeploy Traefik yourself. You create a `HelmChartConfig` with the same name and namespace as the chart (`traefik` / `kube-system`), and the controller:

1. merges your `valuesContent` into the chart's values,
2. runs a Job (`helm-install-traefik`) that does `helm upgrade`,
3. which rolls out Traefik with the new settings.

[deploy/traefik-config.yaml](deploy/traefik-config.yaml):

```yaml
apiVersion: helm.cattle.io/v1
kind: HelmChartConfig
metadata:
  name: traefik
  namespace: kube-system
spec:
  valuesContent: |-
    providers:
      kubernetesGateway:
        enabled: true
```

### Steps

```bash
# 1. Apply
kubectl apply -f deploy/traefik-config.yaml

# 2. Watch the helm upgrade job (wait for 1/1)
kubectl -n kube-system get jobs -w | grep traefik
kubectl -n kube-system logs -f job/helm-install-traefik     # optional

# 3. Wait for the new Traefik pod
kubectl -n kube-system rollout status deploy/traefik

# 4. Confirm the provider flag
kubectl -n kube-system get deploy traefik \
  -o jsonpath='{.spec.template.spec.containers[0].args}' | tr ',' '\n' | grep -i gateway
# "--providers.kubernetesgateway"

# 5. Confirm the GatewayClass is accepted
kubectl get gatewayclass
# NAME      CONTROLLER                      ACCEPTED
# traefik   traefik.io/gateway-controller   True
```

### Side effect: a default Gateway

The Traefik chart also creates its own Gateway:

```bash
kubectl get gateway -A
# default       web               traefik   ...   True
# kube-system   traefik-gateway   traefik   ...   True
```

It's harmless, but it's a second entrance. To keep `web` as the only Gateway, disable the chart's one:

```yaml
  valuesContent: |-
    providers:
      kubernetesGateway:
        enabled: true
    gateway:
      enabled: false
```

### Persistence

The `HelmChartConfig` is stored in the cluster, so it survives `k3d cluster stop/start`. If you **recreate** the cluster, re-apply it, or have k3s auto-deploy it at creation:

```bash
k3d cluster create ... \
  -v "$PWD/deploy/traefik-config.yaml:/var/lib/rancher/k3s/server/manifests/traefik-config.yaml@server:0"
```

---

## 6. Migrating python-app

The Ingress being replaced:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: python-app
spec:
  rules:
  - http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: python-app
            port:
              number: 8080
```

### The Gateway (platform layer, once per cluster/environment)

[deploy/gateway.yaml](deploy/gateway.yaml):

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: web
spec:
  gatewayClassName: traefik
  listeners:
  - name: http
    protocol: HTTP
    port: 8000          # Traefik's internal "web" entrypoint, NOT 80
    allowedRoutes:
      namespaces:
        from: Selector
        selector:
          matchLabels:
            gateway-access: "true"
```

> **Port 8000, not 80.** Traefik's Service listens on 80 but forwards to the pod's `web` entrypoint on **8000** (`--entryPoints.web.address=:8000/tcp`). A Traefik Gateway listener must use the entrypoint port, or it never becomes ready.

### The HTTPRoute (app layer, replaces the Ingress)

[deploy/httproute.yaml](deploy/httproute.yaml):

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: python-app
spec:
  parentRefs:
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: web
      namespace: default
  hostnames:
    - python.localhost
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
      - name: python-app
        port: 8080
```

### Migration order (no downtime)

1. Enable the Gateway provider ([section 5](#5-enabling-gateway-api-in-k3s-traefik)).
2. Apply the Gateway and HTTPRoute **while the Ingress still exists**. Traefik serves both side by side.
3. Check the route is accepted and `curl` works through it.
4. Delete the Ingress **from the cluster and from the repo**:
   ```bash
   kubectl delete ingress python-app
   rm deploy/ingress.yaml   # otherwise `kubectl apply -f deploy/` brings it back
   ```

---

## 7. Adding another app

A new app = its own **Deployment + Service + HTTPRoute** pointing at `web`. **The Gateway is not touched.** That's the role split in practice: platform owns the entrance, each app team brings a route.

Example, [deploy/whoami.yaml](deploy/whoami.yaml) (`traefik/whoami` is a tiny demo server on port 80):

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: whoami
spec:
  replicas: 1
  selector:
    matchLabels:
      app: whoami
  template:
    metadata:
      labels:
        app: whoami
    spec:
      containers:
        - name: whoami
          image: traefik/whoami:v1.10
          ports:
            - containerPort: 80
---
apiVersion: v1
kind: Service
metadata:
  name: whoami
spec:
  selector:
    app: whoami
  ports:
    - port: 80
      targetPort: 80
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: whoami
spec:
  parentRefs:
    - name: web                 # same Gateway as python-app
  hostnames:
    - whoami.localhost
  rules:
    - backendRefs:
        - name: whoami
          port: 80
```

```bash
kubectl apply -f deploy/whoami.yaml
kubectl get gateway web -o jsonpath='{.status.listeners[0].attachedRoutes}'   # 2
curl whoami.localhost:8081/
```

`*.localhost` needs no hosts-file entry: browsers and modern curl (7.88 here) resolve it to `127.0.0.1` and still send the hostname, which the Gateway uses to route. With older curl: `curl -H "Host: whoami.localhost" localhost:8081/`.

---

## 8. Route precedence: why every app gets a hostname

Unlike Traefik with duplicate Ingresses (which silently picked one), Gateway API defines how overlapping routes are resolved:

1. An exact hostname beats a wildcard, which beats no hostname.
2. Then the longest path match wins.
3. Then method, header and query matches.
4. Then the **oldest** route (by creation time), then alphabetical.

So a route with **no hostname** and path `/` catches every request that no more specific route claims. It works, but gets confusing as apps are added. Giving every HTTPRoute its own `hostnames:` keeps ownership clear. That's why `python.localhost` was added to python-app's route.

---

## 9. Routes from other namespaces

The Gateway's `allowedRoutes` decides which namespaces may attach routes:

| Setting | Who can attach |
|---|---|
| `from: Same` | Only routes in the Gateway's namespace (`default`) |
| `from: All` | Any namespace |
| `from: Selector` + `matchLabels` | Only namespaces carrying the label (**current setup**) |

With `Selector`, each app namespace must be labelled:

```bash
kubectl label namespace <ns> gateway-access=true
```

A route in a different namespace than the Gateway must name the Gateway's namespace:

```yaml
  parentRefs:
    - name: web
      namespace: default
```

In production, apps usually each get their own namespace, and the platform team controls which ones may use the shared Gateway. Ingress had no equivalent control.

---

## 10. Features Ingress needed annotations for

All standard, so they work the same on any Gateway API controller.

**Canary / traffic split:**

```yaml
  rules:
  - backendRefs:
    - name: python-app
      port: 8080
      weight: 90
    - name: python-app-v2
      port: 8080
      weight: 10
```

**Strip a path prefix** (`/python/api/v1/details` → `/api/v1/details`):

```yaml
  rules:
  - matches:
    - path: { type: PathPrefix, value: /python }
    filters:
    - type: URLRewrite
      urlRewrite:
        path: { type: ReplacePrefixMatch, replacePrefixMatch: / }
    backendRefs:
    - name: python-app
      port: 8080
```

Other built-in filters: `RequestRedirect` (e.g. HTTP → HTTPS), `RequestHeaderModifier`, `ResponseHeaderModifier`, `RequestMirror`.

---

## 11. Troubleshooting

### Incident: `404 page not found` on both apps after adding hostnames

**Symptom:**

```
➜ curl whoami.localhost:8081/
404 page not found
➜ curl python.localhost:8081/
404 page not found
```

**Diagnosis:**

1. Pods and Services were fine, and `curl -H 'Host: ...'` also gave 404, so this wasn't a DNS or hostname problem.
2. Route status showed both routes **rejected**:
   ```bash
   kubectl get httproute -o yaml | grep -A12 'status:'
   #   reason: NotAllowedByListeners
   #   status: "False"
   #   type: Accepted
   ```
3. The Gateway had nothing attached:
   ```bash
   kubectl get gateway web -o yaml
   #   allowedRoutes: from: Selector, matchLabels: gateway-access: "true"
   #   listeners: attachedRoutes: 0
   ```
4. The namespace lacked the label:
   ```bash
   kubectl get ns default --show-labels
   # default   Active   kubernetes.io/metadata.name=default
   ```

**Root cause:** the Gateway had been switched from `from: Same` to the safer `from: Selector`, but the `default` namespace was never labelled. The Gateway refused both routes, so Traefik had no routes and returned its own plain-text 404.

**Fix:**

```bash
kubectl label namespace default gateway-access=true
kubectl get gateway web -o jsonpath='{.status.listeners[0].attachedRoutes}'   # 2
```

**Lesson:** this label lives only in the cluster, not in git. Recreating the cluster loses it. To make it reproducible, commit a namespace manifest:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: default
  labels:
    gateway-access: "true"
```

### Incident: custom hostname works in curl but not in the browser (WSL)

**Symptom:** the HTTPRoute hostname was changed to `python-app.test.com` and an entry was added to `/etc/hosts`. curl worked, Edge didn't:

```
➜ curl python-app.test.com:8081/api/v1/details
{"time": "...", "hostname": "python-app-5d55f498dc-blplx"}
```

Edge: `Hmmm... can't reach this page`, `DNS_PROBE_FINISHED_NXDOMAIN`.

**Diagnosis:** curl and Edge read **different hosts files**. curl runs in WSL (Linux), Edge runs on Windows:

```
curl (WSL)   → /etc/hosts                                → has python-app.test.com ✓
Edge (Win)   → C:\Windows\System32\drivers\etc\hosts     → missing ✗ → public DNS → NXDOMAIN
```

```bash
grep test.com /etc/hosts                                   # 127.0.0.1 python-app.test.com
grep test.com /mnt/c/Windows/System32/drivers/etc/hosts    # nothing
```

`NXDOMAIN` means "this name doesn't exist" from public DNS, so the request never reached the cluster. The Gateway and route were fine, which is why curl worked.

**Fix:**

1. Open **Notepad as Administrator** and open `C:\Windows\System32\drivers\etc\hosts` (set the file-type filter to *All Files*).
2. Add and save:
   ```
   127.0.0.1 python-app.test.com
   ```
3. Clear Edge's DNS cache at `edge://net-internals/#dns` → **Clear host cache**, or run `ipconfig /flushdns` in an admin PowerShell.
4. Browse to `http://python-app.test.com:8081/api/v1/details`. Type `http://` so Edge doesn't try https.

**Gotcha: WSL overwrites `/etc/hosts`.** Its header says `# This file was automatically generated by WSL`. WSL rebuilds it **from the Windows hosts file** on every start, so entries added only in WSL vanish after `wsl --shutdown` or a reboot. Maintain the **Windows** hosts file; WSL copies it.

**Lesson: use reserved hostnames.** `test.com` is a real, registered domain. If the hosts entry is ever missing, requests go to someone else's server. Prefer:

| Name | Hosts entry needed? |
|---|---|
| `python-app.localhost` | No. `*.localhost` resolves to 127.0.0.1 automatically in browsers and modern curl, on Windows and WSL. |
| `python-app.test` | Yes (Windows hosts file). `.test` is reserved by RFC 2606 and never exists publicly. |

### Not a bug: `python.localhost:8081/` returns 404

Flask only serves `/api/v1/details` and `/api/v1/healthz`, so it has no page at `/`. Tell the two 404s apart by the body:

| Response body | Who sent it | Meaning |
|---|---|---|
| `404 page not found` (plain text) | Traefik | No route matched: routing problem |
| HTML `<title>404 Not Found</title>` | Flask | Routing worked, the app has no such path |

### Debug order

Work from the controller down to the app:

| Layer | Check | Command |
|---|---|---|
| Provider on? | `--providers.kubernetesgateway` in args | `kubectl -n kube-system get deploy traefik -o jsonpath='{.spec.template.spec.containers[0].args}'` |
| GatewayClass | `ACCEPTED True` | `kubectl get gatewayclass` |
| Gateway | `PROGRAMMED True`, `attachedRoutes` > 0 | `kubectl get gateway -A` / `kubectl describe gateway web` |
| Route attached? | `Accepted True` | `kubectl describe httproute <name>` → Status → Parents → Conditions |
| Backend resolved? | `ResolvedRefs True` | same as above |
| Service has pods? | Endpoints not empty | `kubectl get endpoints <svc>` |
| App works in-cluster? | Real response | `kubectl run tmp --rm -i --restart=Never --image=curlimages/curl -- curl -s <svc>:<port>/<path>` |
| End to end | Real response | `curl <app>.localhost:8081/<path>` |

### Symptom → likely cause

| Symptom | Likely cause |
|---|---|
| No `traefik` GatewayClass | Provider not enabled; check `HelmChartConfig` name/namespace and `helm-install-traefik` job logs |
| `helm-install-traefik` job fails | YAML indentation in `valuesContent` |
| Gateway not `Programmed` | Listener port isn't a Traefik entrypoint port (use **8000**, not 80) |
| Route `Accepted: False`, `NotAllowedByListeners` | Namespace not allowed by `allowedRoutes` (missing label, or `Same` with route in another namespace) |
| Route `Accepted: False`, `NoMatchingParent` | `parentRefs` name/namespace wrong |
| Route `ResolvedRefs: False`, `BackendNotFound` | Wrong Service name or port in `backendRefs` |
| Plain `404 page not found` | No accepted route matches the host/path |
| Flask HTML 404 | Routing fine; path doesn't exist in the app |
| `ERR_CONNECTION_REFUSED` | Wrong host port; use `:8081` |
| `DNS_PROBE_FINISHED_NXDOMAIN` in browser, curl works (WSL) | Hostname only in WSL `/etc/hosts`; add it to the Windows hosts file |
| Custom hostname stops working after reboot (WSL) | WSL regenerated `/etc/hosts` from the Windows hosts file |
| Deleted Ingress comes back | `ingress.yaml` still in `deploy/` and re-applied |
