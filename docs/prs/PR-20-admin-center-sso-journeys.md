# PR-20 — Admin Center: identity handoff + the Browser-QA journey suite (P4 close)

**Phase:** P4 (close) · **ADR:** ADR-030 (**Accepted** with this PR) · **Size:** M ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-20 ·
**Depends on:** PR-16..19 (every Admin Center page), PR-11 (Browser QA
harness, ADR-016), PR-15 (chat-stack baseline) · **Status:** implemented on
`claude/next-ready-pr-bnq7r3` as the eighteenth stacked commit (PR-3..19
unmerged; each commit reverts independently)

## Scope (as delivered)

The two items the plan names for the P4 close — the SSO handoff with Basic
fallback and the five zero-CLI journeys of
[WEBUI_PRODUCT_STRATEGY.md](../WEBUI_PRODUCT_STRATEGY.md) §5 as Browser-QA
workflows in CI — plus the page changes the journeys needed.

- **Identity handoff (`monitor/monitor.py`).** The viewer / admin
  dependencies now return an `Identity`: the role string the existing
  checks compare against, carrying the person (`user`) and how they arrived
  (`via`). Resolution order: the pre-existing paths (auth off, the machine
  bearer, HTTP Basic) → a **trusted header** from a reverse proxy
  (`MONITOR_SSO_TRUSTED_HEADER`, honoured only with the shared secret in
  `X-EZAI-Proxy-Secret` equal to `MONITOR_SSO_TRUSTED_SECRET`; the addresses
  in `MONITOR_SSO_ADMINS` are admins, everyone else a viewer) → the
  **OpenWebUI session** (the `token` cookie, validated against
  `MONITOR_SSO_OPENWEBUI_URL/api/v1/auths/`, cached 60 s and bounded;
  OpenWebUI admin ⇒ admin, user ⇒ viewer, pending or invalid ⇒ not signed
  in) → the Basic challenge. Everything is opt-in by environment; without
  the keys the monitor behaves exactly as before. The Admin Center forwards
  `Identity.user` as `X-EZAI-User`, so the daemon's audit trail names the
  person (`nita@example.com via admin-center`). The daemon is untouched.
- **No native dialogs (`monitor/admin_center.py`).** Every confirmation and
  reason — activate / upgrade / retire / uninstall / rollback, approve /
  reject, cancel run, remove project — is an inline form (`form.inline`,
  confirm button `[data-confirm]`) instead of `prompt()` / `confirm()`.
  The Browser QA harness has no dialog step by design (native dialogs are
  auto-dismissed), so the journeys could not have driven the old dialogs;
  the forms also make the reason visible in a screenshot.
- **Overview banner.** The last registry change (generation, note, when,
  diff lines, the generation a rollback restores) from
  `GET /v1/generations?limit=2`, with *Roll back* → reason → confirm through
  the PR-17 rollback route — journey 5 in three clicks. `/api/ezai/overview`
  also returns the resolved role.
- **Sprint views** carry "How to merge (stays human on purpose)" with the
  merge command, on the Sprints card and the run detail — journey 3's last
  step. `/favicon.ico` answers 204 so a bare browser tab produces no console
  error the harness would count.
- **The journey suite.** `agentd/examples/browser-qa.admin-center.yaml` —
  five workflows, seven screenshots, the harness's own step vocabulary
  (goto / fill / select / click / expect_text / expect_no_text /
  expect_visible / wait_for / screenshot). `agentd/tests/fixtures/admin_center_app.py`
  — the launcher the harness starts: a bootstrapped scratch platform
  (registry seed plus an unpinned `planner` role in the reasoning group so
  swapping the reasoning model is a governed change), the lifecycle seams
  the offline suites fake, an in-process control plane with fake sprint /
  evolution pipelines, the monitor as shipped (`monitor.py` loaded from the
  repository) served by uvicorn on the port the harness picks, login
  disabled for the walkthrough. Runnable by hand for a click-through.
- **Wiring.** Compose gives the monitor the four `MONITOR_SSO_*` keys
  (`MONITOR_SSO_OPENWEBUI_URL` defaults to `http://openwebui:8080`, the
  others empty); `.env.example` documents them; the chat-stack baseline
  treats `MONITOR_SSO_` like `EZAI_CONTROL_` (additive — the fixture is
  byte-identical).
- **Excluded by design:** a reverse proxy shipped with the stack (the
  trusted-header path documents the contract a proxy must meet); an
  identity store of the platform's own; the chat-side step of journey 1
  (needs OpenWebUI); approving a real runtime switch in journey 2 (needs a
  host where a variant of the other runtime fits — on the fixture's class
  the pre-check rules every candidate out, which is what the journey
  asserts); rejecting an evolution proposal in Governance in journey 4 (the
  evolution pipeline does not submit change requests yet — the PR-18 note
  stands); a runtime verb or any contract change (1.1.0 is unchanged).

## Design decisions made in this PR

1. **Validate the WebUI session against the WebUI, never decode it.** The
   monitor treats OpenWebUI's token as opaque and asks OpenWebUI who it is;
   any failure (network, 401, unknown role) means "not signed in here" and
   falls through to Basic. No shared signing key, no coupling to OpenWebUI's
   token format.
2. **A header alone is never trusted.** The trusted-header path requires
   the shared secret on every request, so a client that can reach the
   monitor directly cannot claim an identity; the deployment contract is
   "the proxy sets both".
