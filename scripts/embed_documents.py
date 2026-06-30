#!/usr/bin/env python3
"""
scripts/embed_documents.py
─────────────────────────────────────────────────────────────────────────────
Embed text documents into Qdrant for RAG (Retrieval-Augmented Generation).

Usage:
    python3 embed_documents.py --input-dir ~/documents

Options:
    --input-dir    Folder containing .txt or .md files  (required)
    --qdrant-url   Qdrant server URL                    (default: http://localhost:6333)
    --embed-url    Embedding server URL                 (default: http://localhost:8001/v1)
    --collection   Qdrant collection name               (default: my-knowledge-base)
    --batch-size   Chunks per embedding batch           (default: 16)
    --chunk-size   Words per chunk                      (default: 400)
    --overlap      Word overlap between chunks          (default: 50)
─────────────────────────────────────────────────────────────────────────────
"""
import argparse
import sys
from pathlib import Path

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct


# ── Helpers ───────────────────────────────────────────────────────────────────
def get_embeddings(texts: list[str], embed_url: str) -> list[list[float]]:
    """Send texts to the embedding server and return vectors."""
    resp = requests.post(
        f"{embed_url}/embeddings",
        json={"model": "nomic-embed-text-v1.5", "input": texts},
        timeout=(5, 60),
    )
    resp.raise_for_status()
    return [item["embedding"] for item in resp.json()["data"]]


def chunk_text(text: str, chunk_size: int = 400, overlap: int = 50) -> list[str]:
    """Split text into overlapping word-level chunks."""
    words = text.split()
    chunks = []
    step = max(1, chunk_size - overlap)
    for i in range(0, len(words), step):
        chunk = " ".join(words[i : i + chunk_size])
        if chunk.strip():
            chunks.append(chunk)
    return chunks


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Embed documents into Qdrant for RAG search"
    )
    parser.add_argument("--input-dir",   required=True,   help="Folder with .txt/.md files")
    parser.add_argument("--qdrant-url",  default="http://localhost:6333")
    parser.add_argument("--embed-url",   default="http://localhost:8001/v1")
    parser.add_argument("--collection",  default="my-knowledge-base")
    parser.add_argument("--batch-size",  type=int, default=16)
    parser.add_argument("--chunk-size",  type=int, default=400)
    parser.add_argument("--overlap",     type=int, default=50)
    args = parser.parse_args()

    if args.overlap >= args.chunk_size:
        print(f"[ERROR] --overlap ({args.overlap}) must be less than --chunk-size ({args.chunk_size})")
        sys.exit(1)

    input_path = Path(args.input_dir)
    if not input_path.exists():
        print(f"[ERROR] Input directory not found: {args.input_dir}")
        sys.exit(1)

    # Find all text files
    files = sorted(
        list(input_path.glob("**/*.txt")) + list(input_path.glob("**/*.md"))
    )
    if not files:
        print(f"[ERROR] No .txt or .md files found in {args.input_dir}")
        sys.exit(1)

    print(f"Found {len(files)} files in {args.input_dir}")

    # ── Connect to services ───────────────────────────────────────────────────
    try:
        client = QdrantClient(url=args.qdrant_url, timeout=10)
        client.get_collections()  # test connection
    except Exception as e:
        print(f"[ERROR] Cannot connect to Qdrant at {args.qdrant_url}: {e}")
        sys.exit(1)

    try:
        get_embeddings(["connection test"], args.embed_url)
    except Exception as e:
        print(f"[ERROR] Cannot connect to embed server at {args.embed_url}: {e}")
        sys.exit(1)

    # ── Create collection if needed ───────────────────────────────────────────
    current_dim = len(get_embeddings(["dim test"], args.embed_url)[0])
    existing_names = {c.name for c in client.get_collections().collections}
    if args.collection not in existing_names:
        client.create_collection(
            args.collection,
            vectors_config=VectorParams(size=current_dim, distance=Distance.COSINE),
        )
        print(f"Created Qdrant collection '{args.collection}' (dim={current_dim})")
    else:
        coll_info = client.get_collection(args.collection)
        stored_dim = coll_info.config.params.vectors.size
        if stored_dim != current_dim:
            print(
                f"[ERROR] Collection '{args.collection}' was built with dim={stored_dim} "
                f"but current embed model returns dim={current_dim}. "
                f"Delete the collection or use a different --collection name."
            )
            sys.exit(1)
        print(f"Using existing collection '{args.collection}' (dim={stored_dim})")

    # ── Chunk all documents ───────────────────────────────────────────────────
    all_chunks: list[str] = []
    all_meta:   list[dict] = []

    for filepath in files:
        try:
            text = filepath.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            print(f"  [SKIP] {filepath.name}: {e}")
            continue

        chunks = chunk_text(text, args.chunk_size, args.overlap)
        for idx, chunk in enumerate(chunks):
            all_chunks.append(chunk)
            all_meta.append({
                "source":      filepath.name,
                "source_path": str(filepath),
                "chunk_index": idx,
                "text":        chunk,
            })

        print(f"  {filepath.name}: {len(chunks)} chunks")

    print(f"\nTotal chunks to embed: {len(all_chunks)}")

    # ── Embed and upload in batches ───────────────────────────────────────────
    point_id = 0
    for i in range(0, len(all_chunks), args.batch_size):
        batch_texts = all_chunks[i : i + args.batch_size]
        batch_meta  = all_meta[i : i + args.batch_size]

        vectors = get_embeddings(batch_texts, args.embed_url)
        if len(vectors) != len(batch_texts):
            print(f"\n[ERROR] Embed server returned {len(vectors)} vectors for {len(batch_texts)} texts")
            sys.exit(1)

        points = [
            PointStruct(id=point_id + j, vector=vec, payload=meta)
            for j, (vec, meta) in enumerate(zip(vectors, batch_meta))
        ]
        try:
            client.upsert(collection_name=args.collection, points=points)
        except Exception as e:
            print(f"\n[ERROR] Qdrant upsert failed at batch starting index {i}: {e}")
            sys.exit(1)
        point_id += len(batch_texts)

        done = min(i + args.batch_size, len(all_chunks))
        print(f"  Embedded {done} / {len(all_chunks)} chunks", end="\r")

    print(f"\n\n[DONE] {point_id} chunks stored in collection '{args.collection}'")
    print(f"       Qdrant: {args.qdrant_url}/collections/{args.collection}")


if __name__ == "__main__":
    main()
