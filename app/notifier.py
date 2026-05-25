"""
Slack notifier — posts investigation results to a Slack incoming webhook.
Falls back to stdout if SLACK_WEBHOOK_URL is not set.
"""

import logging
import httpx
from app.config import settings

logger = logging.getLogger(__name__)

SLACK_CHAR_LIMIT = 2900  # leave headroom under Slack's 3000-char attachment limit


async def notify_slack(alert_name: str, namespace: str, pod: str, diagnosis: str) -> None:
    truncated = diagnosis[:SLACK_CHAR_LIMIT] + ("…" if len(diagnosis) > SLACK_CHAR_LIMIT else "")

    if not settings.slack_webhook_url:
        logger.info(
            "[SLACK-STDOUT] Alert=%s namespace=%s pod=%s\n%s",
            alert_name, namespace, pod, diagnosis,
        )
        return

    payload = {
        "text": f":rotating_light: *K8s Alert: {alert_name}*  |  namespace: `{namespace}`  |  pod: `{pod}`",
        "attachments": [
            {
                "color": "danger",
                "text": truncated,
                "footer": "k8s-ai-agent • Phase 1 (read-only)",
                "mrkdwn_in": ["text"],
            }
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(settings.slack_webhook_url, json=payload)
            resp.raise_for_status()
    except Exception as e:
        logger.error("Failed to send Slack notification: %s", e)
