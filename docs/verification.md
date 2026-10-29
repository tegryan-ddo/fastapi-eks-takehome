# Verification

What has been checked, what each check is meant to catch, and the remaining
staging and real-application acceptance checks.

## Submission v2 evidence

Chart and delivery source: `643dbb755d4c18db2b12986c204c7fc0e28eb1d8`.
Additional build-agent verification is recorded at
`d3fcc92b452c07174bbba0b6be6bed31818f44fb` and reconciled below.
These AWS build statuses were reported for the revisions shown; they are not
all runs of the same revision.

| Build | Source revision | Reported status | Build ID |
| --- | --- | --- | --- |
| Validation | `643dbb7` | Succeeded | `cb87b2e3-5aab-40ef-af95-9f7de6ae640f` |
| Staging deployment | `643dbb7` | Succeeded | `f0526314-041d-423e-b294-8a67c39dae4f` |
| Image build | `61fb83d` | Succeeded | `6b1a85ac-3fda-44f2-97b2-cd44c9a68dbd` |
| Main release build | `5d4e0d2` | Succeeded | `6441bd9f-ec0e-4595-8b4f-b2e813e4c0eb` |
| Database bootstrap | `73de0a8` | Succeeded | `6f6c2137-f44a-4457-8706-53282bbf7e0a` |
| Secrets sync | `73de0a8` | Succeeded | `1de29198-37ef-41a8-bc68-9a2a9855dbad` |
| RDS integration | `73de0a8` | Succeeded | `d3bd2a66-e146-4c60-a4a8-800b0a0c0da0` |

The later main release build is for merge commit
`5d4e0d2b60a8c7dd163f7cfc0d0647cd2e1e0c6e`. Its success does not establish
deployment of that image; the independently observed live version below is
`0.1.0-61fb83d`.

Raw build logs and JUnit reports were not available for this snapshot. These
are overall build receipts, configured checks, and the build agent's recorded
run results below, not independently reviewed individual log/JUnit results.
The earlier integration run
is evidence at `73de0a8`, not a claim that its suite ran again at `643dbb7`.
See the [README](../README.md) for the submission scope.

### Independently observed public HTTPS checks

