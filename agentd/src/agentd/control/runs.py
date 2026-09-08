"""Async run registry (PR-10, ADR-028; OPENWEBUI_INTEGRATION §2 "async run
protocol": start returns a run id within seconds, the caller polls; the
control plane owns run lifecycle and concurrency limits).

A ``RunRegistry`` executes the platform's existing pipelines — ``run``
(`execute_run`), ``fix`` (`heal_run`), ``sprint`` (`run_sprint_autonomous`
/ `run_sprint`), ``evolve`` (`run_evolution`), ``plan`` (`plan_only`, the A0
dry-run) — on a bounded worker pool, records every job as one JSON file
under ``<config>/control/runs/`` (recovered on restart; interrupted jobs are
marked failed), and audits submissions, outcomes and cancellations.

**Cancellation without touching the core graph:** a queued job is cancelled
before it starts; a running job's model client is wrapped by
``CancellableLLM``, which raises ``RunCancelled`` at the job's next model
call — the pipeline unwinds through its own ``finally`` blocks (memory
store closed, journal intact) and the job ends ``cancelled``. Subprocesses
already running (validation, git) finish on their own.

**Limits:** ``max_concurrent`` workers; ``max_queued`` jobs may wait;
beyond that a submission is refused (``too_many_runs``). At most one
in-place job (``fix``) per project at a time (``project_busy``) — worktree
kinds may overlap.

The API never grants push: ``git.allow_push`` is forced off for every job
(the T3 ceiling of the chat-ops boundary; pushes stay with the CLI/human).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentd.audit import AuditLog
from agentd.config import AgentdConfig
from agentd.llm import LLMClient, build_llm
from agentd.logging_setup import get_logger

log = get_logger("ezaid.runs")

KINDS = ("run", "fix", "sprint", "evolve", "plan")
#: Kinds that act on the project's working tree itself (exclusive per project).
IN_PLACE_KINDS = frozenset({"fix"})
TERMINAL = frozenset({"completed", "failed", "cancelled"})
DIRNAME = "runs"
DEFAULT_MAX_CONCURRENT = 2
DEFAULT_MAX_QUEUED = 8
FIX_DEFAULT_GOAL = "repair failing validation checks"

#: HTTP status per refusal code (the API maps ``RunRefused`` with it).
REFUSAL_STATUS = {"not_found": 404, "project_busy": 409, "run_finished": 409,
                  "too_many_runs": 429, "invalid_request": 422}


class RunCancelled(RuntimeError):
    """Raised inside a job at its next model call once cancellation was requested."""


class RunRefused(ValueError):
    """A submission or cancellation the registry's rules refuse."""

    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ── cancellation ─────────────────────────────────────────────────────────────


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise RunCancelled("run cancelled by request")


