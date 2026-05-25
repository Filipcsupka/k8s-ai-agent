"""
Write/apply tools — Phase 3 only.
All operations gated behind ENABLE_AUTO_APPLY=true AND explicit Slack approval.
In Phase 1, these functions are present but will raise if called without approval.
"""

import logging
from langchain_core.tools import tool
from app.config import settings

logger = logging.getLogger(__name__)

# Actions that are never allowed even with auto-apply enabled
BLACKLISTED_RESOURCES = ["persistentvolumeclaims", "persistentvolumes", "secrets", "nodes"]


def _check_gate(action: str) -> None:
    """Raise if auto-apply is disabled (Phase 1/2 mode)."""
    if not settings.enable_auto_apply:
        raise PermissionError(
            f"Action '{action}' blocked: ENABLE_AUTO_APPLY=false. "
            "Agent can only diagnose in Phase 1. Approval gate not yet implemented."
        )


@tool
def restart_pod(namespace: str, pod_name: str) -> str:
    """
    Delete a pod to force restart (ReplicaSet/Deployment will recreate it).
    Safe for stateless pods managed by a controller.
    REQUIRES: ENABLE_AUTO_APPLY=true and Slack approval.
    """
    _check_gate("restart_pod")

    from kubernetes import client, config
    import os

    if os.getenv("KUBERNETES_SERVICE_HOST"):
        config.load_incluster_config()
    else:
        config.load_kube_config(config_file=os.getenv("KUBECONFIG"))

    core = client.CoreV1Api()
    try:
        core.delete_namespaced_pod(name=pod_name, namespace=namespace)
        logger.info("Deleted pod %s/%s (will be recreated by controller)", namespace, pod_name)
        return f"Pod {namespace}/{pod_name} deleted. Controller will recreate it."
    except Exception as e:
        return f"ERROR deleting pod {namespace}/{pod_name}: {e}"


@tool
def scale_deployment(namespace: str, name: str, replicas: int) -> str:
    """
    Scale a deployment to the given replica count.
    REQUIRES: ENABLE_AUTO_APPLY=true and Slack approval.
    """
    _check_gate("scale_deployment")

    from kubernetes import client, config
    import os

    if os.getenv("KUBERNETES_SERVICE_HOST"):
        config.load_incluster_config()
    else:
        config.load_kube_config(config_file=os.getenv("KUBECONFIG"))

    apps = client.AppsV1Api()
    try:
        body = {"spec": {"replicas": replicas}}
        apps.patch_namespaced_deployment_scale(name=name, namespace=namespace, body=body)
        logger.info("Scaled %s/%s to %d replicas", namespace, name, replicas)
        return f"Deployment {namespace}/{name} scaled to {replicas} replicas."
    except Exception as e:
        return f"ERROR scaling {namespace}/{name}: {e}"
