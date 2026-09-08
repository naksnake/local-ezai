"""Shell execution inside the workspace.

Every execution goes through the run's :class:`agentd.sandbox.Sandbox`
(Phase H1, ADR-021): command allowlist → host subprocess or Docker
container → execution audit log. A workspace without an attached sandbox
(direct tool use in tests) falls back to a bare host executor with the
same semantics as the ADR-014 interim behavior.
"""

from __future__ import annotations

from typing import Any

from agentd.permissions import ToolTier
from agentd.tools.base import Tool, ToolResult
from agentd.workspace import Workspace


def run_command(
    workspace: Workspace, command: str, timeout: float
) -> ToolResult:
    """Run one shell command in the workspace; shared by ExecRun and the
    Validation Agent so every execution follows the same rules."""
    sandbox = workspace.sandbox
    if sandbox is None:
        from agentd.config import SandboxConfig
        from agentd.sandbox import Sandbox

        sandbox = Sandbox(SandboxConfig(mode="host", audit=False), workspace.root)
    return sandbox.run(command, timeout)


class ExecRun(Tool):
    name = "exec_run"
    description = (
        "Run a shell command in the workspace root (e.g. compile, run a single "
        "test, format). Times out; long-running servers are not supported. "
        "Returns combined stdout+stderr and the exit code."
    )
    tier = ToolTier.T2_MUTATE_WORKSPACE
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "number", "description": "Seconds, default 120, max 600"},
        },
        "required": ["command"],
    }

    def run(self, workspace: Workspace, command: str, timeout: float = 120.0) -> ToolResult:
        timeout = min(max(1.0, float(timeout)), 600.0)
        return run_command(workspace, command, timeout)
