# PR-5 — Lifecycle: activate / upgrade / rollback / retire + governance queue

**Phase:** P1 · **ADR:** ADR-027 (Proposed, PR-5 slice added) · **Size:** L ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-5 ·
**Depends on:** PR-1 (store, generations, diff), PR-3 (renderer, drift),
PR-4 (states, side-load probes) · **Status:** implemented on
`claude/next-ready-pr-bnq7r3` as the third commit on top of PR-3/PR-4
(both unmerged; each commit reverts independently)

## Scope (as delivered)

- **`agentd/src/agentd/governance.py`** — the **governance queue**: a
  `ChangeRequest` is the approval object (kind, title, who asked, who
  proposed — human or evolution — base generation, the full proposed
  registry, the generation diff, **affected roles** before → after, and
  evidence: benchmarks of every involved model, fit verdicts, the
  capability report, runtime before/after). One YAML per request under
  `<config>/governance/queue/` for its whole life (pending → approved |
  rejected → applied | failed | superseded); every transition appends to
  the **append-only audit log** `<config>/governance/log.jsonl` (who, when,
  what, evidence keys). Rules in code: decisions are made once, on pending
  requests; a rejection needs a reason; the **approval matrix**
  (MODEL_GOVERNANCE_V2 §2) — a change that alters no role's resolution
  and keeps the slot runtime is approved by *policy* on submission
  (audited), everything else waits for a human; the **evolution lane**
  (§5) — `proposed_by: evolution` needs benchmark evidence, at most one
  open proposal, never auto-approved.
