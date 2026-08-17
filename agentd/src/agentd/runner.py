"""Run assembly — wires config, workspace, journal, tools, agents, and the
graph together for one run. The CLI and the tests both enter here."""

from __future__ import annotations

import secrets
import time
from pathlib import Path

from agentd.agents import (
    BrowserQAAgent,
    CoderAgent,
    DebuggerAgent,
    GitAgent,
    PlannerAgent,
    ValidationAgent,
)
from agentd.agents.memory_agent import MemoryAgent
from agentd.config import AgentdConfig, load_repo_overrides, merge_repo_overrides
from agentd.graph import Orchestrator
from agentd.journal import Journal
from agentd.llm import LLMClient, build_llm
from agentd.logging_setup import get_logger
from agentd.memory import MemoryStore
from agentd.permissions import PermissionPolicy
from agentd.rca import RcaEngine
from agentd.schemas import Plan, RunReport
from agentd.tools import ALL_TOOL_CLASSES
from agentd.tools.base import ToolRegistry
from agentd.workspace import Workspace, create_workspace

log = get_logger("runner")


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def build_registry(config: AgentdConfig, journal: Journal) -> ToolRegistry:
    return ToolRegistry(
        tools=[cls() for cls in ALL_TOOL_CLASSES],
        policy=PermissionPolicy(config),
        journal=journal,
        max_output_chars=config.limits.tool_output_max_chars,
    )


def prepare_run(
    config: AgentdConfig, repo: Path, run_id: str
) -> tuple[AgentdConfig, Workspace, Journal]:
    """Apply repo overrides, create the workspace and the journal."""
    config = merge_repo_overrides(config, load_repo_overrides(repo))
    workspace = create_workspace(config, repo, run_id)
    # Re-read overrides from the workspace itself (worktree == repo content
    # at HEAD; a repo may carry .agentd.yaml only on the checked-out branch).
    config = merge_repo_overrides(config, load_repo_overrides(workspace.root))
    journal = Journal(config.runs_dir / run_id)
    return config, workspace, journal


def resolve_origin_root(repo: Path) -> Path:
    """The primary repository root, even when ``repo`` is a linked worktree
    (git worktrees share one object store; memory must live at the origin)."""
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute",
         "--git-common-dir"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        common = Path(result.stdout.strip())
        if common.name == ".git":
            return common.parent
    return Path(repo)


def build_memory_store(config: AgentdConfig, workspace: Workspace) -> MemoryStore | None:
    """Project memory lives in the ORIGIN repo's .agent/ — outside worktrees,
    persistent across runs (Phase 4, ADR-017). Resolves through linked
    worktrees so sprint tasks share the origin's memory."""
    if not config.memory.enabled:
        return None
    origin = resolve_origin_root(workspace.repo_path)
    return MemoryStore(origin / config.memory.dir)


def execute_run(
    config: AgentdConfig,
    repo: Path,
    request: str,
    llm: LLMClient | None = None,
    run_id: str | None = None,
) -> RunReport:
    """End-to-end run: workspace → graph → report."""
    run_id = run_id or new_run_id()
    config, workspace, journal = prepare_run(config, repo, run_id)
    llm = llm or build_llm(config.llm)
    registry = build_registry(config, journal)
    store = build_memory_store(config, workspace)

    try:
        orchestrator = Orchestrator(
            config=config,
            workspace=workspace,
            journal=journal,
            planner=PlannerAgent(config, llm, registry, journal, memory=store),
            coder=CoderAgent(config, llm, registry, journal),
            validator=ValidationAgent(config, llm, registry, journal),
            git_agent=GitAgent(config, llm, registry, journal),
            debugger=DebuggerAgent(config, llm, registry, journal, memory=store),
            browser_qa=BrowserQAAgent(config, llm, registry, journal),
            rca_engine=RcaEngine(config.limits.stall_threshold),
            memory_agent=(MemoryAgent(config, llm, registry, journal, memory=store)
                          if store is not None else None),
            memory_store=store,
        )
        log.info("run %s starting in %s (branch %s)", run_id, workspace.root,
                 workspace.branch)
        report = orchestrator.run(run_id, request)
    finally:
        if store is not None:
            store.close()
    (journal.run_dir / "report.json").write_text(
        report.model_dump_json(indent=2), encoding="utf-8"
    )
    return report


def _build_orchestrator(config, workspace, journal, llm, registry, store):
    return Orchestrator(
        config=config,
        workspace=workspace,
        journal=journal,
        planner=PlannerAgent(config, llm, registry, journal, memory=store),
        coder=CoderAgent(config, llm, registry, journal),
        validator=ValidationAgent(config, llm, registry, journal),
        git_agent=GitAgent(config, llm, registry, journal),
        debugger=DebuggerAgent(config, llm, registry, journal, memory=store),
        browser_qa=BrowserQAAgent(config, llm, registry, journal),
        rca_engine=RcaEngine(config.limits.stall_threshold),
        memory_agent=(MemoryAgent(config, llm, registry, journal, memory=store)
                      if store is not None else None),
        memory_store=store,
    )


