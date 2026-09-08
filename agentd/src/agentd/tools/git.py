"""Git tools.

All invocations use argument lists (never a shell) and run against the
workspace. Commits pass identity via ``-c user.name/-c user.email`` so the
user's git configuration is never modified (worktrees share the repo's
.git/config). ``git_push`` is the only T3 tool in the MVP — fail-closed
behind ``git.allow_push`` (ADR-008).
"""

from __future__ import annotations

import subprocess
from typing import Any

from agentd.permissions import ToolTier
from agentd.tools.base import Tool, ToolResult
from agentd.workspace import Workspace


def git_run(workspace: Workspace, *args: str, timeout: float = 120.0) -> ToolResult:
    """Run a git command in the workspace and wrap it in a ToolResult."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace.root), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(ok=False, error=f"git {' '.join(args[:2])} timed out")
    output = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode == 0
    return ToolResult(
        ok=ok,
        output=output.strip(),
        error=None if ok else f"git exited with {proc.returncode}",
        exit_code=proc.returncode,
    )


class GitStatus(Tool):
    name = "git_status"
    description = "Show changed files in the workspace (git status --porcelain)."
    tier = ToolTier.T0_READ_WORKSPACE
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    def run(self, workspace: Workspace) -> ToolResult:
        return git_run(workspace, "status", "--porcelain")


class GitDiff(Tool):
    name = "git_diff"
    description = "Show the current diff (unstaged + staged) in the workspace."
    tier = ToolTier.T0_READ_WORKSPACE
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Limit the diff to one path"},
            "staged": {"type": "boolean", "description": "Show the staged diff only"},
        },
    }

    def run(self, workspace: Workspace, path: str = "", staged: bool = False) -> ToolResult:
        args = ["diff"]
        if staged:
            args.append("--cached")
        else:
            args.append("HEAD")
        if path:
            workspace.resolve(path)  # containment check
            args.extend(["--", path])
        return git_run(workspace, *args)


class GitAdd(Tool):
    name = "git_add"
    description = "Stage files in the workspace ('-A' semantics when no paths given)."
    tier = ToolTier.T2_MUTATE_WORKSPACE
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"}},
        },
    }

    #: The runtime's own machine-managed state must never ride along in a
    #: run's commit (relevant in in-place mode, where .agent/ sits inside
    #: the workspace; in worktree mode it lives in the origin repo anyway).
    #: Human-managed .agent files (model_registry.yaml, roadmap.md, ADRs)
    #: are NOT excluded — they are ordinary repository content.
    MEMORY_EXCLUDES = (
        ":(exclude).agent/memory.db*",
        ":(exclude).agent/lessons_learned.json",
        ":(exclude).agent/code-index",
        ":(exclude).agent/model_benchmarks.json",
    )

    def run(self, workspace: Workspace, paths: list[str] | None = None) -> ToolResult:
        if paths:
            for p in paths:
                workspace.resolve(p)  # containment check
            return git_run(workspace, "add", "--", *paths)
        return git_run(workspace, "add", "-A", "--", ".", *self.MEMORY_EXCLUDES)


class GitCommit(Tool):
    name = "git_commit"
    description = "Commit staged changes with the given message."
    tier = ToolTier.T2_MUTATE_WORKSPACE
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "user_name": {"type": "string"},
            "user_email": {"type": "string"},
        },
        "required": ["message"],
    }

    def run(
        self,
        workspace: Workspace,
        message: str,
        user_name: str = "agentd",
        user_email: str = "agentd@local-ezai",
    ) -> ToolResult:
        if not message.strip():
            return ToolResult(ok=False, error="commit message is empty")
        result = git_run(
            workspace,
            "-c", f"user.name={user_name}",
            "-c", f"user.email={user_email}",
            "commit", "-m", message,
        )
        if result.ok:
            sha = git_run(workspace, "rev-parse", "HEAD")
            result.extra["sha"] = sha.output.strip() if sha.ok else ""
        return result


class GitPush(Tool):
    name = "git_push"
    description = "Push the run branch to the remote. Requires explicit enablement."
    tier = ToolTier.T3_PROJECT_VISIBLE
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "remote": {"type": "string", "description": "Default: origin"},
            "branch": {"type": "string"},
        },
        "required": ["branch"],
    }

    def run(self, workspace: Workspace, branch: str, remote: str = "origin") -> ToolResult:
        return git_run(workspace, "push", "-u", remote, branch, timeout=300.0)
