"""Activation · upgrade · rollback — proposals and the atomic apply protocol
(PR-5, ADR-027; docs/MODEL_LIFECYCLE_MANAGEMENT.md §2–§4,
docs/MODEL_ROUTING_DESIGN.md §6, docs/MODEL_GOVERNANCE_V2.md §2).

Proposals (``activate``, ``upgrade``, generic ``propose``) turn an intent
into a ``ChangeRequest``: the proposed registry is validated the way apply
will validate it (integrity, resolution completeness, capability
negotiation, slot rule — a dry render), the diff and affected roles are
computed, evidence is attached, and the request enters the governance
queue. Nothing is written to the registry.

Apply (MODEL_LIFECYCLE §4)::

    approved request → dry render (pure)      never write what cannot render
      → save generation N+1 (snapshot)        append-only history
      → write rendered artifacts (drift-checked)
      → reload consumers (only changed artifacts)
      → health probe
      ok:   request applied, audit
      fail: SELF-ROLLBACK — generation N's content saved as N+2, re-rendered,
            reloaded; request failed with the reason, audit

Rollback to a named generation is the same protocol without approval,
loudly audited and notifying. Because history is append-only, a rollback
is a *new* generation whose content equals the target — ``registry.yaml``
is always the latest number and the trail shows what happened.

Reload and health are pluggable: ``ComposeReloader`` / ``EngineHealth``
drive the real stack (used from the PR-7 cutover on); ``None`` means
render-only (pre-cutover default) and the audit record says so.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agentd.capability import CapabilityVector, classify, fit
from agentd.governance import ChangeRequest, GovernanceQueue
from agentd.lifecycle import (
    EngineHTTP,
    HttpxEngineHTTP,
    LifecycleError,
    Runner,
    default_runner,
    probe_model,
    transition,
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
    save_generation,
)
from agentd.render import (
    ENGINE_COMPOSE_FILENAME,
    ENGINE_PORT,
    ENGINE_SERVICE,
    LITELLM_FILENAME,
    MANIFEST_FILENAME,
    PRESET_FILENAME,
    EmbeddingEntry,
    RenderError,
    RenderResult,
    render,
    rendered_dir,
    served_id_for,
    write_rendered,
)
from agentd.runtime_descriptor import RuntimeDescriptor

log = get_logger("activation")

#: The stack's router service (ADR-001) — spelled once, like ENGINE_SERVICE.
ROUTER_SERVICE = "litellm"
BASE_COMPOSE_FILENAME = "docker-compose.yml"


class HealthError(RuntimeError):
    """The new generation does not serve — triggers self-rollback."""


@dataclass
class Platform:
    """Everything a render/apply needs to know about this installation."""

    config_dir: Path
    platform_root: Path
    descriptors: dict[str, RuntimeDescriptor]
    vector: CapabilityVector
    capability_class: str | None = None
    accelerator: str | None = None
    #: Slot runtime (AI_RUNTIME); None → inferred from the active set.
    runtime: str | None = None
    embedding: EmbeddingEntry | None = None
    rendered_dir_rel: str = "./config/rendered"

    @property
    def klass(self) -> str:
        return self.capability_class or classify(self.vector)

    @property
    def accel(self) -> str:
        return self.accelerator or self.vector.accelerator

    @property
    def rendered(self) -> Path:
        return rendered_dir(self.config_dir)

    def render(self, registry: RegistryV2) -> RenderResult:
        return render(registry, self.descriptors, self.vector,
                      capability_class=self.capability_class, accelerator=self.accelerator,
                      runtime=self.runtime, embedding=self.embedding,
                      rendered_dir_rel=self.rendered_dir_rel)


# ── proposals ────────────────────────────────────────────────────────────────


def _chain(registry: RegistryV2, role: str) -> dict[str, Any] | None:
    try:
        resolution = registry.resolve(role)
    except RegistryResolutionError:
        return None
    return {"primary": resolution.primary, "fallbacks": list(resolution.fallbacks)}


def affected_roles(before: RegistryV2, after: RegistryV2) -> dict[str, dict[str, Any]]:
    """Roles whose resolution chain differs (the approval object of
    MODEL_GOVERNANCE_V2 §2: govern roles, reveal models)."""
    affected: dict[str, dict[str, Any]] = {}
    for role in sorted(set(before.roles) | set(after.roles)):
        old, new = _chain(before, role), _chain(after, role)
        if old != new:
            affected[role] = {"before": old, "after": new}
    return affected


def _try_render(platform: Platform, registry: RegistryV2) -> RenderResult | None:
    try:
        return platform.render(registry)
    except (RenderError, RegistryError):
        return None


def propose(platform: Platform, queue: GovernanceQueue, current: RegistryV2,
            proposed: RegistryV2, *, kind: str, title: str, requested_by: str,
            proposed_by: str = "human",
            evidence: dict[str, Any] | None = None) -> ChangeRequest:
    """Validate a proposed generation exactly as apply will, attach the
    evidence, and enter the queue. Loud on anything apply would refuse."""
    proposed = RegistryV2.model_validate(proposed.model_dump(mode="json"))  # integrity
    try:
        result = platform.render(proposed)  # completeness + negotiation + slot rule
    except (RenderError, RegistryResolutionError) as exc:
        raise LifecycleError(f"proposal '{title}' is not servable as declared:\n{exc}") from exc

    before = _try_render(platform, current)
    affected = affected_roles(current, proposed)
    runtime_switch = before is not None and before.runtime != result.runtime
    requires_approval = bool(affected) or runtime_switch
    newly_active = [name for name, entry in proposed.models.items()
                    if entry.state == "active"
                    and (name not in current.models or current.models[name].state != "active")]
    involved = set(newly_active)
    for change in affected.values():
        for side in (change.get("before"), change.get("after")):
            if side:
                involved.update([side["primary"], *side["fallbacks"]])
    full_evidence: dict[str, Any] = {
        "benchmarks": {name: proposed.models[name].benchmarks
                       for name in sorted(involved) if name in proposed.models
                       and proposed.models[name].benchmarks},
        "fit": {name: fit(proposed.models[name], platform.vector).model_dump()
                for name in newly_active},
        "capability_report": [check.model_dump() for check in result.report],
        "runtime": {"before": before.runtime if before else None, "after": result.runtime,
                    "switch": runtime_switch},
        "capability_class": result.capability_class,
        **(evidence or {}),
    }
    request = ChangeRequest(
        kind=kind, title=title, requested_by=requested_by, proposed_by=proposed_by,
        base_generation=current.generation, proposed=proposed.model_dump(mode="json"),
        diff=diff_generations(current, proposed), affected_roles=affected,
        evidence=full_evidence, requires_approval=requires_approval)
    return queue.submit(request)


def _require_evidence(entry_name: str, registry: RegistryV2) -> None:
    entry = registry.models.get(entry_name)
    if entry is None:
        raise LifecycleError(f"unknown model '{entry_name}'")
    if entry.state == "active":
        return
    if entry.state != "benchmarked":
        raise LifecycleError(
            f"model '{entry_name}' is {entry.state} — activation needs benchmark evidence: "
            "install it, then `model benchmark` it first")


def activate(platform: Platform, queue: GovernanceQueue, registry: RegistryV2, name: str, *,
             requested_by: str, group: str | None = None, role: str | None = None,
             position: int | None = None, proposed_by: str = "human") -> ChangeRequest:
    """``model activate <name> [--group G] [--role R]`` → a change request.

    - ``group``: make the model a member of that group; ``position`` 0 (the
      default with a group) makes it the primary, ``None`` appends.
    - ``role``: pin the role to this model (explicit chain, PR-1 semantics).
    - neither: the model joins each of its declared groups as a fallback.
    """
    _require_evidence(name, registry)
    proposed = registry.model_copy(deep=True)
    entry = proposed.models[name]
    if entry.state != "active":
        transition(entry, "active", reason="activation")
    placements: list[str] = []
    if group is not None:
        if group not in proposed.groups:
            raise LifecycleError(f"unknown group '{group}' (groups: "
                                 + ", ".join(proposed.groups) + ")")
        members = [m for m in proposed.groups[group] if m != name]
        index = 0 if position is None else max(0, min(position, len(members)))
        members.insert(index, name)
        proposed.groups[group] = members
        if group not in entry.groups:
            entry.groups.append(group)
        placements.append(f"group {group} at position {index}")
    if role is not None:
        if role not in proposed.roles:
            raise LifecycleError(f"unknown role '{role}' (roles: "
                                 + ", ".join(proposed.roles) + ")")
        spec = proposed.roles[role]
        spec.pin = [name, *[p for p in spec.pin if p != name]]
        placements.append(f"pinned to role {role}")
    if group is None and role is None:
        for declared in entry.groups:
            if declared in proposed.groups and name not in proposed.groups[declared]:
                proposed.groups[declared].append(name)
                placements.append(f"group {declared} (fallback)")
    title = f"activate {name}" + (f" — {'; '.join(placements)}" if placements else "")
    return propose(platform, queue, registry, proposed, kind="activation", title=title,
                   requested_by=requested_by, proposed_by=proposed_by)


def upgrade(platform: Platform, queue: GovernanceQueue, registry: RegistryV2, old: str,
            new: str, *, requested_by: str, proposed_by: str = "human") -> ChangeRequest:
    """``model upgrade``: the new version (installed side-by-side, benchmarked)
    takes the old one's place in every group and pin; the old version is
    retired (kept on disk for rollback)."""
    if old not in registry.models:
        raise LifecycleError(f"unknown model '{old}'")
    if registry.models[old].state != "active":
        raise LifecycleError(f"'{old}' is {registry.models[old].state}, not active — "
                             "upgrade replaces a serving version")
    _require_evidence(new, registry)
    proposed = registry.model_copy(deep=True)
    transition(proposed.models[new], "active", reason="upgrade")
    transition(proposed.models[old], "retired", reason="upgraded")
    for group, members in proposed.groups.items():
        proposed.groups[group] = _swap(members, old, new)
    for spec in proposed.roles.values():
        spec.pin = _swap(spec.pin, old, new)
    proposed.models[new].groups = sorted(set(proposed.models[new].groups)
                                         | set(registry.models[old].groups))
    evidence = {"upgrade": {"from": old, "to": new,
                            "benchmark_before": registry.models[old].benchmarks,
                            "benchmark_after": registry.models[new].benchmarks}}
    return propose(platform, queue, registry, proposed, kind="upgrade",
                   title=f"upgrade {old} → {new}", requested_by=requested_by,
                   proposed_by=proposed_by, evidence=evidence)


def _swap(chain: list[str], old: str, new: str) -> list[str]:
    out: list[str] = []
    for member in chain:
        replacement = new if member == old else member
        if replacement not in out:
            out.append(replacement)
    return out


# ── reload + health (pluggable) ──────────────────────────────────────────────


class Reloader(Protocol):
    def reload(self, changed: set[str], platform: Platform) -> None: ...


class HealthCheck(Protocol):
    def check(self, registry: RegistryV2, platform: Platform) -> None: ...


class ComposeReloader:
    """Reload only what changed: the engine slot when its override (or the
    router preset) changed, the router when its config changed."""

    def __init__(self, runner: Runner = default_runner) -> None:
        self._run = runner
        self.calls: list[list[str]] = []

    def reload(self, changed: set[str], platform: Platform) -> None:
        base = str(platform.platform_root / BASE_COMPOSE_FILENAME)
        override = str(platform.rendered / ENGINE_COMPOSE_FILENAME)
        commands: list[list[str]] = []
        if changed & {ENGINE_COMPOSE_FILENAME, PRESET_FILENAME}:
            commands.append(["docker", "compose", "-f", base, "-f", override, "up", "-d",
                             ENGINE_SERVICE])
        if LITELLM_FILENAME in changed:
            commands.append(["docker", "compose", "-f", base, "-f", override, "restart",
                             ROUTER_SERVICE])
        for command in commands:
            self.calls.append(command)
            proc = self._run(command)
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
                raise HealthError(f"reload failed ({' '.join(command[-3:])}): "
                                  + " | ".join(tail))


class EngineHealth:
    """The ``wait-ready`` machinery + one completion through the slot for the
    primary of a role: the generation must actually serve."""

    def __init__(self, http: EngineHTTP | None = None, *, base_url: str | None = None,
                 role: str = "chat", sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.http = http or HttpxEngineHTTP()
        self.base_url = (base_url or f"http://127.0.0.1:{ENGINE_PORT}").rstrip("/")
        self.role, self._sleep, self._clock = role, sleep, clock

    def check(self, registry: RegistryV2, platform: Platform) -> None:
        result = platform.render(registry)
        descriptor = platform.descriptors[result.runtime]
        ready = descriptor.verbs.ready
        started = self._clock()
        while True:
            status, _ = self.http.get(f"{self.base_url}{ready.path}")
            if status == 200:
                break
            if self._clock() - started > ready.timeout_s:
                raise HealthError(f"engine not ready within {ready.timeout_s}s after reload "
                                  f"(last HTTP {status or 'nothing'})")
            self._sleep(min(ready.interval_s, 5))
        role = self.role if self.role in registry.roles else next(iter(registry.roles), "")
        if not role:
            return
        primary = registry.resolve(role).primary
        served = served_id_for(descriptor, primary, registry.models[primary],
                               result.capability_class, result.accelerator)
        probe = probe_model(self.http, self.base_url, served,
                            descriptor.verbs.validate_model.max_tokens, clock=self._clock)
        if not probe.ok:
            raise HealthError(f"role '{role}' → '{primary}' does not answer: {probe.error}")


# ── apply protocol ───────────────────────────────────────────────────────────


@dataclass
class ApplyResult:
    ok: bool
    generation: int | None
    message: str
    request_id: str = ""
    changed: list[str] = field(default_factory=list)
    rolled_back: bool = False
    reloaded: bool = False


def _changed_artifacts(before: RenderResult | None, after: RenderResult) -> set[str]:
    if before is None:
        return set(after.artifacts)
    names = set(before.artifacts) | set(after.artifacts)
    return {n for n in names if before.artifacts.get(n) != after.artifacts.get(n)}


def _manifest_generation(platform: Platform) -> int | None:
    path = platform.rendered / MANIFEST_FILENAME
    if not path.is_file():
        return None
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("generation") if isinstance(data, dict) else None


def reconcile(platform: Platform, queue: GovernanceQueue, *, actor: str,
              reloader: Reloader | None = None) -> bool:
    """Heal a half-applied state (crash between snapshot and render): when
    the rendered manifest lags the registry, re-render the current
    generation. Returns True when something was done."""
    if not registry_path(platform.config_dir).is_file():
        return False
    current = load_registry(platform.config_dir)
    rendered_generation = _manifest_generation(platform)
    if rendered_generation == current.generation:
        return False
    result = platform.render(current)
    write_rendered(result, platform.rendered, force=True)
    if reloader is not None:
        reloader.reload(set(result.artifacts), platform)
    queue.record("generation.reconciled", actor, generation=current.generation,
                 rendered_before=rendered_generation, reloaded=reloader is not None)
    log.warning("rendered artifacts lagged the registry (%s → %d): re-rendered",
                rendered_generation, current.generation)
    return True


def _commit(platform: Platform, content: RegistryV2, current: RegistryV2, note: str, *,
            reloader: Reloader | None, health: HealthCheck | None, force_render: bool,
            ) -> tuple[RegistryV2, set[str]]:
    """Snapshot-then-render-then-reload-then-health. Raises on any failure
    AFTER the snapshot is written — the caller self-rolls-back."""
    content = content.model_copy(deep=True)
    content.generation = current.generation
    result = platform.render(content)  # pure: nothing written yet
    before = _try_render(platform, current)
    changed = _changed_artifacts(before, result)
    saved = save_generation(content, platform.config_dir, note=note)
    result = platform.render(saved)  # headers carry the new generation number
    write_rendered(result, platform.rendered, force=force_render)
    if reloader is not None:
        reloader.reload(changed, platform)
    if health is not None:
        health.check(saved, platform)
    return saved, changed


def _restore(platform: Platform, queue: GovernanceQueue, target: RegistryV2, *, reason: str,
             actor: str, reloader: Reloader | None, event: str) -> RegistryV2:
    """Bring generation ``target`` back as a NEW generation and re-render."""
    current = load_registry(platform.config_dir)
    content = target.model_copy(deep=True)
    content.generation = current.generation
    restored = save_generation(content, platform.config_dir,
                               note=f"{event.replace('_', '-')} to generation "
                                    f"{target.generation}: {reason}"[:300])
    result = platform.render(restored)
    write_rendered(result, platform.rendered, force=True)
    if reloader is not None:
        reloader.reload(set(result.artifacts), platform)
    queue.record(f"generation.{event}", actor, generation=restored.generation,
                 restored_from=target.generation, reason=reason)
    return restored


def apply(platform: Platform, queue: GovernanceQueue, request_id: str, *, actor: str,
          reloader: Reloader | None = None, health: HealthCheck | None = None,
          force_render: bool = False) -> ApplyResult:
    """Apply an APPROVED request (MODEL_LIFECYCLE §4). Never leaves a
    half-applied platform: any failure after the snapshot restores the
    previous generation's content as a new generation."""
    request = queue.get(request_id)
    if request.status == "pending":
        raise LifecycleError(f"{request_id} is awaiting approval — `governance approve` first")
    if request.status != "approved":
        raise LifecycleError(f"{request_id} is {request.status} and cannot be applied")

    if registry_path(platform.config_dir).is_file():
        current = load_registry(platform.config_dir)
    elif request.base_generation == 0:
        current = RegistryV2()  # bootstrap: no generation exists yet
    else:
        raise LifecycleError(f"no registry under {platform.config_dir} but {request_id} "
                             f"was made on generation {request.base_generation}")
    if current.generation != request.base_generation:
        queue.mark(request_id, "superseded", actor,
                   error=f"registry moved to generation {current.generation} since the "
                         f"request was made on {request.base_generation} — re-create it")
        return ApplyResult(False, None, f"{request_id} superseded by generation "
                           f"{current.generation}", request_id)
    reconcile(platform, queue, actor=actor, reloader=reloader)

    proposed = RegistryV2.model_validate(request.proposed)
    note = f"{request.kind} {request_id}: {request.title}"
    try:
        result = platform.render(proposed)  # refuse before writing anything
    except (RenderError, RegistryResolutionError) as exc:
        queue.mark(request_id, "failed", actor, error=str(exc))
        return ApplyResult(False, None, f"{request_id} cannot render: {exc}", request_id)
    del result

    try:
        saved, changed = _commit(platform, proposed, current, note, reloader=reloader,
                                 health=health, force_render=force_render)
    except Exception as exc:  # noqa: BLE001 — any failure past the snapshot self-rolls-back
        reason = f"{type(exc).__name__}: {exc}"
        restored = _restore(platform, queue, current, reason=reason, actor=actor,
                            reloader=reloader, event="self_rollback")
        queue.mark(request_id, "failed", actor, error=reason,
                   restored_generation=restored.generation)
        return ApplyResult(False, restored.generation,
                           f"{request_id} failed and was rolled back to the content of "
                           f"generation {current.generation} (now generation "
                           f"{restored.generation}): {reason}", request_id, rolled_back=True,
                           reloaded=reloader is not None)
    queue.mark(request_id, "applied", actor, generation=saved.generation,
               changed=sorted(changed), reloaded=reloader is not None,
               health_checked=health is not None)
    return ApplyResult(True, saved.generation,
                       f"{request_id} applied as generation {saved.generation}"
                       + ("" if reloader else " (rendered only — consumers not reloaded)"),
                       request_id, sorted(changed), reloaded=reloader is not None)


