# Local-EZAI — Operation Manual

Day-to-day operation of the platform (services) and the Autonomous SWE
runtime. Maintenance/repair procedures: [MAINTENANCE_GUIDE.md](MAINTENANCE_GUIDE.md).

## 1. The platform stack (8 services)

| Action | Command |
|---|---|
| **First run, all steps** (the five-step contract) | `make setup` = `./install.sh` (edit `.env` once when asked) + `local-ezai setup` (bootstrap · images · embedding model · up · wait-ready · smoke · report); `make setup-gpu` / `setup-cpu` / `setup-n97` assert a profile; re-run any time — every step is idempotent |
| First run, steps 1–3 only: detect hardware, create or repair `.env` (secrets minted, model seeds validated before any download), one review edit | `./install.sh` (or `make install`; `--yes` skips the edit stop, `--check` writes nothing, `--profile n97` asserts a class) |
| First run, steps 4–8 only | `local-ezai setup [--profile P] [--skip-images] [--skip-smoke] [--skip-banner]`; no seeds in `.env`? `local-ezai init` proposes the catalog's recommended set first |
| System packages on a fresh Ubuntu host (Docker, NVIDIA toolkit, Python venv, Node) | `make setup-system` |
| Air-gapped host (no egress) | on a connected host that ran `make setup`: `make bundle BUNDLE=/media/usb/local-ezai-bundle` (images + weights + seeds of its generation); on the air-gapped host: `./install.sh --offline /media/usb/local-ezai-bundle && make setup-offline BUNDLE=/media/usb/local-ezai-bundle` — the same steps, nothing downloaded; remove `EZAI_OFFLINE` from `.env` to go online later |
| First-run acceptance suite (F1–F11, offline) | `make swe-accept` |
| Parity harness — every management operation via CLI-direct · CLI-connected · API, same result, state and audit (release gate, offline) | `make swe-parity`; the whole P6 gate (lint · chat-stack baseline · drill · acceptance · parity · agnosticism gates · full suite): `make release-gate` |
| Agnosticism gates — the third-runtime drill (a mock runtime runs end to end from descriptor data alone), the H1 word audit (no vendor/brand/SKU strings outside descriptor data), H2–H4 class fixtures (offline) | `make swe-gates` |
| The first-run report and the WebUI card | `config/first-run/report.md` (+ `report.json`); the card is the OpenWebUI banner written to `config/first-run/openwebui.env` — delete the file and `docker compose up -d openwebui` to drop it |
| Start / stop / restart | `make up-n97` (or `up-cpu`/`up`) · `make down` · `make restart` |
| Health of all 8 services | `make health` |
| Wait for the LLM to finish loading | `make wait-ready` |
| Live logs (all / one service) | `make logs` · `make logs-vllm`, `make logs-litellm`, … |
| Benchmark tokens/sec | `make bench` |
| Container status | `make status` |
| Update images | `make update-n97` / `update-cpu` / `update` |

Service map (default ports): OpenWebUI :3000 · Monitor :8888 · LiteLLM
:4000 · LLM engine :8000 · Embed :8001 · Qdrant :6333 · SearXNG :8092 ·
mcpo :8200. Every port is overridable in `.env`; occupied ports are
auto-relocated on `make up*`.

**Secrets** live in `.env` (never committed): `LITELLM_MASTER_KEY`,
`WEBUI_SECRET_KEY`, `MCP_API_KEY`, `SEARXNG_SECRET`, `EZAI_CONTROL_TOKEN`,
monitor passwords. `./install.sh` mints them — on a fresh `.env`, and on a
re-run for any that is missing, empty or still the example's placeholder
(a backup `.env.bak.<timestamp>` is written first; your other values are
never touched). By hand: `openssl rand -hex 32`.

**Monitor RBAC:** `admin` / `viewer` HTTP Basic (passwords in `.env`);
scripts authenticate with `Authorization: Bearer $MCP_API_KEY`.

### The control plane (`ezaid`, optional overlay — V1 P2)

