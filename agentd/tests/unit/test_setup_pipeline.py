"""The setup pipeline — steps 4–8 of the first run and the ``init`` fallback
(PR-22, ADR-031; docs/FINAL_FIRST_RUN_EXPERIENCE.md §3–§4,
docs/FIRST_RUN_EXPERIENCE.md §3–§5).

Offline: Docker is a recording fake runner (git runs for real so the sample
project is a repository), HTTP is a scripted fake, the planner and the
evaluator are stubs, the clock advances on sleep. Model names are fixtures;
the shipped descriptors, catalog and sample project are the subject."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agentd import platform_cli
from agentd import setup_pipeline as sp
from agentd.bootstrap import RUNTIME_KEY, parse_env
from agentd.capability import CapabilityVector
from agentd.config import load_config
from agentd.control.health import DEFAULT_TARGETS
from agentd.main_cli import main
from agentd.platform_cli import build_context
from agentd.registry_v2 import load_registry, registry_path
from agentd.schemas import ModelEvalReport, ModelProbeResult, Plan, PlanTask
from agentd.setup_pipeline import (
    BANNER_KEY,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_REVIEW,
    HOST_PORTS,
    ORCHESTRATOR_MODEL_ID,
    SAMPLE_CODEWORD,
    InitOptions,
    SetupError,
    SetupOptions,
    SetupPipeline,
    run_init,
)
from tests.unit.test_lifecycle import FakeEngineHTTP, FakeRunner

REPO_ROOT = Path(__file__).resolve().parents[3]
CPU_LOW = CapabilityVector(system_memory_gb=15.5, cpu_cores=4, cpu_flags=["avx2"])
SERVICE_BODIES = {"embed": "healthy", "qdrant": "passed", "mcpo": "openapi"}


# ── fakes ────────────────────────────────────────────────────────────────────


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeDocker:
    """Records every command; docker answers as scripted, git runs for real."""

    def __init__(self, fail: dict[str, str] | None = None, stdout: dict[str, str] | None = None):
        self.calls: list[list[str]] = []
        self.fail = fail or {}
        self.stdout = stdout or {}

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        if command[0] == "git":
            return subprocess.run(command, capture_output=True, text=True, check=False)
        joined = " ".join(command)
        for needle, message in self.fail.items():
            if needle in joined:
                return subprocess.CompletedProcess(command, 1, "", message)
        out = next((text for needle, text in self.stdout.items() if needle in joined), "ok")
        return subprocess.CompletedProcess(command, 0, out, "")

    def compose_verbs(self) -> list[str]:
        return [" ".join(c[c.index("compose") + 1:][len(self.compose_files(c)) * 2:])
                for c in self.calls if c[:2] == ["docker", "compose"]]

    @staticmethod
    def compose_files(command: list[str]) -> list[str]:
        return [command[i + 1] for i, tok in enumerate(command) if tok == "-f"]


class FakeHttp:
    def __init__(self, *, engine_after: int = 2, down: set[str] | None = None,
                 answers: list[str] | None = None, openwebui_after_banner: bool = True):
        self.engine_polls = 0
        self.engine_after = engine_after
        self.down = set(down or ())
        self.answers = list(answers or ["ready", f"The codeword is {SAMPLE_CODEWORD}."])
        self.openwebui_after_banner = openwebui_after_banner
        self.banner_recreated = False
        self.posts: list[dict] = []

    def service_for(self, url: str) -> str | None:
        port = int(url.split(":")[2].split("/")[0])
        return next((s for s, (_, default) in HOST_PORTS.items() if default == port), None)

    def get(self, url: str, headers: dict[str, str]) -> tuple[int, str]:
        service = self.service_for(url)
        if service == "engine" and "/health" in url:
            self.engine_polls += 1
            if self.engine_polls < self.engine_after:
                raise ConnectionError("engine not listening yet")
        if service in self.down:
            return 503, "down"
        if service == "openwebui" and self.banner_recreated and not self.openwebui_after_banner:
            return 500, "crash"
        return 200, SERVICE_BODIES.get(service or "", "")

    def post_json(self, url, payload, headers, timeout):
        self.posts.append({"url": url, "payload": payload, "headers": headers})
        answer = self.answers.pop(0) if self.answers else ""
        if answer == "HTTP500":
            return 500, {"error": "no model"}
        return 200, {"choices": [{"message": {"content": answer}}]}


def fake_plan(config, project, task):
    assert project.name == "sample-project" and "health" in task
    assert config.llm.base_url.startswith("http://localhost:") and config.llm.api_key
    return Plan(goal="add a /health endpoint", tasks=[PlanTask(id="T1", intent="add /health")])


def fake_evaluate(config, project):
    return ModelEvalReport(evaluated_at="now", passed=True,
                           results=[ModelProbeResult(role="planner", model="role-planner", ok=True),
                                    ModelProbeResult(role="chat", model="role-chat", ok=True)])


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def platform(tmp_path: Path, monkeypatch):
    """A checkout-like root with the shipped descriptors, catalog defaults,
    compose marker, the bundled sample project, seeds in .env, and the
    bootstrap's docker/HTTP seams faked (as the PR-7 CLI tests do)."""
    root = tmp_path / "local-ezai"
    (root / "config").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "config" / "providers", root / "config" / "providers")
    shutil.copytree(REPO_ROOT / "examples" / "sample-project", root / "examples" / "sample-project")
    (root / "scripts").mkdir()
    (root / "docker-compose.yml").write_text("services: {}\n")
    (root / "w").mkdir()
    for name in ("reasoner", "coder", "chatter"):
        (root / "w" / f"{name}.gguf").write_bytes(name.encode() * 256)
    (root / ".env").write_text(
        f"{RUNTIME_KEY}=llamacpp\nREASONING_MODEL=gguf:{root}/w/reasoner.gguf\n"
        f"CODING_MODEL=gguf:{root}/w/coder.gguf\nCHAT_MODEL=gguf:{root}/w/chatter.gguf\n"
        "LITELLM_MASTER_KEY=sk-test\n", encoding="utf-8")
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    monkeypatch.delenv("EZAI_CAPABILITY_CLASS", raising=False)
    monkeypatch.setattr(platform_cli, "detect_vector", lambda: CPU_LOW)
    monkeypatch.setattr(platform_cli, "default_runner", FakeRunner())
    monkeypatch.setattr(platform_cli, "http_probe", lambda url: 200)
    monkeypatch.setattr(platform_cli, "engine_http", lambda: FakeEngineHTTP(
        timings={"predicted_per_second": 7.5, "prompt_per_second": 40.0, "predicted_n": 100}))
    monkeypatch.setattr(platform_cli, "build_validator",
                        lambda ctx: lambda descriptor, name, entry: platform_cli.lifecycle
                        .ProbeResult(True, "", 0.2, name))
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({
        "llm": {"provider": "scripted", "script_path": str(tmp_path / "script.json")},
        "platform": {"config_dir": str(root / "config")},
        "runs_dir": str(tmp_path / "runs")}), encoding="utf-8")
    (tmp_path / "script.json").write_text("[]")
    ctx = build_context(load_config(cfg), root, actor="nita")
    return SimpleNamespace(root=root, ctx=ctx, cfg=cfg)


