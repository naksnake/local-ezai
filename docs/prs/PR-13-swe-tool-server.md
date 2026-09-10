# PR-13 — `swe-server` MCP tool server (phase P3 opens)

**Phase:** P3 (opens) · **ADR:** ADR-029 → **Proposed** · **Size:** L ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-13 ·
**Depends on:** PR-10 (run endpoints), PR-9 (models / governance / projects
endpoints), PR-8 (auth, audit), the mcpo image pins · **Status:** implemented
on `claude/next-ready-pr-bnq7r3` as the eleventh stacked commit (PR-3..12
unmerged; each commit reverts independently)

## Scope (as delivered)

- **`mcp-servers/swe-server/swe_server.py`** — a vendored Python MCP server
  (FastMCP, stdio) behind mcpo, like `qdrant-rag`. A **thin adapter** over
  the `ezaid` 1.0.0 contract: every tool is one or two HTTP calls carrying
  the service token, `X-EZAI-Client: swe-server` and, for the one mutating
  call (`POST /v1/runs`), a fresh `Idempotency-Key`. It imports nothing from
  agentd (only `mcp` + `httpx`, already pinned in the mcpo venv), so the
  mcpo image stays small.
- **Catalog (start + inspect only):** `swe_projects` · `swe_plan` (starts a
  `plan` job, waits for it — bounded by `EZAI_PLAN_WAIT_S` — and returns the
  plan as markdown with "show it, get confirmation, then `swe_run`") ·
  `swe_run` / `swe_sprint` / `swe_fix` / `swe_evolve` (return the run id
  within the request with the follow hint and the Admin Center link) ·
  `swe_status` (state, timing, journal progress, next step) · `swe_report`
  (markdown per kind — run: goal, plan table, validation incl. Browser QA,
  review verdict + findings, commit/branch, self-healing iterations, models
  used per stage; sprint: task table, waves, report doc; evolve: proposal,
  tasks, benchmark before → after, PR/bundle, "awaiting human review"; plan:
  the table + "executed nothing") · `swe_journal` (bounded excerpt, ≤ 200
  events) · `model_list` · `model_explain` · `governance_queue` (pending +
  decided with Admin Center deep links and the footer "decisions are made
  in the Admin Center or the CLI — never from chat").
- **Refusals are answers.** The daemon's envelope (`not_found` for an
  unregistered project with the `project add` fix, `unauthorized`,
  `report_pending`, an unreachable control plane with `make control-up` /
  `make control-serve`) is rendered as text for the model, never raised.
- **Project allowlist:** enforced by the control plane (PR-10); the tools
  pass project names only and render the refusal.
- **mcpo registration:** `config/mcpo-config.json` gains the `swe` server
  (`/opt/mcpo/bin/python /mcp-servers/swe-server/swe_server.py`, env
  `EZAI_CONTROL_URL=${EZAI_CONTROL_URL_MCPO:-http://ezaid:8010}`, the token,
  `EZAI_ADMIN_URL`); `mcpo/Dockerfile` copies the server; the mcpo service
  passes `EZAI_CONTROL_URL_MCPO`, `EZAI_CONTROL_TOKEN`, `LAN_HOST`,
  `MONITOR_PORT` and gains `extra_hosts: host.docker.internal:host-gateway`
  so it can reach a host daemon (`make control-serve`). `.env.example`
  documents `EZAI_CONTROL_URL_MCPO`. mcpo serves it at `:8200/swe`.
- **ADR-029 → Proposed** (`.agent/decisions.md`); the MCP SDK joins the
  agentd `dev` extra so the suite can build the server.
