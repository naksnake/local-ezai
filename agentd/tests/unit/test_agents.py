import pytest

from agentd.agents import CoderAgent, GitAgent, PlannerAgent, ValidationAgent
from agentd.agents.base import AgentError
from agentd.journal import NullJournal
from agentd.llm import ScriptedLLM
from agentd.runner import build_registry
from agentd.schemas import Plan, TaskResult, ValidationReport
from tests.conftest import CHECK_CMD, PLAN_JSON, git


def make(agent_cls, config, script, journal=None):
    journal = journal or NullJournal()
    llm = ScriptedLLM(script)
    registry = build_registry(config, journal)
    return agent_cls(config, llm, registry, journal), llm


# ── Planner ──────────────────────────────────────────────────────────────────


def test_planner_returns_valid_plan(config, inplace_ws):
    planner, _ = make(PlannerAgent, config, [{"content": PLAN_JSON}])
    plan = planner.run("fix the add bug", inplace_ws)
    assert plan.tasks[0].id == "T1"
    assert plan.tasks[0].check == CHECK_CMD


def test_planner_retries_invalid_json(config, inplace_ws):
    planner, llm = make(
        PlannerAgent, config,
        [{"content": "sorry, thinking out loud"}, {"content": PLAN_JSON}],
    )
    plan = planner.run("fix it", inplace_ws)
    assert plan.goal
    assert len(llm.calls) == 2
    # the retry prompt carries the parse error back to the model
    assert "could not be used" in llm.calls[1]["messages"][1]["content"]


def test_planner_gives_up_after_retries(config, inplace_ws):
    config.llm.retries = 1
    planner, _ = make(
        PlannerAgent, config, [{"content": "nope"}, {"content": "still nope"}]
    )
    with pytest.raises(AgentError, match="no valid structured output"):
        planner.run("fix it", inplace_ws)


def test_planner_can_explore_with_tools(config, inplace_ws):
    planner, llm = make(
        PlannerAgent, config,
        [
            {"tool_calls": [{"name": "fs_ls", "arguments": {}}]},
            {"content": PLAN_JSON},
        ],
    )
    plan = planner.run("fix it", inplace_ws)
    assert plan.tasks
    # tool result was fed back as a tool message
    roles = [m["role"] for m in llm.calls[1]["messages"]]
    assert "tool" in roles


def test_planner_trims_oversized_plans(config, inplace_ws):
    config.limits.max_plan_tasks = 1
    import json

    big = json.loads(PLAN_JSON)
    big["tasks"] = [
        {"id": f"T{i}", "intent": f"task {i}"} for i in range(1, 5)
    ]
    planner, _ = make(PlannerAgent, config, [{"content": json.dumps(big)}])
    plan = planner.run("fix it", inplace_ws)
    assert len(plan.tasks) == 1


def test_planner_cannot_write(config, inplace_ws):
    planner, _ = make(
        PlannerAgent, config,
        [
            {"tool_calls": [{"name": "fs_write",
                             "arguments": {"path": "evil.py", "content": "x"}}]},
            {"content": PLAN_JSON},
        ],
    )
    planner.run("fix it", inplace_ws)
    assert not (inplace_ws.root / "evil.py").exists()


# ── Coder ────────────────────────────────────────────────────────────────────


def _plan():
    return Plan.model_validate(
        {"goal": "fix add", "tasks": [{"id": "T1", "intent": "make add() add"}]}
    )


def test_coder_edits_and_reports(config, inplace_ws):
    coder, _ = make(
        CoderAgent, config,
        [
            {"tool_calls": [{"name": "fs_edit",
                             "arguments": {"path": "calculator.py",
                                           "old_string": "return a - b",
                                           "new_string": "return a + b"}}]},
            {"content": "done"},
        ],
    )
    result = coder.run(_plan(), _plan().tasks[0], inplace_ws)
    assert result.status == "done"
    assert result.files_changed == ["calculator.py"]
    assert "return a + b" in (inplace_ws.root / "calculator.py").read_text()


