"""sandboxd (Phase H1, ADR-021): mode resolution, command allowlist,
execution audit log, and Docker command construction (via a fake docker
binary — no daemon needed)."""

import json
import stat
from pathlib import Path

import pytest

from agentd.config import SandboxConfig
from agentd.sandbox import Sandbox, SandboxError
from agentd.tools.shell import run_command
from agentd.workspace import Workspace


def read_audit(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().strip().splitlines()]


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    """A fake `docker` binary: answers `info`, records `run` args to a file,
    prints a marker. Prepended to PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "docker-args.txt"
    script = bin_dir / "docker"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "info" ]; then echo 27.0; exit 0; fi\n'
        f'printf \'%s\\n\' "$@" > "{record}"\n'
        "echo ran-in-docker\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    return record


# ── host mode + audit ─────────────────────────────────────────────────────────


def test_host_mode_executes_and_audits(tmp_path):
    audit = tmp_path / "run" / "exec_audit.jsonl"
    sandbox = Sandbox(SandboxConfig(mode="host"), tmp_path, audit_path=audit)
    result = sandbox.run("echo hello", timeout=10)
    assert result.ok and "hello" in result.output
    assert sandbox.mode == "host"
    records = read_audit(audit)
    assert len(records) == 1
    assert records[0]["mode"] == "host"
    assert records[0]["command"] == "echo hello"
    assert records[0]["allowed"] is True
    assert records[0]["exit_code"] == 0


def test_audit_disabled_writes_nothing(tmp_path):
    audit = tmp_path / "exec_audit.jsonl"
    sandbox = Sandbox(SandboxConfig(mode="host", audit=False), tmp_path,
                      audit_path=audit)
    assert sandbox.run("echo hi", timeout=10).ok
    assert not audit.exists()


def test_timeout_is_enforced(tmp_path):
    sandbox = Sandbox(SandboxConfig(mode="host"), tmp_path)
    result = sandbox.run("sleep 5", timeout=0.2)
    assert not result.ok
    assert "timed out" in result.error


# ── command allowlist (fail-closed when configured, in every mode) ───────────


def test_empty_allowlist_allows_everything(tmp_path):
    sandbox = Sandbox(SandboxConfig(mode="host"), tmp_path)
    assert sandbox.run("echo unrestricted", timeout=10).ok


def test_allowlist_blocks_nonmatching_command(tmp_path):
    audit = tmp_path / "exec_audit.jsonl"
    cfg = SandboxConfig(mode="host", command_allowlist=[r"^echo ", r"^python3? "])
    sandbox = Sandbox(cfg, tmp_path, audit_path=audit)

    allowed = sandbox.run("echo fine", timeout=10)
    assert allowed.ok

    blocked = sandbox.run("curl http://evil.example", timeout=10)
    assert not blocked.ok
    assert "blocked by sandbox allowlist" in blocked.error
    assert blocked.exit_code is None

    records = read_audit(audit)
    assert [r["allowed"] for r in records] == [True, False]
    assert records[1]["command"].startswith("curl")


def test_run_command_routes_through_workspace_sandbox(tmp_path):
    """The shared executor (ExecRun tool + Validation Agent) honors the
    run's sandbox — proven by an allowlist denial."""
    ws = Workspace(root=tmp_path, repo_path=tmp_path, branch="main",
                   mode="in-place")
    ws.sandbox = Sandbox(SandboxConfig(mode="host",
                                       command_allowlist=[r"^true$"]), tmp_path)
    assert run_command(ws, "true", timeout=5).ok
    denied = run_command(ws, "echo not-listed", timeout=5)
    assert not denied.ok and "allowlist" in denied.error


def test_run_command_without_sandbox_falls_back_to_host(tmp_path):
    ws = Workspace(root=tmp_path, repo_path=tmp_path, branch="main",
                   mode="in-place")
    result = run_command(ws, "echo legacy-path", timeout=5)
    assert result.ok and "legacy-path" in result.output


# ── mode resolution ───────────────────────────────────────────────────────────


def test_auto_without_image_stays_on_host(tmp_path, fake_docker):
    # Docker "available" (fake), but no image configured → host (the
    # backward-compatible default: a meaningful image is an operator choice).
    sandbox = Sandbox(SandboxConfig(mode="auto"), tmp_path)
    assert sandbox.mode == "host"


def test_auto_with_image_and_daemon_uses_docker(tmp_path, fake_docker):
    cfg = SandboxConfig(mode="auto", image="myproj-ci:latest")
    sandbox = Sandbox(cfg, tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    assert sandbox.mode == "docker"
    result = sandbox.run("pytest -q", timeout=30)
    assert result.ok and "ran-in-docker" in result.output

    args = fake_docker.read_text().splitlines()
    ws = str(tmp_path / "ws")
    assert args[0] == "run" and "--rm" in args
    assert f"{ws}:{ws}" in args            # host-identical mount
    assert args[args.index("-w") + 1] == ws
    assert args[args.index("--network") + 1] == "none"   # default-deny egress
    assert args[args.index("--memory") + 1] == "2g"      # resource limits
    assert args[args.index("--cpus") + 1] == "2.0"
    assert args[args.index("--pids-limit") + 1] == "512"
    assert "myproj-ci:latest" in args
    # the command itself runs under sh -c
    assert args[-3:] == ["sh", "-c", "pytest -q"]
    assert "PYTHONDONTWRITEBYTECODE=1" in args


def test_docker_mode_requires_image(tmp_path):
    sandbox = Sandbox(SandboxConfig(mode="docker"), tmp_path)
    with pytest.raises(SandboxError, match="image"):
        _ = sandbox.mode


def test_docker_mode_requires_daemon(tmp_path, monkeypatch):
    # a docker binary that fails `info` (daemon down)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "docker"
    script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bin_dir))
    sandbox = Sandbox(SandboxConfig(mode="docker", image="x"), tmp_path)
    with pytest.raises(SandboxError, match="daemon"):
        _ = sandbox.mode


def test_worktree_origin_gitdir_is_mounted(tmp_path, fake_docker):
    ws = tmp_path / "worktree"
    ws.mkdir()
    origin_git = tmp_path / "origin" / ".git"
    (ws / ".git").write_text(
        f"gitdir: {origin_git}/worktrees/wt1\n", encoding="utf-8"
    )
    sandbox = Sandbox(SandboxConfig(mode="auto", image="img"), ws)
    assert sandbox.run("git status", timeout=10).ok
    args = fake_docker.read_text().splitlines()
    assert f"{origin_git}:{origin_git}" in args


def test_env_passthrough(tmp_path, fake_docker, monkeypatch):
    monkeypatch.setenv("MY_SECRET_TOKEN", "t0k")
    cfg = SandboxConfig(mode="auto", image="img",
                        env_passthrough=["MY_SECRET_TOKEN", "UNSET_VAR"])
    sandbox = Sandbox(cfg, tmp_path)
    assert sandbox.run("env", timeout=10).ok
    args = fake_docker.read_text().splitlines()
    assert "MY_SECRET_TOKEN" in args      # present env var forwarded
    assert "UNSET_VAR" not in args        # absent one skipped
