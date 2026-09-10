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
**P6 slice (2026-09-09, PR-24) — the parity harness as the release gate:**
CLI_AND_WEBUI §7's consistency test exists as `agentd/tests/parity/`
(`make swe-parity`; part of `make release-gate` and of the manual CI
workflow). Decisions taken in it: (1) **identical worlds, not shared
state** — each surface acts on its own copy of the platform, so what is
compared is *results*, never one surface reading what another wrote; (2)
**the API is called as the console** (`X-EZAI-Client: admin-center`), so
the matrix's third column exercises its own identity path; (3)
**equivalence is defined on three planes** — response bodies, declarative
state, operation audit — and **the transport's annotation is normalised
explicitly, as data**: the `via <client>` suffix, the daemon's `api.*` /
`run.*` / `control.*` / `auth.*` events (equal between the two daemon
surfaces, absent in-process), timestamps, run ids, hashes, durations and
each world's paths; (4) **absence is asserted, never skipped** — repo-work
verbs (`run`, `plan`, `memory`) never probe a daemon and equal the API's
runs report for report and journal for journal, `up`/`down` stay direct
even when connected is requested and no contract operation exists, the
memory verb's record is the store itself while the daemon's curated add is
also platform-audited (PR-19's decision, pinned); (5) the §3 table is
parsed and every row needs a harness row and a chat-ceiling classification,
and every mutation the contract offers must be classified by a row. One
defect found and fixed in the same PR: `model rollback --json` printed its
ROLLBACK notice before the JSON, so `--json` was not one document.
**P6 slice (2026-09-09, PR-25) — the agnosticism gates (2) and (3) as
tests:** `agentd/tests/gates/` (`make swe-gates`, in `make release-gate` and
the CI workflow). (1) **The third-runtime drill is a fixture, not a
product runtime**: `mockengine` lives under `tests/fixtures/providers/`,
its image is an in-test OpenAI-API stub, and the drill copies the descriptor
into a checkout and runs bootstrap → real side-load validation and
benchmark → render → day-2 install / benchmark / activate / approve /
rollback → the pipeline's wait-ready → `up --rendered` through the CLI; a
grep tripwire proves no shipped code, data, compose file or script names
the runtime. The fixture is deliberately unlike the shipped descriptors
wherever the contract allows (readiness path, timing keys, served-id
template, mount paths), so a silent assumption of a shipped value shows.
(2) **H1 is a test with its exceptions as data, each with a reason, and a
stale exception fails** — the scan covers code, packaged data, prompts, the
console, the tool server and the continuity layer; `config/providers/` is
the sanctioned home; the frozen chat stack's legacy profile files and
comments are pinned rather than edited (ADR-002). (3) **What the gates
found:** the resolver's default runtime for a `gguf:`/`hf:` source was the
first descriptor serving the format alphabetically — with a third runtime
present, `model install <source>` would install for another runtime than
the slot's; fixed (`lifecycle.slot_runtime_for`: the slot's runtime when it
serves the format, else the format rule — invisible while each format has
one server). Four brand words in user-facing text were reworded (the
`up-n97` help, a prompt usage note, a legacy script header, a monitor
comment). Recorded as a residual for the release train, not fixed here:
the control plane's health table probes the engine slot at its own
`/health` rather than the active descriptor's readiness path (the
pipeline's wait-ready is descriptor-driven; the sweep is operator data);
and a day-2 `model install` of an undeclared user source carries no
tool-call format (the bootstrap's generic default applies to seeds only).
**P6 slice (2026-09-09, PR-26) — the release train, up to the human
steps:** the five guides absorb the V1 surfaces; `docs/RELEASE_NOTES.md`
exists with the v1.0.0 entry (CLAUDE.md's mandated fourth guide);
`docs/SOAK_RUNBOOK.md` + `scripts/soak.sh` (`make soak`) make P6 exit
criterion 2 a fixed, logged schedule — health and status every 15 min,
bench hourly, a scripted SWE run every 2 h, lifecycle churn (benchmark → a
new generation → rollback) every 6 h as the rollback-under-load exercise,
an evolution cycle and container stats daily, one JSON line per step and a
summary the release report quotes; `docs/V1_RELEASE_REPORT.md` checks the
product DoD item by item against tests and PR artifacts and ends in the
sign-off table. Decisions: (1) **the version is bumped in the release PR**
(`__version__` and `pyproject.toml` both 1.0.0 — they disagreed before) as
MAINTENANCE_GUIDE §6 prescribes, **the tag is not**: a test asserts no
`v1.0.0` tag exists in the repository, because production releases are a
human act (CLAUDE.md, GOVERNANCE.md); (2) **soak results are a hardware
measurement** — the PR delivers the runbook, the driver and the results
template with the offline evidence filled in and marks the host rows
pending, rather than claiming a soak that did not run; (3) the two PR-25
residuals are **known limitations in the release notes**, not fixed in the
train. With PR-26 the V1 plan's twenty-six PRs are delivered; ADR-025..031
stand as built.

## ADR-027 — Registry v2 · PAL · governed model lifecycle (P1)
**Date:** 2026-09-01 · **Status:** **Accepted** (2026-09-08, phase P1
closed with PR-7; entered as Proposed with PR-1)
**As built (PR-1..7):** the platform's model state is a **generation-
versioned Registry v2** (`config/models/registry.yaml` + immutable,
append-only `generations/`; rollbacks are new generations) resolved
deterministically role → pin | group → first active model; **runtimes are
descriptors** (`config/providers/*.yaml`: six-verb contract, image table per
accelerator kind, tuning per capability class) and the **renderer** turns
registry + descriptors + the host's capability vector into the LiteLLM
config (model + `role-*` aliases → `engine:8000`), the engine-slot compose
override, the ADR-020-shaped role map, and a capability report, with
render-time **capability negotiation** over every role's chain and a hash
manifest that refuses drift; the **lifecycle** installs (resumable,
checksummed, validated through a side-loaded engine), benchmarks, and
retires/uninstalls with guards; **governance** is a file-backed
change-request queue with an append-only audit log, a computed approval
matrix (policy-approved when no serving role or slot runtime changes),
a bounded evolution lane, and an **atomic apply** (dry render → snapshot →
write → reload changed → health → self-rollback on any failure);
**agentd binds to role aliases** (no model name in code) and resolves
aliases < platform role map < per-repo ADR-020 registry; the
`local-ezai model|governance|project|status|up|down|bootstrap` namespaces
are the direct-mode CLI; the **bootstrap** consumes `.env` seeds once
(legacy families migrated, F8/F10/F11) into generation 1 with the one
implicit approval, and the **cutover** makes the rendered artifacts the live
ones. Reference default set: the CLAUDE.md map as packaged data, golden-
tested against `.agent/model_registry.yaml` throughout.
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
**PR-3 slice (2026-09-08) — runtime descriptors + renderer (ADR-026
R-1/R-3/R-4):** runtimes are YAML descriptors in `config/providers/`
(schema `runtime_descriptor.py`): served formats, capabilities
(tool-call parsers, JSON output, `parallel_models`, `hot_swap`), the
six-verb contract as data (`materialize` realized; `control`/`ready`/
`validate_model`/`bench` declared for PR-4/5), image table keyed by
**accelerator kind**, tuning keyed by **capability class**, templated
commands/compose extras. `render.py` fills templates only — it holds no
engine, image, flag, or vendor string (tested) — and produces
`litellm-config.yaml` (model + `role-*` aliases → `engine:8000`),
`docker-compose.engine.yml` (`deploy` under compose `!override`),
`role_map.yaml` (ADR-020 shape; golden-chained to this repo's
`.agent/model_registry.yaml`), `capability_report.yaml`, and a router
`engine-models.ini` when llama.cpp serves several models. **Capability
negotiation** at render time covers primaries **and fallbacks**
(tool_calling / json_output / min_context vs class budget / source
format); the one-slot rule (one runtime; `parallel_models: false` ⇒ one
active model) is enforced; all problems aggregate into one loud error.
`write_rendered` persists to `<config>/rendered/` with a hash
`manifest.yaml` and **refuses drift** unless forced. The compose slot
service gains the neutral network alias `engine` (name, port, behavior
unchanged). Parallel path until the PR-7 cutover; no process verb runs in
this slice. As-built refinements: fallbacks negotiated (not only
primaries); llama.cpp multi-model via router preset (upstream-verified);
the reference default set spans two runtimes and is therefore reported as
a slot conflict — LiteLLM/role-map rendering remains independently
callable for explain/approval surfaces.
**PR-4 slice (2026-09-08) — lifecycle install / validate / benchmark:**
`fetch.py` resolves `hf:` · `gguf:` (url / `hf://org/repo/file` / path) ·
catalog id · `auto` (every other form is rejected with the accepted forms
listed, F8) and fetches GGUF natively (HTTP-Range resume, streaming
SHA-256 recorded into `source.sha256`) or hub repositories through the
`hf download` path the scripts already use (local CLI or the same
throwaway container). `catalog.py` is data with pluggable sources
(packaged `defaults/catalog.yaml` seed — hub-verified sizes — merged with
`config/catalog/*.yaml`) and a requirements-driven recommender: variants a
runtime serves × the group's role contracts × `fit()` placement, every
candidate returned with its verdict (R-5/F9). `lifecycle.py` holds the
state machine as a data table (`TRANSITIONS`, enforced by `transition`),
`install()` (resolve → fetch into the descriptor's `weights.host_dir` →
`validate_model` via a **side-loaded** engine → `installed` with measured
`size_gb` | `failed` with the reason; idempotent on verified weights) and
`benchmark()` (`bench` verb: descriptor-named server timings or
tokens/wall-clock → entry `benchmarks` + capped per-model series under a
new `models` key of `.agent/model_benchmarks.json`, carried forward by
`evaluate-models`; → `benchmarked`). The side-load is the PR-3
`materialize_service()` of one model as a standalone compose project on an
ephemeral port, always torn down. Generations are persisted when the
registry is servable; pre-bootstrap results stay in memory and say so
(PR-1's write-time invariant is respected, not worked around). Additive
schema: `ModelEntry.artifact/installed_at/error/license`,
`BenchVerb.rate_key/prompt_rate_key/count_key`, `ModelEvalReport.models`.
Not in this slice: activation/rollback/retire (PR-5), CLI verbs (PR-6),
the vLLM side-load memory gate / scheduled swap window (gating lands with
activation), curated per-class catalog content (P5).
**PR-5 slice (2026-09-08) — activate / upgrade / rollback / retire +
governance queue:** `governance.py` is the file-backed queue
(`config/governance/queue/<id>.yaml`, one file per request for life) and
append-only audit log (`log.jsonl`); a `ChangeRequest` carries the full
proposed generation, the diff, **affected roles** before → after, and
evidence (benchmarks, fit, capability report, runtime). Rules are code:
decisions once, rejection needs a reason, the **approval matrix is
computed** (no role chain or slot-runtime change ⇒ approved by actor
`policy`, audited), the **evolution lane** is bounded (evidence required,
≤1 open, never auto-approved). `activation.py`: `activate`/`upgrade`/
`propose` validate a proposal exactly as apply will (dry render) and
enter the queue; `apply` runs MODEL_LIFECYCLE §4 — stale check
(`superseded`), reconcile, dry render, save N+1, write artifacts
(drift-checked), reload **changed artifacts only**, health — and on ANY
failure past the snapshot **self-rolls-back** by saving N's content as a
new generation (history is append-only; rollbacks are generations);
`rollback` reuses the protocol without approval, audited + notifying;
`reconcile` heals a registry ahead of its rendered manifest. Reload/health
are seams (`ComposeReloader`, `EngineHealth`) defaulting to render-only
until the PR-7 cutover (audited as such). `lifecycle.retire` (non-serving
active only; primary or last member blocked) and `uninstall`
(retired/failed only; rollback target ⇒ `force`). Deferred: CLI verbs
(PR-6), bootstrap gen 1 (PR-7), git-committing generations (bootstrap
flag), the proposal-creating evolution lane (N6′).
**PR-6 slice (2026-09-08) — CLI namespaces + role aliases in code (CF-3
closed):** `config.py` defaults every role to `role-<role>`
(`ROLE_ALIAS_PREFIX`, `LLM_ROLES`; no model name remains in code — tested
by source grep); the shipped hand-written LiteLLM profiles gain the
`role-*` alias entries as data so every profile keeps working before the
cutover. `routing.py` implements the three layers of MODEL_ROUTING_DESIGN
§5 — aliases < platform role map (rendered `role_map.yaml`, else Registry
v2 resolution) < per-repo ADR-020 registry, where a role the repo declares
is taken exactly (repo pin = explicit chain, platform fallbacks not
inherited); the platform is found via `platform.config_dir` /
`$AGENTD_PLATFORM__CONFIG_DIR` or by walking up from the project, never
from cwd or the package (hermetic). `prepare_run`, `evaluate-models`, and
`models` share the resolution. `platform_cli.py` adds the direct-mode
namespaces `model install|benchmark|activate|upgrade|rollback|retire|
uninstall|explain|history|catalog`, `governance list|show|approve|reject`
(approve applies the PR-5 protocol; policy-approved proposals apply at
once), `project add|list|remove` (`config/projects.yaml`, audited),
`status`, `up|down` (compose wrappers; profile file chain derived from the
profile name). Behavior note: the default routing path changes as the plan
foresaw (one pre-existing assertion of the old hard-coded default updated);
the PR-1 golden test passes unchanged — the tripwire held.
**PR-7 slice (2026-09-08) — bootstrap core + `.env` seed consumption +
cutover (P1 closes):** `bootstrap.py` reads the V1 seeds once (`AI_RUNTIME`,
three group seeds, optional `EZAI_ROLE_PIN_*`, `<SEED>_TOOL_FORMAT`,
`<SEED>_CONTEXT`) or migrates the first legacy `.env` family from the data
table `defaults/legacy_seeds.yaml` into one model for all groups keeping the
served name (F11); validates every problem with its fix before any download
(F8: runtime, scheme, format × runtime, slot capacity, `auto` feasibility,
tool format vs runtime parsers — undeclared user sources default to a
runtime's generic handler only when it lists one — context vs contracts,
pins); installs + benchmarks through the PR-4 verbs; plans generation 1
(reference roles + contracts, groups from seeds, pins); the ONE implicit
approval (`propose(implicit_approval=True)`, refused on a platform with
history — a forced re-run becomes a governed request) → PR-5 apply →
`EZAI_SEEDS_CONSUMED` stamped into `.env`; `--dry-run` shows the F10 diff.
Cutover: compose mounts `config/rendered/litellm-config.yaml`; the three
hand-written variants become test fixtures; `make bootstrap` /
`require-rendered`; every `make up*` adds the rendered engine override and
`make setup-*` bootstraps before `up` (host venv created on demand — the
first departure from "no host Python", by design). ADR-027 → Accepted.

## ADR-028 — `ezaid` Platform Control Plane (P2)
**Date:** 2026-09-08 · **Status:** **Accepted** (2026-09-09, PR-12 — P2
closed: parity smoke, two concurrent runs through the API, kill-the-daemon,
contract frozen at 1.0.0). Entered as Proposed with PR-8.
**As built (PR-8..12):** `agentd.control` — FastAPI behind the optional
`agentd[control]` extra, opt-in compose overlay or host daemon (`make
control-up` / `make control-serve`); bearer service token + forwarded
identity; the platform's single audit log; one error vocabulary shared with
the CLI; 29 operations (health, identity, audit, model lifecycle, generations,
roles, catalog, governance, projects, async runs) — each mutating one
idempotency-keyed and audited, mapped to one CLI verb; the CLI's connected
mode makes the daemon transparent (same text/JSON/errors) and falls back to
direct mode when it is absent; the contract `docs/api/ezaid-openapi.json`
is frozen at `1.0.0` with a tripwire and a frozen inventory.
**Context:** ADR-025 (1) requires one control plane wrapping the Python
functions the platform already has, so that CLI connected mode, the Admin
Center and the SWE tool server are thin clients of one contract and one
audit log, while the CLI's direct mode keeps working with the stack down.
P1 delivered the functions (Registry v2, lifecycle, governance, renderer,
bootstrap) behind the direct-mode CLI; nothing served them.
**Decision (PR-8 slice — service skeleton):** `agentd.control` is a FastAPI
application shipped behind the optional extra `agentd[control]` (the only
web framework in agentd; direct mode and every repo-work verb never import
it) and started as the **opt-in compose overlay** `docker-compose.control.yml`
(`ezaid` on `EZAI_CONTROL_PORT`, default 8010; `./config` mounted as the
platform; ADR-002: rollback = don't start it). Surface of this slice:
liveness `GET /health` (open), `GET /v1/health` (control info + the
`local-ezai status` snapshot + one probe per stack service — the probe
table is **data**, addresses services by compose name and the engine slot
by its neutral `engine` alias, and `control.health_targets` edits it),
`GET /v1/whoami`, `GET /v1/audit`, `/openapi.json`. **Authentication:** a
bearer **service token** (`EZAI_CONTROL_TOKEN`, never defaulted in code —
no token, no start; constant-time compare) plus **forwarded identity**
(`X-EZAI-User`, `X-EZAI-Client`) recorded as the audit actor `<user> via
<client>`; rejected calls are audited without the secret. **Single audit
log:** `agentd/audit.py` (append-only JSONL) extracted from the governance
queue without changing path or record shape — governance transitions and
control-plane events share `config/governance/log.jsonl`. **One error
envelope** `{"error": {"code", "message", "fix"}}` for every failure — the
vocabulary the CLI (connected mode) and the Admin Center print verbatim.
**Contract artifact:** `docs/api/ezaid-openapi.json`, `info.version =
CONTRACT_VERSION` (`1.0.0-draft.8`), tripwire-tested on the contract
surface; `make control-spec` regenerates. `local-ezai status` probes the
control plane.
**Consequences:** PR-9 (lifecycle + governance endpoints, idempotency keys,
shared errors) and PR-10 (async run registry) build on the auth dependency,
the audit log, the envelope and the snapshot; PR-11 gives the CLI its
connected mode against the same contract; PR-12 freezes the contract at
`1.0.0`, adds the kill-the-daemon and two-concurrent-runs tests and flips
this ADR to Accepted. Until then the overlay stays opt-in (`make
control-up`) and no existing behavior changes.
**PR-9 slice (2026-09-08) — lifecycle + governance endpoints:** the PR-6
verbs' orchestration became **shared operations** in `platform_cli`
(returning the CLI's `--json` mapping); the CLI verbs format them and
`control/api.py` serves them — 19 operations on `/v1` (models install /
benchmark / activate / upgrade / retire / uninstall, generations history /
rollback, roles explain, catalog + recommendations, governance list / show
/ approve / reject, projects), each summary naming its CLI verb, the actor
being the forwarded identity. **Parity is tested**: CLI `--json` == API
body for every read verb. **Idempotency keys** (`Idempotency-Key`: replay
with `Idempotency-Replayed`, `409 idempotency_conflict` on a different
payload; one JSON record per key under `config/control/idempotency/`).
**Shared error vocabulary** `agentd/platform_errors.py`: `classify()` maps
the platform exceptions to `{code, message, fix}` + HTTP status + exit code
(`not_found` by the modules' own phrasing); the API returns the envelope,
the CLI prints the same envelope in `--json` mode. Every mutating call is
audited as `api.<operationId>` with the forwarded actor; mutations are
serialized in the daemon; `reload: true` is refused (`reload_unavailable`)
where no docker CLI exists — the shipped container — before the apply
protocol starts. Contract `1.0.0-draft.9`. Deferred: run endpoints (PR-10),
connected mode (PR-11), freeze + phase-close tests (PR-12).
**PR-10 slice (2026-09-08) — run endpoints, the async run registry:**
`control/runs.py` executes the platform's **existing** pipelines (`run` /
`fix` / `sprint` / `evolve` / `plan` = the A0 dry-run) on a bounded worker
pool with the job's id as the run id — the CLI's journals and reports are
what the API serves; records persist under `config/control/runs/` and are
recovered on restart (interrupted jobs → `failed`, audited `run.orphaned`).
**Cancellation without touching the core graph:** queued jobs cancel at
once; a running job's model client is wrapped (`CancellableLLM`) so the
pipeline stops at its next model call. **Limits:** `control.
max_concurrent_runs` + `max_queued_runs` (beyond → `429 too_many_runs`),
one in-place job per project (`409 project_busy`). **The chat-ops
ceiling** is enforced in the registry: runs start only on registered
projects (`resolve_project`, else `404` with the `project add` fix) and
every job runs with `git.allow_push = False`. Endpoints: `POST /v1/runs`
(202), `GET /v1/runs`, `GET /v1/runs/{id}` (+ journal progress),
`/report` (`409 report_pending` while unfinished), `/journal?tail=`,
`POST /v1/runs/{id}/cancel`; `run.submitted` / `run.finished` /
`run.cancel_requested` audited with the forwarded actor. Contract
`1.0.0-draft.10`. Deployment constraint recorded, not changed: a job needs
the project on the daemon's filesystem (host `ezaid`, or the projects
directory mounted at the same path) — the shipped overlay mounts only
`config/`; PR-12/P3 decide the default. Deferred: connected mode (PR-11),
freeze + kill-the-daemon + two-concurrent-runs tests (PR-12).
**PR-11 slice (2026-09-09) — CLI connected mode:** the management verbs
(`model` / `governance` / `project` / `status`) probe the daemon's liveness
once (`control.url` / `EZAI_CONTROL_URL`, default `http://localhost:<port>`,
1 s) and, when it answers, run through the API (`control/client.py::
ConnectedOps` — the PR-9 verb ↔ endpoint mapping, token + forwarded
identity `X-EZAI-User`/`X-EZAI-Client: cli` + a fresh `Idempotency-Key` per
mutation); otherwise in-process as P1 built them. The verb formatters call
`ctx.ops.<operation>()` on a `PlatformContext` (`DirectOps`) or a
`ConnectedContext` — **same text, same JSON, same error object and exit
codes** (parity tested for seven verbs, plus a real uvicorn socket).
`--transport auto|connected|direct` > `$EZAI_TRANSPORT` > `auto`; a
requested `connected` with no daemon, or a reachable daemon with no
configured token, fails fast (exit 2, fix named) — never a silent fallback
that would fork the audit; `bootstrap` / `up` / `down` are host-only and
always direct. `status` shows its `transport:`. Decision recorded: direct
mode keeps P1's in-process management (the strategy's "fail fast" applies
to a requested connected transport and to the no-platform case) — both
modes write one declarative store. Deferred: freeze + kill-the-daemon +
two-concurrent-runs tests (PR-12), the release-gate parity harness (PR-24 —
delivered as `agentd/tests/parity/`; see the ADR-026 P6 slice).
**PR-12 slice (2026-09-09) — phase close, ADR-028 → Accepted:** the P2
exit criteria as tests — (2) two real scripted runs on two repositories
started through `POST /v1/runs`, active together, both completed with
reports, plus a gated pair with a mid-flight cancel; (3) a real `ezaid`
process SIGKILLed after the CLI used it: repo work (`plan`) unaffected,
`status` falls back to direct on the same state, `--transport connected`
fails fast, the audit log shows `control.started` without `control.stopped`;
(4) `CONTRACT_VERSION = "1.0.0"`, artifact regenerated, a frozen inventory
of the 29 operations pinned (a surface change bumps the version — additive
→ minor, breaking → major — and updates the inventory); (1) is the PR-11
parity smoke. **Deployment decision** (deferred by PR-10): two shapes in
V1 — the container overlay (`make control-up`) for model/governance/health
over `config/`, and the host daemon (`make control-serve`, agentd venv) for
SWE runs because it shares the filesystem with the registered projects;
starting the daemon from `make up` stays opt-in (ADR-002; the P5 installer
may wire a default). Stability item fixed in the close: run listing orders
by submission, not by the second-resolution timestamp. P3 (ADR-029), P4
(ADR-030) and P5 (ADR-031) proceed in parallel from PR-12.

## ADR-029 — Chat-ops boundary: the SWE Tool Server (P3)
**Date:** 2026-09-09 · **Status:** **Accepted** (2026-09-09, PR-15 — the P3
close; entered as Proposed with PR-13)
**Context:** OpenWebUI is the product's front door (ADR-025); chat must be
able to start autonomous work and read its results without ever reaching
the governance boundary — a prompt-injected conversation may waste a run,
never ship, merge, activate or roll anything back (OPENWEBUI_INTEGRATION
§5). The control plane (ADR-028) serves everything chat needs.
**Decision (PR-13 slice — the tool server):** a **vendored MCP server**
(`mcp-servers/swe-server/swe_server.py`, FastMCP over stdio) behind mcpo at
`:8200/swe`, a **thin adapter** over the frozen 1.0.0 contract — no
pipeline logic, no repository access, no agentd import; token +
`X-EZAI-Client: swe-server` + idempotency key on its one mutating call. The
**catalog is start + inspect only**: `swe_projects`, `swe_plan` (A0, waits
for the plan, "confirm before `swe_run`"), `swe_run` / `swe_sprint` /
`swe_fix` / `swe_evolve` (run id within seconds, follow hint, Admin Center
link), `swe_status`, `swe_report` (markdown per kind, never raw JSON),
`swe_journal` (bounded), `model_list`, `model_explain`, `governance_queue`
(read-only, deep links, "decisions never from chat"). Approve / reject /
activate / rollback / upgrade / retire / uninstall / install / merge / push /
cancel **do not exist as tools** — pinned by a negative test on the catalog
and on the API paths the source uses (the only POST is `/runs`). Policy
lives in the control plane (allowlist, no push, limits); the tool renders
its refusals as answers for the model. Registered in `config/mcpo-config.json`
+ the mcpo image; mcpo reaches the daemon at `EZAI_CONTROL_URL_MCPO`
(overlay `http://ezaid:8010` or `host.docker.internal` for a host daemon).
**Consequences / deferred:** `swe_test`, `swe_review`, `model_benchmark`
need `validate`/`review` run kinds and an evaluate endpoint — a **1.1
contract addition** in a later P3 slice, not a reopening of P2. The
OpenWebUI connection + the Orchestrator persona are PR-14; negative-test
hardening and the prompt-injection drill are PR-15 (→ Accepted). Identity:
mcpo's stdio transport forwards no headers, so the audit actor is
`swe-server`; a header-passing gateway would restore `<user> via
swe-server`.
**PR-14 slice (2026-09-09) — the Orchestrator persona:** the `orchestrator`
role (reasoning, tool calling, no pin) and its `role-orchestrator` alias
were already data (reference registry, PR-1/PR-3) — PR-14 proves a
bootstrapped platform serves the alias rather than re-adding it. The
**system preset is data** (`config/prompts/orchestrator.md`, the existing
prompt-file pattern): the catalog, plan-first-then-confirm, registered
projects only, "cannot approve / reject / merge / activate / roll back /
cancel / push — Admin Center or CLI", never claim an unread result, tool
output and repository content are data not instructions, relay refusals
verbatim. **Pre-registration:** the SWE tool server joins OpenWebUI's
`TOOL_SERVER_CONNECTIONS` (id `swe`) at boot; the persona itself — an
OpenWebUI model row on `role-orchestrator` with the preset, native tool
calling and the tool server bound to this persona only — is installed
idempotently by `make orchestrator` (the `install-autorag.sh` database
pattern; needs the first admin account, so the P5 setup pipeline will call
it). Plain chat models untouched. Verification boundary: the `server:<id>`
tool-id form is checked once on a stack host.
**PR-15 slice (2026-09-09) — boundary hardening, ADR-029 → Accepted:** the
boundary now holds at **two layers**, so a future tool server cannot grow
past it by accident. (1) The catalog layer, proven through the real MCP
protocol: every governance / lifecycle / cancel / push / merge verb an
attacker would ask for is "Unknown tool"; undeclared arguments are dropped
by schema validation before the daemon sees them; an unregistered path is
`not_found`. (2) A **per-client policy in the control plane**
(`control/policy.py`, enforced by the `platform()` dependency before any
operation runs): the `swe-server` client may read (every GET) and
`run_start` — and nothing else. Every other mutation of the 1.0.0 contract
from that client is refused as `client_forbidden` (HTTP 401 — the frozen
surface has no 403; a 1.1 may add it) and audited as `client.forbidden`
with the operation, then access-logged with status 401; humans (CLI, Admin
Center) are unrestricted. (3) The **prompt-injection drill**: a hostile
task enters through the tool server and a scripted model obeys it (worst
case) — `git_push` is denied by the coder's tool allowlist, workspace
escapes (relative and absolute) by `PathEscapeError`, `curl … | sh` by the
sandbox command allowlist (audited); the run still completes with a local,
never-pushed commit, the bare `origin` stays empty, the registry
generation and the governance queue are untouched, and the injected text
is recorded as data only. (4) The **chat/RAG byte-identical regression**:
`scripts/chat-stack-baseline.py` snapshots the chat path (compose services
openwebui/litellm/embed-server/qdrant/searxng/mcpo/monitor minus the SWE
additions, the four original mcpo servers, the LiteLLM RAG hook and the
SearXNG settings by hash) into a committed fixture; a test compares the
tree against it, so a future PR that touches the chat stack must update
the baseline on purpose. Deferred, unchanged: the 1.1 tool additions
(`swe_test`, `swe_review`, `model_benchmark`), a header-passing gateway for
`<user> via swe-server`. Run locally with `make swe-drill`; the hosted CI
runs the same package (workflow remains manual-only).

## ADR-030 — Admin Center: the monitor evolves into the platform console (P4)
**Date:** 2026-09-09 · **Status:** Accepted (entered Proposed with PR-16;
Accepted with the P4 close, PR-20)
**Context:** WEBUI_ADMIN_CENTER names the monitor (:8888 — RBAC admin/viewer,
health view, knowledge-base bar) as the management console: an evolution of a
component we own, not a new frontend project and not an OpenWebUI fork. The
control plane (ADR-028, contract 1.0.0) already serves everything the console
must show, and the capped chat surface (ADR-029) sends humans here for
decisions.
**Decision (PR-16 slice — the client and the first pages):** (1) the monitor
stays **one service, one image**; the Admin Center is a sibling module
(`monitor/admin_center.py`) installed on the existing FastAPI app with the
existing RBAC dependencies — viewer reads, admin mutates; `/` (health +
knowledge) is unchanged apart from the shared header and a navigation bar.
(2) **The browser never talks to the daemon**: page JavaScript calls the
monitor (`/api/ezai/…`), the monitor calls `ezaid` with the service token
(`EZAI_CONTROL_TOKEN`, server side only) and forwards the monitor login as
the human (`X-EZAI-User: admin|viewer`, `X-EZAI-Client: admin-center` → audit
actor `<login> via admin-center`), a fresh `Idempotency-Key` per mutation.
(3) **Pages are path routes** serving one template — `/overview`, `/runs`,
`/runs/{id}` — so the deep links the CLI and the SWE tools already print
resolve; the view is chosen client-side from the path, framework-free (UX
principle 5: the monitor's look evolves, no rewrite, no build step). (4)
**Data is aggregated server-side per page** (one request renders a page):
Overview = aggregated health + platform snapshot + role explanations (roles
are the interface — orchestrator/planner/coder/debugger/reviewer/chat, a role
the registry lacks is skipped) + the pending queue + recent runs; run detail
= record + report (once written) + journal tail. (5) **A daemon that is down
is a page state**, not a broken console: `connected: false` with the same
code/message/fix the CLI prints; health and the knowledge base keep working.
(6) **Parity or absence:** the only mutation is *cancel* (admin) — the
matrix's "view, cancel"; starting work from the console is deliberately
absent, governance decisions arrive with their evidence panels (PR-18).
Because the monitor login is ambient (HTTP Basic), mutations additionally
require the `X-Requested-With: admin-center` header the page sends — the
same-origin guard a cross-site form cannot satisfy. (7)
**Wiring mirrors the mcpo precedent:** compose gives the monitor
`EZAI_CONTROL_URL` (`EZAI_CONTROL_URL_MONITOR`, default overlay
`http://ezaid:8010`, `host.docker.internal` for a host daemon), the token and
`extra_hosts`; the chat-stack baseline (PR-15) treats these keys as additive,
so its fixture is unchanged.
**Consequences / deferred:** Models/Routing/Runtime pages (PR-17), the
Governance queue with approval modal and evidence (PR-18),
Sprints/Evolution/Memory/Projects (PR-19), the SSO trusted-header handoff and
the five zero-CLI journeys as Browser-QA workflows (PR-20 → Accepted).
Identity is the monitor login (two roles) until SSO; a forwarded OpenWebUI
identity later replaces the header value without touching the daemon.
Validation already follows the plan's rule "the product is tested by its own
testing capability": a real-Chromium smoke renders the pages with console
errors failing the test; the declarative journey suite is PR-20.
**PR-17 slice (2026-09-09) — Models / Routing / Runtime pages:** (1)
**Role-first, models as evidence** (WEBUI_PRODUCT_STRATEGY §1): the Models
page draws group panels whose serving order is the *resolution* order (an
unpinned role's primary + fallbacks) followed by the group's non-serving
members, then role cards, the catalog, the generations and the queue; a
group no model belongs to and no role resolves through is invisible to the
contract — and to the page. (2) **Fit badges come only from the platform's
recommender** (`GET /v1/catalog/recommendations`): a model that matches a
catalog candidate on its runtime shows that verdict; a user-supplied
source shows "no verdict" and its measured tokens/s — the browser never
computes fit (HARDWARE_AGNOSTIC §3 has one `fit()`). (3) **One additive
data enrichment in the control plane, no surface change:** the snapshot's
per-model entries gain `groups`, `context`, `format`, `license`; the
`models` object is untyped in the 1.0.0 contract, the artifact and the
frozen inventory are byte-identical. (4) **Mutations are the CLI's, through
the daemon, admin only, same-origin guarded:** install (catalog id or any
`hf:`/`gguf:` source), benchmark, activate and upgrade (→ the approval queue
whenever a serving role changes; the page says so and names the CLI verb
that decides until PR-18), retire, uninstall (with `force` for rollback
targets), rollback to a generation (immediate, audited, reason recorded).
Every refusal is the daemon's own error object — a source with no
declared tool-call format cannot become the coder's primary, and the page
shows the negotiation text. (5) **Routing = the explain view** of
MODEL_ROUTING_DESIGN §7: every defined role's source (pin or group),
chain, reason lines, contract, per-model checks with failure text and the
generation it was resolved from; roles the registry does not define are
listed as such; the generation history with diffs. (6) **Runtime page,
honest about switching:** the engine slot (runtime, class, accelerator,
memory, engine/router health, active models) and, for every other runtime
the descriptors serve, a **pre-check** — which active models lack a
variant for it (with the RUNTIME_ABSTRACTION §5 fix wording) and which
catalog candidates fit per group, contract failures included (on a small
host the vllm descriptor's context budget rules out every candidate — the
page carries the recommender's sentence). A switch is described as what it
is in P1: activating a model served by the other runtime, a change request
flagged `runtime.switch` that needs approval; **no switch button**, since
the contract has no runtime verb (a 1.1 candidate, not this slice).
**PR-18 slice (2026-09-09) — the Governance queue and the approval view:**
(1) **Evidence next to every button** (WEBUI_ADMIN_CENTER §5.1): the approval
view at `/governance/<id>` — the deep link the SWE tools and the CLI print —
lays out *what changes* (the diff lines, each affected role before → after),
*evidence* (benchmarks measured on this host, fit verdicts of newly active
models, the render-time capability report per role × model × runtime, the
runtime before/after with the switch flag, the capability class), *proposed
by* (human or evolution, with the "advisory, never operator" note), and
*reversibility* (the generation one rollback restores), then the decision:
Reject with a required reason, Approve & apply. (2) **The queue is one
list** with the history behind a status filter; every row carries who
decided, when, why and the resulting generation — the audit record, read
back from the request itself. (3) **The daemon rules, not the page:** both
decisions go through `POST /v1/governance/{id}/approve|reject` as the
monitor login (`admin via admin-center`), admin role only, same-origin
guarded; "a rejection needs a reason" and "decisions are made once" are the
daemon's own refusals (`governance_rule`, 409) shown verbatim; a viewer
reads everything and decides nothing. (4) **The proposed registry dump
stays server-side** — the human evidence is the diff and the evidence block,
not a YAML tree. (5) **Honest about item types:** only the activation
module submits change requests today (activation, upgrade; a runtime switch
is a flag on them), so evolution proposals and release candidates are
described as joining when their pipelines submit requests, with evolution
runs pointed to on the Runs page meanwhile — no synthetic rows. Behavior
note: two unmerged tests (PR-16, PR-17) that asserted the monitor had no
approve route flip to the guard the route now enforces.
**PR-19 slice (2026-09-09) — Sprints / Evolution / Memory / Projects:** (1)
**The contract grows additively, by its own rule:** the Memory page needs
data 1.0.0 never served, so the control plane gains `GET|POST
/v1/projects/{name}/memory` (`project_memory`, `project_memory_add` — browse
by kind or search; add a curated rule / style / decision, exported to
`lessons_learned.json`, audited `memory.added`) and the contract becomes
**1.1.0**: every 1.0.0 operation, path and method unchanged, the artifact
regenerated, the inventory test pins both the 29 frozen operations and the
two additions, the chat-boundary list gains the new mutation (the PR-15
client policy already refuses it to chat — chat may read memory, never
write it). Fixes and implementation history stay run-recorded: the API
accepts the three curated kinds only. The CLI's `memory` verb stays
direct-mode repository work (no connected twin). (2) **The console starts
sprints and evolution cycles** — WEBUI_PRODUCT_STRATEGY §3.5/§3.6 and the
release-gated zero-CLI journeys 3 and 4 require it, and the CLI verbs
exist (`local-ezai sprint|evolve`); this **amends the parity matrix's
"view, cancel"** for those two kinds only (recorded in CLI_AND_WEBUI §3 as
built). Plain runs, fixes and plans keep starting from the CLI or chat; the
monitor refuses other kinds before the daemon is asked. Same guards as
every mutation: admin role, same-origin header, daemon refusals verbatim.
(3) **Sprints and Evolution are views over the run registry** (`GET
/v1/runs?kind=…` + reports): waves and task results, and the dependency
graph as mermaid *source* generated from the plan — the runtime's rendered
report document lives in the repository, which the contract does not serve,
and the offline product loads no rendering library. Evolution cycles show
proposal, improvements, benchmark before → after, the PR or bundle and the
standing "awaiting human review"; the page says proposals reach the
Governance queue only once the pipeline submits change requests. (4)
**Projects** is the allowlist with each project's work (run count, active,
last run) and links to its runs and memory; registering needs the path on
the control plane's filesystem, and the page says so.
**PR-20 slice (2026-09-09) — identity handoff, the journeys as Browser-QA
workflows; P4 close → Accepted:** (1) **Identity comes from the chat surface
when it can; the login stays the fallback.** The monitor's viewer / admin
dependencies resolve, in order: an explicit Basic login; a trusted identity
header set by a reverse proxy in front of the monitor
(`MONITOR_SSO_TRUSTED_HEADER`, honoured only together with the shared secret
in `X-EZAI-Proxy-Secret` — a header alone is never trusted; the addresses in
`MONITOR_SSO_ADMINS` are admins, everyone else a viewer); the OpenWebUI
session cookie (`token`, validated server-side against
`MONITOR_SSO_OPENWEBUI_URL/api/v1/auths/` — OpenWebUI's admin is the
console's admin, a user a viewer, a pending account is not signed in —
cached 60 s); otherwise the Basic challenge. All of it is opt-in by
environment: without the keys the PR-16 monitor is unchanged, and so are
`MONITOR_AUTH=false` and the dashboard. The identity travels to the daemon
as `X-EZAI-User: <email>`, so the audit trail names the person
(`nita@example.com via admin-center`), not the role — the PR-16 slice's
deferred line, closed without touching the daemon. (2) **No native
dialogs:** every confirmation and reason (activate / upgrade / retire /
uninstall / rollback, approve / reject, cancel, remove) is an inline form on
the page. The Browser QA harness has no dialog step by design (ADR-016), so
a native `prompt()` would have made the journeys untestable by the
product's own capability; the forms also give the reason a field a
screenshot can show. (3) **The Overview banner** names the last registry
change (generation, note, when, diff lines, the generation a rollback
restores) with *Roll back* → reason → confirm — journey 5 in three clicks
over the PR-17 rollback route. (4) **The walkthroughs are the tests:** the
five zero-CLI journeys of WEBUI_PRODUCT_STRATEGY §5 are one declarative
Browser-QA workflow file (`agentd/examples/browser-qa.admin-center.yaml`),
run by the platform's own harness (`BrowserQAHarness`, real headless
Chromium, console errors fail the step) against a launcher
(`agentd/tests/fixtures/admin_center_app.py`) that serves the monitor as
shipped over an in-process daemon on a bootstrapped scratch platform with
the lifecycle seams the offline suites fake; the run is an ordinary CI
test. (5) **P4 exit criteria, honestly:** criterion 1 (one queue, decided on
either surface) is proven both ways — the CLI proposes and the console
approves, the console proposes and the CLI approves; criterion 3 runs the
five journeys with three stated boundaries: journey 1 ends at the Routing
page (the chat-side subtitle check needs OpenWebUI), journey 2 covers the
pre-check and the format-gap explanation (approving a real runtime switch
needs a host where a variant of the other runtime fits), journey 4 covers
triggering a cycle and reading its proposal (rejecting it in Governance and
the memory write-back wait for the evolution pipeline to submit change
requests — the PR-18 note stands). Criterion 2 (the chat side) is
ADR-029's, closed with PR-14/15.
**As built, PR-16..20 — the decision as it stands:** the monitor is the
Admin Center: one service, one image; a server-side control-plane client
(token never in the browser); pages as path routes over one template;
server-side aggregation per page; a daemon that is down as a page state;
parity or absence (console starts for sprint and evolve only, no runtime
verb, no role editing); admin mutations through the daemon behind the
same-origin header with the daemon's refusals verbatim; evidence next to
every decision; the audit trail naming the person. Contract **1.1.0** (the
additive memory operations) is the only control-plane change the console
needed. Deferred beyond P4: evolution and release-candidate items in the
queue (pipeline work), a runtime verb (a 1.x contract slice), a reverse
proxy shipped with the stack for the trusted-header path, WebUI-side deep
links into the console.

## ADR-031 — Installer & onboarding: the five-step first run (P5)
**Date:** 2026-09-09 · **Status:** Accepted (entered Proposed with PR-21;
Accepted with the P5 close, PR-23)
**Context:** TARGET_PRODUCT_V1 §2 promises one edit of `.env`, once, and
never a config file again; FINAL_FIRST_RUN_EXPERIENCE §3 decomposes `make
setup` into detect → validate → secrets → fetch → render → up → verify →
report, and the bootstrap core (PR-7) already covers validate, the model
part of fetch, and render. Still a README paragraph until now: hardware
detection into `.env`, secrets, the review-edit stop, the repair of an
existing install (F5) — and, later in the phase, the smoke, the "Platform
ready" card, `local-ezai init` and the offline bundle.
**Decision (PR-21 slice — `install.sh`, steps 1–3):** (1) **One thin bash
entry, one Python module.** `install.sh` does preflight (python3 ≥ 3.10;
Docker present or the fix printed — it installs nothing but the agentd venv
it needs, through `make swe-install`) and runs `python -m agentd.installer`;
every decision lives in the module, offline-testable, reusing the platform's
own pieces — `capability.detect_vector` / `classify` (step 1),
`bootstrap.read_seeds` / `validate_seeds` (F8), the descriptors, the
catalog. No second detector, no second validator. (2) **Detection is
recorded, assertions are honored.** The detected vector and class are
written to `.env` as a comment block (stable text: an unchanged host
re-renders byte-identical); a class asserted with `--profile
cpu|n97|n97-igpu` or `--class` is written as `EZAI_CAPABILITY_CLASS`, which
re-runs of the installer and `platform_cli.build_context` (make exports
`.env`) both honor, so the installer and `make bootstrap` never disagree on
the class; `--profile gpu` checks that an accelerator exists and asserts
nothing (HARDWARE_AGNOSTIC §1: setup targets assert a class). (3) **The
runtime default is descriptor data.** A new optional field
`default_for_classes` on a runtime descriptor names the classes for which
the installer proposes it as `AI_RUNTIME` (shipped: the GGUF runtime for the
CPU classes, the HF runtime for the accelerator classes); candidates are the
descriptors with an image for the detected accelerator kind, a host no
default serves gets the first candidate with the reason printed, `--runtime`
overrides and is checked against the host. The module names no runtime,
model or vendor. (4) **The installer never picks models** (ADR-026: users
select models, the platform adapts). A fresh `.env` is the shipped example
with its seven placeholder secrets minted (the LiteLLM key keeps `sk-`),
`AI_RUNTIME` uncommented in the seed section and the hardware recorded; the
model seeds are the human's one edit. Validation runs the bootstrap's F8
rules against the chosen class and runtime and adds a class-aware hint when
the file still carries the example's legacy accelerator-sized default on a
CPU class; nothing is downloaded. (5) **Repair mode (F5) edits `.env` and
nothing else:** a timestamped backup first; user values kept byte for byte,
in place; only secrets that are missing, empty or equal to the example's
placeholder are minted — the example's model defaults and settings are
never copied into a user's file; `AI_RUNTIME` is set only when unset and the
seeds not yet consumed; a consumed stamp means the seeds are history and are
reported, not validated; a second run changes nothing and writes no backup;
`config/`, `models/` and volumes are never touched. (6) **The one review-edit
stop** (F2/F7): a freshly created `.env` opens once in `$VISUAL` / `$EDITOR`
(else nano, vi) on a terminal and is validated after the editor closes;
without a terminal the installer prints what to edit and exits 3; `--yes`
accepts the generated file; `--check` writes nothing. Exit codes: 0 ready ·
1 problems printed · 2 usage / preflight · 3 review stop. (7) **The legacy
make entry points stay.** `make install` runs the script; `make setup` keeps
its meaning (system packages) until PR-22 folds steps 4–8 into it; the
installer's "next" lines name `make bootstrap` and the profile's `make up-*`
/ `make setup-*` from the preset table (data).
**Consequences / deferred:** PR-22 — `make setup` = `install.sh` + fetch →
render → up → verify → report, the "Platform ready" card, `local-ezai init`
as the fallback when seeds are missing, the class assertion passed by `make
setup-*`; PR-23 — offline bundle, the scripted F1–F11 acceptance suite,
onboarding under Browser QA → Accepted. Host Python remains a V1
requirement (PR-7's departure from "no host Python").
**PR-22 slice (2026-09-09) — the setup pipeline, the smoke, the card,
`init`:** (1) **`make setup` is the five-step contract now.** It runs
`install.sh` (steps 1–3; a fresh `.env` stops once for the seeds — the one
edit) and then `local-ezai setup`, the host-only verb behind steps 4–8: the
PR-7 bootstrap when no registry exists (models fetched, validated,
benchmarked, generation 1 rendered), images (`docker compose pull` for the
image services, `build` for the platform's own, with the rendered engine
override), the RAG embedding model (one shared script), `up -d` for the
profile, wait-ready (the active runtime descriptor's `ready` verb on the
host port, then the daemon's health table addressed on host ports), the
smoke, the report. Every step is idempotent, so a re-run repairs rather
than repeats. The system-package script lives on as `make setup-system`;
`setup-gpu|cpu|n97` pass their profile assertion through both halves. (2)
**What "ready" means.** Engine and router healthy and one chat turn answered
on the chat role alias — what a user's first message depends on. The RAG
answer over a sample document (embedded with the stack's own embed script),
`plan_only` on the bundled sample project and the evaluate-models probes are
reported with their detail but advisory: they depend on the model the user
chose and must not brick a working stack. Any other service down is a
warning naming the compose logs to read. (3) **The report is a file, the
card is a banner.** `config/first-run/report.{json,md}` and the ✔ block with
the WebUI and Admin Center URLs (on `LAN_HOST` when set, on the relocated
ports). The "Platform ready" card reaches the WebUI as OpenWebUI's own
`WEBUI_BANNERS` setting through an optional compose `env_file`
(`config/first-run/openwebui.env` — never `.env`: make exports `.env`
verbatim and would hand compose a quoted JSON); OpenWebUI is recreated and
probed, and the file is removed with a second recreate if it does not come
back, so a banner can never break chat. The Orchestrator persona install is
attempted and deferred to "after your first login: make orchestrator" when
no account exists yet. (4) **The sample project is bundled**
(`examples/sample-project`, a dependency-free HTTP service without
`/health`), copied to `<checkout>/sample-project`, made a repository and
registered, so the first autonomous task never touches a user's repository.
(5) **`local-ezai init` is the wizard of FIRST_RUN_EXPERIENCE §3 in its
fallback role:** hardware check, the recommended set per group from the
catalog recommender with fit verdicts, each accepted or overridden (a
catalog id, `hf:` or `gguf:`), written to `.env` as catalog ids — explicit,
auditable in the generation-1 diff (F10) — then the pipeline; seeds already
present are respected, invalid ones reported, a bootstrapped platform goes
straight to setup. (6) **Engine models come from the registry**, so `make
up*` no longer forces the legacy chat-model download and fetches only the
embedding model when missing (the same script); `download-gpu` stays as the
legacy path. (7) The CLI's platform context reads an asserted class from
the platform's `.env` when the environment does not carry it (a shell
instead of make) — the PR-21 decision "installer and CLI agree" completed.
**Deferred to PR-23 (→ Accepted):** the offline bundle (`install.sh
--offline`), the scripted F1–F11 acceptance suite, onboarding under Browser
QA. A first-run card in the Admin Center waits for a contract operation that
carries the report (parity or absence).
**PR-23 slice (2026-09-09) — the offline bundle, the acceptance suite, the
card in the console; P5 close → Accepted:** (1) **The air-gapped first run
is the same wizard with the fetches replaced by a directory.** `local-ezai
bundle create <dir>` on a connected, bootstrapped host saves what a first
run fetches — every compose image (`docker save`), the registry's GGUF
artifacts and the hub cache the engine and the embedding server mount —
with a manifest of the seeds (GGUF seeds carry a `{weights}` placeholder),
the images and the checksums. `install.sh --offline <dir>` consumes it after
`.env` is written: images loaded, weights placed where the runtime
descriptors mount them (checksums verified, present files skipped), the
seeds written to `.env` pointing at the local files, `EZAI_OFFLINE=1`
stamped, no review stop (the bundle's seeds are the operator's decision).
In offline mode the pipeline verifies the images are present instead of
pulling or building, and the bootstrap's fetchers refuse the network with
the fix named — present, verified weights are reused, anything else fails
loudly (F6: same steps, no egress). The bundle is a directory; transport is
`tar`. (2) **F1–F11 are scripted** (`agentd/tests/acceptance/`, `make
swe-accept`): one offline test per criterion over the PR-22 harness — F1 a
fresh accelerator host to smoke green, F2 exactly one hand-edited file
once, F3 the low-power class through the same wizard with a smaller set,
F4 an abort at every step resumable and never half-configured (no
generation, or exactly one, rendered and stamped), F5 the re-run repairs and
wipes nothing, F6 the bundle with no egress, F7 zero prompts on a fully
specified `.env`, F8 fixes before any download, F9 `auto` per group with
the verdict shown, F10 the generation-1 diff equal to the seeds, F11 a
legacy `.env` migrated without user action. F1's thirty minutes is a host
measurement (the P6 soak runbook); the suite times the platform's own work
with the downloads excluded. (3) **The suite found a defect and this slice
fixes it:** the PR-7 bootstrap deduplicated seeds by their reference text,
so three `auto` seeds became one model for every group; `auto` is now the
recommender's answer per group, and a recommendation two groups share is
installed once. (4) **Onboarding under Browser QA, in the console.** The
contract grows additively to **1.2.0** with `GET /v1/first-run`
(`first_run_report`, read-only) serving the report `local-ezai setup`
wrote; the Admin Center Overview shows the "Platform ready" card (groups →
models, the smoke tally, Start chatting · Try the Orchestrator · Models &
routing) while a report exists — an older daemon shows no card — and the
journey suite's sixth workflow drives it in a real Chromium: the item PR-22
deferred.
**As built, PR-21..23 — the decision as it stands:** the five-step contract
of FINAL_FIRST_RUN_EXPERIENCE holds end to end. `install.sh` detects or
asserts the class, generates or repairs `.env` with minted secrets, stops
once for the human's edit, and validates the seeds with every fix before
any download; `local-ezai setup` bootstraps, fetches images, starts,
waits, smoke-tests and reports, shows the card and installs the persona —
both behind `make setup`, with `init` as the fallback when seeds are
missing and the offline bundle for air-gapped hosts; every step is
idempotent; the eleven acceptance criteria are scripted. Host Python and
Docker remain the two prerequisites; the system-package script is `make
setup-system`. Deferred beyond P5: a one-file bundle format, bundling the
Python environment, the OpenWebUI side under Browser QA (not in CI), F1's
wall clock (P6 soak).
