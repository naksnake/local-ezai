"""Registry v2 — the platform-scope model registry (PR-1, ADR-027).

Schema, store, generations, and deterministic resolution for the
productization architecture (docs/MODEL_ROUTING_DESIGN.md,
docs/MODEL_GOVERNANCE_V2.md):

    ROLE ──► (pin chain | GROUP order) ──► first ACTIVE model = primary,
                                           the rest of the order = fallbacks

This module has **no consumers yet** — PR-6 binds the runtime to it and
PR-7 bootstraps the on-disk instance (``config/models/registry.yaml``).
The per-repo ADR-020 registry (`model_registry.py`) is untouched and keeps
override precedence.

Resolution semantics (as-built refinement over MODEL_ROUTING_DESIGN §4,
forced by the golden test):

- a role WITHOUT a pin resolves through its group's ordered member list;
- a role WITH a pin resolves through the pin chain **exactly** — a pin is
  an explicit ordered chain (string or list) and does NOT inherit group
  fallbacks. What you pin is what you get; this is what makes the
  CLAUDE.md ``agent_model_map`` (e.g. ``reviewer: llama3`` with no
  fallback) reproducible byte-for-byte.
- models in any state other than ``active`` are invisible to resolution;
- an unresolvable role fails loudly at resolution time, never silently at
  request time.

Model names live in DATA (the packaged reference registry, user
registries), never in code — ADR-026 R-1/H1 discipline.
"""

from __future__ import annotations

import time
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from agentd.logging_setup import get_logger

log = get_logger("registry_v2")

REGISTRY_VERSION = 2
REGISTRY_DIRNAME = "models"
REGISTRY_FILENAME = "registry.yaml"
GENERATIONS_DIRNAME = "generations"

#: Lifecycle states (docs/MODEL_LIFECYCLE_MANAGEMENT.md §1). Only
#: ``active`` is visible to resolution.
ModelState = Literal[
    "registered", "installed", "benchmarked", "active", "retired", "failed",
]
SERVING_STATE: ModelState = "active"


class RegistryError(ValueError):
    """Invalid registry content (schema or referential integrity)."""


class RegistryResolutionError(RegistryError):
    """A role (or several) cannot be served by any active model."""


# ── schema ───────────────────────────────────────────────────────────────────


class RoleContract(BaseModel):
    """Capability requirements a resolved model+runtime must satisfy
    (docs/MODEL_GOVERNANCE_V2.md §3). Enforced at render time from PR-3
    onward; carried as data from PR-1."""

    tool_calling: bool = False
    json_output: bool = False
    min_context: int = 0


class ModelEntry(BaseModel):
    """One concrete model artifact known to the platform."""

    #: Runtime id (a provider descriptor name, e.g. "llamacpp", "vllm").
    provider: str
    #: Source reference, e.g. {"hf": "...repo..."} or {"gguf": "url|path"}.
    source: dict[str, str] = Field(default_factory=dict)
    #: Group memberships (informational here; ordering lives in `groups`).
    groups: list[str] = Field(default_factory=list)
    context: int = 0
    state: ModelState = "registered"
    #: Declared artifact size in GB — measured from actual bytes at install
    #: time (PR-4), consumed by capability.fit() (PR-2). Never inferred
    #: from the model's name or family (ADR-026). 0 = unknown.
    size_gb: float = 0.0
    #: Chat-template / tool-call metadata for capability negotiation (PR-3).
    template: str = ""
    tool_call_format: str = ""
    #: Latest measurements (filled by the lifecycle manager, PR-4).
    benchmarks: dict[str, Any] = Field(default_factory=dict)


class RoleSpec(BaseModel):
    """How a logical role resolves to models."""

    group: str
    #: Explicit ordered chain; overrides the group entirely (see module
    #: docstring). Accepts a single string in YAML; normalized to a list.
    pin: list[str] = Field(default_factory=list)
    requires: RoleContract = Field(default_factory=RoleContract)

    @field_validator("pin", mode="before")
    @classmethod
    def _pin_as_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value


class Resolution(BaseModel):
    """The answer to "what serves role X, and why" (explain-ready)."""

    role: str
    primary: str
    fallbacks: list[str] = Field(default_factory=list)
    generation: int = 0
    #: Human-readable explanation lines (source of the order, skips).
    reason: list[str] = Field(default_factory=list)


