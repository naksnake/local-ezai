# PR-22 — Setup pipeline + smoke: the first run, steps 4–8, the card, `init` (phase P5)

**Phase:** P5 · **ADR:** ADR-031 (Proposed; PR-22 slice recorded) · **Size:** M
(delivered at the upper edge: one module, one test file, wiring) ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-22 ·
**Depends on:** PR-21 (`install.sh`, class assertion, `EnvText`), PR-7
(bootstrap core), PR-3 (descriptor `ready` verb, rendered engine override),
PR-4 (catalog recommender), PR-8 (health probe table), PR-14 (persona
installer), PR-15 (chat-stack baseline) · **Status:** implemented on
`claude/next-ready-pr-bnq7r3` as the twentieth stacked commit (PR-3..21
unmerged; each commit reverts independently)

## Scope (as delivered)

Steps 4–8 of [FINAL_FIRST_RUN_EXPERIENCE.md](../FINAL_FIRST_RUN_EXPERIENCE.md)
§3, the "Platform ready" card of §4, and the `local-ezai init` fallback of
[FIRST_RUN_EXPERIENCE.md](../FIRST_RUN_EXPERIENCE.md) §3.

- **`local-ezai setup`** (host-only platform verb; `agentd/src/agentd/setup_pipeline.py`).
  - **4 fetch.** The PR-7 bootstrap when no registry exists (models fetched,
    validated, benchmarked; generation 1 rendered — step 5), through a new
    shared `platform_cli.run_bootstrap` the CLI verb also uses; then
    `docker compose pull` for the image services and `build` for the
    platform's own, both with the rendered engine override; then the RAG
    embedding model through `scripts/download-embed.sh` (skipped when
    present; a failure is a warning — RAG stays off until it succeeds).
  - **6 up.** `docker compose up -d` for the profile (`--profile`, else the
    detected or asserted class through the preset table) + the rendered
    override; wait-ready polls the active runtime descriptor's `ready` verb
    (path, timeout) on the host port, then the daemon's health table
    (`control/health.DEFAULT_TARGETS`) remapped to host ports until every
    service answers or the budget ends. Engine or router down fails the run
    with the compose-logs hint; any other service down is a warning.
  - **7 verify.** One **chat turn** on the chat role alias through the router
    — required; then, advisory: a **RAG** answer over a sample document the
    pipeline writes and embeds with the stack's own embed script
    (`scripts/embed-documents.sh`, the one implementation `make embed` now
    uses too); **`plan_only`** on the bundled sample project (copied,
    git-initialised, registered as `sample-project`); the **evaluate-models**
    probes of every role. Each check reports its detail.
  - **8 report.** `config/first-run/report.{json,md}`; the ✔ block with the
    WebUI and Admin Center URLs (`LAN_HOST` when set, relocated ports) and
    the Orchestrator deep link; the "Platform ready" **card as an OpenWebUI
    banner** — OpenWebUI's own `WEBUI_BANNERS` setting through an optional
    compose `env_file` (`config/first-run/openwebui.env`) the pipeline
    writes, then recreates and probes OpenWebUI, and removes again (second
    recreate) if it does not come back; the **Orchestrator persona**
    installed through the PR-14 script, or deferred to "after your first
    login: make orchestrator" when no account exists.
  - **Idempotent.** Registry present → bootstrap skipped; images and up are
    compose no-ops; the embedding download skips when present; the sample
    project is copied once and "already registered" is tolerated; the
    banner is rewritten. `--skip-images`, `--skip-smoke`, `--skip-banner`.
  - **"Ready"** = engine and router healthy, a registry, and the chat turn
    answered (or `--skip-smoke`). Exit 0 ready · 1 failed.
- **`local-ezai init`** — the wizard as fallback: hardware check → the
  **recommended set** per group (the catalog recommender's first eligible
  entry for this class and runtime, with placement, speed band, size) →
  Enter / `--yes` accepts, or a catalog id / `hf:` / `gguf:` overrides per
  group → seeds written to `.env` as catalog ids (auditable in the
  generation-1 diff) → the pipeline. Seeds already present are respected;
  invalid explicit seeds are reported with their fixes; a bootstrapped
  platform goes straight to setup; no terminal → exit 3 unless `--yes`.
- **`examples/sample-project`** — a dependency-free HTTP service with a
  deliberately missing `/health` route, a standard-library test suite and a
  `.agentd.yaml` validation command; the working copy lives at
  `<checkout>/sample-project` (ignored).
