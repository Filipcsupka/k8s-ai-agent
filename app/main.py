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


async def _gated_run(alert: dict) -> None:
    async with _semaphore:
        await run_agent(alert)


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
