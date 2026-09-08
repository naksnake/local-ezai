# PR-4 — Lifecycle: install / validate / benchmark

**Phase:** P1 · **ADR:** ADR-027 (Proposed, PR-4 slice added) · **Size:** L ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-4 ·
**Depends on:** PR-1 (registry, generations), PR-2 (`fit()`), PR-3
(descriptors, `materialize_service`) · **Status:** implemented on
`claude/next-ready-pr-bnq7r3` as a second commit on top of PR-3 (PR-3 was
not yet merged; each commit reverts independently)

## Scope (as delivered)

- **`agentd/src/agentd/fetch.py`** — the source **resolver matrix**
  (`parse_source`): `hf:<org/repo>`, `gguf:<https url>`,
  `gguf:hf://<org/repo>/<file.gguf>` (rewritten to the hub's resolve URL),
  `gguf:<local path>`, `<catalog id>`, `auto`; every other form fails with
  the accepted forms listed (F8). **Fetchers** put weights where the
  runtime descriptor mounts them: `GGUFFetcher` streams with HTTP-Range
  resume and SHA-256 while streaming (mismatch ⇒ download discarded;
  local files hard-linked/copied into the weights dir; present-and-verified
  files reused), `HFFetcher` delegates to `hf download` — the local CLI
  when present, otherwise the identical throwaway-container invocation of
  `scripts/download-models*.sh` — and measures the snapshot.
- **`agentd/src/agentd/catalog.py` + `defaults/catalog.yaml`** — catalog as
  **pluggable data**: packaged seed merged with `<config>/catalog/*.yaml`
  (later sources override by id). Entries declare groups, context, chat
  template, tool-call format, license, and per-format variants with
  declared sizes — never a brand. Seed sizes were read from the hub API on
  2026-09-08 (seven entries covering the stack's existing profile defaults
  and the CLAUDE.md reference set; curated per-class content is P5).
  **Recommender** (`recommend` / `recommend_one`, R-5): variants a runtime
  serves × the group's role contracts (`render.check_contract`) × `fit()`
  placement; ranked accelerator-before-system, then declared size, then
  context; every candidate is returned with its verdict (F9) and a
  no-fit answer explains each rejection.
- **`agentd/src/agentd/lifecycle.py`** — the **state machine as data**
  (`TRANSITIONS`, enforced by `transition()`); **`install()`**: resolve
  (registry name · hf · gguf · catalog · auto[group]) → the runtime that
  serves the format → fetch into `weights.host_dir` (compose interpolation
  resolved from the environment, relative to the platform root) →
  descriptor `validate_model` via a **side-loaded engine** → `installed`
  (measured `size_gb`, `artifact`, `installed_at`, `source.sha256`) or
  `failed` (`error` names the step). Routing/groups untouched.
  **`benchmark()`**: `bench` verb (descriptor-named server timings, else
  completion tokens over wall clock) against the side-load or a live
  `base_url` → `entry.benchmarks` + capped per-model series under the
  `models` key of `.agent/model_benchmarks.json` → `benchmarked` (an
  `active` model keeps its state). **`SideLoad`**: `materialize_service()`
  of one model as a standalone compose project on an ephemeral port
  (`docker compose up -d` → `ready` probe within `timeout_s` → `down -v`
  always). Generations are persisted when the registry is servable; the
  pre-bootstrap case completes in memory and says so.
- **Additive schema/data:** `ModelEntry.artifact / installed_at / error /
  license`; `BenchVerb.rate_key / prompt_rate_key / count_key` (llama.cpp
  descriptor names its timing fields); `ModelEvalReport.models` with
  `evaluate_models` carrying it forward; `render.check_contract`,
  `render.materialize_service`, `render.served_id_for` extracted (public,
  output byte-identical — PR-3 goldens unchanged).
- **Excluded by design:** activate / upgrade / rollback / retire and the
  governance queue (PR-5); CLI verbs (PR-6); `.env` seeds, bootstrap, and
  reading `.env` for weights paths (PR-7 — callers pass `env`); the vLLM
  side-load memory gate / scheduled swap window (fit verdicts exist; gating
  lands with activation); per-role protocol probes (remain
  `evaluate-models`); `scripts/bench.sh` (untouched thin wrapper).

## Design decisions made in this PR

1. **The runtime that serves the format chooses the fetcher and the
   directory.** `gguf` → native fetcher into the llama.cpp descriptor's
   `weights.host_dir`; hub formats → hub client into the vLLM descriptor's
   cache dir. Adding a runtime/format never touches lifecycle code.
2. **Validation is a real load.** `installed` means "weights present +
   provider-validated" (MODEL_LIFECYCLE §1), so `install` side-loads the
   engine and requires a well-formed one-token completion. Without docker
   compose the install is `failed` with that reason — never silently
   "installed".
3. **PR-1's write-time invariant is respected, not worked around.** Before
   any activation a registry with declared roles is unservable and cannot
   be a generation; `install`/`benchmark` return `persisted=False` with an
   explicit message. Post-bootstrap installs persist generations because
   installs never change the active set.
4. **Benchmark data coexists with ADR-024.** A new `models` key in
   `.agent/model_benchmarks.json`; `evaluate-models` owns the rest of the
   file and now carries `models` forward (a lifecycle-only file is not
   treated as a previous evaluation).
5. **Timing field names are descriptor data** (`rate_key`, …) so the bench
   code contains no runtime-specific response shape (H1 mini-gate
   extended to `lifecycle.py`, `fetch.py`, `catalog.py`).
6. **Model names in the catalog are data**, exactly like the reference
   registry; DeepSeek-R1-Distill declares `tool_call_format: generic`
   (template-driven handler only), so the recommender rejects it for
   tool-calling roles on runtimes without a generic handler — honest, and
   tested.

## Tests (35 new; suite 378 → 413, all pre-existing tests unmodified)

- Resolver matrix (7 accepted forms) and 8 rejection cases each listing
  the accepted forms (F8).
- GGUF: verify + reuse + Range resume with identical hash; checksum
  mismatch discards; local import via link/copy; missing file.
- HF: hub client delegation (`HF_HUB_CACHE`, snapshot size, idempotent
  reuse, container-mode command mirrors the scripts), failure surfaced.
- Compose interpolation (`${VAR:-d}`, `${VAR-d}`, `$VAR`) and weights dir
  from descriptor data (default anchored at platform root; env wins).
- State machine: the diagram's paths, refused transitions name the
  reachable states, activation path present as data.
- Catalog: seed declares requirements not brands; operator override +
  merge; empty-variant rejection; `provider_for_format`; recommender on
  `cpu-low`/llama.cpp vs `accel-large`/vLLM with contract rejection naming
  the parser; explainable no-fit error.
- Install: gguf URL (register, fetch, validate, persist generation 1,
  routing untouched); idempotent reuse; hf via selected runtime; format
  not served; validation failure → `failed` → retry → `installed`
  (generations 1, 2); fetch failure → `failed`; catalog id and `auto`
  (with group/runtime); missing group / unknown id / duplicate name; the
  pre-bootstrap non-persistence case.
- Side-load: standalone compose (image, port, command, resolved host
  paths, plain `deploy`, no `container_name`), project name, `version →
  up → down`; named volumes declared for hub runtimes; docker missing / up
  failing / readiness timeout all loud and always torn down; probe +
  `SideLoadValidator`.
- Benchmark: descriptor timings vs wall clock vs no data; side-load path
  records entry + trend + generation note and state; live path skips the
  side-load and keeps `active`; not-installed refused; trend cap 20 and
  coexistence with `evaluate_models` (real ScriptedLLM run).
- H1 tripwire over the three new modules.

## Behavior notes

- **`agentd/src/agentd/evaluate.py`**: writes the new `models` key (empty
  when no lifecycle benchmarks exist) and no longer records a "previous
  evaluation" summary from a file that has neither `results` nor `history`.
  Existing evaluate tests pass unmodified; the file remains readable by
  every previous consumer (extra key only).
- **`config/providers/llamacpp.yaml`**: `verbs.bench` gains the timing
  field names (data). PR-3 goldens are byte-identical.
- No compose, Makefile, script, or hand-written config changes.

## Size note

The plan sizes PR-4 as L (≤ ~1500 net LOC). As specified it bundles
resolvers, two fetchers, side-load verbs, benchmark, catalog, and
recommender; delivered it is ~1,200 LOC of code+data plus ~730 LOC of
tests. If the reviewer prefers, the catalog + recommender (`catalog.py`,
`defaults/catalog.yaml`, their tests) split cleanly into an S-sized
follow-up with no code change elsewhere except the `auto`/catalog branch
of `resolve_target`.

## Rollback note

`git revert <commit>` restores the previous state: new modules, data, and
tests are additive; schema fields are optional with defaults (registries
and benchmark files written by this code stay loadable by the previous
code, which ignores unknown keys only where it does not validate them —
`ModelEvalReport` is written, never parsed, by `evaluate-models`). No
migrations. Side-loads are ephemeral compose projects torn down on exit;
a crash mid-side-load leaves an `ezai-sideload-<model>` project removable
with `docker compose -p <name> down -v`.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane,
  unavailable here — to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry (deviations justified above: side-load
      memory gate deferred to activation; `.env` reading deferred to PR-7;
      catalog seed minimal, content is P5)
- [x] New tests cover the scope; full suite green (413); ruff clean
- [x] Pre-existing tests unmodified; PR-1 golden and PR-3 goldens passing
- [x] Architecture docs updated to as-built (MODEL_LIFECYCLE §1/§2,
      PROVIDER_ABSTRACTION §5/§6, HARDWARE_AGNOSTIC §3, FINAL_FRE §2);
      ADR-027 gains the PR-4 slice (still Proposed)
- [x] Rollback note present
- [x] No model/vendor names in **code**: model names live in
      `defaults/catalog.yaml` and test fixtures; the hub base URL and the
      scripts' `python:3.11-slim` helper image are infrastructure strings,
      not vendor/SKU strings
