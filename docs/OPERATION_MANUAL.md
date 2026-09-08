# Local-EZAI — Operation Manual

Day-to-day operation of the platform (services) and the Autonomous SWE
runtime. Maintenance/repair procedures: [MAINTENANCE_GUIDE.md](MAINTENANCE_GUIDE.md).

## 1. The platform stack (8 services)

| Action | Command |
|---|---|
| First-time setup (per hardware profile) | `make setup-n97` · `make setup-cpu` · `make setup-gpu` |
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
monitor passwords. Generate with `openssl rand -hex 32`.

**Monitor RBAC:** `admin` / `viewer` HTTP Basic (passwords in `.env`);
scripts authenticate with `Authorization: Bearer $MCP_API_KEY`.

### The control plane (`ezaid`, optional overlay — V1 P2)

| Action | Command |
|---|---|
| Start / stop / logs | `make control-up` · `make control-down` · `make control-logs` |
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

**Runs through the control plane** execute the same pipelines as the CLI,
but only on projects registered with `local-ezai project add`, never with
push, and within `control.max_concurrent_runs` / `max_queued_runs`. The
daemon needs the project on its own filesystem: run `ezaid` on the host
(`.venv-agentd/bin/ezaid`) for SWE runs, or mount the projects directory
into the container at the same path (see the commented hint in
`docker-compose.control.yml`). A daemon restart marks its interrupted runs
`failed` (the journal on disk shows how far they got).

## 2. The Autonomous SWE runtime

| Action | Command |
|---|---|
| Install (Linux dev) | `make swe-install` (+ `make swe-browsers` for Browser QA) |
| Install (pipx / Windows) | see [agentd/INSTALL.md](../agentd/INSTALL.md) |
| Self-test the runtime | `make swe-test` (offline, 260+ tests) · `make swe-lint` |
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
