"""Sprint Agent — requirement analysis, task breakdown, dependency graph
(Phase 6, ADR-019).

Reads a sprint specification (markdown), explores the repository read-only,
and produces a schema-validated :class:`~agentd.schemas.SprintPlan`:
extracted requirements, self-contained tasks, and an explicit dependency
graph. DAG integrity (unique ids, known dependencies, no cycles) is
enforced by deterministic validation — a structurally broken graph is fed
back to the model for correction, bounded by the usual retry budget.

The Sprint Agent plans; it never executes. The wave executor
(:mod:`agentd.sprint_exec`) turns the plan into parallel agent runs.
"""

from __future__ import annotations

from agentd.agents.base import BaseAgent
from agentd.agents.prompts import load_prompt
from agentd.schemas import SprintPlan
from agentd.sprint import validate_dependencies
from agentd.workspace import Workspace

_MAX_SPEC_CHARS = 16_000


def _validate_sprint_plan(data: dict) -> SprintPlan:
    plan = SprintPlan.model_validate(data)
    errors = validate_dependencies(plan.tasks)
    if errors:
        raise ValueError("invalid dependency graph: " + "; ".join(errors))
    return plan


class SprintAgent(BaseAgent):
    agent_name = "sprint"
    role = "sprint"
    tool_names = ["fs_ls", "fs_read", "fs_glob", "code_grep"]  # read-only

    def run(self, spec_text: str, workspace: Workspace) -> SprintPlan:
        spec = spec_text[:_MAX_SPEC_CHARS]
        user = (
            f"Repository workspace: {workspace.root.name} "
            f"(branch {workspace.branch})\n\n"
            f"Sprint specification (markdown):\n---\n{spec}\n---\n"
        )
        if self.memory is not None:
            user += self._memory_block(spec)
        plan: SprintPlan = self.ask_for_json(
            load_prompt("sprint"), user, workspace, _validate_sprint_plan
        )
        self.journal.append(
            "SPRINT_PLAN",
            goal=plan.goal,
            requirements=len(plan.requirements),
            tasks=[t.id for t in plan.tasks],
            edges=[f"{d}->{t.id}" for t in plan.tasks for d in t.depends_on],
        )
        return plan

    def _memory_block(self, spec: str) -> str:
        from agentd.memory import render_planner_context

        block = render_planner_context(
            self.memory, spec[:500], self.config.memory.max_context_items
        )
        if not block:
            return ""
        self.journal.append("MEMORY_INJECTED", agent=self.agent_name,
                            chars=len(block))
        return ("\nProject memory (respect the rules; learn from the "
                "lessons):\n" + block + "\n")
