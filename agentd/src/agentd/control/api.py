"""Lifecycle + governance endpoints (PR-9, ADR-028; CLI_AND_WEBUI_STRATEGY
§3 parity matrix, §6 contract discipline).

Every operation here is the SAME function the direct-mode CLI verb calls
(``platform_cli`` operations) — the response body is the CLI's ``--json``
output, the actor is the forwarded identity, and each mutating operation
maps to exactly one CLI verb (named in its summary). Mutations are
serialized inside the daemon; idempotency keys and the audit of every
mutating call live in the app middleware.

Reloading consumers from this process needs the docker CLI on the host it
runs on; the shipped container has none, so a ``reload`` request is
refused with the fix named instead of failing half-way.
"""

from __future__ import annotations

import shutil
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from agentd import platform_cli as ops
from agentd.control import API_PREFIX
from agentd.control.deps import PLATFORM_ERRORS, ApiError, PlatformDep
from agentd.governance import ChangeRequest

router = APIRouter(prefix=API_PREFIX)


def docker_available() -> bool:
    """Seam: can this process reload consumers (compose) at all?"""
    return shutil.which("docker") is not None


def _check_reload(reload: bool) -> None:
    if reload and not docker_available():
        raise ApiError(409, "reload_unavailable",
                       "this control plane cannot reload consumers (no docker CLI where it runs)",
                       "apply without reload, then `make up` — or run the verb with --reload "
                       "from the local-ezai CLI on the host")


# ── request / response models (the contract) ─────────────────────────────────


class ModelList(BaseModel):
    generation: int | None
    models: dict[str, dict[str, Any]]


class InstallRequest(BaseModel):
    ref: str = Field(description="hf:<org/repo> · gguf:<url|hf://org/repo/file|path> · "
                                 "catalog id · auto (needs group)")
    name: str | None = None
    runtime: str | None = None
    group: str | None = None
    refetch: bool = False


class InstallOutcome(BaseModel):
    ok: bool
    name: str
    state: str
    artifact: str
    size_gb: float
    error: str
    persisted: bool
    message: str


class BenchmarkRequest(BaseModel):
    base_url: str | None = Field(None, description="measure the live engine instead of a side-load")


