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
`WEBUI_SECRET_KEY`, `MCP_API_KEY`, `SEARXNG_SECRET`, monitor passwords.
Generate with `openssl rand -hex 32`.

**Monitor RBAC:** `admin` / `viewer` HTTP Basic (passwords in `.env`);
scripts authenticate with `Authorization: Bearer $MCP_API_KEY`.

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
