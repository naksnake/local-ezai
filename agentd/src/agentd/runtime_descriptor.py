"""Runtime descriptors — the Provider Abstraction Layer's plug-in seam
(PR-3, ADR-026 R-3/R-4, docs/RUNTIME_ABSTRACTION_STRATEGY.md §2,
docs/PROVIDER_ABSTRACTION.md §2).

A runtime (either shipped engine, or a future one) is a YAML descriptor
under ``<config>/providers/<runtime>.yaml`` plus container images. The
descriptor is the **single sanctioned home** for runtime and hardware
knowledge: image per accelerator kind, engine flags, device wiring,
tuning per capability class. This module only knows the *shape* of a
descriptor — it never knows a runtime.

Six-verb contract (RUNTIME_ABSTRACTION §2):

- ``materialize`` and ``capabilities`` are pure data → realized by the
  renderer (``render.py``) in this PR;
- ``control`` (start/stop/restart), ``ready``, ``validate_model``, ``bench``
  are declared here as data the lifecycle manager (PR-4/PR-5) consumes.

Templates: string values may contain ``{placeholder}`` tokens that the
renderer fills from a documented context (builtins such as ``{port}``,
``{cpu_cores}``, ``{served_count}``; per-model values such as
``{model_name}``, ``{model_path}``, ``{model_ctx}``,
``{tool_call_parser}``; and every key of the merged ``tuning`` table).
Compose interpolation (``${VAR:-default}``) passes through untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from agentd.logging_setup import get_logger

log = get_logger("runtime_descriptor")

PROVIDERS_DIRNAME = "providers"
DESCRIPTOR_VERSION = 1

#: Accelerator kinds a descriptor may carry images for — the API families
#: of capability.AcceleratorKind (kind, never brand).
ACCELERATOR_KINDS = ("cuda", "rocm", "igpu", "none")
#: Capability classes a descriptor may tune for (capability.CapabilityClass).
CAPABILITY_CLASSES = ("accel-large", "accel-small", "cpu-standard", "cpu-low")

#: Tuning keys every descriptor must define (the renderer's contract with
#: descriptor data — see ``render.py``).
REQUIRED_TUNING_KEYS = ("ctx_size",)


class DescriptorError(ValueError):
    """Invalid or missing runtime descriptor."""


def _scalar_text(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    if value is None:
        return ""
    if isinstance(value, bool):  # engine flags/INI keys want lowercase booleans
        return "true" if value else "false"
    return str(value)


def _stringify_scalars(value: Any) -> Any:
    """Tuning tables are ``key → template string``; YAML authors write
    numbers and booleans naturally, so scalars are normalized to strings."""
    if isinstance(value, dict):
        return {str(k): _scalar_text(v) for k, v in value.items()}
    return value


# ── schema ───────────────────────────────────────────────────────────────────


class Capabilities(BaseModel):
    """What the runtime can do — negotiation input (RUNTIME_ABSTRACTION §4)."""

    #: ``ModelEntry.tool_call_format`` values this runtime can parse.
    tool_call_parsers: list[str] = Field(default_factory=list)
    json_output: bool = False
    #: Several models behind one engine slot (requires ``materialize.multi``).
    parallel_models: bool = False
    hot_swap: bool = False


class ControlVerb(BaseModel):
    """start / stop / restart — how the slot process is driven (PR-4/5)."""

    kind: str = "compose"


class ReadyVerb(BaseModel):
    """``ready?`` — readiness probe data (also rendered as the compose
    healthcheck)."""

    path: str = "/health"
    timeout_s: int = 600
    interval_s: int = 30
    start_period_s: int = 30
    retries: int = 3
    #: Compose healthcheck test command (template; ``{port}``/``{path}``).
    healthcheck: list[str] = Field(default_factory=list)


class ValidateVerb(BaseModel):
    probe: str = "chat_completion"
    max_tokens: int = 1


class BenchVerb(BaseModel):
    max_tokens: int = 120
    #: Response field carrying server-side timings ("" → measure client-side).
    timings_field: str = ""


class Verbs(BaseModel):
    control: ControlVerb = Field(default_factory=ControlVerb)
    ready: ReadyVerb = Field(default_factory=ReadyVerb)
    validate_model: ValidateVerb = Field(default_factory=ValidateVerb)
    bench: BenchVerb = Field(default_factory=BenchVerb)


class WeightsMount(BaseModel):
    """Where installed weights live on the host and inside the engine."""

    host_dir: str
    container_dir: str
    read_only: bool = True


class PresetSpec(BaseModel):
    """Multi-model form: an INI preset file rendered per generation —
    ``[*]`` common keys + one ``[<model_name>]`` section per served model."""

    container_path: str
    common: dict[str, str] = Field(default_factory=dict)
    per_model: dict[str, str] = Field(default_factory=dict)

    _norm = field_validator("common", "per_model", mode="before")(_stringify_scalars)


class SingleForm(BaseModel):
    """Exactly one served model: the classic command line."""

    command: list[str]
    #: Appended only when the served model negotiated tool calling.
    tool_calling_args: list[str] = Field(default_factory=list)


class MultiForm(BaseModel):
    """Several served models behind one slot (per-model settings, tool
    calling included, live in the preset sections)."""

    command: list[str]
    preset: PresetSpec


class Materialize(BaseModel):
    """``materialize(model_set, vector) → engine spec`` as data."""

    #: The id the engine answers to for a model (LiteLLM routes to it).
    served_id: str = "{model_name}"
    single: SingleForm
    multi: MultiForm | None = None


class AcceleratorEntry(BaseModel):
    """The (runtime × accelerator-kind) row of HARDWARE_AGNOSTIC §2."""

    image: str
    #: Extra command args for this accelerator (single form).
    args: list[str] = Field(default_factory=list)
    #: Extra ``[*]`` preset keys for this accelerator (multi form).
    preset: dict[str, str] = Field(default_factory=dict)
    #: Tuning overrides for this accelerator.
    tuning: dict[str, str] = Field(default_factory=dict)
    #: Verbatim compose service extras (deploy/devices/cap_add/…), templated.
    compose: dict[str, Any] = Field(default_factory=dict)

    _norm = field_validator("preset", "tuning", mode="before")(_stringify_scalars)


class RuntimeDescriptor(BaseModel):
    """One runtime, fully described as data."""

    runtime: str
    version: int = DESCRIPTOR_VERSION
    display_name: str = ""
    serves_formats: list[str]
    capabilities: Capabilities = Field(default_factory=Capabilities)
    verbs: Verbs = Field(default_factory=Verbs)
    weights: WeightsMount
    materialize: Materialize
    #: Runtime-level compose service extras (environment, shm_size, volumes…).
    service: dict[str, Any] = Field(default_factory=dict)
    #: Default tuning table; ``classes[<class>]`` then the accelerator's
    #: ``tuning`` override it (later wins).
    tuning: dict[str, str] = Field(default_factory=dict)
    classes: dict[str, dict[str, str]] = Field(default_factory=dict)
    accelerators: dict[str, AcceleratorEntry]

    _norm = field_validator("tuning", mode="before")(_stringify_scalars)

    @field_validator("classes", mode="before")
    @classmethod
    def _norm_classes(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: _stringify_scalars(v) for k, v in value.items()}
        return value

    @model_validator(mode="after")
    def _check(self) -> RuntimeDescriptor:
        problems: list[str] = []
        if not self.runtime or not self.runtime.replace("-", "").replace("_", "").isalnum():
            problems.append(f"runtime id '{self.runtime}' must be alphanumeric")
        if not self.serves_formats:
            problems.append("serves_formats must list at least one format")
        if not self.accelerators:
            problems.append("accelerators must contain at least one kind")
        for kind in self.accelerators:
            if kind not in ACCELERATOR_KINDS:
                problems.append(
                    f"unknown accelerator kind '{kind}' (known: "
                    f"{', '.join(ACCELERATOR_KINDS)})")
        for name in self.classes:
            if name not in CAPABILITY_CLASSES:
                problems.append(
                    f"unknown capability class '{name}' (known: "
                    f"{', '.join(CAPABILITY_CLASSES)})")
        for key in REQUIRED_TUNING_KEYS:
            if key not in self.tuning:
                problems.append(f"tuning.{key} is required")
        if self.capabilities.parallel_models and self.materialize.multi is None:
            problems.append(
                "capabilities.parallel_models is true but materialize.multi "
                "is missing")
        if not self.capabilities.parallel_models and self.materialize.multi is not None:
            problems.append(
                "materialize.multi is declared but capabilities.parallel_models "
                "is false")
        if problems:
            raise DescriptorError(
                f"descriptor '{self.runtime}': " + "; ".join(problems))
        return self

    def tuning_for(self, capability_class: str, accelerator: str) -> dict[str, str]:
        """Merged tuning table: defaults ← class ← accelerator."""
        merged = dict(self.tuning)
        merged.update(self.classes.get(capability_class, {}))
        entry = self.accelerators.get(accelerator)
        if entry is not None:
            merged.update(entry.tuning)
        return merged

    def source_for(self, source: dict[str, str]) -> tuple[str, str] | None:
        """The (format, reference) of a model source this runtime serves —
        a model may declare several variants; the runtime picks its own."""
        for fmt in self.serves_formats:
            if source.get(fmt):
                return fmt, source[fmt]
        return None


# ── loading ──────────────────────────────────────────────────────────────────


def providers_dir(config_dir: Path) -> Path:
    return Path(config_dir) / PROVIDERS_DIRNAME


def descriptor_path(config_dir: Path, runtime: str) -> Path:
    return providers_dir(config_dir) / f"{runtime}.yaml"


def parse_descriptor(text: str, origin: str) -> RuntimeDescriptor:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise DescriptorError(f"{origin} must contain a YAML mapping")
    try:
        return RuntimeDescriptor.model_validate(data)
    except DescriptorError:
        raise
    except Exception as exc:  # pydantic ValidationError → uniform error type
        raise DescriptorError(f"{origin}: {exc}") from exc


def load_descriptor(path: Path) -> RuntimeDescriptor:
    path = Path(path)
    if not path.is_file():
        raise DescriptorError(f"no runtime descriptor at {path}")
    descriptor = parse_descriptor(path.read_text(encoding="utf-8"), str(path))
    if descriptor.runtime != path.stem:
        raise DescriptorError(
            f"{path}: file name '{path.stem}' must equal runtime id "
            f"'{descriptor.runtime}'")
    return descriptor


def load_descriptors(config_dir: Path) -> dict[str, RuntimeDescriptor]:
    """Every descriptor under ``<config>/providers/``, keyed by runtime id."""
    directory = providers_dir(config_dir)
    if not directory.is_dir():
        raise DescriptorError(
            f"no runtime descriptors at {directory} — the platform ships "
            "config/providers/*.yaml")
    descriptors: dict[str, RuntimeDescriptor] = {}
    for path in sorted(directory.glob("*.yaml")):
        descriptors[path.stem] = load_descriptor(path)
    if not descriptors:
        raise DescriptorError(f"{directory} contains no *.yaml descriptors")
    log.debug("runtime descriptors loaded: %s", ", ".join(descriptors))
    return descriptors
