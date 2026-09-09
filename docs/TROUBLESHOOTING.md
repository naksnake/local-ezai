# Local-EZAI — Troubleshooting

Symptom → cause → fix, for the platform stack, the first run, the control
plane, the Admin Center, model governance and the Autonomous SWE runtime.
Preventive care: [MAINTENANCE_GUIDE.md](MAINTENANCE_GUIDE.md).

## 1. Platform stack (chat / RAG services)

| Symptom | Likely cause | Fix |
|---|---|---|
| A service is red in `make health` | container crashed / port clash | `make logs-<svc>`; ports auto-relocate on `make up*` — check `.env` overrides |
| Chat replies never arrive | model still loading | `make wait-ready`; on a low-power host the first load takes minutes |
| RAG citations missing | embed-server or Qdrant down; empty collection | `make health`; re-ingest with `make embed` |
| Web search fails | SearXNG rate-limited or down | `make logs-searxng`; restart with `make restart` |
| MCP tools missing in OpenWebUI | mcpo down or wrong `MCP_API_KEY` | verify `:8200/docs` opens; re-add the tool URLs with the key from `.env` |

## 2. First run (`./install.sh`, `make setup`, `local-ezai init`)

| Symptom | Meaning | Fix |
|---|---|---|
| `install.sh` exits **3** ("review stop") | no terminal to open `.env` in an editor | edit `.env` as the message says (`AI_RUNTIME` + the three seeds), re-run; `--yes` accepts a complete file without the stop |
| exits **1** with a list of problems | the model seeds are invalid — checked before any download (F8) | every line names its fix: unknown runtime, a format `AI_RUNTIME` does not serve, no tool-call format for a tool-calling group (`<SEED>_TOOL_FORMAT=…` or a catalog id), `auto` with nothing that fits this hardware |
| exits **2** | preflight: Docker with compose, or python3 ≥ 3.10, missing | `make setup-system` (system packages), then again |
| `a model registry already exists` / `.env seeds were already consumed` | the platform is bootstrapped; seeds are read once | day-2 changes: `local-ezai model …` or the Admin Center; `bootstrap --force` files a governed re-bootstrap request |
| `the engine did not answer … within …` | the model is still loading, or the runtime image does not match the accelerator kind | `docker compose logs vllm \| tail -30`; check `AI_RUNTIME` against `local-ezai status` (class, accelerator); re-run `make setup` — every step is idempotent |
| "✗ Platform not ready" — the chat turn failed | the router cannot reach the engine, or the chat role has no servable model | `make health`, `local-ezai status`, `local-ezai model explain chat`; ready means one chat turn answered |
| RAG / plan / models smoke are **warnings** | advisory checks that depend on the model you chose | proceed; `config/first-run/report.md` records what failed and why |
| offline: `these images are not on this host` | the bundle was not consumed, or is incomplete | `./install.sh --offline <dir>` again; or remove `EZAI_OFFLINE` from `.env` to go online |
| `checksum mismatch` while consuming a bundle | a weights file is corrupt or truncated | re-copy the bundle directory; nothing was placed |
| no "Platform ready" card in the WebUI | the banner env file was rolled back (OpenWebUI did not come back) or an admin edited the banner list | the same card is `config/first-run/report.md` and the Admin Center Overview; `local-ezai setup` rewrites it |
| a legacy `.env` (`CHAT_MODEL=Org/Repo`, `CPU_*`, `N97_*`) | migrated into generation 1 automatically, served name kept | nothing to do; `local-ezai model history` shows `migrated_from` |

## 3. Control plane (`ezaid`) and connected mode

| Symptom | Likely cause | Fix |
|---|---|---|
| `control plane unreachable at …` (exit 2) | `--transport connected` (or `EZAI_TRANSPORT=connected`) but no daemon | `make control-up` (overlay) or `make control-serve` (host) — or drop the flag: auto mode falls back to in-process |
| `control plane reachable … but no service token is configured` (exit 2) | the daemon answers but `EZAI_CONTROL_TOKEN` is not in your shell | export it (the value in `.env`) or `--transport direct` |
| `invalid service token` (401) | token mismatch | same `EZAI_CONTROL_TOKEN` as the daemon's; rejected calls are audited |
| `reload_unavailable` (409) | the daemon runs in the container (no docker CLI) | apply without `--reload`, then `make up`; or run the verb with `--reload` from the host CLI |
| `no registered project named or located at …` (404) when starting a run from chat or the API | the project is not on the allowlist, or the daemon cannot see its path | `local-ezai project add <path>`; run `ezaid` on the host (`make control-serve`) or mount the projects directory into the container at the same path |
| `too_many_runs` (429) / `project_busy` (409) | concurrency limits; one in-place job per project | wait, cancel one (`POST /v1/runs/{id}/cancel`, Admin Center Runs), or raise `control.max_concurrent_runs` |
| a run ended `failed: control plane restarted while the run was …` | the daemon restarted mid-run | resubmit; the journal on disk shows how far it got |
| `idempotency_conflict` (409) | the same `Idempotency-Key` reused for a different request | a new key per operation; reuse only to retry the same one |
| audit actor reads `nita via cli` / `via admin-center` / `via swe-server` | the forwarded identity: the human and the surface | normal — one audit log for every surface |

