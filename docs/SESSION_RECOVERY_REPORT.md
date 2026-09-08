# Session Recovery Report — Audit Against CLAUDE.md

**Date:** 2026-08-17
**Trigger:** `CLAUDE.md` (project rules) was added to the repository
(`origin/main dabb53e`, mirrored on this branch as `7bf26f5`) **after**
Phases 0–6 of the Autonomous SWE subsystem were implemented on
`claude/local-ezai-architecture-kc6gcb`. This report audits everything
built this session against those rules.
**Scope:** current repository state · existing Local-EZAI functionality ·
newly implemented functionality (`agentd/`, ~6,300 src LOC + ~4,100 test
LOC, 240 tests).
**Companion:** [RECOVERY_PLAN.md](RECOVERY_PLAN.md) — the alignment plan.
No code is removed or redesigned by this audit.

---

## 0. Verdict in one paragraph

The implementation **honored the letter of the prime directive before it
was written** — nothing in Local-EZAI was replaced, rebuilt, or broken
(verified by diff: zero changes to any runtime file). It **fell short of
the spirit** in one structural way: CLAUDE.md defines Local-EZAI as *the
platform* (Chat / Agents / Models / MCP / Knowledge / Tools) with
Autonomous SWE as *one integrated feature* — but `agentd` grew as a
**parallel sibling** that touches the platform only at the LiteLLM model
seam. It does not appear in the platform's MCP tool plane, its chat, its
knowledge base, or its monitor; it duplicates a chat surface; and it
ignores the mandated `agent_model_map`. Every one of these is fixable by
**integration, not removal** — which is exactly what CLAUDE.md prescribes.

---

## 1. Preserved functionality ✅

Everything Local-EZAI shipped before this session works unchanged.

**Evidence** (diff `fab97ec` (pre-session baseline) → `HEAD`, restricted to
pre-existing paths):

| Area | Files touched | Nature |
|---|---|---|
| Compose stack (all 4 profiles), all 8 services | **0 files** | untouched |
| `config/` (LiteLLM, auto-RAG hook, mcpo, SearXNG, prompts) | **0 files** | untouched |
| Service code (`embed-server/`, `monitor/`, `mcpo/`, `mcp-servers/`) | **0 files** | untouched |
| `scripts/`, `tools/`, `k8s/`, `slurm/`, `.env.example`, `docs/DEPLOY-N97.md` | **0 files** | untouched |
| `README.md` | +22 lines | additive section pointing at agentd |
| `Makefile` | +29 lines | additive `swe-*` targets, own `.PHONY` |
| `.gitignore` | +4 lines | additive entries |

Total modification to pre-existing files: **+55 / −0 lines**, none of them
runtime. Chat, auto-RAG, MCP tools in chat, web search, monitor RBAC,
model profiles, K8s/Slurm paths: all preserved. The additive-overlay
discipline (ADR-002) held for six phases.

Positive accidental synergies found during audit:
- the pre-existing `.gitignore` rule `*.db` already keeps
  `.agent/memory.db` out of this repository's git status;
- `local-ezai chat` calls go through LiteLLM, so the platform's auto-RAG
  hook **already injects KB context into agent chat** — an integration
  that exists by construction, not by design.

## 2. Duplicated functionality ⚠️

| # | New (agentd) | Existing (Local-EZAI) | Assessment |
|---|---|---|---|
| D1 | `local-ezai chat` REPL | **OpenWebUI** — the platform's Chat pillar (multi-user, RAG, tools, web search) | A second, strictly weaker chat surface. Justifiable only as a terminal convenience; the platform's chat should be the front door to SWE (see I1) |
| D2 | `MemoryStore` (SQLite, per-repo `.agent/`) | mcpo **`memory`** MCP server (knowledge graph) and **Qdrant KB** (semantic) | Three memory/knowledge stores, zero bridges. Scopes genuinely differ (SWE run-lessons vs chat memory vs document KB) — not a replacement, but an unintegrated triplication of the "Knowledge" pillar |
| D3 | Two CLIs: legacy `ezai` + `local-ezai` | — (internal duplication) | Overlapping `memory`/run-inspection commands, two arg conventions; maintenance burden created this session |
| D4 | Sprint report generation (`docs/sprints/*.md`) | — | Partial overlap with CLAUDE.md's **Documentation Agent** mandate (USER_GUIDE / OPERATION_MANUAL / MAINTENANCE_GUIDE / RELEASE_NOTES), which remains unbuilt |