On 28 October 2026 (America/Vancouver), read-only requests to
[the staging hostname](https://mobile-api-staging.digitaldevops.io) succeeded
with TLS certificate verification enabled:

| Path | HTTP status | Response |
| --- | --- | --- |
| `/` | 200 | `{"service":"demo-api","version":"0.1.0-61fb83d","demo":true}` |
| `/health/ready` | 200 | `{"status":"ready"}` |
| `/health/live` | 200 | `{"status":"ok"}` |
| `/demo/records/5` | 200 | `{"id":5,"note":"staging check before rollout to 61fb83d","created_at":"2026-10-29T00:50:11.823612+00:00"}` |
| `/demo/batch-runs/2026-10-29` | 200 | `{"business_date":"2026-10-29","record_count":3,"run_count":2,"first_run_at":"2026-10-29T00:48:35.933635+00:00","last_run_at":"2026-10-29T01:00:32.624863+00:00"}` |

These establish public HTTPS reachability, reported application identity,
health, and the currently persisted record and batch summary. They do not
independently reconstruct the earlier writes, before/after-upgrade sequence,
or Job executions, and do not demonstrate sustained capacity or rollout
continuity. The reported version is not an image digest. The staging build
compares runtime image IDs with its resolved ECR digest, but the exact digest
was not captured in this record.

### Additional results recorded by the build agent

The verification update at `d3fcc92` records the following staging work on
28 October 2026 Pacific (29 October UTC). These details are attributed to that
record; its underlying run logs were not independently reviewed here.
The current record and summary were separately read through HTTPS above;
the historical sequence remains attributed to the build agent.

Record 5 was written through the public HTTPS API before the upgrade to
`61fb83d` and read back after the upgrade replaced the API pods. The record
also describes three Helm revisions: the initial install and two upgrades,
the second retaining the same image. This supports demo persistence across that upgrade; it does not
measure request errors during the rollout, and pool-wait instrumentation is
still absent.

Two manual batch Jobs ran for UTC business date `2026-10-29`. The first
recorded `record_count: 1`, `run_count: 1`; after the second,
`GET /demo/batch-runs/2026-10-29` returned one summary with `record_count: 3`
and `run_count: 2`. Exact Job names and UTC times are retained below. This is
the demo's upsert/replay behavior, not the real batch job's business guarantee.

The build agent records that HPA CPU metrics became available after adding
metrics-server. That demonstrates metric availability, not scaling behavior
under representative load. It also records 92 validation tests and a clean
secret scan, 26/26 container checks, and six RDS integration tests; these
counts are reported results, not a fresh inspection of their reports.

There are five levels of evidence. Keep them apart:

| Level | Status |
| --- | --- |
| Unit tests (demo app, secrets loader) | Configured in the validation build, which reports success at `643dbb7`; not rerun during snapshot preparation. |
| Rendered manifests (lint, schema, relationships) | Configured in that validation build; raw per-check output was not inspected here. |
| Local container (image built and run with Docker or Podman; a PostgreSQL 17 container stands in for RDS) | Configured in the image build, which reports success at `61fb83d`; no independent per-check count is claimed. |
| RDS (the real database, through the VPC-connected `mobile-api-integration` runner) | Integration build reports success at `73de0a8`; the build agent records the migration and six passing tests. Its JUnit report and logs were not inspected here. |
| Kubernetes cluster (install, migration hook, rollout, CronJob, HPA, Ingress) | Staging build reports success at `643dbb7`; public HTTPS identity/health were independently observed. The build agent additionally records upgrade persistence, staging batch replay, and CPU metric availability. Failure/load scenarios remain separate. |

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
each file against a SHA-256 stored in the script before unpacking it.
kubeconform also fetches Kubernetes JSON schemas from GitHub when it runs.

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
image. Both projects are defined by infra/ (CDK). Overall AWS build statuses
are recorded above; their raw logs and individual test results were not
reviewed while preparing this snapshot.

The build agent also records an earlier failed build at `a5b7eae`: the public
RDS CA bundle was git-ignored and therefore absent from a clean checkout.
Committing the bundle fixed that failure; the repository includes a test for
its presence and checksum. This is recorded build history, not an additional
failure reproduced during snapshot preparation.

`buildspecs/deploy-staging.yml` is configured to run the migration hook through
Helm, wait for the Deployment rollout, and compare the image IDs of Running,
non-terminating API pods with the resolved ECR digest. With HTTPS configured,
it runs the smoke test and an HTTP write/read through the public hostname;
otherwise it uses a check Job against the in-cluster Service. It then creates
one manual batch Job, waits for completion, prints its logs, and checks its
image digest. A successful build reports completion of this configured flow.
It does not assert the batch's persisted business output, replay the staging
batch within one build, or measure request errors throughout a rolling
deployment. The additional staging replay and persistence observations above
come from the build agent's recorded verification across deployments.

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

The earlier verification notes report that each relationship check was
exercised against a deliberately broken chart copy. Those failure checks were
not rerun while preparing this snapshot.

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
- **HTTPS certificates:** verified by the smoke test; the build agent records
  a passing run against the staging hostname. The independent GET checks
  above also verified TLS.
- **`--expect-demo`:** applies to the demo image only. The real service's
  identity is checked with `--expect-service` and `--expect-version`.

## Checks against RDS

The `mobile-api-integration` build reports success at `73de0a8` (ID above).
Its buildspec runs the migration against the configured RDS database and then
`pytest tests/integration`, exporting a JUnit report. The build agent records
`0001_initial` applied and six passing integration tests. That report and the
raw logs were not available here; statuses below attribute those recorded
results rather than claiming independent log review. Local PostgreSQL checks
remain separate from this RDS evidence.

| Check | How | Status |
| --- | --- | --- |
| Migration over TLS (`sslmode=verify-full`) against RDS as `mobile_api_app` | `mobile-api-integration` runner: `python -m app.migrate` | Build-agent record: `0001_initial` applied; matching integration build succeeded; raw output not reviewed |
| Write then read through the application's database code | `tests/integration` in the runner | Build-agent record: six integration tests passed; JUnit not reviewed |
| Batch run and replay for one business date: one `batch_runs` row, `run_count` 2 | `tests/integration` in the runner | Build-agent record: six integration tests passed; separate staging Job replay is recorded below |
| Repeat migration after schema is applied | First Helm install's migration hook, after the integration runner | Build-agent record: the hook completed and the install proceeded; no independent migration log review |
| Record survives a pod replacement | `POST /demo/records`, Helm image upgrade, `GET` | Build agent records record 5 surviving the upgrade to `61fb83d`; its current row was independently read. Pool-wait metric remains uninstrumented. |
| Readiness fails, liveness holds, no restarts, automatic recovery, against RDS | Controlled staging database-connectivity failure and restoration | Outstanding; no live RDS outage/recovery result recorded |

## Manual staging batch run

The staging CronJob `mobile-api-batch` is configured with `suspend: true`, so
it does not run on a schedule. The successful staging deployment build is
configured to create one manual Job and wait for completion. That is a batch
execution smoke check. The build agent records persisted output and same-date
replay across two deploys below; the commands remain here to reproduce those
checks before promotion:

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

The build agent's record at `d3fcc92` names Job
`mobile-api-batch-manual-20261029004832`, which completed in 5s for UTC
business date `2026-10-29` with `record_count: 1` and `run_count: 1`. The next
deploy's Job, `mobile-api-batch-manual-20261029010029`, also completed in 5s
for that date. The recorded HTTPS summary then contained one row with
`record_count: 3`, `run_count: 2`, `first_run_at: 2026-10-29T00:48:35Z`, and
`last_run_at: 2026-10-29T01:00:32Z`. Both times fall on 28 October in Pacific
time; the business date remains `2026-10-29` UTC. Each Job used its associated
release image. The record provides only abbreviated digest prefixes
(`dd89cf3d…` then `5b2e6b47…`), not full digests independently verified here.
The changed `record_count` is expected because replay recalculates the count;
the guarantee demonstrated by the recorded result is one summary row per
business date with an incremented run count.

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

The build agent records execution of the demo persistence and batch replay
parts of scenarios 1 and 4. Public HTTPS identity and health were independently
checked; the upgrade sequence and Job executions rely on the recorded results
described above. The current record and batch summary were also independently
read through HTTPS. The pool-wait metric in scenario 1 is not implemented, so its
full assertion set is not marked passed. Scenarios 2 and 3 need the real
application and payment-provider contract. Scenario 5 needs a deliberately
slow endpoint, which the demo lacks. Scenarios 5, 6, and 7 remain outstanding.

| # | Scenario | Action | Assertions | Status |
| --- | --- | --- | --- | --- |
| 1 | Real database read and write | `POST /demo/records` with a unique note through the staging hostname; `GET /demo/records/{id}`; replace the API pods through a Helm upgrade or rollout restart; the same `GET` again. | `201`, then the same record twice, including after every pod was replaced. The database pool wait metric stays below its threshold. The record is synthetic and is removed afterwards through the integration runner, not the API. For the real application, the same against its own endpoints and schema. | Build agent records record 5 surviving the image upgrade to `61fb83d`; its current row was independently read. Pool-wait metric is absent, so the complete scenario is not passed. |
| 2 | Duplicate webhook | Send one correctly signed test event (provider sandbox, or a locally signed payload with the staging secret) twice. | Both deliveries get the status the provider expects. Exactly one business effect, for example one payment-state transition. The second delivery is recorded as a duplicate. The completion metric increases by one. | Unexecuted: needs real app and provider contract |
| 3 | Invalid-signature webhook | Send the same event with a tampered body, and again signed with the wrong secret. | Rejected with the agreed 4xx. No state change. The signature-rejected metric increases. The payload and signature are not logged. | Unexecuted: needs real app and provider contract |
| 4 | Batch output and replay for the same business period | Run a manual Job for period P (above), then another for P. | Both Jobs complete using their associated release image. `GET /demo/batch-runs/P` returns one row with `run_count: 2`; `record_count` reflects the current records and can change between runs. For the real application: expected business output for P, and no duplicate output after replay. | Build agent records two Jobs for UTC date `2026-10-29` and a count refreshed from 1 to 3. The current summary with `run_count: 2` and `record_count: 3` was independently read. This supports the demo's recorded replay, not the real job's idempotency. |
| 5 | Rollout with requests in flight | Hold steady load, including requests longer than 5s, on the staging HTTPS hostname while `helm upgrade` replaces every pod. | No 5xx and no connection errors. Every slow request completes. Ready replicas never drop below the current count. | Outstanding: controlled rollout under load, including a slow endpoint |
| 6 | Unhealthy replacement | Deploy a release whose readiness can never pass, for example `--set api.readinessProbe.httpGet.path=/not-ready`. | New pods stay unready and receive no traffic. Old pods keep serving, so the smoke test passes throughout. `helm upgrade --rollback-on-failure` fails at its timeout and restores the previous revision. The pipeline stage fails. | Outstanding: deliberately unhealthy replacement and rollback test |
| 7 | Database outage and recovery | Remove the security-group rule that allows the pods to reach RDS on 5432; after a few minutes, restore it. | Within about 10s every pod is NotReady with reason `database unavailable`, the Service has no endpoints, and `/health/live` still returns 200 from inside the cluster. Restart counts do not change. Within about 10s of the rule returning, every pod is Ready again without a restart. | Outstanding: controlled RDS outage and recovery test |
