"""The ``ezaid`` FastAPI application (PR-8 skeleton, PR-9 endpoints; ADR-028).

Surface:

- ``GET /health`` — liveness, unauthenticated (docker/compose healthcheck,
  ``local-ezai status``);
- ``GET /v1/health`` — the aggregated report: control-plane info, the
  platform snapshot ``local-ezai status`` shows, and one probe per stack
  service;
- ``GET /v1/whoami`` — the caller identity the audit log records;
- ``GET /v1/audit`` — the tail of the platform's single audit log;
- the lifecycle, governance and project operations of ``api.py`` (PR-9) —
  the same functions the direct-mode CLI verbs call;
- ``GET /openapi.json`` / ``/docs`` — the contract (versioned artifact under
  ``docs/api/``, tripwire-tested).

Every ``/v1`` operation authenticates with the service token and accepts
the forwarded identity headers (``deps.py``); rejected calls are audited
(never with the token). Errors are one envelope everywhere
(``{"error": {"code", "message", "fix"}}``) — platform exceptions are
classified by ``platform_errors`` exactly as the CLI classifies them.
Mutating calls (``POST``/``DELETE``) pass through one middleware that
honors ``Idempotency-Key`` (replay / conflict) and audits the call with the
caller's identity and the operation id.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from agentd import __version__
from agentd.audit import AuditLog, AuditRecord
from agentd.config import ControlConfig
from agentd.control import (
    API_PREFIX,
    CLIENT_HEADER,
    CONTRACT_VERSION,
    DEFAULT_PORT,
    SERVICE_NAME,
    USER_HEADER,
)
from agentd.control.api import router as v1_router
from agentd.control.deps import (  # noqa: F401 — ApiError/ErrorEnvelope re-exported
    UNAUTHORIZED,
    ApiError,
    CallerDep,
    ErrorBody,
    ErrorEnvelope,
    envelope,
    record,
)
from agentd.control.health import (
    Prober,
    ServiceHealth,
    ServiceTarget,
    httpx_prober,
    probe_services,
    targets_from,
)
from agentd.control.idempotency import DIRNAME as IDEMPOTENCY_DIRNAME
from agentd.control.idempotency import HEADER as IDEMPOTENCY_HEADER
from agentd.control.idempotency import MAX_KEY_LENGTH, REPLAYED_HEADER, IdempotencyStore
from agentd.control.runs import DIRNAME as RUNS_DIRNAME
from agentd.control.runs import REFUSAL_STATUS, RunRefused, RunRegistry
from agentd.control.runs_api import router as runs_router
from agentd.logging_setup import get_logger
from agentd.platform_cli import PlatformContext, platform_snapshot
from agentd.platform_errors import classify, platform_exceptions

log = get_logger("ezaid")

TITLE = "Local-EZAI Platform Control Plane (ezaid)"
DESCRIPTION = (
    "One authenticated control plane over the platform's declarative state — "
    "Registry v2 + generations, runtime descriptors, governance queue, rendered "
    "artifacts — for the `local-ezai` CLI (connected mode), the Admin Center and "
    "the SWE tool server. Every `/v1` call presents the service token as a bearer "
    f"credential and may forward the human it acts for (`{USER_HEADER}`) and its "
    f"own name (`{CLIENT_HEADER}`); the audit log records `<user> via <client>`. "
    f"Mutating calls accept an `{IDEMPOTENCY_HEADER}` header: a retry with the same "
    "key and payload replays the stored response, a different payload is a conflict. "
    "Every error is `{\"error\": {\"code\", \"message\", \"fix\"}}` — the same object "
    "the CLI prints."
)
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CONTROL_DIRNAME = "control"


# ── response models of the skeleton operations ───────────────────────────────


class Liveness(BaseModel):
    status: str = "ok"
    service: str = SERVICE_NAME
    version: str
    contract: str
    uptime_s: float


class ControlInfo(BaseModel):
    service: str = SERVICE_NAME
    version: str
    contract: str
    started_at: str
    uptime_s: float
    platform_root: str = ""


class Identity(BaseModel):
    actor: str = Field(description="what the audit log records: `<user> via <client>`")
    client: str
    user: str | None = None


class HealthReport(BaseModel):
    ok: bool = Field(description="every probed service answered healthy")
    control: ControlInfo
    platform: dict[str, Any] = Field(
        description="the `local-ezai status` snapshot: generation, rendered generation, "
                    "slot runtime, model states, pending approvals")
    services: list[ServiceHealth]


class AuditPage(BaseModel):
    total: int
    records: list[AuditRecord]


def _operation_id(request: Request) -> str:
    route = request.scope.get("route")
    return (getattr(route, "operation_id", None) or getattr(route, "name", None)
            or f"{request.method} {request.url.path}")


# ── application ──────────────────────────────────────────────────────────────


def create_app(settings: ControlConfig, ctx: PlatformContext | None = None, *,
               audit: AuditLog | None = None, prober: Prober | None = None,
               targets: tuple[ServiceTarget, ...] | None = None,
               environ: Mapping[str, str] | None = None,
               runs: RunRegistry | None = None) -> FastAPI:
    """Build the service. ``ctx`` is the platform (None only for spec
    generation — the platform endpoints then answer 503); ``audit`` defaults
    to the platform's governance log; ``prober``/``targets``/``environ`` are
    the test seams of the health aggregation; ``runs`` replaces the run
    registry (tests inject fake pipelines)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        app.state.started_mono = time.monotonic()
        record(app, "control.started", SERVICE_NAME, version=__version__,
               contract=CONTRACT_VERSION, host=settings.host, port=settings.port,
               platform_root=str(ctx.root) if ctx else "")
        log.info("ezaid %s (contract %s) serving %s:%s", __version__, CONTRACT_VERSION,
                 settings.host, settings.port)
        yield
        if app.state.runs is not None:
            app.state.runs.shutdown(wait=False)
        record(app, "control.stopped", SERVICE_NAME)

    app = FastAPI(title=TITLE, version=CONTRACT_VERSION, description=DESCRIPTION,
                  lifespan=lifespan, docs_url="/docs", redoc_url=None,
                  openapi_url="/openapi.json")
    app.state.settings = settings
    app.state.ctx = ctx
    app.state.audit = audit if audit is not None else (
        AuditLog(ctx.queue.log_path) if ctx is not None else None)
    app.state.prober = prober or httpx_prober()
    app.state.targets = targets if targets is not None else targets_from(settings.health_targets)
    app.state.environ = environ
    app.state.started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    app.state.started_mono = time.monotonic()
    app.state.mutation_lock = threading.Lock()
    app.state.idempotency = (
        IdempotencyStore(ctx.config_dir / CONTROL_DIRNAME / IDEMPOTENCY_DIRNAME)
        if ctx is not None else None)
    if runs is not None:
        app.state.runs = runs
    elif ctx is not None:
        app.state.runs = RunRegistry(ctx.config, ctx.config_dir / CONTROL_DIRNAME / RUNS_DIRNAME,
                                     audit=app.state.audit,
                                     max_concurrent=settings.max_concurrent_runs,
                                     max_queued=settings.max_queued_runs)
    else:
        app.state.runs = None

    def control_info() -> ControlInfo:
        return ControlInfo(version=__version__, contract=CONTRACT_VERSION,
                           started_at=app.state.started_at,
                           uptime_s=round(time.monotonic() - app.state.started_mono, 3),
                           platform_root=str(ctx.root) if ctx else "")

    # ── error handlers: one envelope everywhere ──────────────────────────

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return envelope(exc.status, exc.code, exc.message, exc.fix, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed"}
        return envelope(exc.status_code, codes.get(exc.status_code, f"http_{exc.status_code}"),
                        str(exc.detail), "see /openapi.json for the operations this service offers",
                        dict(exc.headers or {}))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', []))}: {err.get('msg', '')}"
            for err in exc.errors())
        return envelope(422, "invalid_request", problems or "invalid request",
                        "check the parameters against /openapi.json")

    async def _platform_failure(_: Request, exc: Exception) -> JSONResponse:
        info = classify(exc)  # the CLI's classification — same code, message, fix
        return envelope(info.status, info.code, info.message, info.fix)

    for exception_class in platform_exceptions():
        app.add_exception_handler(exception_class, _platform_failure)

    @app.exception_handler(RunRefused)
    async def _run_refused(_: Request, exc: RunRefused) -> JSONResponse:
        return envelope(REFUSAL_STATUS.get(exc.code, 409), exc.code, exc.message, exc.fix)

    # ── mutating calls: idempotency + audit ──────────────────────────────

    @app.middleware("http")
    async def govern_mutations(request: Request, call_next):
        if request.method not in MUTATING_METHODS:
            return await call_next(request)
        store: IdempotencyStore | None = app.state.idempotency
        key = request.headers.get(IDEMPOTENCY_HEADER)
        if key is not None and len(key) > MAX_KEY_LENGTH:
            return envelope(400, "invalid_request",
                            f"{IDEMPOTENCY_HEADER} is longer than {MAX_KEY_LENGTH} characters",
                            "use a short unique key (a UUID)")
        fingerprint: str | None = None
        if key and store is not None:
            body = await request.body()
            fingerprint = IdempotencyStore.fingerprint(request.method, request.url.path,
                                                       request.url.query, body)
            stored = store.get(key)
            if stored is not None:
                if stored.fingerprint != fingerprint:
                    return envelope(409, "idempotency_conflict",
                                    f"{IDEMPOTENCY_HEADER} {key!r} was already used for a "
                                    "different request",
                                    "use a new key for every new operation; reuse a key only "
                                    "to retry the same one")
                return Response(content=stored.body, status_code=stored.status,
                                media_type=stored.media_type,
                                headers={REPLAYED_HEADER: "true", IDEMPOTENCY_HEADER: key})
        response = await call_next(request)
        raw = b"".join([chunk async for chunk in response.body_iterator])
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        replay = Response(content=raw, status_code=response.status_code, headers=headers)
        operation = _operation_id(request)
        if key and store is not None and fingerprint is not None and response.status_code < 500:
            store.put(key, fingerprint, response.status_code, raw,
                      response.headers.get("content-type", "application/json"), operation)
            replay.headers[IDEMPOTENCY_HEADER] = key
        who = getattr(request.state, "caller", None)
        if who is not None:  # rejected calls are already audited as auth.rejected
            record(app, f"api.{operation}", who.actor, method=request.method,
                   path=request.url.path, status=response.status_code, client=who.client,
                   idempotency_key=key or "")
        return replay

    # ── skeleton operations ──────────────────────────────────────────────

    @app.get("/health", response_model=Liveness, operation_id="liveness", tags=["control"],
             summary="Liveness (unauthenticated)")
    def liveness() -> Liveness:
        return Liveness(version=__version__, contract=CONTRACT_VERSION,
                        uptime_s=round(time.monotonic() - app.state.started_mono, 3))

    @app.get(f"{API_PREFIX}/health", response_model=HealthReport, operation_id="aggregated_health",
             tags=["health"], summary="Stack + platform health", responses={
                 **UNAUTHORIZED,
                 503: {"model": ErrorEnvelope, "description": "not attached to a platform"}})
    def aggregated_health(_: CallerDep) -> HealthReport:
        if ctx is None:
            raise ApiError(503, "platform_unavailable", "ezaid is not attached to a platform",
                           "start ezaid inside the local-ezai checkout or set "
                           "AGENTD_PLATFORM__CONFIG_DIR")
        services = probe_services(app.state.targets, app.state.prober, environ=app.state.environ)
        return HealthReport(ok=all(s.ok for s in services), control=control_info(),
                            platform=platform_snapshot(ctx), services=services)

    @app.get(f"{API_PREFIX}/whoami", response_model=Identity, operation_id="whoami",
             tags=["control"], summary="The identity the audit log records for this caller",
             responses=UNAUTHORIZED)
    def whoami(who: CallerDep) -> Identity:
        return Identity(actor=who.actor, client=who.client, user=who.user)

    @app.get(f"{API_PREFIX}/audit", response_model=AuditPage, operation_id="audit_tail",
             tags=["governance"], summary="Tail of the platform audit log",
             responses={**UNAUTHORIZED,
                        503: {"model": ErrorEnvelope, "description": "no audit log attached"}})
    def audit_tail(_: CallerDep,
                   limit: Annotated[int, Query(ge=1, le=1000,
                                               description="newest records to return")] = 50,
                   ) -> AuditPage:
        audit_log: AuditLog | None = app.state.audit
        if audit_log is None:
            raise ApiError(503, "audit_unavailable", "ezaid has no audit log attached",
                           "start ezaid inside a platform")
        return AuditPage(total=audit_log.count(), records=audit_log.tail(limit))

    app.include_router(v1_router)  # PR-9: lifecycle · governance · projects
    app.include_router(runs_router)  # PR-10: async run registry

    # ── the contract document ────────────────────────────────────────────

    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=CONTRACT_VERSION,
                             description=app.description, routes=app.routes)
        schema["info"]["x-contract-status"] = "draft — frozen by the P2 phase-close PR"
        schema["servers"] = [{"url": f"http://localhost:{DEFAULT_PORT}",
                              "description": "compose overlay (EZAI_CONTROL_PORT)"}]
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
    return app


