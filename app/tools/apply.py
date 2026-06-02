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


def _check_gate(action: str) -> None:
    if not settings.enable_auto_apply:
        raise PermissionError(
            f"Action '{action}' blocked: ENABLE_AUTO_APPLY=false. "
            "Set ENABLE_AUTO_APPLY=true to enable the /apply endpoint."
        )


def restart_pod(namespace: str, pod_name: str) -> str:
    """Delete a pod to force restart. Safe for pods managed by a Deployment/ReplicaSet."""
    _check_gate("restart_pod")
    core = client.CoreV1Api(api_client=_api_client())
    try:
        core.delete_namespaced_pod(name=pod_name, namespace=namespace)
        logger.info("Deleted pod %s/%s — controller will recreate", namespace, pod_name)
        return f"Pod {namespace}/{pod_name} deleted. Controller will recreate it."
    except Exception as e:
        return f"ERROR deleting pod {namespace}/{pod_name}: {e}"


def scale_deployment(namespace: str, name: str, replicas: int) -> str:
    """Scale a deployment to the given replica count."""
    _check_gate("scale_deployment")
    apps = client.AppsV1Api(api_client=_api_client())
    try:
        apps.patch_namespaced_deployment_scale(
            name=name, namespace=namespace, body={"spec": {"replicas": replicas}}
        )
        logger.info("Scaled %s/%s to %d replicas", namespace, name, replicas)
        return f"Deployment {namespace}/{name} scaled to {replicas} replicas."
    except Exception as e:
        return f"ERROR scaling {namespace}/{name}: {e}"


def patch_deployment_memory(namespace: str, name: str, container: str, memory_limit: str) -> str:
    """
    Update memory limit for a container in a deployment.
    memory_limit format: '512Mi', '1Gi', etc.
    Use for OOMKilled fixes when the pod needs more memory.
    """
    _check_gate("patch_deployment_memory")
    apps = client.AppsV1Api(api_client=_api_client())
    try:
        dep = apps.read_namespaced_deployment(name=name, namespace=namespace)
        containers = dep.spec.template.spec.containers
        target = next((c for c in containers if c.name == container), None)
        if target is None:
            return f"ERROR: container '{container}' not found in {namespace}/{name}"
        if target.resources is None:
            target.resources = client.V1ResourceRequirements()
        if target.resources.limits is None:
            target.resources.limits = {}
        target.resources.limits["memory"] = memory_limit
        apps.patch_namespaced_deployment(name=name, namespace=namespace, body=dep)
        logger.info("Patched %s/%s container=%s memory limit → %s", namespace, name, container, memory_limit)
        return f"Deployment {namespace}/{name} container={container} memory limit set to {memory_limit}."
    except Exception as e:
        return f"ERROR patching deployment {namespace}/{name}: {e}"
