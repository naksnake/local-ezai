"""Project memory — persistent, per-repository learning (Phase 4, ADR-017).

Storage lives in the **origin repository's** ``.agent/`` directory:

- ``.agent/memory.db``            — SQLite store (single source of truth)
- ``.agent/lessons_learned.json`` — human-readable export, regenerated
                                    after every recorded run

Because runs execute in git worktrees, the origin's ``.agent/`` sits
*outside* the workspace: memory persists across runs and branches and can
never leak into a run's diff. (In in-place mode the Git Agent additionally
excludes the memory files from staging.)

Memory kinds (the six persisted concerns):

    architecture_decision · coding_style · project_rule ·
    failed_fix · successful_fix · implementation

The store is **lazily created**: read operations on a repo without memory
return empty results and create nothing (``ezai plan`` stays traceless);
the first write creates ``.agent/``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentd.logging_setup import get_logger

log = get_logger("memory")

DB_NAME = "memory.db"
LESSONS_NAME = "lessons_learned.json"
SCHEMA_VERSION = 1

KIND_ARCHITECTURE = "architecture_decision"
KIND_STYLE = "coding_style"
KIND_RULE = "project_rule"
KIND_FAILED_FIX = "failed_fix"
KIND_SUCCESSFUL_FIX = "successful_fix"
KIND_IMPLEMENTATION = "implementation"

ALL_KINDS = (
    KIND_ARCHITECTURE, KIND_STYLE, KIND_RULE,
    KIND_FAILED_FIX, KIND_SUCCESSFUL_FIX, KIND_IMPLEMENTATION,
)
CURATED_KINDS = (KIND_ARCHITECTURE, KIND_STYLE, KIND_RULE)
FIX_KINDS = (KIND_FAILED_FIX, KIND_SUCCESSFUL_FIX)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT NOT NULL,
    run_id           TEXT NOT NULL DEFAULT '',
    kind             TEXT NOT NULL,
    title            TEXT NOT NULL,
    content          TEXT NOT NULL,
    error_signature  TEXT NOT NULL DEFAULT '',
    category         TEXT NOT NULL DEFAULT '',
    files            TEXT NOT NULL DEFAULT '[]',
    data             TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind);
CREATE INDEX IF NOT EXISTS idx_memories_signature ON memories(error_signature);
"""


@dataclass
class MemoryRecord:
    id: int
    created_at: str
    run_id: str
    kind: str
    title: str
    content: str
    error_signature: str = ""
    category: str = ""
    files: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "created_at": self.created_at, "run_id": self.run_id,
            "kind": self.kind, "title": self.title, "content": self.content,
            "error_signature": self.error_signature, "category": self.category,
            "files": self.files, "data": self.data,
        }


