"""Synthesized-template checks for the CDK stacks.

    python -m pytest infra   (with infra/requirements.txt installed)
"""

import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stacks import BuildStack, DatabaseStack, FoundationStack, IntegrationStack  # noqa: E402

ENV = cdk.Environment(account="111111111111", region="us-east-1")


@pytest.fixture(scope="module")
def templates():
    app = cdk.App()
    foundation = FoundationStack(app, "Foundation", env=ENV)
    database = DatabaseStack(app, "Database", env=ENV)
    build = BuildStack(app, "Build", env=ENV, github_owner="o", github_repo="r",
                       connection_arn=foundation.connection_arn, repository=foundation.repository)
    integration = IntegrationStack(app, "Integration", env=ENV, github_owner="o", github_repo="r",
                                   connection_arn=foundation.connection_arn, vpc=database.vpc,
                                   runner_security_group=database.runner_security_group,
                                   master_secret=database.master_secret)
    return {name: Template.from_stack(stack) for name, stack in
            [("foundation", foundation), ("database", database), ("build", build), ("integration", integration)]}


def projects(template):
    return {p["Properties"]["Name"]: p["Properties"] for p in template.find_resources("AWS::CodeBuild::Project").values()}


def policy_actions(template):
    """Every IAM action granted in the template, lower-cased."""
    actions = set()
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            listed = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
            actions |= {a.lower() for a in listed}
    return actions


def test_repository_is_immutable_scanned_and_retained(templates):
    templates["foundation"].has_resource("AWS::ECR::Repository", {
        "Properties": {
            "RepositoryName": "mobile-api",
            "ImageTagMutability": "IMMUTABLE",
            "ImageScanningConfiguration": {"ScanOnPush": True},
        },
        "DeletionPolicy": "Retain",
    })


def test_every_project_authenticates_through_codeconnections(templates):
    for name in ("build", "integration"):
        for project_name, project in projects(templates[name]).items():
            assert project["Source"]["Type"] == "GITHUB", project_name
            assert project["Source"]["Auth"]["Type"] == "CODECONNECTIONS", project_name


def test_only_the_release_build_is_privileged_and_can_push_images(templates):
    build = projects(templates["build"])
    assert build["mobile-api-build"]["Environment"]["PrivilegedMode"] is True
    assert build["mobile-api-validate"]["Environment"].get("PrivilegedMode", False) is False
    for project in projects(templates["integration"]).values():
        assert project["Environment"].get("PrivilegedMode", False) is False
    assert any(a.startswith("ecr:") for a in policy_actions(templates["build"]))
    assert not any(a.startswith("ecr:") for a in policy_actions(templates["integration"]))


def test_webhooks_trigger_validation_on_pull_requests_and_build_on_main(templates):
    build = projects(templates["build"])
    validate_events = {f["Pattern"] for group in build["mobile-api-validate"]["Triggers"]["FilterGroups"]
                       for f in group if f["Type"] == "EVENT"}
    assert validate_events == {"PULL_REQUEST_CREATED, PULL_REQUEST_UPDATED, PULL_REQUEST_REOPENED"}
    (build_group,) = build["mobile-api-build"]["Triggers"]["FilterGroups"]
    assert {(f["Type"], f["Pattern"]) for f in build_group} == {("EVENT", "PUSH"), ("HEAD_REF", "refs/heads/main")}
    for project in projects(templates["integration"]).values():
        assert "Triggers" not in project, "VPC runners are started by hand"


def test_no_role_can_reach_eks_or_write_secrets(templates):
    for name, template in templates.items():
        actions = policy_actions(template)
        assert not any(a.startswith("eks:") for a in actions), name
        assert not any(a.startswith("secretsmanager:") and a != "secretsmanager:getsecretvalue" for a in actions), name


def test_database_is_private_encrypted_and_requires_tls(templates):
    database = templates["database"]
    database.has_resource_properties("AWS::RDS::DBInstance", {
        "DBInstanceIdentifier": "mobile-api-staging",
        "PubliclyAccessible": False,
        "StorageEncrypted": True,
        "DBName": "mobileapi",
        "CACertificateIdentifier": "rds-ca-rsa2048-g1",
    })
    database.has_resource_properties("AWS::RDS::DBParameterGroup", {"Parameters": {"rds.force_ssl": "1"}})
    database.has_resource_properties("AWS::EC2::Subnet", {"MapPublicIpOnLaunch": True})  # NAT lives here
    for subnet in database.find_resources("AWS::RDS::DBSubnetGroup").values():
        assert subnet["Properties"]["SubnetIds"], "the instance needs its own subnet group"


def test_database_port_opens_only_to_the_runner_security_group(templates):
    database = templates["database"]
    ingress = [r["Properties"] for r in database.find_resources("AWS::EC2::SecurityGroupIngress").values()]
    assert ingress, "expected an ingress rule for the runners"
    for rule in ingress:
        assert (rule["FromPort"], rule["ToPort"], rule["IpProtocol"]) == (5432, 5432, "tcp")
        assert "SourceSecurityGroupId" in rule and "CidrIp" not in rule, rule
    for group in database.find_resources("AWS::EC2::SecurityGroup").values():
        for rule in group["Properties"].get("SecurityGroupIngress", []):
            assert rule.get("CidrIp") != "0.0.0.0/0"


def test_vpc_runners_read_only_their_own_secrets(templates):
    integration = templates["integration"]
    integration.has_resource_properties("AWS::CodeBuild::Project", {
        "Name": "mobile-api-integration",
        "VpcConfig": Match.object_like({"Subnets": Match.any_value()}),
    })
    # The integration project may read the runtime secret but not the master secret.
    for policy in integration.find_resources("AWS::IAM::Policy").values():
        role_names = [r["Ref"] for r in policy["Properties"]["Roles"]]
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            if statement["Action"] == "secretsmanager:GetSecretValue" and any("Integration" in r for r in role_names):
                resource = statement["Resource"]
                serialized = str(resource)
                assert "/mobile-api/staging/runtime-*" in serialized, serialized
                assert "rds-master" not in serialized and "Database" not in serialized, serialized
