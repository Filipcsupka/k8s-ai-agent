"""
Read-only Kubernetes tools used by the agent to investigate alerts.
All functions use the Python kubernetes SDK — in-cluster auth when running in k8s,
kubeconfig file for local dev.
"""

import json
import os
from kubernetes import client, config
from langchain_core.tools import tool

# k3s uses projected (bound) service account tokens that rotate.
# Global config caches the token at import time → goes stale → 401.
# Fix: read token fresh from file on every API client creation.
_SA_TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_SA_CERT  = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"


def _api_client() -> client.ApiClient:
    """Build ApiClient. In-cluster: reads SA token fresh every call (handles rotation)."""
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        host = (
            f"https://{os.environ['KUBERNETES_SERVICE_HOST']}"
            f":{os.environ['KUBERNETES_SERVICE_PORT_HTTPS']}"
        )
        with open(_SA_TOKEN) as f:
            token = f.read().strip()
        cfg = client.Configuration(
            host=host,
            api_key={"authorization": f"Bearer {token}"},
        )
        cfg.ssl_ca_cert = _SA_CERT
        return client.ApiClient(configuration=cfg)
    else:
        config.load_kube_config(
            config_file=os.getenv("KUBECONFIG", os.path.expanduser("~/.kube/config"))
        )
        return client.ApiClient()


def _core() -> client.CoreV1Api:
    return client.CoreV1Api(api_client=_api_client())


def _apps() -> client.AppsV1Api:
    return client.AppsV1Api(api_client=_api_client())


@tool
def get_pod_logs(namespace: str, pod_name: str, container: str = "", lines: int = 100) -> str:
    """
    Get logs from a pod container. Use to find error messages, stack traces, crash reasons.
    Args:
        namespace: k8s namespace
        pod_name: exact pod name
        container: container name (optional, needed for multi-container pods)
        lines: number of tail lines to return (default 100)
    """
    try:
        kwargs: dict = {"namespace": namespace, "name": pod_name, "tail_lines": lines}
        if container:
            kwargs["container"] = container
        logs = _core().read_namespaced_pod_log(**kwargs)
        return logs.strip() or "(no log output)"
    except Exception as e:
        return f"ERROR getting logs for {namespace}/{pod_name}: {e}"


@tool
def get_previous_pod_logs(namespace: str, pod_name: str, container: str = "", lines: int = 100) -> str:
    """
    Get logs from the PREVIOUS (crashed) container instance.
    Use for CrashLoopBackOff — current container may not have useful logs yet.
    """
    try:
        kwargs: dict = {
            "namespace": namespace,
            "name": pod_name,
            "tail_lines": lines,
            "previous": True,
        }
        if container:
            kwargs["container"] = container
        logs = _core().read_namespaced_pod_log(**kwargs)
        return logs.strip() or "(no previous logs)"
    except Exception as e:
        return f"ERROR getting previous logs for {namespace}/{pod_name}: {e}"


