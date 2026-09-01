# First-Run Experience (FRE)

**Promise:** from a fresh machine to a chatting, governed, SWE-capable
platform in ≤ 30 minutes on the golden path — having edited **one file,
once** (`.env`), and never again
([TARGET_PRODUCT_V1.md](TARGET_PRODUCT_V1.md) §2).

## 1. The golden path

```
1. git clone …/local-ezai && cd local-ezai
2. ./install.sh                      # or: make setup (kept as alias)
      ├─ detects hardware → proposes a profile (gpu | cpu | n97 | n97-igpu)
      ├─ generates .env from .env.example:
      │     · secrets auto-generated (openssl rand — today's manual step)
      │     · ports defaulted, conflicts auto-relocated (existing behavior)
      │     · EZAI_CONTROL_TOKEN minted (control plane ↔ tools trust)
      ├─ opens .env ONCE for review/edit  ← the only file a user ever edits
      ├─ docker compose up (profile)  +  wait-ready
      └─ prints:  ✔ platform up →  http://<host>:3000
3. Browser → OpenWebUI: create the first (admin) account   [existing flow]
4. Onboarding (below) → chatting + SWE-ready
```

Air-gapped variant: `install.sh --offline <bundle>` consumes a pre-fetched
image+weights bundle; steps are otherwise identical.

## 2. What `.env` is — and is not

`.env` = **installation parameters only**: hardware profile, ports,
generated secrets, optional proxy settings. It contains **no models, no
routing, no agent configuration** — all of that is lifecycle-managed state
([MODEL_LIFECYCLE_MANAGEMENT.md](MODEL_LIFECYCLE_MANAGEMENT.md) §5).
Changing `.env` later is a reinstall-grade operation (documented, rare:
port moves, migration), not management.

## 3. Onboarding — the Model Bootstrap wizard

First login with an empty serving set triggers onboarding (Admin Center
page, deep-linked from a pinned first-run message in OpenWebUI; CLI
equivalent: `local-ezai init`):

```
Step 1  HARDWARE CHECK    detected profile, RAM/VRAM/disk budget, engine
                          availability (llama.cpp / vLLM) — with verdicts
Step 2  MODEL SET         curated catalog filtered to the profile:
                          "Recommended set" (one per group:
                           reasoning / coding / chat) preselected;
                          sizes + expected tokens/sec shown
Step 3  INSTALL           downloads with progress; provider load-validation
Step 4  BENCHMARK         evaluate-models probes + tokens/sec per model
Step 5  ACTIVATE          the ONE auto-approved activation: generation 1
                          is created from the wizard's explicit confirmations
                          (there is no previous state to protect; every
                          later activation goes through the queue)
Step 6  SMOKE             scripted end-to-end: one chat turn · one RAG
                          answer over a sample doc · one `swe_plan` against
                          the bundled sample project — all three green
                          ⇒ "Platform ready" checklist
```

Result: generation 1, an auditable baseline, all roles resolvable
([MODEL_ROUTING_DESIGN.md](MODEL_ROUTING_DESIGN.md) §4 fails loudly if not).

## 4. First SWE contact (guided, optional)

The final onboarding card offers two paths:

- **Chat path:** opens a conversation with the Orchestrator persona
  pre-filled: *"Plan: add a /health endpoint to the sample project"* —
  demonstrating plan → run → report inside chat
  ([OPENWEBUI_INTEGRATION.md](OPENWEBUI_INTEGRATION.md) §3).
- **CLI path:** copy-paste block —
  `local-ezai <sample> plan "add a /health endpoint"` then `run`.

Both use the bundled sample project so the first experience never risks a
user repo, and both end by showing the run report + the branch + the
Admin Center run page — teaching the review-then-merge habit from minute
one.

## 5. Day-2 handoff (what the user is told at the end)

A closing card, mirrored in `local-ezai status`:

- manage models → Admin Center **Models** / `local-ezai model …`
- approve things → **Governance** queue (nothing ships itself)
- run engineering work → Orchestrator chat or `local-ezai run/sprint`
- health → **Overview** / `local-ezai status`
- *you will not need to edit any file; if a doc tells you to, it's a bug.*

## 6. FRE acceptance criteria (release-gated)

| # | Criterion | Target |
|---|---|---|
| F1 | fresh GPU host → smoke green | ≤ 30 min (excl. model downloads on slow links: progress + resumability required instead) |
| F2 | files hand-edited on the golden path | exactly 1 (`.env`), once |
| F3 | n97 profile completes the same wizard (smaller recommended set) | yes |
| F4 | wizard abort at any step | resumable; never a half-configured platform (generations are atomic) |
| F5 | `install.sh` re-run on an installed host | detects, offers repair/upgrade, never wipes state |
| F6 | offline bundle install | same wizard, no egress |

The FRE is itself validated by the platform's own Browser QA agent
(scripted onboarding workflow) in CI — the installer becomes a tested
artifact, not a README ([V1_IMPLEMENTATION_PLAN.md](V1_IMPLEMENTATION_PLAN.md) §P5).
