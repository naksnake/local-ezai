"""
embed-server/server.py
─────────────────────────────────────────────────────────────────────
OpenAI-compatible embedding server.
Runs nomic-embed-text-v1.5 on CPU from the local HuggingFace cache.
Endpoint: POST /v1/embeddings  (same format as OpenAI Embeddings API)
"""
from typing import List, Union

from fastapi import FastAPI
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer

app = FastAPI(title="Embed Server", version="1.0.0")

MODEL_ID = "nomic-embed-text-v1.5"

# ── Load model from the HuggingFace cache mounted at /root/.cache/huggingface
print("Loading nomic-embed-text-v1.5 from cache...", flush=True)
model = SentenceTransformer(
    "nomic-ai/nomic-embed-text-v1.5",
    trust_remote_code=True,
    device="cpu",
    cache_folder="/root/.cache/huggingface",
)
EMBEDDING_DIM = model.get_sentence_embedding_dimension()
print(f"Embedding model ready. Dimension: {EMBEDDING_DIM}", flush=True)


# ── Request / response schemas ───────────────────────────────────────────────
class EmbedRequest(BaseModel):
    input: Union[str, List[str]]
    model: str = MODEL_ID


# ── Endpoints ────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "healthy", "model": MODEL_ID, "dim": EMBEDDING_DIM}


@app.post("/v1/embeddings")
def embed(req: EmbedRequest):
    texts = [req.input] if isinstance(req.input, str) else req.input
    vectors = model.encode(texts, normalize_embeddings=True).tolist()
    total_tokens = sum(len(t.split()) for t in texts)
    return {
        "object": "list",
        "model": MODEL_ID,
        "data": [
            {"object": "embedding", "index": i, "embedding": vec}
            for i, vec in enumerate(vectors)
        ],
        "usage": {"prompt_tokens": total_tokens, "total_tokens": total_tokens},
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
