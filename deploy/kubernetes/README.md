# Running sandboxes on Kubernetes (agent-sandbox)

`RUNTIME=kubernetes` runs each sandbox as a pod managed by
[kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox)
instead of a local Docker container. Compared with the docker backend this gains
multi-node scheduling, pause/resume that keeps a persistent volume, and optional
gVisor / Kata isolation. Compared with the hosted remote runtime it is
self-hostable on any conformant cluster, from kind to GKE.

Each sandbox is a `SandboxClaim`; the controller turns that into a `Sandbox` and a
pod running the agent server. Nothing is stored in the app server: the cluster is
the source of truth, exactly as the docker backend treats the docker daemon.

## Prerequisites

1. A cluster with the agent-sandbox controller and extensions installed:

   ```bash
   export VERSION=v0.5.2
   kubectl apply -f "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${VERSION}/sandbox-with-extensions.yaml"
   kubectl -n agent-sandbox-system rollout status deploy --timeout=180s
   ```

2. A bootstrap Secret shared by the app server and dormant Agent Server pods:

   ```bash
   kubectl create secret generic openhands-agent-server-bootstrap \
     --from-literal=secret-key="$(openssl rand -base64 32)"
   ```

   Expose the same value to the app-server container as `OH_SECRET_KEY`.

3. The template, pool, and RBAC from this directory:

   ```bash
   kubectl apply -f sandboxtemplate.yaml
   kubectl apply -f sandboxwarmpool.yaml
   kubectl apply -f rbac.yaml        # only needed when the app server runs in-cluster
   ```

## Configuration

```bash
export RUNTIME=kubernetes           # or k8s
```

Everything else has a default; override through config as needed:

| Setting | Default | Purpose |
|---|---|---|
| `namespace` | `default` | Namespace holding the pool and sandboxes |
| `warm_pool` | unset | Pool to claim from. When unset the sandbox spec id is used, so several pools can be offered as runtimes |
| `sandbox_url_pattern` | `http://{sandbox_name}.{namespace}.svc.cluster.local:{port}` | How a sandbox is reached. Placeholders: `{sandbox_name}` (the Sandbox resource, which cluster DNS resolves), `{sandbox_id}` (app-level id), `{namespace}`, `{port}` |
| `webhook_base_url` | in-cluster service URL | This app server, as reachable *from* a sandbox pod |
| `max_num_sandboxes` | `10` | Concurrency cap; the oldest sandboxes are paused beyond it |
| `inject_session_key` | `true` | Give every sandbox its own session API key |
| `shutdown_after_seconds` | unset | TTL safety net; the controller deletes the claim afterwards |

Pod-level concerns live in the `SandboxTemplate` rather than here. That covers the
image, CPU and memory, `runtimeClassName`, network policy and volumes.

The default Linux image includes the official Perforce `p4` r25.2 client and
`p4-mcp-server` v2026.4.3126709. The build verifies both published artifacts
with pinned SHA256 values and exposes them as `/usr/local/bin/p4` and
`/usr/local/bin/p4-mcp-server`. Perforce credentials are supplied only through
conversation secrets; keep plaintext `P4PORT`, `P4USER`, and `P4PASSWD` out of
manifests. The redistribution notice is in
`containers/sandbox/PERFORCE-NOTICE.txt`.

Execution profiles are enforced at claim creation:

- The selected profile chooses a `SandboxWarmPool`, whose
  `sandboxTemplateRef` must match the profile's `template_name`.
- The template-owned `workspace` volume claim must match the profile's storage
  class and size. Claims do not override volume claims because agent-sandbox
  cold-starts any claim with `volumeClaimTemplates`.
- The container image, workspace mount, resource requests/limits, node selector,
  and tolerations are validated against the selected template before a claim is
  created.

The adopted Sandbox owns its workspace PVC. Agent-sandbox reattaches that PVC
when a paused sandbox resumes or a pod restarts, and garbage-collects it when the
Sandbox is deleted. Durable Agent Server state therefore lives under
`/workspace/.openhands` on Linux and `C:\workspace\.openhands` on Windows. This
preserves lifecycle semantics without a shared PVC, `subPathExpr`, or
claim-specific pod environment.

The global legacy startup safety limit is configured with
`OH_APP_CONVERSATION_SANDBOX_STARTUP_TIMEOUT` (120 seconds by default). Curated
profiles override that absolute ceiling with `startup_timeout_seconds`; the
bundled Linux profile uses 300 seconds and Windows uses 600 seconds. The
independent inactivity deadline is configured with
`OH_APP_CONVERSATION_SANDBOX_STARTUP_INACTIVITY_TIMEOUT` (120 seconds by
default). Authoritative Kubernetes progress refreshes only the inactivity
deadline; it never extends the profile's absolute ceiling. Node warm capacity
and image pre-pull remain the primary cold-start mitigation.

Conversation start tasks expose `sandbox_startup_progress` while status is
`WAITING_FOR_SANDBOX`. The object contains:

