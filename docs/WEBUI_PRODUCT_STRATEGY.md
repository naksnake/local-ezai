# WebUI Product Strategy — the final user-facing experience

**Goal:** after setup, a user with **no CLI knowledge** manages the entire
platform — runtime, models, projects, agents, sprints, evolution — through
the browser. Two connected surfaces, one product: **OpenWebUI** (do work)
and the **Admin Center** (govern the platform), cross-linked everywhere
([WEBUI_ADMIN_CENTER.md](WEBUI_ADMIN_CENTER.md) remains the page-level
spec; this document is the product experience over it).

## 1. Experience principles

1. **Roles first, models as evidence** — the UI speaks *Reasoning / Coding
   / Chat* and agent roles; concrete model names appear as the
   implementation detail on cards and evidence panels
   ([MODEL_GOVERNANCE_V2.md](MODEL_GOVERNANCE_V2.md) §2).
2. **Nothing ships itself** — every consequential button leads through the
   Governance queue with evidence beside the Approve button.
3. **Everything measured locally** — fit badges and tokens/sec come from
   *this box's* benchmarks, never vendor claims
   ([HARDWARE_AGNOSTIC_ARCHITECTURE.md](HARDWARE_AGNOSTIC_ARCHITECTURE.md) §4).
4. **Deep links both ways** — chat answers link to Admin pages; Admin
   pages offer "discuss in chat" (opens the Orchestrator with context).
5. **CLI parity, not CLI dependence** — every action here maps to a CLI
   verb ([CLI_AND_WEBUI_STRATEGY.md](CLI_AND_WEBUI_STRATEGY.md)); the UI
   shows the equivalent command in a footer tooltip (teaches, never
   requires).

## 2. Navigation map

```
OpenWebUI (:3000)                       Admin Center (:8888)
├─ Chat (models = role entries §4)      ├─ Overview
├─ Orchestrator persona  ──deep links──►├─ Models        (role/group first)
├─ Knowledge (existing)                 ├─ Runtime
└─ Tools: SWE tool server               ├─ Projects
                                        ├─ Agents & Runs
                                        ├─ Sprints
                                        ├─ Evolution
                                        ├─ Governance   (queue + history)
                                        ├─ Memory · Knowledge · Health
```

## 3. Wireframes (Admin Center)

### 3.1 Overview
```
┌ Local-EZAI ────────────────────────────── admin@host · gen 14 ┐
│ ● Platform healthy   Runtime: llamacpp on cpu-standard        │
│                                                               │
│  REASONING            CODING              CHAT                │
│  ┌───────────────┐    ┌───────────────┐   ┌───────────────┐   │
│  │ hermes3       │    │ qwen2.5-coder │   │ llama-3-8b    │   │
│  │ 41 tok/s ✓fit │    │ 38 tok/s ✓fit │   │ 52 tok/s ✓fit │   │
│  │ +1 fallback   │    │ +1 fallback   │   │ +1 fallback   │   │
│  └───────────────┘    └───────────────┘   └───────────────┘   │
│                                                               │
│  ⚠ 2 items await your approval            [Open Governance]   │
│  Recent: run swe/a1b2 ✔ · sprint s-9 ✔ · evolve ev-3 ⏳queue   │
└───────────────────────────────────────────────────────────────┘
```

### 3.2 Models (role-first; models inside)
```
┌ Models ────────────────────────────────── [＋ Add model] ─────┐
│ Groups                        Roles                           │
│ ┌ REASONING ────────────────┐ ┌────────────────────────────┐  │
│ │ 1 hermes3      ACTIVE ●   │ │ planner    → REASONING     │  │
│ │ 2 deepseek-r1  ACTIVE ●   │ │ debugger   → 📌 deepseek-r1│  │
│ │ 3 mistral-x    installed ○│ │ reviewer   → 📌 llama-3-8b │  │
│ │   [benchmark] [activate…] │ │ orchestrator → REASONING   │  │
│ └───────────────────────────┘ │ …            [explain any] │  │
│ ┌ CODING … ┐ ┌ CHAT … ┐       └────────────────────────────┘  │
│                                                               │
│ model card (click): source · runtime · quant · context ·      │
│  state · fit badge · bench history sparkline · [upgrade]      │
│  [retire] [rollback to gen N] — mutating buttons queue-gated  │
└───────────────────────────────────────────────────────────────┘
```
"Add model" accepts a catalog pick **or any `hf:`/`gguf:` source** —
user-supplied models are equal citizens (R-5).

