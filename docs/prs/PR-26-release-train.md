# PR-26 — Release train: guides, release notes, the soak runbook, the DoD report, version 1.0.0 (phase P6 closes at the human steps)

**Phase:** P6 · **ADR:** ADR-026 (Accepted; P6 slice recorded — with PR-26
the V1 plan's twenty-six PRs are delivered and ADR-025..031 stand as built) ·
**Size:** M (documentation, one script, a version string, five tripwire
tests) · **Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-26 ·
**Depends on:** PR-1..25 (everything the guides describe and the gates the
release checklist runs) · **Status:** implemented on
`claude/next-ready-pr-bnq7r3` as the twenty-fourth stacked commit (PR-3..25
unmerged; each commit reverts independently) · **Merged:** PR #5 → `main`,
2026-09-10 · **Follow-up (2026-09-10):** the tag tripwire is corrected — see
"Follow-up" below

## Scope (as delivered)

The plan's entry: docs refresh, the 72 h soak runbook + results, the product
DoD checklist item by item, human release sign-off, tag `v1.0.0`. The first
three are delivered; the last two are **human acts** the PR prepares and
evidences, per CLAUDE.md ("production releases require approval; pull
request merges require approval") and GOVERNANCE.md.

- **The five guides absorb the V1 surfaces.**
  - `USER_GUIDE.md` (rewritten): the five-step first run (`make setup`, the
    one edit of `.env`, `init`, the offline bundle, the Platform-ready card);
    chat with role entries and the SWE tools; **Autonomous SWE from chat**
    (the Orchestrator: plan → confirm → run → report; what chat can and
    cannot do, and why); the CLI flows (unchanged content); **models and
    approvals as a user** (`status`, `model explain`, `catalog`, install →
    benchmark → activate → approve → rollback; evidence on the console);
    the Admin Center's pages and sign-in; where platform state lives.
  - `TROUBLESHOOTING.md` (rewritten, every prior row kept): new sections for
    the first run (exit codes 1/2/3 and their meaning, the bootstrapped /
    consumed refusals, wait-ready, the required chat smoke vs advisory
    checks, offline bundles, the banner, legacy `.env`), the control plane
    and connected mode (unreachable, no token, 401, `reload_unavailable`,
    unregistered project, limits, orphaned runs, idempotency, the forwarded
    identity), the Admin Center (daemon down, the cookie handoff, viewer vs
    admin, stale image, proxy header), governance and the lifecycle
    (decided once, reasons, retire refusals, drift, negotiation failures,
    format × runtime, descriptor errors, an added runtime, what rollback
    does), a platform-not-found row, and the gates (release-gate red, H1,
    parity, the drill, the baseline).
  - `MAINTENANCE_GUIDE.md` (rewritten): the V1 layout (descriptors, state,
    rendered outputs, machine-managed dirs, the gates' test packages, the
    contract artifact) with change policy per area; routine maintenance
    incl. `make release-gate`, the manual CI trigger and the contract bump
    rule; backups; state hygiene incl. generations, daemon records and soak
    dirs; **managing models day 2** (the V1 verbs, approval, runtime switch,
    unknown models, the per-repo override); **generations, rendering and
    rollback** (atomic apply with self-rollback, rollback as a new
    generation, drift, reload, generation vs rendered); extending (a new
    runtime = descriptor data, catalog entries, agents, tools, verbs that
    must be shared operations); the **release procedure** (gate → soak →
    version in both files → release notes + report → docs → human approval →
    tag → wheel); the health checklist incl. `local-ezai status` and the
    baseline.
  - `CLI_REFERENCE.md`: the verb ↔ endpoint table gains the 1.1.0 memory
    operations and the 1.2.0 first-run report with the contract-version
    note; `model install` documents the PR-25 resolver rule.
  - `OPERATION_MANUAL.md`: the suite size, `make release-gate` beside the
    self-test, a soak row in the stack table, the per-release cadence row
    (gate, soak, docs, report, notes).
- **`docs/RELEASE_NOTES.md`** — created (the fourth guide CLAUDE.md mandates
  and the Documentation Agent prepends to; it did not exist): the v1.0.0
  entry (highlights per phase, compatibility and upgrade notes, known
  limitations, verification) and the 1.0.0rc1 / 0.7.0 history pointing at
  their reports.
- **`docs/SOAK_RUNBOOK.md` + `scripts/soak.sh` (`make soak`)** — P6 exit
  criterion 2 as a procedure and a driver: two hosts (one per capability
  family, matching the committed profiles), Day 0 timed as the F1
  measurement, a fixed schedule (health and status every 15 min, bench
  hourly, a scripted SWE run on the bundled sample project every 2 h,
  **lifecycle churn every 6 h** — benchmark the chat primary → a new
  generation → `model rollback` — as the rollback-under-load exercise, an
  evolution cycle and `docker stats` daily), one JSON line per step, a
  `results.md` summary with the failures to explain, pass criteria, what to
  collect, a results template per host, and how the results feed the
  report. `--dry-run` prints the schedule without a stack; `--tick 60
  --hours 0.5` rehearses the whole schedule in 30 minutes.
- **`docs/V1_RELEASE_REPORT.md`** — the verdict ("ready for human
  sign-off"), what V1 delivered per phase, the final validation table (every
  gate and its command), the **DoD of TARGET_PRODUCT_V1 §8 item by item with
  evidence** (tests, PR artifacts, journeys, the soak's Day-0 timing for the
  30-minute item), the invariants of §6, the P6 exit criteria with the two
  human ones marked pending and the soak results table to fill in, the
  known limitations, the refreshed guides, and the human release checklist
  ending in the sign-off table and `git tag v1.0.0`.
- **Version 1.0.0** in `agentd/src/agentd/__init__.py` and
  `agentd/pyproject.toml` (they read `1.0.0rc1` and `0.5.0` before —
  inconsistent); `local-ezai version`, `ezaid --version`, `/health` and the
  `control.started` audit record carry it.
- **Tripwires** (`tests/integration/test_release_train.py`): one version
  everywhere and named by the newest release-notes entry (entries dated,
  newest first); the report's DoD table covers every §8 item and states the
  P6 criteria with the human ones pending; the soak driver's syntax, its
  dry-run schedule (288 ticks / 72 h: 72 benches, 36 runs, 12 churns, 3
  evolutions, 3 stats), the rehearsal window, the unknown-argument exit, the
  runbook's phrases, the Make target; each guide carries its V1 anchors and
  the four mandated guides exist; the report names the tag as a human step
  and records that the plan's tag name is already taken (follow-up below;
  originally: "no `v1.0.0` tag exists in the repository").
- **Excluded by design:** the tag, the sign-off and the merge (human); soak
  *results* (a 72 h hardware measurement — the report's host rows are
  pending, not fabricated); any code change beyond the version strings and
  the new script (the two PR-25 residuals stay recorded as known
  limitations); regenerating the guides with the Documentation Agent
  (needs the model plane; the hand-written refresh is what it would have
  refreshed).

## Design decisions made in this PR

1. **Bump the version, never tag.** MAINTENANCE_GUIDE §6 (and the previous
   release procedure) put the version bump in the release PR so the merged
   tree is what gets tagged; the tag itself is the human's act after the
   sign-off table reads "approve" in every row. (The PR shipped with a test
   asserting the tag is absent; the follow-up below removed that assertion —
   see why.)
2. **Soak = a fixed, logged schedule, not a checklist of vibes.** The driver
   fixes the cadence, records every step with its output tail, and never
   aborts on a failure (a failure is a data point to explain); the runbook
   defines "unexplained" and makes the human's own use of the platform part
   of the load. The Day-0 timing gives the 30-minute DoD item its number.
3. **Rollback under load is a scheduled step.** Benchmark → generation →
   rollback every 6 h while chat is served is the smallest churn that needs
   no approval, exercises the side-load, the renderer, the apply protocol
   and rollback, and leaves the platform where it was.
4. **Evidence, not assertion.** The DoD table points at the test or artifact
   that proves each item; where the proof is a host measurement it says so
   and where the results go. Pending stays pending.
5. **Guides are rewritten where the surface changed shape** (user,
   troubleshooting, maintenance) and edited where it only grew (CLI
   reference, operation manual); every pre-existing row and command was
   kept.
6. **Release notes are the mandated fourth guide.** The Documentation Agent's
   prompt prepends dated entries at the top; the file is created in that
   shape so evolution runs slot in above.

## Tests (5 new; suite 738 → 743)

`tests/integration/test_release_train.py` — the version is one and the same
everywhere; the DoD checklist covers every target-product item and states
the P6 criteria; the soak driver's schedule is what the runbook says (syntax
check, dry-run counts, rehearsal, bad argument, runbook phrases, Make
target); the guides carry the V1 surfaces and the four mandated guides
exist; the tag is the human's act. Every pre-existing test passes unmodified
(the control-plane liveness test compares the version dynamically).

