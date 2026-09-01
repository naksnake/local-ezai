# Hardware-Agnostic Architecture

**Requirement:** the platform must not be optimized for — or coupled to —
any specific GPU, vendor, or box. Users choose CPU or GPU (any vendor the
runtimes support) and the platform adapts. Remediation R-2 of
[V1_PRODUCT_REVIEW.md](V1_PRODUCT_REVIEW.md).

## 1. Principle: capabilities, not SKUs

Today's profiles are SKU-shaped (`gpu`, `cpu`, `n97`, `n97-igpu`). V1
replaces the *meaning* (not the commands) with **capability classes**
derived from a detected **capability vector**:

```
capability vector (detected at setup, stored in .env by the installer):
  accelerator: cuda | rocm | igpu | none        # kind, not brand
  accel_memory_gb, system_memory_gb, cpu_cores, cpu_flags (avx2/512, …)
  disk_free_gb
```

| Capability class | Rule of thumb | Typical member |
|---|---|---|
| `accel-large` | accelerator ≥ 16 GB | discrete GPU workstation |
| `accel-small` | accelerator < 16 GB | small dGPU / big iGPU |
| `cpu-standard` | no accelerator, ≥ 16 GB RAM | typical server/desktop |
| `cpu-low` | no accelerator, < 16 GB RAM | mini-PCs (the N97 preset lives here) |

Everything that used to key off a profile now keys off the class:
runtime image selection, engine arguments (layers/threads/kv-cache),
context budgets, catalog recommendations, benchmark expectations, FRE
recommended sets.

**Continuity:** `make setup-gpu` / `setup-cpu` / `setup-n97` remain as
entry points; they *assert* a class (gpu→accel-*, cpu→cpu-standard,
n97→cpu-low preset) instead of selecting bespoke files. Existing installs
keep working; `n97` becomes an alias, not an architecture.

## 2. Where hardware knowledge is allowed to live

Exactly **one** place: runtime descriptor data
([RUNTIME_ABSTRACTION_STRATEGY.md](RUNTIME_ABSTRACTION_STRATEGY.md) §2) —
the `(runtime × accelerator-kind)` table of images and argument templates:

```yaml
# excerpt — data, not code
runtime: llamacpp
accelerators:
  cuda:  {image: ..., args: {gpu_layers: auto}}
  rocm:  {image: ..., args: {gpu_layers: auto}}
  igpu:  {image: ... (vulkan/sycl build), args: ...}
  none:  {image: ..., args: {threads: auto}}
```

Forbidden locations for hardware knowledge (checked in review):
agent code and prompts · registry roles/groups · CLI/WebUI surfaces
(they show the *class*, never a brand) · docs' golden paths · catalogs
(entries state *requirements*, e.g. min memory at a quantization — never
"needs an RTX ...").

A vendor never named in a descriptor is still supported the day its
runtime supports it — that is the test of agnosticism.

## 3. Fitting: the only hardware-aware computation

One pure function, `fit(model_entry, capability_vector) → verdict`,
computes: fits-in-accelerator / fits-in-RAM / doesn't-fit, expected
context ceiling, and a coarse speed band — from model size/quantization
and the vector. It powers:

- FRE recommendations and warnings ("this model will run CPU-only, expect
  ~X tok/s");
- activation validation ("activating this set exceeds memory; retire Y
  or choose a smaller quantization");
- the WebUI's fit badges ([WEBUI_PRODUCT_STRATEGY.md](WEBUI_PRODUCT_STRATEGY.md)).

It estimates conservatively and **never blocks a user override**
(`--i-know` / explicit WebUI confirmation): agnosticism includes the
freedom to run something slowly.

## 4. Benchmarks replace assumptions

Wherever a design would want a hardware assumption, it uses **measured
data instead**: `tokens_per_s` from the benchmark harness on *this* box,
recorded per model in the registry and trend history. Recommendations,
evolution evidence, and the WebUI always show local measurements, never
vendor marketing classes.

## 5. What explicitly stays out of scope

Vendor-specific tuning guides (belong in runtime upstreams) ·
multi-accelerator scheduling · heterogeneous multi-node — all V1.x+, none
require architecture changes because they arrive as descriptor data and
new capability-vector fields.

## 6. Acceptance tests (release-gated)

| # | Test |
|---|---|
| H1 | The word audit: no GPU vendor/brand or SKU string outside runtime-descriptor data and historical docs (CI grep gate) |
| H2 | Same `.env` role seeds produce a working platform on `accel-large` and `cpu-low` with only class-appropriate models substituted by the recommender |
| H3 | Fit verdicts for a 7B Q4 model correct on all four classes (fixture vectors) |
| H4 | `setup-n97` = `cpu-low` preset equivalence (byte-identical rendered artifacts modulo class name) |
