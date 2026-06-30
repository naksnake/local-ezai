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

pass=0; fail=0; warn=0

echo ""
echo -e "${BOLD}${CYAN}══════════════════════════════════════════${NC}"
echo -e "${BOLD}${CYAN}       AI Service Health Check             ${NC}"
echo -e "${BOLD}${CYAN}══════════════════════════════════════════${NC}"
echo ""

# ── Check function ─────────────────────────────────────────────────────────
chk() {
    local label=$1 url=$2 pattern=$3 hdr=${4:-}

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
        echo -e "      ${YELLOW}→ check logs: docker compose logs $(echo "$label" | tr '[:upper:]' '[:lower:]' | tr ' ' '-')${NC}"
        ((fail++))
    fi
}

chk "vLLM inference"   "http://localhost:8000/health"                     "healthy"
chk "Embed server"     "http://localhost:8001/health"                     "healthy"
chk "Qdrant"           "http://localhost:6333/healthz"                    "qdrant"
chk "SearXNG"          "http://localhost:8090/search?q=test&format=json"  '"results"'
chk "mcpo tools"       "http://localhost:8200/openapi.json"               "openapi"   "Authorization: Bearer ${MCP_KEY}"
chk "LiteLLM proxy"    "http://localhost:4000/models"                     '"data"'    "Authorization: Bearer ${LITELLM_KEY}"
chk "OpenWebUI"        "http://localhost:3000"                            "Open WebUI"
chk "Monitor"          "http://localhost:8888/api/status"                 "server_time"

echo ""
echo -e "  ${BOLD}Passed: ${GREEN}${pass}${NC}  |  Failed: ${RED}${fail}${NC}"
echo -e "${CYAN}══════════════════════════════════════════${NC}"
echo ""

if [[ $fail -eq 0 ]]; then
    echo -e "  ${GREEN}All services healthy!${NC}  →  http://localhost:3000"
else
    echo -e "  ${YELLOW}Tip: vLLM takes 2–5 minutes to load the model.${NC}"
    echo    "  Run again after waiting, or check logs:"
    echo    "    docker compose logs vllm | tail -30"
fi
echo ""

exit $fail
