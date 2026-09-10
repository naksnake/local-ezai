"""``install.sh`` — the bash entry of the first run (PR-21, ADR-031): preflight,
the module behind it, exit codes, and that it writes nothing but ``.env``.

The script is copied into a temporary checkout and driven with
``EZAI_PYTHON`` set to this interpreter (agentd importable), so the venv
step is skipped and no network is used. The host's real hardware is
detected — the assertions stay independent of it."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentd.bootstrap import RUNTIME_KEY, parse_env
from agentd.installer import EXIT_OK, EXIT_PROBLEMS, EXIT_REVIEW, EXIT_USAGE, SECRET_KEYS

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = parse_env((REPO_ROOT / ".env.example").read_text(encoding="utf-8"))


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "clone"
    (root / "config").mkdir(parents=True)
    (root / "scripts").mkdir()
    shutil.copytree(REPO_ROOT / "config" / "providers", root / "config" / "providers")
    for name in (".env.example", "install.sh", "docker-compose.yml"):
        shutil.copy(REPO_ROOT / name, root / name)
    shutil.copy(REPO_ROOT / "scripts" / "check-ports.sh", root / "scripts" / "check-ports.sh")
    (root / "w").mkdir()
    for name in ("reasoner", "coder", "chatter"):
        (root / "w" / f"{name}.gguf").write_bytes(name.encode() * 256)
    return root


def sh(root: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "EZAI_CAPABILITY_CLASS"}
    env["EZAI_PYTHON"] = sys.executable
    return subprocess.run(["bash", str(root / "install.sh"), *args], env=env,
                          cwd=cwd or root.parent, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=120)


def valid_env(root: Path) -> str:
    return (f"{RUNTIME_KEY}=llamacpp\nREASONING_MODEL=gguf:{root}/w/reasoner.gguf\n"
            f"CODING_MODEL=gguf:{root}/w/coder.gguf\nCHAT_MODEL=gguf:{root}/w/chatter.gguf\n"
            "OPENWEBUI_PORT=3100\n")


def test_fresh_run_without_a_terminal_creates_env_and_stops_for_the_one_edit(checkout):
    proc = sh(checkout)
    assert proc.returncode == EXIT_REVIEW, proc.stderr + proc.stdout
    env = parse_env((checkout / ".env").read_text(encoding="utf-8"))
    for key in SECRET_KEYS:
        assert env[key] != EXAMPLE[key]
    assert env[RUNTIME_KEY]                       # chosen from the descriptors for this host
    assert "edit " in proc.stdout and "then re-run ./install.sh" in proc.stdout
    assert "hardware  accelerator" in proc.stdout
    assert not list(checkout.glob(".env.bak.*")) and not (checkout / "config" / "models").exists()


def test_repair_run_is_ready_and_names_the_next_command(checkout):
    (checkout / ".env").write_text(valid_env(checkout), encoding="utf-8")
    proc = sh(checkout)
    assert proc.returncode == EXIT_OK, proc.stderr + proc.stdout
    assert "next      make bootstrap" in proc.stdout and "repaired (backup:" in proc.stdout
    env = parse_env((checkout / ".env").read_text(encoding="utf-8"))
    assert env["OPENWEBUI_PORT"] == "3100" and env[RUNTIME_KEY] == "llamacpp"
    assert env["CHAT_MODEL"] == f"gguf:{checkout}/w/chatter.gguf"
    assert all(env[key] and env[key] != EXAMPLE[key] for key in SECRET_KEYS)
    [backup] = checkout.glob(".env.bak.*")
    assert backup.read_text(encoding="utf-8") == valid_env(checkout)
    # a second run changes nothing and makes no second backup
    again = sh(checkout)
    assert again.returncode == EXIT_OK and "nothing to repair" in again.stdout
    assert len(list(checkout.glob(".env.bak.*"))) == 1


def test_check_writes_nothing_and_yes_accepts_the_generated_file(checkout):
    proc = sh(checkout, "--check")
    assert proc.returncode in (EXIT_OK, EXIT_PROBLEMS) and not (checkout / ".env").exists()
    assert "--check: nothing written" in proc.stdout
    proc = sh(checkout, "--yes", "--json")
    assert proc.returncode in (EXIT_OK, EXIT_PROBLEMS) and (checkout / ".env").is_file()
    assert '"created": true' in proc.stdout and '"exit_code"' in proc.stdout


def test_usage_help_and_preflight(checkout, tmp_path):
    proc = sh(checkout, "--help")
    assert proc.returncode == 0 and "1 detect" in proc.stdout and "Exit codes" in proc.stdout
    proc = sh(checkout, "--class", "gigantic")
    assert proc.returncode == EXIT_USAGE and "not a capability class" in proc.stderr
    proc = sh(checkout, "--bogus-flag")
    assert proc.returncode == EXIT_USAGE and "--bogus-flag" in proc.stderr
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    shutil.copy(REPO_ROOT / "install.sh", empty / "install.sh")
    proc = sh(empty)
    assert proc.returncode == EXIT_USAGE and "local-ezai checkout" in proc.stderr
    assert not (empty / ".env").exists()
