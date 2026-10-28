# AWS resources for the take-home

A CDK app (Python) that provisions the AWS side of this exercise in account
`882384879235`, region `us-east-1`. It covers the build path and the database
that stands in for the external PostgreSQL dependency. It does **not** create
an EKS cluster, the release pipeline, ingress, or monitoring; those remain
proposals in [docs/operations.md](../docs/operations.md).

| Stack | Creates | Why |
| --- | --- | --- |
| `MobileApiFoundation` | ECR repository `mobile-api` (immutable tags, scan on push, last 30 images kept, retained on delete). CodeConnections connection `fastapi-eks-takehome` to GitHub. | Where images live; how CodeBuild reads the repository. |
| `MobileApiDatabase` | VPC `mobile-api` (10.60.0.0/16, two AZs, one NAT gateway; public, `runners`, and isolated `database` subnets). RDS PostgreSQL 17.11 `mobile-api-staging`, db.t4g.micro, 20 GiB gp3, encrypted, not public, TLS required (`rds.force_ssl`), CA `rds-ca-rsa2048-g1`, master credentials generated into secret `/mobile-api/staging/rds-master`. Security group opening 5432 only to the runners (and to an EKS workload security group when given as context). | Reproduces the external database with private connectivity. |
| `MobileApiBuild` | CodeBuild `mobile-api-validate` (pull requests, `buildspecs/validate.yml`) and `mobile-api-build` (pushes to `main`, `buildspecs/build.yml`, privileged for Docker, pushes images by commit SHA). | Verification and image delivery. |
| `MobileApiIntegration` | CodeBuild `mobile-api-db-admin` (one-off: creates the application role) and `mobile-api-integration` (migrations and integration tests against RDS). Both run in the `runners` subnets, started by hand. | The only paths into the private database. |

Every CodeBuild role is limited to its job: the build role can push to the one
ECR repository and nothing else; the integration role can read only the
runtime secret; only the db-admin role can read the master secret; no role has
any `eks:` permission.

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

To allow EKS workloads to reach the database once the cluster exists:

```sh
npx aws-cdk@2.1146.1 deploy MobileApiDatabase -c eksWorkloadSecurityGroupId=sg-0123456789abcdef0
```

## Operator steps for the database (outside this repository and any agent)

1. Store the application credentials in Secrets Manager with the reviewed
   loader, entering values at its hidden prompts:
   `python scripts/load_secrets.py --profile <profile> --region us-east-1 --expected-account-id 882384879235 --secret-name /mobile-api/staging/runtime`.
   `DATABASE_URL` must be
   `postgresql://mobile_api_app:<password>@<DatabaseEndpoint>:5432/mobileapi?sslmode=verify-full`.
2. Create the role in the database: `aws codebuild start-build --project-name mobile-api-db-admin`.
   It reads both secrets inside the VPC and runs `platform/db/create_app_user.sql`.
   Repeat it after changing the password in the secret.
3. Run the schema and the integration tests:
   `aws codebuild start-build --project-name mobile-api-integration`.

## Cost (approximate, us-east-1)

| Resource | Rate |
| --- | --- |
| NAT gateway | about $0.045 per hour plus data, roughly $33 per month |
| RDS db.t4g.micro + 20 GiB gp3 | about $0.016 per hour plus $2.30 per month storage, roughly $14 per month |
| CodeBuild | per build minute while running; nothing when idle |
| ECR, Secrets Manager, logs | cents per month at this scale |

## Remove

```sh
npx aws-cdk@2.1146.1 destroy --all
```

The ECR repository is retained (delete it by hand after removing its images),
the RDS instance is replaced by a final snapshot, and the master secret is
deleted with the instance. The connection and the GitHub App installation are
removed separately.
