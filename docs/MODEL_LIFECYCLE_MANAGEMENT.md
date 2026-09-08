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

## 2. Operations

### `model install <name|source>`
Resolve from the **curated catalog** (a maintained list of known-good
models per group/provider/hardware profile, shipped with the platform and
updatable) or an explicit source (HF repo / GGUF URL / local path).
Steps: download → checksum → provider load-validation (llama.cpp or vLLM
dry-load + one-token probe) → `installed`. Idempotent; resumable;
disk-budget-aware (warns against the runs/workspaces prune guidance,
OPERATION_MANUAL).

### `model benchmark [<name>]`
Extends the shipped `evaluate-models` machinery (ADR-020/024): per-role
protocol probes + latency, **tokens/sec** (the existing `make bench`
measurement, absorbed), and the run-history quality metrics. Results are
recorded on the model entry and in `.agent/model_benchmarks.json` trend
history; benchmarking a non-active model uses a **temporary side-load**
(llama.cpp) or a scheduled engine swap window (vLLM) per
[PROVIDER_ABSTRACTION.md](PROVIDER_ABSTRACTION.md) §6.

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

## 5. "No config editing" — how it is actually guaranteed

| File users edit today | V1 status |
|---|---|
| `.env` | **installation-time only** (ports, secrets, hardware profile); never needed for day-2 |
| `config/litellm_config.yaml` | rendered artifact (generated header; drift detection: control plane refuses to render over unexpected manual edits and says so) |
| compose overrides / engine flags | rendered from provider descriptors + registry |
| `.agent/model_registry.yaml` (per repo) | still supported (ADR-020) but now *written for you* by `local-ezai model pin --repo` if desired; hand-editing remains allowed here — it is repo content, not platform config |
| `agentd.yaml` global config | absorbed: platform-level settings become control-plane state; env `AGENTD_*` remains for development |

## 6. Governance summary

- activate / upgrade ⇒ **approval required** (queue, evidence attached)
- rollback ⇒ immediate, audited, notifies
- install / benchmark / retire ⇒ self-service, audited
- evolution may **propose** routing changes on benchmark regression
  (roadmap N6′) — its proposals land in the same queue with the same
  evidence format; it can never approve them.
