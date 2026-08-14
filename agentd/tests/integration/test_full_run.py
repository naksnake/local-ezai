"""End-to-end graph runs: real git, real files, real validation subprocesses;
only the LLM is scripted."""

import subprocess

from agentd.llm import ScriptedLLM
from agentd.runner import execute_run
from tests.conftest import fix_loop_script, git, happy_path_script


def test_happy_path_run(config, tmp_repo):
    report = execute_run(
        config, tmp_repo, "fix the add bug",
        llm=ScriptedLLM(happy_path_script()), run_id="itest1",
    )

    assert report.status == "completed"
    assert report.branch == "swe/itest1"
    assert report.fix_attempts == 0

    # plan / tasks / validation / commit envelopes are all populated
    assert report.plan and report.plan.tasks[0].id == "T1"
    assert [r.status for r in report.task_results] == ["done"]
    assert report.validation and report.validation.passed
    assert report.commit and len(report.commit.sha) == 40
    assert report.commit.pushed is False  # fail-closed push

    # the fix landed on the run branch, visible from the original repo ...
    fixed = git(tmp_repo, "show", "swe/itest1:calculator.py")
    assert "return a + b" in fixed
    added_test = git(tmp_repo, "show", "swe/itest1:test_calculator.py")
    assert "test_add" in added_test

    # ... while the user's checkout is untouched
    assert "return a - b" in (tmp_repo / "calculator.py").read_text()
    assert git(tmp_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(tmp_repo, "status", "--porcelain") == ""


def test_happy_path_journal_and_report(config, tmp_repo):
    report = execute_run(
        config, tmp_repo, "fix the add bug",
        llm=ScriptedLLM(happy_path_script()), run_id="itest2",
    )
    run_dir = config.runs_dir / "itest2"
    assert (run_dir / "report.json").is_file()

    import json

    events = [
        json.loads(line)
        for line in (run_dir / "journal.jsonl").read_text().strip().splitlines()
    ]
    types = [e["type"] for e in events]
    for expected in (
        "RUN_SUBMITTED", "STATE_ENTERED", "AGENT_SPAWNED", "LLM_CALL",
        "TOOL_CALLED", "TOOL_RESULT", "PLAN_READY", "TASK_RESULT",
        "CHECK_STARTED", "CHECK_FINISHED", "VALIDATION", "GIT_DELIVERY",
        "RUN_TERMINAL",
    ):
        assert expected in types, f"missing {expected} in journal"
    assert types[-1] == "RUN_TERMINAL"
    assert events[-1]["payload"]["status"] == "completed"
    # seq numbers are strictly increasing from 1
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    # the denied push decision is in the audit trail
    push_calls = [
        e for e in events
        if e["type"] == "TOOL_CALLED" and e["payload"]["tool"] == "git_push"
    ]
    assert push_calls and push_calls[0]["payload"]["allowed"] is False
    assert report.journal_path == str(run_dir / "journal.jsonl")


def test_fix_loop_recovers_from_validation_failure(config, tmp_repo):
    report = execute_run(
        config, tmp_repo, "fix the add bug",
        llm=ScriptedLLM(fix_loop_script()), run_id="itest3",
    )
    assert report.status == "completed"
    assert report.fix_attempts == 1
    assert [r.task_id for r in report.task_results] == ["T1", "FIX1"]
    assert report.validation and report.validation.passed
    assert "return a + b" in git(tmp_repo, "show", "swe/itest3:calculator.py")


def test_fix_budget_exhaustion_fails_cleanly(config, tmp_repo):
    config.limits.max_fix_attempts = 0
    script = fix_loop_script()[:3]  # plan + one bad coder attempt only
    report = execute_run(
        config, tmp_repo, "fix the add bug",
        llm=ScriptedLLM(script), run_id="itest4",
    )
    assert report.status == "failed"
    assert "validation still failing" in (report.error or "")
    assert report.commit is None  # nothing was committed
    # workspace preserved for autopsy
    assert (config.workspace.root / "itest4" / "calculator.py").is_file()


def test_push_delivery_when_enabled(config, tmp_repo, tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git(tmp_repo, "remote", "add", "origin", str(bare))
    config.git.allow_push = True

    report = execute_run(
        config, tmp_repo, "fix the add bug",
        llm=ScriptedLLM(happy_path_script()), run_id="itest5",
    )
    assert report.status == "completed"
    assert report.commit and report.commit.pushed is True
    assert git(bare, "rev-parse", "refs/heads/swe/itest5") == report.commit.sha


def test_in_place_mode_commits_on_current_branch(config, tmp_repo):
    config.workspace.mode = "in-place"
    report = execute_run(
        config, tmp_repo, "fix the add bug",
        llm=ScriptedLLM(happy_path_script()), run_id="itest6",
    )
    assert report.status == "completed"
    assert report.branch == "main"
    assert report.workspace_path == str(tmp_repo)
    assert "return a + b" in (tmp_repo / "calculator.py").read_text()
    assert git(tmp_repo, "log", "-1", "--format=%s").startswith("fix: Fix the add()")


def test_coder_declared_failure_aborts_run(config, tmp_repo):
    from tests.conftest import planner_response

    script = [planner_response(), {"content": "FAILED: file is beyond repair"}]
    report = execute_run(
        config, tmp_repo, "fix the add bug",
        llm=ScriptedLLM(script), run_id="itest7",
    )
    assert report.status == "failed"
    assert "T1 failed" in (report.error or "")
