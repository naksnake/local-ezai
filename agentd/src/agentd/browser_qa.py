"""Browser QA engine — Playwright-driven, declarative, deterministic.

The engine (ADR-016):

1. **Launches the application** under test from the workspace
   (:class:`AppLauncher`): free port allocation, ``{port}`` substitution,
   HTTP readiness polling, log capture, process-group teardown.
2. **Runs real user workflows** declared in the repo's ``.agentd.yaml``
   (login, create/update/delete customer, ...) as ordered steps with a
   small, explicit vocabulary (see :data:`STEP_ACTIONS`).
3. **Validates pages** via ``expect_*`` steps (auto-retrying Playwright
   assertions) and **detects console errors** (``console.error`` + uncaught
   page errors) on every page of every workflow.
4. **Captures screenshots** on explicit ``screenshot`` steps and
   automatically on failure, into the run's artifacts directory.
5. **Generates a validation report** (:class:`~agentd.schemas.BrowserQAReport`)
   that merges into the run's ValidationReport — a workflow counts as
   failed on step failure, verification failure, *or* any console error.

Like the Validation Agent, this is a harness: no LLM decides pass/fail.
Playwright is imported lazily so the rest of agentd works without the
``browser`` extra installed; a configured-but-unavailable browser stage is
a validation FAILURE (fail-closed — commits stay blocked), never a skip.

Step vocabulary (one action key per step):

    - goto: "/customers"
    - click: "#save-btn"
    - fill: {selector: "#name", value: "Ada Lovelace"}
    - select: {selector: "#country", value: "GB"}
    - expect_text: {selector: "#customer-list", contains: "Ada Lovelace"}
    - expect_no_text: {selector: "#customer-list", contains: "Deleted Person"}
    - expect_visible: "#welcome"
    - expect_url: {contains: "/customers"}
    - expect_title: {contains: "Customers"}
    - wait_for: "#spinner-done"
    - screenshot: "after-create"
"""

from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import time
from pathlib import Path

import httpx

from agentd.config import BrowserQAConfig, BrowserWorkflowSpec
from agentd.logging_setup import get_logger
from agentd.schemas import (
    BrowserQAReport,
    BrowserStepResult,
    BrowserWorkflowResult,
    CheckResult,
    ValidationReport,
)
from agentd.workspace import Workspace

log = get_logger("browser_qa")

STEP_ACTIONS = frozenset(
    {"goto", "click", "fill", "select", "expect_text", "expect_no_text",
     "expect_visible", "expect_url", "expect_title", "wait_for", "screenshot"}
)

_APP_LOG_TAIL = 4_000


class BrowserQASetupError(RuntimeError):
    """The stage could not run at all (browser missing, app won't start)."""


# ── console-error filtering ──────────────────────────────────────────────────


def compile_ignore_patterns(patterns: list[str]) -> list[re.Pattern[str]]:
    """Compile ``ignore_console_patterns`` (invalid regexes fail loudly)."""
    return [re.compile(p) for p in patterns]


