"""
Slack notifier — posts investigation results to a Slack incoming webhook.
Falls back to stdout if SLACK_WEBHOOK_URL is not set.

When a proposed_action is present, appends a second attachment with the
exact /apply curl command so the operator can approve with one copy-paste.
"""

import json
import logging
from typing import Optional
import httpx
from app.config import settings

logger = logging.getLogger(__name__)

SLACK_CHAR_LIMIT = 2500  # headroom for second attachment


_ACTION_DESCRIPTIONS: dict[str, str] = {
    "restart_pod": (
        "Deletes the pod — Kubernetes controller recreates it automatically. "
        "Equivalent to `kubectl delete pod`. Pod will be unavailable for ~10-30s."
    ),
    "scale_deployment": (
        "Changes the deployment replica count. "
        "Scaling to 0 stops all pods; scaling up creates new ones."
    ),
    "patch_deployment_memory": (
        "Updates the memory limit for a container in the deployment. "
        "Triggers a rolling restart — old pods replaced one by one."
    ),
}


def _action_target(action: dict) -> str:
    """Human-readable target description for the proposed action."""
    a = action.get("action", "unknown")
    ns = action.get("namespace", "?")
    if a == "restart_pod":
        return f"pod `{action.get('pod_name', '?')}` in namespace `{ns}`"
    if a == "scale_deployment":
        return f"deployment `{action.get('name', '?')}` in namespace `{ns}` → replicas={action.get('replicas', '?')}"
    if a == "patch_deployment_memory":
        return (
            f"deployment `{action.get('name', '?')}` container `{action.get('container', '?')}` "
            f"in namespace `{ns}` → memory limit={action.get('memory_limit', '?')}"
        )
    return f"namespace `{ns}`"


def _apply_snippet(action: dict) -> str:
    """Build the curl command for POST /apply from the action dict."""
    payload = json.dumps(action, separators=(",", ":"))
    return (
        "```\n"
        "kubectl exec -n ai-agent deploy/k8s-ai-agent -- \\\n"
        "  curl -s -X POST http://localhost:8000/apply \\\n"
        "       -H 'Content-Type: application/json' \\\n"
        f"       -d '{payload}'\n"
        "```"
    )


async def notify_slack(
    alert_name: str,
    namespace: str,
    pod: str,
    diagnosis: str,
    proposed_action: Optional[dict] = None,
) -> None:
    truncated = diagnosis[:SLACK_CHAR_LIMIT] + ("…" if len(diagnosis) > SLACK_CHAR_LIMIT else "")

    if not settings.slack_webhook_url:
        action_str = f"\nProposed action: {proposed_action}" if proposed_action else ""
        logger.info(
            "[SLACK-STDOUT] Alert=%s namespace=%s pod=%s\n%s%s",
            alert_name, namespace, pod, diagnosis, action_str,
        )
        return

    attachments = [
        {
            "color": "danger",
            "text": truncated,
            "footer": "k8s-ai-agent • Phase 2 (read-only diagnosis + proposed actions)",
            "mrkdwn_in": ["text"],
        }
    ]

    if proposed_action:
        action_name = proposed_action.get("action", "unknown")
        description = _ACTION_DESCRIPTIONS.get(action_name, "Executes a cluster change.")
        target = _action_target(proposed_action)
        snippet = _apply_snippet(proposed_action)
        attachments.append(
            {
                "color": "warning",
                "title": f":wrench: Proposed fix: {action_name}",
                "text": (
                    f"*Target:* {target}\n"
                    f"*What it does:* {description}\n\n"
                    f"*Agent does NOT apply this automatically.* "
                    f"Review the diagnosis above, then run this command to approve:\n"
                    f"{snippet}"
                ),
                "footer": "This command calls /apply on the agent pod — only runs if ENABLE_AUTO_APPLY=true.",
                "mrkdwn_in": ["text"],
            }
        )

    payload = {
        "text": (
            f":rotating_light: *K8s Alert: {alert_name}*  |  "
            f"namespace: `{namespace}`  |  pod: `{pod}`"
        ),
        "attachments": attachments,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.post(settings.slack_webhook_url, json=payload)
            resp.raise_for_status()
    except Exception as e:
        logger.error("Failed to send Slack notification: %s", e)
