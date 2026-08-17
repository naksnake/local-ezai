"""Validation Agent — test execution, build validation, lint validation.

Harness-first by design (AGENT_DESIGN §3.5): command execution and result
parsing are deterministic code; no LLM stands between a failing exit code
and the verdict. Commands come from, in priority order:

1. the target repo's ``.agentd.yaml`` → ``validation.commands``
2. the global config → ``validation.commands``
3. autodetection (Python: pytest / ruff), if enabled

Categories: ``test``, ``lint``, ``build`` — each a list of shell commands.
No configured or detected checks → the report passes with an explicit
warning in ``summary`` (journaled), so "green" is never silently vacuous.
"""

from __future__ import annotations

import sys

from agentd.agents.base import BaseAgent
from agentd.schemas import CheckResult, ValidationReport
from agentd.tools.shell import run_command
from agentd.workspace import Workspace

# CLAUDE.md Validation Agent responsibilities: tests, build, lint, TYPE
_CATEGORY_ORDER = ("lint", "type", "build", "test")


class ValidationAgent(BaseAgent):
    agent_name = "validator"
    role = "validator"
    tool_names: list[str] = []  # deterministic — no LLM tools needed

    def run(self, workspace: Workspace) -> ValidationReport:
        commands = self._resolve_commands(workspace)
        checks: list[CheckResult] = []
        for category in _CATEGORY_ORDER:
            for index, command in enumerate(commands.get(category, [])):
                name = f"{category}[{index}]"
                self.journal.append("CHECK_STARTED", name=name, command=command)
                result = run_command(
                    workspace, command, timeout=self.config.validation.command_timeout
                )
                checks.append(
                    CheckResult(
                        name=name,
                        command=command,
                        ok=result.ok,
                        exit_code=result.exit_code,
                        duration_ms=result.duration_ms,
                        output_tail=result.output[-3000:],
                    )
                )
                self.journal.append(
                    "CHECK_FINISHED",
                    name=name,
                    ok=result.ok,
                    exit_code=result.exit_code,
                )

        if not checks:
            report = ValidationReport(
                passed=True,
                checks=[],
                summary=(
                    "no validation commands configured or detected — "
                    "nothing was verified"
                ),
            )
        else:
            failed = [c.name for c in checks if not c.ok]
            report = ValidationReport(
                passed=not failed,
                checks=checks,
                summary=(
                    f"all {len(checks)} check(s) passed"
                    if not failed
                    else f"failed: {', '.join(failed)}"
                ),
            )
        self.journal.append("VALIDATION", passed=report.passed, summary=report.summary)
        return report

    # ── command resolution ──────────────────────────────────────────────────

    def _resolve_commands(self, workspace: Workspace) -> dict[str, list[str]]:
        configured = {
            k: list(v) for k, v in self.config.validation.commands.items() if v
        }
        if configured:
            return configured
        if self.config.validation.autodetect:
            return self._autodetect(workspace)
        return {}

    @staticmethod
    def _autodetect(workspace: Workspace) -> dict[str, list[str]]:
        # Use the running interpreter, not a hardcoded binary name:
        # 'python3' does not exist on Windows, and the interpreter running
        # agentd is the one whose environment carries pytest/ruff.
        python = f'"{sys.executable}"'
        root = workspace.root
        detected: dict[str, list[str]] = {}
        has_pytest_layout = (
            (root / "pyproject.toml").is_file()
            or (root / "pytest.ini").is_file()
            or (root / "tests").is_dir()
            or any(root.glob("test_*.py"))
        )
        if has_pytest_layout:
            detected["test"] = [f"{python} -m pytest -q --color=no"]
        pyproject = root / "pyproject.toml"
        if pyproject.is_file() and "[tool.ruff]" in pyproject.read_text(
            encoding="utf-8", errors="replace"
        ):
            detected["lint"] = [f"{python} -m ruff check ."]
        return detected
