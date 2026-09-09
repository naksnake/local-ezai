"""Platform verbs of the ``local-ezai`` CLI (PR-6, ADR-025/026;
docs/CLI_AND_WEBUI_STRATEGY.md §5, docs/MODEL_LIFECYCLE_MANAGEMENT.md §2)::

    local-ezai model       install|benchmark|activate|upgrade|rollback|retire|
                           uninstall|explain|history|catalog
    local-ezai governance  list|show|approve|reject
    local-ezai project     add|list|remove
    local-ezai status
    local-ezai up|down     [--profile P] [--rendered]

Two transports, one UX (PR-11, CLI_AND_WEBUI_STRATEGY §2):

- **Connected mode** — the ``ezaid`` control plane answers its liveness
  probe: the management verbs go through the API (``control/client.py``),
  so the audit trail, the queue and the idempotency keys are shared with
  the Admin Center and the tool server. Chosen automatically; forced with
  ``--transport connected`` (fails fast when the daemon is unreachable).
- **Direct mode** — no daemon: the verbs act in-process on the platform's
  declarative state — Registry v2 + generations, runtime descriptors,
  catalog, governance queue — through the PR-3/4/5 modules. Nothing here
  knows a runtime or a model name; the platform is found through
  ``platform.config_dir`` (config / ``AGENTD_PLATFORM__CONFIG_DIR``) or by
  walking up from the project.

Both modes format the SAME operation result (PR-9): the direct operation's
mapping is the API's response body. ``bootstrap``, ``up`` and ``down`` act
on the host (compose, ``.env``) and are always direct; the repo-work verbs
(run/plan/…) are untouched and keep working with the stack down.

Seams (module attributes, replaceable in tests): ``build_validator``,
``engine_http``, ``default_runner``, ``http_probe``; the connected transport's
``control.client.client_factory``.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agentd import activation, lifecycle
from agentd.activation import ComposeReloader, EngineHealth, Platform
from agentd.capability import PROFILE_PRESETS, CapabilityVector, detect_vector
from agentd.catalog import Catalog, load_catalog, recommend
from agentd.config import AgentdConfig
from agentd.control import (
    CLI_CLIENT_NAME,
    TRANSPORT_ENV,
    TRANSPORTS,
    URL_ENV,
)
from agentd.control import DEFAULT_PORT as CONTROL_PORT
from agentd.control import PORT_ENV as CONTROL_PORT_ENV
from agentd.control import TOKEN_ENV as CONTROL_TOKEN_ENV
from agentd.control import client as control_client
from agentd.control.client import ConnectedOps, ControlPlaneError
from agentd.governance import ChangeRequest, GovernanceQueue
from agentd.lifecycle import (
    EngineHTTP,
    HttpxEngineHTTP,
    LifecycleError,
    SideLoadValidator,
    default_runner,
)
from agentd.logging_setup import get_logger
from agentd.platform_errors import PlatformError, classify, platform_exceptions
from agentd.registry_v2 import (
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
    check_contract,
    rendered_dir,
)
from agentd.routing import PLATFORM_ENV, find_platform_config
from agentd.runtime_descriptor import CAPABILITY_CLASSES, RuntimeDescriptor, load_descriptors

log = get_logger("platform-cli")

PLATFORM_COMMANDS = ("model", "governance", "project", "status", "up", "down", "bootstrap",
                     "setup", "init")
#: Verbs that act on THIS host (compose files, .env) — never sent to a daemon.
HOST_ONLY_COMMANDS = ("bootstrap", "up", "down", "setup", "init")
#: A capability class ASSERTED by the operator (install.sh --profile/--class
#: writes it to .env; make exports .env) — the installer and the CLI then
#: agree on the class instead of each detecting (PR-21). Unset → detect.
CLASS_ENV = "EZAI_CAPABILITY_CLASS"
PROJECTS_FILENAME = "projects.yaml"
DEFAULT_GROUPS = ("reasoning", "coding", "chat")
ROUTER_PORT = 4000
BASE_COMPOSE = "docker-compose.yml"


__all__ = ["ConnectedContext", "PlatformContext", "PlatformError", "build_context",
           "dispatch_platform"]


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

    @property
    def ops(self) -> DirectOps:
        return DirectOps(self)


class DirectOps:
    """The operations below, bound to a platform context — the in-process
    twin of ``ConnectedOps`` (same method names, same results)."""

    def __init__(self, ctx: PlatformContext) -> None:
        self._ctx = ctx

    def __getattr__(self, name: str) -> Callable[..., dict[str, Any]]:
        operation = _DIRECT_OPERATIONS.get(name)
        if operation is None:
            raise AttributeError(name)
        return lambda *args, **kwargs: operation(self._ctx, *args, **kwargs)


@dataclass
class ConnectedContext:
    """What a verb needs when the daemon does the work: who acts, where."""

    actor: str
    url: str
    ops: ConnectedOps


def default_actor(actor: str | None = None) -> str:
    return actor or os.environ.get("USER") or CLI_CLIENT_NAME


def build_context(config: AgentdConfig, project: Path, actor: str | None = None) -> PlatformContext:
    config_dir = find_platform_config(config, project)
    if config_dir is None or not (config_dir / "providers").is_dir():
        raise PlatformError(
            "no Local-EZAI platform found for this directory — run inside the "
            f"local-ezai checkout, set platform.config_dir, or export {PLATFORM_ENV}")
    descriptors = load_descriptors(config_dir)
    vector = detect_vector()
    asserted = (os.environ.get(CLASS_ENV) or "").strip() or None
    if asserted is None:  # not exported (a shell, not make) → the platform's own .env
        env_file = config_dir.parent / ".env"
        if env_file.is_file():
            from agentd.bootstrap import parse_env

            asserted = (parse_env(env_file.read_text(encoding="utf-8")).get(CLASS_ENV)
                        or "").strip() or None
    if asserted and asserted not in CAPABILITY_CLASSES:
        raise PlatformError(f"{CLASS_ENV}={asserted} is not a capability class (known: "
                            f"{', '.join(CAPABILITY_CLASSES)}) — fix or remove the line in .env")
    platform = Platform(config_dir=config_dir, platform_root=config_dir.parent,
                        descriptors=descriptors, vector=vector, capability_class=asserted)
    return PlatformContext(
        config=config, config_dir=config_dir, root=config_dir.parent,
        descriptors=descriptors, catalog=load_catalog(config_dir), vector=vector,
        platform=platform, queue=GovernanceQueue(config_dir), actor=default_actor(actor))


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


# ── operations (PR-9): one brain for the CLI verbs AND the control-plane API ──
#
# Each operation acts on the platform through the PR-3/4/5 modules and
# returns the JSON-able mapping the CLI prints with ``--json`` and the API
# returns as its response body — the parity the product promises
# (CLI_AND_WEBUI_STRATEGY §3/§7). The ``cmd_*`` verbs below only format.


def install_model(ctx: PlatformContext, ref: str, *, name: str | None = None,
                  runtime: str | None = None, group: str | None = None,
                  refetch: bool = False) -> dict[str, Any]:
    registry = ctx.registry(required=False) or ctx.fresh_registry()
    result = lifecycle.install(
        registry, ref, descriptors=ctx.descriptors, vector=ctx.vector,
        platform_root=ctx.root, validator=build_validator(ctx), catalog=ctx.catalog,
        runtime=runtime, group=group, name=name,
        capability_class=ctx.platform.klass, accelerator=ctx.platform.accel,
        refetch=refetch, persist_dir=ctx.config_dir)
    entry = result.registry.models[result.name]
    return {"ok": result.ok, "name": result.name, "state": result.state,
            "artifact": entry.artifact, "size_gb": entry.size_gb, "error": entry.error,
            "persisted": result.persisted, "message": result.message}


def benchmark_model(ctx: PlatformContext, name: str, *,
                    base_url: str | None = None) -> dict[str, Any]:
    updated, result = lifecycle.benchmark(
        ctx.registry(), name, descriptors=ctx.descriptors, vector=ctx.vector,
        platform_root=ctx.root, workdir=ctx.workdir, capability_class=ctx.platform.klass,
        accelerator=ctx.platform.accel, base_url=base_url, runner=default_runner,
        http=engine_http(), agent_dir=ctx.root / ctx.config.memory.dir,
        persist_dir=ctx.config_dir)
    record = updated.models[name].benchmarks
    return {"name": name, **record, "state": updated.models[name].state,
            "persisted": result.persisted,
            "message": f"benchmark {name}: {result.tokens_per_s:.1f} tok/s "
                       f"({result.via}, {result.completion_tokens} tokens in "
                       f"{result.latency_s:.1f}s)"}


def apply_request(ctx: PlatformContext, request_id: str, *, reload: bool = False) -> dict[str, Any]:
    reloader, health = reload_seams(reload)
    result = activation.apply(ctx.platform, ctx.queue, request_id, actor=ctx.actor,
                              reloader=reloader, health=health)
    return {"request": request_id, "ok": result.ok, "generation": result.generation,
            "message": result.message, "changed": result.changed,
            "rolled_back": result.rolled_back}


def _proposal_outcome(ctx: PlatformContext, req: ChangeRequest, *, reload: bool) -> dict[str, Any]:
    """A proposal is applied at once when policy approved it (no serving
    role affected); otherwise it waits in the queue."""
    applied = apply_request(ctx, req.id, reload=reload) if req.status == "approved" else None
    return {"request": req.model_dump(mode="json"), "applied": applied}


def activate_model(ctx: PlatformContext, name: str, *, group: str | None = None,
                   role: str | None = None, position: int | None = None,
                   reload: bool = False) -> dict[str, Any]:
    req = activation.activate(ctx.platform, ctx.queue, ctx.registry(), name,
                              requested_by=ctx.actor, group=group, role=role, position=position)
    return _proposal_outcome(ctx, req, reload=reload)


def upgrade_model(ctx: PlatformContext, old: str, new: str, *,
                  reload: bool = False) -> dict[str, Any]:
    req = activation.upgrade(ctx.platform, ctx.queue, ctx.registry(), old, new,
                             requested_by=ctx.actor)
    return _proposal_outcome(ctx, req, reload=reload)


def rollback_generation(ctx: PlatformContext, *, to_generation: int | None = None,
                        reason: str = "", reload: bool = False,
                        notify: Callable[[str], None] | None = None) -> dict[str, Any]:
    ctx.registry()
    reloader, health = reload_seams(reload)
    result = activation.rollback(ctx.platform, ctx.queue, actor=ctx.actor,
                                 to_generation=to_generation, reason=reason,
                                 reloader=reloader, health=health, notify=notify)
    return {"ok": result.ok, "generation": result.generation, "message": result.message,
            "rolled_back": result.rolled_back}


def retire_model(ctx: PlatformContext, name: str) -> dict[str, Any]:
    updated, persisted = lifecycle.retire(ctx.registry(), name, persist_dir=ctx.config_dir)
    return {"name": name, "state": "retired", "generation": updated.generation,
            "persisted": persisted,
            "message": f"retired {name} (generation {updated.generation}); weights kept for "
                       "rollback"}


def uninstall_model(ctx: PlatformContext, name: str, *, force: bool = False) -> dict[str, Any]:
    updated, persisted = lifecycle.uninstall(ctx.registry(), name, config_dir=ctx.config_dir,
                                             force=force, persist_dir=ctx.config_dir)
    return {"name": name, "removed": True, "generation": updated.generation,
            "persisted": persisted,
            "message": f"uninstalled {name}: weights removed, registry generation "
                       f"{updated.generation}"}


def explain_role(ctx: PlatformContext, role: str) -> dict[str, Any]:
    registry = ctx.registry()
    resolution = registry.resolve(role)
    spec = registry.roles[role]
    checks: dict[str, Any] = {}
    for name in [resolution.primary, *resolution.fallbacks]:
        entry = registry.models[name]
        descriptor = ctx.descriptors.get(entry.provider)
        if descriptor is None:
            checks[name] = {"ok": False, "checks": {},
                            "failures": [f"no descriptor for '{entry.provider}'"]}
        else:
            passed, failures = check_contract(name, entry, descriptor, spec.requires,
                                              ctx.platform.klass, ctx.platform.accel)
            checks[name] = {"ok": not failures, "checks": passed, "failures": failures}
    return {"role": role, "generation": registry.generation,
            "source": {"pin": list(spec.pin), "group": spec.group},
            "primary": resolution.primary, "fallbacks": resolution.fallbacks,
            "reason": resolution.reason, "contract": spec.requires.model_dump(),
            "checks": checks, "ok": all(c["ok"] for c in checks.values())}


def generation_history(ctx: PlatformContext, *, limit: int = 10) -> dict[str, Any]:
    ctx.registry()
    numbers = list_generations(ctx.config_dir)[-max(1, limit):]
    entries: list[dict[str, Any]] = []
    previous: RegistryV2 | None = None
    for number in numbers:
        generation = load_generation(ctx.config_dir, number)
        diff = diff_generations(previous, generation) if previous is not None else []
        entries.append({"generation": number, "saved_at": generation.saved_at,
                        "note": generation.note, "diff": diff,
                        "active": sorted(n for n, e in generation.models.items()
                                         if e.state == "active")})
        previous = generation
    return {"generations": entries}


def catalog_listing(ctx: PlatformContext) -> dict[str, Any]:
    return {"count": len(ctx.catalog.entries),
            "sources": f"packaged seed + {ctx.config_dir / 'catalog'}/*.yaml",
            "entries": {k: v.model_dump() for k, v in ctx.catalog.entries.items()}}


def catalog_recommendations(ctx: PlatformContext, group: str, *,
                            runtime: str | None = None) -> dict[str, Any]:
    registry = ctx.registry(required=False)
    contracts = [spec.requires for spec in (registry.roles.values() if registry else [])
                 if spec.group == group]
    ranked = recommend(ctx.catalog, group, ctx.vector, ctx.descriptors, runtime=runtime,
                       contracts=contracts, capability_class=ctx.platform.klass,
                       accelerator=ctx.platform.accel)
    return {"group": group, "class": ctx.platform.klass, "accelerator": ctx.platform.accel,
            "candidates": [{"id": r.catalog_id, "format": r.format, "provider": r.provider,
                            "eligible": r.eligible, "verdict": r.verdict.model_dump(),
                            "contract_failures": r.contract_failures, "explain": r.explain()}
                           for r in ranked]}


def list_requests(ctx: PlatformContext, *, status: str | None = None) -> dict[str, Any]:
    return {"requests": [r.model_dump(mode="json") for r in ctx.queue.list(status=status)]}


def show_request(ctx: PlatformContext, request_id: str) -> dict[str, Any]:
    return {"request": ctx.queue.get(request_id).model_dump(mode="json")}


def approve_request(ctx: PlatformContext, request_id: str, *, reason: str = "",
                    reload: bool = False) -> dict[str, Any]:
    req = ctx.queue.approve(request_id, by=ctx.actor, reason=reason)
    return {"request": req.model_dump(mode="json"),
            "applied": apply_request(ctx, req.id, reload=reload)}


def reject_request(ctx: PlatformContext, request_id: str, *, reason: str = "") -> dict[str, Any]:
    req = ctx.queue.reject(request_id, by=ctx.actor, reason=reason)
    return {"request": req.model_dump(mode="json"), "applied": None}


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


def list_projects(ctx: PlatformContext) -> dict[str, Any]:
    return {"projects": _load_projects(ctx)}


def add_project(ctx: PlatformContext, path_arg: str, *, name: str | None = None) -> dict[str, Any]:
    path = Path(path_arg).expanduser().resolve()
    if not (path / ".git").exists():
        raise LifecycleError(f"{path} is not a git repository")
    projects = _load_projects(ctx)
    name = name or path.name
    if any(p["path"] == str(path) or p["name"] == name for p in projects):
        raise LifecycleError(f"project '{name}' ({path}) is already registered")
    project = {"name": name, "path": str(path),
               "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "added_by": ctx.actor}
    projects.append(project)
    _save_projects(ctx, projects)
    ctx.queue.record("project.added", ctx.actor, name=name, path=str(path))
    return {"project": project, "removed": None, "message": f"registered project {name} → {path}"}


def resolve_project(ctx: PlatformContext, ref: str) -> dict[str, str]:
    """The registered project named or located at ``ref`` — the allowlist
    the chat-ops boundary relies on (OPENWEBUI_INTEGRATION §5): a run may
    only start on a project a human registered."""
    resolved = str(Path(ref).expanduser().resolve()) if "/" in ref else ref
    for project in _load_projects(ctx):
        if project["name"] == resolved or project["path"] == resolved:
            return project
    raise LifecycleError(f"no registered project named or located at '{ref}' — register it "
                         "first: local-ezai project add <path>")


def remove_project(ctx: PlatformContext, target: str) -> dict[str, Any]:
    projects = _load_projects(ctx)
    resolved = str(Path(target).expanduser().resolve()) if "/" in target else target
    kept = [p for p in projects if p["name"] != resolved and p["path"] != resolved]
    if len(kept) == len(projects):
        raise LifecycleError(f"no registered project named or located at '{target}'")
    _save_projects(ctx, kept)
    ctx.queue.record("project.removed", ctx.actor, target=target)
    return {"project": None, "removed": target, "message": f"removed project {target}"}


# ── project memory (PR-19, contract 1.1.0) ──────────────────────────────────


def _memory_store(ctx: PlatformContext, project: dict[str, str]):
    from agentd.memory import MemoryStore

    return MemoryStore(Path(project["path"]) / ctx.config.memory.dir)


def project_memory(ctx: PlatformContext, ref: str, *, kind: str | None = None,
                   search: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Browse a registered project's memory — the store ``local-ezai memory``
    prints (ADR-017: SQLite in the origin repository's ``.agent/``); the
    Admin Center's Memory page reads it through the control plane."""
    from agentd.memory import ALL_KINDS

    project = resolve_project(ctx, ref)
    if kind is not None and kind not in ALL_KINDS:
        raise LifecycleError(f"unknown memory kind '{kind}' — one of: {', '.join(ALL_KINDS)}")
    store = _memory_store(ctx, project)
    try:
        kinds = [kind] if kind else None
        if not store.exists:
            records = []
        elif search:
            records = store.search(search, kinds=kinds, limit=limit)
        else:
            records = store.recent(kinds=kinds, limit=limit)
        return {"project": project["name"], "path": str(store.db_path), "exists": store.exists,
                "total": store.count(), "counts": {k: store.count(k) for k in ALL_KINDS},
                "records": [r.to_dict() for r in records]}
    finally:
        store.close()


