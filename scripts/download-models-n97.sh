#!/usr/bin/env bash
# scripts/download-models-n97.sh
# ─────────────────────────────────────────────────────────────────────────────
# Downloads the small quantized model set for Intel N97 / low-power CPU boxes.
# Run this BEFORE `make up-n97` for the first time.
#
#   Chat model:  Qwen2.5-3B-Instruct  Q4_K_M GGUF  (~2 GB, llama.cpp)
#   Embeddings:  nomic-embed-text-v1.5              (~500 MB, embed-server)
#
# Runs the HuggingFace CLI inside a throwaway container — no host Python,
# no venv. Downloads are resumable: re-run after an interruption.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'

# Pick up HF_TOKEN / MODELS_DIR / N97_* overrides when run outside make
if [[ -f .env ]]; then set -a; source .env; set +a; fi

GGUF_DIR="${N97_GGUF_DIR:-$PWD/models/gguf}"
CACHE_DIR="${MODELS_DIR:-$PWD/models/hf-cache}"
GGUF_REPO="${N97_GGUF_REPO:-Qwen/Qwen2.5-3B-Instruct-GGUF}"
GGUF_FILE="${N97_MODEL_FILE:-qwen2.5-3b-instruct-q4_k_m.gguf}"
EMBED_MODEL="${CPU_EMBED_MODEL:-nomic-ai/nomic-embed-text-v1.5}"

mkdir -p "$GGUF_DIR" "$CACHE_DIR"

echo ""
echo -e "${CYAN}══ Downloading AI Models (N97 / CPU profile) ══${NC}"
echo "  GGUF directory:  $GGUF_DIR"
echo "  HF cache:        $CACHE_DIR"
echo ""
echo -e "${CYAN}[1/2]${NC} ${GGUF_REPO} → ${GGUF_FILE}  (~2 GB, 4-bit quantized)"
echo -e "${CYAN}[2/2]${NC} ${EMBED_MODEL}  (~500 MB, RAG document search)"
echo ""

docker run --rm \
    -v "$GGUF_DIR":/gguf \
    -v "$CACHE_DIR":/hf-cache \
    -e HF_HUB_CACHE=/hf-cache \
    -e HF_TOKEN \
    python:3.11-slim \
    bash -c "pip install -q 'huggingface_hub[cli]' && \
             hf download $GGUF_REPO $GGUF_FILE --local-dir /gguf && \
             hf download $EMBED_MODEL && \
             hf download nomic-ai/nomic-bert-2048"
             # ^ nomic-embed's trust_remote_code lives in this separate repo

echo ""
echo -e "${GREEN}══ All models downloaded ══${NC}"
echo ""
echo "  GGUF location:  $GGUF_DIR/$GGUF_FILE"
echo "  Next: make build && make pull-n97 && make up-n97"
echo ""

# ── Alternatives ───────────────────────────────────────────────────────────────
echo "  Alternative chat models (set N97_GGUF_REPO / N97_MODEL_FILE / N97_MODEL_NAME in .env):"
echo "    1.5B, Apache-2.0, faster (~2x):"
echo "      N97_GGUF_REPO=Qwen/Qwen2.5-1.5B-Instruct-GGUF"
echo "      N97_MODEL_FILE=qwen2.5-1.5b-instruct-q4_k_m.gguf"
echo "      N97_MODEL_NAME=qwen2.5-1.5b"
echo "  Note: Qwen2.5-3B is under the Qwen Research License (non-commercial use)."
echo "        For commercial deployments prefer the 1.5B (Apache-2.0)."
echo ""
