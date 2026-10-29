# Build and deployment guide

This is the detailed reproduction guide for the submission. Start with the
[root README](../README.md) for scope, decisions, and the recorded verification
status. Run commands from the repository root. Examples use synthetic data;
enter real credentials only through the local secrets-loader prompts.

## Prerequisites

- Docker 24 or later, or Podman. Replace `docker` with `podman` if needed.
- Python 3.12 or later for the verification commands.
- Helm 4.3.1 for the deployment command below, matching the repository's
  pinned verification tool. The verification commands download that version.
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
defined by [infra/](../infra/README.md). Enter application credentials only in
trusted local prompts. Authorized CodeBuild jobs retrieve credentials from
Secrets Manager during execution; credentials must not be committed, printed,
or passed as command-line arguments. At the submission snapshot, the database
bootstrap build failed and RDS integration had not been verified. The steps,
in order:

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
[docs/operations.md](operations.md#application-secrets) for the cluster
integration, the restart needed after a change, and the tool's limits.

## Deploy with your own environment values

A successful cluster install has not been verified for this submission snapshot.
Put your environment specifics in a values file of your own:

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
  [docs/operations.md](operations.md#rds-environment));
- metrics-server, which the HPA reads CPU metrics from;
- an IngressClass named `alb` with an ALB controller behind it (EKS Auto Mode
  provides the controller), if the Ingress is enabled;
- the deployer RBAC in `platform/deploy-rbac.yaml`, if you deploy with the
  pipeline's role.

`HELM_DRIVER=configmap` keeps Helm's release history out of Secrets; see
[docs/operations.md](operations.md#2-permissions).

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
[docs/verification.md](verification.md) lists what each check catches and
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