- **Makefile.** `make setup` = `install.sh` + `local-ezai setup` (the
  five-step contract; a fresh `.env` still stops once for the seeds);
  `make setup-system` = the system-package script (was `make setup`);
  `setup-gpu|cpu|n97` = `install.sh --profile X` + `local-ezai setup
  --profile X`; `up-run` / `up-cpu-run` / `up-n97-run` fetch only the
  embedding model when missing (`download-embed.sh`) — the engine's models
  come from the registry; `download-embed` target; `embed` calls the shared
  script; `download-gpu` stays as the legacy path; the CLI is resolved when
  the recipe runs (the venv may not exist when make parses).
- **Compose / baseline.** The openwebui service gains an optional `env_file`
  (`required: false`); the chat-stack baseline treats it as additive, so the
  fixture is unchanged. `platform_cli.build_context` reads an asserted class
  from the platform's `.env` when the environment does not carry it.
- **Excluded by design:** the offline bundle and the F1–F11 acceptance
  suite (PR-23); Browser QA of onboarding (PR-23); an Admin Center card (no
  contract operation carries the report — parity or absence); any model
  choice by the pipeline beyond `init`'s proposal, which the human confirms;
  a banner in `.env` (see decision 3); changes to the contract, the daemon,
  or the chat stack's behavior.

## Design decisions made in this PR

1. **The pipeline is a module with seams, the shell is thin.** Docker, HTTP,
   the planner, the evaluator, sleep and the clock are injected, so every
   step — including failures, idempotence and the banner rollback — runs
   offline in the suite; `make setup` is two lines.
2. **"Ready" is what a first message needs.** Engine, router and one chat
   turn are required; the RAG, plan and probe checks depend on the model the
   user chose and are reported, not enforced — a working stack is never
   declared broken by an advisory check, and never declared ready without
   the required one.
3. **The banner goes through compose, not `.env`.** `make` includes and
   exports `.env` verbatim, so a quoted JSON there would reach compose as a
   quoted string and crash OpenWebUI at start; a JSON without quotes would
   break `source .env` in the shell scripts. The optional `env_file` compose
   already supports (`required: false`) avoids both; OpenWebUI is probed
   after the recreate and the file removed if it does not come back, so a
   banner can never take chat down.
4. **Reuse over rewrite.** The bootstrap (PR-7), the health table (PR-8),
   the persona installer (PR-14), the recommender (PR-4), `compose_files`
   (PR-6), `EnvText` and `suggest_profile` (PR-21) and the existing embed
   script are the pipeline's parts; the two new shell scripts replace
   duplicated `docker run` blocks in the Makefile.
5. **`make setup` changes meaning, nothing disappears.** The FRE contract
   names `make setup` as step 3; the system-package script it used to run is
   `make setup-system`, `install.sh` names it when Docker is missing.
6. **Engine models are the registry's.** `make up*` no longer downloads the
   legacy `CHAT_MODEL` before starting (with V1 seeds that path failed on a
   non-repository value); the embedding model is the only pre-start fetch.
7. **`init` writes catalog ids, not `auto`.** The choice the human confirmed
   is what the generation-1 diff shows (F10); `auto` remains available by
   hand.

## Tests (17 new; suite 667 → 684)

`tests/unit/test_setup_pipeline.py` — a checkout-like platform (shipped
descriptors, catalog, sample project, seeds in `.env`), the bootstrap's
seams faked as in the PR-7 CLI tests, Docker a recording fake (git real),
HTTP scripted, planner/evaluator stubs, a fake clock:

- **The first run end to end:** bootstrap → generation 1 + rendered
  override; `pull <image services>`, `build`, `up -d`, `up -d openwebui`
  through the base file + the rendered override; the embedding script; the
  engine polled through the descriptor's ready path then every service; the
  four smoke checks (router URL, chat alias, bearer key); the sample
  document with the codeword; the sample project copied, a repository,
  registered; URLs on `LAN_HOST` and the relocated port; `report.json` /
  `report.md`; the banner JSON (single-quoted in the env_file, the six
  fields, no quote character in the content); the persona line; the
  progress lines.
- **A second run is idempotent:** bootstrap skipped, generation unchanged,
  no re-init of the sample project, no duplicate registration, the persona
  deferred when the script reports no account.
- **Failures stop at the right step, always with the fix:** seed problems →
  no docker command, the F8 text kept in full, the report still written;
  pull failure → no `up`; skip flags → skipped statuses and no compose
  traffic beyond `up -d`; engine never ready → the compose-logs hint,
  health `{engine: False}`, persona skipped; router down → failed; searxng
  down → warning, still ready.
- **Smoke semantics:** a failing chat turn fails the run; a wrong RAG
  answer, a raising planner and failing probes are warnings, ready stays
  true and the banner says "Smoke 1/4".
