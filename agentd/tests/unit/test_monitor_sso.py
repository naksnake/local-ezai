"""The OpenWebUI session handoff's guard rails (PR-20, ADR-030; review fixes):
the `token` cookie is looked up at OpenWebUI only when it is shaped like
OpenWebUI's JWT, the lookups share one client behind a small gate, and the
result cache — forged tokens included — stays bounded.

Offline: the monitor module as shipped (loaded like the Admin Center tests
load it), OpenWebUI faked through the module's transport seam."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from tests.integration.test_admin_center import load_monitor

OPENWEBUI = "http://openwebui.test"


def jwt(who: str) -> str:
    """A cookie shaped like OpenWebUI's `token` (header.payload.signature)."""
    return f"eyJhbGciOiJIUzI1NiJ9.{who}.c2lnbmF0dXJl"


@pytest.fixture
def sso(monkeypatch):
    monitor = load_monitor(monkeypatch, extra_env={"MONITOR_SSO_OPENWEBUI_URL": OPENWEBUI})
    calls: list[str] = []

    def openwebui(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "openwebui.test" and request.url.path == "/api/v1/auths/"
        token = request.headers["Authorization"].removeprefix("Bearer ")
        calls.append(token)
        if token.split(".")[1].startswith("admin"):
            return httpx.Response(200, json={"id": "u1", "email": "nita@example.com",
                                             "role": "admin"})
        return httpx.Response(401, json={"detail": "invalid token"})

    monitor.SSO_TRANSPORT = httpx.MockTransport(openwebui)
    return SimpleNamespace(monitor=monitor, calls=calls)


def identity(monitor, token):
    return asyncio.run(monitor._openwebui_identity(token))


@pytest.mark.parametrize("cookie", [None, "", "t-admin", "a.b", "a..c", "a.b.c.d", "a.b/c.d",
                                    "a b.c.d", "a.b.c\n", jwt("x" * 4096)])
def test_a_cookie_not_shaped_like_a_jwt_never_reaches_openwebui(sso, cookie):
    assert identity(sso.monitor, cookie) is None
    assert sso.calls == [] and sso.monitor._sso_cache == {}


def test_a_well_formed_bogus_token_is_looked_up_once_then_remembered(sso):
    forged = jwt("forged")
    assert identity(sso.monitor, forged) is None
    assert identity(sso.monitor, forged) is None
    assert sso.calls == [forged] and sso.monitor._sso_cache[forged][1] is None
    # once the entry has expired, OpenWebUI is asked again
    sso.monitor._sso_cache[forged] = (0.0, None)
    assert identity(sso.monitor, forged) is None and sso.calls == [forged, forged]
    # a genuine one resolves through the same shared client, and is cached too
    admin = identity(sso.monitor, jwt("admin-1"))
    assert admin == "admin" and admin.user == "nita@example.com" and admin.via == "openwebui"
    assert identity(sso.monitor, jwt("admin-1")) == "admin"
    assert sso.calls == [forged, forged, jwt("admin-1")]
    assert sso.monitor._sso_http() is sso.monitor._sso_client is not None


def test_the_cache_stays_bounded_under_a_flood_of_distinct_fresh_tokens(sso, monkeypatch):
    monitor = sso.monitor
    monkeypatch.setattr(monitor, "SSO_CACHE_MAX", 5)
    for i in range(3):  # stale entries go before anything fresh does
        monitor._sso_cache[jwt(f"stale-{i}")] = (0.0, None)
    tokens = [jwt(f"admin-{i}") for i in range(40)]
    for token in tokens:
        assert identity(monitor, token) == "admin"
    assert len(monitor._sso_cache) == 5
    assert set(monitor._sso_cache) == set(tokens[-5:])  # the oldest went, the newest stayed
    assert len(sso.calls) == 40


def test_lookups_run_through_one_gate(sso, monkeypatch):
    monitor = sso.monitor
    monkeypatch.setattr(monitor, "_sso_gate", asyncio.Semaphore(2))
    inflight, peak = 0, 0

    async def slow_openwebui(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.02)
        inflight -= 1
        return httpx.Response(200, json={"id": "u1", "email": "nita@example.com",
                                         "role": "admin"})

    monitor.SSO_TRANSPORT = httpx.MockTransport(slow_openwebui)

    async def flood():
        return await asyncio.gather(*[monitor._openwebui_identity(jwt(f"admin-{i}"))
                                      for i in range(12)])

    assert all(who == "admin" for who in asyncio.run(flood()))
    assert peak == 2 and len(monitor._sso_cache) == 12
