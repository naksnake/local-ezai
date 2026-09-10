# PR-17 — Admin Center: Models / Routing / Runtime pages (phase P4)

**Phase:** P4 · **ADR:** ADR-030 (Proposed; PR-17 slice recorded) · **Size:** L ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-17 ·
**Depends on:** PR-16 (monitor client + page template), PR-12 (frozen 1.0.0
contract), PR-9 (lifecycle / governance / role-explain / catalog endpoints),
PR-4/PR-5 (lifecycle + activation semantics) · **Status:** implemented on
`claude/next-ready-pr-bnq7r3` as the fifteenth stacked commit (PR-3..16
unmerged; each commit reverts independently)

## Scope (as delivered)

Three more pages on the monitor, rendering only what the control plane
serves, with the lifecycle mutations the parity matrix commits to
([CLI_AND_WEBUI_STRATEGY.md](../CLI_AND_WEBUI_STRATEGY.md) §3).

- **Models page (`/models`) — role-first, models as evidence.** Group panels
  whose serving order is the *resolution* order (an unpinned role's primary
  and fallbacks; a pinned-only group shows its active members) followed by
  the group's non-serving members with state, runtime and format, size and
  context, measured tokens/s and a **fit badge** — the platform recommender's
  verdict for a matching catalog candidate on the model's runtime, or "no
  verdict" for a user-supplied source (fit is never computed in the browser).
  Models in no group ("not in any group"), role cards linking to the Routing
  page, the catalog with per-variant verdicts for the active runtime, the
  generation history with diffs, the pending queue. **Mutations (admin):**
  add model (catalog id or any `hf:` / `gguf:` source, optional name, group,
  runtime), install a catalog variant, benchmark, activate into a group,
  upgrade to another model, retire, uninstall (force on request), roll back
  to a generation (reason recorded). Activation and upgrade land in the
  approval queue whenever a serving role changes; the page reports the
  request id and the CLI verb that decides it until the Governance page
  (PR-18).
- **Routing page (`/routing`) — the explain view** of
  [MODEL_ROUTING_DESIGN.md](../MODEL_ROUTING_DESIGN.md) §7: the standing
  table (role → pin or group → primary → fallbacks → contract → ok), one card
  per defined role with the resolver's reason lines, the contract, the
  per-model checks with their failure text and the generation the answer was
  resolved from; roles the registry does not define are listed as such; the
  generation history with diffs.
- **Runtime page (`/runtime`) — the engine slot and the switch pre-check**
  of [RUNTIME_ABSTRACTION_STRATEGY.md](../RUNTIME_ABSTRACTION_STRATEGY.md)
  §5: active runtime, capability class, accelerator, this host's memory and
  cores, engine/router health, the active models; for every other runtime
  the descriptors serve, a pre-check naming the active models that lack a
  variant for it (with the fix: install one or keep the current runtime) and
  the catalog candidates per group with fit and contract verdicts. The page
  states how a switch happens today — activating a model served by the other
  runtime, a change request flagged as a runtime switch that needs approval —
  and offers **no switch button**, because the contract has no runtime verb.
- **One additive data enrichment in the control plane.** The platform
  snapshot's per-model entries gain `groups`, `context`, `format` and
  `license` (`platform_cli.platform_snapshot`). The `models` object is
  untyped in the 1.0.0 contract: the regenerated OpenAPI artifact is
  byte-identical and the frozen inventory unchanged. Without membership the
  role-first cards cannot be drawn from the contract.
- **Guards, as in PR-16.** Every mutation needs the admin role and the
  `X-Requested-With: admin-center` header the page sends (ambient Basic
  credentials, cross-site forms). Errors are the daemon's own envelopes.
- **Excluded by design:** governance approve/reject and evidence panels
  (PR-18); Sprints / Evolution / Memory / Projects (PR-19); SSO and the
  journey suite (PR-20); any new contract operation — a runtime verb, role or
  group editing (WEBUI_ADMIN_CENTER's "edit role/group assignment" has no
  1.0.0 operation and no CLI verb: parity or absence); client-side fit
  computation; changes to the health dashboard beyond the navigation.

## Design decisions made in this PR

1. **Serving order from resolution, not from the registry file.** The
   contract exposes role resolution, not the groups' member order; the
   page shows what serves (the order that matters) and lists the rest. A
   group with no member and no role is invisible to any thin client — and
   to the page — rather than guessed at.
2. **Fit is the platform's word.** Badges come from the recommender's
   verdicts (`fit()` runs once, in the platform); a source that is not a
   catalog entry gets "no verdict" and its measured tokens/s — evidence over
   assumption (HARDWARE_AGNOSTIC §4).
