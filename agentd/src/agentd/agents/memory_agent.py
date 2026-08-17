"""Memory Agent — persistent, per-repository learning (Phase 4, ADR-017).

Learns deterministically from every terminal run:

- **debugging attempts** → every DEBUG→FIX→REVALIDATE iteration whose
  revalidation still failed becomes a ``failed_fix`` memory (root cause,
  approach, error signature, category) — the raw material for "avoid
  repeating previous mistakes";
- **successful repairs** → iterations whose revalidation passed become
  ``successful_fix`` memories;
- **validation failures** without a successful heal (budget/stall aborts)
  are captured on the run's ``implementation`` record;
- every run leaves an ``implementation`` history record (goal, status,
  files, iterations, commit).

Optionally (``memory.distill: true``), after a completed run the agent asks
the LLM (``memory`` role) to distill up to three durable observations —
coding styles, project rules, architecture decisions — from the run
summary. Curated knowledge can also be added explicitly via
``ezai remember``.

After recording, the store is exported to ``.agent/lessons_learned.json``.
"""

from __future__ import annotations

from agentd.agents.base import BaseAgent
from agentd.agents.prompts import load_prompt
from agentd.memory import (
    CURATED_KINDS,
    KIND_FAILED_FIX,
    KIND_IMPLEMENTATION,
    KIND_SUCCESSFUL_FIX,
    MemoryStore,
)
from agentd.schemas import RunReport, extract_json_object
from agentd.workspace import Workspace


class MemoryAgent(BaseAgent):
    agent_name = "memory"
    role = "memory"
    tool_names: list[str] = []

    # ── recording (deterministic) ────────────────────────────────────────────

    def record_run(self, report: RunReport, workspace: Workspace) -> int:
        """Persist everything learnable from one run; returns records added."""
        store: MemoryStore | None = self.memory
        if store is None:
            return 0
        added = 0

        goal = report.plan.goal if report.plan else report.request
        for healing in report.healing:
            kind = (KIND_SUCCESSFUL_FIX if healing.revalidation_passed
                    else KIND_FAILED_FIX)
            outcome = ("revalidation passed" if healing.revalidation_passed
                       else f"revalidation still failed "
                            f"(fix task {healing.fix_status})")
            store.record(
                kind=kind,
                title=f"{'/'.join(healing.categories) or 'fix'}: "
                      f"{healing.root_cause[:150]}",
                content=(
                    f"root cause: {healing.root_cause}\n"
                    f"approach: {healing.approach}\n"
                    f"outcome: {outcome} (iteration {healing.iteration} of "
                    f"run {report.run_id}, goal: {goal})"
                ),
                run_id=report.run_id,
                error_signature=healing.error_signature,
                category=healing.categories[0] if healing.categories else "",
                data={
                    "approach": healing.approach,
                    "confidence": healing.confidence,
                    "fix_task_id": healing.fix_task_id,
                    "fix_status": healing.fix_status,
                    "iteration": healing.iteration,
                },
            )
            added += 1

        files = sorted({f for r in report.task_results for f in r.files_changed})
        summary_bits = [
            f"status: {report.status}",
            f"tasks: {', '.join(r.task_id for r in report.task_results) or 'none'}",
            f"healing iterations: {report.iterations_used}",
        ]
        if report.commit and report.commit.sha:
            summary_bits.append(f"commit: {report.commit.sha[:12]} on {report.commit.branch}")
        if report.error:
            summary_bits.append(f"error: {report.error[:300]}")
        if report.validation and not report.validation.passed:
            summary_bits.append(f"final validation: {report.validation.summary[:200]}")
        store.record(
            kind=KIND_IMPLEMENTATION,
            title=(report.plan.goal if report.plan else report.request)[:150],
            content="\n".join(summary_bits),
            run_id=report.run_id,
            files=files,
            data={"status": report.status, "iterations": report.iterations_used,
                  "branch": report.branch},
        )
        added += 1

        if (self.config.memory.distill and report.status == "completed"):
            added += self._distill(report, workspace, store)

        lessons = store.export_lessons()
        self.journal.append(
            "MEMORY_RECORDED",
            records=added,
            total=store.count(),
            db=str(store.db_path),
            lessons=str(lessons),
        )
        self.log.info("memory: %d record(s) added (%d total)", added, store.count())
        return added

    # ── optional LLM distillation ────────────────────────────────────────────

    def _distill(self, report: RunReport, workspace: Workspace,
                 store: MemoryStore) -> int:
        try:
            user = (
                f"Goal: {report.plan.goal if report.plan else report.request}\n"
                f"Tasks:\n" + "\n".join(
                    f"- {r.task_id}: {r.summary[:200]}" for r in report.task_results
                )
                + f"\nFiles changed: "
                  f"{', '.join(f for r in report.task_results for f in r.files_changed) or 'none'}"
            )
            outcome = self.run_loop(load_prompt("memory_distill"), user,
                                    workspace, max_turns=1)
            data = extract_json_object(outcome.final_text)
            observations = data.get("observations") or []
        except Exception as exc:  # noqa: BLE001 — distillation is best-effort
            self.log.warning("memory distillation skipped: %s", exc)
            return 0

        added = 0
        for obs in observations[:3]:
            kind = obs.get("kind", "")
            title = (obs.get("title") or "").strip()
            content = (obs.get("content") or "").strip()
            if kind in CURATED_KINDS and title and content:
                store.record(kind=kind, title=title, content=content,
                             run_id=report.run_id)
                added += 1
        if added:
            self.journal.append("MEMORY_DISTILLED", count=added)
        return added
