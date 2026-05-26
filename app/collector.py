"""
Investigation collector — saves every completed agent investigation to disk.

Files land in settings.investigations_dir (default /data/investigations/).
Format: one JSON file per investigation, named by timestamp + alertname.

Structure:
  {
    "timestamp": "2026-05-26T21:44:52Z",
    "alert_name": "KubePodCrashLooping",
    "namespace": "chaos",
    "pod": "chaos-crashloop-xxx",
    "severity": "warning",
    "alert_labels": {...},
    "duration_sec": 28.4,
    "tool_calls": ["list_pods", "get_previous_pod_logs", "get_events"],
    "diagnosis": "## Diagnosis\n...",
    "reviewed": false,
    "correct": null,
    "notes": ""
  }

Phase 2: scripts/ingest_to_chroma.py reads files where reviewed=true, correct=true
and ingests diagnosis as runbook into ChromaDB collection k8s-runbooks.
"""

import json
import logging
import os
from datetime import datetime, timezone

from app.config import settings

logger = logging.getLogger(__name__)


def save_investigation(
    alert: dict,
    diagnosis: str,
    duration_sec: float,
    tool_calls: list[str],
) -> None:
    inv_dir = settings.investigations_dir
    try:
        os.makedirs(inv_dir, exist_ok=True)
    except Exception as e:
        logger.warning("Cannot create investigations dir %s: %s", inv_dir, e)
        return

    labels = alert.get("labels", {})
    alert_name = labels.get("alertname", "unknown")
    namespace = labels.get("namespace", "unknown")
    pod = labels.get("pod", "")

    ts = datetime.now(timezone.utc)
    ts_str = ts.strftime("%Y%m%dT%H%M%SZ")
    safe_alert = alert_name.replace("/", "-")
    filename = f"{ts_str}-{safe_alert}-{namespace}.json"

    record = {
        "timestamp": ts.isoformat(),
        "alert_name": alert_name,
        "namespace": namespace,
        "pod": pod,
        "severity": labels.get("severity", "unknown"),
        "alert_labels": labels,
        "duration_sec": round(duration_sec, 1),
        "tool_calls": tool_calls,
        "diagnosis": diagnosis,
        "reviewed": False,
        "correct": None,
        "notes": "",
    }

    path = os.path.join(inv_dir, filename)
    try:
        with open(path, "w") as f:
            json.dump(record, f, indent=2)
        logger.info("Investigation saved: %s (%.1fs, %d tools)", path, duration_sec, len(tool_calls))
    except Exception as e:
        logger.warning("Failed to save investigation to %s: %s", path, e)
