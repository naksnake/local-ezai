# PR-9 — Lifecycle + governance endpoints (control plane, phase P2)

**Phase:** P2 · **ADR:** ADR-028 (Proposed; PR-9 slice recorded) · **Size:** M ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-9 ·
**Depends on:** PR-8 (auth dependency, audit log, envelope, `platform_snapshot`),
PR-6 verbs, PR-5 queue + apply protocol, PR-4 install/benchmark ·
**Status:** implemented on `claude/next-ready-pr-bnq7r3` as the seventh
stacked commit (PR-3..8 unmerged; each commit reverts independently)

## Scope (as delivered)

- **One brain (`platform_cli` operations).** The orchestration inside every
  PR-6 verb moved into shared operation functions — `install_model`,
  `benchmark_model`, `activate_model`, `upgrade_model`, `rollback_generation`,
  `retire_model`, `uninstall_model`, `explain_role`, `generation_history`,
  `catalog_listing`, `catalog_recommendations`, `list_requests`,
  `show_request`, `approve_request`, `reject_request`, `list_projects`,
  `add_project`, `remove_project`, `apply_request` — each returning the
  JSON-able mapping the CLI prints with `--json`. The `cmd_*` verbs only
  format; the API returns the same mappings as typed responses. **Parity is
  a test:** for every read verb, `local-ezai … --json` equals the API body.
- **`agentd/control/api.py` — 19 operations** on `/v1`, each summary naming
  its CLI verb: `GET /models` · `POST /models` (install) ·
  `POST /models/{name}/benchmark` · `POST /models/{name}/activate` ·
  `POST /models/upgrade` · `POST /generations/rollback` ·
  `POST /models/{name}/retire` · `DELETE /models/{name}` (uninstall) ·
  `GET /roles/{role}` (explain) · `GET /generations` (history) ·
  `GET /catalog` · `GET /catalog/recommendations?group=` ·
  `GET /governance[?status=]` · `GET /governance/{id}` ·
  `POST /governance/{id}/approve` · `POST /governance/{id}/reject` ·
  `GET /projects` · `POST /projects` · `DELETE /projects?target=`.
  The actor of every operation is the forwarded identity
  (`<user> via <client>`) — `requested_by`, decisions and `added_by` show
  it. Mutations are serialized by one lock inside the daemon.
- **Idempotency keys** (`Idempotency-Key`, `control/idempotency.py`): a
  retry with the same key and payload replays the stored response
  (`Idempotency-Replayed: true`) without re-running the mutation; the same
  key with a different payload is `409 idempotency_conflict`; refusals are
  stored too. Records: one JSON file per key under
  `config/control/idempotency/`.
