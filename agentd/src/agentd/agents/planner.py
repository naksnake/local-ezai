"""Planner Agent — requirement analysis, task decomposition, plan generation.

Explores the repository read-only, then emits a schema-validated `Plan v1`
envelope (schemas.Plan). Invalid or oversized plans are rejected/trimmed by
code, not by trust.
"""

from __future__ import annotations

from agentd.agents.base import BaseAgent
from agentd.agents.prompts import load_prompt
from agentd.schemas import Plan
from agentd.workspace import Workspace


def _validate_plan(data: dict) -> Plan:
    """The schema permits empty task lists (synthetic fix/commit plans);
    a PLANNER, however, must always produce at least one task."""
    plan = Plan.model_validate(data)
    if not plan.tasks:
        raise ValueError("the plan must contain at least one task")
    return plan


class PlannerAgent(BaseAgent):
    agent_name = "planner"
    role = "planner"
    tool_names = ["fs_ls", "fs_read", "fs_glob", "code_grep"]

    def run(self, request: str, workspace: Workspace) -> Plan:
        system = load_prompt("planner")
        user = (
            f"Repository workspace: {workspace.root.name} "
            f"(branch {workspace.branch})\n"
            f"Maximum number of tasks: {self.config.limits.max_plan_tasks}\n\n"
            f"Change request:\n{request}"
        )
        user += self._memory_block(request)
        plan: Plan = self.ask_for_json(system, user, workspace, _validate_plan)

        max_tasks = self.config.limits.max_plan_tasks
        if len(plan.tasks) > max_tasks:
            self.journal.append(
                "PLAN_TRIMMED", from_tasks=len(plan.tasks), to_tasks=max_tasks
            )
            plan = plan.model_copy(update={"tasks": plan.tasks[:max_tasks]})

        self.journal.append(
            "PLAN_READY",
            goal=plan.goal,
            tasks=[t.id for t in plan.tasks],
            risks=plan.risks,
        )
        return plan

    def _memory_block(self, request: str) -> str:
        """Project memory injected into planning (Phase 4, ADR-017)."""
        if self.memory is None:
            return ""
        from agentd.memory import render_planner_context

        block = render_planner_context(
            self.memory, request, self.config.memory.max_context_items
        )
        if not block:
            return ""
        self.journal.append("MEMORY_INJECTED", agent=self.agent_name,
                            chars=len(block))
        return (
            "\n\nProject memory (persisted knowledge from previous runs — "
            "respect the rules and learn from the lessons):\n" + block
        )
