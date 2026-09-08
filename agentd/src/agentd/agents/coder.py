"""Coding Agent — reads the repository, modifies/creates files, adds tests.

Implements exactly one plan task per invocation through the shared tool
loop. Changed files are computed from git, not from model claims.
"""

from __future__ import annotations

from agentd.agents.base import BaseAgent
from agentd.agents.prompts import load_prompt
from agentd.schemas import Plan, PlanTask, TaskResult
from agentd.tools.git import git_run
from agentd.workspace import Workspace


class CoderAgent(BaseAgent):
    agent_name = "coder"
    role = "coder"
    tool_names = [
        "fs_read",
        "fs_write",
        "fs_edit",
        "fs_ls",
        "fs_glob",
        "code_grep",
        "code_symbols",
        "exec_run",
        "git_status",
        "git_diff",
    ]

    def run(self, plan: Plan, task: PlanTask, workspace: Workspace) -> TaskResult:
        system = load_prompt("coder")
        hints = ", ".join(task.files_hint) if task.files_hint else "(none given)"
        user = (
            f"Overall goal: {plan.goal}\n\n"
            f"Your task ({task.id}, kind: {task.kind}):\n{task.intent}\n\n"
            f"Likely files: {hints}\n"
            f"Verified by: {task.check or 'the project validation suite'}\n"
        )
        before = self._changed_files(workspace)
        outcome = self.run_loop(system, user, workspace)
        after = self._changed_files(workspace)
        files_changed = sorted(after - before) if after != before else sorted(after)

        failed = outcome.budget_exhausted or outcome.final_text.strip().startswith("FAILED:")
        summary = (
            outcome.final_text.strip()
            or "turn budget exhausted before the task was finished"
        )
        result = TaskResult(
            task_id=task.id,
            status="failed" if failed else "done",
            summary=summary[:2000],
            files_changed=files_changed,
            turns_used=outcome.turns_used,
        )
        self.journal.append("TASK_RESULT", **result.model_dump())
        return result

    @staticmethod
    def _changed_files(workspace: Workspace) -> set[str]:
        status = git_run(workspace, "status", "--porcelain")
        files: set[str] = set()
        if status.ok:
            for line in status.output.splitlines():
                parts = line.strip().split(maxsplit=1)
                if len(parts) != 2:
                    continue
                path = parts[1].strip().strip('"')
                if " -> " in path:  # rename: keep the new name
                    path = path.split(" -> ", 1)[1]
                files.add(path)
        return files
