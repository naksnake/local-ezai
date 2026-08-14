# Agent Design — Local Autonomous Software Engineer Platform

**Date:** 2026-08-14
**Scope:** Requirement 3 of the transformation — *define all required agents*.
Runtime mechanics live in [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md) §4;
control flow in [WORKFLOW_DESIGN.md](WORKFLOW_DESIGN.md).

---

## 1. Design stance

- **One orchestrator, few specialists.** Agents are added only where a
  distinct *toolset + prompt + budget* measurably beats a single generalist
  (validated in Phase 4 against the fixture suite). Default bias: fewer
  agents, sharper tools.
- **Deterministic router, stochastic workers.** The Orchestrator's control
  decisions (which state, which gate, retry or stop) are workflow-engine
  code. LLM judgment is confined to step *content* (plans, edits, verdicts).
- **Structured envelopes between agents.** Agents exchange JSON-schema-
  validated results, never raw transcripts. A sub-agent's transcript stays in
  its own journal scope; only the envelope enters the parent's context —
  this is the primary context-pressure control on small models.
- **Toolsets are allowlists.** Each agent's tools are enumerated at spawn;
  the permission engine enforces them regardless of what the model asks for.
- **Model roles, not model names.** Agents bind to LiteLLM aliases
  (`swe-planner`, `swe-coder`, `swe-reviewer`, `swe-fast`) so hardware
  profiles remap capability without touching agent code (ADR-007).

---

## 2. Roster overview

| Agent | Kind | Model role | Tool tiers | Phase |
|---|---|---|---|---|
| Orchestrator | deterministic + LLM routing assist | `swe-planner` (routing only) | none directly | P1/P3 |
| Planner | LLM | `swe-planner` | T0, T1 | P3 |
| Context / Research | LLM | `swe-fast` (escalates to planner) | T0, T1 | P4 (v0 in P1) |
| Implementer | LLM | `swe-coder` | T0, T2 | P3 (single-agent), P4 (specialist) |
| Tester / Verifier | LLM-assisted harness | `swe-fast` | T0, T2 (exec only) | P3 |
| Debugger | LLM | `swe-coder` | T0, T2 | P4 |
| Reviewer | LLM | `swe-reviewer` | T0 | P4 |
| Integrator / Docs | LLM | `swe-fast` | T0, T2, T3 (gated) | P4/P6 |
| Security Auditor | LLM + static rules | `swe-reviewer` | T0 | P7 (optional gate) |
| Memory Curator | LLM | `swe-fast` | T0, T3 (memory writes, gated) | P5 |

---

## 3. Agent specifications

### 3.1 Orchestrator
- **Purpose:** owns a run end-to-end; executes the state machine; spawns
  specialists; enforces budgets and gates; assembles the final report.
- **Nature:** mostly deterministic code. Uses one bounded LLM call only where
  routing genuinely needs judgment (e.g. classifying an ambiguous task,
  choosing which specialist a failure belongs to).
- **Inputs:** task submission (goal, repo, autonomy, budgets); journal.
- **Outputs:** run report envelope; state transitions (journaled).
- **Termination:** any terminal state (DONE / FAILED / CANCELLED / BLOCKED).
- **Never:** calls mutating tools itself; edits code; overrides a gate.

### 3.2 Planner
- **Purpose:** turn a goal + retrieved context into a reviewable plan.
- **Inputs:** task goal, project memory file, Context agent's repo brief,
  relevant KB/lessons hits.
- **Output envelope — `Plan v1`:**
  ```json
  {
    "goal": "…", "assumptions": ["…"],
    "tasks": [{"id": "T1", "intent": "…", "files_hint": ["…"],
               "check": "how this task is verified", "depends_on": []}],
    "verification": {"commands": ["…"], "success": "…"},
    "risks": ["…"], "out_of_scope": ["…"],
    "estimated_steps": 12
  }
  ```
- **Constraints:** ≤ N tasks (profile-dependent; 3 on N97, 8 on GPU); every
  task must name its check — *unverifiable tasks are rejected by schema*.
- **Termination:** valid plan, or CLARIFYING question set, or "infeasible"
  verdict with reasons (→ BLOCKED).

### 3.3 Context / Research
- **Purpose:** cheap, parallelizable repo/web exploration; returns a **brief**
  (map of relevant files/symbols, conventions, entry points, citations), not
  file dumps.
- **Tools:** `fs.read`, `fs.glob`, `code.grep`, `code.symbols`,
  `code.semantic`, `kb.search`, `web.search`, `web.fetch`.
- **Output envelope:** `RepoBrief v1` — ranked files w/ line refs, build/test
  commands discovered, constraints found in project memory, open questions.
- **Budget:** hard step/token caps; results cached in the run journal so
  repeated questions are free.

### 3.4 Implementer
- **Purpose:** execute exactly one plan task: edit code in the workspace.
- **Tools:** `fs.read/write/edit`, `fs.glob`, `code.grep`, `exec.run`
  (limited to fast feedback like compilers/formatters), `git.diff`,
  `git.commit` (checkpoint).