3. **Identity is a `str` subclass.** Every existing `role == "admin"`
   comparison in the monitor and the Admin Center keeps working unchanged;
   only the forwarded header reads the person.
4. **Testable by the product's own capability.** The strategy's rule is that
   the walkthroughs *are* the tests, and the platform's Browser QA harness
   is declarative and dialog-free — so the console dropped native dialogs
   rather than the suite growing an escape hatch.
5. **The launcher is a fixture, not a product surface.** It reuses the
   suites' fakes and seams and lives under `tests/fixtures`; the YAML is an
   example a human can run by hand, and CI runs it through the harness.
6. **Honest boundaries stay in the artifact.** Where a journey stops short
   of the strategy's wording, the YAML, the strategy's as-built note and
   ADR-030 say why and what would lift the boundary.

## Tests (6 new; suite 640 → 646)

`tests/integration/test_admin_center_journeys.py`:

- **Trusted header:** admin by list, viewer otherwise; the header without
  the secret and with a wrong secret → 401 with the Basic challenge; the
  person is what travels on the wire (`X-EZAI-User`) and what the daemon
  audits (`memory.added` by `nita@example.com via admin-center`); a proxied
  viewer reads and cannot mutate.
- **OpenWebUI session:** OpenWebUI mocked at `/api/v1/auths/`; an admin
  token → admin with the email forwarded; a second page → the lookup is
  cached (one call); a user token → viewer, mutation 403; a pending account
  → 401; a forged token → 401 with the Basic challenge; Basic still works
  next to the cookie and the dashboard is unchanged.
- **Opt-in:** the PR-16 monitor (no SSO environment) ignores headers and
  cookies.
- **P4 exit criterion 1, both directions:** the CLI proposes an activation
  and the console approves it (decision `admin via admin-center`, applied);
  the console benchmarks and proposes a second activation and the CLI
  approves it from the same queue; the history counts two applied requests
  with both deciders.
- **P4 exit criterion 3:** the five journeys run under `BrowserQAHarness`
  in a real headless Chromium (skipped where no Chromium can launch):
  report without error, five workflows in order, every step passed with no
  console or page errors, summary "all 5 workflow(s) passed", seven
  screenshots on disk (3 / 1 / 1 / 1 / 1).
- **Wiring:** compose keys and defaults, `.env.example`, the YAML has five
  workflows, the launcher exists, the chat-stack baseline is unchanged.
- Updated: `test_admin_center_governance.py` (the real-Chromium smoke now
  approves and rejects through the inline forms instead of a dialog
  handler), `test_admin_center.py` (`load_monitor` accepts extra
  environment and clears the SSO keys), `tests/security/test_chat_stack_regression.py`
  (the additive-prefix assertion covers `MONITOR_SSO_`). Every other test
  passes unmodified; goldens and the chat-stack baseline unchanged.

## Behavior notes

- New monitor environment (all optional): `MONITOR_SSO_TRUSTED_HEADER`,
  `MONITOR_SSO_TRUSTED_SECRET`, `MONITOR_SSO_ADMINS`,
  `MONITOR_SSO_OPENWEBUI_URL`. Unset = the PR-16 behavior.
- The `/api/ezai/overview` payload gains `role` and `last_change`; the
  Overview page gains the banner; confirmations moved from browser dialogs
  to inline forms on every page; `/favicon.ico` → 204.
- The audit actor for console actions becomes the person when a handoff
  identity is present (`<email> via admin-center`); with the Basic login it
  stays `admin via admin-center` / `viewer via admin-center`.
- No contract change (1.1.0), no daemon change, no CLI change.

## Verification boundary

OpenWebUI was mocked at its `/api/v1/auths/` endpoint; no reverse proxy was
run (the trusted-header tests send the headers directly). Lifecycle seams
(fetch, side-load, probe, validator) were faked as in the PR-17 suite; the
sprint and evolution pipelines were faked as in PR-19. The journeys ran in a
real headless Chromium against the monitor under uvicorn, started by the
harness's own app launcher.

## Rollback note

`git revert <commit>` removes the handoff (the monitor is Basic-only again;
`MONITOR_SSO_*` keys, if present in `.env`, are ignored), restores the
dialog-based confirmations, removes the banner, the merge hint, the favicon
route, the YAML, the launcher and the tests, and the compose keys. No data
migration: nothing is persisted by this PR beyond ordinary audit lines.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane —
  unavailable here; to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry (trusted-header handoff with Basic
      fallback; the five journeys as Browser-QA workflows in CI); the
      OpenWebUI-session handoff is the same plan line ("from OpenWebUI")
      delivered without a proxy dependency; page changes are the journeys'
      prerequisites, listed above
- [x] New tests cover the scope; full suite green (646); ruff clean
- [x] Pre-existing tests unmodified except the three unmerged tests
      updated as listed; goldens passing; chat-stack baseline unchanged
- [x] Architecture docs updated to as-built (WEBUI_ADMIN_CENTER §2 + §4,
      WEBUI_PRODUCT_STRATEGY §5, OPERATION_MANUAL, README); ADR-030 flipped
      to Accepted with the PR-20 slice and the PR-16..20 summary; roadmap
      and plan mark P4 closed
- [x] Rollback note present
- [x] No model/vendor names in code
