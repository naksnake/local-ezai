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
