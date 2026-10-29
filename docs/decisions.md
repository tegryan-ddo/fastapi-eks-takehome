# Decisions and assumptions

Covers the demo application, its container image, the Helm chart, and the
database the demo uses. Delivery, secrets handling, RDS networking, and
monitoring are designed in [operations.md](operations.md).

**Submission v2:** chart and delivery source
`643dbb755d4c18db2b12986c204c7fc0e28eb1d8`. The staging deployment and validation
builds report success at that revision. Earlier bootstrap, secrets sync, RDS
integration, and image builds also report success at the revisions recorded in
[verification.md](verification.md). Public HTTPS identity and health checks
were independently verified on 28 October 2026 (America/Vancouver); the app
reports version `0.1.0-61fb83d`. This does not establish that every staging
acceptance scenario passed. See the [README](../README.md) for scope.

The build agent's additional verification record at
`d3fcc92b452c07174bbba0b6be6bed31818f44fb` reports persistence across an image
upgrade, two staging batch executions for UTC business date `2026-10-29`, and
working HPA CPU metrics. The current record 5 and batch summary were also
independently read through HTTPS. Historical execution details remain the
build agent's recorded results; their raw logs were not reviewed here.

## Assumptions

### Application

- **Demo application assumption.** Only the assignment brief was supplied: no
  application source or image. `examples/demo-api` demonstrates container
  startup, health checks, shutdown, PostgreSQL reads and writes, schema
  migration, and batch execution. The payment-provider integration, the
  employer's business logic, and production throughput remain unverified.
- The real service is a stateless FastAPI app served by Uvicorn on port 8000.
  Any pod can handle any request; durable state lives in PostgreSQL.
- It exposes `/health/live` (the process can respond; no dependency checks) and
  `/health/ready` (initialization has finished and the database answers). If
  its paths differ, only the probe values change.
- Its daily batch runs from the same image with a different command. The demo
  uses `python -m app.batch`; the real command is set through
  `batch.command`.
- It reads `DATABASE_URL` and `PAYMENTS_WEBHOOK_SECRET` from environment
  variables. The batch job and the migration need only `DATABASE_URL`.
- It writes to disk only under `/tmp`.
- On SIGTERM it finishes in-flight requests within 20 seconds. This is
  Uvicorn's graceful shutdown, capped by a timeout in the Dockerfile.
- **To confirm with the application team:** the batch schedule, time zone,
  business-date semantics (the demo defaults to the current UTC date), expected
  duration, and completion deadline; and whether the real service's driver,
  pool, and schema tooling match the demo's.

### Database

- The exercise uses Amazon RDS for PostgreSQL 17, defined in
  [infra/](../infra/README.md), to reproduce the external database dependency.
  The employer's database is managed elsewhere; its provisioning and tuning
  are out of scope. The RDS integration runner reports success at `73de0a8`;
  the staging deployment build reports success at `643dbb7`.
- The network design keeps RDS private and restricts port 5432 to the EKS
  cluster security group in the same VPC and the integration runner. The
  build agent records TLS connections from API pods, the migration hook,
  the batch Job, and the integration runner. The independently
  observed public readiness response confirms the running app's database
  check succeeded, not a complete audit of networking or access controls.
- The application is configured to use the dedicated role `mobile_api_app`,
  never the master user. The db-admin runner obtains the master credentials
  from Secrets Manager to create the role. Build receipts and their limits
  are recorded in [verification.md](verification.md).
- Connections use TLS with certificate and hostname verification against the
  RDS certificate bundle.

### Platform

- The employer's EKS cluster exists and runs Kubernetes 1.30 or later.
  `Chart.yaml` enforces this through `kubeVersion`, because the chart uses the
  native `preStop` sleep action. For this exercise, [infra/](../infra/README.md)
  defines the EKS Auto Mode cluster `mobile-api-staging` in the RDS VPC.
  The build agent records an EKS 1.37 cluster. A staging deployment build
  reports success, and the public application's
  HTTPS identity and health endpoints have been checked independently.
