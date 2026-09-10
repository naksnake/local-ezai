"""Role aliases in code + the three routing layers (PR-6, ADR-026 R-1 /
CF-3; docs/MODEL_ROUTING_DESIGN.md §5): aliases < platform Registry v2 <
per-repo ADR-020 registry. Plus the tripwire that the stack's hand-written
LiteLLM configs actually serve every role alias."""

from __future__ import annotations

from pathlib import Path

import yaml

from agentd.config import LLM_ROLES, ROLE_ALIAS_PREFIX, AgentdConfig
from agentd.registry_v2 import RegistryV2, save_generation
from agentd.render import render, write_rendered
from agentd.routing import (
    PLATFORM_ENV,
    apply_platform_routing,
    effective_routing,
    find_platform_config,
    platform_role_map,
)
from agentd.runtime_descriptor import load_descriptors

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "agentd" / "src" / "agentd"
DESCRIPTORS = load_descriptors(REPO_ROOT / "config")

REPO_REGISTRY = ("agent_model_map:\n"
                 "  planner:\n    primary: repo-planner\n"   # pinned, no fallback
                 "  reviewer:\n    primary: repo-reviewer\n    fallback: [repo-spare]\n")


# ── aliases are the only names in code (CF-3) ────────────────────────────────


def test_code_defaults_are_role_aliases_not_model_names():
    llm = AgentdConfig().llm
    for role in LLM_ROLES:
        assert llm.roles[role] == f"{ROLE_ALIAS_PREFIX}{role}"
    assert llm.model_for_role("planner") == "role-planner"
    assert llm.model_for_role("never-configured") == "role-chat"  # `default`
    # the source of the config module carries no model name anywhere
    text = (SRC / "config.py").read_text(encoding="utf-8").lower()
    for forbidden in ("qwen", "llama", "hermes", "deepseek", "mistral"):
        assert forbidden not in text


def test_hand_written_litellm_configs_serve_every_role_alias():
    """The alias switch (PR-6) did not break the shipped profiles: each
    LiteLLM profile mapped every LLM role alias to a model it served itself.
    The profiles were retired into rendered output at the PR-7 cutover and
    live on as fixtures — the renderer's own alias coverage is tested in
    test_render.py."""
    llm_roles = [r for r in LLM_ROLES if r not in ("validator", "git")]
    legacy_dir = Path(__file__).resolve().parents[1] / "fixtures" / "legacy"
    for name in ("litellm-config.yaml", "litellm-config.cpu.yaml", "litellm-config.n97.yaml"):
        config = yaml.safe_load((legacy_dir / name).read_text(encoding="utf-8"))
        entries = {e["model_name"]: e["litellm_params"]["model"] for e in config["model_list"]}
        served = {e["litellm_params"]["model"] for n, e in
                  ((e["model_name"], e) for e in config["model_list"])
                  if not n.startswith(ROLE_ALIAS_PREFIX)}
        for role in llm_roles:
            alias = f"{ROLE_ALIAS_PREFIX}{role}"
            assert alias in entries, f"{name}: {alias} missing"
            assert entries[alias] in served, f"{name}: {alias} → unserved {entries[alias]}"


# ── platform discovery ───────────────────────────────────────────────────────


def make_platform(tmp_path: Path) -> Path:
    root = tmp_path / "platform"
    (root / "config" / "providers").mkdir(parents=True)
    (root / "docker-compose.yml").write_text("services: {}\n")
    return root


def test_find_platform_config_explicit_env_walkup_and_none(tmp_path, monkeypatch):
    root = make_platform(tmp_path)
    project = root / "some" / "repo"
    project.mkdir(parents=True)
    monkeypatch.delenv(PLATFORM_ENV, raising=False)
    assert find_platform_config(AgentdConfig(), project) == root / "config"  # walk-up
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert find_platform_config(AgentdConfig(), outside) is None  # hermetic
    explicit = AgentdConfig.model_validate({"platform": {"config_dir": str(tmp_path / "x")}})
    assert find_platform_config(explicit, outside) == (tmp_path / "x").resolve()
    assert find_platform_config(AgentdConfig(), outside,
                                environ={PLATFORM_ENV: str(tmp_path / "y")}) \
        == (tmp_path / "y").resolve()


# ── layer 2: the platform's role map ─────────────────────────────────────────


