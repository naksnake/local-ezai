"""Idempotency keys for mutating control-plane calls (PR-9; ADR-028;
docs/CLI_AND_WEBUI_STRATEGY.md §6 "every mutating endpoint … idempotency-keyed").

A client that retries a `POST`/`DELETE` (lost connection, timeout) sends
the same ``Idempotency-Key``; the control plane answers with the response
it already produced instead of repeating the mutation. The same key with a
different request is a conflict. Records live under
``<config>/control/idempotency/`` as one small JSON file per key (the
platform's declarative state directory — no database, ADR-025).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotency-Replayed"
MAX_KEY_LENGTH = 200
DIRNAME = "idempotency"


@dataclass(frozen=True)
class StoredResponse:
    key: str
    fingerprint: str
    status: int
    body: str
    media_type: str
    operation: str
    stored_at: str


class IdempotencyStore:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    @staticmethod
    def fingerprint(method: str, path: str, query: str, body: bytes) -> str:
        digest = hashlib.sha256()
        digest.update(f"{method.upper()} {path}?{query}\n".encode())
        digest.update(body)
        return digest.hexdigest()

    def _path(self, key: str) -> Path:
        return self.directory / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")

    def get(self, key: str) -> StoredResponse | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return StoredResponse(**json.loads(path.read_text(encoding="utf-8")))

    def put(self, key: str, fingerprint: str, status: int, body: bytes, media_type: str,
            operation: str) -> StoredResponse:
        record = StoredResponse(key=key, fingerprint=fingerprint, status=status,
                                body=body.decode("utf-8", errors="replace"),
                                media_type=media_type or "application/json",
                                operation=operation,
                                stored_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(record), sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        return record

    def count(self) -> int:
        return len(list(self.directory.glob("*.json"))) if self.directory.is_dir() else 0
