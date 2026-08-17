"""LangGraph orchestration — the self-healing workflow (Phase 2, ADR-015).

Implements the required state machine, deterministically (ADR-010/ADR-013):

    START → PLAN → CODE (task loop) → VALIDATE ──passed──► GIT → SUCCESS/END
                     ▲                    │ failed
                     │                    ▼
                     │                  DEBUG  (Debug Agent + RCA engine)
                     │                    │
                     │                    ▼
                     └──(initial tasks)  FIX   (Coding Agent applies strategy)
                                          │
                                          ▼
                                      REVALIDATE  (same validate node,
                                          │        journaled distinctly)
                                          └── loop, bounded by
                                              limits.max_heal_iterations (10)
                                              + stall detection (ADR-015)

Loop-integrity guarantees, all in code, never model judgment:
- at most ``max_heal_iterations`` DEBUG→FIX→REVALIDATE cycles per run;
- early abort when the identical failure signature persists for
  ``stall_threshold`` consecutive validations (symptom-patching detector);
- routes read only validated state fields; every transition is journaled.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from agentd.agents import (
    BrowserQAAgent,
    CoderAgent,
    DebuggerAgent,
    GitAgent,
    PlannerAgent,
    ValidationAgent,
)
from agentd.agents.memory_agent import MemoryAgent
from agentd.browser_qa import merge_validation, skipped_report
from agentd.config import AgentdConfig
from agentd.journal import Journal
from agentd.logging_setup import get_logger
from agentd.memory import MemoryStore, find_repeated_approach
from agentd.rca import RcaEngine
from agentd.schemas import (
    CommitInfo,
    DebugReport,
    ErrorAnalysis,
    HealingIteration,
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
    commit: dict[str, Any] | None
    # ── self-healing state (Phase 2) ──
    iteration: int  # completed/entered DEBUG→FIX→REVALIDATE cycles
    healing: list[dict[str, Any]]  # HealingIteration records
    rca: list[dict[str, Any]]  # ErrorAnalysis of the latest failure
    signatures: list[str]  # combined signature per failed validation
    stalled: bool
    last_debug: dict[str, Any] | None  # DebugReport of the latest DEBUG
    repeat_warning: str | None  # memory: proposed fix repeats a failed one


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
        debugger: DebuggerAgent,
        browser_qa: BrowserQAAgent | None = None,
        rca_engine: RcaEngine | None = None,
        memory_agent: MemoryAgent | None = None,
        memory_store: MemoryStore | None = None,
    ) -> None:
        self.config = config
        self.workspace = workspace
        self.journal = journal
        self.planner = planner
        self.coder = coder
        self.validator = validator
        self.git_agent = git_agent
        self.debugger = debugger
        self.browser_qa = browser_qa
        self.rca = rca_engine or RcaEngine(config.limits.stall_threshold)
        self.memory_agent = memory_agent
        self.memory_store = memory_store
        self.graph = self._build()

    # ── graph wiring ─────────────────────────────────────────────────────────

    def _build(self):
        builder = StateGraph(RunState)
        builder.add_node("plan", self._plan_node)
        builder.add_node("code", self._code_node)
        builder.add_node("validate", self._validate_node)
        builder.add_node("debug", self._debug_node)
        builder.add_node("fix", self._fix_node)
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
            {"git": "git", "debug": "debug", "abort": "abort"},
        )
        builder.add_conditional_edges(
            "debug", self._route_after_debug, {"fix": "fix", "abort": "abort"}
        )
        builder.add_edge("fix", "validate")  # REVALIDATE
        builder.add_conditional_edges(
            "git", self._route_after_git, {"end": END, "abort": "abort"}
        )
        builder.add_edge("abort", END)
        return builder.compile()

    # ── nodes ────────────────────────────────────────────────────────────────

    def _plan_node(self, state: RunState) -> dict[str, Any]:
        self.journal.append("STATE_ENTERED", state="PLAN")
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
        self.journal.append("STATE_ENTERED", state="CODE", task=task.id)
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
        iteration = state.get("iteration", 0)
        state_name = "VALIDATE" if iteration == 0 else "REVALIDATE"
        self.journal.append("STATE_ENTERED", state=state_name, iteration=iteration)
        try:
            report = self.validator.run(self.workspace)
            # ── Browser QA stage (Phase 3): merged into the same verdict, so
            # the git gate and the self-healing loop cover UI failures too.
            if self.browser_qa is not None and self.config.browser_qa.enabled:
                if report.passed:
                    browser = self.browser_qa.run(self.workspace)
                else:
                    # Fail fast: don't launch the app on broken code. The
                    # stage still counts as NOT succeeded (commits blocked).
                    browser = skipped_report("command checks failed")
                    self.journal.append("BROWSER_QA", passed=False, skipped=True,
                                        summary=browser.summary)
                report = merge_validation(report, browser)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"validation crashed: {exc}", "status": "failed"}
        log.info("%s: %s", state_name.lower(), report.summary)
        update: dict[str, Any] = {"validation": report.model_dump()}

        # Close the observability record of the iteration we just revalidated.
        if iteration > 0 and state.get("last_debug"):
            update["healing"] = self._close_iteration(state, report)

        # Failed → run the RCA engine and update stall detection.
        if not report.passed:
            analyses = self.rca.analyze(report)
            signature = self.rca.combined_signature(analyses)
            signatures = list(state.get("signatures", [])) + [signature]
            stalled = self.rca.is_stalled(signatures)
            self.journal.append(
                "RCA_REPORT",
                iteration=iteration,
                categories=[a.category for a in analyses],
                signature=signature,
                locations=[loc for a in analyses for loc in a.locations][:10],
                stalled=stalled,
            )
            update.update(
                {
                    "rca": [a.model_dump() for a in analyses],
                    "signatures": signatures,
                    "stalled": stalled,
                }
            )
        return update

    def _close_iteration(
        self, state: RunState, report: ValidationReport
    ) -> list[dict[str, Any]]:
        iteration = state.get("iteration", 0)
        debug_report = DebugReport.model_validate(state["last_debug"])
        signatures = state.get("signatures", [])
        analyses = [ErrorAnalysis.model_validate(a) for a in state.get("rca", [])]
        fix_results = [
            r for r in state.get("task_results", [])
            if r.get("task_id") == f"HEAL{iteration}"
        ]
        record = HealingIteration(
            iteration=iteration,
            error_signature=signatures[-1] if signatures else "",
            categories=sorted({a.category for a in analyses}),
            root_cause=debug_report.root_cause,
            confidence=debug_report.confidence,
            approach=debug_report.fix_strategy.approach,
            fix_task_id=f"HEAL{iteration}",
            fix_status=fix_results[-1]["status"] if fix_results else "missing",
            revalidation_passed=report.passed,
        )
        self.journal.append(
            "HEAL_ITERATION",
            iteration=iteration,
            passed=report.passed,
            root_cause=record.root_cause,
            fix_status=record.fix_status,
        )
        log.info(
            "healing iteration %d/%d: %s",
            iteration,
            self.config.limits.max_heal_iterations,
            "revalidation PASSED" if report.passed else "still failing",
        )
        return list(state.get("healing", [])) + [record.model_dump()]

    def _debug_node(self, state: RunState) -> dict[str, Any]:
        iteration = state.get("iteration", 0) + 1
        self.journal.append("STATE_ENTERED", state="DEBUG", iteration=iteration)
        log.info("debugging (iteration %d/%d)", iteration,
                 self.config.limits.max_heal_iterations)
        plan = Plan.model_validate(state["plan"])
        report = ValidationReport.model_validate(state["validation"])
        analyses = [ErrorAnalysis.model_validate(a) for a in state.get("rca", [])]
        history = [HealingIteration.model_validate(h)
                   for h in state.get("healing", [])]
        try:
            debug_report = self.debugger.run(
                plan.goal, report, analyses, history, self.workspace
            )
        except Exception as exc:  # noqa: BLE001
            log.error("debugging failed: %s", exc)
            return {"error": f"debugging failed: {exc}", "status": "failed",
                    "iteration": iteration}
        log.info("root cause (%s confidence): %s", debug_report.confidence,
                 debug_report.root_cause[:120])
        update: dict[str, Any] = {"last_debug": debug_report.model_dump(),
                                  "iteration": iteration}
        # "Avoid repeating previous mistakes": detect a proposed approach
        # that already failed for this exact failure in a previous RUN.
        if self.memory_store is not None:
            signatures = [a.signature for a in analyses]
            repeated = find_repeated_approach(
                self.memory_store, signatures, debug_report.fix_strategy.approach
            )
            if repeated is not None:
                warning = (
                    f"WARNING: a nearly identical fix approach already FAILED "
                    f"in run {repeated.run_id} "
                    f"('{repeated.data.get('approach', repeated.title)[:150]}'). "
                    "Re-examine the diagnosis before applying; if you proceed, "
                    "the implementation must differ substantively."
                )
                self.journal.append(
                    "MEMORY_REPEAT_WARNING",
                    previous_run=repeated.run_id,
                    previous_approach=repeated.data.get("approach", "")[:200],
                    proposed_approach=debug_report.fix_strategy.approach[:200],
                )
                log.warning("memory: proposed fix repeats a failed approach "
                            "from run %s", repeated.run_id)
                update["repeat_warning"] = warning
        return update

    def _fix_node(self, state: RunState) -> dict[str, Any]:
        iteration = state.get("iteration", 0)
        self.journal.append("STATE_ENTERED", state="FIX", iteration=iteration)
        plan = Plan.model_validate(state["plan"])
        debug_report = DebugReport.model_validate(state["last_debug"])
        task = self._fix_task(iteration, debug_report,
                              state.get("repeat_warning"))
        plan = plan.model_copy(update={"tasks": list(plan.tasks) + [task]})
        log.info("applying fix %s: %s", task.id,
                 debug_report.fix_strategy.approach[:100])
        try:
            result = self.coder.run(plan, task, self.workspace)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"fix {task.id} crashed: {exc}", "status": "failed"}
        self.journal.append(
            "FIX_APPLIED",
            task=task.id,
            status=result.status,
            files_changed=result.files_changed,
        )
        results = list(state.get("task_results", [])) + [result.model_dump()]
        # A failed fix attempt does NOT abort the run: revalidation will fail
        # and the next DEBUG iteration sees the failed attempt in its history
        # (bounded by max_heal_iterations / stall detection).
        return {"plan": plan.model_dump(), "task_results": results,
                "repeat_warning": None}

    @staticmethod
    def _fix_task(iteration: int, debug_report: DebugReport,
                  repeat_warning: str | None = None) -> PlanTask:
        strategy = debug_report.fix_strategy
        steps = "\n".join(f"- {s}" for s in strategy.steps)
        evidence = "\n".join(f"- {e}" for e in debug_report.evidence[:6])
        intent = (
            f"Apply the fix for a diagnosed root cause "
            f"(category: {debug_report.category}, "
            f"confidence: {debug_report.confidence}).\n\n"
            f"Root cause: {debug_report.root_cause}\n"
            f"Why it is the cause, not the symptom: "
            f"{debug_report.why_root_cause or 'n/a'}\n\n"
            f"Repair approach: {strategy.approach}\n"
            f"Steps:\n{steps}\n\n"
            f"Evidence from debugging:\n{evidence or '- (see validation output)'}\n\n"
            "Fix the root cause exactly as diagnosed. Do NOT weaken or delete "
            "tests/checks, and do not silence errors."
        )
        if repeat_warning:
            intent += f"\n\n{repeat_warning}"
        return PlanTask(
            id=f"HEAL{iteration}",
            intent=intent,
            files_hint=strategy.files_to_change or debug_report.affected_files,
            check="all configured validation checks pass",
            kind="fix",
        )

    def _git_node(self, state: RunState) -> dict[str, Any]:
        self.journal.append("STATE_ENTERED", state="GIT")
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
        error = state.get("error") or self._derive_abort_error(state)
        self.journal.append("RUN_TERMINAL", status="failed", error=error,
                            iterations=state.get("iteration", 0))
        log.error("run failed: %s", error)
        return {"status": "failed", "error": error}

    def _derive_abort_error(self, state: RunState) -> str:
        validation = state.get("validation") or {}
        if validation and not validation.get("passed", True):
            iteration = state.get("iteration", 0)
            if state.get("stalled"):
                return (
                    "no progress: the identical failure signature persisted "
                    f"across {self.config.limits.stall_threshold} consecutive "
                    f"validations ({iteration} debug/fix iteration(s) did not "
                    "address the root cause) — stopping instead of patching "
                    "symptoms"
                )
            if iteration >= self.config.limits.max_heal_iterations:
                return (
                    "self-healing budget exhausted: validation still failing "
                    f"after {iteration} debug/fix iteration(s) "
                    f"(max {self.config.limits.max_heal_iterations})"
                )
            return f"validation failed: {validation.get('summary', '')}"
        return "aborted"

    # ── routes (read validated state only — never model output) ─────────────

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

    def _route_after_validate(self, state: RunState) -> Literal["git", "debug", "abort"]:
        if state.get("status") == "failed":
            return "abort"
        validation = state.get("validation") or {}
        if validation.get("passed"):
            return "git"  # SUCCESS path
        if state.get("stalled"):
            return "abort"
        if state.get("iteration", 0) >= self.config.limits.max_heal_iterations:
            return "abort"
        return "debug"

    def _route_after_debug(self, state: RunState) -> Literal["fix", "abort"]:
        if state.get("status") == "failed" or not state.get("last_debug"):
            return "abort"
        return "fix"

    def _route_after_git(self, state: RunState) -> Literal["end", "abort"]:
        if state.get("status") == "failed":
            return "abort"
        return "end"

    # ── entry point ──────────────────────────────────────────────────────────

    def run(self, run_id: str, request: str) -> RunReport:
        self.journal.append("RUN_SUBMITTED", run_id=run_id, request=request,
                            workspace=str(self.workspace.root),
                            branch=self.workspace.branch,
                            max_heal_iterations=self.config.limits.max_heal_iterations)
        initial: RunState = {
            "run_id": run_id,
            "request": request,
            "status": "running",
            "error": None,
            "plan": None,
            "task_index": 0,
            "task_results": [],
            "validation": None,
            "commit": None,
            "iteration": 0,
            "healing": [],
            "rca": [],
            "signatures": [],
            "stalled": False,
            "last_debug": None,
            "repeat_warning": None,
        }
        final: RunState = self.graph.invoke(
            initial, config={"recursion_limit": self.config.limits.recursion_limit}
        )
        status = "completed" if final.get("status") == "completed" else "failed"
        if status == "completed":
            self.journal.append("RUN_TERMINAL", status="completed",
                                iterations=final.get("iteration", 0))
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
            healing=[HealingIteration.model_validate(h)
                     for h in final.get("healing", [])],
            iterations_used=final.get("iteration", 0),
        )
        # Memory Agent learns from every terminal run — debugging attempts,
        # validation failures, successful repairs (Phase 4, ADR-017).
        if self.memory_agent is not None:
            try:
                self.memory_agent.record_run(report, self.workspace)
            except Exception as exc:  # noqa: BLE001 — memory must never fail a run
                log.warning("memory recording failed: %s", exc)
        return report