class RegistryV2(BaseModel):
    """The platform model registry — one generation of routing state."""

    version: Literal[2] = REGISTRY_VERSION
    generation: int = 0
    saved_at: str = ""
    note: str = ""
    providers: list[str] = Field(default_factory=list)
    models: dict[str, ModelEntry] = Field(default_factory=dict)
    #: group name → ordered member list (order = priority = fallback order).
    groups: dict[str, list[str]] = Field(default_factory=dict)
    roles: dict[str, RoleSpec] = Field(default_factory=dict)

    # ── referential integrity ────────────────────────────────────────────

    @model_validator(mode="after")
    def _check_references(self) -> RegistryV2:
        problems: list[str] = []
        for group, members in self.groups.items():
            for member in members:
                if member not in self.models:
                    problems.append(
                        f"group '{group}' references unknown model '{member}'")
        for role, spec in self.roles.items():
            if spec.group not in self.groups:
                problems.append(
                    f"role '{role}' references unknown group '{spec.group}'")
            for pinned in spec.pin:
                if pinned not in self.models:
                    problems.append(
                        f"role '{role}' pins unknown model '{pinned}'")
        if self.providers:
            for name, entry in self.models.items():
                if entry.provider not in self.providers:
                    problems.append(
                        f"model '{name}' uses undeclared provider "
                        f"'{entry.provider}'")
        if problems:
            raise RegistryError(
                "registry integrity: " + "; ".join(problems))
        return self

    # ── resolution ───────────────────────────────────────────────────────

    def resolve(self, role: str) -> Resolution:
        """Deterministic role resolution (module docstring semantics)."""
        spec = self.roles.get(role)
        if spec is None:
            raise RegistryResolutionError(
                f"role '{role}' is not defined in the registry "
                f"(generation {self.generation})")
        if spec.pin:
            order = list(spec.pin)
            reason = [f"pin chain: {', '.join(order)} "
                      "(explicit — group fallbacks not inherited)"]
        else:
            order = list(self.groups[spec.group])
            reason = [f"group '{spec.group}' order: {', '.join(order)}"]

        serving: list[str] = []
        for name in order:
            state = self.models[name].state
            if state == SERVING_STATE:
                serving.append(name)
            else:
                reason.append(f"skipped {name} (state: {state})")
        if not serving:
            raise RegistryResolutionError(
                f"role '{role}' has no active model: candidates "
                + ", ".join(f"{n} ({self.models[n].state})" for n in order)
                + f" — activate a model in group '{spec.group}' "
                  "(or fix the pin) before this generation can serve")
        return Resolution(
            role=role,
            primary=serving[0],
            fallbacks=serving[1:],
            generation=self.generation,
            reason=reason,
        )

    def resolve_all(self) -> dict[str, Resolution]:
        """Resolution-completeness check: every role must be servable.
        Raises one aggregated, loud error naming every failing role."""
        resolutions: dict[str, Resolution] = {}
        failures: list[str] = []
        for role in self.roles:
            try:
                resolutions[role] = self.resolve(role)
            except RegistryResolutionError as exc:
                failures.append(str(exc))
        if failures:
            raise RegistryResolutionError(
                f"{len(failures)} role(s) unresolvable:\n- "
                + "\n- ".join(failures))
        return resolutions

    def role_map(self) -> tuple[dict[str, str], dict[str, list[str]]]:
        """The exact shapes the runtime consumes (ADR-020 compatible):
        ``roles[role] = primary`` and ``role_fallbacks[role] = [...]`` —
        fallback entries only when non-empty, matching
        ``apply_model_registry`` behavior byte-for-byte."""
        resolutions = self.resolve_all()
        roles = {role: r.primary for role, r in resolutions.items()}
        fallbacks = {role: r.fallbacks for role, r in resolutions.items()
                     if r.fallbacks}
        return roles, fallbacks


# ── store: load / save / generations ─────────────────────────────────────────


def registry_dir(config_dir: Path) -> Path:
    return Path(config_dir) / REGISTRY_DIRNAME


def registry_path(config_dir: Path) -> Path:
    return registry_dir(config_dir) / REGISTRY_FILENAME


def generations_dir(config_dir: Path) -> Path:
    return registry_dir(config_dir) / GENERATIONS_DIRNAME


def _generation_path(config_dir: Path, number: int) -> Path:
    return generations_dir(config_dir) / f"gen-{number:04d}.yaml"


def _parse(text: str, origin: str) -> RegistryV2:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise RegistryError(f"{origin} must contain a YAML mapping")
    try:
        return RegistryV2.model_validate(data)
    except RegistryError:
        raise
    except Exception as exc:  # pydantic ValidationError → uniform error type
        raise RegistryError(f"{origin}: {exc}") from exc


