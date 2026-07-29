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

# Disable OpenWebUI's BUILT-IN knowledge tools (search_knowledge_bases etc.)
# for the local chat models: they search OpenWebUI's internal store — always
# empty here — and small models keep picking them over the real KB, then
# report "no results". meta.builtinTools.knowledge=false removes them.
[[ -f .env ]] && { set -a; source .env; set +a; }
CHAT_MODELS="${N97_MODEL_NAME:-qwen2.5-3b} ${CPU_CHAT_MODEL_NAME:-qwen2.5-1.5b} ${CHAT_MODEL_NAME:-qwen2.5-7b} qwen2.5-3b qwen2.5-1.5b"

docker exec -e CHAT_MODELS="$CHAT_MODELS" openwebui python3 - <<'PY'
import json, os, sqlite3, time

DB = "/app/backend/data/webui.db"
db = sqlite3.connect(DB)
cols = [r[1] for r in db.execute("PRAGMA table_info(model)")]
if not cols:
    raise SystemExit("model table not found — disable built-in knowledge tools "
                     "manually: Admin Panel > Models > edit model > Built-in Tools.")

admin = db.execute("SELECT id FROM user WHERE role='admin' LIMIT 1").fetchone()
now = int(time.time())
models = list(dict.fromkeys(os.environ["CHAT_MODELS"].split()))

for mid in models:
    row = db.execute("SELECT meta FROM model WHERE id = ?", (mid,)).fetchone()
    if row:
        meta = json.loads(row[0] or "{}")
        meta.setdefault("builtinTools", {})["knowledge"] = False
        db.execute("UPDATE model SET meta = ?, updated_at = ? WHERE id = ?",
                   (json.dumps(meta), now, mid))
        print(f"updated existing model override: {mid}")
    else:
        new = {
            "id": mid, "user_id": admin[0] if admin else None,
            "base_model_id": None, "name": mid,
            "params": json.dumps({}),
            "meta": json.dumps({"builtinTools": {"knowledge": False}}),
            "access_control": None, "is_active": 1,
            "updated_at": now, "created_at": now,
        }
        new = {k: v for k, v in new.items() if k in cols}
        db.execute(
            f"INSERT INTO model ({','.join(new)}) VALUES ({','.join('?' * len(new))})",
            list(new.values()),
        )
        print(f"created model override: {mid} (built-in knowledge tools off)")

db.commit()
PY

# OpenWebUI caches functions and model meta in memory — restart to load
docker restart openwebui > /dev/null
echo ""
echo "✅ Auto-RAG installed as a global filter, and OpenWebUI's built-in"
echo "   knowledge tools are disabled for the local chat models."
echo "   OpenWebUI is restarting (~20s). Every chat now searches YOUR"
echo "   knowledge base automatically — no tools needed."
echo "   Manage the filter under Admin Panel → Functions (valves: top_k, min_score)."
