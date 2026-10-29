# AWS resources for the take-home

A CDK app (Python) that provisions the AWS side of this exercise in account
`882384879235`, region `us-east-1`. It covers the build path, the database that
stands in for the external PostgreSQL dependency, and a staging EKS cluster with
the projects that deploy to it. All five stacks are deployed, and the demo runs
on the cluster (2026-10-29). It does **not** create the release pipeline,
production, or monitoring; those remain proposals in
[docs/operations.md](../docs/operations.md).

| Stack | Creates | Why |
| --- | --- | --- |
| `MobileApiFoundation` | ECR repository `mobile-api` (immutable tags, scan on push, last 30 images kept, retained on delete). CodeConnections connection `fastapi-eks-takehome` to GitHub. | Where images live; how CodeBuild reads the repository. |
| `MobileApiDatabase` | VPC `mobile-api` (10.60.0.0/16, two AZs, one NAT gateway; public, `runners`, and isolated `database` subnets). RDS PostgreSQL 17.11 `mobile-api-staging`, db.t4g.micro, 20 GiB gp3, encrypted, not public, TLS required (`rds.force_ssl`), CA `rds-ca-rsa2048-g1`, master credentials generated into secret `/mobile-api/staging/rds-master`. Security group opening 5432 only to the runners, to the staging cluster's security group (rule added by `MobileApiEks`), and to an external EKS workload security group when given as context. | Reproduces the external database with private connectivity. |
| `MobileApiBuild` | CodeBuild `mobile-api-validate` (pull requests, `buildspecs/validate.yml`) and `mobile-api-build` (pushes to `main`, `buildspecs/build.yml`, privileged for Docker, pushes images by commit SHA). | Verification and image delivery. |
| `MobileApiIntegration` | CodeBuild `mobile-api-db-admin` (one-off: creates the application role) and `mobile-api-integration` (migrations and integration tests against RDS). Both run in the `runners` subnets, started by hand. | The only paths into the private database. |
| `MobileApiEks` | EKS Auto Mode cluster `mobile-api-staging` (Kubernetes 1.37, `system` and `general-purpose` node pools, built-in load balancing and block storage, API and audit logs). The managed `metrics-server` add-on (v0.9.0-eksbuild.14; Auto Mode does not include it, and the HPA cannot read CPU without it). Access entries for the cluster admin, the deploy role and the secrets-sync role. The database security group opens 5432 to the cluster security group. CodeBuild `mobile-api-secrets-sync` and `mobile-api-deploy-staging`. Route53 permissions for the HTTPS hostname, and an ACM certificate when none is given. | The staging cluster the chart deploys to; see the section below. |

Every CodeBuild role is limited to its job: the build role can push to the one
ECR repository and nothing else; the integration role can read only the
runtime secret; only the db-admin role can read the master secret; the deploy
and secrets-sync roles hold `eks:DescribeCluster` on the staging cluster and
nothing else in EKS, and their Kubernetes permissions come from access entries
bound to the namespaced RBAC in `platform/`.

## Deploy

Prerequisites: Node.js 20+ (for the CDK CLI through `npx`), Python 3.12+, AWS
credentials for the account, and a CDK-bootstrapped account/region (already
done here; otherwise `npx aws-cdk@2.1146.1 bootstrap`).

```sh
python -m venv infra/.venv
source infra/.venv/bin/activate          # Windows: infra\.venv\Scripts\activate
python -m pip install -r infra/requirements.txt

python -m pytest infra/tests             # template assertions
cd infra
npx aws-cdk@2.1146.1 synth --quiet
npx aws-cdk@2.1146.1 deploy MobileApiFoundation MobileApiDatabase
```

Then **complete the GitHub connection** (one time, by a GitHub owner of the
repository): open the `ConnectionConsoleUrl` output, choose the pending
connection `fastapi-eks-takehome`, "Update pending connection", and either pick
the existing "AWS Connector for GitHub" installation or install it on the
`tegryan-ddo` account. CodeBuild webhooks cannot be created while the
connection is pending.

