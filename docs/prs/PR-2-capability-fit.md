# PR-2 — Capability vector + classes + fit()

**Phase:** P1 · **ADR:** ADR-027 (Proposed, unchanged) · **Size:** S ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-2 ·
**Status:** implemented on `claude/local-ezai-architecture-kc6gcb`

## Scope (as delivered)

- `agentd/src/agentd/capability.py`:
  - `CapabilityVector` — accelerator **kind** (cuda/rocm/igpu/none — API
    families, never brands), accel/system memory, cores, SIMD flags, disk.
  - `classify(vector)` — pure threshold mapping to
    `accel-large | accel-small | cpu-standard | cpu-low`; undetectable
    memory errs conservative (`cpu-low`).
  - `PROFILE_PRESETS` + `class_for_profile` — legacy profile names
    (`gpu/cpu/n97/n97-igpu`) survive **only** as aliases (H4); `gpu`
    asserts detection rather than a class.
  - `fit(model, vector)` — pure, conservative placement verdict over a
    Registry v2 `ModelEntry`: accelerator/system/none/unknown placement,
    speed band, tight-headroom warnings, `overridable=True` always
    (fit informs; it never blocks).
  - best-effort detection with injectable helpers; pure parsers for
    meminfo/cpuinfo.
- `registry_v2.ModelEntry` gains **optional** `size_gb` (declared artifact
  bytes; filled at install time by PR-4) — the only sizing input `fit()`
  accepts.
- **Excluded by design:** consumers (renderer PR-3, recommender/lifecycle
  PR-4, installer PR-21); persisting detected vectors; any engine/runtime
  awareness.

## Agnosticism notes (requirements 4–6)

- **No model-family assumptions:** sizing comes exclusively from declared
  `size_gb`; unknown size yields an honest `unknown` verdict, never a
  name-based guess (tested).
- **No runtime assumptions:** the module never learns what serves a model;
  test fixtures use a fictional `provider: anyruntime`.
- **Hardware tool names** exist only in the declarative
  `ACCELERATOR_PROBES` table — detection must invoke each kind's canonical
  driver interface. This table is the second sanctioned location (beside
  runtime descriptors) and must be allowlisted by the H1 CI gate (PR-25);
  recorded as an as-built note in HARDWARE_AGNOSTIC_ARCHITECTURE §2.

## Tests (14 new; suite 330 → 344, all pre-existing tests unmodified)

- Classification: four classes from fixture vectors (H3), 16 GB
  boundaries, conservative unknown-memory behavior.
- Presets: `n97`/`n97-igpu` → `cpu-low`, `cpu` → `cpu-standard`, `gpu`
  → measured (H4); unknown profile rejected.
- fit(): accelerator-fast, system-spill warning, no-SIMD slow,
  tight-headroom warning, doesn't-fit advisory (overridable), and the
  unknown-size honesty test.
- Detection: pure parser fixtures; injected-probe assembly; a host smoke
  test that only asserts *classifiability*, never a specific host shape.
- Registry: `size_gb` round-trip + default (additive schema proof).
- **Golden test: untouched and passing** — resolution behavior is
  byte-for-byte unchanged (this PR adds no resolution logic).

## Behavior notes

**None.** No consumer exists; no existing module behavior changes. The
`ModelEntry.size_gb` field is optional with default `0.0` — existing
registry data (including the packaged reference set) loads unchanged.

## Rollback note

Pure additive code plus one optional schema field: `git revert <commit>`
fully restores the previous state. No data migrations (no persisted
registry instance exists yet; generations written by reverted code remain
loadable since the field was optional).

## Self-review

- `local-ezai . review` requires the model plane (unavailable in this
  environment) — to be attached on a stack-connected re-run. Checklist
  review performed:
- [x] Scope matches the plan entry (deviation: `size_gb` schema addition,
      justified — fit() needs declared data to avoid model-family
      heuristics)
- [x] New tests cover the scope; full suite green (344); ruff clean
- [x] Pre-existing tests unmodified; golden test passing
- [x] Architecture doc updated (HARDWARE_AGNOSTIC_ARCHITECTURE §2 as-built
      note on the probe-table carve-out)
- [x] Rollback note present
- [x] No model/vendor names in code (accelerator *kinds* + the sanctioned
      probe table only; fixtures use synthetic names)
