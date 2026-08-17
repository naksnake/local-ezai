"""The Evolution workflow — ``local-ezai evolve`` (CLAUDE.md, ADR-020).

    Analyze history → Analyze failures → Identify bottlenecks →
    Propose improvements → Implement → Validate → Benchmark →
    Create PR → **Human approval**

Mechanics:

1. **Evidence** is gathered deterministically from the platform's own
   records: project memory (implementation history, failed/successful
   fixes, repeated signatures), recent run reports, and the roadmap.
2. The **Evolution Agent** turns evidence into a schema-validated proposal
   (1–3 self-contained improvements).
3. Each improvement runs as a **full pipeline** (plan → code → validate →
   Browser QA → self-heal → gated commit) in place on one
   ``evolve/<id>`` worktree branch — improvements build on each other.
4. **Benchmark**: the full validation suite is timed on the branch before
   and after the improvements (evidence for the human reviewer that checks
   still pass and how long they take).
5. **Documentation**: a dated entry is prepended to
   ``docs/RELEASE_NOTES.md`` and committed (green cycles only).
6. **PR**: the branch is pushed only if ``git.allow_push`` permits (T3),
   and a pull request is created via the configured forge — or a local
   PR proposal bundle is written when no forge is configured. The workflow
   always terminates awaiting a human; it never merges.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from agentd.config import AgentdConfig, load_repo_overrides, merge_repo_overrides
from agentd.forge import create_pull_request
from agentd.journal import Journal
from agentd.llm import LLMClient, build_llm
from agentd.logging_setup import get_logger
from agentd.memory import FIX_KINDS, KIND_IMPLEMENTATION, MemoryStore
from agentd.model_registry import apply_model_registry
from agentd.runner import (
    build_memory_store,
    build_registry,
    execute_run,
    new_run_id,
    resolve_origin_root,
)
from agentd.schemas import (
    BenchmarkResult,
    EvolutionProposal,
    EvolutionReport,
    SprintTaskResult,
)
from agentd.tools.git import git_run
from agentd.workspace import Workspace, create_workspace

log = get_logger("evolution")

MAX_IMPROVEMENTS = 3


def run_evolution(
    config: AgentdConfig,
    repo: Path,
    focus: str = "",
    llm: LLMClient | None = None,
    evolution_id: str | None = None,
) -> EvolutionReport:
    evolution_id = evolution_id or new_run_id()
    repo = Path(repo).resolve()
    config = apply_model_registry(config, resolve_origin_root(repo))
    # Repo overrides (validation commands, limits, browser QA) apply to the
    # benchmark stage and the proposal budget, not only to the task runs.
    config = merge_repo_overrides(config, load_repo_overrides(repo))

    evolve_config = config.model_copy(deep=True)
    evolve_config.git.branch_prefix = "evolve/"
    workspace = create_workspace(evolve_config, repo, evolution_id)
    journal = Journal(config.runs_dir / evolution_id)
    llm = llm or build_llm(config.llm)

    # ── 1. analyze history / failures / bottleneck evidence ─────────────────
    evidence = gather_evidence(config, repo)
    journal.append("EVOLUTION_EVIDENCE", chars=len(evidence))

    # ── 2. propose ───────────────────────────────────────────────────────────
    proposal = _propose(config, workspace, journal, llm, evidence, focus)
    improvements = proposal.improvements[:MAX_IMPROVEMENTS]
    if len(proposal.improvements) > MAX_IMPROVEMENTS:
        journal.append("EVOLUTION_TRIMMED",
                       from_count=len(proposal.improvements),
                       to_count=MAX_IMPROVEMENTS)

    # ── 4a. baseline benchmark (before any change) ───────────────────────────
    benchmark_before = _benchmark(config, workspace, llm, journal, "before")

    # ── 3. implement (full pipeline per improvement, sequential) ─────────────
    tasks: list[SprintTaskResult] = []
    failed = False
    for index, improvement in enumerate(improvements, start=1):
        if failed:
            tasks.append(SprintTaskResult(
                index=index, task=improvement.title, run_id="",
                status="skipped", task_id=improvement.id,
                error="evolution stopped after earlier failure"))
            continue
        task_config = config.model_copy(deep=True)
        task_config.workspace.mode = "in-place"
        run_id = f"{evolution_id}-{improvement.id.lower()}"
        report = execute_run(task_config, workspace.root,
                             improvement.description, llm=llm, run_id=run_id)
        tasks.append(SprintTaskResult(
            index=index, task=improvement.title, run_id=run_id,
            status="completed" if report.status == "completed" else "failed",
            task_id=improvement.id,
            commit_sha=report.commit.sha if report.commit else "",
            error=report.error,
            iterations_used=report.iterations_used,
        ))
        journal.append("EVOLUTION_TASK", improvement=improvement.id,
                       status=tasks[-1].status, commit=tasks[-1].commit_sha[:12])
        if report.status != "completed":
            failed = True

    status = "failed" if failed else "completed"

    # ── 4b. post benchmark ────────────────────────────────────────────────────
    benchmark_after = _benchmark(config, workspace, llm, journal, "after")

    # ── 5. documentation (release notes) — green cycles only ────────────────
    release_notes_updated = False
    if status == "completed":
        release_notes_updated = _update_release_notes(
            workspace, evolution_id, proposal, tasks, journal)

    # ── 6. pull request / proposal bundle — human approval is terminal ───────
    pull_request = None
    if status == "completed":
        pushed_note = ""
        if config.git.allow_push:
            push = git_run(workspace, "push", "-u", config.git.remote,
                           workspace.branch, timeout=300.0)
            pushed_note = ("pushed" if push.ok
                           else f"push failed: {push.output[:150]}")
            journal.append("EVOLUTION_PUSH", ok=push.ok)
        pull_request = create_pull_request(
            config, workspace.root, workspace.branch,
            title=f"evolve: {proposal.title}",
            body=_pr_body(proposal, tasks, benchmark_before, benchmark_after,
                          pushed_note),
            out_dir=journal.run_dir,
        )
        journal.append("EVOLUTION_PR", created=pull_request.created,
                       url=pull_request.url, bundle=pull_request.bundle_path,
                       note=pull_request.note)
    else:
        journal.append("EVOLUTION_PR", created=False,
                       note="skipped — evolution cycle failed")

    report = EvolutionReport(
        evolution_id=evolution_id, status=status, branch=workspace.branch,
        workspace_path=str(workspace.root), proposal=proposal, tasks=tasks,
        benchmark_before=benchmark_before, benchmark_after=benchmark_after,
        release_notes_updated=release_notes_updated,
        pull_request=pull_request,
        error=next((t.error for t in tasks if t.status == "failed"), None),
    )
    journal.append("RUN_TERMINAL", status=status, mode="evolution")
    (journal.run_dir / "report.json").write_text(
        report.model_dump_json(indent=2), encoding="utf-8")
    return report


# ── evidence (deterministic) ──────────────────────────────────────────────────


def gather_evidence(config: AgentdConfig, repo: Path) -> str:
    """History, failures, and run outcomes from the platform's own records."""
    sections: list[str] = []
    origin = resolve_origin_root(repo)
    store = MemoryStore(origin / config.memory.dir)
    try:
        if store.exists:
            history = store.recent([KIND_IMPLEMENTATION], limit=15)
            sections.append("Implementation history (most recent first):\n"
                            + "\n".join(f"- {r.title}: "
                                        f"{r.content.splitlines()[0][:120]}"
                                        for r in history))
            failed = [r for r in store.recent(list(FIX_KINDS), limit=40)
                      if r.kind == "failed_fix"]
            if failed:
                signatures: dict[str, int] = {}
                for record in failed:
                    signatures[record.error_signature] = (
                        signatures.get(record.error_signature, 0) + 1)
                repeated = {s: n for s, n in signatures.items() if n > 1}
                sections.append(
                    "Failed fixes (approach → signature):\n"
                    + "\n".join(f"- {r.data.get('approach', r.title)[:100]} "
                                f"→ {r.error_signature[:80]}"
                                for r in failed[:10]))
                if repeated:
                    sections.append(
                        "REPEATED failure signatures (patterns):\n"
                        + "\n".join(f"- x{n}: {s[:100]}"
                                    for s, n in repeated.items()))
        else:
            sections.append("(no project memory yet)")
    finally:
        store.close()

    runs_dir = Path(config.runs_dir)
    if runs_dir.is_dir():
        outcomes = []
        for run_dir in sorted(runs_dir.iterdir(), reverse=True)[:20]:
            report_file = run_dir / "report.json"
            if report_file.is_file():
                try:
                    data = json.loads(report_file.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                outcomes.append(
                    f"- {run_dir.name}: {data.get('status', '?')}, "
                    f"iterations={data.get('iterations_used', 0)}"
                    + (f", error: {data.get('error', '')[:80]}"
                       if data.get("error") else ""))
        if outcomes:
            sections.append("Recent runs:\n" + "\n".join(outcomes))

    roadmap = origin / config.memory.dir / "roadmap.md"
    if roadmap.is_file():
        sections.append("Roadmap (head):\n"
                        + "\n".join(roadmap.read_text(encoding="utf-8")
                                    .splitlines()[:40]))
    return "\n\n".join(sections)


# ── stages ────────────────────────────────────────────────────────────────────


def _propose(config, workspace, journal, llm, evidence, focus) -> EvolutionProposal:
    from agentd.agents.evolution_agent import EvolutionAgent

    registry = build_registry(config, journal)
    store = build_memory_store(config, workspace)
    try:
        agent = EvolutionAgent(config, llm, registry, journal, memory=store)
        return agent.run(evidence, workspace, focus=focus)
    finally:
        if store is not None:
            store.close()


def _benchmark(config, workspace: Workspace, llm, journal,
               label: str) -> BenchmarkResult:
    """Timed full validation on the evolve worktree (commands + Browser QA)."""
    from agentd.agents import BrowserQAAgent, ValidationAgent
    from agentd.browser_qa import merge_validation, skipped_report

    registry = build_registry(config, journal)
    started = time.monotonic()
    validator = ValidationAgent(config, llm, registry, journal)
    report = validator.run(workspace)
    if config.browser_qa.enabled:
        browser = (BrowserQAAgent(config, llm, registry, journal).run(workspace)
                   if report.passed else skipped_report("command checks failed"))
        report = merge_validation(report, browser)
    duration = time.monotonic() - started
    result = BenchmarkResult(passed=report.passed, checks=len(report.checks),
                             duration_seconds=round(duration, 2))
    journal.append("EVOLUTION_BENCHMARK", label=label,
                   passed=result.passed, checks=result.checks,
                   seconds=result.duration_seconds)
    return result


def _update_release_notes(workspace: Workspace, evolution_id: str,
                          proposal: EvolutionProposal,
                          tasks: list[SprintTaskResult], journal) -> bool:
    path = workspace.root / "docs" / "RELEASE_NOTES.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = (
        f"## {date} — evolution {evolution_id}: {proposal.title}\n\n"
        + "\n".join(f"- {t.task_id}: {t.task} "
                    f"(`{t.commit_sha[:10]}`)" for t in tasks
                    if t.status == "completed")
        + "\n\n"
    )
    if path.is_file():
        existing = path.read_text(encoding="utf-8")
        lines = existing.splitlines(keepends=True)
        # keep a leading H1 title if present, prepend the entry below it
        if lines and lines[0].startswith("# "):
            head = "".join(lines[:1]) + "\n"
            body = "".join(lines[1:]).lstrip("\n")
            path.write_text(head + entry + body, encoding="utf-8")
        else:
            path.write_text(entry + existing, encoding="utf-8")
    else:
        path.write_text("# Release Notes\n\n" + entry, encoding="utf-8")

    add = git_run(workspace, "add", "--", "docs/RELEASE_NOTES.md")
    commit = git_run(
        workspace, "-c", "user.name=agentd", "-c", "user.email=agentd@local-ezai",
        "commit", "-m",
        f"docs: release notes for evolution {evolution_id}\n\n"
        f"Agentd-Evolution: {evolution_id}",
    )
    ok = add.ok and commit.ok
    journal.append("EVOLUTION_RELEASE_NOTES", committed=ok)
    return ok


