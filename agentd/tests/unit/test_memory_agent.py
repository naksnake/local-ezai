"""Memory Agent — learning from run outcomes, distillation, journal events,
and memory injection into planner/debugger prompts."""

import json

from agentd.agents import DebuggerAgent, MemoryAgent, PlannerAgent
from agentd.journal import Journal, NullJournal
from agentd.llm import ScriptedLLM
from agentd.memory import (
    KIND_FAILED_FIX,
    KIND_IMPLEMENTATION,
    KIND_RULE,
    KIND_SUCCESSFUL_FIX,
    MemoryStore,
)
from agentd.rca import RcaEngine
from agentd.runner import build_registry
from agentd.schemas import (
    CheckResult,
    CommitInfo,
    HealingIteration,
    Plan,
    RunReport,
    TaskResult,
    ValidationReport,
)
from tests.conftest import PLAN_JSON

SIG = "test[0]|assertion|AssertionError|"


def make_report(healing=None, status="completed") -> RunReport:
    return RunReport(
        run_id="run-mem",
        status=status,
        request="fix the add bug",
        repo_path="/r", workspace_path="/w", branch="swe/run-mem",
        plan=Plan.model_validate(
            {"goal": "fix the add bug", "tasks": [{"id": "T1", "intent": "x"}]}),
        task_results=[TaskResult(task_id="T1", status="done", summary="edited",
                                 files_changed=["calculator.py"])],
        validation=ValidationReport(passed=(status == "completed"), summary="ok"),
        commit=CommitInfo(sha="a" * 40, branch="swe/run-mem")
        if status == "completed" else None,
        healing=healing or [],
        iterations_used=len(healing or []),
        error=None if status == "completed" else "boom",
    )


def heal(passed: bool, iteration: int = 1) -> HealingIteration:
    return HealingIteration(
        iteration=iteration,
        error_signature=SIG,
        categories=["assertion"],
        root_cause="wrong operator in add()",
        confidence="high",
        approach="use addition in add()" if passed else "try multiplication",
        fix_task_id=f"HEAL{iteration}",
        fix_status="done",
        revalidation_passed=passed,
    )


def make_agent(config, store, script=None, journal=None):
    journal = journal or NullJournal()
    return MemoryAgent(config, ScriptedLLM(script or []),
                       build_registry(config, journal), journal, memory=store)


def test_record_run_learns_fixes_and_history(config, tmp_path, inplace_ws):
    store = MemoryStore(tmp_path / ".agent")
    agent = make_agent(config, store)
    report = make_report(healing=[heal(False, 1), heal(True, 2)])
    added = agent.record_run(report, inplace_ws)

    assert added == 3  # failed_fix + successful_fix + implementation
    assert store.count(KIND_FAILED_FIX) == 1
    assert store.count(KIND_SUCCESSFUL_FIX) == 1
    assert store.count(KIND_IMPLEMENTATION) == 1

    failed = store.recent([KIND_FAILED_FIX])[0]
    assert failed.error_signature == SIG
    assert failed.data["approach"] == "try multiplication"
    assert "revalidation still failed" in failed.content

    implementation = store.recent([KIND_IMPLEMENTATION])[0]
    assert implementation.files == ["calculator.py"]
    assert implementation.data["status"] == "completed"

    # lessons_learned.json regenerated
    lessons = json.loads(store.lessons_path.read_text())
    assert lessons["total_memories"] == 3
    store.close()


def test_record_failed_run_captures_error(config, tmp_path, inplace_ws):
    store = MemoryStore(tmp_path / ".agent")
    agent = make_agent(config, store)
    agent.record_run(make_report(status="failed"), inplace_ws)
    implementation = store.recent([KIND_IMPLEMENTATION])[0]
    assert "error: boom" in implementation.content
    store.close()


