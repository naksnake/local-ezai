from agentd.config import AgentdConfig
from agentd.journal import NullJournal
from agentd.permissions import PermissionPolicy, ToolTier
from agentd.runner import build_registry


def test_read_tiers_allowed():
    policy = PermissionPolicy(AgentdConfig())
    assert policy.check("fs_read", ToolTier.T0_READ_WORKSPACE).allowed
    assert policy.check("web_fetch", ToolTier.T1_READ_EXTERNAL).allowed


def test_workspace_mutation_allowed():
    policy = PermissionPolicy(AgentdConfig())
    assert policy.check("fs_write", ToolTier.T2_MUTATE_WORKSPACE).allowed


def test_push_fail_closed_by_default():
    policy = PermissionPolicy(AgentdConfig())
    decision = policy.check("git_push", ToolTier.T3_PROJECT_VISIBLE)
    assert not decision.allowed
    assert "allow_push" in decision.reason


def test_push_allowed_when_enabled():
    config = AgentdConfig()
    config.git.allow_push = True
    assert PermissionPolicy(config).check("git_push", ToolTier.T3_PROJECT_VISIBLE).allowed


def test_other_t3_tools_stay_denied_even_with_push_enabled():
    config = AgentdConfig()
    config.git.allow_push = True
    assert not PermissionPolicy(config).check("pr_create", ToolTier.T3_PROJECT_VISIBLE).allowed


def test_t4_always_denied():
    config = AgentdConfig()
    config.git.allow_push = True
    assert not PermissionPolicy(config).check("rm_rf", ToolTier.T4_DESTRUCTIVE).allowed


def test_unknown_tool_denied():
    decision = PermissionPolicy(AgentdConfig()).check("mystery", None)
    assert not decision.allowed
    assert "fail-closed" in decision.reason


def test_registry_denies_push_and_journals(config, inplace_ws, journal):
    registry = build_registry(config, journal)
    result = registry.execute("git_push", {"branch": "main"}, inplace_ws, agent="test")
    assert not result.ok
    assert "permission denied" in result.error
    events = journal.read()
    called = [e for e in events if e["type"] == "TOOL_CALLED"][-1]
    assert called["payload"]["tool"] == "git_push"
    assert called["payload"]["allowed"] is False


def test_registry_enforces_agent_allowlist(config, inplace_ws):
    registry = build_registry(config, NullJournal())
    result = registry.execute(
        "fs_write", {"path": "x", "content": "y"}, inplace_ws,
        agent="planner", allowlist=["fs_read"],
    )
    assert not result.ok
    assert "allowlist" in result.error
    assert not (inplace_ws.root / "x").exists()


def test_registry_unknown_tool(config, inplace_ws):
    registry = build_registry(config, NullJournal())
    result = registry.execute("no_such_tool", {}, inplace_ws, agent="test")
    assert not result.ok
