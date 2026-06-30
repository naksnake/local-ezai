"""
tools/knowledge-base-search.py
─────────────────────────────────────────────────────────────────────────────
OpenWebUI Python Tool — Knowledge Base Search

HOW TO INSTALL:
  1. Open OpenWebUI: http://localhost:3000
  2. Go to Admin Panel → Tools → + New Tool
  3. Copy-paste this entire file into the code editor
  4. Click Save
  5. In any chat, enable the 🔧 wrench icon to activate tools

WHAT IT DOES:
  Embeds the user's query and searches your Qdrant collection for matching
  document chunks. Returns the top results with source names and relevance.
─────────────────────────────────────────────────────────────────────────────

title: Knowledge Base Search
description: Search your personal documents stored in the Qdrant vector database
version: 1.0.0
author: ai-service
"""
import requests
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        """
        Configuration for this tool.
        Edit these in the tool's Settings panel in OpenWebUI.
        """
        qdrant_url:  str = Field(default="http://qdrant:6333",
                                  description="Qdrant server URL")
        embed_url:   str = Field(default="http://embed-server:8001/v1",
                                  description="Embedding server URL")
        collection:  str = Field(default="my-knowledge-base",
                                  description="Qdrant collection to search")
        max_results: int = Field(default=5,
                                  description="Maximum chunks to return (1-10)")

    def __init__(self):
        self.valves = self.Valves()

    def search_knowledge_base(self, query: str) -> str:
        """
        Search the personal knowledge base for relevant documents.
        Use this tool when the user asks about stored documents,
        personal notes, or any content in their knowledge base.
        """
        try:
            # Step 1: Embed the query
            embed_resp = requests.post(
                f"{self.valves.embed_url}/embeddings",
                json={"model": "nomic-embed-text-v1.5", "input": query},
                timeout=15,
            )
            embed_resp.raise_for_status()
            vector = embed_resp.json()["data"][0]["embedding"]

            # Step 2: Search Qdrant for nearest neighbours
            search_resp = requests.post(
                f"{self.valves.qdrant_url}/collections/{self.valves.collection}/points/search",
                json={
                    "vector": vector,
                    "limit": max(1, min(self.valves.max_results, 10)),
                    "with_payload": True,
                },
                timeout=15,
            )
            search_resp.raise_for_status()
            results = search_resp.json().get("result", [])

            # Step 3: Format results
            if not results:
                return (
                    f"No relevant documents found for: '{query}'\n"
                    "Embed some documents first with: make embed"
                )

            output = []
            for i, r in enumerate(results, 1):
                source = r["payload"].get("source", "unknown source")
                text   = r["payload"].get("text", "")[:600]
                score  = round(r["score"], 3)
                output.append(f"[{i}] {source}  (relevance: {score})\n{text}")

            return "\n\n---\n\n".join(output)

        except requests.exceptions.ConnectionError as e:
            return (
                "Cannot connect to the embedding server or Qdrant.\n"
                "Check that both containers are running:\n"
                "  docker compose ps embed-server qdrant"
            )
        except Exception as e:
            return f"Search error: {type(e).__name__}: {e}"
