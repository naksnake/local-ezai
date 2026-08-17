# Recovery Plan — Aligning the Autonomous SWE Subsystem with Local-EZAI

**Date:** 2026-08-17
**Input:** [SESSION_RECOVERY_REPORT.md](SESSION_RECOVERY_REPORT.md) (audit
against the newly added CLAUDE.md)
**Governing rules:** CLAUDE.md — *always extend, always integrate, always
preserve backward compatibility; agents propose, humans approve.*

**What this plan is:** the sequence that turns the current
"correct-but-parallel" SWE subsystem into an **integrated feature of the
Local-EZAI platform**, per the product tree
(Chat / Agents / Models / MCP / Knowledge / Tools / Autonomous SWE).

**What this plan is not:** a redesign or a removal. Nothing built in
Phases 0–6 is deleted; nothing in the existing stack is modified beyond
the same additive conventions used so far. Deprecations (phase R4) are
proposals that require explicit human approval before any code changes.

---

## Guiding corrections (from the audit)

1. **Integration over adjacency.** The platform's own surfaces (OpenWebUI
   chat, mcpo, Qdrant, SearXNG, monitor) become the primary way to reach
   and observe the SWE subsystem; the CLI remains as the developer/CI
   interface, explicitly positioned as *an interface to* Local-EZAI.
2. **The rules file is the routing authority.** `agent_model_map` from
   CLAUDE.md becomes a shipped configuration, with fallback support.
3. **Finish the mandated roster** (Documentation, Evolution) before any
   new invented capabilities.
4. **Governance before autonomy growth.** The propose→PR→approve loop is a
   prerequisite for the self-evolution ambitions, not an afterthought.

---

## Phase R1 — Platform integration layer *(highest priority)*

Realizes: audit items **I1, I2, I3, I4**. Everything additive.

| Step | Deliverable | Existing asset reused |
|---|---|---|
| R1.1 | **SWE MCP toolset**: a small MCP server (vendored like `mcp-servers/qdrant-rag/`) exposing `swe_run`, `swe_sprint`, `swe_status`, `swe_report`, `swe_plan`; registered by an **additive block** in `config/mcpo-config.json`; auto-appears in OpenWebUI via the existing `TOOL_SERVER_CONNECTIONS` mechanism → **platform chat drives autonomous SWE** | mcpo, OpenWebUI tool auto-registration |
| R1.2 | **`kb_search` agent tool (T1)**: Planner, Debugger, and Sprint agents query the platform knowledge base | Qdrant + embed-server + the payload conventions of `embed_documents.py` |
| R1.3 | **`web_search`/`web_fetch` agent tools (T1)**: agents research documentation the way platform chat already can | SearXNG JSON API, fetch pattern |
| R1.4 | **Knowledge write-back**: on sprint/run completion, ingest the sprint report and new `lessons_learned` entries into a dedicated Qdrant collection (`swe-lessons`) — platform chat can answer "what did the sprint change?" | embed pipeline; auto-RAG hook picks it up automatically |
| R1.5 | **Monitor visibility**: a runs/sprints card sourced from `~/.agentd/runs` report.json files — either a standalone page served by agentd or an optional monitor card (monitor code untouched by default) | journal/report files (ADR-006) |

**Acceptance:** from a stock OpenWebUI chat, a user can launch a sprint,
watch its status, and read its report — without installing the CLI. Agent
prompts show KB/web evidence in journals. Existing chat behavior unchanged.

**Guards:** all new tools carry the existing T0–T4 tiers; `swe_*` mutating
tools are T3-equivalent behind `MCP_API_KEY` + explicit enablement;
fail-closed defaults unchanged.

## Phase R2 — Model routing alignment

Realizes: **I5** (+ **I8**).

- Extend role resolution to `primary` + ordered `fallback` models (retry on
  transport failure/unavailability; journaled `MODEL_FALLBACK` events).
- Ship `config/agentd.roles.claude-md.yaml` matching `agent_model_map`
  verbatim (planner→hermes3/deepseek-r1, coder→qwen3-coder/deepseek-r1,
  debugger→deepseek-r1/hermes3, reviewer→llama3, documentation→llama3,
  memory→hermes3, evolution→deepseek-r1) plus the LiteLLM alias blocks
  needed to serve those names — as **new** config files referenced by an
  additive compose overlay, never edits to existing profiles.
