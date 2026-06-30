#!/usr/bin/env bash
# scripts/download-models.sh
# ─────────────────────────────────────────────────────────────────────────────
# Downloads AI models from HuggingFace to the local cache.
# Run this BEFORE starting the Docker stack for the first time.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

CACHE_DIR="${HOME}/ai-models/hf-cache"
mkdir -p "$CACHE_DIR"

# Activate Python venv (where huggingface_hub is installed)
if [[ -f "$HOME/ai-env/bin/activate" ]]; then
    source "$HOME/ai-env/bin/activate"
else
    echo -e "${YELLOW}[WARN]${NC} ~/ai-env not found. Run setup.sh first."
    exit 1
fi

echo ""
echo -e "${CYAN}══ Downloading AI Models ══${NC}"
echo "  Cache directory: $CACHE_DIR"
echo ""

# ── Main chat model ────────────────────────────────────────────────────────────
echo -e "${CYAN}[1/2]${NC} Downloading Qwen2.5-7B-Instruct (main chat model)"
echo "      Size: ~15 GB | Good at: chat, tool use, code"
echo ""
huggingface-cli download Qwen/Qwen2.5-7B-Instruct \
    --cache-dir "$CACHE_DIR"
echo -e "${GREEN}[OK]${NC}  Qwen2.5-7B downloaded"
echo ""

# ── Embedding model ────────────────────────────────────────────────────────────
echo -e "${CYAN}[2/2]${NC} Downloading nomic-embed-text-v1.5 (embedding model)"
echo "      Size: ~500 MB | Used for: RAG document search"
echo ""
huggingface-cli download nomic-ai/nomic-embed-text-v1.5 \
    --cache-dir "$CACHE_DIR"
echo -e "${GREEN}[OK]${NC}  nomic-embed-text downloaded"
echo ""

# ── Summary ────────────────────────────────────────────────────────────────────
echo -e "${GREEN}══ All models downloaded ══${NC}"
echo ""
echo "  Cache location: $CACHE_DIR"
echo "  Total size: $(du -sh "$CACHE_DIR" | cut -f1)"
echo ""
echo "  Next: make build && make pull && make up"
echo ""

# ── Optional: smaller model for CPU-only or low VRAM ──────────────────────────
echo "  Optional smaller models:"
echo "    3B (4GB VRAM):  huggingface-cli download Qwen/Qwen2.5-3B-Instruct --cache-dir $CACHE_DIR"
echo "    0.5B (CPU OK):  huggingface-cli download Qwen/Qwen2.5-0.5B-Instruct --cache-dir $CACHE_DIR"
echo ""
