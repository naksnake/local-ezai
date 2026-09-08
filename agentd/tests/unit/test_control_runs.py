"""Async run registry + run endpoints (PR-10, ADR-028; OPENWEBUI_INTEGRATION
§2 async run protocol): start returns at once, status/report/journal follow,
cancel stops a queued job now and a running one at its next model call;
concurrency limits; the project allowlist; recovery after a restart.

Offline: one real scripted pipeline (`run`, then `plan`) drives the actual
runner through the daemon; the limit/cancel matrix uses gated fake pipelines
so timing is deterministic."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from agentd import platform_cli
from agentd.activation import Platform
from agentd.capability import CapabilityVector
from agentd.config import ControlConfig, load_config
from agentd.control import API_PREFIX, CLIENT_HEADER, USER_HEADER
from agentd.control.app import create_app
from agentd.control.runs import (
    CancellableLLM,
    CancelToken,
    RunCancelled,
    RunRecord,
    RunRefused,
    RunRegistry,
    configure_job,
)
from agentd.llm import ScriptedLLM
from agentd.platform_cli import build_context
from agentd.registry_v2 import save_generation
from agentd.render import write_rendered
from agentd.runtime_descriptor import load_descriptors
from agentd.schemas import RunReport
from tests.conftest import git, happy_path_script, planner_response
from tests.unit.test_activation import seed_registry

REPO_ROOT = Path(__file__).resolve().parents[3]
TOKEN = "test-service-token-run"
AUTH = {"Authorization": f"Bearer {TOKEN}", USER_HEADER: "nita", CLIENT_HEADER: "cli"}
V1 = API_PREFIX
TERMINAL = ("completed", "failed", "cancelled")


@pytest.fixture
def platform_root(tmp_path: Path) -> Path:
    root = tmp_path / "platform"
    (root / "config").mkdir(parents=True)
    import shutil

    shutil.copytree(REPO_ROOT / "config" / "providers", root / "config" / "providers")
    (root / "docker-compose.yml").write_text("services: {}\n")
    saved = save_generation(seed_registry(), root / "config", note="bootstrap")
    platform = Platform(config_dir=root / "config", platform_root=root,
                        descriptors=load_descriptors(root / "config"),
                        vector=CapabilityVector(system_memory_gb=15.5, cpu_cores=4),
                        capability_class="cpu-low", accelerator="none")
    write_rendered(platform.render(saved), platform.rendered)
    return root


@pytest.fixture
def daemon(platform_root: Path, tmp_repo: Path, tmp_path: Path, monkeypatch):
    """A control plane whose config drives the scripted LLM and whose
    project allowlist holds the fixture repo (registered as 'repo')."""
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    script_file = tmp_path / "script.json"
    script_file.write_text(json.dumps(happy_path_script()), encoding="utf-8")
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "llm": {"provider": "scripted", "script_path": str(script_file)},
        "platform": {"config_dir": str(platform_root / "config")},
        "workspace": {"root": str(tmp_path / "ws")},
        "runs_dir": str(tmp_path / "runs"),
        "validation": {"autodetect": False},
    }), encoding="utf-8")
    ctx = build_context(load_config(cfg_file), platform_root, actor="ezaid")
    platform_cli.add_project(ctx, str(tmp_repo))
    return SimpleNamespace(ctx=ctx, script_file=script_file, repo=tmp_repo,
                           runs_dir=tmp_path / "runs",
                           records_dir=platform_root / "config" / "control" / "runs")


def make_client(daemon, **kwargs) -> TestClient:
    app = create_app(ControlConfig(token=TOKEN), daemon.ctx,
                     prober=lambda url, headers: (200, "ok"), **kwargs)
    return TestClient(app)


def wait_terminal(client: TestClient, run_id: str, timeout: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"{V1}/runs/{run_id}", headers=AUTH).json()
        if body.get("status") in TERMINAL:
            return body
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} did not finish in time: {body}")


class DummyLLM:
    def __init__(self) -> None:
        self.models_used: dict[str, str] = {}
        self.calls = 0

    def chat(self, role, messages, *args, **kwargs):
        self.calls += 1
        self.models_used[role] = "dummy"
        return {"content": "ok"}


class Gate:
    """A fake pipeline: blocks until released per run, then makes one model
    call (the cancellation point) and returns a completed RunReport."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._release: dict[str, threading.Event] = {}
        self._started: dict[str, threading.Event] = {}
        self.calls: list[str] = []

    def _events(self, run_id: str) -> tuple[threading.Event, threading.Event]:
        with self._lock:
            return (self._release.setdefault(run_id, threading.Event()),
                    self._started.setdefault(run_id, threading.Event()))

    def __call__(self, config, repo, record, llm):
        release, started = self._events(record.run_id)
        self.calls.append(record.run_id)
        started.set()
        assert release.wait(30), f"gate for {record.run_id} never released"
        llm.chat("planner", [])  # a cancelled job stops here
        return RunReport(run_id=record.run_id, status="completed", request=record.request,
                         repo_path=str(repo), workspace_path=str(repo), branch="swe/x")

    def wait_started(self, run_id: str, timeout: float = 10.0) -> bool:
        return self._events(run_id)[1].wait(timeout)

    def release(self, run_id: str) -> None:
        self._events(run_id)[0].set()


