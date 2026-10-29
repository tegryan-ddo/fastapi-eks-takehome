# FastAPI on Amazon EKS

This submission deploys a FastAPI service on port 8000 using Helm. The API,
schema migration, and daily batch job use one container image. PostgreSQL is
external to Kubernetes; an optional AWS CDK environment provisions RDS to
exercise that dependency.

Only the assignment brief was supplied. The included application demonstrates
health checks, PostgreSQL reads and writes, schema migration, shutdown, and
batch execution. It does not implement the employer's business logic or a
payment-provider integration. Its tests do not establish capacity at
2,000–3,000 requests per second.

## Submission snapshot and evidence

The implementation snapshot is
[`643dbb7`](https://github.com/tegryan-ddo/fastapi-eks-takehome/commit/643dbb755d4c18db2b12986c204c7fc0e28eb1d8).
This submission branch adds documentation only. The deployed application
reports version `0.1.0-61fb83d`; the subsequent code commits add metrics-server
configuration and refine the deployment check, without changing application
source. Results below were captured on **28 October 2026 (Pacific time)**.

The staging demonstration is available at
[mobile-api-staging.digitaldevops.io](https://mobile-api-staging.digitaldevops.io/).
Independent HTTPS checks returned 200 for `/`, `/health/ready`, and
`/health/live`; the root response identified `demo-api`,
`0.1.0-61fb83d`, and `demo: true`.

| Check | Source revision | Recorded result |
| --- | --- | --- |
| AWS CodeBuild validation | `643dbb7` | **Passed** — [build](https://us-east-1.console.aws.amazon.com/codebuild/home?region=us-east-1#/builds/mobile-api-validate:cb87b2e3-5aab-40ef-af95-9f7de6ae640f/view/new) |
| Release image build and container checks | `61fb83d` | **Passed** — [build](https://us-east-1.console.aws.amazon.com/codebuild/home?region=us-east-1#/builds/mobile-api-build:6b1a85ac-3fda-44f2-97b2-cd44c9a68dbd/view/new) |
| Release build after merging into `main` | `5d4e0d2` | **Passed** — [build](https://us-east-1.console.aws.amazon.com/codebuild/home?region=us-east-1#/builds/mobile-api-build:6441bd9f-ec0e-4595-8b4f-b2e813e4c0eb/view/new) |
| RDS application-role bootstrap | `73de0a8` | **Passed** — [build](https://us-east-1.console.aws.amazon.com/codebuild/home?region=us-east-1#/builds/mobile-api-db-admin:6f6c2137-f44a-4457-8706-53282bbf7e0a/view/new) |
| RDS migration and integration tests | `73de0a8` | **Passed** — [build](https://us-east-1.console.aws.amazon.com/codebuild/home?region=us-east-1#/builds/mobile-api-integration:d3bd2a66-e146-4c60-a4a8-800b0a0c0da0/view/new) |
| Runtime Secret sync | `73de0a8` | **Passed** — [build](https://us-east-1.console.aws.amazon.com/codebuild/home?region=us-east-1#/builds/mobile-api-secrets-sync:1de29198-37ef-41a8-bc68-9a2a9855dbad/view/new) |
| Staging deployment and its acceptance checks | `643dbb7` | **Passed** — [build](https://us-east-1.console.aws.amazon.com/codebuild/home?region=us-east-1#/builds/mobile-api-deploy-staging:f0526314-041d-423e-b294-8a67c39dae4f/view/new) |
| Production promotion, monitoring, alert routing, image vulnerability release gates | — | **Written design; not claimed as deployed** |

Build results are GitHub commit statuses reported by CodeBuild; the AWS
console links require account access. The staging buildspec checks rollout,
running-pod image digests, service identity/health, a database write/read, and
manual batch completion. The independent HTTPS checks above verify public
reachability and health only. Raw build logs, the exact deployed image digest,
and individual test reports were not independently inspected for this record.
The build agent's [recorded staging verification](https://github.com/tegryan-ddo/fastapi-eks-takehome/blob/d3fcc92b452c07174bbba0b6be6bed31818f44fb/docs/verification.md)
also reports a record surviving an image upgrade and two manual batch Jobs
for the same UTC business date. A separate read-only HTTPS check confirmed
that record 5 was present and that the batch summary for `2026-10-29` had
`record_count: 3` and `run_count: 2`. This demonstrates the demo's upsert
behavior, not the real application's business idempotency. Remaining load,
failure, and production scenarios are listed in
[verification details](docs/verification.md).

## Review and reproduce

Start with [chart defaults](charts/mobile-api/values.yaml), the
[templates](charts/mobile-api/templates), and the
[staging](environments/staging.yaml) /
[production](environments/production.yaml) overrides. This README is the short
decision document; [decisions](docs/decisions.md) and
[operations](docs/operations.md) contain the supporting detail.

Render both environments without AWS access:

```sh
helm lint charts/mobile-api -f environments/staging.yaml
helm lint charts/mobile-api -f environments/production.yaml
helm template mobile-api charts/mobile-api --namespace mobile-api-staging \
  -f environments/staging.yaml
helm template mobile-api charts/mobile-api --namespace mobile-api-production \
  -f environments/production.yaml
```

The [build and deployment guide](docs/setup.md) contains the local PostgreSQL
container example, verification commands, secret-loading instructions, and Helm
deployment command. [infra/](infra/README.md) is optional demonstration
infrastructure. The chart can consume an existing cluster, ingress controller,
runtime Secret, and external database as assumed by the brief.

## Deployment decisions

**Availability and scaling.** The Deployment uses rolling updates with
`maxUnavailable: 0` and `maxSurge: 25%`. The PDB permits one voluntary
disruption and selects API pods only. The Service also excludes batch and
migration pods. HPA owns the replica count: two to four pods in staging and
three to twelve in production, initially targeting 60% CPU utilization
against a 500m CPU request. These are starting values, not capacity claims.
CPU is an explicit assumption to confirm with the application team and
representative load tests; I/O-bound latency may need a different signal.
The default API pool allows five database connections per pod. Capacity
planning must include batch, migration, rollout, and terminating-pod
connections as well as steady-state replicas. Multi-AZ placement constraints
remain follow-up work.

**Health and shutdown.** Startup and liveness use `/health/live`, which never
checks PostgreSQL. Readiness uses `/health/ready` and a bounded database check
because the demonstration's useful data operations require PostgreSQL. A
shared database outage can make all pods unready; the application returns
controlled failures and must recover when connectivity returns. This policy
needs revisiting if the real service can provide useful degraded operation.
The image runs one Uvicorn worker. A five-second pre-stop delay and
twenty-second graceful shutdown fit within the thirty-second pod grace
period; actual ingress draining still needs cluster testing.

**Batch and migrations.** The CronJob uses a specified UTC schedule,
`concurrencyPolicy: Forbid`, bounded retries, and start/runtime deadlines.
Automatic scheduling is suspended in staging. Before promotion, create a
manual Job from the deployed template and inspect its output and replay
behavior. Manual Jobs are outside the CronJob's concurrency protection.
The demo upserts one summary row per business date and increments
`run_count` on replay. This does not prove the real job's business
idempotency. Schema migrations run in a pre-install/pre-upgrade Job, with
version tracking and a PostgreSQL advisory lock. The runtime Secret must
exist before that hook runs. Application rollback does not undo a database
migration, so production changes need compatible expand/contract migrations.

**Security and configuration.** Workloads run as UID 10001 with a read-only
root filesystem, a writable `/tmp`, dropped Linux capabilities, and no mounted
Kubernetes API token. Helm references secret names and keys; credentials are
not Helm values. The local loader prompts for credentials without echoing
them and writes to AWS Secrets Manager. A separate staging sync project
copies the runtime secret into Kubernetes; a secret change requires a
controlled rollout for pods using environment variables. Database TLS
verifies the server certificate and hostname using the public RDS CA bundle.
The demonstration EKS stack enables both public and private API access;
the production design uses a private endpoint and appropriately placed
deployment runners.

## Delivery

GitHub hosts the source; AWS CodeBuild performs validation and image builds.
The release build tags an ECR image by source commit and records its digest.
The chart accepts a digest and applies it to API, migration, and batch
workloads. Staging deployment and secret-sync buildspecs are included, with successful
execution recorded above.

The proposed production flow is CodeConnections → CodePipeline →
validation/build → staging → verification → manual approval → production.
Promote the same tested image digest and chart revision. Staging acceptance
includes the expected application version, a database write/read, a manual
batch run, and verification of the business result. Serialize deployments;
use explicit rollback behavior and stop on failed verification. Keep
production permissions and runtime secrets separate from staging.
[The delivery design](docs/operations.md) covers IAM/RBAC, network access,
security gates, and migration/rollback limits.

## Monitoring and paging

Monitoring is a written design, as requested by the brief. CloudWatch would
combine ALB request errors and latency, EKS workload metrics, application
logs/traces, and an external HTTPS synthetic check. Application metrics must
also report webhook processing outcomes and lag, database pool waits, and
the last successful batch business period. HTTP success alone does not prove
a payment completed, and Job completion alone does not prove correct
business output.

Page at 3am for sustained customer-visible failures or fast SLO burn,
confirmed service unavailability, payments failing or falling behind their
agreed deadline, or a batch completion deadline being missed. Include a
runbook and a clear owner with each page. Investigate isolated recovered
restarts, rising resource use without user impact, slow capacity trends,
and staging/build failures during working hours. Alert thresholds, SLO
targets, payment deadlines, and on-call routing need agreement with the
application team. [The monitoring proposal](docs/operations.md#6-monitoring)
provides the signal and routing detail.

## Scope, omissions, and questions before production

The priority was workload behavior, bounded failure handling, safe
configuration, reproducible checks, and explicit operational decisions.
The demonstration infrastructure makes the external dependency testable;
database tuning remains out of scope.

Real provider signature verification, durable acceptance, duplicate handling,
ordering, and retries require the provider contract and application code.
The demo does not acknowledge or process payment webhooks. Production
throughput, topology spread, NetworkPolicy, base-image digest pinning,
production infrastructure, full pipeline automation, observability
installation, and enforceable vulnerability release gates remain unfinished.
Secret scanning and functional tests are implemented; that does not imply
the proposed image/source vulnerability gates are active.

Before go-live, agree the availability and latency objectives, webhook
durability and retry contract, batch schedule/business-date semantics and
deadline, database connection budget, permitted degraded behavior, and
rollback-compatible migration process. The staging HTTPS path and recorded deployment checks have passed. Verify
production ingress and draining, credential rotation, zone placement,
representative load, and the alert delivery path before claiming production
readiness.