- The resource metrics API is required for CPU-based HPA. The infrastructure
  includes metrics-server. The build agent records that CPU was initially
  `<unknown>` and became available after the add-on was installed. Receiving
  CPU metrics is demonstrated by that record; autoscaling under representative
  load remains an acceptance check.
- A cluster secrets integration already exists. It creates the namespaced
  Kubernetes Secret `mobile-api-runtime` from AWS Secrets Manager before the
  first install (section "Secrets" below). For this exercise, the
  `mobile-api-secrets-sync` CodeBuild project stands in for it.
- An IngressClass `alb` with an ALB controller exists when the Ingress is
  enabled. EKS Auto Mode provides the controller.
- Worker nodes are x86-64. Graviton (arm64) nodes would need a multi-arch image
  build.
- Images live in Amazon ECR, and nodes can pull from it. The repository in
  `values.yaml` is this exercise's repository, provisioned by `infra/`;
  another account overrides it in its own values file.
- Target namespaces may enforce the Pod Security Standards `restricted`
  profile. All three workloads comply with it.

## Decisions

### Demo application

- A small package, `app/`: `main.py` (HTTP), `db.py` (pool and queries),
  `migrate.py` (schema), `batch.py` (batch entry point), and `logs.py`
  (JSON-line events).
- FastAPI lifespan handles startup and shutdown. Startup creates the pool with
  `min_size=0`, so it never blocks on, or exits because of, an unreachable
  database. Readiness is true only after lifespan startup **and** a pooled
  `SELECT 1` within a 1.5s budget, and false again once shutdown starts.
- Logs are one JSON object per line: startup, shutdown, migration, and batch
  events on stdout, errors on stderr. Uvicorn's access log records method,
  path, and status, never bodies. Nothing logs credentials, the database URL,
  or the environment.
- Missing or invalid `DATABASE_URL` is a clear configuration error and a
  non-zero exit from `migrate` and `batch`. There is no in-memory fallback and
  no reporting of database work that did not happen.
- The batch accepts only strict `YYYY-MM-DD`. Invalid input logs
  `batch_rejected` and exits 2. The CronJob's `podFailurePolicy` fails the Job
  at once on exit code 2, because retrying bad arguments cannot help. Exit 1
  (configuration or database failure) is retried up to `backoffLimit`.

### Database access

| Setting | Value | Reason |
| --- | --- | --- |
| Driver | asyncpg, bounded pool | Async, so database waits never block the single-worker event loop. Parameterized queries only. |
| Pool size | minimum 0 (fixed), `DATABASE_POOL_MAX=5` | Starts without pre-opening connections; reuses up to 5 per single-worker pod. Budget active, surge, and terminating pods, plus runners, batch, and other clients. |
| Connect timeout | `DATABASE_CONNECT_TIMEOUT=3` s | A dead host fails fast instead of holding a request. |
| Pool acquisition timeout | `DATABASE_ACQUIRE_TIMEOUT=2` s | Saturation surfaces as an error, not an unbounded queue. |
| Query timeout | `DATABASE_QUERY_TIMEOUT=5` s | No request waits longer than the client would. |
| TLS | `sslmode=verify-full`, CA bundle at `/srv/certs/rds-global-bundle.pem` | Certificate **and** hostname verification. The bundle is the RDS global bundle from `truststore.pki.rds.amazonaws.com`, baked into the image so no pod fetches it at runtime. |

The bundle covers the instance's CA (`rds-ca-rsa2048-g1`). When AWS publishes
new CAs, re-download the bundle and rebuild the image; `DATABASE_CA_BUNDLE`
overrides the path for images that cannot be rebuilt in time. `sslmode=disable`
exists for local throwaway containers only.

These are initial settings. The real service's pool size must be
set against the measured Postgres `max_connections` and the HPA maximum.

### Readiness and database outages

Readiness includes the database; liveness does not.

- A pod that cannot reach PostgreSQL cannot serve, so it leaves the Service
  within about 10s (readiness every 5s, 2 failures). It is **not** restarted:
  liveness still passes, so the process stays up with its health endpoints
  exposed, and readiness returns automatically when connectivity does.
