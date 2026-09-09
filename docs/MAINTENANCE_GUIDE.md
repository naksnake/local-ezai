# Local-EZAI — Maintenance Guide

How to keep the platform healthy, upgrade it, and repair it. Day-to-day
operation: [OPERATION_MANUAL.md](OPERATION_MANUAL.md). Symptom-driven
fixes: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## 1. Repository layout (what you are maintaining)

| Area | Contents | Change policy |
|---|---|---|
| `docker-compose*.yml`, `Dockerfile.*`, `config/*.yaml`, `monitor/`, `scripts/`, `Makefile` | the 8-service chat/RAG stack (+ the Admin Center on the monitor) | **byte-stable behavior** (ADR-002; `scripts/chat-stack-baseline.py --check` is the tripwire); additive overlays only |
| `config/providers/*.yaml` | runtime descriptors — the **only** home of runtime and hardware knowledge | data; adding a runtime = one YAML + images, zero code ([RUNTIME_ABSTRACTION_STRATEGY.md](RUNTIME_ABSTRACTION_STRATEGY.md) §2/§6) |
| `config/models/`, `config/governance/`, `config/projects.yaml`, `config/catalog/` | declarative platform state: registry + generations, the audit log and queue, the project allowlist, catalog additions | machine-written through `local-ezai …` / the Admin Center; back it up; never hand-edit the registry |
| `config/rendered/` | LiteLLM config, engine compose override, role map, capability report, manifest | **outputs** — never edit (drift is refused); re-rendered on every generation |
| `config/first-run/`, `config/control/`, `config/soak/` | first-run report + banner, the daemon's idempotency and run records, soak logs | machine-managed, git-ignored |
| `agentd/` | Autonomous SWE runtime + the platform (`platform_cli`, `control/`, lifecycle, renderer, installer, pipeline) | tests + ruff + ADR required for every change; the contract `docs/api/ezaid-openapi.json` is versioned |
| `agentd/tests/{unit,integration,security,acceptance,parity,gates}` | the suite and the release gates | a pre-existing test changes only with a "Behavior notes" justification in the PR |
| `docs/` | architecture + guides (this directory) + `docs/prs/` (one artifact per PR) | update in the same PR as the change |
| `.agent/` | roadmap, ADRs, architecture note, project memory | ADR for decisions; memory files are machine-managed |
| `.agentd.yaml` | the platform's own validation commands (self-hosting) | keep lint+test in sync with CI |

## 2. Routine maintenance

### The SWE runtime and the platform (agentd)

```bash
make swe-install      # (re)install editable + dev deps into the venv
make swe-test         # full offline suite (740+ tests) — green before any merge
make swe-lint         # ruff over src + tests
make release-gate     # lint · chat-stack baseline · boundary drill · F1–F11 · parity · agnosticism gates · suite
```

- **Dependency policy:** every dependency is version-floored in
  `agentd/pyproject.toml`. Upgrade deliberately: bump, run
  `make swe-test`, commit lockstep with any code adaptation.
- **Playwright:** `make swe-browsers` installs Chromium. In managed
  environments set `PLAYWRIGHT_BROWSERS_PATH`; the harness falls back to
  `$PLAYWRIGHT_BROWSERS_PATH/chromium` on version mismatch.
- **CI:** `.github/workflows/agentd-ci.yml` runs lint, the suite, the drill,
  the acceptance suite, the parity harness and the agnosticism gates — on
  **manual trigger** (`workflow_dispatch`, by decision); run
  `make release-gate` locally before every push to the integration branch.
- **The contract:** a change to the control plane's surface bumps
  `agentd.control.CONTRACT_VERSION` (additive → minor, breaking → major),
  regenerates `docs/api/ezaid-openapi.json` (`make control-spec`) and updates
  the inventory in `tests/integration/test_phase_p2_close.py` — one PR.

### The platform stack

- `make update-<profile>` pulls new images; **pin regressions immediately**
  (see the `mcp==2.0.0` incident noted in `mcpo/Dockerfile`).
- Back up before upgrades: `.env`, `config/models/`, `config/governance/`,
  `config/projects.yaml`, `config/catalog/`, the `qdrant-data/` volume, the
  OpenWebUI volume.
- After updating: `make health && make bench && local-ezai status` and
  compare with the last known-good numbers; the monitor image is rebuilt
  with `docker compose build monitor`.

### State hygiene

| State | Grows how | Prune with |
|---|---|---|
| `~/.agentd/runs/` | one dir per run (journal, report, screenshots) | `rm -rf` old run dirs |
| `~/.agentd/workspaces/` | one worktree per run | `git worktree remove <path>` in origin repo, then `git worktree prune` |
| `swe/*`, `sprint/*`, `evolve/*` branches | one per run | `git branch -D` after merge/reject |
| `<repo>/.agent/memory.db` | rows per terminal run | keep; it is the learning substrate. `local-ezai memory --search` to inspect |
| `<repo>/.agent/model_benchmarks.json` | overwritten per `evaluate-models` | nothing to do |
| `config/models/` generations | one per lifecycle change | keep — they are the rollback targets; `local-ezai model uninstall --force` removes weights a stored generation could still use |
| `config/control/runs/`, `config/control/idempotency/` | one record per API run / mutation | old records may be deleted while the daemon is stopped |
| `config/soak/` | one directory per soak | copy `results.md` into the release report, then delete |

## 3. Managing models (day 2)

Models are **state with generations**; nothing is edited in a file:

