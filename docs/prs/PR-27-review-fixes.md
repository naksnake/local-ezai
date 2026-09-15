# PR-27 — Review hardening before the tag: the fourteen confirmed findings of the pre-release code review, two defects found while fixing them, and the offline SWE demo

**Phase:** post-V1 hardening (before the `v1.0.0` tag) · **ADR:** none new —
ADR-028 gains an as-built note; no architecture, state or contract change ·
**Size:** M (thirteen source files, one script, one Make target, thirty-two
new tests, eight documents touched) · **Plan entry:** outside V1_PR_PLAN — the
review of the merged release train
([PR #5](https://github.com/naksnake/local-ezai/pull/5)) found these ·
**Depends on:** PR-1..26 (merged) · **Status:** implemented on
`claude/friendly-thompson-qhmslv`

## Why

The release train merged with the full suite green, but a code review of the
merge (fifteen candidates, each traced end to end by an independent pass;
one refuted) found fourteen defects none of the 743 tests covered — three of
them in the path the 72 h soak and the sign-off depend on. Every fix below
carries the regression test that was missing. Nothing here changes the
contract (1.2.0), the declarative state, or the chat stack (baseline
unchanged).

## Scope (as delivered)

### Control plane (`agentd/control/`)

- **Model install / benchmark from the shipped `ezaid` container could never
  succeed** (`api.py` → `lifecycle.install`): the image has no docker CLI and
  no models mount, `SideLoad` raised an unclassified `FileNotFoundError`
  after the download, the request ended 500 and the soak's 6-hourly churn
  failed under `make control-up`. Fixed where it belongs — in the lifecycle:
  `require_compose()` turns a missing binary and a failing `docker compose
  version` into one `LifecycleError` with the fix (run the verb direct on the
  host, or `make control-serve`), and `SideLoadValidator.preflight()` is
  asked by `install` **before any download**. The hub fetcher's container
  mode reports a missing docker the same way (`FetchError`). `scripts/soak.sh`
  runs the churn's benchmark direct (it needs the host's docker) and the
  rollback through the daemon; the runbook says so.
- **The idempotency middleware ran before authentication** (`app.py`): a
  stored response replayed to any caller holding the key, and 401 rejections
  were cached under the client's key so a corrected retry replayed the
  rejection. Now a replay authenticates the token and the client policy
  first (an unauthenticated caller goes to the route, which rejects and
  audits), a replay from another client is `idempotency_conflict`, replays
  are audited as `api.replayed`, and 401s are never stored. The stored
  record gains a `client` field (default `""` — records written before it
  read back unchanged).
- **A cancelled run ended `failed`** (`runs.py`, `graph.py`): `RunCancelled`
  was a `RuntimeError` and every graph node's `except Exception` turned it
  into a failed report before the registry could see it. `RunCancelled` now
  derives from `BaseException` (as `KeyboardInterrupt` and asyncio's
  `CancelledError` do, for the same reason); the record, the audit and the
  journal's terminal event all say `cancelled`. Tested through the real
  graph, not a fake pipeline.
- **The mutation lock was held for entire model downloads** (`api.py`):
  install now fetches outside the lock (`lifecycle.prefetch` → `install(fetched=…)`,
  serialized only with other downloads), so an approval is never held behind
  a multi-GB fetch; every mutation waits a bounded 30 s for the one in flight
  and then answers `409 mutation_busy` instead of hanging the CLI (unbounded
  read timeout) or showing the console a false `control_unreachable`.
- **`/v1/audit` parsed the log twice per call; `/v1/health` re-parsed every
  queue YAML** (`audit.py`, `governance.py`): `AuditLog.page()` reads once;
  the queue caches each request file's parse by `(mtime_ns, size)` and hands
  out copies.

### Lifecycle, rendering, first run

- **`install()` over an active or retired model raised a forbidden
  transition** (`lifecycle.py`) instead of the documented "never raises for
  fetch/validation failures": a re-install of a serving model now keeps it
  `active` on success, and a failed re-fetch or re-validation of an active or
  retired entry records the reason and demotes nothing (the registry keeps
  resolving). `InstallResult.ok` = not failed and no reason recorded.
- **A stack started before the first render broke every later render**
  (`render.py`): compose bind-mounts `config/rendered/litellm-config.yaml`,
  docker creates a directory for a missing source, `write_rendered` died on
  `IsADirectoryError`. An empty stray directory is reclaimed; one with content
  is a `RenderError` naming what to do. `local-ezai up` now refuses, with the
  fix, when the rendered LiteLLM config is missing (the Make targets already
  did via `require-rendered`).
- **`local-ezai up`/`down` hardcoded the `gpu` profile** (`platform_cli.py`):
  the default is now the detected class's profile via `suggest_profile`, as
  `setup` and `bundle` already did; `--profile` still wins.
- **`SetupPipeline.step()` caught a narrower tuple than
  `platform_exceptions()`** (`setup_pipeline.py`): registry, render, catalog,
  descriptor and governance errors escaped without a step result and without
  the report; it now uses the shared vocabulary. The unreachable `except
  OSError` around the report step went with it.
- **Found while fixing:** `config/first-run/report.json` always said
  `ok: true` — the exit code was assigned after the file was written; the
  outcome is now decided inside the report step before the write (the
  Platform-ready card reads that file).

### Monitor and tool server

- **The SSO cookie cache was unbounded and every unknown cookie hit
  OpenWebUI** (`monitor/monitor.py`): eviction dropped only expired entries
  (none within the 60 s TTL); a cookie of any shape triggered a fresh client
  and an outbound validation. Now a real cap (expired first, then oldest), a
  JWT-shape pre-check before any network call, a semaphore on concurrent
  validations, one shared client; negative caching kept; the seam the
  journey test injects kept.
- **`swe_plan` blocked the whole tool server for up to 180 s**
  (`mcp-servers/swe-server/swe_server.py`): a sync FastMCP tool sleeps on the
  event loop, and mcpo shares one stdio process across every chat. Tools are
  now registered through an `async` wrapper that runs the unchanged sync
  method in a worker thread (schemas byte-identical, verified against the
  real FastMCP); the HTTP client gains a finite read timeout.

### Build, hygiene, duplication

- **`make bootstrap` / `control-serve` / `control-spec` ran a bare CLI on a
  fresh checkout** (`Makefile`): recipe variables were expanded before the
  venv existed; they now resolve the CLI at run time, as `setup` did.
- **`config/control/` and `config/rendered/` were not git-ignored** although
  the maintenance guide said so; they are, the guide is true. **Found while
  fixing:** the generic `models/` rule also hid `config/models/` (the
  registry and generations ADR-025 calls git-committed); it is anchored to
  `/models/` now — a bootstrapped checkout shows `config/models/` as
  trackable, which is the intent.
- **Duplicated helpers** removed: `setup_pipeline._tail` → `bundle._tail`,
  `platform_cli._manifest_generation` → `activation._manifest_generation`.

### The offline SWE demo (`make swe-demo`)

`scripts/swe-demo.sh` + `examples/swe-demo/responses.json` +
`docs/SWE_DEMO.md`: the whole pipeline — plan → code → validate → review →
commit on a `swe/<run-id>` branch — on a scratch copy of the bundled sample
project, in about a minute, with **no GPU, no model download and no running
stack**. Only the model's answers are scripted (agentd's existing `scripted`
provider); the worktree, the edits, the project's own unittest gate, the
review gate, the commit, the journal, the report and the project memory are
real. The guide walks from the demo to the real first run and the everyday
verbs. An integration test runs the script as shipped.

