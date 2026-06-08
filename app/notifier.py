"""
Notifier — sends investigation results to Discord (native embeds + buttons) or Slack.

Priority: Discord bot API (with buttons) → Discord webhook (no buttons) → Slack → stdout.
"""

import json
import logging
import re
from typing import Optional
import httpx
from app.config import settings

logger = logging.getLogger(__name__)

# Regex to extract named sections from agent output
_SECTION_RE = re.compile(r"^##\s+(\w[\w ]*\w)\s*\n(.*?)(?=\n##\s|\Z)", re.MULTILINE | re.DOTALL)

DISCORD_CHAR_LIMIT = 4000
SLACK_CHAR_LIMIT = 2500

# Discord embed colors (decimal)
_COLOR_ALERT = 15158332    # red    #E74C3C
_COLOR_ACTION = 15844367   # yellow #F1C40F
_COLOR_INFO = 3447003      # blue   #3498DB

# Discord component styles
_STYLE_PRIMARY = 1   # blue
_STYLE_SUCCESS = 3   # green
_STYLE_DANGER = 4    # red

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
    "rollback_deployment": (
        "Rolls the deployment back to its previous revision. "
        "Equivalent to `kubectl rollout undo`. Triggers a rolling update back to the last known-good image."
    ),
}


def _parse_sections(text: str) -> dict[str, str]:
    """Extract ## Section content from agent diagnosis text."""
    return {m.group(1).strip(): m.group(2).strip() for m in _SECTION_RE.finditer(text)}


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
    if a == "rollback_deployment":
        return f"deployment `{action.get('name', '?')}` in namespace `{ns}` → previous revision"
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


def _build_embeds(
    alert_name: str,
    namespace: str,
    pod: str,
    diagnosis: str,
    proposed_action: Optional[dict],
    auto_applied: Optional[str] = None,
    is_rag_hit: bool = False,
) -> list[dict]:
    sections = _parse_sections(diagnosis)
    summary = sections.get("Summary", "")
    next_steps = sections.get("Next Steps", "")

    # Build full technical detail block (everything except Summary and Proposed Action)
    skip = {"Summary", "Proposed Action"}
    tech_parts = []
    for m in _SECTION_RE.finditer(diagnosis):
        title = m.group(1).strip()
        if title not in skip:
            tech_parts.append(f"**{title}**\n{m.group(2).strip()}")
    tech_detail = "\n\n".join(tech_parts)

    # Primary embed: plain-English summary (or full text if no summary section)
    primary_text = summary if summary else diagnosis[:DISCORD_CHAR_LIMIT]
    primary_text = primary_text[:DISCORD_CHAR_LIMIT] + ("…" if len(primary_text) > DISCORD_CHAR_LIMIT else "")
    embeds = [
        {
            "description": primary_text,
            "color": _COLOR_ALERT,
            "footer": {"text": "k8s-ai-agent"},
        }
    ]

    # Secondary embed: technical details — only for new investigations, not RAG cache hits
    # RAG hits are known patterns; the team has already seen the full diagnosis
    if tech_detail and summary and not is_rag_hit:
        tech_truncated = tech_detail[:DISCORD_CHAR_LIMIT] + ("…" if len(tech_detail) > DISCORD_CHAR_LIMIT else "")
        embeds.append(
            {
                "title": "🔍 Technical Details",
                "description": tech_truncated,
                "color": 0x2C2F33,  # dark grey — less prominent
            }
        )

    # Action embed
    rag_badge = " *(recurring — known pattern)*" if is_rag_hit else ""
    if proposed_action:
        action_name = proposed_action.get("action", "unknown")
        target = _action_target(proposed_action)
        if auto_applied:
            embeds.append(
                {
                    "title": f"⚡ Already fixed: {action_name}{rag_badge}",
                    "description": (
                        f"**What we did:** {target}\n"
                        f"**Result:** {auto_applied}\n\n"
                        f"Did this resolve the issue? Your feedback trains the agent to act faster next time."
                    ),
                    "color": _COLOR_INFO,
                    "footer": {"text": "Auto-applied — high confidence fix"},
                }
            )
        else:
            description = _ACTION_DESCRIPTIONS.get(action_name, "Executes a cluster change.")
            embeds.append(
                {
                    "title": f"🔧 Ready to fix: {action_name}{rag_badge}",
                    "description": (
                        f"**What:** {target}\n"
                        f"**Effect:** {description}\n\n"
                        f"Click **Fix it now** to apply. The agent will execute this automatically."
                    ),
                    "color": _COLOR_ACTION,
                    "footer": {"text": "Requires your approval — click Fix it now below"},
                }
            )
    elif next_steps:
        # No automated action — show plain-English next steps
        next_truncated = next_steps[:1000] + ("…" if len(next_steps) > 1000 else "")
        embeds.append(
            {
                "title": f"👉 What to do next{rag_badge}",
                "description": next_truncated,
                "color": 0xFFA500,  # orange — attention needed
                "footer": {"text": "Manual action required — agent cannot fix this automatically"},
            }
        )

    return embeds