def add_project_memory(ctx: PlatformContext, ref: str, *, kind: str, text: str) -> dict[str, Any]:
    """``local-ezai memory --add`` for a registered project: a curated rule,
    style or architecture decision, remembered and exported to
    ``lessons_learned.json``. Fixes and implementation history are recorded
    by runs, never by hand."""
    from agentd.memory import CURATED_KINDS

    project = resolve_project(ctx, ref)
    if kind not in CURATED_KINDS:
        raise LifecycleError(f"curated memory is one of {', '.join(CURATED_KINDS)} — fixes and "
                             "implementation history are recorded by runs")
    if not text.strip():
        raise LifecycleError("a memory entry needs text")
    store = _memory_store(ctx, project)
    try:
        record_id = store.record(kind=kind, title=text.strip()[:60], content=text.strip(),
                                 run_id="manual", data={"by": ctx.actor})
        store.export_lessons()
    finally:
        store.close()
    ctx.queue.record("memory.added", ctx.actor, project=project["name"], kind=kind,
                     memory_id=record_id)
    return {"id": record_id, "kind": kind, "project": project["name"],
            "message": f"remembered #{record_id} [{kind}] for {project['name']}"}


# ── CLI verbs: format what the operations return ─────────────────────────────


def _report_proposal(args: argparse.Namespace, data: dict[str, Any]) -> int:
    req = ChangeRequest.model_validate(data["request"])
    lines = _request_lines(req)
    applied = data.get("applied")
    if applied is None:
        lines.append(f"awaiting human approval: local-ezai governance approve {req.id}")
        _emit(args, data, lines)
        return 0
    lines += ["approved by policy (no serving role affected) — applying", applied["message"]]
    _emit(args, data, lines)
    return 0 if applied["ok"] else 1


