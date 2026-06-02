# AI Agent Concepts — Learning Guide

This doc explains the core concepts behind how this agent works.
Read this before touching `app/agent.py`.

---

## 1. What Is an AI Agent?

A regular LLM call is:
```
User prompt → LLM → Answer
```

An **agent** is:
```
Problem → LLM → "I need more info, call tool X" → Tool runs → Result back to LLM → LLM → "Call tool Y" → ... → Final Answer
```

The LLM **decides** what actions to take. It's not scripted. Given a pod name and an alert, it might:
1. Call `list_pods` to see the namespace state
2. Call `get_events` to see recent warnings
3. Call `describe_pod` to check container state
4. Call `get_pod_logs` to read the actual error
5. Return a diagnosis

The key insight: **the LLM chooses the tool order based on what it sees**. Different alerts → different tool sequences.

---

## 2. The ReAct Pattern

**ReAct = Reason + Act** (from a 2022 paper by Google/Princeton).

Each step the agent does:
1. **Reason** — "Based on what I know so far, I should check X next"
2. **Act** — calls a tool
3. **Observe** — reads the tool output
4. Back to step 1, repeat until it has enough info to answer

This is just the LLM completing text. It writes something like:
```
Thought: The pod is in CrashLoopBackOff. I should check previous logs.
Action: get_previous_pod_logs(namespace="ai-chat", pod_name="rag-api-xxx")
Observation: ERROR: Cannot connect to ChromaDB at localhost:8001...
Thought: Root cause found. ChromaDB service is misconfigured.
Final Answer: ## Diagnosis...
```

The framework (LangGraph) handles parsing the tool calls and feeding results back.

---

## 3. Tool Calling

Modern LLMs (including qwen3) support **structured tool calling**:

```python
@tool
def get_pod_logs(namespace: str, pod_name: str, lines: int = 100) -> str:
    """Get logs from a pod. Use to diagnose crashes."""
    # ... implementation
```

The `@tool` decorator + docstring tells the LLM:
- What the function does (from the docstring)
- What parameters it takes (from type hints)
- What it returns (from return type hint)

When you call `llm.bind_tools([get_pod_logs, ...])`, the LLM receives a JSON schema description of each tool. It can then "call" a tool by generating structured JSON. The framework executes it and returns the result.

**Critical**: The docstring IS the tool description the LLM reads. Write it for the LLM, not for humans. Explain WHEN to use it, not what it does.

---

## 4. LangGraph's `create_react_agent`

LangGraph manages the loop for you:

```python
agent = create_react_agent(llm, tools)
result = await agent.ainvoke({"messages": [SystemMessage(...), HumanMessage(...)]})
```

Internally it's a graph with two nodes:
```
START → agent_node → (tool calls?) → tool_node → agent_node → ... → END
```

- `agent_node`: calls the LLM, gets response
- `tool_node`: if LLM wants tools, executes them, returns results as ToolMessages
- Loop continues until LLM produces a response with no tool calls

The `messages` key holds the full conversation history (including all tool call results). This is how the LLM "remembers" what it already checked.

---

## 5. Why Ollama Instead of OpenAI?

| | Ollama (local) | OpenAI/Claude API |
|---|---|---|
| Cost | Free | Per token |
| Privacy | All data stays local | Sent to external |
| Latency | Low (same node) | Network dependent |
| Capability | Good with qwen3:8b | Better with GPT-4/Claude |
| Tool calling | Supported by qwen3 | Excellent |

For ops/infra data (logs, configs), local is strongly preferred — you don't want sensitive cluster data leaving the network.

