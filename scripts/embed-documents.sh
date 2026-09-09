#!/usr/bin/env bash
# scripts/embed-documents.sh
# ─────────────────────────────────────────────────────────────────────────────
# Embed every document under DOCUMENTS_DIR (default ./documents) into the
# Qdrant knowledge base — the one implementation `make embed` and the first
# run's RAG smoke share. Runs scripts/embed_documents.py in a throwaway
# container against the host ports (no host Python).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ -f .env ]]; then set -a; source .env; set +a; fi

DOCUMENTS="${DOCUMENTS_DIR:-$PWD/documents}"
COLLECTION="${RAG_COLLECTION:-my-knowledge-base}"
QDRANT_PORT="${QDRANT_PORT:-6333}"
EMBED_PORT="${EMBED_PORT:-8001}"

mkdir -p "$DOCUMENTS"
echo "Embedding documents from $DOCUMENTS into collection $COLLECTION..."
docker run --rm --network host \
    -v "$PWD/scripts":/scripts:ro \
    -v "$DOCUMENTS":/documents:ro \
    python:3.11-slim \
    bash -c "pip install -q qdrant-client requests pypdf && \
             python3 /scripts/embed_documents.py \
               --input-dir /documents \
               --qdrant-url http://localhost:${QDRANT_PORT} \
               --embed-url http://localhost:${EMBED_PORT}/v1 \
               --collection ${COLLECTION}"