## Behavior notes

- `agentd.__version__` is `1.0.0` (was `1.0.0rc1`); the wheel version is
  `1.0.0` (was `0.5.0`). `local-ezai version`, `ezaid --version`, `GET
  /health` and `control.started` report it. No contract change (1.2.0).
- New: `scripts/soak.sh`, `make soak`, `docs/RELEASE_NOTES.md`,
  `docs/SOAK_RUNBOOK.md`, `docs/V1_RELEASE_REPORT.md`. Nothing renamed or
  removed; no compose, daemon, CLI-verb or chat-stack change.

## Verification boundary

The soak driver ran only in dry-run and syntax check here; its live schedule
needs a bootstrapped host with the stack and the CLI (the runbook's
prerequisites). The DoD evidence is the suite and the PR artifacts on this
branch; the 30-minute first-run number and the soak rows are host
measurements the release manager fills in. The Documentation Agent was not
run (model plane unavailable); the refresh is hand-written from the code
and the PR artifacts.

## Follow-up (2026-09-10, after the merge)

`test_the_tag_is_the_humans_act` asserted `git tag --list v1.0.0` is empty.
The repository **already has** a `v1.0.0` tag (2026-08-14, "Release version
1.0.0_WebChat", the chat platform's release at `fab97ec`, an ancestor of
`main`). This checkout had been cloned without tags, so the suite was green
here; every ordinary clone fetches the tag, so `make swe-test` and `make
release-gate` failed off a fresh clone. Two consequences, both recorded:

