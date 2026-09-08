# Local-EZAI — V1 Product Review

**Scope:** review of the productization architecture
([TARGET_PRODUCT_V1.md](TARGET_PRODUCT_V1.md) and companions) against the
critical product requirement: **hardware-agnostic, runtime-agnostic,
model-agnostic** — users freely choose CPU/GPU, llama.cpp/vLLM/future
runtimes, and any model family, without architecture changes.
**Output:** coupling findings (CF-*) with remediations (R-*), and the
final V1 recommendation (§5). Companion deep-dives:
[HARDWARE_AGNOSTIC_ARCHITECTURE.md](HARDWARE_AGNOSTIC_ARCHITECTURE.md),
[RUNTIME_ABSTRACTION_STRATEGY.md](RUNTIME_ABSTRACTION_STRATEGY.md),
[FINAL_FIRST_RUN_EXPERIENCE.md](FINAL_FIRST_RUN_EXPERIENCE.md),
[WEBUI_PRODUCT_STRATEGY.md](WEBUI_PRODUCT_STRATEGY.md),
[MODEL_GOVERNANCE_V2.md](MODEL_GOVERNANCE_V2.md).

## 1. What the architecture already gets right

- **Layered routing** (role → group → model → provider) is inherently
  model-agnostic: swapping any model touches data, never code
  ([MODEL_ROUTING_DESIGN.md](MODEL_ROUTING_DESIGN.md)).
- **Rendered artifacts + generations** kill day-2 file editing and give
  rollback ([MODEL_LIFECYCLE_MANAGEMENT.md](MODEL_LIFECYCLE_MANAGEMENT.md)).
- **PAL descriptors** already treat llama.cpp/vLLM as data
  ([PROVIDER_ABSTRACTION.md](PROVIDER_ABSTRACTION.md)).
- **Consumers speak OpenAI-API-via-LiteLLM only** — no agent, UI, or tool
  knows an engine exists (ADR-001/ADR-007).
- Governance (roles approved by humans, evidence attached) is
  vendor-neutral by construction.

## 2. Coupling findings

Severity: 🔴 violates agnosticism today · 🟡 latent/naming coupling ·
🟢 acceptable, document only.

| ID | Finding | Kind | Sev | Where |
|---|---|---|---|---|
| CF-1 | `.env` expresses "which model" **differently per hardware** (`CHAT_MODEL` vs `CPU_CHAT_MODEL` vs `N97_MODEL_FILE/NAME`), and names must be manually kept in sync with three hand-maintained LiteLLM config variants | hardware×model×runtime entanglement | 🔴 | `.env.example`, `config/litellm-config{,.cpu,.n97}.yaml` |
| CF-2 | Hardware profiles are an enum of SKUs (`n97`, `n97-igpu`) rather than capability classes; a new box means new profile files | hardware | 🔴 | compose profiles, Makefile, FRE docs |
| CF-3 | agentd's code defaults name a concrete model (`qwen2.5-7b` in every role default) | model-family | 🔴 | `agentd` config defaults |
| CF-4 | Default routing pins concrete model names (hermes3/deepseek-r1/qwen3-coder/llama3) as if architectural; they are one *reference set*, not the design | model-family | 🟡 | MODEL_ROUTING_DESIGN §4 example, CLAUDE.md map |
| CF-5 | The engine slot's service name is `vllm` even when llama.cpp runs in it | runtime (naming) | 🟡 | ADR-001, compose |
| CF-6 | Tool-calling assumed via the "hermes parser" — actually a property of the *(runtime × chat-template)* pair, not a platform constant | model-family/runtime | 🟡 | PAL descriptors, engine profiles |
| CF-7 | `VLLM_IMAGE` override comment pushes image/architecture choice onto users; CUDA silently assumed for the GPU profile | hardware/vendor | 🟡 | `.env.example`, compose GPU profile |
| CF-8 | Curated catalog could drift into a "blessed models" list | model-family | 🟡 | FIRST_RUN_EXPERIENCE §3 |
| CF-9 | Chat UI exposes raw model names as the user's mental model | model-family (UX) | 🟡 | OpenWebUI selector today |
| CF-10 | LiteLLM as the single API layer; embed-server pinned per collection | provider | 🟢 | stack invariants |

## 3. Remediations (all additive; fold into plan phase P1 unless noted)

- **R-1 (CF-1, CF-3, CF-4)** — *Roles become the only stable names.*
  `.env` gains runtime-agnostic seeds (`AI_RUNTIME`, `REASONING_MODEL`,
  `CODING_MODEL`, `CHAT_MODEL`) read **once** at bootstrap to render
  generation 1; the per-profile variable families and the three LiteLLM
  variants are retired into rendered artifacts. LiteLLM serves **role
  aliases** (`role-planner`, `role-chat`, …) and agentd's code defaults
  bind to those aliases — completing ADR-007: *no repository code contains
  a model name.* The CLAUDE.md map remains honored as the reference
  default *data set*. Details:
  [MODEL_GOVERNANCE_V2.md](MODEL_GOVERNANCE_V2.md),
  [FINAL_FIRST_RUN_EXPERIENCE.md](FINAL_FIRST_RUN_EXPERIENCE.md).
