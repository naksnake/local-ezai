# PR-25 — Agnosticism gates: the third-runtime drill, H1 word audit, H2–H4 fixtures (phase P6)

**Phase:** P6 · **ADR:** ADR-026 (Accepted; the gates its consequences
named — P6 slice recorded) · **Size:** M ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-25 ·
**Depends on:** PR-2 (capability vector, classes, `fit`), PR-3 (descriptors,
renderer), PR-4 (lifecycle install/benchmark, side-load, catalog
recommender), PR-5 (activation, apply, rollback), PR-7 (bootstrap), PR-21/22
(installer, setup pipeline and its harness), PR-23 (acceptance harness),
PR-24 (`release-gate`) · **Status:** implemented on
`claude/next-ready-pr-bnq7r3` as the twenty-third stacked commit (PR-3..24
unmerged; each commit reverts independently)

## Scope (as delivered)

The agnosticism proof ADR-026 gated V1 on: the third-runtime drill of
[RUNTIME_ABSTRACTION_STRATEGY.md](../RUNTIME_ABSTRACTION_STRATEGY.md) §6 and
the release-gated tests H1–H4 of
[HARDWARE_AGNOSTIC_ARCHITECTURE.md](../HARDWARE_AGNOSTIC_ARCHITECTURE.md) §6,
as `agentd/tests/gates/` (`make swe-gates`; part of `make release-gate` and
the manual-trigger CI workflow).

