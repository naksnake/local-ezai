import subprocess
from pathlib import Path

import pytest

from agentd.workspace import (
    PathEscapeError,
    WorkspaceError,
    create_workspace,
    ensure_git_repo,
    remove_workspace,
)
from tests.conftest import git


def test_worktree_created(config, tmp_repo: Path):
    ws = create_workspace(config, tmp_repo, "run1")
    assert ws.mode == "worktree"
    assert ws.branch == "swe/run1"
    assert (ws.root / "calculator.py").is_file()
    assert ws.root != tmp_repo
    # branch is visible from the main repo
    assert "swe/run1" in git(tmp_repo, "branch", "--list", "swe/run1")
    remove_workspace(ws)
    assert not ws.root.exists()
    # commits/branch survive worktree removal
    assert "swe/run1" in git(tmp_repo, "branch", "--list", "swe/run1")


def test_in_place_mode(config, tmp_repo: Path):
    config.workspace.mode = "in-place"
    ws = create_workspace(config, tmp_repo, "run2")
    assert ws.root == tmp_repo
    assert ws.branch == "main"
    assert ws.mode == "in-place"


def test_resolve_containment(config, tmp_repo: Path):
    ws = create_workspace(config, tmp_repo, "run3")
    assert ws.resolve("calculator.py").name == "calculator.py"
    with pytest.raises(PathEscapeError):
        ws.resolve("../outside.txt")
    with pytest.raises(PathEscapeError):
        ws.resolve("/etc/passwd")
    with pytest.raises(PathEscapeError):
        ws.resolve("a/../../b")


def test_resolve_symlink_escape(config, tmp_repo: Path, tmp_path: Path):
    ws = create_workspace(config, tmp_repo, "run4")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (ws.root / "link").symlink_to(outside)
    with pytest.raises(PathEscapeError):
        ws.resolve("link")


def test_not_a_repo(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorkspaceError, match="not a git repository"):
        ensure_git_repo(plain)


def test_repo_without_commits(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    subprocess.run(["git", "-C", str(empty), "init", "-q"], check=True)
    with pytest.raises(WorkspaceError, match="no commits"):
        ensure_git_repo(empty)


def test_missing_path(tmp_path: Path):
    with pytest.raises(WorkspaceError, match="does not exist"):
        ensure_git_repo(tmp_path / "nope")
