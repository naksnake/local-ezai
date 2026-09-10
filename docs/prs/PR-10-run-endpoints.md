# PR-10 — Run endpoints: async run registry (control plane, phase P2)

**Phase:** P2 · **ADR:** ADR-028 (Proposed; PR-10 slice recorded) · **Size:** M ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-10 ·
**Depends on:** PR-9 (dependencies, envelope, `resolve_project` allowlist),
PR-8 (audit log), the existing pipelines (`runner`, `sprint_exec`,
`evolution`), the project allowlist (PR-6) ·
**Status:** implemented on `claude/next-ready-pr-bnq7r3` as the eighth
stacked commit (PR-3..9 unmerged; each commit reverts independently)

## Scope (as delivered)

- **`agentd/control/runs.py` — the run registry.** `RunRegistry.submit`
  starts a job on a bounded worker pool and returns its record at once;
  the job executes the platform's **existing** pipeline for its kind —
  `run` → `execute_run`, `fix` → `heal_run`, `sprint` →
  `run_sprint_autonomous` (or `run_sprint` with `simple`), `evolve` →
  `run_evolution`, `plan` → `plan_only` (the A0 dry-run) — with the job's id
  as the run id, so run directories, journals and reports are the same
  artifacts the CLI produces. Records (`RunRecord`: kind, project, request,
  actor, status queued|running|completed|failed|cancelled, timestamps,
  error, report/journal paths, options) persist as one JSON file per run
  under `config/control/runs/` and are **recovered on restart** —
  a job that was queued or running when the daemon stopped is marked
  `failed` with the reason (`run.orphaned` audited).
- **Cancellation without touching the core graph.** A queued job is
  cancelled immediately. A running job's model client is wrapped by
  `CancellableLLM`: the pipeline stops at its **next model call**
  (`RunCancelled` unwinds through the pipelines' own `finally` blocks;
  journal intact, memory store closed), the record ends `cancelled`.
  Subprocesses already running (validation, git) finish on their own.
- **Concurrency limits** (`control.max_concurrent_runs` = 2,
  `control.max_queued_runs` = 8): beyond workers + queue a start is refused
  with `429 too_many_runs`; at most one **in-place** job (`fix`) per
  project at a time (`409 project_busy`) — worktree kinds may overlap.
- **The chat-ops ceiling, enforced here:** a run starts only on a
  **registered project** (`resolve_project` over `config/projects.yaml`;
  otherwise `404 not_found` with the fix `local-ezai project add`), and
  `configure_job` forces `git.allow_push = False` for every job — the API
  never pushes (OPENWEBUI_INTEGRATION §5).
- **`agentd/control/runs_api.py` — six operations:** `POST /v1/runs`
  (`202`, `RunStart`: kind, project name|path, `task` for run/plan, `goal`
  for fix, `spec` markdown for sprint, `focus` for evolve; options `simple`,
  `keep_going`, `in_place`, `max_iterations`, `max_parallel`) ·
  `GET /v1/runs[?kind&status&project&limit]` (newest first, active count,
  limits) · `GET /v1/runs/{id}` (record + journal progress: events, last
  event, last time) · `GET /v1/runs/{id}/report` (`409 report_pending`
  while unfinished; `404` when it ended without one, error quoted) ·
  `GET /v1/runs/{id}/journal?tail=N` (bounded excerpt) ·
  `POST /v1/runs/{id}/cancel` (`409 run_finished` afterwards). Submissions,
  outcomes and cancellations are audited (`run.submitted` / `run.finished` /
  `run.cancel_requested`, actor = forwarded identity) next to the
  middleware's `api.run_start` / `api.run_cancel`.
- **Contract `1.0.0-draft.10`** (29 operations), artifact regenerated.
- **Excluded (later PRs):** CLI connected mode (PR-11); spec freeze, the
  kill-the-daemon and two-concurrent-runs phase-close tests (PR-12); the
  SWE tool server that calls these endpoints (PR-13); Admin Center run
  pages (P4); hard-killing a running subprocess; coordination with runs the
  direct-mode CLI starts on the same repository.

## Design decisions made in this PR