- **R-2 (CF-2, CF-7)** — *Capability classes replace SKU profiles.* Setup
  detects a **capability vector** (accelerator kind cuda/rocm/igpu/none,
  VRAM/RAM/cores/AVX) and maps it to a class (`accel-large`, `accel-small`,
  `cpu-standard`, `cpu-low`); `n97` survives as a preset alias of
  `cpu-low`. Engine images per (runtime × accelerator) are descriptor
  data, never user homework. Details:
  [HARDWARE_AGNOSTIC_ARCHITECTURE.md](HARDWARE_AGNOSTIC_ARCHITECTURE.md).
- **R-3 (CF-5)** — keep the compose service name `vllm` for compatibility
  (ADR-001 unbroken), add a **runtime-neutral network alias `engine`**
  and use only neutral terms ("engine slot", `AI_RUNTIME`) in every
  user-facing surface and new consumer. Details:
  [RUNTIME_ABSTRACTION_STRATEGY.md](RUNTIME_ABSTRACTION_STRATEGY.md) §3.
- **R-4 (CF-6)** — *capability negotiation*: model entries declare their
  chat-template/tool-call needs; runtime descriptors declare supported
  parsers; render-time resolution proves each role's requirements are met
  (already designed for tool-calling — generalize to template/parser
  pairs). Details: RUNTIME_ABSTRACTION_STRATEGY §4.
- **R-5 (CF-8)** — the catalog is **data with pluggable sources** and a
  requirements-driven recommender (fit to capability class + role
  contract), with user-supplied models first-class equal citizens.
- **R-6 (CF-9)** — WebUI leads with **role-based entries** ("EZAI Chat",
  "EZAI Reasoning"); raw model names remain visible as implementation
  detail everywhere evidence matters, but stop being the management
  handle. Details: [WEBUI_PRODUCT_STRATEGY.md](WEBUI_PRODUCT_STRATEGY.md),
  decision in [MODEL_GOVERNANCE_V2.md](MODEL_GOVERNANCE_V2.md) §2.
- **R-7 (CF-10)** — accept: LiteLLM stays (required component), but the
  dependency contract is "an OpenAI-compatible router", documented so it
  remains swappable in principle.

## 4. Governance & evolution review outcomes

- **Model names vs roles:** recommendation **B-with-transparency** —
  govern logical roles/groups, reveal (never hide) implementation models
  as evidence. Full rationale: [MODEL_GOVERNANCE_V2.md](MODEL_GOVERNANCE_V2.md) §2.
- **Evolution participation:** advisory-only lane — benchmark reviews,
  model recommendations, and pre-filled upgrade proposals into the
  existing governance queue; never an automatic activation; rejections
  recorded to memory so failed experiments aren't re-proposed. Design:
  MODEL_GOVERNANCE_V2 §5.
- **WebUI completeness:** runtime, models, projects, agents, sprints,
  evolution all manageable without CLI knowledge after setup:
  [WEBUI_PRODUCT_STRATEGY.md](WEBUI_PRODUCT_STRATEGY.md).

## 5. Final recommendation for Local-EZAI V1

**Proceed to implementation with the ADR-025 architecture amended by
R-1…R-7 (recorded as ADR-026).** Concretely:

1. The **interface users and code depend on is logical**: roles and
   groups. Model names, runtimes, and hardware are *data* resolved at
   render time — after R-1/R-3, no code, prompt, UI string, or default
   references a model family, an engine, or a GPU vendor.
2. The **first-run contract** is exactly the mandated five steps — clone,
   edit `.env` once (runtime + three role models), `make setup[-cpu|-gpu]`,
   open the WebUI, use everything
   ([FINAL_FIRST_RUN_EXPERIENCE.md](FINAL_FIRST_RUN_EXPERIENCE.md)).
3. **V1 ships with llama.cpp + vLLM**; a future runtime is a descriptor +
   image, proven by the "third-runtime drill" acceptance test
   (RUNTIME_ABSTRACTION_STRATEGY §6) — no architecture change permitted or
   required.
4. **Humans stay the final authority**: evolution recommends, the queue
   decides, generations make every decision reversible.
5. Implementation sequencing is unchanged
   ([V1_IMPLEMENTATION_PLAN.md](V1_IMPLEMENTATION_PLAN.md)) with the
   decoupling remediations folded into **P1** (they are registry/PAL/
   bootstrap work) and the FRE deltas into **P5**.
