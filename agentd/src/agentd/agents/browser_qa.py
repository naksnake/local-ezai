"""Browser QA Agent — UI-level validation via Playwright (Phase 3, ADR-016).

Deterministic harness agent (like the Validation Agent): it launches the
application, drives the repo's declared user workflows in a real browser,
watches every page for console/page errors, captures screenshots, and
returns a :class:`~agentd.schemas.BrowserQAReport`. No LLM decides pass or
fail.

The report merges into the run's ValidationReport (``merge_validation``),
which is what blocks git commits until Browser QA succeeds.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from agentd.agents.base import BaseAgent
from agentd.browser_qa import BrowserQAHarness
from agentd.schemas import BrowserQAReport
from agentd.workspace import Workspace


class BrowserQAAgent(BaseAgent):
    agent_name = "browser_qa"
    role = "validator"  # no LLM calls; role kept for uniform wiring
    tool_names: list[str] = []

    #: Test seam: a callable ``(config.browser_qa, artifacts_dir) -> harness``
    #: where harness has ``.run(workspace) -> BrowserQAReport``. None → real
    #: Playwright harness.
    harness_factory = None

    def run(self, workspace: Workspace) -> BrowserQAReport:
        cfg = self.config.browser_qa
        if not cfg.enabled or not cfg.workflows:
            return BrowserQAReport(
                enabled=False, passed=True,
                summary="browser QA not configured",
            )

        artifacts_dir = self._artifacts_dir()
        self.journal.append(
            "BROWSER_QA_STARTED",
            workflows=[w.name for w in cfg.workflows],
            headless=cfg.headless,
            artifacts_dir=str(artifacts_dir),
        )
        factory = self.harness_factory or BrowserQAHarness
        report: BrowserQAReport = factory(cfg, artifacts_dir).run(workspace)

        for wf in report.workflows:
            self.journal.append(
                "BROWSER_WORKFLOW",
                name=wf.name,
                passed=wf.passed,
                steps=len(wf.steps),
                failed_step=wf.failed_step,
                console_errors=len(wf.console_errors),
                page_errors=len(wf.page_errors),
                screenshots=wf.screenshots,
            )
        self.journal.append(
            "BROWSER_QA",
            passed=report.passed,
            summary=report.summary,
            error=report.error,
            app_url=report.app_url,
        )
        self.log.info("browser QA: %s", report.summary)
        return report

    def _artifacts_dir(self) -> Path:
        if getattr(self.journal, "is_persistent", True):
            return Path(self.journal.run_dir) / "browser-qa"
        return Path(tempfile.mkdtemp(prefix="agentd-browser-qa-"))
