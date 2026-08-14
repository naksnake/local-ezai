from pathlib import Path

from agentd.tools.git import GitAdd, GitCommit, GitPush, GitStatus
from agentd.tools.shell import ExecRun
from tests.conftest import git

# ── exec_run ─────────────────────────────────────────────────────────────────


def test_exec_ok(inplace_ws):
    result = ExecRun().run(inplace_ws, command="echo hello")
    assert result.ok
    assert result.exit_code == 0
    assert "hello" in result.output


def test_exec_nonzero_exit(inplace_ws):
    result = ExecRun().run(inplace_ws, command="exit 3")
    assert not result.ok
    assert result.exit_code == 3


def test_exec_runs_in_workspace_cwd(inplace_ws):
    result = ExecRun().run(inplace_ws, command="pwd")
    assert result.ok
    assert Path(result.output.strip()).resolve() == inplace_ws.root.resolve()


def test_exec_timeout(inplace_ws):
    result = ExecRun().run(inplace_ws, command="sleep 5", timeout=1)
    assert not result.ok
    assert "timed out" in result.error
    assert result.exit_code is None


def test_exec_output_capped(inplace_ws):
    result = ExecRun().run(
        inplace_ws, command="python3 -c \"print('x' * 100000)\""
    )
    assert result.ok
    assert result.truncated
    assert len(result.output) <= 20_000


# ── git tools ────────────────────────────────────────────────────────────────


def test_git_status_add_commit(inplace_ws):
    (inplace_ws.root / "new.txt").write_text("hi\n")
    status = GitStatus().run(inplace_ws)
    assert status.ok and "new.txt" in status.output

    add = GitAdd().run(inplace_ws)
    assert add.ok

    commit = GitCommit().run(inplace_ws, message="test: add new.txt")
    assert commit.ok
    assert len(commit.extra.get("sha", "")) == 40
    assert git(inplace_ws.root, "log", "-1", "--format=%s") == "test: add new.txt"
    # user's git config untouched — identity came from -c flags
    assert git(inplace_ws.root, "log", "-1", "--format=%an") == "agentd"


def test_git_commit_empty_message(inplace_ws):
    result = GitCommit().run(inplace_ws, message="   ")
    assert not result.ok


def test_git_push_without_remote_fails_cleanly(inplace_ws):
    result = GitPush().run(inplace_ws, branch="main")
    assert not result.ok
    assert result.exit_code not in (None, 0)


def test_git_push_to_bare_remote(inplace_ws, tmp_path):
    bare = tmp_path / "bare.git"
    import subprocess

    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git(inplace_ws.root, "remote", "add", "origin", str(bare))
    result = GitPush().run(inplace_ws, branch="main")
    assert result.ok
    assert git(bare, "rev-parse", "refs/heads/main")