| Action | Command |
|---|---|
| Start / stop / logs (container overlay: models, governance, health) | `make control-up` · `make control-down` · `make control-logs` |
| Run on the host instead (foreground; sees your repositories → SWE runs through the API) | `make control-serve` (`EZAI_CONTROL_HOST`, `EZAI_CONTROL_PORT`) |
| Liveness (open) | `curl http://localhost:8010/health` |
| Aggregated health (stack services + platform state) | `curl -H "Authorization: Bearer $EZAI_CONTROL_TOKEN" http://localhost:8010/v1/health` |
| Audit tail | `curl -H "Authorization: Bearer $EZAI_CONTROL_TOKEN" "http://localhost:8010/v1/audit?limit=50"` |
| Who am I (the audit actor) | `… -H "X-EZAI-User: nita" -H "X-EZAI-Client: cli" http://localhost:8010/v1/whoami` |
| Contract | `http://localhost:8010/docs` · `/openapi.json` · committed at `docs/api/ezaid-openapi.json` (`make control-spec` regenerates) |
| Models / queue / projects (PR-9) | `GET /v1/models` · `GET /v1/governance` · `GET /v1/projects` · `GET /v1/roles/<role>` · `GET /v1/generations` · `GET /v1/catalog` |
| Request an activation (idempotent retry-safe) | `curl -X POST -H "Authorization: Bearer $EZAI_CONTROL_TOKEN" -H "X-EZAI-User: nita" -H "Idempotency-Key: $(uuidgen)" -H "Content-Type: application/json" -d '{"group":"coding"}' http://localhost:8010/v1/models/<name>/activate` |
| Approve and apply a pending request | `curl -X POST … -d '{"reason":"benchmarked, fits"}' http://localhost:8010/v1/governance/cr-0001/approve` (then `make up` to reload consumers) |
| Start an SWE run on a registered project (PR-10) | `curl -X POST … -d '{"kind":"run","project":"my-app","task":"add input validation to the signup form"}' http://localhost:8010/v1/runs` → `202` with `run_id` |
| Follow / inspect / cancel a run | `GET /v1/runs` · `GET /v1/runs/<id>` · `GET /v1/runs/<id>/report` · `GET /v1/runs/<id>/journal?tail=50` · `POST /v1/runs/<id>/cancel` |

Every `/v1` call presents the service token; the calling surface forwards
the human it acts for in `X-EZAI-User` (and names itself in
`X-EZAI-Client`), which the audit log records as `<user> via <client>` —
also as `requested_by` / decision author of the change requests it creates.
Mutating calls are audited as `api.<operation>`; retries with the same
`Idempotency-Key` replay the stored answer instead of repeating the
mutation. Every error is `{"error": {"code", "message", "fix"}}`, the same
object `local-ezai … --json` prints. The full verb ↔ endpoint table:
[CLI_REFERENCE.md](CLI_REFERENCE.md). Without a token the service refuses
to start. `local-ezai status` shows `control up|down`. Port:
`EZAI_CONTROL_PORT` (default 8010).

**CLI connected mode (PR-11):** with the daemon up, `local-ezai model|
governance|project|status` go through it automatically (probe of
`EZAI_CONTROL_URL`, default `http://localhost:8010`) using
`EZAI_CONTROL_TOKEN` and your identity — one audit trail for CLI, Admin
Center and tool server. `local-ezai status` shows `transport: connected …`
or `direct (in-process)`. Force a mode with `--transport connected|direct`
(or `EZAI_TRANSPORT`); a CLI on another machine sets `EZAI_CONTROL_URL` +
`EZAI_CONTROL_TOKEN`. Outputs and errors are identical in both modes.

**SWE tools in chat (PR-13):** mcpo also serves the SWE Tool Server at
`http://<host>:8200/swe` (start + inspect only: plan, run, sprint, fix,
evolve, status, report, journal, models, governance queue — no approvals).
It talks to the control plane at `EZAI_CONTROL_URL_MCPO` (default
`http://ezaid:8010` = the container overlay; set
`http://host.docker.internal:8010` when the daemon runs on the host with
`make control-serve`). It is pre-registered in OpenWebUI at boot (Admin
Panel → Settings → Tools, "Local-EZAI SWE"); installs whose admin edited
the tool list in the UI add it by hand (URL `http://<LAN_HOST>:8200/swe`,
bearer `MCP_API_KEY`). **`make orchestrator`** (after the first admin
account exists, idempotent) installs the *Local-EZAI Orchestrator* persona
— `role-orchestrator` + the preset `config/prompts/orchestrator.md` + the
SWE tools enabled for that persona only; plain chat models are untouched.
Runs it starts are audited as actor `swe-server`.

