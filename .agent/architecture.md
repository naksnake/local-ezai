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

openwebui:3000 · litellm:4000 (auto-RAG hook `config/litellm_custom_callbacks.py`;
routing rendered per model generation into `config/rendered/`)
· vllm:8000 (engine slot, network alias `engine`; materialization rendered
per generation) · embed-server:8001 · qdrant:6333 · searxng:8092
· mcpo:8200 (filesystem/memory/fetch/qdrant-rag + `swe` — the SWE tool
server, PR-13) · monitor:8888 (RBAC)
· **ezaid:8010** (V1 control plane, opt-in overlay
`docker-compose.control.yml` — `make control-up`; bearer
`EZAI_CONTROL_TOKEN`, contract `docs/api/ezaid-openapi.json`).
Profiles: GPU (base) / cpu / n97 / n97-igpu via compose overrides with
`!override` on `deploy`, plus the rendered engine override on every start.
Config via `.env` (`.env.example` = schema; model seeds read once by
`make bootstrap`).

## Phases 1–7 — shipped (agentd/)

The `agentd/` sub-project implements the first slices of the target
architecture (see [agentd/README.md](../agentd/README.md)):

- **v1.0 hardening** (ADR-021..024): **execution sandbox** — one policed
  executor for every agent command: regex command allowlist, host or
  Docker execution (`sandbox.image` + reachable daemon ⇒ disposable
  container, workspace-only mount at the host-identical path, network
  `none`, memory/cpu/pids limits, explicit env passthrough), per-run
  `exec_audit.jsonl`, journaled `SANDBOX_MODE`; **mandatory reviewer
  gate** — REVIEW node between green validation and commit in every
  delivering pipeline incl. `local-ezai commit` (`request_changes` or
  high-severity findings block; finding categories incl.
  security/architecture/maintainability; untracked files included in the
  reviewed diff); **semantic code intelligence** — ast/Tree-sitter symbol
  index + import graph persisted in `.agent/code-index/`, repo map
  injected into planning, `code_symbols` tool for
  Planner/Coder/Debugger/Reviewer; **model transparency & dashboard** —
  fallback-aware `models_used` per run, `models` / `explain-run` commands,
  run-history quality metrics + trend history in
  `.agent/model_benchmarks.json`, `evaluate-models --report` →
  docs/MODEL_GOVERNANCE_REPORT.md; evolution evidence reads benchmark
  trends and refuses repeated failed experiments.
- **Model governance & self-sustainability** (ADR-020): per-repo
  `.agent/model_registry.yaml` (`agent_model_map`: primary + fallback per
  role) applied at run preparation; runtime fallback chains (journaled
  `LLM_FALLBACK`); `evaluate-models` probe harness →
  `.agent/model_benchmarks.json`; **Documentation Agent** (generates the
  four repo guides, uncommitted); **Evolution Agent + `evolve` pipeline**
  (evidence → proposal ≤3 improvements → full pipelines on `evolve/<id>` →
  before/after benchmark → RELEASE_NOTES → PR via forge none/gh/api —
  **human-approval terminal, no merge capability exists**); root
  `.agentd.yaml` makes the platform self-hosting (bootstrap exit);
  `type` validation category; production guides under `docs/`
  (final review: [docs/FINAL_RELEASE_REPORT.md](../docs/FINAL_RELEASE_REPORT.md)).
- **Autonomous sprint execution** (ADR-019): Sprint Agent analyzes
  `sprint.md` → requirements + task breakdown + validated dependency DAG
  (cycles/unknown deps rejected deterministically, fed back for retry);
  topological waves; **parallel task execution** in per-task worktrees
  forked from the sprint tip (thread pool, `sprint.max_parallel`),
  ordered merge-back (`--no-ff`; conflict → task failed); dependents of
  failed tasks skipped; every task = full pipeline incl. validation,
  Browser QA, self-healing, memory, gated commit; sprint documentation
  `docs/sprints/sprint-<id>.md` (mermaid DAG + outcomes) committed when
  green; `--simple` keeps the Phase 5 sequential path.
