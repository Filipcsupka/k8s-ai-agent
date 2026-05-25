# Local Development Guide

## Prerequisites

- Python 3.12+
- Access to Ollama on GPU node (Tailscale must be up: `100.86.152.16`)
- kubeconfig at `/Users/filipcsupka/moje/infra/kubeconfig.yaml`

## Setup

```bash
cd /Users/filipcsupka/moje/k8s-ai-agent

# virtual env
python3 -m venv .venv
source .venv/bin/activate

# install deps
pip install -r requirements.txt

# copy env
cp .env.example .env
# .env is pre-filled with correct values for local dev (Tailscale IP, kubeconfig path)
```

## Run

```bash
uvicorn app.main:app --reload --port 8000
```

Health check:
```bash
curl http://localhost:8000/health
```

## Test Without AlertManager

Use the `/investigate` endpoint to trigger a real investigation manually:

```bash
# Test with a real pod from your cluster
curl -X POST http://localhost:8000/investigate \
  -H 'Content-Type: application/json' \
  -d '{
    "alertname": "PodCrashLoopBackOff",
    "namespace": "ai-chat",
    "pod": "rag-api-xxx-yyy",
    "severity": "critical",
    "summary": "Pod is crash looping"
  }'
```

Watch logs in the terminal — you'll see each tool call the agent makes and the final diagnosis.

## Test With Fake AlertManager Payload

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
        "summary": "Pod is crash looping",
        "description": "Pod ai-chat/rag-api-xxx is restarting 5 times per 10 minutes"
      }
    }]
  }'
```

## Docker (alternative)

```bash
docker-compose up --build
```

This mounts your kubeconfig read-only into the container.

## Checking What Ollama Model Is Available

```bash
ssh ja@100.86.152.16 ollama list
```

If `qwen3:8b` is not listed:
```bash
ssh ja@100.86.152.16 ollama pull qwen3:8b
```

## Debugging Agent Tool Calls

Set log level to DEBUG to see every tool call:
```bash
LOG_LEVEL=DEBUG uvicorn app.main:app --reload
```

Or add a breakpoint in `app/tools/k8s.py` to inspect what the agent is asking for.

## Common Local Dev Issues

**`kubernetes.config.ConfigException: Invalid kube-config file`**
→ Check `KUBECONFIG` in `.env` points to valid file

**`httpx.ConnectError: Failed to connect to 100.86.152.16:11434`**
→ Tailscale not connected, or Ollama not running on GPU node
→ Check: `ssh ja@100.86.152.16 systemctl status ollama`

**`ModuleNotFoundError: No module named 'langchain_ollama'`**
→ Run `pip install -r requirements.txt` in the venv

**Agent produces no output / times out**
→ Ollama may be loading the model (first call is slow — 30-60s)
→ Try: `curl http://100.86.152.16:11434/api/tags` to check Ollama is responding