def _build_components(
    proposed_action: Optional[dict],
    investigation_id: Optional[str],
    auto_applied: Optional[str] = None,
) -> list[dict]:
    if not investigation_id:
        return []

    if auto_applied:
        # Fix already executed — feedback buttons only (no Apply Fix)
        buttons = [
            {
                "type": 2,
                "style": _STYLE_SUCCESS,
                "label": "Issue resolved",
                "emoji": {"name": "✅"},
                "custom_id": f"approve:{investigation_id}",
            },
            {
                "type": 2,
                "style": _STYLE_DANGER,
                "label": "Still broken",
                "emoji": {"name": "❌"},
                "custom_id": f"reject:{investigation_id}",
            },
        ]
    else:
        buttons = [
            {
                "type": 2,
                "style": _STYLE_SUCCESS,
                "label": "Looks right",
                "emoji": {"name": "✅"},
                "custom_id": f"approve:{investigation_id}",
            },
            {
                "type": 2,
                "style": _STYLE_DANGER,
                "label": "Wrong diagnosis",
                "emoji": {"name": "❌"},
                "custom_id": f"reject:{investigation_id}",
            },
        ]
        if proposed_action and proposed_action.get("action") not in (None, "none", "unknown"):
            buttons.append({
                "type": 2,
                "style": _STYLE_PRIMARY,
                "label": "Fix it now",
                "emoji": {"name": "⚡"},
                "custom_id": f"apply:{investigation_id}",
            })

    return [{"type": 1, "components": buttons}]


async def _send_discord_bot(
    alert_name: str,
    namespace: str,
    pod: str,
    diagnosis: str,
    proposed_action: Optional[dict],
    investigation_id: Optional[str],
    auto_applied: Optional[str] = None,
    is_rag_hit: bool = False,
) -> Optional[str]:
    pod_str = f" • pod `{pod}`" if pod else ""
    recur = " 🔁" if is_rag_hit else ""
    content = f":rotating_light: **K8s Alert: {alert_name}**{recur} • namespace `{namespace}`{pod_str}"

    embeds = _build_embeds(alert_name, namespace, pod, diagnosis, proposed_action, auto_applied, is_rag_hit)
    components = _build_components(proposed_action, investigation_id, auto_applied)

    payload = {"content": content, "embeds": embeds, "components": components}
    headers = {
        "Authorization": f"Bot {settings.discord_bot_token}",
        "Content-Type": "application/json",
    }
    url = f"https://discord.com/api/v10/channels/{settings.discord_channel_id}/messages"

    async with httpx.AsyncClient(timeout=10.0) as http:
        resp = await http.post(url, json=payload, headers=headers)
        resp.raise_for_status()


async def _send_discord_webhook(
    alert_name: str,
    namespace: str,
    pod: str,
    diagnosis: str,
    proposed_action: Optional[dict],
) -> None:
    pod_str = f" • pod `{pod}`" if pod else ""
    content = f":rotating_light: **K8s Alert: {alert_name}** • namespace `{namespace}`{pod_str}"
    embeds = _build_embeds(alert_name, namespace, pod, diagnosis, proposed_action)

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
    investigation_id: Optional[str] = None,
    auto_applied: Optional[str] = None,
    is_rag_hit: bool = False,
) -> Optional[str]:
    if settings.discord_bot_token and settings.discord_channel_id:
        try:
            msg_id = await _send_discord_bot(
                alert_name, namespace, pod, diagnosis,
                proposed_action, investigation_id, auto_applied, is_rag_hit,
            )
            return msg_id
        except Exception as e:
            logger.error("Discord bot notification failed: %s — falling back to webhook", e)

    if settings.discord_webhook_url:
        try:
            await _send_discord_webhook(alert_name, namespace, pod, diagnosis, proposed_action)
            return
        except Exception as e:
            logger.error("Discord webhook notification failed: %s", e)

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
