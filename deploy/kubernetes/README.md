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

2. The template, pool, and RBAC from this directory:

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

Execution profiles are enforced at claim creation:

- The selected profile chooses a `SandboxWarmPool`, whose
  `sandboxTemplateRef` must match the profile's `template_name`.
- The claim overrides the template's `workspace` volume claim with the profile's
  workspace storage class and size. Templates must set
  `volumeClaimTemplatesPolicy: Overrides`.
- The durable Azure Files PVC, container image, resource requests/limits, mount
  roots, node selector, and tolerations are validated against the selected
  template before a claim is created. A mismatch fails explicitly instead of
  launching a sandbox with different infrastructure than the profile describes.

The bundled Linux and Windows templates mount the existing Halo Azure Files PVC
`openhands-halo-conversations`. To use another PVC or storage class, update the
template and seed the matching profile fields together (the profile can be seeded
with `OH_HALO_DURABLE_STATE_PVC` and `OH_HALO_DURABLE_STORAGE_CLASS`). Changing
only the profile is rejected at sandbox creation.

Linux mounts the sandbox's Azure Files directory at `/openhands` with
`subPathExpr: $(OH_SANDBOX_ID)`. Kubernetes Windows containers do not support
volume subPath mounts, so Windows mounts the whole share at `C:\OpenHands` and
the app server expands `{sandbox_id}` into the conversations, bash-events, and
persistence environment paths. This preserves deterministic per-sandbox
directories without relying on an unsupported Windows mount feature.

The global legacy startup fallback is configured with
`OH_APP_CONVERSATION_SANDBOX_STARTUP_TIMEOUT` (120 seconds by default). Curated
profiles override it with `startup_timeout_seconds`; the bundled Linux profile
uses 300 seconds and Windows uses 600 seconds. Node warm capacity and image
pre-pull remain the primary cold-start mitigation.

The default Linux image extends the matching OpenHands agent-server release with the
official Perforce MCP server v2026.4.3126709. The build verifies the published
Linux zip with SHA256
`f2ea428cf35acec04e822472b090128ff9e522d07264ab1211d46b05e479a387` and exposes
the stdio server as `/usr/local/bin/p4-mcp-server`. The upstream
[`perforce/p4mcp-server`](https://github.com/perforce/p4mcp-server) project is
licensed under the
[MIT license](https://github.com/perforce/p4mcp-server/blob/v2026.4.3126709/LICENSE.txt);
its source is not forked or vendored here.

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

The app server gives each sandbox a unique session API key by injecting it as an
environment variable on the claim. agent-sandbox cold starts a pod whenever a claim
carries `spec.env`, and the app server always sets webhook and port variables there,
so **pre-warmed replicas are never used today**, whatever `inject_session_key` is set
to. Turning that flag off only drops the per-sandbox key, which is not safe on a
shared cluster.

Using the warm path would mean sending no claim environment at all: bake the
configuration into the template and hand each sandbox its key through the agent
server after the claim binds. That needs post-adoption credential injection from
agent-sandbox, which does not exist yet.

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
kubectl apply -f deploy/kubernetes/

# optional on kind: preload the image so the first sandbox starts quickly
docker pull ghcr.io/openhands/agent-server:1.37.1-python-p4mcp-2026.4.3126709
kind load docker-image ghcr.io/openhands/agent-server:1.37.1-python-p4mcp-2026.4.3126709 --name ohk8s

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
