# CLI Reference — `local-ezai`

The production interface to Local-EZAI's Autonomous SWE subsystem.
Install: [agentd/INSTALL.md](../agentd/INSTALL.md). All commands drive the
agent roster (Planner, Coder, Validator, Debugger, Browser QA, Reviewer,
Memory, Documentation, Evolution, Sprint, Git).

## Project selection

```bash
local-ezai .                        # bare path → chat for that project
local-ezai /home/test/project/CRM   # any directory as first argument
local-ezai -C /path <command>       # git-style explicit path
local-ezai <command>                # default: current directory
```

## Global options (before or after the command)

`--config FILE` (or `$AGENTD_CONFIG`) · `--verbose` · `--version`

## Commands

| Command | What it does | Writes to your branch? |
|---|---|---|
| `chat` | memory-aware REPL with the local model (`/reset`, `/exit`) | no |
| `plan "<task>"` | execution plan (JSON), traceless dry-run | no |
| `run "<task>" [--push] [--in-place] [--max-iterations N] [--json]` | full pipeline: plan → code → validate → Browser QA → self-heal → **review gate** → commit on worktree branch `swe/<id>` | no (branch) |
| `code "<task>" [--in-place] [--json]` | plan + implement only; changes left **uncommitted** | no (worktree) |
| `test [--json]` | validation in place: lint / **type** / build / test + Browser QA | no |
| `fix [--goal G] [--max-iterations N] [--json]` | repair failing validation in place (VALIDATE → DEBUG → FIX → REVALIDATE, max 10 iterations); review gate, then commit when green | **yes** (current branch) |
| `review [--json]` | Reviewer Agent over the working-tree diff (or last commit when clean); exit 1 on `request_changes` | no |
| `commit [-m MSG] [--push]` | validate, then commit the working tree — **blocked until validation incl. Browser QA is green AND the reviewer gate approves** | **yes** |
| `memory [--add TEXT --kind K] [--search TERM] [--limit N]` | inspect / add project memory (`.agent/memory.db`) | no (memory only) |
| `docs [--focus F] [--json]` | Documentation Agent generates/refreshes USER_GUIDE, OPERATION_MANUAL, MAINTENANCE_GUIDE, RELEASE_NOTES (uncommitted) | no (worktree files) |
| `sprint <spec.md> [--simple] [--keep-going] [--max-parallel N] [--push] [--json]` | autonomous sprint: requirement analysis → dependency waves → **parallel** task pipelines → merged commits + committed sprint report on `sprint/<id>` | no (branch) |
| `evolve [--focus F] [--push] [--json]` | evolution cycle: analyze history/failures/bottlenecks → propose → implement → validate → benchmark → **PR / proposal bundle** on `evolve/<id>`; always ends awaiting a human | no (branch) |
| `roadmap [--full]` | show `.agent/roadmap.md` (milestone lines, or the full file) | no |
| `evaluate-models [--json] [--report]` | probe every routed model role (incl. fallback chains); aggregate run-history quality metrics; record `.agent/model_benchmarks.json` (with trend history); `--report` writes `docs/MODEL_GOVERNANCE_REPORT.md` | no (benchmarks file) |
| `models [--json]` | show the live model routing: primary + fallback per agent role, from `.agent/model_registry.yaml` | no |
| `explain-run [run-id] [--json]` | which model handled each stage (planning/coding/debugging/review/…) of a run — fallback-aware; defaults to the project's latest run | no |
| `version` | print the version | no |

## Platform namespaces (V1 · PR-6, direct mode)

These verbs act on the **platform's** declarative state — Registry v2 +
generations, runtime descriptors, catalog, governance queue under
`config/` — not on a repository. The platform is found via
`platform.config_dir` in the agentd config, `$AGENTD_PLATFORM__CONFIG_DIR`,
or by running inside the local-ezai checkout. Every mutation persists a
generation and an audit record; `--json` is available everywhere.

