"""Autonomous sprint execution — parallel multi-agent collaboration
(Phase 6, ADR-019).

Pipeline over one sprint spec:

1. **Requirement analysis / task breakdown / dependency graph** — the
   Sprint Agent turns ``sprint.md`` into a validated :class:`SprintPlan`.
2. **Wave scheduling** (deterministic): tasks are grouped into topological
   waves; tasks within a wave are independent.
3. **Parallel agent execution**: a single-task wave runs in place on the
   sprint worktree; a multi-task wave runs each task in its **own worktree
   branched from the sprint tip**, concurrently (thread pool, bounded by
   ``sprint.max_parallel``), then merges the task branches back into the
   sprint branch in plan order. Every task is a full pipeline — Planner,
   Coder, **Validation**, **Browser QA**, self-healing, Memory, and the
   commit gate — so nothing merges without passing its checks.
4. Tasks whose dependencies failed are **skipped**; a merge conflict marks
   the task failed (documented limitation: parallel tasks should touch
   disjoint files — the Sprint Agent's dependency graph is what prevents
   collisions).
5. **Documentation**: ``docs/sprints/sprint-<id>.md`` (goal, requirements,
   mermaid dependency graph, per-task outcomes) is generated and — when the
   sprint is green — committed as the final commit on the sprint branch.

LLM sharing: pass ``llm`` for one shared client (real HTTP clients are
thread-safe) or ``llm_factory(task_spec)`` for per-task clients (required
for scripted/stateful providers in parallel waves).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agentd.config import AgentdConfig
from agentd.journal import Journal
from agentd.llm import LLMClient, build_llm
from agentd.logging_setup import get_logger
from agentd.runner import build_memory_store, build_registry, execute_run, new_run_id
from agentd.schemas import (
    RunReport,
    SprintPlan,
    SprintReport,
    SprintTaskResult,
    SprintTaskSpec,
)
from agentd.sprint import topological_waves
from agentd.tools.git import git_run
from agentd.workspace import Workspace, create_workspace

log = get_logger("sprint_exec")


def run_sprint_autonomous(
    config: AgentdConfig,
    repo: Path,
    spec_text: str,
    spec_file: str = "",
    llm: LLMClient | None = None,
    llm_factory: Callable[[SprintTaskSpec], LLMClient] | None = None,
    sprint_id: str | None = None,
    keep_going: bool = False,
) -> SprintReport:
    sprint_id = sprint_id or new_run_id()
    repo = Path(repo).resolve()

    sprint_config = config.model_copy(deep=True)
    sprint_config.git.branch_prefix = "sprint/"
    workspace = create_workspace(sprint_config, repo, sprint_id)
    journal = Journal(config.runs_dir / sprint_id)

    # LLM resolution: an explicit `llm` is shared everywhere; otherwise a
    # factory provides per-task clients (the analysis stage asks it with the
    # sentinel id "_analysis_"); otherwise one real client is built + shared.
    shared_llm = llm or (build_llm(config.llm) if llm_factory is None else None)

    def llm_for(task: SprintTaskSpec) -> LLMClient:
        if shared_llm is not None:
            return shared_llm
        return llm_factory(task)

    analyst_llm = shared_llm if shared_llm is not None else llm_factory(
        SprintTaskSpec(id="_analysis_", title="sprint analysis", description="-")
    )

    # ── 1. requirement analysis → plan with dependency graph ────────────────
    plan = _analyze(config, workspace, journal, spec_text, analyst_llm)
    waves = topological_waves(plan.tasks)
    journal.append("SPRINT_WAVES",
                   waves=[[t.id for t in wave] for wave in waves])
    log.info("sprint %s: %d task(s) in %d wave(s) on %s",
             sprint_id, len(plan.tasks), len(waves), workspace.branch)

    # ── 2/3. wave-by-wave execution ──────────────────────────────────────────
    results: dict[str, SprintTaskResult] = {}
    reports: dict[str, RunReport] = {}
    index_of = {t.id: i + 1 for i, t in enumerate(plan.tasks)}
    stop_scheduling = False

    for wave_number, wave in enumerate(waves, start=1):
        runnable: list[SprintTaskSpec] = []
        for task in wave:
            failed_deps = [d for d in task.depends_on
                           if results.get(d) is None
                           or results[d].status != "completed"]
            if stop_scheduling or failed_deps:
                reason = (
                    f"dependency failed/skipped: {', '.join(failed_deps)}"
                    if failed_deps else "sprint stopped after earlier failure"
                )
                results[task.id] = _skipped(task, index_of, wave_number, reason)
                journal.append("SPRINT_TASK", task=task.id, status="skipped",
                               reason=reason)
                continue
            runnable.append(task)

        if not runnable:
            continue
        journal.append("SPRINT_WAVE_STARTED", wave=wave_number,
                       tasks=[t.id for t in runnable],
                       parallel=len(runnable) > 1)

        if len(runnable) == 1:
            task = runnable[0]
            result, report = _run_in_sprint_worktree(
                config, workspace, task, sprint_id, index_of, wave_number,
                llm_for(task))
            results[task.id] = result
            if report is not None:
                reports[task.id] = report
        else:
            wave_results = _run_parallel_wave(
                config, repo, workspace, runnable, sprint_id, index_of,
                wave_number, llm_for, journal)
            for task_id, (result, report) in wave_results.items():
                results[task_id] = result
                if report is not None:
                    reports[task_id] = report

        for task in runnable:
            outcome = results[task.id]
            journal.append("SPRINT_TASK", task=task.id, status=outcome.status,
                           wave=wave_number, commit=outcome.commit_sha[:12],
                           merged=outcome.merged, error=outcome.error)
            if outcome.status != "completed" and not keep_going:
                stop_scheduling = True

    ordered = [results[t.id] for t in plan.tasks]
    status = ("completed"
              if all(r.status == "completed" for r in ordered) else "failed")
    report = SprintReport(
        sprint_id=sprint_id, status=status, branch=workspace.branch,
        workspace_path=str(workspace.root), spec_file=spec_file,
        plan=plan, waves=len(waves), tasks=ordered,
    )

    # ── 5. documentation ─────────────────────────────────────────────────────
    report.report_doc = _write_sprint_doc(workspace, report, reports, journal)

    journal.append("RUN_TERMINAL", status=status, mode="sprint",
                   completed=report.completed_count, total=len(ordered))
    (journal.run_dir / "report.json").write_text(
        report.model_dump_json(indent=2), encoding="utf-8")
    return report


# ── stages ────────────────────────────────────────────────────────────────────


def _analyze(config, workspace, journal, spec_text, llm) -> SprintPlan:
    from agentd.agents.sprint_agent import SprintAgent

    registry = build_registry(config, journal)
    store = build_memory_store(config, workspace)
    try:
        agent = SprintAgent(config, llm, registry, journal, memory=store)
        return agent.run(spec_text, workspace)
    finally:
        if store is not None:
            store.close()


def _run_in_sprint_worktree(config, workspace, task, sprint_id, index_of,
                            wave_number, llm):
    """Single-task wave: run in place on the sprint worktree."""
    task_config = config.model_copy(deep=True)
    task_config.workspace.mode = "in-place"
    run_id = f"{sprint_id}-{task.id.lower()}"
    report = execute_run(task_config, workspace.root, task.description,
                         llm=llm, run_id=run_id)
    return _to_result(task, report, run_id, index_of, wave_number), report


def _run_parallel_wave(config, repo, workspace, tasks, sprint_id, index_of,
                       wave_number, llm_for, journal):
    """Multi-task wave: one worktree per task branched from the sprint tip,
    concurrent execution, then ordered merge-back into the sprint branch."""
    branch_of: dict[str, str] = {}
    worktree_of: dict[str, Path] = {}
    for task in tasks:
        task_branch = f"{workspace.branch}-{task.id.lower()}"
        task_dir = Path(config.workspace.root) / f"{sprint_id}-{task.id.lower()}"
        task_dir.parent.mkdir(parents=True, exist_ok=True)
        result = git_run(
            Workspace(root=repo, repo_path=repo, branch="", mode="in-place"),
            "worktree", "add", "-b", task_branch, str(task_dir),
            workspace.branch,
        )
        if not result.ok:
            raise RuntimeError(f"could not create worktree for {task.id}: "
                               f"{result.output}")
        branch_of[task.id] = task_branch
        worktree_of[task.id] = task_dir

    outcomes: dict[str, tuple[SprintTaskResult, RunReport | None]] = {}
    lock = threading.Lock()

    def run_task(task: SprintTaskSpec) -> None:
        task_config = config.model_copy(deep=True)
        task_config.workspace.mode = "in-place"
        run_id = f"{sprint_id}-{task.id.lower()}"
        log.info("wave %d: task %s starting in parallel", wave_number, task.id)
        try:
            report = execute_run(task_config, worktree_of[task.id],
                                 task.description, llm=llm_for(task),
                                 run_id=run_id)
            outcome = _to_result(task, report, run_id, index_of, wave_number)
        except Exception as exc:  # noqa: BLE001 — a task crash fails the task
            log.error("task %s crashed: %s", task.id, exc)
            outcome, report = _failed(task, str(exc), run_id, index_of,
                                      wave_number), None
        with lock:
            outcomes[task.id] = (outcome, report)

    max_workers = max(1, config.sprint.max_parallel)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(pool.map(run_task, tasks))

    # Ordered merge-back into the sprint branch (plan order = tasks order).
    for task in tasks:
        outcome, _ = outcomes[task.id]
        if outcome.status == "completed" and outcome.commit_sha:
            merge = git_run(
                workspace,
                "-c", f"user.name={config.git.user_name}",
                "-c", f"user.email={config.git.user_email}",
                "merge", "--no-ff", "-m",
                f"merge sprint task {task.id}: {task.title[:60]}",
                branch_of[task.id],
            )
            if not merge.ok:
                git_run(workspace, "merge", "--abort")
                outcome.merged = False
                outcome.status = "failed"
                outcome.error = (
                    f"merge conflict integrating {task.id} — parallel tasks "
                    f"touched overlapping files: {merge.output[:300]}"
                )
                journal.append("SPRINT_MERGE_CONFLICT", task=task.id,
                               branch=branch_of[task.id])
        # cleanup: worktree away, branch away (merged commits survive)
        origin_ws = Workspace(root=repo, repo_path=repo, branch="", mode="in-place")
        git_run(origin_ws, "worktree", "remove", "--force",
                str(worktree_of[task.id]))
        git_run(origin_ws, "branch", "-D", branch_of[task.id])
    return outcomes


# ── documentation (capability 7) ─────────────────────────────────────────────


def _write_sprint_doc(workspace, report: SprintReport,
                      run_reports: dict[str, RunReport], journal) -> str:
    doc_rel = f"docs/sprints/sprint-{report.sprint_id}.md"
    doc_path = workspace.root / doc_rel
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(_render_sprint_doc(report, run_reports),
                        encoding="utf-8")
    journal.append("SPRINT_DOC", path=doc_rel, committed=False)

    if report.status == "completed":
        # Commit the documentation as the sprint's final commit. Validation
        # was green at the last task commit and the doc changes no code.
        add = git_run(workspace, "add", "--", doc_rel)
        commit = git_run(
            workspace,
            "-c", "user.name=agentd", "-c", "user.email=agentd@local-ezai",
            "commit", "-m",
            f"docs: sprint {report.sprint_id} report\n\n"
            f"Agentd-Sprint: {report.sprint_id}",
        )
        if add.ok and commit.ok:
            journal.append("SPRINT_DOC", path=doc_rel, committed=True)
    return doc_rel


def _render_sprint_doc(report: SprintReport,
                       run_reports: dict[str, RunReport]) -> str:
    plan = report.plan
    lines = [
        f"# Sprint {report.sprint_id}",
        "",
        f"**Goal:** {plan.goal if plan else '(unanalyzed)'}",
        f"**Status:** {report.status.upper()} — "
        f"{report.completed_count}/{len(report.tasks)} task(s) completed "
        f"in {report.waves} wave(s)",
        f"**Branch:** `{report.branch}`",
        "",
    ]
    if plan and plan.requirements:
        lines += ["## Requirements", ""]
        lines += [f"- {req}" for req in plan.requirements]
        lines.append("")
    if plan and plan.tasks:
        lines += ["## Dependency graph", "", "```mermaid", "flowchart TD"]
        for task in plan.tasks:
            lines.append(f'    {task.id}["{task.id}: {task.title[:40]}"]')
        for task in plan.tasks:
            for dep in task.depends_on:
                lines.append(f"    {dep} --> {task.id}")
        lines += ["```", ""]
    lines += ["## Tasks", ""]
    for outcome in report.tasks:
        run_report = run_reports.get(outcome.task_id)
        lines.append(f"### {outcome.task_id or outcome.index}: "
                     f"{outcome.task[:80]}")
        lines.append("")
        lines.append(f"- status: **{outcome.status}** (wave {outcome.wave})")
        if outcome.depends_on:
            lines.append(f"- depends on: {', '.join(outcome.depends_on)}")
        if outcome.commit_sha:
            merged = "" if outcome.merged else " — **merge failed**"
            lines.append(f"- commit: `{outcome.commit_sha[:12]}`{merged}")
        if run_report is not None:
            if run_report.validation:
                lines.append(f"- validation: {run_report.validation.summary}")
            if run_report.iterations_used:
                lines.append(f"- self-healing iterations: "
                             f"{run_report.iterations_used}")
            files = sorted({f for r in run_report.task_results
                            for f in r.files_changed})
            if files:
                lines.append(f"- files: {', '.join(files[:15])}")
        if outcome.error:
            lines.append(f"- error: {outcome.error[:300]}")
        lines.append("")
    if plan and plan.notes:
        lines += ["## Notes", "", plan.notes, ""]
    return "\n".join(lines)


# ── result helpers ────────────────────────────────────────────────────────────


def _to_result(task, report: RunReport, run_id, index_of,
               wave_number) -> SprintTaskResult:
    return SprintTaskResult(
        index=index_of[task.id], task=task.title, run_id=run_id,
        status="completed" if report.status == "completed" else "failed",
        task_id=task.id, wave=wave_number, depends_on=task.depends_on,
        commit_sha=report.commit.sha if report.commit else "",
        error=report.error, iterations_used=report.iterations_used,
    )


def _failed(task, error, run_id, index_of, wave_number) -> SprintTaskResult:
    return SprintTaskResult(
        index=index_of[task.id], task=task.title, run_id=run_id,
        status="failed", task_id=task.id, wave=wave_number,
        depends_on=task.depends_on, error=error,
    )


def _skipped(task, index_of, wave_number, reason) -> SprintTaskResult:
    return SprintTaskResult(
        index=index_of[task.id], task=task.title, run_id="",
        status="skipped", task_id=task.id, wave=wave_number,
        depends_on=task.depends_on, error=reason,
    )
