# Build and deployment guide

Start with the [submission README](../README.md) for decisions and recorded
verification results. Commands below reproduce the local demonstration or
deploy into another environment; they are not prerequisites for reading the
submission. Run commands from the repository root. Use synthetic test data
and enter real credentials only through trusted local prompts.

## Prerequisites

- Docker 24 or later, or Podman. Replace `docker` with `podman` if needed.
- Python 3.12 or later for the verification commands.
- Helm 4.x (verified with 4.3.1). Lint and template also work with Helm 3.x,
  but the deploy command below uses Helm 4 flags. The verification commands
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
  public.ecr.aws/docker/library/postgres:17.11-alpine

docker build --build-arg APP_VERSION=0.1.0 -t demo-api:0.1.0 examples/demo-api

# Same restrictions as the chart: read-only root filesystem, writable /tmp,
# no Linux capabilities, no privilege escalation (the image runs as UID 10001)
run() {
  docker run --rm --network demo --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
    -e DATABASE_URL=postgresql://demo@demo-postgres:5432/mobileapi?sslmode=disable "$@"
}

# PostgreSQL takes a few seconds to accept connections on first start
for i in $(seq 1 30); do docker exec demo-postgres pg_isready -U demo -q && break; sleep 1; done

run demo-api:0.1.0 python -m app.migrate                        # apply the schema first
run -d --name demo-api -p 8000:8000 demo-api:0.1.0              # then the API

curl -i http://localhost:8000/health/ready
curl -i -X POST http://localhost:8000/demo/records -H 'content-type: application/json' -d '{"note":"hello"}'
curl -i http://localhost:8000/demo/records/1
python scripts/smoke_test.py http://localhost:8000 --expect-version 0.1.0 --expect-demo

run demo-api:0.1.0 python -m app.batch --business-date 2026-10-27    # run_count 1
run demo-api:0.1.0 python -m app.batch --business-date 2026-10-27    # same row, run_count 2
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
created by [infra/](../infra/README.md). No database password appears in Git or
passes through an AI agent; the two VPC runners receive credentials from
Secrets Manager at run time, inside the VPC. The steps, in order (all three
were done for staging on 28 October 2026 (Pacific)):

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

   For staging, the operator chose a different route with the same boundary:
   `aws secretsmanager create-secret --name /mobile-api/staging/runtime
   --secret-string file://<local file>` from their own machine, then deleted
   the file. The values never reached Git or the agent. The loader remains the
   designed path because it writes nothing to disk.

2. **Create the application role once.** Start the `mobile-api-db-admin`
   CodeBuild project. It connects as the RDS master user and runs
   `platform/db/create_app_user.sql`, creating `mobile_api_app` with the
   password from the secret you just loaded. The master credentials stay in
   Secrets Manager (`/mobile-api/staging/rds-master`) and are used by nothing
   else.

3. **Migrate and test against RDS.** Start the `mobile-api-integration`
   CodeBuild project. It runs `python -m app.migrate` and then
   `python -m pytest tests/integration` inside the VPC, with `DATABASE_URL`
   injected from the secret. On staging it applied `0001_initial` and passed
   all 6 tests with `sslmode=verify-full`.

Uploading a secret and making it available to pods are separate steps. See
[docs/operations.md](operations.md#application-secrets) for the cluster
integration, the restart needed after a change, and the tool's limits.

## Deploy with your own environment values

This is what `mobile-api-deploy-staging` runs
([buildspecs/deploy-staging.yml](../buildspecs/deploy-staging.yml)); it has
deployed the demo to the staging cluster three times. To deploy elsewhere, put your
environment specifics in a values file of your own:

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

Those are Helm 4 flags; with Helm 3 use `--atomic --wait` instead.

To expose the API, enable the Ingress in the same file with your host and the
ALB annotations shown in `environments/staging.yaml`. TLS terminates at the
ALB with an ACM certificate; the chart holds no certificate.

The cluster needs:

- the namespace;
- the Secret named by `runtimeSecret.name`, **before** the first install: the
  migration hook reads `DATABASE_URL` from it;
- network access from the pods to RDS on port 5432 (see
  [docs/operations.md](operations.md#rds-environment));
- metrics-server, which the HPA reads CPU metrics from. EKS Auto Mode does not
  include it; the staging cluster gets it as a managed add-on from
  [infra/](../infra/README.md);
- an IngressClass named `alb` with an ALB controller behind it (EKS Auto Mode
  provides the controller), if the Ingress is enabled;
- the deployer RBAC in `platform/deploy-rbac.yaml`, if you deploy with the
  pipeline's role. On staging these came from the one-time bootstrap in
  `platform/eks/` plus that file.

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
against RDS, where it has passed. The first run downloads pinned,
checksum-verified Helm, kubeconform, gitleaks, and kubectl binaries into
`.tools/`. [docs/verification.md](verification.md) lists what each check
catches, what ran on staging, and what still needs the real application.

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

