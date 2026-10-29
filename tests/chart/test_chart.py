"""Chart checks: Helm lint, Kubernetes schema validation, the relationships
between rendered resources that a schema cannot establish, and whether the
deployer RBAC in platform/ can deploy the chart without broad permissions.

    python -m pytest tests/chart

When the HPA, Ingress, and CronJob are added, extend the relationship checks:
HPA scaleTargetRef names the Deployment; Ingress backends name the Service and
its port; CronJob pods do not match the Service selector.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import tools

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "charts" / "mobile-api"
DOCKERFILE = ROOT / "examples" / "demo-api" / "Dockerfile"
DEPLOYER_RBAC = ROOT / "platform" / "deploy-rbac.yaml"
ENVIRONMENTS = ["staging", "production"]

CHART_YAML = yaml.safe_load((CHART / "Chart.yaml").read_text())
VALUES = yaml.safe_load((CHART / "values.yaml").read_text())
REPOSITORY = VALUES["image"]["repository"]
DIGEST = "sha256:" + "0" * 64

# The application's actual health endpoints (examples/demo-api/main.py).
LIVE_PATH = "/health/live"
READY_PATH = "/health/ready"

MANAGE_VERBS = {"get", "list", "watch", "create", "update", "patch", "delete"}
FORBIDDEN_RESOURCES = {"secrets", "pods/exec", "pods/attach", "pods/portforward"}
FORBIDDEN_VERBS = {"impersonate", "escalate", "bind"}


def run(*cmd):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)


def helm_template(env, *overrides):
    cmd = [tools.path("helm"), "template", "mobile-api", "charts/mobile-api", "-f", f"environments/{env}.yaml"]
    for override in overrides:
        cmd += ["--set", override]
    result = run(*cmd)
    assert result.returncode == 0, f"helm template failed for {env}:\n{result.stderr}"
    return result.stdout


def manifests(env, *overrides):
    """Rendered resources keyed by kind (the chart renders one of each)."""
    return {doc["kind"]: doc for doc in yaml.safe_load_all(helm_template(env, *overrides)) if doc}


def pod_spec(resources):
    return resources["Deployment"]["spec"]["template"]["spec"]


def api_container(pod):
    return next(c for c in pod["containers"] if c["name"] == "api")


def kubeconform(path):
    version = minimum_kubernetes_version()
    result = run(tools.path("kubeconform"), "-strict", "-summary", "-kubernetes-version", version, str(path))
    assert result.returncode == 0, f"{path.name} is invalid for Kubernetes {version}:\n{result.stdout}{result.stderr}"


def minimum_kubernetes_version():
    match = re.match(r">=\s*(\d+\.\d+\.\d+)", CHART_YAML["kubeVersion"])
    assert match, f"Chart.yaml kubeVersion {CHART_YAML['kubeVersion']!r} has no '>=x.y.z' minimum"
    return match.group(1)


def uvicorn_graceful_shutdown_seconds():
    for line in DOCKERFILE.read_text().splitlines():
        if line.startswith("CMD "):
            cmd = json.loads(line[len("CMD "):])
            return int(cmd[cmd.index("--timeout-graceful-shutdown") + 1])
    pytest.fail(f"No exec-form CMD in {DOCKERFILE}")


def deployer_rbac():
    docs = list(yaml.safe_load_all(DEPLOYER_RBAC.read_text()))
    role = next(doc for doc in docs if doc["kind"] == "ClusterRole")
    return role, docs


def granted_verbs(rules, api_group, resource):
    verbs = set()
    for rule in rules:
        if api_group in rule.get("apiGroups", []) and resource in rule.get("resources", []):
            verbs |= set(rule.get("verbs", []))
    return verbs


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_helm_lint(env):
    result = run(tools.path("helm"), "lint", "charts/mobile-api", "-f", f"environments/{env}.yaml", "--strict")
    assert result.returncode == 0, f"helm lint failed for {env}:\n{result.stdout}{result.stderr}"


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_rendered_resources_match_kubernetes_schema(env, tmp_path):
    rendered = tmp_path / f"{env}.yaml"
    rendered.write_text(helm_template(env))
    kubeconform(rendered)


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_service_selects_only_this_releases_api_pods(env):
    resources = manifests(env)
    deployment, service = resources["Deployment"], resources["Service"]
    selector = service["spec"]["selector"]
    pod_labels = deployment["spec"]["template"]["metadata"]["labels"]

    assert selector == deployment["spec"]["selector"]["matchLabels"], (
        f"Service selector {selector} differs from Deployment selector {deployment['spec']['selector']['matchLabels']}"
    )
    assert selector.items() <= pod_labels.items(), f"Service selector {selector} does not match pod labels {pod_labels}"
    assert selector.get("app.kubernetes.io/component") == "api", (
        "Service selector must include app.kubernetes.io/component=api so non-API pods never receive traffic"
    )
    changing = {"app.kubernetes.io/version", "helm.sh/chart"} & selector.keys()
    assert not changing, f"Selectors must not contain labels that change between releases: {sorted(changing)}"


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_service_and_probes_target_the_http_container_port(env):
    resources = manifests(env)
    container = api_container(pod_spec(resources))
    ports = {p["name"]: p["containerPort"] for p in container["ports"]}

    assert ports.get("http") == 8000, f"Container must expose port 8000 named 'http'; found {ports}"
    target = resources["Service"]["spec"]["ports"][0]["targetPort"]
    assert target in ports, f"Service targetPort {target!r} is not a named container port ({sorted(ports)})"

    expected_paths = {"startupProbe": LIVE_PATH, "livenessProbe": LIVE_PATH, "readinessProbe": READY_PATH}
    for probe, path in expected_paths.items():
        http_get = container[probe]["httpGet"]
        assert http_get["port"] in ports, f"{probe} port {http_get['port']!r} is not a named container port"
        assert http_get["path"] == path, f"{probe} must call {path}, not {http_get['path']}"


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ((), f"{REPOSITORY}:{CHART_YAML['appVersion']}"),
        (("image.tag=1.2.3",), f"{REPOSITORY}:1.2.3"),
        ((f"image.digest={DIGEST}",), f"{REPOSITORY}@{DIGEST}"),
        (("image.tag=1.2.3", f"image.digest={DIGEST}"), f"{REPOSITORY}@{DIGEST}"),
    ],
    ids=["default-tag", "explicit-tag", "digest", "digest-wins-over-tag"],
)
def test_image_reference(overrides, expected):
    image = api_container(pod_spec(manifests("production", *overrides)))["image"]
    assert image == expected, f"With --set {list(overrides)}: expected image {expected}, rendered {image}"


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_workload_security_settings(env):
    resources = manifests(env)
    pod = pod_spec(resources)
    pod_security = pod.get("securityContext", {})
    container_security = api_container(pod).get("securityContext", {})
    capabilities = container_security.get("capabilities", {})

    problems = []
    if pod_security.get("runAsNonRoot") is not True:
        problems.append("pod securityContext.runAsNonRoot must be true")
    if pod_security.get("runAsUser", 0) == 0:
        problems.append("pod securityContext.runAsUser must be a non-zero UID")
    if pod_security.get("seccompProfile", {}).get("type") != "RuntimeDefault":
        problems.append("pod seccompProfile must be RuntimeDefault")
    if container_security.get("allowPrivilegeEscalation") is not False:
        problems.append("container allowPrivilegeEscalation must be false")
    if container_security.get("privileged"):
        problems.append("container must not be privileged")
    if "ALL" not in capabilities.get("drop", []):
        problems.append("container must drop ALL capabilities")
    if capabilities.get("add"):
        problems.append(f"container must not add capabilities: {capabilities['add']}")
    if container_security.get("readOnlyRootFilesystem") is not True:
        problems.append("container readOnlyRootFilesystem must be true")
    if pod.get("automountServiceAccountToken") is not False:
        problems.append("automountServiceAccountToken must be false; the API does not call Kubernetes")
    if pod.get("serviceAccountName") != resources["ServiceAccount"]["metadata"]["name"]:
        problems.append("pod must use the chart's dedicated ServiceAccount")
    assert not problems, f"{env}:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_tmp_is_writable_with_read_only_root(env):
    pod = pod_spec(manifests(env))
    mounts = {m["mountPath"]: m["name"] for m in api_container(pod).get("volumeMounts", [])}
    volumes = {v["name"]: v for v in pod.get("volumes", [])}
    assert "/tmp" in mounts, "/tmp must be mounted because the root filesystem is read-only"
    assert "emptyDir" in volumes.get(mounts["/tmp"], {}), "/tmp must be backed by an emptyDir volume"


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_shutdown_fits_termination_grace_period(env):
    pod = pod_spec(manifests(env))
    pre_stop = api_container(pod)["lifecycle"]["preStop"]["sleep"]["seconds"]
    uvicorn = uvicorn_graceful_shutdown_seconds()
    grace = pod["terminationGracePeriodSeconds"]
    assert pre_stop + uvicorn < grace, (
        f"preStop sleep {pre_stop}s + Uvicorn graceful shutdown {uvicorn}s (Dockerfile) "
        f"must be less than terminationGracePeriodSeconds {grace}s"
    )


def test_deployer_rbac_matches_kubernetes_schema():
    kubeconform(DEPLOYER_RBAC)


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_deployer_can_manage_every_rendered_kind(env):
    role, _ = deployer_rbac()
    needed = [("", "configmaps", "Helm release history")]
    for resource in manifests(env).values():
        kind = resource["kind"]
        plural = kind.lower() + ("es" if kind.endswith("s") else "s")
        needed.append((resource["apiVersion"].rpartition("/")[0], plural, kind))

    missing = [
        f"{group or 'core'}/{plural} ({why}): missing {sorted(MANAGE_VERBS - granted_verbs(role['rules'], group, plural))}"
        for group, plural, why in needed
        if MANAGE_VERBS - granted_verbs(role["rules"], group, plural)
    ]
    assert not missing, f"{DEPLOYER_RBAC.name} cannot deploy the {env} chart:\n  " + "\n  ".join(missing)


def test_deployer_rbac_stays_narrow():
    role, docs = deployer_rbac()
    problems = []
    for doc in docs:
        if doc["kind"] not in ("ClusterRole", "RoleBinding"):
            problems.append(f"unexpected {doc['kind']}: only namespaced RoleBindings may grant the role")
        if doc["kind"] == "RoleBinding" and doc["roleRef"]["name"] != role["metadata"]["name"]:
            problems.append(f"RoleBinding in {doc['metadata']['namespace']} must reference {role['metadata']['name']}")
    for rule in role["rules"]:
        resources, verbs = set(rule.get("resources", [])), set(rule.get("verbs", []))
        if "*" in resources | verbs | set(rule.get("apiGroups", [])):
            problems.append(f"wildcard in rule {rule}")
        if resources & FORBIDDEN_RESOURCES:
            problems.append(f"must not grant {sorted(resources & FORBIDDEN_RESOURCES)}")
        if verbs & FORBIDDEN_VERBS:
            problems.append(f"must not grant verbs {sorted(verbs & FORBIDDEN_VERBS)}")
        if "rbac.authorization.k8s.io" in rule.get("apiGroups", []):
            problems.append("must not manage RBAC objects")

    groups = [s["name"] for doc in docs if doc["kind"] == "RoleBinding" for s in doc["subjects"]]
    if len(groups) != len(set(groups)):
        problems.append(f"each environment needs its own group, or one role can deploy to both: {groups}")
    assert not problems, f"{DEPLOYER_RBAC.name}:\n  " + "\n  ".join(problems)
