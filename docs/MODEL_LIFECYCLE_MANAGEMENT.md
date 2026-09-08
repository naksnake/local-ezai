# Model Lifecycle Management

**Scope:** how models enter, serve, and leave the platform — install ·
activate · benchmark · rollback · upgrade · explain — with **no config
file ever edited by a human after installation**, and with activation
governed (model replacement requires human approval, CLAUDE.md).

All operations exist twice with identical semantics: `local-ezai model
<verb>` and the Admin Center **Models** page — both thin clients of the
same control-plane operations ([CLI_AND_WEBUI_STRATEGY.md](CLI_AND_WEBUI_STRATEGY.md)).

## 1. Lifecycle state machine

```
            install                    benchmark
 registered ────────► installed ───────────────► benchmarked
 (catalog /              │   ▲                        │
  manual add)            │   └────── upgrade ──┐      │ activate (≈ approval)
            failed ◄─────┤                     │      ▼
                         │                 ┌───┴─── active ──► retired
                         └─ uninstall      │  rollback │
                                           └───────────┘ (previous generation)
```

| State | Meaning | Visible to routing? |
|---|---|---|
| `registered` | known (catalog entry or user-added source), no weights on disk | no |
| `installed` | weights present + provider-validated (loads, answers a probe) | no |
| `benchmarked` | evaluation evidence recorded | no |
| `active` | member of the serving set; resolvable by groups/roles | **yes** |
| `retired` | kept on disk for rollback/history; excluded from resolution | no |
| `failed` | install/validation failed; error retained | no |

Only the `benchmarked → active` and `active → active(new version)`
transitions change what serves users — **those two require approval**.
Everything else is freely self-service.

> **As-built (PR-4, `lifecycle.TRANSITIONS`):** the machine is a data table
> enforced by `transition()`: registered → installed | failed; installed →
> installed (re-install) | benchmarked | failed; benchmarked → installed |
> benchmarked | active | failed; active → active | retired; retired →
> active | installed; failed → installed | failed (retry). The activation
> paths are exercised by PR-5. `uninstall` is removal, not a state.

## 2. Operations

### `model install <name|source>`
Resolve from the **curated catalog** (a maintained list of known-good
models per group/provider/hardware profile, shipped with the platform and
updatable) or an explicit source (HF repo / GGUF URL / local path).
Steps: download → checksum → provider load-validation (llama.cpp or vLLM
dry-load + one-token probe) → `installed`. Idempotent; resumable;
disk-budget-aware (warns against the runs/workspaces prune guidance,
OPERATION_MANUAL).

> **As-built (PR-4, `agentd/src/agentd/lifecycle.py` + `fetch.py` +
> `catalog.py`):** `install()` resolves a registry name, `hf:<org/repo>`,
> `gguf:<url | hf://org/repo/file.gguf | path>`, a catalog id, or `auto`
> (group + recommender) into a model entry whose runtime is chosen by the
> descriptor that serves the source format; fetches into the directory the
> runtime descriptor mounts (`weights.host_dir`, compose-interpolated) — GGUF
> natively with HTTP-Range resume and streaming SHA-256 (recorded back into
> `source.sha256`; a mismatch discards the download), hub repositories
> through the same `hf download` path the scripts use; then runs the
> descriptor's `validate_model` verb against a **side-loaded** engine
> (§PROVIDER_ABSTRACTION 6) and sets `installed` (measured `size_gb`,
> `artifact`, `installed_at`) or `failed` (`error` names the step). Present
> and verified weights are reused. Installing never touches groups or
> routing; the generation is persisted when the registry is servable
> (pre-bootstrap results stay in memory and say so).