### 3.3 Runtime
```
┌ Runtime ──────────────────────────────────────────────────────┐
│ Active: llamacpp   (engine slot · class cpu-standard)          │
│   serves: gguf · multi-model ✓ · hot-swap ✓ · tool-calls ✓     │
│ Available: vllm — requires HF/AWQ formats                      │
│   [Switch to vllm…] → pre-check report:                        │
│      ✓ hermes3 (hf source available)                           │
│      ✗ llama-3-8b is GGUF-only → install HF variant or keep    │
│        llamacpp                     [resolve] [cancel]         │
│  (switch = governed generation change; rollback always listed) │
└───────────────────────────────────────────────────────────────┘
```

### 3.4 Agents & Runs
```
┌ Agents & Runs ────────────────── filter: [all|swe|fix|evolve] ┐
│ run a1b2  ✔ completed  "add JWT auth"  crm  branch swe/a1b2   │
│ ├ Plan (3 tasks) · Validation ✔ (+Browser QA) · Review ✔       │
│ ├ Healing: 1 iteration (root cause shown)                      │
│ ├ Models used: planner hermes3 · coder qwen2.5-coder …         │
│ └ [journal] [exec audit] [discuss in chat] [how to merge]      │
│ run c3d4  ✖ review-blocked  … findings: 1 high (security)      │
└────────────────────────────────────────────────────────────────┘
```
"How to merge" shows the copy-paste git commands + explains why merging
stays human (the one deliberate CLI/console hand-off, stated honestly).

### 3.5 Sprints
```
┌ Sprints ─────────────────────────────── [＋ New sprint] ──────┐
│ [＋] = paste/write markdown spec → target project → dry-run    │
│      plan preview (tasks + dependency graph) → [Start]         │
│ sprint s-9 ✔  6/6 tasks · 2 waves · report: docs/sprints/s-9   │
│   (mermaid dependency graph rendered inline)                   │
└────────────────────────────────────────────────────────────────┘
```

### 3.6 Evolution
```
┌ Evolution ────────────────────────────────────────────────────┐
│ [Run evolution cycle ▸]  focus: [optional text]  target: repo  │
│ ev-3 ⏳ awaiting approval                                       │
│  ├ evidence: 2 repeated failure signatures · reviewer latency ↑ │
│  ├ proposal: 2 improvements (titles…)                           │
│  ├ benchmark: before 58s ✔ → after 41s ✔                        │
│  └ [open in Governance]                                        │
│ history: ev-2 approved→merged · ev-1 rejected (reason logged)  │
└────────────────────────────────────────────────────────────────┘
```

### 3.7 Governance — approval modal (the platform's most important screen)
```
┌ Approve: activate mistral-x for REASONING (gen 14 → 15) ──────┐
│ WHAT CHANGES   reasoning order: [mistral-x, hermes3, …]        │
│                roles affected: planner, orchestrator (…)       │
│ EVIDENCE       bench: 47 tok/s (hermes3: 41) · probes ✓ JSON ✓ │
│                fit ✓ (est. 9.1 GB of 16 GB) · source, license  │
│ PROPOSED BY    evolution ev-4 (advisory)   [view reasoning]    │
│ REVERSIBILITY  one-click rollback to gen 14                    │
│           [ Reject (reason…) ]      [ Approve & apply ]        │
└────────────────────────────────────────────────────────────────┘
```

## 4. OpenWebUI model selector (role entries)

The selector leads with governed **role entries** — `EZAI Chat`,
`EZAI Reasoning`, `EZAI Coding`, `EZAI Orchestrator` — each subtitled with
its current implementation ("Chat · currently llama-3-8b · gen 14").
Direct model-name entries move behind an admin-off-by-default "advanced
models" toggle. Users think in capabilities; audit still sees names.

## 5. Journeys that must need zero CLI (release-gated walkthroughs)

1. Swap the reasoning model: Models → Add model (`hf:` URI) → benchmark →
   activate → approve → chat verifies via role subtitle.
2. Switch runtime llamacpp→vllm: Runtime → pre-check → resolve format gap
   → approve → rollback drill.
3. Register a project and run a sprint from a pasted spec; read the
   report; follow "how to merge".
4. Trigger evolution, reject its proposal with a reason; verify the
   rejection lands in memory (no re-proposal).
5. Roll back a bad activation from the Overview banner in ≤ 3 clicks.

Each journey ships as a Browser-QA workflow in CI — the walkthroughs *are*
the tests.
