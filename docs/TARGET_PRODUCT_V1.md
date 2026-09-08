# Local-EZAI — Target Product V1 (Productization Phase)

**Status:** Architecture — approved direction for the Productization Phase ·
**Owners:** Principal Architect / Product Manager ·
**Rule:** extend the current architecture; redesign nothing; preserve every
existing capability (platform stack AND Autonomous SWE).

## 1. Product statement

**Local-EZAI V1 is one integrated, self-hosted AI platform**: chat with
local models, retrieve from your own knowledge, search privately, and hand
software work to an autonomous engineering runtime that plans, codes,
validates, reviews, and evolves — all governed by you, all on your own
hardware.

Five pillars, one product:

| Pillar | Exists today as | V1 productization delta |
|---|---|---|
| 1. OpenWebUI | chat UI :3000 | becomes the front door to *everything* (SWE chat-ops, admin entry points) |
| 2. Local-EZAI Runtime | 8-service Docker stack + Makefile | day-2 operations move from `make`/file edits to CLI/WebUI |
| 3. Autonomous SWE | `agentd` + `local-ezai` CLI (11 agents, sandbox, reviewer gate) | reachable from OpenWebUI, not only the terminal |
| 4. Self Evolution | `evolve` pipeline → human-approved PRs | approval queue visible/actionable in the WebUI |
| 5. Model Governance | `.agent/model_registry.yaml`, fallbacks, `evaluate-models` | full lifecycle: install/activate/benchmark/rollback/upgrade/explain, via CLI **and** WebUI |

## 2. The one-sentence contract with the user

> **Edit `.env` once at installation. Never edit a config file again.**

After first run, every management operation is performed through
**OpenWebUI** (and the Admin Center it links to) or the **`local-ezai`
CLI** — both driving the same control plane over the same declarative
state ([CLI_AND_WEBUI_STRATEGY.md](CLI_AND_WEBUI_STRATEGY.md)).

## 3. Personas & top jobs

| Persona | Top jobs in V1 |
|---|---|
| **Operator** (installs & runs the box) | first run ([FIRST_RUN_EXPERIENCE.md](FIRST_RUN_EXPERIENCE.md)); health; model install/upgrade/rollback; backups |
| **Builder** (uses SWE daily) | `run`/`sprint`/`fix` from CLI or chat; review run reports; merge branches |
| **Governor** (approves) | approve model activations, evolution PRs, releases — from the Admin Center queue |
| **Chat user** | talk, RAG, web search — unchanged, plus "ask the platform to do engineering work" |

## 4. V1 component map (extension-only)

```
                         ┌────────────────────────────────────────────┐
   Browser ──────────────►  OpenWebUI :3000  ──link/SSO──►  Admin     │
                         │   chat · RAG · tools · chat-ops │  Center  │
                         │                                 │  :8888   │
                         └───────┬─────────────────┬───────┴────┬─────┘
                                 │ OpenAI API      │ OpenAPI    │ REST
                                 ▼                 ▼ tools      ▼
   Terminal ──► local-ezai ─► LiteLLM :4000    mcpo :8200   ezaid :8010
                CLI │            │  ▲            │(SWE tool     │ Platform
                    │            ▼  │            │ server NEW)  │ Control
                    │      engine slot :8000     └──────┬───────┘ Plane(NEW)
                    │      (vLLM | llama.cpp,           ▼
                    │       always named `vllm`)   agentd runtime
                    └──────────────────────────────► (11 agents, LangGraph,
                          in-process (offline mode)   sandbox, reviewer gate,
                                                      memory, code intel)
   Declarative state (single source of truth, git-versioned):
     .env (install-time only) · config/models/* (Registry v2 + generations)
     config/providers/* (PAL descriptors) · rendered artifacts:
     litellm config + compose overrides  ·  per-repo .agent/*
```

**NEW in V1 (all additive):**

1. **`ezaid` — Platform Control Plane** (:8010, `EZAI_CONTROL_PORT`): one
   OpenAPI service wrapping what already exists as Python functions —
   agentd pipelines, model lifecycle, registry, provider operations,
   health. CLI, Admin Center, and the SWE tool server are thin clients.