def platform_registry() -> RegistryV2:
    def model(name: str) -> dict:
        return {"provider": "llamacpp", "state": "active", "context": 8192,
                "tool_call_format": "hermes",
                "source": {"gguf": f"https://example.invalid/w/{name}.gguf"}}
    return RegistryV2.model_validate({
        "providers": ["llamacpp", "vllm"],
        "models": {"p1": model("p1"), "p2": model("p2"), "c1": model("c1")},
        "groups": {"reasoning": ["p1", "p2"], "coding": ["c1"], "chat": ["p2"]},
        "roles": {"planner": {"group": "reasoning"}, "coder": {"group": "coding"},
                  "reviewer": {"group": "reasoning"}, "chat": {"group": "chat"}},
    })


def test_platform_role_map_prefers_rendered_then_registry(tmp_path):
    root = make_platform(tmp_path)
    config_dir = root / "config"
    assert platform_role_map(config_dir) is None  # pre-bootstrap
    saved = save_generation(platform_registry(), config_dir, note="gen1")
    mapping, source = platform_role_map(config_dir)
    assert source == "registry generation 1"
    assert mapping["planner"] == {"primary": "p1", "fallback": ["p2"]}
    assert mapping["coder"] == {"primary": "c1", "fallback": []}
    # once rendered, the rendered role map (what LiteLLM serves) wins
    from agentd.capability import CapabilityVector

    result = render(saved, DESCRIPTORS, CapabilityVector(system_memory_gb=32, cpu_cores=8),
                    capability_class="cpu-standard")
    write_rendered(result, config_dir / "rendered")
    mapping, source = platform_role_map(config_dir)
    assert source.startswith("rendered role map (generation 1")
    assert mapping["planner"]["primary"] == "p1"


# ── precedence: aliases < platform < repo (ADR-020 regression suite) ─────────


def test_three_layer_precedence(tmp_path):
    root = make_platform(tmp_path)
    save_generation(platform_registry(), root / "config", note="gen1")
    repo = root / "work" / "repo"
    (repo / ".agent").mkdir(parents=True)
    (repo / ".agent" / "model_registry.yaml").write_text(REPO_REGISTRY)

    effective, sources = effective_routing(AgentdConfig(), repo)
    llm = effective.llm
    # layer 3: repo pins are exact chains — no platform fallback inherited
    assert llm.roles["planner"] == "repo-planner" and "planner" not in llm.role_fallbacks
    assert llm.roles["reviewer"] == "repo-reviewer"
    assert llm.role_fallbacks["reviewer"] == ["repo-spare"]
    # layer 2: platform seeds roles the repo does not declare, with fallbacks
    assert llm.roles["coder"] == "c1" and "coder" not in llm.role_fallbacks
    assert llm.roles["chat"] == "p2"
    # layer 1: roles the platform does not know keep their alias
    assert llm.roles["debugger"] == "role-debugger"
    assert sources[0] == "platform: registry generation 1" and sources[1].startswith("repo:")

    # no platform: aliases + repo only (today's behavior, byte-for-byte)
    lonely = tmp_path / "lonely"
    (lonely / ".agent").mkdir(parents=True)
    (lonely / ".agent" / "model_registry.yaml").write_text(REPO_REGISTRY)
    alone, sources = effective_routing(AgentdConfig(), lonely)
    assert alone.llm.roles["planner"] == "repo-planner"
    assert alone.llm.roles["coder"] == "role-coder"
    assert sources == [f"repo: {lonely / '.agent' / 'model_registry.yaml'}"]


def test_apply_platform_routing_without_platform_is_identity():
    config = AgentdConfig()
    same, source = apply_platform_routing(config, None)
    assert same is config and source is None


def test_prepare_run_seeds_from_platform_and_keeps_repo_override(config, tmp_repo, tmp_path):
    from agentd.runner import prepare_run

    root = make_platform(tmp_path)
    save_generation(platform_registry(), root / "config", note="gen1")
    config.platform.config_dir = root / "config"
    (tmp_repo / ".agent").mkdir()
    (tmp_repo / ".agent" / "model_registry.yaml").write_text(REPO_REGISTRY)
    run_config, _, _ = prepare_run(config, tmp_repo, "routing1")
    assert run_config.llm.roles["coder"] == "c1"            # platform
    assert run_config.llm.roles["planner"] == "repo-planner"  # repo wins
    assert "planner" not in run_config.llm.role_fallbacks
    assert run_config.llm.roles["debugger"] == "role-debugger"  # alias


def test_prepare_run_without_platform_keeps_aliases(config, tmp_repo):
    from agentd.runner import prepare_run

    run_config, _, _ = prepare_run(config, tmp_repo, "routing2")
    assert run_config.llm.roles["planner"] == "role-planner"
    assert run_config.llm.role_fallbacks == {}
