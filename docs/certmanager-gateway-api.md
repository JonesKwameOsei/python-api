# HTTPS on the Gateway with cert-manager

How the Traefik Gateway (`web` in `default`) went from plain HTTP to HTTPS: cert-manager issues a wildcard `*.test.com` certificate from a local CA, the Gateway terminates TLS on a new `https` listener, and Argo CD moves onto that listener. Includes why Let's Encrypt was dropped, how to trust the CA on Windows and WSL, and what went wrong along the way.

This builds on [ingress-to-gateway.md](ingress-to-gateway.md) (the Gateway) and [argocd-deploy-with-helm.md](argocd-deploy-with-helm.md) (Argo CD behind it).

## Contents

1. [What we're building](#1-what-were-building)
2. [Why not Let's Encrypt](#2-why-not-lets-encrypt)
3. [Where TLS lives in the Gateway API](#3-where-tls-lives-in-the-gateway-api)
4. [Starting point](#4-starting-point)
5. [Install cert-manager](#5-install-cert-manager)
6. [Open port 443 on k3d](#6-open-port-443-on-k3d)
7. [Create a local CA](#7-create-a-local-ca)
8. [Issue the wildcard certificate](#8-issue-the-wildcard-certificate)
9. [Add an HTTPS listener to the Gateway](#9-add-an-https-listener-to-the-gateway)
10. [Move Argo CD to HTTPS](#10-move-argo-cd-to-https)
11. [Trust the CA on your machine](#11-trust-the-ca-on-your-machine)
12. [Verifying](#12-verifying)
13. [Heads up](#13-heads-up)
14. [Troubleshooting](#14-troubleshooting)
15. [Cheat sheet](#15-cheat-sheet)
16. [Next steps](#16-next-steps)

---

## 1. What we're building

```text
browser ──https://argocd.test.com:8443──► k3d load balancer (8443 → 443)
                                              │
                                              ▼
                                  Traefik "websecure" entrypoint (8443)
                                  Gateway "web", listener "https"
                                  TLS terminated here with Secret test-com-tls
                                              │  plain HTTP from here on
                                              ▼
                                  HTTPRoute ──► Service ──► pod

cert-manager:
  ClusterIssuer "selfsigned" ──signs──► root CA "local-root-ca" (Secret in cert-manager ns)
  ClusterIssuer "local-ca"   ──signs with local-root-ca──► Certificate "*.test.com" (Secret test-com-tls in default ns)
```

| Piece | File | Job |
|---|---|---|
| cert-manager | Helm release | Creates and renews certificates, stores them as Secrets |
| `selfsigned` + `local-ca` ClusterIssuers, `local-root-ca` Certificate | [deploy/clusterissuer.yaml](../deploy/clusterissuer.yaml) | Your own private certificate authority |
| `test-com-tls` Certificate | [deploy/certificate.yaml](../deploy/certificate.yaml) | One wildcard cert for every `*.test.com` app |
| `https` listener | [deploy/gateway.yaml](../deploy/gateway.yaml) | Decrypts HTTPS using that cert |
| `sectionName: https` | [values-argo.yaml](../charts/python-app/argocd/values-argo.yaml) | Puts Argo CD's route on the HTTPS listener |

---

## 2. Why not Let's Encrypt

The first attempt used a Let's Encrypt ClusterIssuer with the HTTP-01 challenge. Even with the YAML fixed (see [troubleshooting](#clusterissuer-unknown-field-parentref)), it **can't work in this cluster**:

- **You don't own `test.com`.** Let's Encrypt only issues certificates for domains you control.
- **The cluster isn't reachable from the internet.** HTTP-01 works by Let's Encrypt fetching `http://<domain>/.well-known/acme-challenge/<token>` on port 80. `*.test.com` resolves to `127.0.0.1` only on your machine.

For a local cluster, the standard approach is to **create your own CA with cert-manager and trust it on your machine**. It's the same cert-manager workflow (Issuer → Certificate → Secret → Gateway) with no internet involved. When you move to a real domain on a public cluster, you swap the issuer for Let's Encrypt and nothing else changes.

| | Local CA (this guide) | Let's Encrypt |
|---|---|---|
| Needs a real domain | No | Yes |
| Needs public port 80 (HTTP-01) or DNS API access (DNS-01) | No | Yes |
| Trusted by browsers automatically | No, you install the CA once | Yes |
| Good for | Local and dev clusters | Anything public |

---

## 3. Where TLS lives in the Gateway API

The question was: *"Is there a quick way to do it in the values files?"* Mostly no, and the reason is a key Gateway API idea.

With **Ingress**, every Ingress carried its own `tls:` block, so each app configured TLS in its own values. With the **Gateway API**, **TLS belongs to the Gateway's listener**. Routes never hold certificates. They only choose which listener to attach to.

| Where | What changes |
|---|---|
| Gateway | New `https` listener with the certificate. This is the main work. |
| Argo CD values | `sectionName: http` becomes `sectionName: https` |
| python-app values | Nothing. Its route has no `sectionName`, so it attaches to **every** listener and gets HTTPS automatically. |

This is the division of roles the Gateway API was designed around: the platform owner manages the Gateway and certificates once, and app teams just attach routes.

### TLS termination

`tls.mode: Terminate` means the Gateway decrypts the traffic and forwards **plain HTTP** to the pods. That's why Argo CD keeps `server.insecure: true`. The pods never deal with TLS.

---

## 4. Starting point

What the cluster looked like before, and why each item mattered:

| Item | State | Impact |
|---|---|---|
| cert-manager | Not installed | Install it first ([section 5](#5-install-cert-manager)) |
| Traefik `websecure` entrypoint | Listens on port `8443` in the cluster, Service port `443` | The Gateway's HTTPS listener must use port **`8443`**, not `443` |
| k3d load balancer | Only `8081 → 80` mapped | **Nothing reached port 443 from your machine.** You must add a mapping ([section 6](#6-open-port-443-on-k3d)). |
| Gateway `web` | One `http` listener on `8000` | Add an `https` listener |

How to check these yourself:

```bash
# Traefik entrypoint ports (look for websecure)
kubectl -n kube-system get deploy traefik -o jsonpath='{.spec.template.spec.containers[0].args}' | tr ',' '\n' | grep entryPoints

# Traefik Service ports
kubectl -n kube-system get svc traefik -o jsonpath='{range .spec.ports[*]}{.name} {.port}->{.targetPort}{"\n"}{end}'

# What k3d exposes on your machine
docker ps --format '{{.Names}} {{.Ports}}' | grep serverlb
```

### Gateway listener port = Traefik entrypoint port

Traefik maps a Gateway listener to the entrypoint with the **same port**:

| Listener port | Traefik entrypoint | Service port | k3d host port |
|---|---|---|---|
| `8000` | `web` | `80` | `8081` |
| `8443` | `websecure` | `443` | `8443` |

Using `port: 443` on the listener (as most docs do) wouldn't match any Traefik entrypoint, and the listener wouldn't be programmed.

---

## 5. Install cert-manager

```bash
# CRDs first
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.4/cert-manager.crds.yaml

# Then the controller
helm repo add jetstack https://charts.jetstack.io
helm repo update

helm upgrade --install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --version v1.14.4 \
  --set installCRDs=false

kubectl -n cert-manager get pods     # cert-manager, cainjector, webhook: all Running
```

> **Heads up:** v1.14.4 is from 2024. It's fine for this guide, because we write Certificates by hand. A newer version is needed if you later want cert-manager to read the Gateway and create Certificates for you (see [next steps](#16-next-steps)). Check what's current with `helm search repo jetstack/cert-manager --versions | head`.

---

## 6. Open port 443 on k3d

k3d only forwards the ports you ask for when the cluster is created. Add one for HTTPS:

```bash
k3d cluster edit k3s-default --port-add "8443:443@loadbalancer"

docker ps --format '{{.Names}} {{.Ports}}' | grep serverlb
# k3d-k3s-default-serverlb 0.0.0.0:8081->80/tcp, 0.0.0.0:8443->443/tcp, 0.0.0.0:6550->6443/tcp
```

`8443:443@loadbalancer` means: host port `8443` goes to port `443` on the k3d load balancer, which reaches Traefik's `websecure` entrypoint. This recreates the load balancer container, so expect a few seconds of downtime.

---

## 7. Create a local CA

[deploy/clusterissuer.yaml](../deploy/clusterissuer.yaml):

```yaml
# 1. A bootstrap issuer that can only self-sign
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: selfsigned
spec:
  selfSigned: {}
---
# 2. Use it to create a root CA certificate (stored as a Secret in cert-manager's namespace)
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: local-root-ca
  namespace: cert-manager
spec:
  isCA: true
  commonName: local-root-ca
  secretName: local-root-ca
  duration: 87600h        # 10 years
  privateKey:
    algorithm: ECDSA
    size: 256
  issuerRef:
    name: selfsigned
    kind: ClusterIssuer
---
# 3. The issuer you actually use: signs certificates with that root CA
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: local-ca
spec:
  ca:
    secretName: local-root-ca
```

### Why three objects

| Object | Role |
|---|---|
| `selfsigned` ClusterIssuer | Can only sign a certificate with its own key. Used **once**, to create the CA. |
| `local-root-ca` Certificate | `isCA: true` makes it a certificate authority. Its key and cert go into the Secret `local-root-ca`. |
| `local-ca` ClusterIssuer | Signs app certificates with that CA. This is the one everything else uses. |

Why not use `selfsigned` directly for the app certificate? Every self-signed certificate is its own root, so you'd have to trust each one separately on your machine. With a CA, you trust **one** root and every certificate it signs is trusted.

### Why the CA lives in `cert-manager`

A **ClusterIssuer** reads its Secret from cert-manager's *cluster resource namespace*. By default that's the namespace cert-manager runs in (`--cluster-resource-namespace=$(POD_NAMESPACE)`). Put `local-root-ca` anywhere else and `local-ca` won't find it.

```bash
kubectl apply -f deploy/clusterissuer.yaml
kubectl get clusterissuer                              # selfsigned and local-ca: READY True
kubectl -n cert-manager get certificate local-root-ca  # READY True
```

---

## 8. Issue the wildcard certificate

[deploy/certificate.yaml](../deploy/certificate.yaml):

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: test-com-tls
  namespace: default          # must match the Gateway's namespace
spec:
  secretName: test-com-tls
  dnsNames:
    - "*.test.com"            # covers python-app.test.com, argocd.test.com, and future apps
  issuerRef:
    name: local-ca
    kind: ClusterIssuer
```

| Field | Why |
|---|---|
| `namespace: default` | A Gateway can only use Secrets **in its own namespace**, unless a ReferenceGrant allows otherwise. The Gateway is in `default`, so the Secret must be too. |
| `"*.test.com"` | One wildcard cert for every app. Quoted because YAML treats a leading `*` specially. A wildcard covers **one level only**: `argocd.test.com` yes; `test.com` and `a.b.test.com` no. |
| `issuerRef` | Signed by the local CA from section 7. |

cert-manager writes the result to Secret `test-com-tls` (keys `tls.crt`, `tls.key`, `ca.crt`) and renews it automatically before it expires.

```bash
kubectl apply -f deploy/certificate.yaml
kubectl -n default get certificate test-com-tls    # READY True
kubectl -n default get secret test-com-tls         # type kubernetes.io/tls
```

---

## 9. Add an HTTPS listener to the Gateway

[deploy/gateway.yaml](../deploy/gateway.yaml):

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
      port: 8000
      allowedRoutes:
        namespaces:
          from: Selector
          selector:
            matchLabels:
              gateway-access: "true"
    - name: https
      protocol: HTTPS
      port: 8443                  # Traefik's websecure entrypoint
      tls:
        mode: Terminate           # the Gateway decrypts, then talks HTTP to the pods
        certificateRefs:
          - name: test-com-tls
      allowedRoutes:
        namespaces:
          from: Selector
          selector:
            matchLabels:
              gateway-access: "true"
```

| Field | Why |
|---|---|
| `name: https` | The `sectionName` routes use to attach to this listener |
| `port: 8443` | Must match Traefik's `websecure` entrypoint ([section 4](#gateway-listener-port--traefik-entrypoint-port)) |
| `tls.mode: Terminate` | Decrypt at the Gateway; backends get plain HTTP |
| `certificateRefs` | The Secret from section 8. `kind: Secret` is the default, so it's omitted. |
| `allowedRoutes` | The same label rule as the `http` listener. Each listener has its own rule, so copy it, or the HTTPS listener has different access rules. |

The `http` listener stays, so both protocols work side by side (see [heads up](#13-heads-up)).

```bash
kubectl apply -f deploy/gateway.yaml
kubectl get gateway web -o jsonpath='{range .status.listeners[*]}{.name}: {.conditions[?(@.type=="Programmed")].status}{"\n"}{end}'
# http: True
# https: True
```

---

## 10. Move Argo CD to HTTPS

In [values-argo.yaml](../charts/python-app/argocd/values-argo.yaml), change only the `sectionName`:

```yaml
server:
  httproute:
    enabled: true
    hostnames:
      - argocd.test.com
    parentRefs:
      - name: web
        namespace: default
        sectionName: https       # was: http
```

**Keep `configs.params.server.insecure: true`.** The Gateway handles TLS, and Argo CD still receives plain HTTP. Setting it to `false` makes Argo CD serve HTTPS itself, so the Gateway would be sending HTTP to an HTTPS backend. The result is redirect loops or 502 errors.

```bash
helm upgrade --install argocd argo/argo-cd -n argocd \
  -f charts/python-app/argocd/values-argo.yaml

kubectl -n argocd get httproute argocd-server -o jsonpath='{.spec.parentRefs[*].sectionName}'   # https
```

### The CLI on HTTPS

Before the CA is trusted (section 11), skip certificate checks:

```bash
argocd login argocd.test.com:8443 --grpc-web --insecure --username admin
```

After the CA is trusted in WSL, drop `--insecure`:

```bash
argocd login argocd.test.com:8443 --grpc-web --username admin
```

| Flag | HTTP (before) | HTTPS, CA not trusted | HTTPS, CA trusted |
|---|---|---|---|
| Port | `8081` | `8443` | `8443` |
| `--plaintext` | Yes | **No** | **No** |
| `--insecure` | No | Yes | No |
| `--grpc-web` | Yes | Yes | Yes |

---

## 11. Trust the CA on your machine

Until you do this, browsers show **"Your connection is not private"**. Here's why:

```text
argocd.test.com cert  ──signed by──►  local-root-ca  ──signed by──►  ??? (not trusted)
```

Your machine has never seen `local-root-ca`. Add it to the trusted root list once, and every certificate it signs becomes trusted, including `*.test.com` and any future ones.

### Export the CA certificate

```bash
kubectl -n cert-manager get secret local-root-ca -o jsonpath='{.data.ca\.crt}' | base64 -d > local-root-ca.crt

openssl x509 -in local-root-ca.crt -noout -subject -enddate   # check: CN = local-root-ca
```

This only exports the **public certificate** (`ca.crt`). See the [warning about the private key](#the-ca-private-key).

### WSL and Windows are separate

WSL and Windows each keep **their own** trusted-CA list:

| Tool | Runs in | Uses the trust store of |
|---|---|---|
| `curl`, `argocd` CLI | WSL | Debian (`/etc/ssl/certs`) |
| Chrome, Edge | Windows | Windows certificate store |
| Firefox | Windows | Firefox's own store |

Trust the CA in each one you use.

### WSL (curl, argocd CLI)

```bash
sudo cp local-root-ca.crt /usr/local/share/ca-certificates/
sudo update-ca-certificates        # "1 added"
```

The file must end in `.crt` for `update-ca-certificates` to pick it up.

### Windows (Chrome, Edge): GUI

The file lives inside WSL, so open it from Windows through the WSL network path:

1. Press **Win + R**, paste this and press Enter:
   ```text
   \\wsl.localhost\Debian\home\jones\platform\devops\python-api
   ```
   File Explorer opens your project folder.
2. Double-click **local-root-ca.crt**, then click **Install Certificate...**.
3. Store Location: choose **Current User**, then click Next. This doesn't need admin rights; **Local Machine** also works but needs admin.
4. Choose **Place all certificates in the following store**, click **Browse...**, and pick **Trusted Root Certification Authorities**.
5. Click Next, then Finish. Windows asks whether to install a certificate from "local-root-ca". Click **Yes**.

### Windows: one command from WSL

Run this from the project folder. It does the same as the GUI steps, including the Yes/No popup:

```bash
certutil.exe -user -addstore Root "$(wslpath -w local-root-ca.crt)"
```

`wslpath -w` turns the WSL path into a Windows path that `certutil.exe` understands.

### Firefox

Firefox ignores the Windows store. Go to **Settings → Privacy & Security → Certificates → View Certificates → Authorities → Import**, pick the file, and tick **Trust this CA to identify websites**.

### Then

Fully close the browser (all windows), reopen it, and go to `https://argocd.test.com:8443`. You should see a padlock and no warning.

---

## 12. Verifying

```bash
# Issuers and certificates
kubectl get clusterissuer                    # local-ca, selfsigned: True
kubectl get certificate -A                   # local-root-ca, test-com-tls: True

# Gateway listeners and how many routes each has
kubectl get gateway web -o jsonpath='{range .status.listeners[*]}{.name}: programmed={.conditions[?(@.type=="Programmed")].status} routes={.attachedRoutes}{"\n"}{end}'

# Which listener each route uses
kubectl get httproute -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} section={.spec.parentRefs[*].sectionName}{"\n"}{end}'

# HTTPS end to end; verify=0 means the certificate is trusted
curl -s -o /dev/null -w "%{http_code} verify=%{ssl_verify_result}\n" https://argocd.test.com:8443

# The certificate the Gateway actually serves
openssl s_client -connect argocd.test.com:8443 -servername argocd.test.com </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -issuer -ext subjectAltName
# issuer=CN = local-root-ca
# DNS:*.test.com
```

What it looked like when everything was working:

```text
local-ca     True
selfsigned   True
cert-manager   local-root-ca   True
default        test-com-tls    True

http: programmed=True routes=0
https: programmed=True routes=1

argocd/argocd-server section=https

https://argocd.test.com:8443   200 verify=0
```

---

## 13. Heads up

### Argo CD no longer answers on HTTP

Argo CD's route is attached **only** to the `https` listener, so `http://argocd.test.com:8081` now returns **404**. That's expected: use `https://argocd.test.com:8443`. Update bookmarks and any `argocd login ...:8081 --plaintext` commands.

### HTTP isn't redirected to HTTPS yet

Both listeners are active. A route without `sectionName` (like python-app's) works on both `http://...:8081` and `https://...:8443`, and nothing pushes users to HTTPS. Forcing HTTPS needs:

1. a redirect HTTPRoute on the `http` listener (`RequestRedirect` filter, `scheme: https`, `port: 8443`), and
2. a small chart change so python-app's route attaches only to `https`. Otherwise it competes with the redirect route on the `http` listener.

### The CA private key

The Secret `local-root-ca` in `cert-manager` holds the CA's **private key** (`tls.key`). Anyone who has it can create certificates that your machine will trust **for any website**, including your bank, because you told your machine to trust this CA.

- **Never** export `tls.key`, copy it or commit it.
- `local-root-ca.crt` (the public certificate) is harmless. It can only *check* certificates, not create them. It's still machine-specific, so it's kept out of Git: `*.crt` is in [.gitignore](../.gitignore).
- If you delete the cluster, the CA goes with it. A new cluster gives you a new CA, so you'll need to trust it again (and remove the old one from Windows: `certmgr.msc` → Trusted Root Certification Authorities → Certificates → `local-root-ca`).

### Don't use this CA outside your machine

The local CA is only for development. On a public cluster with a real domain, use a Let's Encrypt ClusterIssuer. The Certificate and Gateway stay the same; only `issuerRef` changes.

### Wildcard scope

`*.test.com` covers one level of subdomain. A hostname like `api.python-app.test.com` would need its own `dnsNames` entry (e.g. `"*.python-app.test.com"`).

---

## 14. Troubleshooting

### ClusterIssuer: unknown field `parentRef`

```text
Error from server (BadRequest): error when creating "deploy/clusterissuer.yaml": ClusterIssuer in version "v1"
cannot be handled as a ClusterIssuer: strict decoding error: unknown field "spec.acme.solvers[0].http01.gatewayHTTPRoute.parentRef"
```

The cert-manager API field is **`parentRefs`**, plural, and it takes a list:

```yaml
solvers:
  - http01:
      gatewayHTTPRoute:
        parentRefs:            # not parentRef
          - name: web
            namespace: default
```

Fixing it would let the ClusterIssuer apply, but Let's Encrypt still can't issue certificates here ([section 2](#2-why-not-lets-encrypt)). That's why the issuer was replaced with the local CA.

### What was wrong in the first draft of this guide

The original version of this file was copied from a generic example. Each of these settings would have broken this cluster:

| Draft setting | Problem | What worked |
|---|---|---|
| Let's Encrypt ClusterIssuer | No owned domain, not publicly reachable | Local CA ([section 7](#7-create-a-local-ca)) |
| `gatewayHTTPRoute.parentRef` | Field doesn't exist | `parentRefs` (but not needed with the local CA) |
| `server.insecure: false` | Argo CD serves HTTPS behind a Gateway that sends HTTP; redirect loops or 502 errors | Keep `server.insecure: true` |
| Certificate in namespace `argocd` | The Gateway in `default` can't read Secrets from other namespaces without a ReferenceGrant | Certificate in `default` |
| Certificate for `argocd.test.com` only | One cert per app, each one added to the Gateway | Wildcard `*.test.com` |
| `gatewayClassName: nginx` | This cluster runs Traefik | `traefik` |
| Listener `port: 443` | No Traefik entrypoint on 443 | `8443` (`websecure`) |
| No k3d port mapping | Nothing reaches 443 from the host | `k3d cluster edit --port-add "8443:443@loadbalancer"` |
| `allowedRoutes.namespaces.from: All` | Drops the `gateway-access` label rule the `http` listener enforces | Same selector on both listeners |
| HTTPS only, no `http` listener | Breaks everything still on HTTP | Keep both listeners |

### Symptom table

| Symptom | Likely cause | Fix / check |
|---|---|---|
| Certificate stuck `READY False` | Issuer not ready, or CA Secret in the wrong namespace | `kubectl describe certificate <name> -n <ns>`; `kubectl get clusterissuer` |
| `local-ca` ClusterIssuer not ready: secret not found | `local-root-ca` not in `cert-manager` namespace | Keep the CA Certificate in `cert-manager` |
| Listener `https` not `Programmed` | Port doesn't match a Traefik entrypoint, or Secret missing | `kubectl describe gateway web`, then check the listener conditions |
| Listener status `RefNotPermitted` / `InvalidCertificateRef` | Secret in another namespace, or not type `kubernetes.io/tls` | Certificate in the Gateway's namespace |
| `curl: (7) Failed to connect ... port 8443` | k3d not forwarding 8443 | `docker ps \| grep serverlb`; add the port mapping |
| `curl: (60) SSL certificate problem: unable to get local issuer certificate` | CA not trusted in WSL | [Trust in WSL](#wsl-curl-argocd-cli), or use `curl -k` |
| Browser "Your connection is not private" | CA not trusted in Windows or Firefox | [Trust in Windows](#windows-chrome-edge-gui); restart the browser |
| Browser still warns after installing the CA | Browser not fully restarted, or cert put in the wrong store | Close all windows; check `certmgr.msc` → Trusted Root Certification Authorities |
| `ERR_TOO_MANY_REDIRECTS` / 502 on Argo CD | `server.insecure: false` | Set it back to `true` |
| Argo CD 404 on `http://...:8081` | Route is on `https` only | Expected; use `https://...:8443` |
| `argocd login` fails with a TLS error | Still using `--plaintext`, or port 8081 | `argocd.test.com:8443 --grpc-web` (plus `--insecure` if the CA isn't trusted) |
| Route `Accepted: False`, `NotAllowedByListeners` | Namespace missing the `gateway-access=true` label | `kubectl label namespace <ns> gateway-access=true` |

Useful detail commands:

```bash
kubectl describe certificate test-com-tls -n default
kubectl get certificaterequest -A
kubectl -n cert-manager logs deploy/cert-manager --tail=50
kubectl describe gateway web
```

---

## 15. Cheat sheet

```bash
# One-time setup
k3d cluster edit k3s-default --port-add "8443:443@loadbalancer"
kubectl apply -f deploy/clusterissuer.yaml
kubectl apply -f deploy/certificate.yaml
kubectl apply -f deploy/gateway.yaml
helm upgrade --install argocd argo/argo-cd -n argocd -f charts/python-app/argocd/values-argo.yaml

# Trust the CA
kubectl -n cert-manager get secret local-root-ca -o jsonpath='{.data.ca\.crt}' | base64 -d > local-root-ca.crt
sudo cp local-root-ca.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates     # WSL
certutil.exe -user -addstore Root "$(wslpath -w local-root-ca.crt)"                            # Windows

# Status
kubectl get clusterissuer
kubectl get certificate -A
kubectl get gateway web -o jsonpath='{range .status.listeners[*]}{.name}: {.conditions[?(@.type=="Programmed")].status}{"\n"}{end}'

# Test
curl -s -o /dev/null -w "%{http_code} verify=%{ssl_verify_result}\n" https://argocd.test.com:8443
argocd login argocd.test.com:8443 --grpc-web --username admin

# Force a certificate renewal (cmctl), or delete the Secret and cert-manager re-issues it
kubectl -n default delete secret test-com-tls
```

---

## 16. Next steps

- **Redirect HTTP to HTTPS:** a redirect HTTPRoute on the `http` listener, plus a `sectionName` value in the python-app chart so its route attaches only to `https`.
- **Upgrade cert-manager:** recent versions can watch the Gateway itself. Annotate it with `cert-manager.io/cluster-issuer: local-ca`, enable Gateway API support in cert-manager, and it creates the Certificate from the listener's `hostname` and `certificateRefs`, so you don't need `certificate.yaml`.
- **Manage this with Argo CD:** put `deploy/clusterissuer.yaml`, `certificate.yaml` and `gateway.yaml` under an Argo CD Application so the platform layer is GitOps too.
- **Real domain:** on a public cluster, add a Let's Encrypt ClusterIssuer (HTTP-01 via `gatewayHTTPRoute.parentRefs`, or DNS-01 for wildcards) and point `issuerRef` at it.