As a future upgrade path: swap `ChatOllama` for `ChatAnthropic` or `ChatOpenAI` in `agent.py` — everything else stays the same (LangChain's abstraction).

---

## 6. The System Prompt — Most Important Tuning Knob

The system prompt in `app/prompts.py` is where you control agent behavior:

- **Tool usage strategy** — which tool to call first, in what situation
- **Output format** — structured diagnosis format
- **Investigation depth** — when to stop calling tools
- **Tone** — terse vs verbose

If the agent gives bad diagnoses, the first fix is almost always improving the system prompt. Common issues:
- Agent calls too many tools → add "Stop when root cause is clear"
- Agent gives vague answers → add "Be specific: name the pod, container, exact error"
- Agent misses OOM → add to strategy: "For OOMKilled: always check resource usage vs limits"

This is called **prompt engineering** and it's iterative — test → observe → adjust.

---

## 7. State Management in LangGraph

The agent state is just a list of messages:

```python
{"messages": [
    SystemMessage(content="You are a k8s expert..."),
    HumanMessage(content="Alert: CrashLoopBackOff in ai-chat/rag-api"),
    AIMessage(content="", tool_calls=[{"name": "list_pods", "args": {...}}]),
    ToolMessage(content="rag-api-xxx: phase=Running ready=0/1 restarts=5"),
    AIMessage(content="", tool_calls=[{"name": "get_pod_logs", ...}]),
    ToolMessage(content="ERROR: ChromaDB connection refused"),
    AIMessage(content="## Diagnosis\nChromaDB is unreachable..."),
]}
```

Each tool call and result is preserved. The LLM sees the full history and builds on it.

---

## 8. Async and Concurrency

The agent uses `asyncio`. FastAPI is async-native. The semaphore in `main.py` limits concurrent investigations:

```python
_semaphore = asyncio.Semaphore(3)  # max 3 simultaneous investigations
```

Why? Ollama can only handle one or a few parallel inferences (8GB VRAM on RTX 2070). Without the semaphore, a burst of alerts would queue them all against Ollama simultaneously, causing timeouts.

Each alert investigation is run as a background task (`background_tasks.add_task`), so the webhook endpoint returns immediately (AlertManager expects fast response) while investigation happens asynchronously.

---

## 8. RAG — Retrieval-Augmented Generation

Without RAG, the agent starts every investigation from zero. With RAG it checks
institutional memory first — past diagnoses and runbooks — before touching the
cluster.

```
Alert arrives
  → search_past_diagnoses("KubePodCrashLooping ai-chat OOMKilled")
  → ChromaDB returns top-3 similar past diagnoses
  → If similarity > 50%: agent uses them as starting hypothesis
  → Verifies with 1-2 live tool calls instead of full investigation
  → Much faster, more accurate (uses what worked before)
```

### What's in the RAG DB

**Runbooks** (`runbooks/*.md`) — ingested unconditionally on every CronJob run.
These are the "textbook" knowledge: what each alert means, which tools to call,
common causes and their fixes.

**Past investigations** (`/data/investigations/*.json`) — ingested only when a
human marks `reviewed=true` AND `correct=true`. These are real incidents from
your cluster with confirmed diagnoses.

### How to grow the knowledge base

1. Alert fires → agent investigates → JSON saved to PVC
2. You review the JSON, set `reviewed=true, correct=true`
3. Next CronJob run (every 30 min) ingests it into ChromaDB
4. Future similar alerts hit this knowledge instantly

Add more runbooks in `runbooks/` as you encounter new alert types.
The format is free markdown — the LLM reads it as-is.

### ChromaDB version pinning (important!)

ChromaDB breaks across minor versions. Server, k8s-ai-agent client, and rag-api
client must ALL be the same exact version. Currently `chromadb==0.6.3`.

Never use `>=` — pip resolves to latest which will be incompatible.

---

## 9. Human-in-the-Loop Approval Gate (Phase 3)

The agent never applies fixes automatically. The flow is:

```
Agent diagnoses → proposes one ACTION → Slack shows curl command
  → Human reads diagnosis → decides to approve or ignore
  → Human copies curl command → POST /apply executes the fix
```

The `ACTION:` line in the agent output is parsed by `agent.py` and passed to
the notifier. Notifier formats it as a `kubectl exec` command in the Slack message.

The `/apply` endpoint is the gate — it only runs if `ENABLE_AUTO_APPLY=true`.
Since the service is ClusterIP-only (not public), only someone with cluster
access can call it.

**Supported actions:**
- `restart_pod` — deletes pod (controller recreates)
- `scale_deployment` — patches replica count
- `patch_deployment_memory` — updates memory limit (OOMKilled fix)

**Future (Phase 4):** Slack interactive buttons — click Approve/Deny directly in
Slack without copying a curl command. Requires a Slack app with interactive
components and a public endpoint (Tailscale Funnel or ingress).

---

## Further Reading

- [LangGraph docs](https://langchain-ai.github.io/langgraph/) — especially "ReAct agent" tutorial
- [ReAct paper](https://arxiv.org/abs/2210.03629) — the original paper (readable)
- [Ollama tool calling](https://ollama.com/blog/tool-support) — how qwen3 handles tools
- [kubernetes Python client](https://github.com/kubernetes-client/python/tree/master/kubernetes/docs) — API reference
