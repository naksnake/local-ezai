# Architecture Decision Records

> One entry per decision. Statuses: Proposed / **Accepted** / Superseded(by).
> New states in the workflow machine, new agents, or deviations from
> [docs/TARGET_ARCHITECTURE.md](../docs/TARGET_ARCHITECTURE.md) require a new
> ADR here, in the same PR.

Index: [001](#adr-001) [002](#adr-002) [003](#adr-003) [004](#adr-004)
[005](#adr-005) [006](#adr-006) [007](#adr-007) [008](#adr-008)
[009](#adr-009) [010](#adr-010) [011](#adr-011) [012](#adr-012)
[013](#adr-013) [014](#adr-014) [015](#adr-015) [016](#adr-016)
[017](#adr-017)

---

## ADR-001 — All model access goes through LiteLLM's OpenAI-compatible seam
**Date:** 2026-08-14 · **Status:** Accepted
**Context:** The stack's proven invariant is the engine-agnostic inference
slot (service always named `vllm`, OpenAI API on :8000) behind LiteLLM; it is
what makes N97↔GPU portability free.
**Decision:** Agents and all new services consume models exclusively via
LiteLLM aliases. No component may address an engine directly.
**Consequences:** Hardware/model swaps stay config-only; role tiering
(ADR-007) becomes possible; we accept LiteLLM as a single point of failure it
already is today.

## ADR-002 — Additive evolution: new planes as overlay services; existing runtime frozen
**Date:** 2026-08-14 · **Status:** Accepted
**Context:** Transformation mandate explicitly forbids modifying the existing
runtime; the chat stack has real users.
**Decision:** All SWE-platform capability ships as new services (agentd,
toolgw, sandboxd, codeidx, memoryd) in an additive `docker-compose.swe.yml`
overlay with additive Make targets and `.env` vars. Existing services,
compose files, and behavior stay byte-identical; the monitor gains run views
only by consuming agentd's API.
**Consequences:** Rollback = don't start the overlay. Some duplication
(mcpo and toolgw coexist) is accepted deliberately. No migration of existing
volumes ever.

## ADR-003 — MCP is the single tool protocol; the gateway adds policy, not protocol
**Date:** 2026-08-14 · **Status:** Accepted
**Context:** The stack already runs 4 MCP servers behind mcpo; MCP is the
ecosystem standard for agent tools.
**Decision:** Every agent tool is an MCP tool. toolgw (mcpo's successor for
agents) contributes registry, risk tiers, per-run scoping, validation, and
audit — never a bespoke RPC scheme. Existing MCP servers are reused as-is.
**Consequences:** Third-party MCP servers plug in cheaply; policy is enforced
in exactly one place; mcpo remains untouched for chat use.

## ADR-004 — Sandbox = per-run runner containers + git worktrees; isolation before autonomy
**Date:** 2026-08-14 · **Status:** Accepted
**Context:** Autonomy requires arbitrary command execution; the host and the
control plane must be structurally out of reach. Alternatives considered:
host execution with path guards (rejected: one bug = host compromise),
microVMs (rejected for v1: heavy on N97-class hardware), a single shared
runner (rejected: cross-run contamination).
**Decision:** sandboxd creates one container per run (non-root, no
privileges, read-only rootfs except workspace, CPU/mem/pids caps,
default-deny egress with allowlist proxy) mounted on a git worktree
(`workspaces/<run-id>`, branch `swe/<run-id>`). No mutating tool exists
outside it. Push credentials live only in sandboxd (host side). Docker access
only via a filtered socket proxy. **No T2 tool ships before this exists.**
**Consequences:** Cheap parallelism and rollback (git), a natural deliverable
(branch); we accept docker-socket coupling for v1 (k8s driver deferred,
ADR-011) and per-run container overhead (~seconds).

## ADR-005 — Qdrant remains the only vector store; one collection per concern, one embed model per collection
**Date:** 2026-08-14 · **Status:** Accepted
**Context:** Qdrant + embed-server already work across all hardware profiles;
the dimension-mismatch trap is already guarded in `embed_documents.py`.
**Decision:** Code index (`code-<repo>`), lessons (`swe-lessons`), and the
existing KB are separate Qdrant collections. The existing rule — never mix
embedding models in one collection; model switch = new collection — is
platform law.
**Consequences:** No new database dependency; retrieval quality tuning stays
per-concern; re-index cost on embed-model changes is accepted.

## ADR-006 — Event-sourced runs: SQLite + JSONL journal as the single source of truth
**Date:** 2026-08-14 · **Status:** Accepted
**Context:** Autonomous runs need resume, audit, and post-hoc distillation;
the stack has no state store today. Alternatives: Postgres (rejected: new
heavyweight dependency on N97), in-memory + snapshots (rejected: lossy audit).
**Decision:** Every run appends events (schema in WORKFLOW_DESIGN §4) to a
per-run JSONL journal indexed by SQLite (`runs.db`). All state — machine
position, UI views, resume, memory distillation, metrics — derives from
replay. No side-channel state.
**Consequences:** Crash recovery is replay; audits are complete; we accept
journal growth (retention pruning) and the discipline that *everything* must
be an event.

## ADR-007 — Model tiering by role alias (`swe-planner/coder/reviewer/fast/embed`)
**Date:** 2026-08-14 · **Status:** Accepted
**Context:** Local model quality varies enormously across hardware profiles
(1.5B GGUF ↔ 72B); agent code must not care. Coder-tuned models already
pre-wired in the N97 profile prove the demand.
**Decision:** Agents bind to LiteLLM role aliases only. Each hardware profile
maps aliases to models in its LiteLLM config (new config files, referenced by
the overlay); a second inference slot is an optional GPU-profile overlay.
**Consequences:** Capability scales with hardware transparently; per-profile
capability floors are documented honestly (TARGET §1) instead of pretending
uniform quality.

## ADR-008 — Fail-open retrieval, fail-closed action; risk tiers T0–T4 × autonomy A0–A3
**Date:** 2026-08-14 · **Status:** Accepted
**Context:** The auto-RAG hook's fail-open design is correct for context
enrichment and would be catastrophic for actions. Prompt injection via
fetched content must not be able to trigger side effects.
**Decision:** Tool calls are classified T0 (read, workspace) → T4
(destructive/host); the permission engine evaluates tier × autonomy level ×
project policy × scope, failing **closed** on any error. T3 requires human
grant below A3; T4 is always denied to agents. Tier policy is enforced
outside the model. Memory/procedural writes are T3 (memory never
self-modifies silently).
**Consequences:** Some friction at A1/A2 (asks); complete audit of grants;
injection can at worst waste read budget, not act.

## ADR-009 — Control plane in Python (FastAPI), consistent with the existing codebase
**Date:** 2026-08-14 · **Status:** Accepted
**Context:** embed-server and monitor are FastAPI services; scripts are
Python; the only Node component is the vendored MCP server. Team/agent
familiarity and MCP/tree-sitter/docker SDK maturity all favor Python.
Alternatives: Node/TS (rejected: splits the codebase), Go (rejected:
rewrite-grade cost, no in-repo precedent).
**Decision:** agentd, toolgw, sandboxd, codeidx, memoryd, and the ezai CLI
are Python 3.12 / FastAPI services, pinned like everything else.
**Consequences:** One language across the platform; asyncio discipline
required for the runtime loop; per-service images stay slim.

## ADR-010 — Deterministic orchestration; LLMs never control loop integrity
**Date:** 2026-08-14 · **Status:** Accepted
**Context:** Small local models are unreliable controllers; letting model
output drive control flow makes budgets and termination unenforceable.
**Decision:** The workflow state machine, gates, budgets, retries, and cycle
limits are code in agentd. LLM output fills step content (plans, edits,
verdicts) as schema-validated envelopes; adding a state or agent requires an
ADR.
**Consequences:** The machine provably terminates (bounded cycles); model
upgrades improve quality without changing safety properties; we forgo
free-form "agent decides everything" flexibility on purpose.

## ADR-011 — Compose-first deployment; k8s for the control plane deferred to a gate at M6
**Date:** 2026-08-14 · **Status:** Accepted
**Context:** The k8s manifests cover only part of today's stack; the platform
must run on single-box N97 hardware where K3s adds cost without value;
sandboxd's v1 driver is Docker.
**Decision:** The SWE platform targets Docker Compose through v1.0.
sandboxd's workspace/exec API is transport-abstract so a k8s (Jobs) driver
can be added later; the k8s-parity decision is re-evaluated at milestone M6.
**Consequences:** One deployment path to harden for v1; k8s users wait; no
architectural door is closed.

## ADR-012 — Project memory convention: per-repo `CLAUDE.md`/`AGENT.md`, `.agent/` for this repo
**Date:** 2026-08-14 · **Status:** Accepted
**Context:** No project-memory convention existed (this repo had no
CLAUDE.md); Claude-Code practice shows a repo-level memory file is the
highest-leverage context an agent gets.
**Decision:** The platform injects a target repo's `CLAUDE.md` (fallback
`AGENT.md`) into every run's system context. This repo's own memory lives in
`.agent/` (architecture.md, roadmap.md, decisions.md), maintained
docs-as-code. Curator-proposed changes to any memory file are T3-gated diffs
through normal review.
**Consequences:** Agents working on any repo get durable conventions;
memory stays reviewable and versioned with the code it describes.

## ADR-013 — LangGraph as the orchestration substrate for the workflow engine
**Date:** 2026-08-14 · **Status:** Accepted
**Context:** Phase 1 required an orchestration framework. ADR-010 mandates
deterministic control flow; the question was build-vs-use for the state
machine executor. Hand-rolled loop (rejected: re-implements graph routing,
retries and future checkpointing), heavyweight workflow engines like
Temporal (rejected: infrastructure cost on N97-class hardware).
**Decision:** The workflow engine is implemented as a **LangGraph
StateGraph** inside agentd: nodes call agents, conditional edges route on
typed state, cycle budgets and a recursion limit bound execution. LLM output
never selects edges — routes read only validated state fields (preserving
ADR-010). LangGraph's checkpointing is the intended substrate for Phase 3
resume, alongside the journal.
**Consequences:** Graph topology is declarative and testable; we accept the
langchain-core dependency tree in agentd only (the chat stack is
unaffected); the Phase 3 full state machine extends this graph rather than
replacing it.

## ADR-014 — Interim execution isolation: git worktrees + policed host subprocesses (pre-sandboxd)
**Date:** 2026-08-14 · **Status:** Accepted (interim — superseded when
Phase 2 sandboxd lands)
**Context:** ADR-004 gates mutating tools on container sandboxing, but the
Phase 1 MVP mandate requires a working edit-test-commit loop now.
**Decision:** MVP mutating tools operate under four compensating controls:
(1) all writes confined to a **git worktree** on branch `swe/<run-id>` —
the user's checkout is never touched and rollback is branch deletion;
(2) **path containment** enforced at resolve time (traversal + symlink
escapes rejected); (3) `exec_run`/validation commands run as host
subprocesses with wall-clock timeouts and output caps, cwd-pinned to the
workspace; (4) **T3 fail-closed**: `git_push` denied unless explicitly
enabled per run. Residual risk is documented in agentd/README.md
("Safety model"): shell commands can read host state and reach the LAN.
**Consequences:** Useful autonomy ships in Phase 1 with an honest risk
statement; the exec contract (`run_command`) is the seam where sandboxd's
container executor replaces the host executor in Phase 2 without changing
tool or agent code. A2+ autonomy defaults remain gated on Phase 2.

## ADR-015 — Self-healing workflow: read-only Debug Agent + deterministic RCA engine, bounded by iterations and stall detection
**Date:** 2026-08-14 · **Status:** Accepted (supersedes the Phase-1
diagnose node; realizes WORKFLOW_DESIGN's inner DIAGNOSE loop and pulls the
Debugger agent forward from roadmap Phase 4)
**Context:** Blind retry loops on validation failures either thrash (small
models re-trying the same broken idea) or "succeed" by patching symptoms —
weakening tests, swallowing exceptions. The platform mandate is that the
Debug Agent identify root causes instead. Prompting alone cannot guarantee
that; the guarantee must be structural. Alternatives considered: letting the
Coder self-diagnose inline (rejected: the agent that wrote the bug re-reads
it with the same blind spot, and diagnosis quality is unobservable); an
LLM-based error classifier (rejected: categorization must be deterministic
to be trustworthy for routing and stall detection).
**Decision:** Validation failures enter a DEBUG → FIX → REVALIDATE loop
built from three separated responsibilities:
(1) a **deterministic RCA engine** (`rca.py`) categorizes every failing
check (syntax/import/assertion/exception/timeout/environment/lint/build/
unknown) by ordered regex rules, extracts locations and a stable **error
signature**, and seeds a per-category fix strategy;
(2) a **read-only Debug Agent** (reproduce/read/grep/diff only — no write
tools) that must produce a schema-validated `DebugReport`: root cause,
confidence, cause-vs-symptom justification (`why_root_cause`), evidence,
and a concrete `fix_strategy`, with all prior iterations in its context so
failed strategies are never repeated;
(3) the **Coding Agent** applies the strategy as a `HEAL-n` task.
Loop integrity is code (ADR-010): a hard cap of
`limits.max_heal_iterations` (default **10**) cycles, and **stall
detection** — the identical combined failure signature persisting for
`stall_threshold` (default 3) consecutive validations aborts the run with a
"no progress" verdict. A failed fix attempt does not abort; it becomes
history for the next iteration. Every step is journaled (`RCA_REPORT`,
`DEBUG_REPORT`, `FIX_APPLIED`, `HEAL_ITERATION`) and surfaced in the run
report (`healing[]`, `iterations_used`) and the `ezai runs`/`ezai journal`
commands.
**Consequences:** Symptom-patching cannot loop (the stall detector converts
it into a fast, explained failure); diagnosis quality is observable and
auditable per iteration; the diagnose/repair separation costs one extra LLM
call per iteration, accepted for the audit trail. The failure taxonomy in
WORKFLOW_DESIGN §7 gains a deterministic implementation that later phases
(Reviewer, BLOCKED-state routing) can reuse.

## ADR-016 — Browser QA: declarative Playwright harness in the validation pipeline; console errors fail; commits gated
**Date:** 2026-08-14 · **Status:** Accepted (pulls UI-level verification
forward into the VERIFYING stage of WORKFLOW_DESIGN)
**Context:** Unit/lint/build checks cannot see a broken login page or a
runtime console error. The Phase 3 mandate: launch the real application,
drive real user workflows (login, customer CRUD) in a real browser, and
block delivery until they pass. An LLM-driven browser agent was considered
and rejected for the validation path: verification must be deterministic
and cheap to re-run every REVALIDATE iteration (ADR-010's philosophy —
verdicts come from exit codes and assertions, never model judgment).
Selenium was rejected in favor of Playwright (auto-waiting assertions,
console/pageerror hooks, headless reliability).
**Decision:** Browser QA is a **deterministic harness agent** executing
**declarative workflow specs** from the target repo's ``.agentd.yaml``
(step vocabulary: goto/click/fill/select/expect_text/expect_no_text/
expect_visible/expect_url/expect_title/wait_for/screenshot). The engine
launches the app per validation pass (free port, readiness poll, log
capture, process-group teardown), runs every workflow in headless Chromium,
records console.error + uncaught page errors on every page, and captures
screenshots (explicit steps + automatically on failure). **Failure rules:**
a workflow fails on step failure, failed verification, or ANY console error
(``ignore_console_patterns`` is an explicit, per-repo escape hatch; default
strict). A configured-but-unusable stage (Playwright missing, app dead,
invalid spec) is a failure, never a skip. Browser results merge into the
ValidationReport as ``browser[<workflow>]`` checks, so the RCA engine
(category ``browser``) and the DEBUG→FIX→REVALIDATE loop self-heal UI bugs;
each revalidation relaunches the app against the edited code. **Commits are
gated twice:** the graph route reaches GIT only on green validation, and
the Git Agent independently refuses a failing ValidationReport (journaling
``COMMIT_BLOCKED``). Command checks run first; the app is only launched on
code that passes them, and the skipped stage still counts as not-succeeded.
**Consequences:** UI regressions block delivery mechanically; specs live
with the repo and are refactor-stable evidence for the Debug Agent;
per-validation app launches cost seconds (accepted for hermetic
revalidation); dynamic exploratory browser testing (an LLM probing the app)
remains future work outside the validation gate.

## ADR-017 — Project memory: SQLite in the origin repo's `.agent/`, deterministic learning, repeat-mistake detection
**Date:** 2026-08-14 · **Status:** Accepted (implements the
procedural/episodic slices of TARGET_ARCHITECTURE §6 and the Memory Curator
role of AGENT_DESIGN §3.10, per-repository)
**Context:** The runtime forgot everything between runs: it could re-attempt
a fix that already failed yesterday, and had no channel for project rules or
conventions. Requirements: persist architecture decisions, coding styles,
project rules, failed/successful fixes, and implementation history in the
repo's ``.agent/`` using SQLite; learn from debugging attempts, validation
failures, and successful repairs; avoid repeating previous mistakes; feed
planning and debugging. Alternatives considered: Qdrant vectors (rejected
for this layer: exact signature matching, not semantic recall, is what
prevents repeated mistakes — semantic memory remains the codeidx phase);
LLM-summarized learning as the primary channel (rejected: recording from
run outcomes must be deterministic to be trustworthy; LLM distillation is
an optional, curated-kinds-only addition).
**Decision:** A **MemoryStore** (``.agent/memory.db``, SQLite, six kinds)
plus a regenerated human-readable export (``.agent/lessons_learned.json``).
The **Memory Agent** records deterministically at every terminal run: each
failed healing iteration → ``failed_fix`` (root cause, approach, error
signature, category); each successful one → ``successful_fix``; every run →
``implementation`` history (validation-failure aborts included). Curated
kinds enter via ``ezai remember`` or optional LLM distillation
(``memory.distill``, off by default). **Read-side integration:** the
Planner receives rules/styles/decisions/relevant lessons/history; the Debug
Agent receives approaches that already failed for the exact error signature
(instructed never to repeat them) and previously successful repairs;
additionally a **deterministic repeat detector** (normalized word-set
similarity vs. failed approaches for the same signature) journals
``MEMORY_REPEAT_WARNING`` and stamps the warning into the fix task.
**Placement:** memory lives in the ORIGIN repository's ``.agent/`` — outside
run worktrees — so it persists across runs/branches and cannot enter a
run's diff; the Git Agent additionally excludes ``memory.db*`` and
``lessons_learned.json`` from staging (in-place mode); the store is lazily
created so read-only operations (``ezai plan``, ``ezai memory``) leave zero
traces.
**Consequences:** Cross-run learning with a full audit trail
(``MEMORY_INJECTED/RECORDED/REPEAT_WARNING/DISTILLED`` events); memory is
inspectable (SQLite + JSON) and per-repo portable; teams should gitignore
``.agent/memory.db*``; unbounded growth is deferred to a retention policy
(revisit when stores exceed practical prompt-selection sizes).