- the test keeps its documentation assertions (the report names the tag as
  the human's act, the sign-off rows are open) and now asserts the report
  records the collision; it no longer reads the repository's tag state — a
  tag-state assertion also fails forever on the tagged commit once the
  release *is* cut, which was a design error of the original test;
- the tag name is an open human decision: `v1.0.0` must not be moved;
  V1_RELEASE_REPORT §7 step 5 lays out the choice (`1.1.0` per semver, or
  `2.0.0`) and the files that move with it (version strings, the newest
  RELEASE_NOTES entry, the report heading, the release-train test pins).

Lesson for the Memory Agent: before naming a release in a plan or a test,
list the repository's tags (`git ls-remote --tags origin`); never assert
git tag state in the suite.

## Rollback note

`git revert <commit>` restores the previous guides and version strings and
removes the release notes, the runbook, the driver, the report and the
target. No state, contract or runtime change.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane —
  unavailable here; to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry up to the human steps (docs refresh,
      soak runbook + results *template*, DoD item by item); deviations
      listed and justified above: results pending the hardware run, the
      tag and sign-off left to humans, release notes created
- [x] New tests cover the release artifacts' claims; full suite green (743);
      ruff clean; chat-stack baseline `--check` matches
- [x] Pre-existing tests unmodified; goldens passing
- [x] Architecture docs updated (the five guides, V1_IMPLEMENTATION_PLAN
      §P6, RELEASE_NOTES, V1_RELEASE_REPORT, `.agent/architecture.md`);
      ADR-026 P6 slice recorded; roadmap: V1 plan delivered, human steps
      listed, M7 in progress
- [x] Rollback note present
- [x] No model/vendor/runtime names introduced in code (the H1 gate scans
      the new script and the Makefile)
