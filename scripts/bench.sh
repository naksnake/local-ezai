#!/usr/bin/env bash
# scripts/bench.sh — one-question benchmark against the local LLM.
# Prints prompt-processing and generation speed in tokens/second.
# Usage: make bench   (or: bash scripts/bench.sh)
set -euo pipefail

[[ -f .env ]] && { set -a; source .env; set +a; }
PORT="${LLM_PORT:-8000}"
MODEL="${N97_MODEL_NAME:-${CPU_CHAT_MODEL_NAME:-qwen2.5-1.5b}}"

echo "→ POST http://localhost:${PORT}/v1/chat/completions  (model: ${MODEL}, ~120 tokens)"
echo "  Generating..."

START=$(date +%s.%N)
RESP=$(curl -sf "http://localhost:${PORT}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{"model":"'"${MODEL}"'","messages":[{"role":"user","content":"Count from one to thirty in English words, comma separated."}],"max_tokens":120}') \
    || { echo "❌ No response from the LLM server on port ${PORT} — run 'make health'"; exit 1; }
END=$(date +%s.%N)

RESP="$RESP" ELAPSED=$(awk "BEGIN{print $END-$START}") python3 - <<'PY'
import json, os

r = json.loads(os.environ["RESP"])
elapsed = float(os.environ["ELAPSED"])

text = r["choices"][0]["message"]["content"].strip().replace("\n", " ")
print(f"\nresponse: {text[:100]}{'...' if len(text) > 100 else ''}\n")

t = r.get("timings")          # llama.cpp includes this; vLLM does not
u = r.get("usage") or {}
if t:
    print(f"prompt processing : {t['prompt_n']:>4} tokens @ {t['prompt_per_second']:6.1f} tok/s")
    print(f"generation        : {t['predicted_n']:>4} tokens @ {t['predicted_per_second']:6.1f} tok/s")
else:
    ct = u.get("completion_tokens")
    if ct:
        print(f"generation ≈ {ct/elapsed:.1f} tok/s ({ct} tokens in {elapsed:.1f}s, incl. prompt)")
print(f"\ntotal round-trip  : {elapsed:.1f}s")
PY
