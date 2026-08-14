"""Shared fixtures: throwaway git repos, offline configs, scripted LLMs.

The whole suite runs offline — no model server, no network. ScriptedLLM
replays canned responses; everything else (files, git, subprocesses) is real.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agentd.config import AgentdConfig
from agentd.journal import Journal, NullJournal
from agentd.permissions import PermissionPolicy
from agentd.runner import build_registry
from agentd.tools.base import ToolRegistry
from agentd.workspace import Workspace

BUGGY_CALCULATOR = "def add(a, b):\n    return a - b\n"

# Validation command that needs nothing beyond the system python3.
CHECK_CMD = 'python3 -c "import calculator; assert calculator.add(2, 3) == 5"'

REPO_AGENTD_YAML = f"validation:\n  commands:\n    test:\n      - {CHECK_CMD!r}\n"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def git_commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=t@t",
         "commit", "-m", message],
        capture_output=True, text=True, check=True,
    )


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """A git repo containing a buggy calculator and a validation config."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    (repo / "calculator.py").write_text(BUGGY_CALCULATOR, encoding="utf-8")
    (repo / ".agentd.yaml").write_text(REPO_AGENTD_YAML, encoding="utf-8")
    git_commit_all(repo, "initial commit")
    return repo


@pytest.fixture
def config(tmp_path: Path) -> AgentdConfig:
    """Offline config with all state under tmp_path."""
    cfg = AgentdConfig()
    cfg.workspace.root = tmp_path / "workspaces"
    cfg.runs_dir = tmp_path / "runs"
    cfg.validation.autodetect = False
    cfg.llm.provider = "scripted"
    return cfg


@pytest.fixture
def inplace_ws(tmp_repo: Path) -> Workspace:
    """Workspace acting directly on the fixture repo (for tool unit tests)."""
    return Workspace(root=tmp_repo, repo_path=tmp_repo, branch="main", mode="in-place")


@pytest.fixture
def registry(config: AgentdConfig) -> ToolRegistry:
    return build_registry(config, NullJournal())


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    return Journal(tmp_path / "runs" / "test")


def make_registry(config: AgentdConfig, journal=None) -> ToolRegistry:
    return build_registry(config, journal or NullJournal())


def policy(config: AgentdConfig) -> PermissionPolicy:
    return PermissionPolicy(config)


# ── canned LLM scripts ───────────────────────────────────────────────────────

PLAN_JSON = json.dumps(
    {
        "goal": "Fix the add() bug in calculator.py",
        "assumptions": [],
        "tasks": [
            {
                "id": "T1",
                "intent": (
                    "calculator.add subtracts instead of adding; make it "
                    "return a + b and add a regression test file "
                    "test_calculator.py"
                ),
                "files_hint": ["calculator.py"],
                "check": CHECK_CMD,
                "kind": "fix",
            }
        ],
        "risks": [],
    }
)


def planner_response() -> dict:
    return {"content": PLAN_JSON}


def happy_path_script() -> list[dict]:
    """Planner → coder reads, fixes, writes a test → final summary."""
    return [
        planner_response(),
        {"tool_calls": [{"name": "fs_read", "arguments": {"path": "calculator.py"}}]},
        {
            "tool_calls": [
                {
                    "name": "fs_edit",
                    "arguments": {
                        "path": "calculator.py",
                        "old_string": "return a - b",
                        "new_string": "return a + b",
                    },
                },
                {
                    "name": "fs_write",
                    "arguments": {
                        "path": "test_calculator.py",
                        "content": (
                            "import calculator\n\n\n"
                            "def test_add():\n"
                            "    assert calculator.add(2, 3) == 5\n"
                        ),
                    },
                },
            ]
        },
        {"content": "Fixed add() to use addition and added a regression test."},
    ]


def fix_loop_script() -> list[dict]:
    """Coder botches the first fix; validation fails; second attempt lands."""
    return [
        planner_response(),
        {
            "tool_calls": [
                {
                    "name": "fs_edit",
                    "arguments": {
                        "path": "calculator.py",
                        "old_string": "return a - b",
                        "new_string": "return a * b",
                    },
                }
            ]
        },
        {"content": "Changed the operator."},
        # FIX1 task after validation failure:
        {
            "tool_calls": [
                {
                    "name": "fs_edit",
                    "arguments": {
                        "path": "calculator.py",
                        "old_string": "return a * b",
                        "new_string": "return a + b",
                    },
                }
            ]
        },
        {"content": "Corrected the operator to addition."},
    ]
