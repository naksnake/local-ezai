# agentd — Local Autonomous Software Engineering Runtime (Phase 1 MVP)

`agentd` turns [local-ezai](../README.md) into a minimum viable **autonomous
software engineer**: give it a change request and a git repository, and it
plans the work, edits the code on an isolated branch, runs the project's
tests/lint/build, retries on failures within a budget, and commits (and
optionally pushes) the result — using **only the local models already served
by the stack**.

It is the Phase 1 implementation of the architecture defined in
[docs/TARGET_ARCHITECTURE.md](../docs/TARGET_ARCHITECTURE.md) /
[docs/MIGRATION_PLAN.md](../docs/MIGRATION_PLAN.md), and it changes nothing
about the existing 8-service chat stack (ADR-002: additive evolution).

```
ezai run "fix the off-by-one in pagination and add a regression test" \
    --repo ~/code/myapp
```

```
            ┌────────────────────── LangGraph state machine ─────────────────────┐
request ──► │ plan ──► code (task loop) ──► validate ──► git ──► report          │
            │            ▲                     │ fail (≤ max_fix_attempts)        │
            │            └───── diagnose ◄─────┘                                  │
            └──────────────── every step journaled (JSONL) ──────────────────────┘
                 │                │                │
             Planner           Coding          Validation + Git agents
             (read-only     (fs/edit/grep/     (deterministic: your test,
              repo tools)    exec tools)        lint, build commands + git)
```

---

## Contents

