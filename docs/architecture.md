# Architecture

## System Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                      GPU Node (k3sgpu)                            │
│                      Tailscale: 100.86.152.16                     │
│                                                                   │
│  ┌─────────────┐     ┌──────────────────────────────────────────┐ │
│  │   Ollama    │◄────│           k8s-ai-agent                   │ │
│  │  qwen3:8b   │     │       namespace: ai-agent                │ │
│  │  port 11434 │     │                                          │ │
│  └─────────────┘     │  FastAPI  ←── AlertManager               │ │
│                      │     │     ←── POST /investigate (manual)  │ │
│  ┌─────────────┐     │     │                                    │ │
│  │  ChromaDB   │◄────│  search_past_diagnoses  ← RAG (1st tool) │ │
│  │  k8s-runbooks│    │     │                                    │ │
│  │  collection │     │  LangGraph ReAct loop                   │ │
│  └─────────────┘     │     │                                    │ │
│                      │  k8s Tools (read-only)                   │ │
│                      │  └─ list_pods                            │ │
│                      │  └─ get_events                           │ │
│                      │  └─ describe_pod / describe_deployment   │ │
│                      │  └─ get_pod_logs / get_previous_pod_logs │ │
│                      │  └─ get_node_status / get_resource_usage │ │
│                      └────────────────┬─────────────────────────┘ │
└───────────────────────────────────────│───────────────────────────┘
                                        │
                            ┌───────────┴───────────┐
                            ▼                       ▼
                   Slack (diagnosis            stdout fallback
                   + proposed action          (no webhook)
                   + apply curl cmd)
                            │
                   Human reviews in Slack
                            │
                            ▼
                   POST /apply  (approval gate)
                            │
                            ▼
                   k8s write ops
                   (restart_pod / scale_deployment
                    / patch_deployment_memory)
```

## Components

### FastAPI (`app/main.py`)
- `POST /alert` — AlertManager webhook (v4 payload)
- `POST /investigate` — manual trigger for testing
- `POST /apply` — human approval gate; executes a proposed fix
  - Requires `ENABLE_AUTO_APPLY=true`
  - Actions: `restart_pod`, `scale_deployment`, `patch_deployment_memory`
  - Returns 403 if auto-apply disabled

### Agent Loop (`app/agent.py`)
- `create_react_agent(llm, tools)` — LangGraph manages the think/act loop
- `asyncio.wait_for` — 120s hard timeout per investigation
- `asyncio.Semaphore(3)` — max 3 concurrent investigations (protects Ollama)
- After diagnosis, regex-extracts `ACTION:` line from output → passes to notifier
- Saves investigation JSON to `/data/investigations/` PVC

### RAG (`app/tools/rag.py`)
- `search_past_diagnoses(query)` — always first tool in the agent loop
- Connects to ChromaDB `k8s-runbooks` collection
- Embeddings computed client-side via `OllamaEmbeddingFunction` (nomic-embed-text)
- Returns top-3 matches with cosine similarity > 50%
- Gracefully returns empty string if ChromaDB unreachable

### Tools (`app/tools/k8s.py`)
- Pure read-only k8s API calls via Python `kubernetes` SDK
- Fresh SA token read per call (fixes 401 on k3s projected token rotation)
- Each tool is a `@tool`-decorated function

### Apply Tools (`app/tools/apply.py`)
- Write operations: `restart_pod`, `scale_deployment`, `patch_deployment_memory`
- Same fresh-token auth pattern as k8s.py
- Gated by `ENABLE_AUTO_APPLY` env var
- Called by `/apply` endpoint, NOT by the agent directly

### Notifier (`app/notifier.py`)
- Sends diagnosis to Slack incoming webhook
- If `proposed_action` present: adds second Slack attachment with exact
  `kubectl exec ... curl /apply` command to copy-paste for one-click approval
- Falls back to stdout if `SLACK_WEBHOOK_URL` empty

### Ingest CronJob (`scripts/ingest_to_chroma.py`)
- Runs every 30 min in `ai-agent` namespace
- **Source 1**: `runbooks/*.md` — always ingested, no gate (authoritative)
- **Source 2**: `/data/investigations/*.json` — only if `reviewed=true AND correct=true`
- Embeddings computed client-side, raw vectors upserted to ChromaDB
- Idempotent (upsert by stable doc ID)

## Data Flow

```
1. AlertManager fires → POST /alert (or manual POST /investigate)
2. FastAPI queues background task per alert
3. Agent builds investigation prompt from alert labels/annotations
4. LangGraph loop starts:
   a. search_past_diagnoses(query) — RAG check FIRST
      → if high-similarity runbook found, use as hypothesis + verify with 1-2 tools
   b. LLM calls k8s read tools as needed (max 7 total)
   c. LLM generates final diagnosis in structured format
5. agent.py extracts ACTION: line from output (regex)
6. Notifier sends to Slack:
   - Attachment 1: full diagnosis
   - Attachment 2 (if action): proposed fix + kubectl exec curl command
7. Investigation JSON saved to PVC (reviewed=false, correct=null)
8. [Human reviews in Slack, approves by running the curl command]
9. POST /apply executes the fix → Slack confirms
```

## Approval Gate Flow (Phase 3)

```
Agent proposes:
  ACTION: restart_pod namespace=ai-chat pod_name=rag-api-xxx-yyy

Slack shows:
  kubectl exec -n ai-agent deploy/k8s-ai-agent -- \
    curl -s -X POST http://localhost:8000/apply \
         -H 'Content-Type: application/json' \
         -d '{"action":"restart_pod","namespace":"ai-chat","pod_name":"rag-api-xxx-yyy"}'

Human runs command → agent deletes pod → controller recreates it
```

## RAG Feedback Loop

```
Alert fires
  → Agent investigates (uses runbooks from ChromaDB)
  → Diagnosis saved to /data/investigations/ as JSON
  → Human marks: reviewed=true, correct=true (or false)
  → CronJob picks it up → ingests into ChromaDB
  → Next similar alert → RAG finds this past diagnosis → faster resolution
```

## Kubernetes Topology

- **Namespace**: `ai-agent`
- **ServiceAccount**: `k8s-ai-agent` with ClusterRole `k8s-ai-agent-reader`
  - Read: pods, logs, events, nodes, namespaces, deployments, replicasets, metrics
  - Write: pods/delete, deployments/patch (for /apply endpoint)
- **Node**: GPU node (pinned via `nodeSelector: accelerator: nvidia`)
- **Service**: ClusterIP only (internal — AlertManager reaches via DNS)
- **PVC**: `agent-investigations` at `/data/investigations`

## ChromaDB

Shared with `ai-chat` namespace (same ChromaDB instance as rag-api).

- **Host**: `chromadb.ai-chat.svc.cluster.local:8000`
- **Collection**: `k8s-runbooks` (separate from `filip_knowledge` used by rag-api)
- **Version**: must be pinned exactly — client and server must match
  - Current: `chromadb==0.6.3` (server + k8s-ai-agent client + rag-api client)
  - Do NOT use `>=` — pip resolves to latest which breaks against the server

## Secrets

```bash
kubectl -n ai-agent create secret generic k8s-ai-agent-secrets \
  --from-literal=slack-webhook-url=https://hooks.slack.com/...
```

Secret is optional — if missing, agent logs to stdout.

## Phase Roadmap

| Phase | What | Status |
|-------|------|--------|
| 1 | Alert → read-only diagnosis → Slack | live |
| 2 | Runbook RAG (ChromaDB) | **live** |
| 3 | Human approval gate → /apply fixes | **live** |
| 4 | Slack interactive buttons (Approve/Deny in Slack) | planned |
