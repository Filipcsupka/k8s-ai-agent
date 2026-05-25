# ImagePullBackOff / ErrImagePull

## What Is It

k8s cannot pull the container image from the registry.
`ErrImagePull` = first failure. `ImagePullBackOff` = repeated failure with backoff.

## How to Confirm

```bash
kubectl get pod <pod-name> -n <namespace>
# STATUS: ImagePullBackOff

kubectl describe pod <pod-name> -n <namespace>
# Events section shows the exact pull error
```

## Agent Tool Sequence

1. `get_events` filtered by pod name → look for pull error message (has exact registry + image + error)
2. `describe_pod` → confirm image name and tag

No need for `get_pod_logs` — container never started, so there are no logs.

## Common Root Causes

| Event Message | Root Cause | Fix |
|---------------|------------|-----|
| `not found` | Wrong image name or tag (typo, tag doesn't exist) | Fix image tag in deployment |
| `unauthorized` / `403` | Missing registry credentials | Add imagePullSecret |
| `timeout` / `no route to host` | Node can't reach registry | Check node network, DNS, registry down? |
| `manifest unknown` | Tag was deleted from registry | Push correct tag or use different tag |
| `toomanyrequests` | Docker Hub rate limit | Use GHCR or add credentials |

## Fix Commands

```bash
# See exact image name configured
kubectl get deployment <name> -n <ns> -o jsonpath='{.spec.template.spec.containers[*].image}'

# Check if imagePullSecrets are configured
kubectl get deployment <name> -n <ns> -o jsonpath='{.spec.template.spec.imagePullSecrets}'

# Create pull secret for GHCR (GitHub Container Registry)
kubectl create secret docker-registry ghcr-creds \
  --docker-server=ghcr.io \
  --docker-username=<github-username> \
  --docker-password=<github-pat> \
  -n <namespace>

# Attach pull secret to deployment
kubectl patch deployment <name> -n <ns> --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/imagePullSecrets","value":[{"name":"ghcr-creds"}]}]'

# Verify image exists (run locally)
docker manifest inspect ghcr.io/<user>/<image>:<tag>
```

## Risk Level

**Low** — pod is not running at all, so any fix attempt can only improve things.
**Medium** — changing imagePullSecrets affects all pods in the deployment.