@tool
def get_events(namespace: str, resource_name: str = "") -> str:
    """
    Get Kubernetes events for a namespace. Optionally filter by resource name.
    Shows warnings, errors, scheduling failures, image pull errors.
    Args:
        namespace: k8s namespace
        resource_name: filter events by involved object name (optional)
    """
    try:
        events = _core().list_namespaced_event(namespace=namespace)
        items = events.items

        if resource_name:
            items = [e for e in items if resource_name in (e.involved_object.name or "")]

        # sort by time descending, take top 25
        def ts(e):
            return e.last_timestamp or e.event_time or ""

        items = sorted(items, key=ts, reverse=True)[:25]

        if not items:
            return f"(no events in namespace={namespace}" + (f" for {resource_name}" if resource_name else "") + ")"

        lines = []
        for e in items:
            lines.append(
                f"[{e.type}] {e.reason}: {e.message} "
                f"(object: {e.involved_object.kind}/{e.involved_object.name}, count: {e.count})"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"ERROR getting events in {namespace}: {e}"


@tool
def describe_pod(namespace: str, pod_name: str) -> str:
    """
    Describe a pod: phase, conditions, container states, restart counts, resource limits/requests.
    Use to check OOMKilled reason, resource limits, readiness conditions.
    """
    try:
        pod = _core().read_namespaced_pod(name=pod_name, namespace=namespace)

        container_info = []
        for cs in pod.status.container_statuses or []:
            state_str = ""
            if cs.state.running:
                state_str = f"running since {cs.state.running.started_at}"
            elif cs.state.waiting:
                state_str = f"waiting: {cs.state.waiting.reason} — {cs.state.waiting.message}"
            elif cs.state.terminated:
                t = cs.state.terminated
                state_str = f"terminated: exitCode={t.exit_code} reason={t.reason} message={t.message}"

            last_state_str = ""
            if cs.last_state.terminated:
                t = cs.last_state.terminated
                last_state_str = f"exitCode={t.exit_code} reason={t.reason} finishedAt={t.finished_at}"

            container_info.append({
                "name": cs.name,
                "ready": cs.ready,
                "restartCount": cs.restart_count,
                "state": state_str,
                "lastState": last_state_str or "none",
            })

        # resource limits from spec
        resource_info = []
        for c in pod.spec.containers:
            res = c.resources
            resource_info.append({
                "name": c.name,
                "requests": res.requests if res else None,
                "limits": res.limits if res else None,
            })

        conditions = [
            {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
            for c in (pod.status.conditions or [])
        ]

        result = {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "phase": pod.status.phase,
            "nodeName": pod.spec.node_name,
            "conditions": conditions,
            "containers": container_info,
            "resources": resource_info,
        }
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        return f"ERROR describing pod {namespace}/{pod_name}: {e}"


@tool
def describe_deployment(namespace: str, name: str) -> str:
    """
    Describe a deployment: replicas, conditions, strategy, resource limits.
    Use when a deployment is not reaching desired replica count.
    """
    try:
        dep = _apps().read_namespaced_deployment(name=name, namespace=namespace)
        result = {
            "name": dep.metadata.name,
            "namespace": dep.metadata.namespace,
            "replicas": {
                "desired": dep.spec.replicas,
                "ready": dep.status.ready_replicas,
                "available": dep.status.available_replicas,
                "unavailable": dep.status.unavailable_replicas,
            },
            "conditions": [
                {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
                for c in (dep.status.conditions or [])
            ],
            "containers": [
                {
                    "name": c.name,
                    "image": c.image,
                    "requests": c.resources.requests if c.resources else None,
                    "limits": c.resources.limits if c.resources else None,
                }
                for c in dep.spec.template.spec.containers
            ],
        }
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        return f"ERROR describing deployment {namespace}/{name}: {e}"


@tool
def list_pods(namespace: str, label_selector: str = "") -> str:
    """
    List all pods in a namespace with phase, ready status, restart count.
    Use as the first tool to get an overview of namespace health.
    Args:
        namespace: k8s namespace
        label_selector: optional label filter e.g. "app=my-app"
    """
    try:
        kwargs: dict = {"namespace": namespace}
        if label_selector:
            kwargs["label_selector"] = label_selector
        pods = _core().list_namespaced_pod(**kwargs)

        if not pods.items:
            return f"(no pods in namespace={namespace})"

        lines = []
        for pod in pods.items:
            statuses = pod.status.container_statuses or []
            restarts = sum(c.restart_count for c in statuses)
            ready_count = sum(1 for c in statuses if c.ready)
            total_count = len(statuses)
            lines.append(
                f"{pod.metadata.name}: phase={pod.status.phase} "
                f"ready={ready_count}/{total_count} restarts={restarts} "
                f"node={pod.spec.node_name}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"ERROR listing pods in {namespace}: {e}"


@tool
def get_node_status() -> str:
    """
    Get status of all cluster nodes: Ready condition, CPU/memory capacity, taints.
    Use for node-related alerts or when pods are stuck Pending (scheduling issues).
    """
    try:
        nodes = _core().list_node()
        lines = []
        for node in nodes.items:
            conditions = {c.type: c.status for c in (node.status.conditions or [])}
            ready = conditions.get("Ready", "Unknown")
            taints = [f"{t.key}={t.value}:{t.effect}" for t in (node.spec.taints or [])]
            cap = node.status.capacity or {}
            lines.append(
                f"{node.metadata.name}: Ready={ready} "
                f"cpu={cap.get('cpu')} memory={cap.get('memory')} "
                f"taints=[{', '.join(taints)}]"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"ERROR getting node status: {e}"


@tool
def get_resource_usage(namespace: str) -> str:
    """
    Get current CPU and memory usage for pods via metrics-server.
    Use for OOMKilled or high CPU alerts to compare usage vs limits.
    Requires metrics-server to be installed in the cluster.
    """
    try:
        custom = client.CustomObjectsApi(api_client=_api_client())
        metrics = custom.list_namespaced_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            namespace=namespace,
            plural="pods",
        )
        lines = []
        for pod in metrics.get("items", []):
            pod_name = pod["metadata"]["name"]
            for container in pod.get("containers", []):
                usage = container.get("usage", {})
                lines.append(
                    f"{pod_name}/{container['name']}: "
                    f"cpu={usage.get('cpu', 'n/a')} memory={usage.get('memory', 'n/a')}"
                )
        return "\n".join(lines) or f"(no metrics available for namespace={namespace})"
    except Exception as e:
        return f"ERROR getting resource usage (metrics-server may not be installed): {e}"
