# PR-7 — Bootstrap core + `.env` seed consumption + cutover

**Phase:** P1 (closes) · **ADR:** ADR-027 → **Accepted** · **Size:** M ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-7 ·
**Depends on:** PR-1 store, PR-3 renderer, PR-4 install/benchmark + catalog,
PR-5 propose/apply, PR-6 CLI namespaces · **Status:** implemented on
`claude/next-ready-pr-bnq7r3` as the fifth stacked commit (PR-3..6
unmerged; each commit reverts independently)

## Scope (as delivered)

- **`agentd/src/agentd/bootstrap.py` — the bootstrap core**
  (FINAL_FRE §2–§3 steps 2, 4–5): `parse_env` · `read_seeds` (V1 seeds
  `AI_RUNTIME` + `REASONING_MODEL`/`CODING_MODEL`/`CHAT_MODEL`, optional
  `EZAI_ROLE_PIN_<role>`, `<SEED>_TOOL_FORMAT`, `<SEED>_CONTEXT`; the
  consumption stamp) · **F11 migration** from `defaults/legacy_seeds.yaml`
  (a data table of the legacy `CHAT_MODEL`/`CPU_*`/`N97_*` families: the
  first family present becomes one model seeding all three groups, with the
  legacy **served name kept** so existing chats keep working, and the
  tool-call parser the legacy engine flags assumed) · **F8 validation**
  before any download (unknown/missing runtime, unknown scheme, format the
  runtime does not serve, more distinct models than a single-model slot
  hosts, `auto` the recommender cannot satisfy, declared tool format the
  runtime does not parse, undeclared format on a runtime without a generic
  handler when the group's roles need tools, declared context below a role
  contract, unknown pin role — every problem with its fix) ·
  `plan_generation` (reference roles + contracts, no reference pins, groups
  from seeds, `.env` pins) · **`bootstrap()`**: install (PR-4: fetch into
  the runtime's weights dir, side-load `validate_model`) → benchmark →
  activate → the **one implicit approval** (`propose(implicit_approval=True)`,
  honored only when no generation exists) → PR-5 `apply` (render, reload,
  health, self-rollback) → `stamp_env` (`EZAI_SEEDS_CONSUMED=1@<ts>` +
  a read-once notice). `--dry-run` returns the planned generation-1 diff
  (F10) with nothing fetched or written. A forced re-run on a platform with
  history files a **governed** change request instead of re-bootstrapping.
- **CLI:** `local-ezai bootstrap [--env] [--dry-run] [--skip-benchmark]
  [--force] [--reload] [--by] [--json]` (platform namespace).
- **Cutover:** `docker-compose.yml` mounts `config/rendered/litellm-config.yaml`;
  the cpu/n97 overrides drop their LiteLLM volume overrides; the three
  hand-written variants move to `agentd/tests/fixtures/legacy/` (historical
  reference for the diff review and the PR-3/PR-6 tests); the `Makefile`
  gains `bootstrap` (installs the agentd venv on demand, runs the CLI) and
  `require-rendered`, every `up*`/`update*` target adds the rendered engine
  override, every `setup-*` runs `bootstrap` between download and up;
  `.env.example` documents the seeds; `scripts/download-models.sh` accepts
  the `hf:` scheme.
- **ADR-027 → Accepted** with the as-built summary of PR-1..7.
- **Excluded by design:** `install.sh`, secrets minting, the single
  review-edit stop, repair mode (PR-21); smoke suite, "Platform ready"
  card, `local-ezai init` wizard, compose `up`/wait-ready inside the
  bootstrap (PR-22); offline bundle (PR-23); `k8s/litellm.yaml` (ADR-011);
  hardware detection into `.env` (PR-21 — the bootstrap detects live).

## Design decisions made in this PR

1. **Migration keeps the served name.** `CHAT_MODEL_NAME` /
   `CPU_CHAT_MODEL_NAME` / `N97_MODEL_NAME` become the registry name, so
   the rendered LiteLLM config serves the same alias the user's OpenWebUI
   chats already select, next to the new `role-*` aliases (tested end to
   end from a legacy `.env`).
2. **Legacy context flags are not model capabilities.** `MAX_MODEL_LEN` &
   co. were engine budgets; carrying them into `ModelEntry.context` made
   the default GPU profile (4096) fail the 8192-token role contracts. The
   class budget applies instead; an explicit `<SEED>_CONTEXT` below a
   contract is an F8 problem.
3. **Undeclared user sources get a runtime-dependent default, never a
   guess.** A bare `hf:`/`gguf:` seed has no declared tool-call format; on
   a runtime listing a template-driven `generic` handler it is used,
   otherwise validation names the parsers and the `<SEED>_TOOL_FORMAT`
   fix. Catalog seeds carry their own declarations.
4. **The implicit approval is generation 1 only** (MODEL_GOVERNANCE_V2 §2);
   `propose` refuses it on a platform with history, and the queue records
   the policy decision with a bootstrap-specific reason.
5. **The cutover is complete, not half.** Mounting the rendered LiteLLM
   config while the engine still came from `.env` flags would break vLLM
   (served-name mismatch); the Makefile therefore adds the rendered engine
   override on every start and refuses to start an un-bootstrapped platform
   with the fix printed.
6. **Bootstrapping needs the agentd CLI on the host.** `make bootstrap`
   creates `.venv-agentd` when neither the venv nor `local-ezai` exists. This
   is the V1 direction (the control plane and CLI are Python) and the first
   departure from "no host Python"; called out in Behavior notes.

## Tests (23 new; suite 459 → 482)

- `tests/unit/test_bootstrap.py`: `.env` parsing; V1 seeds + pins +
  inline comments/quotes; missing/invalid seed problems with the fix;
  consumption stamp; **F11**: accelerator, cpu, gguf families (served name,
  local file preferred over hub URL, `AI_RUNTIME` wins, precedence,
  no-seeds problem); **the shipped `.env.example` migrates and validates**
  (tripwire); **F8 matrix** (unknown/missing runtime, format × runtime,
  slot capacity, bad scheme, unknown catalog id, infeasible `auto`,
  unknown pin, undeclared/unknown tool format); `plan_generation`
  (reference roles without reference pins, seed pin, unknown pin target);
  **end to end**: generation 1 with active models, groups, roles, pins,
  benchmarks, rendered router preset + aliases, implicit approval audited,
  **F10 diff equals seeds**, stamp blocks re-run; consumed/invalid seeds
  refused with nothing written; dry run writes nothing; **migrated
  single-model bootstrap serves the legacy name**; skip-benchmark audited;
  missing benchmark function refused; implicit approval reserved for
  generation 1; **a forced re-bootstrap on a platform with history files a
  pending governed request** (diff against the live generation, no second
  stamp) that a human approves and PR-5 applies as generation 2; declared
  tool format/context reach the generation and a too-small context is
  refused by F8 before any download; `stamp_env` idempotent.
- `tests/integration/test_platform_cli.py`: `bootstrap --dry-run` then a
  real run (generation 1, benchmarks, rendered files, stamp, `status`),
  re-run refused; seed problems reported before downloading; missing
  `.env` → exit 2.
- PR-3/PR-6 tests now read the retired configs from
  `tests/fixtures/legacy/`; PR-1 golden and PR-3 goldens untouched.

## Behavior notes (the cutover)

- **`make up` / `up-cpu` / `up-n97` / `up-n97-igpu` / `update*` require a
  bootstrapped platform** and add `config/rendered/docker-compose.engine.yml`;
  an un-bootstrapped platform is refused with `make bootstrap` named.
  `make setup-gpu|cpu|n97` run `bootstrap` after downloading models.
- **`docker-compose.yml` mounts the rendered LiteLLM config**; the cpu/n97
  overrides no longer swap LiteLLM configs. `config/litellm-config{,.cpu,
  .n97}.yaml` are gone from `config/` (fixtures under `agentd/tests/`).
- **Existing installs:** the first `make bootstrap` migrates the legacy
  `.env` family in place (F11); the served model name is preserved, so
  OpenWebUI chats keep their selected model. Reviewed diff: the rendered
  config for a migrated GPU `.env` = the legacy file's model entry + the
  ten `role-*` aliases (PR-6 already added those to the legacy files) +
  the embedding entry, all at `engine:8000` instead of `vllm:8000`.
- **Host Python:** `make bootstrap` (and therefore `make setup-*`) creates
  `.venv-agentd` via `make swe-install` when the CLI is absent.
- **`up-n97-igpu` converges on detection.** The rendered engine override is
  the last compose layer, so its image/command (from the descriptor's
  accelerator entry for the *detected* accelerator — `igpu` whenever
  `/dev/dri` exists and no dedicated-accelerator tool answers) win over the
  hand-written `docker-compose.n97-igpu.yml`; that file's `devices` merge
  and it is kept for compatibility. On an N97 with a render node, `up-n97`
  and `up-n97-igpu` therefore start the same Vulkan engine.
- `.env.example` gains the seed section; `download-models.sh` strips a
  leading `hf:`.

## Rollback note

`git revert <commit>` restores the hand-written mounts and files, the
Makefile targets, and removes the bootstrap module. A platform that was
bootstrapped keeps `config/models/`, `config/rendered/`,
`config/governance/` and the `.env` stamp — inert files for the previous
code (the previous compose mounts the restored hand-written config). No
data migration is needed in either direction.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane —
  unavailable here; to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry (additions justified above:
      `<SEED>_TOOL_FORMAT`/`_CONTEXT`, the generic-handler default, the
      engine override in every `make up*`, the venv auto-install, `--force`
      as a governed request)
- [x] New tests cover the scope incl. F8/F10/F11; full suite green (482);
      ruff clean
- [x] Pre-existing tests unmodified except the two PR-3/PR-6 tests
      re-pointed at the fixture copies of the retired files; goldens
      passing
- [x] Architecture docs updated to as-built (FINAL_FRE §3, MODEL_LIFECYCLE
      §5, CURRENT_ARCHITECTURE, CLI_REFERENCE, README, DEPLOY-N97);
      ADR-027 → Accepted
- [x] Rollback note present
- [x] No model/vendor names in code: legacy variable names and the
      parsers they implied live in `defaults/legacy_seeds.yaml` (data);
      `bootstrap.py` names no model, engine, or brand
