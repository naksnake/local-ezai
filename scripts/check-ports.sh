#!/usr/bin/env bash
# scripts/check-ports.sh
# ─────────────────────────────────────────────────────────────────────────────
# Auto-resolve host port conflicts before starting the stack.
#
# For every published port: if something else on this machine already owns
# it (and it isn't one of our own containers from a previous run), pick the
# next free port at <default + 4000> and persist the override in .env so it
# stays stable across restarts. Run automatically by make up / up-cpu / up-n97.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

YELLOW='\033[1;33m'; GREEN='\033[0;32m'; NC='\033[0m'

touch .env
set -a; source .env; set +a

OUR_CONTAINERS="openwebui litellm vllm embed-server qdrant searxng mcpo monitor"

VARS=(OPENWEBUI_PORT LITELLM_PORT QDRANT_PORT LLM_PORT EMBED_PORT SEARXNG_PORT MCPO_PORT MONITOR_PORT)
DEFAULTS=(3000 4000 6333 8000 8001 8090 8200 8888)

port_busy() {
    if command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${1}\$"
        return
    fi
    # fallback without iproute2: a TCP connect succeeds only if a listener
    # is accepting on the port
    if (exec 3<>"/dev/tcp/127.0.0.1/${1}") 2>/dev/null; then
        exec 3>&- 3<&- 2>/dev/null || true
        return 0
    fi
    return 1
}

owned_by_us() {
    local owner
    owner=$(docker ps --filter "publish=${1}" --format '{{.Names}}' 2>/dev/null | head -1)
    [[ -n "$owner" ]] && grep -qw "$owner" <<< "$OUR_CONTAINERS"
}

next_free() {
    local p=$1
    while port_busy "$p"; do p=$((p + 1)); done
    echo "$p"
}

changed=0
for i in "${!VARS[@]}"; do
    var="${VARS[$i]}"
    port="${!var:-${DEFAULTS[$i]}}"
    if port_busy "$port" && ! owned_by_us "$port"; then
        new_port=$(next_free $((DEFAULTS[$i] + 4000)))
        echo -e "${YELLOW}⚠ Port ${port} (${var}) is taken by another service — using ${new_port} instead.${NC}"
        if grep -q "^${var}=" .env; then
            sed -i "s|^${var}=.*|${var}=${new_port}|" .env
        else
            echo "${var}=${new_port}" >> .env
        fi
        changed=1
    fi
done

if [[ $changed -eq 1 ]]; then
    echo -e "${GREEN}→ New ports saved to .env — they stay stable on every future run.${NC}"
fi
