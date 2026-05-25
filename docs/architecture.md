# Architecture

## System Overview

```
┌──────────────────────────────────────────────────────────┐
│                   GPU Node (k3sgpu)                       │
│                   Tailscale: 100.86.152.16                │
│                                                           │
│  ┌─────────────┐     ┌─────────────────────────────────┐ │
│  │   Ollama    │◄────│         k8s-ai-agent            │ │
│  │  qwen3:8b   │     │   namespace: ai-agent           │ │
│  │  port 11434 │     │                                 │ │
│  └─────────────┘     │   FastAPI  ←── AlertManager     │ │
│                      │      │                          │ │
│  ┌─────────────┐     │   LangGraph ReAct loop          │ │
│  │  ChromaDB   │◄────│      │                          │ │
│  │  (future    │     │   k8s Tools                     │ │
│  │  runbooks)  │     │   └─ list_pods                  │ │
│  └─────────────┘     │   └─ get_events                 │ │
│                      │   └─ describe_pod               │ │
└──────────────────────│   └─ get_pod_logs               │─┘
                       │   └─ get_resource_usage         │
                       └──────────────┬──────────────────┘
                                      │
                          ┌───────────┼───────────┐
                          ▼           ▼           ▼
                     k8s API      Slack         stdout
                    (read-only)  webhook        (fallback)
```

## Components

### FastAPI (`app/main.py`)
- Receives AlertManager webhook at `POST /alert`
- Parses AlertManager v4 payload
- Fires background task per alert
- `POST /investigate` — manual trigger for testing

### Agent Loop (`app/agent.py`)
- `create_react_agent(llm, tools)` — LangGraph manages the think/act loop
- `asyncio.wait_for` — 120s hard timeout per investigation
- `asyncio.Semaphore(3)` — max 3 concurrent investigations (protects Ollama)
- On completion, calls notifier

### Tools (`app/tools/k8s.py`)
- Pure read-only k8s API calls via Python `kubernetes` SDK
- In-cluster auth when `KUBERNETES_SERVICE_HOST` is set
- Kubeconfig file for local dev
- Each tool is a `@tool`-decorated function — LangGraph registers them automatically

### Notifier (`app/notifier.py`)
- Sends diagnosis to Slack incoming webhook
- Falls back to `stdout` if `SLACK_WEBHOOK_URL` is empty (good for early dev)

### Apply Gate (`app/tools/apply.py`)
- Phase 3 — write operations
- All functions check `ENABLE_AUTO_APPLY` flag
- Currently raises `PermissionError` if called (Phase 1)

## Data Flow

```
1. AlertManager fires → POST /alert
2. FastAPI parses payload, queues background task per alert
3. Agent builds investigation prompt from alert labels/annotations
4. LangGraph loop starts:
   a. LLM receives system prompt + investigation prompt
   b. LLM decides which tool to call
   c. Tool executes k8s API call
   d. Result returned to LLM as ToolMessage
   e. Repeat until LLM has enough to diagnose
5. LLM generates final diagnosis (## Diagnosis format)
6. Notifier sends to Slack or logs to stdout
```

## Kubernetes Topology

- **Namespace**: `ai-agent`
- **ServiceAccount**: `k8s-ai-agent` with ClusterRole `k8s-ai-agent-reader`
- **Node**: GPU node (pinned via `nodeSelector: accelerator: nvidia`)
  - Same node as Ollama → low-latency HTTP to Ollama
  - No GPU resource limit needed (agent is CPU-only; Ollama manages GPU separately)
- **Service**: ClusterIP only (internal — AlertManager reaches via DNS)

## Ollama Access

Ollama runs as a systemd service on the GPU node host (`OLLAMA_HOST=0.0.0.0:11434`).
It's exposed to the cluster via a headless k8s Service + Endpoints in `ai-chat` namespace.
The agent calls it at: `http://ollama.ai-chat.svc.cluster.local:11434`

## Phase Roadmap

| Phase | What | Status |
|-------|------|--------|
| 1 | Alert → Diagnose → Slack report | **current** |
| 2 | Runbook RAG (ChromaDB) | planned |
| 3 | Approval gate + auto-apply | planned |

## Secrets

```bash
# Create the secret before deploying
kubectl -n ai-agent create secret generic k8s-ai-agent-secrets \
  --from-literal=slack-webhook-url=https://hooks.slack.com/...
```

Secret is optional — if missing, agent logs to stdout.
