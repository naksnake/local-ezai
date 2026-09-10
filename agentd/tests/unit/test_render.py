"""Runtime descriptors + renderer (PR-3, ADR-026 R-1/R-3/R-4;
docs/PROVIDER_ABSTRACTION.md, docs/RUNTIME_ABSTRACTION_STRATEGY.md §4).

Golden files live in tests/fixtures/rendered/<profile>/ — one directory per
legacy profile equivalent (n97 · n97-igpu · gpu · cpu) plus the multi-model
router form. Regenerate deliberately with RENDER_UPDATE_GOLDENS=1 and review
the diff: a golden change IS a rendered-artifact change.

All model names are synthetic fixtures; no test depends on the host."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from agentd.capability import CapabilityVector, class_for_profile
from agentd.model_registry import load_model_registry
from agentd.registry_v2 import RegistryV2, reference_registry
from agentd.render import (
    ENGINE_COMPOSE_FILENAME,
    ENGINE_HOST,
    ENGINE_SERVICE,
    LITELLM_FILENAME,
    MANIFEST_FILENAME,
    PRESET_FILENAME,
    ROLE_MAP_FILENAME,
    DriftError,
    EmbeddingEntry,
    RenderError,
    check_drift,
    negotiate,
    render,
    render_role_map,
    write_rendered,
)
from agentd.runtime_descriptor import (
    DescriptorError,
    RuntimeDescriptor,
    load_descriptor,
    load_descriptors,
    parse_descriptor,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"
GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "rendered"

EMBEDDING = EmbeddingEntry(alias="embed-x", model="example/embed-x",
                           api_base="http://embed-server:8001/v1")


@pytest.fixture(scope="module")
def descriptors() -> dict[str, RuntimeDescriptor]:
    return load_descriptors(CONFIG_DIR)


def registry(runtime: str, count: int = 1, **model_overrides) -> RegistryV2:
    """Synthetic single-runtime registry: `count` active models, four roles
    with the reference contracts."""
    names = ["alpha", "beta", "gamma"][:count]

    def source(name: str) -> dict[str, str]:
        if runtime == "llamacpp":
            return {"gguf": f"https://example.invalid/{name}-q4_k_m.gguf"}
        return {"hf": f"example/{name}"}

    models = {name: {"provider": runtime, "source": source(name), "state": "active",
                     "context": 8192, "template": "chatml",
                     "tool_call_format": "hermes", **model_overrides}
              for name in names}
    return RegistryV2.model_validate({
        "generation": 3, "providers": ["llamacpp", "vllm"], "models": models,
        "groups": {"reasoning": names, "coding": names[::-1], "chat": names},
        "roles": {
            "planner": {"group": "reasoning",
                        "requires": {"tool_calling": True, "min_context": 8192}},
            "coder": {"group": "coding",
                      "requires": {"tool_calling": True, "min_context": 8192}},
            "reviewer": {"group": "reasoning", "pin": names[0],
                         "requires": {"tool_calling": True, "json_output": True}},
            "chat": {"group": "chat", "requires": {"min_context": 4096}},
        }})


def cpu_low_vector(**overrides) -> CapabilityVector:
    return CapabilityVector(system_memory_gb=15.5, cpu_cores=4,
                            cpu_flags=["avx2"], **overrides)


def accel_large_vector() -> CapabilityVector:
    return CapabilityVector(accelerator="cuda", accel_memory_gb=24,
                            system_memory_gb=64, cpu_cores=16,
                            cpu_flags=["avx2", "avx512f"])


# ── descriptors: the six-verb contract as data ───────────────────────────────


def test_shipped_descriptors_load_and_declare_the_contract(descriptors):
    assert set(descriptors) == {"llamacpp", "vllm"}
    for name, descriptor in descriptors.items():
        assert descriptor.runtime == name
        assert descriptor.serves_formats
        # six verbs: materialize + capabilities (renderer), control/ready/
        # validate_model/bench (lifecycle manager data)
        assert descriptor.materialize.single.command
        assert descriptor.verbs.control.kind == "compose"
        assert descriptor.verbs.ready.path.startswith("/")
        assert descriptor.verbs.validate_model.max_tokens >= 1
        assert descriptor.verbs.bench.max_tokens > 0
        assert descriptor.capabilities.tool_call_parsers
        # per-accelerator image table (HARDWARE_AGNOSTIC §2) — kinds, not brands
        assert set(descriptor.accelerators) <= {"cuda", "rocm", "igpu", "none"}
        assert "none" in descriptor.accelerators  # every runtime has a CPU path
    assert descriptors["llamacpp"].capabilities.parallel_models
    assert descriptors["llamacpp"].materialize.multi is not None
    assert not descriptors["vllm"].capabilities.parallel_models


def test_descriptor_schema_rejects_inconsistent_data(descriptors):
    base = yaml.safe_load((CONFIG_DIR / "providers" / "vllm.yaml").read_text())
    base["capabilities"]["parallel_models"] = True  # …but no multi form
    with pytest.raises(DescriptorError, match="materialize.multi is missing"):
        parse_descriptor(yaml.safe_dump(base), "fixture")
    base = yaml.safe_load((CONFIG_DIR / "providers" / "vllm.yaml").read_text())
    base["accelerators"]["rtx"] = base["accelerators"]["cuda"]  # a brand, not a kind
    with pytest.raises(DescriptorError, match="unknown accelerator kind 'rtx'"):
        parse_descriptor(yaml.safe_dump(base), "fixture")
    del base["accelerators"]["rtx"]
    del base["tuning"]["ctx_size"]
    with pytest.raises(DescriptorError, match="tuning.ctx_size is required"):
        parse_descriptor(yaml.safe_dump(base), "fixture")


def test_descriptor_file_name_must_match_runtime_id(tmp_path):
    text = (CONFIG_DIR / "providers" / "vllm.yaml").read_text()
    (tmp_path / "other.yaml").write_text(text)
    with pytest.raises(DescriptorError, match="must equal runtime id"):
        load_descriptor(tmp_path / "other.yaml")
    with pytest.raises(DescriptorError, match="no runtime descriptors"):
        load_descriptors(tmp_path)


# ── golden renders: the four legacy profiles + the router form ───────────────

PROFILES = {
    # profile → (runtime, vector, asserted class, active model count)
    "n97": ("llamacpp", cpu_low_vector(), class_for_profile("n97"), 1),
    "n97-igpu": ("llamacpp", cpu_low_vector(accelerator="igpu"),
                 class_for_profile("n97-igpu"), 1),
    "gpu": ("vllm", accel_large_vector(), None, 1),
    "cpu": ("vllm", CapabilityVector(system_memory_gb=32, cpu_cores=8,
                                     cpu_flags=["avx2"]), class_for_profile("cpu"), 1),
    "multi": ("llamacpp", accel_large_vector(), None, 3),
}


#: The engine-slot materialization is the artifact that varies per
#: (runtime × class × accelerator) — golden per profile; the n97 reference
#: additionally freezes the complete artifact set.
ENGINE_GOLDENS = {ENGINE_COMPOSE_FILENAME, PRESET_FILENAME}


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_golden_render_per_profile(descriptors, profile):
    runtime, vector, capability_class, count = PROFILES[profile]
    result = render(registry(runtime, count), descriptors, vector,
                    capability_class=capability_class, embedding=EMBEDDING)
    frozen = {name: text for name, text in result.artifacts.items()
              if profile == "n97" or name in ENGINE_GOLDENS}
    golden = GOLDEN_DIR / profile
    if os.environ.get("RENDER_UPDATE_GOLDENS"):
        golden.mkdir(parents=True, exist_ok=True)
        for stale in golden.glob("*"):
            stale.unlink()
        for name, text in frozen.items():
            (golden / name).write_text(text, encoding="utf-8")
    expected = {path.name: path.read_text(encoding="utf-8")
                for path in golden.glob("*")}
    assert expected, f"missing goldens for {profile} (RENDER_UPDATE_GOLDENS=1)"
    assert set(frozen) == set(expected)
    for name, text in frozen.items():
        assert text == expected[name], f"{profile}/{name} drifted from golden"


def test_h4_legacy_preset_renders_identically_to_its_class(descriptors):
    """H4: the low-power SKU profile is an alias of cpu-low — byte-identical
    artifacts whether asserted by preset or by class."""
    reg = registry("llamacpp")
    via_preset = render(reg, descriptors, cpu_low_vector(),
                        capability_class=class_for_profile("n97"))
    via_class = render(reg, descriptors, cpu_low_vector(), capability_class="cpu-low")
    assert via_preset.artifacts == via_class.artifacts
    assert via_preset.capability_class == "cpu-low"


def test_render_is_deterministic(descriptors):
    reg = registry("vllm")
    first = render(reg, descriptors, accel_large_vector(), embedding=EMBEDDING)
    second = render(reg, descriptors, accel_large_vector(), embedding=EMBEDDING)
    assert first.artifacts == second.artifacts  # no timestamps, no host state


# ── rendered LiteLLM config ──────────────────────────────────────────────────


def test_litellm_serves_role_aliases_at_the_engine_alias(descriptors):
    result = render(registry("llamacpp", 3), descriptors, accel_large_vector(),
                    embedding=EMBEDDING)
    config = yaml.safe_load(result.artifacts[LITELLM_FILENAME])
    entries = {e["model_name"]: e["litellm_params"] for e in config["model_list"]}
    # one alias per served model + one per role (R-1) + the embedding route
    assert {"alpha", "beta", "gamma"} <= set(entries)
    assert {"role-planner", "role-coder", "role-reviewer", "role-chat"} <= set(entries)
    assert entries["role-coder"]["model"] == "openai/gamma"  # coding group order
    assert entries["embed-x"]["api_base"] == "http://embed-server:8001/v1"
    # the engine is addressed by its neutral alias only (R-3)
    engine_entries = [p for n, p in entries.items() if n != "embed-x"]
    assert all(p["api_base"] == f"http://{ENGINE_HOST}:8000/v1" for p in engine_entries)
    assert f"{ENGINE_SERVICE}:8000" not in result.artifacts[LITELLM_FILENAME]
    assert result.artifacts[LITELLM_FILENAME].startswith("# GENERATED")


def test_litellm_platform_sections_match_the_hand_written_config(descriptors):
    """Byte-identical chat behavior across the cutover (PR-7): master key +
    auto-RAG hook sections equal the retired hand-written config (kept as
    a fixture under tests/fixtures/legacy/)."""
    rendered = yaml.safe_load(
        render(registry("vllm"), descriptors, accel_large_vector())
        .artifacts[LITELLM_FILENAME])
    legacy = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "fixtures" / "legacy" / "litellm-config.yaml")
        .read_text())
    assert rendered["general_settings"] == legacy["general_settings"]
    assert rendered["litellm_settings"] == legacy["litellm_settings"]
    legacy_key = legacy["model_list"][0]["litellm_params"]["api_key"]
    assert all(e["litellm_params"]["api_key"] == legacy_key
               for e in rendered["model_list"])


# ── role map: ADR-020 shape, golden chain ────────────────────────────────────


def test_role_map_round_trips_through_the_adr020_loader_golden(tmp_path):
    """GOLDEN: the rendered role map of the reference set, parsed by the
    shipped ADR-020 loader, equals this repository's live
    .agent/model_registry.yaml (CLAUDE.md agent_model_map)."""
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    (agent_dir / "model_registry.yaml").write_text(
        render_role_map(reference_registry()), encoding="utf-8")
    rendered = load_model_registry(agent_dir)
    live = load_model_registry(REPO_ROOT / ".agent")
    assert live, "repo .agent/model_registry.yaml missing — golden broken"
    for role, spec in live.items():
        assert rendered[role] == spec, f"role map drift for '{role}'"


def test_role_map_carries_generation_runtime_and_aliases():
    data = yaml.safe_load(render_role_map(registry("vllm", 2), "vllm"))
    assert data["generation"] == 3 and data["runtime"] == "vllm"
    assert data["agent_model_map"]["planner"] == {"primary": "alpha", "fallback": ["beta"]}
    assert data["agent_model_map"]["reviewer"] == {"primary": "alpha"}  # no empty key
    assert data["aliases"]["planner"] == "role-planner"


# ── capability negotiation (CF-6): failures name the missing capability ──────


def test_reference_set_negotiates_on_accel_large(descriptors):
    report = negotiate(reference_registry(), descriptors, "accel-large")
    assert report and all(check.ok for check in report)
    roles = {check.role for check in report}
    assert {"orchestrator", "planner", "coder", "debugger", "reviewer",
            "memory", "chat", "documentation", "evolution", "sprint"} <= roles
    # fallbacks are negotiated too — a fallback that cannot serve the role
    # would otherwise fail silently at request time
    assert any(check.position == "fallback" for check in report)


def test_negotiation_names_missing_tool_calling(descriptors):
    reg = registry("llamacpp", tool_call_format="mystery-format")
    failures = [f for c in negotiate(reg, descriptors, "cpu-low") for f in c.failures]
    assert failures
    assert all("missing capability tool_calling" in f for f in failures)
    assert "mystery-format" in failures[0] and "hermes" in failures[0]
    with pytest.raises(RenderError, match="missing capability tool_calling"):
        render(reg, descriptors, cpu_low_vector())


def test_negotiation_names_min_context_with_class_budget(descriptors):
    """A class budget below a role's contract is a render-time error that
    names the role, the model, the runtime and both numbers."""
    checks = [c for c in negotiate(registry("vllm"), descriptors, "cpu-low")
              if not c.ok]
    # 8192-token roles AND the 4096-token chat role exceed a 2048 budget
    assert {c.role for c in checks} == {"planner", "coder", "chat"}
    message = next(c for c in checks if c.role == "planner").failures[0]
    assert "missing capability min_context" in message
    assert "needs 8192" in message and "gets 2048" in message
    assert "class cpu-low" in message and "'vllm'" in message


def test_negotiation_names_json_output(descriptors):
    text = (CONFIG_DIR / "providers" / "llamacpp.yaml").read_text()
    data = yaml.safe_load(text)
    data["capabilities"]["json_output"] = False
    weak = {"llamacpp": parse_descriptor(yaml.safe_dump(data), "fixture")}
    failing = [c for c in negotiate(registry("llamacpp"), weak, "accel-large")
               if not c.ok]
    assert [c.role for c in failing] == ["reviewer"]
    assert "missing capability json_output" in failing[0].failures[0]


def test_format_mismatch_explains_the_fix(descriptors):
    """A GGUF-only model routed to vLLM fails at render time with the
    formats named (RUNTIME_ABSTRACTION §5 wording), never at request time."""
    reg = registry("vllm", source={"gguf": "https://example.invalid/alpha.gguf"})
    with pytest.raises(RenderError) as err:
        render(reg, descriptors, accel_large_vector())
    assert "format: model 'alpha' is gguf" in str(err.value)
    assert "serves hf, awq, gptq" in str(err.value)


def test_model_with_both_variants_is_served_in_the_runtime_format(descriptors):
    """Agnosticism: a model may declare several variants; each runtime picks
    the one it serves — switching runtimes is then pure data."""
    both = {"hf": "example/alpha", "gguf": "https://example.invalid/alpha.gguf"}
    on_vllm = render(registry("vllm", source=both), descriptors, accel_large_vector())
    assert "--model\n    - example/alpha" in on_vllm.artifacts[ENGINE_COMPOSE_FILENAME]
    on_llamacpp = render(registry("llamacpp", source=both), descriptors,
                         accel_large_vector())
    assert "/models/alpha.gguf" in on_llamacpp.artifacts[ENGINE_COMPOSE_FILENAME]


# ── engine slot: one runtime, honest conflicts ───────────────────────────────


def test_mixed_runtime_active_set_is_a_named_conflict(descriptors):
    """The reference set spans two runtimes; one slot serves one runtime —
    the error lists the models per runtime and the fix."""
    with pytest.raises(RenderError) as err:
        render(reference_registry(), descriptors, accel_large_vector())
    message = str(err.value)
    assert "spans several runtimes" in message
    assert "llamacpp: deepseek-r1, llama3" in message
    assert "vllm: hermes3, qwen3-coder" in message
    assert "AI_RUNTIME" in message


def test_selected_runtime_lists_foreign_active_models(descriptors):
    with pytest.raises(RenderError) as err:
        render(reference_registry(), descriptors, accel_large_vector(),
               runtime="llamacpp")
    message = str(err.value)
    assert "runtime 'llamacpp' owns the engine slot" in message
    assert "hermes3, qwen3-coder (vllm)" in message


def test_single_model_runtime_refuses_several_active_models(descriptors):
    with pytest.raises(RenderError) as err:
        render(registry("vllm", 2), descriptors, accel_large_vector())
    assert "serves one model per engine instance but 2 are active" in str(err.value)
    assert "alpha, beta" in str(err.value)


def test_missing_accelerator_image_names_supported_kinds(descriptors):
    """No silent CPU fallback: a runtime without an image for the detected
    accelerator kind fails loudly; the explicit override renders the CPU path."""
    vector = CapabilityVector(accelerator="igpu", system_memory_gb=32, cpu_cores=8)
    reg = registry("vllm")
    with pytest.raises(RenderError) as err:
        render(reg, descriptors, vector, capability_class="cpu-standard")
    assert "no image for accelerator kind 'igpu'" in str(err.value)
    assert "supports: cuda, rocm, none" in str(err.value)
    result = render(reg, descriptors, vector, capability_class="cpu-standard",
                    accelerator="none")
    assert result.accelerator == "none"
    assert "vllm-openai-cpu" in result.artifacts[ENGINE_COMPOSE_FILENAME]


def test_render_aggregates_every_problem(descriptors):
    """One loud error naming all reasons — never a fix-one-see-the-next loop."""
    reg = registry("vllm", 2, tool_call_format="unknown")
    with pytest.raises(RenderError) as err:
        render(reg, descriptors, accel_large_vector())
    message = str(err.value)
    assert "problem(s):" in message
    assert "missing capability tool_calling" in message
    assert "one model per engine instance" in message


def test_unknown_placeholder_is_a_descriptor_error(descriptors):
    data = yaml.safe_load((CONFIG_DIR / "providers" / "vllm.yaml").read_text())
    data["materialize"]["single"]["command"].append("{bogus}")
    broken = {"vllm": parse_descriptor(yaml.safe_dump(data), "fixture")}
    with pytest.raises(RenderError, match="unknown placeholder '{bogus}'"):
        render(registry("vllm"), broken, accel_large_vector())


# ── engine compose override: structure and the override tag ─────────────────


class _OverrideLoader(yaml.SafeLoader):
    """Reads compose's !override tag back as a plain mapping."""


