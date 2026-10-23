# python-app

Welcome to the TechDocs home page for **python-app**, a lightweight Flask REST API that reports the current server time and hostname. It is used as a sample service for exploring Kubernetes, Helm, Argo CD and Backstage.

!!! note
    This documentation is a placeholder. Sections marked _TBD_ will be expanded as the service evolves.

## Overview

| Property  | Value                                                            |
| --------- | ---------------------------------------------------------------- |
| Type      | Service                                                          |
| Owner     | `development`                                                    |
| Lifecycle | `experimental`                                                   |
| Language  | Python 3.12 (Flask, Flask-RESTful)                               |
| Port      | `5000`                                                           |
| Source    | [GitHub](https://github.com/JonesKwameOsei/python-api)           |

## API Reference

### `GET /api/v1/info`

Returns the current time, the hostname of the pod serving the request, and a welcome message.

```json
{
  "time": "04:19:09PM on October 22, 2026",
  "hostname": "python-app-7d9f8c6b5d-x2k4q",
  "message": "You are welcome to the python-app! 💝💖",
  "deployed_on": "kubernetes"
}
```

### `GET /api/v1/healthz`

Health check endpoint used by Kubernetes liveness and readiness probes.

```json
{
  "status": "up"
}
```

## Getting Started

### Run locally

```bash
pip install -r requirements.txt
python src/app.py
curl http://localhost:5000/api/v1/info
```

### Run with Docker

```bash
docker build -t python-app .
docker run -p 5000:5000 python-app
```

## Deployment

The service can be deployed to Kubernetes in several ways:

- **Raw manifests**: `deploy/` contains the Deployment, Service, Gateway, HTTPRoute and cert-manager resources.
- **Helm**: `charts/python-app/` packages the app as a Helm chart.
- **Argo CD**: `argocd/python-app.yaml` defines a GitOps Application that syncs the Helm chart using `argocd/values-argo.yaml`.

Traffic is routed through the Gateway API with TLS certificates issued by cert-manager.

## Repository Layout

```text
.
├── src/                 # Flask application source
├── charts/python-app/   # Helm chart
├── deploy/              # Plain Kubernetes manifests
├── argocd/              # Argo CD Application and values
├── docs/                # TechDocs (this site)
├── Dockerfile
├── requirements.txt
└── catalog-info.yaml    # Backstage component definition
```

## Monitoring & Operations

_TBD_: dashboards, alerts and runbooks.

## Support

Questions or issues? Reach out to the **development** team or open an issue on the [GitHub repository](https://github.com/JonesKwameOsei/python-api/issues).