## 4. Admin Center (`http://<host>:8888`)

| Symptom | Likely cause | Fix |
|---|---|---|
| pages say the control plane is down | daemon not running, or the URL is wrong for the deployment shape | `EZAI_CONTROL_URL_MONITOR`: `http://ezaid:8010` (overlay) or `http://host.docker.internal:8010` (`make control-serve`); health and knowledge keep working meanwhile |
| a Basic-auth prompt although you are logged into OpenWebUI | the cookie path needs the WebUI and the monitor under the **same host name**, or the `MONITOR_SSO_*` keys are unset | open both under one host name and set `MONITOR_SSO_OPENWEBUI_URL`; otherwise use the monitor login (`admin` / `viewer`) |
| buttons missing, only read views | you are a viewer | log in as `admin`, be an OpenWebUI admin, or be listed in `MONITOR_SSO_ADMINS` |
| pages look stale after an update | the monitor image was not rebuilt | `docker compose build monitor && docker compose up -d monitor` |
| a proxy identity header is ignored | `X-EZAI-Proxy-Secret` missing or ≠ `MONITOR_SSO_TRUSTED_SECRET` | configure the proxy to send both; the header alone is never trusted |

## 5. Governance and the model lifecycle

| Symptom | Meaning | Fix |
|---|---|---|
| `decisions are made once, on pending requests` | the request was already approved or rejected | `governance show <id>`; propose again if needed |
| `a rejection needs a reason` | — | `--reason "…"` (recorded; evolution proposals remember it) |
| `'x' is the primary of role(s) … — retiring it changes what serves users` | a serving primary cannot be retired | activate a replacement first (approval-gated), then retire |
| `refusing to render into config/rendered` (drift) | a rendered file was edited by hand | rendered files are outputs: revert the edit and change state through `local-ezai model …`, or pass `--force` deliberately |
| activation refused: `missing capability tool_calling` / `min_context` / `json_output` | negotiation: (model × runtime) does not meet the role's contract on this class | declare the model's `tool_call_format` / context (catalog entry, or `<SEED>_TOOL_FORMAT` at bootstrap), pick a runtime that parses it, or activate into a group whose roles need less |
| `… is gguf but AI_RUNTIME=… serves hf` | format × runtime mismatch | install the served variant of the model, or set the runtime that serves the format |
| `serves one model per engine slot but … distinct models` | the selected runtime hosts one model | seed the three groups with one model, or a runtime with `parallel_models` |
| `unknown accelerator kind 'rtx'` / `tuning.ctx_size is required` / `has no image for accelerator kind …` | a runtime descriptor under `config/providers/` is invalid or lacks your accelerator | fix the YAML (kinds are `cuda`, `rocm`, `igpu`, `none`; [RUNTIME_ABSTRACTION_STRATEGY.md](RUNTIME_ABSTRACTION_STRATEGY.md) §2); descriptors are data, no code changes |
| a runtime you added is not offered | the descriptor file name must equal its `runtime:` id | rename; `install.sh --runtime <id>`; `agentd/tests/fixtures/providers/mockengine.yaml` is a complete template |
| `model rollback` did what? | the previous (or `--to-generation N`) content became a **new** generation | `local-ezai model history`; nothing is deleted |

## 6. CLI won't start / project errors (exit code 2)

| Message | Fix |
|---|---|
| `not a git repository` | `git init` first — every SWE command needs a repo |
| `spec file not found` / `no roadmap` | check the path; `roadmap` needs `.agent/roadmap.md` in the repo (or origin repo when run from a worktree) |
| `local-ezai: command not found` | re-run `make swe-install` (or `pipx install ./agentd`); ensure the venv/bin is on PATH |
| `no Local-EZAI platform found for this directory` | a platform verb outside the checkout | run inside the checkout, set `platform.config_dir`, or export `AGENTD_PLATFORM__CONFIG_DIR` |

## 7. Model plane errors (exit code 3 in chat; failures elsewhere)