- **Production CLI `local-ezai`** (ADR-018): path-first selection
  (`local-ezai .` / `-C path` / cwd; bare path opens chat); commands
  chat · plan · run · code · test · fix · review · commit · memory ·
  sprint · docs · evolve · roadmap · evaluate-models (ADR-020), each a
  pipeline subset over the agents; in-place commands
  (test/fix/review/commit) vs worktree commands (run/code/sprint);
  sprint = markdown spec → sequential full pipelines on one shared
  `sprint/<id>` branch; cross-platform (Windows: sys.executable
  autodetect, process-group handling); packaging via pipx/pip
  (agentd/INSTALL.md), legacy `ezai` CLI retained.
- **11 agents**: Planner (read-only tools → validated `Plan`), Coder
  (fs/grep/exec tools, one plan task per invocation), Validator
  (deterministic test/lint/build harness), **Debugger** (read-only
  root-cause analysis → structured `DebugReport`, ADR-015), **Browser QA**
  (deterministic Playwright harness: app launch, declarative user
  workflows, console-error detection, screenshots — ADR-016), **Memory**
  (per-repo SQLite learning: `.agent/memory.db` + `lessons_learned.json`,
  ADR-017), **Reviewer** (read-only adversarial diff review → structured
  verdict/findings, memory-style-aware, ADR-018), **Sprint** (requirement
  analysis → task DAG for parallel execution, ADR-019),
  **Documentation** (generates USER_GUIDE/OPERATION_MANUAL/
  MAINTENANCE_GUIDE/RELEASE_NOTES, ADR-020), **Evolution** (read-only
  evidence → improvement proposals, ADR-020), Git (add/commit,
  T3-gated push, **refuses failing validation** — `COMMIT_BLOCKED`;
  never stages memory files).
- **Project memory rules** (ADR-017): six kinds (architecture_decision /
  coding_style / project_rule / failed_fix / successful_fix /
  implementation); deterministic recording from every terminal run; memory
  injected into Planner + Debugger prompts; deterministic repeat-mistake
  detection (`MEMORY_REPEAT_WARNING`); store lives in the ORIGIN repo's
  `.agent/`, lazily created, excluded from commits.
- **Browser QA pipeline rules** (ADR-016): validation fails on step
  failure, failed verification, or ANY console error; stage skipped-but-
  not-succeeded when command checks fail; commits gated until green;
  browser failures self-heal via the same DEBUG→FIX→REVALIDATE loop
  (RCA category `browser`); app relaunched per revalidation.
- **Self-healing LangGraph machine** (ADR-013/ADR-015): PLAN → CODE↺ →
  VALIDATE → (fail) DEBUG → FIX → REVALIDATE loop — max
  `limits.max_heal_iterations` (10) cycles + stall detection
  (`stall_threshold` identical failure signatures → abort); routes never
  read LLM output.
- **RCA engine** (`rca.py`, deterministic): error categorization
  (syntax/import/assertion/exception/timeout/environment/lint/build/
  unknown), locations, stable signatures, strategy seeds.
- **Tool layer**: `Tool`/`ToolRegistry` with tiers T0–T3, per-agent
  allowlists, output caps, journaled permission decisions (fail-closed).
- **Isolation (interim, ADR-014)**: git worktree per run on `swe/<run-id>`,
  path containment, timeouts; container sandboxing remains open.
- **Config**: defaults < YAML < `AGENTD_*` env; per-repo `.agentd.yaml`
  (validation/limits only — a repo cannot self-grant push).
- **Journal**: JSONL per run under `~/.agentd/runs/<run-id>/` + report.json
  incl. `healing[]` records; `ezai runs` / `ezai journal` for inspection.
- Entry points: `ezai run|plan|runs|journal|version`; Make targets
  `swe-install`, `swe-test`, `swe-lint`, `swe-run`, `swe-plan`. Tests are
  fully offline (ScriptedLLM); CI in `.github/workflows/agentd-ci.yml`.

## Productization P1 — done (ADR-025/026, ADR-027 Accepted; docs/V1_PR_PLAN.md)

Live since the PR-7 cutover: `make bootstrap` consumes the `.env` seeds
into generation 1, compose mounts the rendered LiteLLM config and every
`make up*` adds the rendered engine override; day-2 model changes go
through `local-ezai model …` + the governance queue. Modules:

