"""Capability vector, classes, and fit() (PR-2, ADR-026 R-2 /
docs/HARDWARE_AGNOSTIC_ARCHITECTURE.md). All fixtures are synthetic —
no test depends on the host it runs on."""

import pytest

from agentd.capability import (
    PROFILE_PRESETS,
    CapabilityVector,
    _parse_cpu_flags,
    _parse_meminfo,
    class_for_profile,
    classify,
    detect_vector,
    fit,
)
from agentd.registry_v2 import ModelEntry


def vector(**overrides) -> CapabilityVector:
    return CapabilityVector(**overrides)


def model(size_gb: float, context: int = 8192) -> ModelEntry:
    return ModelEntry(provider="anyruntime", size_gb=size_gb, context=context)


# ── classification (H3 fixtures) ─────────────────────────────────────────────


def test_classify_four_classes_from_fixture_vectors():
    assert classify(vector(accelerator="cuda", accel_memory_gb=24,
                           system_memory_gb=64)) == "accel-large"
    assert classify(vector(accelerator="rocm", accel_memory_gb=8,
                           system_memory_gb=32)) == "accel-small"
    assert classify(vector(accelerator="igpu", accel_memory_gb=0,
                           system_memory_gb=16)) == "accel-small"
    assert classify(vector(system_memory_gb=32)) == "cpu-standard"
    assert classify(vector(system_memory_gb=8)) == "cpu-low"


def test_classify_boundaries_and_unknown_memory_err_conservative():
    assert classify(vector(accelerator="cuda", accel_memory_gb=16)) == "accel-large"
    assert classify(vector(system_memory_gb=16)) == "cpu-standard"
    # undetectable memory (0) never inflates the class
    assert classify(vector()) == "cpu-low"


def test_profile_presets_h4():
    """Legacy profiles are aliases, not architectures (H4)."""
    assert class_for_profile("n97") == "cpu-low"
    assert class_for_profile("n97-igpu") == "cpu-low"
    assert class_for_profile("cpu") == "cpu-standard"
    # 'gpu' asserts detection — the class still comes from measurement
    measured = vector(accelerator="cuda", accel_memory_gb=8,
                      system_memory_gb=32)
    assert class_for_profile("gpu", measured) == "accel-small"
    assert PROFILE_PRESETS["gpu"] is None


def test_unknown_profile_rejected():
    with pytest.raises(ValueError, match="unknown profile 'rack42'"):
        class_for_profile("rack42")


# ── fit() (pure, conservative, never blocking) ───────────────────────────────


def test_fit_in_accelerator_is_fast():
    verdict = fit(model(size_gb=5.0),
                  vector(accelerator="cuda", accel_memory_gb=16,
                         system_memory_gb=32, cpu_flags=["avx2"]))
    assert verdict.fits and verdict.placement == "accelerator"
    assert verdict.speed_band == "fast"
    assert verdict.required_memory_gb == 6.5  # 5.0 × 1.3 overhead
    assert verdict.context_ceiling == 8192
    assert verdict.warnings == []
    assert verdict.overridable


def test_fit_spills_to_system_memory_with_warning():
    verdict = fit(model(size_gb=10.0),
                  vector(accelerator="cuda", accel_memory_gb=8,
                         system_memory_gb=64, cpu_flags=["avx2"]))
    assert verdict.fits and verdict.placement == "system"
    assert verdict.speed_band == "moderate"
    assert any("system memory" in w for w in verdict.warnings)


def test_fit_cpu_without_simd_is_slow():
    verdict = fit(model(size_gb=4.0),
                  vector(system_memory_gb=16, cpu_flags=[]))
    assert verdict.fits and verdict.placement == "system"
    assert verdict.speed_band == "slow"


def test_fit_tight_headroom_warns():
    # 12 GB required vs 13 GB available → <15% headroom
    verdict = fit(model(size_gb=9.3),
                  vector(system_memory_gb=13, cpu_flags=["avx2"]))
    assert verdict.fits
    assert any("tight fit" in w for w in verdict.warnings)


def test_fit_does_not_fit_is_advisory_not_blocking():
    verdict = fit(model(size_gb=40.0),
                  vector(accelerator="cuda", accel_memory_gb=8,
                         system_memory_gb=16))
    assert not verdict.fits and verdict.placement == "none"
    assert verdict.context_ceiling == 0
    assert any("override explicitly" in w for w in verdict.warnings)
    assert verdict.overridable  # fit informs, humans decide


def test_fit_unknown_size_is_honest_not_guessed():
    """No model-family heuristics: without declared bytes the verdict is
    'unknown', never an estimate from the name (ADR-026)."""
    verdict = fit(model(size_gb=0.0), vector(system_memory_gb=32))
    assert verdict.fits and verdict.placement == "unknown"
    assert verdict.speed_band == "unknown"
    assert any("size_gb=0" in w for w in verdict.warnings)


# ── detection plumbing (parsers pure; assembly injectable) ──────────────────


MEMINFO = "MemTotal:       32612344 kB\nMemFree:  1207044 kB\n"
CPUINFO = ("processor : 0\nflags : fpu vme avx avx2 fma sse4_2 "
           "clflush avx512f\n")


def test_parse_meminfo_and_cpu_flags():
    assert _parse_meminfo(MEMINFO) == 31.1
    assert _parse_meminfo("garbage") == 0.0
    assert _parse_cpu_flags(CPUINFO) == ["avx", "avx2", "avx512f", "fma"]
    assert _parse_cpu_flags("no flags line") == []


def test_detect_vector_assembles_injected_probes(monkeypatch):
    import agentd.capability as cap

    monkeypatch.setattr(cap, "_detect_accelerator", lambda: ("cuda", 24.0))
    monkeypatch.setattr(cap, "_system_memory_gb", lambda: 62.5)
    monkeypatch.setattr(cap, "_cpu_flags", lambda: ["avx2"])
    detected = detect_vector()
    assert detected.accelerator == "cuda"
    assert detected.accel_memory_gb == 24.0
    assert detected.system_memory_gb == 62.5
    assert detected.cpu_flags == ["avx2"]
    assert detected.cpu_cores >= 1
    assert classify(detected) == "accel-large"


def test_detect_vector_runs_on_this_host():
    """Smoke: detection never crashes and always yields a classifiable
    vector, whatever the host is."""
    detected = detect_vector()
    assert detected.accelerator in ("cuda", "rocm", "igpu", "none")
    assert classify(detected) in ("accel-large", "accel-small",
                                  "cpu-standard", "cpu-low")


# ── registry schema addition stays additive ──────────────────────────────────


def test_model_entry_size_gb_roundtrip_and_default():
    entry = ModelEntry.model_validate({"provider": "anyruntime",
                                       "size_gb": 4.2})
    assert entry.size_gb == 4.2
    assert ModelEntry(provider="anyruntime").size_gb == 0.0  # optional
