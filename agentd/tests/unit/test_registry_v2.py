"""Registry v2 (PR-1, ADR-027): schema round-trip, referential integrity,
deterministic resolution, generations + diff — and the GOLDEN TEST proving
the reference data reproduces today's CLAUDE.md routing byte-for-byte
through the shipped ADR-020 mechanism."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from agentd.model_registry import load_model_registry
from agentd.registry_v2 import (
    ModelEntry,
    RegistryError,
    RegistryResolutionError,
    RegistryV2,
    RoleSpec,
    diff_generations,
    list_generations,
    load_generation,
    load_registry,
    reference_registry,
    registry_path,
    save_generation,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def small_registry(**overrides) -> RegistryV2:
    """A minimal valid registry for behavioral tests (names are fixtures,
    not defaults)."""
    data = {
        "providers": ["llamacpp", "vllm"],
        "models": {
            "alpha": {"provider": "vllm", "state": "active"},
            "beta": {"provider": "llamacpp", "state": "active"},
            "gamma": {"provider": "llamacpp", "state": "installed"},
        },
        "groups": {"reasoning": ["alpha", "gamma", "beta"],
                   "chat": ["beta"]},
        "roles": {
            "planner": {"group": "reasoning"},
            "chat": {"group": "chat"},
        },
    }
    data.update(overrides)
    return RegistryV2.model_validate(data)


# ── golden test (do not weaken: the productization tripwire) ─────────────────


def test_golden_reference_reproduces_claude_md_routing_byte_for_byte():
    """Registry v2 reference data must resolve to EXACTLY what the shipped
    ADR-020 loader produces from this repository's live
    .agent/model_registry.yaml (the CLAUDE.md agent_model_map). Any
    difference in a primary or a fallback list is a routing change and
    must fail here first."""
    adr020 = load_model_registry(REPO_ROOT / ".agent")
    assert adr020, "repo .agent/model_registry.yaml missing — golden broken"

    roles, fallbacks = reference_registry().role_map()
    for role, spec in adr020.items():
        assert roles[role] == spec["primary"], (
            f"primary drift for '{role}': registry v2 says {roles[role]!r}, "
            f"ADR-020 says {spec['primary']!r}")
        assert fallbacks.get(role, []) == spec["fallback"], (
            f"fallback drift for '{role}': registry v2 says "
            f"{fallbacks.get(role, [])!r}, ADR-020 says {spec['fallback']!r}")


def test_golden_reference_covers_all_runtime_roles():
    """Every LLM-using role of the runtime resolves in the reference set
    (resolution completeness for the mandated roster + orchestrator)."""
    resolutions = reference_registry().resolve_all()
    for role in ("orchestrator", "planner", "coder", "debugger", "reviewer",
                 "memory", "chat", "documentation", "evolution", "sprint"):
        assert role in resolutions
        assert resolutions[role].primary


# ── resolution semantics ─────────────────────────────────────────────────────


def test_group_resolution_order_and_active_filtering():
    resolution = small_registry().resolve("planner")
    # gamma is 'installed' → skipped; order otherwise preserved
    assert resolution.primary == "alpha"
    assert resolution.fallbacks == ["beta"]
    assert any("skipped gamma (state: installed)" in line
               for line in resolution.reason)
    assert any("group 'reasoning' order" in line for line in resolution.reason)


def test_pin_is_an_explicit_chain_without_group_fallbacks():
    registry = small_registry()
    registry.roles["planner"].pin = ["beta"]
    resolution = registry.resolve("planner")
    assert resolution.primary == "beta"
    assert resolution.fallbacks == []  # what you pin is what you get
    assert "pin chain" in resolution.reason[0]


def test_pin_accepts_single_string_in_yaml():
    spec = RoleSpec.model_validate({"group": "g", "pin": "solo"})
    assert spec.pin == ["solo"]


def test_pinned_inactive_models_are_skipped():
    registry = small_registry()
    registry.roles["planner"].pin = ["gamma", "alpha"]
    resolution = registry.resolve("planner")
    assert resolution.primary == "alpha" and resolution.fallbacks == []


def test_unknown_role_fails_loudly():
    with pytest.raises(RegistryResolutionError, match="role 'nope'"):
        small_registry().resolve("nope")


def test_group_without_active_model_fails_loudly_with_states():
    registry = small_registry()
    registry.models["beta"].state = "retired"
    with pytest.raises(RegistryResolutionError) as err:
        registry.resolve("chat")
    assert "beta (retired)" in str(err.value)
    assert "activate a model in group 'chat'" in str(err.value)


def test_resolve_all_aggregates_every_failure():
    registry = small_registry()
    registry.models["beta"].state = "failed"
    registry.models["alpha"].state = "retired"
    with pytest.raises(RegistryResolutionError) as err:
        registry.resolve_all()
    message = str(err.value)
    assert "2 role(s) unresolvable" in message
    assert "'planner'" in message and "'chat'" in message


def test_role_map_shapes_match_adr020_application():
    roles, fallbacks = small_registry().role_map()
    assert roles == {"planner": "alpha", "chat": "beta"}
    # chat has no fallbacks → no entry at all (byte-compatible with
    # apply_model_registry, which only sets non-empty fallback lists)
    assert fallbacks == {"planner": ["beta"]}


# ── referential integrity ────────────────────────────────────────────────────


def test_group_referencing_unknown_model_rejected():
    with pytest.raises((RegistryError, ValidationError), match="unknown model 'ghost'"):
        small_registry(groups={"reasoning": ["ghost"], "chat": ["beta"]})


def test_role_referencing_unknown_group_rejected():
    with pytest.raises((RegistryError, ValidationError), match="unknown group 'void'"):
        small_registry(roles={"planner": {"group": "void"}})


def test_pin_referencing_unknown_model_rejected():
    with pytest.raises((RegistryError, ValidationError), match="pins unknown model 'ghost'"):
        small_registry(roles={"planner": {"group": "reasoning",
                                          "pin": ["ghost"]}})


def test_undeclared_provider_rejected():
    with pytest.raises((RegistryError, ValidationError), match="undeclared provider"):
        small_registry(models={
            "alpha": {"provider": "mystery", "state": "active"},
            "beta": {"provider": "llamacpp", "state": "active"},
            "gamma": {"provider": "llamacpp", "state": "installed"},
        })


# ── store: round-trip, generations, immutability, diff ──────────────────────


def test_save_and_load_round_trip(tmp_path):
    saved = save_generation(small_registry(), tmp_path, note="genesis")
    assert saved.generation == 1
    assert saved.saved_at and saved.note == "genesis"

    loaded = load_registry(tmp_path)
    assert loaded.generation == 1
    assert loaded.models["alpha"].provider == "vllm"
    assert loaded.roles["planner"].group == "reasoning"
    assert registry_path(tmp_path).is_file()
    # the YAML on disk is plain data, no python tags
    raw = yaml.safe_load(registry_path(tmp_path).read_text())
    assert raw["version"] == 2 and raw["generation"] == 1


def test_generations_are_sequential_and_immutable(tmp_path):
    first = save_generation(small_registry(), tmp_path)
    registry = load_registry(tmp_path)
    registry.models["gamma"].state = "active"
    second = save_generation(registry, tmp_path, note="activate gamma")
    assert (first.generation, second.generation) == (1, 2)
    assert list_generations(tmp_path) == [1, 2]

    old = load_generation(tmp_path, 1)
    assert old.models["gamma"].state == "installed"
    assert load_generation(tmp_path, 2).models["gamma"].state == "active"

    # immutability: saving over an existing snapshot number is refused
    stale = load_generation(tmp_path, 1)  # generation 1 → would save as 2
    with pytest.raises(RegistryError, match="immutable"):
        save_generation(stale, tmp_path)


def test_unservable_generation_is_never_persisted(tmp_path):
    registry = small_registry()
    registry.models["beta"].state = "retired"  # chat role unservable
    with pytest.raises(RegistryResolutionError):
        save_generation(registry, tmp_path)
    assert not registry_path(tmp_path).exists()
    assert list_generations(tmp_path) == []


def test_load_registry_missing_names_bootstrap(tmp_path):
    with pytest.raises(RegistryError, match="bootstrap"):
        load_registry(tmp_path)


def test_diff_generations_reports_every_change_kind():
    old = small_registry()
    new = old.model_copy(deep=True)
    new.models["gamma"].state = "active"
    new.models["delta"] = ModelEntry(provider="vllm", state="registered")
    del new.models["beta"]
    new.groups["chat"] = ["alpha"]
    new.groups["reasoning"] = ["alpha", "gamma"]
    new.roles["chat"].group = "reasoning"
    new.roles["planner"].pin = ["alpha"]
    new.roles["extra"] = RoleSpec(group="chat")

    lines = diff_generations(old, new)
    assert "model gamma: state installed → active" in lines
    assert "model added: delta (vllm, state registered)" in lines
    assert "model removed: beta" in lines
    assert "group chat: [beta] → [alpha]" in lines
    assert "role chat: group chat → reasoning" in lines
    assert "role planner: pin [] → [alpha]" in lines
    assert "role added: extra → group chat" in lines
    assert diff_generations(old, old) == []
