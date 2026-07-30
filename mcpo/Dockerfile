FROM node:22-slim

WORKDIR /app

# mcpo and the fetch MCP server are Python packages (PyPI only — neither
# exists on npm); filesystem/memory are npm packages; qdrant-rag is vendored.
# Everything is PINNED: mcp 2.0.0 broke mcpo 0.0.20 (streamablehttp_client
# rename), so unpinned rebuilds are a time bomb. Bump versions together.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/mcpo \
    && /opt/mcpo/bin/pip install --no-cache-dir \
        mcpo==0.0.20 \
        mcp==1.28.1 \
        mcp-server-fetch==2026.7.10
ENV PATH="/opt/mcpo/bin:${PATH}"

RUN npm install -g \
    @modelcontextprotocol/server-filesystem@2026.7.10 \
    @modelcontextprotocol/server-memory@2026.7.4

# Copy and install the custom Qdrant RAG MCP server
COPY mcp-servers/qdrant-rag/ /mcp-servers/qdrant-rag/
WORKDIR /mcp-servers/qdrant-rag
RUN npm install --omit=dev

WORKDIR /app
EXPOSE 8200

COPY mcpo/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# entrypoint.sh renders ${VAR:-default} placeholders in the mounted
# /app/mcpo-config.json, then starts mcpo on the rendered copy
ENV MCP_API_KEY=local-tools-key
CMD ["/entrypoint.sh"]
