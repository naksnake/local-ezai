"""The first-run acceptance criteria, scripted (PR-23, ADR-031 Accepted;
V1_IMPLEMENTATION_PLAN §P5 exit criteria).

F1–F6 are docs/FIRST_RUN_EXPERIENCE.md §6, F7–F11 docs/FINAL_FIRST_RUN_EXPERIENCE.md
§6. Offline, like the rest of the suite: Docker is a recording fake, HTTP is
scripted, the planner and the evaluator are stubs, downloads are pretend
fetchers; the bootstrap, the registry and its generations, the renderer, git
and the `.env` editing are real. F1's "≤ 30 minutes" is a wall-clock target
measured on a host (the P6 soak runbook); here the platform's own work is
timed with the downloads excluded. Model names are fixtures throughout."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from agentd import platform_cli
from agentd.bootstrap import CONSUMED_KEY, RUNTIME_KEY, parse_env
from agentd.bundle import IMAGES_TAR, MANIFEST, WEIGHTS_PLACEHOLDER, create_bundle
from agentd.capability import CapabilityVector
from agentd.catalog import recommend
from agentd.config import load_config
from agentd.fetch import Fetched, FetchError, GGUFFetcher, gguf_filename
from agentd.installer import EXIT_OK as INSTALL_OK
from agentd.installer import EXIT_PROBLEMS as INSTALL_PROBLEMS
from agentd.installer import Options as InstallOptions
from agentd.installer import choose_runtime, run_install
from agentd.lifecycle import OFFLINE_KEY, offline_fetcher
from agentd.platform_cli import build_context
from agentd.registry_v2 import list_generations, load_registry, reference_registry, registry_path
from agentd.render import MANIFEST_FILENAME, rendered_dir
from agentd.setup_pipeline import (
    EXIT_FAILED,
    EXIT_OK,
    InitOptions,
    SetupOptions,
    SetupPipeline,
    recommended_set,
    run_init,
)
from tests.unit import test_setup_pipeline as pipeline_tests
from tests.unit.test_setup_pipeline import (
    CPU_LOW,
    Clock,
    FakeDocker,
    FakeHttp,
    fake_evaluate,
    fake_plan,
    steps,
)

#: The PR-22 harness: a checkout-like platform with the bootstrap's seams faked.
platform = pipeline_tests.platform

REPO_ROOT = Path(__file__).resolve().parents[3]
ACCEL = CapabilityVector(accelerator="cuda", accel_memory_gb=24, system_memory_gb=64,
                         cpu_cores=16, cpu_flags=["avx2"])
IMAGES = "ghcr.io/example/router:1\nghcr.io/example/engine:2\nlocal/monitor:dev\n"


# ── harness ──────────────────────────────────────────────────────────────────


class PretendFetcherFactory:
    """Writes a tiny artifact where the real fetcher would download — no
    network. Local GGUF seeds are imported like the real fetcher does."""

    def __init__(self, fail_for: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_for = fail_for

    def __call__(self, fmt: str, directory: Path):
        factory = self

        class _Fetcher:
            def fetch(self, ref: str, *, expected_sha256: str = "", refetch: bool = False):
                factory.calls.append((fmt, ref))
                if factory.fail_for and factory.fail_for in ref:
                    raise FetchError(f"pretend outage while fetching {ref}")
                directory.mkdir(parents=True, exist_ok=True)
                if fmt == "gguf":
                    if "://" not in ref and Path(ref).expanduser().is_file():
                        return GGUFFetcher(directory).fetch(ref, expected_sha256=expected_sha256,
                                                            refetch=refetch)
                    final = directory / gguf_filename(ref)
                    final.write_bytes(b"g" * 4096)
                    return Fetched(str(final), 4096, expected_sha256 or "pretend")
                repo_dir = directory / f"models--{ref.replace('/', '--')}"
                snapshot = repo_dir / "snapshots" / "abc"
                snapshot.mkdir(parents=True, exist_ok=True)
                (snapshot / "model.safetensors").write_bytes(b"w" * 2048)
                return Fetched(str(repo_dir), 2048)

        return _Fetcher()


class BundleDocker(FakeDocker):
    """`docker save` writes the tarball. With ``network=False`` anything that
    would reach the network (pull, build, `docker run`) is refused — the
    air-gapped host."""

    def __init__(self, *, network: bool, **kwargs) -> None:
        super().__init__(**kwargs)
        self.network = network

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        if command[0] == "docker" and "save" in command:
            Path(command[command.index("-o") + 1]).write_bytes(b"images")
            self.calls.append(list(command))
            return subprocess.CompletedProcess(command, 0, "", "")
        if not self.network and command[0] == "docker" and (
                "pull" in command or "build" in command or command[1:2] == ["run"]):
            self.calls.append(list(command))
            return subprocess.CompletedProcess(command, 1, "", "network unreachable (air-gapped)")
        return super().__call__(command)

    def network_attempts(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "docker"
                and ("pull" in c or "build" in c or c[1:2] == ["run"])]


def context_for(platform, vector: CapabilityVector, monkeypatch, root: Path | None = None):
    monkeypatch.setattr(platform_cli, "detect_vector", lambda: vector)
    config = load_config(platform.cfg)
    if root is not None:
        config.platform.config_dir = root / "config"
    return build_context(config, root or platform.root, actor="nita")


def pipeline(ctx, root: Path, *, docker=None, http=None, fetchers=None, environ=None,
             **options):
    clock = Clock()
    said: list[str] = []
    docker = docker or FakeDocker(stdout={"config --images": IMAGES})
    http = http or FakeHttp()
    run = SetupPipeline(ctx, SetupOptions(root=root, **options), runner=docker, http=http,
                        plan_fn=fake_plan, evaluate_fn=fake_evaluate, fetcher_factory=fetchers,
                        sleep=clock.sleep, clock=clock.now, environ=environ or {},
                        now="2026-09-09T12:00:00", say=said.append)
    return run, docker, http, said


def seeds_text(root: Path) -> str:
    return (f"{RUNTIME_KEY}=llamacpp\nREASONING_MODEL=gguf:{root}/w/reasoner.gguf\n"
            f"CODING_MODEL=gguf:{root}/w/coder.gguf\nCHAT_MODEL=gguf:{root}/w/chatter.gguf\n"
            "LITELLM_MASTER_KEY=sk-test\n")


def ensure_example(root: Path) -> None:
    if not (root / ".env.example").is_file():
        shutil.copy(REPO_ROOT / ".env.example", root / ".env.example")


def install(root: Path, vector: CapabilityVector = CPU_LOW, **kwargs):
    editor_fn = kwargs.pop("editor_fn", None)
    runner = kwargs.pop("runner", None)
    options = InstallOptions(root=root, interactive=kwargs.pop("interactive", False), **kwargs)
    extra = {"editor_fn": editor_fn} if editor_fn else {}
    return run_install(options, vector_fn=lambda: vector, now="2026-09-09T12:00:00",
                       say=lambda text: None, runner=runner, **extra)


def assert_never_half_configured(root: Path) -> None:
    """F4's invariant: either no generation at all, or exactly one, rendered
    and stamped — never a registry without its artifacts or the reverse."""
    config = root / "config"
    env_text = (root / ".env").read_text(encoding="utf-8") if (root / ".env").is_file() else ""
    if not registry_path(config).is_file():
        assert not (rendered_dir(config) / MANIFEST_FILENAME).exists()
        assert CONSUMED_KEY not in env_text
        return
    generations = list_generations(config)
    assert len(generations) == 1
    rendered = yaml.safe_load((rendered_dir(config) / MANIFEST_FILENAME).read_text())
    assert rendered["generation"] == load_registry(config).generation == 1
    assert f"{CONSUMED_KEY}=1@" in env_text


# ── F1 · fresh accelerator host → smoke green ────────────────────────────────


def test_f1_fresh_accelerator_host_reaches_smoke_green(platform, monkeypatch):
    ctx = context_for(platform, ACCEL, monkeypatch)
    assert ctx.platform.klass == "accel-large" and ctx.platform.accel == "cuda"
    started = time.monotonic()
    run, docker, http, _ = pipeline(ctx, platform.root)
    report = run.run()
    assert report.ready and report.exit_code == EXIT_OK, report.lines()
    assert report.profile == "gpu" and all(s.status == "ok" for s in report.steps)
    assert [(c.name, c.ok) for c in report.smoke] == [("chat", True), ("RAG", True),
                                                       ("swe_plan", True), ("models", True)]
    # the wall-clock target excludes downloads — these are the only network-shaped steps
    network = [c for c in docker.calls
               if "pull" in c or "build" in c or c[-1].endswith("download-embed.sh")]
    assert len(network) == 3
    assert time.monotonic() - started < 60  # the platform's own work is seconds, not minutes


# ── F2 · exactly one file hand-edited, once ──────────────────────────────────


def test_f2_exactly_one_file_is_hand_edited_once(platform):
    (platform.root / ".env").unlink()
    ensure_example(platform.root)
    edits: list[Path] = []

    def editor(path: Path, command: list[str]) -> None:
        edits.append(path)
        path.write_text(path.read_text(encoding="utf-8") + seeds_text(platform.root),
                        encoding="utf-8")

    before = {p.relative_to(platform.root): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in platform.root.rglob("*") if p.is_file()}
    report = install(platform.root, interactive=True, editor="fake", editor_fn=editor)
    assert report.exit_code == INSTALL_OK and report.editor_opened
    assert edits == [platform.root / ".env"]   # the one edit, once
    after = {p.relative_to(platform.root): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in platform.root.rglob("*") if p.is_file()}
    assert {p for p in after if before.get(p) != after[p]} == {Path(".env")}
    # everything else the platform writes itself; no further prompt ever appears
    run, *_ = pipeline(platform.ctx, platform.root)
    assert run.run().ready


# ── F3 · the low-power class completes the same wizard, smaller set ──────────


def test_f3_low_power_profile_completes_the_wizard_with_a_smaller_set(platform, monkeypatch):
    roles = reference_registry().roles

    def first_eligible(ctx, runtime):
        picks = {}
        for group in ("reasoning", "coding", "chat"):
            contracts = [s.requires for s in roles.values() if s.group == group]
            ranked = recommend(ctx.catalog, group, ctx.vector, ctx.descriptors, runtime=runtime,
                               contracts=contracts, capability_class=ctx.platform.klass,
                               accelerator=ctx.platform.accel)
            picks[group] = next(r for r in ranked if r.eligible)
        return picks

    small_runtime = choose_runtime(platform.ctx.descriptors, "cpu-low", "none")[0]
    small = first_eligible(platform.ctx, small_runtime)
    large_ctx = context_for(platform, ACCEL, monkeypatch)
    large = first_eligible(large_ctx, choose_runtime(large_ctx.descriptors, "accel-large",
                                                     "cuda")[0])
    for group in small:
        assert small[group].model.size_gb <= large[group].model.size_gb, group
    assert sum(r.model.size_gb for r in small.values()) < sum(
        r.model.size_gb for r in large.values())
    # the wizard on the low-power host: recommended set → seeds → the pipeline
    (platform.root / ".env").write_text("LITELLM_MASTER_KEY=sk-test\n", encoding="utf-8")
    clock = Clock()
    outcome = run_init(platform.ctx, InitOptions(root=platform.root, assume_yes=True,
                                                 interactive=False),
                       say=lambda text: None, runner=FakeDocker(stdout={"config --images": IMAGES}),
                       http=FakeHttp(), plan_fn=fake_plan, evaluate_fn=fake_evaluate,
                       fetcher_factory=PretendFetcherFactory(), sleep=clock.sleep,
                       clock=clock.now, environ={}, now="2026-09-09T12:00:00")
    assert outcome.ready and outcome.exit_code == EXIT_OK
    env = parse_env((platform.root / ".env").read_text(encoding="utf-8"))
    assert {env["REASONING_MODEL"], env["CODING_MODEL"], env["CHAT_MODEL"]} == {
        r.catalog_id for r in small.values()}
    assert set(outcome.models.values()) == {r.catalog_id for r in small.values()}


# ── F4 · abort at any step → resumable, never half-configured ────────────────


@pytest.mark.parametrize("abort", ["fetch", "pull", "build", "up", "engine", "smoke"])
def test_f4_abort_at_any_step_is_resumable_and_never_half_configured(platform, abort):
    fail = {"pull": {"pull": "registry unreachable"}, "build": {"build": "no network"},
            "up": {"up -d": "daemon gone"}}.get(abort, {})
    http = FakeHttp(engine_after=10_000) if abort == "engine" else \
        FakeHttp(answers=["HTTP500"]) if abort == "smoke" else FakeHttp()
    fetchers = PretendFetcherFactory(fail_for="coder") if abort == "fetch" else None
    run, docker, _, _ = pipeline(platform.ctx, platform.root,
                                 docker=FakeDocker(fail=fail, stdout={"config --images": IMAGES}),
                                 http=http, fetchers=fetchers)
    report = run.run()
    assert report.exit_code == EXIT_FAILED and not report.ready
    failed = [s for s in report.steps if s.status == "failed"]
    assert len(failed) == 1
    assert_never_half_configured(platform.root)
    if abort == "fetch":
        assert not registry_path(platform.root / "config").is_file()
        assert "pretend outage" in failed[0].detail
    else:
        assert load_registry(platform.root / "config").generation == 1
    # the abort is recoverable: the same command again finishes the job
    again, *_ = pipeline(platform.ctx, platform.root)
    report = again.run()
    assert report.ready and report.exit_code == EXIT_OK, report.lines()
    assert len(list_generations(platform.root / "config")) == 1  # never a second generation 1
    assert_never_half_configured(platform.root)


# ── F5 · re-run on an installed host: repair, never wipe ─────────────────────


def test_f5_rerun_detects_the_install_repairs_and_never_wipes_state(platform):
    ensure_example(platform.root)
    run, *_ = pipeline(platform.ctx, platform.root)
    assert run.run().ready
    registry_before = (platform.root / "config" / "models" / "registry.yaml").read_bytes()
    env_before = parse_env((platform.root / ".env").read_text(encoding="utf-8"))
    report = install(platform.root)
    assert report.exit_code == INSTALL_OK and not report.created
    assert report.consumed and report.problems == []   # seeds are history, not re-validated
    env_after = parse_env((platform.root / ".env").read_text(encoding="utf-8"))
    for key in ("REASONING_MODEL", "CODING_MODEL", "CHAT_MODEL", RUNTIME_KEY, CONSUMED_KEY,
                "LITELLM_MASTER_KEY"):
        assert env_after[key] == env_before[key], key
    assert (platform.root / "config" / "models" / "registry.yaml").read_bytes() == registry_before
    assert len(list_generations(platform.root / "config")) == 1
    assert len(list(platform.root.glob(".env.bak.*"))) == 1 and report.backup
    again, *_ = pipeline(platform.ctx, platform.root)
    second = again.run()
    assert second.ready and steps(second)["bootstrap"] == "skipped"
    assert install(platform.root).changed is False   # repair is idempotent


# ── F6 · offline bundle: the same wizard, no egress ──────────────────────────


def test_f6_offline_bundle_runs_the_same_wizard_with_no_egress(platform, monkeypatch, tmp_path):
    # the connected host: first run, then the bundle
    connected = BundleDocker(network=True, stdout={"config --images": IMAGES})
    run, *_ = pipeline(platform.ctx, platform.root, docker=connected)
    assert run.run().ready
    bundle = tmp_path / "bundle"
    manifest = create_bundle(platform.ctx, bundle, profile="n97", runner=connected, environ={},
                             now="2026-09-09T12:00:00")
    assert manifest.images == IMAGES.split() and (bundle / IMAGES_TAR).is_file()
    assert (bundle / MANIFEST).is_file() and manifest.generation == 1
    assert sorted(g["file"] for g in manifest.gguf) == ["chatter.gguf", "coder.gguf",
                                                        "reasoner.gguf"]
    assert all(seed.startswith(f"gguf:{WEIGHTS_PLACEHOLDER}/") for seed in manifest.seeds.values())
    for item in manifest.gguf:
        copy = bundle / "weights" / "gguf" / item["file"]
        assert hashlib.sha256(copy.read_bytes()).hexdigest() == item["sha256"]

    # the air-gapped host: a fresh clone, no .env, no network
    target = tmp_path / "airgapped"
    (target / "config").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "config" / "providers", target / "config" / "providers")
    shutil.copytree(REPO_ROOT / "examples", target / "examples")
    (target / "scripts").mkdir()
    (target / "docker-compose.yml").write_text("services: {}\n")
    ensure_example(target)
    airgapped = BundleDocker(network=False, stdout={"config --images": IMAGES})
    report = install(target, offline_bundle=bundle, runner=airgapped)
    assert report.exit_code == INSTALL_OK, report.problems
    assert report.created and report.bundle.startswith("consumed") and not report.editor_opened
    assert ["docker", "load", "-i", str(bundle / IMAGES_TAR)] in airgapped.calls
    env = parse_env((target / ".env").read_text(encoding="utf-8"))
    assert env[OFFLINE_KEY] == "1" and env[RUNTIME_KEY] == "llamacpp"
    assert env["REASONING_MODEL"] == f"gguf:{target}/models/gguf/reasoner.gguf"
    assert (target / "models" / "gguf" / "coder.gguf").is_file()
    assert env["LITELLM_MASTER_KEY"] != parse_env((REPO_ROOT / ".env.example").read_text())[
        "LITELLM_MASTER_KEY"]

    ctx = context_for(platform, CPU_LOW, monkeypatch, root=target)
    run, docker, _, said = pipeline(ctx, target, docker=airgapped)
    report = run.run()
    assert report.ready and report.exit_code == EXIT_OK and report.offline, report.lines()
    assert steps(report)["images"] == "skipped" and "offline" in report.steps[1].detail
    assert report.steps[0].detail.endswith("(offline: nothing downloaded)")
    assert "offline (no egress)" in said[0]
    assert airgapped.network_attempts() == []   # nothing ever tried to leave the host
    assert load_registry(target / "config").generation == 1
    assert set(report.models.values()) == {"reasoner", "coder", "chatter"}
    # the fetchers refuse the network in offline mode, naming the fix
    with pytest.raises(FetchError, match=f"offline mode \\({OFFLINE_KEY}=1\\)"):
        offline_fetcher("gguf", tmp_path / "w").fetch("https://example.invalid/m.gguf")
    with pytest.raises(FetchError, match="offline mode"):
        offline_fetcher("hf", tmp_path / "cache").fetch("org/repo")


# ── F7 · zero interactive prompts when .env is fully specified ───────────────


def test_f7_fully_specified_env_completes_with_zero_prompts(platform):
    ensure_example(platform.root)

    def never(*args, **kwargs):
        raise AssertionError("a prompt appeared")

    report = install(platform.root, interactive=True, editor="fake", editor_fn=never)
    assert report.exit_code == INSTALL_OK and not report.editor_opened
    clock = Clock()
    outcome = run_init(platform.ctx, InitOptions(root=platform.root, interactive=True),
                       ask=never, say=lambda text: None,
                       runner=FakeDocker(stdout={"config --images": IMAGES}), http=FakeHttp(),
                       plan_fn=fake_plan, evaluate_fn=fake_evaluate, sleep=clock.sleep,
                       clock=clock.now, environ={}, now="2026-09-09T12:00:00")
    assert outcome.ready and outcome.exit_code == EXIT_OK


# ── F8 · invalid seeds rejected at step 2 with the fix, before any download ──


def test_f8_invalid_seeds_are_rejected_with_the_fix_before_any_download(platform):
    ensure_example(platform.root)
    (platform.root / ".env").write_text(
        f"{RUNTIME_KEY}=vllm\nREASONING_MODEL=gguf:{platform.root}/w/reasoner.gguf\n"
        f"CODING_MODEL=hf:org/a\nCHAT_MODEL=hf:org/a\nLITELLM_MASTER_KEY=sk-test\n",
        encoding="utf-8")
    report = install(platform.root)
    assert report.exit_code == INSTALL_PROBLEMS
    assert any("REASONING_MODEL is gguf but AI_RUNTIME=vllm serves" in p for p in report.problems)
    assert all("—" in p for p in report.problems)   # every problem carries its fix
    fetchers = PretendFetcherFactory()
    run, docker, http, _ = pipeline(platform.ctx, platform.root, fetchers=fetchers)
    assert run.run().exit_code == EXIT_FAILED
    assert fetchers.calls == [] and docker.calls == [] and http.posts == []
    assert not (platform.root / "config" / "models").exists()


# ── F9 · `auto` → a class-appropriate choice with the verdict shown ──────────


def test_f9_auto_seeds_pick_class_appropriate_models_with_the_verdict_shown(platform):
    (platform.root / ".env").write_text(
        f"{RUNTIME_KEY}=llamacpp\nREASONING_MODEL=auto\nCODING_MODEL=auto\nCHAT_MODEL=auto\n"
        "LITELLM_MASTER_KEY=sk-test\n", encoding="utf-8")
    proposals = recommended_set(platform.ctx, "llamacpp")
    for proposal in proposals:
        assert proposal.ref and "eligible: placement system" in proposal.explanation
        assert "GB" in proposal.explanation
    run, *_ = pipeline(platform.ctx, platform.root, fetchers=PretendFetcherFactory())
    report = run.run()
    assert report.ready, report.lines()
    registry = load_registry(platform.root / "config")
    assert set(registry.models) == {p.ref for p in proposals}
    for proposal in proposals:
        assert registry.groups[proposal.group] == [proposal.ref]


# ── F10 · the generation-1 diff shows exactly the seeds ──────────────────────


def test_f10_generation_one_diff_equals_the_env_seeds(platform):
    run, *_ = pipeline(platform.ctx, platform.root)
    assert run.run().ready
    env = parse_env((platform.root / ".env").read_text(encoding="utf-8"))
    [request] = [r for r in platform.ctx.queue.list() if r.kind == "bootstrap"]
    assert request.evidence["seeds"] == {"reasoning": env["REASONING_MODEL"],
                                         "coding": env["CODING_MODEL"],
                                         "chat": env["CHAT_MODEL"]}
    assert request.evidence["migrated_from"] is None
    mentioned = {name for name in ("reasoner", "coder", "chatter")
                 if any(name in line for line in request.diff)}
    assert mentioned == {"reasoner", "coder", "chatter"}
    assert not any("alpha" in line or "beta" in line for line in request.diff)


# ── F11 · a legacy .env migrates to generation 1 without user action ─────────


def test_f11_legacy_env_migrates_to_generation_one_without_user_action(platform, monkeypatch):
    ensure_example(platform.root)
    (platform.root / ".env").write_text(
        "CHAT_MODEL=Org/Chat-7B-Instruct\nCHAT_MODEL_NAME=chat-7b\nMAX_MODEL_LEN=4096\n"
        "LITELLM_MASTER_KEY=sk-test\n", encoding="utf-8")
    report = install(platform.root, ACCEL)
    assert report.exit_code == INSTALL_OK and report.migrated_from == "accelerator-hub"
    assert report.runtime == choose_runtime(platform.ctx.descriptors, "accel-large", "cuda")[0]
    ctx = context_for(platform, ACCEL, monkeypatch)
    run, *_ = pipeline(ctx, platform.root, fetchers=PretendFetcherFactory())
    outcome = run.run()
    assert outcome.ready, outcome.lines()
    registry = load_registry(platform.root / "config")
    assert set(registry.models) == {"chat-7b"}   # the served name is kept
    assert all(registry.groups[g] == ["chat-7b"] for g in ("reasoning", "coding", "chat"))
    assert outcome.models == {"reasoning": "chat-7b", "coding": "chat-7b", "chat": "chat-7b"}
    assert f"{CONSUMED_KEY}=1@" in (platform.root / ".env").read_text(encoding="utf-8")


# ── wiring ───────────────────────────────────────────────────────────────────


def test_wiring_make_targets_install_flag_and_the_suite_location():
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    for target in ("bundle:", "setup-offline:", "swe-accept:"):
        assert f"\n{target}" in makefile, target
    assert "bundle create $(BUNDLE)" in makefile and "--offline $(BUNDLE)" in makefile
    assert "tests/acceptance" in makefile
    script = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
    assert "--offline" in script and "docker load" not in script  # the module loads, not the shell
    assert (REPO_ROOT / "agentd" / "tests" / "acceptance" / "__init__.py").is_file()
