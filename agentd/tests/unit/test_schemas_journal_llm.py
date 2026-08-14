import json

import pytest
from pydantic import ValidationError

from agentd.journal import Journal
from agentd.llm import LLMError, ScriptedLLM, parse_chat_response
from agentd.schemas import Plan, ValidationReport, extract_json_object

# ── schemas ──────────────────────────────────────────────────────────────────

PLAN = {
    "goal": "g",
    "tasks": [{"id": "T1", "intent": "do it"}],
}


def test_plan_minimal():
    plan = Plan.model_validate(PLAN)
    assert plan.tasks[0].kind == "feature"


def test_plan_duplicate_ids_rejected():
    bad = {"goal": "g", "tasks": [{"id": "T1", "intent": "a"}, {"id": "T1", "intent": "b"}]}
    with pytest.raises(ValidationError):
        Plan.model_validate(bad)


def test_plan_requires_tasks():
    with pytest.raises(ValidationError):
        Plan.model_validate({"goal": "g", "tasks": []})


def test_validation_failure_evidence():
    report = ValidationReport.model_validate(
        {
            "passed": False,
            "checks": [
                {"name": "test[0]", "command": "pytest", "ok": False,
                 "exit_code": 1, "output_tail": "AssertionError: boom"},
                {"name": "lint[0]", "command": "ruff", "ok": True, "exit_code": 0},
            ],
        }
    )
    evidence = report.failure_evidence()
    assert "test[0]" in evidence and "boom" in evidence
    assert "lint[0]" not in evidence


def test_extract_json_plain():
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_fenced():
    text = "Here is the plan:\n```json\n{\"a\": 1}\n```\nDone."
    assert extract_json_object(text) == {"a": 1}


def test_extract_json_prose_wrapped():
    assert extract_json_object('Sure! {"a": {"b": 2}} hope that helps') == {"a": {"b": 2}}


def test_extract_json_failure():
    with pytest.raises(ValueError):
        extract_json_object("no json here")


# ── journal ──────────────────────────────────────────────────────────────────


def test_journal_appends_and_reads(tmp_path):
    journal = Journal(tmp_path / "r1")
    assert journal.append("A", x=1) == 1
    assert journal.append("B", path=tmp_path) == 2  # Path is serialized
    events = journal.read()
    assert [e["type"] for e in events] == ["A", "B"]
    assert events[0]["seq"] == 1
    # every line is valid standalone JSON
    lines = journal.path.read_text().strip().splitlines()
    assert all(json.loads(line) for line in lines)


# ── llm parsing ──────────────────────────────────────────────────────────────


def _response(message):
    return {"choices": [{"message": message}]}


def test_parse_plain_content():
    resp = parse_chat_response(_response({"role": "assistant", "content": "hi"}))
    assert resp.content == "hi"
    assert not resp.wants_tools


def test_parse_tool_calls():
    resp = parse_chat_response(
        _response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "fs_read", "arguments": '{"path": "a.py"}'},
                    }
                ],
            }
        )
    )
    assert resp.wants_tools
    assert resp.tool_calls[0].name == "fs_read"
    assert resp.tool_calls[0].arguments == {"path": "a.py"}
    assert resp.tool_calls[0].parse_error is None


def test_parse_malformed_arguments():
    resp = parse_chat_response(
        _response(
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "fs_read", "arguments": "{not json"}}
                ],
            }
        )
    )
    assert resp.tool_calls[0].parse_error is not None


def test_parse_malformed_response():
    with pytest.raises(LLMError):
        parse_chat_response({"nope": True})


def test_scripted_llm_replays_and_exhausts():
    llm = ScriptedLLM([{"content": "one"}, {"tool_calls": [{"name": "fs_ls"}]}])
    assert llm.chat("planner", []).content == "one"
    second = llm.chat("coder", [])
    assert second.tool_calls[0].name == "fs_ls"
    with pytest.raises(LLMError, match="exhausted"):
        llm.chat("coder", [])
    assert len(llm.calls) == 3
