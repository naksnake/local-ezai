"""F9 at the bootstrap core (PR-23): three `auto` seeds are three
recommendations, one per group — not one model copied into every group
(the defect the acceptance suite surfaced in PR-7's dedupe by reference).
A recommendation two groups share is installed once and serves both."""

from __future__ import annotations

from agentd.catalog import recommend_one
from agentd.registry_v2 import load_registry, reference_registry, registry_path
from tests.unit.test_bootstrap import (
    CATALOG,
    CPU_LOW,
    DESCRIPTORS,
    platform_for,
    root,
    run_bootstrap,
)

_ = root  # the PR-7 fixture: a fresh platform, no registry

AUTO_ENV = "AI_RUNTIME=llamacpp\nREASONING_MODEL=auto\nCODING_MODEL=auto\nCHAT_MODEL=auto\n"


def expected_picks() -> dict[str, str]:
    roles = reference_registry().roles
    picks = {}
    for group in ("reasoning", "coding", "chat"):
        contracts = [s.requires for s in roles.values() if s.group == group]
        picks[group] = recommend_one(CATALOG, group, CPU_LOW, DESCRIPTORS, runtime="llamacpp",
                                     contracts=contracts, capability_class="cpu-low",
                                     accelerator="none").catalog_id
    return picks


def test_auto_seeds_resolve_per_group_and_share_installs(root):
    platform = platform_for(root)
    picks = expected_picks()
    assert len(set(picks.values())) >= 2, "the shipped catalog recommends different models"
    # the dry run names a placeholder per group — never one model for all three
    dry, _ = run_bootstrap(root, AUTO_ENV, platform=platform, dry_run=True)
    assert dry.dry_run and dry.models == {"reasoning": "auto:reasoning",
                                          "coding": "auto:coding", "chat": "auto:chat"}
    assert not registry_path(root / "config").is_file()
    # the real run: each group gets its recommendation; a shared pick is installed once
    result, _ = run_bootstrap(root, AUTO_ENV, platform=platform)
    assert result.generation == 1 and result.models == picks
    registry = load_registry(root / "config")
    assert set(registry.models) == set(picks.values())
    for group, pick in picks.items():
        assert registry.groups[group] == [pick] and group in registry.models[pick].groups
        assert registry.models[pick].state == "active"
