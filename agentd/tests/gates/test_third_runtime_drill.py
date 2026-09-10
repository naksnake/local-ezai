"""The third-runtime drill (PR-25, P6; RUNTIME_ABSTRACTION_STRATEGY §6,
ADR-026 (3)): a runtime no platform code has ever heard of — ``mockengine``,
a descriptor under tests/fixtures/providers/ plus an OpenAI-API stub standing
in for its image — is installed, validated, benchmarked, activated, rendered,
served, and rolled back purely through descriptor data. Zero diffs outside
``config/providers/`` and test fixtures: the tripwire below greps for it.

The lifecycle's real side-load path runs: docker is the recording fake of
the PR-4 tests, but every side-load lands on the stub's port (the
``free_port`` seam) and the readiness probe, the one-token validation and
the benchmark are real HTTP against the stub. The renderer, the bootstrap,
the governance queue, the setup pipeline's wait-ready and the compose
verb all consume the fixture descriptor as they would a shipped one."""

from __future__ import annotations

import io
import json
import shutil
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agentd import lifecycle, platform_cli
from agentd.bootstrap import CONSUMED_KEY, RUNTIME_KEY
from agentd.capability import CapabilityVector
from agentd.config import load_config
from agentd.control import TRANSPORT_ENV
from agentd.installer import EXIT_OK, EXIT_PROBLEMS, choose_runtime, run_install
from agentd.installer import Options as InstallOptions
from agentd.lifecycle import SideLoadValidator
from agentd.main_cli import main
from agentd.platform_cli import build_context
from agentd.registry_v2 import list_generations, load_registry, reference_registry
from agentd.render import (
    ENGINE_COMPOSE_FILENAME,
    ENGINE_SERVICE,
    LITELLM_FILENAME,
    PRESET_FILENAME,
    ROLE_MAP_FILENAME,
    rendered_dir,
)
from agentd.runtime_descriptor import load_descriptors
from agentd.setup_pipeline import SetupOptions, SetupPipeline
from tests.unit import test_setup_pipeline as pipeline_tests
from tests.unit.test_lifecycle import FakeRunner
from tests.unit.test_render import _OverrideLoader

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "mockengine.yaml"
RUNTIME = "mockengine"
SEEDS = ("drill-reasoner", "drill-coder", "drill-chat")
CPU_LOW = CapabilityVector(system_memory_gb=15.5, cpu_cores=4, cpu_flags=["avx2"])
BENCH_RATE = 42.0


# ── the "image": an OpenAI-API stub ──────────────────────────────────────────


class MockEngine(ThreadingHTTPServer):
    """What the descriptor's image would be: readiness on the descriptor's
    path, ``/v1/chat/completions`` answering with a usage block and the
    descriptor's own timing block — nothing the platform knows by name."""

    def __init__(self, ready_path: str) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.ready_path = ready_path
        self.polls: list[str] = []
        self.requests: list[dict] = []

    @property
    def port(self) -> int:
        return int(self.server_address[1])


