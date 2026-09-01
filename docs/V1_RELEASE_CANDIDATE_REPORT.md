# Local-EZAI — v1.0 Release Candidate Report

**Version:** agentd 1.0.0rc1 · **Date:** 2026-09-01 ·
**Scope:** the v1.0 hardening sprint closing the gaps identified in
[FINAL_RELEASE_REPORT.md](FINAL_RELEASE_REPORT.md) (v0.7.0) ·
**Decision records:** ADR-021, ADR-022, ADR-023, ADR-024

## Verdict

**RELEASE CANDIDATE — all nine success criteria satisfied.**
311 offline tests passing (43 added this sprint), ruff clean, zero
behavioral changes to the pre-existing chat/RAG stack, all prior
functionality preserved (every pre-existing test still passes, adjusted
only where the new mandatory gate adds a pipeline stage).

## 1. What this sprint delivered

### Phase 1 — Container sandbox (`sandboxd`, ADR-021; closes ADR-014)
One policed executor (`sandbox.py`) for **every** agent shell execution
(`exec_run`, all validation categories, debugging reproductions,
evolution benchmarks):
- **Command allowlist** (`sandbox.command_allowlist`) — fail-closed when
  configured, active in every mode.
- **Docker execution** whenever possible (`mode: auto`: daemon answers AND
  `sandbox.image` configured; `mode: docker` is strict): disposable
  `docker run --rm` per command, **restricted filesystem** (workspace-only
  mount at the host-identical path; origin `.git` for worktrees),
  **configurable resource limits** (`--memory/--cpus/--pids-limit`),
  default-deny network, explicit env passthrough, **execution timeout**
  enforced host-side with `docker kill`.
- **Execution audit log** — every command (refused included) appended to
  `~/.agentd/runs/<id>/exec_audit.jsonl`; resolved mode journaled.
- Backward compatible by construction: without an image/daemon the host
  executor behaves exactly as before. Repos cannot weaken the sandbox.
- Guide: [SANDBOX_GUIDE.md](SANDBOX_GUIDE.md).

### Phase 2 — Mandatory reviewer gate (ADR-022)
The workflow is now
`PLAN → CODE → VALIDATE(+Browser QA) → [DEBUG → FIX → REVALIDATE]* →
REVIEW → COMMIT → (opt-in) PUSH/PR`:
- REVIEW node in both compiled graphs; also guards `local-ezai commit`.
- **Blocks commit on critical issues**: `request_changes` always;
  findings at `review.block_severities` (default `high`) even under
  approve. Blocked runs fail with the full structured `ReviewReport` in
  `report.json` + `REVIEW_GATE` journal events.
- **Security / architecture / maintainability detection**: mandatory
  finding taxonomy + reviewer prompt sections for each.
- The reviewed diff includes **untracked files** — new-file-only changes
  cannot bypass the gate (hole found and closed during this sprint).
- Process: [REVIEW_PROCESS.md](REVIEW_PROCESS.md).

### Phase 3 — Semantic code intelligence (ADR-023)
- Symbol/function/class extraction: Python via stdlib `ast`; JS/TS/Go/Rust
  via optional **Tree-sitter** grammars (`agentd[intel]`, graceful
  degradation).
- **Dependency graph**: import edges resolved to repo files + hotspots.
- **Persisted index**: `.agent/code-index/{symbols.json,graph.json}`,
  content-hash incremental, refreshed per run, machine-managed (never
  committed; `plan` stays traceless with an in-memory index).
- Serving **Planner** (repository map injected into planning), **Coder /
  Debugger / Reviewer** (read-only `code_symbols` tool).
- Guide: [CODE_INTELLIGENCE.md](CODE_INTELLIGENCE.md).

### Phases 4–5 — Model transparency & explainability
- **`local-ezai models`**: live per-role routing (primary + fallback) from
  `.agent/model_registry.yaml`, exactly as a run resolves it.
- **`local-ezai explain-run [run-id]`**: which model handled planning,
  coding, debugging, review, memory, evolution — **fallback-aware**
  (clients record the model that actually served each role;
  persisted as `models_used` in every run report); deterministic stages
  labeled (Validation harness, Browser QA: Playwright).