## Tests (32 new, all offline — 775 in the suite, from 743)

`test_lifecycle.py` (preflight refuses before download; prefetch + reuse;
re-install of active/retired), `test_control_api.py` (replay needs the
token, 401 never cached, other client conflicts, `mutation_busy`),
`test_control_runs.py` (cancel through the real graph ends `cancelled`),
`test_render.py` (stray directory reclaimed / refused),
`test_review_hardening.py` (queue parse cache, audit page, hub fetch without
docker), `test_setup_pipeline.py` (non-`PlatformError` platform errors become
step results; `ok`/`exit_code` in the report file), `test_platform_cli.py`
(profile default from the class; `up` refuses without the rendered config),
`test_monitor_sso.py` (13: cap, shape check, negative cache, gate),
`test_swe_server.py` (every tool async; a waiting plan blocks no other call),
`test_swe_demo.py` (the demo runs green as shipped). The refuted candidate
(Compose ≥ 2.24 for `env_file` long syntax) needed no change: the README
already requires 2.24.4 for the `!override` tag.

## Docs

`RELEASE_NOTES.md` (the hardening in the v1.0.0 entry), `TROUBLESHOOTING.md`
(`mutation_busy`, the side-load refusal, replay rules, the stray directory),
`SOAK_RUNBOOK.md` (churn transport), `MAINTENANCE_GUIDE.md` (ignored dirs),
`CLI_REFERENCE.md` (`up`/`down`), `USER_GUIDE.md` + `README.md` +
`agentd/README.md` (the demo), `.agent/roadmap.md`, `.agent/decisions.md`
(ADR-028 as-built note), this document.

## Verification

`ruff check` clean · the full offline suite green: 775 passed, 0 failed (real
Chromium journeys included) · `scripts/chat-stack-baseline.py --check`
unchanged · `make -n bootstrap` resolves the CLI at run time · the demo run
green end to end in this environment (3 tests on the delivered branch, 40
journal events, `explain-run` attributing every stage).

## Out of scope (recorded, not changed)

The two PR-25 residuals stand (the health table's engine probe path; a
tool-call format for day-2 installs of undeclared sources). `make
control-spec` still has no auto-install line. The `ai-models/` ignore rule is
untouched. The soak, the sign-off and the tag remain human acts.
