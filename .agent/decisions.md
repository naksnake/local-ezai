# Architecture Decision Records

> One entry per decision. Statuses: Proposed / **Accepted** / Superseded(by).
> New states in the workflow machine, new agents, or deviations from
> [docs/TARGET_ARCHITECTURE.md](../docs/TARGET_ARCHITECTURE.md) require a new
> ADR here, in the same PR.

Index: [001](#adr-001) [002](#adr-002) [003](#adr-003) [004](#adr-004)
[005](#adr-005) [006](#adr-006) [007](#adr-007) [008](#adr-008)
[009](#adr-009) [010](#adr-010) [011](#adr-011) [012](#adr-012)

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
