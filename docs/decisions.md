# Decisions and assumptions

Covers the demo application, its container image, and the Helm chart.
Delivery, secrets handling, and monitoring are designed in
[operations.md](operations.md).

## Assumptions

### Application

- **Demo application assumption.** Only the assignment brief was supplied: no
  application source or image. `examples/demo-api` demonstrates container
  startup, health checks, shutdown, and batch execution. Postgres access,
  payment-provider integration, real batch behaviour, and production throughput
  remain unverified.
- The real service is a stateless FastAPI app served by Uvicorn on port 8000.
  Any pod can handle any request; durable state lives in Postgres.
- It exposes `/health/live` (the process can respond; no dependency checks) and
  `/health/ready` (initialization has finished and it can serve). If its paths
  differ, only the probe values change.
- Its daily batch runs from the same image with a different command. The demo
  uses `python -m app.batch`; the real command is set through
  `batch.command`.
- It reads `DATABASE_URL` and `PAYMENTS_WEBHOOK_SECRET` from environment
  variables. The batch job needs only `DATABASE_URL`.
- It writes to disk only under `/tmp`.
- On SIGTERM it finishes in-flight requests within 20 seconds. This is
  Uvicorn's graceful shutdown, capped by a timeout in the Dockerfile.
- **To confirm with the application team:** the batch schedule, time zone,
  business-date semantics (the demo defaults to the current UTC date), expected
  duration, and completion deadline.

### Platform

- The EKS cluster exists and runs Kubernetes 1.30 or later. `Chart.yaml`
  enforces this through `kubeVersion`, because the chart uses the native
  `preStop` sleep action.
- metrics-server (the resource metrics API) is installed. Without it, the HPA
  cannot read CPU utilization and does not scale.
- A cluster secrets integration already exists. It creates a namespaced
  Kubernetes Secret from AWS Secrets Manager (section "Secrets" below).
- Worker nodes are x86-64. Graviton (arm64) nodes would need a multi-arch image
  build.
- Images live in Amazon ECR, and nodes can pull from it. The repository URL in
  `values.yaml` is a placeholder.
- Target namespaces may enforce the Pod Security Standards `restricted`
  profile. Both workloads comply with it.
- Pods can already reach the external Postgres (VPC routing, security groups).
  Provisioning and tuning Postgres are outside this assignment.

## Decisions

### Demo application

- A small package, `app/`: `main.py` (HTTP), `batch.py` (batch entry point),
  and `logs.py` (JSON-line events).
- FastAPI lifespan handles startup and shutdown. Readiness is an in-process
  flag, true only between the end of lifespan startup and the start of
  lifespan shutdown. It makes no network calls. Uvicorn opens its socket only
  after startup, so in practice the demo reports ready whenever it answers. The
  flag states the contract the real service must keep.
- Logs are one JSON object per line: startup, shutdown, and batch events on
  stdout, errors on stderr. Uvicorn's access log records method, path, and
  status, never bodies. Nothing logs credentials or the environment.
- The batch accepts only strict `YYYY-MM-DD`. Invalid input logs
  `batch_rejected` and exits 2. The CronJob's `podFailurePolicy` fails the Job
  at once on exit code 2, because retrying bad arguments cannot help.

### One Uvicorn worker per pod

Kubernetes already supervises processes and scales replicas, so each pod runs a
single Uvicorn process.

- One Python process is bounded by the GIL to about one core. This keeps the
  CPU request meaningful and makes autoscaling by pod count straightforward.
- Probes test the process that serves traffic. With several workers, a probe
  reaches only whichever worker accepts it.
- A crash or OOM kill affects one small pod, not several workers at once.
- Cost: more pods, each with its own baseline memory and its own Postgres
  connection pool. At peak, maxReplicas × pool size must stay within Postgres's
  connection budget. This may require a pooler such as RDS Proxy or PgBouncer.

### Health probes

| Probe | Endpoint | Timing | Reason |
| --- | --- | --- | --- |
| Startup | `/health/live` | every 2s, up to 30 failures (60s) | Gives slow starts time before liveness applies. Uses the dependency-free endpoint, so a Postgres outage during a deploy cannot cause restart loops. |
| Readiness | `/health/ready` | every 5s, 2 failures | Removes a pod from the Service within about 10s when it cannot serve. |
| Liveness | `/health/live` | every 10s, 3s timeout, 3 failures | Restarts only a wedged process, after about 30s. Deliberately lenient: a saturated event loop answers slowly, and restarting pods under load makes overload worse. |

Liveness must never check Postgres. A brief database outage would otherwise
restart every pod at once. Whether the real app's readiness should include a
Postgres check is still open. Including it would pull every pod out of
rotation during a database outage.

### Resources and scaling (initial assumptions, not measurements)

| Setting | Value | Reason |
| --- | --- | --- |
| API CPU request | `500m` | Scheduling guarantee, and the base the HPA's utilization is measured against |
| API memory request = limit | `512Mi` | Memory cannot be reclaimed like CPU. Reserving what the pod may use means it is not the first evicted under node pressure. |
| Batch CPU / memory | `250m` / `256Mi` request, `256Mi` limit | The demo batch does almost nothing; size the real job from a measured run |
| CPU limits | none | CFS throttling adds latency spikes. Requests still guarantee a fair share. |
| HPA | CPU, 60% average utilization; 2–4 replicas by default, 3–12 in production | See the HPA assumption below |
| PDB | `maxUnavailable: 1`, API pods only | Node drains and cluster upgrades take API pods one at a time |

