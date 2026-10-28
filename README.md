# fastapi-eks-takehome

Helm deployment of a Python/FastAPI service (`mobile-api`) to an existing
Amazon EKS cluster. The service serves HTTP on port 8000, uses an external
PostgreSQL database, receives payment-provider webhooks, and runs a daily batch
job. Expected load is 2,000–3,000 requests per second at peak and about a tenth
of that overnight.

The demonstration includes real PostgreSQL persistence through Amazon RDS. The
RDS instance is provisioned by [infra/](infra/README.md) only to reproduce the
external database dependency for this exercise. The Helm chart consumes that
dependency; it does not run PostgreSQL inside Kubernetes.

> **Demo application assumption.** Only the assignment brief was supplied. The
> included FastAPI application demonstrates container startup, health checks,
> shutdown, PostgreSQL reads and writes, schema migration, and batch execution.
> The payment-provider integration, the employer's business logic, and
> production throughput remain unverified.

> **HPA assumption.** CPU utilization is the initial HPA signal. Before
> production, confirm or refine that choice with the application team and
> representative load tests. Database or I/O waits may increase latency without
> increasing CPU. Replica limits also need to respect the database connection
> budget: each pod holds up to 5 connections.

> **Manual staging job validation.** Automatic batch scheduling is suspended in
> staging. Before promotion, a Job is created manually from the deployed CronJob
> template and its result inspected (see
> [docs/verification.md](docs/verification.md#manual-staging-batch-run)). The
> demo batch writes one `batch_runs` row per business date and increments its
> `run_count` on replay. For the real application, acceptance also requires the
> expected business output and a safe replay of the same business period.

## Status

| Area | Status |
| --- | --- |
| Demo application: HTTP API with PostgreSQL persistence, migration command, batch command | Done; unit-tested, and checked locally against a PostgreSQL 17 container |
| Container image | Built and checked locally with Podman under the chart's restrictions |
| Chart: Deployment, Service, ServiceAccount, HPA, PDB, CronJob, migration hook Job, secret references | Linted, rendered, schema-validated, and relationship-checked; **not installed on a cluster** |
| Staging database: VPC, RDS PostgreSQL 17, VPC-connected db-admin and integration runners | Provisioned by [infra/](infra/README.md) (CDK); **integration tests against RDS pending the operator steps below** |
| Build path: ECR repository, CodeConnections, CodeBuild validate and build projects | Provisioned by [infra/](infra/README.md) (CDK) |
| Staging cluster: EKS Auto Mode `mobile-api-staging` in the RDS VPC, `mobile-api-deploy-staging` (runs Helm), `mobile-api-secrets-sync` (writes the Kubernetes Secret `mobile-api-runtime` from Secrets Manager) | Being provisioned by [infra/](infra/README.md) (CDK); in progress, nothing deployed to it yet |
| Ingress (ALB) | In the chart, off by default; the host and ACM certificate are supplied at deploy time; **not tested on a cluster** |
| Secrets loader (`scripts/load_secrets.py`) | Included; offline tests pass with dummy values and mocked AWS clients |
| Delivery pipeline, production deploy project, monitoring | Written proposal: [docs/operations.md](docs/operations.md) |
| Topology spread, production database | Not started |

Reasons and assumptions are in [docs/decisions.md](docs/decisions.md).

## Layout

```text
charts/mobile-api/            Helm application chart
  values.yaml                 defaults shared by every environment
  templates/                  Deployment, Service, ServiceAccount, HPA, PDB, CronJob,
                              migration hook Job, Ingress (off by default)
environments/staging.yaml     staging overrides (CronJob suspended)
environments/production.yaml  production overrides (HPA range)
examples/demo-api/            demo FastAPI app (package app/, app/migrations/) and Dockerfile
infra/                        CDK: ECR, CodeConnections, CodeBuild, VPC, RDS, VPC runners
platform/deploy-rbac.yaml     Kubernetes RBAC for the deploy roles (proposed, not applied)
platform/db/                  SQL run once by the db-admin runner to create the app role
scripts/                      container check, smoke test, secret scan, secrets loader,
                              pinned tool downloads
tests/                        chart, demo app, integration, and secrets-loader tests
buildspecs/validate.yml       CodeBuild validation build (pull requests)
buildspecs/build.yml          CodeBuild release build (pushes to main; pushes the image)
docs/decisions.md             assumptions and reasons for consequential choices
docs/operations.md            delivery, secrets, RDS, and monitoring design (AWS)
docs/verification.md          what each check catches; manual batch run; acceptance plan
```

## The demo application

| Command or endpoint | Behaviour |
| --- | --- |
| `GET /` | `200 {"service":"demo-api","version":"<APP_VERSION>","demo":true}` |
| `GET /health/live` | `200 {"status":"ok"}` whenever the process can answer. Never touches the database. |
| `GET /health/ready` | `200 {"status":"ready"}` only after lifespan startup **and** a pooled `SELECT 1` that completes within 1.5s. Otherwise `503 {"status":"not ready","reason":"starting"\|"database unavailable"}`. It checks connectivity only, not payments or business data. |
| `POST /demo/records {"note": "..."}` | `201` with the stored record. Synthetic demonstration data, up to 200 characters. |
| `GET /demo/records/{id}`, `GET /demo/records?limit=N` | Read records back from PostgreSQL. |
| `GET /demo/batch-runs/{business_date}` | The batch summary row for that date: `record_count`, `run_count`, first and last run time. |
| `python -m app.migrate` | Applies `app/migrations/NNNN_*.sql` in order, once each, tracked in `schema_migrations` under an advisory lock. Idempotent; exits 1 on failure. Run before the application, never from API pods. |
| `python -m app.batch [--business-date YYYY-MM-DD]` | Counts `demo_records` and upserts one `batch_runs` row for the date in one transaction. Replaying the same date updates that row and increments `run_count`. Exits 0, 2 for bad arguments, 1 for a configuration or database failure. Default date: current UTC date (a demo default). |

Every command needs `DATABASE_URL`
(`postgresql://user:password@host:5432/mobileapi?sslmode=verify-full`). Without
it, or with an unreachable database at migrate or batch time, the command logs
a clear error and exits non-zero. There is no in-memory fallback. The API
starts without the database and reports not ready until it can reach it. TLS
is verified against the RDS global certificate bundle baked into the image;
`sslmode=disable` is for local throwaway databases only.

The image runs one Uvicorn worker on `0.0.0.0:8000` as UID 10001. Logs are JSON
lines on stdout (errors on stderr) and never contain credentials, the database
URL, environment dumps, or request bodies.

## Prerequisites

- Docker 24 or later, or Podman. Replace `docker` with `podman` if needed.
- Python 3.12 or later for the verification commands.
- Helm 3.x or 4.x for the manual chart commands. The verification commands
  download their own pinned Helm.
- `curl`, and a POSIX shell (bash, zsh, or Git Bash on Windows). Run commands
  from the repository root. In Git Bash, run `export MSYS_NO_PATHCONV=1` first,
  or it rewrites `/tmp` in the `docker run` commands into a Windows path.

## Build and run locally

A throwaway PostgreSQL container stands in for RDS. It trusts local connections
and holds no real data, which is why `sslmode=disable` is acceptable here.

```sh
docker network create demo
docker run -d --rm --name demo-postgres --network demo \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_USER=demo -e POSTGRES_DB=mobileapi \
  postgres:17-alpine

docker build --build-arg APP_VERSION=0.1.0 -t demo-api:0.1.0 examples/demo-api

# Same restrictions as the chart: read-only root filesystem, writable /tmp,
# no Linux capabilities, no privilege escalation (the image runs as UID 10001)
RUN="docker run --rm --network demo --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  -e DATABASE_URL=postgresql://demo@demo-postgres:5432/mobileapi?sslmode=disable"

$RUN demo-api:0.1.0 python -m app.migrate                       # apply the schema first
$RUN -d --name demo-api -p 8000:8000 demo-api:0.1.0             # then the API

curl -i http://localhost:8000/health/ready
curl -i -X POST http://localhost:8000/demo/records -H 'content-type: application/json' -d '{"note":"hello"}'
curl -i http://localhost:8000/demo/records/1
python scripts/smoke_test.py http://localhost:8000 --expect-version 0.1.0 --expect-demo

$RUN demo-api:0.1.0 python -m app.batch --business-date 2026-10-27   # run_count 1
$RUN demo-api:0.1.0 python -m app.batch --business-date 2026-10-27   # same row, run_count 2
curl -i http://localhost:8000/demo/batch-runs/2026-10-27

docker stop demo-api demo-postgres   # SIGTERM; both are removed because of --rm
docker network rm demo
```

`python scripts/verify_container.py` performs all of the above as one check,
and also replaces the API container between the write and the read, stops and
restarts the database to confirm readiness fails and recovers while liveness
stays up, times the SIGTERM shutdown, and checks invalid batch input and a
missing `DATABASE_URL`.

## Render the chart

```sh
helm lint charts/mobile-api -f environments/staging.yaml
helm lint charts/mobile-api -f environments/production.yaml

helm template mobile-api charts/mobile-api -f environments/staging.yaml
helm template mobile-api charts/mobile-api -f environments/production.yaml
```

The output includes the migration hook Job, which Helm runs before the other
resources on install and upgrade. The image tag defaults to the chart's
`appVersion`. A deployment should pin an immutable digest, which then takes
precedence over any tag for the API, the batch job, and the migration Job:

```sh
DIGEST=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
helm template mobile-api charts/mobile-api -f environments/production.yaml \
  --set image.digest=$DIGEST
```

## Set up the staging database and secrets (operator)

The staging RDS instance, its VPC, and two VPC-connected CodeBuild runners are
created by [infra/](infra/README.md). Nothing in this conversation, in CI, or in
Git ever holds a database password. The steps, in order:

1. **Load the application secret** from your own trusted terminal. The
   database password is one you choose for the application role; type it only
   at the hidden prompts.

   ```sh
   python -m pip install -r requirements-secrets.txt
   aws sso login --profile <profile>
   python scripts/load_secrets.py --profile <profile> --region us-east-1 \
     --expected-account-id <account-id> --secret-name /mobile-api/staging/runtime
   # DATABASE_URL: postgresql://mobile_api_app:<password>@<rds-endpoint>:5432/mobileapi?sslmode=verify-full
   # PAYMENTS_WEBHOOK_SECRET: any value for the demo
   ```

2. **Create the application role once.** Start the `mobile-api-db-admin`
   CodeBuild project. It connects as the RDS master user and runs
   `platform/db/create_app_user.sql`, creating `mobile_api_app` with the
   password from the secret you just loaded. The master credentials stay in
   Secrets Manager (`/mobile-api/staging/rds-master`) and are used by nothing
   else.

3. **Migrate and test against RDS.** Start the `mobile-api-integration`
   CodeBuild project. It runs `python -m app.migrate` and then
   `python -m pytest tests/integration` inside the VPC, with `DATABASE_URL`
   injected from the secret.

Uploading a secret and making it available to pods are separate steps. See
[docs/operations.md](docs/operations.md#application-secrets) for the cluster
integration, the restart needed after a change, and the tool's limits.

## Deploy with your own environment values

Not yet run against a cluster. Put your environment specifics in a values file
of your own:

```yaml
# my-staging.yaml
image:
  repository: <account>.dkr.ecr.<region>.amazonaws.com/mobile-api
runtimeSecret:
  # The Kubernetes Secret your cluster's secrets integration creates from
  # /mobile-api/staging/runtime. Required: every workload reads DATABASE_URL.
  name: mobile-api-runtime
```

```sh
HELM_DRIVER=configmap helm upgrade --install mobile-api charts/mobile-api \
  --namespace mobile-api-staging \
  -f environments/staging.yaml -f my-staging.yaml \
  --set image.digest=$DIGEST \
  --rollback-on-failure --wait=watcher --timeout 10m
```

To expose the API, enable the Ingress in the same file with your host and the
ALB annotations shown in `environments/staging.yaml`. TLS terminates at the
ALB with an ACM certificate; the chart holds no certificate.

The cluster needs:

- the namespace;
- the Secret named by `runtimeSecret.name`, **before** the first install: the
  migration hook reads `DATABASE_URL` from it;
- network access from the pods to RDS on port 5432 (see
  [docs/operations.md](docs/operations.md#rds-environment));
- metrics-server, which the HPA reads CPU metrics from;
- an IngressClass named `alb` with an ALB controller behind it (EKS Auto Mode
  provides the controller), if the Ingress is enabled;
- the deployer RBAC in `platform/deploy-rbac.yaml`, if you deploy with the
  pipeline's role.

`HELM_DRIVER=configmap` keeps Helm's release history out of Secrets; see
[docs/operations.md](docs/operations.md#2-permissions).

## Verify

```sh
python -m pip install -r requirements-dev.txt

python -m pytest                     # chart, demo app, and secrets-loader tests
python scripts/verify_container.py   # build and check the image with a PostgreSQL container
python scripts/secret_scan.py        # Git history and working tree, redacted
```

`tests/integration` runs where `DATABASE_URL` points at a reachable database:
locally against the container above, or in the `mobile-api-integration` runner
against RDS. The first run downloads pinned, checksum-verified Helm,
kubeconform, and gitleaks binaries into `.tools/`.
[docs/verification.md](docs/verification.md) lists what each check catches and
what still needs RDS, a cluster, or the real application.

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
