"""local-ezai production CLI: all commands against real repos, scripted LLM.

Config comes from a YAML file (scripted provider) exactly as a user would
supply it — no in-process shortcuts except stdin for chat.
"""

import json

import pytest
import yaml

from agentd.main_cli import main, split_path_argument
from tests.conftest import (
    CHECK_CMD,
    coder_edit,
    debug_response,
    git,
    healing_script,
    planner_response,
)

APPROVE_JSON = json.dumps({"verdict": "approve", "summary": "clean", "findings": []})
CHANGES_JSON = json.dumps({
    "verdict": "request_changes", "summary": "bug present",
    "findings": [{"severity": "high", "file": "calculator.py", "line": 2,
                  "issue": "subtracts instead of adding",
                  "suggestion": "use a + b"}],
})


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """Returns invoke(script, *argv) → (exit_code, stdout)."""
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)

    def invoke(script, *argv, capsys):
        script_file = tmp_path / "script.json"
        script_file.write_text(json.dumps(script), encoding="utf-8")
        cfg = {
            "llm": {"provider": "scripted", "script_path": str(script_file)},
            "workspace": {"root": str(tmp_path / "ws")},
            "runs_dir": str(tmp_path / "runs"),
            "validation": {"autodetect": False},
        }
        cfg_file = tmp_path / "cfg.yaml"
        cfg_file.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        code = main([*argv, "--config", str(cfg_file)])
        return code, capsys.readouterr().out

    return invoke


# ── path selection ────────────────────────────────────────────────────────────


def test_split_path_argument(tmp_path):
    assert split_path_argument([str(tmp_path), "run", "x"]) == (str(tmp_path),
                                                                ["run", "x"])
    assert split_path_argument(["run", "x"]) == (None, ["run", "x"])
    assert split_path_argument(["-C", ".", "test"]) == (None, ["-C", ".", "test"])
    assert split_path_argument([str(tmp_path)]) == (str(tmp_path), [])
    # a non-existent path is not swallowed (argparse will report it)
    assert split_path_argument(["nope-dir", "run"]) == (None, ["nope-dir", "run"])


def test_leading_path_selects_project(tmp_repo, cli, capsys):
    code, out = cli([planner_response()], str(tmp_repo), "plan", "fix the add bug",
                    capsys=capsys)
    assert code == 0
    assert json.loads(out)["tasks"][0]["id"] == "T1"


def test_dash_c_selects_project(tmp_repo, cli, capsys):
    code, out = cli([planner_response()], "-C", str(tmp_repo), "plan",
                    "fix the add bug", capsys=capsys)
    assert code == 0


def test_commands_default_to_cwd(tmp_repo, cli, capsys, monkeypatch):
    monkeypatch.chdir(tmp_repo)
    code, out = cli([planner_response()], "plan", "fix the add bug", capsys=capsys)
    assert code == 0


