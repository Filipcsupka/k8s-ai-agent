"""
LangGraph ReAct agent loop.

Flow:
  Alert payload → build investigation prompt → ReAct loop (think → tool → think → ...) → diagnosis

The agent uses ChatOllama (qwen3:8b) with tool calling.
LangGraph's create_react_agent manages the think/act loop automatically.
"""

import asyncio
import logging
import re
import time
from typing import Optional
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama
from langgraph.prebuilt import create_react_agent

from app.collector import save_investigation
from app.config import settings
from app.notifier import notify_slack
from app.prompts import SYSTEM_PROMPT
from app.tools.rag import lookup_runbook, search_past_diagnoses, check_high_similarity_match
from app.tools.k8s import (
    get_pod_logs,
    get_previous_pod_logs,
    get_events,
    describe_pod,
    describe_deployment,
    list_pods,
    get_node_status,
    get_resource_usage,
)

logger = logging.getLogger(__name__)

_ACTION_RE = re.compile(r"^ACTION:\s*(\S+)\s*(.*?)\s*$", re.MULTILINE)
_CONFIDENCE_RE = re.compile(r"^CONFIDENCE:\s*(high|medium|low)\s*$", re.MULTILINE | re.IGNORECASE)


def _extract_proposed_action(text: str) -> Optional[dict]:
    """Parse 'ACTION: <name> key=val key=val' from agent output. Returns None if none/invalid."""
    m = _ACTION_RE.search(text)
    if not m:
        return None
    action = m.group(1)
    if action == "none":
        return None
    params_str = m.group(2)
    try:
        params = dict(p.split("=", 1) for p in params_str.split() if "=" in p)
    except Exception:
        return None
    return {"action": action, **params}


def _extract_confidence(text: str) -> str:
    """Parse 'CONFIDENCE: high/medium/low' from agent output. Defaults to 'medium'."""
    m = _CONFIDENCE_RE.search(text)
    return m.group(1).lower() if m else "medium"


async def _maybe_auto_apply(proposed_action: dict, confidence: str) -> Optional[str]:
    """
    Execute the fix without a human button click when:
      - ENABLE_AUTO_APPLY=true
      - confidence matches AUTO_APPLY_CONFIDENCE_THRESHOLD (default: high)
      - action is in AUTO_APPLY_ACTIONS whitelist (default: restart_pod)
    Returns result string on success, None if not auto-applied.
    """
    if not settings.enable_auto_apply:
        return None
    if confidence.lower() != settings.auto_apply_confidence_threshold.lower():
        return None
    whitelist = {a.strip() for a in settings.auto_apply_actions.split(",") if a.strip()}
    action_name = proposed_action.get("action", "")
    if action_name not in whitelist:
        return None

    from app.tools import apply as apply_tools  # late import avoids circular dep
    try:
        if action_name == "restart_pod":
            ns = proposed_action.get("namespace", "")
            pod = proposed_action.get("pod_name", "")
            if not ns or not pod:
                logger.warning("Auto-apply restart_pod: missing namespace or pod_name in %s", proposed_action)
                return None
            result = apply_tools.restart_pod(namespace=ns, pod_name=pod)
            logger.info("Auto-applied restart_pod %s/%s: %s", ns, pod, result)
            return result
        elif action_name == "patch_deployment_memory":
            ns = proposed_action.get("namespace", "")
            name = proposed_action.get("name", "")
            container = proposed_action.get("container", "")
            memory_limit = proposed_action.get("memory_limit", "")
            if not all([ns, name, container, memory_limit]):
                logger.warning("Auto-apply patch_deployment_memory: missing fields in %s", proposed_action)
                return None
            result = apply_tools.patch_deployment_memory(
                namespace=ns, name=name, container=container, memory_limit=memory_limit
            )
            logger.info("Auto-applied patch_deployment_memory %s/%s: %s", ns, name, result)
            return result
        else:
            logger.warning("Auto-apply: unhandled whitelisted action %s", action_name)
            return None
    except Exception as e:
        logger.error("Auto-apply failed for %s: %s", action_name, e)
        return None


# lookup_runbook first (direct metadata, no threshold), then past investigations, then live k8s
TOOLS = [
    lookup_runbook,
    search_past_diagnoses,
    list_pods,
    get_events,
    describe_pod,
    describe_deployment,
    get_pod_logs,
    get_previous_pod_logs,
    get_node_status,
    get_resource_usage,
]

llm = ChatOllama(
    model=settings.ollama_model,
    base_url=settings.ollama_base_url,
    temperature=0,  # deterministic — we want consistent diagnosis, not creative answers
)

