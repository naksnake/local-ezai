# V1 PR Plan — PR-ready decomposition of the implementation plan

**Purpose:** turn [V1_IMPLEMENTATION_PLAN.md](V1_IMPLEMENTATION_PLAN.md)
(P1–P6, amended by ADR-026) into a sequence of **individually reviewable,
individually mergeable pull requests** — each small enough to review in
one sitting, each leaving the platform releasable, each written so its
Scope section can be pasted into `local-ezai sprint` as a task brief
(self-building where practical).

## 1. Conventions (apply to every PR)

| Aspect | Rule |
|---|---|
| Branch | `feature/p<phase>-<slug>` from `main`; autonomous slices arrive on `swe/*`/`sprint/*` and are cherry-merged into the feature branch by a human |
| Gates | `make swe-test` (311+ tests, all pre-existing tests **unmodified**) + `make swe-lint` + new tests for the PR's own scope; CI green before review |
| Self-review | `local-ezai . review` output attached to the PR description (the platform reviews its own PRs; the human reviewer sees its findings) |
| Docs-as-code | the architecture doc the PR implements is updated from *designed* → *as-built* in the same PR; phase ADR (ADR-027..031) enters `Proposed` in the phase's first PR and flips to `Accepted` in its last |
| Behavior preservation | any diff to a pre-existing test or rendered default is called out in a **"Behavior notes"** section — empty means byte-identical |
| Size budget | S ≤ ~300 net LOC · M ≤ ~800 · L ≤ ~1500 (split anything larger) |
| Merge authority | human (repo owner); no PR merges itself — including evolution-authored ones |
| Rollback note | every PR states how to revert (git revert suffices unless it migrates state — then the PR must ship the down-migration) |

## 2. Dependency graph

```mermaid
graph LR
  subgraph P1
    PR1-->PR2-->PR3-->PR4-->PR5-->PR6-->PR7
  end
  subgraph P2
    PR8-->PR9-->PR10-->PR11-->PR12
  end
  subgraph P3
    PR13-->PR14-->PR15
  end
  subgraph P4
    PR16-->PR17-->PR18-->PR19-->PR20
  end
  subgraph P5
    PR21-->PR22-->PR23
  end
  subgraph P6
    PR24-->PR25-->PR26
  end
  PR7-->PR8
  PR12-->PR13
  PR12-->PR16
  PR12-->PR21
  PR15-->PR24
  PR20-->PR24
  PR23-->PR24
```

P3 / P4 / P5 run in parallel after PR-12; P6 is the release train.

## 3. The PRs

### Phase P1 — Registry v2 · PAL · Lifecycle (ADR-027) — CLI-first, no new services