def rollback(platform: Platform, queue: GovernanceQueue, *, actor: str,
             to_generation: int | None = None, reason: str = "",
             reloader: Reloader | None = None, health: HealthCheck | None = None,
             notify: Callable[[str], None] | None = None) -> ApplyResult:
    """``model rollback [--to-generation N]``: the previous (or named)
    generation's whole routing state comes back — no approval queue,
    loudly audited, notifies. Same commit protocol, same self-rollback."""
    current = load_registry(platform.config_dir)
    generations = list_generations(platform.config_dir)
    target_no = to_generation if to_generation is not None else current.generation - 1
    if target_no not in generations or target_no == current.generation:
        raise LifecycleError(
            f"cannot roll back to generation {target_no} (current {current.generation}; "
            f"available: {', '.join(map(str, generations)) or 'none'})")
    target = load_generation(platform.config_dir, target_no)
    note = f"rollback to generation {target_no}" + (f": {reason}" if reason else "")
    try:
        saved, changed = _commit(platform, target, current, note, reloader=reloader,
                                 health=health, force_render=True)
    except Exception as exc:  # noqa: BLE001
        why = f"{type(exc).__name__}: {exc}"
        restored = _restore(platform, queue, current, reason=why, actor=actor,
                            reloader=reloader, event="self_rollback")
        queue.record("generation.rollback_failed", actor, generation=restored.generation,
                     target=target_no, reason=why)
        return ApplyResult(False, restored.generation,
                           f"rollback to {target_no} failed ({why}); generation "
                           f"{current.generation} content restored as {restored.generation}",
                           rolled_back=True, reloaded=reloader is not None)
    queue.record("generation.rolled_back", actor, generation=saved.generation,
                 target=target_no, reason=reason, changed=sorted(changed), notified=True)
    message = (f"rolled back to the content of generation {target_no} (now generation "
               f"{saved.generation})" + (f": {reason}" if reason else ""))
    (notify or log.warning)(f"ROLLBACK by {actor}: {message}")
    return ApplyResult(True, saved.generation, message, changed=sorted(changed),
                       reloaded=reloader is not None)
