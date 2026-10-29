"""EKS Auto Mode cluster for staging, and the CodeBuild projects that deploy to it.

One simplification against docs/operations.md section 3: the Kubernetes API
endpoint is public (and private), so the deploy and secrets-sync projects run
outside the VPC. Kubernetes authorization comes from EKS access entries mapped
to the groups bound by platform/deploy-rbac.yaml and platform/eks/.

HTTPS is optional and driven by CDK context:
  domainName      host for the Ingress and the Route53 record
  hostedZoneId    public zone that holds that record
  certificateArn  an existing ACM certificate; without it one is requested
                  with DNS validation in that zone
"""

import aws_cdk as cdk
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_eks as eks
from aws_cdk import aws_iam as iam
from aws_cdk import aws_route53 as route53
from constructs import Construct

from stacks import RUNTIME_SECRET_NAME, github_project

CLUSTER_NAME = "mobile-api-staging"
KUBERNETES_VERSION = "1.37"  # newest in standard support, 2026-10-28
NAMESPACE = "mobile-api-staging"
DEPLOYERS_GROUP = "mobile-api-staging-deployers"  # bound by platform/deploy-rbac.yaml
SECRETS_GROUP = "mobile-api-staging-secrets"  # bound by platform/eks/secrets-sync-rbac.yaml
CLUSTER_ADMIN_POLICY = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

CLUSTER_POLICIES = [
    "AmazonEKSClusterPolicy", "AmazonEKSComputePolicy", "AmazonEKSBlockStoragePolicyV2",
    "AmazonEKSLoadBalancingPolicy", "AmazonEKSNetworkingPolicy",
]
NODE_POLICIES = ["AmazonEKSWorkerNodeMinimalPolicy", "AmazonEC2ContainerRegistryPullOnly"]


def managed(names):
    return [iam.ManagedPolicy.from_aws_managed_policy_name(name) for name in names]


class EksStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, *, vpc: ec2.IVpc,
                 database_security_group: ec2.ISecurityGroup, repository: ecr.IRepository,
                 connection_arn: str, github_owner: str, github_repo: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        cluster_role = iam.Role(
            self, "ClusterRole",
            # Auto Mode needs sts:TagSession as well as sts:AssumeRole.
            assumed_by=iam.ServicePrincipal("eks.amazonaws.com").with_session_tags(),
            managed_policies=managed(CLUSTER_POLICIES),
        )
        node_role = iam.Role(
            self, "NodeRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=managed(NODE_POLICIES),
        )

        cluster = eks.CfnCluster(
            self, "Cluster",
            name=CLUSTER_NAME,
            version=KUBERNETES_VERSION,
            role_arn=cluster_role.role_arn,
            resources_vpc_config=eks.CfnCluster.ResourcesVpcConfigProperty(
                subnet_ids=vpc.select_subnets(subnet_group_name="nodes").subnet_ids,
                endpoint_public_access=True,
                endpoint_private_access=True,
            ),
            # Access entries only; the creating identity gets nothing implicitly.
            access_config=eks.CfnCluster.AccessConfigProperty(
                authentication_mode="API", bootstrap_cluster_creator_admin_permissions=False,
            ),
            # Auto Mode: EKS runs networking, compute, storage and load balancing.
            bootstrap_self_managed_addons=False,
            compute_config=eks.CfnCluster.ComputeConfigProperty(
                enabled=True, node_pools=["system", "general-purpose"], node_role_arn=node_role.role_arn,
            ),
            kubernetes_network_config=eks.CfnCluster.KubernetesNetworkConfigProperty(
                elastic_load_balancing=eks.CfnCluster.ElasticLoadBalancingProperty(enabled=True),
            ),
            storage_config=eks.CfnCluster.StorageConfigProperty(
                block_storage=eks.CfnCluster.BlockStorageProperty(enabled=True),
            ),
            logging=eks.CfnCluster.LoggingProperty(cluster_logging=eks.CfnCluster.ClusterLoggingProperty(
                enabled_types=[eks.CfnCluster.LoggingTypeConfigProperty(type=t) for t in ("api", "audit")],
            )),
        )

        # Auto Mode nodes carry the cluster security group, so pods reach RDS through it.
        ec2.CfnSecurityGroupIngress(
            self, "DatabaseFromCluster",
            group_id=database_security_group.security_group_id,
            source_security_group_id=cluster.attr_cluster_security_group_id,
            ip_protocol="tcp", from_port=5432, to_port=5432, description="EKS workloads",
        )

        # HTTPS: certificate and the permissions to publish the ALB hostname.
        domain_name = self.node.try_get_context("domainName")
        hosted_zone_id = self.node.try_get_context("hostedZoneId")
        certificate_arn = self.node.try_get_context("certificateArn")
        https = bool(domain_name and hosted_zone_id)
        if https and not certificate_arn:
            zone = route53.HostedZone.from_hosted_zone_id(self, "HostedZone", hosted_zone_id)
            certificate_arn = acm.Certificate(
                self, "Certificate", domain_name=domain_name, validation=acm.CertificateValidation.from_dns(zone),
            ).certificate_arn

        source = dict(owner=github_owner, repo=github_repo, connection_arn=connection_arn)
        cluster_scope = dict(
            CLUSTER_NAME=codebuild.BuildEnvironmentVariable(value=cluster.ref),
            NAMESPACE=codebuild.BuildEnvironmentVariable(value=NAMESPACE),
        )
        describe_cluster = iam.PolicyStatement(actions=["eks:DescribeCluster"], resources=[cluster.attr_arn])

        # Helm release, rollout checks, HTTPS write/read, manual batch Job.
        # IMAGE_TAG (the commit SHA) is passed at start-build.
        deploy_variables = dict(
            cluster_scope, ECR_REPOSITORY_URI=codebuild.BuildEnvironmentVariable(value=repository.repository_uri),
        )
        if https:
            deploy_variables.update(
                INGRESS_HOST=codebuild.BuildEnvironmentVariable(value=domain_name),
                CERTIFICATE_ARN=codebuild.BuildEnvironmentVariable(value=certificate_arn),
                HOSTED_ZONE_ID=codebuild.BuildEnvironmentVariable(value=hosted_zone_id),
            )
        deploy = github_project(
            self, "DeployStaging", name="mobile-api-deploy-staging", buildspec="buildspecs/deploy-staging.yml",
            timeout_minutes=45, environment_variables=deploy_variables, **source,
        )
        deploy.add_to_role_policy(describe_cluster)
        deploy.add_to_role_policy(iam.PolicyStatement(
            actions=["ecr:DescribeImages"], resources=[repository.repository_arn],
        ))
        if https:
            deploy.add_to_role_policy(iam.PolicyStatement(
                actions=["route53:ChangeResourceRecordSets", "route53:ListResourceRecordSets"],
                resources=[f"arn:aws:route53:::hostedzone/{hosted_zone_id}"],
            ))
            deploy.add_to_role_policy(iam.PolicyStatement(
                actions=["route53:GetChange"], resources=["arn:aws:route53:::change/*"],
            ))

        # Copies the runtime secret into the namespace. The only role that can
        # read the secret and write Kubernetes Secrets.
        secrets_sync = github_project(
            self, "SecretsSync", name="mobile-api-secrets-sync", buildspec="buildspecs/secrets-sync.yml",
            timeout_minutes=10, environment_variables=cluster_scope, **source,
        )
        secrets_sync.add_to_role_policy(describe_cluster)
        secrets_sync.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=[self.format_arn(
                service="secretsmanager", resource="secret",
                resource_name=f"{RUNTIME_SECRET_NAME}-*", arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
            )],
        ))

        admin_principal = (self.node.try_get_context("clusterAdminPrincipalArn")
                           or f"arn:aws:iam::{self.account}:user/teg")
        self.access_entry("AdminAccess", cluster, admin_principal, admin=True)
        self.access_entry("DeployerAccess", cluster, deploy.role.role_arn, groups=[DEPLOYERS_GROUP])
        self.access_entry("SecretsSyncAccess", cluster, secrets_sync.role.role_arn, groups=[SECRETS_GROUP])

        cdk.CfnOutput(self, "ClusterName", value=cluster.ref)
        cdk.CfnOutput(self, "ClusterEndpoint", value=cluster.attr_endpoint)
        cdk.CfnOutput(self, "ClusterSecurityGroupId", value=cluster.attr_cluster_security_group_id)
        cdk.CfnOutput(self, "OidcIssuerUrl", value=cluster.attr_open_id_connect_issuer_url)
        cdk.CfnOutput(self, "KubeconfigCommand",
                      value=f"aws eks update-kubeconfig --name {CLUSTER_NAME} --region {self.region}")
        cdk.CfnOutput(self, "IngressHost", value=domain_name or "(HTTPS not configured)")
        cdk.CfnOutput(self, "CertificateArn", value=certificate_arn or "(HTTPS not configured)")

    def access_entry(self, id, cluster, principal_arn, *, groups=None, admin=False):
        policies = None
        if admin:
            policies = [eks.CfnAccessEntry.AccessPolicyProperty(
                policy_arn=CLUSTER_ADMIN_POLICY,
                access_scope=eks.CfnAccessEntry.AccessScopeProperty(type="cluster"),
            )]
        eks.CfnAccessEntry(
            self, id, cluster_name=cluster.ref, principal_arn=principal_arn, type="STANDARD",
            kubernetes_groups=groups, access_policies=policies,
        )
