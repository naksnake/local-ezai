# CLI ↔ WebUI Strategy

**The rule that makes the product coherent:** after installation there are
exactly **two management surfaces** — OpenWebUI (+ the Admin Center it
links to) and the `local-ezai` CLI — and they are **thin clients of one
control plane over one declarative state**. Neither surface owns logic;
neither can do something the other cannot (except where deliberately
asymmetric, §4).

## 1. One brain, many hands

```
                 local-ezai CLI ──────────┐
   OpenWebUI chat-ops (SWE tool server) ──┤
                 Admin Center :8888 ──────┼──► ezaid — Platform Control Plane :8010
                                          │      · OpenAPI (single spec)
   (future: chat-ops MCP, CI hooks) ──────┘      · wraps agentd pipelines,
                                                   lifecycle mgr, PAL renderer,
                                                   governance queue, health
                                                 · single audit log
```

`ezaid` is an **extension**, not a rewrite: it serves the Python functions
that already exist (`runner.py` pipelines, `evaluate.py`, `model_registry`,
compose operations the Makefile performs) behind one authenticated OpenAPI
surface. It is stateless over the declarative stores:

| Store | Content |
|---|---|
| `config/models/*` | Registry v2 + generations ([MODEL_ROUTING_DESIGN.md](MODEL_ROUTING_DESIGN.md)) |
| `config/providers/*` | PAL descriptors |
| `~/.agentd/runs`, workspaces | run journals/reports (existing) |
| per-repo `.agent/*` | memory, repo registry overrides (existing) |
| governance log | approvals/rejections, append-only |

> **As built (PR-8, ADR-028 Proposed):** `ezaid` exists as `agentd.control`
> (FastAPI behind the `agentd[control]` extra; direct mode never imports
> it), started as the opt-in overlay `docker-compose.control.yml`
> (`make control-up`, `EZAI_CONTROL_PORT`). This slice serves liveness
> `/health`, `/v1/health` (control info + the `status` snapshot + one probe
> per stack service, the table being data), `/v1/whoami`, `/v1/audit`, and
> `/openapi.json`. Authentication is the service token `EZAI_CONTROL_TOKEN`
> (bearer; no token → no start) with forwarded identity `X-EZAI-User` /
> `X-EZAI-Client`, recorded in the **single audit log** — `agentd/audit.py`,
> the same `config/governance/log.jsonl` the governance queue writes.
> Lifecycle/governance/run endpoints (PR-9/10) and connected mode (PR-11)
> follow on these primitives.

## 2. CLI offline-first guarantee (non-negotiable)

The SWE runtime must keep working with the stack **down** (a laptop, CI, a
dev container). Therefore the CLI has two modes, chosen automatically:

- **Connected mode** — `ezaid` reachable: management verbs go through the
  API (shared audit, shared queue, WebUI sees everything).
- **Direct mode** — repo-work verbs (`run/plan/code/test/fix/review/
  commit/sprint/memory/docs/evolve/...`) execute in-process exactly as
  today; management verbs that require the platform (`model install/
  activate`, governance) fail fast with "control plane unreachable".

Same commands, same outputs; only the transport differs. This preserves
every existing behavior and test.

