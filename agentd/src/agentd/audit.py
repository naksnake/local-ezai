"""The platform's append-only audit log (ADR-025 "single audit log").

One JSONL file, one record shape, written by every management surface:
the governance queue (PR-5) records request transitions, the ``ezaid``
control plane (PR-8) records its own lifecycle, rejected authentication
attempts and — from PR-9 on — every mutating API call with the caller's
forwarded identity. Records are appended under a process-wide lock and
never rewritten; readers get the tail.

Extracted from ``governance.py`` in PR-8 without changing the file path
(``<config>/governance/log.jsonl``) or the record fields, so logs written
before this module existed read back unchanged.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class AuditRecord(BaseModel):
    ts: str
    event: str
    actor: str
    request_id: str = ""
    generation: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class AuditLog:
    """Append-only JSONL log. ``record`` appends one line (atomic for the
    small writes involved — the file is opened in append mode and a lock
    serializes writers within the process); ``tail`` reads back."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(self, event: str, actor: str, *, request_id: str = "",
               generation: int | None = None, **details: Any) -> AuditRecord:
        entry = AuditRecord(ts=now(), event=event, actor=actor, request_id=request_id,
                            generation=generation, details=details)
        line = json.dumps(entry.model_dump(mode="json"), sort_keys=True) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        return entry

    def tail(self, limit: int | None = None) -> list[AuditRecord]:
        if not self.path.is_file():
            return []
        records = [AuditRecord.model_validate(json.loads(line))
                   for line in self.path.read_text(encoding="utf-8").splitlines() if line]
        return records[-limit:] if limit else records

    def count(self) -> int:
        if not self.path.is_file():
            return 0
        return sum(1 for line in self.path.read_text(encoding="utf-8").splitlines() if line)