- **Excluded by design:** `swe_test`, `swe_review`, `model_benchmark` — they
  need `validate` / `review` run kinds and an evaluate endpoint that the
  frozen 1.0.0 contract does not have; recorded as a **1.1 contract
  addition** for a later P3 slice rather than reopening P2 here. OpenWebUI
  pre-registration (`TOOL_SERVER_CONNECTIONS`) and the Orchestrator persona
  (PR-14). Negative-test hardening + prompt-injection drill (PR-15).
  Forwarding the OpenWebUI user identity: mcpo's stdio transport carries no
  request headers, so the audit actor is `swe-server` (recorded limitation;
  a header-passing gateway would restore `<user> via swe-server`).

## Design decisions made in this PR

1. **Vendored, not packaged.** The server lives under `mcp-servers/` like
   the other custom server and runs in the mcpo venv; installing agentd into
   the mcpo image would drag the whole agent runtime along for a 500-line
   adapter. The test suite loads it by path.
2. **The daemon enforces; the tool renders.** Allowlist, token, limits and
   the no-push rule are the control plane's (PR-10); the tool server holds
   no policy of its own to get wrong — and cannot bypass it.
3. **No governance verbs by construction.** The negative test pins the
   catalog, checks that the only `POST` path in the source is `/runs`, and
   that no API path used contains approve / reject / activate / rollback /
   upgrade / retire / uninstall / install / merge / push / cancel.
4. **`swe_plan` is synchronous for the model.** A plan is one model call;
   waiting (bounded) gives the conversation the plan → confirm → run shape
   of §3 without a second round trip.
5. **Markdown, never JSON dumps** (§4): renderers per kind, deep links to
   the Admin Center (P4) already in place.

## Tests (7 new; suite 558 → 565)

- `tests/integration/test_swe_server.py`: the catalog is start + inspect
  only (names, forbidden verbs, the only POST is `/runs`, no agentd import,
  FastMCP registers exactly the twelve tools with descriptions and
  schemas); **the §3 loop from one conversation** — `swe_projects`, a real
  scripted `swe_plan` (zero traces in the repo), `swe_run` → `swe_status`
  (running → completed, next-step hints) → `swe_report` (validation, review,
  commit on `swe/`, models used, Admin Center link, no raw JSON) →
  `swe_journal` (bounded), audited as actor `swe-server`; the other start
  tools (`fix`/`sprint`/`evolve`) and a pending report; refusals rendered
  (unregistered project with the fix, unknown run/role, wrong token audited
  without the secret, unreachable control plane with both start commands);
  model and governance views read-only (a CLI-made request appears with its
  deep link; nothing the tools do changes its status); mcpo registration
  and image wiring (config entry, Dockerfile copy, compose env,
  `extra_hosts`, existing servers untouched, OpenWebUI connections
  untouched); `main` refuses to start without the token.
- All earlier tests pass unmodified; goldens intact.

## Behavior notes

- The chat stack's existing tools (filesystem / memory / fetch / qdrant-rag)
  are untouched; mcpo additionally serves `/swe`. **OpenWebUI shows no new
  tool until PR-14 registers the connection** — chat users see zero change.
- The mcpo service gains four environment variables and an `extra_hosts`
  entry; `make build` rebuilds the mcpo image (one `COPY` line added).
- `agentd[dev]` now installs `mcp>=1.28,<2`.

## Rollback note

`git revert <commit>` removes the server directory, the mcpo config entry,
the Dockerfile line, the compose env/extra_hosts and the ADR entry. A
rebuilt mcpo image simply stops serving `/swe`; nothing else changes.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane —
  unavailable here; to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry (deviations justified above: three
      catalog tools deferred to a 1.1 contract slice; OpenWebUI
      registration left to PR-14)
- [x] New tests cover the scope; full suite green (565); ruff clean (the
      vendored server linted with the agentd config)
- [x] Pre-existing tests unmodified; goldens passing
- [x] Architecture docs updated to as-built (OPENWEBUI_INTEGRATION §2,
      CURRENT_ARCHITECTURE §3.3, OPERATION_MANUAL, README); ADR-029 entered
      as Proposed
- [x] Rollback note present
- [x] No model/vendor names in code (the server names roles and kinds only)
