"""
FastAPI entrypoint.

Endpoints:
  GET  /health       — liveness check
  POST /alert        — AlertManager webhook receiver
  POST /investigate  — manual trigger for testing (send an alert payload directly)
"""

import logging
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel

from app.agent import run_agent
from app.config import settings
from app.tools import apply as apply_tools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="k8s-ai-agent",
    version="0.1.0",
    description="Kubernetes L2 support AI agent — Phase 1 (read-only diagnosis)",
)

# Semaphore limits concurrent investigations to avoid hammering Ollama
import asyncio
_semaphore = asyncio.Semaphore(settings.max_concurrent_investigations)
_in_flight: set[str] = set()


def _alert_key(alert: dict) -> str:
    labels = alert.get("labels", {})
    return f"{labels.get('alertname')}|{labels.get('namespace')}|{labels.get('pod', '')}"


async def _gated_run(alert: dict) -> None:
    key = _alert_key(alert)
    if key in _in_flight:
        logger.info("Dedup: skipping %s (already in flight)", key)
        return
    _in_flight.add(key)
    try:
        async with _semaphore:
            await run_agent(alert)
    finally:
        _in_flight.discard(key)


# ── Models ────────────────────────────────────────────────────────────────────

class AlertManagerPayload(BaseModel):
    """AlertManager webhook v4 payload schema."""
    version: str = "4"
    groupKey: str = ""
    status: str  # "firing" | "resolved"
    groupLabels: dict = {}
    commonLabels: dict = {}
    commonAnnotations: dict = {}
    externalURL: str = ""
    alerts: list[dict] = []


class ManualAlert(BaseModel):
    """Minimal alert for manual/test triggers."""
    alertname: str
    namespace: str
    pod: str = ""
    severity: str = "warning"
    summary: str = ""
    description: str = ""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "model": settings.ollama_model, "auto_apply": settings.enable_auto_apply}


@app.post("/alert")
async def receive_alert(payload: AlertManagerPayload, background_tasks: BackgroundTasks):
    """AlertManager webhook endpoint. Fires one agent investigation per firing alert."""
    if payload.status != "firing":
        logger.info("Ignoring %s alert (not firing)", payload.status)
        return {"status": "ignored", "reason": "only firing alerts are investigated"}

    if not payload.alerts:
        return {"status": "ignored", "reason": "no alerts in payload"}

    for alert in payload.alerts:
        background_tasks.add_task(_gated_run, alert)
        logger.info(
            "Queued investigation: %s",
            alert.get("labels", {}).get("alertname", "unknown"),
        )

    return {"status": "accepted", "count": len(payload.alerts)}


class ApplyRequest(BaseModel):
    """Payload for the human-approval apply endpoint."""
    action: str        # restart_pod | scale_deployment | patch_deployment_memory
    namespace: str
    pod_name: str = ""      # restart_pod
    name: str = ""          # scale_deployment, patch_deployment_memory
    replicas: int = 1       # scale_deployment
    container: str = ""     # patch_deployment_memory
    memory_limit: str = ""  # patch_deployment_memory


@app.post("/apply")
async def apply_action(req: ApplyRequest):
    """
    Human-approval endpoint — execute a fix proposed by the agent.

    Requires ENABLE_AUTO_APPLY=true. Caller is responsible for reviewing the
    agent's diagnosis in Slack before triggering this.

    Example (from inside cluster):
        kubectl exec -n ai-agent deploy/k8s-ai-agent -- \\
          curl -s -X POST http://localhost:8000/apply \\
               -H 'Content-Type: application/json' \\
               -d '{"action":"restart_pod","namespace":"ai-chat","pod_name":"rag-api-xxx"}'
    """
    if not settings.enable_auto_apply:
        raise HTTPException(status_code=403, detail="ENABLE_AUTO_APPLY=false — set to true to enable /apply")

    logger.info("Apply request: action=%s namespace=%s", req.action, req.namespace)

    if req.action == "restart_pod":
        if not req.pod_name:
            raise HTTPException(status_code=400, detail="pod_name required for restart_pod")
        result = apply_tools.restart_pod(namespace=req.namespace, pod_name=req.pod_name)
    elif req.action == "scale_deployment":
        if not req.name:
            raise HTTPException(status_code=400, detail="name required for scale_deployment")
        result = apply_tools.scale_deployment(namespace=req.namespace, name=req.name, replicas=req.replicas)
    elif req.action == "patch_deployment_memory":
        if not req.name or not req.container or not req.memory_limit:
            raise HTTPException(status_code=400, detail="name, container, memory_limit required")
        result = apply_tools.patch_deployment_memory(
            namespace=req.namespace, name=req.name,
            container=req.container, memory_limit=req.memory_limit,
        )
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {req.action}")

    if result.startswith("ERROR"):
        raise HTTPException(status_code=500, detail=result)

    return {"status": "applied", "action": req.action, "result": result}


@app.post("/investigate")
async def manual_investigate(alert: ManualAlert, background_tasks: BackgroundTasks):
    """
    Manual trigger for testing — fire an investigation without AlertManager.
    Example:
        curl -X POST http://localhost:8000/investigate \\
          -H 'Content-Type: application/json' \\
          -d '{"alertname":"CrashLoopBackOff","namespace":"ai-chat","pod":"rag-api-xxx"}'
    """
    payload = {
        "labels": {
            "alertname": alert.alertname,
            "namespace": alert.namespace,
            "pod": alert.pod,
            "severity": alert.severity,
        },
        "annotations": {
            "summary": alert.summary,
            "description": alert.description,
        },
    }
    background_tasks.add_task(_gated_run, payload)
    return {"status": "accepted", "alert": alert.alertname}
