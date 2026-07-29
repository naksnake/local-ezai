#!/usr/bin/env bash
# scripts/install-autorag.sh
# ─────────────────────────────────────────────────────────────────────────────
# Installs tools/auto-rag-filter.py into OpenWebUI as a GLOBAL filter
# function, directly in its database — no UI steps needed.
#
# Requires: the openwebui container running, and at least one account
# created (the first signup becomes admin and owns the function).
#
# Usage: make install-autorag   (or: bash scripts/install-autorag.sh)
# Re-run any time to update the filter to the latest version in the repo.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

if ! docker ps --format '{{.Names}}' | grep -qx openwebui; then
    echo "❌ openwebui container is not running — start the stack first."
    exit 1
fi

docker cp tools/auto-rag-filter.py openwebui:/tmp/auto-rag-filter.py

docker exec openwebui python3 - <<'PY'
import json, sqlite3, time

DB = "/app/backend/data/webui.db"
content = open("/tmp/auto-rag-filter.py").read()

db = sqlite3.connect(DB)
cols = [r[1] for r in db.execute("PRAGMA table_info(function)")]
if not cols:
    raise SystemExit("function table not found — unexpected OpenWebUI version; "
                     "paste tools/auto-rag-filter.py via Admin Panel > Functions instead.")

admin = db.execute("SELECT id FROM user WHERE role='admin' LIMIT 1").fetchone()
if not admin:
    raise SystemExit("no admin user yet — open the WebUI, create the first "
                     "account, then re-run this script.")

now = int(time.time())
row = {
    "id": "auto_rag_knowledge_base",
    "user_id": admin[0],
    "name": "Auto RAG (Knowledge Base)",
    "type": "filter",
    "content": content,
    "meta": json.dumps({
        "description": "Automatically searches the Qdrant knowledge base and "
                       "injects matching excerpts into every message.",
        "manifest": {},
    }),
    "valves": json.dumps({}),
    "is_active": 1,
    "is_global": 1,
    "updated_at": now,
    "created_at": now,
}
row = {k: v for k, v in row.items() if k in cols}

db.execute("DELETE FROM function WHERE id = 'auto_rag_knowledge_base'")
db.execute(
    f"INSERT INTO function ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
    list(row.values()),
)
db.commit()
print(f"installed function 'auto_rag_knowledge_base' (columns: {', '.join(row)})")
PY

# OpenWebUI caches functions in memory — restart to load it
docker restart openwebui > /dev/null
echo ""
echo "✅ Auto-RAG installed as a global filter. OpenWebUI is restarting (~20s)."
echo "   Every chat now automatically searches the knowledge base — no tools needed."
echo "   Manage it under Admin Panel → Functions (valves: top_k, min_score, collection)."
