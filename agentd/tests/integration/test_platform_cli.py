"""local-ezai platform namespaces (PR-6): model · governance · project ·
status · up/down against a throwaway platform directory, in direct mode.
Docker/HTTP edges are replaced through the module's seams; the scripted
LLM config is the same a user would supply."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from agentd import platform_cli
from agentd.main_cli import main
from agentd.registry_v2 import load_registry, save_generation
from agentd.render import write_rendered
from agentd.runtime_descriptor import load_descriptors
from tests.unit.test_activation import seed_registry
from tests.unit.test_lifecycle import FakeEngineHTTP, FakeRunner

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def platform_root(tmp_path: Path) -> Path:
    """A bootstrapped platform: descriptors, generation 1, rendered."""
    root = tmp_path / "platform"
    (root / "config").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "config" / "providers", root / "config" / "providers")
    (root / "docker-compose.yml").write_text("services: {}\n")
    (root / "docker-compose.n97.yml").write_text("services: {}\n")
    saved = save_generation(seed_registry(), root / "config", note="bootstrap")
    from agentd.activation import Platform
    from agentd.capability import CapabilityVector

    platform = Platform(config_dir=root / "config", platform_root=root,
                        descriptors=load_descriptors(root / "config"),
                        vector=CapabilityVector(system_memory_gb=15.5, cpu_cores=4),
                        capability_class="cpu-low", accelerator="none")
    write_rendered(platform.render(saved), platform.rendered)
    return root


@pytest.fixture
def cli(tmp_path, monkeypatch, platform_root):
    """invoke(*argv) → (exit_code, stdout); docker + HTTP seams faked."""
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    runner = FakeRunner()
    monkeypatch.setattr(platform_cli, "default_runner", runner)
    monkeypatch.setattr(platform_cli, "http_probe", lambda url: 200)
    monkeypatch.setattr(platform_cli, "engine_http", lambda: FakeEngineHTTP(
        timings={"predicted_per_second": 15.5, "prompt_per_second": 80.0, "predicted_n": 100}))
    monkeypatch.setattr(platform_cli, "build_validator",
                        lambda ctx: lambda descriptor, name, entry: platform_cli.lifecycle
                        .ProbeResult(True, "", 0.2, name))
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "llm": {"provider": "scripted", "script_path": str(tmp_path / "script.json")},
        "platform": {"config_dir": str(platform_root / "config")},
        "runs_dir": str(tmp_path / "runs"),
    }), encoding="utf-8")
    (tmp_path / "script.json").write_text("[]")

    def invoke(*argv, capsys):
        code = main([*argv, "--config", str(cfg_file), "--by", "nita"]
                    if argv and argv[0] in ("model", "governance") and argv[1] in
                    ("activate", "upgrade", "rollback", "approve", "reject")
                    else [*argv, "--config", str(cfg_file)])
        return code, capsys.readouterr().out

    invoke.runner = runner  # type: ignore[attr-defined]
    return invoke


# ── status / explain / catalog / history ─────────────────────────────────────


def test_status_reports_generation_models_and_health(cli, platform_root, capsys):
    code, out = cli("status", capsys=capsys)
    assert code == 0
    assert f"platform:   {platform_root}" in out
    assert "generation: 1 — bootstrap · rendered 1" in out
    assert "runtime:    llamacpp" in out and "engine up · router up" in out
    assert "approvals:  0 pending" in out
    assert "alpha" in out and "active" in out and "beta" in out and "benchmarked" in out
    code, out = cli("status", "--json", capsys=capsys)
    data = json.loads(out)
    assert data["generation"] == 1 and data["models"]["beta"]["tokens_per_s"] == 12.0


def test_status_without_platform_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    monkeypatch.delenv(platform_cli.PLATFORM_ENV, raising=False)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({"llm": {"provider": "scripted"}}))
    plain = tmp_path / "plain"
    plain.mkdir()
    assert main([str(plain), "status", "--config", str(cfg)]) == 2


def test_model_explain_cites_the_contract(cli, capsys):
    code, out = cli("model", "explain", "coder", capsys=capsys)
    assert code == 0
    assert "role coder — generation 1" in out and "primary:  alpha" in out
    assert "contract: alpha [ok]" in out and "tool_calling" in out
    code, out = cli("model", "explain", "nope", capsys=capsys)
    assert code == 1


def test_model_catalog_lists_and_recommends(cli, capsys):
    code, out = cli("model", "catalog", capsys=capsys)
    assert code == 0 and "catalog:" in out and "qwen2.5-coder-1.5b-instruct" in out
    code, out = cli("model", "catalog", "--group", "coding", "--json", capsys=capsys)
    data = json.loads(out)
    assert data["class"] == "cpu-low" and data["candidates"][0]["eligible"] is True
    assert all("verdict" in c for c in data["candidates"])


def test_model_history_shows_generations(cli, capsys):
    code, out = cli("model", "history", capsys=capsys)
    assert code == 0 and "generation 1" in out and "bootstrap" in out


# ── install / benchmark (docker + HTTP seams faked) ──────────────────────────


def test_model_install_local_gguf_then_benchmark(cli, platform_root, tmp_path, capsys):
    weights = tmp_path / "gamma-q4.gguf"
    weights.write_bytes(b"g" * 4096)
    code, out = cli("model", "install", f"gguf:{weights}", "--name", "gamma-q4", capsys=capsys)
    assert code == 0 and "install gamma-q4: installed" in out
    registry = load_registry(platform_root / "config")
    assert registry.generation == 2 and registry.models["gamma-q4"].state == "installed"
    assert Path(registry.models["gamma-q4"].artifact).parent == platform_root / "models" / "gguf"

    code, out = cli("model", "benchmark", "gamma-q4", capsys=capsys)
    assert code == 0 and "benchmark gamma-q4: 15.5 tok/s (side-load" in out
    registry = load_registry(platform_root / "config")
    assert registry.models["gamma-q4"].state == "benchmarked"
    assert registry.models["gamma-q4"].benchmarks["tokens_per_s"] == 15.5
    assert (platform_root / ".agent" / "model_benchmarks.json").is_file()


def test_model_install_rejects_bad_reference(cli, capsys):
    code, out = cli("model", "install", "s3:nowhere", capsys=capsys)
    assert code == 1


# ── activate → governance → apply → rollback ─────────────────────────────────


def test_activation_flow_through_the_queue(cli, platform_root, capsys):
    code, out = cli("model", "activate", "beta", "--group", "coding", capsys=capsys)
    assert code == 0
    assert "change request cr-0001: activate beta" in out
    assert "role coder: alpha → beta (fallbacks alpha)" in out
    assert "awaiting human approval: local-ezai governance approve cr-0001" in out
    assert load_registry(platform_root / "config").generation == 1  # nothing applied yet

    code, out = cli("governance", "list", capsys=capsys)
    assert "cr-0001  pending" in out
    code, out = cli("governance", "show", "cr-0001", capsys=capsys)
    assert "evidence: beta 12.0 tok/s" in out

    code, out = cli("governance", "approve", "cr-0001", "--reason", "looks good", capsys=capsys)
    assert code == 0 and "approved cr-0001 by nita" in out
    assert "applied as generation 2" in out and "rendered only" in out
    after = load_registry(platform_root / "config")
    assert after.generation == 2 and after.resolve("coder").primary == "beta"

    code, out = cli("governance", "approve", "cr-0001", capsys=capsys)
    assert code == 1  # decisions are made once

    code, out = cli("model", "rollback", "--reason", "too slow", capsys=capsys)
    assert code == 0 and "ROLLBACK by nita" in out
    assert load_registry(platform_root / "config").resolve("coder").primary == "alpha"
    code, out = cli("model", "history", "--json", capsys=capsys)
    assert [g["generation"] for g in json.loads(out)["generations"]] == [1, 2, 3]


def test_policy_approved_activation_applies_immediately(cli, platform_root, capsys):
    code, out = cli("model", "activate", "beta", "--group", "spare", capsys=capsys)
    assert code == 0 and "approved by policy" in out and "applied as generation 2" in out
    assert load_registry(platform_root / "config").models["beta"].state == "active"


def test_reject_requires_reason_and_records_it(cli, capsys):
    cli("model", "activate", "beta", "--group", "coding", capsys=capsys)
    code, out = cli("governance", "reject", "cr-0001", capsys=capsys)
    assert code == 1  # a rejection needs a reason
    code, out = cli("governance", "reject", "cr-0001", "--reason", "regression", capsys=capsys)
    assert code == 0 and "rejected cr-0001 by nita: regression" in out
    code, out = cli("governance", "list", "--status", "rejected", capsys=capsys)
    assert "cr-0001  rejected" in out


def test_upgrade_retire_uninstall_flow(cli, platform_root, capsys):
    code, out = cli("model", "upgrade", "alpha", "beta", capsys=capsys)
    assert code == 0 and "upgrade alpha → beta" in out
    code, out = cli("governance", "approve", "cr-0001", capsys=capsys)
    assert code == 0
    registry = load_registry(platform_root / "config")
    assert registry.models["alpha"].state == "retired"
    assert registry.resolve("chat").primary == "beta"
    # the retired old version is a rollback target → uninstall needs --force
    code, out = cli("model", "uninstall", "alpha", capsys=capsys)
    assert code == 1
    code, out = cli("model", "uninstall", "alpha", "--force", capsys=capsys)
    assert code == 0 and "uninstalled alpha" in out
    assert "alpha" not in load_registry(platform_root / "config").models
    # retiring the serving primary is refused with the roles named
    code, out = cli("model", "retire", "beta", capsys=capsys)
    assert code == 1


# ── project allowlist ────────────────────────────────────────────────────────


def test_project_add_list_remove(cli, tmp_repo, platform_root, capsys):
    code, out = cli("project", "add", str(tmp_repo), capsys=capsys)
    assert code == 0 and f"registered project repo → {tmp_repo}" in out
    code, out = cli("project", "list", capsys=capsys)
    assert str(tmp_repo) in out
    data = yaml.safe_load((platform_root / "config" / "projects.yaml").read_text())
    assert data["projects"][0]["name"] == "repo" and data["projects"][0]["added_by"]
    code, out = cli("project", "add", str(tmp_repo), capsys=capsys)
    assert code == 1  # already registered
    code, out = cli("project", "remove", "repo", capsys=capsys)
    assert code == 0
    code, out = cli("project", "list", capsys=capsys)
    assert "(no projects registered" in out


# ── up / down wrappers ───────────────────────────────────────────────────────


def test_up_and_down_wrap_compose_profiles(cli, platform_root, capsys):
    code, out = cli("up", "--profile", "n97", capsys=capsys)
    assert code == 0
    command = cli.runner.calls[-1]
    assert command[:2] == ["docker", "compose"] and command[-2:] == ["up", "-d"]
    assert str(platform_root / "docker-compose.yml") in command
    assert str(platform_root / "docker-compose.n97.yml") in command
    code, out = cli("up", "--rendered", capsys=capsys)  # gpu profile + rendered engine
    command = cli.runner.calls[-1]
    assert str(platform_root / "config" / "rendered" / "docker-compose.engine.yml") in command
    assert str(platform_root / "docker-compose.n97.yml") not in command
    code, out = cli("down", capsys=capsys)
    assert code == 0 and cli.runner.calls[-1][-1] == "down"
    code, out = cli("up", "--profile", "rack42", capsys=capsys)
    assert code == 2


def test_compose_files_profile_chain(platform_root):
    files = platform_cli.compose_files(platform_root, "n97-igpu", rendered=False)
    assert [f.name for f in files] == ["docker-compose.yml", "docker-compose.n97.yml"]
    (platform_root / "docker-compose.n97-igpu.yml").write_text("services: {}\n")
    files = platform_cli.compose_files(platform_root, "n97-igpu", rendered=False)
    assert [f.name for f in files][-1] == "docker-compose.n97-igpu.yml"


# ── the existing `models` command shows the platform layer ───────────────────


def test_models_command_reports_platform_source(cli, platform_root, tmp_repo, capsys):
    code, out = cli(str(tmp_repo), "models", capsys=capsys)
    assert code == 0
    assert "platform: registry generation 1" in out or "platform: rendered role map" in out
    assert "primary:  alpha" in out


def test_repo_work_stays_hermetic_without_platform(tmp_repo, tmp_path, monkeypatch, capsys):
    """The repo verbs never need the platform: a plain config + tmp repo
    resolves aliases only (today's behavior)."""
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    monkeypatch.delenv(platform_cli.PLATFORM_ENV, raising=False)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({"llm": {"provider": "scripted"}}))
    code = main([str(tmp_repo), "models", "--json", "--config", str(cfg)])
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and data["sources"] == []
    assert data["roles"]["planner"]["primary"] == "role-planner"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


