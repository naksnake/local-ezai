# Model Routing Design (Registry v2)

**Problem:** today routing lives in two hand-maintained places — LiteLLM's
config (model aliases → engine) and per-repo `.agent/model_registry.yaml`
(role → primary/fallback, ADR-020). Productization requires logical
roles, model groups, provider awareness, lifecycle states, and rollback —
**with zero hand-editing after installation** and full backward
compatibility with ADR-020.

## 2. The routing model — four layers

```
ROLE (what the agent does)      orchestrator planner coder debugger reviewer
                                memory chat  (+ documentation evolution sprint)
        │ role → group (+ optional pins)
        ▼
GROUP (what kind of model)      reasoning · coding · chat
        │ group → ordered member list
        ▼
MODEL (a concrete artifact)     name, provider, format/quant, context, state
        │ model → provider runtime
        ▼
PROVIDER (how it is served)     llama.cpp | vLLM  → engine slot :8000 → LiteLLM :4000
```

Resolution: **role → (pin | group) → ordered models → first ACTIVE model =
primary; the rest of the order = the fallback chain** — feeding exactly
the existing runtime mechanism (`llm.roles` / `llm.role_fallbacks`,
ADR-020). The runtime does not change; only where its inputs come from
does.

## 3. Logical roles (V1)

| Role | Default group | Serves | Notes |
|---|---|---|---|
| `orchestrator` | reasoning | the OpenWebUI Orchestrator persona; chat-ops routing | **new role**, additive to the existing roles dict |
| `planner` | reasoning | Planner Agent | |
| `coder` | coding | Coding Agent | |
| `debugger` | reasoning | Debug Agent | |
| `reviewer` | reasoning | Reviewer Agent (gate) | |
| `memory` | chat | Memory distillation (optional) | |
| `chat` | chat | plain chat + `local-ezai chat` | |
| `documentation` / `evolution` / `sprint` | chat / reasoning / reasoning | existing agents | preserved unchanged |
| `validator` / `git` | — | deterministic; no LLM | routing N/A, listed for completeness |

## 4. Registry v2 — declarative state

Platform scope, git-versioned, mutated **only** through the lifecycle
operations ([MODEL_LIFECYCLE_MANAGEMENT.md](MODEL_LIFECYCLE_MANAGEMENT.md)):

```yaml
# config/models/registry.yaml   (illustrative schema, not hand-edited)
version: 2
generation: 14                  # monotonically increasing; enables rollback
providers: [llamacpp, vllm]     # descriptors in config/providers/*.yaml

models:
  hermes3:
    provider: vllm
    source: {hf: NousResearch/Hermes-3-Llama-3.1-8B}
    groups: [reasoning]
    context: 16384
    state: active               # lifecycle state, see MODEL_LIFECYCLE_MANAGEMENT
    benchmarks: {last: 2026-09-01T..., tokens_per_s: 41.2, eval_pass: true}
  qwen3-coder:
    provider: vllm
    groups: [coding]
    state: active
  deepseek-r1:
    provider: llamacpp
    source: {gguf: ...-Q4_K_M.gguf}
    groups: [reasoning, coding]
    state: active
  llama3:
    provider: llamacpp
    groups: [chat, reasoning]
    state: active

groups:                          # ordered = priority = fallback order
  reasoning: [hermes3, deepseek-r1, llama3]
  coding:    [qwen3-coder, deepseek-r1]
  chat:      [llama3, hermes3]

roles:                           # group by default; explicit pin optional
  orchestrator: {group: reasoning}
  planner:      {group: reasoning}
  coder:        {group: coding}
  debugger:     {group: reasoning, pin: deepseek-r1}   # pin overrides order
  reviewer:     {group: reasoning, pin: llama3}
  memory:       {group: chat, pin: hermes3}
  chat:         {group: chat}
  documentation: {group: chat, pin: llama3}
  evolution:    {group: reasoning, pin: deepseek-r1}
  sprint:       {group: reasoning}
```

The pins above reproduce today's mandated CLAUDE.md `agent_model_map`
exactly — Registry v2 is a superset, not a replacement.

### Resolution algorithm (deterministic, in code)

```
resolve(role):
  entry = roles[role]                    # else role 'default'
  order = groups[entry.group]
  if entry.pin: order = [pin] + (order without pin)
  serving = [m for m in order if models[m].state == active]
  primary = serving[0]; fallbacks = serving[1:]
  → llm.roles[role]=primary ; llm.role_fallbacks[role]=fallbacks
```

A role whose group has **no active model** fails resolution loudly at
render time — never silently at request time.

> **As-built refinement (PR-1, `agentd/src/agentd/registry_v2.py`):** a
> `pin` is an **explicit ordered chain** (string or list) and does *not*
> inherit group fallbacks — what you pin is what you get. The golden test
> forced this: CLAUDE.md's `reviewer: llama3` has no fallback, which the
> "pin + rest of group" rule above could not reproduce. Unpinned roles
> resolve through their group exactly as specified. The reference default
> set ships as packaged data (`agentd/src/agentd/defaults/
> reference_registry.yaml`); the live instance file is created by the
> bootstrap (PR-7), not by PR-1.

## 5. Compatibility & precedence

Effective routing for a run =

```
Registry v2 (platform)  <  per-repo .agent/model_registry.yaml (ADR-020)
```

- The per-repo file keeps its existing format and semantics (a repo can
  pin its own roles); nothing existing breaks.
- `prepare_run` continues to call `apply_model_registry`; it now seeds the
  config from Registry v2 resolution first (via the rendered defaults),
  then applies repo overrides — same override philosophy as `.agentd.yaml`.

## 6. Rendered artifacts (never hand-edited after install)

From `registry.yaml` + provider descriptors, the control plane **renders**:

1. **LiteLLM config** — one alias per model + one alias per role
   (`role-orchestrator`, …), all pointing at the engine slot per ADR-001.
   Today's hand-edited `config/litellm_config.yaml` becomes generated
   output with a "GENERATED — edit via local-ezai/Admin Center" header.
2. **Engine slot materialization** — which provider/profile/weights the
   `vllm`-named service loads ([PROVIDER_ABSTRACTION.md](PROVIDER_ABSTRACTION.md)).
3. **Runtime defaults** — the role→primary/fallback map handed to agentd.

Render is atomic per generation: write generation N+1, validate, reload,
record; rollback = re-render generation N
([MODEL_LIFECYCLE_MANAGEMENT.md](MODEL_LIFECYCLE_MANAGEMENT.md) §4).

## 7. Explain routing (the transparency contract)

Three levels, all served from the same resolution:

| Question | Surface |
|---|---|
| "What WOULD serve role X and why?" | `local-ezai model explain <role>` / Admin Center **Routing** page: pin/group, order, states, active generation, and the reason each earlier candidate was skipped |
| "What is the standing table?" | `local-ezai models` (existing, now Registry-v2-backed) / Routing page table |
| "What DID serve stage Y of run Z?" | `local-ezai explain-run` (existing, fallback-aware `models_used`) / run detail page |

Every explain answer names the **generation** it was resolved from, so an
answer is reproducible even after later changes.

## 8. Fallback semantics (unchanged, restated)

Request-time fallback walks the chain on model failure (journaled
`LLM_FALLBACK`, ADR-020). Routing-time resolution (§4) decides the chain;
request-time behavior stays exactly the shipped mechanism. A model in any
non-`active` state is invisible to resolution — lifecycle is the single
lever that changes routing.