class BenchmarkOutcome(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    tokens_per_s: float
    state: str
    persisted: bool
    message: str


class ActivateRequest(BaseModel):
    group: str | None = None
    position: int | None = Field(None, description="0 = primary (default with group)")
    role: str | None = Field(None, description="pin this role to the model")
    reload: bool = False


class UpgradeRequest(BaseModel):
    old: str
    new: str
    reload: bool = False


class RollbackRequest(BaseModel):
    to_generation: int | None = None
    reason: str = ""
    reload: bool = False


class ApplyOutcome(BaseModel):
    request: str
    ok: bool
    generation: int | None
    message: str
    changed: list[str]
    rolled_back: bool


class ProposalOutcome(BaseModel):
    request: ChangeRequest
    applied: ApplyOutcome | None = Field(
        None, description="set when policy approved the request and it was applied at once")


class RollbackOutcome(BaseModel):
    ok: bool
    generation: int | None
    message: str
    rolled_back: bool


class RetireOutcome(BaseModel):
    name: str
    state: str
    generation: int
    persisted: bool
    message: str


class UninstallOutcome(BaseModel):
    name: str
    removed: bool
    generation: int
    persisted: bool
    message: str


class RoleExplanation(BaseModel):
    role: str
    generation: int
    source: dict[str, Any]
    primary: str
    fallbacks: list[str]
    reason: list[str]
    contract: dict[str, Any]
    checks: dict[str, Any]
    ok: bool


class GenerationHistory(BaseModel):
    generations: list[dict[str, Any]]


class CatalogListing(BaseModel):
    count: int
    sources: str
    entries: dict[str, Any]


class Recommendations(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    group: str
    capability_class: str = Field(alias="class")
    accelerator: str
    candidates: list[dict[str, Any]]


class RequestList(BaseModel):
    requests: list[ChangeRequest]


class RequestView(BaseModel):
    request: ChangeRequest


class ApproveRequest(BaseModel):
    reason: str = ""
    reload: bool = False


class RejectRequest(BaseModel):
    reason: str = Field(description="required — recorded and, for evolution proposals, remembered")


class DecisionOutcome(BaseModel):
    request: ChangeRequest
    applied: ApplyOutcome | None = None


class ProjectList(BaseModel):
    projects: list[dict[str, Any]]


class ProjectAdd(BaseModel):
    path: str
    name: str | None = None


class ProjectOutcome(BaseModel):
    project: dict[str, Any] | None = None
    removed: str | None = None
    message: str


class MemoryView(BaseModel):
    """A registered project's memory (contract 1.1.0, PR-19)."""

    project: str
    path: str
    exists: bool
    total: int
    counts: dict[str, int]
    records: list[dict[str, Any]]


class MemoryAdd(BaseModel):
    kind: Literal["project_rule", "coding_style", "architecture_decision"] = "project_rule"
    text: str = Field(min_length=1, description="the rule / style / decision to remember")


class MemoryAdded(BaseModel):
    id: int
    kind: str
    project: str
    message: str


# ── models ───────────────────────────────────────────────────────────────────


@router.get("/models", response_model=ModelList, operation_id="models_list", tags=["models"],
            summary="Registered models and their states (CLI: local-ezai status)",
            responses=PLATFORM_ERRORS)
def models_list(ctx: PlatformDep) -> ModelList:
    snapshot = ops.platform_snapshot(ctx)
    return ModelList(generation=snapshot["generation"], models=snapshot["models"])


@router.post("/models", response_model=InstallOutcome, operation_id="model_install",
             tags=["models"], summary="Fetch + validate a model (CLI: local-ezai model install)",
             responses=PLATFORM_ERRORS)
def model_install(request: Request, ctx: PlatformDep, body: InstallRequest) -> InstallOutcome:
    with request.app.state.mutation_lock:
        return InstallOutcome(**ops.install_model(ctx, body.ref, name=body.name,
                                                  runtime=body.runtime, group=body.group,
                                                  refetch=body.refetch))


@router.post("/models/{name}/benchmark", response_model=BenchmarkOutcome,
             operation_id="model_benchmark", tags=["models"],
             summary="Measure tokens/sec on this host (CLI: local-ezai model benchmark)",
             responses=PLATFORM_ERRORS)
def model_benchmark(request: Request, ctx: PlatformDep, name: str,
                    body: BenchmarkRequest | None = None) -> BenchmarkOutcome:
    with request.app.state.mutation_lock:
        return BenchmarkOutcome(**ops.benchmark_model(
            ctx, name, base_url=body.base_url if body else None))


@router.post("/models/{name}/activate", response_model=ProposalOutcome,
             operation_id="model_activate", tags=["models"],
             summary="Request activation — approval when a serving role changes "
                     "(CLI: local-ezai model activate)", responses=PLATFORM_ERRORS)
def model_activate(request: Request, ctx: PlatformDep, name: str,
                   body: ActivateRequest | None = None) -> ProposalOutcome:
    body = body or ActivateRequest()
    _check_reload(body.reload)
    with request.app.state.mutation_lock:
        return ProposalOutcome(**ops.activate_model(ctx, name, group=body.group, role=body.role,
                                                    position=body.position, reload=body.reload))


@router.post("/models/upgrade", response_model=ProposalOutcome, operation_id="model_upgrade",
             tags=["models"], summary="Swap a serving version for a benchmarked one — approval "
                                      "(CLI: local-ezai model upgrade)",
             responses=PLATFORM_ERRORS)
def model_upgrade(request: Request, ctx: PlatformDep, body: UpgradeRequest) -> ProposalOutcome:
    _check_reload(body.reload)
    with request.app.state.mutation_lock:
        return ProposalOutcome(**ops.upgrade_model(ctx, body.old, body.new, reload=body.reload))


@router.post("/generations/rollback", response_model=RollbackOutcome,
             operation_id="generation_rollback", tags=["models"],
             summary="Restore the previous or a named generation — no approval, audited "
                     "(CLI: local-ezai model rollback)", responses=PLATFORM_ERRORS)
def generation_rollback(request: Request, ctx: PlatformDep,
                        body: RollbackRequest | None = None) -> RollbackOutcome:
    body = body or RollbackRequest()
    _check_reload(body.reload)
    with request.app.state.mutation_lock:
        return RollbackOutcome(**ops.rollback_generation(
            ctx, to_generation=body.to_generation, reason=body.reason, reload=body.reload))


@router.post("/models/{name}/retire", response_model=RetireOutcome, operation_id="model_retire",
             tags=["models"], summary="Remove a non-serving active model from resolution "
                                      "(CLI: local-ezai model retire)",
             responses=PLATFORM_ERRORS)
def model_retire(request: Request, ctx: PlatformDep, name: str) -> RetireOutcome:
    with request.app.state.mutation_lock:
        return RetireOutcome(**ops.retire_model(ctx, name))


@router.delete("/models/{name}", response_model=UninstallOutcome, operation_id="model_uninstall",
               tags=["models"], summary="Delete the weights of a retired/failed model "
                                        "(CLI: local-ezai model uninstall)",
               responses=PLATFORM_ERRORS)
def model_uninstall(request: Request, ctx: PlatformDep, name: str,
                    force: Annotated[bool, Query(
                        description="even if a stored generation could roll back to it")] = False,
                    ) -> UninstallOutcome:
    with request.app.state.mutation_lock:
        return UninstallOutcome(**ops.uninstall_model(ctx, name, force=force))


@router.get("/roles/{role}", response_model=RoleExplanation, operation_id="role_explain",
            tags=["models"], summary="What serves a role, why, and the contract check "
                                     "(CLI: local-ezai model explain)", responses=PLATFORM_ERRORS)
def role_explain(ctx: PlatformDep, role: str) -> RoleExplanation:
    return RoleExplanation(**ops.explain_role(ctx, role))


@router.get("/generations", response_model=GenerationHistory, operation_id="generation_history",
            tags=["models"], summary="Generations with notes and diffs "
                                     "(CLI: local-ezai model history)", responses=PLATFORM_ERRORS)
def generation_history(ctx: PlatformDep,
                       limit: Annotated[int, Query(ge=1, le=500)] = 10) -> GenerationHistory:
    return GenerationHistory(**ops.generation_history(ctx, limit=limit))


@router.get("/catalog", response_model=CatalogListing, operation_id="catalog_list",
            tags=["catalog"], summary="Catalog entries (CLI: local-ezai model catalog)",
            responses=PLATFORM_ERRORS)
def catalog_list(ctx: PlatformDep) -> CatalogListing:
    return CatalogListing(**ops.catalog_listing(ctx))


@router.get("/catalog/recommendations", response_model=Recommendations,
            operation_id="catalog_recommend", tags=["catalog"],
            summary="Ranked recommendations for a group with fit verdicts "
                    "(CLI: local-ezai model catalog --group)", responses=PLATFORM_ERRORS)
def catalog_recommend(ctx: PlatformDep, group: Annotated[str, Query()],
                      runtime: Annotated[str | None, Query()] = None) -> Recommendations:
    return Recommendations.model_validate(ops.catalog_recommendations(ctx, group, runtime=runtime))


# ── governance ───────────────────────────────────────────────────────────────


@router.get("/governance", response_model=RequestList, operation_id="governance_list",
            tags=["governance"], summary="The approval queue (CLI: local-ezai governance list)",
            responses=PLATFORM_ERRORS)
def governance_list(ctx: PlatformDep,
                    status: Annotated[str | None, Query()] = None) -> RequestList:
    return RequestList(**ops.list_requests(ctx, status=status))


@router.get("/governance/{request_id}", response_model=RequestView, operation_id="governance_show",
            tags=["governance"], summary="One change request with its evidence "
                                         "(CLI: local-ezai governance show)",
            responses=PLATFORM_ERRORS)
def governance_show(ctx: PlatformDep, request_id: str) -> RequestView:
    return RequestView(**ops.show_request(ctx, request_id))


@router.post("/governance/{request_id}/approve", response_model=DecisionOutcome,
             operation_id="governance_approve", tags=["governance"],
             summary="Approve a pending request AND apply it — human act "
                     "(CLI: local-ezai governance approve)", responses=PLATFORM_ERRORS)
def governance_approve(request: Request, ctx: PlatformDep, request_id: str,
                       body: ApproveRequest | None = None) -> DecisionOutcome:
    body = body or ApproveRequest()
    _check_reload(body.reload)
    with request.app.state.mutation_lock:
        return DecisionOutcome(**ops.approve_request(ctx, request_id, reason=body.reason,
                                                     reload=body.reload))


@router.post("/governance/{request_id}/reject", response_model=DecisionOutcome,
             operation_id="governance_reject", tags=["governance"],
             summary="Reject a pending request with a recorded reason — human act "
                     "(CLI: local-ezai governance reject)", responses=PLATFORM_ERRORS)
def governance_reject(request: Request, ctx: PlatformDep, request_id: str,
                      body: RejectRequest) -> DecisionOutcome:
    with request.app.state.mutation_lock:
        return DecisionOutcome(**ops.reject_request(ctx, request_id, reason=body.reason))


# ── projects (chat-ops allowlist) ────────────────────────────────────────────


@router.get("/projects", response_model=ProjectList, operation_id="projects_list",
            tags=["projects"], summary="Registered repositories (CLI: local-ezai project list)",
            responses=PLATFORM_ERRORS)
def projects_list(ctx: PlatformDep) -> ProjectList:
    return ProjectList(**ops.list_projects(ctx))


@router.post("/projects", response_model=ProjectOutcome, operation_id="project_add",
             tags=["projects"], summary="Register a repository (CLI: local-ezai project add)",
             responses=PLATFORM_ERRORS)
def project_add(request: Request, ctx: PlatformDep, body: ProjectAdd) -> ProjectOutcome:
    with request.app.state.mutation_lock:
        return ProjectOutcome(**ops.add_project(ctx, body.path, name=body.name))


@router.delete("/projects", response_model=ProjectOutcome, operation_id="project_remove",
               tags=["projects"], summary="Unregister by name or path "
                                          "(CLI: local-ezai project remove)",
               responses=PLATFORM_ERRORS)
def project_remove(request: Request, ctx: PlatformDep,
                   target: Annotated[str, Query(description="project name or path")],
                   ) -> ProjectOutcome:
    with request.app.state.mutation_lock:
        return ProjectOutcome(**ops.remove_project(ctx, target))


# ── project memory (contract 1.1.0, PR-19 — additive) ────────────────────────


@router.get("/projects/{name}/memory", response_model=MemoryView, operation_id="project_memory",
            tags=["projects"], summary="Browse a registered project's memory "
                                       "(CLI: local-ezai memory)", responses=PLATFORM_ERRORS)
def project_memory(ctx: PlatformDep, name: str,
                   kind: Annotated[str | None, Query(description="one memory kind")] = None,
                   search: Annotated[str | None, Query(description="keyword search")] = None,
                   limit: Annotated[int, Query(ge=1, le=500)] = 50) -> MemoryView:
    return MemoryView(**ops.project_memory(ctx, name, kind=kind, search=search, limit=limit))


@router.post("/projects/{name}/memory", response_model=MemoryAdded,
             operation_id="project_memory_add", tags=["projects"],
             summary="Add a curated memory entry (CLI: local-ezai memory --add)",
             responses=PLATFORM_ERRORS)
def project_memory_add(request: Request, ctx: PlatformDep, name: str,
                       body: MemoryAdd) -> MemoryAdded:
    with request.app.state.mutation_lock:
        return MemoryAdded(**ops.add_project_memory(ctx, name, kind=body.kind, text=body.text))
