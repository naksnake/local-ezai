"""P4 close (PR-20, ADR-030 → Accepted): the SSO handoff into the Admin
Center, the shared approval queue between CLI and console (P4 exit criterion
1), and the five zero-CLI journeys of WEBUI_PRODUCT_STRATEGY §5 run by the
platform's own Browser QA harness (P4 exit criterion 3)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from agentd import platform_cli
from agentd.browser_qa import BrowserQAHarness, validate_workflows
from agentd.config import BrowserQAConfig
from agentd.workspace import Workspace
from tests.integration import test_admin_center_models as models_tests
from tests.integration.test_admin_center import (
    ADMIN,
    AUTH,
    V1,
    VIEWER,
    attach,
    launch_chromium,
    load_monitor,
)
from tests.integration.test_admin_center_models import PAGE, audited, registry
from tests.integration.test_admin_center_workbench import make_repo

REPO_ROOT = Path(__file__).resolve().parents[3]
JOURNEYS = REPO_ROOT / "agentd" / "examples" / "browser-qa.admin-center.yaml"
SSO_ENV = {"MONITOR_SSO_TRUSTED_HEADER": "X-Forwarded-Email",
           "MONITOR_SSO_TRUSTED_SECRET": "proxy-secret",
           "MONITOR_SSO_ADMINS": "Nita@example.com",
           "MONITOR_SSO_OPENWEBUI_URL": "http://openwebui.test"}
PROXY_ADMIN = {"X-Forwarded-Email": "nita@example.com", "X-EZAI-Proxy-Secret": "proxy-secret"}
PROXY_GUEST = {"X-Forwarded-Email": "guest@example.com", "X-EZAI-Proxy-Secret": "proxy-secret"}

#: The PR-17 fixture (daemon with faked lifecycle seams, monitor without SSO).
center = models_tests.center


@pytest.fixture
def sso(center, monkeypatch):
    """A second monitor instance over the same daemon, SSO switched on; a
    registered project so curated memory has somewhere to go."""
    platform_cli.add_project(center.ctx, str(make_repo(center.weights / "repo")))
    monitor = load_monitor(monkeypatch, extra_env=SSO_ENV)
    web, wire = attach(monitor, center.control)
    return SimpleNamespace(center=center, monitor=monitor, web=web, wire=wire)


# ── SSO handoff ──────────────────────────────────────────────────────────────


def test_trusted_header_handoff_names_the_human_and_needs_the_proxy_secret(sso):
    assert sso.web.get("/api/ezai/overview", headers=PROXY_ADMIN).json()["role"] == "admin"
    assert sso.web.get("/api/ezai/overview", headers=PROXY_GUEST).json()["role"] == "viewer"
    # a header without the secret — or with a wrong one — is never trusted
    bare = sso.web.get("/api/ezai/overview", headers={"X-Forwarded-Email": "nita@example.com"})
    assert bare.status_code == 401 and bare.headers["www-authenticate"].startswith("Basic")
    assert sso.web.get("/api/ezai/overview",
                       headers={**PROXY_ADMIN, "X-EZAI-Proxy-Secret": "wrong"}).status_code == 401
    # the human, not the role, reaches the control plane and its audit trail
    added = sso.web.post("/api/ezai/memory", headers={**PROXY_ADMIN, **PAGE},
                         json={"project": "repo", "kind": "project_rule", "text": "via proxy"})
    assert added.status_code == 200, added.text
    assert sso.wire.requests[-1].headers["X-EZAI-User"] == "nita@example.com"
    assert {r.headers["X-EZAI-User"] for r in sso.wire.requests} == {"nita@example.com",
                                                                     "guest@example.com"}
    assert ("memory.added", "nita@example.com via admin-center") in audited(sso.center)
    # a proxied viewer reads and never mutates
    assert sso.web.post("/api/ezai/memory", headers={**PROXY_GUEST, **PAGE},
                        json={"project": "repo", "kind": "project_rule",
                              "text": "x"}).status_code == 403
    assert sso.web.get("/api/ezai/memory", headers=PROXY_GUEST).json()["memory"]["total"] == 1


def test_openwebui_session_handoff_is_validated_against_openwebui_and_cached(sso):
    calls: list[str] = []
    users = {"t-admin": {"email": "nita@example.com", "role": "admin"},
             "t-user": {"email": "guest@example.com", "role": "user"},
             "t-pending": {"email": "new@example.com", "role": "pending"}}

    def openwebui(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/auths/" and request.url.host == "openwebui.test"
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        calls.append(token)
        if token in users:
            return httpx.Response(200, json={"id": f"id-{token}", "name": "Someone",
                                             **users[token]})
        return httpx.Response(401, json={"detail": "invalid token"})

    sso.monitor.SSO_TRANSPORT = httpx.MockTransport(openwebui)
    sso.web.cookies.set("token", "t-admin")
    data = sso.web.get("/api/ezai/overview").json()
    assert data["connected"] and data["role"] == "admin"
    assert sso.wire.requests[-1].headers["X-EZAI-User"] == "nita@example.com"
    sso.web.get("/api/ezai/models")  # a second page: the lookup is cached
    assert calls == ["t-admin"]
    sso.web.cookies.set("token", "t-user")
    assert sso.web.get("/api/ezai/overview").json()["role"] == "viewer"
    assert sso.web.post("/api/ezai/models/gamma/benchmark", headers=PAGE).status_code == 403
    sso.web.cookies.set("token", "t-pending")
    assert sso.web.get("/api/ezai/overview").status_code == 401  # not yet activated in OpenWebUI
    sso.web.cookies.set("token", "t-forged")
    forged = sso.web.get("/api/ezai/overview")
    assert forged.status_code == 401 and forged.headers["www-authenticate"].startswith("Basic")
    # Basic stays the fallback next to the cookie; the dashboard is unchanged
    sso.web.cookies.clear()
    assert sso.web.get("/api/ezai/overview", auth=VIEWER).json()["role"] == "viewer"
    assert sso.web.get("/api/status", auth=ADMIN).status_code == 200


def test_sso_is_opt_in(center):
    """The PR-16 monitor (no SSO environment) ignores headers and cookies."""
    assert center.web.get("/api/ezai/overview", headers=PROXY_ADMIN).status_code == 401
    center.web.cookies.set("token", "t-admin")
    assert center.web.get("/api/ezai/overview").status_code == 401
    center.web.cookies.clear()
    assert center.web.get("/api/ezai/overview", auth=ADMIN).json()["role"] == "admin"


# ── P4 exit criterion 1: one shared queue, decided on either surface ─────────


def test_activation_requested_in_cli_is_approved_in_the_admin_center_and_vice_versa(center):
    # CLI proposes …
    proposal = center.control.post(f"{V1}/models/beta/activate", headers=AUTH,
                                   json={"group": "coding"}).json()["request"]
    assert proposal["requested_by"] == "nita via cli" and proposal["status"] == "pending"
    # … the Admin Center sees it and approves it
    queue = center.web.get("/api/ezai/governance", auth=ADMIN).json()
    assert [r["id"] for r in queue["requests"]] == [proposal["id"]]
    approved = center.web.post(f"/api/ezai/governance/{proposal['id']}/approve", auth=ADMIN,
                               headers=PAGE, json={"reason": "reviewed in the console"})
    assert approved.status_code == 200 and approved.json()["applied"]["ok"]
    assert approved.json()["request"]["decision"]["by"] == "admin via admin-center"
    generation = registry(center).generation
    # the Admin Center proposes (gamma needs evidence first) …
    assert center.web.post("/api/ezai/models/gamma/benchmark", auth=ADMIN,
                           headers=PAGE).status_code == 200
    second = center.web.post("/api/ezai/models/gamma/activate", auth=ADMIN, headers=PAGE,
                             json={"group": "chat"}).json()["request"]
    assert second["requested_by"] == "admin via admin-center" and second["status"] == "pending"
    # … the CLI sees it in the same queue and approves it
    listed = center.control.get(f"{V1}/governance", headers=AUTH,
                                params={"status": "pending"}).json()["requests"]
    assert [r["id"] for r in listed] == [second["id"]]
    decided = center.control.post(f"{V1}/governance/{second['id']}/approve", headers=AUTH,
                                  json={"reason": "reviewed on the CLI"})
    assert decided.status_code == 200 and decided.json()["applied"]["ok"]
    assert registry(center).generation > generation
    history = center.web.get("/api/ezai/governance", auth=VIEWER).json()
    assert history["counts"]["applied"] == 2 and history["pending"] == 0
    assert {r["decision"]["by"] for r in history["requests"]} == {"admin via admin-center",
                                                                  "nita via cli"}


# ── P4 exit criterion 3: the journeys on the platform's own Browser QA ───────


def test_the_five_zero_cli_journeys_run_on_the_platforms_browser_qa(tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    with sync_api.sync_playwright() as playwright:  # skip when no Chromium can launch
        launch_chromium(playwright).close()
    text = JOURNEYS.read_text(encoding="utf-8")
    text = text.replace("{python}", sys.executable).replace("{state}", str(tmp_path / "state"))
    config = BrowserQAConfig.model_validate(yaml.safe_load(text)["browser_qa"])
    assert validate_workflows(config.workflows) == []
    workspace = Workspace(root=REPO_ROOT, repo_path=REPO_ROOT, branch="main", mode="in-place")
    report = BrowserQAHarness(config, tmp_path / "artifacts").run(workspace)
    assert report.error is None, f"{report.error}\n{report.app_log_tail}"
    assert [w.name for w in report.workflows] == [
        "j1-swap-the-reasoning-model", "j2-runtime-switch-pre-check",
        "j3-register-project-and-run-a-sprint", "j4-trigger-evolution-and-review",
        "j5-roll-back-from-the-overview-banner"]
    for workflow in report.workflows:
        assert workflow.passed, (workflow.name, workflow.failed_step, workflow.console_errors,
                                 workflow.page_errors, report.app_log_tail[-3000:])
    assert report.passed and report.summary == "all 5 workflow(s) passed"
    shots = [s for w in report.workflows for s in w.screenshots]
    assert len(shots) == 7 and all(Path(s).is_file() and Path(s).stat().st_size > 0 for s in shots)
    assert [len(w.screenshots) for w in report.workflows] == [3, 1, 1, 1, 1]


# ── wiring ───────────────────────────────────────────────────────────────────


def test_wiring_compose_env_example_and_the_chat_stack_baseline():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    env = dict(item.split("=", 1) for item in compose["services"]["monitor"]["environment"])
    assert env["MONITOR_SSO_OPENWEBUI_URL"] == "${MONITOR_SSO_OPENWEBUI_URL:-http://openwebui:8080}"
    for key in ("MONITOR_SSO_TRUSTED_HEADER", "MONITOR_SSO_TRUSTED_SECRET", "MONITOR_SSO_ADMINS"):
        assert env[key] == f"${{{key}:-}}"
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "MONITOR_SSO_OPENWEBUI_URL" in example and "MONITOR_SSO_TRUSTED_SECRET" in example
    spec = yaml.safe_load(JOURNEYS.read_text(encoding="utf-8"))
    assert len(spec["browser_qa"]["workflows"]) == 5
    assert (REPO_ROOT / "agentd" / "tests" / "fixtures" / "admin_center_app.py").is_file()
    # the SSO keys are additive to the chat stack (the PR-15 baseline is unchanged)
    import importlib.util

    loader = importlib.util.spec_from_file_location(
        "chat_stack_baseline_pr20", REPO_ROOT / "scripts" / "chat-stack-baseline.py")
    tool = importlib.util.module_from_spec(loader)
    assert loader.loader is not None
    loader.loader.exec_module(tool)
    assert tool.snapshot() == tool.load_baseline()