def gated_registry(daemon, gate: Gate, *, max_concurrent: int = 1, max_queued: int = 1,
                   kinds=("run", "fix")) -> RunRegistry:
    return RunRegistry(daemon.ctx.config, daemon.records_dir, audit=daemon.ctx.queue.audit_log,
                       max_concurrent=max_concurrent, max_queued=max_queued,
                       pipelines={kind: gate for kind in kinds},
                       llm_factory=lambda cfg: DummyLLM())


# ── the real pipeline through the daemon ─────────────────────────────────────


def test_scripted_run_end_to_end_through_the_api(daemon):
    with make_client(daemon) as client:
        started = client.post(f"{V1}/runs", headers=AUTH,
                              json={"kind": "run", "project": "repo", "task": "fix the add bug"})
        assert started.status_code == 202, started.text
        body = started.json()
        assert body["status"] in ("queued", "running") and body["kind"] == "run"
        assert body["actor"] == "nita via cli" and body["project"] == str(daemon.repo)
        run_id = body["run_id"]

        final = wait_terminal(client, run_id)
        assert final["status"] == "completed", final
        assert final["progress"]["events"] > 0 and final["started_at"] and final["finished_at"]
        report = client.get(f"{V1}/runs/{run_id}/report", headers=AUTH).json()
        assert report["status"] == "completed" and report["report"]["status"] == "completed"
        assert report["report"]["branch"].startswith("swe/") and report["report"]["commit"]["sha"]
        assert "return a + b" in git(daemon.repo, "show",
                                      f"{report['report']['branch']}:calculator.py")
        journal = client.get(f"{V1}/runs/{run_id}/journal", headers=AUTH,
                             params={"tail": 1000}).json()
        assert journal["total"] == len(journal["events"]) > 5
        assert {"RUN_SUBMITTED", "RUN_TERMINAL"} <= {e["type"] for e in journal["events"]}
        tail = client.get(f"{V1}/runs/{run_id}/journal", headers=AUTH, params={"tail": 3}).json()
        assert len(tail["events"]) == 3 and tail["total"] == journal["total"]

        listed = client.get(f"{V1}/runs", headers=AUTH).json()
        assert [r["run_id"] for r in listed["runs"]] == [run_id]
        assert listed["active"] == 0 and listed["max_concurrent"] == 2
        assert (daemon.records_dir / f"{run_id}.json").is_file()
        finished = client.post(f"{V1}/runs/{run_id}/cancel", headers=AUTH)
        assert finished.status_code == 409 and finished.json()["error"]["code"] == "run_finished"
    events = [(r.event, r.actor) for r in daemon.ctx.queue.audit()]
    assert ("api.run_start", "nita via cli") in events
    assert ("run.submitted", "nita via cli") in events
    assert ("run.finished", "nita via cli") in events


