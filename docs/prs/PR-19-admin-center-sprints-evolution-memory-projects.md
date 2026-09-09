# PR-19 — Admin Center: Sprints / Evolution / Memory / Projects pages (phase P4)

**Phase:** P4 · **ADR:** ADR-030 (Proposed; PR-19 slice recorded) · **Size:** M ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-19 ·
**Depends on:** PR-16..18 (monitor client, template, guards, page
conventions), PR-10 (run endpoints), PR-9 (project operations), PR-12 (the
contract's versioning rule) · **Status:** implemented on
`claude/next-ready-pr-bnq7r3` as the seventeenth stacked commit (PR-3..18
unmerged; each commit reverts independently)

## Scope (as delivered)

The remaining pages of [WEBUI_ADMIN_CENTER.md](../WEBUI_ADMIN_CENTER.md) §2
and the two console starts of
[WEBUI_PRODUCT_STRATEGY.md](../WEBUI_PRODUCT_STRATEGY.md) §3.5–§3.6.

- **Projects (`/projects`).** The chat-ops allowlist: every registered
  repository with who registered it and when, its work (run count, active
  runs, last run) and links to its runs and its memory. Admins register a
  repository by path (optional name) and remove one — `POST` / `DELETE
  /v1/projects` through the daemon, refusals verbatim (not a git repository,
  already registered, unknown). The page states that the control plane needs
  the path on its own filesystem.
- **Sprints (`/sprints`).** Every sprint run with status, project, spec
  excerpt and, from its report, goal, waves, branch, report document, the
  task results (wave, dependencies, status, commit, error) and the
  **dependency graph as mermaid source** generated from the plan. Admins
  start a sprint from a pasted markdown specification (target project,
  sequential / keep-going options) — journey 3.
- **Evolution (`/evolution`).** Every cycle with proposal, failure patterns,
  bottlenecks, improvements, benchmark before → after, tasks, the pull
  request or proposal bundle, release-notes flag and the standing "awaiting
  human review". Admins run a cycle with an optional focus — journey 4. A
  banner lists evolution proposals in the governance queue when any exist
  and otherwise states that proposals join the queue once the pipeline
  submits change requests.
- **Memory (`/memory`).** A registered project's memory browser: project
  rules, coding styles, architecture decisions, failed and successful fixes
  (with error signature, category, files), implementation history; counts
  per kind, kind filter, keyword search, the store path. Admins remember a
  curated rule / style / decision — the twin of `local-ezai memory --add`.
- **Contract 1.1.0 (additive).** `GET /v1/projects/{name}/memory`
  (`project_memory`: kind, search, limit) and `POST
  /v1/projects/{name}/memory` (`project_memory_add`: one of the three
  curated kinds + text; exports `lessons_learned.json`; audited
  `memory.added`). `CONTRACT_VERSION` 1.0.0 → 1.1.0, the artifact
  regenerated, `x-contract-status` names the addition; every 1.0.0
  operation is unchanged and pinned as such.
- **Console starts, two kinds.** `POST /api/ezai/runs` forwards `sprint` and
  `evolve` starts to `POST /v1/runs` as the monitor login; any other kind is
  refused by the monitor before the daemon is asked. The Runs page gained a
  `project` filter for the Projects page's links.
- **Excluded by design:** a mermaid rendering library (offline product; the
  source is offered instead); a sprint dry-run preview (no such run kind);
  submitting evolution proposals or release candidates into the queue (the
  pipelines' work); a connected-mode CLI twin for `memory` (direct-mode
  repository work); SSO and the journey suite (PR-20).

## Design decisions made in this PR

1. **Version the contract instead of bending the page.** The Memory page
   had no data in 1.0.0; the P2 close's rule says additive → minor bump,
   regenerate, update the inventory — so that is what happened, with the
   frozen 29 pinned separately from the two additions. Chat still cannot
   write memory: the PR-15 client policy lists `run_start` only.
2. **Curated kinds only by hand.** Fixes and implementation history are
   run-recorded evidence; the API's `kind` is a literal of the three curated
   kinds, so the wrong kind is a 422 before it reaches the store.
3. **Amend the matrix, in the open.** The later product strategy's
   wireframes and journeys need the console to start sprints and evolution
   cycles; both verbs exist on the CLI, so parity holds. Runs, fixes and
   plans stay off the console (no wireframe, no journey).
4. **Graph as source.** The dependency graph is generated from the plan
   the report carries and offered as mermaid text — the same graph the
   runtime writes into the sprint report document, without a rendering
   library or a file-read operation the contract lacks.
5. **Reports fetched in one round, absent ones tolerated.** A run that ended
   without a report shows the journal link, never an error.

## Tests (13 new — 12 here + 1 boundary row; suite 627 → 640)

`tests/integration/test_admin_center_workbench.py` — the monitor over the
in-process daemon with fake sprint / evolution pipelines returning realistic
reports and a seeded memory store in the fixture repository:

- The contract's 1.1.0 operations directly: version, paths, browse (counts,
  order, kind filter, search), unknown project 404, bad kind 409, chat may
  read but its write is `client_forbidden`.