def test_coder_failed_marker(config, inplace_ws):
    coder, _ = make(CoderAgent, config, [{"content": "FAILED: cannot find the file"}])
    result = coder.run(_plan(), _plan().tasks[0], inplace_ws)
    assert result.status == "failed"


def test_coder_budget_exhausted(config, inplace_ws):
    config.limits.max_agent_turns = 2
    coder, _ = make(
        CoderAgent, config,
        [{"tool_calls": [{"name": "fs_ls", "arguments": {}}]}] * 2,
    )
    result = coder.run(_plan(), _plan().tasks[0], inplace_ws)
    assert result.status == "failed"
    assert "budget" in result.summary


# ── Validator (deterministic) ────────────────────────────────────────────────


def test_validator_pass_and_fail(config, inplace_ws):
    config.validation.commands = {"test": ["true"], "lint": ["false"]}
    validator, _ = make(ValidationAgent, config, [])
    report = validator.run(inplace_ws)
    assert not report.passed
    assert {c.name: c.ok for c in report.checks} == {"lint[0]": False, "test[0]": True}


def test_validator_nothing_configured_warns(config, inplace_ws):
    config.validation.commands = {}
    config.validation.autodetect = False
    validator, _ = make(ValidationAgent, config, [])
    report = validator.run(inplace_ws)
    assert report.passed
    assert "nothing was verified" in report.summary


def test_validator_autodetects_pytest_and_ruff(config, inplace_ws):
    import sys

    (inplace_ws.root / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    (inplace_ws.root / "tests").mkdir()
    detected = ValidationAgent._autodetect(inplace_ws)
    python = f'"{sys.executable}"'
    assert detected["test"] == [f"{python} -m pytest -q --color=no"]
    assert detected["lint"] == [f"{python} -m ruff check ."]


# ── Git agent (deterministic path) ───────────────────────────────────────────


def _delivery_inputs():
    plan = _plan()
    results = [TaskResult(task_id="T1", status="done", summary="fixed add()")]
    report = ValidationReport(passed=True, summary="all 1 check(s) passed")
    return plan, results, report


def test_git_agent_commits_with_template_message(config, inplace_ws):
    (inplace_ws.root / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
    agent, _ = make(GitAgent, config, [])
    plan, results, report = _delivery_inputs()
    info = agent.run(inplace_ws, plan, results, report, "run-x")
    assert len(info.sha) == 40
    assert info.message.startswith("feat: fix add")
    assert "Agentd-Run: run-x" in info.message
    assert info.pushed is False
    assert "allow_push" in (info.push_error or "")
    assert git(inplace_ws.root, "log", "-1", "--format=%an") == "agentd"


def test_git_agent_no_changes(config, inplace_ws):
    agent, _ = make(GitAgent, config, [])
    plan, results, report = _delivery_inputs()
    info = agent.run(inplace_ws, plan, results, report, "run-x")
    assert info.sha == ""
    assert info.message == "no changes to commit"


def test_git_agent_pushes_when_allowed(config, inplace_ws, tmp_path):
    import subprocess

    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git(inplace_ws.root, "remote", "add", "origin", str(bare))
    config.git.allow_push = True
    (inplace_ws.root / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
    agent, _ = make(GitAgent, config, [])
    plan, results, report = _delivery_inputs()
    info = agent.run(inplace_ws, plan, results, report, "run-y")
    assert info.pushed is True
    assert git(bare, "rev-parse", "refs/heads/main") == info.sha


def test_git_agent_fix_only_plan_uses_fix_prefix(config, inplace_ws):
    (inplace_ws.root / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
    plan = Plan.model_validate(
        {"goal": "repair add()", "tasks": [{"id": "T1", "intent": "x", "kind": "fix"}]}
    )
    results = [TaskResult(task_id="T1", status="done", summary="fixed")]
    report = ValidationReport(passed=True, summary="ok")
    agent, _ = make(GitAgent, config, [])
    info = agent.run(inplace_ws, plan, results, report, "run-z")
    assert info.message.startswith("fix: repair add()")
