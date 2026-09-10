"""H2 · H3 · H4 — the class fixtures of HARDWARE_AGNOSTIC_ARCHITECTURE §6
(PR-25, P6; ADR-026 (2)), over the PR-22/PR-23 first-run harness.

H2  the same `.env` role seeds produce a working platform on `accel-large`
    and on `cpu-low`, each group served by the recommender's pick for that
    class — nothing else differs but what the class tuning of the descriptor
    says;
H3  the fit verdict of a 7B Q4 model is right on all four class fixture
    vectors (and honest at the edges: spill, no SIMD, no fit, unknown size);
H4  `setup-n97` is the `cpu-low` preset, end to end: install by profile and
    by class, then the pipeline — byte-identical rendered artifacts."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from agentd.bootstrap import RUNTIME_KEY
from agentd.capability import CapabilityVector, classify, fit
from agentd.installer import EXIT_OK as INSTALL_OK
from agentd.registry_v2 import ModelEntry, load_registry
from agentd.render import MANIFEST_FILENAME, rendered_dir
from agentd.setup_pipeline import recommended_set
from tests.acceptance import test_fre_acceptance as fre
from tests.unit import test_setup_pipeline as pipeline_tests
from tests.unit.test_setup_pipeline import CPU_LOW

#: The PR-22 harness: a checkout-like platform with the bootstrap's seams faked.
platform = pipeline_tests.platform

AUTO_SEEDS = (f"{RUNTIME_KEY}=llamacpp\nREASONING_MODEL=auto\nCODING_MODEL=auto\n"
              "CHAT_MODEL=auto\nLITELLM_MASTER_KEY=sk-test\n")


# ── H2 · same seeds, two classes ─────────────────────────────────────────────


def test_h2_same_seeds_produce_a_working_platform_on_accel_large_and_cpu_low(
        platform, monkeypatch, tmp_path):
    (platform.root / ".env").write_text(AUTO_SEEDS, encoding="utf-8")
    accel_root = tmp_path / "accel"
    shutil.copytree(platform.root, accel_root)          # the same checkout, before any run
    outcomes = {}
    for klass, vector, root in (("cpu-low", CPU_LOW, platform.root),
                                ("accel-large", fre.ACCEL, accel_root)):
        ctx = fre.context_for(platform, vector, monkeypatch,
                              root=None if root == platform.root else root)
        assert ctx.platform.klass == klass
        run, *_ = fre.pipeline(ctx, root, fetchers=fre.PretendFetcherFactory())
        report = run.run()
        assert report.ready and report.capability_class == klass, report.lines()
        picks = {p.group: p.ref for p in recommended_set(ctx, "llamacpp")}
        assert all(picks.values()), picks                # every group has an eligible pick
        registry = load_registry(root / "config")
        assert {g: registry.groups[g] for g in picks} == {g: [ref] for g, ref in picks.items()}
        outcomes[klass] = (registry, picks, report)
    cpu, accel = outcomes["cpu-low"][0], outcomes["accel-large"][0]
    assert cpu.roles == accel.roles and set(cpu.groups) == set(accel.groups)
    assert outcomes["cpu-low"][2].runtime == outcomes["accel-large"][2].runtime == "llamacpp"
    assert outcomes["accel-large"][2].accelerator == "cuda"
    assert outcomes["cpu-low"][2].accelerator == "none"
    # the models are the recommender's per class; the materialization is the
    # descriptor's per (class × accelerator) — different images/args, same code
    compose = {k: (root / "config" / "rendered" / "docker-compose.engine.yml").read_text()
               for k, root in (("cpu-low", platform.root), ("accel-large", accel_root))}
    assert compose["cpu-low"] != compose["accel-large"]
    assert "class cpu-low · accelerator none" in compose["cpu-low"]
    assert "class accel-large · accelerator cuda" in compose["accel-large"]
    substituted = {g for g in cpu.groups if cpu.groups[g] != accel.groups[g]}
    assert substituted == {g for g, ref in outcomes["cpu-low"][1].items()
                           if outcomes["accel-large"][1][g] != ref}


# ── H3 · a 7B Q4 model on the four class vectors ─────────────────────────────

SEVEN_B_Q4 = ModelEntry(provider="llamacpp",
                        source={"gguf": "https://example.invalid/seven-b-q4_k_m.gguf"},
                        size_gb=4.4, context=32768, template="chatml", tool_call_format="hermes")
REQUIRED_GB = round(4.4 * 1.3, 2)

VECTORS = {
    "accel-large": CapabilityVector(accelerator="cuda", accel_memory_gb=24, system_memory_gb=64,
                                    cpu_cores=16, cpu_flags=["avx2", "avx512f"]),
    "accel-small": CapabilityVector(accelerator="cuda", accel_memory_gb=8, system_memory_gb=32,
                                    cpu_cores=8, cpu_flags=["avx2"]),
    "cpu-standard": CapabilityVector(system_memory_gb=32, cpu_cores=8, cpu_flags=["avx2"]),
    "cpu-low": CapabilityVector(system_memory_gb=15.5, cpu_cores=4, cpu_flags=["avx2"]),
}
EXPECTED = {"accel-large": ("accelerator", "fast"), "accel-small": ("accelerator", "fast"),
            "cpu-standard": ("system", "moderate"), "cpu-low": ("system", "moderate")}


@pytest.mark.parametrize("klass", sorted(VECTORS))
def test_h3_seven_b_q4_fit_verdict_is_correct_per_class(klass):
    vector = VECTORS[klass]
    assert classify(vector) == klass
    verdict = fit(SEVEN_B_Q4, vector)
    assert verdict.fits and (verdict.placement, verdict.speed_band) == EXPECTED[klass]
    assert verdict.required_memory_gb == REQUIRED_GB and verdict.overridable
    assert verdict.context_ceiling == 32768 and verdict.warnings == []


def test_h3_fit_is_honest_at_the_edges():
    no_simd = fit(SEVEN_B_Q4, CapabilityVector(system_memory_gb=15.5, cpu_cores=4))
    assert (no_simd.placement, no_simd.speed_band) == ("system", "slow")
    spill = fit(SEVEN_B_Q4, CapabilityVector(accelerator="cuda", accel_memory_gb=4,
                                             system_memory_gb=32, cpu_flags=["avx2"]))
    assert spill.fits and spill.placement == "system"
    assert any("does not fit accelerator memory" in w for w in spill.warnings)
    too_small = fit(SEVEN_B_Q4, CapabilityVector(system_memory_gb=5, cpu_cores=2))
    assert not too_small.fits and too_small.placement == "none" and too_small.overridable
    assert any(f"~{REQUIRED_GB:.1f} GB" in w and "override" in w for w in too_small.warnings)
    unknown = fit(SEVEN_B_Q4.model_copy(update={"size_gb": 0.0}), VECTORS["accel-large"])
    assert unknown.placement == "unknown" and unknown.fits and unknown.warnings


# ── H4 · setup-n97 = cpu-low, end to end ─────────────────────────────────────


def test_h4_the_n97_profile_and_the_cpu_low_class_render_byte_identically(
        platform, monkeypatch, tmp_path):
    fre.ensure_example(platform.root)
    by_class_root = tmp_path / "by-class"
    shutil.copytree(platform.root, by_class_root)
    by_preset = fre.install(platform.root, CPU_LOW, profile="n97")
    by_class = fre.install(by_class_root, CPU_LOW, capability_class="cpu-low")
    assert by_preset.exit_code == by_class.exit_code == INSTALL_OK
    assert by_preset.capability_class == by_class.capability_class == "cpu-low"
    assert by_preset.asserted.startswith("--profile n97")
    assert by_class.asserted.startswith("--class cpu-low")
    for root in (platform.root, by_class_root):
        ctx = fre.context_for(platform, CPU_LOW, monkeypatch,
                              root=None if root == platform.root else root)
        assert ctx.platform.klass == "cpu-low"
        run, *_ = fre.pipeline(ctx, root)
        report = run.run()
        assert report.ready and report.capability_class == "cpu-low", report.lines()
        assert report.profile == "n97"                    # the class maps back to the same preset

    def artifacts(root: Path) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in rendered_dir(root / "config").iterdir()}

    preset_files, class_files = artifacts(platform.root), artifacts(by_class_root)
    assert set(preset_files) == set(class_files) and len(preset_files) >= 4
    assert MANIFEST_FILENAME in preset_files              # hashes included: nothing differs
    for name in preset_files:
        assert preset_files[name] == class_files[name], f"{name} differs"

    def scrubbed_registry(root: Path) -> dict:
        data = load_registry(root / "config").model_dump(mode="json")
        data.pop("saved_at", None)
        for model in data["models"].values():            # wall-clock stamps of the two runs
            model.pop("installed_at", None)
            model.get("benchmarks", {}).pop("last", None)
        text = yaml.safe_dump(data)
        for prefix in (str(by_class_root), str(platform.root)):   # both seeds name the first root
            text = text.replace(prefix, "<root>")
        return yaml.safe_load(text)

    assert scrubbed_registry(platform.root) == scrubbed_registry(by_class_root)