1. [Quick start](#quick-start)
2. [How a run works](#how-a-run-works)
3. [The four agents](#the-four-agents)
4. [CLI reference](#cli-reference)
5. [Configuration](#configuration)
6. [Tool layer & permissions](#tool-layer--permissions)
7. [Safety model & limitations](#safety-model--limitations)
8. [Observability: journal & report](#observability-journal--report)
9. [Development & testing](#development--testing)
10. [Extending](#extending)
11. [Architecture mapping](#architecture-mapping)

---

## Quick start

Requirements: Python ≥ 3.10, git ≥ 2.20, and a running local-ezai stack
(any profile — the runtime talks to LiteLLM's OpenAI-compatible API).

```bash
# from the local-ezai repo root
make swe-install          # creates ./.venv-agentd and installs agentd

# point it at the stack (defaults shown; usually nothing to change)
export AGENTD_LLM__BASE_URL="http://localhost:4000/v1"
export LITELLM_MASTER_KEY="sk-..."          # same key as the stack's .env

# dry run: plan only, nothing is written anywhere (autonomy A0)
.venv-agentd/bin/ezai plan "add a --version flag to the CLI" --repo ~/code/myapp

# full run: worktree branch swe/<run-id>, edits, validation, local commit
.venv-agentd/bin/ezai run "add a --version flag to the CLI" --repo ~/code/myapp

# same, but also push the branch to origin (T3 action, explicit opt-in)
.venv-agentd/bin/ezai run "..." --repo ~/code/myapp --push
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
5. **diagnose** — on failure, the failing evidence becomes a bounded `FIX-n`
   task and the loop returns to **code** (at most
   `limits.max_fix_attempts` times).
6. **git** — the Git Agent stages everything, commits with a deterministic
   (or optionally LLM-written) message, and pushes only if allowed.
7. **report** — a `RunReport` (also saved as `report.json` next to the
   journal) summarizes plan, tasks, validation, commit, and errors.

Every step appends to the run journal (`~/.agentd/runs/<run-id>/journal.jsonl`).

## The four agents

| Agent | Responsibilities (per the Phase 1 requirements) | LLM use | Tools (allowlist) |
|---|---|---|---|
| **Planner** | requirement analysis · task decomposition · execution-plan generation | yes (`planner` role) | `fs_ls`, `fs_read`, `fs_glob`, `code_grep` (all read-only) |
| **Coding** | read repository · modify files · create files · generate tests | yes (`coder` role) | `fs_read`, `fs_write`, `fs_edit`, `fs_ls`, `fs_glob`, `code_grep`, `exec_run`, `git_status`, `git_diff` |
| **Validation** | test execution · build validation · lint validation | no (deterministic harness) | direct command execution with timeouts |
| **Git** | `git add` · `git commit` · `git push` | optional (commit message only, off by default) | `git_status`, `git_diff`, `git_add`, `git_commit`, `git_push` |

Agents exchange **validated envelopes** (`Plan`, `TaskResult`,
`ValidationReport`, `CommitInfo` — see `schemas.py`), never transcripts.
Tool allowlists are enforced by the registry regardless of what a model
asks for; the Planner physically cannot write, the Reviewer-style guarantees
arrive with later phases.

## CLI reference

```
ezai run  <request> --repo PATH [--push] [--in-place] [--config FILE] [--json] [--verbose]
ezai plan <request> --repo PATH [--config FILE] [--verbose]
ezai version
```

| Flag | Meaning |
|---|---|
| `--repo PATH` | Target git repository (must have at least one commit) |
| `--push` | Enable the T3 `git_push` action for this run (default: denied) |
| `--in-place` | Edit the repo directly instead of a worktree (opt-in) |
| `--config FILE` | YAML config file (see below) |
| `--json` | Print the full `RunReport` as JSON |
| `--verbose` | Debug logging |

Exit codes: `0` completed · `1` run failed (see report/journal) ·
`2` workspace error (not a git repo, no commits) · `130` interrupted.

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

limits:
  max_plan_tasks: 8
  max_agent_turns: 24           # per agent invocation
  max_fix_attempts: 2           # validate-fail → fix cycles
  tool_output_max_chars: 8000
  recursion_limit: 150          # LangGraph safety net

validation:
  commands: {}                  # test/lint/build → lists of shell commands
  autodetect: true              # detect pytest/ruff for Python repos
  command_timeout: 600

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
root; its `validation:` and `limits:` sections override the global config
for runs against that repo:

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

Honest statement of the MVP's isolation level (ADR-014):

- **Git worktree isolation**: agents act on a worktree + dedicated branch.
  Your checkout and branches are untouched; rollback = delete the branch.
- **Path containment**: all file tools resolve inside the workspace;
  traversal and symlink escapes are rejected.
- **Fail-closed push**: nothing leaves the machine unless you pass `--push`.
- ⚠️ **`exec_run` and validation commands run as host subprocesses** (with
  timeouts, in the workspace cwd). A malicious or confused model could run
  commands that read host state or reach your LAN. Phase 2 (`sandboxd`)
  moves execution into per-run containers with default-deny egress —
  until then, run agentd as a low-privilege user and only against repos you
  trust it to build.
- No plan-approval gate yet (`ezai plan` is the manual preview); the full
  gate/autonomy machine (A0–A3) arrives in Phase 3.

## Observability: journal & report

- `~/.agentd/runs/<run-id>/journal.jsonl` — append-only event stream
  (ADR-006): `RUN_SUBMITTED`, `STATE_ENTERED`, `AGENT_SPAWNED`, `LLM_CALL`,
  `TOOL_CALLED`/`TOOL_RESULT` (with permission decisions), `CHECK_STARTED`/
  `CHECK_FINISHED`, `PLAN_READY`, `TASK_RESULT`, `VALIDATION`,
  `GIT_DELIVERY`, `RUN_TERMINAL`.
- `~/.agentd/runs/<run-id>/report.json` — the final `RunReport`.
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
│   ├── cli.py                ezai entry point
│   ├── config.py             layered configuration system
│   ├── graph.py              LangGraph orchestration (state machine)
│   ├── runner.py             run assembly (workspace+journal+agents+graph)
│   ├── journal.py            append-only JSONL event journal
│   ├── llm.py                OpenAI-compatible + scripted LLM clients
│   ├── schemas.py            Plan / TaskResult / ValidationReport / CommitInfo
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
│       ├── git_agent.py      Git Agent
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

| This MVP | Target architecture | Phase |
|---|---|---|
| `graph.py` (LangGraph) | Workflow Engine in agentd (WORKFLOW_DESIGN.md) | P3 completes gates/BLOCKED/resume |
| `tools/` + `permissions.py` | toolgw with tiers T0–T4 | P2 adds MCP hub + per-run scoping |
| `workspace.py` (worktrees) | sandboxd execution plane | P2 adds runner containers + egress policy |
| `journal.py` (JSONL) | event-sourced runs (ADR-006) | P1 adds SQLite index + resume |
| 4 agents | AGENT_DESIGN.md roster subset | P4 adds Reviewer/Debugger/Context/Curator |
| `llm.py` roles | model role tiering (ADR-007) | P1 adds LiteLLM `swe-*` aliases per profile |

Decisions introduced by this MVP: **ADR-013** (LangGraph as orchestration
substrate) and **ADR-014** (interim isolation: worktrees + host subprocess
execution until sandboxd) in [.agent/decisions.md](../.agent/decisions.md).