**Transport (PR-11).** When the `ezaid` control plane answers its liveness
probe (`EZAI_CONTROL_URL`, default `http://localhost:8010`), these verbs run
**through it** — with `EZAI_CONTROL_TOKEN`, your identity forwarded
(`X-EZAI-User`, `X-EZAI-Client: cli`), an idempotency key per mutation —
so the audit trail and the queue are shared with the Admin Center and the
tool server; otherwise they run in-process. Same commands, same text, same
JSON, same errors. `--transport auto|connected|direct` (or `EZAI_TRANSPORT`)
forces a mode: `connected` fails fast (exit 2) when the daemon is
unreachable or no token is configured; `direct` never contacts it.
`status` prints which transport answered. `bootstrap`, `up` and `down`
always act on this host.

| Command | What it does | Approval? |
|---|---|---|
| `model install <ref> [--name N] [--runtime R] [--group G] [--refetch]` | `<ref>` = `hf:<org/repo>` · `gguf:<url \| hf://org/repo/file.gguf \| path>` · catalog id · `auto` (needs `--group`); fetch → side-load validation → `installed` / `failed` | no (audited) |
| `model benchmark <name> [--base-url URL]` | tokens/sec on this host (side-loaded engine, or the live one) → recorded on the entry + `.agent/model_benchmarks.json`; → `benchmarked` | no |
| `model activate <name> [--group G] [--position N] [--role R] [--reload]` | change request: primary of a group (`--position 0`, default with `--group`), pinned to a role, or fallback of its declared groups; policy-approved and applied at once when no serving role changes | **yes** when a serving role changes |
| `model upgrade <old> <new> [--reload]` | change request swapping versions in every group and pin; old version retired (kept) | **yes** |
| `model rollback [--to-generation N] [--reason R] [--reload]` | restore the previous or a named generation as a new generation; notifies | no (audited) |
| `model retire <name>` | remove a non-serving active model from resolution (a serving primary or last member is refused) | no |
| `model uninstall <name> [--force]` | delete weights of a retired/failed model; `--force` when a stored generation could roll back to it | no |
| `model explain <role>` | what serves the role and why (generation, pin/group, skips) + the role contract check per model | — |
| `model history [--limit N]` | generations with notes and diffs | — |
| `model catalog [--group G] [--runtime R]` | catalog entries, or ranked recommendations for a group with fit verdicts (F9) | — |
| `governance list [--status S]` · `show <id>` | the approval queue and one request's evidence (diff, affected roles, benchmarks) | — |
| `governance approve <id> [--reason R] [--by NAME]` | approve a pending request **and apply it** (render → reload → health → self-rollback on failure) | human act |
| `governance reject <id> --reason R` | reject with a recorded reason | human act |
| `project add <path> [--name N]` · `list` · `remove <name\|path>` | the chat-ops project allowlist (`config/projects.yaml`) | no (audited) |
| `status` | platform root, capability class, generation vs rendered generation, slot runtime, model states, pending approvals, engine/router health | — |
| `up [--profile gpu\|cpu\|n97\|n97-igpu] [--rendered]` · `down [--profile P]` | `docker compose` wrappers over the profile files; `--rendered` adds `config/rendered/docker-compose.engine.yml` | — |
| `bootstrap [--env PATH] [--dry-run] [--skip-benchmark] [--force] [--reload]` | consume the `.env` model seeds **once** into generation 1: validate (every problem with its fix, before any download) → install → benchmark → activate → render → stamp `EZAI_SEEDS_CONSUMED`; legacy `CHAT_MODEL`/`CPU_*`/`N97_*` migrated automatically; `--dry-run` shows the planned diff; `--force` on a bootstrapped platform files a governed change request instead | implicit (generation 1 only) |

`--reload` (activate/upgrade/rollback/approve) reloads consumers and
health-checks the new generation through the descriptor's probes; without it
the generation is rendered only (the default until the cutover, PR-7).
`--by NAME` sets the actor recorded in `config/governance/log.jsonl`
(default `$USER`).

## The installer (`install.sh`, V1 · PR-21)