**PR-1 · Registry v2 store + generations** — M — ✅ **implemented**
([prs/PR-1-registry-v2.md](prs/PR-1-registry-v2.md); 19 tests incl. the
golden test, suite 330 green)
Scope: `config/models/registry.yaml` schema (models × states, ordered
groups reasoning/coding/chat, roles with group/pin/**contract** fields per
MODEL_GOVERNANCE_V2 §3), generation snapshots + diff, load/validate
(resolution-completeness check).
Tests: schema round-trip; resolution algorithm incl. pins, inactive
skipping, loud empty-group failure; **golden test: reproduces today's
CLAUDE.md routing byte-for-byte**; generation diff.
Excludes: any consumer.

**PR-2 · Capability vector + classes + fit()** — S — ✅ **implemented**
([prs/PR-2-capability-fit.md](prs/PR-2-capability-fit.md); 14 tests,
suite 344 green, golden intact)
Scope: detection (`accelerator kind/vram/ram/cores/flags`), class mapping
(`accel-large|accel-small|cpu-standard|cpu-low`, `n97` preset alias), pure
`fit(model, vector)` with conservative verdicts + override flag.
Tests: fixture vectors → classes (H3, H4 seeds); fit verdicts for
reference sizes/quants.

**PR-3 · Runtime descriptors + renderer** — L — ✅ **implemented**
([prs/PR-3-runtime-renderer.md](prs/PR-3-runtime-renderer.md); 34 tests
incl. goldens per legacy profile, suite 378 green, PR-1 golden intact)
Scope: `config/providers/{llamacpp,vllm}.yaml` (six-verb contract of
RUNTIME_ABSTRACTION §2, per-accelerator image tables); renderer producing
LiteLLM config (model + **role aliases**), engine-slot materialization,
runtime role map; drift detection on rendered files; neutral `engine`
network alias added to compose.
Tests: render golden files per (runtime × class); drift refusal;
capability negotiation failures name the missing capability (CF-6).
Behavior notes: existing hand-written LiteLLM configs untouched until
PR-7 (renderer writes to a parallel path until cutover).

**PR-4 · Lifecycle: install / validate / benchmark** — L — ✅ **implemented**
([prs/PR-4-lifecycle-install-benchmark.md](prs/PR-4-lifecycle-install-benchmark.md);
35 tests, suite 413 green, PR-1/PR-3 goldens intact)
Scope: source resolvers (`hf:`, `gguf:` url/path, catalog id), checksummed
resumable download, provider `validate_model`, `bench` absorbing `make
bench`; catalog as pluggable data + requirements-driven recommender
(`auto`); model states registered→installed→benchmarked→failed.
Tests: state machine transitions; resolver matrix; recommender fit
against fixture vectors; benchmark recording into registry + trend file.

**PR-5 · Lifecycle: activate / upgrade / rollback / retire + governance queue** — L — ✅ **implemented**
([prs/PR-5-activation-governance.md](prs/PR-5-activation-governance.md);
22 tests incl. failed-health self-rollback and crash injection, suite 435
green, goldens intact)
Scope: activation request objects (generation diff + evidence), file-backed
approval queue (approve/reject with reason, append-only governance log),
atomic render→reload→health→self-rollback protocol
(MODEL_LIFECYCLE §4), generation rollback, retire/uninstall guards.
Tests: approval-gated activation; failed-health self-rollback; rollback
to generation N; blocked retire of a last serving member; audit records.

**PR-6 · CLI namespaces + role aliases in code** — M — ✅ **implemented**
([prs/PR-6-cli-namespaces-role-aliases.md](prs/PR-6-cli-namespaces-role-aliases.md);
24 tests, suite 459 green, PR-1 golden intact through the alias switch)
Scope: `local-ezai model …`, `governance …`, `project …`, `status`,
`up/down` wrappers; agentd role defaults switch to `role-*` aliases
(**removes the last model names from code — CF-3**); `prepare_run` seeds
from Registry v2 with per-repo ADR-020 overrides preserved.
Tests: CLI integration per verb (scripted); alias binding; ADR-020
override precedence regression suite.
Behavior notes: default model resolution path changes — golden test from
PR-1 must still hold.

**PR-7 · Bootstrap core + `.env` seed consumption + cutover** — M — ✅ **implemented**
([prs/PR-7-bootstrap-cutover.md](prs/PR-7-bootstrap-cutover.md); 23 tests
incl. F8/F10/F11, suite 482 green, goldens intact; **ADR-027 → Accepted**)
Scope: `bootstrap(seed_env) → generation 1` (AI_RUNTIME + three group
seeds, `auto`, consumed-once stamping), legacy `CPU_*/N97_*/CHAT_MODEL_*`
migration (F11), **cutover**: rendered LiteLLM config becomes the real
one; three legacy config variants retired.
Tests: seed parsing matrix (F8 cases), migration goldens, generation-1
diff equals seeds (F10).
Behavior notes: the cutover PR — reviewed with rendered-vs-legacy config
diffs attached. **ADR-027 → Accepted.**

### Phase P2 — `ezaid` control plane (ADR-028)

**PR-8 · Service skeleton** — M — ✅ **implemented**
([prs/PR-8-control-plane-skeleton.md](prs/PR-8-control-plane-skeleton.md);
22 tests, suite 504 green, goldens intact; **ADR-028 → Proposed**)
Scope: OpenAPI app, service-token auth + forwarded identity, append-only
audit log, `/health` aggregation, compose overlay service
(`EZAI_CONTROL_PORT`), versioned spec artifact.
As built: `agentd.control` (FastAPI behind the `agentd[control]` extra),
`agentd/audit.py` shared with the governance queue, contract
`1.0.0-draft.8` at `docs/api/ezaid-openapi.json` (contract-surface
tripwire), opt-in overlay `docker-compose.control.yml` + `make control-*`,
`local-ezai status` shows control health.
**PR-9 · Lifecycle + governance endpoints** — M — ✅ **implemented**
([prs/PR-9-lifecycle-governance-endpoints.md](prs/PR-9-lifecycle-governance-endpoints.md);
22 tests incl. CLI/API parity, suite 526 green, goldens intact)
Scope: expose PR-4/5 operations; idempotency keys; error objects shared
with CLI.
As built: shared operations in `platform_cli` (CLI verbs format, API
serves — parity tested), 19 `/v1` operations with their CLI mapping,
`Idempotency-Key` replay/conflict, `agentd/platform_errors.py` (the CLI
prints the same `{"error": …}` in `--json` mode), audited mutating calls,
reload refused where the daemon cannot run compose; contract
`1.0.0-draft.9`.
**PR-10 · Run endpoints** — M — ✅ **implemented**
([prs/PR-10-run-endpoints.md](prs/PR-10-run-endpoints.md); 11 tests incl.
a real scripted run through the daemon, suite 537 green, goldens intact)
Scope: async run registry (start/status/report/cancel for run/sprint/fix/
evolve), concurrency limits.
As built: `control/runs.py` + `runs_api.py` over the existing pipelines
(+ `plan` as the A0 kind), records under `config/control/runs/` recovered
on restart, cooperative cancellation through the model client, limits
`control.max_concurrent_runs`/`max_queued_runs`, one in-place job per
project, registered projects only and never a push; contract
`1.0.0-draft.10`.
**PR-11 · CLI connected mode** — M — ✅ **implemented**
([prs/PR-11-cli-connected-mode.md](prs/PR-11-cli-connected-mode.md); 17
tests incl. the parity smoke and a real-socket run, suite 554 green,
goldens intact)
Scope: transport auto-detect, identical UX and outputs both modes;
management verbs fail fast offline; parity smoke.
As built: `control/client.py::ConnectedOps` mirrors the direct operations
(verbs format `ctx.ops.*` on either context — same text/JSON/errors),
`--transport auto|connected|direct` / `EZAI_TRANSPORT` / `EZAI_CONTROL_URL`,
a requested connected transport or a token-less reachable daemon fails fast
(exit 2), `bootstrap`/`up`/`down` stay host-only; `status` shows its
transport.
**PR-12 · Phase close** — S — ✅ **implemented**
([prs/PR-12-p2-phase-close.md](prs/PR-12-p2-phase-close.md); 4 tests,
suite 558 green, goldens intact; **ADR-028 → Accepted — P2 closed**)
Scope: kill-the-daemon test, two-concurrent-runs test, spec freeze.
As built: a real `ezaid` process SIGKILLed with repo work unaffected and
management verbs falling back / failing fast; two real scripted runs on
two repositories supervised concurrently through the API plus a gated
cancel pair; contract frozen at `1.0.0` with a 29-operation inventory;
deployment shapes decided (`make control-up` container overlay for model /
governance, `make control-serve` host daemon for SWE runs; `make up` does
not start it in V1). P3 / P4 / P5 are ready in parallel.

### Phase P3 — OpenWebUI integration (ADR-029)

**PR-13 · swe-server MCP tool server** — L — ✅ **implemented**
([prs/PR-13-swe-tool-server.md](prs/PR-13-swe-tool-server.md); 7 tests
incl. the plan → confirm → run → report loop and the catalog's negative
check, suite 565 green, goldens intact; **ADR-029 → Proposed**)
Scope: tool catalog of OPENWEBUI_INTEGRATION §2 (start/inspect only),
project allowlist, mcpo registration, markdown report rendering.
As built: vendored `mcp-servers/swe-server/` (FastMCP, thin over the 1.0.0
contract, no agentd import), twelve tools, refusals rendered as answers,
mcpo `swe` entry + image copy + compose env (`EZAI_CONTROL_URL_MCPO`,
`host.docker.internal`); `swe_test`/`swe_review`/`model_benchmark` deferred
to a 1.1 contract slice; OpenWebUI connection left to PR-14.
**PR-14 · Orchestrator persona** — S — ✅ **implemented**
([prs/PR-14-orchestrator-persona.md](prs/PR-14-orchestrator-persona.md);
8 tests incl. a bootstrapped platform serving `role-orchestrator`, suite
573 green, goldens intact)
Scope: `orchestrator` role entry + LiteLLM alias (data), system preset,
first-run pre-registration.
As built: role + alias proven (they were data since P1), the preset
`config/prompts/orchestrator.md`, the SWE tool server pre-registered in
OpenWebUI's `TOOL_SERVER_CONNECTIONS`, `make orchestrator` installing the
persona model row (base `role-orchestrator`, preset, tool server bound to
this persona only) idempotently after the first login.
**PR-15 · Boundary hardening** — M — ✅ **implemented**
([prs/PR-15-boundary-hardening.md](prs/PR-15-boundary-hardening.md); 21
tests, suite 594 green, goldens intact; **ADR-029 → Accepted, P3 closed**)
Scope: negative tests proving governance mutations unreachable from chat;
prompt-injection drill script in CI; regression pass showing chat/RAG
byte-identical.
As built: a per-client policy in the control plane (`swe-server` may read
and `run_start` only; everything else `client_forbidden` + audited), the
catalog and every other 1.0.0 mutation negative-tested through the real MCP
protocol and the API, the injection drill (hostile task obeyed by a
scripted model: push, workspace escapes and an unlisted shell command all
denied and journaled; local commit, empty `origin`, registry and queue
untouched), a committed chat-stack baseline (`scripts/chat-stack-baseline.py`)
compared by test; `make swe-drill` + the CI step.

### Phase P4 — Admin Center (ADR-030)

**PR-16 · ezaid client + Overview/Runs pages** — L (monitor extension) —
✅ **implemented**
([prs/PR-16-admin-center-overview-runs.md](prs/PR-16-admin-center-overview-runs.md);
10 tests incl. a real-Chromium smoke, suite 604 green, goldens intact;
**ADR-030 → Proposed**)
Scope: the control-plane client inside the monitor, the Overview and Runs
pages (run detail deep-linkable, cancel for admins).
As built: `monitor/admin_center.py` installed on the existing monitor app
with its RBAC (viewer reads, admin cancels), token server-side, the monitor
login forwarded as the audited human (`<login> via admin-center`), pages as
path routes from one framework-free template, data aggregated per page, a
daemon that is down is a page state; compose/Dockerfile/`.env.example`
wiring in the mcpo pattern; the chat-stack baseline treats the monitor's
control-plane keys as additive (fixture unchanged).
**PR-17 · Models/Routing/Runtime pages** — L — ✅ **implemented**
([prs/PR-17-admin-center-models-routing-runtime.md](prs/PR-17-admin-center-models-routing-runtime.md);
15 tests incl. a real-Chromium smoke with an admin action, suite 619
green, goldens intact; ADR-030 PR-17 slice)
Scope: role-first cards, fit badges, explain views, runtime switch
pre-check UX.
As built: Models page (group panels in resolution order, fit badges from the
platform recommender only, catalog with per-variant verdicts, generations,
queue; admin mutations install · benchmark · activate · upgrade · retire ·
uninstall · rollback through the daemon, approval-gated where a serving role
changes), Routing page (MODEL_ROUTING §7 explain view: reasons, contracts,
checks, generation), Runtime page (engine slot + per-runtime switch
pre-check naming blockers and candidates; no switch button — the contract
has no runtime verb). One additive snapshot enrichment (per-model groups /
context / format / license); the 1.0.0 artifact is byte-identical.
**PR-18 · Governance queue + approval modal** — M — ✅ **implemented**
([prs/PR-18-admin-center-governance.md](prs/PR-18-admin-center-governance.md);
8 tests incl. a real-Chromium smoke approving and rejecting from the page,
suite 627 green, goldens intact; ADR-030 PR-18 slice)
Scope: evidence panels, approve/reject, deep links.
As built: `/governance` (pending queue + history with status filter and
decision records) and the approval view `/governance/<id>` — what changes
(diff, affected roles before → after), evidence (host benchmarks, fit,
capability report, runtime switch flag, class), proposed by, reversibility
(the generation a rollback restores), decision (Reject with required
reason, Approve & apply) — through the daemon as the monitor login, admin
only, same-origin guarded, refusals verbatim; Overview/Models link to it;
honest note that only activation/upgrade requests enter the queue today.
**PR-19 · Sprints/Evolution/Memory/Projects pages** — M — ✅ **implemented**
([prs/PR-19-admin-center-sprints-evolution-memory-projects.md](prs/PR-19-admin-center-sprints-evolution-memory-projects.md);
13 tests incl. a real-Chromium smoke, suite 640 green, goldens intact;
ADR-030 PR-19 slice; **contract 1.1.0**, additive)
As built: Projects (allowlist with each project's work; register / remove
through the daemon), Sprints (runs + reports: waves, task results, the
dependency graph as mermaid source; console start from a pasted spec —
journey 3), Evolution (cycles + reports: proposal, benchmark before → after,
PR / bundle, "awaiting human review"; console start with focus — journey
4), Memory (per-project browser with kinds, search, counts; remember a
curated rule / style / decision). The Memory page needed data 1.0.0 never
served: `GET|POST /v1/projects/{name}/memory` added by the contract's own
rule (minor bump, artifact regenerated, inventory pins 29 + 2). Console
starts amend the parity matrix for sprint and evolve only.
**PR-20 · SSO handoff + Browser-QA suite** — M — ✅ **implemented**
([prs/PR-20-admin-center-sso-journeys.md](prs/PR-20-admin-center-sso-journeys.md);
6 tests incl. the five journeys driven by the platform's own Browser QA
harness in a real Chromium, suite 646 green, goldens intact; **ADR-030 →
Accepted, P4 closed**)
Scope: trusted-header handoff (Basic fallback), the five zero-CLI journeys
of WEBUI_PRODUCT_STRATEGY §5 as Browser-QA workflows in CI.
As built: identity handoff in the monitor — the Basic login, else a
proxy-set trusted header with a shared secret (`MONITOR_SSO_TRUSTED_HEADER`
/ `_SECRET` / `_ADMINS`), else the OpenWebUI session cookie validated
against `MONITOR_SSO_OPENWEBUI_URL`; opt-in, Basic stays the fallback, the
audit trail names the person. Inline confirmations replace native dialogs;
the Overview banner rolls back the last change in three clicks; sprint views
carry "how to merge". The five journeys are
`agentd/examples/browser-qa.admin-center.yaml`, run by `BrowserQAHarness`
against a launcher serving the monitor over an in-process daemon; journeys
1, 2 and 4 stop at stated boundaries (the chat-side check needs OpenWebUI; a
real runtime switch needs a fitting variant; evolution proposals are not yet
queue items). P4 exit criterion 1 proven on both surfaces.

### Phase P5 — First-Run Experience (ADR-031)

**PR-21 · install.sh** — M — ✅ **implemented**
([prs/PR-21-install-sh.md](prs/PR-21-install-sh.md); 21 tests incl. the
bash entry driven end to end and the F5 repair proof, suite 667 green,
goldens intact; **ADR-031 Proposed**)
Scope: capability detect, `.env` generation/validation with printed fixes
**before any download** (F8), secrets minting, single review-edit stop,
re-run repair mode (F5).
As built: `install.sh` (preflight + the agentd venv) → `python -m
agentd.installer`: detect with the platform's own `capability.py`, or assert
a class (`--profile` / `--class`, recorded as `EZAI_CAPABILITY_CLASS` and
honored by `make bootstrap`); a fresh `.env` from the example with the seven
secrets minted, `AI_RUNTIME` from the descriptors' new `default_for_classes`
data and the hardware recorded; repair of an existing `.env` (backup, user
values byte-identical, only missing / placeholder secrets minted,
idempotent, nothing else on disk touched); the bootstrap's F8 validation
with a class-aware hint, nothing downloaded; the one review-edit stop
(editor on a terminal, exit 3 otherwise, `--yes` skips it, `--check` writes
nothing); `make install`. The installer never picks models.
**PR-22 · Setup pipeline + smoke** — M — ✅ **implemented**
([prs/PR-22-setup-pipeline.md](prs/PR-22-setup-pipeline.md); 17 tests over
an offline pipeline — Docker, HTTP, planner and evaluator injected — suite
684 green, goldens and the chat-stack baseline intact; ADR-031 PR-22 slice)
Scope: steps 4–8 of FINAL_FIRST_RUN_EXPERIENCE §3
(fetch→render→up→verify→report), the "Platform ready" card, `local-ezai
init` fallback wizard.
As built: `make setup` = `install.sh` + `local-ezai setup` — the bootstrap
when no registry exists, images with the rendered engine override, the
embedding model, `up -d`, wait-ready by the runtime descriptor's `ready`
verb and the health table on host ports, smoke (chat turn required; RAG,
`plan_only` on the bundled sample project and evaluate-models advisory),
`config/first-run/report.{json,md}`, the card as an OpenWebUI banner through
an optional env_file with automatic rollback, the persona attempted;
`make setup-system` keeps the system-package script; `setup-gpu|cpu|n97`
assert their profile; `make up*` fetch only the embedding model;
`local-ezai init` proposes the recommended set from the catalog and writes
catalog ids to `.env` before running the pipeline.
**PR-23 · Offline bundle + FRE acceptance suite** — M — ✅ **implemented**
([prs/PR-23-offline-bundle-acceptance.md](prs/PR-23-offline-bundle-acceptance.md);
20 tests (25 collected) — the eleven criteria scripted, the bundle's edges,
the console card, the sixth journey in real Chromium — suite 709 green,
goldens and the chat-stack baseline intact; **ADR-031 → Accepted, P5 closed**)
Scope: bundle create/consume, scripted F1–F11 acceptance tests, onboarding
Browser-QA'd.
As built: `local-ezai bundle create <dir>` (every compose image, the
registry's GGUF artifacts, the hub cache, a manifest with seeds and
checksums) and `install.sh --offline <dir>` (images loaded, weights placed,
seeds and `EZAI_OFFLINE=1` written; the pipeline verifies images instead of
pulling, the fetchers refuse the network); `make bundle` /
`make setup-offline`; `agentd/tests/acceptance/` (`make swe-accept`) with
one offline test per criterion; contract **1.2.0** adds the read-only
`first_run_report`, the Overview shows the Platform-ready card, journey
`j0-first-run-card` joins the Browser-QA suite. The suite surfaced, and this
PR fixed, the bootstrap's `auto` dedupe (one model for every group).

### Phase P6 — Parity, agnosticism proof, release

**PR-24 · Parity harness** — M — ✅ **implemented**
([prs/PR-24-parity-harness.md](prs/PR-24-parity-harness.md); 15 tests —
eleven row tests over the twelve §3 rows, the chat ceiling per row, the
doc-table and gate-wiring tripwires, the normaliser's own test — suite 724
green, goldens and the chat-stack baseline intact; **P6 opened**)
Scope: CLI-direct vs CLI-connected vs API state/audit equivalence across
the CLI_AND_WEBUI_STRATEGY §3 matrix; release-gate wiring in CI.
As built: `agentd/tests/parity/` (`make swe-parity`) runs every matrix row
on three identical worlds — one per surface, the API called as the console
(`X-EZAI-Client: admin-center`) — and asserts equal response bodies, equal
declarative state and equal operation audit, the transports' annotations
normalised explicitly as data (`via <client>`, `api.*` / `run.*` events,
timestamps, run ids, hashes, paths); deliberately absent cells are asserted
absent (repo-work verbs never probe the daemon, `up`/`down` have no
operation, the memory verb's record is the store), the chat client's
ceiling is checked row by row, and the §3 table itself is a tripwire.
`make release-gate` chains lint · chat-stack baseline · drill · acceptance
· parity · full suite; the manual-trigger CI workflow gains the acceptance
and parity steps. The gate found, and this PR fixed, `model rollback
--json` printing a text notice before its JSON.
**PR-25 · Agnosticism gates** — M — ✅ **implemented**
([prs/PR-25-agnosticism-gates.md](prs/PR-25-agnosticism-gates.md); 14 tests
— the drill in three, the H1 audit in four, H2–H4 in seven — suite 738
green, goldens and the chat-stack baseline intact)
Scope: `mockengine` third-runtime drill (RUNTIME_ABSTRACTION §6) green with
zero diffs outside descriptors; H1 vendor-string CI audit; H2–H4 fixtures.
As built: `agentd/tests/gates/` (`make swe-gates`, in `make release-gate` and
the manual CI workflow). The drill's descriptor is a test fixture
(`tests/fixtures/providers/mockengine.yaml`), its image an in-test OpenAI-API
stub; bootstrap → real side-load validate + benchmark → render (multi form)
→ day-2 install/benchmark/activate/approve/rollback → the pipeline's
wait-ready on the descriptor's readiness path → `up --rendered`, all through
the CLI, with a grep tripwire that no shipped code names the runtime. The
H1 audit's scope and allowances are data with reasons; stale allowances
fail. H2 runs the same `auto` seeds on both classes, H3 fits a 7B Q4 entry
on the four fixture vectors, H4 proves `--profile n97` ≡ `--class cpu-low`
end to end. Found and fixed: the resolver's default runtime for a source
ignored the active slot (`lifecycle.slot_runtime_for`). Found and reworded:
four brand words in help text, a prompt note, a script header, a comment.
Residual recorded for PR-26: the health table's engine probe path.
**PR-26 · Release train** — M — ✅ **implemented up to the human steps**
([prs/PR-26-release-train.md](prs/PR-26-release-train.md); 5 tripwire tests
— suite 743 green)
Scope: docs refresh (USER_GUIDE, OPERATION_MANUAL, CLI_REFERENCE,
TROUBLESHOOTING, MAINTENANCE_GUIDE absorb new surfaces), 72 h soak runbook +
results, product DoD checklist (TARGET_PRODUCT_V1 §8) item-by-item, human
release sign-off, tag **v1.0.0**.
As built: the five guides rewritten or extended for V1 (the five-step first
run, the Orchestrator, the Admin Center, models and approvals as a user; the
gates and the soak in operations; contract 1.1.0/1.2.0 rows and the
resolver rule in the CLI reference; five new troubleshooting sections; the
V1 layout, generation/rollback operations and the release procedure in
maintenance); `docs/RELEASE_NOTES.md` created (CLAUDE.md mandate) with the
v1.0.0 entry and the prior history; `docs/SOAK_RUNBOOK.md` + `scripts/soak.sh`
(`make soak`: a fixed 72 h schedule of health, status, bench, scripted SWE
runs, lifecycle churn with rollback under load, evolution and container
stats, one JSON line per step, a results summary, a dry-run mode; the Day-0
timing doubles as the F1 measurement); `docs/V1_RELEASE_REPORT.md` with the
DoD checked item by item against evidence, the P6 criteria status, known
limitations and the human checklist ending in the sign-off table and the
tag; version **1.0.0** in `__init__` and `pyproject.toml` (they disagreed
before). **Human steps remaining, by design:** the soak results on both
host classes, the sign-off, the merge, the tag — agents propose, humans
approve.

## 4. Sizing & sequence summary

| Phase | PRs | Sizes | Parallel with |
|---|---|---|---|
| P1 | PR-1..7 | S:1 M:3 L:3 | — (foundation) |
| P2 | PR-8..12 | S:1 M:4 | — |
| P3 | PR-13..15 | S:1 M:1 L:1 | P4, P5 |
| P4 | PR-16..20 | M:3 L:2 | P3, P5 |
| P5 | PR-21..23 | M:3 | P3, P4 |
| P6 | PR-24..26 | M:3 | — (gate) |

26 PRs total; longest dependency chain PR-1→…→PR-12→(P3/P4/P5)→PR-24→26.

## 5. Risk register (PR-level)

| PR | Risk | Mitigation baked into the PR |
|---|---|---|
| PR-3/PR-7 | LiteLLM/engine reload behavior differs per runtime | render-to-parallel-path first; cutover isolated in PR-7 with diff review; self-rollback protocol tested before cutover |
| PR-5 | half-applied generation on crash | atomic snapshot-then-render + health-gated commit; crash-injection test |
| PR-6 | alias switch changes resolution silently | PR-1 golden test is the tripwire; runs both before/after in the same PR |
| PR-13 | tool surface creep | catalog frozen in the ADR-029 spec; negative tests enumerate forbidden verbs |
| PR-21 | installer bricks an existing install | repair-mode test (F5) is a merge condition |

## 6. Definition of PR-ready (checklist pasted into each PR description)

- [ ] Scope matches this plan's entry (deviations listed and justified)
- [ ] New tests cover the PR's scope; full suite green; lint clean
- [ ] Pre-existing tests unmodified (or diff justified under Behavior notes)
- [ ] Architecture doc updated to as-built; phase ADR status correct
- [ ] `local-ezai . review` findings attached and addressed
- [ ] Rollback note present (revert or down-migration)
- [ ] No model/vendor names introduced into code or defaults (H1 discipline)