def is_ignored(text: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(p.search(text) for p in patterns)


# ── spec validation (deterministic, before anything launches) ────────────────


def validate_workflows(specs: list[BrowserWorkflowSpec]) -> list[str]:
    """Return a list of human-readable spec errors (empty when valid)."""
    errors: list[str] = []
    for spec in specs:
        for index, step in enumerate(spec.steps, start=1):
            if not isinstance(step, dict) or len(step) != 1:
                errors.append(
                    f"workflow '{spec.name}' step {index}: each step must be "
                    f"a single-key mapping, got {step!r}"
                )
                continue
            action = next(iter(step))
            if action not in STEP_ACTIONS:
                errors.append(
                    f"workflow '{spec.name}' step {index}: unknown action "
                    f"'{action}' (known: {', '.join(sorted(STEP_ACTIONS))})"
                )
    return errors


# ── application lifecycle ─────────────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class AppLauncher:
    """Starts/stops the application under test and reports readiness."""

    def __init__(self, config: BrowserQAConfig, workspace: Workspace) -> None:
        self._config = config
        self._workspace = workspace
        self._proc: subprocess.Popen[bytes] | None = None
        self._log_file: Path | None = None
        self.url: str = ""

    def start(self, log_dir: Path) -> str:
        """Launch (if a start command is configured) and wait until ready.

        Returns the resolved base URL. Raises BrowserQASetupError with the
        app log tail when the app never becomes ready.
        """
        app = self._config.app
        port = _free_port()
        self.url = app.url.replace("{port}", str(port)).rstrip("/")

        if app.start.strip():
            command = app.start.replace("{port}", str(port))
            log_dir.mkdir(parents=True, exist_ok=True)
            self._log_file = log_dir / "app.log"
            env = dict(os.environ)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTHONUNBUFFERED"] = "1"
            env["PORT"] = str(port)
            log_handle = self._log_file.open("wb")
            self._proc = subprocess.Popen(  # noqa: S602 — repo-configured command
                command,
                shell=True,
                cwd=str(self._workspace.root),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,  # own process group → clean teardown
            )
            log.info("app starting: %s (port %d)", command, port)

        ready_url = self.url + "/" + app.ready_path.lstrip("/")
        deadline = time.monotonic() + app.startup_timeout
        last_error = "no response"
        while time.monotonic() < deadline:
            if self._proc and self._proc.poll() is not None:
                raise BrowserQASetupError(
                    f"app exited with code {self._proc.returncode} before "
                    f"becoming ready\napp log tail:\n{self.log_tail()}"
                )
            try:
                response = httpx.get(ready_url, timeout=2.0, follow_redirects=True)
                if response.status_code < 400:
                    log.info("app ready at %s", self.url)
                    return self.url
                last_error = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
            time.sleep(0.25)
        self.stop()
        raise BrowserQASetupError(
            f"app not ready at {ready_url} within {app.startup_timeout:.0f}s "
            f"(last error: {last_error})\napp log tail:\n{self.log_tail()}"
        )

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            os.killpg(self._proc.pid, signal.SIGTERM)
            self._proc.wait(timeout=5)
        except (ProcessLookupError, PermissionError):
            pass
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self._proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._proc.wait(timeout=5)
        finally:
            self._proc = None

    def log_tail(self) -> str:
        if self._log_file and self._log_file.exists():
            text = self._log_file.read_text(encoding="utf-8", errors="replace")
            return text[-_APP_LOG_TAIL:]
        return ""


# ── the harness ───────────────────────────────────────────────────────────────


class BrowserQAHarness:
    """Runs every configured workflow against the launched app."""

    def __init__(self, config: BrowserQAConfig, artifacts_dir: Path) -> None:
        self.config = config
        self.artifacts_dir = Path(artifacts_dir)

    def run(self, workspace: Workspace) -> BrowserQAReport:
        spec_errors = validate_workflows(self.config.workflows)
        if spec_errors:
            return _setup_failure("invalid workflow spec:\n- " + "\n- ".join(spec_errors))

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return _setup_failure(
                "playwright is not installed — install the browser extra "
                "(pip install 'agentd[browser]') and run "
                "'playwright install chromium'"
            )

        launcher = AppLauncher(self.config, workspace)
        try:
            app_url = launcher.start(self.artifacts_dir)
        except BrowserQASetupError as exc:
            return _setup_failure(str(exc))

        workflows: list[BrowserWorkflowResult] = []
        try:
            with sync_playwright() as playwright:
                browser = self._launch(playwright)
                try:
                    for spec in self.config.workflows:
                        workflows.append(
                            self._run_workflow(browser, spec, app_url)
                        )
                finally:
                    browser.close()
        except BrowserQASetupError as exc:
            return _setup_failure(str(exc), app_log=launcher.log_tail())
        finally:
            launcher.stop()

        passed = all(w.passed for w in workflows)
        failed = [w.name for w in workflows if not w.passed]
        summary = (
            f"all {len(workflows)} workflow(s) passed"
            if passed
            else "failed workflows: " + ", ".join(failed)
        )
        return BrowserQAReport(
            enabled=True,
            passed=passed,
            workflows=workflows,
            app_url=app_url,
            app_log_tail=launcher.log_tail(),
            summary=summary,
        )

    # ── browser/session plumbing ─────────────────────────────────────────────

    def _launch(self, playwright):
        kwargs: dict = {"headless": self.config.headless}
        if self.config.chromium_executable:
            kwargs["executable_path"] = self.config.chromium_executable
            return playwright.chromium.launch(**kwargs)
        try:
            return playwright.chromium.launch(**kwargs)
        except Exception as first_error:  # noqa: BLE001 — try the provisioned browser
            fallback = Path(
                os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
            ) / "chromium"
            if fallback.exists():
                log.info("managed chromium unavailable (%s); using %s",
                         str(first_error).splitlines()[0][:80], fallback)
                kwargs["executable_path"] = str(fallback)
                return playwright.chromium.launch(**kwargs)
            raise BrowserQASetupError(
                f"could not launch chromium: {first_error}"
            ) from first_error

    def _run_workflow(
        self, browser, spec: BrowserWorkflowSpec, app_url: str
    ) -> BrowserWorkflowResult:
        from playwright.sync_api import expect

        timeout_ms = self.config.step_timeout * 1000
        expect.set_options(timeout=timeout_ms)
        ignore = compile_ignore_patterns(self.config.ignore_console_patterns)

        started = time.monotonic()
        console_errors: list[str] = []
        page_errors: list[str] = []
        screenshots: list[str] = []
        steps: list[BrowserStepResult] = []
        failed_step: str | None = None

        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text)
            if msg.type == "error" and not is_ignored(msg.text, ignore) else None,
        )
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        try:
            for index, step in enumerate(spec.steps, start=1):
                action, argument = next(iter(step.items()))
                step_started = time.monotonic()
                try:
                    self._execute_step(page, action, argument, app_url,
                                       spec.name, screenshots)
                    steps.append(BrowserStepResult(
                        index=index, action=action,
                        detail=_detail(argument), ok=True,
                        duration_ms=int((time.monotonic() - step_started) * 1000),
                    ))
                except Exception as exc:  # noqa: BLE001 — one step fails the workflow
                    error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}"
                    steps.append(BrowserStepResult(
                        index=index, action=action,
                        detail=_detail(argument), ok=False, error=error,
                        duration_ms=int((time.monotonic() - step_started) * 1000),
                    ))
                    failed_step = f"step {index} ({action}: {_detail(argument)}) — {error}"
                    self._capture(page, spec.name, "failure", screenshots)
                    break
        finally:
            context.close()

        steps_passed = failed_step is None
        # Phase 3 rule: browser test failure OR console errors OR failed
        # workflow verification → FAILED.
        passed = steps_passed and not console_errors and not page_errors
        return BrowserWorkflowResult(
            name=spec.name,
            passed=passed,
            steps_passed=steps_passed,
            steps=steps,
            failed_step=failed_step,
            console_errors=console_errors,
            page_errors=page_errors,
            screenshots=screenshots,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def _execute_step(self, page, action: str, argument, app_url: str,
                      workflow: str, screenshots: list[str]) -> None:
        from playwright.sync_api import expect

        if action == "goto":
            page.goto(app_url + "/" + str(argument).lstrip("/"))
        elif action == "click":
            page.click(str(argument))
        elif action == "fill":
            page.fill(argument["selector"], str(argument["value"]))
        elif action == "select":
            page.select_option(argument["selector"], str(argument["value"]))
        elif action == "expect_text":
            expect(page.locator(argument["selector"])).to_contain_text(
                str(argument["contains"])
            )
        elif action == "expect_no_text":
            expect(page.locator(argument["selector"])).not_to_contain_text(
                str(argument["contains"])
            )
        elif action == "expect_visible":
            expect(page.locator(str(argument))).to_be_visible()
        elif action == "expect_url":
            part = argument["contains"] if isinstance(argument, dict) else str(argument)
            page.wait_for_url(re.compile(".*" + re.escape(part) + ".*"))
        elif action == "expect_title":
            part = argument["contains"] if isinstance(argument, dict) else str(argument)
            expect(page).to_have_title(re.compile(".*" + re.escape(part) + ".*"))
        elif action == "wait_for":
            page.wait_for_selector(str(argument))
        elif action == "screenshot":
            self._capture(page, workflow, str(argument), screenshots)
        else:  # unreachable — validate_workflows ran first
            raise ValueError(f"unknown action: {action}")

    def _capture(self, page, workflow: str, name: str,
                 screenshots: list[str]) -> None:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.-]", "_", f"{workflow}-{name}")
        path = self.artifacts_dir / f"{safe}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            screenshots.append(str(path))
        except Exception as exc:  # noqa: BLE001 — screenshots must never fail a run
            log.warning("screenshot '%s' failed: %s", name, exc)


