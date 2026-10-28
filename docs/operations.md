# Delivery and operations proposal

**Status: mostly proposal.** Provisioned by [infra/](../infra/README.md) (CDK):
the ECR repository, the CodeConnections connection, the validate and build
CodeBuild projects, the staging VPC with its RDS PostgreSQL instance, and the
two VPC-connected runner projects (db-admin and integration). In progress, and
subject to change: an EKS Auto Mode cluster `mobile-api-staging` in the same
VPC, a `mobile-api-deploy-staging` project that runs Helm, and a
`mobile-api-secrets-sync` project that writes the Kubernetes Secret from
Secrets Manager. Nothing else in this document exists: no CodePipeline,
production deploy project, VPC endpoint, dashboard, or alarm. The table at the
end separates implemented, provisioned, and proposed.

GitHub hosts the source code and is how the repository is shared. Everything
that builds, checks, or deploys runs in AWS. No GitHub Actions workflows and
no GitHub-to-AWS OIDC federation are used.

## 1. Release flow

```text
GitHub ──AWS CodeConnections──▶ CodePipeline V2, execution mode QUEUED
                                artifacts in S3, images in ECR

Stage      Actions, in order                                   Runs in
---------  --------------------------------------------------  ---------------------------
Source     fetch commit C                                      CodeConnections
Build      build-and-validate → image-gate → package           build project (build role)
Staging    deploy-staging → verify-staging → approve           staging deploy project,
                                                               then a manual approval
Production image-gate (re-check) → deploy-production           production deploy project
           → verify-production
```

### Steps

| Step | What happens |
| --- | --- |
| **Source** | CodeConnections delivers one specific commit, C, from the GitHub repository. Every later step works from artifacts derived from C. |
| **Build and validate** | The build project (`mobile-api-build`, [buildspecs/build.yml](../buildspecs/build.yml)) runs `python -m pytest`: application tests, and chart checks (lint and render for both environments, schema validation, relationship checks). It then runs `scripts/verify_container.py`, which builds the image and checks it under the chart's restrictions against a PostgreSQL container: endpoints, a write and a read across a container replacement, a batch run and its replay, readiness during a database outage and after recovery, SIGTERM shutdown, and invalid batch input (see [verification.md](verification.md)). Finally it pushes the image to ECR tagged with the commit SHA and prints `release.json` with the digest, D. |
| **Image gate** | Blocks the release until Inspector has scanned digest D and it has no unapproved critical or high findings (section 4). |
| **Package** | Produces release artifact R: `helm package` of the chart, versioned `<chart version>+<short commit>`; `values/staging.yaml` and `values/production.yaml` taken from commit C; `release-values.yaml` setting `image.digest: D`; and `release.json` recording the commit, chart version, image digest, and pipeline execution ID. |
| **Deploy staging** | The staging deploy project runs Helm with R only. The migration hook Job runs first, inside `helm upgrade`; if it fails, no pod changes. |
| **Verify staging** | Checks rollout health and application behaviour (below), and writes a verification report to S3. |
| **Approve production** | A CodePipeline manual approval. `CustomData` names the commit, chart version, and digest; `ExternalEntityLink` points to the staging verification report; `NotificationArn` alerts approvers through SNS. Only a release-approver IAM group may call `codepipeline:PutApprovalResult`. The timeout is shortened from the 7-day default to 24 hours, so stale releases expire. |
| **Deploy production** | Re-checks the image gate (continuous scanning may have found new issues), then deploys the same artifact R and digest D with `values/production.yaml` from R. |
| **Verify production** | Confirms rollout completion, runs the HTTPS smoke test, and requires the production alarms to stay out of `ALARM` for a 10-minute bake period before the release counts as successful. |

Both deploy projects run the same command, using files from R only:

```sh
HELM_DRIVER=configmap helm upgrade --install mobile-api ./mobile-api-*.tgz \
  --namespace "$NAMESPACE" \
  -f "values/$ENVIRONMENT.yaml" -f release-values.yaml \
  --rollback-on-failure --wait=watcher --timeout 10m --history-max 20
```

