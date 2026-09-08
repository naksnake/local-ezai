# V1 Implementation Plan (Productization Phase)

> **2026-09-01 product review (ADR-026):** the agnosticism review
> ([V1_PRODUCT_REVIEW.md](V1_PRODUCT_REVIEW.md)) amends this plan without
> resequencing it — **P1 additionally carries remediations R-1…R-5**
> (role aliases end model names in code; capability classes replace SKU
> profiles; neutral `engine` alias; capability negotiation; catalog as
> data + recommender) and **P5 implements
> [FINAL_FIRST_RUN_EXPERIENCE.md](FINAL_FIRST_RUN_EXPERIENCE.md)** (the
> `.env`-seeded bootstrap, F7–F11) in place of the interactive-choice
> wizard. P6's release gates gain H1–H4
> ([HARDWARE_AGNOSTIC_ARCHITECTURE.md](HARDWARE_AGNOSTIC_ARCHITECTURE.md) §6)
> and the third-runtime drill
> ([RUNTIME_ABSTRACTION_STRATEGY.md](RUNTIME_ABSTRACTION_STRATEGY.md) §6).

> **PR-level decomposition:** [V1_PR_PLAN.md](V1_PR_PLAN.md) breaks these
> phases into 26 individually reviewable pull requests with dependency
> graph, sizing, per-PR gates, and a PR-ready checklist.

**Input:** the eight productization architecture documents
([TARGET_PRODUCT_V1.md](TARGET_PRODUCT_V1.md) and its references).
**Constraints:** extend-only (no redesign), every existing platform and
Autonomous SWE capability preserved and tested at every phase, CLAUDE.md
governance (agents propose, humans approve) applies to the plan itself.
**Method:** each phase is sized to be executable as platform sprints
(`local-ezai . sprint`) with human-reviewed PRs — productization is built
by the product wherever practical (bootstrap exit in action).

## 0. Phase overview & dependencies

```
P1 Registry v2 + PAL + Lifecycle (CLI-first)      ← foundation, no new services
   │
P2 ezaid Control Plane (OpenAPI)                  ← wraps P1 + existing pipelines
   ├──────────────┬──────────────────┐
P3 OpenWebUI      P4 Admin Center    P5 First-Run Experience
   integration       (monitor ext.)     (installer + wizard)
   └──────────────┴──────────────────┘
P6 Parity, hardening, soak → V1 release
```

P1→P2 are strictly sequential; P3/P4/P5 parallelize after P2; P6 gates the
release. Every phase ends with: suite green (311+ and growing), ruff
clean, docs + ADR in the same PR, human review.

## P1 — Registry v2, Provider Abstraction, Model Lifecycle (CLI-first)

**Builds:** `config/models/registry.yaml` + generations store; provider
descriptors + renderer (compose override, LiteLLM config, runtime role
map); lifecycle engine with the state machine and operations
(install/benchmark/activate/upgrade/rollback/retire/explain); curated
model catalog (per profile); CLI namespaces `model`, `governance`
(file-backed queue), `project`, `status`, `up/down` wrappers;
`prepare_run` seeding from Registry v2 (per-repo ADR-020 overrides
preserved).
**Explicitly not yet:** any new service, any UI.
**Key risks:** LiteLLM hot-reload behavior; vLLM restart windows →
mitigated by the render/reload/health/self-rollback protocol
([MODEL_LIFECYCLE_MANAGEMENT.md](MODEL_LIFECYCLE_MANAGEMENT.md) §4) and the
side-load design ([PROVIDER_ABSTRACTION.md](PROVIDER_ABSTRACTION.md) §6).
**Exit criteria:**
1. install → benchmark → activate(approve) → upgrade → rollback, entirely
   via CLI, zero file edits, full audit + generations diffable;
2. resolution reproduces today's CLAUDE.md routing byte-for-byte on a
   migrated install (golden test);
3. `config/litellm_config.yaml` is rendered output with drift detection;
4. all existing tests green; offline/direct CLI mode untouched.
**ADR:** ADR-027 (Registry v2 + PAL + generations).

## P2 — `ezaid` Platform Control Plane

**Builds:** the OpenAPI service (:8010) wrapping P1 lifecycle + existing
run pipelines (async run registry with ids/status/cancel), governance
queue, health aggregation; service-token auth + forwarded user identity;
append-only audit log; CLI connected-mode transport (auto-detect,
identical UX; direct mode remains for repo work)
([CLI_AND_WEBUI_STRATEGY.md](CLI_AND_WEBUI_STRATEGY.md)).
Deployment: one new compose service in the existing overlay pattern
(ADR-002), port via `EZAI_CONTROL_PORT`.
**Exit criteria:**
1. every parity-matrix CLI verb works identically in direct and connected
   mode (parity harness, §P6 seed);
