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
import re
import threading
from datetime import datetime, timezone

from app.config import settings

logger = logging.getLogger(__name__)

# In-memory dedup counter: (alert_name, namespace, pod_base) → count
# Populated lazily on first save by scanning existing files.
_counts: dict[tuple[str, str, str], int] = {}
_counts_lock = threading.Lock()
_counts_loaded = False


def _pod_base(pod: str) -> str:
    """Strip k8s ReplicaSet + pod hash suffixes: foo-7d9f8b-xkz2p → foo."""
    return re.sub(r"(-[a-f0-9]{7,10}){1,2}(-[a-z0-9]{5})?$", "", pod)


def _load_counts(inv_dir: str) -> None:
    """Scan existing JSON files and populate _counts. Called once."""
    global _counts_loaded
    try:
        for fname in os.listdir(inv_dir):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(inv_dir, fname)
            try:
                with open(fpath) as f:
                    rec = json.load(f)
                key = (
                    rec.get("alert_name", "unknown"),
                    rec.get("namespace", "unknown"),
                    _pod_base(rec.get("pod", "")),
                )
                _counts[key] = _counts.get(key, 0) + 1
            except Exception:
                pass
        logger.info("Dedup: loaded counts from %d existing files", sum(_counts.values()))
    except Exception as e:
        logger.warning("Dedup: failed to scan %s: %s", inv_dir, e)
    _counts_loaded = True


def save_investigation(
    alert: dict,
    diagnosis: str,
    duration_sec: float,
    tool_calls: list[str],
    proposed_action: dict | None = None,
) -> str | None:
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
    pod_base = _pod_base(pod)
    safe_alert = alert_name.replace("/", "-")
    key = (alert_name, namespace, pod_base)

    with _counts_lock:
        global _counts_loaded
        if not _counts_loaded:
            _load_counts(inv_dir)

        count = _counts.get(key, 0)
        limit = settings.max_investigations_per_scenario
        if count >= limit:
            logger.info(
                "Dedup: skip %s/%s/%s (%d/%d already saved)",
                alert_name, namespace, pod_base, count, limit,
            )
            try:
                matches = sorted(
                    f for f in os.listdir(inv_dir)
                    if f.endswith(".json") and f"-{safe_alert}-{namespace}." in f
                )
                if not matches:
                    return None
                filename = matches[-1]
                if proposed_action is not None:
                    path = os.path.join(inv_dir, filename)
                    try:
                        with open(path) as f:
                            rec = json.load(f)
                        rec["proposed_action"] = proposed_action
                        with open(path, "w") as f:
                            json.dump(rec, f, indent=2)
                    except Exception:
                        pass
                return filename
            except Exception:
                return None

        # Reserve slot before releasing lock
        _counts[key] = count + 1

    ts = datetime.now(timezone.utc)
    ts_str = ts.strftime("%Y%m%dT%H%M%SZ")
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
        "proposed_action": proposed_action,
        "reviewed": False,
        "correct": None,
        "notes": "",
    }

    path = os.path.join(inv_dir, filename)
    try:
        with open(path, "w") as f:
            json.dump(record, f, indent=2)
        logger.info(
            "Investigation saved: %s (%.1fs, %d tools, slot %d/%d)",
            path, duration_sec, len(tool_calls), count + 1, settings.max_investigations_per_scenario,
        )
        return filename
    except Exception as e:
        # Rollback counter on write failure
        with _counts_lock:
            _counts[key] = max(0, _counts.get(key, 1) - 1)
        logger.warning("Failed to save investigation to %s: %s", path, e)
        return None
