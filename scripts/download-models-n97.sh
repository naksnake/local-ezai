#!/usr/bin/env bash
# scripts/download-models-n97.sh
# ─────────────────────────────────────────────────────────────────────────────
# Downloads the small quantized model set for Intel N97 / low-power CPU boxes.
# Run this BEFORE `make up-n97` for the first time.
#
#   Chat model:  Qwen2.5-3B-Instruct  Q4_K_M GGUF  (~2 GB, llama.cpp)
#   Embeddings:  nomic-embed-text-v1.5              (~500 MB, embed-server)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

GGUF_DIR="${N97_GGUF_DIR:-$HOME/ai-models/gguf}"
CACHE_DIR="${MODELS_DIR:-$HOME/ai-models/hf-cache}"
GGUF_REPO="${N97_GGUF_REPO:-Qwen/Qwen2.5-3B-Instruct-GGUF}"
GGUF_FILE="${N97_MODEL_FILE:-qwen2.5-3b-instruct-q4_k_m.gguf}"

mkdir -p "$GGUF_DIR" "$CACHE_DIR"

# Activate Python venv (where huggingface_hub is installed)
if [[ -f "$HOME/ai-env/bin/activate" ]]; then
    source "$HOME/ai-env/bin/activate"
else
    echo -e "${YELLOW}[WARN]${NC} ~/ai-env not found. Run setup.sh first."
    exit 1
fi

# huggingface_hub >= 0.34 renamed the CLI to `hf`; keep the old name working
HF_CLI="huggingface-cli"
command -v hf &>/dev/null && HF_CLI="hf"

echo ""
echo -e "${CYAN}══ Downloading AI Models (N97 / CPU profile) ══${NC}"
echo "  GGUF directory:  $GGUF_DIR"
echo "  HF cache:        $CACHE_DIR"
echo ""

# ── Main chat model (quantized GGUF for llama.cpp) ────────────────────────────
echo -e "${CYAN}[1/2]${NC} Downloading ${GGUF_REPO} → ${GGUF_FILE}"
echo "      Size: ~2 GB | 4-bit quantized, fits comfortably in 16 GB RAM"
echo ""
"$HF_CLI" download "$GGUF_REPO" "$GGUF_FILE" \
    --local-dir "$GGUF_DIR"
echo -e "${GREEN}[OK]${NC}  ${GGUF_FILE} downloaded"
echo ""

# ── Embedding model (same as the standard stack) ──────────────────────────────
echo -e "${CYAN}[2/2]${NC} Downloading nomic-embed-text-v1.5 (embedding model)"
echo "      Size: ~500 MB | Used for: RAG document search"
echo ""
"$HF_CLI" download nomic-ai/nomic-embed-text-v1.5 \
    --cache-dir "$CACHE_DIR"
echo -e "${GREEN}[OK]${NC}  nomic-embed-text downloaded"
echo ""

# ── Summary ────────────────────────────────────────────────────────────────────
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
