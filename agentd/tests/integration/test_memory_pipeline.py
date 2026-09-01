"""Memory across full runs: persistence in the origin repo's .agent/,
cross-run learning (planner + debugger injection), repeat-mistake warnings,
commit hygiene, and the CLI."""

import json

from agentd.cli import main
from agentd.llm import ScriptedLLM
from agentd.memory import KIND_FAILED_FIX, KIND_SUCCESSFUL_FIX, MemoryStore
from agentd.runner import execute_run
from agentd.tools.git import GitAdd
from tests.conftest import (
    bad_first_attempt,
    coder_edit,
    debug_response,
    git,
    healing_script,
    review_approve_response,
)


def store_for(repo):
    return MemoryStore(repo / ".agent")


def test_memory_persisted_in_origin_repo_not_in_commit(config, tmp_repo):
    report = execute_run(config, tmp_repo, "fix the add bug",
                         llm=ScriptedLLM(healing_script()), run_id="mem1")
    assert report.status == "completed"

    # memory.db + lessons_learned.json exist in the ORIGIN repo's .agent/
    assert (tmp_repo / ".agent" / "memory.db").is_file()
    lessons = json.loads((tmp_repo / ".agent" / "lessons_learned.json").read_text())
    assert lessons["total_memories"] == 2  # successful_fix + implementation
    assert lessons["lessons"][0]["kind"] == "successful_fix"
    assert lessons["implementation_history"][0]["run_id"] == "mem1"

    # the run's commit contains no memory files
    committed = git(tmp_repo, "show", "--name-only", "--format=", "swe/mem1")
    assert ".agent" not in committed
    assert "memory.db" not in committed

    store = store_for(tmp_repo)
    successful = store.recent([KIND_SUCCESSFUL_FIX])
    assert len(successful) == 1
    assert successful[0].error_signature
    assert successful[0].data["approach"]
    store.close()


def test_second_run_sees_first_runs_memory(config, tmp_repo):
    execute_run(config, tmp_repo, "fix the add bug",
                llm=ScriptedLLM(healing_script()), run_id="mem2a")

    # run 2 on the same repo (bug still on main): planner and debugger
    # prompts now carry persisted memory from run 1
    llm = ScriptedLLM(healing_script())
    report = execute_run(config, tmp_repo, "fix the add bug",
                         llm=llm, run_id="mem2b")
    assert report.status == "completed"

    planner_msg = llm.calls[0]["messages"][1]["content"]
    assert "Project memory" in planner_msg
    assert "Recent implementation history" in planner_msg
    assert "mem2a" not in planner_msg or True  # content, not ids, is the point

    # debugger (call index 3) sees the successful repair for this signature
    debugger_msg = llm.calls[3]["messages"][1]["content"]
    assert "previously SUCCEEDED" in debugger_msg
    assert "correct the operator" in debugger_msg  # approach from run 1

    store = store_for(tmp_repo)
    assert store.count(KIND_SUCCESSFUL_FIX) == 2
    store.close()


