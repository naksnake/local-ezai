# PR-18 — Admin Center: Governance queue + approval view (phase P4)

**Phase:** P4 · **ADR:** ADR-030 (Proposed; PR-18 slice recorded) · **Size:** M ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-18 ·
**Depends on:** PR-16/PR-17 (monitor client, template, same-origin guard,
page conventions), PR-9 (governance endpoints), PR-5 (activation + queue
semantics) · **Status:** implemented on `claude/next-ready-pr-bnq7r3` as the
sixteenth stacked commit (PR-3..17 unmerged; each commit reverts
independently)

## Scope (as delivered)

The page [WEBUI_ADMIN_CENTER.md](../WEBUI_ADMIN_CENTER.md) §3 calls the one
that matters most, on the monitor, over the frozen 1.0.0 contract.

- **Governance page (`/governance`).** *Awaiting approval*: every pending
  change request with id, kind, what changes, who requested it (and who
  proposed it when not a human), the affected roles or the runtime-switch
  flag, its age. *History*: decided requests behind a status filter
  (approved / applied / rejected / failed / superseded, with counts); each
  row shows who decided, when, the reason and the resulting generation. A
  note states which item types enter the queue today.
- **Approval view (`/governance/<id>`)** — the deep link the SWE tool
  server's `governance_queue` and the CLI already print. Laid out as
  WEBUI_PRODUCT_STRATEGY §3.7: **What changes** (diff lines; each affected
  role before → after), **Evidence** (benchmarks measured on this host, fit
  verdicts of newly active models with their warnings, the render-time
  capability report per role × model × runtime with failure text, runtime
  before → after with the switch flag, the capability class), **Proposed
  by** (requested by / proposed by; evolution is marked advisory; policy
  approvals are named), **Reversibility** (the generation one rollback
  restores, the CLI twin), **Decision** (recorded by / when / reason and the
  apply result, or, for an admin on a pending request, *Reject (reason…)*
  and *Approve & apply*).
- **Decisions through the daemon.** `POST /api/ezai/governance/{id}/approve
  | reject` forward to `POST /v1/governance/{id}/approve | reject` as the
  monitor login (audit actor `admin via admin-center`), admin role only,
  same-origin header required; the daemon's refusals pass through verbatim:
  a rejection needs a reason, decisions are made once (`governance_rule`,
  409). Viewers read the queue and the approval view and decide nothing.
- **Cross-links.** The Overview and Models banners now link to the queue and
  each request id to its approval view (the "arrives in the next slice" text
  is gone); the proposal notice on the Models page names the deep link; the
  navigation gains **Governance** on every page.
- **Excluded by design:** any new contract operation; submitting evolution
  proposals or release candidates into the queue (their pipelines' work);
  memory write-back on rejection beyond what the daemon does; SSO (PR-20);
  Sprints / Evolution / Memory / Projects (PR-19).

## Design decisions made in this PR

1. **The evidence block, not the registry dump.** The full proposed
   registry is what the daemon applies, not what a human weighs; the page
   shows the diff, the affected roles and the evidence, and leaves the dump
   on the daemon.
2. **Reversibility is stated in generations.** The approval view names the
   generation an approval creates and the one a rollback restores, with the
   Models page and the CLI verb that do it (UX principle 1: an approve button
   never appears without what justifies it *and* what undoes it).
3. **No synthetic item types.** Evolution PRs and release candidates are
   designed queue items but nothing submits them yet; the page says so
   rather than faking rows.
4. **Two unmerged assertions flipped, not preserved.** PR-16 and PR-17 tests
   encoded "the monitor has no approve route"; they now assert the guard the
   route enforces (the same pattern PR-14 used for PR-13's compose check).

## Tests (8 new; suite 619 → 627)

`tests/integration/test_admin_center_governance.py` — the PR-17 fixture
(monitor over the in-process daemon with faked lifecycle seams):

- An empty queue is a page state (counts, pending 0; page behind the login).
- A pending activation shows its evidence: summary row (kind, requester
  `admin via admin-center`, affected role, no runtime switch, no evidence in
  the listing), detail (diff lines, before → after chain, benchmarks, fit
  with warnings, capability report entries, runtime, class, `reversible_to`,
  no `proposed` dump), the deep-link page, the Overview pointing at it.
- Approve from the page: applied, generation 2, decision by/reason, the
  stored request `applied` with `result.generation`, counts, the Models page
  showing the new chain, audit events `api.governance_approve`,
  `request.approved`, `request.applied` by `admin via admin-center`, an
  idempotency key on the call.
- Reject: no reason → 409 `governance_rule` "needs a reason" (generation
  untouched); with reason → rejected; approve afterwards → "made once";
  the status filter; audit; a missing request → 404.
- Both decisions (parametrized): viewer 403, anonymous 401, admin without
  the page header 400 `same_origin_required`; the daemon never asked; a
  viewer still reads queue and detail.
- Navigation reaches Governance from every page; the deep link is behind
  the login; the dashboard is intact.
- **Real-Chromium smoke:** the viewer sees two pending requests, the
  evidence and no decision buttons; the admin approves one from the view
  (prompt reason recorded, badge `applied`, "now generation 3"), rejects the
  other (badge `rejected`, reason shown, generation unchanged), and the
  queue shows 0 pending with the applied generation in the history; console
  errors fail the test.
- `test_admin_center.py` and `test_admin_center_models.py`: the "no approve
  route" assertions flipped (see Behavior notes). All other earlier tests
  pass unmodified; goldens, 1.0.0 artifact and chat-stack baseline unchanged.

## Behavior notes

- New monitor routes: `/governance`, `/governance/{id}`,
  `/api/ezai/governance[?status=]`, `/api/ezai/governance/{id}`,
  `/api/ezai/governance/{id}/approve | reject`. Navigation gains Governance.
- Overview and Models banner texts changed from "the Governance page arrives
  in the next slice" to links; the Models page's proposal notice names the
  deep link instead of the CLI verb.
- Two unmerged tests changed: PR-16's `test_overview_shows_the_governance_
  queue_it_may_not_decide` and PR-17's `test_lifecycle_through_the_monitor_
  as_admin` asserted `404` for the approve route; the former now asserts the
  same-origin refusal (400), the latter drops the assertion (the CLI still
  approves there, so the audit shows two humans).
- No control-plane or contract change.

## Verification boundary

Exercised in a real headless Chromium against the monitor under uvicorn with
the in-process daemon; the apply path used no engine reload (no docker in
the sandbox, `reload=False` as on the CLI). Evolution-proposal rows will
appear once the evolution pipeline submits change requests (not this PR).

## Rollback note

`git revert <commit>` removes the routes, the page script, the navigation
entry, the banner links and the tests, and restores the two flipped
assertions. No state or contract migration.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane —
  unavailable here; to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry (evidence panels, approve/reject, deep
      links); deviations: none
- [x] New tests cover the scope; full suite green (627); ruff clean
- [x] Pre-existing tests unmodified except the two unmerged PR-16/PR-17
      assertions listed above; goldens passing; 1.0.0 artifact and
      chat-stack baseline unchanged
- [x] Architecture docs updated to as-built (WEBUI_ADMIN_CENTER §3,
      CLI_AND_WEBUI_STRATEGY §3, MODEL_GOVERNANCE_V2 §2, OPERATION_MANUAL,
      README); ADR-030 PR-18 slice recorded
- [x] Rollback note present
- [x] No model/vendor names in code