def _pr_body(proposal: EvolutionProposal, tasks, before: BenchmarkResult,
             after: BenchmarkResult, pushed_note: str) -> str:
    patterns = [f"- {p}" for p in proposal.failure_patterns] or ["- (none listed)"]
    bottlenecks = [f"- {b}" for b in proposal.bottlenecks] or ["- (none listed)"]
    lines = [
        "Autonomous evolution cycle. **Human review and approval required "
        "before merge.**",
        "",
        f"**History:** {proposal.history_summary}",
        "",
        "**Failure patterns addressed:**",
        *patterns,
        "",
        "**Bottlenecks identified:**",
        *bottlenecks,
        "",
        "**Improvements:**",
        *[f"- {t.task_id}: {t.task} — {t.status}"
          + (f" (`{t.commit_sha[:10]}`)" if t.commit_sha else "")
          for t in tasks],
        "",
        "**Benchmark (full validation):**",
        f"- before: {'PASS' if before.passed else 'FAIL'}, "
        f"{before.checks} check(s), {before.duration_seconds}s",
        f"- after:  {'PASS' if after.passed else 'FAIL'}, "
        f"{after.checks} check(s), {after.duration_seconds}s",
    ]
    if pushed_note:
        lines += ["", f"Branch: {pushed_note}"]
    if proposal.notes:
        lines += ["", f"Notes: {proposal.notes}"]
    return "\n".join(lines)
