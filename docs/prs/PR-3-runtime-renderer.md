# PR-3 — Runtime descriptors + renderer

**Phase:** P1 · **ADR:** ADR-027 (Proposed, PR-3 slice added) · **Size:** L ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-3 ·
**Depends on:** PR-1 (registry, role contracts), PR-2 (capability vector,
classes) · **Status:** implemented on `claude/next-ready-pr-bnq7r3`

## Scope (as delivered)

- **`config/providers/llamacpp.yaml`, `config/providers/vllm.yaml`** — the
  two V1 runtime descriptors as **data**: served formats, capabilities
  (tool-call parsers, JSON output, `parallel_models`, `hot_swap`), the
  six-verb contract (`materialize` + `capabilities` realized by the
  renderer; `control` / `ready` / `validate_model` / `bench` declared for
  PR-4/5), weights mount, per-**accelerator-kind** image table
  (cuda/rocm/igpu/none — kinds, never brands), per-**capability-class**
  tuning (context budget, threads, memory caps), and verbatim compose
  extras (device wiring, reservations). Every runtime- or hardware-specific
  string in this PR lives in these two files.
- **`agentd/src/agentd/runtime_descriptor.py`** — descriptor schema
  (pydantic), consistency validation (kinds/classes known, `ctx_size`
  present, `parallel_models` ⇔ multi form), loader for
  `<config>/providers/*.yaml`, merged tuning (`defaults ← class ←
  accelerator`), and `source_for()` — a model may declare several source
  variants; each runtime picks the format it serves.