## 3. Replacement attempts 🔍

**No functional replacement occurred** — no existing capability was
removed, rerouted, or shadowed in the runtime. Three *replacement-pattern
risks* exist at the positioning level and must be corrected by framing and
integration:

| # | Risk | Detail |
|---|---|---|
| R1 | **Naming claims the product.** | The CLI is named `local-ezai` — the platform's own name — and its shape mirrors Claude Code. CLAUDE.md: *"This is NOT a Claude Code clone"*; SWE is one feature. The binary name implicitly rebrands the platform as the CLI. Keep the binary (backward compatibility), reposition it explicitly as *the SWE subsystem's interface to Local-EZAI* |
| R2 | **A parallel front door.** | `local-ezai chat` opens a model conversation outside OpenWebUI. Combined with R1, a user could live entirely in the CLI and never touch the platform — the "new product beside the old" failure mode CLAUDE.md forbids. Mitigation is I1 (make platform chat drive SWE), not deletion |
| R3 | **Model routing ignores the mandate.** | CLAUDE.md specifies `agent_model_map` (planner→hermes3/deepseek-r1, coder→qwen3-coder/…, debugger→deepseek-r1/…, reviewer/documentation→llama3, memory→hermes3, evolution→deepseek-r1). agentd's role map defaults every role to `qwen2.5-7b`, has **no fallback mechanism**, and lacks `documentation`/`evolution` roles entirely |

## 4. Breaking changes ✅ none found

Checked systematically:

- **Runtime/services/config:** zero diffs (see §1). Ports, env contracts,
  compose profiles, health checks: identical.
- **Make targets:** all pre-existing targets byte-identical; new targets
  additive with a separate `.PHONY` line.
- **CI:** `.github/workflows/agentd-ci.yml` is new and path-filtered to
  `agentd/**` — cannot affect the stack.
- **Backward compatibility of the session's own increments:** the `ezai`
  CLI kept working through Phases 5–6; `--simple` preserved Phase 5 sprint
  behavior when Phase 6 changed the default.

Two **new side effects** (documented, non-breaking, listed for
transparency): agentd creates an untracked `.agent/` (memory) in target
repositories on first run, and `run`/`code` leave worktrees under
`~/.agentd/workspaces` plus `swe/*` branches in target repos by design
(the deliverable). Neither alters existing behavior.

One **rule deviation to log**: CLAUDE.md's Session Startup Rules
(read CLAUDE.md → .agent → architecture docs → analyze → plan) could not
be followed in earlier phases because CLAUDE.md did not exist yet; the
`.agent/` convention it mandates was, however, anticipated by ADR-012 and
followed throughout.

## 5. Integration opportunities 🎯 (the recovery surface)

Ordered by how directly they realize CLAUDE.md's product tree.