1. **Reuse the pipelines, own only the lifecycle.** The registry adds no
   pipeline logic; every job is the same function the CLI calls with the
   same config, and the CLI's artifacts (journal, `report.json`) are what
   the API serves. Reports for kinds whose pipeline returns a model without
   writing one (`plan`, `sprint`, `evolve`) are persisted by the registry
   as `report.json` in the run directory — one layout for every kind.
2. **Cooperative cancellation through the model client.** The pipelines
   already accept an `llm`; wrapping it is the one seam that stops a run
   between model calls without changing `graph.py` (a core-runtime change
   would need its own approval under CLAUDE.md). The record says so: a
   running cancel is "at the next model call".
3. **`plan` is a run kind.** It is the A0 dry-run of `run` (same runner,
   same journal); the P3 tool catalog needs it (`swe_plan`), and excluding
   it would have forced a second, sync code path there.
4. **Refusals are recorded outcomes, limits are refusals.** A failing
   pipeline is a `failed` record with the error (a fact about the run); a
   full queue is a `429` (a fact about the daemon). Both are audited.
5. **Deployment constraint stated, not papered over.** A job needs the
   project on the same filesystem as the daemon (plus `git`, and docker for
   the sandbox). The shipped container mounts only `config/`; the run
   endpoints are therefore usable where `ezaid` runs on the host (the
   venv's `ezaid`) or where the projects directory is mounted into the
   container at the same path (the overlay carries the commented hint).
   PR-12/P3 decide the default; nothing in this PR changes the overlay's
   behavior.

## Tests (11 new; suite 526 → 537)

- `tests/unit/test_control_runs.py`:
  **the real scripted pipeline through the daemon** — `POST /v1/runs` →
  202 within the request, polled to `completed`, report with `swe/` branch
  and commit, the fix visible in git, journal (full and bounded tail),
  list, persisted record, cancel-after-finish 409, audit records; **plan
  job** → the plan as report and zero traces in the repo; refusals:
  unregistered project 404 with the fix, missing `task` / `spec` 422,
  unknown kind 422, unknown run 404; **concurrency** (1 worker + 1 queued:
  running → queued → 429, the queued job takes the freed slot);
  **in-place exclusivity** (second `fix` 409 naming the busy run; a `run`
  may overlap); **cancel** (queued → cancelled now and never started;
  running → `cancel_requested`, report pending 409, stops at the next model
  call → `cancelled`, then 409/404); **report pending then written by the
  registry** with options persisted; failed pipeline → recorded error, 404
  report naming it, audited; **restart recovery** (running → failed with
  "restarted", completed untouched, audited, unknown kind refused);
  `CancellableLLM` (forwards, raises after cancel) and `configure_job`
  (push forced off, options applied, `in_place` only for `run`); the six
  run operations in the contract (202/429/409, secured, CLI mapping on
  start).
- PR-9's `test_control_api.py`: the exact operation set gained the six run
  operations; the CLI-mapping check skips `run_cancel` (the Admin Center's
  asymmetry per CLI_AND_WEBUI §3).
- PR-8 tripwire regenerated; PR-1/PR-3 goldens intact; all CLI tests
  unmodified.

## Behavior notes

- No CLI output changes. No compose, Make or `.env` change; the overlay
  gains a commented volume hint only.
- New state directory `config/control/runs/` (one JSON per job). Run
  directories, journals and reports keep their layout under `runs_dir`.
- Two new config keys with defaults (`control.max_concurrent_runs`,
  `control.max_queued_runs`).

## Rollback note

`git revert <commit>` removes the registry, the endpoints and the config
keys; `config/control/runs/*.json` files are inert for older code; run
directories written by jobs are ordinary runs (`ezai runs` lists them).

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane —
  unavailable here; to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry (additions justified above: the `plan`
      kind, the allowlist + no-push enforcement, restart recovery, the
      report persistence for non-run kinds)
- [x] New tests cover the scope; full suite green (537); ruff clean
- [x] Pre-existing tests unmodified except two PR-9 (unmerged) assertions
      extended for the grown surface; goldens passing
- [x] Architecture docs updated to as-built (OPENWEBUI_INTEGRATION §2,
      CLI_AND_WEBUI_STRATEGY §3, CLI_REFERENCE, OPERATION_MANUAL,
      CURRENT_ARCHITECTURE); ADR-028 PR-10 slice recorded
- [x] Rollback note present
- [x] No model/vendor names in code (control package H1 grep covers the
      new modules)
