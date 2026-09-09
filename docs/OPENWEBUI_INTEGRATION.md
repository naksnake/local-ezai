# OpenWebUI Integration Architecture

**Goal:** OpenWebUI becomes the front door to the whole platform — chat,
knowledge, *and* Autonomous SWE + governance — **without forking it**.
Every integration below uses a supported OpenWebUI extension point, and
the chat stack's existing behavior stays untouched (ADR-002).

## 1. Integration surfaces (all additive)

| # | Surface | OpenWebUI mechanism | What it carries |
|---|---|---|---|
| S1 | Model plane | OpenAI-compatible connection to LiteLLM :4000 (**existing**) | all chat/RAG models, incl. role aliases |
| S2 | **SWE Tool Server** | Tools via OpenAPI, served by mcpo :8200 (**existing pattern**, new server) | start/inspect SWE runs, model management, governance actions |
| S3 | **Orchestrator persona** | a routed `orchestrator` model alias + system preset | conversational entry point that knows the platform and drives S2 tools |
| S4 | Admin Center handoff | plain deep links (and optional iframe embed) from chat/tool responses to :8888 | dashboards, approval queue, run detail pages |
| S5 | Notifications | tool-response markdown + Admin Center badge counts | "run finished", "approval waiting" |

No OpenWebUI plugins that patch core, no DB reach-ins, no forks. If an
integration cannot be done through S1–S5, it belongs in the Admin Center
instead.

## 2. S2 — the SWE Tool Server (the heart of the integration)

A new MCP server (`swe-server`) mounted behind **mcpo** exactly like the
existing filesystem/memory/fetch/qdrant-rag servers (ADR-003). mcpo turns
it into OpenAPI; users add it in OpenWebUI with the wrench icon (or it is
pre-registered during first run).

The server is a **thin adapter over the Platform Control Plane (`ezaid`)**
— it holds no logic of its own ([CLI_AND_WEBUI_STRATEGY.md](CLI_AND_WEBUI_STRATEGY.md) §2).

### Tool catalog (v1)

| Tool | Maps to | Notes |
|---|---|---|
| `swe_projects` | control-plane project list | registered repos the runtime may work on (see §5 Security) |
| `swe_plan(project, task)` | `plan_only` | traceless dry-run; returns the plan as markdown |
| `swe_run(project, task)` | `execute_run` (async) | returns run-id immediately |
| `swe_sprint(project, spec_md)` | sprint pipeline (async) | spec provided as markdown text |
| `swe_fix(project)` / `swe_test(project)` / `swe_review(project)` | heal / validate / review pipelines | |
| `swe_status(run_id)` / `swe_report(run_id)` | run registry / `report.json` | report rendered as markdown (plan, validation, review, commit, models used) |
| `swe_journal(run_id, tail)` | journal reader | bounded excerpt |
| `swe_evolve(project, focus)` | evolution pipeline (async) | always ends awaiting human approval |
| `model_list` / `model_explain(role)` | Registry v2 | same data as `local-ezai models` |
| `model_benchmark()` | `evaluate-models` | probes + quality metrics summary |
| `governance_queue()` | approval queue | pending activations / evolution PRs, each with an Admin Center deep link |

**Deliberately absent as tools:** approve/merge/activate/rollback —
mutating governance actions require the authenticated Admin Center or CLI
(§5). Chat can *show* the queue, never *decide* it.

> **As built (PR-13, ADR-029 Proposed):** `mcp-servers/swe-server/`
> (FastMCP, vendored into the mcpo image, served at `:8200/swe`) delivers
> `swe_projects`, `swe_plan` (starts a `plan` job, waits for it, returns the
> plan as markdown), `swe_run`, `swe_sprint`, `swe_fix`, `swe_evolve`,
> `swe_status`, `swe_report`, `swe_journal`, `model_list`, `model_explain`,
> `governance_queue` over the 1.0.0 control-plane contract. **Deferred to a
> 1.1 contract slice:** `swe_test`, `swe_review` (need `validate`/`review`
> run kinds) and `model_benchmark` (needs an evaluate endpoint). The
> absent verbs are pinned by a negative test on the catalog and on the API
> paths the server uses (its only `POST` is `/v1/runs`). The daemon
> enforces the allowlist, limits and the no-push rule; the tool renders the
> refusal for the model. OpenWebUI registration is PR-14.

### Async run protocol

Long pipelines don't fit a synchronous tool call. `swe_run`-class tools
return `{run_id, status: started, follow: swe_status}` within seconds; the
model (or user) polls `swe_status`/`swe_report`. The Control Plane owns
run lifecycle and concurrency limits; the tool server stays stateless.