- **Shared error vocabulary** (`agentd/platform_errors.py`): `classify()`
  maps the platform's exception types to `{code, message, fix}` + HTTP
  status + CLI exit code (`lifecycle_refused` 409, `governance_rule` 409,
  `resolution_incomplete` 409, `registry_invalid` / `render_refused` /
  `catalog_refused` / `bootstrap_refused` 422, `platform_unavailable` 503,
  `not_found` 404 by the modules' own "unknown model / no change request /
  not defined in the registry / no registered project" phrasing). The API
  returns it as the PR-8 envelope; **the CLI prints the same envelope in
  `--json` mode** with unchanged exit codes. `PlatformError` moved into this
  module (re-exported by `platform_cli`).
- **Audit of every mutating call**: `api.<operationId>` with the forwarded
  actor, method, path, status, client and idempotency key — next to the
  operation's own records (`request.submitted`, `request.approved`, …) in
  the single log. Reads are not audited; rejected authentication stays
  `auth.rejected`.
- **Reload guard:** a `reload: true` request is refused with
  `409 reload_unavailable` (fix named) where the docker CLI is absent —
  always the case in the shipped container — instead of failing half-way
  through the apply protocol.
- **Contract `1.0.0-draft.9`**, artifact regenerated (23 operations;
  `ChangeRequest` and the outcome models in `components.schemas`).
- **Excluded (later PRs):** run endpoints / async job registry (PR-10 —
  install and benchmark stay synchronous calls, exactly like direct mode);
  CLI connected mode (PR-11); spec freeze + phase-close tests (PR-12);
  cross-process locking between CLI and daemon; docker socket in the
  container.

## Design decisions made in this PR

1. **Operations live in `platform_cli`, next to the seams.** The tests
   patch `platform_cli.default_runner` / `engine_http` / `build_validator`;
   keeping the operations in that module makes the same fakes serve both
   surfaces and avoids a circular import — the API imports the CLI module,
   never the reverse.
2. **The operation's dict is the contract.** Response models are built from
   the operation's mapping (`Model(**data)`), so a field drift between CLI
   JSON and API body fails a test rather than a client.
3. **`not_found` by phrasing.** The modules already say "unknown model …"
   consistently; classifying on that phrasing gives correct 404s without
   touching PR-4/5 code.
4. **Idempotency + audit as one middleware** on `POST`/`DELETE`: the
   request body is read once (the framework caches it), the response is
   buffered, stored under the key and echoed; the caller set by the auth
   dependency (`request.state`) names the actor.
5. **Fail closed on reload.** Refusing before the apply protocol starts is
   the only safe answer where the daemon cannot run compose; the fix points
   at `make up` or the host CLI's `--reload`.

## Tests (22 new; suite 504 → 526)

- `tests/unit/test_platform_errors.py` (3): every platform exception →
  code/status/exit; not-found phrasing wins regardless of module; the catch
  list.
- `tests/unit/test_control_api.py` (18): **parity** (six read verbs: CLI
  `--json` == API body; `/models` == the `status` snapshot); activation →
  pending → queue/show → approve (applied as generation 2, decision by the
  forwarded actor) → decided-once 409 → rollback → history, with `api.*`
  and `request.*` audit records; policy-approved activation applies at
  once; reject requires a reason (422 invalid_request for a missing body
  field, 409 governance_rule for an empty one) and lists as rejected;
  install (local weights) then benchmark, bad reference and unknown model
  as envelopes; upgrade → approve → uninstall blocked (rollback target) →
  forced → serving primary retire refused → 404 on unknown; role explain
  incl. contract checks and 404; projects add/list/duplicate/remove/404;
  **idempotency** replay (no second mutation), conflict, oversize key, and a
  replayed refusal; **reload refused** before anything happens; mutating
  calls audited with the forwarded identity (reads not, auth rejections
  separately); the OpenAPI document lists all 23 operations, every `/v1`
  op secured, every mutating op with 404/409 and its CLI mapping.
- `tests/integration/test_platform_cli.py` (1): refused verbs print the
  shared error object in `--json` mode (404 phrasing, lifecycle refusal,
  unknown role); text mode unchanged.
- PR-8's `test_control_plane.py`: two exact-set assertions on the operation
  list became superset assertions (the surface grew as planned).
- All PR-6 CLI tests pass unmodified through the refactor; PR-1/PR-3
  goldens intact.

## Behavior notes

- **CLI text output byte-identical** for every verb.
- **CLI `--json` output:** `model activate` / `model upgrade` /
  `governance approve` print **one** JSON document
  `{"request": …, "applied": …|null}` instead of two consecutive documents;
  `project add` / `project remove` print JSON in `--json` mode (they printed
  text before); `model install` gains `ok`, `model benchmark` gains `state`
  and `message`, `model catalog` gains `count`/`sources` (listing) and
  `accelerator`/`explain` (recommendations), `model explain` gains `source`
  and `ok`. Refused verbs print `{"error": {...}}` in `--json` mode.
- No compose, Make, `.env`, or registry/generation format change. New state
  directory `config/control/idempotency/` (created on first keyed call).

## Rollback note

`git revert <commit>` restores the inline verb implementations, removes
`api.py`, `deps.py`, `idempotency.py`, `platform_errors.py` and the contract
bump. `config/control/idempotency/` files are inert for older code. Audit
records written by the API (`api.*`) remain readable (same record shape).

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane —
  unavailable here; to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry (additions justified above: the
      operation layer, the reload guard, the `--json` error envelope in the
      CLI, the mutation lock)
- [x] New tests cover the scope; full suite green (526); ruff clean
- [x] Pre-existing tests unmodified except two PR-8 (unmerged) assertions
      widened to supersets; goldens passing
- [x] Architecture docs updated to as-built (CLI_AND_WEBUI_STRATEGY §3/§6,
      CLI_REFERENCE, OPERATION_MANUAL, CURRENT_ARCHITECTURE); ADR-028 PR-9
      slice recorded
- [x] Rollback note present
- [x] No model/vendor names in code (control package H1 grep covers the
      new modules)
