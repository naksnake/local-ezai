"""Bootstrap — ``.env`` seeds → generation 1 (PR-7, ADR-027 closes;
docs/FINAL_FIRST_RUN_EXPERIENCE.md §2–§3, docs/V1_PR_PLAN.md PR-7).

The one time a human names models: ``.env`` seeds the three groups and
the runtime, once::

    AI_RUNTIME=llamacpp|vllm
    REASONING_MODEL=hf:<org/repo> | gguf:<url|hf://org/repo/file|path> | <catalog id> | auto
    CODING_MODEL=…    CHAT_MODEL=…
    EZAI_ROLE_PIN_<role>=<seeded model name>          # optional

Pipeline (FINAL_FRE §3 steps 2, 4–5 — the core; install.sh/make setup wrap it):

    read_seeds   .env text → Seeds (legacy families migrated, F11)
    validate     every problem printed WITH its fix before anything is
                 downloaded (F8): runtime, scheme, format × runtime, slot
                 capacity, `auto` feasibility, pins
    plan         the generation-1 registry: reference roles + contracts,
                 groups from the seeds, pins
    bootstrap    install (fetch + validate_model) → benchmark → the ONE
                 implicit approval → apply (render, reload, health,
                 self-rollback) → stamp the seeds as consumed

Read once: after bootstrap ``EZAI_SEEDS_CONSUMED`` is stamped into ``.env``
and the seeds are never read again; day-2 changes go through
``local-ezai model …`` (MODEL_LIFECYCLE §5).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from agentd import activation, lifecycle
from agentd.activation import Platform
from agentd.catalog import Catalog, CatalogError, recommend_one
from agentd.fetch import SourceError, SourceRef, parse_source
from agentd.governance import GovernanceQueue
from agentd.lifecycle import LifecycleError, Validator
from agentd.logging_setup import get_logger
from agentd.registry_v2 import (
    RegistryV2,
    RoleSpec,
    diff_generations,
    load_registry,
    reference_registry,
    registry_path,
)
from agentd.runtime_descriptor import RuntimeDescriptor

log = get_logger("bootstrap")

RUNTIME_KEY = "AI_RUNTIME"
SEED_GROUPS: dict[str, str] = {"REASONING_MODEL": "reasoning", "CODING_MODEL": "coding",
                               "CHAT_MODEL": "chat"}
PIN_PREFIX = "EZAI_ROLE_PIN_"
CONSUMED_KEY = "EZAI_SEEDS_CONSUMED"
#: Optional per-seed declarations for user-supplied sources (catalog entries
#: carry their own): ``<SEED>_TOOL_FORMAT`` (tool-call format the runtime must
#: parse), ``<SEED>_CONTEXT`` (context length to serve at).
TOOL_FORMAT_SUFFIX = "_TOOL_FORMAT"
CONTEXT_SUFFIX = "_CONTEXT"
#: The template-driven tool-call handler a runtime may list; undeclared
#: user sources default to it when the runtime supports it.
GENERIC_TOOL_FORMAT = "generic"
_BARE_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


class BootstrapError(ValueError):
    """The seeds cannot become a generation; the message lists every fix."""


# ── .env parsing ─────────────────────────────────────────────────────────────


def parse_env(text: str) -> dict[str, str]:
    """``KEY=value`` lines (quotes stripped, comments ignored) → mapping."""
    env: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        env[key] = value
    return env


def legacy_families() -> list[dict[str, Any]]:
    text = (resources.files("agentd") / "defaults" / "legacy_seeds.yaml").read_text(
        encoding="utf-8")
    return list((yaml.safe_load(text) or {}).get("families", []))


# ── seeds ────────────────────────────────────────────────────────────────────


@dataclass
class ModelSeed:
    key: str          # the .env variable
    group: str
    ref: str          # a reference parse_source accepts
    name: str | None = None
    context: int = 0
    tool_call_format: str = ""
    origin: str = "seed"  # "seed" | "legacy:<family id>"


@dataclass
class Seeds:
    runtime: str | None
    models: dict[str, ModelSeed]      # group → seed
    pins: dict[str, str] = field(default_factory=dict)  # role → seeded model name
    consumed: str | None = None
    migrated_from: str | None = None
    problems: list[str] = field(default_factory=list)

    def distinct_refs(self) -> list[str]:
        seen: list[str] = []
        for seed in self.models.values():
            if seed.ref not in seen:
                seen.append(seed.ref)
        return seen


def _is_new_form(value: str) -> bool:
    """A V1 seed value: scheme-prefixed, `auto`, or a catalog id — never a
    bare ``org/repo`` (the legacy form)."""
    if not value or _BARE_REPO.match(value):
        return False
    try:
        parse_source(value)
    except SourceError:
        return False
    return True


def read_seeds(env: Mapping[str, str]) -> Seeds:
    """The seeds of an ``.env`` mapping: V1 form when present, otherwise the
    first legacy family (F11) translated into one model for all groups."""
    seeds = Seeds(runtime=(env.get(RUNTIME_KEY) or "").strip() or None, models={},
                  consumed=(env.get(CONSUMED_KEY) or "").strip() or None)
    for key, group in SEED_GROUPS.items():
        value = (env.get(key) or "").strip()
        if value and _is_new_form(value):
            context_text = (env.get(key + CONTEXT_SUFFIX) or "").strip()
            seeds.models[group] = ModelSeed(
                key, group, value,
                context=int(context_text) if context_text.isdigit() else 0,
                tool_call_format=(env.get(key + TOOL_FORMAT_SUFFIX) or "").strip())
    if seeds.models:
        for key, group in SEED_GROUPS.items():
            if group not in seeds.models:
                value = (env.get(key) or "").strip()
                what = "missing" if not value else f"not a model reference ({value})"
                seeds.problems.append(
                    f"{key} is {what} — set hf:<org/repo>, "
                    "gguf:<url|hf://org/repo/file.gguf|path>, a catalog id, or auto")
    else:
        _migrate_legacy(env, seeds)
    for key, value in env.items():
        if key.startswith(PIN_PREFIX) and value.strip():
            seeds.pins[key[len(PIN_PREFIX):].lower()] = value.strip()
    return seeds


def _migrate_legacy(env: Mapping[str, str], seeds: Seeds) -> None:
    for family in legacy_families():
        if not any((env.get(var) or "").strip() for var in family["trigger_vars"]):
            continue
        if family["format"] == "gguf":
            file = (env.get(family["file_var"]) or family["file_default"]).strip()
            directory = (env.get(family["dir_var"]) or family["dir_default"]).strip()
            local = Path(directory).expanduser() / file
            repo = (env.get(family["repo_var"]) or family["repo_default"]).strip()
            ref = f"gguf:{local}" if local.is_file() else f"gguf:hf://{repo}/{file}"
        else:
            value = (env.get(family["ref_var"]) or family["ref_default"]).strip()
            if _is_new_form(value):  # already a V1 reference under a legacy key
                continue
            ref = value if value.startswith("hf:") else f"hf:{value}"
        name = (env.get(family["name_var"]) or "").strip() or None
        seeds.migrated_from = family["id"]
        seeds.runtime = seeds.runtime or family["runtime"]
        # The legacy engine context flag was a serving budget, not the
        # model's capability — the class budget takes over (roles negotiate).
        for key, group in SEED_GROUPS.items():
            seeds.models[group] = ModelSeed(
                key, group, ref, name, tool_call_format=family.get("tool_call_format", ""),
                origin=f"legacy:{family['id']}")
        log.info("legacy .env family '%s' migrated into the three seeds (%s)",
                 family["id"], ref)
        return
    seeds.problems.append(
        "no model seeds found — set AI_RUNTIME and REASONING_MODEL / CODING_MODEL / "
        "CHAT_MODEL in .env (hf:<org/repo>, gguf:<…>, a catalog id, or auto)")


# ── validation (F8: every problem with its fix, before any download) ─────────


def validate_seeds(seeds: Seeds, descriptors: dict[str, RuntimeDescriptor],
                   catalog: Catalog, platform: Platform,
                   registry_roles: Mapping[str, RoleSpec] | None = None) -> list[str]:
    problems = list(seeds.problems)
    if seeds.runtime is None:
        problems.append(f"{RUNTIME_KEY} is missing — set one of: "
                        + ", ".join(sorted(descriptors)))
    elif seeds.runtime not in descriptors:
        problems.append(f"{RUNTIME_KEY}={seeds.runtime} is not a shipped runtime — "
                        "set one of: " + ", ".join(sorted(descriptors)))
    if problems:
        return problems
    descriptor = descriptors[seeds.runtime or ""]
    roles = registry_roles or reference_registry().roles
    refs: dict[str, SourceRef] = {}
    for group, seed in seeds.models.items():
        try:
            ref = parse_source(seed.ref)
        except SourceError as exc:
            problems.append(f"{seed.key}: {exc}")
            continue
        refs[group] = ref
        for role, spec in roles.items():
            if spec.group == group and seed.context and spec.requires.min_context > seed.context:
                problems.append(
                    f"{seed.key}{CONTEXT_SUFFIX}={seed.context} is below the "
                    f"{spec.requires.min_context}-token contract of role {role} — raise it or "
                    "drop the override (the class budget then applies)")
        parsers = descriptor.capabilities.tool_call_parsers
        if ref.format and seed.tool_call_format and seed.tool_call_format not in parsers:
            problems.append(
                f"{seed.key}{TOOL_FORMAT_SUFFIX}={seed.tool_call_format} is not a tool-call "
                f"format {seeds.runtime} parses ({', '.join(parsers)})")
        elif ref.format and not seed.tool_call_format and GENERIC_TOOL_FORMAT not in parsers \
                and any(spec.requires.tool_calling for spec in roles.values()
                        if spec.group == group):
            problems.append(
                f"{seed.key}: no tool-call format declared and {RUNTIME_KEY}={seeds.runtime} "
                f"has no generic handler — roles of group {group} need tool calling: set "
                f"{seed.key}{TOOL_FORMAT_SUFFIX}=<one of: {', '.join(parsers)}> or use a "
                "catalog id")
        if ref.format and ref.format not in descriptor.serves_formats:
            problems.append(
                f"{seed.key} is {ref.format} but {RUNTIME_KEY}={seeds.runtime} serves "
                f"{', '.join(descriptor.serves_formats)} — install a "
                f"{'/'.join(descriptor.serves_formats)} variant, or set {RUNTIME_KEY} to a "
                "runtime that serves " + ref.format)
        if ref.kind == "catalog":
            entry = catalog.entries.get(ref.ref)
            if entry is None:
                problems.append(f"{seed.key}={ref.ref} is not a catalog id (known: "
                                + ", ".join(sorted(catalog.entries)) + ")")
            elif not any(fmt in descriptor.serves_formats for fmt in entry.variants):
                problems.append(f"{seed.key}={ref.ref} has no variant {seeds.runtime} serves "
                                f"({', '.join(entry.variants)})")
        if ref.kind == "auto":
            contracts = [spec.requires for spec in roles.values() if spec.group == group]
            try:
                recommend_one(catalog, group, platform.vector, descriptors,
                              runtime=seeds.runtime, contracts=contracts,
                              capability_class=platform.capability_class,
                              accelerator=platform.accelerator)
            except CatalogError as exc:
                problems.append(f"{seed.key}=auto: {str(exc).splitlines()[0]} — name a "
                                "model explicitly")
    distinct = len({s.ref for s in seeds.models.values()
                    if refs.get(s.group) and refs[s.group].kind != "auto"})
    if distinct > 1 and not descriptor.capabilities.parallel_models:
        problems.append(
            f"{RUNTIME_KEY}={seeds.runtime} serves one model per engine slot but the seeds "
            f"name {distinct} distinct models — seed the three groups with the same "
            f"model, or set {RUNTIME_KEY} to a runtime with parallel_models")
    for role in seeds.pins:
        if role not in roles:
            problems.append(f"{PIN_PREFIX}{role.upper()}: unknown role '{role}' (roles: "
                            + ", ".join(sorted(roles)) + ")")
    return problems


# ── planning the generation (pure) ───────────────────────────────────────────


def seed_name(seed: ModelSeed) -> str:
    if seed.name:
        return seed.name
    return parse_source(seed.ref).default_name()


def plan_generation(seeds: Seeds, installed: RegistryV2, names: Mapping[str, str]) -> RegistryV2:
    """The generation-1 content: the installed registry + reference roles
    and contracts (no reference pins) + groups from the seeds + seed pins.
    ``names`` maps group → registry model name."""
    proposed = installed.model_copy(deep=True)
    roles: dict[str, RoleSpec] = {}
    for role, spec in reference_registry().roles.items():
        roles[role] = RoleSpec(group=spec.group, requires=spec.requires.model_copy())
    for role, target in seeds.pins.items():
        if role in roles and target in proposed.models:
            roles[role].pin = [target]
        elif role in roles:
            raise BootstrapError(f"{PIN_PREFIX}{role.upper()}={target} names no seeded model "
                                 f"(seeded: {', '.join(sorted(set(names.values())))})")
    proposed.roles = roles
    for group in SEED_GROUPS.values():
        name = names[group]
        proposed.groups[group] = [name]
        entry = proposed.models[name]
        if group not in entry.groups:
            entry.groups.append(group)
    return proposed


# ── the bootstrap ────────────────────────────────────────────────────────────


@dataclass
class BootstrapResult:
    generation: int | None
    request_id: str
    models: dict[str, str]          # group → model name
    runtime: str
    message: str
    diff: list[str]
    dry_run: bool = False
    stamped: bool = False
    problems: list[str] = field(default_factory=list)


BenchmarkFn = Callable[[RegistryV2, str], RegistryV2]


def bootstrap(seeds: Seeds, platform: Platform, queue: GovernanceQueue, catalog: Catalog, *,
              actor: str, validator: Validator, env_path: Path | None = None,
              fetcher_factory: Callable = lifecycle.default_fetcher,
              benchmark_fn: BenchmarkFn | None = None, skip_benchmark: bool = False,
              reloader: activation.Reloader | None = None,
              health: activation.HealthCheck | None = None, dry_run: bool = False,
              force: bool = False, environ: Mapping[str, str] | None = None) -> BootstrapResult:
    """Seeds → generation 1. Raises ``BootstrapError`` with every problem
    when the seeds are invalid or a platform already exists (unless
    ``force``); never leaves a half-configured platform (the PR-5 apply
    protocol self-rolls-back)."""
    if registry_path(platform.config_dir).is_file() and not force:
        raise BootstrapError(
            f"a model registry already exists under {platform.config_dir} — the platform is "
            "bootstrapped; manage models with `local-ezai model …` (or pass force to "
            "propose a re-bootstrap as a governed change request)")
    if seeds.consumed and not force:
        raise BootstrapError(
            f".env seeds were already consumed ({seeds.consumed}) — they are read once; "
            "day-2 changes go through `local-ezai model …` (force re-consumes them)")
    problems = validate_seeds(seeds, platform.descriptors, catalog, platform)
    if problems:
        raise BootstrapError("the .env seeds cannot be bootstrapped — "
                             f"{len(problems)} problem(s):\n- " + "\n- ".join(problems))
    runtime = seeds.runtime or ""

    registry = RegistryV2(providers=sorted(platform.descriptors),
                          groups={group: [] for group in SEED_GROUPS.values()})
    names: dict[str, str] = {}
    installed_refs: dict[str, str] = {}
    for group, seed in seeds.models.items():
        is_auto = parse_source(seed.ref).kind == "auto"
        # `auto` is the recommender's answer PER GROUP (F9), never one model for
        # all groups; an explicit reference seeding several groups is one model.
        key = f"auto:{group}" if is_auto else seed.ref
        if key in installed_refs:
            names[group] = installed_refs[key]
            continue
        if dry_run:
            name = f"auto:{group}" if is_auto else seed_name(seed)
            names[group] = installed_refs[key] = name
            parsers = platform.descriptors[runtime].capabilities.tool_call_parsers
            registry.models[name] = lifecycle.ModelEntry(
                provider=runtime, groups=[group], context=seed.context,
                source=parse_source(seed.ref).as_source(),
                tool_call_format=seed.tool_call_format or (
                    GENERIC_TOOL_FORMAT if GENERIC_TOOL_FORMAT in parsers else ""))
            continue
        if is_auto:
            recommended = _recommended_for(seeds, platform, catalog, group)
            if recommended in registry.models:  # another group already brought it in
                names[group] = installed_refs[key] = recommended
                registry.models[recommended].groups.append(group)
                continue
        result = lifecycle.install(
            registry, seed.ref, descriptors=platform.descriptors, vector=platform.vector,
            platform_root=platform.platform_root, validator=validator, catalog=catalog,
            runtime=runtime, group=group, name=seed.name,
            capability_class=platform.capability_class, accelerator=platform.accelerator,
            fetcher_factory=fetcher_factory, env=environ)
        if not result.ok:
            raise BootstrapError(f"{seed.key}: {result.message}")
        registry = result.registry
        entry = registry.models[result.name]
        if seed.context and not entry.context:
            entry.context = seed.context
        if not entry.tool_call_format:  # user-supplied source: declared or the runtime's generic
            parsers = platform.descriptors[runtime].capabilities.tool_call_parsers
            entry.tool_call_format = seed.tool_call_format or (
                GENERIC_TOOL_FORMAT if GENERIC_TOOL_FORMAT in parsers else "")
        if not skip_benchmark:
            if benchmark_fn is None:
                raise BootstrapError("benchmarking needs a benchmark function (or skip it "
                                     "explicitly)")
            registry = benchmark_fn(registry, result.name)
        names[group] = installed_refs[key] = result.name

    proposed = plan_generation(seeds, registry, names)
    for name in set(names.values()):
        entry = proposed.models[name]
        if dry_run:
            entry.state = "active"
        elif entry.state == "benchmarked":
            lifecycle.transition(entry, "active", reason="bootstrap")
        elif entry.state == "installed" and skip_benchmark:
            entry.state = "active"  # bootstrap-only allowance, audited in the request title
        else:
            raise BootstrapError(f"model '{name}' is {entry.state} — cannot activate")
    diff = diff_generations(RegistryV2(), proposed)
    title = ("bootstrap generation 1 from .env seeds"
             + (f" (migrated from legacy {seeds.migrated_from})" if seeds.migrated_from else "")
             + (" — benchmark skipped" if skip_benchmark else ""))
    if dry_run:
        return BootstrapResult(None, "", names, runtime,
                               "dry run: seeds valid; generation 1 would contain the changes "
                               "below (nothing downloaded or written)", diff, dry_run=True)

    # The ONE implicit approval belongs to generation 1. A forced re-bootstrap
    # of a platform with history is a governed change like any other.
    has_history = registry_path(platform.config_dir).is_file()
    current = load_registry(platform.config_dir) if has_history else RegistryV2()
    evidence = {"seeds": {g: s.ref for g, s in seeds.models.items()},
                "migrated_from": seeds.migrated_from}
    try:
        request = activation.propose(platform, queue, current, proposed, kind="bootstrap",
                                     title=title, requested_by=actor,
                                     implicit_approval=not has_history, evidence=evidence)
    except LifecycleError as exc:
        raise BootstrapError(f"the seeded generation is not servable as declared:\n{exc}") from exc
    if request.status != "approved":
        return BootstrapResult(
            None, request.id, names, runtime,
            f"re-bootstrap proposed as change request {request.id} (the platform already has "
            f"generation {current.generation}) — awaiting approval: local-ezai governance "
            f"approve {request.id}", diff_generations(current, proposed))
    applied = activation.apply(platform, queue, request.id, actor=actor, reloader=reloader,
                               health=health)
    if not applied.ok:
        raise BootstrapError(f"generation 1 could not be applied: {applied.message}")
    stamped = False
    if env_path is not None and env_path.is_file():
        stamp_env(env_path, applied.generation or 1)
        stamped = True
    return BootstrapResult(applied.generation, request.id, names, runtime,
                           f"generation {applied.generation} bootstrapped: "
                           + ", ".join(f"{g} ← {n}" for g, n in names.items())
                           + f" on {runtime}" + (" (seeds stamped as consumed)" if stamped else ""),
                           diff, stamped=stamped)


def _recommended_for(seeds: Seeds, platform: Platform, catalog: Catalog, group: str) -> str:
    """The catalog id `auto` resolves to for a group on this host — the same
    call ``validate_seeds`` already made, so it does not raise here."""
    contracts = [spec.requires for spec in reference_registry().roles.values()
                 if spec.group == group]
    return recommend_one(catalog, group, platform.vector, platform.descriptors,
                         runtime=seeds.runtime, contracts=contracts,
                         capability_class=platform.capability_class,
                         accelerator=platform.accelerator).catalog_id


def stamp_env(env_path: Path, generation: int, now: str | None = None) -> None:
    """Record in ``.env`` that the seeds were consumed (read once — later
    edits are inert and the stamp says so)."""
    stamp = now or time.strftime("%Y-%m-%dT%H:%M:%S")
    text = env_path.read_text(encoding="utf-8")
    block = ("\n# ── local-ezai bootstrap: the model seeds above were consumed into "
             f"generation {generation} on {stamp} ──\n"
             f"# {RUNTIME_KEY} / {' / '.join(SEED_GROUPS)} are read ONCE. Editing them "
             "now has no effect —\n"
             "# manage models with `local-ezai model …` (or the Admin Center).\n"
             f"{CONSUMED_KEY}={generation}@{stamp}\n")
    lines = [line for line in text.splitlines() if not line.startswith(CONSUMED_KEY + "=")]
    env_path.write_text("\n".join(lines).rstrip("\n") + "\n" + block, encoding="utf-8")
