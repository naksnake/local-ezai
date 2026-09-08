# Target Architecture — Local Autonomous Software Engineer Platform

**Status:** Approved target (to-be)
**Date:** 2026-08-14
**Author:** Principal Architect
**Depends on:** [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md)
**Decisions:** recorded as ADRs in [.agent/decisions.md](../.agent/decisions.md)

---

## 1. Vision

Transform local-ezai from a self-hosted chat+RAG stack into a **Local
Autonomous Software Engineer Platform**: a system that accepts a software
task in natural language ("fix this bug", "add this feature", "upgrade this
dependency", "review this PR"), then autonomously plans, edits code in an
isolated workspace, runs builds and tests, iterates until checks pass,
and delivers a reviewed branch/patch/PR — **entirely on the user's own
hardware**, with the same no-cloud, no-data-leaves-the-machine guarantee the
stack has today.

The behavioral reference point is Claude Code: an agentic loop over a small
set of sharp tools (read/edit/search/execute), driven by a capable model,
wrapped in permissions, project memory, and verification.

### Non-goals

- Not a cloud service, not multi-tenant SaaS. One machine, one team, LAN trust
  perimeter (hardened where autonomy demands it).
- Not a general workflow engine. The state machine is purpose-built for
  software-engineering runs (see WORKFLOW_DESIGN.md).
- Not a model-training platform. Inference only.
- Not a replacement for human review by default. Autonomy levels are explicit
  and configurable (§9).

### Honest constraint: model capability floors

Autonomous SWE quality is gated by the weakest model in the loop. The
architecture is model-agnostic, but sets expectations per hardware profile:

| Profile | Realistic capability |
|---|---|
| N97 / 1.5B–3B GGUF | single-file edits, doc/config changes, test triage, commit messages; **plan/verify loops must be short**; not suitable for multi-file refactors |
| Single GPU / 7B–14B (coder-tuned) | small features, bug fixes with tests, dependency bumps |
| Multi-GPU / 32B–72B+ | multi-file features, refactors, meaningful review |

This is why **model tiering by agent role** (§8) is a first-class concern, and
why the workflow engine enforces budgets and verification instead of trusting
model judgment.

---

## 2. Architecture principles

1. **Additive evolution.** The existing 8-service chat stack keeps running
   unmodified. New capability arrives as new services in an additive compose
   overlay (`docker-compose.swe.yml`, future work). (ADR-002)
2. **OpenAI-compatible seams.** All model access goes through LiteLLM. Agents
   never talk to an engine directly. (ADR-001)
3. **MCP is the tool protocol.** Every tool an agent can use is an MCP tool;
   the tool gateway adds policy, scoping, and audit — not a new protocol.
   (ADR-003)
4. **Fail-open retrieval, fail-closed action.** Context enrichment may
   silently degrade; anything that mutates state requires an explicit,
   auditable permission decision. (ADR-008)
5. **Isolation before autonomy.** No shell/write tool exists outside a
   sandboxed workspace. The sandbox is a prerequisite phase, not an
   optimization. (ADR-004)
6. **Event-sourced truth.** Every run is an append-only event journal; all
   state (UI, resume, audit, memory distillation) derives from it. (ADR-006)
7. **Deterministic orchestration, stochastic workers.** Control flow (states,
   retries, budgets, gates) is code; LLMs fill in the content of steps, never
   the integrity of the loop.
8. **Small sharp tools over frameworks.** Prefer read/edit/grep/exec-grade
   primitives that compose, mirroring what demonstrably works in Claude Code.
9. **Everything local, everything inspectable.** SQLite + JSONL + Qdrant on
   the user's disk; no telemetry leaves the machine.

---

## 3. Component architecture (C4 level 2)

```
┌─────────────────────────────────────────────────────────────────────────┐
│ INTERFACES                                                              │
│  OpenWebUI (chat-ops)   ezai CLI   Web Console (monitor v2)   REST/WS   │
│                          git host webhooks (optional, LAN)              │
└───────────────┬─────────────────────────────────────────────────────────┘
                │  task submissions, approvals, run streams
┌───────────────▼─────────────────────────────────────────────────────────┐
│ CONTROL PLANE (new)                                                     │
│  ┌───────────────────────────┐   ┌───────────────────────────────────┐  │
│  │ agentd — Agent Runtime    │   │ Workflow Engine (in agentd)       │  │
│  │  agentic loop / sessions  │   │  run state machine, gates,        │  │
│  │  context manager          │   │  budgets, retries, resume         │  │
│  │  sub-agent scheduler      │   │  event journal (SQLite+JSONL)     │  │
│  └─────┬───────────┬─────────┘   └───────────────────────────────────┘  │
│        │           │                                                    │
│  ┌─────▼─────┐ ┌───▼───────────────┐  ┌──────────────────────────────┐  │
│  │ Permission│ │ toolgw — Tool     │  │ memoryd — Memory Service     │  │
│  │ Engine    │ │ Gateway (mcpo v2) │  │  layered memory API          │  │
│  │ (policy)  │ │  registry, scope, │  │  (working/episodic/semantic/ │  │
│  └───────────┘ │  audit, MCP hub   │  │   procedural/project)        │  │
│                └───┬───────────────┘  └───────┬──────────────────────┘  │
└────────────────────┼──────────────────────────┼─────────────────────────┘
                     │ tool calls (MCP)         │
┌────────────────────▼──────────────┐  ┌────────▼─────────────────────────┐
│ EXECUTION PLANE (new)             │  │ KNOWLEDGE PLANE (new + existing) │
│  sandboxd — Sandbox Manager       │  │  codeidx — Code Intelligence     │
│   per-run workspace containers    │  │   repo indexer (symbols+chunks)  │
│   git worktrees, exec API (PTY),  │  │   hybrid search (BM25+vector)    │
│   resource caps, net policy,      │  │  Qdrant (existing)               │
│   snapshot/rollback               │  │  embed-server (existing)         │
└───────────────────────────────────┘  └──────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────────────┐
│ MODEL PLANE (existing, extended)                                        │
│  LiteLLM ──► inference slot(s): vLLM / llama.cpp  (+ optional 2nd slot) │
│          ──► embed-server                                               │
│  role-based model aliases: swe-planner / swe-coder / swe-reviewer / …   │
└─────────────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────────────┐
│ EXISTING CHAT STACK — unchanged                                         │
│  OpenWebUI · LiteLLM auto-RAG hook · Qdrant · SearXNG · mcpo · monitor  │
└─────────────────────────────────────────────────────────────────────────┘
```

### New services (target)

| Service | Responsibility | Technology (recommended) |
|---|---|---|
| **agentd** | Agent runtime: sessions, agentic loop, context management/compaction, sub-agent scheduling, workflow engine, permission engine, REST+WS API | Python 3.12 / FastAPI (matches embed-server & monitor skills in repo) |
| **toolgw** | Tool gateway: MCP client hub, tool registry with risk tiers, per-run scoping, argument validation, audit log. Evolution of mcpo — mcpo stays for chat; toolgw serves agents | Python (mcp SDK), reusing pinning discipline |
| **sandboxd** | Workspace lifecycle: clone/worktree, per-run containers, exec API with timeouts/PTY, FS scoping, network egress policy, snapshots | Python + Docker API (`/var/run/docker.sock` via least-privilege socket proxy) |
| **codeidx** | Repo indexing: tree-sitter symbol extraction, chunking, embeddings into per-repo Qdrant collections, keyword index, incremental re-index on change | Python + tree-sitter; reuses embed-server + Qdrant |
| **memoryd** | Layered memory API (§6); may start as a library inside agentd and split out later | Python |
| **ezai CLI** | Local terminal client: submit tasks, stream runs, approve gates, inspect journals | Python (single static entry point) |

Existing services are **not modified**; the monitor gains run-visibility in a
later phase by reading agentd's public API (additive endpoint consumption).

---

## 4. Agent runtime (agentd)

The runtime hosts one **Orchestrator** per run and specialist agents as
sub-sessions (full roster and specs in [AGENT_DESIGN.md](AGENT_DESIGN.md)).

Core loop, per agent turn:

```
context assembly → model call (LiteLLM) → tool-call parse →
permission check → tool execution (toolgw/sandboxd) → result envelope →
journal append → loop | yield
```

Key mechanics:

- **Sessions** are resumable: the journal replays into a reconstructed context.
- **Context manager** owns the token budget per model tier: system prompt +
  project memory + task state + working set (files, diffs, test output),
  with **compaction** (summarize-and-evict) when approaching the window —
  critical on 8k-context N97 profiles.
- **Sub-agents** are child sessions with narrowed toolsets and their own
  budgets; results return as structured envelopes, not raw transcripts.
- **Structured outputs** (plans, reviews, verdicts) are JSON-schema-validated
  with bounded retries on parse failure.
- **Hooks** (later phase): pre/post tool-call shell hooks per project, the
  Claude Code pattern, declared in project config.

---

## 5. Tool architecture

### 5.1 Registry and risk tiers

Every tool is registered with: name, JSON schema, **risk tier**, scope
requirements, and audit fields. Tiers drive the permission engine:

| Tier | Examples | Default policy |
|---|---|---|
| T0 read-only, workspace | `fs.read`, `fs.glob`, `code.search`, `git.diff`, `git.log` | always allowed |
| T1 read-only, external | `web.fetch`, `web.search`, `kb.search` | allowed; rate-limited; egress-policied |
| T2 mutating, workspace | `fs.write`, `fs.edit`, `exec.run` (in sandbox), `git.commit` (local) | allowed inside sandbox; journaled |
| T3 mutating, project-visible | `git.push`, PR create/comment, KB writes, memory writes | gated by autonomy level (§9); explicit grant |
| T4 destructive / host-visible | anything outside the sandbox mount, force-push, deletes of shared data | denied; requires per-call human approval |

### 5.2 Core toolset (Claude-Code parity set)

Workspace: `fs.read`, `fs.write`, `fs.edit` (exact-match replace), `fs.glob`,
`fs.ls`. Search: `code.grep` (ripgrep), `code.symbols`, `code.semantic`
(codeidx). Execution: `exec.run` (sandboxed shell; timeout, output caps,
cwd pinning), `exec.bg` + `exec.poll` for servers/watchers. Git: `git.status/
diff/log/branch/commit/apply_patch/worktree`, `git.push` (T3). Web:
`web.search` (SearXNG), `web.fetch`. Knowledge: `kb.search`, `kb.ingest`.
Memory: `memory.read/write/query`. Delegation: `agent.spawn` (sub-agent).

### 5.3 Gateway behavior (toolgw)

- Speaks MCP downstream to tool servers (filesystem/memory/fetch/qdrant-rag
  are reused as-is; new servers added the same way).
- **Per-run scoping**: a run's credentials resolve `fs.*`/`exec.*` to that
  run's workspace only; path traversal is rejected at the gateway, not just
  in the tool.
- **Result envelopes**: `{ok, output, stderr, exit_code, duration_ms,
  truncated, artifacts[]}` — uniform across tools so agents and the journal
  treat results identically.
- **Audit**: every call (args, decision, result hash) appends to the run
  journal; T3/T4 decisions record who/what granted them.

---

## 6. Memory architecture

Five layers, each with a distinct store, writer, and eviction policy.
(ADR-005: Qdrant remains the single vector store, one collection per concern;
never mix embedding models within a collection — existing rule kept.)

| Layer | Contents | Store | Written by | Read by | Lifetime |
|---|---|---|---|---|---|
| **Working** | current context window: task state, working set, recent tool results | in-process (agentd), snapshot in journal | context manager | the active agent | turn-to-turn; compacted under pressure |
| **Episodic** | full run history: every event, tool call, diff, verdict | SQLite (`runs.db`) + JSONL journal per run | workflow engine | resume, audit, UI, Memory Curator | forever (pruning policy configurable) |
| **Semantic — code** | symbol-aware chunks of indexed repos | Qdrant `code-{repo}` collections + keyword index | codeidx (incremental) | Context/Research agents via `code.semantic` | re-indexed on change |
| **Semantic — knowledge** | user documents (existing KB), harvested lessons | Qdrant `my-knowledge-base` (existing) + `swe-lessons` | existing pipelines; Memory Curator | auto-RAG (existing), planner | as today |
| **Procedural** | project memory files: build/test commands, conventions, gotchas | git-tracked Markdown: `CLAUDE.md` / `AGENT.md` per target repo; `.agent/*` for this repo | humans + Memory Curator (PR-style proposals only) | injected into every run's system context | versioned with the repo |
| *(graph)* | entity/relation facts (existing `memory` MCP) | JSON volume | memory tool | chat + agents | as today |

**Distillation loop:** after each run, the Memory Curator agent proposes
(a) lessons → `swe-lessons` collection, (b) procedural updates → a diff
against the project memory file. Procedural writes are T3 (gated), so memory
cannot silently self-modify. Working-memory compaction summaries are journaled
so a resumed run knows what was evicted.

---

## 7. Sandbox architecture

**Model: per-run sibling containers + git worktrees.** (ADR-004)

```
host
├── repos/<name>/            bare or full clone, managed by sandboxd (bind mount)
├── workspaces/<run-id>/     git worktree on branch swe/<run-id>   ─┐
└── docker: sandboxd ──creates──► runner container (per run)        │
             image: per-project toolchain (default: ubuntu+git+     │
             build tools; project may pin its own image)            │
             mounts: workspace rw ◄─────────────────────────────────┘
                     model caches ro (if needed)
             caps:   no privileged, non-root UID, read-only rootfs
                     except workspace + /tmp, memory/cpu/pids caps
             net:    "sandbox-net" — default-deny egress; allowlist:
                     package registries (proxy), LAN git host; NO access
                     to ai-net control services except via toolgw
```

Rules:

1. **Every `exec.run` and `fs.write` happens inside the run's container**,
   never on the host, never in the control plane's filesystem.
2. **Git worktree per run** gives cheap isolation, cheap diffing, and a
   natural deliverable (a branch). Parallel runs on one repo don't collide.
3. **Snapshots**: workspace state is recoverable via git (committed
   checkpoints at phase boundaries) — rollback = `git reset`, not container
   surgery.
4. **Egress policy** is enforced by network, not by prompt: the runner
   container simply cannot reach the internet except through an allowlisting
   proxy (package mirrors configurable per project).
5. **Secrets** never enter the runner: pushes/PRs are executed by sandboxd
   (T3-gated) using host-side credentials; the agent only requests them.
6. **Docker access**: sandboxd is the only service that talks to the Docker
   socket, through a filtered socket proxy limited to container lifecycle
   operations (no host mounts other than the workspace path prefix).
7. **Resource ceilings** per profile: e.g. N97 runner ≤2 CPU / 4 GB;
   GPU host runner ≤8 CPU / 16 GB. One concurrent run on N97-class hardware.

---

## 8. Model plane: role-based tiering

LiteLLM already abstracts engines; the target adds **role aliases** so agent
code never names a model (ADR-007):

| Alias | Role | N97 profile | GPU profile |
|---|---|---|---|
| `swe-planner` | planning, decomposition, review verdicts | Qwen2.5-1.5B/3B | 32B-class instruct |
| `swe-coder` | code generation/edits | Qwen2.5-Coder-1.5B | 7B–32B coder-class |
| `swe-reviewer` | critique, security pass | = planner | may be same as planner |
| `swe-fast` | summarization, compaction, commit messages | 1.5B | 7B |
| `swe-embed` | embeddings | nomic/bge (existing) | same |

A second inference slot (e.g. llama.cpp serving a coder GGUF alongside the
chat model) is an optional compose overlay on GPU-class hardware; on N97
everything maps to the single engine. Roles, not models, are the contract.

---

## 9. Autonomy & permission model

Autonomy level is set **per run** (default per project):

| Level | Name | Behavior |
|---|---|---|
| A0 | Dry-run | plan + proposed diff only; no writes anywhere |
| A1 | Supervised | executes in sandbox; every T3 action requires human approval; plan approval gate ON |
| A2 | Autonomous-local | full sandbox autonomy, auto-verify; delivers a local branch/patch; T3 push/PR requires approval |
| A3 | Autonomous-delivery | may push branch + open PR on the configured LAN git host; merge always human |

The permission engine evaluates `(tool tier × autonomy level × project policy
× path/branch scope)` → allow / deny / ask. "Ask" surfaces through the
active interface (CLI prompt, console button, chat message) and is journaled
with the responder's identity. Fail-closed on any evaluation error.

---

## 10. Interfaces

1. **`ezai` CLI** — primary developer interface: `ezai run "fix #123" --repo
   myapp --autonomy A2`, `ezai runs`, `ezai attach <run>`, `ezai approve
   <gate>`, `ezai diff <run>`. Streams the journal over WS.
2. **Web console** — monitor v2 page: run list, live state-machine view,
   diff viewer, gate approvals (admin role), budgets. Reuses monitor RBAC.
3. **Chat-ops via OpenWebUI** — a `swe` MCP toolset (`swe.submit_task`,
   `swe.run_status`, `swe.approve`) registered exactly like today's tools, so
   the existing chat UI can drive the platform without modification.
4. **REST/WS API** (agentd) — everything above is a client of this; enables
   future webhook triggers (git host on the LAN) as thin adapters.

---

## 11. Security posture (delta from current)

Autonomy raises the stakes; the LAN-trust model is tightened where it matters:

- agentd/toolgw/sandboxd APIs require bearer auth (reuse the `.env` secret
  discipline); no new unauthenticated ports.
- Runner containers: default-deny egress, non-root, no docker socket, no
  control-plane reachability except toolgw.
- Credential custody: git push/PR tokens live only in sandboxd's environment
  (host side), never in prompts, tool results, or the runner.
- Prompt-injection stance: web/KB content is untrusted; T3+ actions can never
  be triggered by content alone — tier policy is enforced outside the model.
- Full audit: every action taken by an agent is reconstructible from the
  journal (who approved what, which diff, which command, exit codes).

---

## 12. Deployment view

- New services ship in `docker-compose.swe.yml` (overlay, additive) with the
  same conventions: `.env` config, memory caps for N97, health endpoints,
  pinned images, Make targets (`make up-swe`, `make swe-run`, …).
- The existing 8 services and all current Make targets remain byte-identical
  in behavior; a user who never runs the overlay sees no change.
- k8s parity is deferred until the compose form stabilizes (roadmap M6+).

---

## 13. Definition of done for the transformation

The platform reaches "v1 autonomous SWE" when, on a GPU-profile machine, this
succeeds end-to-end with no human keystrokes after submission (A2):

> `ezai run "Add a /metrics endpoint to monitor.py exposing poll latencies,
> with tests" --repo local-ezai --autonomy A2`
> → plan → branch `swe/<run>` → edits → tests pass in sandbox → self-review →
> local branch + summary delivered; journal fully replayable; on A3 the same
> run opens a PR on the LAN git host.

Detailed phase gating lives in [MIGRATION_PLAN.md](MIGRATION_PLAN.md).