**Runs through the control plane** execute the same pipelines as the CLI,
but only on projects registered with `local-ezai project add`, never with
push, and within `control.max_concurrent_runs` / `max_queued_runs`. The
daemon needs the project on its own filesystem: run `ezaid` on the host
(`.venv-agentd/bin/ezaid`) for SWE runs, or mount the projects directory
into the container at the same path (see the commented hint in
`docker-compose.control.yml`). A daemon restart marks its interrupted runs
`failed` (the journal on disk shows how far they got).

**Admin Center (PR-16):** the monitor at `http://<host>:8888` is growing into
the platform console. `/overview` shows the stack as the control plane sees
it, the generation, the roles and their models, the pending governance queue
and recent runs; `/runs` lists runs and `/runs/<id>` (the link the CLI and
the SWE tools print) shows plan, validation, review, healing, delivery and
the journal; an admin may cancel a run there, a viewer only reads. The
monitor reaches the control plane at `EZAI_CONTROL_URL_MONITOR` (default
`http://ezaid:8010`, the container overlay; set
`http://host.docker.internal:8010` for `make control-serve`) with
`EZAI_CONTROL_TOKEN` — server-side, never sent to the browser — and forwards
your monitor login as the audited human (`admin via admin-center`). With the
control plane down the pages say so and name the fix; the health view and
the knowledge base keep working. Rebuild the monitor image after updating
(`docker compose build monitor`). **Models / Routing / Runtime (PR-17):**
`/models` manages the model lifecycle as an admin — add a model (catalog id
or `hf:` / `gguf:`), benchmark, activate into a group, upgrade, retire,
uninstall, roll back to a generation — with the same approvals and
refusals as `local-ezai model …`; fit badges are the platform's own
verdicts. `/routing` explains what serves each role and why; `/runtime`
shows the engine slot and a pre-check for switching to another runtime
(a switch is an approval-gated activation of a model that runtime serves).
**Governance (PR-18):** `/governance` is the approval queue — open a request
(`/governance/<id>`, the link the CLI and the chat tools print) to see what
changes, the evidence measured on this host, who proposed it and what a
rollback would restore, then *Reject* (a reason is required and recorded)
or *Approve & apply* as an admin; the same audit trail as
`local-ezai governance approve|reject`. **Projects · Sprints · Evolution ·
Memory (PR-19):** `/projects` manages the allowlist (register a git
repository by a path the control plane can see; remove); `/sprints` shows
every sprint with its waves, task results and dependency graph and lets an
admin start one from a pasted specification; `/evolution` shows every cycle
with proposal, benchmarks and bundle and lets an admin run one; `/memory`
browses a project's rules, styles, decisions and fix lessons, with search,
and lets an admin remember a curated entry (`local-ezai memory --add`). The
memory pages use the control plane's 1.1.0 operations
(`GET|POST /v1/projects/<name>/memory`); everything else is unchanged 1.0.0.
**Sign-in handoff and the walkthroughs (PR-20):** the console accepts three
identities, in this order — the monitor login (`admin` / `viewer`, HTTP
Basic, always available); a trusted identity header set by a reverse proxy
you run in front of the monitor (`MONITOR_SSO_TRUSTED_HEADER`, for example
`X-Forwarded-Email`, honoured only when the proxy also sends
`X-EZAI-Proxy-Secret` equal to `MONITOR_SSO_TRUSTED_SECRET`; the addresses
in `MONITOR_SSO_ADMINS` are admins, everyone else a viewer); and the
OpenWebUI session (the `token` cookie the WebUI sets, validated against
`MONITOR_SSO_OPENWEBUI_URL`, default `http://openwebui:8080` inside compose
— an OpenWebUI admin is a console admin, a user a viewer, a pending account
is not signed in; re-checked once a minute). The cookie path works when the
WebUI and the monitor are opened under the same host name (ports do not
matter to cookies); otherwise the Basic prompt appears. Either way the
audit trail names the person (`you@example.com via admin-center`). Nothing
changes unless the `MONITOR_SSO_*` keys are set (see `.env.example`). Every
confirmation on the console is an inline form (no browser dialogs), and
the Overview banner rolls back the last registry change in three clicks.
The five zero-CLI walkthroughs of WEBUI_PRODUCT_STRATEGY §5 are the
platform's own Browser QA workflows
(`agentd/examples/browser-qa.admin-center.yaml`), part of `make swe-test`
in a real Chromium; to click through the same scratch console by hand:
`python3 agentd/tests/fixtures/admin_center_app.py --port 8899 --state /tmp/ezai-journeys`
and open `http://127.0.0.1:8899/overview`.

