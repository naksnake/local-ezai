# agentd — Local Autonomous Software Engineering Runtime

`agentd` turns [local-ezai](../README.md) into a minimum viable **autonomous
software engineer**: give it a change request and a git repository, and it
plans the work, edits the code on an isolated branch, runs the project's
tests/lint/build, **debugs its own failures down to the root cause** and
self-heals within a bounded iteration budget, then commits (and optionally
pushes) the result — using **only the local models already served by the
stack**.

It implements Phases 1–7 of the architecture defined in
[docs/TARGET_ARCHITECTURE.md](../docs/TARGET_ARCHITECTURE.md) /
[docs/MIGRATION_PLAN.md](../docs/MIGRATION_PLAN.md), and it changes nothing
about the existing 8-service chat stack (ADR-002: additive evolution).

```
cd ~/code/myapp
local-ezai run "fix the off-by-one in pagination and add a regression test"
```

```
        ┌──────────────── LangGraph state machine (self-healing) ─────────────┐
request │  PLAN ──► CODE (task loop) ──► VALIDATE ──passed──► GIT ──► SUCCESS │
   ────►│                                   │ failed                          │
        │                                   ▼                                 │
        │            RCA engine ──────►  DEBUG   (root cause, not symptom)    │
        │        (error categorization,    │                                  │
        │         signatures, stalls)      ▼                                  │
        │                                 FIX    (Coding Agent applies the    │
        │                                   │     diagnosed strategy)         │
        │                                   ▼                                 │
        │                              REVALIDATE ──► loop, max 10 iterations │
        │                                             + stall detection       │
        └───────────────── every step journaled (JSONL) ──────────────────────┘
```

---

## Contents

