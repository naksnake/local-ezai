# PR-12 — P2 phase close: kill-the-daemon, two concurrent runs, spec freeze

**Phase:** P2 (closes) · **ADR:** ADR-028 → **Accepted** · **Size:** S ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-12 ·
**Depends on:** PR-8..PR-11 · **Status:** implemented on
`claude/next-ready-pr-bnq7r3` as the tenth stacked commit (PR-3..11
unmerged; each commit reverts independently)

## Scope (as delivered)

The four P2 exit criteria of [V1_IMPLEMENTATION_PLAN.md](../V1_IMPLEMENTATION_PLAN.md)
§P2, as tests (`tests/integration/test_phase_p2_close.py`) and one decision:

1. **Parity** — the PR-11 smoke (`test_cli_connected.py`): seven verbs
   identical in JSON and text across direct and connected transports, plus
   a real socket. Unchanged here.
2. **Two long runs supervised concurrently through the API** — two real
   scripted runs on two repositories started through `POST /v1/runs`, both
   active at once, both polled to `completed`, both reports carrying a
   commit and the fix visible in each repo's branch; plus a deterministic
   gated pair: both running simultaneously, one cancelled mid-flight
   (`cancel_requested` → `cancelled` at its next model call, report 404
   naming it), the other released to `completed` with its report.
3. **Kill the daemon** — a **real `ezaid` process** (`python -m
   agentd.control --platform … --port …`) serves the platform on a loopback
   port; the CLI works through it in connected mode (status, an activation
   applied as generation 2 with the forwarded identity); the process is
   **SIGKILLed**; then `local-ezai <repo> plan` succeeds in-process (repo
   work unaffected), `status` falls back to direct on the same platform
   state (generation 2 visible), `--transport connected` fails fast (exit
   2, "control plane unreachable"), and the platform's single audit log
   holds `control.started` + `api.model_activate` but no `control.stopped`
   (killed, not stopped).
4. **Spec published and frozen** — `CONTRACT_VERSION = "1.0.0"`,
   `docs/api/ezaid-openapi.json` regenerated with
   `x-contract-status: frozen at 1.0.0 …`, and a **frozen inventory** of the
   29 operations pinned in the test: a surface change must bump the version
   (additive → minor, breaking → major), regenerate the artifact
   (`make control-spec`) and update the inventory in the same PR.

**Decision recorded (deferred to this PR by PR-10):** the deployment
shapes of the control plane in V1 —
- `make control-up` — the container overlay (`docker-compose.control.yml`):
  model lifecycle, governance, projects, health, audit — everything under
  `config/`; it cannot see the host's repositories.
- `make control-serve` (new) — `ezaid` **on the host** (the agentd venv),
  foreground, `--platform config`: the same API plus SWE runs through
  `/v1/runs`, because it shares the filesystem with the registered
  projects (and `git`, docker for the sandbox).
- Starting the daemon from `make up` stays **opt-in** in V1 (ADR-002:
  nothing in the base stack depends on it; the P5 installer may wire a
  default). CLI connected mode makes either shape transparent.

**ADR-028 → Accepted** with the as-built summary of PR-8..12.

**Excluded:** the three-surface parity harness as a release gate (PR-24);
P3/P4/P5 work (they run in parallel from here); new endpoints or verbs;
overlay mounts.

## Design decisions made in this PR

1. **Real process, real signal.** The kill test spawns the actual entry
   point and SIGKILLs it — no in-process shortcut — because the criterion
   is about the CLI surviving the daemon's death, not about a fixture.
2. **Real runs for the concurrency criterion, gated runs for cancel.**
   Two scripted pipelines on two repositories prove the registry runs them
   in parallel end to end; the gated pair proves the supervision semantics
   (both running, cancel one) without timing luck.
3. **Freeze as an inventory, not just a number.** The tripwire compares
   surfaces; the inventory names every operation of 1.0.0, so a reviewer
   sees exactly what changed when the contract moves.
4. **Run listing order by submission, not by timestamp.** The registry
   ordered "newest first" by a second-resolution timestamp, which made two
   runs submitted within one second sort randomly (a PR-10 test was flaky
   under the full suite); it now lists in reverse submission order
   (recovered records load chronologically by id). Fixed here, in the
   phase close, as the phase's own stability item.

## Tests (4 new; suite 554 → 558)

- `test_kill_the_daemon_leaves_repo_work_unaffected` (real subprocess,
  real socket, SIGKILL, then plan / status / connected fail-fast / audit).
- `test_two_real_runs_supervised_concurrently_through_the_api`.
- `test_two_gated_runs_start_status_report_cancel`.
- `test_contract_is_published_and_frozen_at_1_0_0` (version, status
  string, the 29-operation inventory, committed == live surface).
- PR-8 tripwire regenerated; every earlier test passes unmodified.

## Behavior notes

- `info.version` of the contract is `1.0.0` (was `1.0.0-draft.10`);
  `GET /health` and `/v1/health` report `contract: 1.0.0`.
- `GET /v1/runs` lists by submission order (newest first) — identical to
  before except for runs submitted within the same second, which are now
  stable.
- New Make target `control-serve`; no other compose/Make/`.env` change.

## Rollback note

`git revert <commit>` restores the draft contract version, removes the
phase-close tests, the Make target and the ADR status flip. No state change.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane —
  unavailable here; to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry (additions justified above: the
      deployment-shape decision PR-10 deferred here, the listing-order fix)
- [x] New tests cover the scope; full suite green (558); ruff clean
- [x] Pre-existing tests unmodified; goldens passing
- [x] Architecture docs updated to as-built (CLI_AND_WEBUI §7,
      CURRENT_ARCHITECTURE, OPERATION_MANUAL, README); ADR-028 → Accepted
- [x] Rollback note present
- [x] No model/vendor names in code
