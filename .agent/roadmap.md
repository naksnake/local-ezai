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
| M2 | Execution plane | P2 | ✅ done (2026-09-01) — ADR-021 sandboxd: allowlist → host/Docker execution (workspace-only mount, egress `none` default, resource limits) → per-run audit log; docker engages whenever `sandbox.image` + daemon are present. Residual hardening (seccomp/userns) tracked as N1′ | sandboxed edit+test+commit on `swe/<run>`; host untouched; egress default-deny; T3 asks |
| M3 | First autonomous fix | P3 | 🟡 mostly — plan→code→validate→**self-healing debug loop**→commit works end-to-end (ADR-015: RCA engine, read-only Debug Agent, max 10 iterations, stall detection; 128 offline tests); full gate/BLOCKED machine and journal-replay resume still open | seeded-bug fixture fixed end-to-end at A2 (GPU); clean BLOCKED behavior at A1 (N97) |
| M4 | Multi-agent quality | P4 | ✅ done (2026-09-01) — Debugger (ADR-015), Browser QA (ADR-016), Memory Curator (ADR-017), Reviewer (ADR-018), multi-agent parallel collaboration (ADR-019), and the **mandatory reviewer-in-pipeline gate** (ADR-022: REVIEW between green validation and commit, blocking on critical findings, security/architecture/maintainability taxonomy). Context/Research agent folded into code intelligence (ADR-023) | measurable quality delta on 10-task fixture suite; reviewer catches seeded regression |
| M5 | Code intelligence & memory | P5 | 🟡 mostly — project memory (ADR-017) + **semantic code intelligence** (ADR-023: ast/Tree-sitter symbol index, import graph, `.agent/code-index/`, planner repo-map injection, `code_symbols` tool). Qdrant-backed similarity retrieval still open (N3′) | hybrid code retrieval live; tokens/run down vs M4; curator memory proposal merged via review |
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

## Productization Phase (V1 product — architecture accepted 2026-09-01)

ADR-025 defines the integrated product: one control plane (`ezaid`),
declarative model state with generations (Registry v2 + PAL), governed
model lifecycle, OpenWebUI as the front door (SWE tool server +
Orchestrator persona), Admin Center on the monitor, `.env`-once
installation. Execution phases **P1–P6** with exit criteria:
[docs/V1_IMPLEMENTATION_PLAN.md](../docs/V1_IMPLEMENTATION_PLAN.md);
product definition: [docs/TARGET_PRODUCT_V1.md](../docs/TARGET_PRODUCT_V1.md).
P4 (web console) supersedes N4; N5′/N6′ land post-V1 on the P2 governance
queue. Status: 📐 architecture only — no implementation started.

**Product review (ADR-026, 2026-09-01):** agnosticism audit passed with
remediations — roles/groups become the only stable names (role aliases
complete ADR-007; no model names in code), capability classes replace SKU
profiles, runtimes are descriptors behind a neutral `engine` alias,
governance rules "govern roles, reveal models", evolution gains an
advisory-only lane into the approval queue. Final FRE: clone → edit .env
once (`AI_RUNTIME` + three group seeds) → `make setup` → WebUI → done.
See [docs/V1_PRODUCT_REVIEW.md](../docs/V1_PRODUCT_REVIEW.md) and its five
companion documents. Execution decomposition:
[docs/V1_PR_PLAN.md](../docs/V1_PR_PLAN.md) — 26 PRs across P1–P6 with
dependency graph, gates, and per-PR acceptance.

## Next-generation roadmap (post-readiness-review)

The v0.7.0 release review defined the successor milestones **N1–N7**; the
v1.0 hardening sprint (ADR-021..024, version 1.0.0rc1 — see
[docs/V1_RELEASE_CANDIDATE_REPORT.md](../docs/V1_RELEASE_CANDIDATE_REPORT.md))
closed **N1 (container sandbox)**, **N2 (reviewer gate)**, **N3 (symbolic
code intelligence)**, and the benchmark-feedback loop of **N5/N6**
(trend-aware evolution, model dashboards). Remaining, executable by the
platform itself with human-approved merges:

- **N1′** container hardening: seccomp/user-namespace profiles, per-run
  egress allowlists
- **N3′** Qdrant-backed semantic similarity retrieval over the symbol index
- **N4** SWE web console + chat-ops MCP tools (start run / approve gate
  from OpenWebUI)
- **N5′** scheduled evolution cadence (cron) + PR queue dashboard
- **N6′** auto-proposed routing PRs on benchmark regression
- **N7** v1.0 final: security sign-off, 72 h soak on N97 + GPU, retire the
  legacy `ezai` CLI, tag `v1.0.0`

## Deferred beyond v1.0

Multi-node execution · non-git VCS · fine-tuning · cloud-model fallback ·
Windows hosts · multi-tenant isolation · IDE plugins · k8s for the control
plane (decision gate at M6, ADR-011).