class CancellableLLM:
    """The job's model client: checks the token before every model call and
    forwards everything else (``models_used``, …) to the wrapped client."""

    def __init__(self, inner: LLMClient, token: CancelToken) -> None:
        self._inner, self._token = inner, token

    def chat(self, role: str, messages: list[dict[str, Any]], *args: Any, **kwargs: Any) -> Any:
        self._token.check()
        return self._inner.chat(role, messages, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ── records ──────────────────────────────────────────────────────────────────


@dataclass
class RunRecord:
    run_id: str
    kind: str
    project: str
    project_name: str
    request: str
    actor: str = ""
    status: str = "queued"
    submitted_at: str = field(default_factory=_now)
    started_at: str = ""
    finished_at: str = ""
    cancel_requested: bool = False
    error: str = ""
    report_path: str = ""
    journal_path: str = ""
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


#: ``pipeline(config, repo, record, llm) -> report`` (a pydantic model).
Pipeline = Callable[[AgentdConfig, Path, RunRecord, LLMClient], Any]
LLMFactory = Callable[[AgentdConfig], LLMClient]


def default_pipelines() -> dict[str, Pipeline]:
    """The platform's pipelines, imported lazily (they pull the whole agent
    runtime) and given the job's id so run directories match the record."""

    def run(config: AgentdConfig, repo: Path, record: RunRecord, llm: LLMClient) -> Any:
        from agentd.runner import execute_run

        return execute_run(config, repo, record.request, llm=llm, run_id=record.run_id)

    def fix(config: AgentdConfig, repo: Path, record: RunRecord, llm: LLMClient) -> Any:
        from agentd.runner import heal_run

        return heal_run(config, repo, goal=record.request or FIX_DEFAULT_GOAL, llm=llm,
                        run_id=record.run_id)

    def plan(config: AgentdConfig, repo: Path, record: RunRecord, llm: LLMClient) -> Any:
        from agentd.runner import plan_only

        return plan_only(config, repo, record.request, llm=llm, run_id=record.run_id)

    def sprint(config: AgentdConfig, repo: Path, record: RunRecord, llm: LLMClient) -> Any:
        options = record.options
        if options.get("simple"):
            from agentd.runner import run_sprint
            from agentd.sprint import parse_sprint_tasks

            return run_sprint(config, repo, parse_sprint_tasks(record.request),
                              spec_file=options.get("spec_file", ""), llm=llm,
                              sprint_id=record.run_id, keep_going=bool(options.get("keep_going")))
        from agentd.sprint_exec import run_sprint_autonomous

        return run_sprint_autonomous(config, repo, record.request,
                                     spec_file=options.get("spec_file", ""), llm=llm,
                                     sprint_id=record.run_id,
                                     keep_going=bool(options.get("keep_going")))

    def evolve(config: AgentdConfig, repo: Path, record: RunRecord, llm: LLMClient) -> Any:
        from agentd.evolution import run_evolution

        return run_evolution(config, repo, focus=record.request, llm=llm,
                             evolution_id=record.run_id)

    return {"run": run, "fix": fix, "plan": plan, "sprint": sprint, "evolve": evolve}


def configure_job(config: AgentdConfig, record: RunRecord) -> AgentdConfig:
    """The job's config: the daemon's config plus the request's options —
    and never a push (T3 stays with the CLI/human)."""
    config = config.model_copy(deep=True)
    options = record.options
    if options.get("max_iterations") is not None:
        config.limits.max_heal_iterations = max(0, int(options["max_iterations"]))
    if options.get("in_place") and record.kind == "run":
        config.workspace.mode = "in-place"
    if options.get("max_parallel"):
        config.sprint.max_parallel = max(1, int(options["max_parallel"]))
    config.git.allow_push = False
    return config


def outcome_status(result: Any) -> tuple[str, str]:
    """(status, error) of a finished pipeline result: run/sprint/evolution
    reports carry ``status`` (completed|failed) and maybe ``error``; a plan
    has neither and counts as completed."""
    status = getattr(result, "status", "completed")
    error = getattr(result, "error", None) or ""
    return ("completed" if status in ("completed", "done") else "failed"), str(error)


# ── the registry ─────────────────────────────────────────────────────────────


class RunRegistry:
    def __init__(self, config: AgentdConfig, records_dir: Path, *,
                 audit: AuditLog | None = None,
                 max_concurrent: int = DEFAULT_MAX_CONCURRENT,
                 max_queued: int = DEFAULT_MAX_QUEUED,
                 pipelines: dict[str, Pipeline] | None = None,
                 llm_factory: LLMFactory | None = None) -> None:
        self.config = config
        self.records_dir = Path(records_dir)
        self.audit = audit
        self.max_concurrent = max(1, max_concurrent)
        self.max_queued = max(0, max_queued)
        self._pipelines = pipelines if pipelines is not None else default_pipelines()
        self._llm_factory: LLMFactory = llm_factory or (lambda cfg: build_llm(cfg.llm))
        self._executor = ThreadPoolExecutor(max_workers=self.max_concurrent,
                                            thread_name_prefix="ezaid-run")
        self._lock = threading.RLock()
        self._records: dict[str, RunRecord] = {}
        self._tokens: dict[str, CancelToken] = {}
        self._futures: dict[str, Future[None]] = {}
        self._recover()

    # ── persistence ──────────────────────────────────────────────────────

    def _path(self, run_id: str) -> Path:
        return self.records_dir / f"{run_id}.json"

    def _save(self, record: RunRecord) -> None:
        self.records_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(record.run_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record.as_dict(), indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    def _record_audit(self, event: str, record: RunRecord, **details: Any) -> None:
        if self.audit is not None:
            self.audit.record(event, record.actor or "ezaid", run_id=record.run_id,
                              kind=record.kind, project=record.project_name,
                              status=record.status, **details)

    def _recover(self) -> None:
        """Load the records of earlier daemon lives; a job that was queued or
        running when the daemon stopped cannot have survived — mark it failed
        and say so (the journal/report on disk stay as evidence)."""
        if not self.records_dir.is_dir():
            return
        for path in sorted(self.records_dir.glob("*.json")):
            record = RunRecord(**json.loads(path.read_text(encoding="utf-8")))
            if not record.finished:
                record.error = (f"control plane restarted while the run was {record.status} — "
                                "resubmit it; the journal on disk shows how far it got")
                record.status = "failed"
                record.finished_at = _now()
                self._save(record)
                self._record_audit("run.orphaned", record, error=record.error)
            self._records[record.run_id] = record

    # ── queries ──────────────────────────────────────────────────────────

    def get(self, run_id: str) -> RunRecord:
        with self._lock:
            record = self._records.get(run_id)
        if record is None:
            raise RunRefused("not_found", f"no run '{run_id}' in this control plane",
                             "GET /v1/runs lists the known runs")
        return record

    def list(self, *, kind: str | None = None, status: str | None = None,
             project: str | None = None, limit: int = 50) -> list[RunRecord]:
        with self._lock:
            records = list(self._records.values())
        records = [r for r in records
                   if (kind is None or r.kind == kind) and (status is None or r.status == status)
                   and (project is None or project in (r.project, r.project_name))]
        records.sort(key=lambda r: (r.submitted_at, r.run_id), reverse=True)
        return records[:max(1, limit)]

    def active(self) -> list[RunRecord]:
        with self._lock:
            return [r for r in self._records.values() if not r.finished]

    def run_dir(self, record: RunRecord) -> Path:
        return Path(self.config.runs_dir) / record.run_id

    def progress(self, record: RunRecord) -> dict[str, Any]:
        journal = self.run_dir(record) / "journal.jsonl"
        if not journal.is_file():
            return {"events": 0, "last_event": None, "last_ts": None}
        lines = [line for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
        last = json.loads(lines[-1]) if lines else {}
        return {"events": len(lines), "last_event": last.get("type"), "last_ts": last.get("ts")}

    def journal_tail(self, record: RunRecord, tail: int = 50) -> tuple[int, list[dict[str, Any]]]:
        journal = self.run_dir(record) / "journal.jsonl"
        if not journal.is_file():
            return 0, []
        events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()
                  if line.strip()]
        return len(events), events[-max(1, tail):]

    def report(self, record: RunRecord) -> dict[str, Any] | None:
        path = (Path(record.report_path) if record.report_path
                else self.run_dir(record) / "report.json")
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    # ── lifecycle ────────────────────────────────────────────────────────

    def submit(self, kind: str, project: Path, project_name: str, request: str, *,
               actor: str, options: dict[str, Any] | None = None) -> RunRecord:
        if kind not in self._pipelines:
            raise RunRefused("invalid_request", f"unknown run kind '{kind}'",
                             "one of: " + ", ".join(sorted(self._pipelines)))
        from agentd.runner import new_run_id

        with self._lock:
            active = self.active()
            if len(active) >= self.max_concurrent + self.max_queued:
                raise RunRefused(
                    "too_many_runs",
                    f"{len(active)} run(s) active — the control plane accepts "
                    f"{self.max_concurrent} concurrent + {self.max_queued} queued",
                    "wait for a run to finish (GET /v1/runs?status=running) or cancel one")
            if kind in IN_PLACE_KINDS:
                busy = [r for r in active
                        if r.project == str(project) and r.kind in IN_PLACE_KINDS]
                if busy:
                    raise RunRefused(
                        "project_busy",
                        f"run {busy[0].run_id} ({busy[0].kind}) already works in place on "
                        f"{project_name}",
                        "wait for it to finish or cancel it — in-place jobs are exclusive")
            record = RunRecord(run_id=new_run_id(), kind=kind, project=str(project),
                               project_name=project_name, request=request, actor=actor,
                               options=dict(options or {}))
            self._records[record.run_id] = record
            self._tokens[record.run_id] = CancelToken()
            self._save(record)
            self._futures[record.run_id] = self._executor.submit(self._execute, record.run_id)
        self._record_audit("run.submitted", record, request=request[:200])
        log.info("run %s submitted: %s on %s by %s", record.run_id, kind, project_name, actor)
        return record

    def cancel(self, run_id: str, *, actor: str) -> RunRecord:
        with self._lock:
            record = self.get(run_id)
            if record.finished:
                raise RunRefused("run_finished", f"run {run_id} is already {record.status}",
                                 "nothing to cancel — GET /v1/runs/{id}/report has the outcome")
            record.cancel_requested = True
            self._tokens[run_id].cancel()
            if record.status == "queued":  # never started: done here and now
                record.status = "cancelled"
                record.finished_at = _now()
            self._save(record)
        self._record_audit("run.cancel_requested", record, by=actor)
        return record

    def _execute(self, run_id: str) -> None:
        with self._lock:
            record = self._records[run_id]
            token = self._tokens[run_id]
            if record.finished:  # cancelled while queued
                return
            record.status = "running"
            record.started_at = _now()
            self._save(record)
        config = configure_job(self.config, record)
        run_dir = Path(config.runs_dir) / run_id
        try:
            llm = CancellableLLM(self._llm_factory(config), token)
            result = self._pipelines[record.kind](config, Path(record.project), record, llm)
            report_path = run_dir / "report.json"
            if not report_path.is_file() and hasattr(result, "model_dump_json"):
                run_dir.mkdir(parents=True, exist_ok=True)
                report_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
            status, error = outcome_status(result)
        except RunCancelled:
            status, error = "cancelled", "cancelled at the next model call, as requested"
        except Exception as exc:  # noqa: BLE001 — a job's failure is a recorded outcome
            status, error = "failed", f"{type(exc).__name__}: {exc}"
            log.exception("run %s failed", run_id)
        with self._lock:
            record.status = status
            record.error = error
            record.finished_at = _now()
            if (run_dir / "report.json").is_file():
                record.report_path = str(run_dir / "report.json")
            if (run_dir / "journal.jsonl").is_file():
                record.journal_path = str(run_dir / "journal.jsonl")
            self._save(record)
        self._record_audit("run.finished", record, error=error[:300])
        log.info("run %s %s", run_id, status)

    def wait(self, run_id: str, timeout: float | None = None) -> RunRecord:
        """Block until the job's worker returns (tests, graceful shutdown)."""
        future = self._futures.get(run_id)
        if future is not None:
            future.result(timeout=timeout)
        return self.get(run_id)

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)
