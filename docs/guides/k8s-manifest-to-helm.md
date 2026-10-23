# From Kubernetes Manifests to a Helm Chart

How `python-app` went from hand-written manifests in [deploy/](deploy/) to a custom Helm chart in [charts/python-app/](charts/python-app/): why, how each manifest maps to a template, the templating syntax used, how to deploy and operate it, and what broke along the way.

This builds on the Gateway API setup in [ingress-to-gateway.md](ingress-to-gateway.md) and the Service basics in [service-explanation.md](service-explanation.md).

## Contents

1. [Why Helm](#1-why-helm)
2. [What goes in the chart and what stays out](#2-what-goes-in-the-chart-and-what-stays-out)
3. [Why not the `helm create` scaffold](#3-why-not-the-helm-create-scaffold)
4. [Chart layout](#4-chart-layout)
5. [Chart.yaml](#5-chartyaml)
6. [values.yaml](#6-valuesyaml)
7. [_helpers.tpl](#7-_helperstpl)
8. [deployment.yaml](#8-deploymentyaml)
9. [service.yaml](#9-serviceyaml)
10. [httproute.yaml](#10-httprouteyaml)
11. [Template syntax cheat sheet](#11-template-syntax-cheat-sheet)
12. [Deploying](#12-deploying)
13. [Day-to-day operations](#13-day-to-day-operations)
14. [Ports: which one to curl](#14-ports-which-one-to-curl)
15. [Troubleshooting](#15-troubleshooting)
16. [ConfigMap, HPA and NOTES.txt](#16-configmap-hpa-and-notestxt)
17. [Next steps](#17-next-steps)

---

## 1. Why Helm

With raw manifests, every value is hard-coded. The image tag, replica count, hostname and ports are copied across files, and they drift apart. That's how [deploy/httproute.yaml](deploy/httproute.yaml) ended up sending traffic to port `8080` while the Service listened on `5000`.

Helm turns the manifests into **templates** and puts everything that varies into one **values.yaml**. You get:

| Raw manifests | Helm chart |
|---|---|
| `kubectl apply -f deploy/` | `helm install python-app charts/python-app` |
| Edit YAML to change the image tag | `helm upgrade ... --set image.tag=v2` |
| No history, no undo | `helm history` / `helm rollback` |
| Delete each file by hand | `helm uninstall` removes everything it created |
| Values duplicated across files | One source of truth, referenced everywhere |
| One copy per environment | One chart, plus a values file per environment |

## 2. What goes in the chart and what stays out

| Manifest | In the chart? | Why |
|---|---|---|
| [deployment.yaml](deploy/deployment.yaml) | Yes → `templates/deployment.yaml` | Belongs to the app |
| [service.yaml](deploy/service.yaml) | Yes → `templates/service.yaml` | Belongs to the app |
| [httproute.yaml](deploy/httproute.yaml) | Yes → `templates/httproute.yaml` | Belongs to the app |
| [gateway.yaml](deploy/gateway.yaml) | **No** | Shared platform infrastructure. `whoami` uses it too, and uninstalling the app must not remove the entrance for every app. |
| [traefik-config.yaml](deploy/traefik-config.yaml) | **No** | Cluster-level Traefik configuration, owned by the cluster admin |
| [whoami.yaml](deploy/whoami.yaml) | **No** | A different app |
| [namespace-python.yaml](deploy/namespace-python.yaml) | **No** | The namespace must exist, with the `gateway-access` label, before the release is installed. Uninstalling a release should not delete its namespace. |

Rule of thumb: a chart contains what you want created and deleted **together with the app**.

## 3. Why not the `helm create` scaffold

`helm create python-app` generates a generic chart with about 15 optional features (ServiceAccount, HPA, Ingress, HTTPRoute, security contexts, volumes, …). It's a useful reference, but as-is it did not work for this app:

| Scaffold default | Problem |
|---|---|
| `serviceAccount.create: true`, but no `serviceaccount.yaml` template | The Deployment references a ServiceAccount that never gets created, so pods are never created |
| Probes on `path: /` | Flask only serves `/api/v1/details` and `/api/v1/healthz`. `/` returns 404, so pods never become Ready. |
| `httpRoute.parentRefs.name: gateway`, path `/headers`, host `chart-example.local` | Placeholders. Ours are `web`, `/` and `python-app.test.com`. |

So the templates were deleted and rewritten from the manifests, keeping only what this app uses:

```bash
cd charts/python-app
rm -rf templates/* && mkdir -p templates
```

## 4. Chart layout

```text
charts/python-app/
├── Chart.yaml            # chart identity and versions
├── values.yaml           # everything that varies
├── .helmignore
└── templates/
    ├── _helpers.tpl      # reusable snippets (names, labels)
    ├── deployment.yaml   # from deploy/deployment.yaml
    ├── service.yaml      # from deploy/service.yaml
    ├── httproute.yaml    # from deploy/httproute.yaml
    ├── configmap.yaml    # env vars (added later, see section 16)
    ├── hpa.yaml          # optional autoscaler (section 16)
    └── NOTES.txt         # printed after install/upgrade (section 16)
```

The method for each template is the same: start from the working manifest, then replace each hard-coded value with `{{ .Values.<something> }}` and add that key to `values.yaml`.

## 5. Chart.yaml

```yaml
apiVersion: v2
name: python-app
description: Flask API (python-app) deployed behind the Traefik Gateway
type: application
version: 0.1.0      # chart version: bump when templates change
appVersion: "v1"    # app version: used as the default image tag
```

- **`version`** is the chart's own version. Bump it whenever you change the chart.
- **`appVersion`** is the version of the app it deploys. The Deployment template uses it as the image tag when `image.tag` is empty.

## 6. values.yaml

```yaml
replicaCount: 1

image:
  repository: kwameds/python-app
  tag: ""                 # empty = use appVersion from Chart.yaml
  pullPolicy: IfNotPresent

containerPort: 5000       # app.py: app.run(port=5000)

service:
  type: ClusterIP
  port: 5000

healthCheckPath: /api/v1/healthz

httpRoute:
  enabled: true
  gateway:
    name: web             # the Gateway's metadata.name, NOT its listener name
    namespace: default
  hostnames:
    - python-app.test.com
  pathPrefix: /

resources: {}
```

Any of these can be overridden at install time with `--set key=value` or `-f other-values.yaml`.

## 7. _helpers.tpl

Files starting with `_` don't render to Kubernetes objects. They hold named snippets that templates call with `include`.

```yaml
{{- define "python-app.name" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "python-app.selectorLabels" -}}
app: {{ include "python-app.name" . }}
{{- end }}

{{- define "python-app.labels" -}}
{{ include "python-app.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}
```

- **Name from the release:** `helm install python-app ...` produces objects named `python-app`, the same names as the original manifests. Installing the chart again as `python-app-2` gives a second, independent copy.
- **`trunc 63`:** Kubernetes names and labels are limited to 63 characters.
- **Two label sets:**
  - `selectorLabels` stay minimal and fixed. They're used in `spec.selector`, which Kubernetes doesn't allow you to change after creation.
  - `labels` add metadata such as the chart version, which may change on every upgrade.

## 8. deployment.yaml

Mapped from [deploy/deployment.yaml](deploy/deployment.yaml):

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "python-app.name" . }}
  labels:
    {{- include "python-app.labels" . | nindent 4 }}
spec:
  replicas: {{ .Values.replicaCount }}
  selector:
    matchLabels:
      {{- include "python-app.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "python-app.selectorLabels" . | nindent 8 }}
    spec:
      containers:
        - name: {{ .Chart.Name }}
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - name: http
              containerPort: {{ .Values.containerPort }}
          readinessProbe:
            httpGet:
              path: {{ .Values.healthCheckPath }}
              port: http
          livenessProbe:
            httpGet:
              path: {{ .Values.healthCheckPath }}
              port: http
          {{- with .Values.resources }}
          resources:
            {{- toYaml . | nindent 12 }}
          {{- end }}
```

What changed compared with the manifest:

| Manifest | Template |
|---|---|
| `name: python-app` | `{{ include "python-app.name" . }}` |
| `replicas: 1` | `{{ .Values.replicaCount }}` |
| `image: kwameds/python-app:v1` | `repository` + `tag`, with `appVersion` as the fallback |
| `containerPort: 5000` | `{{ .Values.containerPort }}`, plus a port **name** (`http`) |
| No probes | Readiness and liveness probes on `/api/v1/healthz` |
| No resources | Optional `resources`, rendered only when set |

The **readiness probe** stops the Service from sending traffic to a pod until Flask answers. The **liveness probe** restarts the container if Flask hangs.

## 9. service.yaml

Mapped from [deploy/service.yaml](deploy/service.yaml):

```yaml
apiVersion: v1
kind: Service
metadata:
  name: {{ include "python-app.name" . }}
  labels:
    {{- include "python-app.labels" . | nindent 4 }}
spec:
  type: {{ .Values.service.type }}
  selector:
    {{- include "python-app.selectorLabels" . | nindent 4 }}
  ports:
    - name: http
      protocol: TCP
      port: {{ .Values.service.port }}
      targetPort: http     # refers to the container port by its name
```

`targetPort: http` points at the container port **by name**. If `containerPort` changes, the Service follows automatically.

## 10. httproute.yaml

Mapped from [deploy/httproute.yaml](deploy/httproute.yaml):

```yaml
{{- if .Values.httpRoute.enabled }}
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: {{ include "python-app.name" . }}
  labels:
    {{- include "python-app.labels" . | nindent 4 }}
spec:
  parentRefs:
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: {{ .Values.httpRoute.gateway.name }}
      namespace: {{ .Values.httpRoute.gateway.namespace }}
  {{- with .Values.httpRoute.hostnames }}
  hostnames:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: {{ .Values.httpRoute.pathPrefix }}
      backendRefs:
        - name: {{ include "python-app.name" . }}
          port: {{ .Values.service.port }}
{{- end }}
```

- **`backendRefs.port`** comes from `service.port`, the same value the Service uses. This removes the `8080` vs `5000` mismatch from the raw manifest for good.
- **`if .Values.httpRoute.enabled`** lets you install the app without exposing it: `--set httpRoute.enabled=false`.

## 11. Template syntax cheat sheet

| Syntax | Meaning |
|---|---|
| `{{ .Values.x.y }}` | Value from values.yaml (or `--set` / `-f`) |
| `{{ .Release.Name }}` | Name given to `helm install <name>` |
| `{{ .Release.Namespace }}` / `{{ .Release.Service }}` | Target namespace / always `Helm` |
| `{{ .Chart.Name }}` / `{{ .Chart.Version }}` / `{{ .Chart.AppVersion }}` | Fields from Chart.yaml |
| `{{-` / `-}}` | Remove whitespace and newlines on that side of the tag |
| `include "name" .` | Render a snippet defined in `_helpers.tpl` |
| `\| nindent N` | Add a newline, then indent every line by N spaces. N must match where the block sits in the YAML. |
| `\| toYaml` | Turn a map or list from values into YAML |
| `\| default X` | Use X if the value is empty |
| `\| quote` | Wrap the value in quotes |
| `{{- with .Values.x }} … {{- end }}` | Render the block only if `x` is non-empty. Inside it, `.` is `x`. |
| `{{- if .Values.x }} … {{- end }}` | Render conditionally |
| `{{- range .Values.list }} … {{- end }}` | Loop over a list |

Most template bugs are **indentation** bugs. Check the output with `helm template` before installing anything.

## 12. Deploying

### One-time cluster prerequisites

These are platform pieces outside the chart. See [ingress-to-gateway.md](ingress-to-gateway.md).

```bash
kubectl apply -f deploy/traefik-config.yaml                  # enable Traefik's Gateway API provider
kubectl -n kube-system rollout status deploy/traefik
kubectl get gatewayclass                                     # "traefik" must be ACCEPTED=True

kubectl apply -f deploy/gateway.yaml
kubectl get gateway web                                      # PROGRAMMED=True

kubectl label namespace default gateway-access=true --overwrite   # Gateway only accepts routes from labelled namespaces
```

### Switching from kubectl-managed to Helm-managed

Helm won't take over objects created with `kubectl apply`. It fails with `... already exists`. Delete them first:

```bash
kubectl delete -f deploy/deployment.yaml -f deploy/service.yaml -f deploy/httproute.yaml
```

### Check locally, then install

```bash
helm lint charts/python-app
helm template python-app charts/python-app                     # full rendered YAML
helm template python-app charts/python-app | grep -E '^kind'   # expect: Service, Deployment, HTTPRoute

helm install python-app charts/python-app --dry-run=server     # validate against the cluster
helm install python-app charts/python-app
```

### Verify

```bash
kubectl get pods,svc,httproute -l app=python-app
kubectl get httproute python-app -o jsonpath='{.status.parents[0].conditions}'   # Accepted=True

curl http://python-app.test.com:8081/api/v1/details
curl http://python-app.test.com:8081/api/v1/healthz
```

Expected:

```json
{"time": "11:51:47AM on October 18, 2026", "hostname": "python-app-648854b8dc-fkndl"}
{"status": "up"}
```

### Moving the release to its own namespace (`python`)

The release was first installed into `default` and then moved into a dedicated `python` namespace. The chart needed no changes:

- the templates don't set `metadata.namespace`, so every object goes into the release namespace (`-n python`);
- `httpRoute.gateway.namespace: default` still points the route at the Gateway, which stays in `default` as shared infrastructure;
- the HTTPRoute and its backend Service are in the same namespace, so no ReferenceGrant is needed.

What *does* matter is that [deploy/gateway.yaml](deploy/gateway.yaml) only accepts routes from labelled namespaces:

```yaml
allowedRoutes:
  namespaces:
    from: Selector
    selector:
      matchLabels:
        gateway-access: "true"
```

**1. Create the namespace with that label.** The Gateway holds the **selector**. The namespace only has to carry the matching **label**, under `metadata.labels`:

```yaml
# deploy/namespace-python.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: python
  labels:
    gateway-access: "true"
```

```bash
kubectl apply --dry-run=server -f deploy/namespace-python.yaml   # validate first
kubectl apply -f deploy/namespace-python.yaml
kubectl get ns python --show-labels                              # gateway-access=true
```

Why not the alternatives:

- **`helm install --create-namespace`** creates the namespace *without labels*. The route would be rejected (`NotAllowedByListeners`), and the app would return Traefik's 404.
- **A `namespace.yaml` template inside the chart** would make the namespace part of the release, so `helm uninstall` would delete it along with everything else in it.

**2. Install into the new namespace before removing the old release.** Releases are scoped to a namespace, so `python-app` in `python` and `python-app` in `default` are two independent releases. Installing first keeps the app reachable: for a moment both routes claim the same hostname, and the Gateway keeps serving the older one until it's removed.

```bash
helm template python-app charts/python-app -n python | grep -E '^kind'
helm install python-app charts/python-app -n python
kubectl -n python get httproute python-app -o jsonpath='{.status.parents[0].conditions}'   # Accepted=True
```

**3. Remove the old release.**

```bash
helm uninstall python-app -n default
helm list -A                                   # only python-app in python
kubectl get all,httproute -n default -l app=python-app   # No resources found
```

**4. Verify** that the response comes from the pod in `python`:

```bash
kubectl -n python get pods -l app=python-app
curl http://python-app.test.com:8081/api/v1/details   # "hostname" matches the pod name above
```

**5. Remember the namespace.** `helm` and `kubectl` commands without `-n` look in the context's default namespace and report `not found` otherwise. Either add `-n python` every time, or make it the default for this context (Helm uses it too):

```bash
kubectl config set-context --current --namespace=python
kubectl config view --minify -o jsonpath='{..namespace}'   # check the current default
```

## 13. Day-to-day operations

The release lives in `python`. Add `-n python` to the commands below, unless it's already your context's default namespace.

```bash
helm list                                                      # releases in this namespace
helm list -a                                                   # include failed/pending releases
helm status python-app
helm get values python-app                                     # values overridden for this release
helm get manifest python-app                                   # what Helm actually applied

helm upgrade python-app charts/python-app                      # apply chart/values changes
helm upgrade python-app charts/python-app --set image.tag=v2   # roll out a new image
helm upgrade python-app charts/python-app -f values-dev.yaml   # per-environment values

helm history python-app
helm rollback python-app 1                                     # back to revision 1

helm uninstall python-app                                      # removes everything the release created
```

A per-environment values file only lists what differs from `values.yaml`:

```yaml
# values-dev.yaml
replicaCount: 2
httpRoute:
  hostnames:
    - python-app.dev.test.com
```

## 14. Ports: which one to curl

```text
curl :8081 ──► k3d serverlb :80 ──► Traefik Service :80 ──► Traefik pod "web" entrypoint :8000
                                                            (Gateway "web" listener port 8000)
                                                                     │  HTTPRoute (Host: python-app.test.com)
                                                                     ▼
                                                     Service python-app :5000 ──► pod :5000 (Flask)
```

| Port | Where it exists | Reachable from your machine? |
|---|---|---|
| **8081** | Host → k3d load balancer (`-p "8081:80@loadbalancer"`) | **Yes, use this** |
| 8000 | Traefik's `web` entrypoint inside the cluster, which the Gateway listener refers to | No |
| 8080 | Traefik's internal dashboard entrypoint | No |
| 5000 | Service and container port of python-app | No (only via `kubectl port-forward`) |

`python-app.test.com` resolves to `127.0.0.1` through `/etc/hosts`.

## 15. Troubleshooting

### `httproutes ... "python-app" already exists` on install

**Cause:** two templates rendered an HTTPRoute with the same name. `templates/deployment.yaml` had been overwritten with a copy of `httproute.yaml`, so the chart rendered two HTTPRoutes and **no Deployment**. Helm created the first one, failed on the second, and left a `failed` release behind.

The same error also appears when the object was created earlier with `kubectl apply`. Check who owns it:

```bash
kubectl get httproute python-app -o jsonpath='{.metadata.annotations}'
# meta.helm.sh/release-name present → created by Helm; absent → created by kubectl
```

**Fix:**

```bash
helm template python-app charts/python-app | grep -E '^# Source|^kind'   # find the duplicate
# fix the template, then clean up the failed release:
helm uninstall python-app
helm install python-app charts/python-app
```

`--atomic` (Helm 3) or `--rollback-on-failure` (Helm 4) cleans up automatically when an install fails.

### `curl :8081` returns `404 page not found`

The 404 comes from **Traefik**. The request reached Traefik, but no route matched it. The pod and Service were fine. There were two causes, both upstream of the app.

**1. Traefik wasn't acting as a Gateway API controller.**

```text
kubectl get gatewayclass   →  No resources found
kubectl get gateway web    →  PROGRAMMED Unknown ("Waiting for controller")
```

The `HelmChartConfig` from [deploy/traefik-config.yaml](deploy/traefik-config.yaml) wasn't in the cluster. Fix:

```bash
kubectl apply -f deploy/traefik-config.yaml
kubectl -n kube-system rollout status deploy/traefik
kubectl get gatewayclass      # traefik  ACCEPTED=True
kubectl get gateway web       # PROGRAMMED=True
```

**2. The HTTPRoute pointed at a Gateway that doesn't exist.**

`values.yaml` had `httpRoute.gateway.name: http`. That's the **listener** name inside the Gateway, not the Gateway's own name (`web`). Traefik ignores a route whose parent doesn't exist, without logging an error.

How to spot it:

```bash
kubectl get httproute python-app -o jsonpath='{.status}'
# empty → no Gateway has picked up the route

kubectl get gateway web -o jsonpath='{range .status.listeners[*]}{.name} attached={.attachedRoutes}{"\n"}{end}'
# http attached=0 → nothing attached
```

Fix: set `name: web` in values.yaml, **save the file**, then `helm upgrade`. The first two upgrades redeployed `http` because the edit hadn't been saved. Always confirm what Helm will apply:

```bash
helm template python-app charts/python-app | grep -A4 parentRefs
```

### `curl :8000` / `curl :8080` → `Couldn't connect to server`

Those ports exist only inside the cluster. Use `:8081` (see [section 14](#14-ports-which-one-to-curl)).

### Pods never become Ready

Check the probe path. Flask returns 404 on `/`, so probes must use `/api/v1/healthz`:

```bash
kubectl describe pod -l app=python-app | grep -A3 -E 'Readiness|Liveness|Warning'
```

### `strict decoding error: unknown field "spec.selector"` on a Namespace

The first draft of `namespace-python.yaml` put the label under a selector:

```yaml
spec:
  selector:
    matchLabels:
      gateway-access: "true"
```

A Namespace has no selector. Its `spec` only has `finalizers`. A **selector** *chooses* objects (Deployment → pods, Service → pods, Gateway → namespaces). A **label** is what the chosen object *carries*, and it always goes under `metadata.labels`. Here the Gateway holds the selector and the namespace carries the label (see [Moving the release to its own namespace](#moving-the-release-to-its-own-namespace-python)).

Catch this kind of mistake before it reaches the cluster with:

```bash
kubectl apply --dry-run=server -f <file>
```

### Route rejected after moving to another namespace

If `kubectl -n <ns> get httproute python-app -o jsonpath='{.status.parents[0].conditions}'` shows `NotAllowedByListeners`, the namespace is missing the label:

```bash
kubectl get ns <ns> --show-labels
kubectl label namespace <ns> gateway-access=true    # or fix and re-apply the namespace manifest
```

### `Warning: ... missing the kubectl.kubernetes.io/last-applied-configuration annotation`

This appears once when you `kubectl apply` a manifest over an object that was made with `kubectl create`, for example a namespace created by hand first. It's harmless: kubectl adds the annotation it uses to track later changes.

### Quick diagnosis order

Add `-n <namespace>` to every command if the release isn't in your context's default namespace.

1. `helm template ... | grep '^kind'`: does the chart render what you expect?
2. `kubectl get pods -l app=python-app`: Running and `1/1` Ready?
3. `kubectl get endpointslices -l kubernetes.io/service-name=python-app`: does the Service have pod endpoints?
4. `kubectl get gatewayclass` / `kubectl get gateway web`: is the controller working?
5. `kubectl get ns <namespace> --show-labels`: does it carry `gateway-access=true`?
6. `kubectl get httproute python-app -o jsonpath='{.status}'`: `Accepted=True`?
7. `curl http://python-app.test.com:8081/...`

## 16. ConfigMap, HPA and NOTES.txt

Three additions made after the chart was working. All three are driven from values.

### ConfigMap for environment variables

```yaml
# values.yaml
env:
  APP_ENV: dev
  LOG_LEVEL: info
```

```yaml
# templates/configmap.yaml
{{- if .Values.env }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "python-app.name" . }}
  labels:
    {{- include "python-app.labels" . | nindent 4 }}
data:
  {{- range $key, $value := .Values.env }}
  {{ $key }}: {{ $value | quote }}
  {{- end }}
{{- end }}
```

In `deployment.yaml`:

```yaml
  template:
    metadata:
      {{- if .Values.env }}
      annotations:
        # Changes when the ConfigMap changes, so pods restart and pick up new values
        checksum/config: {{ include (print $.Template.BasePath "/configmap.yaml") . | sha256sum }}
      {{- end }}
  ...
          {{- if .Values.env }}
          envFrom:
            - configMapRef:
                name: {{ include "python-app.name" . }}
          {{- end }}
```

- **`range $key, $value`** loops over the map. **`quote`** is needed because ConfigMap values must be strings: `PORT: 5000` or `DEBUG: true` would otherwise be rejected.
- **`envFrom`** turns every key into an environment variable, so adding a variable only means editing values.yaml.
- **The checksum annotation:** environment variables are only read when a container starts. Changing a ConfigMap does **not** restart pods. Putting a hash of the rendered ConfigMap in the pod template means any change to `env` changes the pod template, and the Deployment rolls out new pods.
- **Empty `env`** (`--set env=null` or `env: {}`) skips the ConfigMap, `envFrom` and the annotation.

Check it:

```bash
kubectl exec deploy/python-app -- env | grep -E 'APP_ENV|LOG_LEVEL'
helm upgrade python-app charts/python-app --set env.LOG_LEVEL=debug   # pods roll automatically
```

### Toggleable HorizontalPodAutoscaler

```yaml
# values.yaml
autoscaling:
  enabled: false
  minReplicas: 1
  maxReplicas: 100
  targetCPUUtilizationPercentage: 80
  # targetMemoryUtilizationPercentage: 80
```

```yaml
# templates/hpa.yaml
{{- if .Values.autoscaling.enabled }}
{{- if not (and .Values.resources .Values.resources.requests .Values.resources.requests.cpu) }}
{{- fail "autoscaling.enabled requires resources.requests.cpu: the HPA measures CPU as a percentage of the request" }}
{{- end }}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "python-app.name" . }}
  labels:
    {{- include "python-app.labels" . | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "python-app.name" . }}
  minReplicas: {{ .Values.autoscaling.minReplicas }}
  maxReplicas: {{ .Values.autoscaling.maxReplicas }}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ .Values.autoscaling.targetCPUUtilizationPercentage }}
    {{- with .Values.autoscaling.targetMemoryUtilizationPercentage }}
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: {{ . }}
    {{- end }}
{{- end }}
```

In `deployment.yaml`, `replicas` is left out when the HPA is on:

```yaml
spec:
  {{- if not .Values.autoscaling.enabled }}
  replicas: {{ .Values.replicaCount }}
  {{- end }}
```

- **Why leave out `replicas`:** if the Deployment sets `replicas: 1` while the HPA has scaled to 4, every `helm upgrade` resets it to 1 and the HPA fights back. With the HPA on, the HPA owns the count.
- **Why the `fail` guard:** `averageUtilization: 80` means 80% *of the CPU request*. Without `resources.requests.cpu`, the HPA can't compute it and reports `<unknown>` forever. `fail` stops the install with a clear message instead.
- **Metrics:** the HPA reads from metrics-server, which k3s ships. `kubectl top pods` confirms it works.

Turn it on and watch it:

```bash
helm upgrade python-app charts/python-app --set autoscaling.enabled=true --set autoscaling.maxReplicas=5
kubectl get hpa python-app -w      # TARGETS shows e.g. 4%/80%
```

To see it scale, generate load from inside the cluster:

```bash
kubectl run load --rm -it --image=busybox:1.36 --restart=Never -- \
  sh -c 'while true; do wget -q -O- http://python-app:5000/api/v1/details >/dev/null; done'
```

### NOTES.txt

`templates/NOTES.txt` is rendered like any template, but it's printed to the terminal after `helm install` / `helm upgrade` (and by `helm status`) instead of being applied to the cluster.

```text
{{ .Chart.Name }} {{ .Values.image.tag | default .Chart.AppVersion }} is deployed as release "{{ .Release.Name }}" in namespace "{{ .Release.Namespace }}".
{{- if .Values.autoscaling.enabled }}
Replicas: autoscaled between {{ .Values.autoscaling.minReplicas }} and {{ .Values.autoscaling.maxReplicas }} (target CPU {{ .Values.autoscaling.targetCPUUtilizationPercentage }}%).
{{- else }}
Replicas: {{ .Values.replicaCount }}
{{- end }}

Check the pods:
  kubectl -n {{ .Release.Namespace }} get pods -l app={{ include "python-app.name" . }}

{{- if .Values.httpRoute.enabled }}
{{- $host := first .Values.httpRoute.hostnames }}
{{- $port := .Values.httpRoute.externalPort }}
{{- $base := printf "http://%s%s" $host (ternary "" (printf ":%v" $port) (eq (toString $port) "80")) }}

Check the route is attached to Gateway "{{ .Values.httpRoute.gateway.name }}" (expect Accepted=True):
  kubectl -n {{ .Release.Namespace }} get httproute {{ include "python-app.name" . }} -o jsonpath='{.status.parents[0].conditions}'

Call the API:
  curl {{ $base }}/api/v1/details
  curl {{ $base }}{{ .Values.healthCheckPath }}
{{- else }}

The HTTPRoute is disabled. Reach the API with a port-forward:
  kubectl -n {{ .Release.Namespace }} port-forward svc/{{ include "python-app.name" . }} {{ .Values.service.port }}:{{ .Values.service.port }}
  curl http://localhost:{{ .Values.service.port }}/api/v1/details
{{- end }}
```

- **`httpRoute.externalPort: 8081`** is the host port that reaches the Gateway on this k3d cluster. The cluster has no way of knowing it, so it lives in values and is used only here. Set it to `80` and the port is left out of the URL.
- **`$host := first ...`** stores a value in a variable. **`ternary A B cond`** returns A if `cond` is true, otherwise B.
- With the route disabled, NOTES prints a `port-forward` command instead.

Output after `helm upgrade`:

```text
python-app v1 is deployed as release "python-app" in namespace "default".
Replicas: 1

Check the pods:
  kubectl -n default get pods -l app=python-app

Check the route is attached to Gateway "web" (expect Accepted=True):
  kubectl -n default get httproute python-app -o jsonpath='{.status.parents[0].conditions}'

Call the API:
  curl http://python-app.test.com:8081/api/v1/details
  curl http://python-app.test.com:8081/api/v1/healthz
```

## 17. Next steps

- **Secrets:** a `secret.yaml` alongside the ConfigMap for sensitive values, ideally sourced from an external secret store instead of values.yaml.
- **Resources:** tune `requests`/`limits` once you know the app's real usage (`kubectl top pods`).
- **Gateway chart:** move `gateway.yaml` into its own small platform chart, separate from the apps.
- **Packaging:** `helm package charts/python-app` and push it to an OCI registry (`helm push python-app-0.1.0.tgz oci://...`).
