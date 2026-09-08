# Local-EZAI — Review Process

Since Phase H2 (ADR-022) the Reviewer Agent is a **mandatory gate in the
execution pipeline** — nothing is committed without passing adversarial
review, on agent pipelines and on `local-ezai commit` alike.

## 1. Where review sits in the workflow

```
PLAN → CODE → VALIDATE ──failed──► DEBUG → FIX → REVALIDATE ─┐
                 ▲                                           │
                 └───────────────────────────────────────────┘
                 │ green (commands + type + build + tests
                 ▼        + Browser QA)
              REVIEW  ──blocked──► RUN FAILS (structured report)
                 │ approved
                 ▼
              COMMIT ──(opt-in)──► PUSH / PR
```

Validation — including Browser QA — must be green before review runs: the
reviewer judges working code, and expensive review cycles are never spent
on changes that don't pass their own checks. The gate applies to `run`,
`fix`, `sprint` (every task), `evolve` (every improvement), and `commit`.

## 2. What the reviewer examines

The Reviewer Agent is **read-only by construction** (it cannot edit; its
findings are its only output). It receives the run's full uncommitted diff
(`git diff HEAD`), the goal, and the project's persisted rules and coding
styles from memory, plus read tools (`fs_read`, `code_grep`,
`code_symbols`, `git_diff`) to examine surrounding code.

Every finding is classified:

| Category | Examples |
|---|---|
| **security** | injection, secrets in code, unsafe deserialization, missing auth, disabled TLS |
| **architecture** | layering violations, new tight coupling, circular deps, bypassed abstractions, broken contracts |
| **maintainability** | duplicated logic, dead code, misleading names, missing tests, unjustified complexity |
| correctness | logic errors, edge cases, broken invariants |
| performance / testing / style / other | the rest |

Check-weakening (deleted/skipped/bent tests, swallowed errors) and real
security vulnerabilities are always `high` severity.

## 3. Blocking policy

```yaml
review:
  enabled: true                 # the gate is mandatory by default
  block_severities: ["high"]    # findings that block even under approve
```

A commit is **blocked** when:

- the verdict is `request_changes`, **or**
- any finding is at a blocking severity (default: `high`), even if the
  overall verdict is `approve`.

A blocked run **fails** — it does not silently deliver, and it does not
loop (review findings are judgment, not a failing check for the
self-healing loop). The branch/worktree is preserved for inspection.
Turning the gate off is a global-config decision only; a repo's
`.agentd.yaml` cannot disable its own review gate.

## 4. Structured review reports

Every gate outcome is recorded three ways:

- **`report.json`** — the run report carries the full `ReviewReport`
  (verdict, summary, findings with severity/category/file/line/suggestion).
- **Journal** — `REVIEW` (the agent's outcome) and `REVIEW_GATE`
  (verdict, blocked, finding count, categories, blocking reason).
- **CLI** — a blocked run prints the reason; `local-ezai review` runs the
  same reviewer standalone over your working tree at any time.

## 5. Handling a blocked commit

1. Read the findings: `ezai journal <run-id>` or the run's `report.json`.
2. Fix the underlying issue (yourself, or `local-ezai fix` /
   `local-ezai run` with a sharper task).
3. Re-run — the pipeline validates and reviews again from scratch.
4. If the finding is a false positive, adjust project memory (the reviewer
   respects persisted `project_rule` / `coding_style` records) — e.g.
   `local-ezai memory --add "raw subprocess use is accepted in tools/" --kind project_rule`.

## 6. Relation to human review

The reviewer gate raises the floor; it does not replace human judgment.
Branch merges and PR approvals remain human decisions
([GOVERNANCE.md](GOVERNANCE.md)) — the gate ensures that what reaches a
human has already survived validation **and** an adversarial pass.
