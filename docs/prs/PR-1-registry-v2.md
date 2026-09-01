# PR-1 — Registry v2 store + generations

**Phase:** P1 · **ADR:** ADR-027 (Proposed) · **Size:** M ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-1 ·
**Status:** implemented on `claude/local-ezai-architecture-kc6gcb`

## Scope (as delivered)

- `agentd/src/agentd/registry_v2.py` — platform-scope registry: schema
  (models × lifecycle states, ordered groups, roles with group/pin/
  `requires` contract per MODEL_GOVERNANCE_V2 §3), referential-integrity
  validation, deterministic resolution with explain reasons, resolution-
  completeness check (`resolve_all`), ADR-020-shape `role_map()`,
  generation store (immutable append-only snapshots + atomic
  `registry.yaml` replace), human-readable `diff_generations`.
- `agentd/src/agentd/defaults/reference_registry.yaml` — the CLAUDE.md
  `agent_model_map` as packaged **data** (the sanctioned home for model
  names, ADR-026 R-1/H1).
- **Excluded by design:** all consumers (`prepare_run`, CLI, renderer —
  PR-3/6/7); the live `config/models/registry.yaml` instance (created by
  bootstrap, PR-7); rollback/activation operations (PR-5).

## Design decisions made in this PR

1. **Pin = explicit chain** (no inherited group fallbacks) — forced by the
   golden test; recorded as an as-built refinement in
   MODEL_ROUTING_DESIGN §4.
2. `role_map()` emits fallback entries **only when non-empty**, byte-
   compatible with `apply_model_registry` (ADR-020) semantics.
3. `save_generation` refuses to persist an unservable generation
   (resolution-completeness is a write-time invariant, not advice).
4. Package data lives in `defaults/` (not `data/` — the repo-root
   `.gitignore`'s Docker-volume rule `data/` matches any depth; renaming
   is safer than touching platform ignore rules).

## Tests (19 new; suite 311 → 330, all pre-existing tests unmodified)

- **Golden:** `test_golden_reference_reproduces_claude_md_routing_byte_for_byte`
  — reference data resolution vs the shipped ADR-020 loader on this
  repo's live `.agent/model_registry.yaml`; exact primaries AND exact
  fallback lists. This is the tripwire for PR-6's alias switch.
- Golden completeness: all ten LLM roles (incl. `orchestrator`) resolve.
- Resolution: group order, active-only filtering with reasons, pin chain
  exactness, string-pin normalization, pinned-inactive skipping, unknown
  role, no-active-model loud error, aggregated `resolve_all` failures.
- Integrity: unknown model in group / unknown group in role / unknown pin /
  undeclared provider all rejected.
- Store: YAML round-trip, sequential generations, snapshot immutability,
  unservable-generation write refusal, missing-registry bootstrap hint,
  full diff coverage (model add/remove/state, group order, role
  group/pin, providers).

## Behavior notes

**None.** No existing module, config, or test is touched; the new module
has zero runtime consumers. Rendered/live behavior is byte-identical.

## Rollback note

Pure additive code: `git revert <commit>` fully restores the previous
state. No state migrations, no config changes, no down-migration needed.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane,
  which is not available in this environment — to be attached on a
  stack-connected re-run. Checklist review performed in its place:
- [x] Scope matches the plan entry (deviations: pin semantics + `defaults/`
      dir, both justified above)
- [x] New tests cover the scope; full suite green (330); ruff clean
- [x] Pre-existing tests unmodified
- [x] Architecture doc updated to as-built (MODEL_ROUTING_DESIGN §4 note);
      ADR-027 entered as Proposed
- [x] Rollback note present
- [x] No model/vendor names introduced into **code** (names live in the
      packaged YAML data file and test fixtures — the sanctioned locations)
