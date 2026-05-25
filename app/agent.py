"""
LangGraph ReAct agent loop.

Flow:
  Alert payload → build investigation prompt → ReAct loop (think → tool → think → ...) → diagnosis

The agent uses ChatOllama (qwen3:8b) with tool calling.
LangGraph's create_react_agent manages the think/act loop automatically.
"""

import asyncio
import logging
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama
from langgraph.prebuilt import create_react_agent

from app.config import settings
from app.notifier import notify_slack
from app.prompts import SYSTEM_PROMPT
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

# All read-only tools available to the agent in Phase 1
TOOLS = [
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

    try:
        result = await asyncio.wait_for(
            agent.ainvoke({"messages": messages}),
            timeout=settings.agent_timeout_seconds,
        )
        final = result["messages"][-1].content
        logger.info("Investigation complete: alert=%s", alert_name)
    except asyncio.TimeoutError:
        final = (
            f"Investigation timed out after {settings.agent_timeout_seconds}s. "
            "Manual investigation required."
        )
        logger.warning("Agent timed out for alert=%s", alert_name)
    except Exception as e:
        final = f"Agent error: {e}. Manual investigation required."
        logger.exception("Agent error for alert=%s", alert_name)

    await notify_slack(
        alert_name=alert_name,
        namespace=namespace,
        pod=pod,
        diagnosis=final,
    )
