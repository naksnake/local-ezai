"""Serve the Admin Center for the Browser QA journeys (PR-20, ADR-030):
the monitor as shipped over an in-process control plane, on a bootstrapped
scratch platform, with the lifecycle seams the offline tests fake and fake
sprint / evolution pipelines that return realistic reports.

    python3 agentd/tests/fixtures/admin_center_app.py --port 8899 --state /tmp/ezai-journeys

The state directory receives: platform/ (registry, generations, rendered
artifacts, governance log), sample-project/ (a git repository to register)
and new-model.gguf (a model file to install). Login is disabled: the
journeys are an admin walkthrough; the login paths have their own tests.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

AGENTD_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = AGENTD_DIR.parent
sys.path.insert(0, str(AGENTD_DIR))  # the `tests` package: fakes and fixtures
sys.path.insert(0, str(REPO_ROOT / "monitor"))  # admin_center, a sibling of monitor.py

TOKEN = "journey-service-token"


def build_platform(state: Path):
    import yaml

    from agentd import platform_cli
    from agentd.activation import Platform
    from agentd.capability import CapabilityVector
    from agentd.config import load_config
    from agentd.control import api as control_api
    from agentd.platform_cli import build_context
    from agentd.registry_v2 import RegistryV2, save_generation
    from agentd.render import write_rendered
    from agentd.runtime_descriptor import load_descriptors
    from tests.unit.test_activation import seed_registry
    from tests.unit.test_lifecycle import FakeEngineHTTP, FakeRunner

    root = state / "platform"
    if root.exists():
        shutil.rmtree(root)
    (root / "config").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "config" / "providers", root / "config" / "providers")
    (root / "docker-compose.yml").write_text("services: {}\n")
    seed = seed_registry().model_dump(mode="json")
    # an unpinned role in the reasoning group, so swapping the reasoning
    # model is a governed change (journey 1)
    seed["roles"]["planner"] = {"group": "reasoning", "requires": {"min_context": 4096}}
    saved = save_generation(RegistryV2.model_validate(seed), root / "config", note="bootstrap")
    platform = Platform(config_dir=root / "config", platform_root=root,
                        descriptors=load_descriptors(root / "config"),
                        vector=CapabilityVector(system_memory_gb=15.5, cpu_cores=4),
                        capability_class="cpu-low", accelerator="none")
    write_rendered(platform.render(saved), platform.rendered)

    platform_cli.default_runner = FakeRunner()
    platform_cli.http_probe = lambda url: 200
    platform_cli.engine_http = lambda: FakeEngineHTTP(
        timings={"predicted_per_second": 15.5, "prompt_per_second": 80.0, "predicted_n": 100})
    platform_cli.build_validator = (
        lambda ctx: lambda descriptor, name, entry: platform_cli.lifecycle
        .ProbeResult(True, "", 0.2, name))
    control_api.docker_available = lambda: False

    cfg = state / "agentd.yaml"
    cfg.write_text(yaml.safe_dump({
        "llm": {"provider": "scripted", "script_path": str(state / "script.json")},
        "platform": {"config_dir": str(root / "config")},
        "runs_dir": str(state / "runs")}), encoding="utf-8")
    (state / "script.json").write_text("[]")
    return build_context(load_config(cfg), root, actor="ezaid")


def build_sample_project(state: Path) -> Path:
    project = state / "sample-project"
    if project.exists():
        shutil.rmtree(project)
    project.mkdir(parents=True)
    subprocess.run(["git", "-C", str(project), "init", "-q", "-b", "main"], check=True)
    (project / "README.md").write_text("# sample\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(project), "-c", "user.name=journey",
                    "-c", "user.email=journey@example.invalid", "commit", "-q", "-m", "init"],
                   check=True)
    return project


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8899")))
    parser.add_argument("--state", required=True)
    args = parser.parse_args()
    state = Path(args.state).resolve()
    state.mkdir(parents=True, exist_ok=True)

    ctx = build_platform(state)
    build_sample_project(state)
    (state / "new-model.gguf").write_bytes(b"g" * 4096)

    import httpx
    import uvicorn
    from fastapi.testclient import TestClient

    from agentd.config import ControlConfig
    from agentd.control.app import create_app
    from agentd.control.runs import RunRegistry
    from tests.integration.test_admin_center_workbench import fake_evolve, fake_sprint
    from tests.unit.test_control_runs import DummyLLM

    registry = RunRegistry(ctx.config, state / "platform" / "config" / "control" / "runs",
                           audit=ctx.queue.audit_log, max_concurrent=2, max_queued=2,
                           pipelines={"sprint": fake_sprint, "evolve": fake_evolve},
                           llm_factory=lambda config: DummyLLM())
    control_app = create_app(ControlConfig(token=TOKEN), ctx,
                             prober=lambda url, headers: (200, "ok healthy passed openapi"),
                             runs=registry)
    control = TestClient(control_app)
    control.__enter__()  # runs the daemon's lifespan for the life of this process

    os.environ.update({"MONITOR_AUTH": "false", "EZAI_CONTROL_URL": "http://ezaid.journey:8010",
                       "EZAI_CONTROL_TOKEN": TOKEN})
    spec = importlib.util.spec_from_file_location("monitor_journey",
                                                  REPO_ROOT / "monitor" / "monitor.py")
    monitor = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(monitor)
    monitor.app.state.control_plane.transport = httpx.ASGITransport(app=control_app)
    print(f"admin center journeys: http://127.0.0.1:{args.port}/overview (state {state})",
          flush=True)
    uvicorn.run(monitor.app, host="127.0.0.1", port=args.port, log_level="warning",
                lifespan="off")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