## 2. The Autonomous SWE runtime

| Action | Command |
|---|---|
| Install (Linux dev) | `make swe-install` (+ `make swe-browsers` for Browser QA) |
| Install (pipx / Windows) | see [agentd/INSTALL.md](../agentd/INSTALL.md) |
| Self-test the runtime | `make swe-test` (offline, 590+ tests) · `make swe-lint` |
| Drill the chat-ops boundary | `make swe-drill` (offline: governance unreachable from chat, prompt-injection red-team, chat/RAG byte-identical baseline — `python3 scripts/chat-stack-baseline.py --update` after a deliberate chat-stack change) |
| Point at the model plane | `AGENTD_LLM__BASE_URL=http://localhost:4000/v1` + `LITELLM_MASTER_KEY` |
| Show model routing | `local-ezai models` |
| Verify models + quality metrics | `local-ezai evaluate-models [--report]` |
| Inspect runs | `ezai runs` · `ezai journal <run-id>` · `local-ezai explain-run <run-id>` |
| Enable container execution | set `sandbox.image` in the global config — [SANDBOX_GUIDE.md](SANDBOX_GUIDE.md) |

### Runtime state locations

| State | Path | Safe to delete? |
|---|---|---|
| Run journals/reports/screenshots/PR bundles | `~/.agentd/runs/<id>/` | yes (history only) |
| Run worktrees | `~/.agentd/workspaces/<id>/` | yes — `git worktree remove <path>` then `git worktree prune` in the repo |
| Project memory | `<repo>/.agent/memory.db*`, `lessons_learned.json` | yes, but the agents forget everything learned |
| Model registry / benchmarks | `<repo>/.agent/model_registry.yaml`, `model_benchmarks.json` | registry is config — keep |

### Branch hygiene

Runs deliver branches, never touch your checkout: `swe/<id>` (runs),
`sprint/<id>` (sprints), `evolve/<id>` (evolution). Merge what you want,
delete the rest (`git branch -D`). Pushing is always opt-in (`--push`).

### Autonomy & safety posture

- Commits are **blocked until validation (incl. Browser QA) is green AND
  the reviewer gate approves** — for agents and for `local-ezai commit`
  alike ([REVIEW_PROCESS.md](REVIEW_PROCESS.md)).
- Pushing (`git_push`) and PR creation are fail-closed: off unless enabled
  per run (`--push`) / configured (`forge:`).
- Self-healing is bounded: max 10 debug/fix iterations + stall detection.
- Agent shell commands run through the **execution sandbox** (ADR-021):
  command allowlist + audit log always; disposable Docker containers with
  workspace-only mounts, no network, and resource limits once
  `sandbox.image` is configured ([SANDBOX_GUIDE.md](SANDBOX_GUIDE.md)).
  On the host fallback, keep running the CLI as a low-privilege user
  against repos whose build commands you trust.

## 3. Operating Local-EZAI on itself

The repository is self-hosting: `.agentd.yaml` at the root wires the
platform's own lint + test suite.

```bash
local-ezai . test          # run Local-EZAI's own validation
local-ezai . fix           # self-heal a red suite (commits when green)
local-ezai . evolve        # autonomous improvement → PR proposal
local-ezai . roadmap       # milestone status
```

Requires the dev environment (`make swe-install`) in the shell running the
CLI. Evolution cycles end at a PR/proposal bundle — merge decisions are
always human ([GOVERNANCE.md](GOVERNANCE.md)).

## 4. Scheduled operations (recommended)

| Cadence | Action |
|---|---|
| daily | `make health`; review any red service via `make logs-<svc>` |
| weekly | `local-ezai evaluate-models` per active repo; prune old runs (`rm -rf ~/.agentd/runs/<old>`) |
| per release | `make swe-test`, `local-ezai . docs`, review RELEASE_NOTES |
| monthly | `make update-<profile>` (image updates), re-run `make health` + `make bench` |
