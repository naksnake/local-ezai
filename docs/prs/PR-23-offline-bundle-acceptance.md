# PR-23 — Offline bundle + FRE acceptance suite + the card in the console (phase P5 close)

**Phase:** P5 (closed by this PR) · **ADR:** ADR-031 (→ **Accepted**; PR-23
slice recorded) · **Size:** M (delivered at the upper edge: one module, one
acceptance package, one additive contract operation, wiring) ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-23 ·
**Depends on:** PR-22 (setup pipeline, harness, report), PR-21 (`install.sh`,
`EnvText`), PR-7 (bootstrap core), PR-4 (fetchers, catalog recommender), PR-3
(runtime descriptors, weights mounts), PR-8/PR-12 (contract, inventory),
PR-16/PR-20 (Admin Center Overview, Browser-QA journey suite) · **Status:**
implemented on `claude/next-ready-pr-bnq7r3` as the twenty-first stacked
commit (PR-3..22 unmerged; each commit reverts independently)

## Scope (as delivered)

The three items PR-22 deferred — the offline bundle (F6), the scripted
F1–F11 acceptance suite, onboarding under Browser QA — plus the one contract
operation the Admin Center card was waiting for.

- **Offline bundle** (`agentd/src/agentd/bundle.py`; host-only platform verbs).
  - **`local-ezai bundle create <dir> [--profile X]`** on a connected,
    bootstrapped host (a registry is required; the fix is named otherwise):
    every image of the profile's compose files (`docker compose … config
    --images` → `docker save -o images.tar`), the registry's GGUF artifacts
    (copied with sha256), the hub cache the engine and the embedding server
    mount (`models--*` directories), and `bundle.json` — version, class,
    accelerator, runtime, generation, the seeds (GGUF seeds carry a
    `{weights}` placeholder), the images, the checksums. The bundle is a
    directory; transport is `tar`/`rsync`/a USB stick.
  - **`local-ezai bundle consume <dir> [--env PATH]`** and, the normal path,
    **`install.sh --offline <dir>`**: after `.env` is written, images are
    loaded (`docker load`), GGUF weights placed where the runtime
    descriptors mount them (checksum-verified; present and matching files
    skipped; a mismatch fails loudly), the hub cache copied, `AI_RUNTIME`
    and the three seeds written to `.env` pointing at the local files, and
    `EZAI_OFFLINE=1` stamped. The review-edit stop is skipped: the bundle's
    seeds are the operator's decision. `--check` reports "would consume".
    Docker is required when `--offline` is given (`install.sh` fails with
    the fix, not later in the pipeline).
  - **Offline mode** (`EZAI_OFFLINE=1` in `.env`, or `local-ezai setup
    --offline`): the pipeline verifies every compose image is present
    (`docker image inspect`) instead of pulling or building — missing images
    are listed with the fix; the bootstrap's fetchers are the **offline
    fetchers** (`lifecycle.offline_fetcher`): present, verified weights are
    reused, any network fetch is refused with the URL and the fix named. The
    header says "offline (no egress)"; the bootstrap line "(offline: nothing
    downloaded)". Same wizard, same steps, no egress (F6).
  - **Makefile:** `make bundle BUNDLE=<dir>` and `make setup-offline
    BUNDLE=<dir>` (= `install.sh --offline` + `local-ezai setup --offline`),
    `BUNDLE ?= ./local-ezai-bundle`.
- **The acceptance suite** (`agentd/tests/acceptance/`, `make swe-accept`):
  one offline test per criterion over the PR-22 harness — F1–F6 of
  [FIRST_RUN_EXPERIENCE.md](../FIRST_RUN_EXPERIENCE.md) §6, F7–F11 of
  [FINAL_FIRST_RUN_EXPERIENCE.md](../FINAL_FIRST_RUN_EXPERIENCE.md) §6 (see
  Tests). F1's thirty minutes stays a host measurement (the P6 soak runbook);
  the suite times the platform's own work with the downloads excluded.
- **A defect the suite found, fixed here** (`bootstrap.py`): the PR-7
  bootstrap deduplicated seeds by their reference text, so three `auto`
  seeds collapsed into **one model for every group**. `auto` is now resolved
  per group (`auto:<group>` keys; `_recommended_for` asks the catalog
  recommender for that group), a recommendation two groups share is
  installed once and appended to both groups, and the dry-run names
  `auto:<group>` (F9).
- **Contract 1.2.0** (additive): `GET /v1/first-run` · `first_run_report`
  (read-only) returns `{recorded, path, report}` from
  `config/first-run/report.json` written by `local-ezai setup`
  (`platform_cli.first_run_report`); `recorded: false` with no report. The
  spec artifact `docs/api/ezaid-openapi.json` is regenerated; inventory
  29 + 2 + 1 = 32 operations; `x-contract-status` names 1.2.0.
- **Admin Center Overview: the "Platform ready" card** (`#first-run`) while
  a report exists — the three groups and the model each got, runtime and
  class, the smoke tally, **Start chatting · Try the Orchestrator · Models &
  routing**. An older daemon (no operation) or no report → no card, nothing
  else changes (`overview()` tolerates the ControlPlane error).
