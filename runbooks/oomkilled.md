# OOMKilled — Out of Memory

## What Is It

Pod's container used more memory than its `limits.memory` → Linux kernel OOM killer killed the process.
Container state shows `reason: OOMKilled`.

## How to Confirm

```bash
kubectl describe pod <pod-name> -n <namespace>
# Look for:
# Last State: Terminated
#   Reason: OOMKilled
#   Exit Code: 137
```

Exit code 137 = killed by signal 9 (SIGKILL from OOM killer).

## Agent Tool Sequence

1. `describe_pod` → confirm `lastState.terminated.reason = OOMKilled`
2. `get_resource_usage` → compare actual memory usage vs limit
3. `get_pod_logs` → check if app logged "out of memory" or heap dump before crash

## Common Root Causes

| Cause | Signal | Fix |
|-------|--------|-----|
| Limit too low for workload | Usage close to limit | Increase `limits.memory` |
| Memory leak in application | Usage grows over time | Fix leak; temporary: add liveness probe to restart |
| Batch job spike | Usage spikes briefly | Increase limit or schedule at off-peak |
| JVM not respecting container limits | Java heap bigger than container | Add `-XX:MaxRAMPercentage=75` JVM flag |

## Fix Commands

```bash
# Check current limits
kubectl get pod <pod-name> -n <ns> -o jsonpath='{.spec.containers[*].resources}'

# Patch deployment limits (example: raise to 512Mi)
kubectl patch deployment <name> -n <ns> --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"512Mi"}]'

# Or edit directly
kubectl edit deployment <name> -n <ns>
```

## Prevention

- Always set both `requests` and `limits`
- Set `requests` = expected normal usage, `limits` = 2x requests (headroom)
- Add `metrics-server` + HPA for workloads with variable memory needs
- For JVM: `-XX:+UseContainerSupport -XX:MaxRAMPercentage=75`

## Risk Level

**Low** — increasing memory limit. No data loss. Pod restarts cleanly.
**Medium** — if node is already memory-pressured; new limit may cause eviction of other pods.
Check `get_node_status` first.
