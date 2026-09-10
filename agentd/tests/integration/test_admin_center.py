"""The Admin Center's first pages on the monitor (PR-16, ADR-030 Proposed;
WEBUI_ADMIN_CENTER §1–§2, CLI_AND_WEBUI_STRATEGY §3): the monitor renders
what the control plane serves — Overview, Runs, a deep-linkable run detail,
cancel for admins — and keeps its pre-existing health / knowledge duties.

Offline: the monitor app (monitor/monitor.py + admin_center.py, loaded from
the repository) runs under a TestClient; its control-plane client reaches the
in-process daemon through an ASGI transport, so the page data path is the real
one end to end (a real scripted run, the real audit log). One real-Chromium
smoke renders the pages with console errors failing the test (Browser QA is
part of validation)."""

from __future__ import annotations

import importlib.util
import os
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from agentd.config import ControlConfig
from agentd.control.app import create_app
from tests.unit import test_control_runs as control_runs
from tests.unit.test_control_runs import (
    AUTH,
    TOKEN,
    V1,
    Gate,
    gated_registry,
    wait_terminal,
)

#: The daemon fixtures of the run-endpoint tests, reused as they are.
platform_root = control_runs.platform_root
daemon = control_runs.daemon

REPO_ROOT = Path(__file__).resolve().parents[3]
MONITOR_DIR = REPO_ROOT / "monitor"
ADMIN = ("admin", "adm-pw")
VIEWER = ("viewer", "view-pw")
CONTROL_URL = "http://ezaid.test:8010"


