# CLI Reference — `local-ezai`

The production interface to Local-EZAI's Autonomous SWE subsystem.
Install: [agentd/INSTALL.md](../agentd/INSTALL.md). All commands drive the
agent roster (Planner, Coder, Validator, Debugger, Browser QA, Reviewer,
Memory, Documentation, Evolution, Sprint, Git).

## Project selection

```bash
local-ezai .                        # bare path → chat for that project
local-ezai /home/test/project/CRM   # any directory as first argument
local-ezai -C /path <command>       # git-style explicit path
local-ezai <command>                # default: current directory
```

## Global options (before or after the command)

`--config FILE` (or `$AGENTD_CONFIG`) · `--verbose` · `--version`

## Commands

| Command | What it does | Writes to your branch? |
|---|---|---|
| `chat` | memory-aware REPL with the local model (`/reset`, `/exit`) | no |
| `plan "<task>"` | execution plan (JSON), traceless dry-run | no |
| `run "<task>" [--push] [--in-place] [--max-iterations N] [--json]` | full pipeline: plan → code → validate → Browser QA → self-heal → commit on worktree branch `swe/<id>` | no (branch) |
| `code "<task>" [--in-place] [--json]` | plan + implement only; changes left **uncommitted** | no (worktree) |
| `test [--json]` | validation in place: lint / **type** / build / test + Browser QA | no |
| `fix [--goal G] [--max-iterations N] [--json]` | repair failing validation in place (VALIDATE → DEBUG → FIX → REVALIDATE, max 10 iterations); commits when green | **yes** (current branch) |
| `review [--json]` | Reviewer Agent over the working-tree diff (or last commit when clean); exit 1 on `request_changes` | no |
| `commit [-m MSG] [--push]` | validate, then commit the working tree — **blocked until validation incl. Browser QA is green** | **yes** |
| `memory [--add TEXT --kind K] [--search TERM] [--limit N]` | inspect / add project memory (`.agent/memory.db`) | no (memory only) |
| `docs [--focus F] [--json]` | Documentation Agent generates/refreshes USER_GUIDE, OPERATION_MANUAL, MAINTENANCE_GUIDE, RELEASE_NOTES (uncommitted) | no (worktree files) |
| `sprint <spec.md> [--simple] [--keep-going] [--max-parallel N] [--push] [--json]` | autonomous sprint: requirement analysis → dependency waves → **parallel** task pipelines → merged commits + committed sprint report on `sprint/<id>` | no (branch) |
| `evolve [--focus F] [--push] [--json]` | evolution cycle: analyze history/failures/bottlenecks → propose → implement → validate → benchmark → **PR / proposal bundle** on `evolve/<id>`; always ends awaiting a human | no (branch) |
| `roadmap [--full]` | show `.agent/roadmap.md` (milestone lines, or the full file) | no |
| `evaluate-models [--json]` | probe every routed model role (incl. fallback chains); record `.agent/model_benchmarks.json` | no (benchmarks file) |
| `version` | print the version | no |

## Exit codes

`0` success · `1` run/validation/review/evolution failed · `2` usage or
workspace error (not a git repo, missing spec/roadmap file) · `3` model
unreachable (chat) · `130` interrupted.

## Legacy CLI

`ezai` (Phases 1–4) remains for scripts and run inspection:
`ezai runs` (list runs) and `ezai journal <run-id>` (pretty-print a run's
event journal) work for runs produced by either CLI.

## Key configuration surfaces

- Global: YAML via `--config`, env `AGENTD_*` (`AGENTD_LLM__BASE_URL`, …)
- Per repo: `.agentd.yaml` (validation commands incl. `type:`, limits,
  browser_qa) — cannot self-grant push
- Per repo: `.agent/model_registry.yaml` — per-role primary/fallback models
- Full reference: [agentd/README.md](../agentd/README.md#configuration)