def test_plan_job_reports_the_plan_and_leaves_no_trace(daemon):
    daemon.script_file.write_text(json.dumps([planner_response()]), encoding="utf-8")
    with make_client(daemon) as client:
        started = client.post(f"{V1}/runs", headers=AUTH,
                              json={"kind": "plan", "project": str(daemon.repo),
                                    "task": "fix the add bug"})
        assert started.status_code == 202
        final = wait_terminal(client, started.json()["run_id"])
        assert final["status"] == "completed"
        report = client.get(f"{V1}/runs/{final['run_id']}/report", headers=AUTH).json()
    assert report["report"]["tasks"][0]["id"] == "T1"
    assert git(daemon.repo, "branch", "--list", "swe/*") == ""  # A0: no branch, no changes
    assert git(daemon.repo, "status", "--porcelain") == ""


def test_start_refuses_unregistered_projects_and_incomplete_requests(daemon, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with make_client(daemon) as client:
        foreign = client.post(f"{V1}/runs", headers=AUTH,
                              json={"kind": "run", "project": str(elsewhere), "task": "x"})
        assert foreign.status_code == 404
        assert foreign.json()["error"]["code"] == "not_found"
        assert "project add" in foreign.json()["error"]["message"]
        no_task = client.post(f"{V1}/runs", headers=AUTH, json={"kind": "run", "project": "repo"})
        assert no_task.status_code == 422 and "task" in no_task.json()["error"]["message"]
        no_spec = client.post(f"{V1}/runs", headers=AUTH,
                              json={"kind": "sprint", "project": "repo"})
        assert no_spec.status_code == 422 and "spec" in no_spec.json()["error"]["message"]
        bogus = client.post(f"{V1}/runs", headers=AUTH, json={"kind": "deploy", "project": "repo"})
        assert bogus.status_code == 422 and bogus.json()["error"]["code"] == "invalid_request"
        assert client.get(f"{V1}/runs", headers=AUTH).json()["runs"] == []
        unknown = client.get(f"{V1}/runs/nope", headers=AUTH)
        assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "not_found"


# ── limits · cancellation · outcomes (gated fake pipelines) ──────────────────


def test_concurrency_limits_queue_then_refuse(daemon):
    gate = Gate()
    registry = gated_registry(daemon, gate, max_concurrent=1, max_queued=1)
    with make_client(daemon, runs=registry) as client:
        first = client.post(f"{V1}/runs", headers=AUTH,
                            json={"kind": "run", "project": "repo", "task": "one"}).json()
        assert gate.wait_started(first["run_id"])
        second = client.post(f"{V1}/runs", headers=AUTH,
                             json={"kind": "run", "project": "repo", "task": "two"}).json()
        assert second["status"] == "queued"
        third = client.post(f"{V1}/runs", headers=AUTH,
                            json={"kind": "run", "project": "repo", "task": "three"})
        assert third.status_code == 429 and third.json()["error"]["code"] == "too_many_runs"
        listed = client.get(f"{V1}/runs", headers=AUTH).json()
        assert listed["active"] == 2 and listed["max_concurrent"] == 1 and listed["max_queued"] == 1
        assert [r["status"] for r in listed["runs"]] == ["queued", "running"]  # newest first

        gate.release(first["run_id"])
        assert wait_terminal(client, first["run_id"])["status"] == "completed"
        assert gate.wait_started(second["run_id"])  # the queued job took the freed slot
        gate.release(second["run_id"])
        assert wait_terminal(client, second["run_id"])["status"] == "completed"
        assert client.get(f"{V1}/runs", headers=AUTH).json()["active"] == 0


def test_in_place_jobs_are_exclusive_per_project(daemon):
    gate = Gate()
    registry = gated_registry(daemon, gate, max_concurrent=3, max_queued=3)
    with make_client(daemon, runs=registry) as client:
        first = client.post(f"{V1}/runs", headers=AUTH, json={"kind": "fix", "project": "repo"})
        assert first.status_code == 202
        assert first.json()["request"] == "repair failing validation checks"
        busy = client.post(f"{V1}/runs", headers=AUTH,
                           json={"kind": "fix", "project": "repo", "goal": "again"})
        assert busy.status_code == 409 and busy.json()["error"]["code"] == "project_busy"
        assert first.json()["run_id"] in busy.json()["error"]["message"]
        worktree = client.post(f"{V1}/runs", headers=AUTH,
                               json={"kind": "run", "project": "repo", "task": "parallel"})
        assert worktree.status_code == 202  # worktree kinds may overlap an in-place job
        for run_id in (first.json()["run_id"], worktree.json()["run_id"]):
            gate.release(run_id)
            assert wait_terminal(client, run_id)["status"] == "completed"


def test_cancel_queued_now_and_running_at_the_next_model_call(daemon):
    gate = Gate()
    registry = gated_registry(daemon, gate, max_concurrent=1, max_queued=2)
    with make_client(daemon, runs=registry) as client:
        running = client.post(f"{V1}/runs", headers=AUTH,
                              json={"kind": "run", "project": "repo", "task": "a"}).json()
        assert gate.wait_started(running["run_id"])
        queued = client.post(f"{V1}/runs", headers=AUTH,
                             json={"kind": "run", "project": "repo", "task": "b"}).json()
        assert queued["status"] == "queued"

        cancelled = client.post(f"{V1}/runs/{queued['run_id']}/cancel", headers=AUTH).json()
        assert cancelled["status"] == "cancelled" and cancelled["cancel_requested"]
        assert cancelled["finished_at"]

        requested = client.post(f"{V1}/runs/{running['run_id']}/cancel", headers=AUTH).json()
        assert requested["status"] == "running" and requested["cancel_requested"] is True
        pending = client.get(f"{V1}/runs/{running['run_id']}/report", headers=AUTH)
        assert pending.status_code == 409 and pending.json()["error"]["code"] == "report_pending"

        gate.release(running["run_id"])  # the job reaches its model call → stops
        final = wait_terminal(client, running["run_id"])
        assert final["status"] == "cancelled" and "next model call" in final["error"]
        again = client.post(f"{V1}/runs/{running['run_id']}/cancel", headers=AUTH)
        assert again.status_code == 409 and again.json()["error"]["code"] == "run_finished"
        no_report = client.get(f"{V1}/runs/{running['run_id']}/report", headers=AUTH)
        assert no_report.status_code == 404 and "cancelled" in no_report.json()["error"]["message"]
        registry.wait(queued["run_id"])
    assert queued["run_id"] not in gate.calls  # never started
    events = [(r.event, r.details.get("by")) for r in daemon.ctx.queue.audit()
              if r.event == "run.cancel_requested"]
    assert events == [("run.cancel_requested", "nita via cli")] * 2


def test_report_pending_then_written_by_the_registry(daemon):
    gate = Gate()
    registry = gated_registry(daemon, gate)
    with make_client(daemon, runs=registry) as client:
        run_id = client.post(f"{V1}/runs", headers=AUTH,
                             json={"kind": "run", "project": "repo", "task": "x",
                                   "max_iterations": 3, "in_place": True}).json()["run_id"]
        assert gate.wait_started(run_id)
        pending = client.get(f"{V1}/runs/{run_id}/report", headers=AUTH)
        assert pending.status_code == 409 and pending.json()["error"]["code"] == "report_pending"
        gate.release(run_id)
        final = wait_terminal(client, run_id)
        assert final["status"] == "completed" and final["report_path"].endswith("report.json")
        assert final["options"] == {"max_iterations": 3, "in_place": True}
        report = client.get(f"{V1}/runs/{run_id}/report", headers=AUTH).json()
    assert report["report"]["branch"] == "swe/x"  # the fake pipeline's report, persisted


def test_failed_pipeline_is_a_recorded_outcome(daemon):
    def boom(config, repo, record, llm):
        raise RuntimeError("boom")

    registry = RunRegistry(daemon.ctx.config, daemon.records_dir,
                           audit=daemon.ctx.queue.audit_log,
                           pipelines={"run": boom}, llm_factory=lambda cfg: DummyLLM())
    with make_client(daemon, runs=registry) as client:
        run_id = client.post(f"{V1}/runs", headers=AUTH,
                             json={"kind": "run", "project": "repo", "task": "x"}).json()["run_id"]
        final = wait_terminal(client, run_id)
        assert final["status"] == "failed" and final["error"] == "RuntimeError: boom"
        report = client.get(f"{V1}/runs/{run_id}/report", headers=AUTH)
    assert report.status_code == 404 and "boom" in report.json()["error"]["message"]
    finished = [r for r in daemon.ctx.queue.audit() if r.event == "run.finished"]
    assert finished[-1].details["status"] == "failed" and "boom" in finished[-1].details["error"]


def test_restart_marks_interrupted_runs_failed(daemon):
    daemon.records_dir.mkdir(parents=True)
    interrupted = RunRecord(run_id="20260908-000000-aaaaaa", kind="run", project="/p",
                            project_name="repo", request="x", status="running")
    done = RunRecord(run_id="20260908-000001-bbbbbb", kind="plan", project="/p",
                     project_name="repo", request="y", status="completed")
    for record in (interrupted, done):
        (daemon.records_dir / f"{record.run_id}.json").write_text(json.dumps(record.as_dict()))
    registry = RunRegistry(daemon.ctx.config, daemon.records_dir,
                           audit=daemon.ctx.queue.audit_log,
                           pipelines={}, llm_factory=lambda cfg: DummyLLM())
    recovered = registry.get(interrupted.run_id)
    assert recovered.status == "failed" and "restarted" in recovered.error
    assert registry.get(done.run_id).status == "completed"
    assert [r.run_id for r in registry.list()] == [done.run_id, interrupted.run_id]
    on_disk = json.loads((daemon.records_dir / f"{interrupted.run_id}.json").read_text())
    assert on_disk["status"] == "failed"
    assert daemon.ctx.queue.audit()[-1].event == "run.orphaned"
    with pytest.raises(RunRefused) as err:
        registry.submit("deploy", Path("/p"), "repo", "x", actor="t")
    assert err.value.code == "invalid_request"


def test_cancellable_llm_and_job_config():
    token = CancelToken()
    llm = CancellableLLM(ScriptedLLM([{"content": "first"}, {"content": "second"}]), token)
    assert llm.chat("planner", []).content == "first"
    assert llm.models_used == {"planner": "scripted"}  # forwarded to the wrapped client
    token.cancel()
    with pytest.raises(RunCancelled):
        llm.chat("planner", [])

    config = load_config(environ={})
    config.git.allow_push = True
    record = RunRecord(run_id="r", kind="run", project="/p", project_name="p", request="x",
                       options={"max_iterations": 1, "in_place": True, "max_parallel": 4})
    job = configure_job(config, record)
    assert job.git.allow_push is False  # never from the API
    assert job.limits.max_heal_iterations == 1 and job.workspace.mode == "in-place"
    assert job.sprint.max_parallel == 4 and config.workspace.mode == "worktree"  # copy
    fix = configure_job(config, RunRecord(run_id="f", kind="fix", project="/p", project_name="p",
                                          request="", options={"in_place": True}))
    assert fix.workspace.mode == "worktree"  # in_place is a `run` option; fix is in place anyway


def test_run_operations_are_in_the_contract(daemon):
    with make_client(daemon) as client:
        spec = client.get("/openapi.json").json()
    operations = {op["operationId"]: (method, op) for path, methods in spec["paths"].items()
                  for method, op in methods.items() if path.startswith(f"{V1}/runs")}
    assert set(operations) == {"run_start", "runs_list", "run_get", "run_report", "run_journal",
                               "run_cancel"}
    assert "202" in operations["run_start"][1]["responses"]
    assert "429" in operations["run_start"][1]["responses"]
    assert "409" in operations["run_report"][1]["responses"]
    assert all(op["security"] == [{"serviceToken": []}] for _, op in operations.values())
    assert "CLI: local-ezai run" in operations["run_start"][1]["summary"]