- The check is a pooled `SELECT 1`; `asyncio.wait_for` applies a 1.5s budget
  to the overall acquire/query/release operation, under the probe's 2s
  timeout. It reuses available pooled connections; first use or recovery can
  require the pool to open a connection. The pool minimum is fixed at zero,
  so configuration cannot make startup wait for database connections.
- A database-wide outage therefore makes every pod unready. An ALB can fail
  open when its registered targets are all unhealthy; if targets are removed
  or unused, it can instead return its own 503. The exact outcome depends on
  target registration and controller behavior and needs a live test. The
  application must handle database failure safely even while unready. The
  demo can do nothing useful without the database; no replica always reports
  ready.
- If the real service can do useful work without PostgreSQL (cached reads, a
  degraded mode, a maintenance response), its readiness should **not** include
  the database check, so those pods stay in rotation. A separate maintenance
  response would need an explicit routing design.
- During a deploy, the same outage can make `helm upgrade --wait` time out
  because no new pod becomes ready. `--wait` alone does not roll back:
  automatic rollback requires the documented Helm 4
  `--rollback-on-failure` option. A release rollback does not undo database
  migrations.

### Schema migrations

Migrations run as a Helm hook Job (`templates/migrate-job.yaml`), annotated
`pre-install,pre-upgrade`. Helm creates it before any other resource of the
release changes and waits for it to finish. A failure fails the release before
a single API or batch pod is touched.

| Choice | Reason |
| --- | --- |
| Not at API startup | With many replicas, every pod would race to migrate. Startup and readiness would become unpredictable, and a failed migration would crash-loop the API. |
| Not from a health endpoint | Probes must be cheap and side-effect free. |
| `backoffLimit: 0`, `activeDeadlineSeconds: 600` | `schema_migrations` tracks applied files and an advisory lock serializes runners. Transactional files and their records commit together; nontransactional files need their own safe retry behavior. A hung migration fails after 10 minutes instead of blocking the pipeline. |
| Delete policy `before-hook-creation,hook-succeeded` | Successful Jobs disappear; a failed Job stays, with its pod logs, until the next release or `ttlSecondsAfterFinished` (one day). |
| Namespace default ServiceAccount | A pre-install hook runs before the chart's ServiceAccount exists. No token is mounted, so the account's identity is irrelevant. |
| Runs as `mobile_api_app` | The application role owns its schema objects and has `CREATE` on schema `public`, granted by `platform/db/create_app_user.sql`. No master credentials reach the cluster. |
| One transaction per file, with an opt-out | The default commits each file and its version record atomically. A first-line `-- migrate: no-transaction` marker opts out for operations such as `CREATE INDEX CONCURRENTLY`. The operation and version record are then separate, so authors must handle partial success and safe reruns. |

