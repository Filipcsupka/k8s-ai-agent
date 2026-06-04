# KubeDeploymentReplicasMismatch

## What Is It

Deployment's desired replica count does not match the number of ready replicas.
Pods may be pending, crashing, or failing to schedule.

## How to Confirm

```bash
kubectl get deployment <name> -n <namespace>
# READY column shows fewer replicas than DESIRED
```

## Agent Tool Sequence

1. `describe_deployment` → check desired vs ready vs unavailable replicas, conditions
2. `list_pods` → find which pods are not ready (Pending, CrashLoopBackOff, Error)
3. `get_events` → look for scheduling failures, image pull errors, resource quota issues
4. `describe_pod` → for any unhealthy pod, get container state and resource limits

## Common Root Causes

| Symptom | Root Cause | Fix |
|---------|------------|-----|
| Pods in `Pending` | Insufficient CPU/memory on nodes | Scale nodes or reduce resource requests |
| Pods in `Pending` with `Unschedulable` event | Node selector / taint mismatch | Check nodeSelector and tolerations |
| Pods in `CrashLoopBackOff` | Application error | See CrashLoopBackOff runbook |
| Pods in `ImagePullBackOff` | Bad image tag or registry | See ImagePullBackOff runbook |
| `FailedCreate` event | Resource quota exceeded | `kubectl get resourcequota -n <ns>` |
| `Progressing=False` condition | Deployment stuck mid-rollout | Check if new pods can start at all |

## Fix Commands

```bash
# Check deployment status
kubectl rollout status deployment/<name> -n <ns>

# Check pod events
kubectl describe pod <pod-name> -n <ns>

# Check resource quota
kubectl get resourcequota -n <ns>

# Rollback if bad rollout
kubectl rollout undo deployment/<name> -n <ns>

# Scale manually
kubectl scale deployment/<name> --replicas=<n> -n <ns>
```

## Proposed Actions

- `scale_deployment` — if the target replica count is correct but pods aren't scheduling, scaling to 0 and back can clear stuck state
- Rollback requires human decision — use `kubectl rollout undo`

## Risk Level

**Medium** — service is degraded (fewer replicas = less capacity). Fix action depends on root cause.