### `model benchmark [<name>]`
Extends the shipped `evaluate-models` machinery (ADR-020/024): per-role
protocol probes + latency, **tokens/sec** (the existing `make bench`
measurement, absorbed), and the run-history quality metrics. Results are
recorded on the model entry and in `.agent/model_benchmarks.json` trend
history; benchmarking a non-active model uses a **temporary side-load**
(llama.cpp) or a scheduled engine swap window (vLLM) per
[PROVIDER_ABSTRACTION.md](PROVIDER_ABSTRACTION.md) §6.

> **As-built (PR-4, `lifecycle.benchmark`):** the tokens/sec measurement of
> `make bench` is absorbed as the descriptor's `bench` verb — server-side
> timings when the descriptor names the fields (`timings_field`,
> `rate_key`, …), otherwise completion tokens over wall clock — run against
> a side-loaded engine or, with `base_url`, the live one. The result lands
> on the entry (`benchmarks`: tokens/s, prompt tokens/s, latency, via,
> class) and as a capped per-model series under the new `models` key of
> `.agent/model_benchmarks.json`, which `evaluate-models` now carries
> forward untouched; the entry becomes `benchmarked` (an `active` model
> keeps its state). Per-role protocol probes remain `evaluate-models`.
> The scheduled swap window for runtimes whose side-load does not fit is
> not implemented in this slice — side-load is attempted; the fit verdict is
> advisory data for PR-5/PR-6 to gate on.

### `model activate <name> [--group G] [--role R --pin]`
Creates an **activation request**: proposed generation diff (groups/roles
before → after) + benchmark evidence. The request enters the Governance
queue; on human approval the control plane renders generation N+1
([MODEL_ROUTING_DESIGN.md](MODEL_ROUTING_DESIGN.md) §6), reloads, health-
checks, and records the audit entry. CLI approval path exists too
(`local-ezai governance approve <id>`) — approval is a *human* act on
either surface, never automatic.

### `model upgrade <name>`
Sugar for the safe path: install new version side-by-side (versioned model
id) → benchmark → activation request that swaps versions in place in
groups/pins → old version `retired` (kept). One command, same gates.

### `model rollback [--to-generation N]`
Re-renders the previous (or named) **generation** — the entire routing
state, not just one model — validates, reloads, records. Rollback is the
one mutation that **skips the approval queue** (it restores an already-
approved state; incident response must be fast) but is loudly audited and
notifies admins.

### `model retire <name>` / `model uninstall <name>`
Retire removes from resolution (blocked while the model is the last active
member of a group any role depends on); uninstall additionally deletes
weights (blocked unless retired; requires `--force` if it would empty a
rollback target).

> **As-built (PR-5, `lifecycle.retire` / `lifecycle.uninstall`):** retire
> is self-service only for a **non-serving** active model — a current
> primary of any role is blocked ("activate a replacement first", which
> is approval-gated), as is the last member a role depends on. Uninstall
> is blocked for installed/benchmarked/active models; when any stored
> generation lists the model as active it is a rollback target and
> `force` is required; weights are removed and the model dropped from
> groups and pin chains. `activate`/`upgrade` create change requests
> (`activation.activate` / `activation.upgrade`); `activate` requires the
> `benchmarked` state (evidence), places the model as primary of a group
> (position 0 by default), pinned to a role, or as a fallback of its
> declared groups; `upgrade` swaps the versions in every group and pin and
> retires the old one.

### `model explain <role>` / `models` / `explain-run`
The transparency triad — see
[MODEL_ROUTING_DESIGN.md](MODEL_ROUTING_DESIGN.md) §7.

## 3. Generations — the rollback substrate

Every approved mutation produces **generation N+1**: an immutable snapshot
(registry + rendered artifacts + evidence references) under
`config/models/generations/`, plus a git commit when the platform
directory is a repo (it is, for self-hosted installs). Properties:

- linear history, monotonically numbered, capped with retention (default
  keep-all; weights retention governed separately by `retired` states);
- diffable (`model history`, Routing page diff view);
- the *unit* of rollback and of audit.

