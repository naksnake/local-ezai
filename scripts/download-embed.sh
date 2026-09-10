#!/usr/bin/env bash
# scripts/download-embed.sh
# ─────────────────────────────────────────────────────────────────────────────
# Download the RAG embedding model (and the remote-code repo it loads) into
# the HuggingFace cache — needed by every profile. Skips quickly when both
# are already present. Runs the HF CLI inside a throwaway container: no host
# Python, resumable. Used by `make up*`, `make download-embed` and the first
# run (`local-ezai setup`). The engine's models are NOT handled here — they
# come from the model registry (`make bootstrap`, `local-ezai model …`).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ -f .env ]]; then set -a; source .env; set +a; fi

CACHE_DIR="${MODELS_DIR:-$PWD/models/hf-cache}"
EMBED_MODEL="${CPU_EMBED_MODEL:-nomic-ai/nomic-embed-text-v1.5}"
# nomic-embed loads its custom modeling code (trust_remote_code) from this
# separate repo at runtime — the offline cache must contain it too
EMBED_CODE_REPO="${EMBED_CODE_REPO:-nomic-ai/nomic-bert-2048}"

snapshot_dir() { echo "$CACHE_DIR/models--${1//\//--}/snapshots"; }

if [[ -d "$(snapshot_dir "$EMBED_MODEL")" && -d "$(snapshot_dir "$EMBED_CODE_REPO")" ]]; then
    echo "embedding model present: $EMBED_MODEL ($CACHE_DIR)"
    exit 0
fi

mkdir -p "$CACHE_DIR"
echo "Downloading the embedding model $EMBED_MODEL (~500 MB) into $CACHE_DIR — re-running resumes."
TTY_FLAG=""; [ -t 1 ] && TTY_FLAG="-t"
docker run --rm $TTY_FLAG \
    -v "$CACHE_DIR":/hf-cache \
    -e HF_HUB_CACHE=/hf-cache \
    -e HF_TOKEN \
    python:3.11-slim \
    bash -c "pip install -q 'huggingface_hub[cli]' && \
             hf download $EMBED_MODEL && \
             hf download $EMBED_CODE_REPO"
echo "embedding model downloaded: $EMBED_MODEL"