class MemoryStore:
    """SQLite-backed project memory (one store per target repository)."""

    def __init__(self, agent_dir: Path) -> None:
        self.agent_dir = Path(agent_dir)
        self.db_path = self.agent_dir / DB_NAME
        self.lessons_path = self.agent_dir / LESSONS_NAME
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    # ── connection (lazy) ────────────────────────────────────────────────────

    def _connect(self, create: bool) -> sqlite3.Connection | None:
        with self._lock:
            if self._conn is not None:
                return self._conn
            if not create and not self.db_path.exists():
                return None
            self.agent_dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
            self._conn = conn
            return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def exists(self) -> bool:
        return self.db_path.exists()

    # ── writes ───────────────────────────────────────────────────────────────

    def record(
        self,
        kind: str,
        title: str,
        content: str,
        run_id: str = "",
        error_signature: str = "",
        category: str = "",
        files: list[str] | None = None,
        data: dict[str, Any] | None = None,
    ) -> int:
        if kind not in ALL_KINDS:
            raise ValueError(f"unknown memory kind '{kind}' (known: {ALL_KINDS})")
        conn = self._connect(create=True)
        assert conn is not None
        with self._lock:
            cursor = conn.execute(
                "INSERT INTO memories (created_at, run_id, kind, title, content,"
                " error_signature, category, files, data)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    run_id, kind, title.strip()[:200], content.strip()[:4000],
                    error_signature, category,
                    json.dumps(files or []), json.dumps(data or {}),
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    # ── reads ────────────────────────────────────────────────────────────────

    def _rows(self, where: str, params: tuple, limit: int) -> list[MemoryRecord]:
        conn = self._connect(create=False)
        if conn is None:
            return []
        with self._lock:
            rows = conn.execute(
                f"SELECT * FROM memories WHERE {where} "  # noqa: S608 — fixed clauses
                f"ORDER BY id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [_to_record(r) for r in rows]

    def recent(self, kinds: list[str] | None = None, limit: int = 10) -> list[MemoryRecord]:
        if kinds:
            marks = ",".join("?" * len(kinds))
            return self._rows(f"kind IN ({marks})", tuple(kinds), limit)
        return self._rows("1=1", (), limit)

    def fixes_for_signature(self, signature: str, limit: int = 10) -> list[MemoryRecord]:
        """Fix memories whose error signature matches exactly."""
        if not signature:
            return []
        marks = ",".join("?" * len(FIX_KINDS))
        return self._rows(
            f"kind IN ({marks}) AND error_signature = ?",
            (*FIX_KINDS, signature), limit,
        )

    def fixes_for_category(self, category: str, limit: int = 5) -> list[MemoryRecord]:
        if not category:
            return []
        marks = ",".join("?" * len(FIX_KINDS))
        return self._rows(
            f"kind IN ({marks}) AND category = ?", (*FIX_KINDS, category), limit
        )

    def search(self, text: str, kinds: list[str] | None = None,
               limit: int = 10) -> list[MemoryRecord]:
        """Keyword search (any token, case-insensitive) over title+content."""
        tokens = _tokens(text)
        if not tokens:
            return []
        clauses = ["lower(title || ' ' || content) LIKE ?" for _ in tokens]
        where = "(" + " OR ".join(clauses) + ")"
        params: list[Any] = [f"%{t}%" for t in tokens]
        if kinds:
            marks = ",".join("?" * len(kinds))
            where += f" AND kind IN ({marks})"
            params.extend(kinds)
        return self._rows(where, tuple(params), limit)

    def count(self, kind: str | None = None) -> int:
        conn = self._connect(create=False)
        if conn is None:
            return 0
        with self._lock:
            if kind:
                row = conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE kind = ?", (kind,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        return int(row[0])

    # ── lessons_learned.json export ──────────────────────────────────────────

    def export_lessons(self) -> Path:
        """Regenerate the human-readable ``.agent/lessons_learned.json``."""
        grouped: dict[str, list[dict[str, Any]]] = {
            "project_rules": [r.to_dict() for r in self.recent([KIND_RULE], 100)],
            "coding_styles": [r.to_dict() for r in self.recent([KIND_STYLE], 100)],
            "architecture_decisions": [
                r.to_dict() for r in self.recent([KIND_ARCHITECTURE], 100)
            ],
            "lessons": [
                r.to_dict() for r in self.recent(list(FIX_KINDS), 200)
            ],
            "implementation_history": [
                r.to_dict() for r in self.recent([KIND_IMPLEMENTATION], 200)
            ],
        }
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "total_memories": self.count(),
            **grouped,
        }
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        self.lessons_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return self.lessons_path


def _to_record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        id=row["id"], created_at=row["created_at"], run_id=row["run_id"],
        kind=row["kind"], title=row["title"], content=row["content"],
        error_signature=row["error_signature"], category=row["category"],
        files=json.loads(row["files"]), data=json.loads(row["data"]),
    )


def _tokens(text: str, min_len: int = 4, max_tokens: int = 8) -> list[str]:
    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]+", text.lower())
    seen: list[str] = []
    for word in words:
        if len(word) >= min_len and word not in seen:
            seen.append(word)
        if len(seen) >= max_tokens:
            break
    return seen


# ── "avoid repeating previous mistakes": approach similarity ─────────────────


_STOPWORDS = frozenset(
    {"the", "and", "for", "with", "that", "this", "into", "from", "then",
     "when", "must", "should"}
)


