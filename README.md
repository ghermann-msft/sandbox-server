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

Authenticated organization members can inspect membership through:

- `GET /api/organizations/{organization_id}/members`
- `PATCH /api/organizations/{organization_id}/members/{user_id}` with
  `{"role": "owner" | "admin" | "member"}`

Owners and admins can update shared-organization roles. Personal-organization
roles are immutable, and the final owner of a shared organization cannot be
demoted. Identity provider, tenant, and subject identifiers are response-only.

Self-hosted deployments advertise Canvas integration settings through
`feature_flags.enable_canvas_integrations` in `GET /api/v1/web-client/config`.
Set `ENABLE_CANVAS_INTEGRATIONS=false` to hide them explicitly.

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