**HPA assumption.** CPU utilization is the initial HPA signal. Before
production, confirm or refine it with the application team and representative
load tests. Database or I/O waits may increase latency without increasing CPU.
Replica limits must also respect the database connection budget.

With the HPA enabled, the Deployment omits `spec.replicas`, so a `helm upgrade`
cannot reset the replica count the HPA is managing. `api.replicaCount` applies
only when `autoscaling.enabled` is false.

All of these numbers are placeholders. A load test must measure requests per
second per pod at the target latency, CPU per request, and steady and peak
memory, using the real request mix.

### Daily CronJob

| Setting | Value | Reason |
| --- | --- | --- |
| Schedule, time zone | `30 2 * * *`, `Etc/UTC` | Placeholder. Production values must come from the application team. |
| `suspend` | `true` in staging | Staging runs only manual Jobs, validated before promotion |
| `concurrencyPolicy` | `Forbid` | A run never starts while the previous one is active |
| `startingDeadlineSeconds` | 3600 | A run that cannot start within an hour is skipped, not run late |
| `backoffLimit` | 2 | At most three attempts. `restartPolicy: Never` keeps each attempt's pod and logs. |
| `activeDeadlineSeconds` | 3600 | Stops a run, including retries, after an hour |
| History | 3 successful, 5 failed; finished Jobs deleted after 7 days | Failures are kept longer for diagnosis. The TTL also removes manual Jobs. |

The CronJob uses the same image reference as the API. A digest overrides the
tag for both. Its command replaces the image's HTTP server. Its pods are
labelled `component: batch`, so the API Service, Deployment, and PDB selectors
(`component: api`) never include them.

### Secrets

- The chart only references secrets. `runtimeSecret.name` names a Kubernetes
  Secret that the cluster's secrets integration creates from AWS Secrets
  Manager. The API reads `DATABASE_URL` and `PAYMENTS_WEBHOOK_SECRET` from it,
  and the batch reads only `DATABASE_URL`.
- The name is empty by default, so the demo renders no secret references and
  starts without credentials.
- Secret values never appear in values files, rendered manifests, test
  fixtures, or the image.
- Operators upload values with `scripts/load_secrets.py` from a trusted local
  terminal. Values never pass through CI or an AI agent. The full flow is in
  [operations.md](operations.md#application-secrets).

### Container security

- Base image `python:3.13-slim`, not 3.14. Amazon Inspector Code Security,
  the proposed source scanner, documents Python support only up to 3.13
  (checked October 2026). Python 3.13 receives security fixes until October
  2029. Move to 3.14 once Inspector lists it.
- Dependencies are pinned, including transitive ones. `pip check` fails the
  build on inconsistent pins. pip is then removed from the image, along with
  the vendored libraries that scanners report.
- The image runs as numeric UID/GID 10001. The chart also sets `runAsUser`,
  `runAsGroup`, and `runAsNonRoot` for both workloads, so non-root is enforced
  even if an image forgets.
- No privilege escalation, all Linux capabilities dropped (port 8000 needs
  none), and the `RuntimeDefault` seccomp profile. These are defined once in
  `_helpers.tpl` and shared by both workloads.
- Read-only root filesystem. `/tmp` is a 64Mi `emptyDir`, because Python's
  `tempfile` module and Starlette's multipart uploads (over 1MB) write there.
  If `/tmp` exceeds the size limit, the pod is evicted instead of filling the
  node's disk.
- A dedicated ServiceAccount, with token automounting disabled because neither
  workload calls Kubernetes. EKS Pod Identity and IRSA inject their own
  credentials, so this will not block AWS access later.

### Shutdown

When Kubernetes terminates an API pod:

1. The pod is removed from the Service endpoints. Proxies, and later the load
   balancer, stop routing to it only after a short propagation delay.
2. The `preStop` hook sleeps for 5s, so the pod keeps serving requests that
   arrive during that delay.
3. Uvicorn, as PID 1 through the exec-form `CMD`, receives SIGTERM. It stops
   accepting connections, waits up to 20s for in-flight requests, then runs
   the FastAPI lifespan shutdown.
4. `terminationGracePeriodSeconds: 30` covers the 5s + 20s with a 5s margin
   before SIGKILL. A chart test fails if that budget is exceeded.

The native `sleep` lifecycle action avoids depending on a shell or `sleep`
binary inside the image. Once an ALB is added, its deregistration delay and
this sleep need to be tuned together.

### Rollout, selectors, image reference

- Rolling update with `maxUnavailable: 0` and `maxSurge: 25%`. Replacement pods
  must pass readiness before old ones stop, so ready capacity never drops
  during a deploy. This needs spare cluster capacity for the surge pods.
- Selectors use `app.kubernetes.io/name`, `instance`, and `component`. They
  contain no version, because Deployment selectors are immutable.
- The image can be referenced by digest, which takes precedence, or by tag.
  Promoting a digest from staging to production guarantees production runs the
  exact image that was tested. The tag or digest is set at deploy time with
  `--set`, not stored in environment files.

## Remaining assignment work

- **Ingress and TLS**: an ALB through the AWS Load Balancer Controller, in IP
  target mode with an ACM certificate. This includes a public route for
  payment-provider webhooks; signature verification belongs in the app.
- **Topology placement**: topology spread across Availability Zones and nodes.
- **Database migrations**: a controlled Job per release, designed in
  [operations.md](operations.md#5-rollback-and-migrations), not implemented.
- **Delivery and monitoring**: designed in [operations.md](operations.md).
  Nothing is provisioned.

Also not addressed yet: pinning the base image by digest, NetworkPolicy, and
multi-arch builds.
