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

The implementation snapshot for this submission is
[`73de0a8`](https://github.com/tegryan-ddo/fastapi-eks-takehome/commit/73de0a81352ed9bca79f8d4c9e0ad5d080481fce).
The submission branch adds documentation only. Results below are the recorded
GitHub commit statuses for that exact source revision, captured on
**28 October 2026 (Pacific time)**. This is a fixed snapshot; later work can
be reviewed as a separate update.

| Check | Recorded result |
| --- | --- |
| AWS CodeBuild `mobile-api-validate`: pytest, chart validation, secret scan | **Passed** — [build](https://us-east-1.console.aws.amazon.com/codebuild/home?region=us-east-1#/builds/mobile-api-validate:9c03ab36-c706-48c9-b3fe-984c73efdd04/view/new) |
| AWS CodeBuild `mobile-api-build`: tests, secret scan, container build/checks, ECR release step | **Passed** — [build](https://us-east-1.console.aws.amazon.com/codebuild/home?region=us-east-1#/builds/mobile-api-build:9c8d8f4c-73e8-473f-a97b-c7e58d52cb69/view/new) |
| AWS CodeBuild `mobile-api-db-admin`: create the RDS application role | **Failed** — [build](https://us-east-1.console.aws.amazon.com/codebuild/home?region=us-east-1#/builds/mobile-api-db-admin:3ce3d688-ea37-4da0-a4ed-f3be7e9d9eff/view/new); failure cause is not established in this snapshot |
| RDS integration, Kubernetes rollout, ALB/HTTPS, manual staging batch execution | **Not yet verified** |
| Production promotion, monitoring, alert routing, image vulnerability release gates | **Written design; not verified as deployed** |

The AWS console links require account access. The checked-in buildspecs and
tests describe what each build runs. A successful build is not evidence of a
successful EKS deployment. [Verification details](docs/verification.md) separate
unit, rendered-manifest, local-container, RDS, and cluster checks.

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
workloads. Staging deployment and secret-sync buildspecs are included, but
their successful live execution is not claimed above.

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
rollback-compatible migration process. Verify ingress/TLS and draining,
credential rotation, zone placement, representative load, and the alert
delivery path. Record the live RDS and staging results currently missing
from this submission before claiming operational readiness.

