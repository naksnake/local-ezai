# Local-EZAI — Maintenance Guide

How to keep the platform healthy, upgrade it, and repair it. Day-to-day
operation: [OPERATION_MANUAL.md](OPERATION_MANUAL.md). Symptom-driven
fixes: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## 1. Repository layout (what you are maintaining)

| Area | Contents | Change policy |
|---|---|---|
| `docker-compose*.yml`, `Dockerfile.*`, `config/`, `monitor/`, `scripts/`, `Makefile` | the 8-service chat/RAG stack | **byte-stable behavior** (ADR-002); additive overlays only |
| `agentd/` | Autonomous SWE runtime (11 agents, LangGraph, CLI) | tests + ruff + ADR required for every change |
| `docs/` | architecture + guides (this directory) | update in the same PR as the change |
| `.agent/` | roadmap, ADRs, model registry, project memory | ADR for decisions; memory files are machine-managed |
| `.agentd.yaml` | the platform's own validation commands (self-hosting) | keep lint+test in sync with CI |

## 2. Routine maintenance

### The SWE runtime (agentd)

```bash
make swe-install      # (re)install editable + dev deps into your venv
make swe-test         # full offline suite — must be green before any merge
make swe-lint         # ruff over src + tests
```

- **Dependency policy:** every dependency is version-floored in
  `agentd/pyproject.toml`. Upgrade deliberately: bump, run
  `make swe-test`, commit lockstep with any code adaptation.
- **Playwright:** `make swe-browsers` installs Chromium. In managed
  environments set `PLAYWRIGHT_BROWSERS_PATH`; the harness falls back to
  `$PLAYWRIGHT_BROWSERS_PATH/chromium` on version mismatch.
- **CI:** `.github/workflows/agentd-ci.yml` runs lint + tests on every
  push touching `agentd/`. Keep it green; it is the merge gate.

### The platform stack

- `make update-<profile>` pulls new images; **pin regressions immediately**
  (see the `mcp==2.0.0` incident noted in `mcpo/Dockerfile`).
- Back up before upgrades: `.env`, `qdrant-data/` volume, OpenWebUI volume.
- After updating: `make health && make bench` and compare with the last
  known-good numbers.

### State hygiene

| State | Grows how | Prune with |
|---|---|---|
| `~/.agentd/runs/` | one dir per run (journal, report, screenshots) | `rm -rf` old run dirs |
| `~/.agentd/workspaces/` | one worktree per run | `git worktree remove <path>` in origin repo, then `git worktree prune` |
| `swe/*`, `sprint/*`, `evolve/*` branches | one per run | `git branch -D` after merge/reject |
| `<repo>/.agent/memory.db` | rows per terminal run | keep; it is the learning substrate. `local-ezai memory --search` to inspect |
| `<repo>/.agent/model_benchmarks.json` | overwritten per `evaluate-models` | nothing to do |

## 3. Upgrading models

1. Edit `.agent/model_registry.yaml` (per repo) — set `primary` /
   `fallback` per role. **Model replacement requires human approval**
   (CLAUDE.md governance); do it via a reviewed PR.
2. Ensure LiteLLM serves the new model names (`config/litellm_config.yaml`).
3. Verify: `local-ezai evaluate-models` — every role must pass its probe
   (structured-output roles must return valid JSON).
4. Benchmarks land in `.agent/model_benchmarks.json`; keep the file in the
   PR so the decision is evidenced.

## 4. Extending the runtime

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
- **New CLI command:** add the subparser (use `parents=[common]` with
  `argparse.SUPPRESS` defaults) + handler in `main_cli.py`, document it in
  [CLI_REFERENCE.md](CLI_REFERENCE.md), test it in
  `tests/integration/`.

## 5. Release procedure

1. `make swe-test && make swe-lint` green; CI green.
2. Bump `agentd/src/agentd/__init__.py` `__version__` (semver).
3. `local-ezai . docs` to refresh generated guides; review the diff.
4. Update `docs/RELEASE_NOTES.md` (evolution runs prepend entries
   automatically; hand-written releases add theirs the same way).
5. Human approval on the release PR (**production releases require
   approval** — [GOVERNANCE.md](GOVERNANCE.md)).
6. Tag after merge: `git tag v<version> && git push --tags`.
7. Build/verify the wheel: `python -m build agentd/` — hatchling requires
   `agentd/README.md` to exist.

## 6. Health checklist (run after any maintenance)

```bash
make health                     # 8 services up
local-ezai . test               # platform validates itself (lint + 260+ tests)
local-ezai evaluate-models      # every model role responds correctly
ezai runs                       # journals readable
```

All four green ⇒ the platform is healthy and still self-hosting.
