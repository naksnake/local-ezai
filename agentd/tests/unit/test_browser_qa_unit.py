"""Browser QA — everything testable without a browser: spec validation,
app launcher, report→check conversion, pipeline merge, RCA category,
journal events, and the commit gate."""

import shutil
import sys
from pathlib import Path

import pytest

from agentd.agents import BrowserQAAgent, GitAgent
from agentd.browser_qa import (
    AppLauncher,
    BrowserQASetupError,
    browser_check_results,
    compile_ignore_patterns,
    is_ignored,
    merge_validation,
    skipped_report,
    validate_workflows,
)
from agentd.config import BrowserQAConfig, BrowserWorkflowSpec
from agentd.journal import Journal, NullJournal
from agentd.llm import ScriptedLLM
from agentd.rca import RcaEngine
from agentd.runner import build_registry
from agentd.schemas import (
    BrowserQAReport,
    BrowserStepResult,
    BrowserWorkflowResult,
    CheckResult,
    Plan,
    TaskResult,
    ValidationReport,
)

FIXTURE_APP = Path(__file__).parent.parent / "fixtures" / "customer_app.py"


def wf_result(name="login", passed=True, steps_passed=True, console=(),
              failed_step=None):
    return BrowserWorkflowResult(
        name=name, passed=passed, steps_passed=steps_passed,
        steps=[BrowserStepResult(index=1, action="goto", detail="/", ok=True)],
        failed_step=failed_step, console_errors=list(console),
    )


# ── workflow spec validation ──────────────────────────────────────────────────


def test_valid_spec_accepted():
    specs = [BrowserWorkflowSpec(name="login", steps=[
        {"goto": "/"},
        {"fill": {"selector": "#u", "value": "admin"}},
        {"expect_text": {"selector": "#w", "contains": "Welcome"}},
    ])]
    assert validate_workflows(specs) == []


def test_unknown_action_rejected():
    specs = [BrowserWorkflowSpec(name="x", steps=[{"teleport": "/"}])]
    errors = validate_workflows(specs)
    assert len(errors) == 1
    assert "unknown action 'teleport'" in errors[0]


def test_multi_key_step_rejected():
    specs = [BrowserWorkflowSpec(name="x", steps=[{"goto": "/", "click": "#a"}])]
    errors = validate_workflows(specs)
    assert "single-key mapping" in errors[0]


# ── console-error ignore patterns ────────────────────────────────────────────


def test_ignore_patterns_filter_matching_errors():
    patterns = compile_ignore_patterns([r"favicon\.ico", r"^known noise"])
    assert is_ignored("GET /favicon.ico 404 (Not Found)", patterns)
    assert is_ignored("known noise: analytics blocked", patterns)
    assert not is_ignored("TypeError: x is undefined", patterns)


def test_no_patterns_means_strict():
    assert not is_ignored("anything at all", compile_ignore_patterns([]))


def test_invalid_pattern_fails_loudly():
    import re

    with pytest.raises(re.error):
        compile_ignore_patterns(["([unclosed"])


# ── app launcher (plain HTTP — no playwright) ────────────────────────────────


def _launcher_config(start: str, **app_kwargs) -> BrowserQAConfig:
    return BrowserQAConfig.model_validate(
        {"enabled": True,
         "app": {"start": start, "startup_timeout": 10, **app_kwargs},
         "workflows": [{"name": "w", "steps": [{"goto": "/"}]}]}
    )


def test_app_launcher_starts_and_stops(inplace_ws, tmp_path):
    shutil.copy(FIXTURE_APP, inplace_ws.root / "app.py")
    config = _launcher_config(f"{sys.executable} app.py")
    launcher = AppLauncher(config, inplace_ws)
    url = launcher.start(tmp_path / "logs")
    try:
        import httpx

        response = httpx.get(url + "/", timeout=5)
        assert response.status_code == 200
        assert "Customer Portal" in response.text
        assert "listening on" in launcher.log_tail()
    finally:
        launcher.stop()
    # port was substituted into the URL
    assert url.startswith("http://127.0.0.1:")


def test_app_launcher_dead_app_reports_exit(inplace_ws, tmp_path):
    config = _launcher_config("echo dying && exit 7")
    launcher = AppLauncher(config, inplace_ws)
    with pytest.raises(BrowserQASetupError, match="exited with code 7"):
        launcher.start(tmp_path / "logs")
    assert "dying" in launcher.log_tail()


def test_app_launcher_never_ready_times_out(inplace_ws, tmp_path):
    config = _launcher_config("sleep 60")
    config.app.startup_timeout = 2
    launcher = AppLauncher(config, inplace_ws)
    with pytest.raises(BrowserQASetupError, match="not ready"):
        launcher.start(tmp_path / "logs")
    launcher.stop()


# ── report → validation checks (the three failure rules) ────────────────────


def test_passing_workflow_becomes_ok_check():
    report = BrowserQAReport(enabled=True, passed=True, workflows=[wf_result()],
                             summary="ok")
    checks = browser_check_results(report)
    assert len(checks) == 1
    assert checks[0].name == "browser[login]"
    assert checks[0].ok


def test_console_errors_fail_even_when_steps_pass():
    wf = wf_result(passed=False, steps_passed=True,
                   console=["TypeError: x is undefined"])
    report = BrowserQAReport(enabled=True, passed=False, workflows=[wf],
                             summary="failed")
    checks = browser_check_results(report)
    assert not checks[0].ok
    assert "console errors" in checks[0].output_tail
    assert "TypeError" in checks[0].output_tail