class _Handler(BaseHTTPRequestHandler):
    server: MockEngine

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        self.server.polls.append(self.path)
        if self.path == self.server.ready_path:
            return self._json(200, {"status": "ok"})
        if self.path == "/v1/models":
            return self._json(200, {"object": "list", "data": []})
        return self._json(404, {"error": f"no route {self.path}"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path != "/v1/chat/completions":
            return self._json(404, {"error": f"no route {self.path}"})
        self.server.requests.append(body)
        generated = int(body.get("max_tokens") or 1)
        return self._json(200, {
            "id": "mock-1", "object": "chat.completion", "model": body.get("model"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ready"}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": generated,
                      "total_tokens": 9 + generated},
            "mock_timings": {"tokens_per_second": BENCH_RATE,
                             "prompt_tokens_per_second": 300.0, "generated": generated},
        })

    def log_message(self, *args) -> None:  # quiet
        return None


@pytest.fixture
def stub():
    descriptor = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    server = MockEngine(descriptor["verbs"]["ready"]["path"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


# ── a checkout whose providers/ carries the third descriptor ─────────────────


@pytest.fixture
def drill(tmp_path: Path, monkeypatch, stub: MockEngine):
    root = tmp_path / "local-ezai"
    (root / "config").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "config" / "providers", root / "config" / "providers")
    shutil.copy(FIXTURE, root / "config" / "providers" / FIXTURE.name)   # the only "diff"
    shutil.copytree(REPO_ROOT / "examples" / "sample-project", root / "examples" / "sample-project")
    shutil.copy(REPO_ROOT / ".env.example", root / ".env.example")
    (root / "scripts").mkdir()
    (root / "docker-compose.yml").write_text("services: {}\n")
    weights = root / "w"
    weights.mkdir()
    for name in (*SEEDS, "drill-spare"):
        (weights / f"{name}.gguf").write_bytes(name.encode() * 512)
    (root / ".env").write_text(
        f"{RUNTIME_KEY}={RUNTIME}\n"
        f"REASONING_MODEL=gguf:{weights}/drill-reasoner.gguf\n"
        f"CODING_MODEL=gguf:{weights}/drill-coder.gguf\n"
        f"CHAT_MODEL=gguf:{weights}/drill-chat.gguf\n"
        "LITELLM_MASTER_KEY=sk-drill\n", encoding="utf-8")
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    monkeypatch.delenv("EZAI_CAPABILITY_CLASS", raising=False)
    monkeypatch.setenv(TRANSPORT_ENV, "direct")
    monkeypatch.setenv("USER", "nita")
    runner = FakeRunner()
    monkeypatch.setattr(platform_cli, "detect_vector", lambda: CPU_LOW)
    monkeypatch.setattr(platform_cli, "default_runner", runner)
    monkeypatch.setattr(platform_cli, "http_probe", lambda url: 200)
    # the image: every side-load "starts" (docker faked) on the port the stub listens on;
    # readiness, validation and benchmark are real HTTP through the descriptor's verbs
    monkeypatch.setattr(lifecycle, "free_port", lambda: stub.port)
    clock = pipeline_tests.Clock()   # a probe that never answers times out at once, not in 600 s
    monkeypatch.setattr(platform_cli, "build_validator", lambda ctx: SideLoadValidator(
        ctx.vector, ctx.platform.klass, ctx.platform.accel, platform_root=ctx.root,
        workdir=ctx.workdir, runner=runner, sleep=clock.sleep, clock=clock.now))
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({
        "llm": {"provider": "scripted", "script_path": str(tmp_path / "script.json")},
        "platform": {"config_dir": str(root / "config")},
        "runs_dir": str(tmp_path / "runs")}), encoding="utf-8")
    (tmp_path / "script.json").write_text("[]")
    return SimpleNamespace(root=root, cfg=cfg, weights=weights, runner=runner,
                           config=root / "config")


def cli(drill, *argv: str, as_json: bool = True):
    out = io.StringIO()
    with redirect_stdout(out):
        code = main([*argv, *(["--json"] if as_json else []), "--config", str(drill.cfg)])
    text = out.getvalue()
    return code, (json.loads(text) if as_json and text.strip() else text)


def engine_service(drill) -> dict:
    text = (rendered_dir(drill.config) / ENGINE_COMPOSE_FILENAME).read_text(encoding="utf-8")
    return yaml.load(text, Loader=_OverrideLoader)["services"][ENGINE_SERVICE]


class RecordingHttp(pipeline_tests.FakeHttp):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.gets: list[str] = []

    def get(self, url: str, headers: dict[str, str]):
        self.gets.append(url)
        return super().get(url, headers)


# ── the drill ────────────────────────────────────────────────────────────────


def test_the_third_runtime_runs_end_to_end_through_descriptor_and_image(drill, stub):
    # bootstrap: the seeds name the runtime; install → validate → benchmark → generation 1
    code, data = cli(drill, "bootstrap", "--by", "nita")
    assert code == 0, data
    assert data["generation"] == 1 and data["runtime"] == RUNTIME and data["stamped"]
    registry = load_registry(drill.config)
    assert set(registry.models) == set(SEEDS) and registry.generation == 1
    for entry in registry.models.values():
        assert entry.provider == RUNTIME and entry.state == "active"
        assert entry.benchmarks["tokens_per_s"] == BENCH_RATE      # the descriptor's timing keys
        assert entry.benchmarks["via"] == "side-load" and entry.benchmarks["runtime"] == RUNTIME
        assert entry.tool_call_format == "generic"                  # the runtime's generic handler
    assert f"{CONSUMED_KEY}=1@" in (drill.root / ".env").read_text(encoding="utf-8")
    # …weights placed where the descriptor mounts them
    assert all((drill.root / "models" / "mock" / f"{name}.gguf").is_file() for name in SEEDS)

    # the lifecycle spoke to the image only through the descriptor's verbs
    assert stub.polls and set(stub.polls) == {"/healthz"}           # readiness = descriptor data
    probes = [r for r in stub.requests if r["max_tokens"] == 1]     # validate_model
    benches = [r for r in stub.requests if r["max_tokens"] == 64]   # bench
    assert len(probes) == len(benches) == 3 and len(stub.requests) == 6
    assert {r["model"] for r in stub.requests} == {f"mock/{name}" for name in SEEDS}  # served_id
    assert drill.runner.verbs().count("up") == 6 and drill.runner.verbs().count("down") == 6
    sideload = yaml.safe_load((drill.config / ".sideload" / "drill-coder" /
                               "docker-compose.sideload.yml").read_text(encoding="utf-8"))
    engine = sideload["services"]["engine"]
    assert engine["image"] == "example.invalid/mockengine:cpu"
    assert engine["command"][:2] == ["serve", "/weights/drill-coder.gguf"]
    assert engine["command"][-4:] == ["--latency-ms", "0", "--tools", "generic"]
    assert engine["deploy"] == {"resources": {"limits": {"memory": "1g"}}}

    # generation 1 rendered: the multi form (three models behind one slot)
    service = engine_service(drill)
    assert service["image"] == "example.invalid/mockengine:cpu"
    assert service["command"] == ["serve-many", "/presets/mock-models.ini", "--port", "8000",
                                  "--max", "3"]
    assert service["healthcheck"]["test"][1].endswith(":8000/healthz || exit 1")
    assert service["healthcheck"]["interval"] == "1s" and service["healthcheck"]["retries"] == 2
    assert "${MOCK_WEIGHTS_DIR:-./models/mock}:/weights:ro" in service["volumes"]
    assert any(v.endswith(f"/{PRESET_FILENAME}:/presets/mock-models.ini:ro")
               for v in service["volumes"])
    assert service["environment"] == ["MOCK_LOG_LEVEL=info"]
    preset = (rendered_dir(drill.config) / PRESET_FILENAME).read_text(encoding="utf-8")
    assert "[*]\nlatency_ms = 0\nthreads = 2\n" in preset               # cpu-low class tuning
    assert ("[drill-coder]\npath = /weights/drill-coder.gguf\nctx = 8192\ntools = generic\n"
            in preset)
    litellm = yaml.safe_load((rendered_dir(drill.config) / LITELLM_FILENAME).read_text())
    routes = {m["model_name"]: m["litellm_params"]["model"] for m in litellm["model_list"]}
    assert routes["drill-coder"] == "openai/mock/drill-coder"
    coding_roles = [r for r, s in reference_registry().roles.items() if s.group == "coding"]
    assert coding_roles and all(routes[f"role-{r}"] == "openai/mock/drill-coder"
                                for r in coding_roles)
    role_map = yaml.safe_load((rendered_dir(drill.config) / ROLE_MAP_FILENAME).read_text())
    assert role_map["runtime"] == RUNTIME

    # day 2 through the CLI: install → benchmark → activate (approval) → approve → rollback;
    # no --runtime: the slot's own runtime serves the new source (the drill found the old
    # default picking the first descriptor that serves the format, alphabetically)
    code, data = cli(drill, "model", "install", f"gguf:{drill.weights}/drill-spare.gguf",
                     "--name", "drill-spare")
    assert code == 0 and data["ok"] and data["state"] == "installed", data
    assert load_registry(drill.config).models["drill-spare"].provider == RUNTIME
    assert data["artifact"].endswith("/models/mock/drill-spare.gguf")
    code, data = cli(drill, "model", "benchmark", "drill-spare")
    assert code == 0 and data["tokens_per_s"] == BENCH_RATE and data["state"] == "benchmarked"
    # (into the chat group: a day-2 user source declares no tool-call format, so the
    # tool-calling roles refuse it at render time — negotiation, not the runtime seam)
    code, data = cli(drill, "model", "activate", "drill-spare", "--group", "chat",
                     "--by", "nita")
    assert code == 0 and data["applied"] is None and data["request"]["status"] == "pending"
    request_id = data["request"]["id"]
    code, data = cli(drill, "governance", "approve", request_id, "--reason", "drill",
                     "--by", "nita")
    assert code == 0 and data["applied"]["ok"], data
    registry = load_registry(drill.config)
    assert registry.groups["chat"] == ["drill-spare", "drill-chat"]
    assert registry.models["drill-spare"].state == "active"
    assert engine_service(drill)["command"][-2:] == ["--max", "4"]
    assert "[drill-spare]" in (rendered_dir(drill.config) / PRESET_FILENAME).read_text()
    generation_before = registry.generation
    code, data = cli(drill, "model", "rollback", "--reason", "drill", "--by", "nita")
    assert code == 0 and data["ok"], data
    registry = load_registry(drill.config)
    assert registry.generation == generation_before + 1
    assert registry.groups["chat"] == ["drill-chat"]
    assert registry.models["drill-spare"].state == "benchmarked"
    assert engine_service(drill)["command"][-2:] == ["--max", "3"]
    assert len(list_generations(drill.config)) == registry.generation

    # serve: the setup pipeline waits on the descriptor's readiness path, starts the stack,
    # smoke-tests it and reports the runtime it never heard of
    ctx = build_context(load_config(drill.cfg), drill.root, actor="nita")
    clock = pipeline_tests.Clock()
    http = RecordingHttp()
    run = SetupPipeline(ctx, SetupOptions(root=drill.root, skip_images=True),
                        runner=pipeline_tests.FakeDocker(), http=http,
                        plan_fn=pipeline_tests.fake_plan, evaluate_fn=pipeline_tests.fake_evaluate,
                        sleep=clock.sleep, clock=clock.now, environ={},
                        now="2026-09-09T12:00:00", say=lambda text: None)
    report = run.run()
    assert report.ready, report.lines()
    assert report.runtime == RUNTIME and pipeline_tests.steps(report)["bootstrap"] == "skipped"
    engine_polls = [url for url in http.gets if ":8000/" in url]
    assert engine_polls and engine_polls[0].endswith("/healthz")   # wait-ready = descriptor data
    # residual (recorded in the PR-25 artifact): the generic services sweep that follows
    # probes the engine slot at the health TABLE's path, not the descriptor's
    assert any(url.endswith(":8000/health") for url in engine_polls[1:])

    # up with the rendered override: the compose files the CLI passes include the materialization
    code, text = cli(drill, "up", "--profile", "n97", "--rendered", as_json=False)
    assert code == 0, text
    last = drill.runner.calls[-1]
    assert last[:2] == ["docker", "compose"] and last[-2:] == ["up", "-d"]
    assert str(rendered_dir(drill.config) / ENGINE_COMPOSE_FILENAME) in last


def test_the_installer_and_the_bootstrap_take_the_third_runtime_from_data(drill):
    descriptors = load_descriptors(drill.config)
    assert RUNTIME in descriptors and set(descriptors) >= {"llamacpp", "vllm"}
    runtime, reason, problem = choose_runtime(descriptors, "cpu-low", "none", requested=RUNTIME)
    assert (runtime, reason, problem) == (RUNTIME, "requested with --runtime", None)
    assert choose_runtime(descriptors, "cpu-low", "none")[0] != RUNTIME   # no default class claimed
    # install.sh --runtime mockengine: the seeds validate against the third descriptor (F8)
    report = run_install(InstallOptions(root=drill.root, runtime=RUNTIME, interactive=False),
                         vector_fn=lambda: CPU_LOW, now="2026-09-09T12:00:00", say=lambda t: None)
    assert report.exit_code == EXIT_OK and report.problems == [] and report.runtime == RUNTIME
    # …and a format the runtime does not serve is refused with the fix, before any download
    env = (drill.root / ".env").read_text(encoding="utf-8")
    (drill.root / ".env").write_text(
        env.replace(f"CODING_MODEL=gguf:{drill.weights}/drill-coder.gguf",
                    "CODING_MODEL=hf:example/coder"), encoding="utf-8")
    report = run_install(InstallOptions(root=drill.root, runtime=RUNTIME, interactive=False),
                         vector_fn=lambda: CPU_LOW, now="2026-09-09T12:00:00", say=lambda t: None)
    assert report.exit_code == EXIT_PROBLEMS
    assert any("CODING_MODEL is hf" in p and f"{RUNTIME_KEY}={RUNTIME} serves gguf" in p
               for p in report.problems), report.problems


def test_no_platform_code_knows_the_drill_runtime():
    """Zero diffs outside descriptors and fixtures: the runtime id appears in
    no shipped code, data, compose file, script or descriptor."""
    scanned = [
        *(REPO_ROOT / "agentd" / "src").rglob("*.py"),
        *(REPO_ROOT / "agentd" / "src").rglob("*.yaml"),
        *(REPO_ROOT / "monitor").rglob("*.py"),
        *(REPO_ROOT / "mcp-servers").rglob("*.py"),
        *(REPO_ROOT / "config" / "providers").glob("*.yaml"),
        *(REPO_ROOT / "scripts").glob("*.sh"),
        *REPO_ROOT.glob("docker-compose*.yml"),
        REPO_ROOT / "Makefile", REPO_ROOT / "install.sh", REPO_ROOT / ".env.example",
    ]
    hits = [str(p.relative_to(REPO_ROOT)) for p in scanned
            if RUNTIME in p.read_text(encoding="utf-8", errors="ignore")]
    assert hits == [], f"the drill runtime leaked into platform code: {hits}"
    assert set(load_descriptors(REPO_ROOT / "config")) == {"llamacpp", "vllm"}
