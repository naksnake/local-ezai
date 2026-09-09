"""P2 phase close (PR-12, ADR-028 → Accepted): the exit criteria of
V1_IMPLEMENTATION_PLAN §P2 as tests.

2. two long runs supervised concurrently through the API (start, status,
   report, cancel) — two real scripted runs on two repositories, plus a
   deterministic gated pair with a mid-flight cancel;
3. kill-the-daemon: a REAL `ezaid` process serves the platform, the CLI
   works through it, the process is SIGKILLed — repo work via the CLI is
   unaffected, management verbs fall back to direct (or fail fast when
   connected was requested);
4. the OpenAPI contract is published and versioned: frozen at 1.0.0 by the
   P2 close, extended additively to 1.1.0 by PR-19 (project memory).

(Criterion 1, parity, is the PR-11 smoke in test_cli_connected.py.)"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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
from agentd.control import (
    API_PREFIX,
    CLIENT_HEADER,
    CONTRACT_VERSION,
    SPEC_ARTIFACT,
    TOKEN_ENV,
    TRANSPORT_ENV,
    URL_ENV,
    USER_HEADER,
)
from agentd.control.app import contract_surface, create_app
from agentd.control.runs import RunRegistry
from agentd.lifecycle import free_port
from agentd.main_cli import main
from agentd.platform_cli import build_context
from agentd.registry_v2 import save_generation
from agentd.render import write_rendered
from agentd.runtime_descriptor import load_descriptors
from tests.conftest import (
    BUGGY_CALCULATOR,
    REPO_AGENTD_YAML,
    git,
    git_commit_all,
    happy_path_script,
    planner_response,
)
from tests.unit.test_activation import seed_registry
from tests.unit.test_control_runs import DummyLLM, Gate
from tests.unit.test_lifecycle import FakeEngineHTTP, FakeRunner

REPO_ROOT = Path(__file__).resolve().parents[3]
TOKEN = "p2-close-token"
AUTH = {"Authorization": f"Bearer {TOKEN}", USER_HEADER: "nita", CLIENT_HEADER: "cli"}
V1 = API_PREFIX
TERMINAL = ("completed", "failed", "cancelled")

#: The contract surface. A change here is a contract change: bump
#: CONTRACT_VERSION (additive → minor, breaking → major), regenerate the
#: artifact (make control-spec) and update this inventory in the same PR.
#: 1.0.0 = the 29 operations frozen by the P2 close; 1.1.0 (PR-19) added the
#: two project memory operations, additively.
FROZEN_1_0_0_OPERATIONS = {
    # skeleton (PR-8)
    "liveness", "aggregated_health", "whoami", "audit_tail",
    # lifecycle · governance · projects (PR-9)
    "models_list", "model_install", "model_benchmark", "model_activate", "model_upgrade",
    "generation_rollback", "model_retire", "model_uninstall", "role_explain",
    "generation_history", "catalog_list", "catalog_recommend", "governance_list",
    "governance_show", "governance_approve", "governance_reject", "projects_list",
    "project_add", "project_remove",
    # runs (PR-10)
    "run_start", "runs_list", "run_get", "run_report", "run_journal", "run_cancel",
}
#: Additive 1.1.0 operations (PR-19): a registered project's memory.
CONTRACT_1_1_0_ADDITIONS = {"project_memory", "project_memory_add"}
CONTRACT_OPERATIONS = FROZEN_1_0_0_OPERATIONS | CONTRACT_1_1_0_ADDITIONS


def make_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)
    (path / "calculator.py").write_text(BUGGY_CALCULATOR, encoding="utf-8")
    (path / ".agentd.yaml").write_text(REPO_AGENTD_YAML, encoding="utf-8")
    git_commit_all(path, "initial commit")
    return path


@pytest.fixture
def platform(tmp_path: Path, monkeypatch):
    """A bootstrapped platform + a CLI/daemon config (scripted happy path)."""
    root = tmp_path / "platform"
    (root / "config").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "config" / "providers", root / "config" / "providers")
    (root / "docker-compose.yml").write_text("services: {}\n")
    saved = save_generation(seed_registry(), root / "config", note="bootstrap")
    plat = Platform(config_dir=root / "config", platform_root=root,
                    descriptors=load_descriptors(root / "config"),
                    vector=CapabilityVector(system_memory_gb=15.5, cpu_cores=4),
                    capability_class="cpu-low", accelerator="none")
    write_rendered(plat.render(saved), plat.rendered)
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
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    monkeypatch.delenv(TRANSPORT_ENV, raising=False)
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    monkeypatch.setattr(platform_cli, "default_runner", FakeRunner())
    monkeypatch.setattr(platform_cli, "http_probe", lambda url: 200)
    monkeypatch.setattr(platform_cli, "engine_http", lambda: FakeEngineHTTP(
        timings={"predicted_per_second": 15.5, "prompt_per_second": 80.0, "predicted_n": 100}))
    monkeypatch.setattr(platform_cli, "build_validator",
                        lambda ctx: lambda descriptor, name, entry: platform_cli.lifecycle
                        .ProbeResult(True, "", 0.2, name))
    return SimpleNamespace(root=root, config_dir=root / "config", cfg=cfg,
                           script_file=script_file, tmp=tmp_path)


def wait_terminal(client: TestClient, run_id: str, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"{V1}/runs/{run_id}", headers=AUTH).json()
        if body.get("status") in TERMINAL:
            return body
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} did not finish: {body}")


# ── exit criterion 3: kill the daemon ────────────────────────────────────────


def test_kill_the_daemon_leaves_repo_work_unaffected(platform, tmp_repo, monkeypatch, capsys):
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items() if k != "AGENTD_CONFIG"}
    env[TOKEN_ENV] = TOKEN
    log_file = (platform.tmp / "ezaid.log").open("w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentd.control", "--config", str(platform.cfg),
         "--platform", str(platform.config_dir), "--host", "127.0.0.1", "--port", str(port)],
        env=env, stdout=log_file, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 90
        alive = False
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                if httpx.get(f"{url}/health", timeout=1.0).status_code == 200:
                    alive = True
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        assert alive, f"ezaid did not come up:\n{(platform.tmp / 'ezaid.log').read_text()}"

        # connected mode against the real process
        monkeypatch.setenv(URL_ENV, url)
        code = main(["status", "--json", "--config", str(platform.cfg)])
        status = json.loads(capsys.readouterr().out)
        assert code == 0 and status["transport"] == f"connected {url}"
        code = main(["model", "activate", "beta", "--group", "spare", "--by", "nita", "--json",
                     "--config", str(platform.cfg)])
        outcome = json.loads(capsys.readouterr().out)
        assert code == 0 and outcome["applied"]["generation"] == 2
        assert outcome["request"]["requested_by"] == "nita via cli"
    finally:
        proc.kill()  # SIGKILL: no graceful shutdown, no goodbye
        proc.wait(timeout=15)
        log_file.close()
    assert proc.returncode is not None

    # repo work via the CLI is unaffected (direct, in-process)
    platform.script_file.write_text(json.dumps([planner_response()]), encoding="utf-8")
    code = main([str(tmp_repo), "plan", "fix the add bug", "--config", str(platform.cfg)])
    plan = json.loads(capsys.readouterr().out)
    assert code == 0 and plan["tasks"][0]["id"] == "T1"
    assert git(tmp_repo, "status", "--porcelain") == ""

    # management verbs: auto falls back to direct on the SAME platform state;
    # a requested connected transport fails fast
    code = main(["status", "--json", "--config", str(platform.cfg)])
    status = json.loads(capsys.readouterr().out)
    assert code == 0 and status["transport"] == "direct (in-process)"
    assert status["generation"] == 2 and status["models"]["beta"]["state"] == "active"
    code = main(["status", "--transport", "connected", "--json", "--config", str(platform.cfg)])
    error = json.loads(capsys.readouterr().out)["error"]
    assert code == 2 and "control plane unreachable" in error["message"]
    # the daemon's life is in the platform's single audit log
    events = [r.event for r in build_context(load_config(platform.cfg), platform.root,
                                             actor="t").queue.audit()]
    assert "control.started" in events and "api.model_activate" in events
    assert "control.stopped" not in events  # killed, not stopped


# ── exit criterion 2: two runs supervised concurrently ───────────────────────


def test_two_real_runs_supervised_concurrently_through_the_api(platform):
    repo_a = make_repo(platform.tmp / "repo-a")
    repo_b = make_repo(platform.tmp / "repo-b")
    ctx = build_context(load_config(platform.cfg), platform.root, actor="ezaid")
    platform_cli.add_project(ctx, str(repo_a))
    platform_cli.add_project(ctx, str(repo_b))
    app = create_app(ControlConfig(token=TOKEN, max_concurrent_runs=2), ctx,
                     prober=lambda url, headers: (200, "ok"))
    with TestClient(app) as client:
        started = [client.post(f"{V1}/runs", headers=AUTH,
                               json={"kind": "run", "project": name, "task": "fix the add bug"})
                   for name in ("repo-a", "repo-b")]
        assert all(r.status_code == 202 for r in started), [r.text for r in started]
        ids = [r.json()["run_id"] for r in started]
        assert client.get(f"{V1}/runs", headers=AUTH).json()["active"] == 2
        finals = [wait_terminal(client, run_id) for run_id in ids]
        assert [f["status"] for f in finals] == ["completed", "completed"], finals
        reports = [client.get(f"{V1}/runs/{run_id}/report", headers=AUTH).json()["report"]
                   for run_id in ids]
        listed = client.get(f"{V1}/runs", headers=AUTH).json()
    for repo, report in zip((repo_a, repo_b), reports, strict=True):
        assert report["status"] == "completed" and report["commit"]["sha"]
        assert report["repo_path"] == str(repo)
        assert "return a + b" in git(repo, "show", f"{report['branch']}:calculator.py")
    assert listed["active"] == 0 and [r["status"] for r in listed["runs"]] == ["completed"] * 2
    finished = [r for r in ctx.queue.audit() if r.event == "run.finished"]
    assert len(finished) == 2 and all(r.details["status"] == "completed" for r in finished)


def test_two_gated_runs_start_status_report_cancel(platform):
    ctx = build_context(load_config(platform.cfg), platform.root, actor="ezaid")
    platform_cli.add_project(ctx, str(make_repo(platform.tmp / "repo")))
    gate = Gate()
    registry = RunRegistry(ctx.config, platform.config_dir / "control" / "runs",
                           audit=ctx.queue.audit_log, max_concurrent=2, max_queued=2,
                           pipelines={"run": gate}, llm_factory=lambda cfg: DummyLLM())
    app = create_app(ControlConfig(token=TOKEN), ctx, prober=lambda url, headers: (200, "ok"),
                     runs=registry)
    with TestClient(app) as client:
        a = client.post(f"{V1}/runs", headers=AUTH,
                        json={"kind": "run", "project": "repo", "task": "a"}).json()["run_id"]
        b = client.post(f"{V1}/runs", headers=AUTH,
                        json={"kind": "run", "project": "repo", "task": "b"}).json()["run_id"]
        assert gate.wait_started(a) and gate.wait_started(b)  # concurrently running
        statuses = {r["run_id"]: r["status"]
                    for r in client.get(f"{V1}/runs", headers=AUTH).json()["runs"]}
        assert statuses == {a: "running", b: "running"}
        cancelled = client.post(f"{V1}/runs/{b}/cancel", headers=AUTH).json()
        assert cancelled["cancel_requested"] and cancelled["status"] == "running"
        gate.release(b)
        assert wait_terminal(client, b)["status"] == "cancelled"
        gate.release(a)
        assert wait_terminal(client, a)["status"] == "completed"
        assert client.get(f"{V1}/runs/{a}/report", headers=AUTH).status_code == 200
        gone = client.get(f"{V1}/runs/{b}/report", headers=AUTH)
        assert gone.status_code == 404 and "cancelled" in gone.json()["error"]["message"]
        assert client.get(f"{V1}/runs", headers=AUTH).json()["active"] == 0


# ── exit criterion 4: the contract, frozen ───────────────────────────────────


def test_contract_is_published_and_versioned():
    assert CONTRACT_VERSION == "1.1.0"
    committed = json.loads((REPO_ROOT / SPEC_ARTIFACT).read_text(encoding="utf-8"))
    assert committed["info"]["version"] == "1.1.0"
    status = committed["info"]["x-contract-status"]
    assert status.startswith("1.1.0") and "frozen at 1.0.0" in status and "additive" in status
    live = create_app(ControlConfig(token="spec")).openapi()
    live_ids = {op["operationId"] for methods in live["paths"].values() for op in methods.values()}
    assert live_ids == CONTRACT_OPERATIONS, (
        "the ezaid contract surface changed — bump CONTRACT_VERSION (additive → minor, "
        "breaking → major), regenerate docs/api/ezaid-openapi.json (make control-spec) and "
        "update the inventory in the same PR")
    assert contract_surface(committed) == contract_surface(live)
    assert len(FROZEN_1_0_0_OPERATIONS) == 29  # the P2 close surface, untouched
    assert len(CONTRACT_OPERATIONS) == 31
    # every 1.0.0 operation is still served with its 1.0.0 path and method
    for op_id in FROZEN_1_0_0_OPERATIONS:
        assert any(op["operationId"] == op_id for methods in live["paths"].values()
                   for op in methods.values()), op_id
