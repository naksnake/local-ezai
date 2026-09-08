# Local-EZAI — Self-Evolution Guide

How the platform improves itself — and improves the projects it works on —
without a human in the loop until the merge decision.

## 1. The evolution cycle

```
local-ezai evolve [--focus "..."] [--push]
```

```
ANALYZE HISTORY ─→ ANALYZE FAILURES ─→ IDENTIFY BOTTLENECKS
      └────────────── evidence ──────────────┘
                        ↓
                 PROPOSE (Evolution Agent, ≤3 improvements)
                        ↓
                 BENCHMARK (before: timed full validation)
                        ↓
                 IMPLEMENT (full pipeline per improvement:
                 plan → code → validate → self-heal → commit)
                        ↓
                 BENCHMARK (after) + RELEASE NOTES
                        ↓
                 PULL REQUEST (or PR_PROPOSAL.md bundle)
                        ↓
              ██ HUMAN APPROVAL — the cycle always stops here ██
```

Everything happens on a disposable `evolve/<id>` worktree branch; your
checkout is never touched.

### Evidence gathering (deterministic)

- **History:** implementation records and architecture decisions from
  project memory (`.agent/memory.db`), plus outcomes of recent runs
  (`~/.agentd/runs/*/report.json`).
- **Failures:** failed-fix records, with **repeated failure signatures
  flagged** (`x2: <signature>` — the strongest evolution signal: the same
  error keeps coming back). The proposal rules forbid re-proposing a
  failed experiment — memory is the guard.
- **Benchmark & model trends** (`.agent/model_benchmarks.json`): per-role
  probe state with drift vs the previous evaluation (REGRESSED /
  recovered / latency deltas) plus run-quality rates (planning, coding,
  validation, debugging, review approval) — measured weaknesses are
  first-class evolution targets.
- **Bottlenecks:** roadmap head (`.agent/roadmap.md`) + failure clustering.

### Proposal

The Evolution Agent (read-only tools, `evolution` model role) returns a
schema-validated `EvolutionProposal`: history summary, failure patterns,
bottlenecks, and 1–3 concrete improvements with acceptance criteria.
Journaled as `EVOLUTION_PROPOSAL`.

### Implementation & benchmark

Each improvement runs the **full autonomous pipeline** in place on the
evolution branch, sequentially (later improvements see earlier ones).
Timed full validation runs before and after (`EVOLUTION_BENCHMARK`);
when green, a dated entry is prepended to `docs/RELEASE_NOTES.md` and
committed.

### Delivery

- `--push` + `git.allow_push: true` pushes the branch.
- `forge.kind: gh` → `gh pr create`; `forge.kind: api` → REST call
  (GitHub/Gitea-compatible, token from `$FORGE_TOKEN`).
- `forge.kind: none` (default) → `PR_PROPOSAL.md` bundle in the run dir
  with title, body, and exact push/PR instructions.

The report ends `awaiting human review`. **Evolution never merges** —
see [GOVERNANCE.md](GOVERNANCE.md).

## 2. What feeds evolution: the learning loop

Every terminal run deterministically records to project memory:

| Outcome | Recorded as | Effect on future runs |
|---|---|---|
| Fix attempt failed | `failed_fix` (error signature + approach) | the same approach for the same signature triggers `MEMORY_REPEAT_WARNING` — never repeated blindly |
| Repair succeeded | `successful_fix` | suggested to the Debugger for similar signatures |
| Run completed | `implementation` | history context for Planner + Evolution |
| Human teaching | `project_rule` / `coding_style` / `architecture_decision` via `local-ezai memory --add` | injected into planning |

Lessons export to human-readable `.agent/lessons_learned.json`.

## 3. Model self-governance

Routing lives in `.agent/model_registry.yaml` (per repo):

```yaml
agent_model_map:
  planner:   {primary: hermes3,     fallback: deepseek-r1}
  coder:     {primary: qwen3-coder, fallback: deepseek-r1}
  debugger:  {primary: deepseek-r1, fallback: hermes3}
  reviewer:  {primary: llama3}
  documentation: {primary: llama3}
  memory:    {primary: hermes3}
  evolution: {primary: deepseek-r1}
```

- Fallbacks engage automatically at request time (journaled as
  `LLM_FALLBACK`); exhausting a chain fails the call loudly. Which model
  actually served each stage of a run: `local-ezai explain-run`; the
  standing routing: `local-ezai models`.
- `local-ezai evaluate-models` probes every role (JSON roles must return
  parseable JSON), measures latency, aggregates run-history quality
  metrics, and rolls a trend history into `.agent/model_benchmarks.json` —
  the evidence file for routing PRs (`--report` renders
  [MODEL_GOVERNANCE_REPORT.md](MODEL_GOVERNANCE_REPORT.md)).
- Changing routing is **model replacement** → human-approved PR
  ([GOVERNANCE.md](GOVERNANCE.md)).

## 4. Self-hosting: Local-EZAI evolving Local-EZAI

The repo root carries `.agentd.yaml` wiring the platform's own lint and
test suite, so the platform is a first-class target of its own agents:

```bash
local-ezai . test      # validates itself (ruff + 260+ tests)
local-ezai . fix       # self-heals its own red suite
local-ezai . docs      # regenerates its own guides
local-ezai . evolve    # proposes its own next improvement → PR
local-ezai . roadmap   # reads its own milestones
```

### Bootstrap exit (CLAUDE.md)

Claude Code was the bootstrap engineer. The exit condition —
`Human → Roadmap → Local-EZAI` — holds when a human can drive improvement
using only the platform:

1. Human edits `.agent/roadmap.md` (or writes a `sprint.md`).
2. `local-ezai . sprint sprint.md` or `local-ezai . evolve --focus <goal>`.
3. Platform plans, implements, validates, self-heals, documents,
   benchmarks, and opens the PR.
4. Human reviews and merges.

Every step in that loop is implemented and tested offline
(`make swe-test`). The remaining dependency on humans is exactly the one
that must remain: **approval**.

## 5. Boundaries (by design)

- ≤3 improvements per cycle — small, reviewable PRs.
- Sequential implementation — no parallel self-modification.
- Red validation ⇒ no release notes, no delivery; branch left for autopsy.
- No merge, no deploy, no self-granted push — ever.
