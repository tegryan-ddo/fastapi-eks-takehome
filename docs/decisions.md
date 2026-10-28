# Decisions and assumptions

Covers the first increment: container image, Deployment, Service, and
ServiceAccount. Later increments extend this file.

## Assumptions

### Application

- The real service is a stateless FastAPI app served by Uvicorn on port 8000.
  Any pod can handle any request; durable state lives in Postgres.
- It exposes `/health/live` (the process can respond; no dependency checks) and
  `/health/ready` (initialization has finished and it can serve). If its paths
  differ, only the probe values change.
- It reads configuration from environment variables. The Postgres credentials
  and payment-provider webhook signing secret are injected at runtime, never
  baked into the image or committed to values files.
- It writes to disk only under `/tmp`.
- On SIGTERM it finishes in-flight requests within 20 seconds. This is
  Uvicorn's graceful shutdown, capped by a timeout in the Dockerfile.
- Until the real app is supplied, `examples/demo-api` stands in. Its readiness
  says nothing about Postgres or payments.

### Platform

- The EKS cluster exists and runs Kubernetes 1.30 or later. `Chart.yaml`
  enforces this through `kubeVersion`, because the chart uses the native
  `preStop` sleep action.
- Worker nodes are x86-64. Graviton (arm64) nodes would need a multi-arch image
  build.
- Images live in Amazon ECR, and nodes can pull from it. The repository URL in
  `values.yaml` is a placeholder.
- Target namespaces may enforce the Pod Security Standards `restricted`
  profile. The chart complies with it.
- Pods can already reach the external Postgres (VPC routing, security groups).
  This chart does not manage that.
- Later increments will need the AWS Load Balancer Controller, metrics-server,
  a secrets integration, and EKS Pod Identity or IRSA. None is required yet.

## Decisions

### One Uvicorn worker per pod

Kubernetes already supervises processes and scales replicas, so each pod runs a
single Uvicorn process.

- One Python process is bounded by the GIL to about one core. This keeps the
  CPU request meaningful and makes autoscaling by pod count straightforward.
- Probes test the process that serves traffic. With several workers, a probe
  reaches only whichever worker accepts it.
- A crash or OOM kill affects one small pod, not several workers at once.
- Cost: more pods, each with its own baseline memory and its own Postgres
  connection pool. At peak load, pods × pool size must stay below Postgres's
  connection limit. This may require a pooler such as RDS Proxy or PgBouncer.
  Confirm it during load testing.

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

### Resource settings (initial, not measured)

| Setting | Value | Reason |
| --- | --- | --- |
| CPU request | `500m` | Scheduling guarantee and the future autoscaling baseline. One worker cannot use much more than one core. |
| CPU limit | none | CFS throttling adds latency spikes. The request still guarantees a fair share. |
| Memory request = limit | `512Mi` | Memory cannot be reclaimed like CPU. Reserving what the pod may use means it is not the first evicted under node memory pressure. Exceeding it kills only this container. |

All three are placeholders. A load test must measure requests per second per
pod at the target latency, CPU per request, and steady and peak memory. Only
then can these values and the replica counts be set.

### Container security

- Base image `python:3.14-slim`. Dependencies are pinned, including transitive
  ones, so rebuilds are reproducible.
- The image runs as numeric UID/GID 10001. The chart also sets `runAsUser`,
  `runAsGroup`, and `runAsNonRoot`, so it enforces non-root even if an image
  forgets to.
- No privilege escalation, all Linux capabilities dropped (port 8000 needs
  none), and the `RuntimeDefault` seccomp profile.
- Read-only root filesystem. `/tmp` is a 64Mi `emptyDir`, because Python's
  `tempfile` module and Starlette's multipart uploads (over 1MB) write there.
  If `/tmp` exceeds the size limit, the pod is evicted instead of filling the
  node's disk.
- A dedicated ServiceAccount, with token automounting disabled because the API
  does not call Kubernetes. EKS Pod Identity and IRSA inject their own
  credentials, so this will not block AWS access later.
- The local `docker run` in the README applies equivalent restrictions, so the
  image is exercised under the same constraints outside Kubernetes.

### Shutdown

When Kubernetes terminates a pod:

1. The pod is removed from the Service endpoints. Proxies, and later the load
   balancer, stop routing to it only after a short propagation delay.
2. The `preStop` hook sleeps for 5s, so the pod keeps serving requests that
   arrive during that delay.
3. Uvicorn, as PID 1 through the exec-form `CMD`, receives SIGTERM. It stops
   accepting connections and waits up to 20s for in-flight requests.
4. `terminationGracePeriodSeconds: 30` covers the 5s + 20s with a 5s margin
   before SIGKILL.

The native `sleep` lifecycle action avoids depending on a shell or `sleep`
binary inside the image. Once an ALB is added, its deregistration delay and
this sleep need to be tuned together.

### Rollout, selectors, image reference

- Rolling update with `maxUnavailable: 0` and `maxSurge: 25%`. Replacement pods
  must pass readiness before old ones stop, so ready capacity never drops
  during a deploy. This needs spare cluster capacity for the surge pods.
- Selectors use `app.kubernetes.io/name`, `instance`, and `component: api`.
  They contain no version, because Deployment selectors are immutable. The
  Service selects `component: api` only, so the future CronJob's pods can never
  receive HTTP traffic.
- The image can be referenced by digest, which takes precedence, or by tag.
  Promoting a digest from staging to production guarantees production runs the
  exact image that was tested. The tag or digest is set at deploy time with
  `--set`, not stored in environment files.

## Remaining assignment work

- **Ingress and TLS**: an ALB through the AWS Load Balancer Controller, in IP
  target mode with an ACM certificate. This includes a public route for
  payment-provider webhooks; signature verification belongs in the app.
- **Secrets integration**: Postgres credentials and the webhook signing secret
  from AWS Secrets Manager, through External Secrets Operator or the Secrets
  Store CSI driver with EKS Pod Identity.
- **Autoscaling**: an HorizontalPodAutoscaler on CPU, with minimum and maximum
  replicas per environment, replacing the fixed replica counts. Limited by the
  Postgres connection budget.
- **Disruption protection**: a PodDisruptionBudget for node drains and cluster
  upgrades.
- **Topology placement**: topology spread across Availability Zones and nodes.
- **Daily CronJob**: the batch runs in its own pods, labelled
  `component: batch`, not inside API containers. It needs
  `concurrencyPolicy: Forbid`, deadlines, retry limits, and its own resources.
  Whether it reuses the API image is still open.
- **Delivery and monitoring plans**: proposed in
  [operations.md](operations.md). Nothing is provisioned yet.

Also not addressed yet: pinning the base image by digest, image vulnerability
scanning, NetworkPolicy, and multi-arch builds.