- **`agentd/src/agentd/activation.py`** — **proposals**: `activate`
  (into a group at a position, pinned to a role, or as a fallback of its
  declared groups; requires `benchmarked`), `upgrade` (new version takes
  the old one's place in every group and pin; old → `retired`, kept),
  generic `propose` (validates the proposed generation exactly as apply
  will — integrity, completeness, negotiation, slot rule via a dry render
  — and refuses loudly otherwise). **Apply protocol** (MODEL_LIFECYCLE §4):
  approved request → stale check (`superseded` when the registry moved)
  → reconcile → dry render → **save generation N+1** → write artifacts
  (drift-checked) → **reload only changed artifacts** → **health** → on
  *any* failure past the snapshot, **self-rollback**: generation N's
  content saved as a new generation, re-rendered, reloaded; request
  `failed` with the reason. **Rollback** to the previous or a named
  generation: same protocol, no approval, loudly audited, notifies.
  **Reconcile**: a registry ahead of its rendered manifest (crash between
  snapshot and render) is re-rendered and audited. Pluggable
  `Reloader`/`HealthCheck`: `ComposeReloader` (engine `up -d` when the
  override/preset changed, router `restart` when its config changed) and
  `EngineHealth` (descriptor `ready` probe + one completion for a role's
  primary); `None` = render-only and the audit says so.
- **`agentd/src/agentd/lifecycle.py`** — `retire` (active only; blocked
  while primary of any role or the last member a role depends on; the
  self-service case is audited as a generation) and `uninstall` (blocked
  for installed/benchmarked/active; `force` required when a stored
  generation lists the model active — a rollback target; weights removed,
  model dropped from groups and pins).
- **Excluded by design:** CLI verbs `model activate|upgrade|rollback|
  retire|uninstall`, `governance list|show|approve|reject` (PR-6);
  bootstrap generation 1 and cutover (PR-7); the evolution lane *creating*
  proposals (post-V1 N6′ — the queue already enforces its rules);
  git-committing generations (deferred to bootstrap behind an explicit
  flag: committing into an operator's working tree must be a choice);
  Admin Center pages (P4); the evolution-PR and release item kinds (slots
  exist in the schema, nothing creates them here).

## Design decisions made in this PR

1. **History is append-only, including rollbacks.** A rollback (explicit
   or self-) is a *new* generation whose content equals the target. PR-1's
   store bumps from the current number and refuses to overwrite a
   snapshot, so rewinding `registry.yaml` to an older number would
   collide on the next save; a new generation keeps `registry.yaml` the
   latest number and shows what happened in `model history`.
2. **Never write what cannot render.** The proposed generation is
   rendered in memory before the snapshot; a request that stopped being
   renderable (descriptors changed) fails with nothing written.
3. **Any failure past the snapshot self-rolls-back** — health, reload,
   drift, or an unexpected exception (crash injection). The failed
   generation stays in history; the request records the reason and the
   restored generation number.
4. **Approval by policy is explicit and audited.** `requires_approval` is
   computed from affected roles + slot runtime; policy approvals are
   decisions by actor `policy` in the log, never silent.
5. **Proposals carry their evidence** (MODEL_GOVERNANCE_V2 §1: govern
   roles, reveal models): model names appear in the diff and evidence;
   the approval object is the set of roles whose chain changes.
6. **Reload/health are seams, defaulting to render-only until PR-7.**
   Before the cutover nothing serves from the rendered path, so reloading
   would be theatre; the compose reloader and engine health are shipped,
   tested against fakes, and become the defaults at cutover.
7. **Rollback and self-rollback force the render past drift.** An
   activation refuses to overwrite a hand-edited rendered file (PR-3
   drift rule); a rollback is incident response and must not be blocked by
   one — the manual edit is discarded and the new manifest records the
   restored content. Applying a request still honors drift unless the
   caller passes `force_render=True`.

## Tests (22 new; suite 413 → 435, all pre-existing tests unmodified)

- Queue: sequential ids, files, list/get; approve/reject once; rejection
  needs a reason; JSONL audit one record per event with actor + request;
  policy approval; evolution lane (evidence required, ≤1 open, never
  auto-approved); outcomes only on approved requests.
- Proposals: `activate` request content (diff lines, affected roles
  before/after, benchmarks of involved models, fit, capability report,
  runtime before/after; registry untouched); evidence required
  (`installed` refused); unservable proposal refused with the missing
  capability named; unknown group; placements (fallback, role pin,
  position 0 with a group); policy approval for a group no role depends
  on; `upgrade` swaps every group and pin, retires the old, carries
  before/after benchmarks, applies; `affected_roles` added/removed/
  collapsed chains.
- Apply: refused while pending; commits generation 2 + artifacts
  (role alias → new primary, router preset for two served models),
  manifest, request result with changed set; reload only changed
  artifacts + health checked; **failed health → self-rollback** (history
  1,2,3; 3 equals 1; artifacts restored; reloader called twice; request
  failed with reason and restored generation); **crash injection** in the
  reloader; **reconcile** heals a snapshot without artifacts and runs
  inside apply; stale request superseded; unrenderable approved request
  fails before writing.
- Rollback: previous generation restored without approval, notified and
  audited with target; named target; refusals (unknown, current); health
  failure during rollback restores the pre-rollback generation.
- Retire/uninstall guards; compose reloader touches only what changed;
  engine health waits, probes a role, times out per descriptor; H1
  tripwire over the two new modules.

## Behavior notes

- **No existing file changed** except `lifecycle.py` (two new functions
  appended; existing functions untouched). No compose, config, script,
  or schema changes; PR-1 golden and PR-3 goldens byte-identical.
- The queue and log are new files under `config/governance/`; nothing
  reads them yet besides this code.

## Rollback note

`git revert <commit>` restores the previous state: two new modules, two
appended functions, one test file, docs. No migrations. A platform that
already has `config/governance/` keeps plain YAML/JSONL files that
nothing else interprets.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane,
  unavailable here — to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry (deviations justified above: rollback
      as a new generation; git commit of generations deferred; render-only
      default before cutover)
- [x] New tests cover the scope incl. the risk register's crash-injection
      test; full suite green (435); ruff clean
- [x] Pre-existing tests unmodified; goldens passing
- [x] Architecture docs updated to as-built (MODEL_LIFECYCLE §2/§3/§4,
      MODEL_GOVERNANCE_V2 §2/§5); ADR-027 gains the PR-5 slice (Proposed)
- [x] Rollback note present
- [x] No model/vendor names in code (the stack's service names
      `ENGINE_SERVICE`/`ROUTER_SERVICE` are topology constants, spelled
      once each)