def load_registry(config_dir: Path) -> RegistryV2:
    """The current registry (``<config>/models/registry.yaml``)."""
    path = registry_path(config_dir)
    if not path.is_file():
        raise RegistryError(
            f"no registry at {path} — the platform bootstrap "
            "(generation 1) has not run")
    return _parse(path.read_text(encoding="utf-8"), str(path))


def save_generation(
    registry: RegistryV2, config_dir: Path, note: str = ""
) -> RegistryV2:
    """Persist the registry as the NEXT generation.

    Bumps ``generation``, stamps ``saved_at``/``note``, writes an
    immutable snapshot under ``generations/`` and atomically replaces
    ``registry.yaml``. Returns the saved copy. Generations are
    append-only: an existing snapshot file is never overwritten.
    """
    saved = registry.model_copy(deep=True)
    saved.generation = registry.generation + 1
    saved.saved_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    saved.note = note
    saved.resolve_all()  # never persist an unservable generation

    snapshot = _generation_path(config_dir, saved.generation)
    if snapshot.exists():
        raise RegistryError(
            f"generation {saved.generation} already exists at {snapshot} — "
            "generations are immutable")
    generations_dir(config_dir).mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(saved.model_dump(mode="json"), sort_keys=False)
    snapshot.write_text(text, encoding="utf-8")

    current = registry_path(config_dir)
    tmp = current.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(current)  # atomic on POSIX and Windows
    log.info("registry generation %d saved (%s)", saved.generation,
             note or "no note")
    return saved


def list_generations(config_dir: Path) -> list[int]:
    directory = generations_dir(config_dir)
    if not directory.is_dir():
        return []
    numbers = []
    for path in directory.glob("gen-*.yaml"):
        try:
            numbers.append(int(path.stem.split("-")[1]))
        except (IndexError, ValueError):
            continue
    return sorted(numbers)


def load_generation(config_dir: Path, number: int) -> RegistryV2:
    path = _generation_path(config_dir, number)
    if not path.is_file():
        raise RegistryError(f"generation {number} not found at {path}")
    return _parse(path.read_text(encoding="utf-8"), str(path))


# ── diff (human-readable, powers `model history` and approval modals) ───────


def diff_generations(old: RegistryV2, new: RegistryV2) -> list[str]:
    """What changed between two generations, one line per change."""
    lines: list[str] = []
    for name in sorted(set(old.models) | set(new.models)):
        before, after = old.models.get(name), new.models.get(name)
        if before is None:
            lines.append(f"model added: {name} "
                         f"({after.provider}, state {after.state})")
        elif after is None:
            lines.append(f"model removed: {name}")
        elif before.state != after.state:
            lines.append(f"model {name}: state {before.state} → {after.state}")
    for group in sorted(set(old.groups) | set(new.groups)):
        before_order = old.groups.get(group)
        after_order = new.groups.get(group)
        if before_order != after_order:
            lines.append(
                f"group {group}: [{', '.join(before_order or [])}] → "
                f"[{', '.join(after_order or [])}]")
    for role in sorted(set(old.roles) | set(new.roles)):
        before_spec, after_spec = old.roles.get(role), new.roles.get(role)
        if before_spec is None:
            lines.append(f"role added: {role} → group {after_spec.group}")
        elif after_spec is None:
            lines.append(f"role removed: {role}")
        else:
            if before_spec.group != after_spec.group:
                lines.append(f"role {role}: group {before_spec.group} → "
                             f"{after_spec.group}")
            if before_spec.pin != after_spec.pin:
                lines.append(
                    f"role {role}: pin [{', '.join(before_spec.pin)}] → "
                    f"[{', '.join(after_spec.pin)}]")
    if old.providers != new.providers:
        lines.append(f"providers: {old.providers} → {new.providers}")
    return lines


# ── reference default data set (CLAUDE.md agent_model_map, as data) ─────────


def reference_registry() -> RegistryV2:
    """The packaged reference default set — the CLAUDE.md
    ``agent_model_map`` expressed as Registry v2 data. Model names live
    in ``defaults/reference_registry.yaml``, never in code (ADR-026 R-1)."""
    text = (resources.files("agentd") / "defaults" / "reference_registry.yaml") \
        .read_text(encoding="utf-8")
    return _parse(text, "packaged reference registry")