_OverrideLoader.add_constructor(
    "!override", lambda loader, node: loader.construct_mapping(node, deep=True))


def test_engine_override_targets_the_slot_service_and_replaces_deploy(descriptors):
    result = render(registry("llamacpp"), descriptors, cpu_low_vector(),
                    capability_class="cpu-low")
    text = result.artifacts[ENGINE_COMPOSE_FILENAME]
    assert "deploy: !override" in text  # a plain merge would keep a GPU reservation
    data = yaml.load(text, Loader=_OverrideLoader)
    service = data["services"][ENGINE_SERVICE]
    assert service["image"] == descriptors["llamacpp"].accelerators["none"].image
    # field-for-field the shipped low-power profile: single model, 8192 ctx,
    # 4 threads, jinja templates, 6g cap, health probe
    assert service["command"][:4] == ["-m", "/models/alpha-q4_k_m.gguf", "--alias", "alpha"]
    assert service["command"][service["command"].index("--ctx-size") + 1] == "8192"
    assert service["command"][service["command"].index("--threads") + 1] == "4"
    assert "--jinja" in service["command"]
    assert service["deploy"] == {"resources": {"limits": {"memory": "6g"}}}
    assert service["healthcheck"]["test"][1].startswith("curl -f http://localhost:8000/health")
    assert service["volumes"] == ["${N97_GGUF_DIR:-./models/gguf}:/models:ro"]