def test_failed_verification_becomes_failing_check():
    wf = wf_result(passed=False, steps_passed=False,
                   failed_step="step 4 (expect_text: ...) — AssertionError")
    report = BrowserQAReport(enabled=True, passed=False, workflows=[wf],
                             summary="failed")
    checks = browser_check_results(report)
    assert not checks[0].ok
    assert "failed at step 4" in checks[0].output_tail


def test_setup_error_becomes_failing_check():
    report = BrowserQAReport(enabled=True, passed=False,
                             error="app not ready", summary="setup failed")
    checks = browser_check_results(report)
    assert checks[0].name == "browser[setup]"
    assert not checks[0].ok


def test_disabled_and_skipped_produce_no_checks():
    disabled = BrowserQAReport(enabled=False, passed=True)
    assert browser_check_results(disabled) == []
    assert browser_check_results(skipped_report("commands failed")) == []
    assert skipped_report("commands failed").passed is False  # never "succeeds"


# ── pipeline merge ────────────────────────────────────────────────────────────


def _commands_report(passed=True):
    return ValidationReport(
        passed=passed,
        checks=[CheckResult(name="test[0]", command="pytest", ok=passed,
                            exit_code=0 if passed else 1)],
        summary="all 1 check(s) passed" if passed else "failed: test[0]",
    )


def test_merge_pass_pass():
    browser = BrowserQAReport(enabled=True, passed=True, workflows=[wf_result()],
                              summary="all 1 workflow(s) passed")
    merged = merge_validation(_commands_report(True), browser)
    assert merged.passed
    assert [c.name for c in merged.checks] == ["test[0]", "browser[login]"]
    assert merged.browser is browser


def test_merge_browser_failure_fails_validation():
    wf = wf_result(passed=False, steps_passed=True, console=["boom"])
    browser = BrowserQAReport(enabled=True, passed=False, workflows=[wf],
                              summary="failed workflows: login")
    merged = merge_validation(_commands_report(True), browser)
    assert not merged.passed
    assert "browser QA" in merged.summary


def test_merge_skipped_browser_keeps_failure():
    merged = merge_validation(_commands_report(False),
                              skipped_report("command checks failed"))
    assert not merged.passed
    assert merged.browser.skipped


# ── RCA categorization ────────────────────────────────────────────────────────


def test_rca_categorizes_browser_checks():
    report = ValidationReport(passed=False, checks=[CheckResult(
        name="browser[create-customer]", command="browser workflow",
        ok=False, exit_code=1,
        # content quotes a python-looking error — stage categorization must win
        output_tail="failed at step 4 — AssertionError: expected 'Ada'",
    )])
    analyses = RcaEngine().analyze(report)
    assert analyses[0].category == "browser"
    assert "application code" in analyses[0].suggested_strategy


# ── Browser QA Agent (stub harness) ──────────────────────────────────────────


class StubHarness:
    next_report: BrowserQAReport | None = None

    def __init__(self, cfg, artifacts_dir):
        self.artifacts_dir = artifacts_dir

    def run(self, workspace):
        return StubHarness.next_report


def make_browser_agent(config, journal=None):
    journal = journal or NullJournal()
    agent = BrowserQAAgent(config, ScriptedLLM([]), build_registry(config, journal),
                           journal)
    agent.harness_factory = StubHarness
    return agent


def test_agent_not_configured_is_vacuous_pass(config, inplace_ws):
    agent = make_browser_agent(config)
    report = agent.run(inplace_ws)
    assert report.enabled is False
    assert report.passed is True


def test_agent_journals_workflow_events(config, inplace_ws, tmp_path):
    config.browser_qa = BrowserQAConfig.model_validate(
        {"enabled": True, "workflows": [{"name": "login", "steps": [{"goto": "/"}]}]}
    )
    journal = Journal(tmp_path / "runs" / "bqa")
    StubHarness.next_report = BrowserQAReport(
        enabled=True, passed=False,
        workflows=[wf_result(passed=False, steps_passed=True, console=["boom"])],
        summary="failed workflows: login",
    )
    agent = make_browser_agent(config, journal)
    report = agent.run(inplace_ws)
    assert not report.passed
    types = [e["type"] for e in journal.read()]
    assert "BROWSER_QA_STARTED" in types
    assert "BROWSER_WORKFLOW" in types
    assert "BROWSER_QA" in types
    wf_event = next(e for e in journal.read() if e["type"] == "BROWSER_WORKFLOW")
    assert wf_event["payload"]["console_errors"] == 1


# ── the commit gate (git blocked until browser QA succeeds) ─────────────────


def test_git_agent_refuses_failing_validation(config, inplace_ws, tmp_path):
    (inplace_ws.root / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
    journal = Journal(tmp_path / "runs" / "gate")
    agent = GitAgent(config, ScriptedLLM([]), build_registry(config, journal), journal)
    plan = Plan.model_validate({"goal": "g", "tasks": [{"id": "T1", "intent": "x"}]})
    results = [TaskResult(task_id="T1", status="done", summary="s")]
    failing = merge_validation(
        _commands_report(True),
        BrowserQAReport(enabled=True, passed=False,
                        workflows=[wf_result(passed=False, console=["boom"])],
                        summary="failed workflows: login"),
    )
    from tests.conftest import git

    head_before = git(inplace_ws.root, "rev-parse", "HEAD")
    with pytest.raises(RuntimeError, match="commit blocked"):
        agent.run(inplace_ws, plan, results, failing, "run-gate")
    # nothing was staged or committed
    assert git(inplace_ws.root, "rev-parse", "HEAD") == head_before
    assert git(inplace_ws.root, "diff", "--cached", "--name-only") == ""
    blocked = [e for e in journal.read() if e["type"] == "COMMIT_BLOCKED"]
    assert blocked and "browser QA" in blocked[0]["payload"]["reason"]
