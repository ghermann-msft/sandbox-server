ARG OPENHANDS_BUILD_VERSION=dev
FROM python:3.13.17-slim-trixie AS base

FROM base AS backend-builder

WORKDIR /app

ENV PYTHONPATH=/app \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_VIRTUALENVS_CREATE=1 \
    POETRY_CACHE_DIR=/tmp/poetry_cache

ARG POETRY_VERSION=2.3.4
# Keep the Sandbox control plane on the same SDK source as the hosted Agent
# Server until the workload-identity auth contract is available in a release.
ARG SOFTWARE_AGENT_SDK_REPOSITORY=https://github.com/ghermann-msft/software-agent-sdk.git
ARG SOFTWARE_AGENT_SDK_REF=87b5776c66843665ab218adf04721ae1bc99f045
RUN apt-get update -y \
    && apt-get install -y curl make git build-essential jq gettext \
    && python3 -m pip install "poetry==${POETRY_VERSION}" --break-system-packages

COPY pyproject.toml poetry.lock ./
RUN touch README.md \
    && poetry install --no-root \
    && .venv/bin/pip uninstall -y fastmcp \
    && SDK_GIT_URL="git+${SOFTWARE_AGENT_SDK_REPOSITORY}@${SOFTWARE_AGENT_SDK_REF}" \
    && .venv/bin/pip install --no-cache-dir --force-reinstall \
        "${SDK_GIT_URL}#subdirectory=openhands-agent-server" \
        "${SDK_GIT_URL}#subdirectory=openhands-sdk" \
        "${SDK_GIT_URL}#subdirectory=openhands-tools" \
    && rm -rf "${POETRY_CACHE_DIR}"

FROM base AS openhands-sandbox-server

WORKDIR /app

ARG OPENHANDS_BUILD_VERSION
ARG PIP_VERSION=26.2.2

ENV RUN_AS_OPENHANDS=true \
    OPENHANDS_USER_ID=42420 \
    SANDBOX_LOCAL_RUNTIME_URL=http://host.docker.internal \
    USE_HOST_NETWORK=false \
    WORKSPACE_BASE=/opt/workspace_base \
    OPENHANDS_BUILD_VERSION=${OPENHANDS_BUILD_VERSION} \
    SANDBOX_USER_ID=0 \
    FILE_STORE=local \
    FILE_STORE_PATH=/.openhands \
    INIT_GIT_IN_EMPTY_WORKSPACE=1 \
    SERVE_FRONTEND=false \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    PYTHONPATH=/app

RUN mkdir -p "${FILE_STORE_PATH}" "${WORKSPACE_BASE}" \
    && apt-get update -y \
    && apt-get install -y curl git ssh sudo \
    && rm -rf /var/lib/apt/lists/* \
    && sed -i 's/^UID_MIN.*/UID_MIN 499/' /etc/login.defs \
    && sed -i 's/^UID_MAX.*/UID_MAX 1000000/' /etc/login.defs \
    && groupadd --gid "${OPENHANDS_USER_ID}" openhands \
    && useradd -l -m -u "${OPENHANDS_USER_ID}" --gid "${OPENHANDS_USER_ID}" -s /bin/bash openhands \
    && usermod -aG sudo openhands \
    && echo '%sudo ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers \
    && chown -R openhands:openhands /app "${WORKSPACE_BASE}" \
    && chmod -R 770 /app "${WORKSPACE_BASE}"

COPY --chown=openhands:openhands --chmod=770 --from=backend-builder ${VIRTUAL_ENV} ${VIRTUAL_ENV}

RUN python -m pip install --no-cache-dir "pip==${PIP_VERSION}" \
    && /usr/local/bin/python3 -m pip install --no-cache-dir "pip==${PIP_VERSION}" --break-system-packages

COPY --chown=openhands:openhands --chmod=770 skills skills
COPY --chown=openhands:openhands --chmod=770 openhands openhands
COPY --chown=openhands:openhands pyproject.toml poetry.lock README.md MANIFEST.in LICENSE ./
COPY --chown=openhands:openhands --chmod=770 containers/app/entrypoint.sh /app/entrypoint.sh

RUN LLM_MODEL=azure/test-model \
    LLM_BASE_URL=https://example.openai.azure.com \
    LLM_API_VERSION=2025-04-01-preview \
    LLM_AUTH_TYPE=azure_workload_identity \
    python - <<'PY'
from fastmcp.mcp_config import MCPConfig

from openhands.app_server.app import app
from openhands.app_server.settings.sql_settings_store import _default_hosted_settings
from openhands.sdk.llm import LLM

assert MCPConfig is not None
assert app.routes

llm = LLM(
    model='azure/test-model',
    base_url='https://example.openai.azure.com',
    api_version='2025-04-01-preview',
    auth_type='azure_workload_identity',
)
assert llm.auth_type == 'azure_workload_identity'

settings = _default_hosted_settings()
assert settings.agent_settings.llm.auth_type == 'azure_workload_identity'
assert settings.llm_profiles.require('default').auth_type == (
    'azure_workload_identity'
)
assert settings.llm_profiles.active == 'default'
assert settings.title_llm_profile == 'default'
PY

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "openhands.app_server.app:app", "--host", "0.0.0.0", "--port", "3000"]
