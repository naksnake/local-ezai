# Provider Abstraction Layer (PAL)

**Problem:** the platform must serve models through **llama.cpp** and
**vLLM** interchangeably, selected per model and per hardware profile,
without users touching compose files or engine flags — while preserving
the founding invariant (ADR-001): *the inference service is always named
`vllm`, always speaks the OpenAI API on :8000, and consumers route only
via LiteLLM.*

The PAL is **declarative descriptors + a renderer**, not a new runtime
process. It formalizes what the compose profiles (GPU/cpu/n97/n97-igpu)
already do by hand.

## 1. Layer diagram

```
 Registry v2 (models, groups, roles)          [MODEL_ROUTING_DESIGN.md]
        │  model.provider = llamacpp | vllm
        ▼
 Provider descriptors  config/providers/{llamacpp,vllm}.yaml   (PAL, NEW)
        │  render (control plane)
        ▼
 Rendered artifacts: engine-slot compose override + engine args
                      + LiteLLM model entries
        ▼
 Engine slot  — service name `vllm`, port :8000, OpenAI API  (UNCHANGED)
        ▼
 LiteLLM :4000 — the only consumer-facing API               (UNCHANGED)
```

## 2. Provider descriptor (schema, illustrative)

```yaml
# config/providers/llamacpp.yaml
provider: llamacpp
serves_formats: [gguf]
capabilities:
  tool_calling: {parser: hermes, native: true}
  parallel_models: true          # llama.cpp server can host >1 model
  hot_swap: true                 # load/unload without container restart
  quantizations: [Q4_K_M, Q5_K_M, Q8_0, ...]
profiles:                        # per hardware profile
  gpu:  {image: ..., args_template: ..., gpu_layers: auto}
  cpu:  {image: ..., args_template: ..., threads: auto}
  n97:  {image: ..., args_template: ..., ctx_budget: 8192}
health: {ready_probe: /health, load_timeout_s: 600}

# config/providers/vllm.yaml
provider: vllm
serves_formats: [hf, awq, gptq]
capabilities:
  tool_calling: {parser: hermes, native: true}
  parallel_models: false         # one model per engine instance (V1 stance)
  hot_swap: false                # weight change = container restart
profiles:
  gpu: {image: vllm/vllm-openai, args_template: ..., kv_cache: auto}
  cpu: {image: vllm-cpu build,   args_template: ...}
health: {ready_probe: /health, load_timeout_s: 1200}
```

Descriptors ship with the platform and are versioned; users select
*models*, and the model's `provider` + the host's hardware profile select
the descriptor. Users never author descriptors (extending them is a
platform-development task like any other, via PRs).

## 3. What the renderer produces

For the current generation ([MODEL_LIFECYCLE_MANAGEMENT.md](MODEL_LIFECYCLE_MANAGEMENT.md) §4):

1. **Engine-slot materialization** — the compose override that the
   existing profile system already consumes: which image, args, and
   mounted weights the `vllm`-named service runs. Exactly one **active
   provider per engine slot** in V1 (§7 discusses the second slot).
2. **LiteLLM entries** — one per installed-and-served model + role aliases,
   all pointing at :8000 (ADR-001/ADR-007 unchanged).
3. **Capability report** — consumed by validation: a model routed to a
   role that needs tool calling must sit on a provider/parser combination
   that supports it; resolution fails at render time otherwise
   (never at request time).

## 4. Provider selection rules (deterministic)

1. A model declares its provider (from its format: GGUF ⇒ llamacpp;
   HF/AWQ ⇒ vLLM; catalog entries pre-declare it).
2. The hardware profile filters providers (e.g. n97 ⇒ llamacpp-only by
   default — today's behavior, now explicit data).
3. The **active serving set** (all `active` models) must be satisfiable by
   the engine slot: llama.cpp can host several GGUFs concurrently;
   vLLM hosts one model. Mixed active sets choose the slot's provider by
   rule: *if any active model requires vLLM, vLLM owns the slot and
   llama.cpp models must be inactive or moved to the side-load (§6)* —
   activation validation enforces this and explains conflicts in plain
   language before anything is approved.

## 5. Health & lifecycle hooks (absorbing existing ops)

`make wait-ready`, `make bench`, and the engine log targets become
provider-descriptor-driven control-plane operations (the make targets
remain as thin wrappers — nothing breaks). Every provider exposes:
`materialize`, `start/stop/restart`, `ready?`, `bench(tokens/sec)`,
`validate_model(load+probe)` — the verbs the lifecycle manager calls.

## 6. Side-load slot (benchmark without downtime)

Benchmarking a *candidate* model must not displace the serving set. The
PAL defines an optional **side-load**: a short-lived second engine
container on an ephemeral port, provider-appropriate (cheap for llama.cpp;
gated by VRAM/RAM checks for vLLM — on small profiles the fallback is a
scheduled swap window with explicit user confirmation). Side-loads are
never registered in LiteLLM's public alias space; only the benchmark
harness addresses them.

## 7. Deliberate V1 boundaries

- **One active engine slot.** A permanent second slot (:8002, e.g. vLLM
  for coding + llama.cpp for chat) is designed-for (the renderer and
  registry are slot-aware in schema) but **not shipped** in V1 — it lands
  as V1.x once the single-slot lifecycle has soaked. This keeps V1 inside
  today's resource envelope and compose topology.
- No new inference engines in V1 (the PAL makes adding one a descriptor +
  image task later — e.g. TGI/ollama — but that is out of scope).
- Embeddings stay on the dedicated embed-server :8001, outside the PAL
  (existing invariant: one embedding model per Qdrant collection).
