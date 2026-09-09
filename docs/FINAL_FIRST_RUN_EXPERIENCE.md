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
   > **As-built (PR-4, `fetch.parse_source`):** the resolver accepts
   > `hf:<org/repo>`, `gguf:<https url>`, `gguf:hf://<org/repo>/<file.gguf>`,
   > `gguf:<local path>`, a catalog id, and `auto`; anything else is
   > rejected with every accepted form listed (F8). The catalog is the
   > packaged seed (`agentd/defaults/catalog.yaml`) merged with
   > `config/catalog/*.yaml`.
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

> **As-built (PR-7, `agentd/src/agentd/bootstrap.py`, `local-ezai
> bootstrap`, `make bootstrap`):** steps 2, 4 (the model part), 5 and the
> stamping are the **bootstrap core**: `read_seeds` (V1 seeds, or the first
> legacy family from `defaults/legacy_seeds.yaml` migrated into one model
> for all groups, F11 — the served name is kept so existing chats work) →
> `validate_seeds` (F8: runtime, scheme, format × runtime, slot capacity,
> `auto` feasibility, declared tool format/context vs role contracts, pins —
> every problem with its fix, nothing downloaded) → install + benchmark per
> seed (PR-4 verbs) → `plan_generation` (reference roles + contracts, groups
> from seeds, `EZAI_ROLE_PIN_*`) → the one implicit approval → PR-5 apply
> (render, reload, health, self-rollback) → `EZAI_SEEDS_CONSUMED` stamped
> into `.env`. Optional per-seed declarations for user-supplied sources:
> `<SEED>_TOOL_FORMAT`, `<SEED>_CONTEXT` (undeclared sources default to a
> runtime's generic template-driven handler when it lists one). Steps 1
> (detect), 3 (secrets), 6–8 (up, smoke, report) are PR-21/22 (`install.sh`,
> `make setup`); `make setup-*` already runs `make bootstrap` before `up`.
> `--dry-run` prints the planned generation-1 diff (F10) without fetching.

> **As-built (PR-21, `install.sh` → `agentd/src/agentd/installer.py`):**
> steps 1 and 3 and the `.env` half of step 2. **Detect** with the platform's
> own capability code, or assert a class with `--profile cpu|n97|n97-igpu` /
> `--class` (recorded as `EZAI_CAPABILITY_CLASS`, honored by `make bootstrap`;
> `--profile gpu` only checks that an accelerator exists); the vector is
> recorded in `.env` as a comment. **Generate** `.env` from `.env.example`
> with the seven placeholder secrets minted and `AI_RUNTIME` chosen from the
> runtime descriptors (`default_for_classes`, data), or **repair** an
> existing `.env` (F5: timestamped backup, user values byte-identical, only
> missing or placeholder secrets minted, runtime only when unset and the
> seeds not consumed, idempotent, nothing else on disk touched). **Validate**
> with the bootstrap's own F8 rules against the chosen class and runtime,
> plus a class-aware hint when the file still carries the example's legacy
> accelerator-sized default on a CPU class — nothing downloaded. Then the
> **one review-edit stop**: a fresh `.env` opens once in `$EDITOR` on a
> terminal and is re-validated; without one the installer prints what to
> edit and exits 3; `--yes` accepts the file (F7); `--check` writes nothing.
> The installer never picks models. `make install` runs it; steps 4–8 stay
> `make bootstrap` + `make up-*` until PR-22 folds them into `make setup`.

> **As-built (PR-22, `local-ezai setup`, `agentd/src/agentd/setup_pipeline.py`):**
> `make setup` is the whole tree now — `install.sh` (steps 1–3) then
> `local-ezai setup`: **4 fetch** = the PR-7 bootstrap when no registry
> exists (models fetched, validated, benchmarked; **5 render** happens
> inside it), then `docker compose pull` / `build` with the rendered engine
> override, then the RAG embedding model (`scripts/download-embed.sh`,
> skipped when present); **6 up** = `docker compose up -d` for the profile +
> wait-ready: the active runtime descriptor's `ready` verb on the host port,
> then every service of the daemon's health table; **7 verify** = one chat
> turn on the chat role alias (required), a RAG answer over a sample
> document embedded with the stack's own embed script, `plan_only` on the
> bundled sample project, the evaluate-models probes (the last three are
> advisory — they depend on the model you chose); **8 done** = the ✔ block
> with the WebUI and Admin Center URLs (`LAN_HOST`, relocated ports),
> `config/first-run/report.{json,md}`, the "Platform ready" card as an
> OpenWebUI banner (through an optional compose env_file; rolled back
> automatically if OpenWebUI does not come back), the Orchestrator persona
> installed — or deferred to "after your first login: make orchestrator".
> Every step is idempotent: re-running `make setup` repairs, it never
> repeats a download. `make setup-gpu|cpu|n97` assert their class through
> both halves; the system-package script is `make setup-system`.

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

> **As-built (PR-22):** the card is OpenWebUI's own banner (`WEBUI_BANNERS`,
> type *success*, dismissible), written by the pipeline into the optional
> compose env_file `config/first-run/openwebui.env`: the three groups and
> the model each got, runtime + capability class, the smoke tally, the Admin
> Center URL, how to reach the Orchestrator (pick it in the model list; `make
> orchestrator` after the first login installs it) and the CLI status
> command. The three "buttons" are URLs in the text — the banner is plain
> text; the same card is `config/first-run/report.md` and the terminal's
> ✔ block, with the Orchestrator deep link `/?models=local-ezai-orchestrator`.
> The fallback wizard is `local-ezai init`: hardware check → the recommended
> set per group (catalog recommender, fit verdicts) → accept or type a
> reference → seeds written to `.env` as catalog ids → the pipeline. OpenWebUI
> keeps a UI-saved banner list over the env default, so the card shows on a
> fresh install and never overrides an admin's later edits.

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

> **As-built (PR-23):** each row is a test in `agentd/tests/acceptance/`
> (`make swe-accept`): F7 a fully specified `.env` opens no editor and asks
> nothing; F8 the fixes print and no fetcher, Docker or router call happens;
> F9 `auto` seeds resolve per group to the recommender's eligible entries
> with their verdicts, and generation 1 carries exactly those — this test
> found the PR-7 dedupe defect (three `auto` seeds became one model for every
> group), fixed in PR-23; F10 the bootstrap request's evidence and diff equal
> the seeds; F11 a legacy `.env` migrates with the served name kept and no
> user action.
