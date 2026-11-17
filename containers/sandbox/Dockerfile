ARG AGENT_SERVER_IMAGE=ghcr.io/openhands/agent-server:1.37.1-python

FROM ${AGENT_SERVER_IMAGE} AS perforce-builder

USER root

ARG P4_VERSION=r25.2
ARG P4_URL=https://cdist2.perforce.com/perforce/r25.2/bin.linux26x86_64/p4
ARG P4_SHA256=8f3c2688c537f614fd5ae94a1edb0110c35a08bca4bc856c4cdc5e29b68e457e
ARG P4_MCP_VERSION=2026.4.3126709
ARG P4_MCP_URL=https://github.com/perforce/p4mcp-server/releases/download/v2026.4.3126709/p4-mcp-server-linux.zip
ARG P4_MCP_SHA256=f2ea428cf35acec04e822472b090128ff9e522d07264ab1211d46b05e479a387

RUN apt-get update -y \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && install -d /opt/perforce/downloads /opt/perforce/bin /opt/perforce/p4mcp-server \
    && curl --fail --location --output /opt/perforce/downloads/p4 "${P4_URL}" \
    && echo "${P4_SHA256}  /opt/perforce/downloads/p4" | sha256sum --check - \
    && install -m 0755 /opt/perforce/downloads/p4 /opt/perforce/bin/p4 \
    && curl --fail --location --output /opt/perforce/downloads/p4-mcp-server.zip "${P4_MCP_URL}" \
    && echo "${P4_MCP_SHA256}  /opt/perforce/downloads/p4-mcp-server.zip" | sha256sum --check - \
    && unzip -q /opt/perforce/downloads/p4-mcp-server.zip -d /opt/perforce/downloads/p4-mcp-server \
    && test -x "/opt/perforce/downloads/p4-mcp-server/p4-mcp-server-${P4_MCP_VERSION}/p4-mcp-server" \
    && cp -a "/opt/perforce/downloads/p4-mcp-server/p4-mcp-server-${P4_MCP_VERSION}/." /opt/perforce/p4mcp-server/

FROM ${AGENT_SERVER_IMAGE}

USER root

COPY --from=perforce-builder --chown=root:root /opt/perforce/bin /opt/perforce/bin
COPY --from=perforce-builder --chown=root:root /opt/perforce/p4mcp-server /opt/perforce/p4mcp-server
COPY containers/sandbox/PERFORCE-NOTICE.txt /usr/share/doc/perforce/PERFORCE-NOTICE.txt

RUN chmod -R a+rX /opt/perforce \
    && install -d -o openhands -g openhands /opt/perforce/p4mcp-server/logs \
    && ln -s /opt/perforce/bin/p4 /usr/local/bin/p4 \
    && ln -s /opt/perforce/p4mcp-server/p4-mcp-server /usr/local/bin/p4-mcp-server

USER openhands