```sh
npx aws-cdk@2.1146.1 deploy MobileApiBuild MobileApiIntegration
```

`MobileApiEks` adds its own cluster's rule. To let a different EKS cluster
reach the database:

```sh
npx aws-cdk@2.1146.1 deploy MobileApiDatabase -c eksWorkloadSecurityGroupId=sg-0123456789abcdef0
```

## Operator steps for the database (outside this repository and any agent)

1. Store the application credentials in Secrets Manager with the reviewed
   loader, entering values at its hidden prompts:
   `python scripts/load_secrets.py --profile <profile> --region us-east-1 --expected-account-id 882384879235 --secret-name /mobile-api/staging/runtime`.
   `DATABASE_URL` must be
   `postgresql://mobile_api_app:<password>@<DatabaseEndpoint>:5432/mobileapi?sslmode=verify-full`.
   For staging the operator used the AWS CLI instead
   (`aws secretsmanager create-secret --name /mobile-api/staging/runtime --secret-string file://<local file>`)
   and deleted the file; the values never reached Git or the agent.
2. Create the role in the database: `aws codebuild start-build --project-name mobile-api-db-admin --source-version <commit>`.
   It reads both secrets inside the VPC and runs `platform/db/create_app_user.sql`.
   Repeat it after changing the password in the secret. Ran for staging on
   2026-10-29.
3. Run the schema and the integration tests:
   `aws codebuild start-build --project-name mobile-api-integration --source-version <commit>`.
   Ran for staging on 2026-10-29: `0001_initial` applied, 6 tests passed.

`--source-version` matters until the branch is merged: without it, every
project builds the repository's default branch.

## EKS cluster and staging deployment

`MobileApiEks` creates the staging cluster with EKS Auto Mode, so AWS runs the
nodes, the VPC CNI, CoreDNS, EBS and the load-balancer controller; nothing is
installed into the cluster by this repository except the manifests under
`platform/`. Authentication mode is `API` only and the creating identity gets
no implicit access: the three access entries are the whole list.

| Principal | Access entry | Kubernetes permissions |
| --- | --- | --- |
| `arn:aws:iam::882384879235:user/teg` (override with `-c clusterAdminPrincipalArn=…`) | `AmazonEKSClusterAdminPolicy`, cluster scope | Everything; used only for the one-time bootstrap below |
| `mobile-api-deploy-staging` role | group `mobile-api-staging-deployers` | [`platform/deploy-rbac.yaml`](../platform/deploy-rbac.yaml): the chart's kinds, Jobs, ConfigMaps, pod logs; no Secrets, no exec, no port-forward |
| `mobile-api-secrets-sync` role | group `mobile-api-staging-secrets` | [`platform/eks/secrets-sync-rbac.yaml`](../platform/eks/secrets-sync-rbac.yaml): the one Secret `mobile-api-runtime`, and a restart of the one Deployment |

Two simplifications against [docs/operations.md](../docs/operations.md): the
API endpoint is public as well as private, so the deploy and secrets-sync
projects run outside the VPC (section 3 describes the private-endpoint setup);
and `mobile-api-secrets-sync` stands in for the cluster secrets integration
the design assumes, copying `/mobile-api/staging/runtime` into the namespace.

### HTTPS

Three context values make the deploy project publish the ALB under a hostname
of yours. For staging they are committed in `infra/cdk.json`, so a plain
`deploy` uses them; `-c` flags override them:

```sh
npx aws-cdk@2.1146.1 deploy MobileApiEks \
  -c domainName=mobile-api-staging.digitaldevops.io \
  -c hostedZoneId=Z3OKT7D3Q3TASV \
  -c certificateArn=arn:aws:acm:us-east-1:882384879235:certificate/411f9b4a-bc8f-4342-b7e6-52f39251fa3a
```

