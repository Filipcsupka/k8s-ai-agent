# CrashLoopBackOff

## What Is It

Pod's container starts, crashes, restarts, crashes again — in a loop.
k8s applies exponential backoff between restarts (10s → 20s → 40s → ... → 5min max).

## How to Confirm

```bash
kubectl get pod <pod-name> -n <namespace>
# STATUS column shows: CrashLoopBackOff

kubectl describe pod <pod-name> -n <namespace>
# Shows restart count and last termination reason
```

## Agent Tool Sequence

1. `list_pods` → confirm restartCount is high
2. `describe_pod` → get `lastState.terminated` (exitCode + reason)
3. `get_previous_pod_logs` → logs from the crashed container (BEFORE it restarted)
4. `get_events` → look for OOM, scheduling, or pull errors

**Important**: use `get_previous_pod_logs` not `get_pod_logs` — current container may not have had time to log anything yet.

## Exit Codes

| Exit Code | Meaning | Common Cause |
|-----------|---------|--------------|
| 1 | Application error | Bug, misconfiguration |
| 137 | OOMKilled (SIGKILL) | Memory limit too low |
| 139 | Segfault (SIGSEGV) | Bug in native code |
| 143 | Graceful shutdown (SIGTERM) | Liveness probe killing it |

## Common Root Causes

| Symptom in logs | Root Cause | Fix |
|-----------------|------------|-----|
| `connection refused` to a dependency | Dependency not ready / wrong URL | Check env vars, check if dependency is running |
| `permission denied` on a file/dir | Wrong file permissions or missing volume | Check PVC, ConfigMap, Secret mounts |
| `cannot unmarshal` / `invalid config` | Bad env var or ConfigMap value | Check configuration |
| App exits immediately, no error | Missing required env var | Add env var or check startup command |
| Liveness probe fails | App starts but health endpoint not responding | Fix health endpoint or increase `initialDelaySeconds` |

## Fix Commands

```bash
# Get logs from the crashed (previous) container
kubectl logs <pod-name> -n <ns> --previous

# Describe pod for exit codes and reasons
kubectl describe pod <pod-name> -n <ns>

# Check env vars passed to container
kubectl exec <pod-name> -n <ns> -- env | sort

# Check if the service it depends on is running
kubectl get svc -n <ns>
kubectl get endpoints -n <ns>
```

## Risk Level

**Low** — diagnosing. CrashLoop means service is already down.
**Medium** — applying fix (restart deployment, change config) may cause brief downtime.