- **The third-runtime drill** (`test_third_runtime_drill.py`).
  - **The runtime is a fixture, not a product:**
    `tests/fixtures/providers/mockengine.yaml` — served format, capabilities
    with the generic tool handler, readiness on `/healthz` (not `/health`),
    its own benchmark timing keys (`mock_timings`…), a prefixed served-id
    template (`mock/{model_name}`), its own mount paths, single and multi
    materialization forms, class tuning, images per accelerator kind.
    Deliberately unlike the shipped descriptors wherever the contract allows,
    so a code path that silently assumed a shipped value would show. The
    shipped `config/providers/` is unchanged (a tripwire pins the set).
  - **The image is an in-test OpenAI-API stub** (`MockEngine`, a threaded
    stdlib HTTP server): readiness on the fixture's path,
    `/v1/chat/completions` with a usage block and the fixture's timing block.
    Docker is the PR-4 recording fake; every side-load lands on the stub's
    port through the `free_port` seam; the real `SideLoadValidator` runs
    with a fake clock (a probe that never answers times out at once).
  - **End to end through the CLI**, the descriptor copied into a checkout's
    `config/providers/`: `bootstrap` from `AI_RUNTIME=mockengine` seeds →
    the lifecycle installs, validates (one-token probe) and benchmarks
    (the fixture's timing keys → 42 tok/s) each seed against the stub —
    three validations, three benchmarks, six side-loads up and down, the
    side-load compose documents carrying the fixture's image, command and
    memory limit, weights placed under the fixture's mount dir; generation 1
    rendered in the **multi form** (the fixture's `serve-many` command,
    preset with class tuning and per-model lines, mount, healthcheck on
    `/healthz`, environment), LiteLLM routing the role aliases to the
    prefixed served ids, the role map naming the runtime. Day 2: `model
    install <gguf source>` (served by the slot's own runtime — see the fix),
    `model benchmark`, `model activate --group chat` → pending →
    `governance approve` → applied (four served, `--max 4`), `model
    rollback` → three served again. Serve: the setup pipeline (bootstrap
    skipped, images skipped) waits for the engine on **the descriptor's**
    readiness path, runs the smoke checks and reports the runtime; `up
    --profile n97 --rendered` passes the rendered override to compose.
  - **`install.sh --runtime mockengine`** validates the seeds against the
    third descriptor (F8: a format it does not serve is refused with the
    fix); the class default stays the shipped one (the fixture claims no
    class).
  - **Zero diffs outside descriptors and fixtures, as a test:** the runtime
    id appears in no shipped Python, packaged data, descriptor, compose
    file, script, Makefile, installer or `.env.example`.
- **H1 — the word audit** (`test_h1_word_audit.py`): vendor, brand and SKU
  tokens (word-bounded, case-insensitive; the vendor word that is also an
  English word counts only next to a product word; `amd64` and the
  `-apple-system` font stack excluded; accelerator *kinds* allowed) over the
  platform's code, packaged data, prompts, the console, the tool server,
  the k8s manifests and the **continuity layer** (Makefile, compose files,
  `.env.example`, scripts, `install.sh`). `config/providers/*.yaml` is the
  sanctioned home and is not scanned. Every other exception is an `ALLOWED`
  entry with a reason (the probe table and presets in `capability.py`, the
  legacy profile names in `--profile` help, the host-provisioning script,
  the frozen chat stack's legacy profile files and comments) — **and a
  stale allowance fails**, so the list cannot grow quietly. Docs and tests
  are out of scope by the gate's definition (history and fixtures). Token
  edge cases have their own test.
- **H2–H4** (`test_h2_h3_h4_fixtures.py`, over the acceptance harness):
  **H2** the same `AI_RUNTIME` + three `auto` seeds through the pipeline on
  `accel-large` and on `cpu-low` — both ready, identical roles and groups,
  each group served by the recommender's pick for that class, the engine
  materialized from the descriptor's (class × accelerator) row (different
  artifacts, same code), the substituted groups exactly those where the
  recommender's picks differ; **H3** a 7B Q4 entry (declared 4.4 GB) on the
  four class fixture vectors — accelerator/fast on both accelerator classes,
  system/moderate on both CPU classes, no warnings, `classify` agreeing with
  the class — plus the edges (no SIMD → slow; a small accelerator → spills to
  system memory with the warning; too little memory → does not fit yet
  overridable, the numbers named; unknown size → an honest unknown);
  **H4** `install.sh --profile n97` and `--class cpu-low` on two copies of
  one checkout, then the pipeline: both `cpu-low`, the class mapping back to
  the same preset, **byte-identical rendered artifacts** (manifest
  included) and the same generation.
- **Gate wiring.** `make swe-gates`; `make release-gate` runs it after the
  parity harness; the CI workflow gains the step and **stays
  `workflow_dispatch` only**; READMEs and the operation manual list it.
- **Fixed — a defect the drill found** (`lifecycle.slot_runtime_for`,
  `resolve_target`): with no `--runtime`, a `gguf:`/`hf:` source was
  assigned the **first descriptor serving the format, alphabetically**. With
  a third runtime present, `model install <source>` installed for another
  runtime than the slot's (and side-loaded its image — the drill hung on it
  for the other runtime's full timeout). The slot's own runtime now serves
  the source when it can; otherwise the format rule decides as before.
  Invisible while each format has exactly one server (the shipped pair).
- **Reworded — four brand words the audit found in user-facing text:** the
  `up-n97` Make help, a prompt file's usage note, the legacy download
  script's header, a monitor comment. Wording only; nothing executes
  differently.
- **Excluded by design:** any product runtime (the drill's descriptor stays a
  fixture; TGI/ollama-style runtimes are post-V1, RUNTIME_ABSTRACTION §6);
  changes to the descriptor schema, the renderer, the bootstrap or the
  daemon; edits to the frozen chat stack's compose/env files (their brand
  words are pinned as continuity, ADR-002); the two residuals below;
  PR-26 (docs refresh, soak, DoD, sign-off, tag).

## Findings recorded, not fixed here (for the release train)

1. **The control plane's health table probes the engine slot at its own
   `/health`**, not the active descriptor's readiness path
   (`control/health.py` default targets; `setup_pipeline.host_targets`). The
   pipeline's wait-ready step *is* descriptor-driven (the drill asserts the
   first engine poll hits `/healthz`); the generic services sweep that
   follows uses the table's path (the drill asserts that too, so the seam is
   visible). Both shipped runtimes answer `/health`, so nothing is wrong
   today; a third runtime with another path would show "engine down" in
   `/v1/health` and the console. Fix sketch: derive the engine target's path
   from the active descriptor in `host_targets()` and in
   `aggregated_health` (two small edits; the table stays operator data).