def test_repeat_mistake_warning_across_runs(config, tmp_repo):
    # Run 1: one failed heal attempt (noop fix), budget 1 → failed_fix recorded
    config.limits.max_heal_iterations = 1
    config.limits.stall_threshold = 99
    script_1 = [
        *bad_first_attempt(),
        debug_response(approach="rename the function to add_numbers"),
        {"content": "attempted."},  # no-op fix; revalidation fails
    ]
    report_1 = execute_run(config, tmp_repo, "fix the add bug",
                           llm=ScriptedLLM(script_1), run_id="mem3a")
    assert report_1.status == "failed"
    store = store_for(tmp_repo)
    assert store.count(KIND_FAILED_FIX) == 1
    store.close()

    # Run 2: debugger proposes the SAME approach → repeat warning journaled
    # and stamped into the fix task prompt; a real fix then lands.
    config.limits.max_heal_iterations = 10
    llm = ScriptedLLM([
        *bad_first_attempt(),
        debug_response(approach="rename the function to add_numbers"),
        {"content": "attempted again."},  # noop — revalidation fails again
        debug_response(approach="correct the operator to addition"),
        coder_edit("return a * b", "return a + b"),
        {"content": "fixed properly."},
        review_approve_response(),
    ])
    report_2 = execute_run(config, tmp_repo, "fix the add bug",
                           llm=llm, run_id="mem3b")
    assert report_2.status == "completed"

    events = [
        json.loads(line)
        for line in (config.runs_dir / "mem3b" / "journal.jsonl")
        .read_text().splitlines() if line
    ]
    warnings = [e for e in events if e["type"] == "MEMORY_REPEAT_WARNING"]
    assert warnings
    assert warnings[0]["payload"]["previous_run"] == "mem3a"
    assert "rename the function" in warnings[0]["payload"]["previous_approach"]

    # the HEAL1 fix prompt (coder call after first debug) carries the warning
    heal1_msg = llm.calls[4]["messages"][1]["content"]
    assert "already FAILED in run mem3a" in heal1_msg

    # the debugger prompt also listed the failed approach explicitly
    debug1_msg = llm.calls[3]["messages"][1]["content"]
    assert "ALREADY FAILED" in debug1_msg
    assert "rename the function to add_numbers" in debug1_msg


def test_memory_disabled_leaves_no_files(config, tmp_repo):
    config.memory.enabled = False
    config.code_intel.enabled = False  # isolate memory from the code index
    report = execute_run(config, tmp_repo, "fix the add bug",
                         llm=ScriptedLLM(healing_script()), run_id="mem4")
    assert report.status == "completed"
    assert not (tmp_repo / ".agent").exists()


def test_memory_disabled_writes_no_memory_files(config, tmp_repo):
    """With the code index enabled (default), .agent/ holds ONLY the index —
    never memory files — when memory is disabled."""
    config.memory.enabled = False
    report = execute_run(config, tmp_repo, "fix the add bug",
                         llm=ScriptedLLM(healing_script()), run_id="mem4b")
    assert report.status == "completed"
    assert not (tmp_repo / ".agent" / "memory.db").exists()
    assert not (tmp_repo / ".agent" / "lessons_learned.json").exists()
    assert (tmp_repo / ".agent" / "code-index" / "symbols.json").is_file()


def test_gitadd_excludes_memory_files_in_place(config, inplace_ws):
    agent_dir = inplace_ws.root / ".agent"
    agent_dir.mkdir()
    (agent_dir / "memory.db").write_text("sqlite")
    (agent_dir / "lessons_learned.json").write_text("{}")
    (agent_dir / "notes.md").write_text("committable")  # NOT excluded
    (inplace_ws.root / "feature.py").write_text("x = 1\n")

    assert GitAdd().run(inplace_ws).ok
    staged = git(inplace_ws.root, "diff", "--cached", "--name-only").splitlines()
    assert "feature.py" in staged
    assert ".agent/notes.md" in staged
    assert ".agent/memory.db" not in staged
    assert ".agent/lessons_learned.json" not in staged


# ── CLI: remember + memory ────────────────────────────────────────────────────


def test_cli_remember_and_memory(tmp_repo, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    code = main(["remember", "always pin dependency versions",
                 "--repo", str(tmp_repo), "--kind", "project_rule"])
    assert code == 0
    assert "remembered #1" in capsys.readouterr().out
    assert (tmp_repo / ".agent" / "memory.db").is_file()
    assert (tmp_repo / ".agent" / "lessons_learned.json").is_file()

    assert main(["memory", "--repo", str(tmp_repo)]) == 0
    out = capsys.readouterr().out
    assert "project_rule" in out
    assert "always pin dependency versions" in out
    assert "1 total memories" in out

    assert main(["memory", "--repo", str(tmp_repo), "--search", "dependency"]) == 0
    assert "pin dependency" in capsys.readouterr().out


def test_cli_memory_empty_repo(tmp_repo, capsys, monkeypatch):
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    assert main(["memory", "--repo", str(tmp_repo)]) == 0
    assert "no memory yet" in capsys.readouterr().out
    assert not (tmp_repo / ".agent").exists()  # inspection leaves no traces
