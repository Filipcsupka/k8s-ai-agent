# k8s-ai-agent

Kubernetes L2 support AI agent. Receives alerts from AlertManager, investigates using k8s API tools, diagnoses root cause, and reports to Slack.

Runs in k3s cluster on the GPU node (same node as Ollama). Uses `qwen3:8b` via local Ollama — no external API calls, all data stays in the cluster.

---

## How It Works

```
AlertManager fires → POST /alert → agent wakes up
  → list_pods (namespace overview)
  → get_events (recent warnings)
  → describe_pod (container state, exit codes)
  → get_pod_logs (actual error messages)
  → Ollama (qwen3:8b) diagnoses root cause
  → Slack: diagnosis + recommended fix
```

The agent uses the **ReAct pattern** — the LLM decides which tools to call based on what it sees. It's not scripted. Different alerts → different investigation paths.

→ **Learn more**: [docs/concepts.md](docs/concepts.md)

---

## Phases

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Alert → read-only diagnosis → Slack report | **current** |
| 2 | Runbook RAG — ChromaDB knowledge base for better answers | planned |
| 3 | Approval gate — Slack button to auto-apply fixes | planned |

---

## Stack

| Component | Technology |
|-----------|-----------|
| LLM | `qwen3:8b` via local Ollama |
| Agent framework | LangGraph (`create_react_agent`) |
| k8s client | Python `kubernetes` SDK |
| API server | FastAPI |
| Notifications | Slack incoming webhook |
| Deployment | k3s, GPU node, ArgoCD |

---

## Quick Start

```bash
# 1. Clone and setup
cd /Users/filipcsupka/moje/k8s-ai-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# 2. Run locally
uvicorn app.main:app --reload

# 3. Test with a manual alert
curl -X POST http://localhost:8000/investigate \
  -H 'Content-Type: application/json' \
  -d '{"alertname":"CrashLoopBackOff","namespace":"ai-chat","pod":"rag-api-xxx"}'
```

Full setup guide: [docs/local-dev.md](docs/local-dev.md)

---

## Project Structure

```
k8s-ai-agent/
├── app/
│   ├── main.py          # FastAPI — /alert webhook, /investigate manual trigger
│   ├── agent.py         # LangGraph ReAct loop
│   ├── prompts.py       # System prompt — tune this to improve diagnosis quality
│   ├── notifier.py      # Slack notifier (stdout fallback if no webhook set)
│   ├── config.py        # Settings from env vars
│   └── tools/
│       ├── k8s.py       # Read-only k8s tools (Phase 1)
│       └── apply.py     # Write tools — gated, Phase 3 only
├── infra/
│   ├── namespace.yaml
│   ├── serviceaccount.yaml
│   ├── rbac.yaml        # ClusterRole: read-only (get/list/watch)
│   ├── deployment.yaml  # Pinned to GPU node
│   ├── service.yaml     # ClusterIP — internal only
│   └── alertmanager-route.yaml  # Snippet to merge into AlertManager config
├── runbooks/            # Knowledge base for Phase 2 RAG
│   ├── oomkilled.md
│   ├── crashloop.md
│   └── imagepull.md
├── docs/
│   ├── concepts.md      # AI agent theory — ReAct, tool calling, LangGraph
│   ├── architecture.md  # System design
│   └── local-dev.md     # Dev setup guide
├── Dockerfile
├── docker-compose.yml   # Local dev with kubeconfig mount
└── .env.example
```

---

## Deploy to k3s

```bash
# Create namespace + RBAC
kubectl apply -f infra/namespace.yaml
kubectl apply -f infra/serviceaccount.yaml
kubectl apply -f infra/rbac.yaml

# Optional: Slack secret
kubectl -n ai-agent create secret generic k8s-ai-agent-secrets \
  --from-literal=slack-webhook-url=https://hooks.slack.com/services/...

# Deploy
kubectl apply -f infra/deployment.yaml
kubectl apply -f infra/service.yaml

# Check
kubectl -n ai-agent get pods
kubectl -n ai-agent logs -f deploy/k8s-ai-agent
```

---

## AlertManager Integration

Merge the route + receiver snippet from [infra/alertmanager-route.yaml](infra/alertmanager-route.yaml) into your existing AlertManager config in the `monitoring` namespace.

The agent endpoint: `http://k8s-ai-agent.ai-agent.svc.cluster.local/alert`

---

## Tuning the Agent

Most quality improvements come from editing the system prompt in `app/prompts.py`:

- Agent calls wrong tools → adjust the "Tool usage strategy" section
- Diagnosis too vague → add "Be specific: name the pod, container, exact error"
- Missing a failure pattern → add it to the strategy section
- Output format wrong → edit the "Output format" section

After editing prompt: restart the agent and re-test. No code changes needed.

---

## Adding Runbooks (Phase 2 prep)

Add markdown files to `runbooks/`. Format:
- `## What Is It` — description
- `## How to Confirm` — kubectl commands to verify
- `## Agent Tool Sequence` — which tools to call and in what order
- `## Common Root Causes` — table of symptoms → causes → fixes
- `## Fix Commands` — exact commands
- `## Risk Level` — low/medium/high with explanation

In Phase 2, runbooks will be ingested into ChromaDB and the agent will RAG over them before investigating — giving it institutional knowledge beyond what the LLM was trained on.

---

## Related Projects

- [`ai-chat`](../ai-chat) — RAG chatbot on same GPU node (shares Ollama + ChromaDB)
- [`infra`](../infra) — k3s cluster terraform + ArgoCD

## Docs

- [AI Agent Concepts](docs/concepts.md) — learn how agents work
- [Architecture](docs/architecture.md) — system design
- [Local Dev](docs/local-dev.md) — run it locally
