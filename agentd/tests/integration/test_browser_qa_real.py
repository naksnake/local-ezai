"""Browser QA with a REAL Playwright Chromium against the fixture customer
CRUD app: launch, the four example workflows (login / create / update /
delete customer), console-error detection, failure screenshots, and the
full self-healing pipeline fixing a UI bug end-to-end.

Skipped automatically when playwright or a usable Chromium is unavailable.
The .agentd.yaml used here is the shipped example file verbatim
(agentd/examples/browser-qa.customer-crud.yaml), so these tests also keep
the documentation honest.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytest.importorskip("playwright.sync_api", reason="playwright not installed")

from agentd.agents import BrowserQAAgent  # noqa: E402
from agentd.config import BrowserQAConfig  # noqa: E402
from agentd.journal import Journal  # noqa: E402
from agentd.llm import ScriptedLLM  # noqa: E402
from agentd.runner import build_registry, execute_run  # noqa: E402
from agentd.workspace import Workspace  # noqa: E402
from tests.conftest import debug_response, git, git_commit_all  # noqa: E402

FIXTURE_APP = Path(__file__).parent.parent / "fixtures" / "customer_app.py"
EXAMPLE_YAML = (Path(__file__).parents[2] / "examples"
                / "browser-qa.customer-crud.yaml")


@pytest.fixture(scope="session")
def chromium_ok():
    """Skip the module when no Chromium can be launched (any path)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
            browser.close()
            return True
        except Exception:
            fallback = Path(
                os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/nonexistent")
            ) / "chromium"
            if fallback.exists():
                try:
                    browser = p.chromium.launch(
                        headless=True, executable_path=str(fallback))
                    browser.close()
                    return True
                except Exception:
                    pass
    pytest.skip("no usable chromium for browser QA tests")


def make_repo(tmp_path: Path, yaml_text: str, plant_bug: bool = False) -> Path:
    repo = tmp_path / "webapp"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    shutil.copy(FIXTURE_APP, repo / "app.py")
    if plant_bug:
        source = (repo / "app.py").read_text()
        assert "CUSTOMERS[NEXT_ID[0]] = name" in source
        (repo / "app.py").write_text(
            source.replace("CUSTOMERS[NEXT_ID[0]] = name",
                           'CUSTOMERS[NEXT_ID[0]] = ""')
        )
    (repo / ".agentd.yaml").write_text(yaml_text, encoding="utf-8")
    git_commit_all(repo, "initial commit")
    return repo


def run_agent(config, repo: Path, run_dir: Path):
    config.browser_qa = BrowserQAConfig.model_validate(
        yaml.safe_load((repo / ".agentd.yaml").read_text())["browser_qa"]
    )
    ws = Workspace(root=repo, repo_path=repo, branch="main", mode="in-place")
    journal = Journal(run_dir)
    agent = BrowserQAAgent(config, ScriptedLLM([]),
                           build_registry(config, journal), journal)
    return agent.run(ws), journal


def test_example_crud_workflows_pass(config, tmp_path, chromium_ok):
    repo = make_repo(tmp_path, EXAMPLE_YAML.read_text())
    report, journal = run_agent(config, repo, tmp_path / "run")

    assert report.error is None
    assert report.passed, [
        (w.name, w.failed_step, w.console_errors) for w in report.workflows
    ]
    assert [w.name for w in report.workflows] == [
        "login", "create-customer", "update-customer", "delete-customer",
    ]
    for wf in report.workflows:
        assert wf.passed and wf.steps_passed
        assert not wf.console_errors and not wf.page_errors
    # screenshots captured on explicit steps, files exist on disk
    shots = [s for w in report.workflows for s in w.screenshots]
    assert len(shots) == 4
    assert all(Path(s).is_file() and Path(s).stat().st_size > 0 for s in shots)
    assert report.app_url.startswith("http://127.0.0.1:")


def test_console_errors_fail_validation(config, tmp_path, chromium_ok):
    spec = yaml.safe_load(EXAMPLE_YAML.read_text())
    spec["browser_qa"]["app"]["start"] = "INJECT_CONSOLE_ERROR=1 python3 app.py"
    spec["browser_qa"]["workflows"] = [spec["browser_qa"]["workflows"][0]]  # login
    repo = make_repo(tmp_path, yaml.safe_dump(spec))
    report, _ = run_agent(config, repo, tmp_path / "run")

    assert not report.passed
    wf = report.workflows[0]
    assert wf.steps_passed          # every step worked...
    assert not wf.passed            # ...but console errors fail the workflow
    assert any("fixture console error" in e for e in wf.console_errors)