| # | Opportunity | Realizes | Effort |
|---|---|---|---|
| I1 | **SWE as MCP tools in mcpo** (`swe_run`, `swe_sprint`, `swe_status`, `swe_report`, gated `swe_approve`) — additive block in `config/mcpo-config.json` wrapping the runner API. OpenWebUI's chat (with its existing tool auto-registration) becomes the front door to autonomous SWE | *"The Autonomous SWE subsystem must integrate into Local-EZAI"* — the MCP pillar; dissolves D1/R2 | M |
| I2 | **Knowledge bridge**: (a) agents get `kb_search` (T1) against Qdrant/embed-server so Planner/Debugger/Sprint research the platform KB; (b) sprint reports + `lessons_learned.json` are ingested into the KB so platform chat can answer questions about SWE work | Knowledge pillar; TARGET §5.2 promised `kb.search` and never shipped it | S/M |
| I3 | **Web research tools**: `web_search` (SearXNG) + `web_fetch` (T1) reusing the platform's own services | Tools pillar; TARGET §5.2 parity | S |
| I4 | **Monitor integration**: a runs panel reading `~/.agentd/runs` journals/reports (additive endpoint consumption, monitor service untouched or gains one optional card) | platform observability of SWE | M |
| I5 | **Model routing alignment**: per-role `primary`/`fallback` lists; ship a config profile matching `agent_model_map` verbatim (hermes3/deepseek-r1/qwen3-coder/llama3 as LiteLLM aliases); add `documentation` + `evolution` roles | CLAUDE.md Model Routing section | S/M |
| I6 | **Documentation Agent**: generates/maintains `docs/USER_GUIDE.md`, `OPERATION_MANUAL.md`, `MAINTENANCE_GUIDE.md`, `RELEASE_NOTES.md`; sprint doc generation becomes one of its duties | CLAUDE.md agent roster (mandatory docs) | M |
| I7 | **Evolution Agent + human governance**: Analyze→Propose→Implement→Validate→**Pull Request**→**Human approval**; requires a `pr_create` T3 tool for the forge; approval gates for the CLAUDE.md-listed protected actions | CLAUDE.md Evolution + Human Governance + Self-Evolution sections | L |
| I8 | **`type` validation category** (mypy/pyright/tsc) alongside test/lint/build | CLAUDE.md Validation Agent: "Type validation" | S |
| I9 | **CLI consolidation**: fold legacy `ezai` into `local-ezai` (deprecation shims, no deletion), reposition `chat` as a thin client of the platform (or a documented terminal-convenience with pointers to OpenWebUI) | removes D3, tempers R1/R2 | S/M |

## 6. Agent roster vs. CLAUDE.md

| CLAUDE.md agent | Status | Notes |
|---|---|---|
| Planner | ✅ built | responsibilities match (analysis, decomposition; no code) |
| Coding | ✅ built | targeted edits (`fs_edit` exact-match), tests mandated — matches "never rewrite blindly" |
| Validation | ✅ built | test/build/lint ✓; **type validation missing** (I8) |
| Debug | ✅ built | root cause / fix / re-validation; "never patch symptoms" enforced structurally (ADR-015) |
| Git | ✅ built | gated commits, fail-closed push |
| Browser QA | ✅ built | Playwright; UI/workflow/console — "part of validation" ✓ (ADR-016) |
| Memory | ✅ built | stores decisions/architecture/lessons/recurring fixes in `.agent/` ✓ (ADR-017) |
| Sprint | ✅ built | requirement analysis → DAG → parallel execution (ADR-019) |
| **Documentation** | ❌ missing | only sprint reports exist (D4) |
| **Evolution** | ❌ missing | no propose→PR→approval loop (I7) |
| Reviewer | ➕ extra | not in CLAUDE.md's list; consistent with its principles (human-governance aid) — keep |

## 7. Alignment scorecard

| CLAUDE.md rule | Compliance |
|---|---|
| Never replace / rebuild / remove Local-EZAI | ✅ full (evidence §1, §4) |
| Always extend, integrate, preserve backward compat | 🟡 extended ✓, backward-compatible ✓, **integrated ✗** (LiteLLM seam only) |
| SWE integrates into the platform (Chat/MCP/Knowledge/Tools) | ❌ not yet — I1–I4 |
| Clean architecture / SOLID / DI / modular | ✅ (tool registry, agent allowlists, injected stores/clients, additive overlays) |
| Agent roster (10 agents) | 🟡 8 of 10 + 1 extra |
| Model routing `agent_model_map` | ❌ not honored — I5 |
| Human governance (propose → approve; PRs) | 🟡 fail-closed push ✓, approval gates & PR flow missing — I7 |
| Memory in `.agent/` | ✅ |
| Documentation mandatory (4 named guides) | ❌ — I6 |
| Bootstrap exit (Local-EZAI maintains itself) | ⬜ future — depends on I5–I7 |

**Next:** [RECOVERY_PLAN.md](RECOVERY_PLAN.md) sequences I1–I9 into
alignment phases. Per instruction and per CLAUDE.md governance: no code is
removed, nothing is redesigned, and consolidation steps (I9) are explicitly
flagged as requiring human approval.