2. **A day-2 `model install` of an undeclared user source carries no
   tool-call format**, so tool-calling roles refuse it at render time
   (`missing capability tool_calling … '<none declared>'`). The bootstrap
   applies the runtime's generic handler to undeclared *seeds*; `model
   install` has no equivalent (`--tool-format` or the same default). A
   negotiation rule, not a runtime coupling — the drill activates into the
   chat group for that reason.

## Design decisions made in this PR

1. **The drill's runtime is a test fixture.** Shipping `mockengine` under
   `config/providers/` would offer a stub engine to users and change the
   installer's candidate set; a fixture copied into a temp checkout proves
   the same seam with no product surface. The tripwire makes the "zero
   diffs" claim a test, not a review note.
2. **The stub is the image.** Docker is faked as in every lifecycle test,
   but readiness, validation and benchmark are real HTTP to a real server
   speaking the OpenAI API — the verbs run their real code with the fixture's
   data (path, keys, served id), which is what the drill must prove.
3. **Different on purpose.** Every descriptor field the contract lets vary is
   set unlike the shipped values, so an implicit assumption of a shipped
   value fails loudly rather than passing by coincidence.
4. **H1's exceptions are data with reasons, and must stay in use.** A grep
   gate with a silent allowlist rots; here each allowance names its file,
   its tokens and its reason, and disappears from the list the day the
   occurrence disappears from the file.
5. **The continuity layer is pinned, not cleaned.** The frozen chat stack's
   compose files and `.env.example` carry historical brand words in
   comments; editing them for the gate would touch ADR-002's frozen surface
   for no behavior gain. User-facing text the platform owns (Make help, a
   prompt note, a script header, a comment) was reworded instead.
6. **Fix the defect the drill found, in the resolver, minimally.** The
   slot-runtime preference is one helper and one call site, changes nothing
   for the shipped pair, and is exactly the rule PROVIDER_ABSTRACTION §4
   states (one slot, one runtime). Residuals that would widen the PR are
   recorded with fix sketches.

## Tests (14 new; suite 724 → 738)

`tests/gates/test_third_runtime_drill.py` — the drill (bootstrap → validate
→ benchmark → render → day-2 install/benchmark/activate/approve/rollback →
pipeline wait-ready → `up --rendered`, every assertion listed above); the
installer and bootstrap take the third runtime from data (F8 included); no
platform code knows the drill runtime.

`tests/gates/test_h1_word_audit.py` — no violations outside the sanctioned
places; every allowance in use; the sanctioned home is where the knowledge
lives; token edge cases.

`tests/gates/test_h2_h3_h4_fixtures.py` — H2 (two classes, one seed file);
H3 (four vectors, parametrized) and the edges; H4 end to end.

Every pre-existing test passes unmodified (the resolver change is invisible
to the shipped pair — 94 lifecycle/bootstrap/API/CLI tests re-run
unchanged); goldens and the chat-stack baseline fixture unchanged.

## Behavior notes

- **`local-ezai model install <gguf:|hf: source>` without `--runtime`**: the
  runtime of the active slot serves the source when it serves the format;
  otherwise the first descriptor serving the format, as before. With the
  shipped descriptors the outcome is identical in every case.
- **Wording only:** the `up-n97` help text, a usage note in
  `config/prompts/web-search-assistant.md` (above the prompt body), the
  header comment of `scripts/download-models-n97.sh`, a comment in
  `monitor/monitor.py`.
- New Make target `swe-gates`; `release-gate` runs it; one new CI step on
  the manual workflow. Nothing renamed or removed.
- No contract, daemon, renderer, compose or chat-stack change.

## Verification boundary

The drill's docker is a recording fake and its engine an in-process stub:
what a real `mockengine` container would do is exactly the stub's contract
(readiness, one chat completion, a timing block). The real `docker compose`
side-load and the image pull stay hardware-side. H2–H4 run on the fakes of
the acceptance harness (pretend fetchers, FakeDocker, FakeHttp); `fit()`,
`classify()`, the recommender, the bootstrap, the renderer and the
registry run for real. H1 scans the working tree as committed.

## Rollback note

`git revert <commit>` removes the gates package, the fixture descriptor, the
Make target, the CI step, the doc notes, restores the four original wordings
and the resolver's alphabetical default. No state, contract or runtime
change; nothing to migrate.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane —
  unavailable here; to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry (drill green with zero diffs outside
      descriptors, H1 CI audit, H2–H4 fixtures); deviations listed and
      justified above: the resolver fix, the four rewordings, the two
      recorded residuals
- [x] New tests cover the scope incl. F8 on the third runtime, the no-code
      tripwire, stale allowances and the fit edges; full suite green (738);
      ruff clean; chat-stack baseline `--check` matches
- [x] Pre-existing tests unmodified; goldens passing
- [x] Architecture docs updated to as-built (RUNTIME_ABSTRACTION §6,
      HARDWARE_AGNOSTIC §6, V1_IMPLEMENTATION_PLAN §P6, OPERATION_MANUAL,
      README, agentd README, `.agent/architecture.md`); ADR-026 P6 slice
      recorded; roadmap: next ready PR-26
- [x] Rollback note present
- [x] No model/vendor/runtime names in code: the H1 gate now enforces it;
      the fixture descriptor's names are `example.invalid` images and
      `drill-*` models
