"""Model catalog — pluggable data + requirements-driven recommender (PR-4,
ADR-026 R-5; docs/MODEL_LIFECYCLE_MANAGEMENT.md §2, docs/FINAL_FIRST_RUN_
EXPERIENCE.md §2 rule 1, docs/HARDWARE_AGNOSTIC_ARCHITECTURE.md §3).

The catalog is **data with pluggable sources**: the packaged seed
(``defaults/catalog.yaml``) plus every ``<config>/catalog/*.yaml`` an
operator adds (same ids override). Entries state *requirements and
declared capabilities* — declared size per variant, context, chat
template, tool-call format, license — never hardware brands. A user-supplied
``hf:``/``gguf:`` reference is a first-class citizen: it becomes a registry
entry directly and never needs a catalog row.

The recommender answers ``auto``: for a group and this host's capability
vector it filters variants a runtime serves, checks the role contracts of
the group against the (model × runtime) pair, ranks by ``fit()`` placement
and declared size, and always returns the verdicts it reasoned from (F9).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from agentd.capability import CapabilityVector, FitVerdict, classify, fit
from agentd.logging_setup import get_logger
from agentd.registry_v2 import ModelEntry, RoleContract
from agentd.render import check_contract
from agentd.runtime_descriptor import RuntimeDescriptor

log = get_logger("catalog")

CATALOG_DIRNAME = "catalog"
CATALOG_VERSION = 1


class CatalogError(ValueError):
    """Invalid catalog data or an unsatisfiable recommendation."""


# ── schema ───────────────────────────────────────────────────────────────────


class CatalogVariant(BaseModel):
    """One artifact form of an entry, keyed by served format (hf, gguf, …)."""

    ref: str
    #: Declared artifact size (GiB) — the only sizing input fit() accepts;
    #: install replaces it with measured bytes.
    size_gb: float = 0.0
    quant: str = ""
    sha256: str = ""


class CatalogEntry(BaseModel):
    display_name: str = ""
    license: str = ""
    groups: list[str] = Field(default_factory=list)
    context: int = 0
    template: str = ""
    tool_call_format: str = ""
    notes: str = ""
    variants: dict[str, CatalogVariant]

    @model_validator(mode="after")
    def _check(self) -> CatalogEntry:
        if not self.variants:
            raise CatalogError("a catalog entry needs at least one variant")
        return self


class Catalog(BaseModel):
    version: int = CATALOG_VERSION
    entries: dict[str, CatalogEntry] = Field(default_factory=dict)

    def merged_with(self, other: Catalog) -> Catalog:
        """Later sources override earlier ones by id (operator > packaged)."""
        return Catalog(version=self.version,
                       entries={**self.entries, **other.entries})


def parse_catalog(text: str, origin: str) -> Catalog:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise CatalogError(f"{origin} must contain a YAML mapping")
    try:
        return Catalog.model_validate(data)
    except CatalogError:
        raise
    except Exception as exc:  # pydantic ValidationError → uniform error type
        raise CatalogError(f"{origin}: {exc}") from exc


def packaged_catalog() -> Catalog:
    """The seed shipped with the platform (data, not a blessed list)."""
    text = (resources.files("agentd") / "defaults" / "catalog.yaml").read_text(encoding="utf-8")
    return parse_catalog(text, "packaged catalog")


def catalog_dir(config_dir: Path) -> Path:
    return Path(config_dir) / CATALOG_DIRNAME


def load_catalog(config_dir: Path | None = None) -> Catalog:
    """Packaged seed + ``<config>/catalog/*.yaml`` (sorted; later files win)."""
    catalog = packaged_catalog()
    if config_dir is None:
        return catalog
    directory = catalog_dir(config_dir)
    if directory.is_dir():
        for path in sorted(directory.glob("*.yaml")):
            catalog = catalog.merged_with(
                parse_catalog(path.read_text(encoding="utf-8"), str(path)))
            log.debug("catalog source merged: %s", path)
    return catalog


# ── entries → registry models ────────────────────────────────────────────────


def provider_for_format(fmt: str, descriptors: dict[str, RuntimeDescriptor],
                        runtime: str | None = None) -> str | None:
    """Which runtime serves a format (PROVIDER_ABSTRACTION §4 rule 1). With a
    selected runtime the answer is that runtime or nothing."""
    if runtime is not None:
        descriptor = descriptors.get(runtime)
        return runtime if descriptor and fmt in descriptor.serves_formats else None
    for name in sorted(descriptors):
        if fmt in descriptors[name].serves_formats:
            return name
    return None


def entry_to_model(entry: CatalogEntry, fmt: str, provider: str) -> ModelEntry:
    variant = entry.variants[fmt]
    source = {fmt: variant.ref}
    if variant.sha256:
        source["sha256"] = variant.sha256
    return ModelEntry(provider=provider, source=source, groups=list(entry.groups),
                      context=entry.context, size_gb=variant.size_gb,
                      template=entry.template, tool_call_format=entry.tool_call_format,
                      license=entry.license)


def variant_for(entry: CatalogEntry, descriptors: dict[str, RuntimeDescriptor],
                runtime: str | None = None) -> tuple[str, str]:
    """(format, provider) of the variant a runtime can serve; loud otherwise."""
    for fmt in entry.variants:
        provider = provider_for_format(fmt, descriptors, runtime)
        if provider:
            return fmt, provider
    wanted = f"runtime '{runtime}'" if runtime else "any shipped runtime"
    raise CatalogError(
        f"no variant of this entry is served by {wanted} (variants: "
        f"{', '.join(entry.variants)}) — install a served variant or switch runtime")


# ── recommender (R-5) ────────────────────────────────────────────────────────

_PLACEMENT_RANK = {"accelerator": 0, "system": 1, "unknown": 2, "none": 3}


@dataclass
class Recommendation:
    catalog_id: str
    format: str
    provider: str
    model: ModelEntry
    verdict: FitVerdict
    contract_failures: list[str] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return self.verdict.fits and self.verdict.placement != "unknown" \
            and not self.contract_failures

    def explain(self) -> str:
        status = "eligible" if self.eligible else "rejected"
        reasons = list(self.contract_failures) + list(self.verdict.warnings)
        return (f"{self.catalog_id} [{self.format} → {self.provider}] {status}: "
                f"placement {self.verdict.placement}, {self.verdict.speed_band}, "
                f"~{self.model.size_gb:.1f} GB"
                + (" — " + "; ".join(reasons) if reasons else ""))


def recommend(catalog: Catalog, group: str, vector: CapabilityVector,
              descriptors: dict[str, RuntimeDescriptor], *,
              runtime: str | None = None,
              contracts: list[RoleContract] | None = None,
              capability_class: str | None = None,
              accelerator: str | None = None) -> list[Recommendation]:
    """Every candidate for a group, eligible ones first, best first. Ranking:
    accelerator placement before system memory, then larger declared size
    (a quality proxy without model-family knowledge), then larger context."""
    capability_class = capability_class or classify(vector)
    accelerator = accelerator or vector.accelerator
    candidates: list[Recommendation] = []
    for catalog_id in sorted(catalog.entries):
        entry = catalog.entries[catalog_id]
        if group not in entry.groups:
            continue
        for fmt in entry.variants:
            provider = provider_for_format(fmt, descriptors, runtime)
            if provider is None:
                continue
            model = entry_to_model(entry, fmt, provider)
            failures: list[str] = []
            for contract in contracts or []:
                failures += check_contract(catalog_id, model, descriptors[provider],
                                           contract, capability_class, accelerator)[1]
            candidates.append(Recommendation(
                catalog_id=catalog_id, format=fmt, provider=provider, model=model,
                verdict=fit(model, vector),
                contract_failures=sorted(set(failures))))
    candidates.sort(key=lambda r: (
        not r.eligible, _PLACEMENT_RANK.get(r.verdict.placement, 9),
        -r.model.size_gb, -r.model.context, r.catalog_id))
    return candidates


def recommend_one(catalog: Catalog, group: str, vector: CapabilityVector,
                  descriptors: dict[str, RuntimeDescriptor], **kwargs: Any) -> Recommendation:
    """The ``auto`` answer — or a loud explanation of why nothing fits."""
    ranked = recommend(catalog, group, vector, descriptors, **kwargs)
    for candidate in ranked:
        if candidate.eligible:
            return candidate
    detail = "\n- ".join(c.explain() for c in ranked) or "no catalog entry lists this group"
    raise CatalogError(
        f"no catalog model fits group '{group}' on this host "
        f"(class {kwargs.get('capability_class') or classify(vector)}):\n- {detail}\n"
        "Add an entry under config/catalog/, name a model explicitly "
        "(hf:… / gguf:…), or override the fit verdict deliberately.")
