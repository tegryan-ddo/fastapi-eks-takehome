# fastapi-eks-takehome

Helm deployment of a Python/FastAPI service (`mobile-api`) to an existing
Amazon EKS cluster. The service serves HTTP on port 8000, uses an external
Postgres database, receives payment-provider webhooks, and runs a daily batch
job. Expected load is 2,000–3,000 requests per second at peak and about a tenth
of that overnight.

> **Demo application assumption.** Only the assignment brief was supplied. The
> included FastAPI application demonstrates container startup, health checks,
> shutdown, and batch execution. Postgres access, payment-provider integration,
> real batch behaviour, and production throughput remain unverified.

> **HPA assumption.** CPU utilization is the initial HPA signal. Before
> production, confirm or refine that choice with the application team and
> representative load tests. Database or I/O waits may increase latency without
> increasing CPU. Replica limits also need to respect the database connection
> budget.

> **Manual staging job validation.** Automatic batch scheduling is suspended in
> staging. Before promotion, a Job is created manually from the deployed CronJob
> template and its result inspected (see
> [docs/verification.md](docs/verification.md#manual-staging-batch-run)). For
> the real application, acceptance also requires the expected business output
> and a safe replay of the same business period.

## Status

| Area | Status |
| --- | --- |
| Demo application (`examples/demo-api`): HTTP API and batch command | Done; unit-tested |
| Container image | Built and checked locally with Podman under the chart's restrictions |
| Chart: Deployment, Service, ServiceAccount, HPA, PDB, CronJob, secret references | Linted, rendered, schema-validated, and relationship-checked; **not installed on a cluster** |
| Secrets loader (`scripts/load_secrets.py`) | Included; offline tests pass with dummy values and mocked AWS clients; never run against AWS |
| Verification commands and CodeBuild validation buildspec | Run locally; the buildspec has not run in CodeBuild |
| Delivery and monitoring (CodePipeline, CodeBuild, CloudWatch) | Written proposal: [docs/operations.md](docs/operations.md) |
| Ingress and TLS, topology spread, database migrations | Not started |

Reasons and assumptions are in [docs/decisions.md](docs/decisions.md).

## Layout

```text
charts/mobile-api/            Helm application chart
  values.yaml                 defaults shared by every environment
  templates/                  Deployment, Service, ServiceAccount, HPA, PDB, CronJob
environments/staging.yaml     staging overrides (CronJob suspended)
environments/production.yaml  production overrides (HPA range)
examples/demo-api/            demo FastAPI app (package app/) and its Dockerfile
platform/deploy-rbac.yaml     Kubernetes RBAC for the deploy roles (proposed, not applied)
scripts/                      container check, smoke test, secret scan, secrets loader,
                              pinned tool downloads
tests/                        chart, demo app, and secrets-loader tests (pytest)
buildspecs/validate.yml       CodeBuild validation build (proposed, not run in AWS)
docs/decisions.md             assumptions and reasons for consequential choices
docs/operations.md            delivery, secrets, and monitoring design (AWS)
docs/verification.md          what each check catches; manual batch run; acceptance plan
```

## The demo application

| Command or endpoint | Behaviour |
| --- | --- |
| `GET /` | `200 {"service":"demo-api","version":"<APP_VERSION>","demo":true}` |
| `GET /health/live` | `200 {"status":"ok"}` whenever the process can answer. No dependency calls. |
| `GET /health/ready` | `200 {"status":"ready"}` only between the end of FastAPI lifespan startup and the start of lifespan shutdown, otherwise `503`. It checks nothing else: not Postgres, not payments. |
| `python -m app.batch [--business-date YYYY-MM-DD]` | Logs `batch_started` and `batch_completed` JSON events and exits 0, with no external side effects. With no date it uses the current UTC date (a demo default). An invalid date logs `batch_rejected` and exits 2. |

The image runs one Uvicorn worker on `0.0.0.0:8000` as UID 10001. Logs are JSON
lines on stdout (errors on stderr) and contain no credentials, environment
dumps, or request bodies. The demo needs no database credentials or
payment-provider secret.

## Prerequisites

- Docker 24 or later, or Podman. Replace `docker` with `podman` if needed.
- Python 3.12 or later for the verification commands.
- Helm 3.x or 4.x for the manual chart commands. The verification commands
  download their own pinned Helm.
- `curl`, and a POSIX shell (bash, zsh, or Git Bash on Windows). Run commands
  from the repository root. In Git Bash, run `export MSYS_NO_PATHCONV=1` first,
  or it rewrites `/tmp` in the `docker run` commands into a Windows path.

## Build and run locally

```sh
docker build --build-arg APP_VERSION=0.1.0 -t demo-api:0.1.0 examples/demo-api

# Same restrictions as the chart: read-only root filesystem, writable /tmp,
# no Linux capabilities, no privilege escalation (the image runs as UID 10001)
docker run -d --rm --name demo-api -p 8000:8000 \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  demo-api:0.1.0

curl -i http://localhost:8000/
curl -i http://localhost:8000/health/live
curl -i http://localhost:8000/health/ready
python scripts/smoke_test.py http://localhost:8000 --expect-version 0.1.0 --expect-demo

docker stop demo-api   # SIGTERM; the container is removed because of --rm
```

Run the batch command from the same image, as the CronJob does:

```sh
docker run --rm --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  demo-api:0.1.0 python -m app.batch --business-date 2026-10-27
```

`python scripts/verify_container.py` performs all of the above as one check,
including SIGTERM timing and invalid batch input.

## Render the chart

```sh
helm lint charts/mobile-api -f environments/staging.yaml
helm lint charts/mobile-api -f environments/production.yaml

helm template mobile-api charts/mobile-api -f environments/staging.yaml
helm template mobile-api charts/mobile-api -f environments/production.yaml
```

The image tag defaults to the chart's `appVersion`. A deployment should pin an
immutable digest, which then takes precedence over any tag for both the API and
the batch job:

```sh
DIGEST=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
helm template mobile-api charts/mobile-api -f environments/production.yaml \
  --set image.digest=$DIGEST
```

## Deploy with your own environment values

Not yet run against a cluster. Put your environment specifics in a values file
of your own:

```yaml
# my-staging.yaml
image:
  repository: <account>.dkr.ecr.<region>.amazonaws.com/mobile-api
runtimeSecret:
  # Omit for the demo, which needs no secrets. For the real service: the
  # Kubernetes Secret your cluster's secrets integration creates.
  name: mobile-api-runtime
```

```sh
HELM_DRIVER=configmap helm upgrade --install mobile-api charts/mobile-api \
  --namespace mobile-api-staging \
  -f environments/staging.yaml -f my-staging.yaml \
  --set image.digest=$DIGEST \
  --rollback-on-failure --wait=watcher --timeout 10m
```

The cluster needs:

- the namespace;
- metrics-server, which the HPA reads CPU metrics from;
- the deployer RBAC in `platform/deploy-rbac.yaml`, if you deploy with the
  pipeline's role;
- if `runtimeSecret.name` is set, that Secret. A pod referencing a missing
  Secret will not start.

`HELM_DRIVER=configmap` keeps Helm's release history out of Secrets; see
[docs/operations.md](docs/operations.md#2-permissions).

## Load application secrets (operator only)

`scripts/load_secrets.py` writes `DATABASE_URL` and `PAYMENTS_WEBHOOK_SECRET` to
AWS Secrets Manager. Values are entered at hidden prompts and never appear in
arguments or logs. Run it yourself in a trusted local terminal, never in CI or
an AI agent's terminal:

```sh
python -m pip install -r requirements-secrets.txt
aws sso login --profile <profile>
python scripts/load_secrets.py --profile <profile> --region <region> \
  --expected-account-id <account-id> --secret-name /mobile-api/staging/runtime
```

Uploading a secret and making it available to pods are separate steps. See
[docs/operations.md](docs/operations.md#application-secrets) for the cluster
integration, the restart needed after a change, permissions, and the tool's
limits.

## Verify

```sh
python -m pip install -r requirements-dev.txt

python -m pytest                     # chart, demo app, and secrets-loader tests
python scripts/verify_container.py   # build and check the image (Docker or Podman)
python scripts/secret_scan.py        # Git history and working tree, redacted
```

The first run downloads pinned, checksum-verified Helm, kubeconform, and
gitleaks binaries into `.tools/`. [docs/verification.md](docs/verification.md)
lists what each check catches and what still needs a cluster or the real
application.

## How Helm combines values

Helm builds one set of values for each render, from lowest to highest
precedence:

1. `charts/mobile-api/values.yaml`: the chart defaults.
2. Each `-f` file, in order. A later file overrides an earlier one.
3. Each `--set` flag. This is how a pipeline supplies the image digest at
   deploy time.

Maps are merged key by key. `environments/production.yaml` sets only
`autoscaling.minReplicas` and `autoscaling.maxReplicas`, so
`autoscaling.targetCPUUtilizationPercentage`, the probes, and everything else
still come from `values.yaml`. Lists and scalar values are replaced whole.
Setting a key to `null` removes the default. Environment files hold only what
differs per environment, and never credentials.