- **Banner rollback:** OpenWebUI unhealthy after the recreate → env_file
  removed, a second recreate, the card kept in `report.md`, exit 0.
- **Host targets** mirror the daemon's health table on relocated ports,
  patterns and credentials preserved.
- **`init`:** the recommended set written as catalog ids with `AI_RUNTIME`;
  interactive accept/override per group; a bad reference refused; no
  terminal → exit 3 and `.env` untouched; valid seeds skip the wizard;
  invalid explicit seeds → exit 1 with the problems; missing `.env` → usage
  error; a bootstrapped platform → straight to setup.
- **CLI verbs** through `main`: flags parsed, `--json`, the card, `init`
  exit codes, both verbs host-only.
- **Wiring tripwires:** Makefile recipes (setup, setup-system, the three
  profile targets, the three `up*` recipes, `download-embed`, `embed`),
  executable scripts, the compose `env_file`, `.gitignore`, the baseline
  unchanged with `env_file` normalised, the persona model id shared with
  the installer script, the sample project passing its own validation
  command and lacking the route the first run plans.
- Updated: `test_installer.py` (the class-assertion test now expects the
  CLI to honor `.env` in a plain shell, the environment to win, and
  detection only when no assertion exists); `test_chat_stack_regression.py`
  (the additive `env_file`). Every other pre-existing test passes
  unmodified; goldens and the baseline fixture unchanged.

## Behavior notes

- **`make setup` now runs the first run** (`install.sh` + `local-ezai
  setup`) instead of the system-package script, which is `make setup-system`.
  `make setup-gpu|cpu|n97` run the same two halves with a profile assertion
  instead of `pull · build · download · bootstrap · up · wait-ready` — a
  superset (the smoke and the report are new), same profile files.
- **`make up` / `up-cpu` / `up-n97`** no longer download the legacy chat
  model when its cache directory is missing; they fetch the embedding model
  only. `make download-gpu` (the legacy download) still exists.
- **`make embed`** runs `scripts/embed-documents.sh` (same container, same
  arguments).
- **`docker-compose.yml`:** the openwebui service has an optional
  `env_file` (`config/first-run/openwebui.env`, `required: false`). Absent
  file = today's behavior. The chat-stack baseline fixture is byte-identical
  (the key is normalised as additive).
- **`platform_cli.build_context`** also reads `EZAI_CAPABILITY_CLASS` from
  the platform's `.env` when the variable is not in the environment.
- New CLI verbs `setup` and `init` (host-only, never sent to a daemon); new
  files under `config/first-run/`, a `sample-project/` working copy and a
  `documents/local-ezai-first-run.md` sample document are created by the
  first run (all ignored by git).
- No contract, daemon, monitor or chat-behavior change.

## Verification boundary

Docker, the router, OpenWebUI, the planner and the evaluator were fakes;
the real `docker compose` invocations, the OpenWebUI banner rendering and
the model-dependent smoke answers are exercised on a stack, not in the
suite. The bootstrap ran for real against fake fetch/validate/benchmark
seams (as in PR-7). Git ran for real for the sample project. The
`register-orchestrator.sh` and `download-embed.sh` scripts were driven by
the fake runner; their shell syntax was checked.

## Rollback note

`git revert <commit>` removes the module, the two verbs, the sample project
source, the two scripts and the Makefile, compose, baseline and `.gitignore`
changes; `make setup` runs the system-package script again. Files a first run
created (`config/first-run/`, `sample-project/`, the sample document, the
registered project entry, generation 1) are inert or ordinary platform
state the previous code reads unchanged; an OpenWebUI recreated without the
env_file drops the banner. No data migration.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane —
  unavailable here; to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry (steps 4–8, the card, `init`); deviations
      listed and justified above: the banner through a compose env_file
      rather than `.env`, `make setup-system` for the old `make setup`,
      `make up*` fetching only the embedding model, the `.env` class read in
      `build_context`
- [x] New tests cover the scope incl. idempotence and the banner rollback;
      full suite green (684); ruff clean
- [x] Pre-existing tests unmodified except the two unmerged tests listed
      under Tests; goldens passing; chat-stack baseline fixture unchanged
- [x] Architecture docs updated to as-built (FINAL_FIRST_RUN_EXPERIENCE §3
      + §4, FIRST_RUN_EXPERIENCE §3 + §4, OPENWEBUI_INTEGRATION §3,
      CLI_REFERENCE, OPERATION_MANUAL, README); ADR-031 PR-22 slice recorded
- [x] Rollback note present
- [x] No model/vendor/runtime names in code: compose service names and
      health-table ids only; the recommended set comes from the catalog