# ── model verbs ──────────────────────────────────────────────────────────────


#: What a verb formatter needs: ``actor`` and ``ops`` — direct or connected.
Surface = PlatformContext | ConnectedContext


def cmd_model_install(ctx: Surface, args: argparse.Namespace) -> int:
    data = ctx.ops.install_model(args.ref, name=args.name, runtime=args.runtime,
                                 group=args.group, refetch=args.refetch)
    lines = [data["message"]]
    if data["error"]:
        lines.append(f"  error: {data['error']}")
    if data["ok"]:
        lines.append(f"  next: local-ezai model benchmark {data['name']}")
    _emit(args, data, lines)
    return 0 if data["ok"] else 1


def cmd_model_benchmark(ctx: Surface, args: argparse.Namespace) -> int:
    data = ctx.ops.benchmark_model(args.name, base_url=args.base_url)
    _emit(args, data,
          [data["message"],
           f"  state: {data['state']}"
           + ("" if data["persisted"] else f" — {lifecycle.UNSERVABLE_HINT}"),
           f"  next: local-ezai model activate {args.name} --group <reasoning|coding|chat>"])
    return 0


def cmd_model_activate(ctx: Surface, args: argparse.Namespace) -> int:
    return _report_proposal(args, ctx.ops.activate_model(
        args.name, group=args.group, role=args.role, position=args.position,
        reload=args.reload))


