# KubePodNotReady

## What Is It

One or more pods have been in a non-Ready state for longer than the alert threshold.
Pod is running but readiness probe failing, or stuck in Init/PodInitializing.

## How to Confirm

```bash
kubectl get pod <pod-name> -n <namespace>
# READY column shows 0/1 or similar, STATUS may be Running

kubectl describe pod <pod-name> -n <namespace>
# Conditions: Ready=False with reason
```

## Agent Tool Sequence

1. `describe_pod` → check Ready condition, container states, readiness probe config
2. `get_pod_logs` → app startup errors, dependency failures, config errors
3. `get_events` → readiness probe failures, liveness probe killing restarts

**Note**: use `get_pod_logs` (not previous) — pod is Running, just not Ready.

## Common Root Causes

| Symptom in describe | Root Cause | Fix |
|--------------------|------------|-----|
| `Readiness probe failed: HTTP probe failed with statuscode: 5xx` | App started but returning errors | Check app logs for startup error |
| `Readiness probe failed: connection refused` | App not listening on expected port | Check port config or app startup |
| `Init container not finished` | Init container stuck or failing | Check init container logs |
| `Containers with unready status` + `Back-off restarting failed container` | CrashLoopBackOff overlapping | See CrashLoopBackOff runbook |
| `OOMKilled` in last state | Memory limit too low | See OOMKilled runbook |
| Long startup time | App takes >initialDelaySeconds to be ready | Increase `initialDelaySeconds` in readiness probe |

## Fix Commands

```bash
# Check readiness probe result manually
kubectl exec <pod-name> -n <ns> -- curl -s http://localhost:<port>/health

# Check init container logs
kubectl logs <pod-name> -c <init-container-name> -n <ns>

# Describe to see probe config and failure reason
kubectl describe pod <pod-name> -n <ns>

# Check if app port is actually listening
kubectl exec <pod-name> -n <ns> -- ss -tlnp
```

## Proposed Actions

- `restart_pod` — if readiness probe stuck due to transient error (e.g. external dependency was temporarily down)
- Config/probe changes require human decision and deployment edit

## Risk Level

**Low** — pod is running, just not receiving traffic. Restarting is safe.
**Medium** — if readiness failure indicates app bug in current version, restart only delays the issue.