Costs: the Secret must exist before the first install; a hook Job is not rolled
back with the release, so schema changes must stay backward compatible (expand,
then contract; see [operations.md](operations.md#5-rollback-and-migrations));
and Helm treats hook resources as outside the release, so `helm uninstall`
leaves a failed Job behind until its TTL.

### One Uvicorn worker per pod

Kubernetes already supervises processes and scales replicas, so each pod runs a
single Uvicorn process.

- One Python process is bounded by the GIL to about one core. This keeps the
  CPU request meaningful and makes autoscaling by pod count straightforward.
- Probes test the process that serves traffic. With several workers, a probe
  reaches only whichever worker accepts it.
- A crash or OOM kill affects one small pod, not several workers at once.
- Cost: more pods, each with its own baseline memory and its own connection
  pool. The connection budget must include maximum API replicas, surge and
  terminating pods, batch, migrations, and other clients. A pooler such as RDS
  Proxy or PgBouncer is a possible follow-up after measuring that budget.

### Health probes

| Probe | Endpoint | Timing | Reason |
| --- | --- | --- | --- |
| Startup | `/health/live` | every 2s, up to 30 failures (60s) | Gives slow starts time before liveness applies. Uses the database-free endpoint, so a database outage during a deploy cannot cause restart loops. |
| Readiness | `/health/ready` | every 5s, 2s timeout, 2 failures | Removes a pod from the Service within about 10s when it cannot serve, including when PostgreSQL is unreachable. |
| Liveness | `/health/live` | every 10s, 3s timeout, 3 failures | Restarts only a wedged process, after about 30s. Deliberately lenient: a saturated event loop answers slowly, and restarting pods under load makes overload worse. |

Liveness must never check PostgreSQL. A brief database outage would otherwise
restart every pod at once.

### Resources and scaling (initial assumptions, not measurements)

| Setting | Value | Reason |
| --- | --- | --- |
| API CPU request | `500m` | Scheduling guarantee, and the base the HPA's utilization is measured against |
| API memory request = limit | `512Mi` | Memory cannot be reclaimed like CPU. Reserving what the pod may use means it is not the first evicted under node pressure. |
| Batch CPU / memory | `250m` / `256Mi` request, `256Mi` limit | The demo batch runs two statements; size the real job from a measured run |
| Migration CPU / memory | `100m` / `128Mi` request, `128Mi` limit | Applies small SQL files; a long-running real migration may need more |
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
tag for all three workloads. Its command replaces the image's HTTP server. Its
pods are labelled `component: batch`, so the API Service, Deployment, and PDB
selectors (`component: api`) never include them.

**What the demo batch does, and what replay means here.** In one transaction it
counts `demo_records` and upserts `batch_runs` for the business date:
`INSERT ... ON CONFLICT (business_date) DO UPDATE SET record_count =
EXCLUDED.record_count, run_count = run_count + 1, last_run_at = now()`. The
primary key guarantees one row per date. Running the same date again refreshes
`record_count`, increments `run_count`, and creates nothing else. This shows
that the demo's replay is safe. It says nothing about whether the employer's
real batch job is idempotent; that must be shown with its own output.

### Secrets

- The chart only references secrets. `runtimeSecret.name` (default
  `mobile-api-runtime`) names a Kubernetes Secret that the cluster's secrets
  integration creates from AWS Secrets Manager. The API reads `DATABASE_URL`
  and `PAYMENTS_WEBHOOK_SECRET` from it; the batch and the migration read only
  `DATABASE_URL`.
- PostgreSQL is required, so the Secret must exist before the first install.
  With an empty name the chart renders no references, and every workload fails
  at startup with a clear configuration error instead of running without a
  database.
- Secret values never appear in values files, rendered manifests, test
  fixtures, logs, or the image. A Secret's name is not a credential.
- Operators upload values from a trusted local terminal, never through CI or
  an AI agent. `scripts/load_secrets.py` is the documented path; the build
  agent records that the staging operator instead used the AWS CLI with a
  local file, then deleted that file. The full flow, including creating the
  database role, is in
  [operations.md](operations.md#application-secrets).

### Container security

- Base image `python:3.13-slim`, not 3.14. Amazon Inspector Code Security,
  the proposed source scanner, documents Python support only up to 3.13
  (checked October 2026). Python 3.13 receives security fixes until October
  2029. Move to 3.14 once Inspector lists it.
- Dependencies are pinned, including transitive ones. `pip check` fails the
  build on inconsistent pins. pip is then removed from the image, along with
  the vendored libraries that scanners report.
- The RDS global CA bundle is part of the image, so TLS verification needs no
  network fetch and no writable path at runtime.
- The image runs as numeric UID/GID 10001. The chart also sets `runAsUser`,
  `runAsGroup`, and `runAsNonRoot` for all three workloads, so non-root is
  enforced even if an image forgets.
- No privilege escalation, all Linux capabilities dropped (port 8000 needs
  none), and the `RuntimeDefault` seccomp profile. These are defined once in
  `_helpers.tpl` and shared by every workload.
- Read-only root filesystem. `/tmp` is a 64Mi `emptyDir`, because Python's
  `tempfile` module and Starlette's multipart uploads (over 1MB) write there.
  If `/tmp` exceeds the size limit, the pod is evicted instead of filling the
  node's disk.
- A dedicated ServiceAccount for the API and batch, with token automounting
  disabled because no workload calls Kubernetes. EKS Pod Identity and IRSA
  inject their own credentials, so this will not block AWS access later.

### Shutdown

When Kubernetes terminates an API pod:

1. The pod is removed from the Service endpoints. Proxies, and later the load
   balancer, stop routing to it only after a short propagation delay.
2. The `preStop` hook sleeps for 5s, so the pod keeps serving requests that
   arrive during that delay.
3. Uvicorn, as PID 1 through the exec-form `CMD`, receives SIGTERM. It stops
   accepting connections, waits up to 20s for in-flight requests, then runs
   the FastAPI lifespan shutdown, which closes the pool.
4. `terminationGracePeriodSeconds: 30` covers the 5s + 20s with a 5s margin
   before SIGKILL. A chart test fails if that budget is exceeded.

The native `sleep` lifecycle action avoids depending on a shell or `sleep`
binary inside the image. With the ALB in place, its deregistration delay and
this sleep still need validation together under requests in flight.

### Ingress (off by default)

- One Ingress, class `alb`, routing `/` to the chart Service's `http` port.
  Rendered only when `ingress.enabled` is true, so environments without a
  public hostname create no load balancer.
- TLS terminates at the ALB with an ACM certificate named in the annotations.
  The Ingress has no `spec.tls`, so no certificate or key ever enters the
  cluster, and nothing in the chart expires.
- The host, certificate ARN, and ALB annotations differ per environment and
  are supplied at deploy time through an extra values file. The example in
  `environments/staging.yaml` uses `target-type: ip`, so the ALB sends traffic
  to pods directly; the preStop sleep allows time for routing changes but
  does not guarantee target deregistration has completed. The HTTP listener
  exists only to redirect to HTTPS.
- The payment-provider webhook route will share this ALB; signature
  verification belongs in the application.
- Public HTTPS identity and health responses were independently verified at
  `https://mobile-api-staging.digitaldevops.io`. The build agent records an
  EKS Auto Mode ALB, the account's `*.digitaldevops.io` ACM certificate, and
  a Route53 CNAME published by the deploy build. This does not demonstrate
  zero-error rollouts; the ALB's deregistration delay and preStop sleep still
  need a controlled test with requests in flight.

### Rollout, selectors, image reference

- Rolling update with `maxUnavailable: 0` and `maxSurge: 25%`. Replacement pods
  must pass readiness before the controller removes old ones. This limits
  rollout-driven capacity loss, but does not prevent concurrent failures and
  needs spare cluster capacity for surge pods.
- Selectors use `app.kubernetes.io/name`, `instance`, and `component`. They
  contain no version, because Deployment selectors are immutable. Batch and
  migration pods carry their own `component` value and match no API selector.
- The image can be referenced by digest, which takes precedence, or by tag.
  Promoting a digest from staging to production guarantees production runs the
  exact image that was tested. The tag or digest is set at deploy time with
  `--set`, not stored in environment files.

## Remaining acceptance work

- **Rollout and ingress behavior**: public HTTPS health checks succeeded, and
  the staging deployment build reports success. Controlled rollout, draining,
  and failure tests remain outstanding; no zero-error rollout claim is made.
- **Topology placement**: topology spread across Availability Zones and nodes.
  The build agent records both staging API pods on one Auto Mode node, so
  that staging layout does not demonstrate node or zone redundancy.
- **Production database**: the exercise infrastructure targets staging. The
  production connection budget, pooler decision, and EKS workload networking
  depend on the cluster and the real service. The earlier successful RDS
  integration build does not replace production acceptance testing.
- **Delivery and monitoring**: designed in [operations.md](operations.md).
  Validation, image building, RDS integration, and staging deployment have
  successful build receipts at the revisions listed in
  [verification.md](verification.md). Production promotion, tested alarms,
  representative peak load, pool-wait instrumentation, and the employer's
  payment behavior remain outstanding. Staging persistence and demo batch
  replay have recorded results; they do not establish production business
  correctness. The build agent records that the full pipeline, production,
  and alarms are not yet provisioned.

Also not addressed yet: pinning the base image by digest, NetworkPolicy, and
multi-arch builds.
