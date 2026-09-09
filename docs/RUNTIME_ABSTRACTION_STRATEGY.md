# Runtime Abstraction Strategy

**Requirement:** llama.cpp, vLLM, and *future runtimes* must be user
choices, not architecture. Remediations R-3/R-4 of
[V1_PRODUCT_REVIEW.md](V1_PRODUCT_REVIEW.md); deepens
[PROVIDER_ABSTRACTION.md](PROVIDER_ABSTRACTION.md) ("provider" and
"runtime" are the same concept; "runtime" is the product-facing word).

## 1. The abstraction ladder (who may know what)

```
Layer                          May know about runtimes?
──────────────────────────────────────────────────────────
Agents, prompts, pipelines     NO  — speak role aliases via OpenAI API
OpenWebUI / Admin Center       NO  — display AI_RUNTIME as a label only
CLI verbs                      NO  — `runtime` verbs are generic
LiteLLM rendered config        NO  — points at the engine slot, any runtime
Control plane lifecycle mgr    ONLY through descriptor verbs (§2)
Runtime descriptors + images   YES — the single home of runtime knowledge
```

The test: **adding a runtime touches only the bottom layer.**

## 2. The runtime descriptor contract (the plug-in seam)

A runtime is a YAML descriptor + container images implementing six verbs
(callable behaviors the lifecycle manager invokes; today realized as
compose/exec operations, later possibly as a small adapter):

| Verb | Contract |
|---|---|
| `materialize(model_set, capability_vector) → engine spec` | image + args + mounts for the engine slot |
| `start/stop/restart` | engine slot process control |
| `ready?` | readiness probe (path + timeout from descriptor) |
| `validate_model(entry)` | dry-load + one-token probe |
| `bench(entry) → tokens_per_s` | local measurement |
| `capabilities(entry) → {tool_calling(parser), max_context, formats}` | negotiation input (§4) |

Descriptor data: served formats (gguf/hf/awq/…), per-accelerator images
([HARDWARE_AGNOSTIC_ARCHITECTURE.md](HARDWARE_AGNOSTIC_ARCHITECTURE.md) §2),
concurrency traits (multi-model? hot-swap?), health, side-load support.

**V1 ships two descriptors** (llamacpp, vllm). The contract is the
product; the descriptors are content.

> **As-built (PR-3):** the two descriptors live in `config/providers/`
> (schema: `agentd/runtime_descriptor.py`). `materialize` and
> `capabilities` are realized by the renderer as pure data → artifacts;
> `control`, `ready`, `validate_model`, `bench` are declared in the
> `verbs:` block (probe path/timeouts, probe kind, bench token budget,
> timings field) and are executed by the lifecycle manager from PR-4/PR-5
> on. `ready` already renders as the engine service's compose healthcheck.

## 3. Neutral naming (killing the `vllm`-name coupling, compatibly)

- The compose service name `vllm` is **kept** (ADR-001 compatibility; no
  redesign) and documented as a historical alias.
- An additive docker **network alias `engine`** is attached to the slot;
  every *new* consumer and every rendered artifact references `engine:8000`.
- User-facing vocabulary everywhere: **engine slot**, **runtime**,
  `AI_RUNTIME=llamacpp|vllm|<future>`. The string "vllm" appears in UI/UX
  only when vLLM is genuinely the selected runtime.
- Full rename of the service is deferred to a major version (breaking);
  the alias makes it a no-op later.

> **As-built (PR-3):** `docker-compose.yml` attaches network alias
> `engine` to the slot service; every rendered artifact addresses
> `http://engine:8000/v1`, and a test asserts the alias stays in place. The
> renderer spells the historical service name in exactly one constant
> (`render.ENGINE_SERVICE`) to key the compose override.

## 4. Capability negotiation (model × runtime × role, all data)

Tool calling (and similar features) is a property of the *(runtime,
model chat-template)* pair — never assumed platform-wide (finding CF-6):

```
model entry:    template: chatml|llama3|custom, tool_call_format: hermes|none|...
runtime desc:   tool_call_parsers: [hermes, mistral, ...]
role contract:  requires: {tool_calling: true|false, min_context: N}
                                  [MODEL_GOVERNANCE_V2.md §3]
render-time check:
  for each role → resolved model → runtime:
     role.requires ⊆ capabilities(runtime, model)   else FAIL loudly
```

Consequence: a model family the platform has never heard of works
immediately if it declares its template/format — and a mismatch is a
clear render-time error naming the missing capability, never a silent
request-time failure.

> **As-built (PR-3, `render.negotiate`):** the check runs for every model
> in a role's resolution chain — **fallbacks included** (a fallback that
> cannot serve the role would otherwise fail silently at request time) —
> and covers four properties: `tool_calling` (`tool_call_format ∈ runtime
> tool_call_parsers`), `json_output`, `min_context` against
> `min(model.context, class ctx budget)`, and source format ∈
> `serves_formats` (a model may declare several variants; each runtime picks
> its own). Every failure is aggregated into one error that names the role,
> model, runtime, and the missing capability with both numbers/lists.

## 5. Runtime selection & switching UX

- `.env` seeds `AI_RUNTIME` once
  ([FINAL_FIRST_RUN_EXPERIENCE.md](FINAL_FIRST_RUN_EXPERIENCE.md));
  afterwards the **Runtime** page / `local-ezai runtime` verbs manage it.
- Switching runtimes is a governed generation change like any activation:
  validate the active model set is servable by the target runtime
  (formats! — the check explains "deepseek-r1 is GGUF; vLLM needs HF/AWQ —
  install the HF variant or keep llamacpp"), approval, render, reload,
  rollback available.
- Mixed sets remain constrained by the single engine slot rule
  (PROVIDER_ABSTRACTION §4/§7) — stated to users as a fitting problem, not
  a mystery.

> **As built (PR-17, Admin Center `/runtime`):** the Runtime page shows the
> engine slot (runtime, class, accelerator, memory, engine/router health,
> active models) and, for every other runtime the descriptors serve, a
> **pre-check**: the active models that have no variant for it (with the
> fix above — install a served variant or keep the current runtime) and the
> catalog candidates per group with the recommender's fit and contract
> verdicts (on a small host the vllm descriptor's context budget rules out
> every candidate; the page carries that sentence). Switching is what P1
> built: activating a model served by the other runtime — the change
> request is flagged `runtime.switch` (activation.propose) and needs
> approval. There is **no switch button** and no `local-ezai runtime`
> verb yet: the 1.0.0 contract has no runtime operation; a dedicated verb is
> a 1.1 candidate.

## 6. The third-runtime drill (acceptance test)

Before V1 ships, prove agnosticism empirically: implement a **mock
runtime descriptor** (`mockengine`: an OpenAI-API stub container) end to
end — install→activate→benchmark→serve→rollback purely through descriptor
+ image, with **zero diffs outside** `config/providers/` and test
fixtures. CI keeps this drill green forever; it is the regression test
that the seam stays a seam. (It also becomes the template for real future
runtimes: TGI, ollama, etc. — explicitly out of V1 scope.)

## 7. LiteLLM's position (finding CF-10)

LiteLLM remains the uniform API layer (a required component), but the
architecture's dependency is on the *contract* "OpenAI-compatible router
with alias mapping and hooks", and all LiteLLM-specific knowledge lives in
one renderer module — swappable in principle, unexercised in V1.