def cmd_model_upgrade(ctx: Surface, args: argparse.Namespace) -> int:
    return _report_proposal(args, ctx.ops.upgrade_model(args.old, args.new, reload=args.reload))


def cmd_model_rollback(ctx: Surface, args: argparse.Namespace) -> int:
    data = ctx.ops.rollback_generation(to_generation=args.to_generation,
                                       reason=args.reason or "", reload=args.reload,
                                       notify=print)
    _emit(args, data, [data["message"]])
    return 0 if data["ok"] else 1


def cmd_model_retire(ctx: Surface, args: argparse.Namespace) -> int:
    data = ctx.ops.retire_model(args.name)
    _emit(args, data, [data["message"]])
    return 0


def cmd_model_uninstall(ctx: Surface, args: argparse.Namespace) -> int:
    data = ctx.ops.uninstall_model(args.name, force=args.force)
    _emit(args, data, [data["message"]])
    return 0


def cmd_model_explain(ctx: Surface, args: argparse.Namespace) -> int:
    data = ctx.ops.explain_role(args.role)
    source = data["source"]
    origin = ("pin " + ", ".join(source["pin"])) if source["pin"] else "group " + source["group"]
    lines = [f"role {args.role} — generation {data['generation']}",
             f"  source:   {origin}",
             f"  primary:  {data['primary']}",
             f"  fallback: {', '.join(data['fallbacks']) or '(none)'}"]
    lines += [f"  why:      {reason}" for reason in data["reason"]]
    for name, check in data["checks"].items():
        status = "ok" if check["ok"] else "FAIL"
        if check["failures"]:
            detail = " — " + "; ".join(check["failures"])
        else:
            passed_keys = [k for k, v in check.get("checks", {}).items() if v]
            detail = f" ({', '.join(passed_keys) or 'no requirements'})"
        lines.append(f"  contract: {name} [{status}]{detail}")
    _emit(args, data, lines)
    return 0 if data["ok"] else 1