def heal_run(
    config: AgentdConfig,
    repo: Path,
    goal: str = "repair failing validation checks",
    llm: LLMClient | None = None,
    run_id: str | None = None,
) -> RunReport:
    """`fix` pipeline: no planning/coding — enter at VALIDATE and drive the
    DEBUG → FIX → REVALIDATE loop until green, then commit. Operates
    **in place** on the repository's current branch."""
    from agentd.schemas import Plan

    run_id = run_id or new_run_id()
    config = config.model_copy(deep=True)
    config.workspace.mode = "in-place"
    config, workspace, journal = prepare_run(config, repo, run_id)
    llm = llm or build_llm(config.llm)
    registry = build_registry(config, journal)
    store = build_memory_store(config, workspace)
    try:
        orchestrator = _build_orchestrator(config, workspace, journal, llm,
                                           registry, store)
        log.info("fix run %s on %s (in place)", run_id, workspace.root)
        report = orchestrator.run(
            run_id, goal, initial_plan=Plan(goal=goal, tasks=[]),
            entry="validate",
        )
    finally:
        if store is not None:
            store.close()
    (journal.run_dir / "report.json").write_text(
        report.model_dump_json(indent=2), encoding="utf-8"
    )
    return report


def code_only(
    config: AgentdConfig,
    repo: Path,
    request: str,
    llm: LLMClient | None = None,
    run_id: str | None = None,
) -> RunReport:
    """`code` pipeline: plan + implement only — no validation, no commit.
    Changes are left uncommitted in the workspace (worktree by default)."""
    from agentd.schemas import TaskResult

    run_id = run_id or new_run_id()
    config, workspace, journal = prepare_run(config, repo, run_id)
    llm = llm or build_llm(config.llm)
    registry = build_registry(config, journal)
    store = build_memory_store(config, workspace)
    journal.append("RUN_SUBMITTED", run_id=run_id, request=request, mode="code-only")

    error: str | None = None
    results: list[TaskResult] = []
    plan = None
    try:
        planner = PlannerAgent(config, llm, registry, journal, memory=store)
        coder = CoderAgent(config, llm, registry, journal)
        plan = planner.run(request, workspace)
        for task in plan.tasks:
            result = coder.run(plan, task, workspace)
            results.append(result)
            if result.status == "failed":
                error = f"task {task.id} failed: {result.summary[:300]}"
                break
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        error = str(exc)
    status = "failed" if error else "completed"
    journal.append("RUN_TERMINAL", status=status, mode="code-only", error=error)

    report = RunReport(
        run_id=run_id, status=status, request=request,
        repo_path=str(workspace.repo_path), workspace_path=str(workspace.root),
        branch=workspace.branch, error=error, plan=plan,
        task_results=results, journal_path=str(journal.path),
    )
    if store is not None:
        try:
            MemoryAgent(config, llm, registry, journal,
                        memory=store).record_run(report, workspace)
        finally:
            store.close()
    (journal.run_dir / "report.json").write_text(
        report.model_dump_json(indent=2), encoding="utf-8"
    )
    return report


def validate_repo(
    config: AgentdConfig,
    repo: Path,
    llm: LLMClient | None = None,
    run_id: str | None = None,
):
    """`test` pipeline: run validation (commands + Browser QA) in place and
    return the merged ValidationReport. Read-only with respect to git."""
    from agentd.browser_qa import merge_validation, skipped_report

    run_id = run_id or new_run_id()
    config = config.model_copy(deep=True)
    config.workspace.mode = "in-place"
    config, workspace, journal = prepare_run(config, repo, run_id)
    llm = llm or build_llm(config.llm)
    registry = build_registry(config, journal)
    journal.append("RUN_SUBMITTED", run_id=run_id, mode="validate-only")

    validator = ValidationAgent(config, llm, registry, journal)
    report = validator.run(workspace)
    if config.browser_qa.enabled:
        browser_agent = BrowserQAAgent(config, llm, registry, journal)
        browser = (browser_agent.run(workspace) if report.passed
                   else skipped_report("command checks failed"))
        report = merge_validation(report, browser)
    journal.append("RUN_TERMINAL", status="completed", mode="validate-only",
                   passed=report.passed)
    return report, journal


def commit_repo(
    config: AgentdConfig,
    repo: Path,
    message: str | None = None,
    llm: LLMClient | None = None,
    run_id: str | None = None,
):
    """`commit` pipeline: validate the working tree (commands + Browser QA);
    only a fully green validation reaches the Git Agent (the commit gate
    raises otherwise). In place, on the current branch."""
    from agentd.browser_qa import merge_validation, skipped_report
    from agentd.schemas import Plan

    run_id = run_id or new_run_id()
    config = config.model_copy(deep=True)
    config.workspace.mode = "in-place"
    config, workspace, journal = prepare_run(config, repo, run_id)
    llm = llm or build_llm(config.llm)
    registry = build_registry(config, journal)
    journal.append("RUN_SUBMITTED", run_id=run_id, mode="commit-only")

    report = ValidationAgent(config, llm, registry, journal).run(workspace)
    if config.browser_qa.enabled:
        browser = (BrowserQAAgent(config, llm, registry, journal).run(workspace)
                   if report.passed else skipped_report("command checks failed"))
        report = merge_validation(report, browser)

    git_agent = GitAgent(config, llm, registry, journal)
    plan = Plan(goal=message or "commit working tree changes", tasks=[])
    # Raises "commit blocked" when validation (incl. Browser QA) failed.
    info = git_agent.run(workspace, plan, [], report, run_id)
    journal.append("RUN_TERMINAL", status="completed", mode="commit-only",
                   sha=info.sha)
    return info, report