- **`agentd/src/agentd/render.py`** — pure `render(registry, descriptors,
  vector)` → four artifacts (+ one conditional):
  1. `litellm-config.yaml` — one alias per served model **and one
     `role-<role>` alias per role** (R-1), all at `http://engine:8000/v1`
     (R-3); optional embedding route supplied by the caller as data; the
     stack's `general_settings` / `litellm_settings` sections preserved
     verbatim (byte-equal to today's hand-written file — tested);
  2. `docker-compose.engine.yml` — engine-slot materialization for the
     `vllm`-named service: image per accelerator, templated command,
     weights mount, healthcheck from the `ready` verb, compose extras,
     `deploy` emitted with compose's `!override` tag (a plain merge would
     keep the base file's accelerator reservation);
  3. `role_map.yaml` — the runtime role map in **ADR-020 shape**
     (`agent_model_map`, fallback keys only when non-empty) plus the alias
     table — loadable by the shipped `load_model_registry`;
  4. `capability_report.yaml` — negotiation evidence per (role, model);
  5. `engine-models.ini` — only when the runtime hosts several models
     (llama.cpp router mode: `[*]` common keys + one `[<model>]` section).
  **Capability negotiation** (R-4/CF-6): every role × every model in its
  resolution chain (primary **and** fallbacks) is checked on its runtime —
  tool calling (`tool_call_format ∈ tool_call_parsers`), `json_output`,
  `min_context` against the class budget, and source format ∈
  `serves_formats`. **Slot rule** (PROVIDER_ABSTRACTION §4): the active set
  must be servable by one runtime; `parallel_models: false` runtimes accept
  one active model. All problems are aggregated into **one** `RenderError`
  that names each missing capability and the fix.
  **Persistence**: `write_rendered()` writes atomically to
  `<config>/rendered/`, records SHA-256 hashes in `manifest.yaml`, removes
  artifacts the new render no longer produces, and **refuses drift**
  (`DriftError`: hand-edited or unmanaged files) unless `force=True`.
- **`docker-compose.yml`** — additive network alias `engine` on the
  `vllm` service (R-3). Service name, port, image, command unchanged.
- **Excluded by design:** cutover (hand-written LiteLLM configs untouched;
  renderer writes to the parallel path until PR-7); executing any process
  verb (PR-4/5); CLI verbs and the agentd alias switch (PR-6); bootstrap /
  `.env` seeds (PR-7); the live `config/models/registry.yaml` instance.

## Design decisions made in this PR

1. **Descriptors are templates, the renderer is a filler.** Commands,
   flags, INI keys, images, and device paths are descriptor strings with
   `{placeholders}`; the renderer supplies a documented context (builtins
   `port`/`cpu_cores`/`served_count`/`preset_path`, per-model
   `model_name`/`model_ref`/`model_file`/`model_path`/`model_ctx`/
   `tool_call_parser`, and every merged tuning key). An unknown placeholder
   is a loud descriptor error. Adding a runtime = adding a YAML file
   (the RUNTIME_ABSTRACTION §6 drill becomes possible without code).
2. **llama.cpp `parallel_models: true` via router mode**, verified against
   upstream docs: several active GGUFs render to `--models-preset` + a
   generated INI whose section names are the registry model names (so the
   served id equals the registry name in both forms). One active model
   renders the classic single command — **field-for-field today's
   low-power profile** (`-m … --alias … --ctx-size 8192 --threads 4
   --parallel 1 --jinja`, 6g cap, `/health` probe). vLLM stays one model
   per engine instance (V1 stance).
3. **Negotiation covers fallbacks.** A fallback that cannot satisfy the
   role contract would fail silently at request time — exactly what §4
   forbids — so it fails at render time like a primary.
4. **Class budgets are honest.** `min_context` is checked against
   `min(model.context, class ctx budget)`; the reference set therefore
   negotiates fully on `accel-large` and reports vLLM-on-`cpu-low`
   contexts (2048) as too small for 8192-token roles — a real fitting
   problem stated plainly, not hidden (tested both ways).
5. **The reference set is not single-slot materializable** (it spans both
   runtimes, as the CLAUDE.md map does). Rendering it produces the
   plain-language conflict listing models per runtime and the fix
   (`AI_RUNTIME` + retire/side-load) — tested. LiteLLM/role-map rendering
   is independently callable (`render_role_map`) so explain/approval
   surfaces (PR-5) can use it regardless.
6. **No LiteLLM-level fallbacks.** Role aliases map to the primary only;
   request-time fallback remains agentd's ADR-020 mechanism fed by the
   rendered role map (byte-compatible shape).
7. **Embedding route is caller data.** `EmbeddingEntry` has no defaults —
   the renderer never learns an embedding model name (H1/R-1).

## Tests (34 new; suite 344 → 378, all pre-existing tests unmodified)

- **Goldens** (`tests/fixtures/rendered/`): engine-slot override per
  legacy-profile equivalent — `n97` (llamacpp/none/cpu-low, complete
  artifact set), `n97-igpu` (llamacpp/igpu/cpu-low), `gpu`
  (vllm/cuda/accel-large), `cpu` (vllm/none/cpu-standard) — and `multi`
  (llamacpp router form + preset). Regenerate with
  `RENDER_UPDATE_GOLDENS=1` and review the diff.
- **H4**: the `n97` preset renders byte-identically to class `cpu-low`.
- Determinism (no timestamps/host state); LiteLLM role aliases at the
  `engine` alias, never the service name; platform sections byte-equal to
  the hand-written config.
- **Golden chain**: rendered role map of the reference set, parsed by the
  ADR-020 loader, equals this repo's live `.agent/model_registry.yaml`.
- Negotiation: reference set green on `accel-large` (fallbacks included);
  failures name `tool_calling` (with declared format + runtime parsers),
  `min_context` (both numbers, class, runtime), `json_output`, format
  mismatch (formats named); dual-variant model served in each runtime's
  format.
- Slot rule: mixed-runtime conflict lists models per runtime; selected
  runtime lists foreign models; single-model runtime refuses two active;
  missing accelerator image names supported kinds and the explicit
  `accelerator="none"` path renders; every problem aggregated in one error;
  unknown placeholder → descriptor error.
- Compose override: targets the slot service, `deploy: !override`, n97
  command field-for-field, tool-calling args follow negotiation, router
  preset content + mount + `--models-max` from class tuning.
- Persistence: manifest hashes, drift refusal naming the file and the fix,
  `force` discards, unmanaged files refused, stale artifacts removed.
- Tripwires: `docker-compose.yml` carries the `engine` alias with the slot
  port unchanged; renderer/descriptor modules contain no brand, image,
  device path, or engine flag (H1 mini-gate).
- Descriptor schema: contract fields present; inconsistent data rejected
  (brand as accelerator kind, missing `ctx_size`, `parallel_models` without
  multi form); file name must equal runtime id.

## Behavior notes

- **`docker-compose.yml`**: the `vllm` service's `networks` entry changes
  from list form to mapping form to attach alias `engine`. Service name,
  container name, ports, image, command, volumes, environment, deploy are
  untouched; the CPU/N97/iGPU overrides do not touch `networks`, so the
  merged result differs only by the added alias. Nothing existing
  addresses `engine:8000` yet.
- **No other existing file changes.** `config/litellm-config*.yaml`,
  `.env.example`, Makefile, monitor, k8s manifests are untouched. Rendered
  output goes to `config/rendered/` (not created in the repo — no live
  registry instance exists until PR-7).
- **Golden test (PR-1) untouched and passing**; resolution logic is not
  modified.

## Rollback note

`git revert <commit>` fully restores the previous state: new modules,
descriptor data, tests and fixtures are additive; the compose change is a
network alias with no persisted state. No migrations; no rendered files
are committed.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane,
  which is not available in this environment — to be attached on a
  stack-connected re-run. Checklist review performed in its place:
- [x] Scope matches the plan entry — deviations, all justified above:
      negotiation also covers fallbacks; llama.cpp multi-model realized
      via router preset (upstream-verified); `capability_report.yaml` and
      `manifest.yaml` added as artifacts; no LiteLLM-level fallbacks
- [x] New tests cover the scope; full suite green (378); ruff clean
- [x] Pre-existing tests unmodified; PR-1 golden passing
- [x] Architecture docs updated to as-built (PROVIDER_ABSTRACTION §2/§3,
      RUNTIME_ABSTRACTION §2/§3/§4, MODEL_ROUTING_DESIGN §6,
      MODEL_LIFECYCLE §5); ADR-027 gains the PR-3 slice (still Proposed)
- [x] Rollback note present
- [x] No model/vendor names in **code** — runtime images, device paths,
      engine flags and the `nvidia` reservation driver live exclusively in
      `config/providers/*.yaml` (the sanctioned location); the only engine
      string in code is the historical compose service name (`ENGINE_SERVICE`,
      ADR-001) — to be allowlisted by the H1 gate (PR-25)