2. two long runs supervised concurrently through the API (start, status,
   report, cancel);
3. kill-the-daemon test: repo work via CLI unaffected;
4. OpenAPI spec published and versioned — the contract artifact.
**ADR:** ADR-028 (control plane).

## P3 — OpenWebUI integration (SWE tool server + Orchestrator)

**Builds:** the `swe-server` MCP server behind mcpo (tool catalog per
[OPENWEBUI_INTEGRATION.md](OPENWEBUI_INTEGRATION.md) §2, thin adapter over
ezaid); the `orchestrator` role (registry entry + LiteLLM alias + system
preset); markdown run-report rendering; project allowlist enforcement;
first-run pre-registration of the tool server.
**Exit criteria:**
1. plan → confirm → run → report loop completed from one OpenWebUI
   conversation against the sample project;
2. governance actions provably absent from the tool surface (negative
   tests);
3. prompt-injection drill: a hostile chat task cannot push, merge,
   activate, or leave the sandbox (red-team script in CI);
4. existing chat/RAG/tool behavior byte-identical (regression pass).
**ADR:** ADR-029 (chat-ops boundary).

## P4 — Admin Center (monitor evolution)

**Builds:** the pages and actions of
[WEBUI_ADMIN_CENTER.md](WEBUI_ADMIN_CENTER.md) on the existing monitor
service, all via ezaid; governance queue UX with evidence panels;
OpenWebUI → Admin Center trusted-header handoff (optional, Basic auth
fallback); deep links from CLI/chat outputs.
**Validation:** the platform's own **Browser QA agent** runs declarative
workflows against the Admin Center (login, install→activate approval,
rollback, run detail) — the product is tested by its own testing
capability.
**Exit criteria:**
1. a model activation is requested in CLI and approved in the Admin Center
   (and vice versa), one shared queue;
2. an evolution PR is reviewed end-to-end from the Governance page;
3. Browser QA workflow suite green in CI;
4. monitor's pre-existing functions (health, KB) intact.
**ADR:** ADR-030 (Admin Center + SSO handoff).

## P5 — First-Run Experience

**Builds:** `install.sh` (hardware detect, `.env` generation with minted
secrets, single review-edit stop, profile bring-up); Model Bootstrap
wizard (Admin Center flow + `local-ezai init` CLI twin) per
[FIRST_RUN_EXPERIENCE.md](FIRST_RUN_EXPERIENCE.md); curated catalog
content for all four hardware profiles; bundled sample project; smoke
suite; offline bundle path.
**Exit criteria:** the six FRE acceptance criteria F1–F6, each scripted;
onboarding Browser-QA'd in CI; re-run safety proven.
**ADR:** ADR-031 (installer & onboarding).

## P6 — Parity, hardening, soak → V1

**Builds/does:** the cross-surface **parity harness** as a release gate
(CLI-direct vs CLI-connected vs API state/audit equivalence); security
pass over the new boundary (token handling, tool-surface ceiling, header
SSO); documentation set refresh (USER_GUIDE, OPERATION_MANUAL,
CLI_REFERENCE, TROUBLESHOOTING absorb the new surfaces; MAINTENANCE_GUIDE
gains generation/rollback ops); 72 h soak on GPU + n97 profiles with
scheduled runs, evolutions, and lifecycle churn; carry-over roadmap items
N1′ (container hardening) folded into the soak gate.
**Exit criteria:**
1. parity harness green across the matrix;
2. soak: zero unexplained failures, rollback exercised under load;
3. product Definition of Done ([TARGET_PRODUCT_V1.md](TARGET_PRODUCT_V1.md) §8)
   checked item-by-item;
4. human release sign-off (governance: production releases require
   approval) → tag **v1.0.0**.

## Cross-cutting workstreams (every phase)

- **Preservation proof:** the full pre-existing suite runs unmodified in
  CI; any needed test change is itself review-flagged.
- **Self-building:** each phase drafted as sprint specs; where the
  autonomous pipeline implements a slice, its PRs carry the standard
  gates (validation, Browser QA, reviewer gate) — dog-fooding metrics
  (success rates from the benchmark dashboard) are reported per phase.
- **Docs-as-code:** every phase updates its architecture doc from
  "designed" to "as-built" in the same PR, plus its ADR.
- **Governance checkpoints:** phase completion reviews are human;
  activation-style irreversible changes never batch inside a phase.

## Out-of-scope backlog (post-V1, from the architecture docs)

Second engine slot (PAL §7) · additional providers (TGI/ollama) ·
OpenWebUI-native SSO beyond trusted headers · scheduled evolution cadence
+ auto-proposed routing PRs (N5′/N6′ — queue formats already compatible) ·
multi-node / multi-tenant.