def cmd_model_history(ctx: Surface, args: argparse.Namespace) -> int:
    data = ctx.ops.generation_history(limit=args.limit)
    lines: list[str] = []
    for entry in data["generations"]:
        lines.append(f"generation {entry['generation']}  {entry['saved_at']}  {entry['note']}")
        lines += [f"    {line}" for line in entry["diff"]]
    _emit(args, data, lines or ["(no generations yet)"])
    return 0


def cmd_model_catalog(ctx: Surface, args: argparse.Namespace) -> int:
    if args.group:
        data = ctx.ops.catalog_recommendations(args.group, runtime=args.runtime)
        _emit(args, data,
              [f"catalog recommendations for group {args.group} on class "
               f"{data['class']} ({data['accelerator']})",
               *(f"  {c['explain']}" for c in data["candidates"])]
              or [f"(no catalog entry lists group {args.group})"])
        return 0
    data = ctx.ops.catalog_listing()
    lines = []
    for catalog_id, entry in sorted(data["entries"].items()):
        variants = ", ".join(f"{fmt} {v['size_gb']:.1f} GB"
                             for fmt, v in entry["variants"].items())
        lines.append(f"  {catalog_id:32} {', '.join(entry['groups']):22} "
                     f"{entry['license']:14} {variants}")
    _emit(args, data, [f"catalog: {data['count']} entr(y/ies) ({data['sources']})", *lines])
    return 0


