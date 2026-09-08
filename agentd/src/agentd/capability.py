"""Hardware capability detection, classes, and fit() (PR-2, ADR-026 R-2).

Implements docs/HARDWARE_AGNOSTIC_ARCHITECTURE.md: **capabilities, not
SKUs**.

- ``CapabilityVector`` — what the host actually offers (accelerator KIND,
  memory budgets, cores, SIMD flags, disk). Kinds are API families
  (cuda / rocm / igpu / none), never brands or SKUs.
- ``classify(vector)`` — pure mapping to one of four capability classes
  (``accel-large`` / ``accel-small`` / ``cpu-standard`` / ``cpu-low``).
  Legacy profile names survive only as preset aliases
  (``PROFILE_PRESETS`` — e.g. the historical low-power profile maps to
  ``cpu-low``).
- ``fit(model, vector)`` — a pure, conservative function over a Registry
  v2 ``ModelEntry`` and a vector: fits where, expected speed band,
  warnings. It **never blocks** — enforcement callers must honor an
  explicit user override (agnosticism includes the freedom to run
  something slowly).

Agnosticism rules honored here (ADR-026):

- No model-family knowledge: sizing comes ONLY from declared data
  (``ModelEntry.size_gb``, filled at install time from actual bytes,
  PR-4) — never parsed or guessed from model names.
- No runtime knowledge: nothing here knows what serves the model.
- Hardware tool names appear ONLY in the declarative ``ACCELERATOR_PROBES``
  table below — detection must invoke each kind's canonical driver
  interface; the table is the sanctioned, gate-allowlisted location
  (H1 gate, PR-25), mirroring the runtime-descriptor carve-out.

No consumers yet: the renderer (PR-3) and the recommender/lifecycle
(PR-4) build on this module.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from agentd.logging_setup import get_logger
from agentd.registry_v2 import ModelEntry

log = get_logger("capability")

#: API-family kinds (docs/HARDWARE_AGNOSTIC_ARCHITECTURE.md §1) —
#: "kind, not brand".
AcceleratorKind = Literal["cuda", "rocm", "igpu", "none"]

CapabilityClass = Literal["accel-large", "accel-small", "cpu-standard", "cpu-low"]

#: Accelerator memory (GB) separating accel-large from accel-small.
ACCEL_LARGE_MIN_GB = 16.0
#: System memory (GB) separating cpu-standard from cpu-low.
CPU_STANDARD_MIN_GB = 16.0

#: Legacy profile names → capability class (compatibility data, H4).
#: ``None`` means "detect" — the profile only asserts an accelerator is
#: expected. This table is the only place legacy profile names map to
#: classes.
PROFILE_PRESETS: dict[str, CapabilityClass | None] = {
    "gpu": None,            # accel-large vs accel-small: measured, not assumed
    "cpu": "cpu-standard",
    "n97": "cpu-low",       # historical SKU profile, kept as an alias only
    "n97-igpu": "cpu-low",
}

#: Declarative probe table: (kind, binary, args, memory-line regex in MiB).
#: The ONLY sanctioned home for accelerator tool names outside runtime
#: descriptors (see module docstring).
ACCELERATOR_PROBES: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("cuda", "nvidia-smi",
     ("--query-gpu=memory.total", "--format=csv,noheader,nounits"),
     r"(\d+)"),
    ("rocm", "rocm-smi", ("--showmeminfo", "vram", "--csv"),
     r"(\d{6,})"),
)

#: Weights alone don't serve requests: runtime overhead + KV cache
#: headroom, expressed as one conservative multiplier over declared bytes.
MEMORY_OVERHEAD_FACTOR = 1.3
#: Below this free-headroom ratio a fit gets a "tight" warning.
HEADROOM_WARN_RATIO = 0.15


class CapabilityVector(BaseModel):
    """What this host offers — detected once, stored, never assumed."""

    accelerator: AcceleratorKind = "none"
    accel_memory_gb: float = 0.0
    system_memory_gb: float = 0.0
    cpu_cores: int = 0
    cpu_flags: list[str] = Field(default_factory=list)  # e.g. avx2, avx512f
    disk_free_gb: float = 0.0


class FitVerdict(BaseModel):
    """Where (and how well) a model runs on this vector. Advisory only:
    ``overridable`` is always True — fit informs, humans decide."""

    fits: bool
    placement: Literal["accelerator", "system", "none", "unknown"]
    required_memory_gb: float = 0.0
    available_memory_gb: float = 0.0
    speed_band: Literal["fast", "moderate", "slow", "unknown"]
    context_ceiling: int = 0
    warnings: list[str] = Field(default_factory=list)
    overridable: bool = True


# ── classification (pure) ────────────────────────────────────────────────────


def classify(vector: CapabilityVector) -> CapabilityClass:
    """Capability class from a vector — thresholds, no brands."""
    if vector.accelerator != "none":
        return ("accel-large" if vector.accel_memory_gb >= ACCEL_LARGE_MIN_GB
                else "accel-small")
    return ("cpu-standard" if vector.system_memory_gb >= CPU_STANDARD_MIN_GB
            else "cpu-low")


def class_for_profile(profile: str,
                      vector: CapabilityVector | None = None) -> CapabilityClass:
    """Resolve a legacy profile name to a class (preset aliases); the
    detect-marked profile falls through to classification."""
    if profile not in PROFILE_PRESETS:
        raise ValueError(
            f"unknown profile '{profile}' — known: "
            + ", ".join(sorted(PROFILE_PRESETS)))
    preset = PROFILE_PRESETS[profile]
    if preset is not None:
        return preset
    return classify(vector or detect_vector())


# ── fit (pure, conservative, never blocking) ─────────────────────────────────


def fit(model: ModelEntry, vector: CapabilityVector) -> FitVerdict:
    """Conservative placement verdict for a declared model on a vector.

    Sizing comes exclusively from ``model.size_gb`` (declared/measured
    data). Unknown size → an honest ``unknown`` verdict with a warning,
    never a guess from the model's name or family.
    """
    if model.size_gb <= 0:
        return FitVerdict(
            fits=True,
            placement="unknown",
            speed_band="unknown",
            context_ceiling=model.context,
            warnings=["declared size missing (size_gb=0) — install records "
                      "it; verdict is advisory only"],
        )

    required = round(model.size_gb * MEMORY_OVERHEAD_FACTOR, 2)
    warnings: list[str] = []

    def tightness(available: float) -> None:
        if available > required and (available - required) / available \
                < HEADROOM_WARN_RATIO:
            warnings.append(
                f"tight fit: {available - required:.1f} GB headroom for "
                "context/KV cache — expect a reduced context ceiling")

    if vector.accelerator != "none" and vector.accel_memory_gb >= required:
        tightness(vector.accel_memory_gb)
        return FitVerdict(
            fits=True, placement="accelerator",
            required_memory_gb=required,
            available_memory_gb=vector.accel_memory_gb,
            speed_band="fast", context_ceiling=model.context,
            warnings=warnings,
        )

    if vector.system_memory_gb >= required:
        if vector.accelerator != "none":
            warnings.append(
                "does not fit accelerator memory — will run from system "
                "memory")
        tightness(vector.system_memory_gb)
        has_simd = any(flag.startswith("avx") for flag in vector.cpu_flags)
        return FitVerdict(
            fits=True, placement="system",
            required_memory_gb=required,
            available_memory_gb=vector.system_memory_gb,
            speed_band="moderate" if has_simd else "slow",
            context_ceiling=model.context,
            warnings=warnings,
        )

    available = max(vector.accel_memory_gb, vector.system_memory_gb)
    return FitVerdict(
        fits=False, placement="none",
        required_memory_gb=required,
        available_memory_gb=available,
        speed_band="slow", context_ceiling=0,
        warnings=[
            f"needs ~{required:.1f} GB (declared {model.size_gb:.1f} GB × "
            f"{MEMORY_OVERHEAD_FACTOR} overhead) but at most "
            f"{available:.1f} GB is available — choose a smaller "
            "quantization, or override explicitly to try anyway"],
    )


# ── detection (best-effort, injectable) ──────────────────────────────────────


def detect_vector() -> CapabilityVector:
    """Best-effort host detection from generic surfaces. Every probe is
    isolated in a small helper so tests (and unusual hosts) can override;
    anything undetectable stays 0/none — ``classify`` then errs
    conservative (cpu-low)."""
    kind, accel_gb = _detect_accelerator()
    return CapabilityVector(
        accelerator=kind,
        accel_memory_gb=accel_gb,
        system_memory_gb=_system_memory_gb(),
        cpu_cores=os.cpu_count() or 0,
        cpu_flags=_cpu_flags(),
        disk_free_gb=round(shutil.disk_usage(".").free / 1024**3, 1),
    )


def _detect_accelerator() -> tuple[AcceleratorKind, float]:
    for kind, binary, args, pattern in ACCELERATOR_PROBES:
        path = shutil.which(binary)
        if not path:
            continue
        try:
            proc = subprocess.run([path, *args], capture_output=True,
                                  text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode != 0:
            continue
        match = re.search(pattern, proc.stdout)
        mib = float(match.group(1)) if match else 0.0
        return kind, round(mib / 1024, 1)  # type: ignore[return-value]
    # No dedicated-accelerator tool: a render node implies an iGPU-class
    # device (its memory is shared with the system → 0 dedicated).
    if Path("/dev/dri").is_dir() and any(Path("/dev/dri").iterdir()):
        return "igpu", 0.0
    return "none", 0.0


def _system_memory_gb() -> float:
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        return _parse_meminfo(meminfo.read_text(encoding="utf-8"))
    try:  # POSIX without /proc (e.g. macOS)
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return round(pages * page_size / 1024**3, 1)
    except (ValueError, OSError, AttributeError):
        return 0.0  # unknown → classify errs conservative


def _cpu_flags() -> list[str]:
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        return _parse_cpu_flags(cpuinfo.read_text(encoding="utf-8"))
    return []


# pure parsers (unit-testable without hosts)


def _parse_meminfo(text: str) -> float:
    match = re.search(r"^MemTotal:\s+(\d+)\s*kB", text, re.MULTILINE)
    return round(int(match.group(1)) / 1024**2, 1) if match else 0.0


def _parse_cpu_flags(text: str) -> list[str]:
    match = re.search(r"^(?:flags|Features)\s*:\s*(.+)$", text, re.MULTILINE)
    if not match:
        return []
    interesting = {"avx", "avx2", "avx512f", "neon", "sve", "f16c", "fma"}
    return sorted(set(match.group(1).split()) & interesting)