# create_react_agent builds the think→tool→think loop automatically
# SystemMessage prepended to every invocation via messages list
agent = create_react_agent(llm, TOOLS)


def _build_prompt(alert: dict) -> str:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})

    alert_name = labels.get("alertname", "UnknownAlert")
    namespace = labels.get("namespace", "default")
    pod = labels.get("pod", "")
    severity = labels.get("severity", "unknown")
    summary = annotations.get("summary", "")
    description = annotations.get("description", "")

    lines = [
        f"Alert: {alert_name}",
        f"Severity: {severity}",
        f"Namespace: {namespace}",
    ]
    if pod:
        lines.append(f"Pod: {pod}")
    if summary:
        lines.append(f"Summary: {summary}")
    if description:
        lines.append(f"Description: {description}")
    lines.append(f"All labels: {labels}")

    lines.append("")
    lines.append("Investigate this alert using your tools. Follow your investigation strategy.")
    lines.append("Provide a diagnosis using the required output format.")

    return "\n".join(lines)


async def run_agent(alert: dict) -> None:
    labels = alert.get("labels", {})
    alert_name = labels.get("alertname", "UnknownAlert")
    namespace = labels.get("namespace", "default")
    pod = labels.get("pod", "")

    logger.info("Starting investigation: alert=%s namespace=%s pod=%s", alert_name, namespace, pod)

    prompt = _build_prompt(alert)
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ]

    tool_calls_used: list[str] = []
    t_start = time.monotonic()

    # Short-circuit: if a very similar past investigation exists (≥85% match),
    # skip LangGraph entirely and serve the cached diagnosis.
    rag_hit = check_high_similarity_match(alert_name, namespace)
    if rag_hit:
        cached_diagnosis, similarity = rag_hit
        logger.info(
            "RAG short-circuit: %s/%s → %.1f%% match — skipping agent",
            alert_name, namespace, similarity,
        )
        final = (
            f"*Resolved from past investigation ({similarity}% similarity match — no live tool calls needed)*\n\n"
            + cached_diagnosis
        )
        tool_calls_used = ["check_high_similarity_match"]
        duration = time.monotonic() - t_start
        proposed_action = _extract_proposed_action(final)
        confidence = _extract_confidence(final)
        auto_applied = await _maybe_auto_apply(proposed_action, confidence) if proposed_action else None
        inv_id = save_investigation(alert, final, duration, tool_calls_used, proposed_action=proposed_action)
        await notify_slack(
            alert_name=alert_name,
            namespace=namespace,
            pod=pod,
            diagnosis=final,
            proposed_action=proposed_action,
            investigation_id=inv_id,
            auto_applied=auto_applied,
        )
        return

    invoke_kwargs: dict = {}

    try:
        result = await asyncio.wait_for(
            agent.ainvoke({"messages": messages}, config=invoke_kwargs),
            timeout=settings.agent_timeout_seconds,
        )
        final = result["messages"][-1].content
        # extract tool names from all AIMessage tool_calls in the conversation
        for msg in result["messages"]:
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                    if name:
                        tool_calls_used.append(name)
        logger.info("Investigation complete: alert=%s tools=%s", alert_name, tool_calls_used)
    except asyncio.TimeoutError:
        final = (
            f"Investigation timed out after {settings.agent_timeout_seconds}s. "
            "Manual investigation required."
        )
        logger.warning("Agent timed out for alert=%s", alert_name)
    except Exception as e:
        err = str(e)
        if "ConnectError" in type(e).__name__ or "Connection" in err:
            final = f"Cannot reach Ollama at {settings.ollama_base_url}. Check Ollama service is running."
            logger.error("Ollama unreachable for alert=%s: %s", alert_name, err)
        else:
            final = f"Agent error: {err}. Manual investigation required."
            logger.exception("Agent error for alert=%s", alert_name)

    duration = time.monotonic() - t_start

    proposed_action = _extract_proposed_action(final)
    confidence = _extract_confidence(final)
    if proposed_action:
        logger.info("Proposed action: %s (confidence: %s)", proposed_action, confidence)

    auto_applied = await _maybe_auto_apply(proposed_action, confidence) if proposed_action else None

    inv_id = save_investigation(alert, final, duration, tool_calls_used, proposed_action=proposed_action)

    await notify_slack(
        alert_name=alert_name,
        namespace=namespace,
        pod=pod,
        diagnosis=final,
        proposed_action=proposed_action,
        investigation_id=inv_id,
        auto_applied=auto_applied,
    )
