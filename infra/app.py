"""CDK app for the take-home's AWS resources. See infra/README.md.

    npx aws-cdk@2.1146.1 deploy --all

Stacks, in dependency order:
  MobileApiFoundation   ECR repository and the CodeConnections link to GitHub
  MobileApiDatabase     VPC and the RDS PostgreSQL instance that stands in for
                        the external database
  MobileApiBuild        CodeBuild: validation on pull requests, release build
                        on pushes to main
  MobileApiIntegration  CodeBuild in the VPC: one-off database bootstrap, and
                        integration tests against RDS
  MobileApiEks          EKS Auto Mode cluster for staging, access entries, and
                        the CodeBuild projects that sync the runtime secret
                        and deploy the chart (see infra/eks_stack.py for the
                        HTTPS context values)
"""

import aws_cdk as cdk

from eks_stack import EksStack
from stacks import BuildStack, DatabaseStack, FoundationStack, IntegrationStack

ENV = cdk.Environment(account="882384879235", region="us-east-1")
GITHUB_OWNER = "tegryan-ddo"
GITHUB_REPO = "fastapi-eks-takehome"

app = cdk.App()
foundation = FoundationStack(app, "MobileApiFoundation", env=ENV)
database = DatabaseStack(app, "MobileApiDatabase", env=ENV)
BuildStack(
    app, "MobileApiBuild", env=ENV,
    github_owner=GITHUB_OWNER, github_repo=GITHUB_REPO,
    connection_arn=foundation.connection_arn, repository=foundation.repository,
)
IntegrationStack(
    app, "MobileApiIntegration", env=ENV,
    github_owner=GITHUB_OWNER, github_repo=GITHUB_REPO,
    connection_arn=foundation.connection_arn,
    vpc=database.vpc, runner_security_group=database.runner_security_group,
    master_secret=database.master_secret,
)
EksStack(
    app, "MobileApiEks", env=ENV,
    github_owner=GITHUB_OWNER, github_repo=GITHUB_REPO,
    connection_arn=foundation.connection_arn, repository=foundation.repository,
    vpc=database.vpc, database_security_group=database.database_security_group,
)
app.synth()
