# Local-EZAI — Troubleshooting

Symptom → cause → fix, for the platform stack and the Autonomous SWE
runtime. Preventive care: [MAINTENANCE_GUIDE.md](MAINTENANCE_GUIDE.md).

## 1. Platform stack (chat / RAG services)

| Symptom | Likely cause | Fix |
|---|---|---|
| A service is red in `make health` | container crashed / port clash | `make logs-<svc>`; ports auto-relocate on `make up*` — check `.env` overrides |
| Chat replies never arrive | model still loading | `make wait-ready`; on N97 first load takes minutes |
| RAG citations missing | embed-server or Qdrant down; empty collection | `make health`; re-ingest with `make embed` |
| Web search fails | SearXNG rate-limited or down | `make logs-searxng`; restart with `make restart` |
| MCP tools missing in OpenWebUI | mcpo down or wrong `MCP_API_KEY` | verify `:8200/docs` opens; re-add the tool URLs with the key from `.env` |

## 2. CLI won't start / project errors (exit code 2)

| Message | Fix |
|---|---|
| `not a git repository` | `git init` first — every SWE command needs a repo |
| `spec file not found` / `no roadmap` | check the path; `roadmap` needs `.agent/roadmap.md` in the repo (or origin repo when run from a worktree) |
| `local-ezai: command not found` | re-run `make swe-install` (or `pipx install ./agentd`); ensure the venv/bin is on PATH |

## 3. Model plane errors (exit code 3 in chat; failures elsewhere)

| Symptom | Likely cause | Fix |
|---|---|---|
| `model unreachable` | LiteLLM down / wrong base URL | `make health`; set `AGENTD_LLM__BASE_URL=http://localhost:4000/v1` and the LiteLLM key |
| `model not found` errors mid-run | registry routes to a model LiteLLM doesn't serve | run `local-ezai evaluate-models`; align `.agent/model_registry.yaml` with `config/litellm_config.yaml` |
| Runs silently use fallback models | primary failing; chain continued | check journal for `LLM_FALLBACK` events; probe with `evaluate-models`; fix or reorder the registry |
| Structured-output retries exhausted (`no JSON object found`) | model too weak for the role | route the role to a stronger model in the registry; verify with `evaluate-models` (JSON roles are validated) |

## 4. Runs and self-healing

| Symptom | Meaning | What to do |
|---|---|---|
| Run FAILED with `max healing iterations` | 10 debug/fix cycles didn't converge | read `ezai journal <run-id>`; the DebugReports name root causes — fix manually or re-run with a sharper task |
| Run FAILED with `stall detected` | 3 identical failure signatures in a row | the model is looping on one error; check `MEMORY_REPEAT_WARNING` events — a previously failed approach was likely retried |
| `COMMIT_BLOCKED` | validation (incl. Browser QA) is red | this is by design — the gate never commits red. `local-ezai test` to see what's failing, `local-ezai fix` to heal it |
| `review blocked the commit` | the reviewer gate found critical issues | read the findings (`ezai journal <run-id>` / `report.json`); fix and re-run — see [REVIEW_PROCESS.md](REVIEW_PROCESS.md) §5 |
| `command blocked by sandbox allowlist` | `sandbox.command_allowlist` is configured and the command doesn't match | extend the allowlist regexes in the global config, or drop the offending command |
| `sandbox.mode is 'docker' but ...` | strict docker mode without a daemon/image | start Docker and set `sandbox.image`, or use `mode: auto` — [SANDBOX_GUIDE.md](SANDBOX_GUIDE.md) |
| checks fail only in docker mode | the sandbox image lacks the project toolchain | rebuild the image with dev dependencies; `network: none` also blocks anything fetching from the net |
| Sprint task `skipped` | a dependency task failed | journal records the precise reason; fix the failed task, re-run the sprint |
| Sprint `merge conflict` task failure | parallel tasks touched the same files | encode the ordering as `depends_on` in the sprint (or run with `--simple`) |
| Worktree/branch litter after crashes | interrupted runs | `git worktree prune` + delete stale `swe/*` branches; run dirs under `~/.agentd/runs/` are safe to delete |

## 5. Browser QA

| Symptom | Fix |
|---|---|
| `browser missing` / launch error | `make swe-browsers`; in managed envs set `PLAYWRIGHT_BROWSERS_PATH` (harness falls back to `$PLAYWRIGHT_BROWSERS_PATH/chromium`) |
| App never becomes ready | raise `browser_qa.app.ready_timeout`; check `app_log_tail` in the report — the start command runs from the repo root |
| Workflows fail only on console errors | real errors: fix the app. Known noise (e.g. favicon 404): add to `browser_qa.ignore_console_patterns` — default is strict on purpose |
| Port already in use | set `browser_qa.app.port` explicitly; `{port}` is substituted into the start command and base URL |

## 6. Memory

| Symptom | Fix |
|---|---|
| `database is locked` during parallel sprints | transient lock contention; the store retries with a timeout — if persistent, lower `sprint.max_parallel` |
| Agent keeps repeating a failed approach | verify the failure was recorded: `local-ezai memory --search "<error>"`; the repeat guard only matches the same error signature |
| Memory grew stale / wrong rules | records are rows in `<repo>/.agent/memory.db` — delete bad rows (sqlite3) or the whole db to reset learning |

## 7. Evolution / PR creation

| Symptom | Fix |
|---|---|
| `evolve` failed before proposing | needs evidence: run history (`~/.agentd/runs/`), memory, or a roadmap must exist; run some tasks first |
| Improvements implemented but validation red | evolution never delivers red — the branch is left for inspection; journal has the full trail |
| No PR created, only `PR_PROPOSAL.md` | `forge.kind` is `none` (default). Configure `forge: {kind: gh}` (gh CLI) or `kind: api` + `FORGE_TOKEN` for your Gitea/GitHub |
| `push rejected` | pushing is opt-in (`--push`) and `git.allow_push` must be true; check remote auth |

## 8. Tests & development

| Symptom | Fix |
|---|---|
| Suite fails on a fresh clone | `make swe-install` first (editable install); tests are offline — no stack needed |
| Stale-bytecode weirdness while agents edit code | already mitigated: agent subprocesses run with `PYTHONDONTWRITEBYTECODE=1`; for your own shells, delete `__pycache__` |
| Hatchling build error `README.md not found` | build from `agentd/` where its README lives: `python -m build agentd/` |

## 9. Escalation

1. Reproduce with `--verbose` and capture `ezai journal <run-id>`.
2. Check the run's `report.json` — every stage records structured output.
3. Search project memory for prior fixes:
   `local-ezai memory --search "<symptom>"`.
4. File an issue with journal + report attached; or let the platform try:
   `local-ezai fix` (repairs) / `local-ezai evolve --focus "<symptom>"`
   (proposes a systemic improvement, ends at a human-reviewed PR).
