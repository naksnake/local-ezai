# PR-24 — Parity harness: the release gate over the CLI ↔ WebUI matrix (phase P6 opens)

**Phase:** P6 (opens the release train) · **ADR:** ADR-026 (Accepted; P6
slice recorded — P6 has no ADR of its own, its gates are ADR-026's
consequences; ADR-028's PR-24 deferral is closed) · **Size:** M ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-24 ·
**Depends on:** PR-11/PR-12 (connected mode, the parity smoke this gate
supersedes), PR-9 (shared operations, one error vocabulary), PR-10 (run
registry), PR-15 (client policy), PR-19 (memory operations, 1.1.0), PR-23
(contract 1.2.0) · **Status:** implemented on `claude/next-ready-pr-bnq7r3`
as the twenty-second stacked commit (PR-3..23 unmerged; each commit reverts
independently)

## Scope (as delivered)

[CLI_AND_WEBUI_STRATEGY.md](../CLI_AND_WEBUI_STRATEGY.md) §7: "for each
matrix row, execute via CLI-direct, CLI-connected, and control-plane API,
and assert identical state transitions and audit records" — as the P6
release gate ([V1_IMPLEMENTATION_PLAN.md](../V1_IMPLEMENTATION_PLAN.md) §P6
exit criterion 1), wired into `make` and CI.

- **The harness** (`agentd/tests/parity/harness.py`, test infrastructure):
  - **Three identical worlds per row**, one per surface: a bootstrapped
    platform (the PR-6..11 seed registry, rendered), a sample git
    repository, a local weights file, the daemon over it in-process, and a
    CLI config pointing at it. Surfaces: `cli-direct` (`--transport
    direct`; repo-work verbs are always in-process), `cli-connected`
    (`--transport connected`, the connected mode of PR-11, reaching the
    world's daemon through the CLI's `client_factory` seam), `api` (the
    control-plane operation **as the Admin Center calls it**: service token,
    `X-EZAI-User` = the human, `X-EZAI-Client: admin-center`, an
    idempotency key). The CLI acts as the same human (`USER`).
  - **Three equivalences per row:** the response bodies (the CLI's `--json`
    output *is* the API body — for refusals too, the shared error object),
    the declarative state afterwards (registry, rendered generation,
    generations, queue, projects, the repository's memory) and the
    **operation audit** (what the platform's operations recorded).
  - **The transport's annotation is normalised explicitly, as data:** the
    `via <client>` suffix of the forwarded identity; the daemon-side events
    (`api.<operation>`, `run.*`, `control.*`, `auth.*`) — kept apart,
    asserted **absent in-process and identical between the two daemon
    surfaces**; timestamps, run ids, commit hashes, wall-clock durations,
    each world's paths. `DROP_KEYS` and `PATTERNS` are the whole list; the
    normaliser has its own test, including what it must *not* equalise.
- **The matrix** (`agentd/tests/parity/test_parity_matrix.py`): one test
  per §3 row, the rows as data.
  - Three-surface rows: model install → benchmark → policy-approved
    activation → retire (→ a refused retire of the serving model) →
    uninstall; upgrade of a serving model → pending → show → approve →
    applied (the pinned role follows); activation → rollback → history (→
    a refused rollback to an unknown generation); the explain/catalog
    reads (nothing changes, nothing is audited — anywhere); the queue:
    pending → list → reject with a reason → list rejected → a second
    decision refused, an unknown request `not_found`; project add → list →
    a duplicate refused → remove → list → a second remove `not_found`;
    `status` (its `transport` line is the one designed difference; the
    API's aggregated report wraps the same snapshot) and `model benchmark`.
  - Repo-work rows: the same scripted **run** in-process (`local-ezai <repo>
    run`) and through the daemon (`POST /v1/runs`) — equal report, equal
    journal event sequence, the fix on a run branch, unpushed, the
    repository's own `.agent/` memory written on both; the same **plan** in
    both places — equal plan, equal journal, zero traces in either
    repository; **memory** — the CLI's repo-work add and the daemon's
    curated add produce the same store record; the CLI's repo-work verbs
    **never probe a daemon**.
  - The CLI-only row: `up`/`down` are direct even when connected is
    requested (the compose call is recorded, no daemon call), the contract
    has no such operation (no operation id, no path segment) and `POST
    /v1/up` is a `not_found` envelope.
  - **The chat column**, row by row: the chat-ops client may start runs and
    read; every other mutation is refused — checked against the daemon's
    policy table (PR-15) per row, and **every mutation the contract offers
    must be classified by some row** (a new endpoint without a matrix
    decision fails the gate).
  - **Tripwires:** the §3 table of the strategy doc is parsed — its twelve
    rows must equal the harness rows and the chat classifications, and each
    row's test must exist; the Make targets and the CI steps must exist.
- **Gate wiring.** `make swe-parity` runs the package alone; `make
  release-gate` chains lint · chat-stack baseline `--check` · the boundary
  drill · the F1–F11 acceptance suite · the parity harness · the full suite
  (the P6 train's one command); the agentd CI workflow gains the acceptance
  and parity steps and **stays `workflow_dispatch` only**, as requested
  earlier. `agentd/README.md` and the root README list the targets.
- **One defect found and fixed** (`platform_cli.cmd_model_rollback`):
  `local-ezai model rollback --json` printed its "ROLLBACK by …" notice as
  text before the JSON, so `--json` was not one document. The notice is
  text-mode only now; the JSON carries the same message. Text mode is
  unchanged (the existing PR-6 test still sees the notice).
- **Excluded by design:** any other CLI, daemon or Admin Center change; new
  contract operations (the contract stays 1.2.0); Admin Center pages (the
  console's HTTP identity path is exercised as the `api` surface, its pages
  stay under the PR-20 Browser-QA journeys, per §7); PR-25 (the mockengine
  drill, H1–H4) and PR-26 (docs refresh, soak, sign-off, tag).

## Design decisions made in this PR

1. **Identical worlds, not shared state.** Each surface acts on its own
   copy of the platform, so the gate compares *results*: a surface that
   merely reads what another wrote would pass a shared-state check while
   diverging in what it does. Paths differ per world and are normalised.
2. **The API surface is the console's identity.** The Admin Center forwards
   the human and names itself; calling the API that way exercises the third
   matrix column's audit path (`nita via admin-center`) instead of a
   generic client, and the normaliser proves the human is what matters.
3. **Equivalence on three planes, annotation as data.** Response, state and
   operation audit must be equal; what a transport legitimately adds is
   named in one place and kept apart — the daemon's own events are asserted
   *present and identical* on both daemon surfaces and *absent* in-process,
   rather than dropped. A future difference is a failing assertion, not a
   sieve.
4. **Absence is asserted, never skipped.** Repo-work verbs never probe a
   daemon (the offline-first rule), `up`/`down` have no operation, the
   memory verb's record is the store itself while the daemon's curated add
   is also platform-audited (PR-19's decision) — each pinned, so a change
   of stance is a visible test change.
5. **The doc table is the source of the rows.** The tripwire parses
   CLI_AND_WEBUI §3; a row added to the doc without a harness row, or a
   contract mutation without a chat classification, fails the gate.
6. **Fix the small defect in the affected verb, record it.** The `--json`
   pollution is a one-line change in the formatter; nothing else the gate
   surfaced needed code.

## Tests (15 new; suite 709 → 724)

`tests/parity/test_parity_matrix.py`: eleven row tests covering the twelve
§3 rows (rows 1 and 3 share the run scenario: start + report/journal) —
`test_row_model_lifecycle`, `test_row_approval_flow`, `test_row_rollback`,
`test_row_explain`, `test_row_governance_queue`,
`test_row_project_registration`, `test_row_health_and_bench`,
`test_row_start_run_and_its_report`, `test_row_plan_preview`,
`test_row_memory`, `test_row_stack_lifecycle_is_cli_only`; the chat ceiling
per row with the every-mutation-classified check; the doc-table tripwire;
the Make/CI wiring tripwire; the normaliser's own test.

Every pre-existing test passes unmodified (the PR-6 text-mode rollback test
still sees the ROLLBACK notice); goldens and the chat-stack baseline fixture
unchanged.

## Behavior notes

- **`local-ezai model rollback --json`** prints one JSON document; the
  "ROLLBACK by <actor>: …" notice appears in text mode only (the JSON's
  `message` carries the same text). Text-mode output is unchanged.
- New Make targets `swe-parity`, `release-gate`; two new steps in the
  manual-trigger CI workflow. Nothing renamed or removed.
- No contract, daemon, monitor, compose or chat-stack change.

## Verification boundary

The daemon ran in-process (the FastAPI app behind the CLI's httpx seam) —
the real-socket path is PR-11's and PR-12's; docker and the engine were the
PR-6..11 fakes; git, the registry, the renderer, the governance queue, the
run registry, the scripted pipelines and the memory store were real. The
Admin Center's pages were not driven here (the PR-20 journeys do); its
identity path was.

## Rollback note

`git revert <commit>` removes the test package, the Make targets, the CI
steps, the doc notes and restores the rollback notice in `--json` mode. No
state, contract or runtime change; nothing to migrate.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane —
  unavailable here; to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry (the three-surface harness over the §3
      matrix, release-gate wiring in CI); deviations listed and justified
      above: the `--json` rollback fix, `make release-gate`, the acceptance
      step added to CI alongside the parity step
- [x] New tests cover the scope incl. refusals, absences and the chat
      ceiling; full suite green (724); ruff clean; chat-stack baseline
      `--check` matches
- [x] Pre-existing tests unmodified; goldens passing
- [x] Architecture docs updated to as-built (CLI_AND_WEBUI §7,
      V1_IMPLEMENTATION_PLAN §P6, OPERATION_MANUAL, README, agentd README,
      `.agent/architecture.md`); ADR-026 P6 slice recorded, ADR-028
      deferral closed; roadmap P6 open, next ready PR-25
- [x] Rollback note present
- [x] No model/vendor/runtime names in code: the harness uses the seed
      registry's fixture names and the descriptors on disk
