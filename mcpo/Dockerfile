FROM node:20-slim

WORKDIR /app

# Install mcpo proxy and official MCP server packages globally
RUN npm install -g \
    mcpo \
    @modelcontextprotocol/server-filesystem \
    @modelcontextprotocol/server-memory \
    @modelcontextprotocol/server-fetch

# Copy and install the custom Qdrant RAG MCP server
COPY mcp-servers/qdrant-rag/ /mcp-servers/qdrant-rag/
WORKDIR /mcp-servers/qdrant-rag
RUN npm install --omit=dev

WORKDIR /app
EXPOSE 8200

# mcpo reads the server list from /app/mcpo-config.json (mounted at runtime)
CMD ["mcpo", "--port", "8200", "--api-key", "local-tools-key", \
     "--config", "/app/mcpo-config.json"]
