"""Browser QA pipeline integration WITHOUT a real browser: a stub harness
returns scripted BrowserQAReports, proving the graph-level rules —

- browser results merge into the validation verdict,
- browser failures drive the DEBUG→FIX→REVALIDATE loop,
- git commits are blocked until browser QA succeeds,
- the stage is skipped (but still not "succeeded") when command checks fail.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

from agentd.agents.browser_qa import BrowserQAAgent
from agentd.llm import ScriptedLLM
from agentd.runner import execute_run
from agentd.schemas import (
    BrowserQAReport,
    BrowserStepResult,
    BrowserWorkflowResult,
)
from tests.conftest import (
    debug_response,
    git,
    git_commit_all,
    review_approve_response,
)


def browser_report(passed: bool, console: list[str] | None = None):
    wf = BrowserWorkflowResult(
        name="login",
        passed=passed,
        steps_passed=True,
        steps=[BrowserStepResult(index=1, action="goto", detail="/", ok=True)],
        console_errors=console or ([] if passed else ["ReferenceError: boom"]),
    )
    return BrowserQAReport(
        enabled=True, passed=passed, workflows=[wf],
        summary="all 1 workflow(s) passed" if passed else "failed workflows: login",
    )


class QueueStub:
    """Stub harness factory: pops one report per validation run."""

    queue: list[BrowserQAReport] = []
    calls: int = 0

    def __init__(self, cfg, artifacts_dir):
        pass

    def run(self, workspace):
        QueueStub.calls += 1
        return QueueStub.queue.pop(0)


@pytest.fixture(autouse=True)
def _reset_stub(monkeypatch):
    QueueStub.queue = []
    QueueStub.calls = 0
    monkeypatch.setattr(BrowserQAAgent, "harness_factory", QueueStub)


@pytest.fixture
def ui_repo(tmp_path: Path) -> Path:
    """Repo with passing command checks and browser QA enabled."""
    repo = tmp_path / "ui-repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    (repo / "app.py").write_text("VERSION = 1\n", encoding="utf-8")
    (repo / ".agentd.yaml").write_text(yaml.safe_dump({
        "validation": {"commands": {"test": ["python3 -m py_compile app.py"]}},
        "browser_qa": {
            "enabled": True,
            "app": {"start": "python3 app.py"},
            "workflows": [{"name": "login", "steps": [{"goto": "/"}]}],
        },
    }), encoding="utf-8")
    git_commit_all(repo, "initial commit")
    return repo


PLAN = {
    "content": (
        '{"goal": "fix the login page", "tasks": [{"id": "T1", '
        '"intent": "adjust the app", "files_hint": ["app.py"], '
        '"check": "browser workflows pass", "kind": "fix"}], '
        '"assumptions": [], "risks": []}'
    )
}


def coder_touch(content_note: str) -> list[dict]:
    return [
        {"tool_calls": [{"name": "fs_write",
                         "arguments": {"path": "NOTES.md",
                                       "content": content_note}}]},
        {"content": "made a change"},
    ]


def test_browser_failure_heals_then_commits(config, ui_repo):
    QueueStub.queue = [browser_report(False), browser_report(True)]
    script = [
        PLAN,
        *coder_touch("first attempt"),
        debug_response(category="browser",
                       root_cause="login page script references an undefined "
                                  "variable, raising a console error on load",
                       approach="define the variable before use",
                       files=["app.py"]),
        {"tool_calls": [{"name": "fs_edit",
                         "arguments": {"path": "app.py",
                                       "old_string": "VERSION = 1",
                                       "new_string": "VERSION = 2"}}]},
        {"content": "applied the diagnosed fix"},
        review_approve_response(),
    ]
    report = execute_run(config, ui_repo, "fix the login page",
                         llm=ScriptedLLM(script), run_id="ui1")

    assert report.status == "completed"
    assert report.iterations_used == 1
    assert QueueStub.calls == 2  # VALIDATE + REVALIDATE
    assert report.validation.browser and report.validation.browser.passed
    assert report.healing[0].categories == ["browser"]
    assert "browser[login]" in report.healing[0].error_signature
    # delivered
    assert report.commit and len(report.commit.sha) == 40
    assert "VERSION = 2" in git(ui_repo, "show", "swe/ui1:app.py")


def test_commit_blocked_while_browser_qa_fails(config, ui_repo):
    config.limits.max_heal_iterations = 0
    QueueStub.queue = [browser_report(False)]
    script = [PLAN, *coder_touch("attempt")]
    report = execute_run(config, ui_repo, "fix the login page",
                         llm=ScriptedLLM(script), run_id="ui2")

    assert report.status == "failed"
    assert report.commit is None
    # the run branch has NO new commit — still at the repo's initial commit
    assert (git(ui_repo, "rev-parse", "swe/ui2")
            == git(ui_repo, "rev-parse", "main"))
    assert not report.validation.passed
    failing = [c for c in report.validation.checks if not c.ok]
    assert [c.name for c in failing] == ["browser[login]"]
    assert "ReferenceError" in failing[0].output_tail


def test_browser_skipped_when_commands_fail_and_never_succeeds(config, tmp_path):
    repo = tmp_path / "broken-repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    (repo / "app.py").write_text("VERSION = 1\n", encoding="utf-8")
    (repo / ".agentd.yaml").write_text(yaml.safe_dump({
        "validation": {"commands": {"test": ["false"]}},  # always fails
        "browser_qa": {
            "enabled": True,
            "app": {"start": "python3 app.py"},
            "workflows": [{"name": "login", "steps": [{"goto": "/"}]}],
        },
    }), encoding="utf-8")
    git_commit_all(repo, "initial commit")

    config.limits.max_heal_iterations = 0
    script = [PLAN, *coder_touch("attempt")]
    report = execute_run(config, repo, "fix it",
                         llm=ScriptedLLM(script), run_id="ui3")

    assert report.status == "failed"
    assert QueueStub.calls == 0  # app never launched on broken code
    browser = report.validation.browser
    assert browser and browser.skipped and browser.passed is False
    assert report.commit is None


def test_browser_events_journaled_through_pipeline(config, ui_repo):
    import json

    QueueStub.queue = [browser_report(True)]
    script = [PLAN, *coder_touch("only attempt"), review_approve_response()]
    report = execute_run(config, ui_repo, "fix the login page",
                         llm=ScriptedLLM(script), run_id="ui4")
    assert report.status == "completed"
    journal = config.runs_dir / "ui4" / "journal.jsonl"
    events = [json.loads(line) for line in journal.read_text().splitlines() if line]
    types = [e["type"] for e in events]
    assert "BROWSER_QA_STARTED" in types
    assert "BROWSER_WORKFLOW" in types
    assert "BROWSER_QA" in types
