# Migration Plan — local-ezai → Local Autonomous Software Engineer Platform

**Date:** 2026-08-14
**Inputs:** [GAP_ANALYSIS.md](GAP_ANALYSIS.md), [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md)
**Governing constraints:**
1. **Additive only** — the existing 8-service chat stack is never modified;
   all new capability ships in new services + one compose overlay
   (`docker-compose.swe.yml`) + additive Make targets. (ADR-002)
2. **Isolation before autonomy** — no mutating tool exists before the sandbox
   and permission engine do. (ADR-004, ADR-008)
3. **Every phase ends runnable** — each phase has a demoable exit criterion on
   both a GPU box and (scoped-down) an N97 box.
4. Architecture docs and ADRs are updated in the same PR as the change they
   describe.

---

## Phase overview & dependency graph

```
P0 Foundation docs ──► P1 Runtime core ──► P2 Sandbox & git ──► P3 Workflow &
      (this PR)          (agentd, journal,    (sandboxd, exec,     verification
                          permissions, CLI     worktrees, fs/git    (state machine,
                          skeleton, tests+CI)  tools via toolgw)    first e2e fix)
                                                                        │
              ┌─────────────────────────────┬──────────────────────────┤
              ▼                             ▼                          ▼
        P4 Multi-agent               P5 Code intelligence        P6 Interfaces
        (planner/reviewer/            & memory (codeidx,          (console page,
         curator, sub-agents,         memoryd, distillation,      chat-ops tools,
         model role tiering)          compaction v2)              webhooks)
              └─────────────┬───────────────┴──────────────────────────┘
                            ▼
                      P7 Hardening & release
                      (security pass, budgets/quotas, k8s parity, v1.0)
```

P4, P5, P6 can proceed in parallel once P3 lands; P7 requires all.

---

## Phase 0 — Foundation & governance *(this change set)*

**Scope:** architecture assessment, target design, ADRs, roadmap; establish
`.agent/` as the platform's own project memory. No code.

**Deliverables:** the six `docs/*.md` documents; `.agent/roadmap.md`,
`.agent/architecture.md`, `.agent/decisions.md` (ADR-001…ADR-012).

**Exit criteria:** documents merged; ADR process in effect (any deviation
from target architecture requires a new/updated ADR).

---

## Phase 1 — Agent runtime core (agentd) + engineering baseline

**Scope**
- `agentd` service skeleton: FastAPI, bearer auth, health endpoint; REST+WS
  API for sessions/runs.
- **Event journal**: SQLite `runs.db` + per-run JSONL; append/replay API;
  every later component writes through it. (ADR-006)
