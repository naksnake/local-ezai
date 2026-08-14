# Architecture Reference — .agent working memory

> Condensed, load-bearing facts for anyone (human or agent) working on this
> repo. Canonical detail lives in `docs/`. Keep this file short and current;
> update it in the same PR as any architectural change.

## What this project is

- **Today:** self-hosted chat + RAG stack (8 Docker services). See
  [docs/CURRENT_ARCHITECTURE.md](../docs/CURRENT_ARCHITECTURE.md).
- **Becoming:** Local Autonomous Software Engineer Platform — agent runtime,
  sandboxed execution, workflow state machine, layered memory. See
  [docs/TARGET_ARCHITECTURE.md](../docs/TARGET_ARCHITECTURE.md).

## Invariants (do not break)

1. **Existing chat stack is untouched.** New capability = new services in an
   additive compose overlay (`docker-compose.swe.yml`) + additive Make
   targets + additive `.env` vars. (ADR-002)
2. **The inference service is always named `vllm`** and always serves the
   OpenAI API on `:8000`, regardless of engine (vLLM CUDA/CPU, llama.cpp).
   Consumers route via LiteLLM only. (ADR-001)
3. **MCP is the tool protocol.** New tools = MCP servers behind the gateway;
   never bespoke RPC. (ADR-003)
4. **Fail-open retrieval, fail-closed action.** RAG may degrade silently;
   mutating tools require an explicit permission decision. (ADR-008)
5. **No mutating tool outside the sandbox.** exec/write happen only in
   per-run runner containers on git worktrees. (ADR-004)
6. **Event journal is the source of truth** for run state; resume = replay.
   (ADR-006)
7. **One embedding model per Qdrant collection**; switching models = new
   collection (existing rule, kept).
8. **Pin every dependency/image** for local builds (mcp 2.0.0 once broke
   mcpo; see mcpo/Dockerfile comment).

## Current service map (existing, unchanged)

openwebui:3000 · litellm:4000 (auto-RAG hook `config/litellm_custom_callbacks.py`)
· vllm:8000 (engine slot) · embed-server:8001 · qdrant:6333 · searxng:8092
· mcpo:8200 (filesystem/memory/fetch/qdrant-rag) · monitor:8888 (RBAC).
Profiles: GPU (base) / cpu / n97 / n97-igpu via compose overrides with
`!override` on `deploy`. Config via `.env` (`.env.example` = schema).

## Target additions (control/execution/knowledge planes)

- **agentd** — agent runtime + workflow engine + permission engine (FastAPI).
- **toolgw** — tool gateway: registry, risk tiers T0–T4, per-run scoping,
  audit (mcpo stays for chat).
- **sandboxd** — per-run runner containers + git worktrees
  (`workspaces/<run-id>`, branch `swe/<run-id>`); default-deny egress;
  sole holder of docker-socket (filtered) and push credentials.
- **codeidx** — tree-sitter symbol indexing → Qdrant `code-<repo>`; hybrid
  grep+vector retrieval.
- **memoryd** — layered memory: working / episodic (SQLite+JSONL journal) /
  semantic (Qdrant) / procedural (CLAUDE.md-style files, T3-gated writes).
- **ezai CLI** + web console page + chat-ops MCP tools.
- Model **role aliases** in LiteLLM: `swe-planner / swe-coder / swe-reviewer /
  swe-fast / swe-embed` — agents bind to roles, never model names. (ADR-007)

## Workflow (summary)

INTAKE → PLANNING → PLAN_GATE → EXECUTING (task loop: PREPARE→APPLY→CHECK→
DIAGNOSE, bounded) → VERIFYING → REVIEWING → FINALIZING → DONE; BLOCKED /
FAILED / CANCELLED side-paths; autonomy levels A0 dry-run · A1 supervised ·
A2 autonomous-local · A3 autonomous-delivery. All cycles budget-bounded.
Detail: [docs/WORKFLOW_DESIGN.md](../docs/WORKFLOW_DESIGN.md).

## Agents (summary)

Orchestrator (deterministic) · Planner · Context/Research · Implementer ·
Tester/Verifier · Debugger · Reviewer (read-only, fresh session) ·
Integrator/Docs · Security Auditor · Memory Curator. Structured JSON
envelopes between agents; children don't spawn; toolsets are allowlists.
Detail: [docs/AGENT_DESIGN.md](../docs/AGENT_DESIGN.md).

## Repository conventions

- Platform work: `feature/*` or `claude/*` branches; agent-produced work:
  `swe/<run-id>`.
- Tests + ruff + CI required for all new platform code (Phase 1 onward).
- Docs-as-code: `docs/` + `.agent/` updated in the same PR; deviations from
  target architecture need an ADR in [decisions.md](decisions.md).
- Temp/experiments stay out of the repo root; scripts follow the existing
  "runs in Docker, no host Python" pattern where feasible.
