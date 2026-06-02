"""
Slack notifier — posts investigation results to a Slack incoming webhook.
Falls back to stdout if SLACK_WEBHOOK_URL is not set.

When a proposed_action is present, appends a second attachment with the
exact /apply curl command so the operator can approve with one copy-paste.
"""

import json
import logging
import httpx
from app.config import settings

logger = logging.getLogger(__name__)

SLACK_CHAR_LIMIT = 2500  # headroom for second attachment


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
    proposed_action: dict | None = None,
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
        params = {k: v for k, v in proposed_action.items() if k != "action"}
        param_str = " | ".join(f"{k}=`{v}`" for k, v in params.items())
        snippet = _apply_snippet(proposed_action)
        attachments.append(
            {
                "color": "warning",
                "title": f":wrench: Proposed action: {action_name}",
                "text": f"{param_str}\n\n*To apply (run from any node with kubectl access):*\n{snippet}",
                "footer": "Review diagnosis above before applying. This is irreversible for restart_pod.",
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