These are Helm 4 flags. In Helm 4, `--rollback-on-failure` replaces
`--atomic`, and without it `upgrade` does not wait for resources. With Helm 3,
use `--atomic --wait`. `CLUSTER_NAME`, `NAMESPACE`, and `ENVIRONMENT` are fixed
in each deploy project's configuration, not taken from the artifact, so a
release cannot redirect itself to another environment. The namespaces are
`mobile-api-staging` and `mobile-api-production`. `HELM_DRIVER=configmap` is
explained in section 2.

**No rebuild, no re-fetch.** Promotion reuses R and D. The deploy projects
never build an image and never read Git, so production receives exactly the
bytes that were verified in staging. The deploy buildspec travels inside R, so
the deployment logic also comes from the reviewed commit.

### Staging verification

Checks that also work with the demo:

- **Migration:** `helm upgrade` succeeded, which means the pre-upgrade hook Job
  completed. Helm deletes a successful hook Job, so a surviving
  `job/mobile-api-migrate` is a failed one: read its pod logs.
- **Rollout:**
  - `kubectl rollout status deployment/mobile-api` completes;
  - every API pod is Ready;
  - each running pod's `imageID` contains digest D;
  - no pod restarted after the rollout;
  - the Helm release status is `deployed`.
- **Behaviour:** `python scripts/smoke_test.py https://<staging-host>
  --expect-service <name> --expect-version <version>`, run through the public
  HTTPS hostname, the same path customers use. Then `POST /demo/records` with a
  unique note, `GET /demo/records/{id}`, `kubectl rollout restart
  deployment/mobile-api`, and the same `GET` again: the record outlives the
  pods.