- **Registry v2** (`registry_v2.py`, PR-1): platform-scope models ×
  lifecycle states, ordered groups, roles with pins + `requires`
  contracts; deterministic resolution; immutable generations; reference
  default set as packaged data (`defaults/reference_registry.yaml` — the
  CLAUDE.md map; golden-tested against `.agent/model_registry.yaml`).
- **Capability** (`capability.py`, PR-2): detected vector → class
  (`accel-large|accel-small|cpu-standard|cpu-low`; legacy profiles are
  preset aliases), pure advisory `fit()`.
- **Runtime descriptors + renderer** (`runtime_descriptor.py`,
  `render.py`, `config/providers/{llamacpp,vllm}.yaml`, PR-3): runtimes as
  data (six-verb contract, image table per accelerator kind, tuning per
  class); renderer fills templates → `litellm-config.yaml` (model +
  `role-*` aliases → `engine:8000`), `docker-compose.engine.yml`,
  `role_map.yaml` (ADR-020 shape), capability report, router preset;
  render-time capability negotiation (primaries + fallbacks); one-slot
  rule; hash manifest + drift refusal under `config/rendered/` (parallel
  path until the PR-7 cutover). The compose slot service carries the
  neutral network alias **`engine`** (ADR-026 R-3); `vllm:8000` still
  works.
- **Lifecycle install / validate / benchmark** (`lifecycle.py`,
  `fetch.py`, `catalog.py`, `defaults/catalog.yaml`, PR-4): source
  resolver (`hf:` · `gguf:` · catalog id · `auto`), resumable checksummed
  GGUF fetch + delegated hub download into the descriptor's weights dir,
  state machine as data, `validate_model`/`bench` verbs run against a
  **side-loaded** engine (standalone compose project from the PR-3
  materialization, ephemeral port, always torn down), tokens/sec on the
  entry + per-model trend series in `.agent/model_benchmarks.json`
  (coexists with `evaluate-models`), catalog as pluggable data with a
  `fit()`-driven recommender. No CLI yet (PR-6).
- **Activation + governance** (`governance.py`, `activation.py`, PR-5):
  file-backed change-request queue + append-only audit log under
  `config/governance/`; proposals (`activate`, `upgrade`) carry diff,
  affected roles, evidence and are validated by a dry render; computed
  approval matrix (policy-approved when no role chain/slot runtime
  changes); bounded evolution lane; **atomic apply**: dry render → save
  generation → write artifacts → reload changed → health → self-rollback
  as a new generation on any failure; rollback without approval (audited,
  notifies); reconcile for half-applied states; `retire`/`uninstall`
  guards. Reload/health are pluggable seams, render-only until PR-7.
- **Role aliases + platform CLI** (`routing.py`, `platform_cli.py`,
  PR-6): agentd defaults bind to `role-<role>` aliases (no model name in
  code; the hand-written LiteLLM profiles serve the aliases as data);
  routing layers aliases < platform role map < per-repo ADR-020 registry
  (repo pin = exact chain), platform found via `platform.config_dir` or
  walk-up from the project; `local-ezai model|governance|project|status|
  up|down` namespaces in direct mode over the PR-3/4/5 modules.
- **Bootstrap + cutover** (`bootstrap.py`, `defaults/legacy_seeds.yaml`,
  PR-7): `.env` seeds read once (legacy families migrated as data, served
  name kept), F8 validation before any download, install → benchmark →
  the one implicit approval → apply → `EZAI_SEEDS_CONSUMED` stamp;
  `config/rendered/` is the live LiteLLM + engine config; the hand-written
  LiteLLM variants are test fixtures.
- **Control plane skeleton** (`control/`, `audit.py`, PR-8, ADR-028
  Proposed): `ezaid` — FastAPI behind the `agentd[control]` extra (direct
  mode never imports it): bearer service token + forwarded identity
  (`X-EZAI-User`/`X-EZAI-Client` → audit actor), the **single audit log**
  shared with the governance queue, one error envelope, `/v1/health`
  aggregation (service table as data, engine via the `engine` alias),
  `/v1/whoami`, `/v1/audit`, versioned OpenAPI artifact
  `docs/api/ezaid-openapi.json` (contract-surface tripwire); opt-in
  compose overlay + `make control-*`; `local-ezai status` probes it.