def test_tool_calling_args_follow_negotiation(descriptors):
    with_tools = render(registry("vllm"), descriptors, accel_large_vector())
    assert "--tool-call-parser" in with_tools.artifacts[ENGINE_COMPOSE_FILENAME]
    # a chat-only role set with a model that declares no tool format → no parser flag
    reg = registry("vllm", tool_call_format="")
    reg.roles = {"chat": reg.roles["chat"]}
    without = render(reg, descriptors, accel_large_vector())
    assert "--tool-call-parser" not in without.artifacts[ENGINE_COMPOSE_FILENAME]


def test_multi_model_form_renders_router_preset(descriptors):
    result = render(registry("llamacpp", 3), descriptors, cpu_low_vector(),
                    capability_class="cpu-low")
    compose = yaml.load(result.artifacts[ENGINE_COMPOSE_FILENAME], Loader=_OverrideLoader)
    command = compose["services"][ENGINE_SERVICE]["command"]
    assert command[:2] == ["--models-preset", "/presets/engine-models.ini"]
    assert command[command.index("--models-max") + 1] == "1"  # cpu-low: one loaded at a time
    preset = result.artifacts[PRESET_FILENAME]
    assert preset.startswith("version = 1\n\n[*]\n")
    for name in ("alpha", "beta", "gamma"):
        assert f"\n[{name}]\nmodel = /models/{name}-q4_k_m.gguf\nc = 8192\n" in preset
    assert "jinja = true" in preset
    assert any(v.endswith(":/presets/engine-models.ini:ro")
               for v in compose["services"][ENGINE_SERVICE]["volumes"])


