"""
Write/apply tools — Phase 3 only.
All operations gated behind ENABLE_AUTO_APPLY=true.
The /apply HTTP endpoint is the human approval gate — agent itself stays read-only.
"""

import logging
import os
from kubernetes import client, config
from langchain_core.tools import tool
from app.config import settings

logger = logging.getLogger(__name__)

_SA_TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_SA_CERT  = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

BLACKLISTED_RESOURCES = ["persistentvolumeclaims", "persistentvolumes", "secrets", "nodes"]
PROTECTED_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease", "argocd",
                         "cert-manager", "monitoring", "sealed-secrets"}

_MEMORY_UNITS = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
                  "K": 1000, "M": 1000**2, "G": 1000**3}
_MAX_MEMORY_BYTES = 16 * 1024**3  # 16Gi hard cap


def _parse_memory_bytes(s: str) -> int:
    for suffix, mult in _MEMORY_UNITS.items():
        if s.endswith(suffix):
            return int(s[:-len(suffix)]) * mult
    return int(s)


def _api_client() -> client.ApiClient:
    """Build ApiClient fresh per call — same pattern as k8s.py to avoid 401 on token rotation."""
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        with open(_SA_TOKEN) as f:
            token = f.read().strip()
        cfg = client.Configuration(
            host=f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT']}",
            api_key={"BearerToken": token},
            api_key_prefix={"BearerToken": "Bearer"},
        )
        cfg.ssl_ca_cert = _SA_CERT
        return client.ApiClient(configuration=cfg)
    else:
        kubeconfig_path = settings.kubeconfig or os.path.expanduser("~/.kube/config")
        config.load_kube_config(config_file=kubeconfig_path)
        return client.ApiClient()


def _check_gate(action: str, namespace: str = "") -> None:
    if not settings.enable_auto_apply:
        raise PermissionError(
            f"Action '{action}' blocked: ENABLE_AUTO_APPLY=false. "
            "Set ENABLE_AUTO_APPLY=true to enable the /apply endpoint."
        )
    if namespace in PROTECTED_NAMESPACES:
        raise PermissionError(
            f"Action '{action}' blocked: namespace '{namespace}' is protected. "
            "System namespaces (argocd, kube-system, etc.) are read-only."
        )


def restart_pod(namespace: str, pod_name: str) -> str:
    """Delete a pod to force restart. Safe for pods managed by a Deployment/ReplicaSet."""
    _check_gate("restart_pod", namespace)
    core = client.CoreV1Api(api_client=_api_client())
    try:
        core.delete_namespaced_pod(name=pod_name, namespace=namespace)
        logger.info("Deleted pod %s/%s — controller will recreate", namespace, pod_name)
        return f"Pod {namespace}/{pod_name} deleted. Controller will recreate it."
    except Exception as e:
        return f"ERROR deleting pod {namespace}/{pod_name}: {e}"


def scale_deployment(namespace: str, name: str, replicas: int) -> str:
    """Scale a deployment to the given replica count."""
    _check_gate("scale_deployment", namespace)
    apps = client.AppsV1Api(api_client=_api_client())
    try:
        apps.patch_namespaced_deployment_scale(
            name=name, namespace=namespace, body={"spec": {"replicas": replicas}}
        )
        logger.info("Scaled %s/%s to %d replicas", namespace, name, replicas)
        return f"Deployment {namespace}/{name} scaled to {replicas} replicas."
    except Exception as e:
        return f"ERROR scaling {namespace}/{name}: {e}"


def rollback_deployment(namespace: str, name: str) -> str:
    """
    Roll back a deployment to its previous revision.
    Equivalent to `kubectl rollout undo deployment/<name> -n <namespace>`.
    Finds the second-most-recent ReplicaSet owned by the deployment and restores its pod template.
    """
    _check_gate("rollback_deployment", namespace)
    api = _api_client()
    apps = client.AppsV1Api(api_client=api)
    try:
        dep = apps.read_namespaced_deployment(name=name, namespace=namespace)
        selector = dep.spec.selector.match_labels
        label_selector = ",".join(f"{k}={v}" for k, v in selector.items())

        rs_list = apps.list_namespaced_replica_set(namespace=namespace, label_selector=label_selector)
        owned: list[tuple[int, object]] = []
        for rs in rs_list.items:
            for ref in (rs.metadata.owner_references or []):
                if ref.kind == "Deployment" and ref.name == name:
                    rev = int((rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0"))
                    owned.append((rev, rs))

        if len(owned) < 2:
            return f"ERROR: {namespace}/{name} has no previous revision to roll back to"

        owned.sort(key=lambda x: x[0])
        prev_rev, prev_rs = owned[-2]

        dep.spec.template = prev_rs.spec.template
        apps.patch_namespaced_deployment(name=name, namespace=namespace, body=dep)
        logger.info("Rolled back %s/%s to revision %d", namespace, name, prev_rev)
        return f"Deployment {namespace}/{name} rolled back to revision {prev_rev}."
    except Exception as e:
        return f"ERROR rolling back {namespace}/{name}: {e}"


def patch_deployment_memory(namespace: str, name: str, container: str, memory_limit: str) -> str:
    """
    Update memory limit for a container in a deployment.
    memory_limit format: '512Mi', '1Gi', etc.
    Use for OOMKilled fixes when the pod needs more memory.
    """
    _check_gate("patch_deployment_memory", namespace)
    try:
        new_bytes = _parse_memory_bytes(memory_limit)
    except Exception:
        return f"ERROR: invalid memory_limit format '{memory_limit}' — use e.g. 512Mi, 1Gi"
    if new_bytes > _MAX_MEMORY_BYTES:
        return f"ERROR: memory_limit {memory_limit} exceeds 16Gi safety cap — refusing to apply"
    apps = client.AppsV1Api(api_client=_api_client())
    try:
        try:
            workload = apps.read_namespaced_deployment(name=name, namespace=namespace)
            patch_fn = lambda body: apps.patch_namespaced_deployment(name=name, namespace=namespace, body=body)
            kind = "Deployment"
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise
            workload = apps.read_namespaced_stateful_set(name=name, namespace=namespace)
            patch_fn = lambda body: apps.patch_namespaced_stateful_set(name=name, namespace=namespace, body=body)
            kind = "StatefulSet"
        dep = workload
        containers = dep.spec.template.spec.containers
        target = next((c for c in containers if c.name == container), None)
        if target is None:
            return f"ERROR: container '{container}' not found in {namespace}/{name}"
        if target.resources is None:
            target.resources = client.V1ResourceRequirements()
        if target.resources.limits is None:
            target.resources.limits = {}
        target.resources.limits["memory"] = memory_limit
        # Clamp requests to new limit if existing request > new limit (avoids 422)
        if target.resources.requests and "memory" in target.resources.requests:
            try:
                req_bytes = _parse_memory_bytes(target.resources.requests["memory"])
                if req_bytes > new_bytes:
                    logger.info("Clamping memory request from %s → %s to match new limit",
                                target.resources.requests["memory"], memory_limit)
                    target.resources.requests["memory"] = memory_limit
            except Exception:
                pass
        patch_fn(dep)
        logger.info("Patched %s %s/%s container=%s memory limit → %s", kind, namespace, name, container, memory_limit)
        return f"{kind} {namespace}/{name} container={container} memory limit set to {memory_limit}."
    except Exception as e:
        return f"ERROR patching deployment {namespace}/{name}: {e}"