> **As-built (PR-1/PR-5):** history is strictly append-only — a rollback
> (explicit or self-) is a **new** generation whose content equals the
> target (`note: rollback to generation N`), so `registry.yaml` is always
> the latest number and the trail shows what happened. Rendered artifacts
> are not copied into the snapshot: they are deterministic outputs of the
> registry + descriptors and are re-rendered on rollback. Evidence lives
> on the change request (`config/governance/queue/<id>.yaml`) which the
> generation note references. Committing generations to git is deferred to
> the bootstrap (PR-7) behind an explicit flag.

## 4. Renders & reloads (atomicity)

```
mutate intent → validate (schema, resolution completeness §MODEL_ROUTING 4,
disk, provider capability) → write generation N+1 → render artifacts →
reload consumers (LiteLLM hot-reload; engine slot restart only when the
loaded weights change) → health probe (`wait-ready` machinery) →
   ok: commit generation, audit
   fail: auto re-render generation N (self-rollback), mark request failed
```

An engine-weight change is the only operation with a serving gap; the
Admin Center/CLI states the expected gap up front (per hardware profile)
and requires an extra confirmation.

> **As-built (PR-5, `agentd/src/agentd/activation.py`):** `apply()` runs
> exactly this pipeline for an approved request: stale check (registry
> moved ⇒ request `superseded`), reconcile (a registry ahead of its
> rendered manifest is re-rendered first), **dry render** (nothing is
> written that cannot render), `save_generation` N+1, `write_rendered`
> (drift-checked), reload of the **changed artifacts only**
> (`ComposeReloader`: engine `up -d` when the slot override or router
> preset changed, router `restart` when its config changed), health
> (`EngineHealth`: descriptor `ready` probe + one completion for a role's
> primary). Any exception past the snapshot — health, reload, drift, or a
> crash-injected error — triggers **self-rollback**: generation N's content
> is saved as generation N+2, re-rendered and reloaded; the request is
> marked `failed` with the reason and the restored generation. Reload and
> health are seams; until the PR-7 cutover the default is render-only and
> the audit record says `reloaded: false`.

## 5. "No config editing" — how it is actually guaranteed

| File users edit today | V1 status |
|---|---|
| `.env` | **installation-time only** (ports, secrets, hardware profile); never needed for day-2 |
| `config/litellm_config.yaml` | rendered artifact (generated header; drift detection: control plane refuses to render over unexpected manual edits and says so) — **as built (PR-7):** `config/rendered/litellm-config.yaml` is what compose mounts; the three hand-written variants are retired (test fixtures) |
| compose overrides / engine flags | rendered from provider descriptors + registry — **as built (PR-7):** `config/rendered/docker-compose.engine.yml`, added by every `make up*` / `local-ezai up --rendered` |
| `.agent/model_registry.yaml` (per repo) | still supported (ADR-020) but now *written for you* by `local-ezai model pin --repo` if desired; hand-editing remains allowed here — it is repo content, not platform config |
| `agentd.yaml` global config | absorbed: platform-level settings become control-plane state; env `AGENTD_*` remains for development |

> **As-built (PR-3, `render.write_rendered` / `check_drift`):** drift
> detection is a `manifest.yaml` of SHA-256 content hashes written beside
> the rendered artifacts; a hash mismatch (hand edit) or a managed file
> name without a manifest entry (unmanaged file) makes the next render
> refuse with the file named and the remedy ("change state via
> `local-ezai model …`, or `force` to discard the edit"). Artifacts a new
> generation no longer produces are removed. Rendered files carry a
> GENERATED banner. The parallel path `config/rendered/` becomes the live
> path at the PR-7 cutover.

## 6. Governance summary

- activate / upgrade ⇒ **approval required** (queue, evidence attached)
- rollback ⇒ immediate, audited, notifies
- install / benchmark / retire ⇒ self-service, audited
- evolution may **propose** routing changes on benchmark regression
  (roadmap N6′) — its proposals land in the same queue with the same
  evidence format; it can never approve them.