### Phase 6 — Model benchmark dashboard
`local-ezai evaluate-models` now records, per evaluation:
- availability/protocol probes per role (as before), plus
- **quality metrics from run history**: planning accuracy, coding success
  rate, validation pass rate, debugging success rate, review accuracy
  (approval rate), execution speed (journal wall clock),
- a rolling **trend history** (last 20 evaluations) inside
  `.agent/model_benchmarks.json`,
- `--report` renders **[MODEL_GOVERNANCE_REPORT.md](MODEL_GOVERNANCE_REPORT.md)**
  (routing, probes, metrics, trend table).

### Self-evolution upgrade
`local-ezai evolve` evidence now includes **model benchmark trends**
(regressions, recoveries, latency drift, quality rates) before proposing;
the Evolution Agent's rules forbid re-proposing failed experiments
recorded in memory (on top of the existing `MEMORY_REPEAT_WARNING`
machinery inside the fix loop).

## 2. Final validation

| Suite | Result |
|---|---|
| Unit + integration + workflow tests | **311 passed** (offline, ScriptedLLM) |
| Sandbox tests (allowlist, audit, docker via fake binary, worktree mounts, modes) | ✅ 13 |
| Reviewer-gate tests (block/approve paths, categories, commit gate, journal) | ✅ 9 |
| Code-intelligence tests (symbols, graph, persistence, tool, planner injection) | ✅ 7 |
| Model governance/transparency tests (routing, fallback attribution, metrics, history, report) | ✅ 13 + prior ADR-020 suite |
| Lint | ruff clean |

## 3. Success criteria

| ✔ | Criterion | Evidence |
|---|---|---|
| ✅ | Preserve all current functionality | zero chat-stack changes; all prior tests pass; sandbox/gate defaults are behavior-preserving where unconfigured |
| ✅ | Run inside a container sandbox | ADR-021; `test_sandbox.py` (docker arg construction, mounts, limits) |
| ✅ | Require Reviewer approval before commit | ADR-022; gate in both graphs + `commit`; `test_review_gate.py` |
| ✅ | Understand repository structure semantically | ADR-023; `.agent/code-index/`; `test_code_intel.py` |
| ✅ | Explain active models | `models` + `explain-run`; fallback-aware `models_used`; `test_transparency.py` |
| ✅ | Benchmark model quality | run-history metrics + trend history + governance report |
| ✅ | Improve itself using benchmark feedback | evolution evidence carries benchmark/model trends; tested |
| ✅ | Retain self-sustainability | root `.agentd.yaml` intact; the Human → Roadmap → Local-EZAI loop unchanged, now gated by review |
| ✅ | Remain backward compatible | default `sandbox.image` empty ⇒ host executor; empty allowlist ⇒ allow-all; repo configs cannot alter the new subsystems; legacy `ezai` CLI untouched |

## 4. Remaining before `v1.0.0` final (tracked in `.agent/roadmap.md`)

N1′ container hardening (seccomp/userns, per-run egress allowlists) ·
N3′ Qdrant similarity retrieval over the symbol index · N4 web console +
chat-ops · N5′ scheduled evolution cadence · N6′ auto-proposed routing PRs
on regression · N7 security sign-off + 72 h soak + retire legacy `ezai`
CLI + tag `v1.0.0`.

## 5. Sign-off

- **Chief Architect:** hardening is additive; every new subsystem has an
  ADR, a config surface, and a repo-override policy — *approved*.
- **Principal Engineer:** 311/311 green, lint clean, gate hole
  (untracked-file bypass) found and closed with tests — *approved*.
- **Release Manager:** guides updated (SANDBOX / REVIEW_PROCESS /
  CODE_INTELLIGENCE / MODEL_GOVERNANCE_REPORT + refreshed CLI_REFERENCE,
  USER_GUIDE, OPERATION_MANUAL, GOVERNANCE, SELF_EVOLUTION_GUIDE,
  TROUBLESHOOTING) — *approved as RC*.
- **Product Owner:** all five mission priorities delivered in order —
  *approved as RC*.

**Merge of this release candidate remains a human decision
([GOVERNANCE.md](GOVERNANCE.md)).**