- **Protocol:** diff-first — reads before writing; prefers `fs.edit`
  (exact-match replace) over whole-file writes; commits a checkpoint with a
  conventional message when its task's local check passes.
- **Output envelope:** `TaskResult v1` — status, diff summary, files touched,
  check output, notes for reviewer.
- **Termination:** task check passes, or attempt budget exhausted (→
  Debugger or DIAGNOSE loop per workflow rules).

### 3.5 Tester / Verifier
- **Purpose:** run the project's verification commands (from project memory /
  repo config); parse failures into structured findings.
- **Nature:** harness-first — command execution and result parsing are code;
  the LLM (`swe-fast`) only summarizes/attributes failures.
- **Output envelope:** `Verification v1` — per-command pass/fail, parsed
  failing cases (test id, message, file:line where derivable), flakiness
  hints (retry deltas).
- **Never:** edits code. Verification and implementation are separated so a
  failing check can't be "fixed" by weakening the check silently — check
  file modifications are flagged to the Reviewer.

### 3.6 Debugger
- **Purpose:** root-cause a persistent failure the Implementer couldn't clear
  within its attempt budget; produce a hypothesis + minimal fix plan.
- **Tools:** Implementer's set + `exec.bg/poll` (reproduce servers/watchers)
  + `git.log/blame`.
- **Output:** `Diagnosis v1` — hypothesis, evidence (commands + excerpts),
  proposed minimal change, confidence. Low confidence routes to BLOCKED with
  the evidence attached rather than thrashing.

### 3.7 Reviewer
- **Purpose:** adversarial pass over the run's cumulative diff before
  finalization: correctness, regressions, scope creep, check-weakening,
  style vs project conventions.
- **Tools:** read-only (T0) — the reviewer cannot fix, only report; fixes go
  back through the Implementer (bounded fix cycles, see WORKFLOW_DESIGN §5).
- **Output envelope:** `Review v1` — verdict `approve | request_changes |
  block`, findings [{severity, file, line, issue, suggestion}], each finding
  tagged must-fix / should-fix.
- **Independence rule:** always a *fresh* session (no implementer context) so
  it reviews the diff, not the intention.

### 3.8 Integrator / Docs
- **Purpose:** final packaging: squash/order checkpoint commits into a clean
  narrative, write the final commit message(s)/changelog/PR body, update docs
  touched by the change, execute delivery (T3 `git.push`, `pr.create` —
  gated by autonomy level).
- **Output:** `Delivery v1` — branch, commits, PR URL (A3), summary for the
  human.

### 3.9 Security Auditor *(optional gate, default-on at A3)*
- **Purpose:** scan the cumulative diff for injected secrets, dangerous
  patterns (command injection, path traversal, credential logging),
  dependency risk (new deps vs allowlist).
- **Nature:** static rules first (deterministic scanners), LLM pass second;
  findings feed the Reviewer's must-fix channel.

### 3.10 Memory Curator
- **Purpose:** post-run distillation: extract durable lessons ("this repo's
  tests need `-p no:cacheprovider`", "module X owns feature Y") into the
  `swe-lessons` collection; propose project-memory diffs
  (`CLAUDE.md`/`.agent/*`).
- **Constraint:** all writes are T3-gated proposals — memory never
  self-modifies silently (ADR-008 applied to memory).

---

## 4. Interaction contract

1. **Spawn:** Orchestrator → `agent.spawn(role, task_envelope, toolset,
   budgets)`; the child gets a fresh context: role prompt + project memory +
   its envelope — never the parent transcript.
2. **Report:** child returns exactly one result envelope (schema-validated,
   bounded size); oversized payloads are stored as journal artifacts and
   referenced by id.
3. **Escalate:** children cannot spawn (depth 1 in v1 — revisit via ADR);
   children cannot change autonomy or budgets; "ask the human" bubbles
   through the Orchestrator's gate mechanism only.
4. **Journal:** every spawn/report/escalation is an event; the run is
   reconstructible without any agent's private state.

## 5. Budgets (defaults, per profile)

| Budget | N97 | GPU |
|---|---|---|
| Orchestrator wall clock / run | 30 min | 4 h |
| Planner tokens | 6k | 32k |
| Implementer attempts / task | 2 | 4 |
| Verifier full-suite runs / run | 3 | 10 |
| Review fix-cycles | 1 | 3 |
| Sub-agent concurrent count | 1 | 4 |

Budgets are enforced by the workflow engine (events `BUDGET_WARN` at 80%,
`BUDGET_EXHAUSTED` → controlled wrap-up, never mid-write truncation).

## 6. Prompting strategy (summary)

- Role prompts are versioned files in the repo (`agents/prompts/*.md`,
  Phase 3+), following the proven pattern of
  `config/prompts/web-search-assistant.md`.
- Every prompt carries: role charter, toolset with *when-to-use* guidance,
  envelope schema, hard prohibitions (e.g. Reviewer: "you cannot edit"),
  and the project memory block.
- Few-shot examples are per-role and small-model-tuned on the N97 tier
  (score-based selection deferred to P5 evaluation work).