# ── the contract artifact ────────────────────────────────────────────────────


def spec_json(app: FastAPI) -> str:
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


#: Schemas FastAPI adds for its own validation errors — rendering detail,
#: not contract.
FRAMEWORK_SCHEMAS = frozenset({"HTTPValidationError", "ValidationError"})


def contract_surface(spec: Mapping[str, Any]) -> dict[str, Any]:
    """The parts of an OpenAPI document that ARE the contract: version,
    operations (method, path, id, parameters, response codes, security,
    tags), security schemes and the named schemas. Rendering details that
    differ between framework versions are left out, so the tripwire that
    compares the committed artifact with the live app fails on contract
    changes only."""
    operations: dict[str, Any] = {}
    for path, methods in sorted(spec.get("paths", {}).items()):
        for method, op in sorted(methods.items()):
            operations[f"{method.upper()} {path}"] = {
                "operationId": op.get("operationId"),
                "parameters": sorted(f"{p.get('in')}:{p.get('name')}"
                                     for p in op.get("parameters", [])),
                "responses": sorted(op.get("responses", {})),
                "security": sorted(sorted(s) for s in op.get("security", [])),
                "tags": list(op.get("tags", [])),
            }
    components = spec.get("components", {})
    return {
        "title": spec.get("info", {}).get("title"),
        "version": spec.get("info", {}).get("version"),
        "operations": operations,
        "securitySchemes": sorted(components.get("securitySchemes", {})),
        "schemas": sorted(name for name in components.get("schemas", {})
                          if name not in FRAMEWORK_SCHEMAS),
    }