2. **Registry v2 + Model Lifecycle Manager** — platform-scope model
   registry with groups (reasoning/coding/chat), providers, and
   generations for rollback ([MODEL_ROUTING_DESIGN.md](MODEL_ROUTING_DESIGN.md),
   [MODEL_LIFECYCLE_MANAGEMENT.md](MODEL_LIFECYCLE_MANAGEMENT.md)).
3. **Provider Abstraction Layer (PAL)** — llama.cpp and vLLM as declarative
   provider descriptors behind the unchanged engine-slot invariant
   ([PROVIDER_ABSTRACTION.md](PROVIDER_ABSTRACTION.md)).
4. **SWE Tool Server on mcpo** — Autonomous SWE + model management exposed
   as OpenWebUI tools (ADR-003: MCP stays the tool protocol)
   ([OPENWEBUI_INTEGRATION.md](OPENWEBUI_INTEGRATION.md)).
5. **Admin Center** — the existing monitor :8888 grown into the platform
   console: Models, Runs, Governance queue, Health, Memory
   ([WEBUI_ADMIN_CENTER.md](WEBUI_ADMIN_CENTER.md)).
6. **First-Run Experience** — one command to a working, governed platform
   ([FIRST_RUN_EXPERIENCE.md](FIRST_RUN_EXPERIENCE.md)).

## 5. Supported matrix (V1 commitments)

- **Front ends:** OpenWebUI, `local-ezai` CLI (legacy `ezai` retained,
  frozen).
- **Engines/providers:** llama.cpp and vLLM (CUDA + CPU builds), one
  active engine slot per host profile, uniform access **only** through
  LiteLLM (ADR-001/ADR-007 unchanged).
- **Logical roles:** orchestrator · planner · coder · debugger · reviewer ·
  memory · chat (+ the existing documentation, evolution, sprint roles —
  preserved).
- **Model groups:** reasoning · coding · chat.
- **Model lifecycle:** install · activate · benchmark · rollback ·
  upgrade · explain routing.
- **Hardware profiles:** GPU (primary), cpu, n97, n97-igpu — unchanged.

## 6. Invariants carried into V1 (unchanged and non-negotiable)

1. Existing chat stack behavior stays byte-identical (ADR-002); OpenWebUI
   is **never forked** — integration uses its supported extension points.
2. The inference service is always named `vllm` on :8000; consumers route
   via LiteLLM only (ADR-001).
3. MCP is the tool protocol; new tools = MCP servers behind mcpo (ADR-003).
4. Fail-closed action: push/PR/merge/model-activation are gated; **agents
   propose, humans approve** (CLAUDE.md, ADR-008, ADR-022).
5. Agent execution stays inside the sandbox (ADR-021); commits stay behind
   validation + the reviewer gate (ADR-016/022).
6. Every state change is journaled/auditable; every config users used to
   edit becomes a **rendered artifact** of declarative state — humans edit
   state through tools, never artifacts ([MODEL_LIFECYCLE_MANAGEMENT.md](MODEL_LIFECYCLE_MANAGEMENT.md) §5).

## 7. Non-goals for V1

Multi-node serving · multi-tenant isolation · cloud model fallback ·
fine-tuning · IDE plugins · forking OpenWebUI · replacing LiteLLM ·
more than one simultaneously active engine slot (tracked as V1.x option,
[PROVIDER_ABSTRACTION.md](PROVIDER_ABSTRACTION.md) §7).

## 8. Definition of Done (product level)

1. Fresh machine → chatting with a governed model set in ≤ 30 minutes on
   the golden path, having edited only `.env`
   ([FIRST_RUN_EXPERIENCE.md](FIRST_RUN_EXPERIENCE.md) §6).
2. Every operation in the parity matrix
   ([CLI_AND_WEBUI_STRATEGY.md](CLI_AND_WEBUI_STRATEGY.md) §3) works from
   both CLI and WebUI with identical results.
3. A model can be installed, benchmarked, activated for a role, upgraded,
   and rolled back without any file edit — with a full audit trail and a
   human approval on activation.
4. An SWE task can be started from an OpenWebUI conversation and its
   branch/report inspected from the same conversation.
5. An evolution PR can be reviewed and (dis)approved from the Admin Center.
6. All 311+ existing tests keep passing; the SWE runtime keeps working
   fully offline (no control plane required for repo work).

Execution sequencing: [V1_IMPLEMENTATION_PLAN.md](V1_IMPLEMENTATION_PLAN.md).
