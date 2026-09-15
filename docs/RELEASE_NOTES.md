# Release Notes

Reverse-chronological. The version is agentd's `__version__` (`local-ezai
version`, `ezaid --version`); the control-plane contract has its own version
(`docs/api/ezaid-openapi.json`). Evolution runs prepend their entries here;
hand-written releases do the same.

## v1.0.0 — 2026-09-09 — Local-EZAI V1 (release train; human sign-off and tag pending)

The V1 product of [TARGET_PRODUCT_V1.md](TARGET_PRODUCT_V1.md): the existing
AI platform (chat · agents · models · MCP · knowledge · tools) plus
Autonomous SWE behind **one control plane, declarative model state and two
management surfaces**, installed by editing `.env` once. Twenty-six PRs
(PR-1..26, [V1_PR_PLAN.md](V1_PR_PLAN.md)) across six phases; every PR
carries its artifact under `docs/prs/`. Release evidence:
[V1_RELEASE_REPORT.md](V1_RELEASE_REPORT.md).

### Highlights

- **Registry v2, capability classes, runtime descriptors, governed lifecycle
  (P1, ADR-027).** Models are declarative state with numbered generations
  under `config/models/`; hardware is a detected capability vector mapped to
  four classes (`accel-large` · `accel-small` · `cpu-standard` · `cpu-low`;
  `n97` is a preset alias); runtimes are YAML descriptors under
  `config/providers/` behind a neutral `engine` alias; `local-ezai model
  install | benchmark | activate | upgrade | rollback | retire | uninstall |
  explain | history | catalog` with a governance queue (`governance list |
  show | approve | reject`), atomic apply with self-rollback, a curated
  catalog with fit verdicts, and the `.env` seeds consumed once into
  generation 1 (`bootstrap`).
- **The `ezaid` control plane (P2, ADR-028).** One authenticated OpenAPI
  service (service token + forwarded identity, one audit log, one error
  vocabulary, idempotency keys), 29 operations frozen at contract **1.0.0**,
  async SWE runs on registered projects, and the CLI's **connected mode**
  (same text, JSON and errors through the daemon or in-process).
- **OpenWebUI integration (P3, ADR-029).** The SWE tool server behind mcpo
  (start + inspect only), the *Local-EZAI Orchestrator* persona, and the
  chat-ops ceiling enforced by the control plane and drilled offline
  (governance unreachable from chat, prompt injection, chat/RAG byte-identical).
- **The Admin Center (P4, ADR-030).** The monitor became the platform console:
  Overview, Runs, Models, Routing, Runtime, Governance (approve/reject with
  evidence), Projects, Sprints, Evolution, Memory; the monitor login, a
  trusted-header or OpenWebUI-session sign-in; five zero-CLI journeys run by
  the platform's own Browser QA in real Chromium. Contract **1.1.0** (project
  memory operations).
- **The five-step first run (P5, ADR-031).** `make setup` = `install.sh`
  (detect or assert the class, mint secrets, one review edit of `.env`,
  validate the seeds before any download) + `local-ezai setup` (bootstrap,
  images, embedding model, up, wait-ready, smoke, the "Platform ready" card,
  the persona); `local-ezai init` as the fallback wizard; the **offline
  bundle** (`make bundle` / `install.sh --offline` / `make setup-offline`);
  acceptance criteria F1–F11 scripted (`make swe-accept`). Contract **1.2.0**
  (the first-run report).
- **The release gates (P6, ADR-026).** The parity harness (`make swe-parity`:
  every CLI ↔ WebUI matrix row via CLI-direct, CLI-connected and the API —
  equal bodies, state and audit), the agnosticism gates (`make swe-gates`:
  the third-runtime drill from descriptor data alone, the H1 word audit,
  H2–H4 class fixtures), `make release-gate` in one command, and this
  release train: the soak runbook (`make soak`), the DoD checklist, the
  refreshed guides.

### Review hardening before the tag (PR-27, 2026-09-15)

A code review of the merged release train found fourteen defects the suite
did not cover; each is fixed with its regression test
([prs/PR-27-review-fixes.md](prs/PR-27-review-fixes.md)). The ones you may
notice:

- **Control plane.** Model install and benchmark through the container
  overlay's daemon are refused *before* any download with the fix named (the
  side-load needs the host's docker CLI — run the verb direct, or `make
  control-serve`); idempotent replays require the token and the same client,
  and a 401 is never cached; a cancelled run ends `cancelled`, not `failed`;
  a model download no longer holds the mutation lock, and a mutation that
  waits more than 30 s for another gets `409 mutation_busy` instead of
  hanging; `/v1/audit` and `/v1/health` stop re-parsing their files.
- **Lifecycle and first run.** Re-installing an active model keeps it active
  (a failed re-validation demotes nothing); a stack started before the first
  render no longer breaks rendering (the stray directory docker leaves is
  reclaimed); `local-ezai up` refuses without the rendered config and, like
  `setup`, defaults the profile from the detected class; the setup report
  records the real outcome; all platform errors become step results.
- **Console and chat.** The monitor's OpenWebUI cookie check is bounded (cache
  cap, shape check, at most eight concurrent validations); a waiting
  `swe_plan` no longer blocks the other tools of the SWE tool server.
- **Build and hygiene.** `make bootstrap` / `control-serve` work on a fresh
  checkout; `config/control/` and `config/rendered/` are git-ignored,
  `config/models/` no longer is (the weights rule is anchored to `/models/`).
- **New: the offline SWE demo.** `make swe-demo` runs the whole pipeline on a
  scratch copy of the sample project with a scripted model and no stack —
  the walkthrough for newcomers ([SWE_DEMO.md](SWE_DEMO.md)).

### Compatibility and upgrade notes

- The chat stack is **byte-identical in behavior** (ADR-002): a baseline of
  the compose surface is checked in the suite. The engine service keeps its
  historical `vllm` name; new consumers use the `engine` alias.
- Existing installs: run `./install.sh` (repair mode — your `.env` values are
  kept, only missing secrets are minted) then `make setup`; a legacy `.env`
  model family (`CHAT_MODEL`, `CPU_*`, `N97_*`) migrates into generation 1
  automatically, keeping the served name. The per-profile LiteLLM files are
  now **rendered artifacts** under `config/rendered/` — edit state through
  `local-ezai model …` or the Admin Center, never the files.
- `make setup` now runs the first run; the system-package script moved to
  `make setup-system`. `make up*` no longer downloads the legacy chat model.
- The control plane and the Admin Center's control-plane pages are
  **opt-in** (`make control-up` / `control-serve`); without them every
  management verb works in-process and repo work never needs them.
- `local-ezai model rollback --json` prints one JSON document (the ROLLBACK
  notice is text-mode only). `model install <source>` without `--runtime`
  now prefers the active slot's runtime when it serves the format
  (identical outcome with the shipped runtimes).
- PR-27: a bootstrapped checkout now shows `config/models/` (registry and
  generations) as trackable — commit it if you version your platform state;
  `config/control/` and `config/rendered/` are ignored. Idempotency records
  written before PR-27 read back unchanged (no client recorded → any
  authenticated client may replay them once).

### Known limitations (recorded for the next train)

- The control plane's health table probes the engine slot at `/health`,
  not the active runtime descriptor's readiness path; both shipped runtimes
  answer `/health`.
- A day-2 `model install` of an undeclared user source carries no tool-call
  format (the bootstrap's generic default applies to seeds only); declare
  one with a catalog entry or activate it into a non-tool-calling group.
- One engine slot per host (V1.x option), no runtime switch verb (a switch is
  an approval-gated activation), the 72 h soak is a host measurement.

### Verification

Full offline suite green (743 tests at the release train; 775 after the
PR-27 review hardening), ruff clean, `make release-gate` green, the
chat-stack baseline unchanged. The soak
results and the sign-off are recorded in
[V1_RELEASE_REPORT.md](V1_RELEASE_REPORT.md) §4 and §7 by the release
manager; the `v1.0.0` tag follows the merge of the release PR
([GOVERNANCE.md](GOVERNANCE.md): production releases require approval).

## v1.0.0rc1 — 2026-09-01 — hardening sprint (release candidate)

Container sandbox for every agent command (ADR-021), the mandatory reviewer
gate before commit (ADR-022), semantic code intelligence (ADR-023), model
transparency and the benchmark dashboard (ADR-024). 311 offline tests.
Report: [V1_RELEASE_CANDIDATE_REPORT.md](V1_RELEASE_CANDIDATE_REPORT.md).

## v0.7.0 — 2026-08-17 — self-sustainability

Model registry with fallback routing and `evaluate-models`, the
Documentation and Evolution agents, PR delivery through a forge, the
self-hosting `.agentd.yaml`, eight production guides. 267 offline tests.
Report: [FINAL_RELEASE_REPORT.md](FINAL_RELEASE_REPORT.md).
