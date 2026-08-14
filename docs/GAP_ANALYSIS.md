# Gap Analysis — local-ezai → Local Autonomous Software Engineer Platform

**Date:** 2026-08-14
**Baseline:** [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md)
**Target:** [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md)

Severity: 🔴 blocking (platform cannot exist without it) · 🟠 major
(autonomy is unsafe/ineffective without it) · 🟡 quality (needed for v1
polish). Effort: S / M / L / XL.

---

## 1. Capability matrix — Claude-Code-like capabilities vs. today

| # | Capability (Claude Code reference) | Current state | Gap | Sev | Effort |
|---|---|---|---|---|---|
| 1 | **Agentic loop** (plan → act → observe → iterate until done) | None. Single-turn tool calls inside OpenWebUI chat only | Build agent runtime (agentd) with turn loop, stop conditions, budgets | 🔴 | L |
| 2 | **File tools on a codebase** (read/write/edit/glob/ls) | filesystem MCP tool scoped to `./documents` only | Workspace-scoped fs toolset over real repo checkouts; exact-match edit tool | 🔴 | M |
| 3 | **Code search** (grep/glob; semantic) | Qdrant semantic search over *prose documents* only | ripgrep tool + code-aware indexing (symbols, tree-sitter) into per-repo collections | 🔴 | M/L |
| 4 | **Shell execution** (build, test, run anything) | None — no exec capability at all | Sandboxed `exec.run` with timeouts, output caps, background procs | 🔴 | L |
| 5 | **Sandboxing / isolation** | None (nothing executes, so nothing is isolated) | sandboxd: per-run containers, git worktrees, egress policy, resource caps | 🔴 | L |
| 6 | **Git integration** (status/diff/branch/commit/PR) | None at runtime (repo itself is git-managed, but no tooling) | git toolset (T2 local, T3 push/PR); worktree per run; branch as deliverable | 🔴 | M |
| 7 | **Verification loop** (run tests, read failures, fix, repeat) | None | Workflow engine inner loop: APPLY → CHECK → DIAGNOSE with bounded retries | 🔴 | M |
| 8 | **Planning & task decomposition** | None | Planner agent + plan schema + plan-approval gate | 🔴 | M |
| 9 | **Sub-agents / delegation** | None | agentd sub-sessions with narrowed toolsets and budgets | 🟠 | M |
| 10 | **Permission system** (tiered, ask-before-act) | Single bearer key per tool plane; all-or-nothing | Permission engine: risk tiers T0–T4 × autonomy A0–A3, fail-closed | 🔴 | M |
| 11 | **Project memory** (`CLAUDE.md`, auto-loaded context) | **No CLAUDE.md exists in this repo**; no convention for target repos | Procedural memory layer: per-repo memory file injected each run; `.agent/` for this repo (created with this assessment) | 🟠 | S |
| 12 | **Context management** (window budgeting, compaction) | None (OpenWebUI truncates naively; N97 README documents overflow errors as user problems) | Context manager with token budgets per role, summarize-and-evict compaction, journaled evictions | 🟠 | M |
| 13 | **Session persistence & resume** | Chat logs persist; no task state anywhere | Event-sourced journal (SQLite+JSONL), replay-to-resume | 🔴 | M |
| 14 | **Headless/CLI operation** | Browser-only (plus raw curl to LiteLLM) | `ezai` CLI + REST/WS API | 🟠 | M |
| 15 | **Diff/patch discipline** (propose, review, apply) | None | Diff-first editing; patch artifacts in journal; console diff viewer | 🟠 | M |
| 16 | **Code review capability** | None | Reviewer agent + review schema + gate in workflow | 🟠 | M |
| 17 | **Hooks** (pre/post tool-call project hooks) | None | Project-config hooks executed by toolgw | 🟡 | S |
| 18 | **Skills/playbooks** (reusable procedures) | Prompt file exists for web search (`config/prompts/`) — closest analog | Procedural memory + skill files loaded on demand | 🟡 | M |
| 19 | **Model routing by role** (planner vs coder vs fast) | Single chat model alias per profile; Coder-1.5B pre-wired but manual | LiteLLM role aliases `swe-*`; optional second inference slot | 🟠 | S/M |
| 20 | **Web research for engineering** (docs lookup) | ✅ largely present: SearXNG + fetch tool + proactive-search prompt | Reuse as-is behind T1 policy | — | S |
| 21 | **RAG / knowledge retrieval** | ✅ strong for prose; auto-RAG at proxy | Add code-aware retrieval; keep KB retrieval for design docs | 🟡 | M |
| 22 | **Observability of agent work** (live run view, audit) | Service-level monitor only (up/down, latency) | Run-level console: state machine view, live journal, gate approvals | 🟠 | M |
| 23 | **Cost/budget controls** (token/time/step caps) | None | Budgets enforced by workflow engine per run/phase/agent | 🟠 | S |
| 24 | **Checkpointing / rollback of changes** | None | Git-checkpoint at phase boundaries; `git reset` rollback | 🟠 | S |
| 25 | **Multi-repo awareness** | None | sandboxd repo registry; per-repo index collections | 🟡 | M |
| 26 | **Automated tests for the platform itself** | Zero tests in repo | Test suite + CI from Phase 1 onward (platform code only) | 🟠 | M |

