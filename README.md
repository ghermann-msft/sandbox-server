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

## Docker

```bash
make docker-build
docker compose up
```

The Compose setup mounts the Docker socket and a local workspace so the server
can manage sandbox containers. The sandbox runtime image extends the pinned
OpenHands agent-server image with the official Perforce MCP server
v2026.4.3126709. MCP configuration can invoke the prebundled stdio executable at
`/usr/local/bin/p4-mcp-server`; no package download is needed when a conversation
starts.

Production deployments can build and publish the runtime image independently:

```bash
docker build -f containers/sandbox/Dockerfile \
  --build-arg AGENT_SERVER_IMAGE=ghcr.io/openhands/agent-server:1.37.1-python \
  -t <runtime-image> .
docker push <runtime-image>
```

Set `AGENT_SERVER_IMAGE` on the sandbox-server control-plane container to the
published immutable reference, preferably `<registry>/<repository>@sha256:<digest>`.
This full-reference override takes precedence over the legacy
`AGENT_SERVER_IMAGE_REPOSITORY` and `AGENT_SERVER_IMAGE_TAG` variables.

## Extraction provenance

The extraction is based on:

- source repository: `OpenHands/OpenHands`
- source commit: `ee9e78b7defdfa744e0bbe48c9cafa90b6135ad7`
- permanent monorepo snapshot: [OpenHands/legacy](https://github.com/OpenHands/legacy)
- migration tracker: [OpenHands/OpenHands#15396](https://github.com/OpenHands/OpenHands/issues/15396)

History was filtered to the server and its required repository-local support
files. The dependencies on `openhands-agent-server`, `openhands-sdk`, and
`openhands-tools` remain explicit pinned package dependencies.
