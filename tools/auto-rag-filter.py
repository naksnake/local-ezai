"""
title: Auto RAG (Knowledge Base)
author: local-ezai
version: 1.0.0
description: Automatically searches the local Qdrant knowledge base on every message and injects matching excerpts — no tool calling needed.

HOW TO INSTALL:
  1. Open OpenWebUI → Admin Panel → Functions → + New Function
  2. Paste this entire file, Save
  3. Toggle the function ON, and enable its "Global" switch so it applies
     to every model automatically
  4. Chat normally — answers are grounded in your knowledge base whenever
     it contains something relevant (sources shown in [brackets])

Tune via the function's valves (gear icon): top_k, min_score, collection.
"""
from typing import Optional

import requests
from pydantic import BaseModel, Field


class Filter:
    class Valves(BaseModel):
        enabled: bool = Field(default=True, description="Turn auto-RAG on/off")
        embed_url: str = Field(default="http://embed-server:8001/v1",
                               description="Embedding server URL")
        qdrant_url: str = Field(default="http://qdrant:6333",
                                description="Qdrant server URL")
        collection: str = Field(default="my-knowledge-base",
                                description="Qdrant collection to search")
        top_k: int = Field(default=3, description="Max excerpts to inject")
        min_score: float = Field(default=0.4,
                                 description="Minimum similarity score (0-1)")

    def __init__(self):
        self.valves = self.Valves()

    @staticmethod
    def _last_user_text(messages: list) -> Optional[str]:
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

    def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        if not self.valves.enabled:
            return body
        messages = body.get("messages") or []
        query = self._last_user_text(messages)
        if not query or not query.strip():
            return body

        try:
            emb = requests.post(
                f"{self.valves.embed_url}/embeddings",
                json={"model": "nomic-embed-text-v1.5", "input": query},
                timeout=30,
            ).json()["data"][0]["embedding"]
            hits = requests.post(
                f"{self.valves.qdrant_url}/collections/{self.valves.collection}/points/search",
                json={"vector": emb, "limit": self.valves.top_k, "with_payload": True},
                timeout=30,
            ).json()["result"]
        except Exception:
            # Knowledge base unreachable — let the chat proceed without context
            return body

        excerpts = [
            f"[{h['payload'].get('source', 'unknown')}] {h['payload'].get('text', '')}"
            for h in hits
            if h.get("score", 0) >= self.valves.min_score and h.get("payload")
        ]
        if not excerpts:
            return body

        context = (
            "Relevant excerpts from the local knowledge base — use them to answer, "
            "and mention the source file in [brackets] when you do:\n\n"
            + "\n\n---\n\n".join(excerpts)
        )
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = f"{messages[0]['content']}\n\n{context}"
        else:
            messages.insert(0, {"role": "system", "content": context})
        body["messages"] = messages
        return body