`./install.sh [--yes] [--check] [--profile gpu|cpu|n97|n97-igpu | --class C]
[--runtime R] [--no-editor] [--json]` — also `make install`; the logic is
`python -m agentd.installer --root <checkout>` and runs from the agentd venv
the script creates on demand. Steps 1–3 of the first run: **detect** the
capability class (or assert one — `--profile cpu|n97|n97-igpu` / `--class`
write `EZAI_CAPABILITY_CLASS` into `.env`, which `bootstrap` then honors;
`--profile gpu` only checks that an accelerator exists); **generate** `.env`
from `.env.example` with the seven placeholder secrets minted and
`AI_RUNTIME` chosen from the runtime descriptors, or **repair** an existing
`.env` (backup first; your values untouched; only missing or placeholder
secrets minted; idempotent); **validate** the model seeds with the same
rules as `bootstrap` — every problem with its fix, nothing downloaded. A
fresh `.env` opens once in `$EDITOR` on a terminal; without one the script
prints what to edit and exits 3. Exit codes: 0 ready · 1 problems printed ·
2 usage / preflight · 3 review stop. The installer never picks models.

## The control plane daemon (`ezaid`, V1 · PR-8)

`ezaid` (installed with the `agentd[control]` extra) serves the platform
over OpenAPI on `:8010` (`EZAI_CONTROL_PORT`), authenticated by the bearer
service token `EZAI_CONTROL_TOKEN`. Normally it runs as the compose overlay
(`make control-up`); on the host, run it inside the checkout or point it at
the platform with `--platform config/`.

| Command | What it does |
|---|---|
| `ezaid [--host H] [--port P] [--config C] [--platform DIR]` | serve; refuses to start without a token (exit 2, the fix printed) |
| `ezaid --print-spec` · `ezaid --write-spec [PATH]` | the OpenAPI contract document (needs neither platform nor token); default path `docs/api/ezaid-openapi.json` |
| `ezaid --version` | package version + contract version |

Skeleton endpoints (PR-8): `GET /health` (liveness, open) · `GET /v1/health`
(control info + platform snapshot + per-service probes) · `GET /v1/whoami`
· `GET /v1/audit?limit=N` · `GET /openapi.json`, `/docs`. Forward the human
behind a call with `X-EZAI-User` (and the surface with `X-EZAI-Client`).
Every error is `{"error": {"code", "message", "fix"}}` — the same object
the CLI prints in `--json` mode. `local-ezai status` reports `control up|down`.

### Verb ↔ endpoint mapping (PR-9)

Every operation below is the **same function** the CLI verb calls; the API
body equals the verb's `--json` output (tested). Mutating calls accept
`Idempotency-Key` (a retry with the same key and payload replays the stored
answer; a different payload is `409 idempotency_conflict`) and are audited as
`api.<operationId>` with the forwarded identity.

| CLI verb | Endpoint (operationId) |
|---|---|
| `status` (models part) | `GET /v1/models` (`models_list`) |
| `model install <ref> [--name] [--runtime] [--group] [--refetch]` | `POST /v1/models` `{ref, name?, runtime?, group?, refetch?}` (`model_install`) |
| `model benchmark <name> [--base-url]` | `POST /v1/models/{name}/benchmark` `{base_url?}` (`model_benchmark`) |
| `model activate <name> [--group] [--position] [--role] [--reload]` | `POST /v1/models/{name}/activate` `{group?, position?, role?, reload?}` (`model_activate`) → `{request, applied|null}` |
| `model upgrade <old> <new> [--reload]` | `POST /v1/models/upgrade` `{old, new, reload?}` (`model_upgrade`) |
| `model rollback [--to-generation] [--reason] [--reload]` | `POST /v1/generations/rollback` `{to_generation?, reason?, reload?}` (`generation_rollback`) |
| `model retire <name>` | `POST /v1/models/{name}/retire` (`model_retire`) |
| `model uninstall <name> [--force]` | `DELETE /v1/models/{name}?force=` (`model_uninstall`) |
| `model explain <role>` | `GET /v1/roles/{role}` (`role_explain`) |
| `model history [--limit]` | `GET /v1/generations?limit=` (`generation_history`) |
| `model catalog` · `model catalog --group G [--runtime R]` | `GET /v1/catalog` (`catalog_list`) · `GET /v1/catalog/recommendations?group=&runtime=` (`catalog_recommend`) |
| `governance list [--status]` · `show <id>` | `GET /v1/governance?status=` (`governance_list`) · `GET /v1/governance/{id}` (`governance_show`) |
| `governance approve <id> [--reason] [--reload]` | `POST /v1/governance/{id}/approve` `{reason?, reload?}` (`governance_approve`) → applied at once |
| `governance reject <id> --reason R` | `POST /v1/governance/{id}/reject` `{reason}` (`governance_reject`) |
| `project add <path> [--name]` · `list` · `remove <name\|path>` | `POST /v1/projects` `{path, name?}` (`project_add`) · `GET /v1/projects` (`projects_list`) · `DELETE /v1/projects?target=` (`project_remove`) |