**Bottom line:** of the ~26 reference capabilities, the current stack fully
provides 2 (web research, prose RAG), partially provides 4 (tools protocol,
model abstraction, memory substrate, ops/RBAC habits), and lacks 20 — but the
lacking 20 sit almost entirely in **two new planes** (control + execution)
that can be added without touching what exists.

---

## 2. Asset reuse assessment (what we keep, verbatim)

| Existing asset | Role in target | Change required |
|---|---|---|
| LiteLLM proxy + engine-agnostic `vllm` slot | Model plane | config-only: add `swe-*` role aliases (new config file per profile) |
| embed-server | embeddings for code index + KB | none |
| Qdrant | all vector memory | none (new collections created by codeidx/memoryd) |
| mcpo + 4 MCP servers | chat tool plane; MCP servers reused by toolgw | none to mcpo; toolgw is a sibling, not a replacement |
| SearXNG + web-search/fetch | T1 research tools | none |
| Monitor | ops dashboard; later gains run console page | additive endpoints consumption only (Phase 5) |
| auto-RAG hook | stays for chat; pattern **not** reused for actions | none |
| Makefile/compose/profile discipline | packaging model for all new services | additive targets + one overlay file |
| `.env` config + pinning discipline | applies to all new services | additive variables |
| embed_documents.py chunking/payload conventions | baseline for codeidx document path | superseded for code by symbol-aware chunking |

Nothing currently running requires modification. This satisfies the
"do not modify existing runtime" constraint structurally, not just by policy.

---

## 3. Gap details that shape the design

### 3.1 The execution gap is the deep one
Everything Claude-Code-like ultimately reduces to *safe arbitrary command
execution against a real checkout*. The stack has no exec, no checkout, no
isolation. This is why the migration plan front-loads sandboxd + git
worktrees (Phase 2) before any autonomous behavior (Phase 3+): a tool loop
without isolation would force us to choose between useless (read-only) and
dangerous (host-mutating).

### 3.2 The fail-open habit must be inverted for actions
The auto-RAG hook's fail-open design is correct for retrieval and is exactly
wrong for actions. Today `MONITOR_AUTH=false` and all-or-nothing bearer keys
are acceptable because nothing mutates anything. The permission engine
(ADR-008) must land **with** the first mutating tool, not after.

### 3.3 Context windows are the N97 cliff
The README already documents `ContextWindowExceededError` as a routine failure
on the N97 profile with mere chat+tools. An agentic loop multiplies context
pressure (diffs, test logs). Compaction and terse tool envelopes are
first-class requirements, not optimizations — and role-tiered budgets must be
enforced by the workflow engine.

### 3.4 No platform tests
The platform's own services (monitor 813 lines, callbacks, embed-server, new
agentd/sandboxd/toolgw) have zero automated tests. An autonomous SWE platform
that cannot verify *itself* is not credible; CI and a test suite for all new
code are a Phase 1 exit criterion, and backfilling smoke tests for existing
services is scheduled (without modifying their runtime behavior).

### 3.5 Missing project memory convention
`CLAUDE.md` does not exist; agents (including the ones authoring this
assessment) have no durable context file. The `.agent/` directory introduced
alongside this document set becomes the platform's own procedural memory, and
the same convention (`CLAUDE.md`/`AGENT.md` per target repo) becomes the
injected memory layer for repos the platform works on.

---

## 4. Risk register (top 6)

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Small local models can't sustain multi-step coding loops | High (N97), Med (7B) | Quality collapse, loops burn budgets | Role tiering; short bounded loops; verification-first design; honest per-profile capability floors (TARGET §1); A0/A1 defaults on small profiles |
| Sandbox escape / host mutation | Low | Critical | No privileged containers, socket proxy with path-prefix allowlist, non-root, default-deny egress, T4 hard denies |
| Prompt injection via fetched web/docs content | Med | High (would trigger T3 actions) | Tier policy enforced outside the model; untrusted-content tagging in envelopes; T3 always gated below A3 |
| Scope creep into a general workflow engine | Med | Delivery slip | WORKFLOW_DESIGN fixes one state machine; ADR process for any new state |
| Docker-socket coupling (sandboxd) breaks on k8s later | Med | Med | sandboxd's workspace API is transport-abstract; k8s driver deferred deliberately (roadmap M6+) |
| Journal/DB growth on always-on boxes | Med | Low | retention policy + `ezai runs prune`; JSONL rotation |

---

## 5. Priority order (feeds MIGRATION_PLAN)

1. 🔴 Foundations that everything depends on: journal/state store, agentd
   skeleton, permission engine core (Phase 1).
2. 🔴 Sandbox + git + exec (Phase 2) — unlocks every T2 tool.
3. 🔴 Workflow state machine + verification loop (Phase 3) — first end-to-end
   autonomous fix at A1/A2.
4. 🟠 Specialist agents, review gate, code intelligence (Phase 4–5).
5. 🟡 Console UX, chat-ops, hooks/skills, multi-repo, k8s (Phase 6+).
