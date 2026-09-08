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

## 4. Deliberate asymmetries (and why)

1. **Chat can start work but never govern** — approval/activation/rollback
   from a conversation would let prompt-injected content reach the
   governance boundary ([OPENWEBUI_INTEGRATION.md](OPENWEBUI_INTEGRATION.md) §5).
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

## 7. Consistency test (release gate)

The V1 test suite gains a **parity harness**: for each matrix row, execute
via CLI-direct, CLI-connected, and control-plane API, and assert identical
state transitions and audit records. WebUI is validated by the existing
Browser QA machinery against the Admin Center itself — the platform
dog-foods its own Browser QA agent
([V1_IMPLEMENTATION_PLAN.md](V1_IMPLEMENTATION_PLAN.md) §P4).