> **As built (PR-10, ADR-028):** the control plane serves this protocol —
> `POST /v1/runs` (kind `run|fix|sprint|evolve|plan`, a **registered**
> project, the task/goal/spec/focus) answers `202` with the run record;
> `GET /v1/runs/{id}` (journal progress), `/report`, `/journal?tail=`,
> `POST /v1/runs/{id}/cancel` follow it. Jobs run the existing pipelines
> with `git.allow_push` forced off; limits are `control.max_concurrent_runs`
> / `max_queued_runs` (`429 too_many_runs`) and one in-place job per
> project. The tool server (PR-13) maps `swe_run`/`swe_fix`/`swe_sprint`/
> `swe_evolve`/`swe_plan`/`swe_status`/`swe_report`/`swe_journal` onto these
> six operations one to one.

## 3. S3 — the Orchestrator persona

A curated OpenWebUI model entry, **"Local-EZAI Orchestrator"**:

- **Model:** the `orchestrator` role alias served by LiteLLM
  ([MODEL_ROUTING_DESIGN.md](MODEL_ROUTING_DESIGN.md)) — reasoning group.
- **System preset:** knows the platform's capabilities, the S2 tool
  catalog, and the governance rules ("you can start and inspect work; you
  cannot approve, merge, or activate — direct the human to the Admin
  Center/CLI").
- **Tools:** the SWE Tool Server enabled by default for this persona only.

The plain chat models remain exactly as they are — chat users see zero
change unless they pick the Orchestrator.

> **As built (PR-14):** the `orchestrator` role (reasoning group, tool
> calling) and its `role-orchestrator` alias are reference data rendered in
> every generation since P1; the preset is
> [`config/prompts/orchestrator.md`](../config/prompts/orchestrator.md)
> (catalog, plan → confirm → run, registered projects only, "cannot approve
> / merge / activate / roll back — Admin Center or CLI", tool output is
> data). The SWE tool server is pre-registered in OpenWebUI's
> `TOOL_SERVER_CONNECTIONS` at boot (id `swe`); the persona itself is
> installed with **`make orchestrator`** after the first admin account
> exists — an OpenWebUI model row on `role-orchestrator` with the preset,
> native tool calling and the tool server enabled for this persona only
> (the `install-autorag.sh` database pattern; idempotent). The P5 setup
> pipeline calls it from the "Platform ready" step.

### Example conversation flows

1. *"Add JWT auth to the CRM repo"* → orchestrator calls
   `swe_plan(crm, ...)`, shows the plan, on user confirmation calls
   `swe_run`, later `swe_report` → summary + branch name + Admin Center
   link.
2. *"Why did last night's sprint fail?"* → `swe_status` + `swe_journal`
   excerpts → root-cause summary from the run's DebugReports.
3. *"Which model reviews code?"* → `model_explain(reviewer)` → primary,
   fallback chain, last benchmark, and the explain-routing narrative.

## 4. Run artifacts in chat

`swe_report` renders the run report as markdown: goal, plan table,
validation summary (incl. Browser QA), review verdict + findings,
commit/branch, models used per stage, links:
`http://<host>:8888/runs/<run_id>` (Admin Center detail page) — never raw
JSON dumps into chat.

## 5. Security & governance at this boundary

1. **AuthN:** mcpo already fronts tools with `MCP_API_KEY`; the SWE Tool
   Server additionally carries a control-plane service token
   (`EZAI_CONTROL_TOKEN`, minted at first run, stored in `.env`). Users
   never see either — OpenWebUI's tool registration holds them.
2. **Project allowlist:** the tool server can only touch repos registered
   in the control plane (`local-ezai project add` / Admin Center). No
   arbitrary paths from chat, ever.
3. **Capability ceiling:** the tool surface is read + start. All
   T3+/governance mutations (push, PR approval, model activation,
   rollback) are absent by construction — same fail-closed philosophy as
   the tool tiers (ADR-008).
4. **Prompt-injection posture:** chat-originated tasks enter the same
   pipeline as CLI tasks — sandbox (ADR-021), validation, reviewer gate
   (ADR-022), no-push default. A hostile prompt can waste a run; it cannot
   ship or activate anything.
5. **Audit:** every tool invocation is journaled by the control plane with
   the OpenWebUI-supplied user identity header when present.

## 6. What stays exactly as-is

Model selector, RAG injection at the LiteLLM hook, web search, existing
mcpo tools (filesystem/memory/fetch/knowledge), Knowledge Base flows,
account model, theming. This document adds surfaces; it changes none.