def test_ignore_console_patterns_escape_hatch(config, tmp_path, chromium_ok):
    spec = yaml.safe_load(EXAMPLE_YAML.read_text())
    spec["browser_qa"]["app"]["start"] = "INJECT_CONSOLE_ERROR=1 python3 app.py"
    spec["browser_qa"]["workflows"] = [spec["browser_qa"]["workflows"][0]]
    spec["browser_qa"]["ignore_console_patterns"] = ["fixture console error"]
    repo = make_repo(tmp_path, yaml.safe_dump(spec))
    report, _ = run_agent(config, repo, tmp_path / "run")
    assert report.passed
    assert report.workflows[0].console_errors == []


def test_verification_failure_captures_screenshot(config, tmp_path, chromium_ok):
    spec = yaml.safe_load(EXAMPLE_YAML.read_text())
    spec["browser_qa"]["workflows"] = [
        {"name": "ghost-customer", "steps": [
            {"goto": "/customers"},
            {"expect_text": {"selector": "#customer-list",
                             "contains": "Nonexistent Person"}},
        ]},
    ]
    spec["browser_qa"]["step_timeout"] = 3  # keep the failing assert quick
    repo = make_repo(tmp_path, yaml.safe_dump(spec))
    report, _ = run_agent(config, repo, tmp_path / "run")

    assert not report.passed
    wf = report.workflows[0]
    assert not wf.steps_passed
    assert wf.failed_step and "expect_text" in wf.failed_step
    assert wf.screenshots and wf.screenshots[0].endswith("failure.png")
    assert Path(wf.screenshots[0]).is_file()


def test_self_healing_fixes_ui_bug_and_gates_commit(config, tmp_path, chromium_ok):
    """The crown test: a planted UI bug (created customers get an empty
    name) is caught by real browser QA, diagnosed, fixed, revalidated in a
    fresh app launch, and only then committed."""
    spec = yaml.safe_load(EXAMPLE_YAML.read_text())
    spec["browser_qa"]["workflows"] = spec["browser_qa"]["workflows"][:2]  # login+create
    spec["browser_qa"]["step_timeout"] = 5
    repo = make_repo(tmp_path, yaml.safe_dump(spec), plant_bug=True)

    plan = {"content": json.dumps({
        "goal": "customers are created with an empty name; fix creation",
        "tasks": [{"id": "T1", "intent": "investigate customer creation",
                   "files_hint": ["app.py"],
                   "check": "browser workflow create-customer passes",
                   "kind": "fix"}],
        "assumptions": [], "risks": [],
    })}
    script = [
        plan,
        # T1: an incorrect first attempt (notes only — bug remains)
        {"tool_calls": [{"name": "fs_write",
                         "arguments": {"path": "NOTES.md",
                                       "content": "creation looks off"}}]},
        {"content": "documented findings"},
        # DEBUG: correct root cause
        debug_response(
            category="browser",
            root_cause="the create handler stores an empty string instead of "
                       "the submitted name in app.py",
            approach="store the submitted name when creating a customer",
            steps=["edit app.py: CUSTOMERS[NEXT_ID[0]] must be assigned "
                   "'name', not the empty string"],
            files=["app.py"],
        ),
        # FIX (HEAL1): the real repair
        {"tool_calls": [{"name": "fs_edit",
                         "arguments": {"path": "app.py",
                                       "old_string": 'CUSTOMERS[NEXT_ID[0]] = ""',
                                       "new_string": "CUSTOMERS[NEXT_ID[0]] = name"}}]},
        {"content": "restored the name assignment"},
    ]
    report = execute_run(config, repo, "fix customer creation",
                         llm=ScriptedLLM(script), run_id="uibug1")

    assert report.status == "completed", report.error
    assert report.iterations_used == 1
    assert report.healing[0].categories == ["browser"]
    assert report.healing[0].revalidation_passed is True
    assert report.validation.browser and report.validation.browser.passed
    # commit happened only after browser QA succeeded, on the run branch
    assert report.commit and len(report.commit.sha) == 40
    assert "CUSTOMERS[NEXT_ID[0]] = name" in git(repo, "show", "swe/uibug1:app.py")
    # user checkout untouched, bug still present on main
    assert 'CUSTOMERS[NEXT_ID[0]] = ""' in (repo / "app.py").read_text()
