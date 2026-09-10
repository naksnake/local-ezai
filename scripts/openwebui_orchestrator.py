#!/usr/bin/env python3
"""Install / update the "Local-EZAI Orchestrator" persona in OpenWebUI
(V1 P3, PR-14, ADR-029; docs/OPENWEBUI_INTEGRATION.md §3).

Writes one row into OpenWebUI's ``model`` table (the same mechanism
``scripts/install-autorag.sh`` uses for the Auto-RAG filter): a curated
model entry on top of the ``role-orchestrator`` LiteLLM alias, with the
system preset read from ``config/prompts/orchestrator.md`` and the SWE Tool
Server bound to this persona only. Idempotent — re-run to refresh the
prompt. Plain chat models are never touched.

Runs INSIDE the openwebui container (python3 + sqlite3 are there; nothing
else is needed) — ``scripts/register-orchestrator.sh`` / ``make orchestrator``
copy this file and the prompt in and call::

    python3 openwebui_orchestrator.py /app/backend/data/webui.db orchestrator.md

The pure functions (``extract_prompt``, ``build_model_row``, ``upsert``) are
exercised by agentd's test suite against a sqlite file with OpenWebUI's
column layout.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

MODEL_ID = "local-ezai-orchestrator"
MODEL_NAME = "Local-EZAI Orchestrator"
DEFAULT_BASE_MODEL = "role-orchestrator"
TOOL_SERVER_ID = "swe"
DESCRIPTION = ("Plans and starts autonomous software-engineering work on registered projects "
               "and explains the platform (runs, models, governance queue). Starts and inspects "
               "— never approves, merges, activates or rolls back.")
SUGGESTIONS = [
    {"title": ["Which projects", "can you work on?"], "content": "Which projects can you work on?"},
    {"title": ["Plan a change", "before running it"],
     "content": "Plan this change for me, then wait for my confirmation: "},
    {"title": ["Explain the models", "behind the agents"],
     "content": "Which model serves the coder role, and why?"},
    {"title": ["What is waiting", "for approval?"], "content": "What is in the governance queue?"},
]
_FENCE = re.compile(r"```text\s*\n(.*?)\n```", re.DOTALL)


def extract_prompt(markdown: str) -> str:
    """The single ```text fenced block of the prompt file."""
    blocks = _FENCE.findall(markdown)
    if len(blocks) != 1:
        raise ValueError(f"expected exactly one ```text block in the prompt file, "
                         f"found {len(blocks)}")
    return blocks[0].strip() + "\n"


def build_model_row(prompt: str, *, admin_id: str | None, now: int,
                    base_model: str = DEFAULT_BASE_MODEL,
                    tool_server_id: str = TOOL_SERVER_ID) -> dict[str, Any]:
    """OpenWebUI ``model`` row: the persona over the role alias, the preset
    as system prompt, native tool calling, the SWE tool server bound (as a
    global tool-server tool id), OpenWebUI's own knowledge builtins off
    (they search an empty store), public to every user."""
    meta = {
        "profile_image_url": "/static/favicon.png",
        "description": DESCRIPTION,
        "capabilities": {"vision": False, "usage": True, "citations": True},
        "suggestion_prompts": SUGGESTIONS,
        "tags": [{"name": "local-ezai"}, {"name": "orchestrator"}],
        "toolIds": [f"server:{tool_server_id}"],
        "builtinTools": {"knowledge": False},
    }
    return {
        "id": MODEL_ID, "user_id": admin_id, "base_model_id": base_model, "name": MODEL_NAME,
        "params": json.dumps({"system": prompt, "function_calling": "native"}),
        "meta": json.dumps(meta), "access_control": None, "is_active": 1,
        "updated_at": now, "created_at": now,
    }


def upsert(db_path: Path, prompt_path: Path, *, base_model: str = DEFAULT_BASE_MODEL,
           now: int | None = None) -> str:
    """Create or refresh the persona row. Returns a one-line summary; raises
    ``SystemExit`` with the fix when OpenWebUI is not ready for it."""
    if not Path(db_path).is_file():
        raise SystemExit(f"❌ {db_path} does not exist — unexpected OpenWebUI layout.")
    prompt = extract_prompt(Path(prompt_path).read_text(encoding="utf-8"))
    db = sqlite3.connect(str(db_path))
    try:
        cols = [r[1] for r in db.execute("PRAGMA table_info(model)")]
        if not cols:
            raise SystemExit("❌ model table not found — create the persona manually: Workspace > "
                             "Models > + New (see config/prompts/orchestrator.md).")
        admin = (db.execute("SELECT id FROM user WHERE role='admin' LIMIT 1").fetchone()
                 or db.execute("SELECT id FROM user LIMIT 1").fetchone())
        if not admin:
            raise SystemExit("❌ no user account yet — open the WebUI, sign up (the first account "
                             "becomes admin), then re-run `make orchestrator`.")
        stamp = int(time.time()) if now is None else now
        row = build_model_row(prompt, admin_id=admin[0], now=stamp, base_model=base_model)
        row = {k: v for k, v in row.items() if k in cols}
        existing = db.execute("SELECT created_at FROM model WHERE id = ?", (MODEL_ID,)).fetchone()
        if existing:
            row.pop("created_at", None)
            row.pop("user_id", None)  # keep the original owner
            assignments = ", ".join(f"{k} = ?" for k in row)
            db.execute(f"UPDATE model SET {assignments} WHERE id = ?", [*row.values(), MODEL_ID])
            verb = "updated"
        else:
            db.execute(f"INSERT INTO model ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                       list(row.values()))
            verb = "created"
        db.commit()
    finally:
        db.close()
    return (f"{verb} model '{MODEL_ID}' ({MODEL_NAME}) on base '{base_model}' with the SWE tool "
            f"server bound (server:{TOOL_SERVER_ID}); prompt {len(prompt)} chars")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print("usage: openwebui_orchestrator.py <webui.db> <orchestrator.md> [base-model]",
              file=sys.stderr)
        return 2
    base_model = args[2] if len(args) > 2 else DEFAULT_BASE_MODEL
    print(upsert(Path(args[0]), Path(args[1]), base_model=base_model))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
