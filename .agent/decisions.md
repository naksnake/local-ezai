# Architecture Decision Records

> One entry per decision. Statuses: Proposed / **Accepted** / Superseded(by).
> New states in the workflow machine, new agents, or deviations from
> [docs/TARGET_ARCHITECTURE.md](../docs/TARGET_ARCHITECTURE.md) require a new
> ADR here, in the same PR.

Index: [001](#adr-001) [002](#adr-002) [003](#adr-003) [004](#adr-004)
[005](#adr-005) [006](#adr-006) [007](#adr-007) [008](#adr-008)
[009](#adr-009) [010](#adr-010) [011](#adr-011) [012](#adr-012)
[013](#adr-013) [014](#adr-014) [015](#adr-015) [016](#adr-016)
[017](#adr-017) [018](#adr-018) [019](#adr-019)

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

## ADR-018 — Production CLI `local-ezai`: path-first UX, pipeline subsets as commands, in-place vs. worktree semantics
**Date:** 2026-08-17 · **Status:** Accepted (realizes the `ezai` CLI of
TARGET_ARCHITECTURE §10; pulls the Reviewer agent forward from Phase 4 of
the roadmap)
**Context:** The Phase 1–4 `ezai` CLI was developer plumbing
(`--repo` everywhere, run/plan only). Production use needs the Claude-Code
shape: point the tool at a project (`local-ezai .`,
`local-ezai /path/to/CRM`, or just the cwd), then chat, plan, run, code,
test, fix, review, commit, inspect memory, or execute a whole sprint spec —
cross-platform including Windows.
**Decision:**
(1) **Path-first selection**: a leading directory argument or git-style
`-C` selects the project; commands default to the cwd; a bare path opens
the memory-aware chat REPL.
(2) **Commands are pipeline subsets over the existing agents**, not new
machinery: `plan` (Planner), `code` (Planner+Coder, changes left
uncommitted), `test` (Validator+Browser QA), `fix` (a second graph entry at
VALIDATE with a synthetic task-less plan — the schema now permits empty
task lists while the Planner still rejects them), `review` (a new read-only
**Reviewer Agent** with structured verdict/findings, memory-style-aware),
`commit` (validation-gated Git Agent), `memory`, `sprint`.
(3) **In-place vs. worktree split**: commands that serve the user's own
working tree (`test`, `fix`, `review`, `commit`) run in place on the
current branch; generative commands (`run`, `code`, `sprint`) stay
worktree-isolated.
(4) **Sprint semantics**: a markdown spec (checklists > bullets > numbered)
runs task-by-task as full pipelines **in place on one shared
`sprint/<id>` worktree branch**, one commit per task, one LLM client shared
across tasks, stop-on-failure by default.
(5) **Cross-platform**: pathlib throughout; validation autodetection uses
`sys.executable` (no `python3` assumption); Browser QA app processes use
POSIX process groups or Windows `CREATE_NEW_PROCESS_GROUP`/`taskkill /T`;
packaging via console-script shims (pipx/pip, INSTALL.md), verified wheel
build.
**Consequences:** One discoverable command with stable exit codes
(0/1/2/3/130); the legacy `ezai` CLI remains for scripts and run
inspection; the commit gate now guards *human* commits too
(`local-ezai commit` refuses on red validation); a run_sprint bug class
(per-task client rebuilds resetting scripted/stateful providers) is locked
in by tests.

## ADR-019 — Autonomous sprint execution: Sprint Agent DAG + deterministic wave scheduler + worktree-per-task parallelism
**Date:** 2026-08-17 · **Status:** Accepted (realizes the sub-agent
parallelism of TARGET_ARCHITECTURE §4 / AGENT_DESIGN §4 at sprint
granularity)
**Context:** Phase 5 sprints ran a spec's checklist items sequentially with
no requirement analysis and no parallelism. Phase 6 requires requirement
analysis, task breakdown, a dependency graph, parallel agent execution,
validation + Browser QA, documentation, and git commits from one
``sprint.md``. The central risk of parallel autonomous coding is
git-level interference between concurrent agents.
**Decision:**
(1) A **Sprint Agent** (read-only tools) performs requirement analysis and
task breakdown into a schema-validated ``SprintPlan`` with explicit
``depends_on`` edges. **DAG integrity is code, not trust** (ADR-010):
duplicate ids, unknown/self dependencies and cycles are detected
deterministically (Kahn) and fed back to the model through the bounded
structured-output retry loop.
(2) A **deterministic wave scheduler** groups tasks into topological
waves. Single-task waves run in place on the sprint worktree; multi-task
waves give **each task its own worktree + branch forked from the sprint
tip**, execute them concurrently (thread pool bounded by
``sprint.max_parallel``), then **merge task branches back in plan order**
(``--no-ff``). A merge conflict marks that task failed and aborts cleanly —
parallel tasks are expected to touch disjoint files, and the dependency
graph is the mechanism that encodes that expectation. Task worktrees and
branches are removed after merging.
(3) **Every task is the full pipeline** (Planner→Coder→Validation→Browser
QA→self-healing→Memory→gated commit); dependents of failed tasks are
skipped (precise reason recorded); project memory resolves through
worktrees to the origin repo, so parallel tasks share one store
(SQLite write-lock timeout added).
(4) **Documentation as a deliverable**: ``docs/sprints/sprint-<id>.md``
(goal, requirements, mermaid dependency graph, per-task outcomes with
validation summaries) is generated deterministically from the run reports
and committed as the sprint's final commit only when the sprint is green.
(5) LLM concurrency: one shared client by default (HTTP clients are
thread-safe); an ``llm_factory(task)`` seam provides per-task clients —
required for scripted/stateful providers in parallel waves and used by the
test suite. ``--simple`` preserves the Phase 5 sequential path.
**Consequences:** Independent tasks genuinely execute in parallel with
isolation inherited from the worktree model (ADR-014); merge conflicts are
surfaced as task failures rather than corrupted branches; sprint-level
observability lands in a dedicated journal (SPRINT_PLAN/WAVES/
WAVE_STARTED/TASK/MERGE_CONFLICT/DOC). Cross-task semantic conflicts that
merge cleanly remain undetected until validation of later waves — a
Reviewer-over-the-whole-sprint gate is future work.

## ADR-020 — Production governance: model registry + fallback routing, evaluation harness, Documentation & Evolution agents, forge PR delivery, self-hosting
**Date:** 2026-08-17 · **Status:** Accepted (closes the CLAUDE.md
Documentation/Evolution/Model-Routing/Bootstrap-Exit mandates; final
production-readiness review)
**Context:** The final readiness audit found the platform could plan,
code, validate, heal, remember, and sprint — but could not govern its
model routing declaratively, could not generate PRs, had no Documentation
or Evolution agents, and could not target itself. CLAUDE.md mandates an
exact `agent_model_map`, mandatory documentation, an evolution workflow
ending in human-approved PRs, and a bootstrap exit
(Human → Roadmap → Local-EZAI).
**Decision:**
(1) **Model governance is data, not code**: per-repo
`.agent/model_registry.yaml` (`agent_model_map:` — primary + fallback per
role) parsed by `model_registry.py` and applied at `prepare_run` into
`llm.roles` / `llm.role_fallbacks`. The LLM client walks
`[primary] + fallbacks` per request (journaled `LLM_FALLBACK`), raising
only after the chain is exhausted. `evaluate-models` probes every role
(structured-output roles must return valid JSON), measures latency, and
writes `.agent/model_benchmarks.json` — the evidence artifact for
human-approved routing changes.
(2) **Documentation Agent** (role `documentation`, write-tools limited to
the worktree): generates/refreshes USER_GUIDE, OPERATION_MANUAL,
MAINTENANCE_GUIDE, RELEASE_NOTES under `docs/`; results derived from
`git status --porcelain -uall`, left uncommitted for review.
(3) **Evolution Agent + pipeline** (`evolution.py`): deterministic
evidence gathering (memory history, failed fixes with repeated-signature
flagging, recent run reports, roadmap head) → schema-validated proposal
(≤3 improvements) → full execute_run pipeline per improvement, sequential,
on an `evolve/<id>` worktree → timed before/after benchmark → dated
RELEASE_NOTES entry (green only) → PR delivery. **The pipeline has no
merge step by construction** — it terminates awaiting human review.
(4) **Forge abstraction** (`forge.py`): PR delivery kinds `none`
(PR_PROPOSAL.md bundle, default), `gh` (GitHub CLI), `api`
(GitHub/Gitea-compatible REST, token via `$FORGE_TOKEN`). Configured
globally, never from repo overrides; push remains double-gated
(`git.allow_push` AND `--push`).
(5) **Self-hosting / bootstrap exit**: a root `.agentd.yaml` wires the
platform's own ruff + pytest commands, making Local-EZAI a first-class
target of its own agents (`local-ezai . test|fix|docs|evolve`). A `type`
validation category joins lint/build/test (CLAUDE.md).
**Consequences:** Model replacement becomes a reviewable data diff with
benchmark evidence; the platform can document and improve itself with the
human as the only merge authority; four new CLI commands
(docs/evolve/roadmap/evaluate-models); the agent roster reaches 11.
Accepted residual risk: forge `api` tested against a stub only; live
forges validated operationally.

## ADR-021 — sandboxd: one policed executor for every agent command (allowlist → host/Docker → audit)
**Date:** 2026-09-01 · **Status:** Accepted (closes the interim posture of
ADR-014)
**Context:** ADR-014 shipped host-subprocess execution as an accepted
interim risk. The v1.0 hardening sprint requires container execution
"whenever possible", restricted filesystem access, resource limits, a
command allowlist, and an execution audit log — without breaking
docker-less environments or projects whose checks need their own
toolchain.
**Decision:** A per-run `Sandbox` (sandbox.py) attached to the workspace
at `prepare_run`; `run_command` (the shared path of `exec_run`, all
validation categories, debugging reproductions, and evolution benchmarks)
delegates to it. Three layers in every mode: (1) regex **command
allowlist** — empty allows all (backward compatible), non-empty is
fail-closed; (2) execution — `host` (the ADR-014 behavior) or `docker`:
`docker run --rm` mounting ONLY the workspace at its **host-identical
path** (worktrees also mount the origin `.git`), `--network none` default,
`--memory/--cpus/--pids-limit`, explicit env passthrough, host-side
timeout + `docker kill`; (3) **audit** — every execution, refused ones
included, appended to `<run-dir>/exec_audit.jsonl`; resolved mode
journaled (`SANDBOX_MODE`). Mode `auto` (default) uses docker only when
the daemon answers AND `sandbox.image` is configured — a meaningful image
is an operator decision, so the docker-less and unconfigured cases stay
byte-identical to ADR-014. Repo `.agentd.yaml` cannot configure the
sandbox (a repo must not weaken its own isolation). Git tools and the
Browser QA app launch remain host-side (delivery mechanism / needs a local
port).
**Consequences:** Agent shell execution can no longer touch the host
filesystem or network when an image is configured; every execution is
auditable; strict `docker` mode fails loudly. Residual: git/browser
processes host-side; container escape hardening (user namespaces, seccomp)
deferred to N1′.

## ADR-022 — Mandatory reviewer gate: REVIEW between green validation and commit
**Date:** 2026-09-01 · **Status:** Accepted
**Context:** The Reviewer Agent existed only as a CLI command; the M4
remainder ("reviewer in the pipeline") and the hardening mandate require
review before every commit, blocking on critical issues, with structured
reports and security/architecture/maintainability detection.
**Decision:** A `review` graph node after `validate` (green) and before
`git`, in both compiled graphs (run and fix pipelines) — validation incl.
Browser QA runs first so review judges working code and expensive review
cycles are never spent on red changes. The reviewer receives the full
uncommitted change set via `collect_review_diff` (tracked `git diff HEAD`
**plus untracked file contents** — new-file-only changes must not bypass
the gate; machine-managed `.agent/` state excluded). Blocking policy in
code (`review_blocked`): `request_changes` always blocks; findings at
`review.block_severities` (default `high`) block even under `approve`.
Blocked ⇒ run FAILS with the `ReviewReport` in `report.json` +
`REVIEW_GATE` journal event; no healing loop on judgment. `ReviewFinding`
gains a mandatory-taxonomy `category`
(security/architecture/maintainability/correctness/performance/testing/
style/other). The gate covers `run`/`fix`/`sprint` tasks/`evolve`
improvements AND `local-ezai commit` (runner-level, same policy).
`review.enabled` is global-config only — a repo cannot disable its own
gate. The Git Agent additionally ignores machine-managed `.agent/` status
lines (the code index now exists before the git node).
**Consequences:** Nothing commits unreviewed; scripted tests carry one
reviewer response per green pipeline; the workflow is PLAN → CODE →
VALIDATE(+Browser QA) → [DEBUG → FIX → REVALIDATE]* → REVIEW → COMMIT →
(opt-in) PUSH/PR.

## ADR-023 — Semantic code intelligence: ast/Tree-sitter symbol index + import graph in .agent/code-index/
**Date:** 2026-09-01 · **Status:** Accepted (first slice of M5's codeidx)
**Context:** Agents located code by grep alone; the hardening mandate
requires symbolic repository understanding (symbols, functions, classes,
dependency graph), persisted, serving Planner/Coder/Debugger/Reviewer.
**Decision:** `code_intel.py` builds a per-repo index: Python via stdlib
`ast` (always), JS/TS/Go/Rust via optional Tree-sitter grammars
(`agentd[intel]`; graceful degradation). Persisted under the ORIGIN repo's
`.agent/code-index/` as `symbols.json` (content-hash-keyed cache →
incremental refresh at `prepare_run`, journaled `CODE_INDEX`) and
`graph.json` (import edges resolved to repo files + most-imported
hotspots). Consumption: a budgeted **repository map** injected into the
Planner prompt; a read-only `code_symbols` tool (T0) for
Planner/Coder/Debugger/Reviewer. `plan` builds its index in memory only
(traceless promise kept); the index is machine state — never staged,
excluded like memory files.
**Consequences:** Plans reference real modules; symbol lookup is exact and
cheap; Qdrant-backed similarity search (N3′) can layer on top without
replacing the symbolic index.

## ADR-024 — Model transparency & benchmark dashboard: models/explain-run, run metrics, trend history
**Date:** 2026-09-01 · **Status:** Accepted
**Context:** Routing lived in the registry but was not inspectable; runs
did not record which model (after fallbacks) actually served each role;
evaluate-models measured availability but not quality or drift; evolution
proposed without benchmark evidence.
**Decision:** (1) The LLM clients track fallback-aware `models_used`
(role → serving model); every pipeline persists it in `report.json`.
(2) `local-ezai models` prints the live per-role primary/fallback routing
exactly as a run resolves it; `local-ezai explain-run [id]` attributes
each stage of a run (deterministic stages labeled as such: Validation
harness, Playwright). (3) `evaluate-models` additionally aggregates run
history into `RunMetrics` (planning accuracy, coding success, validation
pass, debugging success, review approval, heal iterations, wall clock),
rolls a capped 20-entry trend history inside
`.agent/model_benchmarks.json`, and `--report` renders
`docs/MODEL_GOVERNANCE_REPORT.md`. (4) `gather_evidence` (evolution) reads
those trends — regressions, latency drift, weak quality rates — as
first-class evidence, and the Evolution prompt forbids re-proposing failed
experiments recorded in memory.
**Consequences:** "Which model did what" is answerable per run and per
role; routing PRs carry measured evidence; evolution is driven by
benchmark feedback, closing the mission's self-improvement loop.

## ADR-025 — Productization architecture: one control plane, declarative state, two management surfaces
**Date:** 2026-09-01 · **Status:** Accepted (architecture only — Phases
P1–P6 of docs/V1_IMPLEMENTATION_PLAN.md implement it; ADR-027..031
reserved for the as-built decisions)
**Context:** The platform is production-ready but operated like an
engineering project: `make` targets, hand-edited LiteLLM config, per-repo
registries, CLI-only SWE. Productization requires an integrated product —
OpenWebUI + runtime + Autonomous SWE + self-evolution + model governance —
where users edit `.env` once at installation and manage everything
afterwards through OpenWebUI or the `local-ezai` CLI, with model lifecycle
(install/activate/benchmark/rollback/upgrade/explain), logical roles
(orchestrator/planner/coder/debugger/reviewer/memory/chat), model groups
(reasoning/coding/chat), and providers (llama.cpp/vLLM).
**Decision (extension-only, no redesign):**
(1) **One control plane** — `ezaid` (:8010, OpenAPI) wrapping the Python
functions that already exist (agentd pipelines, evaluate, registry,
compose ops). CLI (connected mode), Admin Center, and the SWE tool server
are thin clients; the CLI keeps a fully offline **direct mode** for repo
work, preserving every existing behavior and test.
(2) **Declarative state, rendered artifacts** — Registry v2
(`config/models/registry.yaml`: models × lifecycle states, ordered groups
reasoning/coding/chat, roles with group+pin; resolution role→group→first
ACTIVE = primary, rest = fallbacks — feeding the unchanged ADR-020 runtime
mechanism; per-repo `.agent/model_registry.yaml` overrides preserved) +
PAL provider descriptors (`config/providers/*.yaml`) rendering the engine
slot materialization and a **generated** LiteLLM config. Mutations create
immutable, git-committed **generations**; rollback = re-render generation
N. Humans never edit rendered artifacts (drift detection refuses).
(3) **Governed lifecycle** — state machine registered→installed→
benchmarked→active→retired; activation/upgrade require human approval via
one Governance queue (shared with evolution PRs and releases); rollback is
immediate but audited; the first-run wizard's generation 1 is the only
auto-approved activation.
(4) **OpenWebUI as front door, never forked** — SWE tool server as an MCP
server behind mcpo (ADR-003 pattern): start/inspect tools only, no
governance mutations reachable from chat (prompt-injection ceiling); an
`orchestrator` role/persona (new logical role, reasoning group) drives
chat-ops; Admin Center = evolution of the existing monitor (:8888),
rendering only what ezaid serves.
(5) **Invariants restated as product law** — engine slot always named
`vllm` on :8000 behind LiteLLM (ADR-001); one active engine slot in V1
(second slot schema-ready, deferred); chat stack byte-identical (ADR-002);
sandbox + reviewer gate on every chat-originated run (ADR-021/022);
agents propose, humans approve (CLAUDE.md).
**Consequences:** `.env` becomes installation-only; `make` becomes the
installer/developer layer; CLI grows additive namespaces (`model`,
`governance`, `project`, `status`, `up/down`); parity between CLI and
WebUI is a release-gated test (parity harness), and the Admin Center is
validated by the platform's own Browser QA agent. Nine architecture
documents define the target: TARGET_PRODUCT_V1, OPENWEBUI_INTEGRATION,
WEBUI_ADMIN_CENTER, MODEL_ROUTING_DESIGN, MODEL_LIFECYCLE_MANAGEMENT,
PROVIDER_ABSTRACTION, CLI_AND_WEBUI_STRATEGY, FIRST_RUN_EXPERIENCE,
V1_IMPLEMENTATION_PLAN.

## ADR-026 — Agnosticism review: roles are the interface; hardware, runtime, and models are data
**Date:** 2026-09-01 · **Status:** Accepted (amends ADR-025; architecture
only — implemented within plan phases P1/P5/P6)
**Context:** The V1 product must be hardware-, runtime-, and
model-agnostic: users choose CPU/GPU (any vendor), llama.cpp/vLLM/future
runtimes, and any model family without architecture changes. Review of the
productization docs found real couplings: per-hardware model variable
families in `.env` (CHAT_MODEL vs CPU_CHAT_MODEL vs N97_MODEL_*) with
three hand-maintained LiteLLM configs; SKU-shaped profiles (n97);
a concrete model name in agentd code defaults; the engine service's
vLLM-specific name; tool-calling assumed via one parser; a catalog at risk
of becoming a blessed list; raw model names as the chat UX
(docs/V1_PRODUCT_REVIEW.md, CF-1..CF-10).
**Decision:**
(1) **Logical roles/groups are the only stable names.** LiteLLM serves
role aliases (`role-planner`, …); agentd defaults bind to aliases —
completing ADR-007 so no repository code contains a model name. `.env`
seeds groups once (`AI_RUNTIME`, `REASONING_MODEL`, `CODING_MODEL`,
`CHAT_MODEL`, sources as `hf:`/`gguf:` URIs or catalog ids, `auto`
supported), consumed at bootstrap into generation 1 and never re-read;
legacy per-profile variables migrate automatically. The five-step FRE
(clone → edit .env → make setup[-cpu|-gpu] → open WebUI → use everything)
runs with zero interactive prompts (docs/FINAL_FIRST_RUN_EXPERIENCE.md).
(2) **Capability classes replace SKU profiles** (accel-large/accel-small/
cpu-standard/cpu-low from a detected capability vector; `n97` = preset
alias of cpu-low; make targets unchanged). Hardware knowledge is legal in
exactly one place: runtime-descriptor (runtime × accelerator-kind) data.
One pure `fit()` function powers recommendations/validation and never
blocks explicit user override (docs/HARDWARE_AGNOSTIC_ARCHITECTURE.md).
(3) **Runtime = descriptor + images behind six verbs** (materialize,
start/stop/restart, ready?, validate_model, bench, capabilities); the
compose service keeps its historical `vllm` name with an additive neutral
network alias `engine` used by all new consumers; capability negotiation
makes tool-calling a checked property of the (runtime × chat-template)
pair at render time. Acceptance: the mock third-runtime drill must pass
with zero diffs outside descriptors (docs/RUNTIME_ABSTRACTION_STRATEGY.md).
(4) **Governance ruling — govern roles, REVEAL models** (option B amended,
never hidden): group/role/pin/runtime changes are the approved objects;
model names remain visible everywhere evidence matters. Role contracts
(declarative capability requirements per role) close the loop. The
chat selector leads with role entries; raw names behind an advanced
toggle (docs/MODEL_GOVERNANCE_V2.md, docs/WEBUI_PRODUCT_STRATEGY.md).
(5) **Evolution is an advisor, never an operator**: benchmark readings,
fit-checked model recommendations, and pre-filled activation proposals
into the one governance queue (`proposed-by: evolution`, rate-limited,
local-evidence-required); no write path to generations exists; rejections
are recorded to memory and never re-proposed. Humans remain the final
authority on every activation.
**Consequences:** Implementation ADRs renumber to ADR-027..031;
plan P1 absorbs remediations R-1..R-5, P5 implements the final FRE,
P6 gains gates H1–H4 + the third-runtime drill; the platform can adopt a
new GPU vendor, runtime, or model family as pure data — which is the
definition of done for agnosticism.

## ADR-027 — Registry v2: platform model registry with generations (P1)
**Date:** 2026-09-01 · **Status:** Proposed (enters with PR-1; flips to
Accepted when phase P1 closes at PR-7)
**Context:** ADR-025/026 require a platform-scope, declarative,
generation-versioned model registry — roles → groups → models →
runtimes — replacing hand-edited routing after installation, resolvable
deterministically, rollback-able, and reproducing the CLAUDE.md
`agent_model_map` byte-for-byte through the existing ADR-020 runtime
mechanism.
**Decision (PR-1 slice):** `registry_v2.py` implements the schema
(models × lifecycle states registered/installed/benchmarked/active/
retired/failed — only `active` is resolvable; ordered groups; roles with
group + pin + `requires` capability contract), referential-integrity
validation, deterministic resolution with explain reasons, an aggregated
resolution-completeness check, ADR-020-shape `role_map()` (fallback keys
only when non-empty), an append-only immutable generation store with
atomic current-file replacement, and human-readable generation diffs.
**As-built refinement:** a pin is an EXPLICIT ordered chain and does not
inherit group fallbacks — the golden test (reference data vs the live
ADR-020 registry of this repo) is unreproducible otherwise
(`reviewer: llama3` has no fallback). Unservable generations are refused
at write time. The reference default set (CLAUDE.md map) ships as
packaged data in `agentd/defaults/` — model names live in data, never
code (R-1/H1). No consumers are wired in this slice (PR-3/6/7); the live
`config/models/registry.yaml` instance is created by bootstrap (PR-7).
**Consequences:** later PRs build on a store whose invariants
(immutability, write-time servability, byte-compatible role maps) are
already tested; the golden test becomes the standing tripwire for the
PR-6 role-alias switch.