# ── bootstrap (PR-7): .env seeds → generation 1 through the CLI ──────────────


@pytest.fixture
def fresh_platform(tmp_path, monkeypatch):
    """A never-bootstrapped platform with V1 seeds in .env; docker/HTTP faked."""
    root = tmp_path / "fresh"
    (root / "config").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "config" / "providers", root / "config" / "providers")
    (root / "docker-compose.yml").write_text("services: {}\n")
    (root / "w").mkdir()
    for name in ("reasoner", "coder", "chatter"):
        (root / "w" / f"{name}.gguf").write_bytes(name.encode() * 256)
    (root / ".env").write_text(
        f"AI_RUNTIME=llamacpp\nREASONING_MODEL=gguf:{root}/w/reasoner.gguf\n"
        f"CODING_MODEL=gguf:{root}/w/coder.gguf\nCHAT_MODEL=gguf:{root}/w/chatter.gguf\n")
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    monkeypatch.setattr(platform_cli, "default_runner", FakeRunner())
    monkeypatch.setattr(platform_cli, "http_probe", lambda url: 200)
    monkeypatch.setattr(platform_cli, "engine_http", lambda: FakeEngineHTTP(
        timings={"predicted_per_second": 7.5, "prompt_per_second": 40.0, "predicted_n": 100}))
    monkeypatch.setattr(platform_cli, "build_validator",
                        lambda ctx: lambda descriptor, name, entry: platform_cli.lifecycle
                        .ProbeResult(True, "", 0.2, name))
    cfg_file = tmp_path / "fresh-cfg.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "llm": {"provider": "scripted"},
        "platform": {"config_dir": str(root / "config")},
        "runs_dir": str(tmp_path / "runs")}), encoding="utf-8")
    return root, cfg_file


