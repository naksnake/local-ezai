"""Permission engine (MVP slice of TARGET_ARCHITECTURE §5/§9).

Tools carry a risk tier; the policy decides allow/deny **outside the model**
(ADR-008: fail-closed action). In the MVP:

- T0 (read-only, workspace) and T1 (read-only, external): allowed
- T2 (mutating, inside the workspace): allowed — the workspace is the blast
  radius, and path containment is enforced by ``Workspace.resolve``
- T3 (project-visible: ``git_push``): allowed only when explicitly enabled
  (``git.allow_push`` / CLI ``--push``)
- T4 (destructive/host-visible): always denied; no T4 tool is registered,
  and unknown tools are denied by default

Every decision is journaled by the tool registry.
"""

from __future__ import annotations

from enum import IntEnum

from agentd.config import AgentdConfig


class ToolTier(IntEnum):
    T0_READ_WORKSPACE = 0
    T1_READ_EXTERNAL = 1
    T2_MUTATE_WORKSPACE = 2
    T3_PROJECT_VISIBLE = 3
    T4_DESTRUCTIVE = 4


class PermissionDecision:
    __slots__ = ("allowed", "reason")

    def __init__(self, allowed: bool, reason: str) -> None:
        self.allowed = allowed
        self.reason = reason


class PermissionPolicy:
    """Evaluates (tool tier × configuration) → allow/deny. Fail-closed."""

    def __init__(self, config: AgentdConfig) -> None:
        self._config = config

    def check(self, tool_name: str, tier: ToolTier | None) -> PermissionDecision:
        if tier is None:
            return PermissionDecision(False, f"unknown tool '{tool_name}' — denied (fail-closed)")
        if tier in (ToolTier.T0_READ_WORKSPACE, ToolTier.T1_READ_EXTERNAL):
            return PermissionDecision(True, "read-only")
        if tier is ToolTier.T2_MUTATE_WORKSPACE:
            return PermissionDecision(True, "mutation confined to workspace")
        if tier is ToolTier.T3_PROJECT_VISIBLE:
            if tool_name == "git_push" and self._config.git.allow_push:
                return PermissionDecision(True, "git.allow_push is enabled")
            return PermissionDecision(
                False,
                f"T3 tool '{tool_name}' requires explicit enablement "
                "(git.allow_push / --push)",
            )
        return PermissionDecision(False, f"T4 tool '{tool_name}' is always denied to agents")