def review_repo(
    config: AgentdConfig,
    repo: Path,
    llm: LLMClient | None = None,
    run_id: str | None = None,
):
    """`review` pipeline: adversarial review of the working-tree diff
    (or, when the tree is clean, the last commit)."""
    from agentd.agents.reviewer import ReviewerAgent
    from agentd.tools.git import git_run

    run_id = run_id or new_run_id()
    config = config.model_copy(deep=True)
    config.workspace.mode = "in-place"
    config, workspace, journal = prepare_run(config, repo, run_id)
    llm = llm or build_llm(config.llm)
    registry = build_registry(config, journal)
    store = build_memory_store(config, workspace)
    journal.append("RUN_SUBMITTED", run_id=run_id, mode="review-only")

    diff = git_run(workspace, "diff", "HEAD")
    context = "working-tree changes"
    if diff.ok and not diff.output.strip():
        diff = git_run(workspace, "show", "--format=commit %h %s", "HEAD")
        context = "the last commit (working tree is clean)"
    try:
        reviewer = ReviewerAgent(config, llm, registry, journal, memory=store)
        review = reviewer.run(diff.output, workspace, context=context)
    finally:
        if store is not None:
            store.close()
    journal.append("RUN_TERMINAL", status="completed", mode="review-only",
                   verdict=review.verdict)
    return review, journal


def run_sprint(
    config: AgentdConfig,
    repo: Path,
    tasks: list[str],
    spec_file: str = "",
    llm: LLMClient | None = None,
    sprint_id: str | None = None,
    keep_going: bool = False,
):
    """`sprint` pipeline: one shared worktree on branch sprint/<id>; each
    task runs the full pipeline in place on that worktree, committing
    sequentially so tasks build on each other."""
    from agentd.schemas import SprintReport, SprintTaskResult

    sprint_id = sprint_id or new_run_id()
    sprint_config = config.model_copy(deep=True)
    sprint_config.git.branch_prefix = "sprint/"
    workspace = create_workspace(sprint_config, Path(repo).resolve(), sprint_id)
    # One LLM client for the whole sprint: a scripted provider is consumed
    # sequentially across tasks, and a real client is simply reused.
    llm = llm or build_llm(config.llm)
    log.info("sprint %s: %d task(s) on %s", sprint_id, len(tasks), workspace.branch)

    results: list[SprintTaskResult] = []
    failed = False
    for index, task in enumerate(tasks, start=1):
        if failed and not keep_going:
            results.append(SprintTaskResult(index=index, task=task,
                                            run_id="", status="skipped"))
            continue
        task_config = config.model_copy(deep=True)
        task_config.workspace.mode = "in-place"
        run_id = f"{sprint_id}-t{index}"
        report = execute_run(task_config, workspace.root, task,
                             llm=llm, run_id=run_id)
        results.append(SprintTaskResult(
            index=index, task=task, run_id=run_id,
            status="completed" if report.status == "completed" else "failed",
            commit_sha=report.commit.sha if report.commit else "",
            error=report.error,
            iterations_used=report.iterations_used,
        ))
        if report.status != "completed":
            failed = True

    return SprintReport(
        sprint_id=sprint_id,
        status="failed" if failed else "completed",
        branch=workspace.branch,
        workspace_path=str(workspace.root),
        spec_file=spec_file,
        tasks=results,
    )


def plan_only(
    config: AgentdConfig,
    repo: Path,
    request: str,
    llm: LLMClient | None = None,
    run_id: str | None = None,
) -> Plan:
    """A0 dry-run: produce and return the plan without executing anything.

    Runs in-place (the Planner is read-only), so no branch or worktree is
    created — a plan leaves zero traces in the repository.
    """
    run_id = run_id or new_run_id()
    config = config.model_copy(deep=True)
    config.workspace.mode = "in-place"
    config, workspace, journal = prepare_run(config, repo, run_id)
    llm = llm or build_llm(config.llm)
    registry = build_registry(config, journal)
    # Memory is read-only here (lazy store: no .agent/ is created by reads).
    store = build_memory_store(config, workspace)
    planner = PlannerAgent(config, llm, registry, journal, memory=store)
    journal.append("RUN_SUBMITTED", run_id=run_id, request=request, mode="plan-only")
    try:
        plan = planner.run(request, workspace)
    finally:
        if store is not None:
            store.close()
    journal.append("RUN_TERMINAL", status="completed", mode="plan-only")
    return plan