def approaches_similar(a: str, b: str, threshold: float = 0.8) -> bool:
    """True when two fix approaches are effectively the same (normalized
    word-set Jaccard similarity ≥ threshold, stopwords ignored)."""
    set_a = set(_tokens(a, min_len=3, max_tokens=50)) - _STOPWORDS
    set_b = set(_tokens(b, min_len=3, max_tokens=50)) - _STOPWORDS
    if not set_a or not set_b:
        return False
    jaccard = len(set_a & set_b) / len(set_a | set_b)
    return jaccard >= threshold


def find_repeated_approach(
    store: MemoryStore, signatures: list[str], approach: str
) -> MemoryRecord | None:
    """A previously FAILED fix for one of these signatures whose approach is
    effectively identical to the proposed one — the mistake about to be
    repeated."""
    for signature in signatures:
        for record in store.fixes_for_signature(signature, limit=20):
            if record.kind != KIND_FAILED_FIX:
                continue
            previous = record.data.get("approach") or record.content
            if approaches_similar(previous, approach):
                return record
    return None


# ── prompt renderers (read-side integration) ─────────────────────────────────


def _bullet(record: MemoryRecord, max_chars: int = 240) -> str:
    text = record.content.replace("\n", " ").strip()
    if len(text) > max_chars:
        text = text[: max_chars - 3] + "..."
    return f"- [{record.kind}] {record.title}: {text}"


def render_planner_context(store: MemoryStore, request: str,
                           limit_each: int = 5) -> str:
    """Memory block for the Planner: rules, styles, decisions, relevant past
    lessons and implementation history."""
    if not store.exists:
        return ""
    sections: list[str] = []
    curated = [
        ("Project rules (must be respected)", store.recent([KIND_RULE], limit_each)),
        ("Coding styles", store.recent([KIND_STYLE], limit_each)),
        ("Architecture decisions", store.recent([KIND_ARCHITECTURE], limit_each)),
    ]
    for heading, records in curated:
        if records:
            sections.append(heading + ":\n" + "\n".join(_bullet(r) for r in records))

    relevant = store.search(request, kinds=[*FIX_KINDS, KIND_IMPLEMENTATION],
                            limit=limit_each)
    if relevant:
        sections.append(
            "Past lessons relevant to this request:\n"
            + "\n".join(_bullet(r) for r in relevant)
        )
    history = store.recent([KIND_IMPLEMENTATION], limit_each)
    if history:
        sections.append(
            "Recent implementation history:\n"
            + "\n".join(_bullet(r) for r in history)
        )
    return "\n\n".join(sections)


def render_debugger_context(store: MemoryStore, signatures: list[str],
                            categories: list[str], limit_each: int = 5) -> str:
    """Memory block for the Debug Agent: what already failed for this exact
    failure (never repeat), and what worked before (consider)."""
    if not store.exists:
        return ""
    failed: list[MemoryRecord] = []
    succeeded: list[MemoryRecord] = []
    seen: set[int] = set()

    for signature in signatures:
        for record in store.fixes_for_signature(signature, limit_each * 2):
            if record.id in seen:
                continue
            seen.add(record.id)
            (failed if record.kind == KIND_FAILED_FIX else succeeded).append(record)
    for category in categories:
        for record in store.fixes_for_category(category, limit_each):
            if record.id in seen:
                continue
            seen.add(record.id)
            (failed if record.kind == KIND_FAILED_FIX else succeeded).append(record)

    sections: list[str] = []
    if failed:
        sections.append(
            "Fix approaches that ALREADY FAILED for this failure — do NOT "
            "propose these again; your diagnosis must explain what they "
            "missed:\n"
            + "\n".join(
                f"- (run {r.run_id}) {r.data.get('approach', r.title)}: "
                f"{r.content[:200]}"
                for r in failed[:limit_each]
            )
        )
    if succeeded:
        sections.append(
            "Repairs that previously SUCCEEDED on this kind of failure "
            "(consider whether the same cause applies):\n"
            + "\n".join(
                f"- (run {r.run_id}) {r.data.get('approach', r.title)}: "
                f"{r.content[:200]}"
                for r in succeeded[:limit_each]
            )
        )
    return "\n\n".join(sections)
