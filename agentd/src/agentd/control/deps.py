"""Building blocks every ``ezaid`` operation shares (PR-8/PR-9):

- the **error envelope** ``{"error": {"code", "message", "fix"}}`` and the
  ``ApiError`` that produces it;
- the **caller** dependency — service-token authentication + forwarded
  identity (``auth.py``), audited on rejection;
- the **platform** dependency — the ``PlatformContext`` the direct-mode CLI
  uses, with the caller as the actor every audit record names.

Kept apart from ``app.py`` so the endpoint modules can import them without
a cycle.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from agentd.audit import AuditLog
from agentd.control import CLIENT_HEADER, TOKEN_ENV, USER_HEADER
from agentd.control.auth import AuthError, Caller, authenticate
from agentd.platform_cli import PlatformContext

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
PLATFORM_ERRORS: dict[int | str, dict[str, Any]] = {
    **UNAUTHORIZED,
    404: {"model": ErrorEnvelope, "description": "no such model / request / role / project"},
    409: {"model": ErrorEnvelope,
          "description": "refused by a lifecycle or governance rule (message names the fix)"},
    422: {"model": ErrorEnvelope, "description": "invalid request or unservable proposal"},
    503: {"model": ErrorEnvelope, "description": "not attached to a platform"},
}


def record(app: FastAPI, event: str, actor: str, **details: Any) -> None:
    audit: AuditLog | None = getattr(app.state, "audit", None)
    if audit is not None:
        audit.record(event, actor, **details)


# ── caller: service token + forwarded identity ───────────────────────────────

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
        who = authenticate(state.settings.token or "", authorization, user=user, client=client)
    except AuthError as exc:
        record(request.app, "auth.rejected", "anonymous", path=request.url.path,
               reason=exc.message, peer=request.client.host if request.client else "",
               claimed_client=(client or "")[:64])
        raise ApiError(401, exc.code, exc.message, exc.fix,
                       headers={"WWW-Authenticate": "Bearer"}) from exc
    request.state.caller = who  # the audit middleware names the actor from it
    return who


CallerDep = Annotated[Caller, Depends(caller)]


# ── platform: the direct-mode context with the caller as actor ───────────────


def platform(request: Request, who: CallerDep) -> PlatformContext:
    ctx: PlatformContext | None = request.app.state.ctx
    if ctx is None:
        raise ApiError(503, "platform_unavailable", "ezaid is not attached to a platform",
                       "start ezaid inside the local-ezai checkout or set "
                       "AGENTD_PLATFORM__CONFIG_DIR")
    return replace(ctx, actor=who.actor)


PlatformDep = Annotated[PlatformContext, Depends(platform)]
