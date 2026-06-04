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
        save_investigation(alert, final, duration, tool_calls_used)
        await notify_slack(
            alert_name=alert_name,
            namespace=namespace,
            pod=pod,
            diagnosis=final,
            proposed_action=proposed_action,
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
    if proposed_action:
        logger.info("Proposed action: %s", proposed_action)

    save_investigation(alert, final, duration, tool_calls_used)

    await notify_slack(
        alert_name=alert_name,
        namespace=namespace,
        pod=pod,
        diagnosis=final,
        proposed_action=proposed_action,
    )
