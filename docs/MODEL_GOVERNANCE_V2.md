# Model Governance V2 — govern roles, reveal models

**The question posed:** should the platform (A) expose model names
directly, or (B) expose logical roles and hide implementation models?

## 1. Recommendation: **B, amended — govern roles, *reveal* models**

Pure (A) re-couples every surface to model families (finding CF-9) and
makes every model swap a user-facing breaking change. Pure (B) — actually
*hiding* models — would violate the platform's transparency pillar
(explain-routing, benchmark evidence, audit) and CLAUDE.md governance,
which demands humans approve *model replacement* — you cannot approve what
you cannot see.

Therefore:

| Concern | Interface |
|---|---|
| What users/agents/code **depend on** | **logical roles** (orchestrator, planner, coder, debugger, reviewer, memory, chat) and **groups** (reasoning, coding, chat) — the only stable names |
| What governance **decides about** | group membership/order, role→group mapping, pins, runtime — via generations |
| What is always **visible as evidence** | concrete model names, sources, licenses, benchmarks — on cards, approval modals, explain views, audit records |
| What is **never** required | typing/knowing a model name to *use* the platform |

Consequences (remediation R-1/R-6, [V1_PRODUCT_REVIEW.md](V1_PRODUCT_REVIEW.md)):

- LiteLLM serves **role aliases** (`role-chat`, `role-planner`, …);
  agentd defaults bind to aliases — **no model name lives in code**
  (completes ADR-007; fixes CF-3).
- OpenWebUI's selector leads with role entries; raw names behind an
  advanced toggle ([WEBUI_PRODUCT_STRATEGY.md](WEBUI_PRODUCT_STRATEGY.md) §4).
- `.env` bootstrap uses **group seeds** (`REASONING_MODEL`, `CODING_MODEL`,
  `CHAT_MODEL`) — the user names models exactly once, to fill groups
  ([FINAL_FIRST_RUN_EXPERIENCE.md](FINAL_FIRST_RUN_EXPERIENCE.md) §2).
- The CLAUDE.md `agent_model_map` is honored as the *reference default
  data set*; the governed objects are the role/group structures it fills.

## 2. What is governed (approval matrix v2)

| Change | Approval? | Why |
|---|---|---|
| group membership/order change touching a serving role | **yes** | model replacement (CLAUDE.md) |
| role→group remap or pin change | **yes** | changes who serves a role |
| runtime switch | **yes** | changes how everything is served |
| install / benchmark / retire(non-serving) / uninstall(retired) | no (audited) | doesn't change service |
| rollback to a prior generation | no (audited, notifies) | restores an approved state; incident speed |
| bootstrap generation 1 | implicit (the human wrote `.env`) | there is no prior state to protect |

## 3. Role contracts (new, closes the loop with agnosticism)

Each role carries a declarative **contract** — requirements a resolved
model+runtime must satisfy at render time
([RUNTIME_ABSTRACTION_STRATEGY.md](RUNTIME_ABSTRACTION_STRATEGY.md) §4):

```yaml
roles:
  coder:    {group: coding,    requires: {tool_calling: true,  min_context: 8192}}
  planner:  {group: reasoning, requires: {tool_calling: true,  min_context: 8192}}
  reviewer: {group: reasoning, requires: {tool_calling: true,  json_output: true}}
  chat:     {group: chat,      requires: {min_context: 4096}}
  …
```

Contracts are model-family-agnostic (capabilities, not names) and make
"can this model serve this role?" a checkable question — the approval
modal shows the contract check, and `model explain <role>` cites it.

## 4. Transparency triad (unchanged, restated as governance duties)

`model explain <role>` (what would serve and why, per generation) ·
`models`/Routing page (the standing table) · `explain-run` (what actually
served each stage, fallback-aware). Every governance decision links the
generation and evidence snapshot it was made on — decisions are
reproducible forever.

## 5. Self-evolution's role: **advisor, never operator**

The Evolution Agent participates in model governance through one lane —
**advisory artifacts into the existing queue** — with hard boundaries:

| Activity | How | Boundary |
|---|---|---|
| **Benchmark review** | after each `evaluate-models`, evolution may attach a *reading*: regressions, drift, weak quality rates, per-role narrative (extends the existing trend evidence, ADR-024) | commentary only; changes nothing |
| **Model recommendations** | may propose catalog/user-source candidates whose declared capabilities fit a role contract + observed weakness ("reviewer JSON failures ↑; candidate X declares json_output, fits memory") | a *suggestion card* in the Admin Center; installing it is a human click |
| **Upgrade proposals** | may file a **pre-filled activation request** (generation diff + evidence + its reasoning) into the Governance queue, marked `proposed-by: evolution` | identical gate as human requests; **never auto-approved**; rate-limited (≤1 open routing proposal at a time) |

Safeguards (mostly already shipped, now bound to this lane):

1. **No write path exists** from evolution to generations — it can only
   create queue items; the renderer runs solely on human approval.
2. **Rejections are memory** — a rejected proposal is recorded
   (failed-experiment record + reason); the no-repeat rule
   (ADR-024/prompt hard rules) prevents re-proposing it.
3. **Evidence or silence** — a proposal without benchmark data on *this
   hardware* is invalid by schema; "the internet says model X is good" is
   not evidence.
4. **Scope** — evolution proposes routing/upgrade changes only through
   this lane; its code-improvement lane (evolve PRs) remains separate and
   PR-gated as today.

Net effect: the platform notices its own model-quality problems, does the
research, fills in the paperwork — and a human remains the only one
holding the pen that signs.

## 6. Migration note (from MODEL_ROUTING_DESIGN/MODEL_LIFECYCLE v1 docs)

Everything in those documents stands; V2 adds role contracts (§3), the
role-alias completion of ADR-007, the reveal-don't-hide UX ruling, the
approval matrix as written here, and the evolution advisory lane. No
schema breaks: contracts and `proposed-by` are additive fields.