- **Onboarding under Browser QA:** the journey suite gains a sixth workflow,
  **`j0-first-run-card`** (Overview → the card's text, the Orchestrator deep
  link and the `/models` link visible, screenshot); the launcher fixture
  writes a report from a `SetupReport` (`write_first_run_report`) so the
  card exists in the driven console. Six workflows, eight screenshots.
- **Excluded by design:** a bundle *format* beyond a directory (a signed or
  compressed archive is transport, not the platform's concern); bundling the
  chat stack's own build context (the images are saved already built); any
  model choice by the platform (the bundle carries the seeds of the host it
  was created on); Admin Center mutations of the report; changes to the
  bootstrap beyond the `auto` fix; P6 work (parity matrix, agnosticism
  proof, soak, release).

## Design decisions made in this PR

1. **The bundle is a directory, not an archive format.** `docker save`
   already produces one tarball; weights are large and checksummed per file
   so a partial copy is detected by the consume step, not by a second
   archive layer. Operators move directories with the tool they trust.
2. **Consume writes the seeds; setup keeps every other rule.** The bundle
   carries the seeds of the host it was made on, so `install.sh --offline`
   writes them (as `gguf:<local path>` / `hf:<repo>`) and skips the review
   stop; validation (F8), the bootstrap, the generation-1 diff (F10) and the
   smoke run exactly as online. Offline is a *fetch policy*, not a second
   pipeline: one flag on the fetchers and one branch in the images step.
3. **Offline fails loudly, never silently degrades.** A missing image or a
   weight not on disk is a stop with the fix (`install.sh --offline <dir>`,
   or remove `EZAI_OFFLINE`), not a pull attempt that hangs on an air-gapped
   host.
4. **The card waited for the contract, and the contract stays additive.**
   PR-22 refused a console card without an operation carrying the report
   (parity or absence). 1.2.0 adds one read-only operation, the CLI reads
   the same file, and the console degrades to "no card" on a 1.1.0 daemon.
5. **The suite fixes what it finds, in scope.** F9 exposed the `auto`
   dedupe; the fix is confined to the bootstrap's seed loop and reuses the
   recommender the wizard already uses. Nothing else in PR-7 changes.
6. **The acceptance tests are a package, not a mark.** `tests/acceptance/`
   runs with the whole suite and alone (`make swe-accept`), reuses the PR-22
   harness by import (no fixture duplication), and stays offline like every
   other test.

## Tests (20 new test functions, 25 collected; suite 684 → 709)

`tests/acceptance/test_fre_acceptance.py` — the PR-22 harness (checkout-like
platform, seams faked, Docker a recording fake, HTTP scripted, git real):

- **F1** a fresh accelerator host: setup to smoke green on the `gpu`
  profile, every step ok, exactly three network-shaped calls (pull, build,
  the embedding download), the platform's own work timed; **F2** exactly one
  file is hand-edited, once: after the install only `.env` differs in the
  whole checkout (every file hashed before and after), and no further prompt
  appears; **F3** the low-power class's recommended set is no larger per
  group and smaller in total than the accelerator host's, and `init` writes
  it and reaches ready through the same wizard; **F4** (×6: fetch, pull,
  build, up, engine, smoke) an abort at any step leaves no generation or
  exactly one — rendered and stamped — the re-run completes, and generation
  1 is never rendered twice; **F5** the re-run detects the install, repairs
  `.env` (one backup, seeds and secrets unchanged, seeds not re-validated),
  the registry is byte-identical, one generation, the pipeline skips the
  bootstrap, the repair is idempotent; **F6** a bundle created on the
  connected fake host, consumed on an air-gapped one (`BundleDocker
  (network=False)` refuses pull/build; the pretend fetcher refuses every
  URL), same steps, no egress;
  **F7** a fully specified `.env` opens no editor and asks nothing; **F8**
  invalid seeds print the fixes and no fetcher, Docker or router call
  happens; **F9** `auto` per group resolves to the recommender's eligible
  entries with their verdicts and generation 1 carries exactly those;
  **F10** the bootstrap request's evidence and diff equal the seeds;
  **F11** a legacy `.env` migrates with the served name kept, no prompt.
- **Wiring:** the Makefile targets (`bundle`, `setup-offline`, `swe-accept`),
  `install.sh --offline`, the suite location.

`tests/unit/test_bundle.py` — create requires a bootstrapped platform;
manifest refusals (missing, wrong version, malformed) name the fix; consume
needs `.env`, loads images, places weights, writes the seeds and
`EZAI_OFFLINE` without touching the user's lines, skips a placed file on the
second run, refuses a checksum mismatch before placing anything, names a
failing `docker load`; `gguf_weights_dir` follows the descriptors and the
environment; the CLI verbs through `main` (this host's profile, `--json`,
host-only); `setup --offline` and the `.env` key both imply offline, and the
offline images step skips when every image is present or names the missing
one with the fix.

`tests/unit/test_bootstrap_auto.py` — three `auto` seeds resolve per group,
a shared recommendation is installed once and carries both groups.

`tests/integration/test_admin_center_journeys.py` — `GET /v1/first-run`
answers `recorded: false` and the Overview carries no card before a report
exists; once `report.json` is written the daemon serves it and the card
carries the groups, the smoke tally and the Orchestrator deep link; the
read-only operation is visible to the chat client too (no policy change);
six workflows and eight screenshots run in the real Chromium; wiring counts
updated.

- Updated (unmerged PRs only): `test_phase_p2_close.py` (1.2.0, 32
  operations, `first_run_report` in the additive set), `test_control_api.py`,
  `test_admin_center.py`, `test_admin_center_workbench.py` (version text),
  `test_admin_center_journeys.py` (counts). Every other pre-existing test
  passes unmodified; goldens and the chat-stack baseline fixture unchanged.

## Behavior notes

- **Contract 1.1.0 → 1.2.0**, additive: one read-only operation. 1.0.0 and
  1.1.0 clients are unaffected.
- **New CLI verbs** `bundle create`, `bundle consume`; new flag `setup
  --offline`; `install.sh --offline <dir>`. All host-only.
- **New `.env` key** `EZAI_OFFLINE=1`, written by consume; honored by the
  pipeline and `run_bootstrap` (fetchers refuse the network; images verified
  instead of pulled). Remove it to go back online.
- **Bootstrap `auto` semantics** changed from "one dedupe key per reference
  text" to "one per group": `.env` files with `auto` in several groups now
  get a recommendation per group (a shared recommendation is still one
  install). Explicit seeds are unchanged.
- **Admin Center Overview** shows a card only when the daemon serves
  `first_run_report` and a report exists.
- **Makefile:** `bundle`, `setup-offline`, `swe-accept` added; nothing
  renamed or removed.
- No chat-stack, compose, daemon-runtime or monitor-behavior change beyond
  the card; the chat-stack baseline matches.

## Verification boundary

Docker (`save`, `load`, `image inspect`, compose), the router, OpenWebUI,
the planner and the evaluator were fakes; the weights were fixture bytes.
The real `docker save`/`load` round trip and a real air-gapped host are
exercised on hardware, not in the suite (the P6 soak runbook). The
checksum, placement, `.env` editing, registry, generation, renderer and git
paths ran for real. The journeys ran in the pre-installed Chromium against
the launcher fixture with a fake daemon.

## Rollback note

`git revert <commit>` removes the module, the verbs, the offline fetchers,
the contract operation (back to 1.1.0; the spec artifact reverts with it),
the card, the sixth journey, the acceptance package and the Makefile
targets, and restores the `auto` dedupe. A bundle directory is inert data. A
consumed host keeps `EZAI_OFFLINE=1` in `.env`, which older code ignores;
the placed weights and loaded images are ordinary platform state. No data
migration.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane —
  unavailable here; to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry (bundle create/consume, F1–F11 scripted,
      onboarding Browser-QA'd); deviations listed and justified above: the
      contract operation and the console card (the PR-22 condition met),
      the `auto` dedupe fix (found by F9, confined to the seed loop)
- [x] New tests cover the scope incl. checksum mismatch, offline refusals,
      the no-report Overview and every abort step (the older-daemon path —
      no `first_run_report` operation → no card — is the same `None` branch
      the no-report test exercises, reached through the tolerated
      ControlPlane error; not driven separately); full suite green (709);
      ruff clean
- [x] Pre-existing tests unmodified except the unmerged tests listed under
      Tests; goldens passing; chat-stack baseline `--check` matches
- [x] Architecture docs updated to as-built (FIRST_RUN_EXPERIENCE §1/§6,
      FINAL_FIRST_RUN_EXPERIENCE §6, WEBUI_ADMIN_CENTER, CLI_REFERENCE,
      OPERATION_MANUAL, README, `.agent/architecture.md`); ADR-031 Accepted
      with the PR-21..23 as-built summary; roadmap P5 closed, next ready
      PR-24
- [x] Rollback note present; contract change additive and versioned
- [x] No model/vendor/runtime names in code: descriptors, catalog and
      fixtures carry them; the bundle records whatever the host's registry
      holds