1. [Quick start](#quick-start)
2. [How a run works](#how-a-run-works)
3. [The agents](#the-agents)
4. [Self-healing: Debug Agent + RCA engine](#self-healing-debug-agent--rca-engine)
5. [Browser QA: real-browser validation](#browser-qa-real-browser-validation)
6. [Project memory: the Memory Agent](#project-memory-the-memory-agent)
7. [CLI reference](#cli-reference)
6. [Configuration](#configuration)
7. [Tool layer & permissions](#tool-layer--permissions)
8. [Safety model & limitations](#safety-model--limitations)
9. [Observability: journal & report](#observability-journal--report)
10. [Development & testing](#development--testing)
11. [Extending](#extending)
12. [Architecture mapping](#architecture-mapping)

---

## Quick start

Requirements: Python ≥ 3.10, git ≥ 2.20, and a running local-ezai stack
(any profile — the runtime talks to LiteLLM's OpenAI-compatible API).
**Cross-platform: Linux, macOS, Windows** — full instructions incl. pipx
and Windows PowerShell in [INSTALL.md](INSTALL.md).

```bash
# from the local-ezai repo root (Linux dev setup; see INSTALL.md for pipx/Windows)
make swe-install          # creates ./.venv-agentd and installs agentd
. .venv-agentd/bin/activate

# point it at the stack (defaults shown; usually nothing to change)
export AGENTD_LLM__BASE_URL="http://localhost:4000/v1"
export LITELLM_MASTER_KEY="sk-..."          # same key as the stack's .env

cd ~/code/myapp                     # commands work on the current directory
local-ezai plan "add a --version flag"   # dry run — plan only, traceless
local-ezai run  "add a --version flag"   # full pipeline on branch swe/<id>
local-ezai run  "..." --push             # also push the branch (opt-in)

local-ezai /home/test/project/CRM        # a bare path opens chat there
local-ezai sprint sprint28.md            # run a whole markdown spec
```

The deliverable is a **branch** (`swe/<run-id>`) in your repository — your
checkout, current branch, and uncommitted changes are never touched.
Inspect and merge it like any other branch:

```bash
git -C ~/code/myapp log --oneline swe/<run-id>
git -C ~/code/myapp diff main...swe/<run-id>
```

**Model expectations.** Autonomy quality is gated by the local model
(see TARGET_ARCHITECTURE §1). On the GPU profile (7B+, coder-class models
recommended) multi-file tasks work; on the N97 profile (1.5B) keep requests
to single-file changes and expect more failed runs — a failed run is a
journaled, budgeted, clean outcome, not a hang.

> Tip: the stack's auto-RAG hook injects knowledge-base excerpts into every
> LiteLLM chat completion. That is designed for chat; for agent runs you can
> avoid the extra context by pointing agentd directly at the engine:
> `AGENTD_LLM__BASE_URL=http://localhost:8000/v1` (any profile — the engine
> always serves the OpenAI API on :8000).

## How a run works

1. **Workspace.** A git worktree is created at
   `~/.agentd/workspaces/<run-id>` on branch `swe/<run-id>` (from the repo's
   HEAD). `--in-place` opts out for throwaway repos.
2. **plan** — the Planner Agent explores the repo read-only and emits a
   schema-validated plan (bounded number of tasks; every task must declare
   how it is verified).
3. **code** — the Coding Agent implements one task at a time through the
   tool loop (read → edit → optionally spot-check with `exec_run`).
4. **validate** — the Validation Agent runs the project's configured
   `test` / `lint` / `build` commands (see [Configuration](#configuration));
   verdicts come from exit codes, never from model claims.
5. **debug → fix → revalidate** (self-healing) — on failure, the RCA engine
   categorizes every failing check, the Debug Agent identifies the root
   cause and emits a structured `DebugReport` with a fix strategy, the
   Coding Agent applies it as a `HEAL-n` task, and validation re-runs.
   Bounded by `limits.max_heal_iterations` (default **10**) plus stall
   detection (identical failure signature `stall_threshold` times in a row →
   abort instead of patching symptoms).
6. **git** — the Git Agent stages everything, commits with a deterministic
   (or optionally LLM-written) message, and pushes only if allowed.
7. **report** — a `RunReport` (also saved as `report.json` next to the
   journal) summarizes plan, tasks, validation, healing iterations, commit,
   and errors.

Every step appends to the run journal (`~/.agentd/runs/<run-id>/journal.jsonl`).

## The agents

| Agent | Responsibilities | LLM use | Tools (allowlist) |
|---|---|---|---|
| **Planner** | requirement analysis · task decomposition · execution-plan generation | yes (`planner` role) | `fs_ls`, `fs_read`, `fs_glob`, `code_grep` (all read-only) |
| **Coding** | read repository · modify files · create files · generate tests · apply diagnosed fixes | yes (`coder` role) | `fs_read`, `fs_write`, `fs_edit`, `fs_ls`, `fs_glob`, `code_grep`, `exec_run`, `git_status`, `git_diff` |
| **Validation** | test execution · build validation · lint validation | no (deterministic harness) | direct command execution with timeouts |
| **Debug** (Phase 2) | reproduce failures · root-cause identification · structured debugging reports · fix-strategy generation | yes (`debugger` role) | read-only + reproduce: `fs_read`, `fs_ls`, `fs_glob`, `code_grep`, `exec_run`, `git_diff`, `git_status` |
| **Browser QA** (Phase 3) | launch application · run real user workflows · validate pages · detect console errors · capture screenshots · generate validation reports | no (deterministic Playwright harness) | app subprocess + headless Chromium |
| **Memory** (Phase 4) | persist architecture decisions, coding styles, project rules, failed/successful fixes, implementation history · learn from debugging attempts, validation failures, successful repairs | optional (distillation only, off by default) | SQLite store in the repo's `.agent/` |
| **Reviewer** (Phase 5) | adversarial diff review: correctness, regressions, check-weakening, scope creep, style-rule violations (from memory) | yes (`reviewer` role) | read-only: `fs_read`, `fs_ls`, `fs_glob`, `code_grep`, `git_status`, `git_diff` |
| **Sprint** (Phase 6) | requirement analysis · task breakdown · dependency graph for parallel multi-agent execution | yes (`sprint` role) | read-only: `fs_ls`, `fs_read`, `fs_glob`, `code_grep` |
| **Documentation** (Phase 7) | generate/refresh `docs/USER_GUIDE.md`, `OPERATION_MANUAL.md`, `MAINTENANCE_GUIDE.md`, `RELEASE_NOTES.md` (left uncommitted for review) | yes (`documentation` role) | `fs_read`, `fs_ls`, `fs_glob`, `code_grep`, `fs_write`, `fs_edit`, `git_status`, `git_diff` |
| **Evolution** (Phase 7) | analyze run history, failure patterns, bottlenecks · propose ≤3 concrete improvements (implemented by the full pipeline, delivered as a PR — never merged) | yes (`evolution` role) | read-only: `fs_read`, `fs_ls`, `fs_glob`, `code_grep`, `git_status`, `git_diff` |
| **Git** | `git add` · `git commit` · `git push` — **blocked until validation incl. Browser QA succeeds** | optional (commit message only, off by default) | `git_status`, `git_diff`, `git_add`, `git_commit`, `git_push` |

Agents exchange **validated envelopes** (`Plan`, `TaskResult`,
`ValidationReport`, `DebugReport`, `CommitInfo` — see `schemas.py`), never
transcripts. Tool allowlists are enforced by the registry regardless of what
a model asks for: the Planner physically cannot write — and neither can the
Debug Agent, which is the point (see below).

## Self-healing: Debug Agent + RCA engine

When validation fails, the run does not blindly retry — it debugs itself:

1. **RCA engine** (`rca.py`, fully deterministic): categorizes every failing
   check into `syntax / import / assertion / exception / timeout /
   environment / lint / build / unknown` via ordered regex rules, extracts
   the exception, message, and `file:line` locations, computes a stable
   **error signature**, and seeds a category-appropriate fix strategy.
   Journaled as `RCA_REPORT`.
2. **Debug Agent**: receives the failing evidence, the RCA output, and the
   history of previous iterations (so it never repeats a failed strategy).
   It reproduces the failure (`exec_run`), reads the code and diff, traces
   the symptom back to its origin, and emits a **structured debugging
   report** (`DebugReport`): root cause, category, confidence,
   `why_root_cause` (cause-vs-symptom justification), evidence, and a
   concrete `fix_strategy` (approach / steps / files / risk).
3. **Fix**: the Coding Agent — not the Debug Agent — applies the strategy as
   a `HEAL-n` task whose prompt carries the root cause and the hard rules
   (never weaken or delete tests, never silence errors).
4. **Revalidate**: the same validation harness re-runs; the outcome closes
   the iteration's `HealingIteration` observability record.

**Root cause, not symptom — enforced structurally, not just by prompt:**

- the Debug Agent is **read-only**: it cannot "fix" anything by hacking the
  workspace; its only output is a diagnosis that must justify itself
  (`why_root_cause` is part of the schema);
- **stall detection**: if the identical failure signature survives
  `stall_threshold` (default 3) consecutive validations, the run aborts with
  a "no progress" verdict — symptom-patching cannot loop;
- **hard iteration cap**: at most `max_heal_iterations` (default **10**)
  DEBUG→FIX→REVALIDATE cycles per run, enforced by the graph, not the model;
- a **failed fix attempt does not abort the run** — the next debugging
  iteration sees it in history and must explain what it missed.

## Browser QA: real-browser validation

When a repo declares `browser_qa:` in its `.agentd.yaml`, every validation
pass (and every REVALIDATE of the self-healing loop) additionally:

1. **launches the application** from the workspace on a free port
   (`{port}` substitution, HTTP readiness polling, log capture, clean
   process-group teardown — a fresh launch per validation, so revalidation
   always tests the *edited* code);
2. **runs the declared user workflows** in a real headless Chromium via
   Playwright — e.g. login, create customer, update customer, delete
   customer (see [examples/browser-qa.customer-crud.yaml](examples/browser-qa.customer-crud.yaml)
   for a complete, tested configuration);
3. **validates pages** with auto-retrying `expect_*` assertions;
4. **detects console errors** (`console.error` and uncaught page errors) on
   every page of every workflow;
5. **captures screenshots** — on explicit `screenshot` steps and
   automatically on failure — into `~/.agentd/runs/<run-id>/browser-qa/`;
6. **generates a validation report**: per-workflow step results, console
   errors, screenshots, and app log tail, merged into the run's
   ValidationReport and persisted in `report.json`.

**Failure rules (all three enforced):** validation is FAILED when a browser
step fails, when workflow verification (`expect_*`) fails, **or when any
console error exists** — even if every step succeeded. A configured-but-
unusable stage (Playwright missing, app won't start, invalid workflow spec)
is also a failure, never a silent skip.

**Git commits are blocked until Browser QA succeeds** — twice over: the
workflow route only reaches the git state on a fully green validation, and
the Git Agent itself refuses (journaling `COMMIT_BLOCKED`) if handed a
failing validation. Browser failures feed the same RCA → DEBUG → FIX →
REVALIDATE self-healing loop as any other check (category `browser`), so
the runtime can fix a UI bug it discovered in the browser and only then
commit.

Step vocabulary: `goto`, `click`, `fill`, `select`, `expect_text`,
`expect_no_text`, `expect_visible`, `expect_url`, `expect_title`,
`wait_for`, `screenshot`.

Setup: `pip install -e './agentd[browser]'` and `playwright install
chromium` (or `make swe-install && make swe-browsers`). In environments
with a pre-provisioned browser, the harness falls back to
`$PLAYWRIGHT_BROWSERS_PATH/chromium` automatically; pin one explicitly with
`browser_qa.chromium_executable`. `ignore_console_patterns` (regex list)
can whitelist known-noisy console errors — the default is strict.

Fail-fast ordering: command checks run first; the app is only launched on
code that passes them (the skipped stage still counts as *not succeeded*,
so commits stay blocked).

## Project memory: the Memory Agent

Every repository the runtime works on accumulates **persistent memory** in
its own `.agent/` directory:

- **`.agent/memory.db`** — SQLite store, the single source of truth
- **`.agent/lessons_learned.json`** — human-readable export, regenerated
  after every recorded run

Six kinds of knowledge are persisted: `architecture_decision`,
`coding_style`, `project_rule`, `failed_fix`, `successful_fix`,
`implementation` (history).

**What it learns, automatically and deterministically, from every run:**

- every **debugging attempt** — a DEBUG→FIX→REVALIDATE iteration whose
  revalidation still failed becomes a `failed_fix` memory carrying the root
  cause, the attempted approach, the error signature, and the category;
- every **successful repair** becomes a `successful_fix` memory;
- every run (completed or failed, including **validation-failure** aborts)
  leaves an `implementation` history record: goal, status, files changed,
  iterations, commit, final error.

Curated knowledge is added explicitly (`ezai remember`, below) or — when
`memory.distill: true` — distilled by the LLM from completed runs (up to 3
durable observations, restricted to the curated kinds).

**How memory feeds back into the workflows:**

- **Planning**: the Planner's prompt carries project rules, coding styles,
  architecture decisions, lessons relevant to the request (keyword search),
  and recent implementation history (journaled as `MEMORY_INJECTED`).
- **Debugging**: the Debug Agent is shown fix approaches that **already
  failed for this exact error signature in previous runs** (with the
  explicit instruction not to repeat them and to explain what they missed)
  and repairs that previously succeeded on this kind of failure.
- **Repeat-mistake detection** (deterministic): if a new diagnosis proposes
  an approach effectively identical (normalized word-set similarity) to one
  that already failed for the same signature, the runtime journals
  `MEMORY_REPEAT_WARNING` and stamps a warning into the fix task.

**Placement and hygiene:** memory lives in the **origin repository's**
`.agent/` — outside run worktrees — so it persists across runs and branches
and can never ride along in a run's diff; in in-place mode the Git Agent
additionally excludes `memory.db*` / `lessons_learned.json` from staging.
The store is lazily created (reading leaves zero traces; `ezai plan` stays
traceless). Recommended: add `.agent/memory.db*` to the repo's `.gitignore`;
`lessons_learned.json` may be committed deliberately if the team wants the
knowledge in review. Disable entirely with `memory.enabled: false`.

## CLI reference

The production CLI is **`local-ezai`** (Phase 5). It selects the project by
path — `local-ezai <path> <command>`, git-style `-C <path>`, or simply the
current directory — and a bare path opens chat:

```
local-ezai [PATH | -C PATH] COMMAND ...

local-ezai .                                # chat for the current project
local-ezai /home/test/project/CRM           # chat for that project
local-ezai chat                             # interactive session (memory-aware)
local-ezai plan "<task>"                    # execution plan (dry-run, traceless)
local-ezai run  "<task>" [--push] [--in-place] [--max-iterations N] [--json]
local-ezai code "<task>" [--in-place]       # plan + implement; NO commit
local-ezai test [--json]                    # validation (commands + Browser QA)
local-ezai fix  [--goal G] [--max-iterations N]   # repair in place, commit when green
local-ezai review [--json]                  # adversarial diff review (Reviewer Agent)
local-ezai commit [-m MSG] [--push]         # validate, then commit (gated)
local-ezai memory [--add TEXT --kind K] [--search TERM] [--limit N]
local-ezai sprint <spec-file> [--keep-going] [--push] [--json]
local-ezai docs [--focus F]                 # Documentation Agent → 4 repo guides
local-ezai evolve [--focus F] [--push]      # evolution cycle → PR proposal
local-ezai roadmap [--full]                 # show .agent/roadmap.md milestones
local-ezai evaluate-models [--report]       # probe roles + quality metrics + trends
local-ezai models                           # live routing: primary/fallback per role
local-ezai explain-run [run-id]             # which model handled each stage
local-ezai version
```

Command → agents: `plan`/`code` → Planner (+Coder); `run`/`sprint` → the
full roster; `test` → Validator + Browser QA; `fix` → Validator + RCA +
Debugger + Coder + Git (enters the workflow at VALIDATE — no planning);
`review` → Reviewer (reads the working-tree diff, or the last commit when
clean); `commit` → Validator + Browser QA + Git (the commit gate applies);
`memory`/`chat` → Memory; `docs` → Documentation; `evolve` → Evolution +
the full roster (delivery via the `forge:` config — PR or proposal
bundle, always awaiting human approval). Model routing per role comes
from `.agent/model_registry.yaml` (primary + fallback chains), verified
by `evaluate-models` → `.agent/model_benchmarks.json`.

### Autonomous sprint execution (Phase 6)

`local-ezai sprint sprint.md` is a full multi-agent collaboration:

1. **Requirement analysis + task breakdown + dependency graph** — the
   **Sprint Agent** reads the spec, explores the repo read-only, and emits
   a validated `SprintPlan` (requirements, self-contained tasks, explicit
   `depends_on` edges). DAG integrity — unique ids, known deps, no cycles —
   is checked by code and structurally broken graphs are sent back to the
   model for correction.
2. **Wave scheduling** (deterministic): tasks are grouped into topological
   waves; tasks inside a wave are independent.
3. **Parallel agent execution**: a multi-task wave runs each task in its
   own worktree branched from the sprint tip, concurrently (bounded by
   `sprint.max_parallel`, `--max-parallel`); completed task branches merge
   back into the shared `sprint/<id>` branch in plan order (a merge
   conflict fails that task — the dependency graph is what should keep
   parallel tasks on disjoint files). Single-task waves run directly on the
   sprint worktree.
4. Every task is the **full pipeline** — Planner, Coder, **Validation**,
   **Browser QA**, self-healing, Memory, and the commit gate — so nothing
   merges without passing its checks; tasks whose dependencies failed are
   skipped.
5. **Documentation**: `docs/sprints/sprint-<id>.md` (goal, requirements,
   mermaid dependency graph, per-task outcomes incl. validation summaries)
   is generated and, when the sprint is green, committed as the final
   commit on the sprint branch.

Output: implementation + tests (enforced per task) + documentation +
commits, all on one branch. `--simple` skips the analysis and runs
checklist items sequentially (the Phase 5 behavior); `--keep-going`
continues scheduling after failures (dependents of failed tasks are always
skipped).

Exit codes: `0` success · `1` run/validation/review/evolution failed ·
`2` usage or workspace error · `3` model unreachable (chat) · `130`
interrupted.

The Phase 1–4 `ezai` CLI (`run/plan/runs/journal/remember/memory` with
`--repo`) remains available for scripts; `ezai runs` and `ezai journal`
are the run-inspection tools for both CLIs.

Installation and packaging (pipx, pip venvs on Linux/macOS/**Windows**,
wheel builds): **[INSTALL.md](INSTALL.md)**.

## Configuration

Precedence: **defaults < YAML file < `AGENTD_*` environment variables**.
The file is passed with `--config` or `AGENTD_CONFIG`. Environment variables
nest with `__`: `AGENTD_LIMITS__MAX_FIX_ATTEMPTS=3`.

```yaml
# agentd.yaml — all keys optional; defaults shown
llm:
  provider: openai              # openai | scripted (offline replay)
  base_url: http://localhost:4000/v1
  api_key: ""                   # empty → uses $LITELLM_MASTER_KEY
  temperature: 0.2
  max_tokens: 4096
  timeout: 180
  retries: 2
  roles:                        # role → model alias (ADR-007)
    default: qwen2.5-7b
    planner: qwen2.5-7b         # e.g. a larger instruct model on GPU boxes
    coder: qwen2.5-7b           # e.g. qwen2.5-coder-1.5b on the N97 profile
    validator: qwen2.5-7b
    git: qwen2.5-7b
    debugger: qwen2.5-7b        # give this the strongest reasoning model you have

limits:
  max_plan_tasks: 8
  max_agent_turns: 24           # per agent invocation
  max_heal_iterations: 10       # DEBUG→FIX→REVALIDATE cycles per run (hard cap)
  stall_threshold: 3            # identical failure signature N× in a row → abort
  tool_output_max_chars: 8000
  recursion_limit: 150          # LangGraph safety net

validation:
  commands: {}                  # test/lint/build → lists of shell commands
  autodetect: true              # detect pytest/ruff for Python repos
  command_timeout: 600

browser_qa:                     # usually set per-repo in .agentd.yaml
  enabled: false
  app:
    start: "python3 app.py"     # {port} substituted; $PORT exported
    url: "http://127.0.0.1:{port}"
    ready_path: "/"
    startup_timeout: 30
  workflows: []                 # see examples/browser-qa.customer-crud.yaml
  headless: true
  step_timeout: 10
  chromium_executable: ""       # empty → managed browser w/ env fallback
  ignore_console_patterns: []   # strict by default: any console error fails

memory:
  enabled: true
  dir: ".agent"                 # inside the ORIGIN repo (not the worktree)
  max_context_items: 5          # records per section in prompts
  distill: false                # LLM-distilled observations after green runs

git:
  branch_prefix: "swe/"
  remote: origin
  allow_push: false             # T3 — CLI --push flips this per run
  llm_commit_message: false
  user_name: agentd
  user_email: agentd@local-ezai

workspace:
  mode: worktree                # worktree | in-place
  root: ~/.agentd/workspaces

runs_dir: ~/.agentd/runs
log_level: INFO
```

**Per-repository overrides** — a target repo may carry `.agentd.yaml` at its
root; its `validation:`, `limits:`, and `browser_qa:` sections override the
global config for runs against that repo (never `git:` — a repo cannot
self-grant push):

```yaml
# myapp/.agentd.yaml
validation:
  commands:
    test: ["python -m pytest -q"]
    lint: ["ruff check ."]
    build: []
```

If no commands are configured and autodetection finds none, validation
passes **with an explicit warning** in the report — a green run never
silently means "nothing was checked".

## Tool layer & permissions

Every capability is a `Tool` (name, JSON schema, risk tier) executed through
the single `ToolRegistry` gate: allowlist check → permission policy → run →
output cap → journal. Errors reach the model as data, never as crashes.

| Tool | Tier | Notes |
|---|---|---|
| `fs_read`, `fs_ls`, `fs_glob`, `code_grep` | T0 read-only | path-contained to the workspace |
| `fs_write`, `fs_edit` | T2 mutate-workspace | `fs_edit` = exact-match, unique-occurrence replacement |
| `exec_run` | T2 | shell in workspace cwd, wall-clock timeout, output caps |
| `git_status`, `git_diff` | T0 | |
| `git_add`, `git_commit` | T2 | identity via `-c user.name/email` (your git config is untouched) |
| `git_push` | **T3** | denied unless `git.allow_push` / `--push` (fail-closed, ADR-008) |

Policy summary: T0–T2 allowed inside the workspace; T3 requires explicit
enablement; T4 (host-visible/destructive) has no tools and unknown tools are
denied by default. Every decision is journaled.

## Safety model & limitations

The hardened isolation level (ADR-021, superseding the ADR-014 interim):

- **Git worktree isolation**: agents act on a worktree + dedicated branch.
  Your checkout and branches are untouched; rollback = delete the branch.
- **Path containment**: all file tools resolve inside the workspace;
  traversal and symlink escapes are rejected.
- **Execution sandbox** (`sandbox:` config, docs/SANDBOX_GUIDE.md): every
  `exec_run`/validation/debug/benchmark command passes one executor —
  regex **command allowlist** (fail-closed when configured), execution in
  a **disposable Docker container** whenever `sandbox.image` is set and
  the daemon answers (workspace-only mount at the host-identical path,
  `--network none` default, memory/cpu/pids limits, explicit env
  passthrough), and a per-run **audit log**
  (`~/.agentd/runs/<id>/exec_audit.jsonl`). Without an image or Docker,
  execution falls back to timeboxed host subprocesses (allowlist + audit
  still apply) — there, run agentd as a low-privilege user and only
  against repos you trust it to build.
- **Mandatory reviewer gate** (ADR-022, docs/REVIEW_PROCESS.md): after
  green validation, the Reviewer examines the full uncommitted change set
  (untracked files included); `request_changes` or high-severity findings
  (security / architecture / maintainability taxonomy) block the commit —
  in every pipeline and in `local-ezai commit`.
- **Fail-closed push**: nothing leaves the machine unless you pass `--push`.
- No plan-approval gate yet (`local-ezai plan` is the manual preview); the
  full gate/autonomy machine (A0–A3) remains future work.

## Observability: journal & report

- `~/.agentd/runs/<run-id>/journal.jsonl` — append-only event stream
  (ADR-006): `RUN_SUBMITTED`, `STATE_ENTERED` (states `PLAN`, `CODE`,
  `VALIDATE`, `DEBUG`, `FIX`, `REVALIDATE`, `GIT`), `AGENT_SPAWNED`,
  `LLM_CALL`, `TOOL_CALLED`/`TOOL_RESULT` (with permission decisions),
  `CHECK_STARTED`/`CHECK_FINISHED`, `PLAN_READY`, `TASK_RESULT`,
  `VALIDATION`, `RCA_REPORT` (categories, signature, stall flag),
  `DEBUG_REPORT` (root cause, confidence, approach), `FIX_APPLIED`,
  `HEAL_ITERATION` (per-cycle outcome), `BROWSER_QA_STARTED`,
  `BROWSER_WORKFLOW` (per-workflow verdict, console-error count,
  screenshots), `BROWSER_QA`, `COMMIT_BLOCKED`, `GIT_DELIVERY`,
  `RUN_TERMINAL`, and memory events `MEMORY_INJECTED`,
  `MEMORY_REPEAT_WARNING`, `MEMORY_RECORDED`, `MEMORY_DISTILLED`
  (memory bookkeeping may follow `RUN_TERMINAL`).
- `~/.agentd/runs/<run-id>/report.json` — the final `RunReport`, including
  the full `healing` history (one record per DEBUG→FIX→REVALIDATE cycle:
  signature, categories, root cause, confidence, fix status, revalidation
  outcome) and `iterations_used`.
- `ezai runs` lists recent runs with status/iterations/branch; `ezai journal
  <run-id>` pretty-prints the event stream.
- Console logging is operator-facing (`--verbose` for debug); the journal is
  the machine-readable truth.

## Development & testing

```bash
make swe-install     # venv + editable install with dev extras
make swe-test        # unit + integration tests (no network, no models)
make swe-lint        # ruff
```

The test suite runs **fully offline**: a `ScriptedLLM` replays canned
model responses, integration tests drive the real graph end-to-end against
throwaway git repos (real edits, real `git`, real validation subprocesses).
The same mechanism is available at runtime (`llm.provider: scripted`) for
demos and debugging.

Layout:

```
agentd/
├── pyproject.toml            packaging, pytest, ruff
├── src/agentd/
│   ├── main_cli.py           local-ezai — the production CLI (Phase 5)
│   ├── sprint.py             sprint-spec parsing + dependency graph (DAG)
│   ├── sprint_exec.py        autonomous sprint executor (parallel waves,
│   │                         merge-back, sprint documentation)
│   ├── cli.py                ezai entry point (run/plan/runs/journal)
│   ├── config.py             layered configuration system
│   ├── graph.py              LangGraph orchestration (self-healing machine)
│   ├── browser_qa.py         Browser QA engine (Playwright harness + app launcher)
│   ├── memory.py             project memory: SQLite store, lessons export,
│   │                         retrieval renderers, repeat detection
│   ├── rca.py                Root Cause Analysis engine (deterministic)
│   ├── runner.py             run assembly (workspace+journal+agents+graph)
│   ├── journal.py            append-only JSONL event journal
│   ├── llm.py                OpenAI-compatible + scripted LLM clients
│   ├── schemas.py            Plan / TaskResult / ValidationReport /
│   │                         DebugReport / HealingIteration / CommitInfo
│   ├── permissions.py        risk tiers + fail-closed policy
│   ├── workspace.py          git worktree management + path containment
│   ├── logging_setup.py
│   ├── tools/                tool abstraction layer
│   │   ├── base.py           Tool, ToolResult, ToolRegistry
│   │   ├── filesystem.py     fs_read/write/edit/ls/glob
│   │   ├── search.py         code_grep
│   │   ├── shell.py          exec_run (+ shared run_command)
│   │   └── git.py            git_status/diff/add/commit/push
│   └── agents/
│       ├── base.py           shared tool-calling loop + structured output
│       ├── planner.py        Planner Agent
│       ├── coder.py          Coding Agent
│       ├── validator.py      Validation Agent
│       ├── debugger.py       Debug Agent (read-only root-cause analysis)
│       ├── browser_qa.py     Browser QA Agent (deterministic harness)
│       ├── memory_agent.py   Memory Agent (learning + optional distillation)
│       ├── reviewer.py       Reviewer Agent (adversarial diff review)
│       ├── sprint_agent.py   Sprint Agent (requirements → task DAG)
│       ├── git_agent.py      Git Agent (commit gate)
│       └── prompts/          versioned role prompts (*.md)
└── tests/
    ├── unit/                 per-module tests
    └── integration/          full-graph runs on fixture repos
```

## Extending

- **New tool**: subclass `Tool` in `tools/`, assign a tier, add it to
  `ALL_TOOL_CLASSES`, and add its name to the allowlist of the agents that
  may use it. The registry, permissions, journaling and truncation come for
  free.
- **New agent**: subclass `BaseAgent` (set `agent_name`, `role`,
  `tool_names`), give it a prompt file in `agents/prompts/`, and wire a node
  + edges in `graph.py`. Per ADR-010, new states/agents need an ADR entry.
- **Different models per role**: map roles in `llm.roles` — e.g. point
  `coder` at `qwen2.5-coder-1.5b` on the N97 profile (the model is already
  pre-wired in the stack's `.env.example`).

## Architecture mapping

| This runtime | Target architecture | Remaining gap |
|---|---|---|
| `graph.py` (LangGraph, self-healing loop) | Workflow Engine (WORKFLOW_DESIGN.md) — DEBUG/FIX/REVALIDATE realizes the inner APPLY/CHECK/DIAGNOSE loop | gates/BLOCKED/journal-replay resume |
| `rca.py` + `agents/debugger.py` | Debugger agent + failure taxonomy (AGENT_DESIGN §3.6) | — (pulled forward, ADR-015) |
| `browser_qa.py` + `agents/browser_qa.py` | UI-level verification in the VERIFYING state | — (pulled forward, ADR-016) |
| `memory.py` + `agents/memory_agent.py` | procedural/episodic memory layers + Memory Curator (TARGET §6) | Qdrant semantic layer (codeidx) |
| `tools/` + `permissions.py` | toolgw with tiers T0–T4 | MCP hub + per-run scoping |
| `workspace.py` (worktrees) | sandboxd execution plane | runner containers + egress policy (ADR-014 interim) |
| `journal.py` (JSONL) + `ezai runs/journal` | event-sourced runs (ADR-006) | SQLite index + resume |
| `main_cli.py` (local-ezai) | `ezai` CLI interface (TARGET §10) | web console, chat-ops MCP tools |
| `sprint_exec.py` (parallel waves) | sub-agent scheduling / multi-agent collaboration (TARGET §4, AGENT_DESIGN §4) | in-run sub-agent spawning |
| 9 agents | AGENT_DESIGN.md roster subset | Context/Research agent |
| `llm.py` roles | model role tiering (ADR-007) | LiteLLM `swe-*` aliases per profile |

Decisions introduced by this runtime: **ADR-013** (LangGraph as orchestration
substrate), **ADR-014** (interim isolation: worktrees + host subprocess
execution until sandboxd), **ADR-015** (self-healing workflow: read-only
Debug Agent + deterministic RCA engine + bounded iterations + stall
detection), **ADR-016** (Browser QA: declarative Playwright harness,
console-error strictness, commit gate), and **ADR-017** (project memory:
SQLite in the origin repo's `.agent/`, deterministic learning, repeat-
mistake detection) in [.agent/decisions.md](../.agent/decisions.md).
