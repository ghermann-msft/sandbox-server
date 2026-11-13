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
| `sandbox_url_pattern` | `http://{sandbox_name}.{namespace}.svc.cluster.local:{port}` | Client-facing sandbox URL. Placeholders: `{sandbox_name}`, `{sandbox_id}`, `{namespace}`, `{port}`. The default requires `spec.service: true` in the SandboxTemplate; otherwise configure an external router pattern |
| `webhook_base_url` | in-cluster service URL | This app server, as reachable *from* a sandbox pod |
| `max_num_sandboxes` | `10` | Concurrency cap; the oldest sandboxes are paused beyond it |
| `inject_session_key` | `true` | Give every sandbox its own session API key |
| `shutdown_after_seconds` | unset | TTL safety net; the controller deletes the claim afterwards |

### Azure PostgreSQL workload identity

Set `DB_AZURE_IDENTITY=true` to authenticate PostgreSQL connections with
`DefaultAzureCredential` instead of `DB_PASS`. The configured `DB_USER` is the
Microsoft Entra PostgreSQL principal. Every new physical connection obtains a new
token for `https://ossrdbms-aad.database.windows.net/.default`; pooled connections
continue to be reused normally.

At minimum configure `DB_HOST`, `DB_NAME`, `DB_USER`, and `DB_SSL_MODE=require`.
Use the standard Azure Identity environment variables when needed, including
`AZURE_CLIENT_ID` to select a user-assigned workload identity. Leave
`DB_AZURE_IDENTITY` unset or false to preserve static `DB_PASS` authentication.

### Server-managed LLM

Set `OH_ALLOW_USER_LLM_CONFIGURATION=false` to make the app server's `LLM_*`
configuration authoritative for OpenHands conversation launches. This also hides
the BYOK editing UI. The default is true, which preserves existing OSS and Cloud
behavior and uses each user's persisted LLM settings.

Managed mode requires `LLM_MODEL`. It also accepts `LLM_API_KEY`,
`LLM_BASE_URL`, `LLM_API_VERSION`, and `LLM_AUTH_TYPE`. Halo's Azure OpenAI
workload-identity configuration is:

```bash
OH_ALLOW_USER_LLM_CONFIGURATION=false
LLM_MODEL=azure/gpt-5.6-sol
LLM_BASE_URL=https://<resource>.openai.azure.com
LLM_API_VERSION=<azure-openai-api-version>
LLM_AUTH_TYPE=azure_workload_identity
```

Azure authentication uses the workload identity available to the process making
the LLM call; both app-server and agent-server pods must receive the intended
identity. No per-user API key or settings row is required. The agent-server image
must support the `azure_workload_identity` LLM authentication mode.

Pod-level concerns live in the `SandboxTemplate` rather than here. That covers the
image, CPU and memory, `runtimeClassName`, network policy and volumes.

The app server checks Agent Server readiness over the backing pod IP, independent
of `sandbox_url_pattern`, and authenticates the `/alive` request with the sandbox
session key. Its service account therefore needs `get` access to pods; the supplied
`rbac.yaml` includes that permission.

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
docker pull ghcr.io/openhands/agent-server:1.37.1-python
kind load docker-image ghcr.io/openhands/agent-server:1.37.1-python --name ohk8s

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