- `phase`: `WAITING_FOR_CAPACITY`, `PROVISIONING_NODE`, `SCHEDULING`,
  `PROVISIONING_STORAGE`, `PULLING_IMAGE`, `STARTING_CONTAINER`,
  `WAITING_FOR_AGENT_SERVER`, `READY`, or `FAILED`.
- `status`: `PROGRESSING`, `READY`, or `FAILED`.
- Sanitized `message` and stable `reason` values suitable for display and
  branching. Raw event messages, registry names, node labels, secret names, and
  volume paths are not serialized.
- `started_at`, `updated_at`, `phase_started_at`, `elapsed_seconds`,
  `phase_elapsed_seconds`, and `inactivity_seconds`.

The same `AppConversationStartTask` shape is emitted item-by-item by
`POST /app-conversations/stream-start` and persisted for
`GET /app-conversations/start-tasks/search` and the task lookup endpoints.

The app server observes SandboxClaim/Sandbox conditions and their owned Pod/PVC
resources plus Kubernetes events. Repeated copies of the same scheduler event do
not count as progress. Autoscaler activation, scheduling, PVC phase changes,
image pull, container state, and readiness transitions do. Deterministic
scheduler constraints and terminal image/storage/container failures fail
immediately with a sanitized task detail containing the sandbox reference ID.

The default Linux image extends the matching OpenHands agent-server release with the
official Perforce CLI and MCP server described above.

The Windows profile accepts any digest-pinned registry reference matching the
runtime contract. Halo publishes its image as
`ghcr.io/ghermann-msft/agent-server:<sha>-windows-ltsc2025-amd64@sha256:<digest>`;
the same exact reference must be present in both the profile and Windows
SandboxTemplate before the profile is marked available.

### Reaching sandboxes from a browser

The default `sandbox_url_pattern` is cluster-internal, which is right when the app
server runs in the cluster and proxies traffic. Agent Canvas talks to the agent
server directly, so a browser-facing deployment needs an externally routable
pattern, for example `https://{sandbox_name}.sandboxes.example.com`, backed by an
Ingress or Gateway that routes to the per-sandbox Service.

### Session keys and warm pools

Claims contain only `warmPoolRef` and optional lifecycle configuration, so they
can adopt an already-running warm Sandbox. The app server stores the generated
session and encryption keys in a claim-owned Kubernetes Secret; the Secret is
never mounted into the pod. After adoption it calls Agent Server `POST /api/init`
with `X-Init-API-Key` and delivers the session/encryption keys, persistence paths,
webhook, CORS origins, ports, and profile runtime environment.

The template's `OH_SECRET_KEY` is only the pool-safe bootstrap credential for
`/api/init`. Keep it identical to the app server's `OH_SECRET_KEY`, rotate it by
recreating unclaimed warm-pool replicas, and never use it as a user session key.

## Verifying

```bash
kubectl get sandboxclaim,sandbox,pods
```

A running conversation shows a claim, a Sandbox reporting `Ready`, and a pod. The
app server pauses a sandbox by setting `spec.operatingMode: Suspended` (the pod is
removed, the volume is kept) and resumes it by setting `Running`.

## End-to-end check

Verified on a kind cluster and on GKE with the manifests in this directory.

```bash
# 1. cluster + controller + manifests (see Prerequisites above)
kind create cluster --name ohk8s
kubectl apply -f "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v0.5.2/sandbox-with-extensions.yaml"
kubectl -n agent-sandbox-system rollout status deploy --timeout=240s
kubectl create secret generic openhands-agent-server-bootstrap \
  --from-literal=secret-key="$OH_SECRET_KEY"
kubectl apply -f deploy/kubernetes/

# optional on kind: preload the image so the first sandbox starts quickly
docker pull ghcr.io/openhands/agent-server:1.37.1-python-p4-r25.2-p4mcp-2026.4.3126709
kind load docker-image ghcr.io/openhands/agent-server:1.37.1-python-p4-r25.2-p4mcp-2026.4.3126709 --name ohk8s

# 2. run the app server against it
RUNTIME=kubernetes SERVE_FRONTEND=false make start

# 3. drive a sandbox through the API
curl -X POST localhost:3000/api/v1/sandboxes -H 'Content-Type: application/json' -d '{}'
curl localhost:3000/api/v1/sandboxes/search
curl -X POST localhost:3000/api/v1/sandboxes/<id>/pause
curl -X POST localhost:3000/api/v1/sandboxes/<id>/resume
```

While a sandbox runs you should see a claim, a ready Sandbox, and a pod:

```bash
kubectl get sandboxclaim,sandbox,pods
```

Pausing sets `spec.operatingMode: Suspended` on the Sandbox (the pod goes away, the
volume stays); resuming sets it back to `Running`. Port-forwarding to the pod and
calling `/health` should return 200, and an authenticated endpoint should return
200 with the sandbox's `X-Session-API-Key` and 401 without it.
