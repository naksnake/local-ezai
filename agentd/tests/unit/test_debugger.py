"""Debug Agent — structured reports, root-cause context, read-only enforcement."""

import pytest

from agentd.agents import DebuggerAgent
from agentd.agents.base import AgentError
from agentd.journal import Journal, NullJournal
from agentd.llm import ScriptedLLM
from agentd.rca import RcaEngine
from agentd.runner import build_registry
from agentd.schemas import CheckResult, HealingIteration, ValidationReport
from tests.conftest import debug_report_json

FAILING_REPORT = ValidationReport(
    passed=False,
    checks=[
        CheckResult(
            name="test[0]",
            command="python3 -c ...",
            ok=False,
            exit_code=1,
            output_tail=(
                "Traceback (most recent call last):\n"
                '  File "<string>", line 1, in <module>\n'
                "AssertionError\n"
            ),
        )
    ],
    summary="failed: test[0]",
)


def make_debugger(config, script, journal=None):
    journal = journal or NullJournal()
    llm = ScriptedLLM(script)
    registry = build_registry(config, journal)
    return DebuggerAgent(config, llm, registry, journal), llm


def analyses():
    return RcaEngine().analyze(FAILING_REPORT)


def test_returns_validated_debug_report(config, inplace_ws):
    debugger, _ = make_debugger(config, [{"content": debug_report_json()}])
    report = debugger.run("fix add", FAILING_REPORT, analyses(), [], inplace_ws)
    assert report.category == "assertion"
    assert report.confidence == "high"
    assert report.fix_strategy.steps
    assert report.root_cause


def test_prompt_carries_rca_and_evidence(config, inplace_ws):
    debugger, llm = make_debugger(config, [{"content": debug_report_json()}])
    debugger.run("fix add", FAILING_REPORT, analyses(), [], inplace_ws)
    user_msg = llm.calls[0]["messages"][1]["content"]
    assert "Root-cause analysis engine output" in user_msg
    assert "category: exception" in user_msg or "category: assertion" in user_msg
    assert "AssertionError" in user_msg
    assert "first debugging iteration" in user_msg


def test_prompt_carries_history_to_avoid_repeats(config, inplace_ws):
    history = [
        HealingIteration(
            iteration=1,
            root_cause="wrong operator",
            confidence="high",
            fix_task_id="HEAL1",
            fix_status="done",
            revalidation_passed=False,
        )
    ]
    debugger, llm = make_debugger(config, [{"content": debug_report_json()}])
    debugger.run("fix add", FAILING_REPORT, analyses(), history, inplace_ws)
    user_msg = llm.calls[0]["messages"][1]["content"]
    assert "iteration 1" in user_msg
    assert "wrong operator" in user_msg
    assert "FAILED" in user_msg  # revalidation outcome visible


def test_debugger_can_reproduce_with_exec(config, inplace_ws):
    debugger, llm = make_debugger(
        config,
        [
            {"tool_calls": [{"name": "exec_run",
                             "arguments": {"command": "echo reproduce"}}]},
            {"content": debug_report_json()},
        ],
    )
    report = debugger.run("fix add", FAILING_REPORT, analyses(), [], inplace_ws)
    assert report.root_cause
    tool_msg = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"]
    assert "reproduce" in tool_msg[0]["content"]


def test_debugger_cannot_write(config, inplace_ws):
    debugger, _ = make_debugger(
        config,
        [
            {"tool_calls": [{"name": "fs_write",
                             "arguments": {"path": "hack.py", "content": "x"}}]},
            {"content": debug_report_json()},
        ],
    )
    debugger.run("fix add", FAILING_REPORT, analyses(), [], inplace_ws)
    assert not (inplace_ws.root / "hack.py").exists()


def test_invalid_json_is_retried(config, inplace_ws):
    debugger, llm = make_debugger(
        config,
        [{"content": "thinking..."}, {"content": debug_report_json()}],
    )
    report = debugger.run("fix add", FAILING_REPORT, analyses(), [], inplace_ws)
    assert report.root_cause
    assert len(llm.calls) == 2


def test_gives_up_after_retries(config, inplace_ws):
    config.llm.retries = 1
    debugger, _ = make_debugger(config, [{"content": "no"}, {"content": "still no"}])
    with pytest.raises(AgentError):
        debugger.run("fix add", FAILING_REPORT, analyses(), [], inplace_ws)


def test_debug_report_journaled(config, inplace_ws, tmp_path):
    journal = Journal(tmp_path / "runs" / "dbg")
    debugger, _ = make_debugger(config, [{"content": debug_report_json()}], journal)
    debugger.run("fix add", FAILING_REPORT, analyses(), [], inplace_ws)
    events = journal.read()
    reports = [e for e in events if e["type"] == "DEBUG_REPORT"]
    assert reports
    payload = reports[0]["payload"]
    assert payload["root_cause"]
    assert payload["category"] == "assertion"
    assert payload["confidence"] == "high"
    assert payload["approach"]


def test_schema_rejects_report_without_strategy_steps(config, inplace_ws):
    import json

    config.llm.retries = 0
    bad = json.loads(debug_report_json())
    bad["fix_strategy"]["steps"] = []
    debugger, _ = make_debugger(config, [{"content": json.dumps(bad)}])
    with pytest.raises(AgentError):
        debugger.run("fix add", FAILING_REPORT, analyses(), [], inplace_ws)
