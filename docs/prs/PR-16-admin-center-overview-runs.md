# PR-16 — Admin Center: `ezaid` client + Overview / Runs pages (phase P4 opens)

**Phase:** P4 · **ADR:** ADR-030 (**→ Proposed** with this PR) · **Size:** L ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-16 ·
**Depends on:** PR-12 (contract frozen at 1.0.0), PR-10 (run endpoints),
PR-8/9 (auth, audit, aggregated health, governance list, role explain) ·
**Status:** implemented on `claude/next-ready-pr-bnq7r3` as the fourteenth
stacked commit (PR-3..15 unmerged; each commit reverts independently)

## Scope (as delivered)

The existing monitor service (:8888) grows into the Admin Center of
[WEBUI_ADMIN_CENTER.md](../WEBUI_ADMIN_CENTER.md), rendering only what the
control plane serves — first the client, then the two pages of this slice.

- **Control-plane client inside the monitor — `monitor/admin_center.py`.**
  A thin async client over the frozen 1.0.0 contract: bearer service token
  (`EZAI_CONTROL_TOKEN`, server side only — the browser never sees it),
  `X-EZAI-Client: admin-center`, the monitor login forwarded as
  `X-EZAI-User` (audit actor `admin via admin-center` / `viewer via
  admin-center`), a fresh `Idempotency-Key` per mutation, the daemon's error
  object passed through unchanged (`{"error": {code, message, fix}}`), and
  two errors of its own in the same shape: `control_unreachable` (with the
  `make control-up` / `make control-serve` / `EZAI_CONTROL_URL_MONITOR` fix)
  and `control_token_missing`.
- **Overview page (`/overview`).** Stack health as the daemon sees it,
  platform facts (generation, capability class, engine runtime), role cards
  (role → group or pin → primary + fallbacks, from `GET /v1/roles/{role}`
  for orchestrator/planner/coder/debugger/reviewer/chat — roles the registry
  lacks are skipped), the pending governance queue with the CLI verb that
  decides it (the Governance page is PR-18), recent runs, and a footer naming
  the generation and registry the view was rendered from (UX principle 3).
  A daemon that is down is a page state — `connected: false` plus the fix —
  while health and the knowledge base keep working.
- **Runs page (`/runs`) and run detail (`/runs/{id}`).** Filterable list
  (kind, status; auto-refresh while anything is active) and the deep link the
  CLI and the SWE tools already print: request, actor, timeline, progress,
  error; the report by kind — plan table, validation checks incl. Browser QA,
  review verdict and findings, self-healing iterations, delivery (commit,
  branch, pushed or not, the merge command that stays human), models used;
  sprint task tables; evolution proposal, benchmark before/after, PR or
  bundle link; the journal tail. **One mutation: cancel, admin role only**
  (the parity matrix's "view, cancel"; viewer gets 403 from the monitor
  before the daemon is asked). Because the monitor login is ambient (HTTP
  Basic), the mutation also requires the `X-Requested-With: admin-center`
  header the page's own JavaScript sends — a cross-site form cannot set it
  (`same_origin_required`, 400).
- **Data path.** Page JavaScript calls the monitor under `/api/ezai/…`
  (`overview`, `runs`, `runs/{id}`, `runs/{id}/cancel`); each page is one
  aggregated request. Pages are served as path routes from one template, the
  path picks the view. The monitor's pre-existing routes and dashboard are
  untouched except for the shared header and a navigation bar
  (Overview · Runs · Health & Knowledge).
- **Wiring.** `docker-compose.yml` gives the monitor `EZAI_CONTROL_URL`
  (`${EZAI_CONTROL_URL_MONITOR:-http://ezaid:8010}`), `EZAI_CONTROL_TOKEN`
  and `host.docker.internal` for a host daemon — the mcpo precedent of PR-13;
  `monitor/Dockerfile` copies the module; `.env.example` documents the URL
  override; `make monitor` names the pages. The chat-stack baseline of PR-15
  treats the monitor's control-plane keys as additive (the same rule it
  applies to mcpo), so the committed fixture is byte-identical.
- **Excluded by design:** Models / Routing / Runtime pages (PR-17); the
  Governance queue UX, approve/reject and evidence panels (PR-18); Sprints /
  Evolution / Memory / Projects pages (PR-19); the SSO trusted-header handoff
  and the five zero-CLI journeys as Browser-QA workflows (PR-20); any new
  control-plane operation (contract stays 1.0.0); starting work from the
  console (absent in the parity matrix); changes to the health dashboard or
  knowledge-base flows beyond the navigation.

## Design decisions made in this PR

1. **One service, one image, a sibling module.** The Admin Center is
   `install()`ed on the monitor's FastAPI app with the monitor's own RBAC
   dependencies injected — no second process, no duplicated auth, no
   circular import.
2. **Server-side client, not a browser client.** Keeping the token in the
   monitor means the console can be exposed on a LAN without exposing the
   control plane's credential; the monitor login becomes the audited human.
3. **Path routes for deep links.** `/runs/<id>` is what the tool server and
   the CLI print; hash routing would have broken those links.
