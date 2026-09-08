"""The ``ezaid`` FastAPI application (PR-8, ADR-028).

Skeleton surface — everything later PRs add hangs off these pieces:

- ``GET /health`` — liveness, unauthenticated (docker/compose healthcheck,
  ``local-ezai status``);
- ``GET /v1/health`` — the aggregated report: control-plane info, the
  platform snapshot ``local-ezai status`` shows, and one probe per stack
  service;
- ``GET /v1/whoami`` — the caller identity the audit log will record;
- ``GET /v1/audit`` — the tail of the platform's single audit log;
- ``GET /openapi.json`` / ``/docs`` — the contract (versioned artifact under
  ``docs/api/``, tripwire-tested).

Every ``/v1`` operation authenticates with the service token and accepts
the forwarded identity headers (``auth.py``); rejected calls are audited
(never with the token). Errors are one envelope everywhere
(``{"error": {"code", "message", "fix"}}``) — the vocabulary the CLI's
connected mode (PR-11) and the Admin Center (P4) print verbatim.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
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
    TOKEN_ENV,
    USER_HEADER,
)
from agentd.control.auth import AuthError, Caller, authenticate
from agentd.control.health import (
    Prober,
    ServiceHealth,
    ServiceTarget,
    httpx_prober,
    probe_services,
    targets_from,
)
from agentd.logging_setup import get_logger
from agentd.platform_cli import PlatformContext, platform_snapshot

log = get_logger("ezaid")

TITLE = "Local-EZAI Platform Control Plane (ezaid)"
DESCRIPTION = (
    "One authenticated control plane over the platform's declarative state — "
    "Registry v2 + generations, runtime descriptors, governance queue, rendered "
    "artifacts — for the `local-ezai` CLI (connected mode), the Admin Center and "
    "the SWE tool server. Every `/v1` call presents the service token as a bearer "
    f"credential and may forward the human it acts for (`{USER_HEADER}`) and its "
    f"own name (`{CLIENT_HEADER}`); the audit log records `<user> via <client>`."
)


# ── error envelope ───────────────────────────────────────────────────────────


class ErrorBody(BaseModel):
    code: str
    message: str
    fix: str = ""


class ErrorEnvelope(BaseModel):
    error: ErrorBody


class ApiError(Exception):
    """An error the API returns as the shared envelope."""

    def __init__(self, status: int, code: str, message: str, fix: str = "",
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.fix = status, code, message, fix
        self.headers = headers or {}


def envelope(status: int, code: str, message: str, fix: str = "",
             headers: dict[str, str] | None = None) -> JSONResponse:
    body = ErrorEnvelope(error=ErrorBody(code=code, message=message, fix=fix))
    return JSONResponse(status_code=status, content=body.model_dump(), headers=headers)


UNAUTHORIZED: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorEnvelope, "description": "missing or invalid service token"},
}


# ── response models ──────────────────────────────────────────────────────────


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


# ── authentication dependency ────────────────────────────────────────────────

bearer_scheme = HTTPBearer(auto_error=False, scheme_name="serviceToken",
                           description=f"the platform service token ({TOKEN_ENV})")

UserHeader = Annotated[str | None, Header(
    alias=USER_HEADER, description="forwarded identity of the human the caller acts for")]
ClientHeader = Annotated[str | None, Header(
    alias=CLIENT_HEADER, description="name of the calling surface (cli, admin-center, …)")]


def caller(request: Request,
           credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
           user: UserHeader = None, client: ClientHeader = None) -> Caller:
    state = request.app.state
    authorization = (f"{credentials.scheme} {credentials.credentials}"
                     if credentials is not None else request.headers.get("authorization"))
    try:
        return authenticate(state.settings.token or "", authorization, user=user, client=client)
    except AuthError as exc:
        _record(request.app, "auth.rejected", "anonymous", path=request.url.path,
                reason=exc.message, peer=request.client.host if request.client else "",
                claimed_client=(client or "")[:64])
        raise ApiError(401, exc.code, exc.message, exc.fix,
                       headers={"WWW-Authenticate": "Bearer"}) from exc


CallerDep = Annotated[Caller, Depends(caller)]


def _record(app: FastAPI, event: str, actor: str, **details: Any) -> None:
    audit: AuditLog | None = getattr(app.state, "audit", None)
    if audit is not None:
        audit.record(event, actor, **details)


# ── application ──────────────────────────────────────────────────────────────


def create_app(settings: ControlConfig, ctx: PlatformContext | None = None, *,
               audit: AuditLog | None = None, prober: Prober | None = None,
               targets: tuple[ServiceTarget, ...] | None = None,
               environ: Mapping[str, str] | None = None) -> FastAPI:
    """Build the service. ``ctx`` is the platform (None only for spec
    generation — the platform endpoints then answer 503); ``audit`` defaults
    to the platform's governance log; ``prober``/``targets``/``environ`` are
    the test seams of the health aggregation."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        app.state.started_mono = time.monotonic()
        _record(app, "control.started", SERVICE_NAME, version=__version__,
                contract=CONTRACT_VERSION, host=settings.host, port=settings.port,
                platform_root=str(ctx.root) if ctx else "")
        log.info("ezaid %s (contract %s) serving %s:%s", __version__, CONTRACT_VERSION,
                 settings.host, settings.port)
        yield
        _record(app, "control.stopped", SERVICE_NAME)

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

    # ── operations ───────────────────────────────────────────────────────

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
    import json

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
