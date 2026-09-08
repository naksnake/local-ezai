"""Run endpoints (PR-10, ADR-028; OPENWEBUI_INTEGRATION §2 async run
protocol; CLI_AND_WEBUI §3 "start run / sprint / fix / evolve").

``POST /v1/runs`` starts a job and answers ``202`` with its record within
milliseconds; ``GET /v1/runs/{id}`` (with journal progress),
``/report``, ``/journal`` follow it; ``POST /v1/runs/{id}/cancel`` stops it.
Runs start only for **registered projects** (the allowlist of
`local-ezai project add`) and never push — the chat-ops ceiling.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from agentd import platform_cli as ops
from agentd.control import API_PREFIX
from agentd.control.deps import PLATFORM_ERRORS, ApiError, ErrorEnvelope, PlatformDep
from agentd.control.runs import FIX_DEFAULT_GOAL, RunRecord, RunRegistry

router = APIRouter(prefix=API_PREFIX, tags=["runs"])

RunKind = Literal["run", "fix", "sprint", "evolve", "plan"]
RUN_ERRORS: dict[int | str, dict[str, Any]] = {
    **PLATFORM_ERRORS,
    429: {"model": ErrorEnvelope, "description": "concurrency limit reached (too_many_runs)"},
}


class RunStart(BaseModel):
    kind: RunKind
    project: str = Field(description="a REGISTERED project — its name or path "
                                     "(local-ezai project add)")
    task: str | None = Field(None, description="run / plan: the request in natural language")
    goal: str | None = Field(None, description="fix: the repair goal "
                                               f"(default: {FIX_DEFAULT_GOAL!r})")
    spec: str | None = Field(None, description="sprint: the sprint specification as markdown")
    focus: str | None = Field(None, description="evolve: optional focus area")
    simple: bool = Field(False, description="sprint: one task per line, sequential")
    keep_going: bool = Field(False, description="sprint: continue after a failed task")
    in_place: bool = Field(False, description="run: edit the working tree instead of a worktree")
    max_iterations: int | None = Field(None, ge=0, description="self-healing budget")
    max_parallel: int | None = Field(None, ge=1, description="sprint: parallel task pipelines")


class RunView(BaseModel):
    run_id: str
    kind: str
    status: str
    project: str
    project_name: str
    request: str
    actor: str
    submitted_at: str
    started_at: str
    finished_at: str
    cancel_requested: bool
    error: str
    report_path: str
    journal_path: str
    options: dict[str, Any]
    progress: dict[str, Any] | None = Field(
        None, description="journal events so far: count, last event type and time")


class RunList(BaseModel):
    runs: list[RunView]
    active: int
    max_concurrent: int
    max_queued: int


class RunReportView(BaseModel):
    run_id: str
    kind: str
    status: str
    report: dict[str, Any]


class JournalExcerpt(BaseModel):
    run_id: str
    total: int
    events: list[dict[str, Any]]


def _registry(request: Request) -> RunRegistry:
    registry: RunRegistry | None = getattr(request.app.state, "runs", None)
    if registry is None:
        raise ApiError(503, "platform_unavailable", "ezaid is not attached to a platform",
                       "start ezaid inside the local-ezai checkout or set "
                       "AGENTD_PLATFORM__CONFIG_DIR")
    return registry


def _view(record: RunRecord, progress: dict[str, Any] | None = None) -> RunView:
    return RunView(**record.as_dict(), progress=progress)


def _request_text(body: RunStart) -> str:
    if body.kind in ("run", "plan"):
        if not (body.task or "").strip():
            raise ApiError(422, "invalid_request", f"a {body.kind} job needs `task`",
                           "describe the work in natural language")
        return body.task.strip()
    if body.kind == "sprint":
        if not (body.spec or "").strip():
            raise ApiError(422, "invalid_request", "a sprint job needs `spec`",
                           "send the sprint specification as markdown text")
        return body.spec
    if body.kind == "fix":
        return (body.goal or "").strip() or FIX_DEFAULT_GOAL
    return (body.focus or "").strip()


@router.post("/runs", response_model=RunView, status_code=202, operation_id="run_start",
             summary="Start a run / fix / sprint / evolve / plan job on a registered project "
                     "(CLI: local-ezai run|fix|sprint|evolve|plan)", responses=RUN_ERRORS)
def run_start(request: Request, ctx: PlatformDep, body: RunStart) -> RunView:
    project = ops.resolve_project(ctx, body.project)  # allowlist: unregistered → not_found
    text = _request_text(body)
    options = {k: v for k, v in {
        "simple": body.simple, "keep_going": body.keep_going, "in_place": body.in_place,
        "max_iterations": body.max_iterations, "max_parallel": body.max_parallel,
    }.items() if v not in (None, False)}
    record = _registry(request).submit(body.kind, Path(project["path"]), project["name"],
                                       text, actor=ctx.actor, options=options)
    return _view(record)


@router.get("/runs", response_model=RunList, operation_id="runs_list",
            summary="Runs known to this control plane, newest first", responses=PLATFORM_ERRORS)
def runs_list(request: Request, _: PlatformDep,
              kind: Annotated[RunKind | None, Query()] = None,
              status: Annotated[str | None, Query()] = None,
              project: Annotated[str | None, Query(description="name or path")] = None,
              limit: Annotated[int, Query(ge=1, le=500)] = 50) -> RunList:
    registry = _registry(request)
    records = registry.list(kind=kind, status=status, project=project, limit=limit)
    return RunList(runs=[_view(r) for r in records], active=len(registry.active()),
                   max_concurrent=registry.max_concurrent, max_queued=registry.max_queued)


@router.get("/runs/{run_id}", response_model=RunView, operation_id="run_get",
            summary="One run with its journal progress", responses=PLATFORM_ERRORS)
def run_get(request: Request, _: PlatformDep, run_id: str) -> RunView:
    registry = _registry(request)
    record = registry.get(run_id)
    return _view(record, registry.progress(record))


@router.get("/runs/{run_id}/report", response_model=RunReportView, operation_id="run_report",
            summary="The finished run's report (plan, validation, review, commit, models used)",
            responses={**PLATFORM_ERRORS,
                       409: {"model": ErrorEnvelope, "description": "still running"}})
def run_report(request: Request, _: PlatformDep, run_id: str) -> RunReportView:
    registry = _registry(request)
    record = registry.get(run_id)
    report = registry.report(record)
    if report is None:
        if not record.finished:
            raise ApiError(409, "report_pending", f"run {run_id} is {record.status}",
                           "poll GET /v1/runs/{id} until it finishes")
        raise ApiError(404, "not_found", f"run {run_id} ended {record.status} without a report"
                       + (f": {record.error}" if record.error else ""),
                       "GET /v1/runs/{id}/journal shows how far it got")
    return RunReportView(run_id=run_id, kind=record.kind, status=record.status, report=report)


@router.get("/runs/{run_id}/journal", response_model=JournalExcerpt, operation_id="run_journal",
            summary="Bounded excerpt of the run's event journal", responses=PLATFORM_ERRORS)
def run_journal(request: Request, _: PlatformDep, run_id: str,
                tail: Annotated[int, Query(ge=1, le=1000)] = 50) -> JournalExcerpt:
    registry = _registry(request)
    total, events = registry.journal_tail(registry.get(run_id), tail=tail)
    return JournalExcerpt(run_id=run_id, total=total, events=events)


@router.post("/runs/{run_id}/cancel", response_model=RunView, operation_id="run_cancel",
             summary="Cancel a queued run now, or a running one at its next model call",
             responses=PLATFORM_ERRORS)
def run_cancel(request: Request, ctx: PlatformDep, run_id: str) -> RunView:
    registry = _registry(request)
    record = registry.cancel(run_id, actor=ctx.actor)
    return _view(record, registry.progress(record))
