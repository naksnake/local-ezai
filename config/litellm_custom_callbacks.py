"""
config/litellm_custom_callbacks.py
─────────────────────────────────────────────────────────────────────────────
LiteLLM proxy hook: automatic RAG for every chat completion.

Runs inside the litellm container (mounted next to config.yaml). On each
chat request it:

  1. Strips OpenWebUI's BUILT-IN knowledge tools from the request. Those
     search OpenWebUI's internal document store — always empty in this
     stack — and small models keep calling them and concluding "no results".
  2. Embeds the latest user message, searches the Qdrant knowledge base,
     and injects the top matching excerpts as system context.

Because this runs at the proxy, it applies to every client (OpenWebUI,
curl, the lab monitor) with no per-client setup, and fails open: if the
knowledge base is unreachable or has no relevant match, the request
passes through unchanged.

Tune via environment variables on the litellm service:
  EMBED_URL, QDRANT_URL, RAG_COLLECTION, RAG_TOP_K, RAG_MIN_SCORE,
  RAG_AUTO (set to "false" to disable injection but keep tool stripping)
"""
import os

import httpx
from litellm.integrations.custom_logger import CustomLogger

EMBED_URL = os.getenv("EMBED_URL", "http://embed-server:8001/v1")
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
COLLECTION = os.getenv("RAG_COLLECTION", "my-knowledge-base")
TOP_K = int(os.getenv("RAG_TOP_K", "3"))
MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "0.4"))
RAG_AUTO = os.getenv("RAG_AUTO", "true").lower() != "false"

# OpenWebUI built-in agentic tools that query ITS internal stores (empty in
# this stack). Distinct from the mcpo tools, which pass through untouched.
BLOCKED_TOOLS = {
    "search_knowledge_bases", "query_knowledge_bases", "kb_exec",
    "search_knowledge_files", "query_knowledge_files", "grep_knowledge_files",
    "list_knowledge", "view_knowledge_file",
}


def _last_user_text(messages: list) -> str | None:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):  # multimodal message: pull the text parts
                return " ".join(
                    p.get("text", "") for p in c
                    if isinstance(p, dict) and p.get("type") == "text"
                )
    return None


class AutoRAGHandler(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if call_type not in ("completion", "acompletion"):
            return data

        # ── 1. remove OpenWebUI's built-in knowledge tools ────────────────
        tools = data.get("tools")
        if tools:
            kept = [
                t for t in tools
                if (t.get("function") or {}).get("name") not in BLOCKED_TOOLS
            ]
            if kept:
                data["tools"] = kept
            else:
                data.pop("tools", None)
                data.pop("tool_choice", None)

        if not RAG_AUTO:
            return data

        # ── 2. inject knowledge-base context ──────────────────────────────
        messages = data.get("messages") or []
        query = _last_user_text(messages)
        if not query or not query.strip():
            return data

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(
                    f"{EMBED_URL}/embeddings",
                    json={"model": "nomic-embed-text-v1.5", "input": query},
                )
                r.raise_for_status()
                vector = r.json()["data"][0]["embedding"]

                r = await client.post(
                    f"{QDRANT_URL}/collections/{COLLECTION}/points/search",
                    json={"vector": vector, "limit": TOP_K, "with_payload": True},
                )
                r.raise_for_status()
                hits = r.json()["result"]
        except Exception:
            return data  # KB down — answer without context, never block chat

        excerpts = [
            f"[{h['payload'].get('source', 'unknown')}] {h['payload'].get('text', '')}"
            for h in hits
            if h.get("score", 0) >= MIN_SCORE and h.get("payload")
        ]
        if not excerpts:
            return data

        context = (
            "Relevant excerpts from the local knowledge base — use them to "
            "answer, and mention the source file in [brackets] when you do:\n\n"
            + "\n\n---\n\n".join(excerpts)
        )
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = f"{messages[0]['content']}\n\n{context}"
        else:
            messages.insert(0, {"role": "system", "content": context})
        data["messages"] = messages
        return data


auto_rag_handler = AutoRAGHandler()
