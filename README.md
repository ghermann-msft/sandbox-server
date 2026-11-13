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
docker compose up --build
```

The Compose setup mounts the Docker socket and a local workspace so the server
can manage sandbox containers.

Release images use the `openhands-agent-server`, `openhands-sdk`, and
`openhands-tools` versions pinned in `poetry.lock`. To build against an Agent
Server image made from an unreleased software-agent-sdk revision, build Sandbox
Server from the same repository and full commit:

```bash
docker build \
  --build-arg SOFTWARE_AGENT_SDK_REPOSITORY=https://github.com/OpenHands/software-agent-sdk.git \
  --build-arg SOFTWARE_AGENT_SDK_REF=b1d818a6078691cd765e05adbc60cd5fecb4c671 \
  -f containers/app/Dockerfile \
  -t openhands-sandbox-server:latest .
```

`SOFTWARE_AGENT_SDK_REF` is opt-in. When it is empty, the Docker build uses the
locked release packages without cloning software-agent-sdk.

## Extraction provenance

The extraction is based on:

- source repository: `OpenHands/OpenHands`
- source commit: `ee9e78b7defdfa744e0bbe48c9cafa90b6135ad7`
- permanent monorepo snapshot: [OpenHands/legacy](https://github.com/OpenHands/legacy)
- migration tracker: [OpenHands/OpenHands#15396](https://github.com/OpenHands/OpenHands/issues/15396)

History was filtered to the server and its required repository-local support
files. The dependencies on `openhands-agent-server`, `openhands-sdk`, and
`openhands-tools` remain explicit pinned package dependencies.
