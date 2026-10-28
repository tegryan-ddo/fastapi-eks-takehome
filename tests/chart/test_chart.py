"""Chart checks: Helm lint, Kubernetes schema validation, the relationships
between rendered resources that a schema cannot establish, and whether the
deployer RBAC in platform/ can deploy the chart without broad permissions.

    python -m pytest tests/chart

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
RUNTIME_SECRET = VALUES["runtimeSecret"]["name"]
# Everything the chart can render: the Ingress is off by default.
ALL_KINDS = ("ingress.enabled=true", "ingress.host=staging.example.test")
# Each workload gets only the keys it needs; only the API handles webhooks.
EXPECTED_SECRET_KEYS = {
    "api": {"DATABASE_URL", "PAYMENTS_WEBHOOK_SECRET"},
    "batch": {"DATABASE_URL"},
    "migrate": {"DATABASE_URL"},
}

# The demo application's actual endpoints and entry points
# (examples/demo-api/app/main.py, app/batch.py, app/migrate.py).
LIVE_PATH = "/health/live"
READY_PATH = "/health/ready"
BATCH_COMMAND = ["python", "-m", "app.batch"]
MIGRATE_COMMAND = ["python", "-m", "app.migrate"]

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


def api_pod(resources):
    return resources["Deployment"]["spec"]["template"]


def batch_pod(resources):
    return resources["CronJob"]["spec"]["jobTemplate"]["spec"]["template"]


def migrate_pod(resources):
    return resources["Job"]["spec"]["template"]


def workload_pods(resources):
    return {
        "api": api_pod(resources)["spec"],
        "batch": batch_pod(resources)["spec"],
        "migrate": migrate_pod(resources)["spec"],
    }


def container(pod_spec):
    return pod_spec["containers"][0]


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


@pytest.mark.parametrize("overrides", [(), ALL_KINDS], ids=["defaults", "with-ingress"])
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_rendered_resources_match_kubernetes_schema(env, overrides, tmp_path):
    rendered = tmp_path / f"{env}.yaml"
    rendered.write_text(helm_template(env, *overrides))
    kubeconform(rendered)


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_api_selectors_select_api_pods_only(env):
    resources = manifests(env)
    api_labels = api_pod(resources)["metadata"]["labels"]
    other_labels = {
        "batch": batch_pod(resources)["metadata"]["labels"],
        "migrate": migrate_pod(resources)["metadata"]["labels"],
    }
    selectors = {
        "Service": resources["Service"]["spec"]["selector"],
        "Deployment": resources["Deployment"]["spec"]["selector"]["matchLabels"],
        "PodDisruptionBudget": resources["PodDisruptionBudget"]["spec"]["selector"]["matchLabels"],
    }

    problems = []
    for owner, selector in selectors.items():
        if not selector.items() <= api_labels.items():
            problems.append(f"{owner} selector {selector} does not match the API pod labels")
        for name, labels in other_labels.items():
            if selector.items() <= labels.items():
                problems.append(f"{owner} selector {selector} also matches {name} pods")
        if selector.get("app.kubernetes.io/component") != "api":
            problems.append(f"{owner} selector must include app.kubernetes.io/component=api")
        if "app.kubernetes.io/instance" not in selector:
            problems.append(f"{owner} selector must include the release (app.kubernetes.io/instance)")
        changing = {"app.kubernetes.io/version", "helm.sh/chart"} & selector.keys()
        if changing:
            problems.append(f"{owner} selector contains labels that change between releases: {sorted(changing)}")
    assert not problems, f"{env}:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_service_and_probes_target_the_http_container_port(env):
    resources = manifests(env)
    api = container(api_pod(resources)["spec"])
    ports = {p["name"]: p["containerPort"] for p in api["ports"]}

    assert ports.get("http") == 8000, f"Container must expose port 8000 named 'http'; found {ports}"
    target = resources["Service"]["spec"]["ports"][0]["targetPort"]
    assert target in ports, f"Service targetPort {target!r} is not a named container port ({sorted(ports)})"

    expected_paths = {"startupProbe": LIVE_PATH, "livenessProbe": LIVE_PATH, "readinessProbe": READY_PATH}
    for probe, path in expected_paths.items():
        http_get = api[probe]["httpGet"]
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
def test_every_workload_uses_the_same_image_reference(overrides, expected):
    resources = manifests("production", *overrides)
    images = {name: container(pod)["image"] for name, pod in workload_pods(resources).items()}
    assert images == dict.fromkeys(workload_pods(resources), expected), (
        f"With --set {list(overrides)}: expected {expected} for every workload, rendered {images}"
    )


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_workload_security_settings(env):
    resources = manifests(env)
    chart_sa = resources["ServiceAccount"]["metadata"]["name"]
    problems = []
    for name, pod in workload_pods(resources).items():
        pod_security = pod.get("securityContext", {})
        container_security = container(pod).get("securityContext", {})
        capabilities = container_security.get("capabilities", {})
        if pod_security.get("runAsNonRoot") is not True:
            problems.append(f"{name}: pod securityContext.runAsNonRoot must be true")
        if pod_security.get("runAsUser", 0) == 0:
            problems.append(f"{name}: pod securityContext.runAsUser must be a non-zero UID")
        if pod_security.get("seccompProfile", {}).get("type") != "RuntimeDefault":
            problems.append(f"{name}: pod seccompProfile must be RuntimeDefault")
        if container_security.get("allowPrivilegeEscalation") is not False:
            problems.append(f"{name}: allowPrivilegeEscalation must be false")
        if container_security.get("privileged"):
            problems.append(f"{name}: container must not be privileged")
        if "ALL" not in capabilities.get("drop", []):
            problems.append(f"{name}: container must drop ALL capabilities")
        if capabilities.get("add"):
            problems.append(f"{name}: container must not add capabilities: {capabilities['add']}")
        if container_security.get("readOnlyRootFilesystem") is not True:
            problems.append(f"{name}: readOnlyRootFilesystem must be true")
        if pod.get("automountServiceAccountToken") is not False:
            problems.append(f"{name}: automountServiceAccountToken must be false; the workloads do not call Kubernetes")
        if name == "migrate":
            # A pre-install hook runs before the chart's ServiceAccount exists.
            if pod.get("serviceAccountName") not in (None, "default"):
                problems.append(f"{name}: hook pod must use the namespace default ServiceAccount")
        elif pod.get("serviceAccountName") != chart_sa:
            problems.append(f"{name}: pod must use the chart's dedicated ServiceAccount")
    assert not problems, f"{env}:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_tmp_is_writable_with_read_only_root(env):
    for name, pod in workload_pods(manifests(env)).items():
        mounts = {m["mountPath"]: m["name"] for m in container(pod).get("volumeMounts", [])}
        volumes = {v["name"]: v for v in pod.get("volumes", [])}
        assert "/tmp" in mounts, f"{name}: /tmp must be mounted because the root filesystem is read-only"
        assert "emptyDir" in volumes.get(mounts["/tmp"], {}), f"{name}: /tmp must be backed by an emptyDir volume"


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_workloads_have_requests_and_memory_limits(env):
    problems = []
    for name, pod in workload_pods(manifests(env)).items():
        resources = container(pod).get("resources", {})
        for request in ("cpu", "memory"):
            if request not in resources.get("requests", {}):
                problems.append(f"{name}: missing {request} request")
        if "memory" not in resources.get("limits", {}):
            problems.append(f"{name}: missing memory limit")
    assert not problems, f"{env}:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_shutdown_fits_termination_grace_period(env):
    pod = api_pod(manifests(env))["spec"]
    pre_stop = container(pod)["lifecycle"]["preStop"]["sleep"]["seconds"]
    uvicorn = uvicorn_graceful_shutdown_seconds()
    grace = pod["terminationGracePeriodSeconds"]
    assert pre_stop + uvicorn < grace, (
        f"preStop sleep {pre_stop}s + Uvicorn graceful shutdown {uvicorn}s (Dockerfile) "
        f"must be less than terminationGracePeriodSeconds {grace}s"
    )


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_hpa_owns_the_replica_count(env):
    resources = manifests(env)
    hpa, deployment = resources["HorizontalPodAutoscaler"], resources["Deployment"]
    target = hpa["spec"]["scaleTargetRef"]
    assert (target["kind"], target["name"]) == ("Deployment", deployment["metadata"]["name"]), (
        f"HPA scaleTargetRef {target} does not name the Deployment {deployment['metadata']['name']}"
    )
    assert "replicas" not in deployment["spec"], (
        "Deployment must omit spec.replicas when the HPA is enabled, or each upgrade resets the HPA's count"
    )
    assert "cpu" in container(api_pod(resources)["spec"])["resources"].get("requests", {}), (
        "The API container needs a CPU request; CPU utilization is measured against it"
    )
    assert hpa["spec"]["minReplicas"] <= hpa["spec"]["maxReplicas"], "HPA minReplicas exceeds maxReplicas"


def test_fixed_replica_count_without_autoscaling():
    resources = manifests("production", "autoscaling.enabled=false")
    assert "HorizontalPodAutoscaler" not in resources, "No HPA may render when autoscaling is disabled"
    assert resources["Deployment"]["spec"].get("replicas") == VALUES["api"]["replicaCount"], (
        "Without autoscaling, the Deployment must use api.replicaCount"
    )


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_cronjob_runs_the_batch_command_with_bounded_settings(env):
    cronjob = manifests(env)["CronJob"]["spec"]
    job = cronjob["jobTemplate"]["spec"]
    pod = job["template"]["spec"]

    assert cronjob["suspend"] is (env == "staging"), (
        f"CronJob suspend must be {env == 'staging'} in {env}: staging runs only manual Jobs"
    )
    assert cronjob["concurrencyPolicy"] == "Forbid", "Scheduled runs must not overlap"
    assert cronjob["schedule"] and cronjob["timeZone"], "Schedule and an explicit time zone are required"
    assert container(pod)["command"] == BATCH_COMMAND, (
        f"Batch container must run {BATCH_COMMAND}, not {container(pod).get('command')}"
    )
    assert pod["restartPolicy"] == "Never", "Each retry must be a new pod, so failed attempts keep their logs"
    bounded = {
        "startingDeadlineSeconds": cronjob.get("startingDeadlineSeconds"),
        "successfulJobsHistoryLimit": cronjob.get("successfulJobsHistoryLimit"),
        "failedJobsHistoryLimit": cronjob.get("failedJobsHistoryLimit"),
        "backoffLimit": job.get("backoffLimit"),
        "activeDeadlineSeconds": job.get("activeDeadlineSeconds"),
        "ttlSecondsAfterFinished": job.get("ttlSecondsAfterFinished"),
    }
    unset = [name for name, value in bounded.items() if not isinstance(value, int)]
    assert not unset, f"CronJob settings must be explicit integers: {unset}"


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_migration_runs_once_as_a_helm_hook_before_workloads_change(env):
    job = manifests(env)["Job"]
    annotations = job["metadata"].get("annotations", {})
    spec = job["spec"]
    pod = spec["template"]["spec"]

    problems = []
    if annotations.get("helm.sh/hook") != "pre-install,pre-upgrade":
        problems.append(f"helm.sh/hook must be pre-install,pre-upgrade, not {annotations.get('helm.sh/hook')!r}")
    policies = set(annotations.get("helm.sh/hook-delete-policy", "").split(","))
    if not {"before-hook-creation", "hook-succeeded"} <= policies:
        problems.append(f"hook-delete-policy must include before-hook-creation and hook-succeeded, not {sorted(policies)}")
    if "hook-failed" in policies:
        problems.append("a failed migration Job must be kept for diagnosis, not deleted by hook-failed")
    if container(pod)["command"] != MIGRATE_COMMAND:
        problems.append(f"migrate container must run {MIGRATE_COMMAND}, not {container(pod).get('command')}")
    if spec.get("backoffLimit") != 0:
        problems.append("backoffLimit must be 0: the command is idempotent and the next release retries")
    if pod.get("restartPolicy") != "Never":
        problems.append("restartPolicy must be Never")
    for field in ("activeDeadlineSeconds", "ttlSecondsAfterFinished"):
        if not isinstance(spec.get(field), int):
            problems.append(f"{field} must be an explicit integer")
    assert not problems, f"{env}:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_secrets_are_referenced_never_embedded(env):
    assert "secretKeyRef" not in helm_template(env, "runtimeSecret.name="), (
        "With an empty runtimeSecret.name, nothing may reference a Secret"
    )

    pods = workload_pods(manifests(env))
    assert set(pods) == set(EXPECTED_SECRET_KEYS), "every workload needs an expected-keys entry"
    for name, pod in pods.items():
        env_vars = container(pod).get("env", [])
        expected = EXPECTED_SECRET_KEYS[name]
        assert {e["name"] for e in env_vars} == expected, f"{name} must expose exactly {sorted(expected)}"
        for entry in env_vars:
            assert "value" not in entry, f"{name}: {entry['name']} must come from a Secret, not a literal value"
            reference = entry["valueFrom"]["secretKeyRef"]
            assert (reference["name"], reference["key"]) == (RUNTIME_SECRET, entry["name"]), (
                f"{name}: {entry['name']} must read key {entry['name']} of Secret {RUNTIME_SECRET}"
            )


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_ingress_renders_only_when_enabled_and_targets_the_service(env):
    assert "Ingress" not in manifests(env), "No Ingress may render while ingress.enabled is false"

    resources = manifests(env, *ALL_KINDS)
    ingress, service = resources["Ingress"], resources["Service"]
    port_names = {p["name"] for p in service["spec"]["ports"]}
    rules = ingress["spec"]["rules"]

    problems = []
    if ingress["spec"].get("ingressClassName") != VALUES["ingress"]["className"]:
        problems.append(f"ingressClassName must be {VALUES['ingress']['className']!r}")
    if "tls" in ingress["spec"]:
        problems.append("spec.tls must be absent: TLS terminates at the ALB through annotations")
    if len(rules) != 1 or rules[0].get("host") != "staging.example.test":
        problems.append(f"expected one rule for the configured host, got {rules}")
    paths = rules[0]["http"]["paths"] if rules else []
    if [(p["path"], p["pathType"]) for p in paths] != [("/", "Prefix")]:
        problems.append(f"expected a single Prefix rule for /, got {paths}")
    for path in paths:
        backend = path["backend"]["service"]
        if backend["name"] != service["metadata"]["name"]:
            problems.append(f"backend {backend['name']!r} is not the chart Service {service['metadata']['name']!r}")
        if backend["port"].get("name") not in port_names:
            problems.append(f"backend port {backend['port']} is not a named Service port ({sorted(port_names)})")
    assert not problems, f"{env}:\n  " + "\n  ".join(problems)


def test_deployer_rbac_matches_kubernetes_schema():
    kubeconform(DEPLOYER_RBAC)


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_deployer_can_manage_every_rendered_kind(env):
    role, _ = deployer_rbac()
    needed = [("", "configmaps", "Helm release history")]
    for resource in manifests(env, *ALL_KINDS).values():
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
