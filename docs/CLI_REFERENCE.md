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
| `run "<task>" [--push] [--in-place] [--max-iterations N] [--json]` | full pipeline: plan → code → validate → Browser QA → self-heal → **review gate** → commit on worktree branch `swe/<id>` | no (branch) |
| `code "<task>" [--in-place] [--json]` | plan + implement only; changes left **uncommitted** | no (worktree) |
| `test [--json]` | validation in place: lint / **type** / build / test + Browser QA | no |
| `fix [--goal G] [--max-iterations N] [--json]` | repair failing validation in place (VALIDATE → DEBUG → FIX → REVALIDATE, max 10 iterations); review gate, then commit when green | **yes** (current branch) |
| `review [--json]` | Reviewer Agent over the working-tree diff (or last commit when clean); exit 1 on `request_changes` | no |
| `commit [-m MSG] [--push]` | validate, then commit the working tree — **blocked until validation incl. Browser QA is green AND the reviewer gate approves** | **yes** |
| `memory [--add TEXT --kind K] [--search TERM] [--limit N]` | inspect / add project memory (`.agent/memory.db`) | no (memory only) |
| `docs [--focus F] [--json]` | Documentation Agent generates/refreshes USER_GUIDE, OPERATION_MANUAL, MAINTENANCE_GUIDE, RELEASE_NOTES (uncommitted) | no (worktree files) |
| `sprint <spec.md> [--simple] [--keep-going] [--max-parallel N] [--push] [--json]` | autonomous sprint: requirement analysis → dependency waves → **parallel** task pipelines → merged commits + committed sprint report on `sprint/<id>` | no (branch) |
| `evolve [--focus F] [--push] [--json]` | evolution cycle: analyze history/failures/bottlenecks → propose → implement → validate → benchmark → **PR / proposal bundle** on `evolve/<id>`; always ends awaiting a human | no (branch) |
| `roadmap [--full]` | show `.agent/roadmap.md` (milestone lines, or the full file) | no |
| `evaluate-models [--json] [--report]` | probe every routed model role (incl. fallback chains); aggregate run-history quality metrics; record `.agent/model_benchmarks.json` (with trend history); `--report` writes `docs/MODEL_GOVERNANCE_REPORT.md` | no (benchmarks file) |
| `models [--json]` | show the live model routing: primary + fallback per agent role, from `.agent/model_registry.yaml` | no |
| `explain-run [run-id] [--json]` | which model handled each stage (planning/coding/debugging/review/…) of a run — fallback-aware; defaults to the project's latest run | no |
| `version` | print the version | no |

## Platform namespaces (V1 · PR-6, direct mode)

These verbs act on the **platform's** declarative state — Registry v2 +
generations, runtime descriptors, catalog, governance queue under
`config/` — not on a repository. The platform is found via
`platform.config_dir` in the agentd config, `$AGENTD_PLATFORM__CONFIG_DIR`,
or by running inside the local-ezai checkout. Every mutation persists a
generation and an audit record; `--json` is available everywhere.

| Command | What it does | Approval? |
|---|---|---|
| `model install <ref> [--name N] [--runtime R] [--group G] [--refetch]` | `<ref>` = `hf:<org/repo>` · `gguf:<url \| hf://org/repo/file.gguf \| path>` · catalog id · `auto` (needs `--group`); fetch → side-load validation → `installed` / `failed` | no (audited) |
| `model benchmark <name> [--base-url URL]` | tokens/sec on this host (side-loaded engine, or the live one) → recorded on the entry + `.agent/model_benchmarks.json`; → `benchmarked` | no |
| `model activate <name> [--group G] [--position N] [--role R] [--reload]` | change request: primary of a group (`--position 0`, default with `--group`), pinned to a role, or fallback of its declared groups; policy-approved and applied at once when no serving role changes | **yes** when a serving role changes |
| `model upgrade <old> <new> [--reload]` | change request swapping versions in every group and pin; old version retired (kept) | **yes** |
| `model rollback [--to-generation N] [--reason R] [--reload]` | restore the previous or a named generation as a new generation; notifies | no (audited) |
| `model retire <name>` | remove a non-serving active model from resolution (a serving primary or last member is refused) | no |
| `model uninstall <name> [--force]` | delete weights of a retired/failed model; `--force` when a stored generation could roll back to it | no |
| `model explain <role>` | what serves the role and why (generation, pin/group, skips) + the role contract check per model | — |
| `model history [--limit N]` | generations with notes and diffs | — |
| `model catalog [--group G] [--runtime R]` | catalog entries, or ranked recommendations for a group with fit verdicts (F9) | — |
| `governance list [--status S]` · `show <id>` | the approval queue and one request's evidence (diff, affected roles, benchmarks) | — |
| `governance approve <id> [--reason R] [--by NAME]` | approve a pending request **and apply it** (render → reload → health → self-rollback on failure) | human act |
| `governance reject <id> --reason R` | reject with a recorded reason | human act |
| `project add <path> [--name N]` · `list` · `remove <name\|path>` | the chat-ops project allowlist (`config/projects.yaml`) | no (audited) |
| `status` | platform root, capability class, generation vs rendered generation, slot runtime, model states, pending approvals, engine/router health | — |
| `up [--profile gpu\|cpu\|n97\|n97-igpu] [--rendered]` · `down [--profile P]` | `docker compose` wrappers over the profile files; `--rendered` adds `config/rendered/docker-compose.engine.yml` | — |

`--reload` (activate/upgrade/rollback/approve) reloads consumers and
health-checks the new generation through the descriptor's probes; without it
the generation is rendered only (the default until the cutover, PR-7).
`--by NAME` sets the actor recorded in `config/governance/log.jsonl`
(default `$USER`).

## Exit codes

`0` success · `1` run/validation/review/evolution failed, or a platform
operation refused (guard, failed install, rejected proposal) · `2` usage or
workspace error (not a git repo, missing spec/roadmap file, no platform
found) · `3` model unreachable (chat) · `130` interrupted.

## Legacy CLI

`ezai` (Phases 1–4) remains for scripts and run inspection:
`ezai runs` (list runs) and `ezai journal <run-id>` (pretty-print a run's
event journal) work for runs produced by either CLI.

## Key configuration surfaces

- Global: YAML via `--config`, env `AGENTD_*` (`AGENTD_LLM__BASE_URL`, …)
- Global only: `sandbox:` (execution sandbox — [SANDBOX_GUIDE.md](SANDBOX_GUIDE.md)),
  `review:` (reviewer gate — [REVIEW_PROCESS.md](REVIEW_PROCESS.md)),
  `code_intel:` ([CODE_INTELLIGENCE.md](CODE_INTELLIGENCE.md))
- Per repo: `.agentd.yaml` (validation commands incl. `type:`, limits,
  browser_qa) — cannot self-grant push, weaken the sandbox, or disable the
  review gate
- Per repo: `.agent/model_registry.yaml` — per-role primary/fallback models
  (a role it declares is taken exactly as declared)
- Routing layers (PR-6): code defaults are **role aliases** (`role-planner`,
  …) → the platform's Registry v2 seeds concrete primaries + fallback chains
  when a platform is attached (`platform.config_dir` /
  `$AGENTD_PLATFORM__CONFIG_DIR` / inside the checkout) → the per-repo
  registry overrides per role. `local-ezai models` shows the effective
  result and its sources.
- Full reference: [agentd/README.md](../agentd/README.md#configuration)
