# Final First-Run Experience

**Supersedes** the wizard-choice flow of
[FIRST_RUN_EXPERIENCE.md](FIRST_RUN_EXPERIENCE.md) §3 where they differ:
model and runtime choices move **into `.env`** (the mandated UX); the
wizard becomes verification and progress, not decision-making. Everything
else (acceptance criteria F1–F6, offline bundles, re-run safety) carries
over.

## 1. The five-step contract

```
1  git clone …/local-ezai && cd local-ezai
2  edit .env          ← the only file a user ever edits, once
3  make setup         (or make setup-cpu / make setup-gpu — class asserts,
                       HARDWARE_AGNOSTIC_ARCHITECTURE §1)
4  open http://<host>:3000
5  use everything     (chat · knowledge · MCP tools · Autonomous SWE ·
                       evolution — no further configuration, ever)
```

## 2. `.env` — the one-time seed (runtime- and hardware-agnostic)

```bash
# ── What runs the models ──────────────────────────────────────
AI_RUNTIME=llamacpp            # llamacpp | vllm | <future runtime id>

# ── What fills the three logical groups (any model, any family) ──
REASONING_MODEL=hf:NousResearch/Hermes-3-Llama-3.1-8B     # or gguf:<url|path>
CODING_MODEL=hf:Qwen/Qwen2.5-Coder-7B-Instruct            # or a catalog id
CHAT_MODEL=gguf:./models/gguf/llama-3-8b-q4_k_m.gguf

# optional overrides (defaults shown are computed, not required):
# REASONING_MODEL=auto        # let the recommender pick for this hardware
# EZAI_ROLE_PIN_debugger=…    # advanced: per-role pin at bootstrap
# ── Ports & secrets: generated/validated by the installer ────────
```

Rules that make this clean:

1. **Model references are source URIs or catalog ids** — `hf:`, `gguf:`
   (URL or path), or a bare catalog id. Any family, any vendor. `auto`
   delegates to the requirements-driven recommender (R-5).
2. **Read once.** `make setup` consumes these seeds to build
   **generation 1** of the model registry and then *never reads them
   again* — no dual source of truth. Post-setup changes happen in the
   WebUI/CLI; the installer stamps the seeds' consumption into `.env` as a
   comment so a later editor is warned.
3. The legacy per-profile families (`CPU_CHAT_MODEL`, `N97_MODEL_*`,
   `CHAT_MODEL_NAME`, per-profile LiteLLM files) are retired; the
   installer migrates existing installs by translating them into
   generation 1 (finding CF-1).

## 3. `make setup` — what actually happens

```
make setup
 ├─ 1 detect     capability vector → class (or asserted by setup-cpu/-gpu)
 ├─ 2 validate   .env seeds: parse sources, fit(model, hardware) verdicts,
 │               runtime×format compatibility (RUNTIME_ABSTRACTION §4/§5)
 │               → any problem printed HERE, with the fix, before pulling
 │                 anything ("CODING_MODEL is GGUF but AI_RUNTIME=vllm …")
 ├─ 3 secrets    generate missing keys/tokens into .env
 ├─ 4 fetch      pull images (per runtime descriptor + class) + download
 │               the three models (progress, resumable, checksums)
 ├─ 5 render     generation 1: registry (groups reasoning/coding/chat ←
 │               the three seeds; roles per MODEL_GOVERNANCE_V2 defaults),
 │               LiteLLM config, engine-slot materialization
 ├─ 6 up         compose up (profile) → wait-ready → validate_model probes
 ├─ 7 verify     smoke: chat turn · RAG answer on the sample doc ·
 │               swe_plan on the sample project · evaluate-models probes
 └─ 8 done       ✔ prints: WebUI URL · Admin Center URL · `local-ezai status`
                 and writes the FIRST_RUN report (shown in the WebUI banner)
```

Failure at any step: actionable message, safe re-run (idempotent steps,
atomic generation — never a half-configured platform).

## 4. First WebUI contact

- OpenWebUI opens on account creation (existing flow, first user = admin).
- A pinned **"Platform ready"** card (from the FIRST_RUN report) shows:
  the three groups and which model the user's `.env` filled them with,
  runtime + hardware class, smoke results — and three buttons:
  **Start chatting** · **Try the Orchestrator** (pre-filled sample-project
  task) · **Open Admin Center**.
- No mandatory wizard remains: choices were made in `.env`; the card is
  confirmation, not configuration. (The interactive Bootstrap wizard from
  FIRST_RUN_EXPERIENCE survives only as the *fallback* when seeds are
  missing/`auto`, and as the Admin Center's "add model" flow.)

## 5. Day-2 (restated, unchanged)

Models/runtime → Admin Center or `local-ezai model|runtime …` · work →
Orchestrator chat or `local-ezai run/sprint` · approvals → Governance
queue. **If any instruction ever says "edit a YAML", it is a bug** —
`.env` model/runtime seeds included: after setup they are history, not
config.

## 6. Acceptance criteria (delta to F1–F6)

| # | Criterion |
|---|---|
| F7 | The five-step contract completes with **zero interactive prompts** when `.env` is fully specified |
| F8 | Invalid seed combinations are rejected at step 2 with a printed fix, before any download |
| F9 | `REASONING_MODEL=auto` (etc.) produces a class-appropriate choice with the fit verdict shown |
| F10 | Generation 1 diff shows exactly the `.env` seeds (auditability of the bootstrap) |
| F11 | Legacy `.env` (CPU_*/N97_* families) migrates to generation 1 without user action |
