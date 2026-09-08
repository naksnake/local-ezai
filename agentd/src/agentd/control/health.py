"""Health aggregation for ``GET /v1/health`` (PR-8; WEBUI_ADMIN_CENTER §3
Overview: "stack health (8 services + ezaid)").

The probe table mirrors what the monitor dashboard already checks, addressed
by the compose service names on ``ai-net`` — the engine slot through its
neutral ``engine`` alias (ADR-026). It is data: ``control.health_targets``
in the agentd config adds, replaces or removes entries; nothing here knows
a runtime or a model. Probes run concurrently and never raise.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx
from pydantic import BaseModel

#: ``prober(url, headers) -> (status_code, body_text)``; exceptions = down.
Prober = Callable[[str, dict[str, str]], tuple[int, str]]

PROBE_TIMEOUT_S = 3.0
BODY_LIMIT = 4000


@dataclass(frozen=True)
class ServiceTarget:
    id: str
    url: str
    #: Substring the body must contain (``""`` = status code only).
    pattern: str = ""
    #: Environment variable holding a bearer credential the probe presents.
    auth_env: str | None = None

    def headers(self, environ: Mapping[str, str]) -> dict[str, str]:
        if self.auth_env and environ.get(self.auth_env):
            return {"Authorization": f"Bearer {environ[self.auth_env]}"}
        return {}


DEFAULT_TARGETS: tuple[ServiceTarget, ...] = (
    ServiceTarget("engine", "http://engine:8000/health"),
    ServiceTarget("router", "http://litellm:4000/health/liveliness"),
    ServiceTarget("embed", "http://embed-server:8001/health", "healthy"),
    ServiceTarget("qdrant", "http://qdrant:6333/healthz", "passed"),
    ServiceTarget("searxng", "http://searxng:8080/healthz"),
    ServiceTarget("mcpo", "http://mcpo:8200/openapi.json", "openapi", auth_env="MCP_API_KEY"),
    ServiceTarget("openwebui", "http://openwebui:8080/health"),
    ServiceTarget("monitor", "http://monitor:8888/api/status", auth_env="MCP_API_KEY"),
)


def targets_from(overrides: Mapping[str, str] | None,
                 defaults: tuple[ServiceTarget, ...] = DEFAULT_TARGETS,
                 ) -> tuple[ServiceTarget, ...]:
    """Apply ``control.health_targets`` (id → URL): a known id gets the new
    URL (its pattern/credential kept), an unknown id is added as a plain
    status-code probe, an empty URL removes the target."""
    if not overrides:
        return defaults
    by_id = {target.id: target for target in defaults}
    for target_id, url in overrides.items():
        if not url:
            by_id.pop(target_id, None)
        elif target_id in by_id:
            current = by_id[target_id]
            by_id[target_id] = ServiceTarget(target_id, url, current.pattern, current.auth_env)
        else:
            by_id[target_id] = ServiceTarget(target_id, url)
    return tuple(by_id.values())


class ServiceHealth(BaseModel):
    id: str
    url: str
    ok: bool
    status: int | None = None
    latency_ms: int = 0
    error: str = ""


def httpx_prober(timeout: float = PROBE_TIMEOUT_S) -> Prober:
    def probe(url: str, headers: dict[str, str]) -> tuple[int, str]:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
            return response.status_code, response.text[:BODY_LIMIT]
    return probe


def probe_services(targets: tuple[ServiceTarget, ...], prober: Prober, *,
                   environ: Mapping[str, str] | None = None,
                   clock: Callable[[], float] = time.monotonic) -> list[ServiceHealth]:
    env = os.environ if environ is None else environ

    def one(target: ServiceTarget) -> ServiceHealth:
        start = clock()
        try:
            status, body = prober(target.url, target.headers(env))
        except Exception as exc:  # a down service is a result, not an error
            return ServiceHealth(id=target.id, url=target.url, ok=False,
                                 latency_ms=int((clock() - start) * 1000),
                                 error=f"{type(exc).__name__}: {exc}"[:200])
        return ServiceHealth(id=target.id, url=target.url,
                             ok=status < 400 and target.pattern in body, status=status,
                             latency_ms=int((clock() - start) * 1000))

    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        return list(pool.map(one, targets))