def _detail(argument) -> str:
    if isinstance(argument, dict):
        return ", ".join(f"{k}={v}" for k, v in argument.items())
    return str(argument)


def _setup_failure(message: str, app_log: str = "") -> BrowserQAReport:
    return BrowserQAReport(
        enabled=True,
        passed=False,
        error=message,
        app_log_tail=app_log,
        summary="browser QA setup failed",
    )


def skipped_report(reason: str) -> BrowserQAReport:
    """Stage configured but not run (e.g. command checks already failed)."""
    return BrowserQAReport(
        enabled=True, passed=False, skipped=True,
        summary=f"skipped — {reason}",
    )


# ── validation-pipeline integration ──────────────────────────────────────────


def browser_check_results(report: BrowserQAReport) -> list[CheckResult]:
    """Render the browser stage as validation checks so the existing
    RCA → DEBUG → FIX loop and the git gate treat UI failures like any
    other failing check."""
    if not report.enabled or report.skipped:
        return []
    if report.error:
        output = report.error
        if report.app_log_tail:
            output += f"\napp log tail:\n{report.app_log_tail}"
        return [CheckResult(name="browser[setup]", command="browser QA setup",
                            ok=False, exit_code=1, output_tail=output[-3000:])]
    checks: list[CheckResult] = []
    for wf in report.workflows:
        checks.append(CheckResult(
            name=f"browser[{wf.name}]",
            command=f"browser workflow '{wf.name}'",
            ok=wf.passed,
            exit_code=0 if wf.passed else 1,
            duration_ms=wf.duration_ms,
            output_tail=_workflow_evidence(wf, report),
        ))
    return checks


def _workflow_evidence(wf: BrowserWorkflowResult, report: BrowserQAReport) -> str:
    if wf.passed:
        return f"{len(wf.steps)} step(s) passed, no console errors"
    parts: list[str] = []
    if wf.failed_step:
        parts.append(f"failed at {wf.failed_step}")
    if wf.console_errors:
        parts.append("console errors (validation fails on any):")
        parts.extend(f"  - {e}" for e in wf.console_errors[:10])
    if wf.page_errors:
        parts.append("uncaught page errors:")
        parts.extend(f"  - {e}" for e in wf.page_errors[:10])
    if wf.screenshots:
        parts.append("screenshots: " + ", ".join(wf.screenshots))
    if report.app_log_tail:
        parts.append(f"app log tail:\n{report.app_log_tail[-1000:]}")
    return "\n".join(parts)[-3000:]


def merge_validation(report: ValidationReport,
                     browser: BrowserQAReport) -> ValidationReport:
    """Combine command checks and the browser stage into one verdict."""
    checks = list(report.checks) + browser_check_results(browser)
    passed = report.passed and browser.passed
    summary = f"{report.summary}; browser QA: {browser.summary}"
    return ValidationReport(passed=passed, checks=checks, summary=summary,
                            browser=browser)
