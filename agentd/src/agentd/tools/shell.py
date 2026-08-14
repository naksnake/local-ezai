"""Shell execution inside the workspace.

Interim isolation (ADR-014): commands run as host subprocesses with the
workspace as cwd, wall-clock timeouts, and output caps. Container-level
sandboxing (sandboxd) replaces this executor in Phase 2 without changing
the tool contract.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from agentd.permissions import ToolTier
from agentd.tools.base import Tool, ToolResult
from agentd.workspace import Workspace

_OUTPUT_CAP = 20_000


def run_command(
    workspace: Workspace, command: str, timeout: float
) -> ToolResult:
    """Run one shell command in the workspace; shared by ExecRun and the
    Validation Agent so every execution follows the same rules."""
    env = dict(os.environ)
    # Agent loops edit files and re-run checks within the same second, and
    # CPython's bytecode cache validates by (mtime seconds, size) — a fix
    # that preserves file size can silently execute stale .pyc bytecode.
    # Keeping agent-run commands from writing bytecode makes checks hermetic.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(workspace.root),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        partial = ""
        for stream in (exc.stdout, exc.stderr):
            if stream:
                partial += stream if isinstance(stream, str) else stream.decode(errors="replace")
        return ToolResult(
            ok=False,
            error=f"command timed out after {timeout:.0f}s",
            output=partial[-_OUTPUT_CAP:],
            exit_code=None,
        )
    output = (proc.stdout or "") + (proc.stderr or "")
    truncated = len(output) > _OUTPUT_CAP
    if truncated:
        output = output[-_OUTPUT_CAP:]
    ok = proc.returncode == 0
    return ToolResult(
        ok=ok,
        output=output,
        error=None if ok else f"exit code {proc.returncode}",
        exit_code=proc.returncode,
        truncated=truncated,
    )


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
