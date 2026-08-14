"""Self-healing workflow end-to-end: PLAN → CODE → VALIDATE → DEBUG → FIX →
REVALIDATE, bounded by max_heal_iterations (10) and stall detection.

Real git worktrees, real validation subprocesses; only the LLM is scripted.
"""

import json

from agentd.llm import ScriptedLLM
from agentd.runner import execute_run
from tests.conftest import (
    bad_first_attempt,
    coder_edit,
    debug_response,
    git,
    healing_script,
)


def _events(config, run_id):
    journal = config.runs_dir / run_id / "journal.jsonl"
    return [json.loads(line) for line in journal.read_text().strip().splitlines()]


def test_one_iteration_heals_and_delivers(config, tmp_repo):
    report = execute_run(
        config, tmp_repo, "fix the add bug",
        llm=ScriptedLLM(healing_script()), run_id="heal1",
    )
    assert report.status == "completed"
    assert report.iterations_used == 1
    assert report.validation and report.validation.passed
    assert [r.task_id for r in report.task_results] == ["T1", "HEAL1"]

    # structured healing record (observability requirement)
    assert len(report.healing) == 1
    record = report.healing[0]
    assert record.iteration == 1
    assert record.categories == ["assertion"]
    assert record.error_signature
    assert record.root_cause.startswith("add() uses the wrong")
    assert record.confidence == "high"
    assert record.fix_task_id == "HEAL1"
    assert record.fix_status == "done"
    assert record.revalidation_passed is True

    # the healed fix is committed on the run branch
    assert report.commit and report.commit.sha
    assert "return a + b" in git(tmp_repo, "show", "swe/heal1:calculator.py")


def test_state_machine_trajectory_journaled(config, tmp_repo):
    execute_run(
        config, tmp_repo, "fix the add bug",
        llm=ScriptedLLM(healing_script()), run_id="heal2",
    )
    events = _events(config, "heal2")
    states = [e["payload"]["state"] for e in events if e["type"] == "STATE_ENTERED"]
    # the required machine: PLAN → CODE → VALIDATE → DEBUG → FIX → REVALIDATE → GIT
    assert states == ["PLAN", "CODE", "VALIDATE", "DEBUG", "FIX", "REVALIDATE", "GIT"]

    types = [e["type"] for e in events]
    for expected in ("RCA_REPORT", "DEBUG_REPORT", "FIX_APPLIED", "HEAL_ITERATION"):
        assert expected in types, f"missing {expected}"

    rca = next(e for e in events if e["type"] == "RCA_REPORT")
    assert rca["payload"]["categories"] == ["assertion"]
    assert rca["payload"]["signature"]
    heal = next(e for e in events if e["type"] == "HEAL_ITERATION")
    assert heal["payload"]["iteration"] == 1
    assert heal["payload"]["passed"] is True


def test_two_iterations_until_success(config, tmp_repo):
    script = [
        *bad_first_attempt(),
        # iteration 1: diagnosis leads to another wrong fix
        debug_response(approach="use integer division"),
        coder_edit("return a * b", "return a // b"),
        {"content": "Tried integer division."},
        # iteration 2: correct fix
        debug_response(approach="use addition as the goal states"),
        coder_edit("return a // b", "return a + b"),
        {"content": "Corrected to addition."},
    ]
    report = execute_run(
        config, tmp_repo, "fix the add bug",
        llm=ScriptedLLM(script), run_id="heal3",
    )
    assert report.status == "completed"
    assert report.iterations_used == 2
    assert [h.revalidation_passed for h in report.healing] == [False, True]
    assert [r.task_id for r in report.task_results] == ["T1", "HEAL1", "HEAL2"]


def test_max_iterations_cap_enforced(config, tmp_repo):
    config.limits.max_heal_iterations = 1
    config.limits.stall_threshold = 99  # isolate the iteration cap
    script = [
        *bad_first_attempt(),
        debug_response(),
        {"content": "I believe it is fixed."},  # no-op fix
    ]
    report = execute_run(
        config, tmp_repo, "fix the add bug",
        llm=ScriptedLLM(script), run_id="heal4",
    )
    assert report.status == "failed"
    assert report.iterations_used == 1
    assert "self-healing budget exhausted" in (report.error or "")
    assert "(max 1)" in report.error
    assert report.commit is None
    # the failed iteration is still fully recorded
    assert len(report.healing) == 1
    assert report.healing[0].revalidation_passed is False


