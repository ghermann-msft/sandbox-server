ARG AGENT_SERVER_IMAGE=ghcr.io/openhands/agent-server:1.37.1-python

FROM ${AGENT_SERVER_IMAGE} AS p4mcp-builder

USER root

ARG P4_MCP_VERSION=2026.4.3126709
ARG P4_MCP_URL=https://github.com/perforce/p4mcp-server/releases/download/v2026.4.3126709/p4-mcp-server-linux.zip
ARG P4_MCP_SHA256=f2ea428cf35acec04e822472b090128ff9e522d07264ab1211d46b05e479a387

RUN apt-get update -y \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && curl --fail --location --output /tmp/p4-mcp-server.zip "${P4_MCP_URL}" \
    && echo "${P4_MCP_SHA256}  /tmp/p4-mcp-server.zip" | sha256sum --check - \
    && unzip -q /tmp/p4-mcp-server.zip -d /tmp/p4-mcp-server \
    && test -x "/tmp/p4-mcp-server/p4-mcp-server-${P4_MCP_VERSION}/p4-mcp-server" \
    && install -d /opt/perforce/p4mcp-server \
    && cp -a "/tmp/p4-mcp-server/p4-mcp-server-${P4_MCP_VERSION}/." /opt/perforce/p4mcp-server/

FROM ${AGENT_SERVER_IMAGE}

USER root

COPY --from=p4mcp-builder --chown=root:root /opt/perforce/p4mcp-server /opt/perforce/p4mcp-server

RUN chmod -R a+rX /opt/perforce/p4mcp-server \
    && ln -s /opt/perforce/p4mcp-server/p4-mcp-server /usr/local/bin/p4-mcp-server

USER openhands