3. **Additive data, not a contract bump.** Adding keys to the untyped
   `models` object needs no version change; a typed `groups` map would have
   (a 1.1 candidate, with the runtime verb).
4. **Refusals are shown, not softened.** A source without a declared
   tool-call format cannot become the coder's primary; the render-time
   negotiation refuses and the page shows that text — the same the CLI
   prints.
5. **Honest Runtime page.** The pre-check reuses the recommender per group
   and per runtime and says plainly that on this host the vllm descriptor's
   context budget rules out every candidate; a "Switch" button would promise
   an operation the platform does not have.

## Tests (15 new; suite 604 → 619)

`tests/integration/test_admin_center_models.py` — the monitor as shipped
over the in-process daemon with the lifecycle seams the CLI tests fake:

- Models page data: groups in resolution order with members, roles, the
  enrichment keys, no verdict for seed models, recommender candidates with
  verdicts for the active runtime only, catalog + per-variant verdicts,
  history, empty queue.
- Lifecycle through the monitor as admin: install a `gguf:` source →
  benchmark (15.5 tok/s, listed under "not in any group") → its activation
  refused by the negotiation (no tool-call format; the daemon's words pass
  through) → activate a capable model → pending request (coder affected) →
  no approve route on the monitor → approved on the API → generation 2 with
  the new primary and the old one as fallback → rollback to generation 1
  → generation 3; audit actors `admin via admin-center`; idempotency keys
  and forwarded identity on every mutation.
- Retire refused for a serving primary (pass-through), upgrade → approval →
  old model retired, uninstall blocked then forced.
- Every mutation (7, parametrized): viewer 403, anonymous 401, admin without
  the page header 400 `same_origin_required`; the daemon never asked.
- Daemon errors and states pass through (not found, no evidence, bad ref).
- Routing data: the three defined roles with pin/group, chains, checks,
  contracts, reasons and generation; the known-roles list; history.
- Runtime data: active runtime, both runtimes, engine + router health,
  active models, the vllm pre-check not ready, the blocker named with format
  and runtime, per-group candidates with `fits` but `eligible: false` and the
  `min_context` / class-budget failure text.
- Pages behind the login; navigation on every page; dashboard intact.
- **Real-Chromium smoke:** viewer renders Models (no mutation buttons),
  Routing, Runtime; admin benchmarks a model from the page (dialog accepted,
  notice shows the measurement, three group rows update to `benchmarked`,
  audit records `admin via admin-center`); console errors fail the test.

All earlier tests pass unmodified; goldens intact; 1.0.0 inventory and
artifact unchanged; chat-stack baseline unchanged.

## Behavior notes

- New monitor routes: `/models`, `/routing`, `/runtime`, `/api/ezai/models`
  (GET, POST), `/api/ezai/models/upgrade`, `/api/ezai/models/{name}/benchmark
  | activate | retire`, `DELETE /api/ezai/models/{name}`,
  `/api/ezai/generations/rollback`, `/api/ezai/routing`, `/api/ezai/runtime`.
  The navigation bar gains Models · Routing · Runtime on every page.
- `GET /v1/models` and the `platform` object of `GET /v1/health` carry four
  more keys per model; `local-ezai status --json` shows them too. Text
  output is unchanged.
- Rebuild the monitor image for the pages (`docker compose build monitor`).

## Verification boundary

Pages were exercised in a real headless Chromium against the monitor under
uvicorn with the in-process daemon and faked fetch / side-load / validator
seams — not against a live engine. The install path with a real download
and a real side-load is the PR-4 lifecycle's, unchanged here.

## Rollback note

`git revert <commit>` removes the pages, routes and scripts, the navigation
entries, the snapshot enrichment and the tests. No state or contract
migration; the 1.0.0 artifact is untouched.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane —
  unavailable here; to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry (role-first cards, fit badges, explain
      views, runtime switch pre-check UX); deviation: the snapshot gains
      four per-model keys (justified above; contract artifact unchanged)
- [x] New tests cover the scope; full suite green (619); ruff clean
- [x] Pre-existing tests unmodified; goldens passing; 1.0.0 inventory and
      artifact unchanged; chat-stack baseline unchanged
- [x] Architecture docs updated to as-built (WEBUI_ADMIN_CENTER §2,
      CLI_AND_WEBUI_STRATEGY §3, MODEL_LIFECYCLE_MANAGEMENT §2,
      MODEL_ROUTING_DESIGN §7, RUNTIME_ABSTRACTION_STRATEGY §5,
      OPERATION_MANUAL, README); ADR-030 PR-17 slice recorded
- [x] Rollback note present
- [x] No model/vendor names in code (roles and runtimes are data from the
      daemon; the page speaks "runtime", never a brand)