- **Batch:** the CronJob is suspended in staging. A Job created manually from
  `cronjob/mobile-api-batch` for one business date must complete using digest
  D; a second Job for the same date must complete too, and
  `GET /demo/batch-runs/<date>` must show one row with `run_count: 2`. The
  procedure is in [verification.md](verification.md#manual-staging-batch-run).

Required for the real application, specified in
[verification.md](verification.md#staging-acceptance-plan):

- the real schema's migrations against staging data;
- duplicate and invalid-signature webhooks;
- the expected business output of the batch, and a safe replay of the same
  business period (the demo's `batch_runs` upsert shows only the demo's
  replay);
- a rollout while requests are in flight;
- an unhealthy replacement that never becomes ready;
- a database outage: readiness fails, liveness holds, recovery is automatic.

Only the smoke test and `tests/integration` exist as code today. The rollout
and batch commands are proposed, and none has run against a cluster. The
integration tests run against RDS from the `mobile-api-integration` runner
(section 3), not from a cluster.

### Why QUEUED execution, and why stages are grouped this way

CodePipeline locks a **stage**, not an action. While an execution is inside a
stage, later executions wait at the stage's entrance. A stage holding a manual
approval stays locked until the approval is approved, rejected, or times out.
In QUEUED mode, executions are processed in arrival order and never overtake
each other.

This is why deploy, verify, and approve share one Staging stage. While
reviewers consider release N, release N+1 can build and package, but it cannot
redeploy staging and change what is being reviewed. Production deployments
happen strictly in commit order, so an older release can never overwrite a
newer one. In SUPERSEDED mode, by contrast, a newer execution waiting between
stages replaces an older one.

The cost is serialization. A release waiting for approval blocks later releases
from reaching staging, so reject it or let it time out to unblock the queue.

**Independent validation builds** run outside the release pipeline. A separate
CodeBuild project, triggered by pull-request events through CodeConnections,
runs [`buildspecs/validate.yml`](../buildspecs/validate.yml) concurrently for any
branch. It uses a minimal role (logs and reports only), pushes nothing, and has
no deploy permissions.

## 2. Permissions

CodeBuild obtains temporary credentials for its service role automatically. No
long-lived access keys exist, and none are stored in GitHub.

| Role | Used by | Allows | Does not allow |
| --- | --- | --- | --- |
| Build | build-and-validate, image gate, package | ECR push and read on the `mobile-api` repository only (plus `ecr:GetAuthorizationToken`); `ecr:DescribeImageScanFindings`; read and write on the pipeline artifact bucket and its KMS key; its own CloudWatch log group; CodeBuild reports | Any `eks:*` action. It has no EKS access entry, so it cannot authenticate to any cluster. |
| Staging deploy | deploy-staging, verify-staging | `eks:DescribeCluster` on the staging cluster; read artifacts and write verification reports in S3; KMS decrypt; its log group; VPC network-interface permissions | ECR push; production cluster or namespace |
| Production deploy | image re-check, deploy-production, verify-production | The same as staging deploy, but for production, plus `ecr:DescribeImageScanFindings` and `cloudwatch:DescribeAlarms` | ECR push; staging |
| Validation | pull-request validation project | Its log group and report group | Everything else |
| DB admin (`mobile-api-db-admin`) | one-off creation of the application role in RDS | Read `/mobile-api/staging/rds-master` and the `DATABASE_URL` key of `/mobile-api/staging/runtime`; VPC network interfaces; its log group | ECR, EKS, S3 |
| Integration (`mobile-api-integration`) | `python -m app.migrate` and `tests/integration` against RDS | Read `DATABASE_URL` from `/mobile-api/staging/runtime`; VPC network interfaces; its log group and report group | The master secret; ECR push; EKS |

The build, validation, db-admin, and integration roles are provisioned by
[infra/](../infra/README.md) (CDK). The two deploy roles are proposed.

Each role trusts `codebuild.amazonaws.com` with an `aws:SourceArn` condition
naming its own project. The staging project therefore cannot use the
production role. The CodePipeline service role may start these projects, use
the CodeConnections connection, read and write artifacts, and publish approval
notifications.

**Kubernetes authorization** uses EKS access entries mapped to Kubernetes
RBAC. The cluster's authentication mode must be `API` or
`API_AND_CONFIG_MAP`, which is an assumption. Each deploy role gets a standard
access entry with `kubernetesGroups` set to its environment's group:
`mobile-api-staging-deployers` or `mobile-api-production-deployers`. No EKS
access policy is associated, so
[`platform/deploy-rbac.yaml`](../platform/deploy-rbac.yaml) is the role's only
Kubernetes permission.

That file holds one `ClusterRole`, which grants nothing by itself. It is bound
by a `RoleBinding` in each environment's namespace, so each group can act only
in its own namespace. The role allows:

- full management of only the kinds the chart renders: ServiceAccounts,
  Services, Deployments, HPAs, PDBs, and CronJobs;
- Jobs, for the migration hook and for manual staging batch runs;
- ConfigMaps, for Helm's release history;
- read-only access to ReplicaSets, Pods, Events, and pod logs, to wait for and
  verify rollouts and Jobs.

It does not allow:

- Secrets;
- RBAC objects;
- pod `exec`, `attach`, or `port-forward`;
- service-account impersonation;
- cluster-scoped resources, including creating namespaces.

A cluster administrator creates the namespaces and applies the RBAC once. The
pipeline never passes `--create-namespace` and cannot change its own
permissions. This is tighter than the managed `AmazonEKSEditPolicy`, which
would also allow Secrets, `exec`, `port-forward`, and impersonating any service
account in the namespace.

The deploy projects set `HELM_DRIVER=configmap`, so Helm stores release
history in ConfigMaps instead of Secrets. That is why the deployer needs no
access to Secrets at all, including the application credentials the secrets
integration places in the namespace. Pods still receive those credentials: the
kubelet resolves `secretKeyRef` with its own authorization, not the
deployer's.

Trade-offs of this tighter option:

- **New kinds need an RBAC change.** Each kind the chart adds needs a
  cluster-administrator change, for example an Ingress, or custom resources
  such as `ExternalSecret`. The chart tests fail until the role covers every
  rendered kind, so the gap shows up in a pull request, not in a failed
  deployment.
- **Release history is readable.** Anyone who can read ConfigMaps in the
  namespace can read Helm's release history. This is acceptable because values
  and manifests never contain credentials.
- **The driver is hard to change later.** Changing Helm's storage driver later
  means migrating release history, so choose it before the first install.

### Application secrets

Uploading a secret to AWS Secrets Manager does **not** make it available to a
workload. These are separate steps with separate owners.

1. **Upload, by an operator.** `scripts/load_secrets.py` writes one secret per
   environment, `/mobile-api/<environment>/runtime`, holding the JSON keys
   `DATABASE_URL` and `PAYMENTS_WEBHOOK_SECRET`. `DATABASE_URL` is
   `postgresql://mobile_api_app:<password>@<rds-endpoint>:5432/mobileapi?sslmode=verify-full`,
   where the operator chooses the password for the application role.
   - **Where it runs:** in the operator's own trusted terminal, signed in with
     an AWS SSO profile. Never in CI, the pipeline, or an AI agent's terminal.
   - **How values are handled:** each value is typed twice at a hidden prompt
     and sent straight to Secrets Manager. Values never appear in arguments,
     environment variables, files, or output.
   - **Safeguards:** it refuses to run without an interactive terminal,
     confirms the AWS account and the destination name, ignores endpoint
     overrides, never reads a value back, and suppresses SDK logs and raw
     errors.
   - **Operator IAM:** `secretsmanager:DescribeSecret`, plus `CreateSecret` to
     create or `PutSecretValue` to replace, scoped to that environment's secret
     names, plus KMS permissions if a customer-managed key is used.
   - **Unconfirmed writes:** if AWS doesn't confirm a write, the tool prints the
     version token it used. Check that version with `ListSecretVersionIds`
     before writing again.
   - **Limits:** values exist in the process's memory while being sent. A
     compromised workstation or a recorded terminal session can still expose
     them.
2. **Create the database role, once (`mobile-api-db-admin`, provisioned).**
   An operator starts this CodeBuild project after step 1. Inside the VPC it
   connects as the RDS master user and runs
   `platform/db/create_app_user.sql`, which creates `mobile_api_app` with the
   password from the secret loaded in step 1 and grants it `CREATE` on schema
   `public`. The master secret, `/mobile-api/staging/rds-master`, is
   generated by RDS and read by nothing else.
3. **Sync, by the cluster's existing integration (assumed).**
   - **What we assume:** the platform runs External Secrets Operator,
     authenticated with EKS Pod Identity. It maintains a Kubernetes Secret
     `mobile-api-runtime` in each environment's namespace, holding the same two
     keys from `/mobile-api/<environment>/runtime`, and refreshes it on its
     configured interval.
   - **Permissions:** only its role holds `secretsmanager:GetSecretValue`, and
     only on `/mobile-api/<environment>/*`.
   - **Ownership:** neither this chart nor the pipeline manages it.
   - **If the integration differs** (for example the Secrets Store CSI driver),
     only `runtimeSecret.name` and the restart behaviour below change.
   - **This exercise's stand-in:** the `mobile-api-secrets-sync` CodeBuild
     project (infra/, in progress) reads `/mobile-api/staging/runtime` and
     writes Secret `mobile-api-runtime` in the staging namespace. It does not
     refresh on its own; run it again after the secret changes.
4. **Consume, through the chart.**
   - **How:** with `runtimeSecret.name: mobile-api-runtime` (the default), the
     API container reads both keys as environment variables through
     `secretKeyRef`. The batch and the migration hook read only
     `DATABASE_URL`.
   - **What's in Git:** only the Secret's name and key names appear in values
     or manifests.
   - **Required:** PostgreSQL is required, so the Secret must exist before the
     first install; the migration hook reads it. With an empty name the chart
     renders no references, and every workload fails at startup with a clear
     configuration error rather than running without a database.

**After a secret changes:**

- **API:** environment variables are read when a container starts. Once the
  Kubernetes Secret shows the new version, restart the API with `kubectl
  rollout restart deployment/mobile-api -n <namespace>`. This is a normal
  rolling update with `maxUnavailable: 0`.
- **Batch and migration:** the next Job reads the new value. A Job already
  running keeps the old one.
- **Rotation:** a PostgreSQL role holds one password, so changing it breaks
  every running pod until it restarts. Rotate without downtime by alternating
  between two application roles (set the new role's URL in the secret, let it
  sync, restart, then retire the old role), or accept a short window in which
  readiness fails until the rollout restart completes. The webhook-secret
  rotation procedure depends on the payment-provider contract (section 7).

None of the pipeline or deploy roles can read application secrets: they have
no `GetSecretValue`, and the deployer role has no Kubernetes Secrets access.
The two VPC runner roles are the deliberate exception: db-admin reads the
master secret and `DATABASE_URL` once to create the role, and the integration
runner reads `DATABASE_URL`.

## 3. Networking for a private EKS API endpoint

Image building stays in the build project. That is the only project with
privileged mode, because Docker needs it, and it needs no cluster access. The
deploy projects are unprivileged, have no Docker, and run in the VPC:

- **Subnets:** private subnets in the cluster VPC. A peered or Transit Gateway
  VPC also works, but needs the DNS step below.
- **Security groups:** the deploy projects use their own group with egress on
  443. The cluster's control-plane security group allows inbound TCP 443 from
  that group.
- **DNS:** the VPC needs `enableDnsSupport` and `enableDnsHostnames`, so the
  cluster endpoint name resolves to the private endpoint addresses. Outside
  the cluster VPC, Route 53 Resolver rules are needed to forward that name.
- **AWS dependencies:** use the VPC's existing NAT gateway if it has one.
  Otherwise add endpoints:

| Dependency | Needed for | Endpoint |
| --- | --- | --- |
| S3 | Pipeline artifacts; ECR image layers | Gateway endpoint |
| ECR API and ECR Docker registry | The deploy-tools build image; the image re-check | Interface: `ecr.api`, `ecr.dkr` |
| CloudWatch Logs | Build logs | Interface: `logs` |
| EKS | `DescribeCluster` for the kubeconfig | Interface: `eks` |
| CloudWatch | Alarm checks during production verification | Interface: `monitoring` |
| STS | Only if a role is assumed explicitly (`aws eks get-token` signs locally) | Interface: `sts` (optional) |
| Staging and production HTTPS hostnames | Smoke tests through a public ALB | NAT, or an internal ALB reachable in the VPC |

Helm and kubectl come from a pinned deploy-tools image in ECR, so the deploy
projects need no internet access. If the EKS endpoint is public with a CIDR
allowlist instead, keep the same VPC configuration and allow the NAT gateway's
Elastic IP.

### RDS environment

Provisioned by [infra/](../infra/README.md) (CDK) to reproduce the external
database for this exercise. Database tuning is out of scope.

| Item | Setting |
| --- | --- |
| Network | VPC `mobile-api`, private subnets, one NAT gateway for the runners' outbound access (PyPI, GitHub, ECR, Secrets Manager) |
| Instance | RDS for PostgreSQL 17 `mobile-api-staging`, `db.t4g.micro`, storage encrypted, not publicly accessible, database `mobileapi` |
| Inbound | TCP 5432 only from the integration-runner security group and from the workloads of the EKS Auto Mode cluster `mobile-api-staging` being provisioned in the same VPC. Nothing else, including the build project, can reach it. |
| TLS | Server certificate from RDS CA `rds-ca-rsa2048-g1`. Clients use `sslmode=verify-full`, verifying certificate and hostname against the RDS global bundle baked into the image. |
| Master user | `postgres_admin`; credentials generated by RDS into Secrets Manager `/mobile-api/staging/rds-master`. Used only by the one-off `mobile-api-db-admin` runner, never by the application, the pipeline, or in this conversation. |
| Application role | `mobile_api_app`, created by `platform/db/create_app_user.sql` with the password the operator loaded into `/mobile-api/staging/runtime`. Owns the application's schema objects and has `CREATE` on schema `public`, so migrations run as this role. |

How things reach it:

- **Application pods** connect from the EKS cluster over the VPC: the cluster
  shares this VPC, the endpoint's private DNS name resolves from it, and the
  pods' security group is the one allowed in. The cluster is still being
  provisioned, so this path is untested.
- **Integration runner** (`mobile-api-integration`) runs in the private subnets
  with the allowed security group. CodeBuild injects `DATABASE_URL` from
  Secrets Manager at build start; the runner applies the migrations, then runs
  `tests/integration`.
- **db-admin runner** (`mobile-api-db-admin`) uses the same network path once,
  as the master user.
- **Nothing else.** No bastion, no public endpoint, no developer workstations.
  Ad-hoc SQL goes through a one-off runner build.

Production has no database yet; its connection budget and pooler decision are
in section 7.

## 4. Security gates

### Image gate: ECR enhanced scanning with Amazon Inspector

The ECR repository uses enhanced scanning (scan on push, then continuous
rescanning) and immutable tags. The gate runs after the push, and again at the
start of the Production stage:

1. The input is digest D from `release.json`, never a tag.
2. Poll `aws ecr describe-image-scan-findings --repository-name mobile-api
   --image-id imageDigest=D` while `imageScanStatus.status` is `PENDING` or
   `IN_PROGRESS`, for up to 20 minutes. Enhanced scanning has no manual scan,
   so the gate waits for scan-on-push.
3. `ACTIVE` means the scan is complete. Any other status, or a timeout or API
   error, fails the gate as **"scan unavailable"**. This covers `FAILED`,
   `UNSUPPORTED_IMAGE`, `FINDINGS_UNAVAILABLE`, `SCAN_ELIGIBILITY_EXPIRED`,
   and `LIMIT_EXCEEDED`. A missing or failed scan is never reported as clean.
4. Page through all findings. Block on any `CRITICAL` or `HIGH` finding not
   covered by a valid exception, reported as **"findings present"**, which is
   distinct from "scan unavailable".
5. Record the status, counts by severity, and exceptions applied in the
   verification report that the approval links to.

**Exceptions** live in a reviewed file, `security/image-exceptions.yaml`
(proposed, not created). Each entry holds the vulnerability ID, package,
reason, owner, approver, and an expiry date no more than 90 days out. The gate
ignores entries that are expired or missing an owner or expiry, so those
findings block again.

**Support:** Inspector ECR scanning supports Debian 13, the base of
`python:3.13-slim`, and lists Python as a supported language. That it detects
the pip-installed packages in this image is not yet confirmed. Check the first
scan's inventory for `fastapi` and `uvicorn`.

**Local evidence, and a decision this forces.** Trivy 0.76.1 (a local stand-in;
Inspector's results will differ) scanned the demo image on 2026-10-28:

- **Critical:** none.
- **High, fixable:** none. Five came from pip's vendored libraries; pip is now
  removed from the runtime image.
- **High, unfixable:** 44, all in Debian base-image packages with no fixed
  version (util-linux, ncurses, systemd libraries, perl-base).

Under the rule above, every release would block until each unfixable finding
has an owned, expiring exception. The choice is between keeping that strict
rule, with per-CVE exceptions renewed as Debian ships fixes, or blocking only
findings with a fix available, while tracking unfixable ones on the dashboard.
This is listed in section 7.

### Source analysis: Amazon Inspector Code Security

Connect the GitHub repository through Inspector Code Security's GitHub
integration, for SAST, dependency (SCA), and IaC scanning on pull requests and
on a schedule.

**Support check:** AWS documents SAST and SCA for "Python 3.13 or earlier". The
service therefore targets Python 3.13 rather than 3.14 (see
[decisions.md](decisions.md)). The documentation does not name the Python
dependency-manifest formats SCA reads, so coverage of `requirements.txt` is
unconfirmed.

Until a trial scan shows dependency findings for this repository, treat Code
Security's SCA results as advisory. The image gate still covers
known-vulnerable dependencies installed in the image.

## 5. Rollback and migrations

**Preserved for rollback:**

- S3 artifact versioning, and a lifecycle rule keeping release artifacts for
  90 days.
- ECR immutable tags, and a lifecycle rule that keeps the last 30 images and
  never expires images by age alone.
- Helm history, keeping 20 revisions per namespace.
- `release.json` in every artifact, linking commit, chart version, and digest.

**Restoring the previous compatible release:**

1. **Preferred:** CodePipeline stage rollback on the Production stage. This
   starts a new execution using the artifacts of the last successful
   execution, so release R(n-1) and digest D(n-1) go through the same deploy
   project and the same verification. Production can also roll back
   automatically when verification fails (`onFailure: ROLLBACK`).
   - Rollback cannot target the source stage, or an execution that was itself
     a rollback.
   - Editing the pipeline invalidates earlier rollback targets.
2. **Fallback:** if the pipeline is unavailable, run `HELM_DRIVER=configmap helm
   rollback mobile-api <revision> --namespace mobile-api-production --wait` from
   the production deploy project, and record it manually.

Afterwards, run the production verification again: the rollout completes,
running pods use D(n-1), the smoke test passes, and the alarms clear.

**Rollback restores code and manifests only.** It does not:

- reverse database migrations or data written by the faulty release;
- undo payment effects, such as captures, refunds, or webhook events already
  acknowledged, which the provider will not resend;
- undo batch output.

Those need a forward fix and reconciliation against the payment provider's
records.

**Migrations** are a controlled release step, not part of API startup. With 15
or more replicas racing to migrate, startup and readiness would become
unpredictable.

- The chart runs the migration as a Helm `pre-install,pre-upgrade` hook Job
  with the same digest D (`templates/migrate-job.yaml`, implemented). Helm
  creates it before any other resource changes and waits for it. It has
  `backoffLimit: 0` and a 10-minute deadline. If it fails, `helm upgrade`
  fails before any API pod changes, and the failed Job stays for diagnosis.
  The deployer role already covers Jobs and pod logs.
- A hook Job is not part of the release's rollback: `--rollback-on-failure`
  restores the previous manifests, not the schema. Backward-compatible changes
  are what make that safe.
- Schema changes are backward compatible (expand, then contract):
  - A release may only add: new tables, nullable columns, and indexes built
    concurrently.
  - The code works with both the old and the new schema.
  - Drops and renames ship in a later release, once neither the running
    version nor the rollback target uses the old shape.

  This is what makes rollback by one release safe.
- Every migration runs in staging first, so the production approval covers it.

## 6. Monitoring

Everything here is proposed. Nothing is configured yet.

**Logs.** CodeBuild writes build and deploy logs to one CloudWatch Logs group
per project. CodePipeline execution events go through EventBridge to SNS for
failures and pending approvals. The Amazon CloudWatch Observability EKS add-on
(Fluent Bit) ships container output to CloudWatch Logs. The application should
log JSON lines with request ID, route, status, and latency. It must never log
payloads, card data, secrets, or signatures. Every log group has a retention
period set.

**What AWS already measures, and what needs application instrumentation:**

| Signal | Source | State |
| --- | --- | --- |
| Request count, target and load-balancer 5xx, target response time, healthy and unhealthy hosts | ALB metrics | Available once the ALB exists; no app change |
| Pod CPU and memory against requests, restarts, running pods | Container Insights (Observability add-on) | Available once the add-on is installed |
| Database: connections, CPU, free storage and memory, read and write latency | RDS CloudWatch metrics (`DatabaseConnections`, `CPUUtilization`, `FreeStorageSpace`, `FreeableMemory`, `ReadLatency`, `WriteLatency`) | Available now for the staging instance; no app change. There is no read replica, so no replica-lag metric. |
| Per-operation latency, errors, and faults; SLOs | CloudWatch Application Signals: OpenTelemetry auto-instrumentation from the add-on, enabled by the pod annotation `instrumentation.opentelemetry.io/inject-python: "true"` | Supports Python 3.10–3.14. The single Uvicorn worker avoids the documented pre-fork caveat. |
| Build and deploy outcome and duration | CodeBuild metrics, CodePipeline events | Available once the pipeline exists |
| Payment completion: webhooks received, signature-rejected, processed, failed, duplicate; processing lag | Application metrics (CloudWatch Embedded Metric Format) | **Needs instrumentation** |
| Database pool: wait time, connections in use, checkout timeouts | Application metrics | **Needs instrumentation** |
| Batch freshness: last successful completion time, business period, records processed | Application metric at batch end | **Needs instrumentation** |

The ALB can show that a webhook request received a 2xx. Only the application
can show that the payment was processed. Likewise, a Job reported as
`Succeeded` does not prove the batch produced correct output.

**Request-based SLOs and burn-rate alerts** use Application Signals.

- **Availability SLO:** good requests (non-5xx) divided by valid requests.
- **Latency SLO:** requests under a threshold, divided by all requests.
- **Scope:** both SLOs apply to the main API operations and to the webhook
  endpoint.
- **Burn-rate alarms:**
  - Urgent: 1-hour window at 14.4× budget burn, and 6-hour window at 6×.
  - Working hours: 3-day window at 1×.

Each look-back window must be a multiple of the SLO period and shorter than the
SLO interval. The targets themselves are unresolved (section 7).

**Synthetic check.** A CloudWatch Synthetics canary sends HTTPS `GET
/health/ready` and `GET /` to each environment every 5 minutes, checking the
status and the service identity.

- **Safe by design:** read-only requests, no credentials, no webhook or payment
  calls, and no database writes. A distinct User-Agent keeps canary traffic out
  of business metrics.
- **What it catches:** DNS, certificate, load-balancer, and ingress failures
  that in-cluster checks miss, including overnight when real traffic is low.

**Dashboards.** One CloudWatch dashboard per environment shows:

- traffic and errors;
- latency percentiles;
- SLO status and burn rate;
- healthy targets and pod count;
- CPU and memory against requests, and restarts;
- the webhook pipeline;
- database pool waits;
- batch freshness;
- deploy markers.

**Alarm routing** sends alarms through SNS to the agreed operational
destination, which is still unresolved:

| Class | SNS topic | Examples |
| --- | --- | --- |
| **Urgent:** customer or business impact now. Production only, at any hour. | `mobile-api-production-urgent` | SLO fast burn. Canary failing twice in a row. Healthy targets below minimum. Webhook processing failures or lag over threshold (payments not completing). Batch not finished by its deadline. Every pod unready, or `DatabaseConnections` at the instance limit. Failed production verification whose rollback also failed. |
| **Working hours:** investigate, no immediate impact. | `mobile-api-<env>-ops` | SLO slow burn. HPA held at maxReplicas (capacity, or the database connection budget). A failed batch Job that can still finish within its deadline. Restarts or OOM kills. CPU or memory near requests. Rising database pool waits. `DatabaseConnections` above 80% of the limit. `FreeStorageSpace` below 20%. Batch slower than usual but within its deadline. Staging deploy or verify failures. Validation build failures. Image-gate failures. New critical finding on a running image. |

Staging alarms go only to the working-hours topic. Thresholds use rates rather
than counts, because overnight traffic is a tenth of peak. A missing canary
data point counts as breaching; missing 5xx data does not.

## 7. Unresolved inputs

- **Availability target.** The SLO percentage and its window.
- **Latency targets.** Per operation and percentile.
- **Payment-provider contract:**
  - webhook signature scheme;
  - retry schedule and maximum duration;
  - acknowledgement timeout;
  - ordering and duplicate guarantees;
  - source IP ranges;
  - sandbox support.
- **Database connection budget:**
  - the staging instance is a `db.t4g.micro`, whose `max_connections` is low;
    `maxReplicas × DATABASE_POOL_MAX (5)` plus the batch and the runners must
    fit, and the production instance class is unknown;
  - whether a pooler or RDS Proxy is needed;
  - the production database, which is not provisioned.
- **EKS workload security group**, for the RDS inbound rule.
- **Batch contract.**
  - production schedule and time zone;
  - business-date semantics (the demo defaults to the current UTC date);
  - expected duration;
  - completion deadline;
  - which key identifies a business period for safe replay.
- **Image-gate policy for unfixable base-image CVEs** (section 4).
- **Operational destination.** Where SNS delivers, and who is on call.
- **Platform:**
  - one cluster with two namespaces, or separate clusters;
  - EKS endpoint access: private or public;
  - whether NAT is available;
  - whether the cluster's authentication mode supports access entries.
- **Dependency scanning.** Whether Inspector Code Security reads
  `requirements.txt`.

## 8. Implemented versus proposed

| Item | State |
| --- | --- |
| Chart (API, HPA, PDB, CronJob, migration hook Job, Ingress off by default, secret references) and environment values | Implemented. Linted, rendered, schema-validated, and relationship-checked. Not installed on a cluster. |
| EKS Auto Mode cluster `mobile-api-staging`, `mobile-api-deploy-staging`, `mobile-api-secrets-sync` | Being provisioned by infra/ (CDK); in progress. Nothing deployed to it yet. |
| Demo application (PostgreSQL persistence, migration, batch replay) and container image | Implemented. Unit-tested, built, and checked locally with Podman against a PostgreSQL 17 container. |
| Chart, app, integration, and secrets-loader tests; container check; smoke test; secret scan; buildspecs | Implemented and run locally. |
| ECR repository, CodeConnections connection, CodeBuild validate and build projects | Provisioned by infra/ (CDK). |
| Staging VPC, RDS PostgreSQL 17 instance, db-admin and integration runners | Provisioned by infra/ (CDK). Integration tests against RDS pending the operator steps. |
| Secrets loader (`scripts/load_secrets.py`) | Implemented. Offline tests only. Never run against AWS by the agent. |
| Deployer RBAC (`platform/deploy-rbac.yaml`) | Written, schema-validated, and checked against the rendered chart. Not applied to any cluster. |
| Everything else in this document | Proposed only |
