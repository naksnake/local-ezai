#!/usr/bin/env node
/**
 * mcp-servers/qdrant-rag/index.js
 * ─────────────────────────────────────────────────────────────────────────────
 * Custom MCP Server: Qdrant Semantic Search
 *
 * Exposes two tools any MCP-compatible AI can call:
 *   • search_knowledge_base  — embed a query and find matching document chunks
 *   • list_collections       — list all Qdrant collections
 *
 * Environment variables:
 *   QDRANT_URL   Qdrant server URL           (default: http://localhost:6333)
 *   EMBED_URL    Embedding server base URL   (default: http://localhost:8001/v1)
 *   COLLECTION   Collection to search        (default: my-knowledge-base)
 */

const { Server }               = require("@modelcontextprotocol/sdk/server/index.js");
const { StdioServerTransport } = require("@modelcontextprotocol/sdk/server/stdio.js");
const { QdrantClient }         = require("@qdrant/js-client-rest");
const axios                    = require("axios");

const QDRANT_URL = process.env.QDRANT_URL || "http://localhost:6333";
const EMBED_URL  = process.env.EMBED_URL  || "http://localhost:8001/v1";
const COLLECTION = process.env.COLLECTION || "my-knowledge-base";

const qdrant = new QdrantClient({ url: QDRANT_URL });

// ── Create the MCP server ─────────────────────────────────────────────────
const server = new Server(
  { name: "qdrant-rag", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

// ── Tool definitions ──────────────────────────────────────────────────────
server.setRequestHandler("tools/list", async () => ({
  tools: [
    {
      name: "search_knowledge_base",
      description:
        "Search the personal knowledge base for relevant information. " +
        "Use this when the user asks about stored documents, notes, or any " +
        "content they may have uploaded to their knowledge base.",
      inputSchema: {
        type: "object",
        properties: {
          query: {
            type: "string",
            description: "Natural language search query"
          },
          limit: {
            type: "number",
            description: "Number of results to return (default: 5, max: 10)",
            default: 5
          }
        },
        required: ["query"]
      }
    },
    {
      name: "list_collections",
      description: "List all available Qdrant collections (knowledge bases).",
      inputSchema: {
        type: "object",
        properties: {}
      }
    }
  ]
}));

// ── Tool handlers ─────────────────────────────────────────────────────────
server.setRequestHandler("tools/call", async (request) => {
  const { name, arguments: args } = request.params;

  // ── list_collections ──────────────────────────────────────────────────
  if (name === "list_collections") {
    try {
      const resp = await qdrant.getCollections();
      const names = resp.collections.map(c => c.name);
      return {
        content: [{
          type: "text",
          text: names.length > 0
            ? `Available collections:\n${names.map(n => `  • ${n}`).join("\n")}`
            : "No collections found. Run embed_documents.py to populate the knowledge base."
        }]
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Error listing collections: ${err.message}` }],
        isError: true
      };
    }
  }

  // ── search_knowledge_base ─────────────────────────────────────────────
  if (name === "search_knowledge_base") {
    const { query, limit = 5 } = args || {};
    if (!query) {
      return { content: [{ type: "text", text: "query is required." }], isError: true };
    }

    // Step 1: Embed the query using the local embedding server
    let vector;
    try {
      const embedResp = await axios.post(
        `${EMBED_URL}/embeddings`,
        { model: "nomic-embed-text-v1.5", input: query },
        { timeout: 15000 }
      );
      const embeddingData = embedResp.data.data;
      if (!embeddingData || embeddingData.length === 0) {
        return { content: [{ type: "text", text: "Embed server returned no embeddings for the query." }], isError: true };
      }
      vector = embeddingData[0].embedding;
    } catch (err) {
      const msg = err.code === "ECONNREFUSED"
        ? `Cannot reach embedding server at ${EMBED_URL}.\nIs the embed-server container running?`
        : `Embedding error: ${err.message}`;
      return { content: [{ type: "text", text: msg }], isError: true };
    }

    // Step 2: Find nearest neighbours in Qdrant
    try {
      const results = await qdrant.search(COLLECTION, {
        vector,
        limit: Math.max(1, Math.min(Number(limit) || 5, 10)),
        with_payload: true
      });

      if (results.length === 0) {
        return {
          content: [{
            type: "text",
            text: `No results found for: "${query}"\n` +
                  `Make sure you have embedded documents into the '${COLLECTION}' collection.\n` +
                  `Run: make embed`
          }]
        };
      }

      // Step 3: Format results for the LLM to read
      const formatted = results
        .map((r, i) => {
          const source = r.payload?.source   || "unknown source";
          const text   = r.payload?.text     || "";
          const score  = r.score.toFixed(3);
          return `[${i + 1}] ${source}  (relevance: ${score})\n${text.substring(0, 500)}`;
        })
        .join("\n\n---\n\n");

      return {
        content: [{
          type: "text",
          text: `Found ${results.length} relevant passages for: "${query}"\n\n${formatted}`
        }]
      };

    } catch (err) {
      const msg = err.code === "ECONNREFUSED"
        ? `Cannot reach Qdrant at ${QDRANT_URL}.\nIs the Qdrant container running?`
        : `Search error: ${err.message}`;
      return { content: [{ type: "text", text: msg }], isError: true };
    }
  }

  return {
    content: [{ type: "text", text: `Unknown tool: ${name}` }],
    isError: true
  };
});

// ── Start the server ──────────────────────────────────────────────────────
const transport = new StdioServerTransport();
server.connect(transport).then(() => {
  process.stderr.write(`[qdrant-rag] MCP server ready (collection: ${COLLECTION})\n`);
}).catch((err) => {
  process.stderr.write(`[qdrant-rag] Failed to start: ${err.message}\n`);
  process.exit(1);
});
