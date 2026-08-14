"""Debug Agent — root-cause identification for the self-healing workflow.

Read-only by design: the Debug Agent investigates (reproduce, read, grep,
diff) and emits a structured :class:`~agentd.schemas.DebugReport`; the
Coding Agent applies the repair. This separation — plus the deterministic
RCA input and stall detection around it — is how "root cause, not symptom"
is enforced structurally rather than by prompt alone (ADR-015).
"""

from __future__ import annotations

from agentd.agents.base import BaseAgent
from agentd.agents.prompts import load_prompt
from agentd.rca import RcaEngine
from agentd.schemas import (
    DebugReport,
    ErrorAnalysis,
    HealingIteration,
    ValidationReport,
)
from agentd.workspace import Workspace


class DebuggerAgent(BaseAgent):
    agent_name = "debugger"
    role = "debugger"
    # Investigation tools only: reproduce + read. No write, no commit.
    tool_names = [
        "fs_read",
        "fs_ls",
        "fs_glob",
        "code_grep",
        "exec_run",
        "git_diff",
        "git_status",
    ]

    def run(
        self,
        goal: str,
        report: ValidationReport,
        analyses: list[ErrorAnalysis],
        history: list[HealingIteration],
        workspace: Workspace,
    ) -> DebugReport:
        system = load_prompt("debugger")
        user = self._render_case(goal, report, analyses, history)
        debug_report: DebugReport = self.ask_for_json(
            system, user, workspace, DebugReport.model_validate
        )
        self.journal.append(
            "DEBUG_REPORT",
            root_cause=debug_report.root_cause,
            category=debug_report.category,
            confidence=debug_report.confidence,
            affected_files=debug_report.affected_files,
            approach=debug_report.fix_strategy.approach,
        )
        return debug_report

    @staticmethod
    def _render_case(
        goal: str,
        report: ValidationReport,
        analyses: list[ErrorAnalysis],
        history: list[HealingIteration],
    ) -> str:
        if history:
            past = "\n".join(
                f"- iteration {h.iteration}: diagnosed '{h.root_cause}' "
                f"(confidence {h.confidence or '?'}); fix task {h.fix_task_id} "
                f"was {h.fix_status or 'applied'}; revalidation "
                f"{'PASSED' if h.revalidation_passed else 'FAILED'}"
                for h in history
            )
        else:
            past = "(none — this is the first debugging iteration)"
        return (
            f"Overall goal of the run: {goal}\n\n"
            f"Failing validation evidence:\n{report.failure_evidence(3000)}\n\n"
            f"Root-cause analysis engine output (deterministic):\n"
            f"{RcaEngine.render_for_prompt(analyses)}\n\n"
            f"Previous debugging iterations:\n{past}\n\n"
            "Investigate with your tools, then reply with the JSON debugging "
            "report."
        )
