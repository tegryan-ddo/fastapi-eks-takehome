# fastapi-eks-takehome

Helm deployment of a Python/FastAPI service (`mobile-api`) to an existing
Amazon EKS cluster. The service serves HTTP on port 8000, uses an external
Postgres database, receives payment-provider webhooks, and runs a daily batch
job. Expected load is 2,000–3,000 requests per second at peak and about a tenth
of that overnight.

## Status

| Area | Status |
| --- | --- |
| Smoke-test app and container image | Done; image built and run with Podman 5.7.1 under the chart's restrictions, and the smoke test passes |
| Chart: Deployment, ClusterIP Service, ServiceAccount | Done, linted, rendered, and schema-validated; **not yet installed on a cluster** |
| Staging and production values | Minimal (replica counts only) |
| Verification: chart tests, smoke test, secret scan, validation buildspec | Done and run locally; buildspec not yet run in CodeBuild |
| Delivery and monitoring (AWS CodePipeline, CodeBuild, CloudWatch) | Proposal only: [docs/operations.md](docs/operations.md) |
| Ingress and TLS, secrets, autoscaling, disruption budget, topology spread, daily CronJob | Not started |

Reasons and assumptions are in [docs/decisions.md](docs/decisions.md).

## Layout

```text
charts/mobile-api/            Helm application chart
  Chart.yaml
  values.yaml                 defaults shared by every environment
  templates/                  Deployment, Service, ServiceAccount, _helpers.tpl
environments/staging.yaml     staging overrides
environments/production.yaml  production overrides
examples/demo-api/            smoke-test FastAPI app and its Dockerfile
tests/chart/                  chart lint, schema, and relationship checks (pytest)
scripts/                      pinned tool downloads, smoke test, secret scan
buildspecs/validate.yml       CodeBuild validation build (proposed, not run in AWS)
platform/deploy-rbac.yaml     Kubernetes RBAC for the deploy roles (proposed, not applied)
docs/decisions.md             assumptions and reasons for consequential choices
docs/operations.md            delivery and monitoring proposal (AWS)
docs/verification.md          what each check catches; staging acceptance plan
```

## The demo application

No real application was supplied, so `examples/demo-api/` is a **deployment
smoke-test harness**, not the service. It exists only to prove the image and
chart work end to end. Its readiness endpoint confirms only that the demo has
started. It does not check Postgres, payment processing, or anything the real
application needs.

| Endpoint | Response |
| --- | --- |
| `GET /` | `200 {"service":"demo-api","version":"<APP_VERSION>"}` |
| `GET /health/live` | `200 {"status":"ok"}` while the process can respond |
| `GET /health/ready` | `200 {"status":"ready"}` once startup has completed |

## Prerequisites

- Docker 24 or later. Podman also works; replace `docker` with `podman`.
- Helm 3.x or 4.x. Verified with Helm 3.22.1 and 4.3.1. The verification
  commands below download their own pinned Helm.
- Python 3.12 or later for the verification commands.
- `curl`.
- A POSIX shell (bash, zsh, or Git Bash on Windows). Run commands from the
  repository root.

## Run the container locally

Build the image. `APP_VERSION` is baked into the image and reported by `GET /`.

```sh
docker build --build-arg APP_VERSION=0.1.0 -t demo-api:0.1.0 examples/demo-api
```

Run it with the same restrictions the chart applies: read-only root filesystem
with a writable `/tmp`, no Linux capabilities, and no privilege escalation. The
image already runs as UID 10001, and Docker applies its default seccomp
profile.

```sh
docker run -d --rm --name demo-api -p 8000:8000 \
  --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  demo-api:0.1.0
```

In Git Bash on Windows, run `export MSYS_NO_PATHCONV=1` first. Otherwise
Git Bash rewrites `/tmp` into a Windows path and the container does not start.

Call the endpoints:

```sh
curl -i http://localhost:8000/
curl -i http://localhost:8000/health/live
curl -i http://localhost:8000/health/ready
```

Or run the smoke test, which waits for readiness and checks the response:

```sh
python scripts/smoke_test.py http://localhost:8000 --expect-version 0.1.0
```

Stop the container. It is removed automatically because it was started with
`--rm`.

```sh
docker stop demo-api
```

## Lint and render the chart

```sh
helm lint charts/mobile-api -f environments/staging.yaml
helm lint charts/mobile-api -f environments/production.yaml

helm template mobile-api charts/mobile-api -f environments/staging.yaml
helm template mobile-api charts/mobile-api -f environments/production.yaml
```

The image tag defaults to the chart's `appVersion`. Deployments should set a
tag or, preferably, an immutable digest. When both are set, the digest wins.

```sh
helm template mobile-api charts/mobile-api -f environments/production.yaml \
  --set image.tag=0.1.0

DIGEST=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
helm template mobile-api charts/mobile-api -f environments/production.yaml \
  --set image.digest=$DIGEST
```

## Verify

```sh
python -m pip install -r requirements-dev.txt

# Chart: lint both environments, render, validate against Kubernetes 1.30
# schemas, and check selectors, ports, image reference, security settings,
# and that platform/deploy-rbac.yaml can deploy the chart
python -m pytest

# Secrets: Git history and working-tree files, findings redacted
python scripts/secret_scan.py
```

The first run downloads pinned, checksum-verified Helm, kubeconform, and
gitleaks binaries into `.tools/`. See [docs/verification.md](docs/verification.md)
for what each check catches and the staging acceptance plan.

## How Helm combines values

Helm builds one set of values for each render, from lowest to highest
precedence:

1. `charts/mobile-api/values.yaml`: the chart defaults.
2. Each `-f` file, in order. A later file overrides an earlier one.
3. Each `--set` flag. This is how a pipeline supplies the image tag or digest
   at deploy time.

Maps are merged key by key. `environments/production.yaml` sets only
`replicaCount: 3`, so every other value, such as `image.repository`,
`resources`, and the probes, still comes from `values.yaml`. Lists and scalar
values are replaced whole, not merged. Setting a key to `null` removes the
default.

Environment files therefore hold only what differs per environment, and never
credentials.
