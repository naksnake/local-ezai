#!/usr/bin/env bash
# scripts/download-models.sh
# ─────────────────────────────────────────────────────────────────────────────
# Downloads AI models from HuggingFace into the project's model cache.
# Run this BEFORE starting the Docker stack for the first time.
#
# Runs the HuggingFace CLI inside a throwaway container — no host Python,
# no venv. Downloads are resumable: re-run after an interruption.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'

# Pick up HF_TOKEN / MODELS_DIR / CHAT_MODEL when run outside make
if [[ -f .env ]]; then set -a; source .env; set +a; fi

CACHE_DIR="${MODELS_DIR:-$PWD/models/hf-cache}"
CHAT_MODEL="${CHAT_MODEL:-Qwen/Qwen2.5-7B-Instruct}"
EMBED_MODEL="${CPU_EMBED_MODEL:-nomic-ai/nomic-embed-text-v1.5}"

mkdir -p "$CACHE_DIR"

echo ""
echo -e "${CYAN}══ Downloading AI Models ══${NC}"
echo "  Cache directory: $CACHE_DIR"
echo "  Chat model:      $CHAT_MODEL (~15 GB for the 7B default)"
echo "  Embed model:     $EMBED_MODEL (~500 MB)"
echo ""

docker run --rm \
    -v "$CACHE_DIR":/hf-cache \
    -e HF_HUB_CACHE=/hf-cache \
    -e HF_TOKEN \
    python:3.11-slim \
    bash -c "pip install -q 'huggingface_hub[cli]' && \
             hf download $CHAT_MODEL && \
             hf download $EMBED_MODEL && \
             hf download nomic-ai/nomic-bert-2048"
             # ^ nomic-embed's trust_remote_code lives in this separate repo

echo ""
echo -e "${GREEN}══ All models downloaded ══${NC}"
echo "  Cache location: $CACHE_DIR"
echo "  Total size: $(du -sh "$CACHE_DIR" | cut -f1)"
echo ""
echo "  Next: make build && make pull && make up"
echo ""
echo "  Optional smaller models (set CHAT_MODEL in .env, then re-run):"
echo "    3B (4GB VRAM):  CHAT_MODEL=Qwen/Qwen2.5-3B-Instruct"
echo "    0.5B (CPU OK):  CHAT_MODEL=Qwen/Qwen2.5-0.5B-Instruct"
echo ""
