# Deploying Argo CD with Helm behind the Gateway

How to install Argo CD into the cluster with the official `argo/argo-cd` Helm chart, expose its UI through the existing Traefik Gateway (`web` in `default`), and fix the config mistakes that came up along the way.

This builds on the Gateway API setup in [ingress-to-gateway.md](ingress-to-gateway.md) and the Helm basics in [k8s-manifest-to-helm.md](k8s-manifest-to-helm.md).

## Contents

1. [What we're building](#1-what-were-building)
2. [What's in the cluster](#2-whats-in-the-cluster)
3. [The Gateway](#3-the-gateway)
4. [The Argo CD values file](#4-the-argo-cd-values-file)
5. [Deploying](#5-deploying)
6. [Verifying](#6-verifying)
7. [Logging in](#7-logging-in)
8. [Troubleshooting: mistakes made along the way](#8-troubleshooting-mistakes-made-along-the-way)
9. [Cheat sheet](#9-cheat-sheet)

---

## 1. What we're building

```
browser ──► argocd.test.com:8081 ──► Traefik Gateway "web" (listener "http", port 8000)
                                          │
                                          ▼
                                 HTTPRoute (namespace argocd)
                                          │
                                          ▼
                                 Service argocd-server ──► argocd-server pod
```

- The **Gateway** (`web`) already exists and serves `python-app`. Argo CD reuses it instead of getting a new one.
- The **argo-cd Helm chart** creates the HTTPRoute for us when `server.httproute.enabled: true` is set, so we don't write the route by hand.
- TLS isn't used yet. The Gateway listens on plain HTTP, so Argo CD must also serve plain HTTP (see [`server.insecure`](#serverinsecure-true)).

---

## 2. What's in the cluster

Before configuring anything that points at a Gateway, look at what actually exists. Example config from chart docs (`example-gateway`, `gateway-system`) won't match your cluster.

```bash
kubectl get gateway -A
kubectl get gatewayclass
kubectl get ns --show-labels
```

What this cluster has:

```
NAMESPACE     NAME              CLASS     ADDRESS      PROGRAMMED
default       web               traefik   172.20.0.2   True     ← ours, from deploy/gateway.yaml
kube-system   traefik-gateway   traefik   172.20.0.2   True     ← Traefik's built-in one

NAME      CONTROLLER                      ACCEPTED
traefik   traefik.io/gateway-controller   True

default   gateway-access=true   ← the only namespace allowed to attach routes to "web"
```

The three facts that matter for Argo CD:

| Fact | Used in |
|---|---|
| Gateway name is `web`, namespace `default` | `parentRefs[].name` / `parentRefs[].namespace` |
| Its only listener is called `http` | `parentRefs[].sectionName` |
| It only accepts routes from namespaces labelled `gateway-access=true` | label on the `argocd` namespace |

---

## 3. The Gateway

[deploy/gateway.yaml](deploy/gateway.yaml):

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: web
spec:
  gatewayClassName: traefik
  listeners:
  - name: http            # ← this is the sectionName routes refer to
    protocol: HTTP
    port: 8000            # Traefik's "web" entrypoint inside the cluster
    allowedRoutes:
      namespaces:
        from: Selector
        selector:
          matchLabels:
            gateway-access: "true"
```

Only a Gateway-API spec goes here: `gatewayClassName`, `listeners`, `addresses`, `infrastructure`. Application settings, such as Argo CD's `server.insecure`, belong in that app's own values file. See [mistake 2](#mistake-2-argo-cd-settings-inside-the-gateway).

`allowedRoutes` is a security boundary. A route in any namespace without the `gateway-access: "true"` label is **rejected**, even if it names this Gateway correctly.

---

## 4. The Argo CD values file

[argocd/values-argo.yaml](argocd/values-argo.yaml):

```yaml
redis-ha:
  enabled: false

controller:
  replicas: 1

repoServer:
  replicas: 1

applicationSet:
  replicas: 1

global:
  domain: argocd.test.com

configs:
  params:
    server.insecure: true

server:
  replicas: 1

  httproute:
    enabled: true
    hostnames:
      - argocd.test.com
    parentRefs:
      - name: web
        namespace: default
        sectionName: http

  grpcroute:
    enabled: false
```

### Key by key

| Key | Why |
|---|---|
| `redis-ha.enabled: false` | Single Redis instead of a 3-node HA set. Plenty for a local cluster. |
| `*.replicas: 1` | One pod per component. Fine for learning; raise for production. |
| `global.domain` | The URL Argo CD uses for itself, in UI links and SSO redirects. It does **not** set the HTTPRoute's hostnames. See [mistake 7](#mistake-7-httproute-with-no-hostnames). |
| `configs.params.server.insecure: true` | See below. |
| `server.httproute.enabled` | Tells the chart to render an HTTPRoute for the UI and API. |
| `server.httproute.hostnames` | Which `Host` headers the route matches. Leave it empty and the route matches **every** hostname. Keep it in sync with `global.domain`. |
| `server.httproute.parentRefs` | Which Gateway and listener to attach to. Must match section 2 exactly. |
| `server.grpcroute.enabled: false` | There's no gRPC listener. The CLI can go through the HTTP route with `--grpc-web`. |

### `server.insecure: true`

By default `argocd-server` serves **HTTPS** with a self-signed certificate and redirects HTTP to HTTPS. Our Gateway talks to it over plain HTTP, so without this setting you get:

- an endless redirect loop in the browser (`ERR_TOO_MANY_REDIRECTS`), or
- `502 Bad Gateway`, because Traefik can't speak HTTP to an HTTPS backend.

`server.insecure: true` makes the server speak plain HTTP. TLS, when you add it, is handled at the Gateway, which is the usual pattern.

The key is `configs` (plural). `config` is silently ignored.

### One key per component

Every top-level key (`server`, `controller`, `global`, `configs`, ...) must appear **once**. Everything for that component goes under that single key. See [mistake 1](#mistake-1-two-server-blocks).

---

## 5. Deploying

```bash
# 1. Namespace, plus the label the Gateway's allowedRoutes requires
kubectl create namespace argocd
kubectl label namespace argocd gateway-access=true

# 2. Make sure the Gateway is the cleaned-up version
kubectl apply -f deploy/gateway.yaml

# 3. Add the chart repo (once)
helm repo add argo https://argoproj.github.io/argo-helm
helm repo update

# 4. Preview what will be created (optional but useful)
helm template argocd argo/argo-cd -n argocd \
  -f argocd/values-argo.yaml | grep -A20 "kind: HTTPRoute"

# 5. Install / upgrade
helm upgrade --install argocd argo/argo-cd -n argocd \
  -f argocd/values-argo.yaml
```

`upgrade --install` installs if the release is missing and upgrades if it exists, so you can rerun the same command after every values change.

### Hostname

Your machine needs to resolve `argocd.test.com` to the cluster. Add it to the hosts file next to `python-app.test.com`. On WSL, browsers use the **Windows** hosts file at `C:\Windows\System32\drivers\etc\hosts`.

```
127.0.0.1   python-app.test.com argocd.test.com
```

---

## 6. Verifying

```bash
# Pods should all be Running
kubectl get pods -n argocd

# The chart-generated route; HOSTNAMES should show argocd.test.com, not be empty
kubectl get httproute -n argocd
kubectl describe httproute -n argocd
```

In the `describe` output, under `Status → Parents`, look for:

```
Conditions:
  Type:    Accepted       Status: True
  Type:    ResolvedRefs   Status: True
```

| Condition false | Likely reason |
|---|---|
| `Accepted: False`, reason `NotAllowedByListeners` | `argocd` namespace is missing the `gateway-access=true` label |
| `Accepted: False`, reason `NoMatchingParent` | wrong `name`, `namespace` or `sectionName` in `parentRefs` |
| `ResolvedRefs: False` | backend Service/port doesn't exist (pods not up yet?) |

Then test from the terminal and the browser:

```bash
curl -I http://argocd.test.com:8081
```

```
http://argocd.test.com:8081
```

Use the same host port you use for python-app (`8081`). It maps to the Gateway's port `8000` inside the cluster.

---

## 7. Logging in

The username is `admin`. The initial password is generated and stored in a Secret:

```bash
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d; echo
```

With the CLI, go through the HTTP route using gRPC-web, since there's no gRPC listener:

```bash
argocd login argocd.test.com:8081 --grpc-web --plaintext --username admin
```

After you change the password, delete the initial secret:

```bash
argocd account update-password
kubectl -n argocd delete secret argocd-initial-admin-secret
```

---

## 8. Troubleshooting: mistakes made along the way

### Mistake 1: two `server:` blocks

```yaml
server:
  replicas: 1

# ...

server:              # ← "Map keys must be unique"
  httproute:
    ...
```

**What happens:** YAML maps can't repeat a key. Strict parsers fail. Lenient ones (including Helm's) keep only the **last** block, so `replicas: 1` silently disappears.

**Fix:** merge everything under one `server:` key, as shown in [section 4](#4-the-argo-cd-values-file).

You *could* split values across files instead. Helm deep-merges multiple `-f` files in order, later files winning:

```bash
helm upgrade --install argocd argo/argo-cd -n argocd \
  -f values-argo.yaml -f values-routes.yaml
```

For a file this size, one file is simpler.

The VS Code warning `"httproute": Unknown word` is just the spell checker. Ignore it.

### Mistake 2: Argo CD settings inside the Gateway

```yaml
# deploy/gateway.yaml — WRONG
spec:
  listeners: [...]
  config:
    params:
      server.insecure: true
```

**What happens:** `config` isn't a Gateway field. The API server drops it, sometimes with a warning, so it has no effect anywhere.

**Fix:** remove it from the Gateway. Put it in the Argo CD values file as `configs.params.server.insecure: true`.

### Mistake 3: copying placeholder names from the chart docs

```yaml
parentRefs:
  - name: example-gateway       # doesn't exist
    namespace: gateway-system   # doesn't exist
    sectionName: https          # no such listener
```

```
$ kubectl get gateway example-gateway -n gateway-system
Error from server (NotFound): namespaces "gateway-system" not found
```

**Fix:** use what `kubectl get gateway -A` shows: `web` / `default` / `http`.

### Mistake 4: GRPCRoute with no gRPC listener

The chart's example enables `server.grpcroute` with `sectionName: grpc`. Our Gateway has no such listener, so the route would never be accepted.

**Fix:** `grpcroute.enabled: false`, and use `argocd ... --grpc-web` for the CLI.

### Mistake 5 (avoided): forgetting the namespace label

The Gateway only accepts routes from namespaces labelled `gateway-access=true`. Only `default` had it. Without `kubectl label namespace argocd gateway-access=true`, the HTTPRoute is created but **rejected**, and the UI returns 404.

### Mistake 6: passing a URL to `argocd login`

```
$ argocd login http://argocd.test.com:8081 --username admin --insecure --grpc-web
{"level":"fatal","msg":"error dial proxy: dial tcp: address http://argocd.test.com:8081: too many colons in address"}

$ argocd login http://argocd.test.com --username admin --insecure --grpc-web
{"level":"fatal","msg":"error dial proxy: dial tcp: lookup tcp///argocd.test.com: unknown port"}
```

**What happens:** `argocd login` takes a bare `host:port` and adds the scheme itself. With `http://` in front, it reads `http` as the host and the rest as a mangled port. Hence "too many colons" and "unknown port".

The flags were also wrong. `--insecure` still uses HTTPS and only skips the certificate check. Our Gateway listener is plain HTTP, so it needs `--plaintext`.

**Fix:**

```bash
argocd login argocd.test.com:8081 --username admin --grpc-web --plaintext
```

| Flag | Meaning | Needed here? |
|---|---|---|
| `--grpc-web` | Send the CLI's gRPC calls over normal HTTP, because there's no gRPC listener | Yes |
| `--plaintext` | Use HTTP, not HTTPS | Yes, the Gateway listener is HTTP only |
| `--insecure` | Use HTTPS but don't verify the certificate | No. Use it once TLS with a self-signed cert is added. |

Check the session with `argocd account get-user-info`.

### Mistake 7: HTTPRoute with no hostnames

```
$ kubectl -n argocd get httproute
NAME            HOSTNAMES   AGE
argocd-server               8h
```

The HOSTNAMES column is empty, but `http://argocd.test.com:8081` still works. That's because **any** hostname works:

```
$ curl -H "Host: whatever.test.com" http://localhost:8081/
200  <title>Argo CD</title>
```

**What happens:** an HTTPRoute with no `hostnames` matches every `Host` header, so Argo CD became a catch-all on the Gateway. `python-app.test.com` still reached python-app only because, in the Gateway API, a route with an exact hostname takes precedence over one with no hostname. Every other name fell through to Argo CD.

The cause was assuming `global.domain` fills in the route's hostnames. It doesn't. The chart takes them from a separate list that defaults to empty:

```yaml
# helm show values argo/argo-cd   (chart argo-cd-10.10.3)
server:
  httproute:
    hostnames: []
      # - argocd.example.com
```

**Fix:** set `server.httproute.hostnames` explicitly, as in [section 4](#4-the-argo-cd-values-file), then upgrade and check:

```bash
helm upgrade --install argocd argo/argo-cd -n argocd \
  -f charts/python-app/argocd/values-argo.yaml

kubectl -n argocd get httproute     # HOSTNAMES: argocd.test.com
curl -s -o /dev/null -w "%{http_code}\n" -H "Host: whatever.test.com" http://localhost:8081/   # 404
```

**Lesson:** don't assume what a chart does with a value. Check with `helm show values <chart>`, or render it with `helm template` and read the output.

---

## 9. Cheat sheet

```bash
# Inspect
kubectl get gateway -A
kubectl get ns --show-labels
kubectl get httproute -n argocd
kubectl describe httproute -n argocd

# Install / upgrade
helm upgrade --install argocd argo/argo-cd -n argocd \
  -f charts/python-app/argocd/values-argo.yaml

# See the values Helm actually used
helm get values argocd -n argocd

# Render without installing
helm template argocd argo/argo-cd -n argocd \
  -f charts/python-app/argocd/values-argo.yaml

# Admin password
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d; echo

# Remove everything
helm uninstall argocd -n argocd
kubectl delete namespace argocd
```
