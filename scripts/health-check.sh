#!/usr/bin/env bash
# scripts/health-check.sh
# Verifies all 7 services are healthy.
# Usage: bash scripts/health-check.sh  OR  make health

set -uo pipefail

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

# Load API keys from .env if present
if [[ -f .env ]]; then
    set -a; source .env; set +a
fi

LITELLM_KEY="${LITELLM_MASTER_KEY:-sk-ai-service-2024}"
MCP_KEY="${MCP_API_KEY:-local-tools-key}"

# Host ports (override in .env if they clash with other services)
LLM_PORT="${LLM_PORT:-8000}"
EMBED_PORT="${EMBED_PORT:-8001}"
QDRANT_PORT="${QDRANT_PORT:-6333}"
SEARXNG_PORT="${SEARXNG_PORT:-8092}"
MCPO_PORT="${MCPO_PORT:-8200}"
LITELLM_PORT="${LITELLM_PORT:-4000}"
OPENWEBUI_PORT="${OPENWEBUI_PORT:-3000}"
MONITOR_PORT="${MONITOR_PORT:-8888}"

pass=0; fail=0; warn=0

echo ""
echo -e "${BOLD}${CYAN}══════════════════════════════════════════${NC}"
echo -e "${BOLD}${CYAN}       AI Service Health Check             ${NC}"
echo -e "${BOLD}${CYAN}══════════════════════════════════════════${NC}"
echo ""

# ── Check function ─────────────────────────────────────────────────────────
chk() {
    local label=$1 svc=$2 url=$3 pattern=$4 hdr=${5:-}

    local response
    if [[ -n "$hdr" ]]; then
        response=$(curl -sf -H "$hdr" "$url" 2>/dev/null || echo "")
    else
        response=$(curl -sf "$url" 2>/dev/null || echo "")
    fi

    if echo "$response" | grep -q "$pattern"; then
        echo -e "  ${GREEN}✅${NC}  $label"
        ((pass++))
    else
        echo -e "  ${RED}❌${NC}  $label"
        echo -e "      ${YELLOW}→ check logs: docker compose logs $svc${NC}"
        ((fail++))
    fi
}

# Status-code-only check — vLLM's /health returns an empty 200 body, and the
# llama.cpp server used by the N97 profile returns {"status":"ok"}, so a body
# pattern can't cover both.
chk_code() {
    local label=$1 url=$2

    if curl -sf -o /dev/null "$url" 2>/dev/null; then
        echo -e "  ${GREEN}✅${NC}  $label"
        ((pass++))
    else
        echo -e "  ${RED}❌${NC}  $label"
        echo -e "      ${YELLOW}→ check logs: docker compose logs vllm${NC}"
        ((fail++))
    fi
}

chk_code "LLM inference (vLLM / llama.cpp)" "http://localhost:${LLM_PORT}/health"
chk "Embed server"     embed-server "http://localhost:${EMBED_PORT}/health"                     "healthy"
chk "Qdrant"           qdrant       "http://localhost:${QDRANT_PORT}/healthz"                   "passed"
chk "SearXNG"          searxng      "http://localhost:${SEARXNG_PORT}/search?q=test&format=json" '"results"'
chk "mcpo tools"       mcpo         "http://localhost:${MCPO_PORT}/openapi.json"                "openapi"   "Authorization: Bearer ${MCP_KEY}"
chk "LiteLLM proxy"    litellm      "http://localhost:${LITELLM_PORT}/models"                   '"data"'    "Authorization: Bearer ${LITELLM_KEY}"
chk "OpenWebUI"        openwebui    "http://localhost:${OPENWEBUI_PORT}"                        "Open WebUI"
chk "Monitor"          monitor      "http://localhost:${MONITOR_PORT}/api/status"               "server_time" "Authorization: Bearer ${MCP_KEY}"

echo ""
echo -e "  ${BOLD}Passed: ${GREEN}${pass}${NC}  |  Failed: ${RED}${fail}${NC}"
echo -e "${CYAN}══════════════════════════════════════════${NC}"
echo ""

if [[ $fail -eq 0 ]]; then
    echo -e "  ${GREEN}All services healthy!${NC}  →  http://localhost:${OPENWEBUI_PORT}"
else
    echo -e "  ${YELLOW}Tip: vLLM takes 2–5 minutes to load the model.${NC}"
    echo    "  Run again after waiting, or check logs:"
    echo    "    docker compose logs vllm | tail -30"
fi
echo ""

exit $fail