### Run endpoints (PR-10) — the async run protocol

| CLI verb | Endpoint (operationId) |
|---|---|
| `run "<task>"` · `fix [--goal]` · `sprint <spec>` · `evolve [--focus]` · `plan "<task>"` | `POST /v1/runs` `{kind, project, task?, goal?, spec?, focus?, simple?, keep_going?, in_place?, max_iterations?, max_parallel?}` → `202` run record (`run_start`). `project` must be **registered** (`project add`); jobs never push |
| `ezai runs` (legacy listing) | `GET /v1/runs?kind=&status=&project=&limit=` (`runs_list`; active count + limits) |
| — | `GET /v1/runs/{id}` (`run_get`; record + journal progress) |
| `explain-run <id>` / `report.json` | `GET /v1/runs/{id}/report` (`run_report`; `409 report_pending` while unfinished) |
| `ezai journal <id>` | `GET /v1/runs/{id}/journal?tail=N` (`run_journal`) |
| Ctrl-C (direct mode) | `POST /v1/runs/{id}/cancel` (`run_cancel`; queued → cancelled now, running → stops at its next model call; `409 run_finished` afterwards) |

Limits: `control.max_concurrent_runs` (2) workers + `control.max_queued_runs`
(8) waiting, then `429 too_many_runs`; one in-place job (`fix`) per project
at a time (`409 project_busy`). The daemon must see the project on its own
filesystem (host `ezaid`, or the projects directory mounted into the
container at the same path).

`reload: true` is refused with `409 reload_unavailable` where the daemon
cannot run compose (the shipped container): apply without reload, then
`make up`, or run the verb with `--reload` from the host CLI. Error codes:
`not_found` 404 · `lifecycle_refused` / `governance_rule` /
`resolution_incomplete` / `idempotency_conflict` / `reload_unavailable` /
`project_busy` / `run_finished` / `report_pending` 409 · `too_many_runs`
429 · `invalid_request` / `registry_invalid` / `render_refused` /
`catalog_refused` 422 · `unauthorized` 401 · `platform_unavailable` 503. The CLI returns exit
`1` for the 4xx refusals and `2` for `platform_unavailable`, printing the same
object in `--json` mode.

## Exit codes

`0` success · `1` run/validation/review/evolution failed, or a platform
operation refused (guard, failed install, rejected proposal) · `2` usage or
workspace error (not a git repo, missing spec/roadmap file, no platform
found) · `3` model unreachable (chat) · `130` interrupted.

## Legacy CLI

`ezai` (Phases 1–4) remains for scripts and run inspection:
`ezai runs` (list runs) and `ezai journal <run-id>` (pretty-print a run's
event journal) work for runs produced by either CLI.

## Key configuration surfaces

- Global: YAML via `--config`, env `AGENTD_*` (`AGENTD_LLM__BASE_URL`, …)
- Global only: `sandbox:` (execution sandbox — [SANDBOX_GUIDE.md](SANDBOX_GUIDE.md)),
  `review:` (reviewer gate — [REVIEW_PROCESS.md](REVIEW_PROCESS.md)),
  `code_intel:` ([CODE_INTELLIGENCE.md](CODE_INTELLIGENCE.md))
- Per repo: `.agentd.yaml` (validation commands incl. `type:`, limits,
  browser_qa) — cannot self-grant push, weaken the sandbox, or disable the
  review gate
- Per repo: `.agent/model_registry.yaml` — per-role primary/fallback models
  (a role it declares is taken exactly as declared)
- Routing layers (PR-6): code defaults are **role aliases** (`role-planner`,
  …) → the platform's Registry v2 seeds concrete primaries + fallback chains
  when a platform is attached (`platform.config_dir` /
  `$AGENTD_PLATFORM__CONFIG_DIR` / inside the checkout) → the per-repo
  registry overrides per role. `local-ezai models` shows the effective
  result and its sources.
- Full reference: [agentd/README.md](../agentd/README.md#configuration)