# ── governance verbs ─────────────────────────────────────────────────────────


def cmd_governance_list(ctx: Surface, args: argparse.Namespace) -> int:
    data = ctx.ops.list_requests(status=args.status)
    requests = [ChangeRequest.model_validate(r) for r in data["requests"]]
    _emit(args, data,
          [f"  {r.id}  {r.status:10} {r.kind:11} gen {r.base_generation}  {r.title}"
           for r in requests] or ["(governance queue is empty)"])
    return 0


def cmd_governance_show(ctx: Surface, args: argparse.Namespace) -> int:
    data = ctx.ops.show_request(args.id)
    _emit(args, data, _request_lines(ChangeRequest.model_validate(data["request"])))
    return 0


def cmd_governance_approve(ctx: Surface, args: argparse.Namespace) -> int:
    data = ctx.ops.approve_request(args.id, reason=args.reason or "", reload=args.reload)
    applied = data["applied"]
    _emit(args, data, [f"approved {args.id} by {ctx.actor} — applying", applied["message"]])
    return 0 if applied["ok"] else 1


def cmd_governance_reject(ctx: Surface, args: argparse.Namespace) -> int:
    data = ctx.ops.reject_request(args.id, reason=args.reason or "")
    req = ChangeRequest.model_validate(data["request"])
    _emit(args, data,
          [f"rejected {req.id} by {ctx.actor}: {req.decision.reason if req.decision else ''}"])
    return 0


# ── project verbs (chat-ops allowlist, consumed by the P3 tool server) ───────


def cmd_project_add(ctx: Surface, args: argparse.Namespace) -> int:
    data = ctx.ops.add_project(args.path, name=args.name)
    _emit(args, data, [data["message"]])
    return 0


def cmd_project_list(ctx: Surface, args: argparse.Namespace) -> int:
    data = ctx.ops.list_projects()
    _emit(args, data,
          [f"  {p['name']:24} {p['path']}" for p in data["projects"]]
          or ["(no projects registered — local-ezai project add <path>)"])
    return 0


def cmd_project_remove(ctx: Surface, args: argparse.Namespace) -> int:
    data = ctx.ops.remove_project(args.target)
    _emit(args, data, [data["message"]])
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
    # Per-model facts the Admin Center's role-first cards need (PR-17):
    # group membership, context, source format, license — additive keys in
    # the contract's untyped ``models`` object.
    models = {name: {"state": e.state, "runtime": e.provider, "size_gb": e.size_gb,
                     "tokens_per_s": e.benchmarks.get("tokens_per_s"),
                     "groups": list(e.groups), "context": e.context,
                     "format": next(iter(e.source), ""), "license": e.license}
              for name, e in (registry.models.items() if registry else {})}
    active_runtimes = sorted({e.provider for e in (registry.models.values() if registry else [])
                              if e.state == "active"})
    return {"platform_root": str(ctx.root), "config_dir": str(ctx.config_dir),
            "capability_class": ctx.platform.klass, "accelerator": ctx.platform.accel,
            "system_memory_gb": ctx.vector.system_memory_gb, "cpu_cores": ctx.vector.cpu_cores,
            "generation": registry.generation if registry else None,
            "note": registry.note if registry else None,
            "rendered_generation": _manifest_generation(ctx),
            "slot_runtime": active_runtimes[0] if len(active_runtimes) == 1 else active_runtimes,
            "models": models, "pending_approvals": len(pending)}


def status_snapshot(ctx: PlatformContext) -> dict[str, Any]:
    """``local-ezai status`` in direct mode: the snapshot + this host's view
    of the stack health (the connected twin asks the daemon)."""
    data = platform_snapshot(ctx)
    engine_port = os.environ.get("LLM_PORT", str(ENGINE_PORT))
    router_port = os.environ.get("LITELLM_PORT", str(ROUTER_PORT))
    control_port = os.environ.get(CONTROL_PORT_ENV, str(CONTROL_PORT))
    data["health"] = {
        "engine": http_probe(f"http://localhost:{engine_port}/health") == 200,
        "router": http_probe(f"http://localhost:{router_port}/health/liveliness") == 200,
        "control": http_probe(f"http://localhost:{control_port}/health") == 200,
    }
    data["transport"] = "direct (in-process)"
    return data


