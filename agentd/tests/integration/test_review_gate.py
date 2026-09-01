"""Mandatory reviewer gate (Phase H2, ADR-022): every green validation must
pass adversarial review before anything is committed — critical findings
block the commit, on agent pipelines and human `commit` alike."""

import json

import pytest

from agentd.llm import ScriptedLLM
from agentd.runner import commit_repo, execute_run
from agentd.schemas import ReviewFinding
from tests.conftest import (
    git,
    happy_path_script,
    review_approve_response,
    review_block_response,
)


def events_for(config, run_id):
    journal = config.runs_dir / run_id / "journal.jsonl"
    return [json.loads(line) for line in journal.read_text().strip().splitlines()]


def script_without_review():
    """The happy path minus the trailing review approval."""
    return happy_path_script()[:-1]


def test_request_changes_blocks_the_commit(config, tmp_repo):
    script = script_without_review() + [review_block_response()]
    report = execute_run(config, tmp_repo, "fix the add bug",
                         llm=ScriptedLLM(script), run_id="rg1")

    assert report.status == "failed"
    assert "review blocked the commit" in report.error
    assert report.commit is None
    # validation itself was green — only the review gate stopped delivery
    assert report.validation and report.validation.passed
    # the structured review report is part of the run report
    assert report.review and report.review.verdict == "request_changes"
    assert report.review.findings[0].category == "security"
    # nothing landed on the run branch
    assert (git(tmp_repo, "rev-parse", "swe/rg1")
            == git(tmp_repo, "rev-parse", "main"))


def test_high_finding_blocks_even_under_approve(config, tmp_repo):
    approve_with_high = {"content": json.dumps({
        "verdict": "approve",
        "summary": "mostly fine but one critical spot",
        "findings": [{"severity": "high", "category": "correctness",
                      "file": "calculator.py", "issue": "wrong operator",
                      "suggestion": "use +"}],
    })}
    script = script_without_review() + [approve_with_high]
    report = execute_run(config, tmp_repo, "fix the add bug",
                         llm=ScriptedLLM(script), run_id="rg2")
    assert report.status == "failed"
    assert "blocking severity" in report.error
    assert report.commit is None


def test_low_findings_do_not_block(config, tmp_repo):
    approve_with_low = {"content": json.dumps({
        "verdict": "approve",
        "summary": "fine",
        "findings": [{"severity": "low", "category": "maintainability",
                      "file": "calculator.py",
                      "issue": "docstring missing", "suggestion": "add one"}],
    })}
    script = script_without_review() + [approve_with_low]
    report = execute_run(config, tmp_repo, "fix the add bug",
                         llm=ScriptedLLM(script), run_id="rg3")
    assert report.status == "completed"
    assert report.commit and report.commit.sha
    assert report.review and len(report.review.findings) == 1
    assert report.review.findings[0].category == "maintainability"


def test_gate_journal_trail(config, tmp_repo):
    execute_run(config, tmp_repo, "fix the add bug",
                llm=ScriptedLLM(happy_path_script()), run_id="rg4")
    events = events_for(config, "rg4")
    states = [e["payload"]["state"] for e in events if e["type"] == "STATE_ENTERED"]
    assert states == ["PLAN", "CODE", "VALIDATE", "REVIEW", "GIT"]
    gate = next(e for e in events if e["type"] == "REVIEW_GATE")
    assert gate["payload"]["verdict"] == "approve"
    assert gate["payload"]["blocked"] is False


def test_gate_disabled_skips_review(config, tmp_repo):
    config.review.enabled = False
    report = execute_run(config, tmp_repo, "fix the add bug",
                         llm=ScriptedLLM(script_without_review()), run_id="rg5")
    assert report.status == "completed"
    assert report.review is None
    states = [e["payload"]["state"] for e in events_for(config, "rg5")
              if e["type"] == "STATE_ENTERED"]
    assert "REVIEW" not in states


def test_commit_pipeline_blocked_by_review(config, tmp_repo):
    (tmp_repo / "calculator.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    head = git(tmp_repo, "rev-parse", "HEAD")
    with pytest.raises(RuntimeError, match="commit blocked by review"):
        commit_repo(config, tmp_repo, message="ship it",
                    llm=ScriptedLLM([review_block_response()]), run_id="rg6")
    assert git(tmp_repo, "rev-parse", "HEAD") == head  # nothing committed


def test_commit_pipeline_passes_review(config, tmp_repo):
    (tmp_repo / "calculator.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    info, report = commit_repo(config, tmp_repo, message="fix add",
                               llm=ScriptedLLM([review_approve_response()]),
                               run_id="rg7")
    assert info.sha and report.passed


def test_finding_category_defaults_to_other():
    finding = ReviewFinding(severity="low", issue="x")
    assert finding.category == "other"


def test_exec_audit_written_for_run(config, tmp_repo):
    """Phase H1 wiring proof: validation commands of a real run land in the
    run directory's execution audit log."""
    execute_run(config, tmp_repo, "fix the add bug",
                llm=ScriptedLLM(happy_path_script()), run_id="rg8")
    audit = config.runs_dir / "rg8" / "exec_audit.jsonl"
    records = [json.loads(line) for line in audit.read_text().splitlines()]
    assert any("import calculator" in r["command"] for r in records)
    assert all(r["mode"] == "host" and r["allowed"] for r in records)
    events = events_for(config, "rg8")
    sandbox_events = [e for e in events if e["type"] == "SANDBOX_MODE"]
    assert sandbox_events and sandbox_events[0]["payload"]["mode"] == "host"
