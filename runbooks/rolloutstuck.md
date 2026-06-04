# KubeDeploymentRolloutStuck

## What Is It

A deployment rollout has been in progress longer than the `progressDeadlineSeconds`
(default 600s). The new ReplicaSet pods are not becoming ready.

## How to Confirm

```bash
kubectl rollout status deployment/<name> -n <namespace>
# "error: deployment ... exceeded its progress deadline"

kubectl get deployment <name> -n <namespace> -o jsonpath='{.status.conditions}'
# Condition: Progressing=False, reason=ProgressDeadlineExceeded
```

## Agent Tool Sequence

1. `describe_deployment` → check conditions for `ProgressDeadlineExceeded`, replicas state
2. `list_pods` → find new ReplicaSet pods (newer ones with different hash), check their phase
3. `get_events` → scheduling errors, image pull errors, liveness probe failures
4. `describe_pod` → for a stuck new pod: container state, liveness/readiness probe status
5. `get_pod_logs` → if pod is running but not ready: app startup errors, health endpoint issues

## Common Root Causes

| Symptom | Root Cause | Fix |
|---------|------------|-----|
| New pod stuck in `Pending` | Insufficient resources or scheduling constraints | Check node capacity, taints, affinities |
| New pod `CrashLoopBackOff` | App broken in new version | Rollback deployment |
| New pod `Running` but readiness fails | Health endpoint not responding or wrong port | Fix liveness/readiness probe config or app |
| New pod `ImagePullBackOff` | Bad image tag or missing credentials | Fix image tag or image pull secret |
| `FailedCreate` event | ResourceQuota exceeded | Free up resources or increase quota |

## Fix Commands

```bash
# Rollback to last good version
kubectl rollout undo deployment/<name> -n <ns>

# Check rollout history
kubectl rollout history deployment/<name> -n <ns>

# Pause rollout while investigating
kubectl rollout pause deployment/<name> -n <ns>

# Resume after fix
kubectl rollout resume deployment/<name> -n <ns>

# Check readiness probe config
kubectl get deployment <name> -n <ns> -o jsonpath='{.spec.template.spec.containers[0].readinessProbe}'
```

## Proposed Actions

- `restart_pod` for a stuck new pod — may help if init container is wedged
- Rollback requires human decision — use `kubectl rollout undo`

## Risk Level

**Medium** — old pods still serving traffic (deployment strategy keeps them running),
but rollout is blocked. Rolling back is safe; fixing forward requires diagnosing new version.