- **Agentic loop v0**: single agent, T0/T1 tools only (`code.grep` over a
  read-only mounted repo path, `fs.read`, `web.search`, `web.fetch`,
  `kb.search` — reusing SearXNG/Qdrant), LiteLLM client with role aliases
  (`swe-*` config files added per profile — config addition, not modification
  of existing files' semantics… new files referenced only by the overlay).
- **Permission engine core**: tier registry, autonomy levels, decision log;
  only T0/T1 exist yet, but the enforcement path is real from day one.
- **Context manager v0**: token budgeting + naive truncation with journaled
  evictions (compaction v1 arrives in P3, v2 in P5).
- `ezai` CLI v0: `run` (read-only Q&A/research tasks), `runs`, `attach`,
  `journal`.
- **Engineering baseline**: pytest suite + ruff + GitHub Actions CI for all
  *new* code; smoke tests that exercise existing services read-only.

**Exit criteria**
- `ezai run "explain how auto-RAG works in this repo" --repo local-ezai`
  produces a cited answer using grep+read+semantic search on GPU and N97
  profiles; the run is resumable after killing agentd mid-turn.
- CI green; permission engine denies a hand-crafted T2 call (fail-closed
  proof).

**Risks:** context overrun on N97 → keep v0 toolset terse; envelope caps.

---

## Phase 2 — Execution plane: sandbox, git, mutating tools

**Scope**
- `sandboxd`: repo registry (`repos/<name>`), git worktree per run
  (`workspaces/<run-id>`, branch `swe/<run-id>`), runner-container lifecycle
  via filtered docker socket proxy; exec API (timeouts, output caps, PTY,
  background procs); resource caps per profile; **default-deny egress**
  network with allowlist proxy.
- `toolgw`: MCP hub + registry + per-run scoping + audit; wraps sandboxd exec
  and workspace fs; T2 toolset: `fs.write`, `fs.edit`, `exec.run`,
  `git.status/diff/commit/apply_patch`; T3 stubs (`git.push`) wired to the
  permission engine's "ask" path.
- Workspace checkpointing: auto-commit at tool-batch boundaries; `ezai diff`,
  `ezai rollback`.
- Secret custody: push credentials only in sandboxd env; never in runner.

**Exit criteria**
- From CLI: agent edits a scratch repo file, runs its test suite in the
  runner container, commits on `swe/<run>`; host FS outside
  `workspaces/<run>` provably untouched; runner has no internet except
  allowlisted registries; T3 `git.push` triggers an interactive approval.
- Kill-tests: runner OOM/timeout produce clean journaled failures, not hangs.

---

## Phase 3 — Workflow engine & verification loop (first real autonomy)

**Scope**
- Implement the run state machine exactly as specified in
  [WORKFLOW_DESIGN.md](WORKFLOW_DESIGN.md): INTAKE → PLANNING → PLAN_GATE →
  EXECUTING (task loop with APPLY/CHECK/DIAGNOSE) → VERIFYING → REVIEWING →
  FINALIZING → DONE, plus BLOCKED/FAILED/CANCELLED paths, budgets, retries.
- Plan schema (JSON) + Planner prompt; plan gate honoring autonomy level.
- Verification harness: project check commands discovered from project memory
  (`CLAUDE.md`/`AGENT.md`) or configured per repo (test/lint/build), executed
  in the runner; structured failure parsing back into the loop.
- Compaction v1 (summarize-and-evict of tool history at phase boundaries).
- Autonomy A0–A2 fully functional (A3 delivery arrives in P6 with PR tools).

**Exit criteria (the platform's first honest milestone)**
- On a GPU profile: `ezai run "<seeded bug ticket>" --repo <fixture>
  --autonomy A2` fixes a failing test end-to-end without human input; journal
  replays the full state trajectory; second run resumes correctly from a
  mid-EXECUTING kill.
- On N97: same fixture at A1 with a 1.5B coder model completes a single-file
  fix within budget or fails **cleanly** into BLOCKED with a useful summary.

---

## Phase 4 — Multi-agent specialization

**Scope** (roster & specs in [AGENT_DESIGN.md](AGENT_DESIGN.md))
- Sub-agent scheduler in agentd (child sessions, narrowed toolsets, budgets,
  structured result envelopes).
- Specialists: Context/Research, Implementer, Tester, Reviewer (blocking
  verdict schema), Debugger, Docs/Integrator; Orchestrator becomes a router.
- Model role tiering in anger: planner/reviewer on `swe-planner`, edits on
  `swe-coder`, compaction on `swe-fast`; optional second inference slot
  overlay for GPU hosts.
- Review gate wired into REVIEWING state (issues → bounded fix cycles).

**Exit criteria:** measurable quality delta vs P3 single-agent on a 10-task
fixture suite (tracked in-repo); reviewer catches a seeded regression that
tests miss.

---

## Phase 5 — Code intelligence & memory maturation

**Scope**
- `codeidx`: tree-sitter symbol extraction; symbol-aware chunking; per-repo
  Qdrant collections (`code-<repo>`); hybrid retrieval (ripgrep/BM25 +
  vector) behind `code.semantic`/`code.symbols`; incremental re-index on
  worktree change.
- `memoryd` (or agentd module): layered memory API per TARGET §6;
  `swe-lessons` collection; Memory Curator distillation with T3-gated
  procedural-memory proposals (diffs against `CLAUDE.md`/`.agent/*`).
- Compaction v2: retrieval-aware (drop what's re-fetchable, keep decisions).

**Exit criteria:** on the fixture suite, retrieval-hit metrics improve and
average tokens/run drop vs P4; a curator-proposed memory update lands via
normal review.

---

## Phase 6 — Interfaces & delivery (A3)

**Scope**
- Web console page (run list, live state view, diff viewer, gate approvals)
  reading agentd's API — reusing monitor's RBAC pattern; shipped as part of
  the overlay, existing monitor untouched.
- Chat-ops MCP toolset (`swe.submit_task/run_status/approve`) registered in
  OpenWebUI exactly like existing tools.
- Git-host integration for LAN forges (Gitea/GitLab/GitHub): `git.push`,
  `pr.create/comment` as T3 tools; A3 end-to-end; optional webhook adapter
  (review-comment → follow-up run).

**Exit criteria:** DoD scenario from TARGET §13 passes at A3 against a LAN
Gitea; a non-technical operator can approve gates from the console alone.

---

## Phase 7 — Hardening & v1.0

**Scope:** security review of the full action path (threat model: prompt
injection → T3, sandbox escape, credential leakage); quotas
(concurrent runs, disk, retention pruning); failure-mode chaos tests; docs
(operator guide, project onboarding guide: writing `CLAUDE.md` for a target
repo); k8s manifests for the control plane (parity decision per ADR-011);
version pinning + release tagging.

**Exit criteria:** security checklist signed off; 72-hour soak on N97 and GPU
profiles with scheduled runs; `v1.0.0` tag.

---

## Cross-phase policies

- **Testing:** every new service lands with unit tests; the fixture-repo e2e
  suite grows each phase and runs in CI (CPU-only models in CI via a tiny
  GGUF profile).
- **Docs-as-code:** `docs/` + `.agent/` updated in the same PR; ADR required
  for any deviation.
- **Rollback:** each phase is a set of additive services — rollback is
  "don't start the overlay". No phase migrates or mutates existing volumes.
- **Branch/PR convention:** platform work on `feature/*` or `claude/*`
  branches; agent-produced work on `swe/<run-id>`.

## Explicit non-goals (deferred beyond v1.0)

Multi-node execution, non-git VCS, model fine-tuning, cloud model fallback,
Windows hosts, multi-tenant isolation, IDE plugins.