> **As built (PR-11, ADR-028):** the management verbs (`model`,
> `governance`, `project`, `status`) probe `ezaid`'s liveness once
> (`EZAI_CONTROL_URL`, default `http://localhost:8010`, 1 s) and go through
> the API when it answers — token, forwarded identity (`X-EZAI-User` = the
> CLI's actor, `X-EZAI-Client: cli`), a fresh `Idempotency-Key` per
> mutation; otherwise they run in-process (P1). Text, JSON and error
> objects are identical in both transports (tested verb by verb); `status`
> shows its `transport:`. `--transport auto|connected|direct` (then
> `$EZAI_TRANSPORT`) overrides; `--transport connected` without a daemon,
> or a reachable daemon without a configured token, **fails fast** (exit
> 2, fix named) instead of silently forking the audit. As-built nuance to
> the paragraph above: with a platform on disk and no daemon, management
> verbs work in-process (P1's exit criteria, `make bootstrap`); the "fail
> fast" applies to a requested connected transport and to the no-platform
> case. `bootstrap`, `up`, `down` are host-only and always direct.

## 3. Parity matrix (V1 commitments)

| Operation | CLI | OpenWebUI chat | Admin Center |
|---|---|---|---|
| Start run / sprint / fix / evolve | ✅ (existing) | ✅ start via tools | 🔍 view, cancel |
| Plan preview | ✅ | ✅ | 🔍 |
| Run reports / journals / exec audit | ✅ | ✅ summary | ✅ full detail |
| Model install / benchmark / retire | ✅ `model *` (new) | 🔍 status via tools | ✅ |
| Model activate / upgrade (approval flow) | ✅ request + `governance approve` | ❌ (visible, not actionable) | ✅ request + approve |
| Rollback (generation) | ✅ | ❌ | ✅ |
| Explain routing / models / explain-run | ✅ (existing + `model explain`) | ✅ via tools | ✅ Routing page |
| Governance queue (evolution PRs, activations) | ✅ list/approve/reject | 🔍 list + links | ✅ full |
| Memory browse / add rule | ✅ (existing) | ✅ via existing memory tool | ✅ |
| Health / bench / prune | ✅ (`status`, new) | 🔍 | ✅ |
| Project registration (chat-ops allowlist) | ✅ `project add/rm` (new) | ❌ | ✅ |
| Stack install / start / stop | ✅ wrappers over make (new `up/down`) | ❌ | ❌ (can't manage its own host) |

Legend: ✅ full · 🔍 read-only · ❌ deliberately absent.

> **As built (PR-10):** the first row's "start via tools / view, cancel"
> path exists in the control plane — `POST /v1/runs` starts run / fix /
> sprint / evolve / plan jobs on **registered projects only**, `GET
> /v1/runs…` views status, report and journal, `POST /v1/runs/{id}/cancel`
> cancels (queued now, running at the next model call). `cancel` has no CLI
> verb on purpose (direct mode is interactive: Ctrl-C); the API never pushes.

> **As built (PR-16, ADR-030 Proposed):** the Admin Center column starts on
> the monitor (:8888): "Run reports / journals / exec audit — ✅ full detail"
> and "Start run … — 🔍 view, cancel" are live (`/runs`, `/runs/<id>`, cancel
> for the admin role), "Health — ✅" gains the control plane's aggregated view
> on `/overview`, and the governance queue is visible there (decisions follow
> in PR-18). The console is a thin client exactly as this document requires:
> the monitor calls `ezaid` with the service token and forwards its login as
> the human (`admin via admin-center`), so the audit trail is the one the CLI
> writes. Governance / project rows of the column arrive with PR-18..19.

> **As built (PR-17):** the model rows of the Admin Center column are live on
> `/models`, `/routing`, `/runtime`: "Model install / benchmark / retire —
> ✅", "Model activate / upgrade (approval flow) — ✅ request" (approve
> itself follows in PR-18; the page names the CLI verb meanwhile),
> "Rollback (generation) — ✅", "Explain routing — ✅ Routing page". Each
> button is the CLI verb through the daemon (`local-ezai model …`), shown in
> the page footer; a mutation the CLI lacks (role/group editing, a runtime
> switch verb) is absent here too.

> **As built (PR-18):** the governance rows are live — "Model activate /
> upgrade (approval flow) — ✅ request + approve" and "Governance queue —
> ✅ full": `/governance` and `/governance/<id>` decide through
> `POST /v1/governance/{id}/approve|reject` exactly as `local-ezai governance
> approve|reject` does, with the evidence the CLI's `governance show` prints
> laid out next to the buttons. One queue, one audit trail, two surfaces.

> **As built (PR-19) — matrix amendment and the 1.1.0 contract:** the first
> row's Admin Center cell becomes "▶ start **sprint / evolve** (admin) ·
> 🔍 view · cancel": WEBUI_PRODUCT_STRATEGY §3.5/§3.6 and its release-gated
> zero-CLI journeys 3–4 require starting sprints and evolution cycles from
> the console, and both CLI verbs exist (`local-ezai sprint|evolve`), so
> parity holds; runs, fixes and plans still start from the CLI or chat (no
> wireframe, no journey). "Memory browse / add rule — ✅" and "Project
> registration — ✅" are live on `/memory` and `/projects`. The Memory page
> needed data 1.0.0 never served, so the contract grew **additively to
> 1.1.0** (`GET|POST /v1/projects/{name}/memory`) exactly as §6 prescribes:
> minor bump, artifact regenerated, inventory updated in the same PR; the
> CLI's `memory` verb remains direct-mode repository work (no connected
> twin needed — the store is the same `.agent/memory.db`).

## 4. Deliberate asymmetries (and why)

1. **Chat can start work but never govern** — approval/activation/rollback
   from a conversation would let prompt-injected content reach the
   governance boundary ([OPENWEBUI_INTEGRATION.md](OPENWEBUI_INTEGRATION.md) §5).
   *As built (PR-15):* enforced by the control plane itself, not only by
   the tool catalog — the `swe-server` client is allowed `GET *` and
   `POST /v1/runs`; every other 1.0.0 mutation from it is `client_forbidden`
   and audited. The CLI (`cli` client) and the Admin Center are unrestricted,
   so the parity matrix above is unchanged: the same operations exist on
   both surfaces, and only the chat surface is capped.
2. **Stack lifecycle stays CLI/installer-only** — a WebUI that stops its
   own containers strands the user.
3. **`.env` is untouched by both** — install-time file, period
   ([FIRST_RUN_EXPERIENCE.md](FIRST_RUN_EXPERIENCE.md)).

## 5. CLI surface growth (additive namespaces)

Existing 17 commands unchanged. New verb groups (design; exact flags at
implementation):

```
local-ezai model    install|benchmark|activate|upgrade|rollback|retire|
                    uninstall|explain|history|catalog
local-ezai governance   list|show|approve|reject
local-ezai project      add|list|remove
local-ezai status                       # stack + control plane health
local-ezai up|down                      # wrappers over the compose profiles
```

`make` targets remain as the installer/developer layer and become thin
wrappers where they overlap — scripts keep working, docs steer users to
the CLI.

## 6. Contract discipline

- **One OpenAPI spec** is the contract; CLI (connected mode), Admin
  Center, and the SWE tool server are generated/typed against it. A
  feature "exists" only when it is in the spec.
- Every mutating endpoint: authenticated (service token + forwarded user
  identity), idempotency-keyed, audited, and mapped to exactly one CLI
  verb and at most one WebUI action.
- Errors are the same objects everywhere — the Admin Center shows the same
  message the CLI prints (no divergent failure vocabularies).

> **As built (PR-8/PR-9):** the contract is `docs/api/ezaid-openapi.json`,
> `info.version` = `agentd.control.CONTRACT_VERSION` (`1.0.0-draft.9`; the
> P2 close freezes `1.0.0`). A tripwire test compares the committed
> document with the live app on the contract surface (operations,
> parameters, response codes, security, schema names) — a contract change
> ships its regenerated artifact (`make control-spec`). The error object is
> `{"error": {"code", "message", "fix"}}` for every failure, produced by
> one classification (`agentd/platform_errors.py`) that the CLI also prints
> in `--json` mode — same code, same text, same fix. Every mutating
> operation names its CLI verb in the spec (`CLI: local-ezai …`), accepts
> `Idempotency-Key` (replay with `Idempotency-Replayed: true`; a different
> payload under the same key is `409 idempotency_conflict`), and is audited
> as `api.<operationId>` with the forwarded identity. **Parity is a test**
> (PR-9): for every read verb the CLI's `--json` output equals the API body,
> because both call the same operation function.

## 7. Consistency test (release gate)

The V1 test suite gains a **parity harness**: for each matrix row, execute
via CLI-direct, CLI-connected, and control-plane API, and assert identical
state transitions and audit records. WebUI is validated by the existing
Browser QA machinery against the Admin Center itself — the platform
dog-foods its own Browser QA agent
([V1_IMPLEMENTATION_PLAN.md](V1_IMPLEMENTATION_PLAN.md) §P4).

> **As built (P2 close, PR-12):** the seed of this harness exists —
> CLI-direct vs CLI-connected parity for seven verbs (PR-11), two runs
> supervised concurrently through the API, and the kill-the-daemon test (a
> real `ezaid` process SIGKILLed: repo work unaffected, management verbs
> fall back to direct on the same state). The API contract is frozen at
> `1.0.0` (`docs/api/ezaid-openapi.json`, 29 operations, inventory-pinned).
> The three-surface harness with audit-record equivalence becomes the
> release gate in PR-24.
