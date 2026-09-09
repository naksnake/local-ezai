#!/usr/bin/env bash
# scripts/register-orchestrator.sh
# ─────────────────────────────────────────────────────────────────────────────
# Installs the "Local-EZAI Orchestrator" persona into OpenWebUI — a curated
# model on the role-orchestrator alias with the system preset from
# config/prompts/orchestrator.md and the SWE Tool Server (mcpo :8200/swe)
# enabled for this persona only. Directly in OpenWebUI's database, like
# install-autorag.sh — no UI steps. Plain chat models are untouched.
#
# Requires: the openwebui container running, and at least one account
# created (the first signup becomes admin and owns the persona).
#
# Usage: make orchestrator   (or: bash scripts/register-orchestrator.sh)
# Re-run any time to refresh the prompt. Remove with:
#   docker exec openwebui python3 -c "import sqlite3; db=sqlite3.connect('/app/backend/data/webui.db'); db.execute(\"DELETE FROM model WHERE id='local-ezai-orchestrator'\"); db.commit()"
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

if ! docker ps --format '{{.Names}}' | grep -qx openwebui; then
    echo "❌ openwebui container is not running — start the stack first."
    exit 1
fi
for f in scripts/openwebui_orchestrator.py config/prompts/orchestrator.md; do
    if [[ ! -f "$f" ]]; then
        echo "❌ $f not found — run from the repo root after 'git pull'."
        exit 1
    fi
done

BASE_MODEL="${ORCHESTRATOR_BASE_MODEL:-role-orchestrator}"

echo "→ [1/3] Copying the installer and the prompt into the openwebui container..."
docker cp scripts/openwebui_orchestrator.py openwebui:/tmp/openwebui_orchestrator.py
docker cp config/prompts/orchestrator.md openwebui:/tmp/orchestrator.md
echo "→ [2/3] Installing the persona into webui.db (base model: ${BASE_MODEL})..."
docker exec openwebui python3 /tmp/openwebui_orchestrator.py \
    /app/backend/data/webui.db /tmp/orchestrator.md "${BASE_MODEL}"

# OpenWebUI caches model meta in memory — restart to load
echo "→ [3/3] Restarting openwebui to load the persona..."
docker compose restart openwebui >/dev/null
echo "✅ Pick 'Local-EZAI Orchestrator' in the model dropdown. It plans and starts"
echo "   work on registered projects (local-ezai project add <path>) and never approves."