- **Lifecycle + governance endpoints** (`control/api.py`, `control/deps.py`,
  `control/idempotency.py`, `platform_errors.py`, PR-9): the PR-6 verbs'
  orchestration became shared operations in `platform_cli` (CLI formats,
  API serves — parity tested); 19 `/v1` operations (models, generations,
  roles, catalog, governance, projects) with the forwarded identity as
  actor; `Idempotency-Key` replay/conflict; one error vocabulary
  (`classify()` → `{code, message, fix}` + status/exit) printed by the CLI
  in `--json` and returned by the API; mutating calls audited as
  `api.<operationId>`; reload refused where the daemon cannot run compose.
- **Run endpoints** (`control/runs.py`, `control/runs_api.py`, PR-10): the
  async run registry — `POST /v1/runs` (202) starts `run`/`fix`/`sprint`/
  `evolve`/`plan` jobs through the existing pipelines on a bounded worker
  pool; status with journal progress, report, bounded journal excerpt,
  cancel (queued now; running at the next model call via a wrapped model
  client — no core-graph change); records under `config/control/runs/`
  recovered on restart; limits (`control.max_concurrent_runs`,
  `max_queued_runs`; one in-place job per project); registered projects
  only and never a push (the chat-ops ceiling).
- **CLI connected mode** (`control/client.py`, `platform_cli` transport
  selection, PR-11): the management verbs probe the daemon's liveness once
  and, when it answers, run through the API (token + forwarded identity +
  idempotency key) — `ctx.ops` is `DirectOps` in-process or `ConnectedOps`
  over HTTP, the formatters are shared, so text/JSON/errors are identical
  (parity tested, incl. a real socket); `--transport` / `EZAI_TRANSPORT` /
  `EZAI_CONTROL_URL`; a requested connected transport with no daemon (or a
  daemon without a configured token) fails fast; `bootstrap`/`up`/`down`
  stay host-only.
- **P2 closed** (PR-12, ADR-028 Accepted): kill-the-daemon (real process,
  SIGKILL — repo work unaffected, management falls back / fails fast), two
  concurrent runs supervised through the API, contract **frozen at 1.0.0**
  (29 operations, inventory-pinned); deployment shapes: `make control-up`
  (container overlay, model/governance over `config/`) and
  `make control-serve` (host daemon, SWE runs).
- **SWE Tool Server** (`mcp-servers/swe-server/`, PR-13, ADR-029 Proposed):
  a vendored FastMCP server behind mcpo (`:8200/swe`), a thin adapter over
  the 1.0.0 contract (no agentd import) — `swe_projects`, `swe_plan` (A0,
  waits), `swe_run`/`swe_sprint`/`swe_fix`/`swe_evolve` (run id at once),
  `swe_status`, `swe_report` (markdown per kind), `swe_journal`,
  `model_list`, `model_explain`, `governance_queue` (read-only); no
  approve/activate/rollback/cancel tools by construction (negative test);
  refusals rendered as answers; registered in `config/mcpo-config.json`,
  reaching the daemon at `EZAI_CONTROL_URL_MCPO`.
- **Orchestrator persona** (PR-14): the `orchestrator` role + alias are
  reference data since P1 (proven end to end); the system preset is data
  (`config/prompts/orchestrator.md`: catalog, plan-then-confirm, registered
  projects only, no governance from chat, tool output is data); the SWE
  tool server is pre-registered in OpenWebUI's `TOOL_SERVER_CONNECTIONS`;
  `make orchestrator` installs the persona model row on `role-orchestrator`
  with the tool server bound to it only (database pattern of
  `install-autorag.sh`, after the first admin exists).

## Target additions (control/execution/knowledge planes)

- **agentd** — agent runtime + workflow engine + permission engine (FastAPI).
  As built (ADR-028, PR-8): the served control plane is `ezaid`
  (`agentd.control`), an opt-in overlay over the same package; the runtime
  itself stays an in-process library behind the CLI.
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
- Model **role aliases** in LiteLLM — agents bind to roles, never model
  names (ADR-007). As built (ADR-026 R-1, PR-3/PR-6): the aliases are
  `role-<role>` (`role-planner`, `role-coder`, …), rendered from Registry v2
  and carried by the shipped LiteLLM profiles.

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