# ── persistence + drift detection ────────────────────────────────────────────


def test_write_rendered_records_manifest_and_refuses_drift(tmp_path, descriptors):
    result = render(registry("llamacpp", 3), descriptors, accel_large_vector())
    out = tmp_path / "rendered"
    written = write_rendered(result, out)
    assert {p.name for p in written} == set(result.artifacts) | {MANIFEST_FILENAME}
    manifest = yaml.safe_load((out / MANIFEST_FILENAME).read_text())
    assert manifest["generation"] == 3 and manifest["runtime"] == "llamacpp"
    assert set(manifest["artifacts"]) == set(result.artifacts)
    assert check_drift(out) == []

    # re-rendering the same thing is fine
    write_rendered(result, out)

    # a hand edit is refused and named
    litellm = out / LITELLM_FILENAME
    litellm.write_text(litellm.read_text() + "# tweak\n", encoding="utf-8")
    drift = check_drift(out)
    assert len(drift) == 1 and drift[0].startswith(f"{LITELLM_FILENAME}: edited by hand")
    with pytest.raises(DriftError, match="edited by hand") as err:
        write_rendered(result, out)
    assert "local-ezai model" in str(err.value)
    # explicit force discards the manual change
    write_rendered(result, out, force=True)
    assert check_drift(out) == []
    assert litellm.read_text() == result.artifacts[LITELLM_FILENAME]