def pipeline(platform, *, docker=None, http=None, environ=None, **options):
    clock = Clock()
    said: list[str] = []
    docker = docker or FakeDocker()
    http = http or FakeHttp()
    run = SetupPipeline(platform.ctx, SetupOptions(root=platform.root, **options),
                        runner=docker, http=http, plan_fn=fake_plan, evaluate_fn=fake_evaluate,
                        sleep=clock.sleep, clock=clock.now, environ=environ or {},
                        now="2026-09-09T12:00:00", say=said.append)
    return run, docker, http, said


def steps(report) -> dict[str, str]:
    return {s.name: s.status for s in report.steps}


# ── the first run, end to end ────────────────────────────────────────────────


def test_first_run_bootstraps_starts_verifies_and_reports(platform):
    run, docker, http, said = pipeline(platform, environ={"LAN_HOST": "192.168.1.5",
                                                          "OPENWEBUI_PORT": "3100"})
    report = run.run()
    assert report.exit_code == EXIT_OK and report.ready, report.lines()
    assert steps(report) == {"bootstrap": "ok", "images": "ok", "embed model": "ok", "up": "ok",
                             "wait-ready": "ok", "verify": "ok", "report": "ok"}
    # 4/5: the bootstrap made generation 1 and rendered the engine override
    registry = load_registry(platform.root / "config")
    assert registry.generation == 1 and report.generation == 1
    assert report.models == {"reasoning": "reasoner", "coding": "coder", "chat": "chatter"}
    rendered = platform.root / "config" / "rendered" / "docker-compose.engine.yml"
    assert rendered.is_file()
    # images and up went through compose with the base file + the rendered override
    verbs = docker.compose_verbs()
    assert verbs[0].startswith("pull openwebui litellm vllm qdrant searxng")
    assert verbs[1] == "build" and verbs[2] == "up -d" and verbs[3] == "up -d openwebui"
    first = [c for c in docker.calls if c[:2] == ["docker", "compose"]][0]
    assert FakeDocker.compose_files(first) == [str(platform.root / "docker-compose.yml"),
                                               str(rendered)]
    assert ["bash", str(platform.root / "scripts" / "download-embed.sh")] in docker.calls
    # 6: the engine was polled through the descriptor's ready path, then every service
    assert http.engine_polls == 3  # two ready polls, then the health sweep
    assert report.health == {t.id: True for t in DEFAULT_TARGETS}
    # 7: the four smoke checks
    assert [(c.name, c.ok, c.required) for c in report.smoke] == [
        ("chat", True, True), ("RAG", True, False), ("swe_plan", True, False),
        ("models", True, False)]
    assert http.posts[0]["payload"]["model"] == "role-chat"
    assert http.posts[0]["headers"] == {"Authorization": "Bearer sk-test"}
    assert http.posts[0]["url"] == "http://localhost:4000/v1/chat/completions"
    sample_doc = platform.root / "documents" / "local-ezai-first-run.md"
    assert SAMPLE_CODEWORD in sample_doc.read_text(encoding="utf-8")
    assert ["bash", str(platform.root / "scripts" / "embed-documents.sh")] in docker.calls
    project = platform.root / "sample-project"
    assert (project / ".git").is_dir() and (project / "app.py").is_file()
    assert [p["name"] for p in platform_cli.list_projects(platform.ctx)["projects"]] == [
        "sample-project"]
    # 8: report files, URLs on the LAN host and the relocated port, the card
    assert report.urls == {"webui": "http://192.168.1.5:3100",
                           "admin": "http://192.168.1.5:8888/overview",
                           "orchestrator": f"http://192.168.1.5:3100/?models={ORCHESTRATOR_MODEL_ID}"}
    first_run = platform.root / "config" / "first-run"
    data = json.loads((first_run / "report.json").read_text(encoding="utf-8"))
    assert data["ok"] and data["ready"] and data["models"]["chat"] == "chatter"
    card = (first_run / "report.md").read_text(encoding="utf-8")
    assert "✔ Platform ready — http://192.168.1.5:3100" in card and "## Smoke" in card
    assert report.card()[0] == "✔ Platform ready — http://192.168.1.5:3100"
    # the WebUI banner: a single-quoted JSON value in the optional env_file
    banner_env = parse_env((first_run / "openwebui.env").read_text(encoding="utf-8"))
    [banner] = json.loads(banner_env[BANNER_KEY])
    assert banner["id"] == "local-ezai-first-run" and banner["type"] == "success"
    assert "192.168.1.5:8888/overview" in banner["content"] and "'" not in banner["content"]
    assert set(banner) == {"id", "type", "title", "content", "dismissible", "timestamp"}
    assert report.banner == "shown in the WebUI"
    assert report.persona.startswith("Local-EZAI Orchestrator installed")
    assert any("sign up at http://192.168.1.5:3100" in step for step in report.next_steps)
    assert said[0].startswith("local-ezai setup — profile n97 · class cpu-low")
    lines = "\n".join(report.lines())
    assert "[1/7] bootstrap    ✓ generation 1 bootstrapped" in lines and "✔ Platform ready" in lines