- Add `documentation` and `evolution` roles to defaults; add the `type`
  validation category (mypy/pyright/tsc) alongside test/lint/build.

**Acceptance:** with the CLAUDE.md profile loaded, journals show each agent
using its mandated model and falling back correctly when the primary is
down. Existing single-model deployments keep working with zero config
changes (roles default as today).

## Phase R3 — Roster completion & governance

Realizes: **I6, I7** — the two missing CLAUDE.md agents.

- **Documentation Agent** (`documentation` role, T2 within workspace):
  generates and maintains `docs/USER_GUIDE.md`, `docs/OPERATION_MANUAL.md`,
  `docs/MAINTENANCE_GUIDE.md`, `docs/RELEASE_NOTES.md`; absorbs sprint
  report generation as one duty; runs as an optional pipeline stage after
  REVIEWING and as `local-ezai docs`.
- **Evolution Agent** (`evolution` role): the CLAUDE.md workflow —
  Analyze → Propose (structured proposal envelope, human-readable) →
  Implement (standard run pipeline on a branch) → Validate → **Pull
  Request** → **Human approval**. Requires a `pr_create` tool (T3,
  fail-closed like `git_push`) targeting the configured forge.
- **Approval gates**: the CLAUDE.md protected list (architecture
  migrations, core runtime changes, model replacement, production
  releases, PR merges) becomes an explicit policy table in the permission
  engine — agents can *propose* these only; nothing auto-executes.

**Acceptance:** `local-ezai evolve` (or the `swe_evolve` MCP tool) produces
a validated branch + PR + proposal document and then **stops**, awaiting a
human. Self-improvement runs against local-ezai itself are benchmarked and
documented per the Self-Evolution section.

## Phase R4 — Consolidation *(requires human approval — proposals only)*

Realizes: **I9**; addresses D1/D3/R1/R2 from the audit. Because this phase
touches surfaces users may already depend on, each item is a proposal:

| Proposal | Change | Backward compatibility |
|---|---|---|
| P1 | Fold legacy `ezai` into `local-ezai` (`ezai` becomes a thin alias printing a deprecation notice; removal only in a future major version, if ever) | alias preserved indefinitely by default |
| P2 | Reposition `local-ezai chat`: banner + docs state it is a terminal convenience; point users to OpenWebUI for the full platform chat; optionally add `--open-webui` hint printing the platform URL from config | command keeps working unchanged |
| P3 | Documentation pass: README/agentd docs rewritten to present Autonomous SWE as **a feature of Local-EZAI**, with the platform tree from CLAUDE.md as the table of contents | docs only |
| P4 | Record the whole correction as **ADR-020** (integration-first realignment) and refresh `.agent/architecture.md` + `roadmap.md` accordingly | docs only |

**No deletions occur in this phase without explicit sign-off.**

## Phase R5 — Bootstrap exit runway *(after R1–R3 land)*

Per CLAUDE.md's Self-Evolution and Bootstrap Exit sections:

- a benchmark suite for self-runs (fixture tasks + the platform's own
  repo), tracked in-repo;
- scheduled self-maintenance sprints (Evolution Agent proposals: docs
  freshness, dependency pins, failing-check triage) — always ending in
  PRs, never in direct pushes;
- exit criterion: a roadmap item can travel *Human → Roadmap → Local-EZAI*
  (sprint spec → autonomous PR → human merge) with Claude Code uninvolved.

---

## Sequencing & effort

```
R1 platform integration  ────►  R2 model routing ──►  R3 roster+governance
        (M, additive)              (S/M)                    (L)
                                                             │
R4 consolidation (proposals, human-gated) ◄──────────────────┘
R5 bootstrap exit (ongoing, after R3)
```

R1 and R2 are independent and can proceed in parallel. R4 must not start
before a human reviews this plan (CLAUDE.md: agents propose, humans
approve). Every phase keeps the two invariants that survived the audit
intact: **the existing stack is never modified beyond additive overlays**,
and **all validation/commit gates stay fail-closed**.

## Immediate next actions (this branch, no approval needed)

1. ✅ This report + plan committed (`docs/SESSION_RECOVERY_REPORT.md`,
   `docs/RECOVERY_PLAN.md`).
2. On approval of this plan: open ADR-020, update `.agent/roadmap.md` with
   phases R1–R5, and begin R1.1 (SWE MCP toolset) — the single change that
   most directly fulfills *"The Autonomous SWE subsystem must integrate
   into Local-EZAI."*
