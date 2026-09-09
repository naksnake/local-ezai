"""Fixtures of the chat-ops boundary drills: a bootstrapped platform, a
control plane over it (in-process), the SWE tool server wired to it, a
registered repository with a bare `origin` remote, and a sandbox allowlist
— the environment a hostile chat task meets."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
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
from agentd.control import API_PREFIX
from agentd.control.app import create_app
from agentd.platform_cli import build_context
from agentd.registry_v2 import save_generation
from agentd.render import write_rendered
from agentd.runtime_descriptor import load_descriptors
from tests.conftest import git, happy_path_script
from tests.unit.test_activation import seed_registry
from tests.unit.test_lifecycle import FakeEngineHTTP, FakeRunner

REPO_ROOT = Path(__file__).resolve().parents[3]
TOKEN = "drill-service-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
#: The operator's sandbox allowlist for the drill: the project's checks and
#: read-only git — nothing that reaches the network or the host.
COMMAND_ALLOWLIST = [r"^python3? ", r"^pytest", r"^git (status|diff|log)\b"]


def load_swe_server():
    spec = importlib.util.spec_from_file_location(
        "swe_server_drill", REPO_ROOT / "mcp-servers" / "swe-server" / "swe_server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def wait_terminal(client: TestClient, run_id: str, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"{API_PREFIX}/runs/{run_id}", headers=AUTH).json()
        if body["status"] in ("completed", "failed", "cancelled"):
            return body
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} did not finish: {body}")


@pytest.fixture
def world(tmp_path: Path, tmp_repo: Path, monkeypatch):
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

    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git(tmp_repo, "remote", "add", "origin", str(bare))

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
        "sandbox": {"mode": "host", "command_allowlist": COMMAND_ALLOWLIST},
    }), encoding="utf-8")
    ctx = build_context(load_config(cfg), root, actor="ezaid")
    platform_cli.add_project(ctx, str(tmp_repo))
    app = create_app(ControlConfig(token=TOKEN), ctx, prober=lambda url, headers: (200, "ok"))
    client = TestClient(app)
    swe = load_swe_server()
    tools = swe.SweTools(swe.ControlPlane(client, TOKEN, url="http://daemon.test:8010"),
                         admin_url="http://admin.test:8888", plan_wait_s=120,
                         sleep=lambda seconds: time.sleep(0.05))
    return SimpleNamespace(root=root, cfg=cfg, ctx=ctx, client=client, swe=swe, tools=tools,
                           server=swe.build_server(tools), repo=tmp_repo, bare=bare,
                           script_file=script_file, tmp=tmp_path, runs_dir=tmp_path / "runs")