def test_record_run_journals(config, tmp_path, inplace_ws):
    store = MemoryStore(tmp_path / ".agent")
    journal = Journal(tmp_path / "runs" / "j")
    agent = make_agent(config, store, journal=journal)
    agent.record_run(make_report(healing=[heal(True)]), inplace_ws)
    events = [e for e in journal.read() if e["type"] == "MEMORY_RECORDED"]
    assert events and events[0]["payload"]["records"] == 2
    assert events[0]["payload"]["lessons"].endswith("lessons_learned.json")
    store.close()


def test_no_store_is_a_noop(config, inplace_ws):
    agent = make_agent(config, store=None)
    assert agent.record_run(make_report(), inplace_ws) == 0


def test_distillation_records_curated_kinds(config, tmp_path, inplace_ws):
    config.memory.distill = True
    store = MemoryStore(tmp_path / ".agent")
    observations = {"observations": [
        {"kind": "coding_style", "title": "naming",
         "content": "snake_case for all identifiers"},
        {"kind": "project_rule", "title": "tests required",
         "content": "every fix ships with a regression test"},
        {"kind": "gossip", "title": "bad", "content": "ignored kind"},
    ]}
    agent = make_agent(config, store, script=[{"content": json.dumps(observations)}])
    added = agent.record_run(make_report(), inplace_ws)
    assert added == 1 + 2  # implementation + 2 valid observations
    assert store.count("coding_style") == 1
    assert store.count("project_rule") == 1
    store.close()


def test_distillation_failure_is_best_effort(config, tmp_path, inplace_ws):
    config.memory.distill = True
    config.llm.retries = 0
    store = MemoryStore(tmp_path / ".agent")
    agent = make_agent(config, store, script=[{"content": "not json"}])
    added = agent.record_run(make_report(), inplace_ws)
    assert added == 1  # implementation only; distillation skipped quietly
    store.close()


# ── prompt injection (planner / debugger) ─────────────────────────────────────


def test_planner_prompt_includes_memory(config, tmp_path, inplace_ws):
    store = MemoryStore(tmp_path / ".agent")
    store.record(KIND_RULE, "pin deps", "always pin dependency versions")
    journal = Journal(tmp_path / "runs" / "p")
    llm = ScriptedLLM([{"content": PLAN_JSON}])
    planner = PlannerAgent(config, llm, build_registry(config, journal),
                           journal, memory=store)
    planner.run("fix the add bug", inplace_ws)
    user_msg = llm.calls[0]["messages"][1]["content"]
    assert "Project memory" in user_msg
    assert "pin deps" in user_msg
    assert any(e["type"] == "MEMORY_INJECTED" for e in journal.read())
    store.close()


def test_planner_without_memory_unchanged(config, inplace_ws):
    llm = ScriptedLLM([{"content": PLAN_JSON}])
    planner = PlannerAgent(config, llm, build_registry(config, NullJournal()),
                           NullJournal())
    planner.run("fix the add bug", inplace_ws)
    assert "Project memory" not in llm.calls[0]["messages"][1]["content"]


def test_debugger_prompt_includes_failed_approaches(config, tmp_path, inplace_ws):
    from tests.conftest import debug_report_json

    store = MemoryStore(tmp_path / ".agent")
    store.record(KIND_FAILED_FIX, "bad idea", "multiplied instead",
                 run_id="old-run", error_signature=SIG, category="assertion",
                 data={"approach": "try multiplication"})
    report = ValidationReport(
        passed=False,
        checks=[CheckResult(name="test[0]", command="c", ok=False, exit_code=1,
                            output_tail="AssertionError")],
        summary="failed: test[0]",
    )
    analyses = RcaEngine().analyze(report)
    assert analyses[0].signature == SIG  # same signature as the memory
    llm = ScriptedLLM([{"content": debug_report_json()}])
    debugger = DebuggerAgent(config, llm, build_registry(config, NullJournal()),
                             NullJournal(), memory=store)
    debugger.run("fix add", report, analyses, [], inplace_ws)
    user_msg = llm.calls[0]["messages"][1]["content"]
    assert "ALREADY FAILED" in user_msg
    assert "try multiplication" in user_msg
    assert "old-run" in user_msg
    store.close()