def test_bootstrap_cli_dry_run_then_real_run(fresh_platform, capsys):
    root, cfg = fresh_platform
    code = main(["bootstrap", "--dry-run", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 0 and "dry run: seeds valid" in out and "model added: coder" in out
    assert not (root / "config" / "models").exists()

    code = main(["bootstrap", "--config", str(cfg), "--by", "nita"])
    out = capsys.readouterr().out
    assert code == 0 and "generation 1 bootstrapped: reasoning ← reasoner" in out
    assert "seeds stamped as consumed" in out and "next: make up" in out
    registry = load_registry(root / "config")
    assert registry.generation == 1 and registry.resolve("coder").primary == "coder"
    assert registry.models["coder"].benchmarks["tokens_per_s"] == 7.5
    assert (root / "config" / "rendered" / "litellm-config.yaml").is_file()
    assert "EZAI_SEEDS_CONSUMED=1@" in (root / ".env").read_text()

    code = main(["status", "--config", str(cfg)])
    assert code == 0 and "generation: 1 — bootstrap cr-0001" in capsys.readouterr().out
    code = main(["bootstrap", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 1 and "already exists" in out


def test_bootstrap_cli_reports_seed_problems_before_downloading(fresh_platform, capsys):
    root, cfg = fresh_platform
    (root / ".env").write_text("AI_RUNTIME=vllm\nREASONING_MODEL=gguf:https://x.invalid/a.gguf\n"
                               "CODING_MODEL=hf:org/a\nCHAT_MODEL=hf:org/a\n")
    code = main(["bootstrap", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 1 and "REASONING_MODEL is gguf but AI_RUNTIME=vllm serves" in out
    assert not (root / "config" / "models").exists()
    (root / ".env").unlink()
    code = main(["bootstrap", "--config", str(cfg)])
    assert code == 2  # no .env → usage-level error with the fix


# ── the shared error object (PR-9) ───────────────────────────────────────────


def test_refused_verbs_print_the_shared_error_object_in_json_mode(cli, capsys):
    code, out = cli("model", "retire", "nope", "--json", capsys=capsys)
    assert code == 1
    assert json.loads(out) == {"error": {"code": "not_found", "message": "unknown model 'nope'",
                                         "fix": ""}}
    code, out = cli("model", "retire", "alpha", "--json", capsys=capsys)  # serving primary
    assert code == 1 and json.loads(out)["error"]["code"] == "lifecycle_refused"
    code, out = cli("model", "explain", "nope", "--json", capsys=capsys)
    assert code == 1 and json.loads(out)["error"]["code"] == "not_found"
    code, out = cli("model", "retire", "nope", capsys=capsys)  # text mode: logged, exit 1
    assert code == 1 and out == ""


# ── status: control plane health (PR-8) ──────────────────────────────────────


def test_status_reports_control_plane_health(cli, monkeypatch, capsys):
    code, out = cli("status", "--json", capsys=capsys)
    assert code == 0 and json.loads(out)["health"] == {"engine": True, "router": True,
                                                       "control": True}
    monkeypatch.setenv(platform_cli.CONTROL_PORT_ENV, "8765")
    probed: list[str] = []

    def probe(url: str) -> int:
        probed.append(url)
        return 503 if ":8765/" in url else 200

    monkeypatch.setattr(platform_cli, "http_probe", probe)
    code, out = cli("status", capsys=capsys)
    assert code == 0 and "engine up · router up · control down" in out
    assert "http://localhost:8765/health" in probed
