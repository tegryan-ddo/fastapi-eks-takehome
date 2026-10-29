# Verification

What can be checked today, what each check is meant to catch, and the checks
that still need RDS, a cluster, or the real application.

There are five levels of evidence. Keep them apart:

| Level | Status |
| --- | --- |
| Unit tests (demo app, secrets loader) | Run locally |
| Rendered manifests (lint, schema, relationships) | Run locally |
| Local container (image built and run with Docker or Podman; a PostgreSQL 17 container stands in for RDS) | Run locally |
| RDS (the real database, through the VPC-connected `mobile-api-integration` runner) | **Not yet run.** Pending the operator steps in the README. |
| Kubernetes cluster (install, migration hook, rollout, CronJob, HPA, Ingress) | **Not run.** An EKS Auto Mode cluster is being provisioned by infra/; nothing has run on it yet. |

A local HTTP response does not show that ALB routing, DNS, TLS, or rollout
behaviour work. A passing check against the PostgreSQL container does not show
that RDS networking, TLS verification against the RDS CA, or the application
role's permissions work; those need the RDS level. The demo's endpoints say
nothing about payment processing or the employer's business logic.

## Setup

Python 3.12 or later, network access on the first run, and Docker or Podman for
the container check (it also pulls `public.ecr.aws/docker/library/postgres:17.11-alpine`).

```sh
python -m pip install -r requirements-dev.txt
python scripts/tools.py
```

`scripts/tools.py` downloads pinned releases of Helm (v4.3.1), kubeconform
(v0.8.0), gitleaks (v8.30.1), and kubectl (v1.37.2) into `.tools/`. It checks
each file against a SHA-256 stored in the script before unpacking it. kubeconform also fetches
Kubernetes JSON schemas from GitHub when it runs.

## Checks that run now

