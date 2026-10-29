"""Synthesized-template checks for the EKS stack."""

import re
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eks_stack import EksStack  # noqa: E402
from stacks import DatabaseStack, FoundationStack  # noqa: E402

ENV = cdk.Environment(account="111111111111", region="us-east-1")
HTTPS_CONTEXT = {
    "domainName": "api.example.com",
    "hostedZoneId": "Z0123456789",
    "certificateArn": "arn:aws:acm:us-east-1:111111111111:certificate/existing",
}


def synth(context=None):
    app = cdk.App(context=context)
    foundation = FoundationStack(app, "Foundation", env=ENV)
    database = DatabaseStack(app, "Database", env=ENV)
    stack = EksStack(app, "Eks", env=ENV, github_owner="o", github_repo="r",
                     connection_arn=foundation.connection_arn, repository=foundation.repository,
                     vpc=database.vpc, database_security_group=database.database_security_group)
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template():
    return synth()


def cluster(template):
    (resource,) = template.find_resources("AWS::EKS::Cluster").values()
    return resource["Properties"]


def projects(template):
    return {p["Properties"]["Name"]: p for p in template.find_resources("AWS::CodeBuild::Project").values()}


def role_actions(template, role_logical_id):
    """IAM actions granted to one role, lower-cased, with their resources."""
    granted = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        if role_logical_id not in [r.get("Ref") for r in policy["Properties"]["Roles"]]:
            continue
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
            granted += [(a.lower(), statement["Resource"]) for a in actions]
    return granted


def test_cluster_is_auto_mode_with_access_entries_only(template):
    props = cluster(template)
    assert props["Name"] == "mobile-api-staging"
    assert re.fullmatch(r"1\.3\d", props["Version"]), props["Version"]
    assert props["AccessConfig"] == {"AuthenticationMode": "API", "BootstrapClusterCreatorAdminPermissions": False}
    assert props["BootstrapSelfManagedAddons"] is False
    assert props["ComputeConfig"]["Enabled"] is True
    assert set(props["ComputeConfig"]["NodePools"]) == {"system", "general-purpose"}
    assert props["KubernetesNetworkConfig"]["ElasticLoadBalancing"]["Enabled"] is True
    assert props["StorageConfig"]["BlockStorage"]["Enabled"] is True
    assert props["ResourcesVpcConfig"]["EndpointPrivateAccess"] is True
    assert {t["Type"] for t in props["Logging"]["ClusterLogging"]["EnabledTypes"]} == {"api", "audit"}


def test_cluster_role_allows_session_tagging(template):
    roles = template.find_resources("AWS::IAM::Role")
    cluster_role = next(r for r in roles.values()
                        if r["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Principal"]
                        == {"Service": "eks.amazonaws.com"})
    actions = cluster_role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Action"]
    assert set(actions) == {"sts:AssumeRole", "sts:TagSession"}


def test_access_entries_cover_admin_deployer_and_secrets_sync(template):
    entries = [e["Properties"] for e in template.find_resources("AWS::EKS::AccessEntry").values()]
    assert len(entries) == 3
    admin = next(e for e in entries if "user/teg" in str(e["PrincipalArn"]))
    assert admin["AccessPolicies"][0]["PolicyArn"].endswith("AmazonEKSClusterAdminPolicy")
    assert admin["AccessPolicies"][0]["AccessScope"] == {"Type": "cluster"}
    groups = {tuple(e.get("KubernetesGroups", [])) for e in entries if e is not admin}
    assert groups == {("mobile-api-staging-deployers",), ("mobile-api-staging-secrets",)}
    for entry in entries:
        if entry is not admin:
            assert "AccessPolicies" not in entry, "group-mapped roles get permissions only from RBAC"


def test_database_port_opens_to_the_cluster_security_group(template):
    rules = [r["Properties"] for r in template.find_resources("AWS::EC2::SecurityGroupIngress").values()]
    (rule,) = [r for r in rules if r.get("FromPort") == 5432]
    assert rule["ToPort"] == 5432 and rule["IpProtocol"] == "tcp"
    assert "ClusterSecurityGroupId" in str(rule["SourceSecurityGroupId"])


def test_deploy_role_cannot_read_secrets_or_push_images(template):
    deploy = projects(template)["mobile-api-deploy-staging"]
    assert deploy["Properties"]["Environment"].get("PrivilegedMode", False) is False
    actions = {a for a, _ in role_actions(template, deploy["Properties"]["ServiceRole"]["Fn::GetAtt"][0])}
    assert not any(a.startswith("secretsmanager:") for a in actions)
    assert not any(a.startswith("ecr:") and a != "ecr:describeimages" for a in actions)
    assert "eks:describecluster" in actions
    assert not any(a.startswith("route53:") for a in actions), "no Route53 access without HTTPS context"


def test_secrets_sync_role_reads_only_the_runtime_secret(template):
    sync = projects(template)["mobile-api-secrets-sync"]
    granted = role_actions(template, sync["Properties"]["ServiceRole"]["Fn::GetAtt"][0])
    secret_grants = [res for action, res in granted if action == "secretsmanager:getsecretvalue"]
    assert len(secret_grants) == 1 and "/mobile-api/staging/runtime-*" in str(secret_grants[0])
    assert not any(a.startswith("ecr:") for a, _ in granted)
    assert not any(a.startswith("secretsmanager:") and a != "secretsmanager:getsecretvalue" for a, _ in granted)


def test_https_context_wires_certificate_host_and_dns_permissions():
    template = synth(HTTPS_CONTEXT)
    assert not template.find_resources("AWS::CertificateManager::Certificate"), "existing certificate is reused"
    deploy = projects(template)["mobile-api-deploy-staging"]
    variables = {v["Name"]: v["Value"] for v in deploy["Properties"]["Environment"]["EnvironmentVariables"]}
    assert variables["INGRESS_HOST"] == HTTPS_CONTEXT["domainName"]
    assert variables["CERTIFICATE_ARN"] == HTTPS_CONTEXT["certificateArn"]
    assert variables["HOSTED_ZONE_ID"] == HTTPS_CONTEXT["hostedZoneId"]
    granted = role_actions(template, deploy["Properties"]["ServiceRole"]["Fn::GetAtt"][0])
    zone_grants = [res for action, res in granted if action == "route53:changeresourcerecordsets"]
    assert zone_grants == [f"arn:aws:route53:::hostedzone/{HTTPS_CONTEXT['hostedZoneId']}"]


def test_https_context_without_certificate_requests_a_dns_validated_one():
    context = {k: v for k, v in HTTPS_CONTEXT.items() if k != "certificateArn"}
    template = synth(context)
    template.has_resource_properties("AWS::CertificateManager::Certificate", {
        "DomainName": context["domainName"],
        "DomainValidationOptions": [{"DomainName": context["domainName"], "HostedZoneId": context["hostedZoneId"]}],
        "ValidationMethod": "DNS",
    })


def test_metrics_server_addon_backs_the_hpa(template):
    template.has_resource_properties("AWS::EKS::Addon", {
        "AddonName": "metrics-server",
        "AddonVersion": "v0.9.0-eksbuild.14",
        "ClusterName": {"Ref": next(iter(template.find_resources("AWS::EKS::Cluster")))},
    })
