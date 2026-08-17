# Local-EZAI — Governance

**Autonomous does not mean uncontrolled. Agents propose. Humans approve.**
(CLAUDE.md — the constitution of this repository.)

## 1. Decision rights

| Decision | Who decides | Mechanism |
|---|---|---|
| Architecture migrations | human | reviewed PR + ADR in `.agent/decisions.md` |
| Core runtime changes (graph, tools, permissions, gates) | human | reviewed PR + ADR |
| Model replacement / routing changes | human | reviewed PR editing `.agent/model_registry.yaml`, evidenced by `evaluate-models` benchmarks |
| Production releases | human | release PR per [MAINTENANCE_GUIDE.md §5](MAINTENANCE_GUIDE.md) |
| **Pull request merges — all of them** | human | no agent has merge capability, by construction |
| Plans, code, fixes, docs, proposals | agents | delivered on `swe/*` / `sprint/*` / `evolve/*` branches for review |

## 2. Enforcement — where the code says no

Governance here is not policy text; it is enforced by the runtime:

- **No merge capability exists.** The Git Agent can `add`/`commit` and
  (opt-in) `push` a branch. There is no merge tool, and `forge.py` only
  *opens* pull requests. Evolution runs end at
  `awaiting human review` — terminally.
- **Commit gate:** commits are refused (`COMMIT_BLOCKED`) until validation
  — lint, type, build, test, **and Browser QA** — is green. Applies to
  agents and to `local-ezai commit` equally.
- **Fail-closed tools:** every agent has an explicit tool allowlist
  (`permissions.py`); an unlisted tool is denied and journaled. Risk tiers
  T0 (read) → T3 (push). There is no T4 (merge/deploy) implementation.
- **Push is double-gated:** `git.allow_push` config AND per-run `--push`.
  A repo's own `.agentd.yaml` **cannot self-grant push** — only
  validation/limits/browser_qa keys are merged from repo config.
- **Checkout safety:** agents work in disposable git worktrees on their
  own branches; your working tree and current branch are never touched.
- **Bounded autonomy:** self-healing stops at 10 iterations or 3 identical
  failure signatures; structured-output retries are bounded; every
  subprocess has a timeout.

## 3. Audit trail

Every run is event-sourced: `~/.agentd/runs/<id>/journal.jsonl` records
each agent invocation, tool call, permission decision, validation result,
healing iteration, memory write, fallback model use, and terminal state —
plus a structured `report.json`. Inspect with `ezai journal <run-id>`.
Decisions the platform learns (`.agent/memory.db`) and decisions humans
make (ADRs) are both versioned in the repo.

## 4. Change workflow (humans and agents alike)

```
propose (branch/PR) → validate (CI + local-ezai test) → review (human,
optionally aided by `local-ezai review`) → approve → merge (human) → tag
```

Requirements for merge: green CI, docs updated in the same PR, ADR when a
decision deviates from or extends the target architecture.

## 5. Evolution governance

Self-improvement (`local-ezai evolve`, [SELF_EVOLUTION_GUIDE.md](SELF_EVOLUTION_GUIDE.md))
follows the strictest loop: evidence → proposal → implementation →
validation → **benchmark (before/after)** → release notes → PR — and
stops. The PR (or `PR_PROPOSAL.md` bundle when no forge is configured)
is the terminal artifact. Rejecting it is one `git branch -D`.

## 6. Security posture & known limits

- Secrets stay in `.env` / `FORGE_TOKEN` env — never in configs or code.
- ⚠️ **ADR-014 (interim):** agent commands run as host subprocesses inside
  the worktree (path containment + timeouts, no container sandbox yet).
  Until sandboxd ships (M2), run the CLI as a low-privilege user, only on
  repos whose build commands you trust.
- Model plane is local-only by default; no code leaves the machine unless
  you configure push/forge remotes.
