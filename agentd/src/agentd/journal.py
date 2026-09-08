"""Event journal — append-only JSONL per run (ADR-006).

Every meaningful action (state transitions, agent spawns, LLM calls, tool
calls and results, verdicts, terminal states) is appended as one JSON line:

    {"seq": 12, "ts": "2026-08-14T10:22:31.512Z", "type": "TOOL_CALLED",
     "payload": {...}}

The journal is the single source of truth for what a run did; UIs, reports
and future resume/distillation read it. Nothing in the runtime keeps state
that is not derivable from the journal plus the workspace.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Journal:
    """Append-only JSONL journal for one run."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "journal.jsonl"
        self._seq = 0
        self._lock = threading.Lock()

    def append(self, event_type: str, **payload: Any) -> int:
        """Append one event; returns its sequence number."""
        with self._lock:
            self._seq += 1
            record = {
                "seq": self._seq,
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "type": event_type,
                "payload": _jsonable(payload),
            }
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            return self._seq

    #: False for journals with no real directory (NullJournal) — consumers
    #: that write artifacts next to the journal must check this first.
    is_persistent: bool = True

    def read(self) -> list[dict[str, Any]]:
        """Read all events back (for reports, tests, and future resume)."""
        if not self.path.exists():
            return []
        events = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events


class NullJournal(Journal):
    """Journal that records nothing (unit tests of components)."""

    is_persistent = False

    def __init__(self) -> None:  # noqa: D107 — intentionally no directory
        self._seq = 0
        self._lock = threading.Lock()
        self.path = Path("/dev/null")
        self.run_dir = Path("/dev/null")

    def append(self, event_type: str, **payload: Any) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def read(self) -> list[dict[str, Any]]:
        return []


def _jsonable(value: Any) -> Any:
    """Best-effort conversion to JSON-serializable structures."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)
