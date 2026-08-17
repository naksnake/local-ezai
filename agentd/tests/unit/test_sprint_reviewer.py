"""Sprint spec parsing + Reviewer Agent."""

import json

import pytest

from agentd.agents.reviewer import ReviewerAgent
from agentd.journal import Journal, NullJournal
from agentd.llm import ScriptedLLM
from agentd.memory import KIND_STYLE, MemoryStore
from agentd.runner import build_registry
from agentd.sprint import load_sprint_tasks, parse_sprint_tasks

# ── sprint spec parsing ───────────────────────────────────────────────────────


def test_checklist_items_parsed_unchecked_only():
    spec = """# Sprint 28
Some intro text.

## Tasks
- [x] already done task
- [ ] implement login endpoint
- [ ] add customer CRUD
Notes: ship by friday.
"""
    assert parse_sprint_tasks(spec) == [
        "implement login endpoint",
        "add customer CRUD",
    ]


def test_bullets_fallback():
    spec = "goals:\n- implement login\n* add logout\n"
    assert parse_sprint_tasks(spec) == ["implement login", "add logout"]


def test_numbered_fallback():
    spec = "1. first task\n2) second task\n"
    assert parse_sprint_tasks(spec) == ["first task", "second task"]


def test_checklist_takes_priority_over_bullets():
    spec = "- plain bullet ignored when checklists exist\n- [ ] real task\n"
    assert parse_sprint_tasks(spec) == ["real task"]


def test_empty_spec_raises(tmp_path):
    spec_file = tmp_path / "empty.md"
    spec_file.write_text("# nothing here\njust prose\n")
    with pytest.raises(ValueError, match="no tasks found"):
        load_sprint_tasks(spec_file)


def test_load_sprint_tasks_reads_file(tmp_path):
    spec_file = tmp_path / "sprint28.md"
    spec_file.write_text("- [ ] task one\n- [ ] task two\n")
    assert load_sprint_tasks(spec_file) == ["task one", "task two"]


# ── Reviewer Agent ────────────────────────────────────────────────────────────

REVIEW_JSON = json.dumps({
    "verdict": "request_changes",
    "summary": "the change weakens a test",
    "findings": [
        {"severity": "high", "file": "test_calculator.py", "line": 4,
         "issue": "assertion deleted to make the suite pass",
         "suggestion": "restore the assertion and fix add() instead"},
    ],
})

APPROVE_JSON = json.dumps({"verdict": "approve", "summary": "clean", "findings": []})

DIFF = """--- a/test_calculator.py
+++ b/test_calculator.py
@@ -1,4 +1,3 @@
 import calculator
 def test_add():
-    assert calculator.add(2, 3) == 5
+    pass
"""


def make_reviewer(config, script, journal=None, memory=None):
    journal = journal or NullJournal()
    llm = ScriptedLLM(script)
    return ReviewerAgent(config, llm, build_registry(config, journal),
                         journal, memory=memory), llm


def test_reviewer_returns_structured_report(config, inplace_ws):
    reviewer, llm = make_reviewer(config, [{"content": REVIEW_JSON}])
    report = reviewer.run(DIFF, inplace_ws)
    assert report.verdict == "request_changes"
    assert report.findings[0].severity == "high"
    assert report.findings[0].line == 4
    # the diff reached the model
    assert "assert calculator.add(2, 3) == 5" in llm.calls[0]["messages"][1]["content"]


def test_reviewer_empty_diff_approves_without_llm(config, inplace_ws):
    reviewer, llm = make_reviewer(config, [])
    report = reviewer.run("   ", inplace_ws)
    assert report.verdict == "approve"
    assert llm.calls == []


def test_reviewer_injects_memory_styles(config, tmp_path, inplace_ws):
    store = MemoryStore(tmp_path / ".agent")
    store.record(KIND_STYLE, "no bare pass", "test bodies must assert something")
    reviewer, llm = make_reviewer(config, [{"content": REVIEW_JSON}], memory=store)
    reviewer.run(DIFF, inplace_ws)
    user_msg = llm.calls[0]["messages"][1]["content"]
    assert "no bare pass" in user_msg
    assert "flag violations" in user_msg
    store.close()


def test_reviewer_cannot_write(config, inplace_ws):
    reviewer, _ = make_reviewer(config, [
        {"tool_calls": [{"name": "fs_write",
                         "arguments": {"path": "hack.py", "content": "x"}}]},
        {"content": APPROVE_JSON},
    ])
    reviewer.run(DIFF, inplace_ws)
    assert not (inplace_ws.root / "hack.py").exists()


def test_reviewer_journals_verdict(config, inplace_ws, tmp_path):
    journal = Journal(tmp_path / "runs" / "rev")
    reviewer, _ = make_reviewer(config, [{"content": REVIEW_JSON}], journal=journal)
    reviewer.run(DIFF, inplace_ws)
    events = [e for e in journal.read() if e["type"] == "REVIEW"]
    assert events and events[0]["payload"]["verdict"] == "request_changes"
    assert events[0]["payload"]["high"] == 1
