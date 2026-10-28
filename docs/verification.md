# Verification

What can be checked today, what each check is meant to catch, and the checks
that still need a cluster, a staging environment, or the real application.

There are four levels of evidence. Keep them apart:

| Level | Status |
| --- | --- |
| Unit tests (demo app, secrets loader) | Run locally |
| Rendered manifests (lint, schema, relationships) | Run locally |
| Local container (image built and run with Docker or Podman) | Run locally |
| Kubernetes cluster (install, rollout, CronJob, HPA, routing) | **Not run.** No cluster was available. |

A local HTTP response does not show that ALB routing, DNS, TLS, or rollout
behaviour work. The demo's health endpoints say nothing about Postgres, payment
processing, or real batch output.

## Setup

Python 3.12 or later, network access on the first run, and Docker or Podman for
the container check.

```sh
python -m pip install -r requirements-dev.txt
python scripts/tools.py
```

`scripts/tools.py` downloads pinned releases of Helm (v4.3.1), kubeconform
(v0.8.0), and gitleaks (v8.30.1) into `.tools/`. It checks each archive against
a SHA-256 stored in the script before unpacking it. kubeconform also fetches
Kubernetes JSON schemas from GitHub when it runs.

## Checks that run now

| Command | Checks | Fails when |
| --- | --- | --- |
| `python -m pytest` | **Chart:** `helm lint --strict` and rendering for staging and production; kubeconform `-strict` against Kubernetes 1.30 (the `kubeVersion` minimum); the relationships below. **Demo app:** readiness lifecycle, demo identity, log hygiene, batch date handling and exit codes. **Secrets loader:** 21 offline tests with dummy values and mocked AWS clients. | Any check below breaks. |
| `python scripts/verify_container.py` | Builds the image, then runs it non-root with a read-only root, a tmpfs `/tmp`, no capabilities, and no-new-privileges. Runs the smoke test (including `"demo": true`). Checks UID 10001, the read-only root, and the writable `/tmp`. Sends SIGTERM and requires exit code 0, a logged lifespan shutdown, and completion within Uvicorn's 20s allowance. Runs the batch with the default date, an explicit date, and an invalid date. | Image fails to build or start; any endpoint or body is wrong; it runs as root or can write outside `/tmp`; shutdown is unclean or slow; the batch accepts a bad date or fails a good one. |
| `python scripts/smoke_test.py <base-url>` | A running service, local or through the staging HTTPS hostname: readiness within a deadline, liveness, and the identity returned by `GET /`. | Not ready by the deadline. A non-200 response. The wrong service name, version, or demo flag. Connection, TLS, or timeout errors. |
| `python scripts/secret_scan.py` | gitleaks over **both** Git history (all commits reachable from local refs) and the working-tree files Git would track. Findings are redacted. | Any secret found. A scanner error is reported separately from a clean result. |

`buildspecs/validate.yml` runs `pytest` and the secret scan in CodeBuild,
exports JUnit results, and fails the build if either fails. The container check
needs Docker, so it runs in the privileged release build project instead (see
[operations.md](operations.md)). Neither has run in AWS yet.

### Relationship checks (`tests/chart/test_chart.py`)

These are relationships a schema validator cannot see:

| Check | Catches |
| --- | --- |
| Service, Deployment, and PDB selectors match the API pods, never the batch pods, include the release and `component: api`, and contain no version or chart labels | Traffic or disruption budgets that include batch pods. A Service routing to no pods. Selectors that change between releases, which would break upgrades. |
| Service `targetPort` and every probe port are named container ports, and `http` is 8000 | A Service that never reaches the container. Probes hitting a closed port. |
| Startup and liveness probes call `/health/live`; readiness calls `/health/ready` | Liveness pointed at a dependency-aware check, which restarts every pod during a database outage. |
| The API and batch images are both `repository:appVersion` by default, `:tag` when a tag is set, and `@digest` when a digest is set, even alongside a tag | One workload running a different image. Promotion deploying a mutable tag instead of the verified digest. |
| Both workloads: non-root, non-zero UID, `RuntimeDefault` seccomp, no privilege escalation, not privileged, ALL capabilities dropped and none added, read-only root, service-account token not mounted, the chart's own ServiceAccount | Regressions in the security settings chosen in `decisions.md`. |
| Both workloads mount `/tmp` from an `emptyDir`, and have CPU and memory requests plus a memory limit | Nowhere to write temporary files. Unbounded memory. An HPA with nothing to measure against. |
| preStop sleep plus Uvicorn's graceful shutdown (read from the Dockerfile `CMD`) is less than `terminationGracePeriodSeconds` | Pods killed mid-request during rollouts. |
| HPA targets the Deployment, `minReplicas` ≤ `maxReplicas`, and the Deployment omits `replicas` while the HPA is enabled; with autoscaling off, no HPA renders and `api.replicaCount` applies | Each upgrade resetting the HPA's replica count. An HPA scaling nothing. |
| CronJob: suspended in staging only, `Forbid` concurrency, schedule and explicit time zone, the batch command (not the HTTP server), `restartPolicy: Never`, and explicit integer deadlines, retries, and history limits | Staging running batches on a schedule. Overlapping runs. A Job that serves HTTP forever. Unbounded retries or runtime. |
| No secret references without `runtimeSecret.name`. With it, the API gets exactly `DATABASE_URL` and `PAYMENTS_WEBHOOK_SECRET`, and the batch only `DATABASE_URL`, all through `secretKeyRef` and never as literal values | Secrets embedded in manifests. A workload receiving keys it does not need. A demo that cannot start without secrets. |
| `platform/deploy-rbac.yaml` passes schema validation and grants full management of every kind the chart renders, plus Jobs and ConfigMaps | A chart change that would fail in the pipeline with "forbidden". |
| The deployer role has no wildcards, Secrets, pod `exec`, `attach`, or `port-forward`, impersonation, or RBAC rights; it is granted only through namespaced RoleBindings; each environment has its own group | Permission creep. A ClusterRoleBinding granting every namespace. One deploy role able to deploy to both environments. |

Each check was confirmed to fail, with its message, against a deliberately
broken copy of the chart. When an Ingress is added, also check that its
backends name the Service and its port.

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
success means the logs show `batch_started` then `batch_completed`, with
`"demo": true`, and the Job condition is `Complete`. That shows the image and
command work. It says nothing about business output.

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
suspended). Check for an active Job first, and rely on the application's own
replay safety or locking for the rest.

**Template changes.** Changing the CronJob (a new image digest, for example)
affects only Jobs created afterwards. A Job that is already running keeps the
template it was created with. `kubectl create job --from` copies the CronJob as
deployed at that moment.

**Retention.** `ttlSecondsAfterFinished` (7 days) removes finished Jobs,
including manual ones. Copy any logs you need before then.

The deployer role allows creating and reading Jobs and reading pod logs, so the
pipeline's verify-staging step can run these commands.

## Staging acceptance plan

None of these has been executed. Scenarios 1–4 need the real application and
staging dependencies. Scenarios 5 and 6 could run with the demo on any
Kubernetes cluster, but no cluster was available. Scenario 5 also needs a
deliberately slow endpoint, which the demo lacks.

| # | Scenario | Action | Assertions | Status |
| --- | --- | --- | --- | --- |
| 1 | Real database read and write | Through the public API, create a record with a unique test identifier, then read it back through an endpoint that queries Postgres. | Write returns success. Read returns the same data. The record exists in the staging database. The database pool wait metric stays below its threshold. The test record is cleaned up. | Unexecuted: needs real app and staging Postgres |
| 2 | Duplicate webhook | Send one correctly signed test event (provider sandbox, or a locally signed payload with the staging secret) twice. | Both deliveries get the status the provider expects. Exactly one business effect, for example one payment-state transition. The second delivery is recorded as a duplicate. The completion metric increases by one. | Unexecuted: needs real app and provider contract |
| 3 | Invalid-signature webhook | Send the same event with a tampered body, and again signed with the wrong secret. | Rejected with the agreed 4xx. No state change. The signature-rejected metric increases. The payload and signature are not logged. | Unexecuted: needs real app and provider contract |
| 4 | Batch output and replay for the same business period | Run a manual Job for period P (above), check its output, then run another for P. | Both Jobs complete with the release digest. The first produces the expected business output for P. The second creates no duplicate rows or outputs; output is identical. The freshness metric records P. | Unexecuted: needs real app and staging data |
| 5 | Rollout with requests in flight | Hold steady load, including requests longer than 5s, on the staging HTTPS hostname while `helm upgrade` replaces every pod. | No 5xx and no connection errors. Every slow request completes. Ready replicas never drop below the current count. | Unexecuted: needs cluster and slow endpoint |
| 6 | Unhealthy replacement | Deploy a release whose readiness can never pass, for example `--set api.readinessProbe.httpGet.path=/not-ready`. | New pods stay unready and receive no traffic. Old pods keep serving, so the smoke test passes throughout. `helm upgrade --rollback-on-failure` fails at its timeout and restores the previous revision. The pipeline stage fails. | Unexecuted: needs cluster |