```bash
local-ezai model install <hf:…|gguf:…|catalog id> [--name N]   # fetch → side-load validation
local-ezai model benchmark <name>                               # tokens/s on this host
local-ezai model activate <name> --group <reasoning|coding|chat> # change request
local-ezai governance list · show <id> · approve <id> --reason … · reject <id> --reason …
local-ezai model upgrade <old> <new>                            # swap versions, approval
local-ezai model rollback [--to-generation N] --reason …        # previous generation, no approval, audited
local-ezai model retire <name> · uninstall <name> [--force]
local-ezai model history · explain <role> · catalog [--group G]
```

- **Approval is required** whenever a serving role changes (CLAUDE.md
  governance; [MODEL_GOVERNANCE_V2.md](MODEL_GOVERNANCE_V2.md)); the Admin
  Center's Governance page and `governance approve` are the same act.
- **Runtime switch:** activate a model the other runtime serves — the
  request is flagged `runtime.switch` and needs approval; the Runtime page
  pre-checks which active models the target runtime cannot serve.
- **A model the catalog does not know** works when it declares its chat
  template and tool-call format (catalog entry under `config/catalog/`, or
  `<SEED>_TOOL_FORMAT` at bootstrap); a mismatch is a render-time error
  naming the missing capability.
- The per-repo `.agent/model_registry.yaml` still overrides roles for one
  repository; the platform's registry seeds the defaults. `local-ezai
  models` shows the effective routing and its sources.

## 4. Generations, rendering and rollback

- Every lifecycle change persists a **generation** under `config/models/`
  and re-renders `config/rendered/` (LiteLLM config, engine compose
  override, role map, capability report) atomically; the apply protocol
  health-checks the new generation and **rolls itself back** on failure.
- `local-ezai model rollback` restores the previous (or a named) generation
  **as a new generation** — history is never rewritten; the Overview banner
  does the same in three clicks.
- **Drift:** the renderer refuses to overwrite a rendered file that was
  edited by hand. Revert the edit (the files are outputs) or pass `--force`
  deliberately.
- Consumers pick up a new generation with `--reload` (host CLI) or
  `make up` / `local-ezai up --rendered` (the daemon in its container cannot
  reload compose).
- `local-ezai status` shows generation vs **rendered** generation — they
  must match; a lag is repaired by re-applying (`activate` re-renders and
  records `generation.reconciled`).

## 5. Extending the platform

- **New runtime:** a descriptor under `config/providers/<id>.yaml` (file
  name = runtime id) plus images per accelerator kind — **no code**.
  Template: `agentd/tests/fixtures/providers/mockengine.yaml`; proof:
  `make swe-gates` (the third-runtime drill runs a mock runtime end to end).
- **New catalog entry:** `config/catalog/*.yaml` — declared sizes, context,
  template, tool-call format, license; never a hardware brand (the H1 word
  audit enforces it).
- **New agent:** subclass the pattern in `agentd/src/agentd/agents/`
  (system prompt in `agents/prompts/`, allowlisted tools in
  `permissions.py`, structured output schema in `schemas.py`), register in
  `agents/__init__.py`, add unit + integration tests, record an ADR.
- **New tool:** implement `Tool` in `agentd/src/agentd/tools/`, assign a
  risk tier (T0 read → T3 push), add to the per-agent allowlists.
  Fail-closed: an unlisted tool is a denied tool.
- **New validation category:** extend `_CATEGORY_ORDER` in
  `agents/validator.py` and the repo `.agentd.yaml` schema — order matters
  (cheap static checks first).
- **New CLI verb / control-plane operation:** the operation function lives
  in `platform_cli` and is called by **both** the CLI verb and the API
  endpoint (the parity harness fails otherwise); document it in
  [CLI_REFERENCE.md](CLI_REFERENCE.md), classify it in the chat-ceiling table
  of `tests/parity/test_parity_matrix.py`, bump the contract (§2).

## 6. Release procedure

1. `make release-gate` green on the release branch (lint, the chat-stack
   baseline, the boundary drill, F1–F11, the parity harness, the
   agnosticism gates, the full suite); CI green.
2. The **72 h soak** on both host classes per
   [SOAK_RUNBOOK.md](SOAK_RUNBOOK.md) (`make soak`); results into the
   release report; every failure explained or fixed and re-soaked.
3. Bump the version in **both** `agentd/src/agentd/__init__.py`
   (`__version__`) and `agentd/pyproject.toml` (`version`) — semver; the
   contract version moves only when the API surface does.
4. Update [RELEASE_NOTES.md](RELEASE_NOTES.md) (entries at the top, dated;
   evolution runs prepend theirs the same way) and write the release report
   with the product DoD checked item by item
   ([V1_RELEASE_REPORT.md](V1_RELEASE_REPORT.md) is the template).
5. `local-ezai . docs` to refresh the generated guides; review the diff.
6. Human approval on the release PR (**production releases require
   approval** — [GOVERNANCE.md](GOVERNANCE.md)); merge.
7. Tag after merge: `git tag v<version> && git push --tags`.
8. Build/verify the wheel: `python -m build agentd/` — hatchling requires
   `agentd/README.md` to exist.

## 7. Health checklist (run after any maintenance)

```bash
make health                     # 8 services up
local-ezai status               # generation = rendered generation · engine/router/control up · no unexpected approvals pending
local-ezai . test               # the platform validates itself (lint + the suite)
local-ezai evaluate-models      # every model role responds correctly
ezai runs                       # journals readable
python3 scripts/chat-stack-baseline.py --check   # the chat stack is unchanged
```

All green ⇒ the platform is healthy and still self-hosting. Before a
release, `make release-gate` instead of the third line.