This account already has an issued wildcard certificate for
`*.digitaldevops.io` (expires 2027-03-13) and the public zone
`digitaldevops.io`. A wildcard covers exactly one label, so
`mobile-api-staging.digitaldevops.io` works and `api.staging.digitaldevops.io`
would not. Without `certificateArn`, the stack requests a certificate for
`domainName` with DNS validation in that zone (a few minutes). Without
`domainName` and `hostedZoneId`, no Ingress is created and the deploy project
runs its HTTP checks from a short-lived Job inside the cluster; the
alternatives are a domain you own, or a self-signed certificate imported into
ACM, which browsers and the smoke test will reject.

### Order

1. `npx aws-cdk@2.1146.1 deploy MobileApiEks …` (about 15 minutes). It needs
   `MobileApiFoundation` and `MobileApiDatabase`, and an available GitHub
   connection for the two CodeBuild projects.
2. One-time bootstrap, as the cluster admin, from the repository root:

   ```sh
   aws eks update-kubeconfig --name mobile-api-staging --region us-east-1
   kubectl get nodepools                       # confirms Auto Mode and your access
   kubectl apply -f platform/eks/namespace.yaml
   kubectl apply -f platform/eks/ingressclass.yaml
   kubectl apply -f platform/eks/secrets-sync-rbac.yaml
   kubectl apply -f platform/deploy-rbac.yaml
   ```

3. After the operator has stored `/mobile-api/staging/runtime` and
   `mobile-api-db-admin` has created the database role:

   ```sh
   aws codebuild start-build --project-name mobile-api-secrets-sync
   aws codebuild start-build --project-name mobile-api-deploy-staging \
     --environment-variables-override name=IMAGE_TAG,value=<commit sha pushed by mobile-api-build>
   ```

   The deploy build runs the migration hook and the Helm release, checks the
   rollout and the image digest on every Running API pod (terminating pods of
   the previous ReplicaSet are skipped; the first version counted one and
   failed after a successful rollout), smoke-tests `https://<domainName>`,
   writes and reads a record through it, and runs the manual batch Job from
   `docs/verification.md`. After a secret change, rerun secrets-sync with
   `name=RESTART,value=true`.

   All of this ran for staging on 2026-10-29: the bootstrap, secrets-sync, and
   three deploys (image `73de0a8`, then `61fb83d` twice; Helm revision 3). The projects build the
   repository's default branch unless `--source-version <commit>` is given.

To let an existing EKS cluster reach the database instead, pass
`-c eksWorkloadSecurityGroupId=…` to `MobileApiDatabase`.

## Cost (approximate, us-east-1)

| Resource | Rate |
| --- | --- |
| EKS control plane | $0.10 per hour, about $73 per month |
| Auto Mode nodes | EC2 price plus an Auto Mode fee of roughly 12%; the chart's requests fit one or two small instances, about $0.10–0.20 per hour while pods run |
| ALB (with HTTPS) | about $0.0225 per hour plus capacity units, roughly $17–20 per month |
| NAT gateway | about $0.045 per hour plus data, roughly $33 per month |
| RDS db.t4g.micro + 20 GiB gp3 | about $0.016 per hour plus $2.30 per month storage, roughly $14 per month |
| CodeBuild | per build minute while running; nothing when idle |
| ECR, Secrets Manager, logs | cents per month at this scale |

With the cluster running, expect about $0.35–0.45 per hour in total.

## Remove

```sh
npx aws-cdk@2.1146.1 destroy --all
```

Before destroying `MobileApiEks`, delete the Helm release
(`HELM_DRIVER=configmap helm uninstall mobile-api -n mobile-api-staging`) so
Auto Mode removes the ALB, and delete the Route53 record the deploy project
created. The ECR repository is retained (delete it by hand after removing its
images), the RDS instance is replaced by a final snapshot, and the master
secret is deleted with the instance. The connection and the GitHub App
installation are removed separately.