def test_a_second_run_is_idempotent(platform):
    run, docker, http, _ = pipeline(platform)
    assert run.run().exit_code == EXIT_OK
    generation = load_registry(platform.root / "config").generation
    project_head = subprocess.run(["git", "-C", str(platform.root / "sample-project"),
                                   "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    again, docker, http, _ = pipeline(platform, docker=FakeDocker(
        fail={"register-orchestrator.sh": "❌ no user account yet — open the WebUI"}))
    report = again.run()
    assert report.exit_code == EXIT_OK and steps(report)["bootstrap"] == "skipped"
    assert "generation 1 exists" in report.steps[0].detail
    assert load_registry(platform.root / "config").generation == generation
    assert subprocess.run(["git", "-C", str(platform.root / "sample-project"), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout == project_head
    assert not any(c[0] == "git" and "init" in c for c in docker.calls)
    assert [p["name"] for p in platform_cli.list_projects(platform.ctx)["projects"]] == [
        "sample-project"]
    assert report.persona == ("after your first login (the first account is admin): "
                              "make orchestrator")


# ── failures stop at the right step, always with the fix ─────────────────────


def test_seed_problems_stop_before_any_docker_command(platform):
    (platform.root / ".env").write_text(
        f"{RUNTIME_KEY}=vllm\nREASONING_MODEL=gguf:{platform.root}/w/reasoner.gguf\n"
        f"CODING_MODEL=hf:org/a\nCHAT_MODEL=hf:org/a\n", encoding="utf-8")
    run, docker, http, _ = pipeline(platform)
    report = run.run()
    assert report.exit_code == EXIT_FAILED and not report.ready
    assert steps(report) == {"bootstrap": "failed", "report": "ok"}
    assert "REASONING_MODEL is gguf but AI_RUNTIME=vllm serves" in report.steps[0].detail
    assert docker.calls == [] and http.posts == []
    assert not registry_path(platform.root / "config").is_file()
    assert report.banner == "not shown (platform not ready)"
    assert (platform.root / "config" / "first-run" / "report.md").is_file()
    assert report.card()[0].startswith("✗ Platform not ready yet")


def test_image_pull_failure_stops_before_up_and_skip_flags_skip(platform):
    run, docker, _, _ = pipeline(platform, docker=FakeDocker(fail={"pull": "denied"}))
    report = run.run()
    assert steps(report) == {"bootstrap": "ok", "images": "failed", "report": "ok"}
    assert "docker compose pull failed: denied" in report.steps[1].detail
    assert report.exit_code == EXIT_FAILED and "up -d" not in docker.compose_verbs()
    run, docker, http, _ = pipeline(platform, skip_images=True, skip_smoke=True, skip_banner=True)
    report = run.run()
    assert steps(report) == {"bootstrap": "skipped", "images": "skipped", "embed model": "ok",
                             "up": "ok", "wait-ready": "ok", "verify": "skipped", "report": "ok"}
    assert report.exit_code == EXIT_OK and report.ready and report.smoke == []
    assert report.banner == "skipped (--skip-banner)" and http.posts == []
    assert docker.compose_verbs() == ["up -d"]  # no pull, no build, no banner recreate


def test_engine_never_ready_fails_with_the_logs_hint(platform):
    run, docker, http, _ = pipeline(platform, http=FakeHttp(engine_after=10_000))
    report = run.run()
    assert steps(report)["wait-ready"] == "failed" and "verify" not in steps(report)
    detail = report.steps[-2].detail
    assert "did not answer http://localhost:8000/health within 600s" in detail
    assert "docker compose logs vllm" in detail
    assert report.health == {"engine": False} and not report.ready
    assert report.exit_code == EXIT_FAILED and report.persona == "skipped (not ready)"


def test_required_service_down_fails_optional_service_down_warns(platform):
    run, *_ = pipeline(platform, http=FakeHttp(down={"router"}))
    report = run.run()
    assert steps(report)["wait-ready"] == "failed"
    assert "router not healthy" in report.steps[-2].detail
    assert report.health["router"] is False and report.health["engine"] is True
    run, *_ = pipeline(platform, http=FakeHttp(down={"searxng"}))
    report = run.run()
    assert steps(report)["wait-ready"] == "warning" and "down: searxng" in report.steps[-3].detail
    assert report.ready and report.exit_code == EXIT_OK


def test_chat_turn_is_required_the_other_smoke_checks_are_advisory(platform):
    run, *_ = pipeline(platform, http=FakeHttp(answers=["HTTP500"]))
    report = run.run()
    assert steps(report)["verify"] == "failed" and "the chat turn failed" in report.steps[-2].detail
    assert [c.name for c in report.smoke] == ["chat"] and not report.ready
    assert report.exit_code == EXIT_FAILED
    run, docker, http, _ = pipeline(platform, http=FakeHttp(answers=["ready", "no idea"]))
    run.plan_fn = lambda config, project, task: (_ for _ in ()).throw(RuntimeError("planner down"))
    run.evaluate_fn = lambda config, project: ModelEvalReport(
        evaluated_at="now", passed=False,
        results=[ModelProbeResult(role="coder", model="role-coder", ok=False, error="timeout")])
    report = run.run()
    assert steps(report)["verify"] == "warning" and report.ready and report.exit_code == EXIT_OK
    assert [(c.name, c.ok) for c in report.smoke] == [("chat", True), ("RAG", False),
                                                       ("swe_plan", False), ("models", False)]
    assert f"expected {SAMPLE_CODEWORD}" in report.smoke[1].detail
    assert "RuntimeError: planner down" in report.smoke[2].detail
    assert "0/1 roles answered — failed: coder (timeout)" in report.smoke[3].detail
    banner = json.loads(parse_env((platform.root / "config" / "first-run" / "openwebui.env")
                                  .read_text(encoding="utf-8"))[BANNER_KEY])
    assert "Smoke 1/4" in banner[0]["content"]


def test_banner_is_removed_when_openwebui_does_not_come_back(platform):
    run, docker, http, _ = pipeline(platform, http=FakeHttp(openwebui_after_banner=False))
    # the fake flips OpenWebUI to unhealthy once the banner recreate happened
    original_compose = run.compose

    def compose(*verb):
        if verb == ("up", "-d", "openwebui"):
            http.banner_recreated = True
        return original_compose(*verb)

    run.compose = compose
    report = run.run()
    assert report.ready and report.exit_code == EXIT_OK
    assert report.banner.startswith("removed — OpenWebUI did not come back")
    assert not (platform.root / "config" / "first-run" / "openwebui.env").exists()
    assert docker.compose_verbs().count("up -d openwebui") == 2   # set, then rolled back
    assert "Platform ready" in (platform.root / "config" / "first-run" / "report.md").read_text()


def test_host_targets_mirror_the_daemons_health_table_on_host_ports(platform):
    run, *_ = pipeline(platform, environ={"LITELLM_PORT": "4400", "MONITOR_PORT": "9999"})
    targets = {t.id: t for t in run.host_targets()}
    assert set(targets) == {t.id for t in DEFAULT_TARGETS} == set(HOST_PORTS)
    assert targets["router"].url == "http://localhost:4400/health/liveliness"
    assert targets["monitor"].url == "http://localhost:9999/api/status"
    assert targets["monitor"].auth_env == "MCP_API_KEY" and targets["embed"].pattern == "healthy"
    assert run.host_url("router", "/v1") == "http://localhost:4400/v1"
    assert run.public_url("openwebui") == "http://localhost:3000"


# ── init: the fallback wizard ────────────────────────────────────────────────


class StubPipeline:
    made: list = []

    def __init__(self, ctx, options, **kwargs):
        self.ctx, self.options, self.kwargs = ctx, options, kwargs
        StubPipeline.made.append(self)

    def run(self):
        return sp.SetupReport(root=str(self.options.root), ready=True, exit_code=EXIT_OK,
                              models={"chat": "stub"})


def init(platform, env_text: str, *, ask=None, **options):
    (platform.root / ".env").write_text(env_text, encoding="utf-8")
    said: list[str] = []
    StubPipeline.made.clear()
    outcome = run_init(platform.ctx, InitOptions(root=platform.root, interactive=ask is not None,
                                                 **options),
                       ask=ask or (lambda prompt: ""), say=said.append,
                       pipeline_factory=StubPipeline)
    return outcome, said


def test_init_proposes_the_recommended_set_and_writes_catalog_ids(platform):
    outcome, said = init(platform, "LITELLM_MASTER_KEY=sk-test\n", assume_yes=True)
    assert isinstance(outcome, sp.SetupReport) and outcome.exit_code == EXIT_OK
    assert said[0].startswith("hardware check: accelerator none · 15.5 GB RAM · 4 cores → class "
                              "cpu-low")
    assert any(line.startswith("model set (recommended for class cpu-low on llamacpp)")
               for line in said)
    env = parse_env((platform.root / ".env").read_text(encoding="utf-8"))
    assert env[RUNTIME_KEY] == "llamacpp"
    for key in ("REASONING_MODEL", "CODING_MODEL", "CHAT_MODEL"):
        assert env[key] in platform.ctx.catalog.entries, key   # catalog ids, not `auto`
    assert any(line.startswith("seeds written to .env: reasoning ←") for line in said)
    [made] = StubPipeline.made
    assert made.options.root == platform.root and made.options.env_path == platform.root / ".env"


def test_init_interactive_accepts_or_overrides_per_group(platform):
    answers = iter(["", f"gguf:{platform.root}/w/coder.gguf", ""])
    outcome, said = init(platform, "LITELLM_MASTER_KEY=sk-test\n", ask=lambda p: next(answers))
    assert isinstance(outcome, sp.SetupReport)
    env = parse_env((platform.root / ".env").read_text(encoding="utf-8"))
    assert env["CODING_MODEL"] == f"gguf:{platform.root}/w/coder.gguf"
    assert env["REASONING_MODEL"] in platform.ctx.catalog.entries
    outcome, said = init(platform, "LITELLM_MASTER_KEY=sk-test\n", ask=lambda p: "not a ref!")
    assert outcome == EXIT_FAILED and any("not a model reference" in s for s in said)


def test_init_stops_without_a_terminal_and_skips_the_wizard_when_seeds_exist(platform):
    original = "LITELLM_MASTER_KEY=sk-test\n"
    outcome, said = init(platform, original)
    assert outcome == EXIT_REVIEW and (platform.root / ".env").read_text() == original
    assert any("local-ezai init --yes" in s for s in said) and StubPipeline.made == []
    seeds = (f"{RUNTIME_KEY}=llamacpp\nREASONING_MODEL=gguf:{platform.root}/w/reasoner.gguf\n"
             f"CODING_MODEL=gguf:{platform.root}/w/coder.gguf\n"
             f"CHAT_MODEL=gguf:{platform.root}/w/chatter.gguf\n")
    outcome, said = init(platform, seeds)
    assert isinstance(outcome, sp.SetupReport) and len(StubPipeline.made) == 1
    assert not any("model set (recommended" in s for s in said)
    assert (platform.root / ".env").read_text() == seeds   # untouched
    bad = seeds.replace("llamacpp", "vllm")
    outcome, said = init(platform, bad)
    assert outcome == EXIT_FAILED and any("serves hf" in s for s in said)
    (platform.root / ".env").unlink()
    with pytest.raises(SetupError, match="run ./install.sh first"):
        run_init(platform.ctx, InitOptions(root=platform.root, interactive=False))


def test_init_on_a_bootstrapped_platform_goes_straight_to_setup(platform):
    run, *_ = pipeline(platform, skip_smoke=True, skip_banner=True)
    assert run.run().exit_code == EXIT_OK
    outcome, said = init(platform, "LITELLM_MASTER_KEY=sk-test\n")
    assert isinstance(outcome, sp.SetupReport)
    assert any("bootstrapped already" in s for s in said)


# ── CLI verbs ────────────────────────────────────────────────────────────────


def test_cli_setup_and_init_verbs(platform, monkeypatch, capsys):
    monkeypatch.setattr(sp, "SetupPipeline", StubPipeline)
    StubPipeline.made.clear()
    code = main(["setup", "--profile", "n97", "--skip-images", "--skip-banner", "--json",
                 "--config", str(platform.cfg)])
    data = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK and data["ok"] and data["models"] == {"chat": "stub"}
    [made] = StubPipeline.made
    assert made.options.profile == "n97" and made.options.skip_images and made.options.skip_banner
    assert not made.options.skip_smoke
    code = main(["setup", "--config", str(platform.cfg)])
    out = capsys.readouterr().out
    assert code == EXIT_OK and "✔ Platform ready" in out
    monkeypatch.setattr(sp, "run_init", lambda ctx, options, **kw: EXIT_REVIEW)
    assert main(["init", "--config", str(platform.cfg)]) == EXIT_REVIEW
    monkeypatch.setattr(sp, "run_init", lambda ctx, options, **kw: (_ for _ in ()).throw(
        SetupError("no .env — run ./install.sh first")))
    assert main(["init", "--yes", "--config", str(platform.cfg)]) == 2
    assert "run ./install.sh first" in capsys.readouterr().out
    assert "setup" in platform_cli.HOST_ONLY_COMMANDS and "init" in platform_cli.HOST_ONLY_COMMANDS


# ── wiring: Makefile, compose, scripts, baseline, the persona id ─────────────


def test_makefile_compose_scripts_and_baseline_wiring():
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    recipe = makefile.split("\nsetup:", 1)[1].split("\n\n", 1)[0]
    assert "bash install.sh" in recipe and "$(EZAI_SETUP)" in recipe
    assert "\nsetup-system:" in makefile and "scripts/setup.sh" in makefile
    for profile in ("gpu", "cpu", "n97"):
        block = makefile.split(f"\nsetup-{profile}:", 1)[1].split("\n\n", 1)[0]
        assert f"--profile {profile}" in block and "install.sh" in block
    for target in ("up-run", "up-cpu-run", "up-n97-run"):
        block = makefile.split(f"\n{target}:", 1)[1].split("\n\n", 1)[0]
        assert "scripts/download-embed.sh" in block and "download-gpu" not in block
        assert "GPU_MODEL_DIR" not in block and "CHAT_MODEL_DIR" not in block
    assert "\ndownload-embed:" in makefile
    assert "scripts/embed-documents.sh" in makefile.split("\nembed:", 1)[1].split("\n\n", 1)[0]
    for script in ("download-embed.sh", "embed-documents.sh"):
        path = REPO_ROOT / "scripts" / script
        assert path.is_file() and path.stat().st_mode & 0o111, script
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["openwebui"]["env_file"] == [
        {"path": "config/first-run/openwebui.env", "required": False}]
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "config/first-run/" in ignored and "sample-project/" in ignored
    assert (REPO_ROOT / "examples" / "sample-project" / ".agentd.yaml").is_file()
    # the chat-stack baseline treats the optional env_file as additive (fixture unchanged)
    loader = importlib.util.spec_from_file_location(
        "chat_stack_baseline_pr22", REPO_ROOT / "scripts" / "chat-stack-baseline.py")
    tool = importlib.util.module_from_spec(loader)
    assert loader.loader is not None
    loader.loader.exec_module(tool)
    assert tool.snapshot() == tool.load_baseline()
    assert "env_file" not in tool.snapshot()["services"]["openwebui"]


def test_orchestrator_model_id_matches_the_persona_installer():
    loader = importlib.util.spec_from_file_location(
        "openwebui_orchestrator_pr22", REPO_ROOT / "scripts" / "openwebui_orchestrator.py")
    module = importlib.util.module_from_spec(loader)
    assert loader.loader is not None
    loader.loader.exec_module(module)
    assert module.MODEL_ID == ORCHESTRATOR_MODEL_ID


def test_the_bundled_sample_project_validates_with_its_own_command():
    project = REPO_ROOT / "examples" / "sample-project"
    command = yaml.safe_load((project / ".agentd.yaml").read_text())["validation"]["commands"]
    proc = subprocess.run(command["test"][0], shell=True, cwd=project, capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    source = (project / "app.py").read_text(encoding="utf-8")
    assert '"/health"' not in source and "'/health'" not in source   # the route the first run plans
