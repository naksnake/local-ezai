"""Platform verbs of the ``local-ezai`` CLI (PR-6, ADR-025/026;
docs/CLI_AND_WEBUI_STRATEGY.md §5, docs/MODEL_LIFECYCLE_MANAGEMENT.md §2)::

    local-ezai model       install|benchmark|activate|upgrade|rollback|retire|
                           uninstall|explain|history|catalog
    local-ezai governance  list|show|approve|reject
    local-ezai project     add|list|remove
    local-ezai status
    local-ezai up|down     [--profile P] [--rendered]

**Direct mode** (the only mode until the PR-8 control plane): the verbs act
in-process on the platform's declarative state — Registry v2 + generations,
runtime descriptors, catalog, governance queue — through the PR-3/4/5
modules. Nothing here knows a runtime or a model name; the platform is
found through ``platform.config_dir`` (config / ``AGENTD_PLATFORM__CONFIG_DIR``)
or by walking up from the project. The repo-work verbs (run/plan/…) are
untouched and keep working with the stack down.

Seams (module attributes, replaceable in tests): ``build_validator``,
``engine_http``, ``default_runner``, ``http_probe``.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agentd import activation, lifecycle
from agentd.activation import ComposeReloader, EngineHealth, Platform
from agentd.capability import PROFILE_PRESETS, CapabilityVector, detect_vector
from agentd.catalog import Catalog, CatalogError, load_catalog, recommend
from agentd.config import AgentdConfig
from agentd.control import DEFAULT_PORT as CONTROL_PORT
from agentd.control import PORT_ENV as CONTROL_PORT_ENV
from agentd.governance import ChangeRequest, GovernanceError, GovernanceQueue
from agentd.lifecycle import (
    EngineHTTP,
    HttpxEngineHTTP,
    LifecycleError,
    SideLoadValidator,
    default_runner,
)
from agentd.logging_setup import get_logger
from agentd.registry_v2 import (
    RegistryError,
    RegistryResolutionError,
    RegistryV2,
    diff_generations,
    list_generations,
    load_generation,
    load_registry,
    registry_path,
)
from agentd.render import (
    ENGINE_COMPOSE_FILENAME,
    ENGINE_PORT,
    MANIFEST_FILENAME,
    RenderError,
    check_contract,
    rendered_dir,
)
from agentd.routing import PLATFORM_ENV, find_platform_config
from agentd.runtime_descriptor import DescriptorError, RuntimeDescriptor, load_descriptors

log = get_logger("platform-cli")

PLATFORM_COMMANDS = ("model", "governance", "project", "status", "up", "down", "bootstrap")
PROJECTS_FILENAME = "projects.yaml"
DEFAULT_GROUPS = ("reasoning", "coding", "chat")
ROUTER_PORT = 4000
BASE_COMPOSE = "docker-compose.yml"


class PlatformError(Exception):
    """No platform to act on (usage-level failure, exit 2)."""


# ── context ──────────────────────────────────────────────────────────────────


@dataclass
class PlatformContext:
    config: AgentdConfig
    config_dir: Path
    root: Path
    descriptors: dict[str, RuntimeDescriptor]
    catalog: Catalog
    vector: CapabilityVector
    platform: Platform
    queue: GovernanceQueue
    actor: str

    @property
    def workdir(self) -> Path:
        return self.config_dir / ".sideload"

    def registry(self, required: bool = True) -> RegistryV2 | None:
        if registry_path(self.config_dir).is_file():
            return load_registry(self.config_dir)
        if required:
            raise LifecycleError(
                f"no model registry under {self.config_dir} yet — install a model "
                "(`local-ezai model install …`) or run the platform bootstrap")
        return None

    def fresh_registry(self) -> RegistryV2:
        """Pre-bootstrap registry: providers + empty groups, no roles (so it is
        servable and can be persisted); the bootstrap adds roles later."""
        return RegistryV2(providers=sorted(self.descriptors),
                          groups={group: [] for group in DEFAULT_GROUPS})


def build_context(config: AgentdConfig, project: Path, actor: str | None = None) -> PlatformContext:
    config_dir = find_platform_config(config, project)
    if config_dir is None or not (config_dir / "providers").is_dir():
        raise PlatformError(
            "no Local-EZAI platform found for this directory — run inside the "
            f"local-ezai checkout, set platform.config_dir, or export {PLATFORM_ENV}")
    descriptors = load_descriptors(config_dir)
    vector = detect_vector()
    platform = Platform(config_dir=config_dir, platform_root=config_dir.parent,
                        descriptors=descriptors, vector=vector)
    return PlatformContext(
        config=config, config_dir=config_dir, root=config_dir.parent,
        descriptors=descriptors, catalog=load_catalog(config_dir), vector=vector,
        platform=platform, queue=GovernanceQueue(config_dir),
        actor=actor or os.environ.get("USER") or "cli")


# ── seams ────────────────────────────────────────────────────────────────────


def build_validator(ctx: PlatformContext) -> Callable:
    return SideLoadValidator(ctx.vector, ctx.platform.klass, ctx.platform.accel,
                             platform_root=ctx.root, workdir=ctx.workdir)


def engine_http() -> EngineHTTP:
    return HttpxEngineHTTP()


def http_probe(url: str) -> int:
    status, _ = engine_http().get(url)
    return status


def reload_seams(enabled: bool) -> tuple[activation.Reloader | None, activation.HealthCheck | None]:
    if not enabled:
        return None, None
    return ComposeReloader(default_runner), EngineHealth(engine_http())


# ── output helpers ───────────────────────────────────────────────────────────


def _emit(args: argparse.Namespace, data: dict[str, Any], lines: list[str]) -> None:
    if getattr(args, "as_json", False):
        print(json.dumps(data, indent=2, default=str))
    else:
        print("\n".join(lines))


def _request_lines(req: ChangeRequest) -> list[str]:
    lines = [f"change request {req.id}: {req.title}",
             f"  kind: {req.kind} · status: {req.status} · base generation "
             f"{req.base_generation} · by {req.requested_by} ({req.proposed_by})"]
    for role, change in req.affected_roles.items():
        before = change.get("before") or {}
        after = change.get("after") or {}
        lines.append(f"  role {role}: {before.get('primary', '—')} → "
                     f"{after.get('primary', '—')}"
                     + (f" (fallbacks {', '.join(after.get('fallbacks', []))})"
                        if after.get("fallbacks") else ""))
    for line in req.diff:
        lines.append(f"  diff: {line}")
    benchmarks = req.evidence.get("benchmarks", {})
    for name, record in benchmarks.items():
        lines.append(f"  evidence: {name} {record.get('tokens_per_s', '?')} tok/s")
    if req.decision:
        lines.append(f"  decision: {req.status} by {req.decision.by} — {req.decision.reason}")
    if req.result:
        lines.append(f"  result: {json.dumps(req.result, default=str)}")
    return lines


def _apply(ctx: PlatformContext, request_id: str, args: argparse.Namespace) -> int:
    reloader, health = reload_seams(getattr(args, "reload", False))
    result = activation.apply(ctx.platform, ctx.queue, request_id, actor=ctx.actor,
                              reloader=reloader, health=health)
    _emit(args, {"request": request_id, "ok": result.ok, "generation": result.generation,
                 "message": result.message, "changed": result.changed,
                 "rolled_back": result.rolled_back},
          [result.message])
    return 0 if result.ok else 1


def _after_proposal(ctx: PlatformContext, req: ChangeRequest, args: argparse.Namespace) -> int:
    if req.status == "approved":  # policy-approved: self-service, audited
        _emit(args, {"request": req.model_dump(mode="json")},
              [*_request_lines(req), "approved by policy (no serving role affected) — applying"])
        return _apply(ctx, req.id, args)
    _emit(args, {"request": req.model_dump(mode="json")},
          [*_request_lines(req),
           f"awaiting human approval: local-ezai governance approve {req.id}"])
    return 0


# ── model verbs ──────────────────────────────────────────────────────────────


def cmd_model_install(ctx: PlatformContext, args: argparse.Namespace) -> int:
    registry = ctx.registry(required=False) or ctx.fresh_registry()
    result = lifecycle.install(
        registry, args.ref, descriptors=ctx.descriptors, vector=ctx.vector,
        platform_root=ctx.root, validator=build_validator(ctx), catalog=ctx.catalog,
        runtime=args.runtime, group=args.group, name=args.name,
        capability_class=ctx.platform.klass, accelerator=ctx.platform.accel,
        refetch=args.refetch, persist_dir=ctx.config_dir)
    entry = result.registry.models[result.name]
    lines = [result.message]
    if entry.error:
        lines.append(f"  error: {entry.error}")
    if result.ok:
        lines.append(f"  next: local-ezai model benchmark {result.name}")
    _emit(args, {"name": result.name, "state": result.state, "artifact": entry.artifact,
                 "size_gb": entry.size_gb, "error": entry.error, "persisted": result.persisted,
                 "message": result.message}, lines)
    return 0 if result.ok else 1


def cmd_model_benchmark(ctx: PlatformContext, args: argparse.Namespace) -> int:
    registry = ctx.registry()
    updated, result = lifecycle.benchmark(
        registry, args.name, descriptors=ctx.descriptors, vector=ctx.vector,
        platform_root=ctx.root, workdir=ctx.workdir, capability_class=ctx.platform.klass,
        accelerator=ctx.platform.accel, base_url=args.base_url, runner=default_runner,
        http=engine_http(), agent_dir=ctx.root / ctx.config.memory.dir,
        persist_dir=ctx.config_dir)
    record = updated.models[args.name].benchmarks
    _emit(args, {"name": args.name, **record, "persisted": result.persisted},
          [f"benchmark {args.name}: {result.tokens_per_s:.1f} tok/s "
           f"({result.via}, {result.completion_tokens} tokens in {result.latency_s:.1f}s)",
           f"  state: {updated.models[args.name].state}"
           + ("" if result.persisted else f" — {lifecycle.UNSERVABLE_HINT}"),
           f"  next: local-ezai model activate {args.name} --group <reasoning|coding|chat>"])
    return 0


def cmd_model_activate(ctx: PlatformContext, args: argparse.Namespace) -> int:
    req = activation.activate(ctx.platform, ctx.queue, ctx.registry(), args.name,
                              requested_by=ctx.actor, group=args.group, role=args.role,
                              position=args.position)
    return _after_proposal(ctx, req, args)


def cmd_model_upgrade(ctx: PlatformContext, args: argparse.Namespace) -> int:
    req = activation.upgrade(ctx.platform, ctx.queue, ctx.registry(), args.old, args.new,
                             requested_by=ctx.actor)
    return _after_proposal(ctx, req, args)


def cmd_model_rollback(ctx: PlatformContext, args: argparse.Namespace) -> int:
    ctx.registry()
    reloader, health = reload_seams(args.reload)
    result = activation.rollback(ctx.platform, ctx.queue, actor=ctx.actor,
                                 to_generation=args.to_generation, reason=args.reason or "",
                                 reloader=reloader, health=health, notify=print)
    _emit(args, {"ok": result.ok, "generation": result.generation, "message": result.message,
                 "rolled_back": result.rolled_back}, [result.message])
    return 0 if result.ok else 1


def cmd_model_retire(ctx: PlatformContext, args: argparse.Namespace) -> int:
    updated, persisted = lifecycle.retire(ctx.registry(), args.name, persist_dir=ctx.config_dir)
    _emit(args, {"name": args.name, "state": "retired", "generation": updated.generation,
                 "persisted": persisted},
          [f"retired {args.name} (generation {updated.generation}); weights kept for rollback"])
    return 0


def cmd_model_uninstall(ctx: PlatformContext, args: argparse.Namespace) -> int:
    updated, persisted = lifecycle.uninstall(ctx.registry(), args.name, config_dir=ctx.config_dir,
                                             force=args.force, persist_dir=ctx.config_dir)
    _emit(args, {"name": args.name, "removed": True, "generation": updated.generation,
                 "persisted": persisted},
          [f"uninstalled {args.name}: weights removed, registry generation {updated.generation}"])
    return 0


def cmd_model_explain(ctx: PlatformContext, args: argparse.Namespace) -> int:
    registry = ctx.registry()
    resolution = registry.resolve(args.role)
    spec = registry.roles[args.role]
    checks: dict[str, Any] = {}
    lines = [f"role {args.role} — generation {registry.generation}",
             f"  source:   {'pin ' + ', '.join(spec.pin) if spec.pin else 'group ' + spec.group}",
             f"  primary:  {resolution.primary}",
             f"  fallback: {', '.join(resolution.fallbacks) or '(none)'}"]
    lines += [f"  why:      {reason}" for reason in resolution.reason]
    for name in [resolution.primary, *resolution.fallbacks]:
        entry = registry.models[name]
        descriptor = ctx.descriptors.get(entry.provider)
        if descriptor is None:
            checks[name] = {"ok": False, "failures": [f"no descriptor for '{entry.provider}'"]}
        else:
            passed, failures = check_contract(name, entry, descriptor, spec.requires,
                                              ctx.platform.klass, ctx.platform.accel)
            checks[name] = {"ok": not failures, "checks": passed, "failures": failures}
        status = "ok" if checks[name]["ok"] else "FAIL"
        if checks[name]["failures"]:
            detail = " — " + "; ".join(checks[name]["failures"])
        else:
            passed_keys = [k for k, v in checks[name].get("checks", {}).items() if v]
            detail = f" ({', '.join(passed_keys) or 'no requirements'})"
        lines.append(f"  contract: {name} [{status}]{detail}")
    _emit(args, {"role": args.role, "generation": registry.generation,
                 "primary": resolution.primary, "fallbacks": resolution.fallbacks,
                 "reason": resolution.reason, "contract": spec.requires.model_dump(),
                 "checks": checks}, lines)
    return 0 if all(c["ok"] for c in checks.values()) else 1


def cmd_model_history(ctx: PlatformContext, args: argparse.Namespace) -> int:
    ctx.registry()
    numbers = list_generations(ctx.config_dir)[-max(1, args.limit):]
    entries: list[dict[str, Any]] = []
    lines: list[str] = []
    previous: RegistryV2 | None = None
    for number in numbers:
        generation = load_generation(ctx.config_dir, number)
        diff = diff_generations(previous, generation) if previous is not None else []
        entries.append({"generation": number, "saved_at": generation.saved_at,
                        "note": generation.note, "diff": diff,
                        "active": sorted(n for n, e in generation.models.items()
                                         if e.state == "active")})
        lines.append(f"generation {number}  {generation.saved_at}  {generation.note}")
        lines += [f"    {line}" for line in diff]
        previous = generation
    _emit(args, {"generations": entries}, lines or ["(no generations yet)"])
    return 0


def cmd_model_catalog(ctx: PlatformContext, args: argparse.Namespace) -> int:
    if args.group:
        registry = ctx.registry(required=False)
        contracts = [spec.requires for spec in (registry.roles.values() if registry else [])
                     if spec.group == args.group]
        ranked = recommend(ctx.catalog, args.group, ctx.vector, ctx.descriptors,
                           runtime=args.runtime, contracts=contracts,
                           capability_class=ctx.platform.klass, accelerator=ctx.platform.accel)
        _emit(args, {"group": args.group, "class": ctx.platform.klass,
                     "candidates": [{"id": r.catalog_id, "format": r.format,
                                     "provider": r.provider, "eligible": r.eligible,
                                     "verdict": r.verdict.model_dump(),
                                     "contract_failures": r.contract_failures}
                                    for r in ranked]},
              [f"catalog recommendations for group {args.group} on class "
               f"{ctx.platform.klass} ({ctx.platform.accel})",
               *(f"  {r.explain()}" for r in ranked)]
              or [f"(no catalog entry lists group {args.group})"])
        return 0
    lines = []
    for catalog_id, entry in sorted(ctx.catalog.entries.items()):
        variants = ", ".join(f"{fmt} {v.size_gb:.1f} GB" for fmt, v in entry.variants.items())
        lines.append(f"  {catalog_id:32} {', '.join(entry.groups):22} {entry.license:14} "
                     f"{variants}")
    _emit(args, {"entries": {k: v.model_dump() for k, v in ctx.catalog.entries.items()}},
          [f"catalog: {len(ctx.catalog.entries)} entr(y/ies) "
           f"(packaged seed + {ctx.config_dir / 'catalog'}/*.yaml)", *lines])
    return 0


# ── governance verbs ─────────────────────────────────────────────────────────


def cmd_governance_list(ctx: PlatformContext, args: argparse.Namespace) -> int:
    requests = ctx.queue.list(status=args.status)
    _emit(args, {"requests": [r.model_dump(mode="json") for r in requests]},
          [f"  {r.id}  {r.status:10} {r.kind:11} gen {r.base_generation}  {r.title}"
           for r in requests] or ["(governance queue is empty)"])
    return 0


def cmd_governance_show(ctx: PlatformContext, args: argparse.Namespace) -> int:
    req = ctx.queue.get(args.id)
    _emit(args, {"request": req.model_dump(mode="json")}, _request_lines(req))
    return 0


def cmd_governance_approve(ctx: PlatformContext, args: argparse.Namespace) -> int:
    req = ctx.queue.approve(args.id, by=ctx.actor, reason=args.reason or "")
    print(f"approved {req.id} by {ctx.actor} — applying")
    return _apply(ctx, req.id, args)


def cmd_governance_reject(ctx: PlatformContext, args: argparse.Namespace) -> int:
    req = ctx.queue.reject(args.id, by=ctx.actor, reason=args.reason or "")
    _emit(args, {"request": req.model_dump(mode="json")},
          [f"rejected {req.id} by {ctx.actor}: {req.decision.reason if req.decision else ''}"])
    return 0


# ── project verbs (chat-ops allowlist, consumed by the P3 tool server) ───────


def _projects_path(ctx: PlatformContext) -> Path:
    return ctx.config_dir / PROJECTS_FILENAME


def _load_projects(ctx: PlatformContext) -> list[dict[str, str]]:
    path = _projects_path(ctx)
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("projects", [])) if isinstance(data, dict) else []


def _save_projects(ctx: PlatformContext, projects: list[dict[str, str]]) -> None:
    _projects_path(ctx).write_text(
        "# Projects the chat-ops tool server may act on (local-ezai project …)\n"
        + yaml.safe_dump({"projects": projects}, sort_keys=False), encoding="utf-8")


def cmd_project_add(ctx: PlatformContext, args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser().resolve()
    if not (path / ".git").exists():
        raise LifecycleError(f"{path} is not a git repository")
    projects = _load_projects(ctx)
    name = args.name or path.name
    if any(p["path"] == str(path) or p["name"] == name for p in projects):
        raise LifecycleError(f"project '{name}' ({path}) is already registered")
    projects.append({"name": name, "path": str(path),
                     "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "added_by": ctx.actor})
    _save_projects(ctx, projects)
    ctx.queue.record("project.added", ctx.actor, name=name, path=str(path))
    print(f"registered project {name} → {path}")
    return 0


def cmd_project_list(ctx: PlatformContext, args: argparse.Namespace) -> int:
    projects = _load_projects(ctx)
    _emit(args, {"projects": projects},
          [f"  {p['name']:24} {p['path']}" for p in projects]
          or ["(no projects registered — local-ezai project add <path>)"])
    return 0


def cmd_project_remove(ctx: PlatformContext, args: argparse.Namespace) -> int:
    projects = _load_projects(ctx)
    target = str(Path(args.target).expanduser().resolve()) if "/" in args.target else args.target
    kept = [p for p in projects if p["name"] != target and p["path"] != target]
    if len(kept) == len(projects):
        raise LifecycleError(f"no registered project named or located at '{args.target}'")
    _save_projects(ctx, kept)
    ctx.queue.record("project.removed", ctx.actor, target=args.target)
    print(f"removed project {args.target}")
    return 0


# ── status · up · down ───────────────────────────────────────────────────────


def _manifest_generation(ctx: PlatformContext) -> int | None:
    path = rendered_dir(ctx.config_dir) / MANIFEST_FILENAME
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("generation") if isinstance(data, dict) else None


def platform_snapshot(ctx: PlatformContext) -> dict[str, Any]:
    """The platform's declarative state as one JSON-able mapping — what
    ``local-ezai status`` prints and what the control plane's
    ``GET /v1/health`` reports (PR-8). Read fresh on every call."""
    registry = ctx.registry(required=False)
    pending = ctx.queue.list(status="pending")
    models = {name: {"state": e.state, "runtime": e.provider, "size_gb": e.size_gb,
                     "tokens_per_s": e.benchmarks.get("tokens_per_s")}
              for name, e in (registry.models.items() if registry else {})}
    active_runtimes = sorted({e.provider for e in (registry.models.values() if registry else [])
                              if e.state == "active"})
    return {"platform_root": str(ctx.root), "config_dir": str(ctx.config_dir),
            "capability_class": ctx.platform.klass, "accelerator": ctx.platform.accel,
            "generation": registry.generation if registry else None,
            "note": registry.note if registry else None,
            "rendered_generation": _manifest_generation(ctx),
            "slot_runtime": active_runtimes[0] if len(active_runtimes) == 1 else active_runtimes,
            "models": models, "pending_approvals": len(pending)}


def cmd_status(ctx: PlatformContext, args: argparse.Namespace) -> int:
    data = platform_snapshot(ctx)
    engine_port = os.environ.get("LLM_PORT", str(ENGINE_PORT))
    router_port = os.environ.get("LITELLM_PORT", str(ROUTER_PORT))
    control_port = os.environ.get(CONTROL_PORT_ENV, str(CONTROL_PORT))
    health = {
        "engine": http_probe(f"http://localhost:{engine_port}/health") == 200,
        "router": http_probe(f"http://localhost:{router_port}/health/liveliness") == 200,
        "control": http_probe(f"http://localhost:{control_port}/health") == 200,
    }
    data["health"] = health
    models, pending = data["models"], data["pending_approvals"]
    bootstrapped = data["generation"] is not None
    lines = [f"platform:   {ctx.root}",
             f"hardware:   class {ctx.platform.klass} · accelerator {ctx.platform.accel} · "
             f"{ctx.vector.system_memory_gb:.0f} GB RAM · {ctx.vector.cpu_cores} cores",
             f"generation: {data['generation'] if bootstrapped else '(none — not bootstrapped)'}"
             + (f" — {data['note']}" if bootstrapped and data["note"] else "")
             + (f" · rendered {data['rendered_generation']}"
                if data["rendered_generation"] is not None else " · not rendered"),
             f"runtime:    {data['slot_runtime'] or '(no active model)'}",
             f"health:     engine {'up' if health['engine'] else 'down'} · "
             f"router {'up' if health['router'] else 'down'} · "
             f"control {'up' if health['control'] else 'down'}",
             f"approvals:  {pending} pending"
             + (" — local-ezai governance list" if pending else "")]
    if models:
        lines.append("models:")
        for name, info in models.items():
            tps = f"{info['tokens_per_s']} tok/s" if info["tokens_per_s"] else "not benchmarked"
            lines.append(f"  {name:28} {info['state']:12} {info['runtime']:9} "
                         f"{info['size_gb']:5.1f} GB  {tps}")
    _emit(args, data, lines)
    return 0


def compose_files(root: Path, profile: str, rendered: bool) -> list[Path]:
    """Base file + the profile's override chain (``a-b`` ⇒ ``a`` then ``a-b``
    when those files exist) + the rendered engine override on request."""
    if profile not in PROFILE_PRESETS:
        raise PlatformError(f"unknown profile '{profile}' — known: "
                            + ", ".join(sorted(PROFILE_PRESETS)))
    files = [root / BASE_COMPOSE]
    parts = profile.split("-")
    for i in range(1, len(parts) + 1):
        candidate = root / f"docker-compose.{'-'.join(parts[:i])}.yml"
        if candidate.is_file():
            files.append(candidate)
    if rendered:
        override = rendered_dir(root / "config") / ENGINE_COMPOSE_FILENAME
        if not override.is_file():
            raise PlatformError(f"no rendered engine override at {override} — activate a "
                                "generation first")
        files.append(override)
    return files


def _compose(ctx: PlatformContext, args: argparse.Namespace, *verb: str) -> int:
    files = compose_files(ctx.root, args.profile or "gpu", getattr(args, "rendered", False))
    command = ["docker", "compose"]
    for file in files:
        command += ["-f", str(file)]
    command += list(verb)
    print("$ " + " ".join(command))
    proc = default_runner(command)
    tail = (proc.stdout or "").strip().splitlines()[-5:] + \
        (proc.stderr or "").strip().splitlines()[-5:]
    for line in tail:
        print(f"  {line}")
    return 0 if proc.returncode == 0 else 1


def cmd_up(ctx: PlatformContext, args: argparse.Namespace) -> int:
    return _compose(ctx, args, "up", "-d")


def cmd_down(ctx: PlatformContext, args: argparse.Namespace) -> int:
    return _compose(ctx, args, "down")


# ── bootstrap (PR-7): .env seeds → generation 1 ──────────────────────────────


def cmd_bootstrap(ctx: PlatformContext, args: argparse.Namespace) -> int:
    from agentd import bootstrap as bs

    env_path = Path(args.env).expanduser() if args.env else ctx.root / ".env"
    if not env_path.is_file():
        raise PlatformError(f"no {env_path} — copy .env.example to .env and set "
                            f"{bs.RUNTIME_KEY} + {' / '.join(bs.SEED_GROUPS)}")
    seeds = bs.read_seeds(bs.parse_env(env_path.read_text(encoding="utf-8")))
    reloader, health = reload_seams(args.reload)

    def benchmark_fn(registry, name):
        updated, _ = lifecycle.benchmark(
            registry, name, descriptors=ctx.descriptors, vector=ctx.vector,
            platform_root=ctx.root, workdir=ctx.workdir, capability_class=ctx.platform.klass,
            accelerator=ctx.platform.accel, runner=default_runner, http=engine_http(),
            agent_dir=ctx.root / ctx.config.memory.dir)
        return updated

    try:
        result = bs.bootstrap(seeds, ctx.platform, ctx.queue, ctx.catalog, actor=ctx.actor,
                              validator=build_validator(ctx), env_path=env_path,
                              benchmark_fn=benchmark_fn, skip_benchmark=args.skip_benchmark,
                              reloader=reloader, health=health, dry_run=args.dry_run,
                              force=args.force)
    except bs.BootstrapError as exc:
        _emit(args, {"ok": False, "error": str(exc)}, [str(exc)])
        return 1
    lines = [result.message, f"  runtime: {result.runtime}"
             + (f" · migrated from legacy .env family {seeds.migrated_from}"
                if seeds.migrated_from else "")]
    lines += [f"  {line}" for line in result.diff]
    if not result.dry_run:
        lines.append("  next: make up (or local-ezai up --rendered) · local-ezai status")
    _emit(args, {"ok": True, "generation": result.generation, "request": result.request_id,
                 "models": result.models, "runtime": result.runtime, "dry_run": result.dry_run,
                 "diff": result.diff, "stamped": result.stamped,
                 "migrated_from": seeds.migrated_from}, lines)
    return 0


# ── argparse wiring + dispatch ───────────────────────────────────────────────


def add_platform_parsers(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    def leaf(parent: argparse._SubParsersAction, name: str, help_: str) -> argparse.ArgumentParser:
        parser = parent.add_parser(name, help=help_, parents=[common])
        parser.add_argument("--json", action="store_true", dest="as_json")
        return parser

    model = sub.add_parser("model", help="Model lifecycle: install · benchmark · activate · "
                                         "upgrade · rollback · retire · uninstall · explain · "
                                         "history · catalog", parents=[common])
    mv = model.add_subparsers(dest="verb", required=True)
    p = leaf(mv, "install", "Fetch + validate a model (hf:… gguf:… catalog id · auto)")
    p.add_argument("ref")
    p.add_argument("--name", default=None, help="Registry name (default: derived)")
    p.add_argument("--runtime", default=None, help="Serving runtime (default: by format)")
    p.add_argument("--group", default=None, help="Group for `auto`")
    p.add_argument("--refetch", action="store_true")
    p = leaf(mv, "benchmark", "Measure tokens/sec on this host (side-load or --base-url)")
    p.add_argument("name")
    p.add_argument("--base-url", default=None, dest="base_url")
    p = leaf(mv, "activate", "Request activation (approval when a serving role changes)")
    p.add_argument("name")
    p.add_argument("--group", default=None)
    p.add_argument("--position", type=int, default=None, help="0 = primary (default with --group)")
    p.add_argument("--role", default=None, help="Pin this role to the model")
    p.add_argument("--reload", action="store_true", help="Reload consumers + health-check")
    p.add_argument("--by", default=None, help="Actor recorded in the audit log")
    p = leaf(mv, "upgrade", "Swap a serving version for a benchmarked one (approval)")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--reload", action="store_true")
    p.add_argument("--by", default=None)
    p = leaf(mv, "rollback", "Restore the previous (or named) generation — no approval, audited")
    p.add_argument("--to-generation", type=int, default=None, dest="to_generation")
    p.add_argument("--reason", default=None)
    p.add_argument("--reload", action="store_true")
    p.add_argument("--by", default=None)
    p = leaf(mv, "retire", "Remove a non-serving active model from resolution")
    p.add_argument("name")
    p = leaf(mv, "uninstall", "Delete weights of a retired/failed model")
    p.add_argument("name")
    p.add_argument("--force", action="store_true",
                   help="Even if a generation could roll back to it")
    p = leaf(mv, "explain", "What would serve a role, why, and does it meet the contract")
    p.add_argument("role")
    p = leaf(mv, "history", "Generations with notes and diffs")
    p.add_argument("--limit", type=int, default=10)
    p = leaf(mv, "catalog", "Catalog entries, or recommendations for --group (fit verdicts)")
    p.add_argument("--group", default=None)
    p.add_argument("--runtime", default=None)

    governance = sub.add_parser("governance", help="Approval queue: list · show · approve · reject",
                                parents=[common])
    gv = governance.add_subparsers(dest="verb", required=True)
    p = leaf(gv, "list", "Pending and decided change requests")
    p.add_argument("--status", default=None)
    p = leaf(gv, "show", "One change request with its evidence")
    p.add_argument("id")
    p = leaf(gv, "approve", "Approve a pending request and apply it")
    p.add_argument("id")
    p.add_argument("--reason", default=None)
    p.add_argument("--by", default=None)
    p.add_argument("--reload", action="store_true")
    p = leaf(gv, "reject", "Reject a pending request (reason required)")
    p.add_argument("id")
    p.add_argument("--reason", default=None)
    p.add_argument("--by", default=None)

    project = sub.add_parser("project", help="Chat-ops project allowlist: add · list · remove",
                             parents=[common])
    pv = project.add_subparsers(dest="verb", required=True)
    p = leaf(pv, "add", "Register a repository")
    p.add_argument("path")
    p.add_argument("--name", default=None)
    leaf(pv, "list", "Registered repositories")
    p = leaf(pv, "remove", "Unregister by name or path")
    p.add_argument("target")

    leaf(sub, "status", "Platform status: generation, models, approvals, stack health")
    p = leaf(sub, "up", "Start the stack (compose profile; --rendered adds the engine override)")
    p.add_argument("--profile", default=None, help="gpu (default) · cpu · n97 · n97-igpu")
    p.add_argument("--rendered", action="store_true")
    p = leaf(sub, "down", "Stop the stack")
    p.add_argument("--profile", default=None)

    p = leaf(sub, "bootstrap", "Consume the .env model seeds once into generation 1 "
                               "(validate → install → benchmark → activate → render)")
    p.add_argument("--env", default=None, help="Path to .env (default: <platform>/.env)")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="Validate the seeds and show the planned generation; download nothing")
    p.add_argument("--skip-benchmark", action="store_true", dest="skip_benchmark")
    p.add_argument("--force", action="store_true",
                   help="Re-consume the seeds although a registry/stamp exists")
    p.add_argument("--reload", action="store_true", help="Reload consumers + health-check")
    p.add_argument("--by", default=None)


HANDLERS: dict[tuple[str, str | None], Callable[[PlatformContext, argparse.Namespace], int]] = {
    ("model", "install"): cmd_model_install, ("model", "benchmark"): cmd_model_benchmark,
    ("model", "activate"): cmd_model_activate, ("model", "upgrade"): cmd_model_upgrade,
    ("model", "rollback"): cmd_model_rollback, ("model", "retire"): cmd_model_retire,
    ("model", "uninstall"): cmd_model_uninstall, ("model", "explain"): cmd_model_explain,
    ("model", "history"): cmd_model_history, ("model", "catalog"): cmd_model_catalog,
    ("governance", "list"): cmd_governance_list, ("governance", "show"): cmd_governance_show,
    ("governance", "approve"): cmd_governance_approve,
    ("governance", "reject"): cmd_governance_reject,
    ("project", "add"): cmd_project_add, ("project", "list"): cmd_project_list,
    ("project", "remove"): cmd_project_remove,
    ("status", None): cmd_status, ("up", None): cmd_up, ("down", None): cmd_down,
    ("bootstrap", None): cmd_bootstrap,
}


def dispatch_platform(command: str, args: argparse.Namespace, config: AgentdConfig,
                      project: Path) -> int:
    try:
        ctx = build_context(config, project, actor=getattr(args, "by", None))
        handler = HANDLERS[(command, getattr(args, "verb", None))]
        return handler(ctx, args)
    except PlatformError as exc:
        log.error("%s", exc)
        return 2
    except (LifecycleError, GovernanceError, CatalogError, RegistryError,
            RegistryResolutionError, RenderError, DescriptorError) as exc:
        log.error("%s", exc)
        return 1
