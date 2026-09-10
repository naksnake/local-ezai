"""Service-token authentication + forwarded identity (PR-8).

Every ``/v1`` call presents the platform's service token as a bearer
credential; the calling surface may forward the human it acts for
(``X-EZAI-User``) and name itself (``X-EZAI-Client``). The resulting
``Caller`` is what the audit log records as the actor. Token comparison is
constant-time; the token value never reaches a log line.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from agentd.control import TOKEN_ENV

MAX_IDENTITY = 128
DEFAULT_CLIENT = "api"


class AuthError(Exception):
    """A rejected call; ``message`` says why, ``fix`` what to send."""

    code = "unauthorized"

    def __init__(self, message: str, fix: str) -> None:
        super().__init__(message)
        self.message = message
        self.fix = fix


@dataclass(frozen=True)
class Caller:
    client: str
    user: str | None = None

    @property
    def actor(self) -> str:
        return f"{self.user} via {self.client}" if self.user else self.client


def _clean(value: str | None, *, default: str | None = None) -> str | None:
    if value is None:
        return default
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()[:MAX_IDENTITY]
    return cleaned or default


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, credential = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return None
    return credential.strip()


def authenticate(expected_token: str, authorization: str | None, *,
                 user: str | None = None, client: str | None = None) -> Caller:
    """Validate the bearer token against ``expected_token`` and build the
    caller identity. Raises ``AuthError`` (never revealing the token)."""
    if not expected_token:
        raise AuthError("the control plane has no service token configured",
                        f"set {TOKEN_ENV} (or control.token) and restart ezaid")
    presented = bearer_token(authorization)
    if presented is None:
        raise AuthError("missing bearer token",
                        f"send `Authorization: Bearer <{TOKEN_ENV}>`")
    if not hmac.compare_digest(presented.encode("utf-8"), expected_token.encode("utf-8")):
        raise AuthError("invalid service token",
                        f"the token must equal {TOKEN_ENV} of the platform's .env")
    return Caller(client=_clean(client, default=DEFAULT_CLIENT) or DEFAULT_CLIENT,
                  user=_clean(user))