4. **Aggregate per page.** One request per page keeps the UI simple and the
   daemon's access pattern obvious (health, six role explanations, the queue,
   recent runs); a partial failure is a page state, not a half-drawn screen.
5. **Framework-free.** The dashboard's vanilla-JS pattern and dark theme
   tokens are reused; no bundler, no framework, no build step (UX principle 5).
6. **Baseline rule, not baseline rewrite.** The chat-stack normalization
   gains "monitor control-plane keys are additive" instead of regenerating
   the fixture, so the fixture still proves the chat path unchanged.
7. **Same-origin guard on the one mutation.** Ambient Basic credentials make
   a plain form post a CSRF vector; requiring a custom request header costs
   one line in the page and closes it without a token or session system.

## Tests (10 new; suite 594 → 604)

`tests/integration/test_admin_center.py` — the monitor as shipped
(`monitor/monitor.py` + `admin_center.py`, loaded from the repository) under
a TestClient, its control-plane client wired to the real in-process daemon
through a recording ASGI transport:

- Overview renders the platform from the control plane (health green,
  generation, runtime, roles incl. a pinned one, empty queue, empty runs);
  every call carried the token, the client name and the forwarded login; no
  mutation was audited.
- Overview shows a pending change request it cannot decide (no approve route).
- Degraded mode: connection refused → `connected: false` + fix; other
  Admin Center calls answer the envelope as 503; the dashboard and `/api/status`
  still work.
- A missing token is named (`control_token_missing`), the daemon never asked.
- A real scripted run: listed, filtered, detailed (plan, validation, review,
  commit not pushed, journal), the deep-link page served.
- Cancel: viewer 403 (never reaches the daemon), anonymous 401, a form-shaped
  admin post 400 (`same_origin_required`, daemon never asked), admin from the
  page cancels through the daemon — audited as `admin via admin-center`,
  idempotency key present, report `null` after cancellation, pending report
  on a running job.
- Daemon errors pass through as the shared envelope (404 `not_found`); no page
  contains the token.
- Pages behind the monitor login (Basic challenge), navigation present, the
  pre-existing dashboard intact (KB bar, `/api/status` service set).
- Wiring: compose env + `extra_hosts`, Dockerfile copy, `.env.example`, and
  the chat-stack baseline still matching.
- **Real-Chromium smoke:** the monitor under uvicorn, Playwright with the
  viewer login — Overview (healthy, generation, roles, empty queue, recent
  run), a plan run's detail, the filtered list, a not-found detail — with
  console errors and page errors failing the test.
- `tests/security/test_chat_stack_regression.py` gained the monitor
  assertion (no `EZAI_CONTROL_*` key, no `extra_hosts` in the recorded
  surface). All earlier tests pass unmodified; goldens intact; 1.0.0
  inventory unchanged.

## Behavior notes

- The monitor's dashboard header now reads "Local-EZAI Admin Center" with a
  navigation bar; the page title becomes "Local-EZAI Admin Center · Health".
  Every existing route, the SSE stream, the RBAC and the knowledge-base API
  are unchanged. The FastAPI app title/version of the monitor changed
  (1.0.0 → 1.1.0); nothing reads them.
- New monitor routes: `/overview`, `/runs`, `/runs/{id}`, `/api/ezai/*`.
- New compose environment on the monitor (`EZAI_CONTROL_URL`,
  `EZAI_CONTROL_TOKEN`) and `extra_hosts`; a rebuild of the monitor image is
  needed for the pages (`make build` / `docker compose build monitor`).
- Without a running control plane the new pages show "Control plane
  unreachable" with the fix; nothing else changes for existing installs.

## Verification boundary

The pages were exercised by a real headless Chromium against the monitor
running under uvicorn with the in-process daemon — not against a compose
stack. The `host.docker.internal` reachability for a host daemon follows the
mcpo precedent and is checked once on a stack host (`make control-serve`,
then `/overview` on the monitor).

## Rollback note

`git revert <commit>` removes the module, the monitor wiring (import, env,
install call, header/nav), the compose env + `extra_hosts`, the Dockerfile
line, the `.env.example` hint, the baseline rule and the tests. No state or
contract migration; the 1.0.0 artifact and the chat-stack fixture are
untouched.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane —
  unavailable here; to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry (ezaid client + Overview/Runs pages as a
      monitor extension); deviations: none — cancel is the one mutation the
      matrix commits to
- [x] New tests cover the scope; full suite green (604); ruff clean (the new
      module and the tests linted with the agentd config)
- [x] Pre-existing tests unmodified except one additive assertion in the
      PR-15 (unmerged) regression test; goldens passing; chat-stack fixture
      unchanged
- [x] Architecture docs updated to as-built (WEBUI_ADMIN_CENTER §1–§2,
      CLI_AND_WEBUI_STRATEGY §3, OPERATION_MANUAL, README); ADR-030 Proposed
      with the PR-16 slice; roadmap + plan status
- [x] Rollback note present
- [x] No model/vendor names in code (roles only)
