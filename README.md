# OpenHands Sandbox Server

This repository contains the standalone OpenHands API and sandbox control plane.
It is the history-preserving extraction of `openhands/app_server/` from the
former `OpenHands/OpenHands` monorepo.

The repository includes:

- the FastAPI application in `openhands/app_server/`;
- the small local `openhands/analytics/` and `openhands/db/` support packages;
- bundled skills and the focused app-server test suite;
- pinned Python packaging, Docker, and Compose configuration.

The frontend is intentionally not bundled. Use
[OpenHands Agent Canvas](https://github.com/OpenHands/agent-canvas) as the web
client.

## Local development

Requirements:

- Python 3.12 or 3.13
- Poetry 2.3.4 or newer
- Docker when using Docker-backed sandboxes

```bash
make install
make start
```

The server listens on `http://localhost:3000` by default.

Run validation with:

```bash
make lint
make test
```

## Linux Perforce runtime

The Linux sandbox runtime image extends the pinned OpenHands agent-server image
with the official Perforce `p4` r25.2 Linux client and `p4-mcp-server`
v2026.4.3126709. Both downloads are pinned by URL and SHA256. The executable
paths are `/usr/local/bin/p4` and `/usr/local/bin/p4-mcp-server`.

Build the runtime and control-plane images with:

```bash
make docker-build
```

Set `AGENT_SERVER_IMAGE` on the sandbox-server container to the published
immutable runtime reference, preferably
`<registry>/<repository>@sha256:<digest>`. It takes precedence over the legacy
`AGENT_SERVER_IMAGE_REPOSITORY` and `AGENT_SERVER_IMAGE_TAG` variables.

For stdio MCP servers, `P4PORT`, `P4USER`, and `P4PASSWD` are moved into
deterministic per-server conversation secret names and each process-local MCP
environment is rewritten to `${...}` references. Multiple Perforce servers
therefore retain distinct credentials. Other MCP environment fields and remote
server headers are preserved. Do not put plaintext Perforce credentials in
images, manifests, or logs.

The required Perforce Software redistribution attribution and upstream license
links are retained in
[`containers/sandbox/PERFORCE-NOTICE.txt`](containers/sandbox/PERFORCE-NOTICE.txt)
and copied into the runtime image.

## Organization RBAC bootstrap

Authenticated deployments can seed a shared organization and its initial owner at
startup without using identity-provider groups as an authorization source:

```text
OH_RBAC_BOOTSTRAP_ORGANIZATION_ID=<stable UUID>
OH_RBAC_BOOTSTRAP_ORGANIZATION_NAME=Halo Studios
OH_RBAC_BOOTSTRAP_OWNER_PROVIDER=entra
OH_RBAC_BOOTSTRAP_OWNER_TENANT_ID=<immutable tenant ID>
OH_RBAC_BOOTSTRAP_OWNER_OBJECT_ID=<immutable object ID>
```

The configured `UserAuth` implementation must return the same immutable provider,
tenant, and subject identifiers from `get_authentication_identity()`. Organization
membership and permissions are then read only from the sandbox-server database.

## Private Hindsight broker

The Linux-only Hindsight proof of concept is disabled by default. Enable it on
the trusted app-server control plane with:

- `OH_HINDSIGHT_ENABLED=true`
- `OH_HINDSIGHT_BASE_URL` for the private internal service
- `OH_HINDSIGHT_API_KEY_FILE` for the server-side bearer credential file
- `OH_HINDSIGHT_BANK_SECRET_FILE` for the opaque bank-identity secret file
- `OH_HINDSIGHT_DEPLOYMENT_ID` for the immutable deployment-wide `server` scope
- `OH_HINDSIGHT_SCOPE_BINDINGS_JSON_FILE` for organization metadata and
  repository-to-organization allowlists

`OH_HINDSIGHT_FAIL_OPEN` and `OH_HINDSIGHT_LINUX_ONLY` default to `true`.
Recall modes are control-plane bounded to 1024 tokens (`default`), 2048
(`adaptive`), and 4096 (`deep`). Hindsight credentials and bank identifiers
remain in the app server and are never added to sandbox environments.
The deployment mount paths are `/mnt/hindsight-secrets/hindsightApiKey`,
`/mnt/hindsight-secrets/hindsightBankSecret`, and
`/mnt/hindsight-secrets/hindsightScopeBindingsJson`. Scope bindings use:

```json
{
  "organizations": {
    "personal-stable-id": {
      "display_name": "Personal",
      "kind": "personal"
    },
    "halo-studios-stable-id": {
      "display_name": "Halo Studios",
      "kind": "shared"
    }
  },
  "repositories": {
    "github:owner/repository": {
      "organization_ids": ["personal-stable-id", "halo-studios-stable-id"]
    }
  }
}
```

Recall and reflect consume an ordered `scope` list of `{ "type", "id" }`
objects. Supported types are `private`, `workspace`, `repository`,
`organization`, and `server`; the broker validates every supplied ID against
trusted identity, conversation, deployment, and binding records.

OpenHands database RBAC is authoritative for identity, organization selection,
membership, roles, and permissions. Organization memory requires
`hindsight:recall` or `hindsight:retain`; scope-binding files contain metadata
and repository allowlists only.

Conversation creation accepts the requested tenant in `X-Org-Id`, verifies the
authenticated user's durable membership, and persists the stable organization
ID with the conversation. A body-supplied organization cannot bypass that check.

## Docker

```bash
docker compose up --build
```

The Compose setup mounts the Docker socket and a local workspace so the server
can manage sandbox containers.

## Extraction provenance

The extraction is based on:

- source repository: `OpenHands/OpenHands`
- source commit: `ee9e78b7defdfa744e0bbe48c9cafa90b6135ad7`
- permanent monorepo snapshot: [OpenHands/legacy](https://github.com/OpenHands/legacy)
- migration tracker: [OpenHands/OpenHands#15396](https://github.com/OpenHands/OpenHands/issues/15396)

History was filtered to the server and its required repository-local support
files. The dependencies on `openhands-agent-server`, `openhands-sdk`, and
`openhands-tools` remain explicit pinned package dependencies.
