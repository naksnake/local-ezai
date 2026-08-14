"""LangGraph orchestration — the Phase 1 slice of WORKFLOW_DESIGN.md.

The graph is deterministic code (ADR-010/ADR-013): nodes call agents, edges
route on state, budgets bound every cycle. MVP topology:

    START → plan → code ↺ (next task) → validate → git → END
                     ↑                      │
                     └────── diagnose ◄─────┘  (fail, bounded fix attempts)
              any error / exhausted budgets → abort → END

Full target machine (INTAKE/CLARIFYING/PLAN_GATE/REVIEWING/BLOCKED…) arrives
in Phase 3; states here map 1:1 onto its PLANNING/EXECUTING/VERIFYING/
FINALIZING core so the extension is additive.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from agentd.agents import CoderAgent, GitAgent, PlannerAgent, ValidationAgent
from agentd.config import AgentdConfig
from agentd.journal import Journal
from agentd.logging_setup import get_logger
from agentd.schemas import (
    CommitInfo,
    Plan,
    PlanTask,
    RunReport,
    TaskResult,
    ValidationReport,
)
from agentd.workspace import Workspace

log = get_logger("graph")


class RunState(TypedDict, total=False):
    """LangGraph state — JSON-serializable values only."""

    run_id: str
    request: str
    status: str  # running | completed | failed
    error: str | None
    plan: dict[str, Any] | None
    task_index: int
    task_results: list[dict[str, Any]]
    validation: dict[str, Any] | None
    fix_attempts: int
    commit: dict[str, Any] | None


class Orchestrator:
    """Owns the compiled graph and the run-scoped dependencies."""

    def __init__(
        self,
        config: AgentdConfig,
        workspace: Workspace,
        journal: Journal,
        planner: PlannerAgent,
        coder: CoderAgent,
        validator: ValidationAgent,
        git_agent: GitAgent,
    ) -> None:
        self.config = config
        self.workspace = workspace
        self.journal = journal
        self.planner = planner
        self.coder = coder
        self.validator = validator
        self.git_agent = git_agent
        self.graph = self._build()

    # ── graph wiring ─────────────────────────────────────────────────────────

    def _build(self):
        builder = StateGraph(RunState)
        builder.add_node("plan", self._plan_node)
        builder.add_node("code", self._code_node)
        builder.add_node("validate", self._validate_node)
        builder.add_node("diagnose", self._diagnose_node)
        builder.add_node("git", self._git_node)
        builder.add_node("abort", self._abort_node)

        builder.add_edge(START, "plan")
        builder.add_conditional_edges(
            "plan", self._route_after_plan, {"code": "code", "abort": "abort"}
        )
        builder.add_conditional_edges(
            "code",
            self._route_after_code,
            {"code": "code", "validate": "validate", "abort": "abort"},
        )
        builder.add_conditional_edges(
            "validate",
            self._route_after_validate,
            {"git": "git", "diagnose": "diagnose", "abort": "abort"},
        )
        builder.add_edge("diagnose", "code")
        builder.add_conditional_edges(
            "git", self._route_after_git, {"end": END, "abort": "abort"}
        )
        builder.add_edge("abort", END)
        return builder.compile()

    # ── nodes ────────────────────────────────────────────────────────────────

    def _plan_node(self, state: RunState) -> dict[str, Any]:
        self.journal.append("STATE_ENTERED", state="PLANNING")
        try:
            plan = self.planner.run(state["request"], self.workspace)
        except Exception as exc:  # noqa: BLE001 — node failures become run failures
            log.error("planning failed: %s", exc)
            return {"error": f"planning failed: {exc}", "status": "failed"}
        log.info("plan ready: %d task(s) — %s", len(plan.tasks), plan.goal)
        return {"plan": plan.model_dump(), "task_index": 0}

    def _code_node(self, state: RunState) -> dict[str, Any]:
        plan = Plan.model_validate(state["plan"])
        index = state.get("task_index", 0)
        task = plan.tasks[index]
        self.journal.append("STATE_ENTERED", state="EXECUTING", task=task.id)
        log.info("coding task %s (%d/%d): %s", task.id, index + 1, len(plan.tasks),
                 task.intent[:80])
        try:
            result = self.coder.run(plan, task, self.workspace)
        except Exception as exc:  # noqa: BLE001
            log.error("task %s crashed: %s", task.id, exc)
            return {"error": f"task {task.id} failed: {exc}", "status": "failed"}
        results = list(state.get("task_results", []))
        results.append(result.model_dump())
        update: dict[str, Any] = {"task_results": results, "task_index": index + 1}
        if result.status == "failed":
            update["error"] = f"task {task.id} failed: {result.summary[:300]}"
            update["status"] = "failed"
        return update

    def _validate_node(self, state: RunState) -> dict[str, Any]:
        self.journal.append("STATE_ENTERED", state="VERIFYING")
        try:
            report = self.validator.run(self.workspace)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"validation crashed: {exc}", "status": "failed"}
        log.info("validation: %s", report.summary)
        return {"validation": report.model_dump()}

    def _diagnose_node(self, state: RunState) -> dict[str, Any]:
        """Turn validation failures into a bounded fix task (inner loop's
        DIAGNOSE step; the Coding Agent performs the actual repair)."""
        self.journal.append("STATE_ENTERED", state="DIAGNOSE")
        plan = Plan.model_validate(state["plan"])
        report = ValidationReport.model_validate(state["validation"])
        attempt = state.get("fix_attempts", 0) + 1
        fix_task = PlanTask(
            id=f"FIX{attempt}",
            intent=(
                "Validation failed after the previous changes. Repair the "
                "workspace so all checks pass. Failing evidence:\n\n"
                + report.failure_evidence()
            ),
            check="all configured validation checks pass",
            kind="fix",
        )
        tasks = list(plan.tasks) + [fix_task]
        plan = plan.model_copy(update={"tasks": tasks})
        log.info("diagnose: scheduling %s (attempt %d/%d)", fix_task.id, attempt,
                 self.config.limits.max_fix_attempts)
        return {
            "plan": plan.model_dump(),
            "task_index": len(tasks) - 1,
            "fix_attempts": attempt,
        }

    def _git_node(self, state: RunState) -> dict[str, Any]:
        self.journal.append("STATE_ENTERED", state="FINALIZING")
        plan = Plan.model_validate(state["plan"])
        results = [TaskResult.model_validate(r) for r in state.get("task_results", [])]
        report = ValidationReport.model_validate(state["validation"])
        try:
            info = self.git_agent.run(
                self.workspace, plan, results, report, state["run_id"]
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": f"git finalization failed: {exc}", "status": "failed"}
        log.info("delivered: %s on %s (pushed=%s)", info.sha[:12] or "no commit",
                 info.branch, info.pushed)
        return {"commit": info.model_dump(), "status": "completed"}

    def _abort_node(self, state: RunState) -> dict[str, Any]:
        error = state.get("error")
        if not error:
            validation = state.get("validation") or {}
            if validation and not validation.get("passed", True):
                error = (
                    "validation still failing after "
                    f"{state.get('fix_attempts', 0)} fix attempt(s): "
                    f"{validation.get('summary', '')}"
                )
            else:
                error = "aborted"
        self.journal.append("RUN_TERMINAL", status="failed", error=error)
        log.error("run failed: %s", error)
        return {"status": "failed", "error": error}

    # ── routes ───────────────────────────────────────────────────────────────

    def _route_after_plan(self, state: RunState) -> Literal["code", "abort"]:
        if state.get("status") == "failed" or not state.get("plan"):
            return "abort"
        return "code"

    def _route_after_code(self, state: RunState) -> Literal["code", "validate", "abort"]:
        if state.get("status") == "failed":
            return "abort"
        plan = state.get("plan") or {}
        if state.get("task_index", 0) < len(plan.get("tasks", [])):
            return "code"
        return "validate"

    def _route_after_validate(self, state: RunState) -> Literal["git", "diagnose", "abort"]:
        if state.get("status") == "failed":
            return "abort"
        validation = state.get("validation") or {}
        if validation.get("passed"):
            return "git"
        if state.get("fix_attempts", 0) < self.config.limits.max_fix_attempts:
            return "diagnose"
        # Fix budget exhausted; the abort node derives the error message
        # from the validation state (routes never mutate state).
        return "abort"

    def _route_after_git(self, state: RunState) -> Literal["end", "abort"]:
        if state.get("status") == "failed":
            return "abort"
        return "end"

    # ── entry point ──────────────────────────────────────────────────────────

    def run(self, run_id: str, request: str) -> RunReport:
        self.journal.append("RUN_SUBMITTED", run_id=run_id, request=request,
                            workspace=str(self.workspace.root),
                            branch=self.workspace.branch)
        initial: RunState = {
            "run_id": run_id,
            "request": request,
            "status": "running",
            "error": None,
            "plan": None,
            "task_index": 0,
            "task_results": [],
            "validation": None,
            "fix_attempts": 0,
            "commit": None,
        }
        final: RunState = self.graph.invoke(
            initial, config={"recursion_limit": self.config.limits.recursion_limit}
        )
        status = "completed" if final.get("status") == "completed" else "failed"
        if status == "completed":
            self.journal.append("RUN_TERMINAL", status="completed")
        report = RunReport(
            run_id=run_id,
            status=status,  # type: ignore[arg-type]
            request=request,
            repo_path=str(self.workspace.repo_path),
            workspace_path=str(self.workspace.root),
            branch=self.workspace.branch,
            error=final.get("error"),
            plan=Plan.model_validate(final["plan"]) if final.get("plan") else None,
            task_results=[TaskResult.model_validate(r)
                          for r in final.get("task_results", [])],
            validation=(ValidationReport.model_validate(final["validation"])
                        if final.get("validation") else None),
            commit=CommitInfo.model_validate(final["commit"])
            if final.get("commit") else None,
            journal_path=str(self.journal.path),
            fix_attempts=final.get("fix_attempts", 0),
        )
        return report