| Symptom | Likely cause | Fix |
|---|---|---|
| `model unreachable` | LiteLLM down / wrong base URL | `make health`; set `AGENTD_LLM__BASE_URL=http://localhost:4000/v1` and the LiteLLM key |
| `model not found` errors mid-run | routing points at a model LiteLLM doesn't serve | `local-ezai models` (effective routing and its sources); `local-ezai status` (generation vs rendered); `local-ezai evaluate-models` |
| Runs silently use fallback models | primary failing; chain continued | check journal for `LLM_FALLBACK` events; probe with `evaluate-models`; fix or reorder the registry |
| Structured-output retries exhausted (`no JSON object found`) | model too weak for the role | route the role to a stronger model (activate into its group); verify with `evaluate-models` (JSON roles are validated) |

## 8. Runs and self-healing

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

## 9. Browser QA

| Symptom | Fix |
|---|---|
| `browser missing` / launch error | `make swe-browsers`; in managed envs set `PLAYWRIGHT_BROWSERS_PATH` (harness falls back to `$PLAYWRIGHT_BROWSERS_PATH/chromium`) |
| App never becomes ready | raise `browser_qa.app.ready_timeout`; check `app_log_tail` in the report — the start command runs from the repo root |
| Workflows fail only on console errors | real errors: fix the app. Known noise (e.g. favicon 404): add to `browser_qa.ignore_console_patterns` — default is strict on purpose |
| Port already in use | set `browser_qa.app.port` explicitly; `{port}` is substituted into the start command and base URL |

## 10. Memory

| Symptom | Fix |
|---|---|
| `database is locked` during parallel sprints | transient lock contention; the store retries with a timeout — if persistent, lower `sprint.max_parallel` |
| Agent keeps repeating a failed approach | verify the failure was recorded: `local-ezai memory --search "<error>"`; the repeat guard only matches the same error signature |
| Memory grew stale / wrong rules | records are rows in `<repo>/.agent/memory.db` — delete bad rows (sqlite3) or the whole db to reset learning; the Admin Center's Memory page browses the same store |

## 11. Evolution / PR creation

| Symptom | Fix |
|---|---|
| `evolve` failed before proposing | needs evidence: run history (`~/.agentd/runs/`), memory, or a roadmap must exist; run some tasks first |
| Improvements implemented but validation red | evolution never delivers red — the branch is left for inspection; journal has the full trail |
| No PR created, only `PR_PROPOSAL.md` | `forge.kind` is `none` (default). Configure `forge: {kind: gh}` (gh CLI) or `kind: api` + `FORGE_TOKEN` for your Gitea/GitHub |
| `push rejected` | pushing is opt-in (`--push`) and `git.allow_push` must be true; check remote auth |
| `evolution already has an open proposal` | one routing proposal at a time in the queue | decide it (`governance approve\|reject`, or the Governance page) first |

## 12. Tests, gates & development

| Symptom | Fix |
|---|---|
| Suite fails on a fresh clone | `make swe-install` first (editable install); tests are offline — no stack needed |
| `make release-gate` red | run the failing gate alone (`swe-lint`, the baseline `--check`, `swe-drill`, `swe-accept`, `swe-parity`, `swe-gates`, `swe-test`) and read its message — each names the fix |
| the H1 word audit fails | a vendor/brand/SKU string outside descriptor data | express it as descriptor data or a capability class/kind; a legitimate exception is an `ALLOWED` entry **with a reason** in `agentd/tests/gates/test_h1_word_audit.py` |
| the parity harness reports a difference | a CLI verb and its API operation diverged | both must call the one operation function (`platform_cli`); transport annotations are normalised, results are not |
| the third-runtime drill fails | code learned a runtime by name, or assumed a shipped descriptor value | move the knowledge into the descriptor contract; the drill's fixture is the template |
| the chat-stack baseline `--check` differs | the frozen compose surface changed | revert, or after a deliberate chat-stack change: `python3 scripts/chat-stack-baseline.py --update` with the diff reviewed |
| Stale-bytecode weirdness while agents edit code | already mitigated: agent subprocesses run with `PYTHONDONTWRITEBYTECODE=1`; for your own shells, delete `__pycache__` |
| Hatchling build error `README.md not found` | build from `agentd/` where its README lives: `python -m build agentd/` |

## 13. Escalation

1. Reproduce with `--verbose` and capture `ezai journal <run-id>` (or `GET /v1/runs/{id}/journal`).
2. Check the run's `report.json` — every stage records structured output;
   for platform operations, `config/governance/log.jsonl` and `local-ezai
   model history`.
3. Search project memory for prior fixes:
   `local-ezai memory --search "<symptom>"`.
4. File an issue with journal + report attached; or let the platform try:
   `local-ezai fix` (repairs) / `local-ezai evolve --focus "<symptom>"`
   (proposes a systemic improvement, ends at a human-reviewed PR).
