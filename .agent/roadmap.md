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
| M4 | Multi-agent quality | P4 | 🟡 partially — Debugger agent pulled forward and shipped (ADR-015); Reviewer/Context/Curator still open | measurable quality delta on 10-task fixture suite; reviewer catches seeded regression |
| M5 | Code intelligence & memory | P5 | ⬜ not started | hybrid code retrieval live; tokens/run down vs M4; curator memory proposal merged via review |
| M6 | Interfaces & A3 delivery | P6 | ⬜ not started | console gate approvals; chat-ops tools; PR opened on LAN forge at A3 |
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

## Deferred beyond v1.0

Multi-node execution · non-git VCS · fine-tuning · cloud-model fallback ·
Windows hosts · multi-tenant isolation · IDE plugins · k8s for the control
plane (decision gate at M6, ADR-011).
