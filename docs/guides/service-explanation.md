# Kubernetes Services & Ingress on k3d — Explained

Notes from getting `python-app` reachable on a local k3d cluster: what went wrong, why, and how Services/Ingress actually work.

## Contents

1. [Current setup](#1-current-setup)
2. [Troubleshooting: why the app wasn't reachable](#2-troubleshooting-why-the-app-wasnt-reachable)
3. [Two Ingresses with the same rule](#3-two-ingresses-with-the-same-rule)
4. [Fix options: paths vs hostnames](#4-fix-options-paths-vs-hostnames)
5. [Ingress Controller vs Ingress resource](#5-ingress-controller-vs-ingress-resource)
6. [Why the k3d guide deploys nginx](#6-why-the-k3d-guide-deploys-nginx)
7. [Why localhost:8080 doesn't work](#7-why-localhost8080-doesnt-work)
8. [Can it be simplified? Alternatives to Ingress](#8-can-it-be-simplified-alternatives-to-ingress)
9. [Scaling to 100 apps: how production does it](#9-scaling-to-100-apps-how-production-does-it)
10. [Services explained for a Primary 2 child](#10-services-explained-for-a-primary-2-child)
11. [Troubleshooting cheat sheet](#11-troubleshooting-cheat-sheet)

---

## 1. Current setup

Cluster created with the k3d guide:

```bash
k3d cluster create --api-port 6550 -p "8081:80@loadbalancer" --agents 2
```

Manifests in [deploy/](deploy/):

| File | Kind | Purpose |
|---|---|---|
| [deployment.yaml](deploy/deployment.yaml) | Deployment | Runs `kwameds/python-app:v1`, Flask on container port **5000** |
| [service.yaml](deploy/service.yaml) | Service (ClusterIP) | Stable name `python-app:8080` → pod port `5000` |
| [ingress.yaml](deploy/ingress.yaml) | Ingress | Traefik rule: `/` → `python-app:8080` |

App routes (from [src/app.py](src/app.py)): `/api/v1/details`, `/api/v1/healthz`.

**Access it:**

```bash
curl localhost:8081/api/v1/details
# {"time": "...", "hostname": "python-app-5d55f498dc-blplx"}
```

Full request path:

```
Browser → localhost:8081 → k3d serverlb:80 → Traefik:80
        → Ingress rule "/" → Service python-app:8080 → Pod:5000 (Flask)
```

---

## 2. Troubleshooting: why the app wasn't reachable

**Symptom:** `http://localhost/api/v1/details` in the browser → `ERR_CONNECTION_REFUSED`.
Meanwhile `http://localhost:8081` showed "Welcome to nginx!".

**Step 1 — Prove the app itself works (from inside the cluster):**

```bash
kubectl run tmp --rm -i --restart=Never --image=curlimages/curl -- \
  curl -s python-app:8080/api/v1/details
# {"time": "...", "hostname": "python-app-..."}   ← app, Service, Pod all fine
```

So the problem was in getting traffic *into* the cluster, not the app.

**Step 2 — Problem #1: wrong host port.**
No port in the URL means the browser uses **port 80**. Only **8081** was mapped:

```bash
docker ps --format '{{.Names}} {{.Ports}}' | grep serverlb
# k3d-k3s-default-serverlb 0.0.0.0:8081->80/tcp, 0.0.0.0:6550->6443/tcp
```

Nothing listens on host port 80 → connection refused. Correct URL: `http://localhost:8081/api/v1/details`.

**Step 3 — Problem #2: on 8081, nginx answered instead of the app.**

```bash
curl localhost:8081/api/v1/details
# 404 Not Found ... nginx/1.31.7
```

```bash
kubectl get ingress
# nginx        traefik   *   ...   80
# python-app   traefik   *   ...   80
```

Two Ingresses with identical rules (any host, path `/`). Traefik picked `nginx`, which has no `/api/v1/details` page → 404. See [section 3](#3-two-ingresses-with-the-same-rule).

**Fix applied:** deleted the nginx demo resources so only the python-app rule owns `/`:

```bash
kubectl delete ingress nginx
kubectl delete service nginx
kubectl delete deployment nginx
```

---

## 3. Two Ingresses with the same rule

Traefik turns every Ingress into a router rule ("if path starts with `/` → send to X"). For each request it picks the **most specific** matching rule — by default, longer/more detailed rules win.

| Ingress | host | path | backend |
|---|---|---|---|
| `nginx` | `*` | `/` | `nginx:80` |
| `python-app` | `*` | `/` | `python-app:8080` |

Identical rules = a **tie**. Traefik doesn't error or warn; it **silently picks one**, and you shouldn't rely on which.

Ways to break the tie:

- **Different paths** — `/api` → python-app, `/` → nginx (`/api` is more specific, so it wins for `/api/...`).
- **Different hostnames** — `python.localhost` → python-app, `nginx.localhost` → nginx.
- **Only one Ingress owns `/`** — delete the one you don't need (what we did).

---

## 4. Fix options: paths vs hostnames

### Option A — Different paths

Only the python-app Ingress changes (`/` → `/api`); nginx keeps `/`.

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: python-app
spec:
  rules:
  - http:
      paths:
      - path: /api          # was: /
        pathType: Prefix
        backend:
          service:
            name: python-app
            port:
              number: 8080
```

| URL | Goes to |
|---|---|
| `http://localhost:8081/api/v1/details` | python-app |
| `http://localhost:8081/api/v1/healthz` | python-app |
| `http://localhost:8081/` | nginx |

Works because Flask's routes already start with `/api/...` — Traefik passes the full path through unchanged.

### Option B — Different hostnames

Add a `host:` to each Ingress; both paths can stay `/`.

```yaml
# nginx Ingress
spec:
  rules:
  - host: nginx.localhost
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: nginx
            port:
              number: 80
```

```yaml
# python-app Ingress
spec:
  rules:
  - host: python.localhost
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: python-app
            port:
              number: 8080
```

> YAML gotcha: `host:` goes on the `-` line and `http:` moves under it.

| URL | Goes to |
|---|---|
| `http://python.localhost:8081/api/v1/details` | python-app |
| `http://nginx.localhost:8081/` | nginx |
| `http://localhost:8081/` | **404 from Traefik** (no rule matches bare `localhost`) |

No hosts-file edit needed: Edge, Chrome and modern curl resolve any `*.localhost` to `127.0.0.1`, while still sending `Host: python.localhost` so Traefik can route on it. With older curl:

```bash
curl -H "Host: python.localhost" localhost:8081/api/v1/details
```

### Option C — One Ingress, multiple paths

A single Ingress resource can hold several rules:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: apps
spec:
  rules:
  - http:
      paths:
      - path: /api
        pathType: Prefix
        backend:
          service:
            name: python-app
            port:
              number: 8080
      - path: /
        pathType: Prefix
        backend:
          service:
            name: nginx
            port:
              number: 80
```

Valid, but most teams keep **one Ingress per app** so each app's routing lives (and is deployed/deleted) with that app.

---

## 5. Ingress Controller vs Ingress resource

The root of the confusion: **"ingress" means two different things.**

| | Ingress **Controller** | Ingress **resource** |
|---|---|---|
| What it is | A real program running in a pod | A YAML rule (`kind: Ingress`) |
| In this cluster | **Traefik** (`kube-system`), installed automatically by k3s | [deploy/ingress.yaml](deploy/ingress.yaml) |
| How many | One per cluster | Usually one per app |
| Job | Receives traffic and forwards it | Tells the controller *where* to forward |
| Analogy | Receptionist at the front desk | A note: "visitors for X go to room Y" |

The "cluster ingress" is **Traefik** plus the `-p "8081:80@loadbalancer"` mapping that connects your laptop to it.

Traefik only forwards to Services that some Ingress rule mentions. Without a rule naming `python-app`, Traefik doesn't know the app exists — that's why the app needs its own Ingress. It's a second **rule** for the same controller, not a second controller.

```
Traefik (controller, 1 per cluster)
   reads ──► Ingress "python-app"  → python-app:8080
   reads ──► Ingress "<other app>" → <other-app>:<port>
```

---

## 6. Why the k3d guide deploys nginx

In the k3d guide, nginx is a **demo app**, not an ingress controller. It plays the same role as python-app: something to send traffic to. It's used because:

- it's a public image — `kubectl create deployment nginx --image=nginx` works for everyone with no build;
- it serves a page at `/` immediately, so `curl localhost:8081/` proves the chain works:

```
localhost:8081 → serverlb → Traefik → Ingress rule → Service → Pod  ✓
```

Once that works, it has done its job — your own app replaces it.

**Two different nginx's:**

| Name | What it is | Used here? |
|---|---|---|
| `nginx` image (the guide's) | Plain web server — a demo app | Only as a test app (now deleted) |
| `ingress-nginx` | An ingress **controller** (alternative to Traefik) | No — Traefik is the controller |

---

## 7. Why localhost:8080 doesn't work

The Service port is 8080 and the Ingress points to 8080, yet `localhost:8080` fails and `localhost:8081` works.

Port 8080 exists only **inside** the cluster. The browser is **outside**, and the only door from outside is 8081.

```
YOUR LAPTOP                           INSIDE THE CLUSTER (private network)
───────────────                       ─────────────────────────────────────

localhost:8081 ──docker──► serverlb:80 ──► Traefik:80
                                              │  reads Ingress rule
                                              ▼
                                  Service python-app 10.43.239.170:8080
                                              │  targetPort
                                              ▼
                                  Pod 10.42.0.8:5000 (Flask)

localhost:8080 ──► ✗ nothing here
```

| Port | Lives where | Set by |
|---|---|---|
| **8081** | Your laptop | k3d `-p "8081:80@loadbalancer"` — the only mapped door |
| **80** | Traefik, inside the cluster | k3s default |
| **8080** | python-app Service, inside the cluster | `port:` in [service.yaml](deploy/service.yaml) |
| **5000** | Flask container | `targetPort:` / `app.run(port=5000)` |

The `port: 8080` in the Ingress is an instruction **to Traefik** ("forward to `python-app:8080`"). It doesn't open 8080 on your laptop.

Analogy: the building has one public entrance (8081). The python team sits in room 8080. The receptionist knows to walk you to room 8080 — but there's no outside door numbered 8080.

To reach the Service directly on `localhost:8080`, bypassing Traefik:

```bash
kubectl port-forward svc/python-app 8080:8080   # temporary, while the command runs
```

---

## 8. Can it be simplified? Alternatives to Ingress

Removing nginx was the main simplification: one app, one Service, one Ingress, one `/` rule.

Ways to reach the app **without** Ingress:

| Method | How | Good for | Downside |
|---|---|---|---|
| **port-forward** | `kubectl port-forward svc/python-app 8080:8080` | Quick debugging | Only while running; one per terminal; not how real users connect |
| **LoadBalancer Service** | `type: LoadBalancer` + k3d map `-p "8080:8080@loadbalancer"` | A few apps | One host port per app |
| **NodePort Service** | `type: NodePort` + k3d map to the node port | Rarely worth it | Odd ports (30000–32767) |
| **Ingress** (current) | One entry point, one rule per app | Anything beyond a quick test | One extra YAML per app |

**Recommendation: keep the Ingress.** It's free (k3s ships Traefik), it scales (one port for any number of apps), and it matches production. Use port-forward alongside it for quick checks.

**Drop the `:8081`** by mapping host port 80 directly (only if nothing else on your machine uses port 80):

```bash
k3d cluster edit k3s-default --port-add "80:80@loadbalancer"
# or when creating a new cluster:
k3d cluster create --api-port 6550 -p "80:80@loadbalancer" --agents 2
```

Then `http://localhost/api/v1/details` works.

---

## 9. Scaling to 100 apps: how production does it

Each app still gets its **own Service** — that never changes. What's shared vs per-app:

```
                    ONE per cluster (shared)
              ┌────────────────────────────────────┐
Internet ──►  │ Cloud Load Balancer (1 public IP)  │
              │            ▼                       │
              │ Ingress Controller (Traefik/nginx) │
              └────────────────────────────────────┘
                     │        │         │
                ONE per app (100 apps = 100 of each)
                     ▼        ▼         ▼
                 Ingress   Ingress   Ingress     ← routing rule
                 Service   Service   Service     ← stable address
                 Deploy    Deploy    Deploy      ← the pods
```

**Hostnames win in production.** Paths get messy at scale (every app needs a unique prefix, many apps expect to live at `/`):

```
orders.example.com    → orders service
payments.example.com  → payments service
grafana.example.com   → grafana service
```

One **wildcard DNS record** (`*.example.com → load balancer IP`) means a new app only needs a new Ingress with its hostname.

Paths are still used for a single public API made of several services (`api.example.com/orders`, `/payments`), often with the prefix stripped so each service serves at `/` (Traefik `StripPrefix` middleware, ingress-nginx `rewrite-target`).

| Concern | Typical solution |
|---|---|
| Organising 100 apps | **Namespaces** per team/app; each team owns its Ingress |
| Not hand-writing 300 YAMLs | **Helm** or **Kustomize** templates |
| HTTPS | **cert-manager** + Let's Encrypt, per hostname |
| Deploying | **GitOps** (Argo CD / Flux) |
| Newer alternative to Ingress | **Gateway API** (`Gateway` + `HTTPRoute`) |

Locally, practise the production pattern with `*.localhost` hostnames — it behaves like a free wildcard DNS.

---

## 10. Services explained for a Primary 2 child

### The ice cream shop 🍦

Imagine a big ice cream shop. Behind the counter are **helpers** who scoop ice cream. Those helpers are **Pods**.

Helpers come and go. One gets tired and goes home 😴, a new one arrives, maybe standing in a different spot. If you shout "Hey, Tom!", Tom might not be there anymore.

### The counter with a sign

So the shop has a counter with a sign: **"🍦 Ice Cream Here"**. That counter is the **Service**.

You don't need to know which helper is working. You go to the counter and say "One ice cream, please!" and whichever helper is free gives it to you.

- The **sign never moves**, even when helpers change.
- With **3 helpers**, the counter shares customers so nobody gets too busy — **load balancing**.

### How the counter knows its helpers

Every helper wears a **T-shirt with a sticker** saying **"ice-cream"** 👕. The counter says: "Anyone with an *ice-cream* sticker works for me!"

The sticker is the **label**; the counter's rule is the **selector**. A new helper with the right sticker gets customers straight away.

### Three kinds of counter

1. 🏠 **Inside-only counter (ClusterIP)** — only people already inside the building can use it. *(That's why localhost:8080 didn't work!)*
2. 🪟 **Side-window counter (NodePort)** — a little window in the wall with a strange number like 31234.
3. 🚪 **Front-door counter (LoadBalancer)** — a big front door with its own address; anyone can walk in.

### The receptionist (Ingress)

A shopping centre has **lots of shops**. Instead of each shop having its own front door, there's **one big entrance** with a **receptionist** — **Traefik**. You say "I want ice cream!" and they walk you to the ice cream counter. The receptionist's **list of which shop is where** is the **Ingress rules**.

### Grown-up mapping

| Story | Kubernetes | In this project |
|---|---|---|
| Ice cream helpers | Pods | `python-app-5d55f498dc-blplx` |
| Counter with a sign | Service | `python-app` |
| Sign's fixed spot | ClusterIP + port | `10.43.239.170:8080` |
| T-shirt sticker | Label | `app: python-app` |
| "Anyone with this sticker works for me" | Selector | `selector: app: python-app` |
| Helper's own hands | targetPort | `5000` (Flask) |
| Shopping-centre entrance | Port mapping to Traefik | `localhost:8081` |
| Receptionist | Ingress controller | Traefik |
| Receptionist's list | Ingress resource | [deploy/ingress.yaml](deploy/ingress.yaml) |

---

## 11. Troubleshooting cheat sheet

Work from the inside out — find the first layer that breaks.

| Layer | Check | Command |
|---|---|---|
| Pod running? | STATUS `Running`, READY `1/1` | `kubectl get pods -l app=python-app` |
| App logs | Errors on startup / requests | `kubectl logs deploy/python-app` |
| Service finds pods? | Endpoints list pod IP(s) — empty means selector/label mismatch | `kubectl get endpoints python-app` |
| Service works in-cluster? | JSON response | `kubectl run tmp --rm -i --restart=Never --image=curlimages/curl -- curl -s python-app:8080/api/v1/details` |
| Bypass Ingress | Works on `localhost:8080` | `kubectl port-forward svc/python-app 8080:8080` |
| Ingress rules | No duplicate host+path across Ingresses | `kubectl get ingress -A` / `kubectl describe ingress python-app` |
| Traefik running? | Service `traefik` exists with ports 80/443 | `kubectl get svc -n kube-system traefik` |
| Host port mapped? | Which host ports reach the cluster | `docker ps --format '{{.Names}} {{.Ports}}' \| grep serverlb` |
| End to end | JSON response | `curl localhost:8081/api/v1/details` |

**Symptom → likely cause:**

| Symptom | Likely cause |
|---|---|
| `ERR_CONNECTION_REFUSED` on `localhost` | Wrong host port — use the mapped one (`:8081`) |
| 404 with an `nginx/...` footer | Another Ingress (nginx demo) won the same rule |
| Plain `404 page not found` (Traefik) | No Ingress rule matches the host/path (e.g. host-based rules but you hit bare `localhost`) |
| 502 / Bad Gateway | Ingress points at the wrong Service port, or `targetPort` doesn't match the container port |
| Works via port-forward but not via Ingress | Ingress rule/host/path problem or port mapping |
| Endpoints empty | Service `selector` doesn't match pod `labels` |
