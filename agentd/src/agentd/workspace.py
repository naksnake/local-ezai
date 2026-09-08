"""Workspace management — where a run is allowed to act.

Default mode (``workspace.mode: worktree``): the run gets a **git worktree**
on a fresh branch ``swe/<run-id>`` under ``workspace.root``. The user's
checkout, current branch, and uncommitted changes are never touched; the
deliverable is a branch in the repo's object store (interim isolation per
ADR-014 — container sandboxing arrives in Phase 2).

``in-place`` mode edits the repository directly on its current branch and is
an explicit opt-in for throwaway repos and tests.

Every tool resolves paths through :meth:`Workspace.resolve`, which rejects
anything that escapes the workspace root (absolute paths outside it,
``..`` traversal, symlink escapes).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from agentd.config import AgentdConfig
from agentd.logging_setup import get_logger

log = get_logger("workspace")


class WorkspaceError(RuntimeError):
    pass


class PathEscapeError(WorkspaceError):
    """A path tried to leave the workspace."""


@dataclass
class Workspace:
    """The directory a run may read and write, plus its git identity."""

    root: Path
    repo_path: Path
    branch: str
    mode: str
    #: Per-run execution sandbox (agentd.sandbox.Sandbox), attached by
    #: prepare_run. None → tools fall back to a bare host executor.
    sandbox: object | None = field(default=None, repr=False, compare=False)
    #: Semantic code index (agentd.code_intel.CodeIndex), attached by
    #: prepare_run when code intelligence is enabled (ADR-023).
    code_index: object | None = field(default=None, repr=False, compare=False)

    def resolve(self, relpath: str) -> Path:
        """Resolve ``relpath`` inside the workspace or raise PathEscapeError."""
        candidate = Path(relpath)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (self.root / candidate).resolve()
        root = self.root.resolve()
        if resolved != root and root not in resolved.parents:
            raise PathEscapeError(f"path escapes workspace: {relpath}")
        return resolved


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def ensure_git_repo(repo_path: Path) -> None:
    if not repo_path.is_dir():
        raise WorkspaceError(f"repository path does not exist: {repo_path}")
    result = _git(repo_path, "rev-parse", "--is-inside-work-tree", check=False)
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise WorkspaceError(f"not a git repository: {repo_path}")
    head = _git(repo_path, "rev-parse", "--verify", "HEAD", check=False)
    if head.returncode != 0:
        raise WorkspaceError(f"repository has no commits yet: {repo_path}")


def create_workspace(config: AgentdConfig, repo_path: Path, run_id: str) -> Workspace:
    """Prepare the workspace for a run according to the configured mode."""
    repo_path = repo_path.resolve()
    ensure_git_repo(repo_path)
    branch = f"{config.git.branch_prefix}{run_id}"

    if config.workspace.mode == "in-place":
        current = _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        log.info("in-place workspace on %s (branch %s)", repo_path, current)
        return Workspace(root=repo_path, repo_path=repo_path, branch=current, mode="in-place")

    worktree_dir = (config.workspace.root / run_id).resolve()
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    result = _git(
        repo_path,
        "worktree",
        "add",
        "-b",
        branch,
        str(worktree_dir),
        "HEAD",
        check=False,
    )
    if result.returncode != 0:
        raise WorkspaceError(
            f"could not create worktree for {repo_path} at {worktree_dir}: "
            f"{result.stderr.strip()}"
        )
    log.info("worktree %s on branch %s", worktree_dir, branch)
    return Workspace(root=worktree_dir, repo_path=repo_path, branch=branch, mode="worktree")


def remove_workspace(workspace: Workspace) -> None:
    """Remove a worktree (the branch and its commits remain in the repo)."""
    if workspace.mode != "worktree":
        return
    result = _git(
        workspace.repo_path, "worktree", "remove", "--force", str(workspace.root), check=False
    )
    if result.returncode != 0:
        log.warning("worktree removal failed: %s", result.stderr.strip())