def test_non_repo_rejected(tmp_path, cli, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    code, _ = cli([], str(plain), "test", capsys=capsys)
    assert code == 2


# ── run / code / test / fix ───────────────────────────────────────────────────


def test_run_full_pipeline(tmp_repo, cli, capsys):
    code, out = cli(healing_script(), str(tmp_repo), "run", "fix the add bug",
                    capsys=capsys)
    assert code == 0
    assert "[COMPLETED]" in out
    assert "healing:    1 debug/fix iteration(s)" in out


def test_code_command_leaves_uncommitted_changes(tmp_repo, cli, capsys):
    script = [
        planner_response(),
        coder_edit("return a - b", "return a + b"),
        {"content": "implemented"},
    ]
    code, out = cli(script, str(tmp_repo), "code", "fix the add bug", capsys=capsys)
    assert code == 0
    assert "UNCOMMITTED" in out
    # branch exists but has no commits beyond main; the edit sits uncommitted
    assert git(tmp_repo, "rev-parse", "swe/" + _branch_run_id(out)) \
        == git(tmp_repo, "rev-parse", "main")


def _branch_run_id(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("run:"):
            return line.split()[1]
    raise AssertionError(f"no run id in output:\n{out}")


def test_test_command_pass_and_fail(tmp_repo, cli, capsys):
    code, out = cli([], str(tmp_repo), "test", capsys=capsys)  # buggy calculator
    assert code == 1
    assert "[FAIL] test[0]" in out

    (tmp_repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
    code, out = cli([], str(tmp_repo), "test", capsys=capsys)
    assert code == 0
    assert "[PASS] test[0]" in out


def test_fix_command_repairs_in_place_and_commits(tmp_repo, cli, capsys):
    # repo starts with the failing check; fix enters at VALIDATE (no planner)
    script = [
        debug_response(),
        coder_edit("return a - b", "return a + b"),
        {"content": "applied the diagnosed fix"},
    ]
    head_before = git(tmp_repo, "rev-parse", "HEAD")
    code, out = cli(script, str(tmp_repo), "fix", capsys=capsys)
    assert code == 0
    assert "[COMPLETED]" in out
    # repaired IN PLACE on the current branch, and committed
    assert "return a + b" in (tmp_repo / "calculator.py").read_text()
    assert git(tmp_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(tmp_repo, "rev-parse", "HEAD") != head_before
    assert git(tmp_repo, "log", "-1", "--format=%s").startswith("fix:")


def test_fix_command_green_repo_is_noop_commit(tmp_repo, cli, capsys):
    (tmp_repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
    git(tmp_repo, "add", "-A")
    import subprocess
    subprocess.run(["git", "-C", str(tmp_repo), "-c", "user.name=t",
                    "-c", "user.email=t@t", "commit", "-qm", "fixed"], check=True)
    code, out = cli([], str(tmp_repo), "fix", capsys=capsys)
    assert code == 0
    assert "no changes to commit" in out


# ── review / commit ───────────────────────────────────────────────────────────


def test_review_dirty_tree(tmp_repo, cli, capsys):
    (tmp_repo / "calculator.py").write_text("def add(a, b):\n    return a * b\n")
    code, out = cli([{"content": CHANGES_JSON}], str(tmp_repo), "review",
                    capsys=capsys)
    assert code == 1
    assert "REQUEST_CHANGES" in out
    assert "calculator.py:2" in out
    assert "use a + b" in out


def test_review_clean_tree_reviews_last_commit(tmp_repo, cli, capsys):
    code, out = cli([{"content": APPROVE_JSON}], str(tmp_repo), "review",
                    capsys=capsys)
    assert code == 0
    assert "APPROVE" in out


def test_commit_blocked_until_validation_green(tmp_repo, cli, capsys):
    # dirty tree, but the check still fails → gate refuses
    (tmp_repo / "notes.md").write_text("hello\n")
    head = git(tmp_repo, "rev-parse", "HEAD")
    code, out = cli([], str(tmp_repo), "commit", capsys=capsys)
    assert code == 1
    assert git(tmp_repo, "rev-parse", "HEAD") == head  # nothing committed

    # make validation green → commit succeeds
    (tmp_repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
    code, out = cli([], str(tmp_repo), "commit", "-m", "add notes and fix add",
                    capsys=capsys)
    assert code == 0
    assert "committed" in out
    assert git(tmp_repo, "log", "-1", "--format=%s") == "feat: add notes and fix add"


def test_commit_clean_tree(tmp_repo, cli, capsys):
    (tmp_repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
    import subprocess
    subprocess.run(["git", "-C", str(tmp_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_repo), "-c", "user.name=t",
                    "-c", "user.email=t@t", "commit", "-qm", "green"], check=True)
    code, out = cli([], str(tmp_repo), "commit", capsys=capsys)
    assert code == 0
    assert "nothing to commit" in out


# ── memory ────────────────────────────────────────────────────────────────────


def test_memory_add_and_list(tmp_repo, cli, capsys):
    code, out = cli([], str(tmp_repo), "memory", "--add",
                    "always pin dependencies", capsys=capsys)
    assert code == 0
    assert "remembered #1" in out
    code, out = cli([], str(tmp_repo), "memory", capsys=capsys)
    assert code == 0
    assert "always pin dependencies" in out
    assert "1 total memories" in out


# ── chat ──────────────────────────────────────────────────────────────────────


def test_chat_repl(tmp_repo, cli, capsys, monkeypatch):
    lines = iter(["hello there", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    code, out = cli([{"content": "Hi! I can plan and run tasks for you."}],
                    str(tmp_repo), "chat", capsys=capsys)
    assert code == 0
    assert "local-ezai chat — project:" in out
    assert "ezai> Hi! I can plan and run tasks for you." in out


def test_bare_path_opens_chat(tmp_repo, cli, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "/exit")
    code, out = cli([], str(tmp_repo), capsys=capsys)
    assert code == 0
    assert "local-ezai chat" in out


# ── sprint ────────────────────────────────────────────────────────────────────


def plan_for(goal, path, content):
    return {"content": json.dumps({
        "goal": goal, "assumptions": [], "risks": [],
        "tasks": [{"id": "T1", "intent": goal, "files_hint": [path],
                   "check": CHECK_CMD, "kind": "feature"}],
    })}


def sprint_task_script(goal, path, content):
    return [
        plan_for(goal, path, content),
        {"tool_calls": [{"name": "fs_write",
                         "arguments": {"path": path, "content": content}}]},
        {"content": f"done: {goal}"},
    ]


def test_sprint_runs_tasks_on_one_branch(tmp_repo, cli, capsys):
    # make the repo green so each task's validation passes
    (tmp_repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
    import subprocess
    subprocess.run(["git", "-C", str(tmp_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_repo), "-c", "user.name=t",
                    "-c", "user.email=t@t", "commit", "-qm", "green"], check=True)

    spec = tmp_repo / "sprint28.md"
    spec.write_text(
        "# Sprint 28\n"
        "- [ ] create the feature-one module\n"
        "- [ ] create the feature-two module\n"
    )
    script = [
        *sprint_task_script("create the feature-one module", "feature_one.py",
                            "ONE = 1\n"),
        *sprint_task_script("create the feature-two module", "feature_two.py",
                            "TWO = 2\n"),
    ]
    code, out = cli(script, str(tmp_repo), "sprint", str(spec), "--simple",
                    capsys=capsys)
    assert code == 0
    assert "[DONE] 1." in out and "[DONE] 2." in out
    assert "2/2 task(s)" in out

    branch = [ln for ln in out.splitlines() if "branch sprint/" in ln][0]
    branch_name = branch.split("branch ")[-1].strip()
    # both tasks committed sequentially on the shared sprint branch
    files = git(tmp_repo, "show", "--name-only", "--format=", branch_name)
    assert "feature_two.py" in files
    log_out = git(tmp_repo, "log", "--format=%s", branch_name)
    assert len(log_out.splitlines()) == 4  # initial + green + task1 + task2
    assert "feature-two" in log_out.splitlines()[0]
    # task two built on top of task one
    assert "ONE = 1" in git(tmp_repo, "show", f"{branch_name}:feature_one.py")


def test_sprint_stops_on_failure_by_default(tmp_repo, cli, capsys):
    (tmp_repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
    import subprocess
    subprocess.run(["git", "-C", str(tmp_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_repo), "-c", "user.name=t",
                    "-c", "user.email=t@t", "commit", "-qm", "green"], check=True)
    spec = tmp_repo / "sprint.md"
    spec.write_text("- [ ] task one\n- [ ] task two\n")
    script = [
        planner_response(),  # plan for task one
        {"content": "FAILED: cannot implement"},  # coder gives up
        # task two must never be asked for — script would be exhausted
    ]
    code, out = cli(script, str(tmp_repo), "sprint", str(spec), "--simple",
                    capsys=capsys)
    assert code == 1
    assert "[FAIL] 1." in out
    assert "[SKIP] 2." in out
    assert "FAILED" in out


def test_sprint_missing_spec(tmp_repo, cli, capsys):
    code, _ = cli([], str(tmp_repo), "sprint", "no-such-file.md", capsys=capsys)
    assert code == 2
