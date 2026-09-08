"""Governance queue + audit log (PR-5, ADR-027; docs/MODEL_GOVERNANCE_V2.md
§2/§5, docs/WEBUI_ADMIN_CENTER.md §3, docs/MODEL_LIFECYCLE_MANAGEMENT.md §2).

**Agents propose, humans approve.** A ``ChangeRequest`` carries the whole
proposed generation (registry content), the human-readable diff, the
roles whose resolution changes, and the evidence a decision must be made
on (benchmarks, capability report, fit verdicts). It lives as one YAML
file under ``<config>/governance/queue/`` for its entire life
(pending → approved | rejected → applied | failed | superseded); every
transition appends an immutable record to ``<config>/governance/log.jsonl``.

Rules enforced here, not in policy text:

- **approval matrix** — a request that changes no role's resolution and
  keeps the slot runtime is approved by policy on submission (audited);
  everything else waits for a human decision;
- **rejection needs a reason**; decisions are final (no re-approval);
- **evolution lane** (MODEL_GOVERNANCE_V2 §5): a request marked
  ``proposed_by: evolution`` needs benchmark evidence from this host and
  at most one such request may be open at a time; it is never
  auto-approved.

The queue knows nothing about rendering or engines — ``activation.py``
applies approved requests.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from agentd.logging_setup import get_logger

log = get_logger("governance")

GOVERNANCE_DIRNAME = "governance"
QUEUE_DIRNAME = "queue"
LOG_FILENAME = "log.jsonl"

#: The queue holds the Admin Center's item types (WEBUI_ADMIN_CENTER §3);
#: PR-5 creates the model-lifecycle kinds, the others are compatible slots.
RequestKind = Literal["activation", "upgrade", "retire", "runtime_switch", "bootstrap",
                      "evolution_pr", "release"]
RequestStatus = Literal["pending", "approved", "rejected", "applied", "failed", "superseded"]
OPEN_STATUSES = ("pending", "approved")
POLICY_ACTOR = "policy"
EVOLUTION = "evolution"


class GovernanceError(ValueError):
    """A queue operation the rules forbid; the message names the rule."""


class Decision(BaseModel):
    by: str
    at: str
    reason: str = ""


class ChangeRequest(BaseModel):
    """One governed change — the approval object (govern roles, reveal
    models: model names appear only inside diff/evidence)."""

    id: str = ""
    kind: RequestKind
    title: str
    requested_by: str
    proposed_by: str = "human"  # "human" | "evolution"
    created_at: str = ""
    base_generation: int
    #: Full RegistryV2 content of the proposed generation.
    proposed: dict[str, Any]
    diff: list[str] = Field(default_factory=list)
    #: role → {"before": {primary, fallbacks}, "after": {...}} for roles
    #: whose resolution changes (the "affected roles" of the approval modal).
    affected_roles: dict[str, dict[str, Any]] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = True
    status: RequestStatus = "pending"
    decision: Decision | None = None
    result: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


class AuditRecord(BaseModel):
    ts: str
    event: str
    actor: str
    request_id: str = ""
    generation: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class GovernanceQueue:
    """File-backed queue + append-only log under ``<config>/governance/``."""

    def __init__(self, config_dir: Path) -> None:
        self.root = Path(config_dir) / GOVERNANCE_DIRNAME
        self.queue_dir = self.root / QUEUE_DIRNAME
        self.log_path = self.root / LOG_FILENAME

    # ── storage ──────────────────────────────────────────────────────────

    def _path(self, request_id: str) -> Path:
        return self.queue_dir / f"{request_id}.yaml"

    def _write(self, request: ChangeRequest) -> None:
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(request.id)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(request.model_dump(mode="json"), sort_keys=False),
                       encoding="utf-8")
        tmp.replace(path)

    def _next_id(self) -> str:
        existing = list(self.queue_dir.glob("cr-*.yaml")) if self.queue_dir.is_dir() else []
        return f"cr-{len(existing) + 1:04d}"

    def get(self, request_id: str) -> ChangeRequest:
        path = self._path(request_id)
        if not path.is_file():
            raise GovernanceError(f"no change request '{request_id}' in {self.queue_dir}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return ChangeRequest.model_validate(data)

    def list(self, status: str | None = None) -> list[ChangeRequest]:
        if not self.queue_dir.is_dir():
            return []
        requests = [self.get(path.stem) for path in sorted(self.queue_dir.glob("cr-*.yaml"))]
        return [r for r in requests if status is None or r.status == status]

    # ── audit log (append-only) ──────────────────────────────────────────

    def record(self, event: str, actor: str, *, request_id: str = "",
               generation: int | None = None, **details: Any) -> AuditRecord:
        entry = AuditRecord(ts=_now(), event=event, actor=actor, request_id=request_id,
                            generation=generation, details=details)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.model_dump(mode="json"), sort_keys=True) + "\n")
        log.info("governance: %s %s %s", event, request_id or "", actor)
        return entry

    def audit(self, limit: int | None = None) -> list[AuditRecord]:
        if not self.log_path.is_file():
            return []
        records = [AuditRecord.model_validate(json.loads(line))
                   for line in self.log_path.read_text(encoding="utf-8").splitlines() if line]
        return records[-limit:] if limit else records

    # ── lifecycle of a request ───────────────────────────────────────────

    def submit(self, request: ChangeRequest) -> ChangeRequest:
        """Enter the queue. Policy-approves changes that touch no serving
        role; enforces the evolution lane's rules; audits."""
        if request.proposed_by == EVOLUTION:
            if not request.evidence.get("benchmarks"):
                raise GovernanceError(
                    "evolution proposals need benchmark evidence from this host "
                    "(evidence.benchmarks is empty) — evidence or silence")
            open_evolution = [r for r in self.list() if r.proposed_by == EVOLUTION and r.is_open]
            if open_evolution:
                raise GovernanceError(
                    f"evolution already has an open proposal ({open_evolution[0].id}) — "
                    "at most one routing proposal at a time")
            request.requires_approval = True  # never auto-approved
        request.id = request.id or self._next_id()
        request.created_at = request.created_at or _now()
        request.status = "pending"
        self._write(request)
        self.record("request.submitted", request.requested_by, request_id=request.id,
                    generation=request.base_generation, kind=request.kind,
                    title=request.title, proposed_by=request.proposed_by,
                    requires_approval=request.requires_approval,
                    affected_roles=sorted(request.affected_roles))
        if not request.requires_approval:
            reason = ("bootstrap generation 1: implicit approval — the human wrote the seeds"
                      if request.kind == "bootstrap" else
                      "no serving role or slot runtime affected — self-service, audited")
            return self._decide(request, "approved", POLICY_ACTOR, reason)
        return request

    def approve(self, request_id: str, by: str, reason: str = "") -> ChangeRequest:
        return self._decide(self.get(request_id), "approved", by, reason)

    def reject(self, request_id: str, by: str, reason: str) -> ChangeRequest:
        if not reason.strip():
            raise GovernanceError("a rejection needs a reason (it is recorded and, for "
                                  "evolution proposals, remembered)")
        return self._decide(self.get(request_id), "rejected", by, reason)

    def _decide(self, request: ChangeRequest, status: str, by: str, reason: str) -> ChangeRequest:
        if request.status != "pending":
            raise GovernanceError(
                f"{request.id} is {request.status} — decisions are made once, on pending "
                "requests")
        request.status = status  # type: ignore[assignment]
        request.decision = Decision(by=by, at=_now(), reason=reason)
        self._write(request)
        self.record(f"request.{status}", by, request_id=request.id,
                    generation=request.base_generation, reason=reason, kind=request.kind,
                    title=request.title)
        return request

    def mark(self, request_id: str, status: str, actor: str, **result: Any) -> ChangeRequest:
        """Outcome of applying an approved request: applied | failed | superseded."""
        request = self.get(request_id)
        if status not in ("applied", "failed", "superseded"):
            raise GovernanceError(f"'{status}' is not an outcome status")
        if request.status != "approved":
            raise GovernanceError(f"{request.id} is {request.status} — only approved requests "
                                  "are applied")
        request.status = status  # type: ignore[assignment]
        request.result = result
        self._write(request)
        details = {key: value for key, value in result.items() if key != "generation"}
        self.record(f"request.{status}", actor, request_id=request.id,
                    generation=result.get("generation"), **details)
        return request
