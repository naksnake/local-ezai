"""Reviewer Agent — adversarial pass over a diff (Phase 5).

Read-only by construction (like the Debug Agent): the Reviewer cannot fix,
only report — findings go back through humans or the Coding Agent. Reviews
the diff against the goal, the project's persisted coding styles and rules
(memory integration), and the hard prohibitions (check-weakening, silenced
errors, scope creep). Emits a structured `Review v1` envelope.
"""

from __future__ import annotations

from agentd.agents.base import BaseAgent
from agentd.agents.prompts import load_prompt
from agentd.schemas import ReviewReport
from agentd.workspace import Workspace

_MAX_DIFF_CHARS = 24_000


class ReviewerAgent(BaseAgent):
    agent_name = "reviewer"
    role = "reviewer"
    tool_names = ["fs_read", "fs_ls", "fs_glob", "code_grep",
                  "git_status", "git_diff"]

    def run(self, diff_text: str, workspace: Workspace,
            context: str = "") -> ReviewReport:
        if not diff_text.strip():
            report = ReviewReport(verdict="approve",
                                  summary="empty diff — nothing to review")
            self.journal.append("REVIEW", verdict=report.verdict,
                                findings=0, summary=report.summary)
            return report

        diff = diff_text
        truncated = ""
        if len(diff) > _MAX_DIFF_CHARS:
            diff = diff[:_MAX_DIFF_CHARS]
            truncated = "\n[diff truncated — read files with fs_read for the rest]"
        user = (
            (f"Context: {context}\n\n" if context else "")
            + self._memory_block()
            + f"Diff under review:\n```diff\n{diff}\n```{truncated}"
        )
        report: ReviewReport = self.ask_for_json(
            load_prompt("reviewer"), user, workspace, ReviewReport.model_validate
        )
        self.journal.append(
            "REVIEW",
            verdict=report.verdict,
            findings=len(report.findings),
            high=sum(1 for f in report.findings if f.severity == "high"),
            summary=report.summary[:200],
        )
        self.log.info("review: %s (%d finding(s))", report.verdict,
                      len(report.findings))
        return report

    def _memory_block(self) -> str:
        if self.memory is None:
            return ""
        from agentd.memory import KIND_RULE, KIND_STYLE

        records = self.memory.recent([KIND_RULE, KIND_STYLE], limit=10)
        if not records:
            return ""
        lines = "\n".join(f"- [{r.kind}] {r.title}: {r.content[:200]}"
                          for r in records)
        return ("Project rules and coding styles (persisted memory — flag "
                f"violations):\n{lines}\n\n")
