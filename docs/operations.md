# Delivery and operations proposal

**Status: proposal.** None of the AWS resources described here exists yet:
no CodePipeline, CodeBuild project, IAM role, VPC endpoint, ECR repository,
dashboard, or alarm has been provisioned or tested. The table at the end
separates what is implemented in this repository from what is proposed.

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
| **Build and validate** | The build project runs the application tests and the chart checks (lint and render for both environments, schema validation, relationship checks; see [verification.md](verification.md)). It then builds the image, runs it locally with the chart's security restrictions, smoke-tests it, and pushes it to ECR. The output is the image digest, D. |
| **Image gate** | Blocks the release until Inspector has scanned digest D and it has no unapproved critical or high findings (section 4). |
| **Package** | Produces release artifact R: `helm package` of the chart, versioned `<chart version>+<short commit>`; `values/staging.yaml` and `values/production.yaml` taken from commit C; `release-values.yaml` setting `image.digest: D`; and `release.json` recording the commit, chart version, image digest, and pipeline execution ID. |
| **Deploy staging** | The staging deploy project runs Helm with R only. |
| **Verify staging** | Checks rollout health and application behaviour (below), and writes a verification report to S3. |
| **Approve production** | A CodePipeline manual approval. `CustomData` names the commit, chart version, and digest; `ExternalEntityLink` points to the staging verification report; `NotificationArn` alerts approvers through SNS. Only a release-approver IAM group may call `codepipeline:PutApprovalResult`. The timeout is shortened from the 7-day default to 24 hours, so stale releases expire. |
| **Deploy production** | Re-checks the image gate (continuous scanning may have found new issues), then deploys the same artifact R and digest D with `values/production.yaml` from R. |
| **Verify production** | Confirms rollout completion, runs the HTTPS smoke test, and requires the production alarms to stay out of `ALARM` for a 10-minute bake period before the release counts as successful. |

Both deploy projects run the same command, using files from R only:

```sh
helm upgrade --install mobile-api ./mobile-api-*.tgz \
  --namespace "$NAMESPACE" \
  -f "values/$ENVIRONMENT.yaml" -f release-values.yaml \
  --rollback-on-failure --wait=watcher --timeout 10m --history-max 20
```

These are Helm 4 flags. In Helm 4, `--rollback-on-failure` replaces
`--atomic`, and without it `upgrade` does not wait for resources. With Helm 3,
use `--atomic --wait`. `CLUSTER_NAME`, `NAMESPACE`, and `ENVIRONMENT` are fixed
in each deploy project's configuration, not taken from the artifact, so a
release cannot redirect itself to another environment.

**No rebuild, no re-fetch.** Promotion reuses R and D. The deploy projects
never build an image and never read Git, so production receives exactly the
bytes that were verified in staging. The deploy buildspec travels inside R, so
the deployment logic also comes from the reviewed commit.

### Staging verification

Implemented today (runs against the demo):

- Rollout: `kubectl rollout status` completes; every API pod is Ready; each
  running pod's `imageID` contains digest D; no pod restarted after the
  rollout; the Helm release status is `deployed`.
- Behaviour: `python scripts/smoke_test.py https://<staging-host>
  --expect-service <name> --expect-version <version>`, run through the public
  HTTPS hostname, the same path customers use.

Required for the real application, specified in
[verification.md](verification.md#staging-acceptance-plan): a real database
read and write; duplicate and invalid-signature webhooks; a batch replay for
the same business period; a rollout while requests are in flight; and an
unhealthy replacement that never becomes ready.

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

Each role trusts `codebuild.amazonaws.com` with an `aws:SourceArn` condition
naming its own project. The staging project therefore cannot use the
production role. The CodePipeline service role may start these projects, use
the CodeConnections connection, read and write artifacts, and publish approval
notifications.

**Kubernetes authorization** uses EKS access entries. The cluster's
authentication mode must be `API` or `API_AND_CONFIG_MAP`, which is an
assumption. Each deploy role gets an access entry associated with
`AmazonEKSEditPolicy`, scoped to its own namespace. That policy covers
everything the chart manages now or will add: Deployments, Services,
ServiceAccounts, Jobs and CronJobs, HPAs, PDBs, Ingresses, and ConfigMaps. It
also covers Secrets, which Helm needs because it stores release history as
Secrets in the release namespace.

The policy does not cover RBAC objects, cluster-scoped resources, creating
namespaces, or custom resources such as an `ExternalSecret`. Namespaces are
therefore created once by the platform team, and the pipeline never passes
`--create-namespace`. If the chart adds custom resources, map the role to a
Kubernetes group through the access entry's `kubernetesGroups` and bind a
namespaced Role.

That group-plus-Role mapping is also the way to tighten permissions before
production. The Edit policy allows impersonating any service account in the
namespace, and `exec` and `port-forward` into pods.

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
2. **Fallback:** if the pipeline is unavailable, run `helm rollback mobile-api
   <revision> --namespace <ns> --wait` from the production deploy project, and
   record it manually.

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

- The deploy project runs a single Kubernetes Job with the same digest D and
  the migration command, before `helm upgrade`. The Job has `backoffLimit: 0`
  and a deadline. If it fails, the release stops before any API pod changes.
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
| **Urgent:** customer or business impact now. Production only, at any hour. | `mobile-api-production-urgent` | SLO fast burn. Canary failing twice in a row. Healthy targets below minimum. Webhook processing failures or lag over threshold (payments not completing). Batch not finished by its deadline. Failed production verification whose rollback also failed. |
| **Working hours:** investigate, no immediate impact. | `mobile-api-<env>-ops` | SLO slow burn. Restarts or OOM kills. CPU or memory near requests. Rising database pool waits. Batch slower than usual but within its deadline. Staging deploy or verify failures. Validation build failures. Image-gate failures. New critical finding on a running image. |

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
  - Postgres `max_connections`, and how much other clients use;
  - pool size per pod times maximum replicas;
  - whether a pooler or RDS Proxy is needed;
  - whether Postgres is RDS, which determines the AWS metrics available.
- **Batch deadline.** When the daily job must finish, and which key identifies
  a business period.
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
| Chart, environment values, demo application | Implemented. Chart linted, rendered, and schema-validated. Demo run natively. Not installed on a cluster. |
| Chart tests, smoke test, secret scan, validation buildspec | Implemented and run locally. The buildspec has not run in CodeBuild. |
| Container image | Built and run locally with Podman under the chart's restrictions. Not pushed to any registry. |
| Everything else in this document | Proposed only |