| Command | Checks | Fails when |
| --- | --- | --- |
| `python -m pytest` | **Chart:** `helm lint --strict` and rendering for staging and production; kubeconform `-strict` against Kubernetes 1.30 (the `kubeVersion` minimum); the relationships below. **Demo app:** readiness states (`starting`, `database unavailable`, `ready`), demo identity, a clear error without `DATABASE_URL`, no password in any output, batch argument validation and exit codes. **Secrets loader:** 21 offline tests with dummy values and mocked AWS clients. **Integration (`tests/integration`):** only where `DATABASE_URL` points at a reachable, migrated database; a write followed by a read, and a batch run followed by a replay of the same date; it removes the records it created. | Any check below breaks. |
| `python scripts/verify_container.py` | Builds the image, starts a PostgreSQL 17 container, migrates, then runs the API non-root with a read-only root, a tmpfs `/tmp`, no capabilities, and no-new-privileges. The checks are listed under [Container checks](#container-checks-scriptsverify_containerpy). | Image fails to build or start; any endpoint or body is wrong; the record is lost across a container replacement; the batch replay creates a second row; readiness stays up during the outage or stays down after recovery; shutdown is unclean or slow; the batch accepts a bad date; a missing `DATABASE_URL` is not reported clearly. |
| `python scripts/smoke_test.py <base-url>` | A running service, local or through the staging HTTPS hostname: readiness within a deadline, liveness, and the identity returned by `GET /`. | Not ready by the deadline. A non-200 response. The wrong service name, version, or demo flag. Connection, TLS, or timeout errors. |
| `python scripts/secret_scan.py` | gitleaks over **both** Git history (all commits reachable from local refs) and the working-tree files Git would track. Findings are redacted. | Any secret found. A scanner error is reported separately from a clean result. |

`buildspecs/validate.yml` runs `pytest` and the secret scan in CodeBuild on
pull requests; without a database, the integration tests do not run there.
`buildspecs/build.yml` runs those plus the container check, then pushes the
image. Both projects are provisioned by infra/ (CDK) and have run:
`mobile-api-validate` succeeded on a pull-request webhook, and
`mobile-api-build` built, checked (26/26 container checks against PostgreSQL in
Docker), and pushed the image for commit `73de0a8`.

### Relationship checks (`tests/chart/test_chart.py`)

These are relationships a schema validator cannot see:

| Check | Catches |
| --- | --- |
| Service, Deployment, and PDB selectors match the API pods, never the batch or migration pods, include the release and `component: api`, and contain no version or chart labels | Traffic or disruption budgets that include batch or migration pods. A Service routing to no pods. Selectors that change between releases, which would break upgrades. |
| Service `targetPort` and every probe port are named container ports, and `http` is 8000 | A Service that never reaches the container. Probes hitting a closed port. |
| Startup and liveness probes call `/health/live`; readiness calls `/health/ready` | Liveness pointed at the database-aware readiness check, which would restart every pod during a database outage. |
| The API, batch, and migration images are all `repository:appVersion` by default, `:tag` when a tag is set, and `@digest` when a digest is set, even alongside a tag | One workload running a different image. Promotion deploying a mutable tag instead of the verified digest. A migration run with a different schema version than the code. |
| All three workloads: non-root, non-zero UID, `RuntimeDefault` seccomp, no privilege escalation, not privileged, ALL capabilities dropped and none added, read-only root, service-account token not mounted; API and batch use the chart's ServiceAccount, the hook uses the namespace default | Regressions in the security settings chosen in `decisions.md`. |
| All three workloads mount `/tmp` from an `emptyDir`, and have CPU and memory requests plus a memory limit | Nowhere to write temporary files. Unbounded memory. An HPA with nothing to measure against. |
| preStop sleep plus Uvicorn's graceful shutdown (read from the Dockerfile `CMD`) is less than `terminationGracePeriodSeconds` | Pods killed mid-request during rollouts. |
| HPA targets the Deployment, `minReplicas` ≤ `maxReplicas`, and the Deployment omits `replicas` while the HPA is enabled; with autoscaling off, no HPA renders and `api.replicaCount` applies | Each upgrade resetting the HPA's replica count. An HPA scaling nothing. |
| CronJob: suspended in staging only, `Forbid` concurrency, schedule and explicit time zone, the batch command (not the HTTP server), `restartPolicy: Never`, and explicit integer deadlines, retries, and history limits | Staging running batches on a schedule. Overlapping runs. A Job that serves HTTP forever. Unbounded retries or runtime. |
| Migration Job: `helm.sh/hook: pre-install,pre-upgrade`, delete policy `before-hook-creation,hook-succeeded` and not `hook-failed`, the migrate command, `backoffLimit: 0`, `restartPolicy: Never`, explicit deadline and TTL | Migrations running after pods change, or not at all. Failed migration Jobs deleted before anyone reads their logs. Retries of a non-idempotent step. |
| With an empty `runtimeSecret.name`, nothing references a Secret. With the default, the API gets exactly `DATABASE_URL` and `PAYMENTS_WEBHOOK_SECRET`, and the batch and migration only `DATABASE_URL`, all through `secretKeyRef` and never as literal values | Secrets embedded in manifests. A workload receiving keys it does not need. |
| Ingress: nothing renders while `ingress.enabled` is false; enabled, one `Prefix` rule for `/` on the configured host, class `alb`, backend is the chart Service by name and its `http` port by name, and no `spec.tls` | An Ingress pointing at a Service that does not exist, or at the wrong port. TLS configured in two places. An ALB created in environments that did not ask for one. |
| `platform/deploy-rbac.yaml` passes schema validation and grants full management of every kind the chart can render, including the Ingress, plus ConfigMaps | A chart change that would fail in the pipeline with "forbidden". |
| The deployer role has no wildcards, Secrets, pod `exec`, `attach`, or `port-forward`, impersonation, or RBAC rights; it is granted only through namespaced RoleBindings; each environment has its own group | Permission creep. A ClusterRoleBinding granting every namespace. One deploy role able to deploy to both environments. |

Each check was confirmed to fail, with its message, against a deliberately
broken copy of the chart.

### Container checks (`scripts/verify_container.py`)

| Step | Catches |
| --- | --- |
| PostgreSQL 17 container up; `python -m app.migrate` from the image | An image that cannot run the migration command, or migrations that fail on a fresh database |
| API up under the chart's restrictions; smoke test including `"demo": true` | Wrong endpoints or bodies; an image that needs a writable root or capabilities |
| UID 10001, read-only root, writable `/tmp` | Running as root; the read-only filesystem silently off |
| `POST /demo/records`, stop and start a new API container, `GET /demo/records/{id}` | Writes that never reached PostgreSQL. This is a container replacement, not a pod replacement. |
| Batch twice for one date; `GET /demo/batch-runs/{date}` shows `run_count` 1 then 2 and one row | A replay that duplicates rows or fails on conflict |
| Stop PostgreSQL: `/health/live` 200, `/health/ready` 503 `database unavailable`, the API container still running | Liveness that depends on the database (a restart loop); readiness that lies |
| Start PostgreSQL: `/health/ready` 200 again without a restart | A pool that never recovers |
| SIGTERM: exit code 0, lifespan shutdown logged, within the 20s allowance | A server that ignores SIGTERM or hangs past the grace period |
| Batch with an invalid date exits 2; without `DATABASE_URL` the command fails with a clear event | Bad input or configuration accepted silently |

### Smoke-test behaviour

- **Timeouts:** connecting, including the TLS handshake, has its own timeout
  (`--connect-timeout`, default 3s). Each wait for response data has another
  (`--response-timeout`, default 5s).
- **Readiness:** polled until `--ready-deadline` (default 60s). Every failed
  attempt is printed. The last attempt may finish up to one connect and one
  response timeout after the deadline.
- **Liveness and `GET /`:** each is tried once.
- **Output:** names the base URL and each path tested, then prints a final
  `PASSED` or `FAILED`. Exit code 0 means passed, 1 means failed.
- **HTTPS certificates:** verified.
- **`--expect-demo`:** applies to the demo image only. The real service's
  identity is checked with `--expect-service` and `--expect-version`.

## Checks against RDS

None has run. All of them wait for the operator steps in the README: load the
secret, start `mobile-api-db-admin` once, then start `mobile-api-integration`.

| Check | How | Status |
| --- | --- | --- |
| Migration over TLS (`sslmode=verify-full`) against RDS as `mobile_api_app` | `mobile-api-integration` runner: `python -m app.migrate` | Pending |
| Write then read through the application's database code | `tests/integration` in the runner | Pending |
| Batch run and replay for one business date: one `batch_runs` row, `run_count` 2 | `tests/integration` in the runner | Pending |
| Second run of the migration is a no-op | Start the runner again | Pending |
| Record survives a pod replacement | Needs a cluster: `POST`, `kubectl rollout restart`, `GET` | Pending: needs cluster |
| Readiness fails, liveness holds, no restarts, automatic recovery, against RDS | Needs a cluster: remove and restore the 5432 security-group rule | Pending: needs cluster |

## Manual staging batch run

The CronJob `mobile-api-batch` is deployed in staging with `suspend: true`, so
nothing runs on a schedule. Before promotion, run one Job from its current
template:

```sh
NS=mobile-api-staging
JOB=mobile-api-batch-manual-$(date -u +%Y%m%d%H%M%S)

# 0. Make sure no batch Job is already running (see the note on concurrency below)
kubectl get jobs -n "$NS" -l app.kubernetes.io/component=batch

# 1. Create a uniquely named Job from the deployed CronJob template
kubectl create job "$JOB" --from=cronjob/mobile-api-batch -n "$NS"

# 2. Wait for completion, bounded. Exits 0 once the Job is Complete. A failed
#    Job is not detected early: the wait runs to its timeout and exits non-zero.
kubectl wait --for=condition=complete "job/$JOB" -n "$NS" --timeout=10m

# 3. Inspect logs, and diagnose a failure
kubectl logs "job/$JOB" -n "$NS" --timestamps
kubectl describe "job/$JOB" -n "$NS"     # conditions: BackoffLimitExceeded, DeadlineExceeded, PodFailurePolicy
kubectl get pods -n "$NS" -l batch.kubernetes.io/job-name="$JOB"
kubectl get events -n "$NS" --field-selector involvedObject.name="$JOB"

# 4. Verify which image the Job used: requested reference, then resolved digest
kubectl get job "$JOB" -n "$NS" -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
kubectl get pods -n "$NS" -l batch.kubernetes.io/job-name="$JOB" \
  -o jsonpath='{range .items[*]}{.status.containerStatuses[0].imageID}{"\n"}{end}'
```

The image must match the release digest in `release.json`. For the demo,
success means the logs show `batch_started` then `batch_completed` with
`"demo": true`, `record_count`, and `run_count`, and the Job condition is
`Complete`. Run a second Job for the same business date: it must complete, log
`run_count` one higher, and `GET /demo/batch-runs/<date>` through the staging
hostname must return a single row. That shows the image, the command, and the
demo's replay work. It says nothing about the real job's business output.

To run a specific business date, write the Job manifest out, add the argument,
then create it:

```sh
kubectl create job "$JOB" --from=cronjob/mobile-api-batch -n "$NS" --dry-run=client -o yaml > job.yaml
# In job.yaml, under the batch container, add:  args: ["--business-date", "2026-10-27"]
kubectl create -f job.yaml
```

**Concurrency.** `concurrencyPolicy: Forbid` applies only to Jobs the CronJob
controller creates. A manual Job is not checked against it, so a scheduled run
and a manual one can overlap (in production, where the CronJob is not
suspended). Check for an active Job first. The demo's upsert makes an overlap
harmless; the real job needs its own replay safety or locking.

**Template changes.** Changing the CronJob (a new image digest, for example)
affects only Jobs created afterwards. A Job that is already running keeps the
template it was created with. `kubectl create job --from` copies the CronJob as
deployed at that moment.

**Retention.** `ttlSecondsAfterFinished` (7 days) removes finished Jobs,
including manual ones. Copy any logs you need before then.

The deployer role allows creating and reading Jobs and reading pod logs, so the
pipeline's verify-staging step can run these commands.

## Staging acceptance plan

None of these has been executed. Scenarios 1, 4, 6, and 7 can run with the demo
once a cluster exists. Scenarios 2 and 3 need the real application and the
payment-provider contract. Scenario 5 needs a deliberately slow endpoint, which
the demo lacks.

| # | Scenario | Action | Assertions | Status |
| --- | --- | --- | --- | --- |
| 1 | Real database read and write | `POST /demo/records` with a unique note through the staging hostname; `GET /demo/records/{id}`; `kubectl rollout restart deployment/mobile-api` and wait; the same `GET` again. | `201`, then the same record twice, including after every pod was replaced. The database pool wait metric stays below its threshold. The record is synthetic and is removed afterwards through the integration runner, not the API. For the real application, the same against its own endpoints and schema. | Implementable with the demo. Unexecuted: needs cluster |
| 2 | Duplicate webhook | Send one correctly signed test event (provider sandbox, or a locally signed payload with the staging secret) twice. | Both deliveries get the status the provider expects. Exactly one business effect, for example one payment-state transition. The second delivery is recorded as a duplicate. The completion metric increases by one. | Unexecuted: needs real app and provider contract |
| 3 | Invalid-signature webhook | Send the same event with a tampered body, and again signed with the wrong secret. | Rejected with the agreed 4xx. No state change. The signature-rejected metric increases. The payload and signature are not logged. | Unexecuted: needs real app and provider contract |
| 4 | Batch output and replay for the same business period | Run a manual Job for period P (above), then another for P. | Both Jobs complete with the release digest. `GET /demo/batch-runs/P` returns one row with `run_count: 2` and the same `record_count`; the second Job logs the replay. For the real application: the expected business output for P, and no duplicate output after the replay. | Implementable with the demo. Unexecuted: needs cluster |
| 5 | Rollout with requests in flight | Hold steady load, including requests longer than 5s, on the staging HTTPS hostname while `helm upgrade` replaces every pod. | No 5xx and no connection errors. Every slow request completes. Ready replicas never drop below the current count. | Unexecuted: needs cluster and slow endpoint |
| 6 | Unhealthy replacement | Deploy a release whose readiness can never pass, for example `--set api.readinessProbe.httpGet.path=/not-ready`. | New pods stay unready and receive no traffic. Old pods keep serving, so the smoke test passes throughout. `helm upgrade --rollback-on-failure` fails at its timeout and restores the previous revision. The pipeline stage fails. | Unexecuted: needs cluster |
| 7 | Database outage and recovery | Remove the security-group rule that allows the pods to reach RDS on 5432; after a few minutes, restore it. | Within about 10s every pod is NotReady with reason `database unavailable`, the Service has no endpoints, and `/health/live` still returns 200 from inside the cluster. Restart counts do not change. Within about 10s of the rule returning, every pod is Ready again without a restart. | Implementable with the demo. Unexecuted: needs cluster |
