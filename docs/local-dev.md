# Local Development Guide

## Prerequisites

- Python 3.12+
- Access to Ollama on GPU node (Tailscale must be up: `100.86.152.16`)
- kubeconfig at `/Users/filipcsupka/moje/infra/kubeconfig.yaml`

## Setup

```bash
cd /Users/filipcsupka/moje/k8s-ai-agent

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# .env is pre-filled with Tailscale IP + kubeconfig path
```

## Run

```bash
uvicorn app.main:app --reload --port 8000
```

```bash
curl http://localhost:8000/health
# {"status":"ok","model":"qwen3:8b","auto_apply":true}
```

## Test Endpoints

### Manual investigation
```bash
curl -X POST http://localhost:8000/investigate \
  -H 'Content-Type: application/json' \
  -d '{
    "alertname": "KubePodCrashLooping",
    "namespace": "ai-chat",
    "pod": "rag-api-xxx",
    "severity": "critical"
  }'
```

Watch terminal for tool calls + diagnosis + proposed action.

### Fake AlertManager payload
```bash
curl -X POST http://localhost:8000/alert \
  -H 'Content-Type: application/json' \
  -d '{
    "version": "4",
    "status": "firing",
    "alerts": [{
      "status": "firing",
      "labels": {
        "alertname": "KubePodCrashLooping",
        "namespace": "ai-chat",
        "pod": "rag-api-xxx",
        "severity": "critical"
      },
      "annotations": {
        "summary": "Pod is crash looping"
      }
    }]
  }'
```

### Apply a proposed fix (Phase 3)
```bash
# Restart a pod (human approval gate)
curl -X POST http://localhost:8000/apply \
  -H 'Content-Type: application/json' \
  -d '{"action":"restart_pod","namespace":"ai-chat","pod_name":"rag-api-xxx-yyy"}'

# Scale deployment
curl -X POST http://localhost:8000/apply \
  -H 'Content-Type: application/json' \
  -d '{"action":"scale_deployment","namespace":"ai-chat","name":"rag-api","replicas":2}'

# Patch memory limit (OOMKilled fix)
curl -X POST http://localhost:8000/apply \
  -H 'Content-Type: application/json' \
  -d '{"action":"patch_deployment_memory","namespace":"ai-chat","name":"rag-api","container":"rag-api","memory_limit":"512Mi"}'
```

Requires `ENABLE_AUTO_APPLY=true` in `.env`. Returns 403 otherwise.

### Run ingest manually (bootstrap ChromaDB)
```bash
INVESTIGATIONS_DIR=/data/investigations \
RUNBOOKS_DIR=./runbooks \
CHROMA_HOST=chromadb.ai-chat.svc.cluster.local \
OLLAMA_BASE_URL=http://100.86.152.16:11434 \
python -m scripts.ingest_to_chroma
```

## Trigger ingest in cluster

```bash
# Create a one-off job from the cronjob
kubectl create job -n ai-agent --from=cronjob/k8s-ai-agent-ingest ingest-manual-$(date +%s)

# Watch its logs
kubectl logs -n ai-agent -l job-name=ingest-manual-... -f
```

## Verify ChromaDB contents

```bash
# List collections
kubectl exec -n ai-chat deploy/chromadb -- \
  python3 -c "
import sqlite3
conn = sqlite3.connect('/chroma/chroma/chroma.sqlite3')
rows = conn.execute('SELECT name FROM collections').fetchall()
print(rows)
"

# Count documents in k8s-runbooks
kubectl exec -n ai-chat deploy/chromadb -- \
  python3 -c "
import sqlite3
conn = sqlite3.connect('/chroma/chroma/chroma.sqlite3')
cid = conn.execute(\"SELECT id FROM collections WHERE name='k8s-runbooks'\").fetchone()[0]
count = conn.execute('SELECT COUNT(*) FROM embeddings WHERE segment_id IN (SELECT id FROM segments WHERE collection=?)', (cid,)).fetchone()[0]
print('k8s-runbooks docs:', count)
"
```

Expected: 3 after first successful ingest (crashloop, oomkilled, imagepull runbooks).

## Approve a human investigation JSON

```bash
# Path: /data/investigations/<timestamp>-<alert>-<namespace>.json
# Edit the file:
{
  "reviewed": true,
  "correct": true,
  "notes": "Root cause was OOM — memory limit was too low"
}
# Next CronJob run picks it up and ingests into ChromaDB
```

## Common Issues

### `kubernetes.config.ConfigException`
→ Check `KUBECONFIG` in `.env` points to valid file

### `httpx.ConnectError: Failed to connect to 100.86.152.16:11434`
→ Tailscale not connected, or Ollama not running
→ `ssh ja@100.86.152.16 systemctl status ollama`

### `ModuleNotFoundError`
→ `pip install -r requirements.txt` in venv

### Agent times out
→ Ollama loading model (first call 30-60s cold start)
→ `curl http://100.86.152.16:11434/api/tags`

### Ingest 500 from ChromaDB
Two root causes seen in production:

**1. Corrupt collection in SQLite (from version migration)**
If `k8s-runbooks` was created by a different chromadb version, its `config_json_str`
may be `'{}'` — 0.6.3 server can't parse it.
```bash
kubectl exec -n ai-chat deploy/chromadb -- python3 -c "
import sqlite3
conn = sqlite3.connect('/chroma/chroma/chroma.sqlite3')
# Check for bad configs
print(conn.execute('SELECT name, config_json_str FROM collections').fetchall())
# Delete corrupt collection
conn.execute(\"DELETE FROM collections WHERE name='k8s-runbooks'\")
conn.commit()
print('deleted', conn.total_changes, 'rows')
"
```
Then re-trigger ingest.

**2. Client/server version mismatch**
`chromadb>=0.6.0` resolves to 1.5.x which is incompatible with 0.6.3 server.
Always pin exact: `chromadb==0.6.3` in requirements.txt.
Server (`chromadb/chroma:0.6.3`), k8s-ai-agent, and rag-api must ALL be the same version.

### `ENABLE_AUTO_APPLY=false` → /apply returns 403
Set `ENABLE_AUTO_APPLY=true` in `.env` for local dev. In prod it's set via gitops `agent.yaml`.

## Ollama

```bash
ssh ja@100.86.152.16 ollama list   # check available models
ssh ja@100.86.152.16 ollama pull qwen3:8b   # pull if missing
```

## Docker (alternative)

```bash
docker-compose up --build
# Mounts kubeconfig read-only into container
```
