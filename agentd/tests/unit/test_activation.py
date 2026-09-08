"""Governance queue, activation/upgrade proposals, the atomic apply protocol
with self-rollback, generation rollback, retire/uninstall guards (PR-5,
ADR-027; docs/MODEL_LIFECYCLE_MANAGEMENT.md §2–§4, MODEL_GOVERNANCE_V2 §2/§5).

Offline: reload and health are fakes; the platform lives in tmp_path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agentd.activation import (
    ComposeReloader,
    EngineHealth,
    HealthError,
    Platform,
    activate,
    affected_roles,
    apply,
    reconcile,
    rollback,
    upgrade,
)
from agentd.capability import CapabilityVector
from agentd.governance import ChangeRequest, GovernanceError, GovernanceQueue
from agentd.lifecycle import LifecycleError, retire, uninstall
from agentd.registry_v2 import (
    RegistryV2,
    list_generations,
    load_generation,
    load_registry,
    save_generation,
)
from agentd.render import (
    ENGINE_COMPOSE_FILENAME,
    LITELLM_FILENAME,
    MANIFEST_FILENAME,
    PRESET_FILENAME,
    write_rendered,
)
from agentd.runtime_descriptor import load_descriptors
from tests.unit.test_lifecycle import FakeEngineHTTP, FakeRunner

REPO_ROOT = Path(__file__).resolve().parents[3]
CPU_LOW = CapabilityVector(system_memory_gb=15.5, cpu_cores=4, cpu_flags=["avx2"])
DESCRIPTORS = load_descriptors(REPO_ROOT / "config")


def model(state: str, name: str, tokens: float | None = None, **overrides) -> dict:
    entry = {"provider": "llamacpp", "state": state, "context": 8192, "template": "chatml",
             "tool_call_format": "hermes", "groups": ["reasoning", "coding", "chat"],
             "source": {"gguf": f"https://example.invalid/w/{name}.gguf"},
             "artifact": f"/models/{name}.gguf"}
    if tokens is not None:
        entry["benchmarks"] = {"tokens_per_s": tokens, "last": "2026-09-08T00:00:00"}
    entry.update(overrides)
    return entry


def seed_registry() -> RegistryV2:
    """alpha serves everything; beta is benchmarked (activation-ready);
    gamma is only installed (no evidence)."""
    return RegistryV2.model_validate({
        "generation": 0, "providers": ["llamacpp", "vllm"],
        "models": {"alpha": model("active", "alpha", 10.0),
                   "beta": model("benchmarked", "beta", 12.0),
                   "gamma": model("installed", "gamma")},
        "groups": {"reasoning": ["alpha"], "coding": ["alpha"], "chat": ["alpha"], "spare": []},
        "roles": {"coder": {"group": "coding",
                            "requires": {"tool_calling": True, "min_context": 8192}},
                  "chat": {"group": "chat", "requires": {"min_context": 4096}},
                  "reviewer": {"group": "reasoning", "pin": ["alpha"],
                               "requires": {"tool_calling": True, "json_output": True}}},
    })


@pytest.fixture
def platform(tmp_path: Path) -> Platform:
    root = tmp_path / "platform"
    (root / "config").mkdir(parents=True)
    return Platform(config_dir=root / "config", platform_root=root, descriptors=DESCRIPTORS,
                    vector=CPU_LOW, capability_class="cpu-low", accelerator="none")


@pytest.fixture
def queue(platform: Platform) -> GovernanceQueue:
    return GovernanceQueue(platform.config_dir)


def bootstrapped(platform: Platform, registry: RegistryV2 | None = None) -> RegistryV2:
    """Generation 1 on disk, rendered — a running platform."""
    saved = save_generation(registry or seed_registry(), platform.config_dir, note="bootstrap")
    write_rendered(platform.render(saved), platform.rendered)
    return load_registry(platform.config_dir)


class FakeReloader:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[set[str]] = []
        self.fail = fail

    def reload(self, changed: set[str], platform: Platform) -> None:
        self.calls.append(set(changed))
        if self.fail is not None and len(self.calls) == 1:
            raise self.fail


class FakeHealth:
    def __init__(self, error: str = "") -> None:
        self.error = error
        self.checked: list[int] = []

    def check(self, registry: RegistryV2, platform: Platform) -> None:
        self.checked.append(registry.generation)
        if self.error:
            raise HealthError(self.error)


def events(queue: GovernanceQueue) -> list[str]:
    return [record.event for record in queue.audit()]


def manifest_generation(platform: Platform) -> int:
    return yaml.safe_load((platform.rendered / MANIFEST_FILENAME).read_text())["generation"]


# ── the queue ────────────────────────────────────────────────────────────────


def request(**overrides) -> ChangeRequest:
    data = {"kind": "activation", "title": "t", "requested_by": "nita", "base_generation": 1,
            "proposed": {}, "affected_roles": {"chat": {}}}
    data.update(overrides)
    return ChangeRequest.model_validate(data)


def test_queue_submit_decide_and_audit(queue):
    first = queue.submit(request(title="first"))
    second = queue.submit(request(title="second"))
    assert (first.id, second.id) == ("cr-0001", "cr-0002")
    assert first.status == "pending" and first.created_at
    assert (queue.queue_dir / "cr-0001.yaml").is_file()
    assert [r.title for r in queue.list()] == ["first", "second"]

    approved = queue.approve("cr-0001", by="admin", reason="benchmarks look good")
    assert approved.status == "approved" and approved.decision.by == "admin"
    with pytest.raises(GovernanceError, match="decisions are made once"):
        queue.approve("cr-0001", by="admin")
    with pytest.raises(GovernanceError, match="needs a reason"):
        queue.reject("cr-0002", by="admin", reason="  ")
    rejected = queue.reject("cr-0002", by="admin", reason="regression on reviewer")
    assert rejected.status == "rejected" and rejected.decision.reason == "regression on reviewer"
    assert queue.list(status="rejected")[0].id == "cr-0002"
    assert events(queue) == ["request.submitted", "request.submitted", "request.approved",
                             "request.rejected"]
    # append-only JSONL, one record per line, who/when/what
    lines = queue.log_path.read_text().splitlines()
    assert len(lines) == 4
    approval = json.loads(lines[2])
    assert approval["actor"] == "admin" and approval["request_id"] == "cr-0001"
    with pytest.raises(GovernanceError, match="no change request 'cr-9999'"):
        queue.get("cr-9999")


def test_policy_approval_and_the_evolution_lane(queue):
    policy = queue.submit(request(affected_roles={}, requires_approval=False))
    assert policy.status == "approved" and policy.decision.by == "policy"
    assert "request.approved" in events(queue)

    with pytest.raises(GovernanceError, match="evidence or silence"):
        queue.submit(request(proposed_by="evolution", requires_approval=False))
    evo = queue.submit(request(proposed_by="evolution", requires_approval=False,
                               evidence={"benchmarks": {"x": {"tokens_per_s": 9}}}))
    assert evo.status == "pending" and evo.requires_approval  # never auto-approved
    with pytest.raises(GovernanceError, match="at most one routing proposal"):
        queue.submit(request(proposed_by="evolution",
                             evidence={"benchmarks": {"y": {"tokens_per_s": 9}}}))
    queue.reject(evo.id, by="admin", reason="not now")
    queue.submit(request(proposed_by="evolution", evidence={"benchmarks": {"y": {}}}))


def test_outcomes_apply_only_to_approved_requests(queue):
    pending = queue.submit(request())
    with pytest.raises(GovernanceError, match="only approved requests are applied"):
        queue.mark(pending.id, "applied", "system", generation=2)
    queue.approve(pending.id, by="admin")
    with pytest.raises(GovernanceError, match="not an outcome status"):
        queue.mark(pending.id, "rejected", "system")
    done = queue.mark(pending.id, "applied", "system", generation=2)
    assert done.status == "applied" and done.result == {"generation": 2}
    assert not done.is_open


# ── proposals: activate / upgrade ────────────────────────────────────────────


def test_activate_builds_a_request_with_diff_roles_and_evidence(platform, queue):
    current = bootstrapped(platform)
    req = activate(platform, queue, current, "beta", requested_by="nita", group="coding")
    assert req.status == "pending" and req.requires_approval and req.kind == "activation"
    assert req.base_generation == 1 and "group coding at position 0" in req.title
    assert req.affected_roles == {"coder": {"before": {"primary": "alpha", "fallbacks": []},
                                            "after": {"primary": "beta",
                                                      "fallbacks": ["alpha"]}}}
    assert "model beta: state benchmarked → active" in req.diff
    assert "group coding: [alpha] → [beta, alpha]" in req.diff
    # evidence next to every button: benchmarks of every involved model, fit,
    # capability report, runtime before/after
    assert set(req.evidence["benchmarks"]) == {"alpha", "beta"}
    assert req.evidence["fit"]["beta"]["fits"] is True
    assert req.evidence["capability_report"] and req.evidence["runtime"] == {
        "before": "llamacpp", "after": "llamacpp", "switch": False}
    # the registry on disk is untouched by a proposal
    assert load_registry(platform.config_dir).models["beta"].state == "benchmarked"


def test_activate_needs_benchmark_evidence_and_a_servable_proposal(platform, queue):
    current = bootstrapped(platform)
    with pytest.raises(LifecycleError, match="activation needs benchmark evidence"):
        activate(platform, queue, current, "gamma", requested_by="nita", group="coding")
    current.models["beta"].tool_call_format = "mystery"
    with pytest.raises(LifecycleError) as err:
        activate(platform, queue, current, "beta", requested_by="nita", group="coding")
    assert "not servable as declared" in str(err.value)
    assert "missing capability tool_calling" in str(err.value)
    with pytest.raises(LifecycleError, match="unknown group 'nope'"):
        activate(platform, queue, current, "beta", requested_by="nita", group="nope")
    assert queue.list() == []  # nothing entered the queue


def test_activate_placements_fallback_pin_and_policy_approval(platform, queue):
    current = bootstrapped(platform)
    # no placement: joins its declared groups as a fallback → affects chains
    fallback = activate(platform, queue, current, "beta", requested_by="nita")
    proposed = RegistryV2.model_validate(fallback.proposed)
    assert proposed.groups["coding"] == ["alpha", "beta"]
    assert fallback.affected_roles["chat"]["after"]["fallbacks"] == ["beta"]
    assert fallback.requires_approval
    # role pin: explicit chain with the model first
    pinned = activate(platform, queue, current, "beta", requested_by="nita", role="reviewer")
    assert RegistryV2.model_validate(pinned.proposed).roles["reviewer"].pin == ["beta", "alpha"]
    assert set(pinned.affected_roles) == {"reviewer"}
    # a group no role depends on: no serving role affected → approved by policy
    spare = activate(platform, queue, current, "beta", requested_by="nita", group="spare")
    assert spare.affected_roles == {} and not spare.requires_approval
    assert spare.status == "approved" and spare.decision.by == "policy"


def test_upgrade_swaps_versions_everywhere_and_retires_the_old(platform, queue):
    current = bootstrapped(platform)
    req = upgrade(platform, queue, current, "alpha", "beta", requested_by="nita")
    proposed = RegistryV2.model_validate(req.proposed)
    assert req.kind == "upgrade" and req.title == "upgrade alpha → beta"
    assert all(proposed.groups[g] == ["beta"] for g in ("reasoning", "coding", "chat"))
    assert proposed.roles["reviewer"].pin == ["beta"]
    assert proposed.models["alpha"].state == "retired" and proposed.models["beta"].state == "active"
    assert set(req.affected_roles) == {"coder", "chat", "reviewer"}
    assert req.evidence["upgrade"]["benchmark_before"]["tokens_per_s"] == 10.0
    assert req.evidence["upgrade"]["benchmark_after"]["tokens_per_s"] == 12.0
    queue.approve(req.id, by="admin")
    result = apply(platform, queue, req.id, actor="admin")
    assert result.ok and result.generation == 2
    after = load_registry(platform.config_dir)
    assert after.resolve("coder").primary == "beta" and after.models["alpha"].state == "retired"
    with pytest.raises(LifecycleError, match="not active"):
        upgrade(platform, queue, after, "alpha", "gamma", requested_by="nita")
    with pytest.raises(LifecycleError, match="needs benchmark evidence"):
        upgrade(platform, queue, after, "beta", "gamma", requested_by="nita")


def test_affected_roles_reports_added_removed_and_unresolvable():
    before = seed_registry()
    after = before.model_copy(deep=True)
    after.roles["planner"] = after.roles["coder"].model_copy()  # added → served by alpha
    del after.roles["reviewer"]  # removed
    changes = affected_roles(before, after)
    assert changes["planner"] == {"before": None,
                                  "after": {"primary": "alpha", "fallbacks": []}}
    assert changes["reviewer"]["before"]["primary"] == "alpha"
    assert changes["reviewer"]["after"] is None
    assert "coder" not in changes  # unchanged chains are not reported
    broken = before.model_copy(deep=True)
    broken.models["alpha"].state = "retired"  # every chain collapses
    assert affected_roles(before, broken)["coder"]["after"] is None


# ── apply protocol ───────────────────────────────────────────────────────────


def approved_activation(platform, queue, current=None) -> str:
    current = current or bootstrapped(platform)
    req = activate(platform, queue, current, "beta", requested_by="nita", group="coding")
    queue.approve(req.id, by="admin", reason="ok")
    return req.id


def test_apply_waits_for_approval_then_commits_generation_and_artifacts(platform, queue):
    current = bootstrapped(platform)
    req = activate(platform, queue, current, "beta", requested_by="nita", group="coding")
    with pytest.raises(LifecycleError, match="awaiting approval"):
        apply(platform, queue, req.id, actor="admin")
    queue.approve(req.id, by="admin")
    result = apply(platform, queue, req.id, actor="admin")
    assert result.ok and result.generation == 2 and not result.reloaded
    assert "rendered only" in result.message
    after = load_registry(platform.config_dir)
    assert after.generation == 2 and after.models["beta"].state == "active"
    assert after.resolve("coder").primary == "beta"
    assert after.note.startswith("activation cr-0001")
    assert manifest_generation(platform) == 2
    litellm = yaml.safe_load((platform.rendered / LITELLM_FILENAME).read_text())
    aliases = {e["model_name"]: e["litellm_params"]["model"] for e in litellm["model_list"]}
    assert aliases["role-coder"] == "openai/beta" and aliases["role-chat"] == "openai/alpha"
    assert (platform.rendered / PRESET_FILENAME).is_file()  # two served models → router form
    done = queue.get(req.id)
    assert done.status == "applied" and done.result["generation"] == 2
    changed = set(done.result["changed"])
    assert {LITELLM_FILENAME, ENGINE_COMPOSE_FILENAME, PRESET_FILENAME} <= changed
    assert events(queue)[-1] == "request.applied"
    with pytest.raises(LifecycleError, match="is applied and cannot be applied"):
        apply(platform, queue, req.id, actor="admin")


def test_apply_reloads_only_changed_artifacts_and_health_checks(platform, queue):
    req_id = approved_activation(platform, queue)
    reloader, health = FakeReloader(), FakeHealth()
    result = apply(platform, queue, req_id, actor="admin", reloader=reloader, health=health)
    assert result.ok and result.reloaded and result.generation == 2
    assert len(reloader.calls) == 1 and ENGINE_COMPOSE_FILENAME in reloader.calls[0]
    assert health.checked == [2]
    assert queue.get(req_id).result["health_checked"] is True


def test_failed_health_self_rolls_back_as_a_new_generation(platform, queue):
    req_id = approved_activation(platform, queue)
    reloader = FakeReloader()
    result = apply(platform, queue, req_id, actor="admin", reloader=reloader,
                   health=FakeHealth("engine answers 503 after reload"))
    assert not result.ok and result.rolled_back and result.generation == 3
    assert "rolled back to the content of generation 1" in result.message
    # history is append-only: 2 (the failed attempt) stays; 3 restores 1's content
    assert list_generations(platform.config_dir) == [1, 2, 3]
    assert load_generation(platform.config_dir, 2).models["beta"].state == "active"
    restored = load_registry(platform.config_dir)
    assert restored.generation == 3 and restored.models["beta"].state == "benchmarked"
    assert restored.groups["coding"] == ["alpha"] and restored.note.startswith("self-rollback")
    assert manifest_generation(platform) == 3
    litellm = yaml.safe_load((platform.rendered / LITELLM_FILENAME).read_text())
    assert not any(e["litellm_params"]["model"] == "openai/beta" for e in litellm["model_list"])
    assert len(reloader.calls) == 2  # apply, then restore (everything re-rendered)
    failed = queue.get(req_id)
    assert failed.status == "failed" and "HealthError: engine answers 503" in failed.result["error"]
    assert failed.result["restored_generation"] == 3
    assert events(queue)[-2:] == ["generation.self_rollback", "request.failed"]


def test_crash_injection_during_reload_self_rolls_back(platform, queue):
    req_id = approved_activation(platform, queue)
    result = apply(platform, queue, req_id, actor="admin",
                   reloader=FakeReloader(fail=RuntimeError("docker daemon vanished")))
    assert not result.ok and result.rolled_back
    assert queue.get(req_id).result["error"].startswith("RuntimeError: docker daemon vanished")
    assert load_registry(platform.config_dir).resolve("coder").primary == "alpha"


def test_reconcile_heals_a_snapshot_without_rendered_artifacts(platform, queue):
    current = bootstrapped(platform)
    # crash between snapshot and render: registry moved, artifacts did not
    moved = current.model_copy(deep=True)
    moved.models["beta"].state = "active"
    moved.groups["coding"] = ["beta", "alpha"]
    save_generation(moved, platform.config_dir, note="half applied")
    assert manifest_generation(platform) == 1 and load_registry(platform.config_dir).generation == 2
    assert reconcile(platform, queue, actor="startup") is True
    assert manifest_generation(platform) == 2
    assert events(queue) == ["generation.reconciled"]
    assert reconcile(platform, queue, actor="startup") is False
    # apply reconciles first, too (here: rendered artifacts lost entirely)
    (platform.rendered / MANIFEST_FILENAME).unlink()
    current = load_registry(platform.config_dir)
    req = activate(platform, queue, current, "beta", requested_by="nita", role="reviewer")
    queue.approve(req.id, by="admin")
    assert apply(platform, queue, req.id, actor="admin").ok
    assert events(queue)[-2:] == ["generation.reconciled", "request.applied"]
    assert manifest_generation(platform) == 3


def test_stale_request_is_superseded(platform, queue):
    current = bootstrapped(platform)
    stale = activate(platform, queue, current, "beta", requested_by="nita", group="coding")
    queue.approve(stale.id, by="admin")
    moved = current.model_copy(deep=True)
    moved.groups["spare"] = ["alpha"]
    save_generation(moved, platform.config_dir, note="someone else moved first")
    result = apply(platform, queue, stale.id, actor="admin")
    assert not result.ok and "superseded by generation 2" in result.message
    assert queue.get(stale.id).status == "superseded"
    assert load_registry(platform.config_dir).models["beta"].state == "benchmarked"


def test_unrenderable_approved_request_fails_before_writing(platform, queue):
    current = bootstrapped(platform)
    req = activate(platform, queue, current, "beta", requested_by="nita", group="coding")
    queue.approve(req.id, by="admin")
    # the descriptor set changes under the request: its runtime disappears
    crippled = Platform(config_dir=platform.config_dir, platform_root=platform.platform_root,
                        descriptors={"vllm": DESCRIPTORS["vllm"]}, vector=CPU_LOW,
                        capability_class="cpu-low", accelerator="none")
    result = apply(crippled, queue, req.id, actor="admin")
    assert not result.ok and "cannot render" in result.message
    assert queue.get(req.id).status == "failed"
    assert list_generations(platform.config_dir) == [1]  # nothing was written


# ── rollback ─────────────────────────────────────────────────────────────────


def test_rollback_restores_a_generation_without_approval_and_notifies(platform, queue):
    req_id = approved_activation(platform, queue)
    apply(platform, queue, req_id, actor="admin")
    notices: list[str] = []
    result = rollback(platform, queue, actor="oncall", reason="latency regression",
                      notify=notices.append)
    assert result.ok and result.generation == 3
    restored = load_registry(platform.config_dir)
    assert restored.resolve("coder").primary == "alpha"
    assert restored.models["beta"].state == "benchmarked"
    assert restored.note == "rollback to generation 1: latency regression"
    assert manifest_generation(platform) == 3
    assert notices and "ROLLBACK by oncall" in notices[0] and "generation 1" in notices[0]
    rolled = queue.audit()[-1]
    assert rolled.event == "generation.rolled_back" and rolled.details["target"] == 1
    assert rolled.details["notified"] is True and rolled.generation == 3
    # a named target, and refusals
    named = rollback(platform, queue, actor="oncall", to_generation=2, notify=notices.append)
    assert named.ok and load_registry(platform.config_dir).resolve("coder").primary == "beta"
    with pytest.raises(LifecycleError, match="cannot roll back to generation 99"):
        rollback(platform, queue, actor="oncall", to_generation=99)
    with pytest.raises(LifecycleError, match="cannot roll back to generation 4"):
        rollback(platform, queue, actor="oncall", to_generation=4)  # the current one


def test_rollback_health_failure_restores_the_pre_rollback_generation(platform, queue):
    req_id = approved_activation(platform, queue)
    apply(platform, queue, req_id, actor="admin")  # generation 2: beta primary
    result = rollback(platform, queue, actor="oncall", health=FakeHealth("engine dead"),
                      notify=lambda m: None)
    assert not result.ok and result.rolled_back and result.generation == 4
    current = load_registry(platform.config_dir)
    assert current.generation == 4 and current.resolve("coder").primary == "beta"
    assert events(queue)[-2:] == ["generation.self_rollback", "generation.rollback_failed"]


# ── retire / uninstall guards ────────────────────────────────────────────────


def test_retire_guards(platform):
    registry = seed_registry()
    with pytest.raises(LifecycleError, match="primary of role\\(s\\) chat, coder, reviewer"):
        retire(registry, "alpha")
    with pytest.raises(LifecycleError, match="only active models are retired"):
        retire(registry, "gamma")
    with pytest.raises(LifecycleError, match="unknown model"):
        retire(registry, "nope")
    # a non-serving active fallback retires freely and is audited as a generation
    registry.models["beta"].state = "active"
    registry.groups["coding"] = ["alpha", "beta"]
    registry = save_generation(registry, platform.config_dir, note="bootstrap")
    updated, persisted = retire(registry, "beta", persist_dir=platform.config_dir)
    assert persisted and updated.models["beta"].state == "retired"
    assert updated.resolve("coder").fallbacks == []
    assert load_registry(platform.config_dir).note == "retire beta"


def test_uninstall_guards_and_removal(platform, tmp_path):
    registry = seed_registry()
    with pytest.raises(LifecycleError, match="retire it first"):
        uninstall(registry, "alpha")
    # beta was active in generation 1, now retired → a rollback target
    generation_one = registry.model_copy(deep=True)
    generation_one.models["beta"].state = "active"
    generation_one.groups["coding"] = ["alpha", "beta"]
    registry = save_generation(generation_one, platform.config_dir, note="bootstrap")
    weights = tmp_path / "beta.gguf"
    weights.write_bytes(b"w")
    registry.models["beta"].state = "retired"
    registry.models["beta"].artifact = str(weights)
    registry.groups["coding"] = ["alpha", "beta"]
    registry.roles["reviewer"].pin = ["alpha", "beta"]
    with pytest.raises(LifecycleError, match="generation\\(s\\) 1 list 'beta' as active"):
        uninstall(registry, "beta", config_dir=platform.config_dir)
    removed: list[Path] = []
    updated, persisted = uninstall(registry, "beta", config_dir=platform.config_dir, force=True,
                                   remover=removed.append, persist_dir=platform.config_dir)
    assert removed == [weights] and persisted
    assert "beta" not in updated.models and updated.groups["coding"] == ["alpha"]
    assert updated.roles["reviewer"].pin == ["alpha"]
    assert load_registry(platform.config_dir).note == "uninstall beta"
    # a failed install is removable without ceremony (no generation lists it active)
    registry.models["delta"] = registry.models["gamma"].model_copy(update={"state": "failed"})
    cleaned, _ = uninstall(registry, "delta", config_dir=platform.config_dir,
                           remover=removed.append)
    assert "delta" not in cleaned.models


# ── real reload / health adapters (fakes for docker + HTTP) ──────────────────


def test_compose_reloader_touches_only_what_changed(platform):
    runner = FakeRunner()
    reloader = ComposeReloader(runner)
    reloader.reload({ENGINE_COMPOSE_FILENAME}, platform)
    reloader.reload({LITELLM_FILENAME}, platform)
    reloader.reload(set(), platform)
    assert len(runner.calls) == 2
    up, restart = runner.calls
    assert up[-3:] == ["up", "-d", "vllm"]
    assert str(platform.rendered / ENGINE_COMPOSE_FILENAME) in up
    assert str(platform.platform_root / "docker-compose.yml") in up
    assert restart[-2:] == ["restart", "litellm"]
    with pytest.raises(HealthError, match="reload failed"):
        ComposeReloader(FakeRunner(fail_up=True)).reload({PRESET_FILENAME}, platform)


def test_engine_health_waits_for_ready_then_probes_a_role(platform):
    registry = bootstrapped(platform)
    http = FakeEngineHTTP(ready_after=2)
    EngineHealth(http, base_url="http://127.0.0.1:8000", role="coder",
                 sleep=lambda s: None).check(registry, platform)
    assert http.polls == 3
    assert http.posts[0]["model"] == "alpha" and http.posts[0]["max_tokens"] == 1
    with pytest.raises(HealthError, match="role 'coder' → 'alpha' does not answer"):
        EngineHealth(FakeEngineHTTP(fail_probe=True), sleep=lambda s: None,
                     role="coder").check(registry, platform)
    clock = iter([0.0, 0.0, 999.0, 999.0])
    with pytest.raises(HealthError, match="not ready within 600s"):
        EngineHealth(FakeEngineHTTP(ready_after=99), sleep=lambda s: None,
                     clock=lambda: next(clock)).check(registry, platform)


# ── platform tripwire ────────────────────────────────────────────────────────


def test_governance_code_carries_no_runtime_or_vendor_knowledge():
    src = Path(__file__).resolve().parents[2] / "src" / "agentd"
    for module in ("governance.py", "activation.py"):
        text = (src / module).read_text(encoding="utf-8").lower()
        for forbidden in ("nvidia", "ghcr.io", "vllm/", "llama.cpp", "/dev/dri", "qwen",
                          "hermes", "--tool-call-parser"):
            assert forbidden not in text, f"{module} mentions {forbidden!r}"
