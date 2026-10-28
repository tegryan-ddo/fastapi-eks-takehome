# Verification

What can be checked today, what each check is meant to catch, and the
acceptance checks that still need the real application or a staging
environment.

A rendered chart is not a deployed chart. Nothing in this repository has been
installed on a Kubernetes cluster yet. The demo's health endpoints say nothing
about Postgres, payment processing, or the batch job.

## Setup

Python 3.12 or later, and network access for the first run.

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
| `python -m pytest` | Chart: `helm lint --strict` for staging and production; render both; validate against Kubernetes 1.30 schemas, taken from `kubeVersion` in `Chart.yaml`, with kubeconform `-strict`; check relationships between resources and the deployer RBAC. | Lint errors. Template errors. Misspelled or misplaced fields, or fields not in Kubernetes 1.30. Any relationship below broken. |
| `python scripts/smoke_test.py <base-url>` | A running service, local or through the staging HTTPS hostname: readiness within a deadline, liveness, and the identity returned by `GET /`. | Not ready by the deadline. A non-200 response. The wrong service name or version. Connection, TLS, or timeout errors. |
| `python scripts/secret_scan.py` | gitleaks over **both** Git history (all commits reachable from local refs) and the working-tree files Git would track. Findings are redacted. | Any secret found. It also reports a scanner error separately from a clean result. |

`buildspecs/validate.yml` runs the first and third commands in CodeBuild,
exports the pytest results as JUnit, and fails the build if either command
fails. It has not been run in CodeBuild yet.

### Relationship checks (`tests/chart/test_chart.py`)

These are relationships a schema validator cannot see:

| Check | Catches |
| --- | --- |
| Service selector equals the Deployment selector, matches the pod labels, includes `component: api`, and contains no version or chart labels | A Service routing to no pods or to the wrong pods. A future CronJob receiving HTTP traffic. Selectors that change between releases, which would break upgrades. |
| Service `targetPort` and every probe port are named container ports, and `http` is 8000 | A Service that never reaches the container. Probes hitting a closed port. |
| Startup and liveness probes call `/health/live`; readiness calls `/health/ready` | Liveness pointed at a dependency-aware readiness check, which restarts every pod during a database outage. |
| Image is `repository:appVersion` by default, `:tag` when a tag is set, and `@digest` when a digest is set, even when a tag is also set | Promotion deploying a mutable tag instead of the verified digest. |
| Pod `runAsNonRoot`, non-zero UID, `RuntimeDefault` seccomp; container blocks privilege escalation, is not privileged, drops ALL capabilities and adds none, and has a read-only root filesystem; service-account token not mounted; the chart's own ServiceAccount used | Regressions in the workload security settings chosen in `decisions.md`. |
| `/tmp` mounted from an `emptyDir` | A read-only root filesystem with nowhere to write temporary files. |
| preStop sleep plus Uvicorn's graceful shutdown (read from the Dockerfile `CMD`) is less than `terminationGracePeriodSeconds` | Pods killed mid-request during rollouts because the grace period is too short. |
| `platform/deploy-rbac.yaml` passes schema validation and grants full management of every kind the chart renders, plus ConfigMaps for Helm history | A chart change that would fail in the pipeline with "forbidden" because the deployer role was not extended. |
| The deployer role has no wildcards, Secrets, pod `exec`, `attach`, or `port-forward`, impersonation, or RBAC rights; it is granted only through namespaced RoleBindings; each environment has its own group | Permission creep. A ClusterRoleBinding granting every namespace. One deploy role able to deploy to both environments. |

Each check was confirmed to fail, with its message, against a deliberately
broken copy of the chart.

**Extend these when the features exist.** Do not add checks for them earlier.

- **HPA:** `scaleTargetRef` names the Deployment, and CPU requests are set.
- **Ingress:** backends name the Service and its port.
- **CronJob:**
  - its pod labels do not match the Service selector;
  - its image reference follows the same tag and digest rule;
  - it has the same security settings.

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

```sh
python scripts/smoke_test.py http://localhost:8000 --expect-version 0.1.0
python scripts/smoke_test.py https://<staging-host> --expect-service <name> --expect-version <version>
```

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
| 4 | Batch replay for the same business period | Run the batch for period P (`kubectl create job --from=cronjob/...` with P set explicitly), then run it again for P. | Both runs succeed. The second run creates no duplicate rows or outputs; output is identical. The freshness metric records P. | Unexecuted: needs real app and CronJob |
| 5 | Rollout with requests in flight | Hold steady load, including requests longer than 5s, on the staging HTTPS hostname while `helm upgrade` replaces every pod. | No 5xx and no connection errors. Every slow request completes. Ready replicas never drop below the replica count. | Unexecuted: needs cluster and slow endpoint |
| 6 | Unhealthy replacement | Deploy a release whose readiness can never pass, for example `--set readinessProbe.httpGet.path=/not-ready`. | New pods stay unready and receive no traffic. Old pods keep serving, so the smoke test passes throughout. `helm upgrade --rollback-on-failure` fails at its timeout and restores the previous revision. The pipeline stage fails. | Unexecuted: needs cluster |
