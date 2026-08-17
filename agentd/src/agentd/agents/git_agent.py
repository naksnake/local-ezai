"""Git Agent — stage, commit, and (optionally) push the run's changes.

Deterministic by default: staging and committing are direct tool calls; the
commit message is a template unless ``git.llm_commit_message`` is enabled.
Push is a T3 action, fail-closed behind ``git.allow_push`` (ADR-008) — a
denied or failed push is recorded on the CommitInfo, never fatal to the run
(the local branch is the deliverable).
"""

from __future__ import annotations

from agentd.agents.base import BaseAgent
from agentd.agents.prompts import load_prompt
from agentd.schemas import CommitInfo, Plan, TaskResult, ValidationReport
from agentd.tools.git import git_run
from agentd.workspace import Workspace


class GitAgent(BaseAgent):
    agent_name = "git"
    role = "git"
    tool_names = ["git_status", "git_diff", "git_add", "git_commit", "git_push"]

    def run(
        self,
        workspace: Workspace,
        plan: Plan,
        task_results: list[TaskResult],
        validation: ValidationReport,
        run_id: str,
    ) -> CommitInfo:
        # Hard gate (Phase 3, defense-in-depth beyond the graph route):
        # no git action of any kind until validation — including the Browser
        # QA stage when configured — has succeeded.
        if not validation.passed:
            self.journal.append("COMMIT_BLOCKED", reason=validation.summary)
            raise RuntimeError(
                "commit blocked: validation has not passed "
                f"({validation.summary}) — git actions require a fully green "
                "validation, including browser QA when configured"
            )
        status = self.registry.execute(
            "git_status", {}, workspace, agent=self.agent_name, allowlist=self.tool_names
        )
        if status.ok and not status.output.strip():
            self.journal.append("GIT_NO_CHANGES")
            return CommitInfo(branch=workspace.branch, message="no changes to commit")

        add = self.registry.execute(
            "git_add", {}, workspace, agent=self.agent_name, allowlist=self.tool_names
        )
        if not add.ok:
            raise RuntimeError(f"git add failed: {add.error} {add.output}")

        message = self._commit_message(workspace, plan, task_results, validation, run_id)
        commit = self.registry.execute(
            "git_commit",
            {
                "message": message,
                "user_name": self.config.git.user_name,
                "user_email": self.config.git.user_email,
            },
            workspace,
            agent=self.agent_name,
            allowlist=self.tool_names,
        )
        if not commit.ok:
            raise RuntimeError(f"git commit failed: {commit.error} {commit.output}")

        files_committed = sum(
            1 for line in (status.output or "").splitlines() if line.strip()
        )
        info = CommitInfo(
            sha=commit.extra.get("sha", ""),
            message=message,
            branch=workspace.branch,
            files_committed=files_committed,
        )

        push = self.registry.execute(
            "git_push",
            {"branch": workspace.branch, "remote": self.config.git.remote},
            workspace,
            agent=self.agent_name,
            allowlist=self.tool_names,
        )
        if push.ok:
            info.pushed = True
        else:
            # Permission-denied (allow_push off) or transport failure —
            # both are non-fatal, journaled, and surfaced in the report.
            info.push_error = push.error
        self.journal.append(
            "GIT_DELIVERY",
            sha=info.sha,
            branch=info.branch,
            pushed=info.pushed,
            push_error=info.push_error,
        )
        return info

    # ── commit message ──────────────────────────────────────────────────────

    def _commit_message(
        self,
        workspace: Workspace,
        plan: Plan,
        task_results: list[TaskResult],
        validation: ValidationReport,
        run_id: str,
    ) -> str:
        if self.config.git.llm_commit_message:
            try:
                return self._llm_message(workspace, plan, task_results, validation, run_id)
            except Exception as exc:  # noqa: BLE001 — template is the safe fallback
                self.log.warning("LLM commit message failed (%s); using template", exc)
        return self._template_message(plan, task_results, validation, run_id)

    def _llm_message(
        self,
        workspace: Workspace,
        plan: Plan,
        task_results: list[TaskResult],
        validation: ValidationReport,
        run_id: str,
    ) -> str:
        diffstat = git_run(workspace, "diff", "--cached", "--stat")
        user = (
            f"Goal: {plan.goal}\n"
            f"Tasks executed:\n"
            + "\n".join(f"- {r.task_id}: {r.summary[:200]}" for r in task_results)
            + f"\nValidation: {validation.summary}\n"
            f"Diffstat:\n{diffstat.output[:2000]}"
        )
        outcome = self.run_loop(load_prompt("git_commit"), user, workspace, max_turns=1)
        text = outcome.final_text.strip()
        if not text:
            raise RuntimeError("empty commit message from model")
        return f"{text}\n\n{self._trailer(run_id)}"

    def _template_message(
        self,
        plan: Plan,
        task_results: list[TaskResult],
        validation: ValidationReport,
        run_id: str,
    ) -> str:
        kinds = {t.kind for t in plan.tasks}
        prefix = "fix" if kinds == {"fix"} else "feat"
        subject = f"{prefix}: {plan.goal.strip()}"
        if len(subject) > 65:
            subject = subject[:62] + "..."
        body_lines = [f"- {r.task_id}: {r.summary.splitlines()[0][:100]}"
                      for r in task_results]
        body_lines.append(f"Validation: {validation.summary}")
        return subject + "\n\n" + "\n".join(body_lines) + f"\n\n{self._trailer(run_id)}"

    @staticmethod
    def _trailer(run_id: str) -> str:
        return f"Agentd-Run: {run_id}"
