"""The SWE Tool Server (PR-13, ADR-029; OPENWEBUI_INTEGRATION §2/§4/§5): a
thin MCP adapter over the control plane — start + inspect only.

Offline: the vendored server module is loaded from mcp-servers/swe-server and
driven against the in-process daemon (real scripted plan and run pipelines);
the catalog is checked through the real FastMCP registration."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from agentd import platform_cli
from agentd.activation import Platform
from agentd.capability import CapabilityVector
from agentd.config import ControlConfig, load_config
from agentd.control import API_PREFIX
from agentd.control.app import create_app
from agentd.control.runs import RunRegistry
from agentd.platform_cli import build_context
from agentd.registry_v2 import save_generation
from agentd.render import write_rendered
from agentd.runtime_descriptor import load_descriptors
from tests.conftest import git, happy_path_script, planner_response
from tests.unit.test_activation import seed_registry
from tests.unit.test_control_runs import DummyLLM, Gate
from tests.unit.test_lifecycle import FakeEngineHTTP, FakeRunner

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER_DIR = REPO_ROOT / "mcp-servers" / "swe-server"
TOKEN = "swe-server-token"
ADMIN = "http://admin.test:8888"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def load_server():
    spec = importlib.util.spec_from_file_location("swe_server", SERVER_DIR / "swe_server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


swe = load_server()


@pytest.fixture
def daemon(tmp_path: Path, tmp_repo: Path, monkeypatch):
    """A control plane over a bootstrapped platform with `repo` registered,
    the scripted happy path as its model, and the tools wired to it."""
    root = tmp_path / "platform"
    (root / "config").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "config" / "providers", root / "config" / "providers")
    (root / "docker-compose.yml").write_text("services: {}\n")
    saved = save_generation(seed_registry(), root / "config", note="bootstrap")
    platform = Platform(config_dir=root / "config", platform_root=root,
                        descriptors=load_descriptors(root / "config"),
                        vector=CapabilityVector(system_memory_gb=15.5, cpu_cores=4),
                        capability_class="cpu-low", accelerator="none")
    write_rendered(platform.render(saved), platform.rendered)
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    monkeypatch.setattr(platform_cli, "default_runner", FakeRunner())
    monkeypatch.setattr(platform_cli, "http_probe", lambda url: 200)
    monkeypatch.setattr(platform_cli, "engine_http", lambda: FakeEngineHTTP(
        timings={"predicted_per_second": 15.5, "prompt_per_second": 80.0, "predicted_n": 100}))
    monkeypatch.setattr(platform_cli, "build_validator",
                        lambda ctx: lambda descriptor, name, entry: platform_cli.lifecycle
                        .ProbeResult(True, "", 0.2, name))
    script_file = tmp_path / "script.json"
    script_file.write_text(json.dumps(happy_path_script()), encoding="utf-8")
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({
        "llm": {"provider": "scripted", "script_path": str(script_file)},
        "platform": {"config_dir": str(root / "config")},
        "workspace": {"root": str(tmp_path / "ws")},
        "runs_dir": str(tmp_path / "runs"),
        "validation": {"autodetect": False},
    }), encoding="utf-8")
    ctx = build_context(load_config(cfg), root, actor="ezaid")
    platform_cli.add_project(ctx, str(tmp_repo))
    app = create_app(ControlConfig(token=TOKEN), ctx, prober=lambda url, headers: (200, "ok"))
    client = TestClient(app)
    plane = swe.ControlPlane(client, TOKEN, url="http://daemon.test:8010")
    tools = swe.SweTools(plane, admin_url=ADMIN, plan_wait_s=120,
                         sleep=lambda seconds: time.sleep(0.05))
    return SimpleNamespace(tools=tools, client=client, ctx=ctx, repo=tmp_repo,
                           script_file=script_file, config_dir=root / "config")


def wait_done(client: TestClient, run_id: str, timeout: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"{API_PREFIX}/runs/{run_id}", headers=AUTH).json()
        if body["status"] in ("completed", "failed", "cancelled"):
            return body
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} did not finish")


def run_id_in(text: str) -> str:
    match = re.search(r"`(\d{8}-\d{6}-[0-9a-f]{6})`", text)
    assert match, text
    return match.group(1)


# ── the catalog: start + inspect only ────────────────────────────────────────


def test_catalog_is_start_and_inspect_only_by_construction():
    assert set(swe.TOOLS) == {"swe_projects", "swe_plan", "swe_run", "swe_sprint", "swe_fix",
                              "swe_evolve", "swe_status", "swe_report", "swe_journal",
                              "model_list", "model_explain", "governance_queue"}
    for name in swe.TOOLS:
        assert not any(verb in name for verb in swe.FORBIDDEN_VERBS), name
    source = (SERVER_DIR / "swe_server.py").read_text(encoding="utf-8")
    # the only mutating call the server can make is "start a run"
    assert re.findall(r'\.post\("(/[^"]*)"', source) == ["/runs"]
    api_paths = set(re.findall(r'f?"(/[a-z][^"\s]*)"', source))
    assert "/runs" in api_paths and "/governance" in api_paths
    for path in api_paths:
        assert not any(verb in path for verb in swe.FORBIDDEN_VERBS), path
    assert re.search(r"^\s*(import agentd|from agentd)", source, re.M) is None  # stays thin

    tools = swe.SweTools(swe.ControlPlane(httpx.Client(base_url="http://x"), "t"))
    server = swe.build_server(tools)
    registered = asyncio.run(server.list_tools())
    assert [t.name for t in registered] == list(swe.TOOLS)
    assert all(t.description for t in registered)
    plan_tool = next(t for t in registered if t.name == "swe_plan")
    assert set(plan_tool.inputSchema["properties"]) == {"project", "task"}
    assert "confirmation" in plan_tool.description


# ── the loop: projects → plan → confirm → run → report ───────────────────────


def test_plan_confirm_run_report_loop_from_one_conversation(daemon):
    tools = daemon.tools
    projects = tools.swe_projects()
    assert "**Registered projects**" in projects and "| repo |" in projects
    assert str(daemon.repo) in projects

    daemon.script_file.write_text(json.dumps([planner_response()]), encoding="utf-8")
    plan = tools.swe_plan("repo", "fix the add bug")
    assert plan.startswith("## Plan `") and "| T1 |" in plan and "**Goal:**" in plan
    assert "executed nothing" in plan and "swe_run" in plan
    assert git(daemon.repo, "status", "--porcelain") == ""  # A0: no trace
    assert git(daemon.repo, "branch", "--list", "swe/*") == ""

    daemon.script_file.write_text(json.dumps(happy_path_script()), encoding="utf-8")
    started = tools.swe_run("repo", "fix the add bug")
    assert started.startswith("Started **run** `") and "swe_status(run_id)" in started
    run_id = run_id_in(started)
    assert f"{ADMIN}/runs/{run_id}" in started

    status = tools.swe_status(run_id)
    assert f"`{run_id}`" in status and "status **" in status
    final = wait_done(daemon.client, run_id)
    assert final["status"] == "completed", final
    status = tools.swe_status(run_id)
    assert "status **completed**" in status and f'swe_report("{run_id}")' in status

    report = tools.swe_report(run_id)
    assert report.startswith(f"## Run `{run_id}` — **COMPLETED**")
    assert "**Task:** fix the add bug" in report and "| T1 |" in report
    assert "validation: **passed**" in report and "review: **approve**" in report
    assert re.search(r"commit: `[0-9a-f]{12}` on branch `swe/", report)
    assert "**Models used:**" in report and "planner → `scripted`" in report
    assert report.rstrip().endswith(f"Admin Center: {ADMIN}/runs/{run_id}")
    assert "{" not in report.split("Admin Center")[0]  # markdown, never raw JSON

    journal = tools.swe_journal(run_id, tail=5)
    assert journal.startswith(f"**Journal of `{run_id}`** — last 5 of ")
    assert journal.count("\n") >= 6 and "```" in journal
    everything = tools.swe_journal(run_id, tail=500)
    assert "RUN_SUBMITTED" in everything and "RUN_TERMINAL" in everything

    events = [(r.event, r.actor) for r in daemon.ctx.queue.audit()]
    assert ("api.run_start", "swe-server") in events  # the tool server is the actor
    assert ("run.submitted", "swe-server") in events and ("run.finished", "swe-server") in events


def test_other_start_tools_and_pending_report(daemon, tmp_path):
    gate = Gate()
    registry = RunRegistry(daemon.ctx.config, daemon.config_dir / "control" / "runs",
                           audit=daemon.ctx.queue.audit_log, max_concurrent=3, max_queued=3,
                           pipelines={k: gate for k in ("run", "fix", "sprint", "evolve")},
                           llm_factory=lambda cfg: DummyLLM())
    app = create_app(ControlConfig(token=TOKEN), daemon.ctx, prober=lambda u, h: (200, "ok"),
                     runs=registry)
    client = TestClient(app)
    tools = swe.SweTools(swe.ControlPlane(client, TOKEN), admin_url=ADMIN,
                         sleep=lambda s: time.sleep(0.05))
    fix = tools.swe_fix("repo")
    sprint = tools.swe_sprint("repo", "# Sprint\n- [ ] one\n- [ ] two\n")
    evolve = tools.swe_evolve("repo", focus="tests")
    assert fix.startswith("Started **fix** `") and sprint.startswith("Started **sprint** `")
    assert evolve.startswith("Started **evolve** `") and "awaiting human review" in evolve
    ids = [run_id_in(text) for text in (fix, sprint, evolve)]
    for run_id in ids:
        assert gate.wait_started(run_id)
    pending = tools.swe_report(ids[0])
    assert "no report yet" in pending and f'swe_status("{ids[0]}")' in pending
    for run_id in ids:
        gate.release(run_id)
        wait_done(client, run_id)
    assert "status **completed**" in tools.swe_status(ids[1])
    records = client.get(f"{API_PREFIX}/runs", headers=AUTH).json()["runs"]
    assert {r["kind"] for r in records} == {"fix", "sprint", "evolve"}
    assert all(r["request"] for r in records)  # goal default, the spec, the focus


# ── refusals are answers ─────────────────────────────────────────────────────


def test_refusals_and_failures_are_rendered_for_the_model(daemon):
    tools = daemon.tools
    foreign = tools.swe_run("not-registered", "x")
    assert foreign.startswith("⚠️ **not_found**") and "project add" in foreign
    assert tools.swe_status("20260101-000000-abcdef").startswith("⚠️ **not_found**")
    assert tools.model_explain("nope").startswith("⚠️ **not_found**")

    wrong = swe.SweTools(swe.ControlPlane(daemon.client, "wrong-token"), admin_url=ADMIN)
    rejected = wrong.swe_projects()
    assert rejected.startswith("⚠️ **unauthorized**") and "EZAI_CONTROL_TOKEN" in rejected
    assert "wrong-token" not in daemon.ctx.queue.log_path.read_text()

    class Down(httpx.Client):
        def request(self, *args, **kwargs):
            raise httpx.ConnectError("connection refused")

    down = swe.SweTools(swe.ControlPlane(Down(base_url="http://d:8010"), TOKEN,
                                         url="http://d:8010"), admin_url=ADMIN)
    unreachable = down.governance_queue()
    assert unreachable.startswith("⚠️ **control_unreachable**") and "http://d:8010" in unreachable
    assert "make control-up" in unreachable and "make control-serve" in unreachable


# ── models and governance, read-only ─────────────────────────────────────────


def test_model_and_governance_views_are_read_only(daemon):
    tools = daemon.tools
    models = tools.model_list()
    assert "**Models — registry generation 1**" in models
    assert "| alpha | active | llamacpp |" in models and "| beta | benchmarked |" in models
    explain = tools.model_explain("coder")
    assert "**Role `coder`**" in explain and "group coding" in explain
    assert "- primary: `alpha`" in explain and "`alpha` meets the contract" in explain

    assert tools.governance_queue().startswith("The governance queue is empty.")
    platform_cli.activate_model(daemon.ctx, "beta", group="coding")  # a human's CLI request
    queue = tools.governance_queue()
    assert "**Governance queue** — 1 pending, 1 total" in queue
    assert "| cr-0001 | pending | activation |" in queue
    assert f"{ADMIN}/governance/cr-0001" in queue
    assert queue.rstrip().endswith(swe.GOVERNANCE_FOOTER)
    assert not hasattr(tools, "governance_approve") and not hasattr(tools, "swe_cancel")
    # nothing the tools did changed the platform's governance state
    assert daemon.ctx.queue.get("cr-0001").status == "pending"


# ── packaging: mcpo registration ─────────────────────────────────────────────


def test_mcpo_registration_and_image_wiring():
    config = json.loads((REPO_ROOT / "config" / "mcpo-config.json").read_text())
    swe_entry = config["mcpServers"]["swe"]
    assert swe_entry["command"] == "/opt/mcpo/bin/python"
    assert swe_entry["args"] == ["/mcp-servers/swe-server/swe_server.py"]
    assert swe_entry["env"]["EZAI_CONTROL_URL"] == "${EZAI_CONTROL_URL_MCPO:-http://ezaid:8010}"
    assert swe_entry["env"]["EZAI_CONTROL_TOKEN"].startswith("${EZAI_CONTROL_TOKEN")
    assert "${MONITOR_PORT:-8888}" in swe_entry["env"]["EZAI_ADMIN_URL"]
    assert "start + inspect only" in swe_entry["description"]
    # the existing servers are untouched
    assert set(config["mcpServers"]) == {"filesystem", "memory", "fetch", "qdrant-rag", "swe"}

    dockerfile = (REPO_ROOT / "mcpo" / "Dockerfile").read_text()
    assert "COPY mcp-servers/swe-server/ /mcp-servers/swe-server/" in dockerfile
    assert "mcp==1.28.1" in dockerfile  # the pin the vendored server is written against

    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    mcpo = compose["services"]["mcpo"]
    env = dict(item.split("=", 1) for item in mcpo["environment"])
    assert env["EZAI_CONTROL_URL_MCPO"].startswith("${EZAI_CONTROL_URL_MCPO:-http://ezaid:8010")
    assert env["EZAI_CONTROL_TOKEN"].startswith("${EZAI_CONTROL_TOKEN")
    assert "host.docker.internal:host-gateway" in mcpo["extra_hosts"]
    # OpenWebUI's tool-server pre-registration (PR-14) points at the same mcpo path
    openwebui_env = "\n".join(compose["services"]["openwebui"]["environment"])
    assert "/swe" in openwebui_env


def test_main_requires_the_service_token(monkeypatch, capsys):
    monkeypatch.delenv("EZAI_CONTROL_TOKEN", raising=False)
    assert swe.main() == 2
    assert "EZAI_CONTROL_TOKEN" in capsys.readouterr().err