def load_monitor(monkeypatch, *, token: str = TOKEN, extra_env: dict[str, str] | None = None):
    """The monitor service as shipped, configured through its environment."""
    monkeypatch.setenv("MONITOR_AUTH", "true")
    monkeypatch.setenv("MONITOR_ADMIN_PASSWORD", ADMIN[1])
    monkeypatch.setenv("MONITOR_VIEWER_PASSWORD", VIEWER[1])
    monkeypatch.setenv("EZAI_CONTROL_URL", CONTROL_URL)
    monkeypatch.setenv("EZAI_CONTROL_TOKEN", token)
    for key in ("MONITOR_SSO_TRUSTED_HEADER", "MONITOR_SSO_TRUSTED_SECRET", "MONITOR_SSO_ADMINS",
                "MONITOR_SSO_OPENWEBUI_URL"):
        monkeypatch.delenv(key, raising=False)  # SSO is opt-in (PR-20)
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.syspath_prepend(str(MONITOR_DIR))
    sys.modules.pop("monitor_under_test", None)
    spec = importlib.util.spec_from_file_location("monitor_under_test", MONITOR_DIR / "monitor.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_control(daemon, **kwargs) -> TestClient:
    """The daemon over the fixture platform; its stack probe answers every
    target's pattern, so the aggregated health is green."""
    app = create_app(ControlConfig(token=TOKEN), daemon.ctx,
                     prober=lambda url, headers: (200, "ok healthy passed openapi"), **kwargs)
    return TestClient(app)


class Recording(httpx.AsyncBaseTransport):
    """An ASGI transport into the daemon that keeps every request it carried —
    the proof of what the monitor sends (token, client, forwarded identity)."""

    def __init__(self, app) -> None:
        self.inner = httpx.ASGITransport(app=app)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self.inner.handle_async_request(request)


def attach(monitor, control: TestClient) -> tuple[TestClient, Recording]:
    """Point the monitor's control-plane client at the in-process daemon."""
    transport = Recording(control.app)
    monitor.app.state.control_plane.transport = transport
    return TestClient(monitor.app), transport  # no lifespan: the poll loop stays off


@pytest.fixture
def center(daemon, monkeypatch):
    with make_control(daemon) as control:
        monitor = load_monitor(monkeypatch)
        web, wire = attach(monitor, control)
        yield SimpleNamespace(daemon=daemon, control=control, monitor=monitor, web=web, wire=wire)


def audited(daemon) -> set[tuple[str, str]]:
    return {(record.event, record.actor) for record in daemon.ctx.queue.audit()}


def forwarded(wire: Recording, user: str) -> None:
    """Every call the monitor made carried the service token, named itself and
    forwarded the monitor login as the human (audit actor `<user> via admin-center`)."""
    assert wire.requests
    for request in wire.requests:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.headers["X-EZAI-Client"] == "admin-center"
        assert request.headers["X-EZAI-User"] == user
        assert request.url.path.startswith("/v1/")


# ── overview ─────────────────────────────────────────────────────────────────


def test_overview_renders_the_platform_from_the_control_plane(center):
    response = center.web.get("/api/ezai/overview", auth=VIEWER)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["connected"] is True and data["ok"] is True
    assert data["control_url"] == CONTROL_URL and data["control"]["contract"] == "1.2.0"
    platform = data["platform"]
    assert platform["generation"] == 1 and platform["capability_class"] == "cpu-low"
    assert platform["slot_runtime"] == "llamacpp" and platform["pending_approvals"] == 0
    services = {s["id"]: s for s in data["services"]}
    assert {"engine", "router", "monitor", "openwebui"} <= set(services)
    assert all(s["ok"] for s in services.values())
    roles = {r["role"]: r for r in data["roles"]}
    assert set(roles) == {"coder", "reviewer", "chat"}  # the roles this registry defines
    assert roles["coder"]["group"] == "coding" and roles["coder"]["primary"] == "alpha"
    assert roles["reviewer"]["pinned"] is True and roles["reviewer"]["primary"] == "alpha"
    assert data["pending"] == [] and data["runs"] == [] and data["active_runs"] == 0
    assert data["limits"]["max_concurrent"] >= 1
    # the monitor login is the human, the monitor the surface, the token stays server-side
    forwarded(center.wire, "viewer")
    assert {r.url.path for r in center.wire.requests} >= {
        "/v1/health", "/v1/roles/coder", "/v1/governance", "/v1/runs"}
    # reads are not audited by the daemon (only mutations are) — nothing was mutated
    assert not any(event.startswith("api.") for event, _ in audited(center.daemon))


def test_overview_shows_the_governance_queue_it_may_not_decide(center):
    proposal = center.control.post(f"{V1}/models/beta/activate", headers=AUTH,
                                   json={"group": "coding"})
    assert proposal.status_code == 200, proposal.text
    data = center.web.get("/api/ezai/overview", auth=ADMIN).json()
    assert [q["id"] for q in data["pending"]] == ["cr-0001"]
    assert data["pending"][0]["kind"] and "beta" in data["pending"][0]["title"]
    assert data["platform"]["pending_approvals"] == 1
    # the queue is shown here; deciding is the Governance page's (PR-18): a post that
    # is not the page's own (no same-origin header) never reaches the daemon
    forged = center.web.post("/api/ezai/governance/cr-0001/approve", auth=ADMIN)
    assert forged.status_code == 400 and forged.json()["error"]["code"] == "same_origin_required"
    assert center.daemon.ctx.queue.get("cr-0001").status == "pending"


def test_overview_degrades_honestly_when_the_control_plane_is_down(center):
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    center.monitor.app.state.control_plane.transport = httpx.MockTransport(refused)
    data = center.web.get("/api/ezai/overview", auth=VIEWER).json()
    assert data["connected"] is False
    assert data["error"]["code"] == "control_unreachable"
    assert CONTROL_URL in data["error"]["message"] and "make control-up" in data["error"]["fix"]
    # every other Admin Center call answers the same envelope, as HTTP 503
    runs = center.web.get("/api/ezai/runs", auth=VIEWER)
    assert runs.status_code == 503 and runs.json()["error"]["code"] == "control_unreachable"
    # the pre-existing dashboard needs no daemon
    assert center.web.get("/api/status", auth=VIEWER).status_code == 200
    home = center.web.get("/", auth=VIEWER)
    assert home.status_code == 200 and "Knowledge Base" in home.text


def test_a_missing_token_is_named_not_guessed(daemon, monkeypatch):
    with make_control(daemon) as control:
        web, wire = attach(load_monitor(monkeypatch, token=""), control)
        data = web.get("/api/ezai/overview", auth=VIEWER).json()
        assert data["connected"] is False and data["error"]["code"] == "control_token_missing"
        assert "EZAI_CONTROL_TOKEN" in data["error"]["fix"]
        assert wire.requests == []  # the daemon was never asked


# ── runs ─────────────────────────────────────────────────────────────────────


def test_runs_page_lists_and_details_a_real_run(center):
    started = center.control.post(f"{V1}/runs", headers=AUTH,
                                  json={"kind": "run", "project": "repo",
                                        "task": "fix the add bug"})
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    assert wait_terminal(center.control, run_id)["status"] == "completed"

    listed = center.web.get("/api/ezai/runs", auth=VIEWER).json()
    assert [r["run_id"] for r in listed["runs"]] == [run_id] and listed["active"] == 0
    assert center.web.get("/api/ezai/runs?kind=plan", auth=VIEWER).json()["runs"] == []
    assert center.web.get("/api/ezai/runs?status=completed",
                          auth=VIEWER).json()["runs"][0]["run_id"] == run_id

    detail = center.web.get(f"/api/ezai/runs/{run_id}?tail=500", auth=VIEWER).json()
    run, report = detail["run"], detail["report"]
    assert run["status"] == "completed" and run["actor"] == "nita via cli"
    assert run["project_name"] == "repo" and run["kind"] == "run"
    assert report["plan"]["tasks"] and report["validation"]["passed"] is True
    assert report["review"]["verdict"] == "approve"
    assert report["commit"]["sha"] and report["commit"]["pushed"] is False
    assert report["branch"].startswith("swe/")
    types = {event["type"] for event in detail["journal"]}
    assert {"TOOL_CALLED", "TOOL_RESULT"} <= types
    assert detail["journal_total"] == len(detail["journal"])  # tail=500 covers the whole run
    forwarded(center.wire, "viewer")
    assert {r.url.path for r in center.wire.requests} >= {
        f"/v1/runs/{run_id}", f"/v1/runs/{run_id}/report", f"/v1/runs/{run_id}/journal"}
    # the deep link the CLI and the SWE tools print is a page here
    page = center.web.get(f"/runs/{run_id}", auth=VIEWER)
    assert page.status_code == 200 and "Local-EZAI Admin Center" in page.text


def test_cancel_is_admin_only_and_audited_as_the_monitor_login(daemon, monkeypatch):
    gate = Gate()
    with make_control(daemon, runs=gated_registry(daemon, gate)) as control:
        web, wire = attach(load_monitor(monkeypatch), control)
        started = control.post(f"{V1}/runs", headers=AUTH,
                               json={"kind": "run", "project": "repo", "task": "hold"}).json()
        run_id = started["run_id"]
        assert gate.wait_started(run_id)
        # a running job: the detail says the report is pending, nothing fails
        detail = web.get(f"/api/ezai/runs/{run_id}", auth=VIEWER).json()
        assert detail["run"]["status"] == "running" and detail["report"] is None
        # viewer: refused by the monitor's RBAC before the daemon hears of it
        refused = web.post(f"/api/ezai/runs/{run_id}/cancel", auth=VIEWER)
        assert refused.status_code == 403 and "admin role" in refused.json()["detail"]
        assert web.post(f"/api/ezai/runs/{run_id}/cancel").status_code == 401  # anonymous
        # admin, but shaped like a cross-site form post (ambient Basic credentials, no
        # custom header): refused before the daemon hears of it
        forged = web.post(f"/api/ezai/runs/{run_id}/cancel", auth=ADMIN)
        assert forged.status_code == 400
        assert forged.json()["error"]["code"] == "same_origin_required"
        assert not any(event == "api.run_cancel" for event, _ in audited(daemon))
        assert not any(r.url.path.endswith("/cancel") for r in wire.requests)
        # admin from the page: the daemon cancels; the audit names the human and the surface
        cancelled = web.post(f"/api/ezai/runs/{run_id}/cancel", auth=ADMIN,
                             headers={"X-Requested-With": "admin-center"})
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["cancel_requested"] is True
        gate.release(run_id)
        assert wait_terminal(control, run_id)["status"] == "cancelled"
        assert ("api.run_cancel", "admin via admin-center") in audited(daemon)
        cancel_calls = [r for r in wire.requests if r.url.path.endswith("/cancel")]
        assert len(cancel_calls) == 1 and cancel_calls[0].headers["X-EZAI-User"] == "admin"
        assert cancel_calls[0].headers["Idempotency-Key"]  # a fresh key per mutation
        final = web.get(f"/api/ezai/runs/{run_id}", auth=ADMIN).json()
        assert final["run"]["status"] == "cancelled" and final["report"] is None


def test_daemon_errors_pass_through_as_the_shared_envelope(center):
    missing = center.web.get("/api/ezai/runs/20260101-000000-abcdef", auth=VIEWER)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    assert missing.json()["error"]["fix"]
    # the monitor never lets the browser talk to the daemon directly: no token in any page
    for path in ("/", "/overview", "/runs"):
        assert TOKEN not in center.web.get(path, auth=VIEWER).text


# ── pages · wiring ───────────────────────────────────────────────────────────


def test_pages_are_served_behind_the_monitor_login(center):
    for path in ("/overview", "/runs", "/runs/20260101-000000-abcdef"):
        anonymous = center.web.get(path)
        assert anonymous.status_code == 401
        assert anonymous.headers["www-authenticate"].startswith("Basic")
        page = center.web.get(path, auth=VIEWER)
        assert page.status_code == 200, path
        assert "Local-EZAI Admin Center" in page.text and "/api/ezai/" in page.text
        assert 'href="/"' in page.text  # back to health & knowledge
    home = center.web.get("/", auth=VIEWER).text
    assert 'href="/overview"' in home and 'href="/runs"' in home
    assert "Knowledge Base" in home and "rag-btn" in home  # the pre-existing dashboard, intact
    status = center.web.get("/api/status", auth=VIEWER).json()
    assert set(status["services"]) == {"vllm", "embed", "qdrant", "searxng", "litellm", "mcpo",
                                       "openwebui"}


def test_wiring_compose_dockerfile_env_and_the_chat_stack_baseline():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    monitor = compose["services"]["monitor"]
    env = dict(item.split("=", 1) for item in monitor["environment"])
    assert env["EZAI_CONTROL_URL"] == "${EZAI_CONTROL_URL_MONITOR:-http://ezaid:8010}"
    assert env["EZAI_CONTROL_TOKEN"] == "${EZAI_CONTROL_TOKEN:-ezai-control-key}"
    assert "host.docker.internal:host-gateway" in monitor["extra_hosts"]
    dockerfile = (REPO_ROOT / "monitor" / "Dockerfile").read_text(encoding="utf-8")
    assert "monitor/admin_center.py" in dockerfile
    assert "EZAI_CONTROL_URL_MONITOR" in (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    # the chat-stack baseline (PR-15) treats the wiring as additive: the fixture is unchanged
    spec = importlib.util.spec_from_file_location(
        "chat_stack_baseline_pr16", REPO_ROOT / "scripts" / "chat-stack-baseline.py")
    tool = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(tool)
    assert tool.snapshot() == tool.load_baseline()


# ── real browser smoke (Browser QA is part of validation) ────────────────────


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve(app):
    """The monitor under a real uvicorn in a thread (lifespan off: no poll loop)."""
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{port}/api/status", timeout=1.0)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    return server, thread, f"http://127.0.0.1:{port}"


def launch_chromium(playwright):
    try:
        return playwright.chromium.launch(headless=True)
    except Exception:
        fallback = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/nonexistent")) / "chromium"
        if fallback.exists():
            try:
                return playwright.chromium.launch(headless=True, executable_path=str(fallback))
            except Exception:
                pass
    pytest.skip("no usable chromium for the Admin Center smoke")


def test_browser_smoke_pages_render_without_console_errors(center):
    sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    planned = center.control.post(f"{V1}/runs", headers=AUTH,
                                  json={"kind": "plan", "project": "repo",
                                        "task": "look around"}).json()
    run_id = planned["run_id"]
    assert wait_terminal(center.control, run_id)["status"] == "completed"
    server, thread, base = serve(center.monitor.app)
    try:
        probe = httpx.get(f"{base}/api/ezai/overview", auth=VIEWER, timeout=30).json()
        assert probe["connected"] and probe["ok"], probe.get("services")
        with sync_api.sync_playwright() as playwright:
            browser = launch_chromium(playwright)
            context = browser.new_context(
                http_credentials={"username": VIEWER[0], "password": VIEWER[1]})
            page = context.new_page()
            problems: list[str] = []

            def expected(message) -> bool:
                """The browser logs every 4xx response as a console error: the
                favicon the monitor does not serve, and the deliberate
                not-found lookup below, are the only tolerated ones."""
                url = (message.location or {}).get("url", "")
                return "favicon" in url or "20260101-000000-abcdef" in url

            page.on("console", lambda message: problems.append(
                f"{message.text} @ {(message.location or {}).get('url', '')}")
                if message.type == "error" and not expected(message) else None)
            page.on("pageerror", lambda error: problems.append(f"pageerror: {error}"))

            page.goto(f"{base}/overview")
            page.wait_for_selector("#footer:not(:empty)")
            text = page.inner_text("body")
            assert "Platform healthy" in text and "generation 1" in text
            assert "coder" in text and "alpha" in text and "Governance queue empty" in text
            assert run_id in text  # recent runs
            assert page.title() == "Overview · Local-EZAI Admin Center"

            page.goto(f"{base}/runs/{run_id}")
            page.wait_for_selector("#footer:not(:empty)")
            text = page.inner_text("body")  # section titles render upper-cased (CSS)
            assert run_id in text and "a0 dry-run" in text.lower() and "Goal:" in text
            assert "journal" in text.lower()
            assert "Cancel run" not in text  # terminal: nothing to cancel
            assert f"Run {run_id}" in page.title()

            page.goto(f"{base}/runs?kind=plan")
            page.wait_for_selector("#footer:not(:empty)")
            assert run_id in page.inner_text("body")
            assert page.locator("select[data-filter=kind]").input_value() == "plan"

            page.goto(f"{base}/runs/20260101-000000-abcdef")
            page.wait_for_selector("#footer:not(:empty)")
            assert "not_found" in page.inner_text("body")
            assert problems == [], problems
            browser.close()
    finally:
        server.should_exit = True
        thread.join(10)