def test_default_cap_is_ten(config, tmp_repo):
    assert config.limits.max_heal_iterations == 10
    events_needed = [
        *bad_first_attempt(),
    ]
    # 10 iterations of (debug + no-op fix); every signature differs enough?
    # No — identical signatures would stall at 3. Disable stall to prove the
    # hard cap alone stops the loop at exactly 10.
    config.limits.stall_threshold = 99
    for _ in range(10):
        events_needed.append(debug_response())
        events_needed.append({"content": "attempted."})
    report = execute_run(
        config, tmp_repo, "fix the add bug",
        llm=ScriptedLLM(events_needed), run_id="heal5",
    )
    assert report.status == "failed"
    assert report.iterations_used == 10
    assert len(report.healing) == 10
    assert "(max 10)" in report.error


def test_stall_detection_stops_symptom_patching(config, tmp_repo):
    # stall_threshold=3: initial failure + 2 identical revalidations → abort,
    # well before the 10-iteration budget.
    script = [
        *bad_first_attempt(),
        debug_response(),
        {"content": "attempt 1."},  # no-op → same signature
        debug_response(),
        {"content": "attempt 2."},  # no-op → same signature (3rd identical)
    ]
    report = execute_run(
        config, tmp_repo, "fix the add bug",
        llm=ScriptedLLM(script), run_id="heal6",
    )
    assert report.status == "failed"
    assert report.iterations_used == 2
    assert "no progress" in report.error
    assert "root cause" in report.error
    events = _events(config, "heal6")
    last_rca = [e for e in events if e["type"] == "RCA_REPORT"][-1]
    assert last_rca["payload"]["stalled"] is True


def test_failed_fix_attempt_continues_the_loop(config, tmp_repo):
    script = [
        *bad_first_attempt(),
        debug_response(),
        {"content": "FAILED: could not locate the operator"},  # fix attempt fails
        debug_response(),
        coder_edit("return a * b", "return a + b"),
        {"content": "Fixed on the second attempt."},
    ]
    report = execute_run(
        config, tmp_repo, "fix the add bug",
        llm=ScriptedLLM(script), run_id="heal7",
    )
    assert report.status == "completed"
    assert report.iterations_used == 2
    assert report.healing[0].fix_status == "failed"
    assert report.healing[1].fix_status == "done"
    statuses = {r.task_id: r.status for r in report.task_results}
    assert statuses == {"T1": "done", "HEAL1": "failed", "HEAL2": "done"}


def test_fix_task_carries_root_cause_context(config, tmp_repo):
    llm = ScriptedLLM(healing_script())
    execute_run(config, tmp_repo, "fix the add bug", llm=llm, run_id="heal8")
    # call #5 (index 4) is the fix-task coder invocation
    fix_call = llm.calls[4]
    user_msg = fix_call["messages"][1]["content"]
    assert "Root cause:" in user_msg
    assert "not the symptom" in user_msg
    assert "Do NOT weaken or delete" in user_msg
    assert "HEAL1" in user_msg or "kind: fix" in user_msg


def test_healing_records_persisted_in_report_json(config, tmp_repo):
    execute_run(
        config, tmp_repo, "fix the add bug",
        llm=ScriptedLLM(healing_script()), run_id="heal9",
    )
    data = json.loads((config.runs_dir / "heal9" / "report.json").read_text())
    assert data["iterations_used"] == 1
    assert data["healing"][0]["root_cause"]
    assert data["healing"][0]["revalidation_passed"] is True


def test_debugger_crash_fails_run_cleanly(config, tmp_repo):
    config.llm.retries = 0
    script = [
        *bad_first_attempt(),
        {"content": "this is not a debug report"},  # structured output fails
    ]
    report = execute_run(
        config, tmp_repo, "fix the add bug",
        llm=ScriptedLLM(script), run_id="heal10",
    )
    assert report.status == "failed"
    assert "debugging failed" in (report.error or "")
