"""
Notifier — sends investigation results to Discord (native embeds) or Slack.

Priority: Discord → Slack → stdout.
"""

import json
import logging
from typing import Optional
import httpx
from app.config import settings

logger = logging.getLogger(__name__)

DISCORD_CHAR_LIMIT = 4000
SLACK_CHAR_LIMIT = 2500

# Discord embed colors (decimal)
_COLOR_ALERT = 15158332    # red    #E74C3C
_COLOR_ACTION = 15844367   # yellow #F1C40F
_COLOR_INFO = 3447003      # blue   #3498DB

_ACTION_DESCRIPTIONS: dict[str, str] = {
    "restart_pod": (
        "Deletes the pod — Kubernetes controller recreates it automatically. "
        "Equivalent to `kubectl delete pod`. Pod will be unavailable ~10-30s."
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
    a = action.get("action", "unknown")
    ns = action.get("namespace", "?")
    if a == "restart_pod":
        return f"pod `{action.get('pod_name', '?')}` in namespace `{ns}`"
    if a == "scale_deployment":
        return f"deployment `{action.get('name', '?')}` in `{ns}` → replicas={action.get('replicas', '?')}"
    if a == "patch_deployment_memory":
        return (
            f"deployment `{action.get('name', '?')}` container `{action.get('container', '?')}` "
            f"in `{ns}` → memory={action.get('memory_limit', '?')}"
        )
    return f"namespace `{ns}`"


def _apply_snippet(action: dict) -> str:
    payload = json.dumps(action, separators=(",", ":"))
    return (
        "```\n"
        "kubectl exec -n ai-agent deploy/k8s-ai-agent -- \\\n"
        "  curl -s -X POST http://localhost:8000/apply \\\n"
        "       -H 'Content-Type: application/json' \\\n"
        f"       -d '{payload}'\n"
        "```"
    )


async def _send_discord(
    alert_name: str,
    namespace: str,
    pod: str,
    diagnosis: str,
    proposed_action: Optional[dict],
) -> None:
    truncated = diagnosis[:DISCORD_CHAR_LIMIT] + ("…" if len(diagnosis) > DISCORD_CHAR_LIMIT else "")

    pod_str = f" • pod `{pod}`" if pod else ""
    content = f":rotating_light: **K8s Alert: {alert_name}** • namespace `{namespace}`{pod_str}"

    embeds = [
        {
            "description": truncated,
            "color": _COLOR_ALERT,
            "footer": {"text": "k8s-ai-agent"},
        }
    ]

    if proposed_action:
        action_name = proposed_action.get("action", "unknown")
        description = _ACTION_DESCRIPTIONS.get(action_name, "Executes a cluster change.")
        target = _action_target(proposed_action)
        snippet = _apply_snippet(proposed_action)
        embeds.append(
            {
                "title": f"🔧 Proposed fix: {action_name}",
                "description": (
                    f"**Target:** {target}\n"
                    f"**What it does:** {description}\n\n"
                    f"⚠️ **Agent does NOT apply this automatically.** "
                    f"Review diagnosis above, then run to approve:\n{snippet}"
                ),
                "color": _COLOR_ACTION,
                "footer": {"text": "Calls /apply on agent pod — only works if ENABLE_AUTO_APPLY=true"},
            }
        )

    payload = {"content": content, "embeds": embeds}
    async with httpx.AsyncClient(timeout=10.0) as http:
        resp = await http.post(settings.discord_webhook_url, json=payload)
        resp.raise_for_status()


async def _send_slack(
    alert_name: str,
    namespace: str,
    pod: str,
    diagnosis: str,
    proposed_action: Optional[dict],
) -> None:
    truncated = diagnosis[:SLACK_CHAR_LIMIT] + ("…" if len(diagnosis) > SLACK_CHAR_LIMIT else "")

    attachments = [
        {
            "color": "danger",
            "text": truncated,
            "footer": "k8s-ai-agent",
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
                    f"Review the diagnosis above, then run to approve:\n{snippet}"
                ),
                "footer": "Calls /apply on agent pod — only works if ENABLE_AUTO_APPLY=true",
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
    async with httpx.AsyncClient(timeout=10.0) as http:
        resp = await http.post(settings.slack_webhook_url, json=payload)
        resp.raise_for_status()


async def notify_slack(
    alert_name: str,
    namespace: str,
    pod: str,
    diagnosis: str,
    proposed_action: Optional[dict] = None,
) -> None:
    if settings.discord_webhook_url:
        try:
            await _send_discord(alert_name, namespace, pod, diagnosis, proposed_action)
            return
        except Exception as e:
            logger.error("Discord notification failed: %s", e)

    if settings.slack_webhook_url:
        try:
            await _send_slack(alert_name, namespace, pod, diagnosis, proposed_action)
            return
        except Exception as e:
            logger.error("Slack notification failed: %s", e)

    action_str = f"\nProposed action: {proposed_action}" if proposed_action else ""
    logger.info(
        "[STDOUT] Alert=%s namespace=%s pod=%s\n%s%s",
        alert_name, namespace, pod, diagnosis, action_str,
    )