#: name → direct operation; ``DirectOps`` binds them to a context. Every name
#: has a ``ConnectedOps`` method of the same signature (control/client.py).
_DIRECT_OPERATIONS: dict[str, Callable[..., dict[str, Any]]] = {
    "status_snapshot": status_snapshot,
    "install_model": install_model, "benchmark_model": benchmark_model,
    "activate_model": activate_model, "upgrade_model": upgrade_model,
    "rollback_generation": rollback_generation, "retire_model": retire_model,
    "uninstall_model": uninstall_model, "explain_role": explain_role,
    "generation_history": generation_history, "catalog_listing": catalog_listing,
    "catalog_recommendations": catalog_recommendations,
    "list_requests": list_requests, "show_request": show_request,
    "approve_request": approve_request, "reject_request": reject_request,
    "list_projects": list_projects, "add_project": add_project,
    "remove_project": remove_project,
}


def cmd_status(ctx: PlatformContext | ConnectedContext, args: argparse.Namespace) -> int:
    data = ctx.ops.status_snapshot()
    health, models, pending = data["health"], data["models"], data["pending_approvals"]
    bootstrapped = data["generation"] is not None
    lines = [f"platform:   {data['platform_root']}",
             f"transport:  {data['transport']}",
             f"hardware:   class {data['capability_class']} · accelerator "
             f"{data['accelerator']} · {data['system_memory_gb']:.0f} GB RAM · "
             f"{data['cpu_cores']} cores",
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


def run_bootstrap(ctx: PlatformContext, env_path: Path, *, dry_run: bool = False,
                  skip_benchmark: bool = False, force: bool = False, reload: bool = False):
    """The bootstrap with this host's seams (side-load validator, engine HTTP,
    docker runner) — shared by ``local-ezai bootstrap`` and the setup
    pipeline (PR-22). Raises ``PlatformError`` (no .env) or
    ``bootstrap.BootstrapError`` (the seeds' problems, every fix listed)."""
    from agentd import bootstrap as bs

    if not env_path.is_file():
        raise PlatformError(f"no {env_path} — run ./install.sh (or copy .env.example to .env "
                            f"and set {bs.RUNTIME_KEY} + {' / '.join(bs.SEED_GROUPS)})")
    seeds = bs.read_seeds(bs.parse_env(env_path.read_text(encoding="utf-8")))
    reloader, health = reload_seams(reload)

    def benchmark_fn(registry, name):
        updated, _ = lifecycle.benchmark(
            registry, name, descriptors=ctx.descriptors, vector=ctx.vector,
            platform_root=ctx.root, workdir=ctx.workdir, capability_class=ctx.platform.klass,
            accelerator=ctx.platform.accel, runner=default_runner, http=engine_http(),
            agent_dir=ctx.root / ctx.config.memory.dir)
        return updated

    return bs.bootstrap(seeds, ctx.platform, ctx.queue, ctx.catalog, actor=ctx.actor,
                        validator=build_validator(ctx), env_path=env_path,
                        benchmark_fn=benchmark_fn, skip_benchmark=skip_benchmark,
                        reloader=reloader, health=health, dry_run=dry_run, force=force)


def cmd_bootstrap(ctx: PlatformContext, args: argparse.Namespace) -> int:
    from agentd import bootstrap as bs

    env_path = Path(args.env).expanduser() if args.env else ctx.root / ".env"
    try:
        result = run_bootstrap(ctx, env_path, dry_run=args.dry_run,
                               skip_benchmark=args.skip_benchmark, force=args.force,
                               reload=args.reload)
    except bs.BootstrapError as exc:
        _emit(args, {"ok": False, "error": str(exc)}, [str(exc)])
        return 1
    seeds = bs.read_seeds(bs.parse_env(env_path.read_text(encoding="utf-8")))
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


# ── setup · init (PR-22): steps 4–8 of the first run, the fallback wizard ────


def cmd_setup(ctx: PlatformContext, args: argparse.Namespace) -> int:
    from agentd.setup_pipeline import SetupOptions, SetupPipeline

    options = SetupOptions(root=ctx.root, profile=args.profile, skip_images=args.skip_images,
                           skip_smoke=args.skip_smoke, skip_banner=args.skip_banner,
                           env_path=Path(args.env).expanduser() if args.env else None)
    quiet = getattr(args, "as_json", False)
    report = SetupPipeline(ctx, options, say=(lambda text: None) if quiet else print).run()
    if quiet:
        _emit(args, report.as_dict(), [])
    else:
        print("\n".join([""] + report.card()))
    return report.exit_code


def cmd_init(ctx: PlatformContext, args: argparse.Namespace) -> int:
    from agentd.setup_pipeline import InitOptions, SetupError, run_init

    options = InitOptions(root=ctx.root, assume_yes=args.assume_yes, profile=args.profile,
                          env_path=Path(args.env).expanduser() if args.env else None)
    quiet = getattr(args, "as_json", False)
    try:
        outcome = run_init(ctx, options, say=(lambda text: None) if quiet else print)
    except SetupError as exc:
        _emit(args, {"ok": False, "error": str(exc)}, [str(exc)])
        return 2
    if isinstance(outcome, int):
        if quiet:
            _emit(args, {"ok": False, "exit_code": outcome}, [])
        return outcome
    if quiet:
        _emit(args, outcome.as_dict(), [])
    else:
        print("\n".join([""] + outcome.card()))
    return outcome.exit_code


# ── argparse wiring + dispatch ───────────────────────────────────────────────


def add_platform_parsers(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    def leaf(parent: argparse._SubParsersAction, name: str, help_: str) -> argparse.ArgumentParser:
        parser = parent.add_parser(name, help=help_, parents=[common])
        parser.add_argument("--json", action="store_true", dest="as_json")
        parser.add_argument("--transport", choices=TRANSPORTS, default=None,
                            help=f"auto (default; ${TRANSPORT_ENV}): use the control plane "
                                 f"when it answers at ${URL_ENV} · connected: require it · "
                                 "direct: act in-process")
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

    p = leaf(sub, "setup", "First run, steps 4–8: bootstrap → images → up → wait-ready → "
                           "smoke → report (after ./install.sh; idempotent)")
    p.add_argument("--profile", default=None, help="gpu · cpu · n97 · n97-igpu (default: "
                                                   "from the detected or asserted class)")
    p.add_argument("--env", default=None, help="Path to .env (default: <platform>/.env)")
    p.add_argument("--skip-images", action="store_true", dest="skip_images",
                   help="Do not pull/build images (already done)")
    p.add_argument("--skip-smoke", action="store_true", dest="skip_smoke",
                   help="Skip the smoke checks (chat · RAG · plan · model probes)")
    p.add_argument("--skip-banner", action="store_true", dest="skip_banner",
                   help="Do not show the Platform-ready card in the WebUI")
    p = leaf(sub, "init", "Fallback wizard when .env has no model seeds: hardware check → "
                          "recommended model set → seeds → setup")
    p.add_argument("-y", "--yes", action="store_true", dest="assume_yes",
                   help="Accept the recommended set without asking")
    p.add_argument("--profile", default=None)
    p.add_argument("--env", default=None)


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
    ("bootstrap", None): cmd_bootstrap, ("setup", None): cmd_setup, ("init", None): cmd_init,
}


def control_url(config: AgentdConfig) -> str:
    return (config.control.url or f"http://localhost:{config.control.port}").rstrip("/")


def select_transport(command: str, args: argparse.Namespace, config: AgentdConfig,
                     environ: Mapping[str, str] | None = None) -> str:
    """``connected`` or ``direct`` (PR-11). Host-only verbs are always direct;
    otherwise ``--transport`` > ``$EZAI_TRANSPORT`` > ``auto``. Auto probes
    the daemon's liveness once; a requested ``connected`` that gets no
    answer, or a reachable daemon without a configured token, fails fast —
    never a silent fallback that would split the audit trail."""
    env = os.environ if environ is None else environ
    if command in HOST_ONLY_COMMANDS:
        return "direct"
    requested = getattr(args, "transport", None) or env.get(TRANSPORT_ENV) or "auto"
    if requested not in TRANSPORTS:
        raise PlatformError(f"unknown transport '{requested}' — one of: {', '.join(TRANSPORTS)}")
    if requested == "direct":
        return "direct"
    url = control_url(config)
    reachable = control_client.probe_control_plane(url)
    if requested == "connected" and not reachable:
        raise PlatformError(f"control plane unreachable at {url} — start it (make control-up), "
                            f"point {URL_ENV} at it, or use --transport direct")
    if reachable and not config.control.token:
        raise PlatformError(f"control plane reachable at {url} but no service token is "
                            f"configured — set {CONTROL_TOKEN_ENV} (or control.token), or use "
                            "--transport direct")
    return "connected" if reachable else "direct"


def dispatch_platform(command: str, args: argparse.Namespace, config: AgentdConfig,
                      project: Path) -> int:
    try:
        actor = default_actor(getattr(args, "by", None))
        if select_transport(command, args, config) == "connected":
            url = control_url(config)
            ctx: PlatformContext | ConnectedContext = ConnectedContext(
                actor=actor, url=url,
                ops=control_client.connect(url, actor=actor, token=config.control.token))
        else:
            ctx = build_context(config, project, actor=actor)
        handler = HANDLERS[(command, getattr(args, "verb", None))]
        return handler(ctx, args)
    except ControlPlaneError as exc:
        # Connected mode: the daemon's envelope, printed verbatim (PR-11).
        if getattr(args, "as_json", False):
            print(json.dumps({"error": exc.as_dict()}, indent=2))
        log.error("%s", exc.message)
        return exc.exit_code
    except platform_exceptions() as exc:
        # The shared error object (PR-9): the same code/message/fix the
        # control plane returns; `--json` prints it, text mode logs it.
        info = classify(exc)
        if getattr(args, "as_json", False):
            print(json.dumps({"error": info.as_dict()}, indent=2))
        log.error("%s", exc)
        return info.exit_code