def test_write_rendered_refuses_unmanaged_files(tmp_path, descriptors):
    out = tmp_path / "rendered"
    out.mkdir()
    (out / LITELLM_FILENAME).write_text("model_list: []\n", encoding="utf-8")
    with pytest.raises(DriftError, match="unmanaged file"):
        write_rendered(render(registry("vllm"), descriptors, accel_large_vector()), out)


def test_write_rendered_removes_artifacts_the_new_render_no_longer_produces(
        tmp_path, descriptors):
    out = tmp_path / "rendered"
    write_rendered(render(registry("llamacpp", 3), descriptors, accel_large_vector()), out)
    assert (out / PRESET_FILENAME).is_file()
    write_rendered(render(registry("llamacpp", 1), descriptors, accel_large_vector()), out)
    assert not (out / PRESET_FILENAME).exists()  # single form: no stale preset
    assert PRESET_FILENAME not in yaml.safe_load(
        (out / MANIFEST_FILENAME).read_text())["artifacts"]
    assert (out / ROLE_MAP_FILENAME).is_file()


# ── platform tripwires ───────────────────────────────────────────────────────


def test_compose_engine_slot_carries_the_neutral_alias():
    """R-3 as-built: the historical service keeps its name and gains the
    `engine` network alias every rendered artifact relies on."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    service = compose["services"][ENGINE_SERVICE]
    assert ENGINE_HOST in service["networks"]["ai-net"]["aliases"]
    assert service["ports"] == ["${LLM_PORT:-8000}:8000"]  # slot port unchanged


def test_renderer_code_carries_no_runtime_or_vendor_knowledge():
    """H1 discipline: brands, images, flags, device names live in descriptors
    only (accelerator KINDS such as cuda/rocm are API families and allowed)."""
    src = Path(__file__).resolve().parents[2] / "src" / "agentd"
    for module in ("render.py", "runtime_descriptor.py"):
        text = (src / module).read_text(encoding="utf-8").lower()
        for forbidden in ("nvidia", "ghcr.io", "vllm/", "llama.cpp", "/dev/dri",
                          "/dev/kfd", "--gpu-layers", "--tool-call-parser",
                          "--models-preset", "--served-model-name"):
            assert forbidden not in text, f"{module} mentions {forbidden!r}"