- Memory page: browse, kind and search filters, remember from the page
  (id, `data.by`, `lessons_learned.json` regenerated, audit `memory.added`
  and `api.project_memory_add` by `admin via admin-center`), the daemon's
  rules (fix kinds by hand → 422, empty text → 422, unknown project → 404).
- Projects page: list with work counters, register (`added_by` the monitor
  login), refusals (not git, duplicate), remove, unknown → 404, audit.
- Console sprint: start with options, report with plan, waves, task
  results, mermaid source (`graph LR`, `T1 --> T2`, `T1 --> T3`), project
  work counters, Runs page project filter.
- Console evolution: start with focus, proposal, improvements, benchmark
  before → after, bundle, release notes, actor, audit.
- The console starts sprints and evolution only (run / fix / plan / none →
  400 before the daemon); the daemon's own validation for a missing spec
  (422) and an unregistered project (404).
- Every mutation (4, parametrized): viewer 403, anonymous 401, admin
  without the page header 400; viewers read every page's data.
- Pages behind the login, navigation from the dashboard, overview and
  governance; dashboard intact.
- **Real-Chromium smoke:** viewer renders Sprints (goal, graph source,
  failed task), Evolution (proposal, benchmarks, bundle, "awaiting human
  review"), Projects (work count, no remove button), Memory (entries,
  signature, no form); admin remembers a coding style from the page
  (notice, entry appears, subtitle count, audit) and sees the remove button.
- Updated: `test_phase_p2_close.py` (inventory 29 + 2, version 1.1.0,
  status text), `test_control_api.py` (expected operation set),
  `test_chat_boundary.py` (the new mutation in the forbidden calls),
  `test_admin_center.py` (contract string). All other earlier tests pass
  unmodified; goldens and the chat-stack baseline unchanged.

## Behavior notes

- **Contract:** `CONTRACT_VERSION` is 1.1.0; `docs/api/ezaid-openapi.json`
  regenerated (two operations, three schemas added, `info.version` and
  `x-contract-status` changed). Every 1.0.0 operation, path, method and
  schema is unchanged. Clients pinned to 1.0.0 see one more version string.
- New monitor routes: `/projects`, `/sprints`, `/evolution`, `/memory` and
  their `/api/ezai/*` data; `POST /api/ezai/runs` (sprint | evolve). The
  navigation gains Sprints · Evolution · Memory · Projects on every page.
- The parity matrix's "start" row changes for the Admin Center: sprint and
  evolve starts are offered (admin); run / fix / plan are not.
- Four unmerged tests updated as listed above.

## Verification boundary

Sprint and evolution pipelines were faked (the daemon persisted their real
`RunReport`-shaped outputs); the real pipelines are exercised by their own
suites. Pages were rendered in a real headless Chromium against the monitor
under uvicorn with the in-process daemon.

## Rollback note

`git revert <commit>` restores the 1.0.0 contract (constant, artifact,
inventory), removes the two operations, the pages, routes and scripts, and
the tests. No data migration: memory stores created through the API are
ordinary `.agent/memory.db` files the CLI reads too.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane —
  unavailable here; to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry (the four pages); deviations, listed and
      justified above: the additive 1.1.0 contract operations the Memory
      page requires, and the two console starts the product strategy's
      journeys require
- [x] New tests cover the scope; full suite green (640); ruff clean
- [x] Pre-existing tests unmodified except the four unmerged tests updated
      for the contract bump and the new mutation; goldens passing;
      chat-stack baseline unchanged
- [x] Architecture docs updated to as-built (WEBUI_ADMIN_CENTER §2,
      CLI_AND_WEBUI_STRATEGY §3 + §6, OPERATION_MANUAL, README); ADR-030
      PR-19 slice recorded; contract notes in `agentd.control`
- [x] Rollback note present
- [x] No model/vendor names in code
