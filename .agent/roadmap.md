# Roadmap — Local Autonomous Software Engineer Platform

> Living document. Update the status column in the same PR that changes it.
> Full phase detail: [docs/MIGRATION_PLAN.md](../docs/MIGRATION_PLAN.md).

**North star:** submit a software task in natural language; the platform
plans, edits in a sandboxed git worktree, verifies with the project's own
tests, self-reviews, and delivers a branch/PR — entirely on local hardware.

**Definition of done (v1):** DoD scenario in
[docs/TARGET_ARCHITECTURE.md](../docs/TARGET_ARCHITECTURE.md) §13.

## Milestones

| ID | Milestone | Phase | Status | Exit criterion (short) |
|----|-----------|-------|--------|------------------------|
| M0 | Architecture foundation | P0 | ✅ done (2026-08-14) | docs/ + .agent/ merged; ADR process active |
| M1 | Agent runtime core | P1 | ✅ done (2026-08-14) — shipped as the `agentd/` MVP (Planner/Coder/Validator/Git agents, LangGraph graph, tool layer, journal, CLI, 87 offline tests, CI). Deviations recorded: ADR-013 (LangGraph), ADR-014 (interim isolation) | read-only research run over a repo via `ezai` CLI; resumable journal; CI green; fail-closed permission proof |
| M2 | Execution plane | P2 | 🟡 partially (interim) — worktree isolation + policed host exec shipped ahead of schedule under ADR-014; **container sandboxing + egress policy still open** | sandboxed edit+test+commit on `swe/<run>`; host untouched; egress default-deny; T3 asks |
| M3 | First autonomous fix | P3 | 🟡 mostly — plan→code→validate→**self-healing debug loop**→commit works end-to-end (ADR-015: RCA engine, read-only Debug Agent, max 10 iterations, stall detection; 128 offline tests); full gate/BLOCKED machine and journal-replay resume still open | seeded-bug fixture fixed end-to-end at A2 (GPU); clean BLOCKED behavior at A1 (N97) |
| M4 | Multi-agent quality | P4 | 🟡 mostly — Debugger (ADR-015), Browser QA (ADR-016), Memory Curator (ADR-017), **Reviewer** (ADR-018), and **multi-agent parallel collaboration** (ADR-019: Sprint Agent DAG + wave scheduler + worktree-per-task concurrency) shipped; Context/Research agent + reviewer-in-run-pipeline gate still open | measurable quality delta on 10-task fixture suite; reviewer catches seeded regression |
| M5 | Code intelligence & memory | P5 | 🟡 partially — per-repo project memory shipped (ADR-017: SQLite `.agent/memory.db` + lessons export, deterministic learning from debug/validation/repair outcomes, planner+debugger injection, repeat-mistake detection; 190 tests). **Semantic code retrieval (codeidx/Qdrant) still open** | hybrid code retrieval live; tokens/run down vs M4; curator memory proposal merged via review |
| M6 | Interfaces & A3 delivery | P6 | 🟡 mostly — production CLI `local-ezai` (ADR-018), **autonomous sprint execution** (ADR-019), and **PR/forge delivery** (ADR-020: forge none/gh/api, evolution PRs) shipped. Web console + chat-ops MCP tools still open | console gate approvals; chat-ops tools; PR opened on LAN forge at A3 |
| M6.5 | Self-sustainability & governance | P7 | ✅ done (2026-08-17) — ADR-020: model registry + fallback routing + `evaluate-models` benchmarking; Documentation Agent; Evolution Agent + `evolve` pipeline (human-approval terminal); root `.agentd.yaml` self-hosting; 8 production guides; 267 offline tests. Final readiness review: [docs/FINAL_RELEASE_REPORT.md](../docs/FINAL_RELEASE_REPORT.md) | bootstrap exit viable: Human → Roadmap → Local-EZAI loop runs end-to-end |
| M7 | v1.0 hardened release | P7 | ⬜ not started | security sign-off; 72 h soak on N97 + GPU; `v1.0.0` |

## Sequencing rules

1. M1 → M2 → M3 are strictly sequential (runtime → isolation → autonomy).
2. M4, M5, M6 may run in parallel after M3.
3. **Isolation before autonomy:** no mutating tool ships before sandboxd +
   permission engine (ADR-004, ADR-008).
4. Existing chat stack stays byte-identical in behavior at every milestone
   (ADR-002); rollback of any milestone = don't start the overlay.

## Standing workstreams (every phase)

- Fixture-repo e2e suite grows each phase; runs in CI on a tiny-GGUF profile.
- Docs and ADRs updated in the same PR as the change.
- N97 profile kept working (scoped-down budgets), GPU profile kept primary.

## Next-generation roadmap (post-readiness-review)

The v0.7.0 release review defined the successor milestones **N1–N7**
(container sandbox → reviewer gate → code intelligence → web console →
scheduled evolution → model auto-tuning → v1.0): see
[docs/FINAL_RELEASE_REPORT.md §7](../docs/FINAL_RELEASE_REPORT.md).
These are intended to be executed by the platform itself
(`local-ezai . sprint` / `local-ezai . evolve`) with human-approved
merges — the bootstrap exit in practice.

## Deferred beyond v1.0

Multi-node execution · non-git VCS · fine-tuning · cloud-model fallback ·
Windows hosts · multi-tenant isolation · IDE plugins · k8s for the control
plane (decision gate at M6, ADR-011).
