# PR-6 — CLI namespaces + role aliases in code

**Phase:** P1 · **ADR:** ADR-027 (Proposed, PR-6 slice added) · **Size:** M ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-6 ·
**Depends on:** PR-1 (registry), PR-3 (renderer, rendered role map),
PR-4 (install/benchmark), PR-5 (activate/queue/apply) · **Status:**
implemented on `claude/next-ready-pr-bnq7r3` as the fourth commit on top of
PR-3/4/5 (unmerged; each commit reverts independently)

## Scope (as delivered)

- **Role aliases in code (CF-3 closed, ADR-007 completed):**
  `agentd/config.py` defaults every role to `role-<role>`
  (`ROLE_ALIAS_PREFIX`, `LLM_ROLES`; `default` → `role-chat`);
  `model_for_role` never falls back to a model name. The renderer imports
  the same prefix (single definition).
- **Hand-written LiteLLM profiles serve the aliases (data):**
  `config/litellm-config{,.cpu,.n97}.yaml` gain one `role-*` entry per LLM
  role pointing at the model each profile already serves. This is what keeps
  every shipped profile working the moment agentd asks for `role-planner`
  instead of a model name — tested (`test_hand_written_litellm_configs_
  serve_every_role_alias`). Existing entries untouched; PR-7 retires these
  files into rendered output.
- **`agentd/src/agentd/routing.py` — the three routing layers**
  (MODEL_ROUTING_DESIGN §5): aliases < platform role map (rendered
  `role_map.yaml`, else Registry v2 resolution) < per-repo ADR-020 registry,
  where a role the repo declares is taken **exactly** (a repo pin is an
  explicit chain — platform fallbacks are not inherited). Platform
  discovery: `platform.config_dir` (new config section, env
  `AGENTD_PLATFORM__CONFIG_DIR`) or walk-up from the project to
  `docker-compose.yml` + `config/providers/`; never package location or
  cwd (hermetic). `prepare_run`, `evaluate-models`, and `local-ezai models`
  resolve the same layers; `models` prints the sources.
- **`agentd/src/agentd/platform_cli.py` — the platform namespaces**
  (CLI_AND_WEBUI_STRATEGY §5), direct mode, thin over PR-3/4/5:
  `model install|benchmark|activate|upgrade|rollback|retire|uninstall|
  explain|history|catalog`, `governance list|show|approve|reject`
  (approve applies the request; policy-approved proposals apply at once),
  `project add|list|remove` (the chat-ops allowlist `config/projects.yaml`,
  audited), `status` (generation vs rendered generation, slot runtime,
  model states, pending approvals, engine/router health), `up|down`
  (compose wrappers; profile override chain derived generically from the
  profile name — no SKU names in code; `--rendered` adds the engine
  override). `--json` everywhere; `--by` sets the audited actor;
  `--reload` opts into consumer reload + health (render-only default until
  PR-7). Exit codes: 0 · 1 refused/failed · 2 no platform.
- **Wiring:** `main_cli.py` registers the namespaces, skips the git-repo
  requirement for platform verbs, dispatches to `platform_cli`.
  `model_registry.parse_agent_model_map` extracted (loader unchanged).
- **Excluded by design:** connected mode / control plane transport
  (PR-8/11); bootstrap and cutover (PR-7); the chat-ops tool server that
  consumes `projects.yaml` (P3); Admin Center (P4); `k8s/litellm.yaml`
  (deferred with k8s, ADR-011); `make` targets (unchanged, still the
  installer/developer layer).

## Design decisions made in this PR

1. **The alias switch ships with its serving side.** Flipping agentd's
   defaults to `role-*` without LiteLLM serving them would break every
   default profile until PR-7; adding the aliases to the hand-written
   configs is additive data and exactly ADR-007's "each profile maps
   aliases to models". The cutover then replaces the files, not the idea.
2. **A repo pin is an exact chain.** With the platform now supplying
   fallbacks, letting a repo's `reviewer: llama3` inherit platform
   fallbacks would silently change what the golden test guarantees at the
   config level; `apply_platform_routing(skip_roles=…)` leaves declared
   roles entirely to the repo. `apply_model_registry` itself is unchanged
   (byte-identical behavior without a platform).
3. **Hermetic discovery.** Walking up from the *project* (not cwd, not the
   installed package) means test repositories and users' own repos never
   accidentally attach to a developer's platform checkout; self-hosting
   works because the checkout is the project. Everyone else sets
   `platform.config_dir` once (the PR-7 installer will write it).
4. **Pre-bootstrap installs persist.** `model install` on a platform
   without a registry creates one with providers + empty groups and no
   roles — servable, hence persistable — so a single-verb install before
   `make setup` is not lost; the bootstrap adds roles later.
5. **Approve = apply.** A human approval without application would leave
   the queue in a state no surface shows; `governance approve` runs the
   PR-5 protocol immediately and prints the outcome (self-rollback
   included).

## Tests (24 new; suite 435 → 459)

- `tests/unit/test_routing.py`: aliases are the only names in code (source
  grep tripwire); every hand-written LiteLLM profile serves every role
  alias with a model it serves; platform discovery (walk-up, explicit,
  env, none); rendered-vs-registry role map; **three-layer precedence**
  incl. repo pin = exact chain and hermetic no-platform behavior;
  `prepare_run` seeds from the platform and keeps the repo override, or
  keeps aliases without a platform.
- `tests/integration/test_platform_cli.py` (real `main()` with a YAML
  config, seams faked): status (+json, no-platform exit 2), explain with
  contract, catalog + recommendations, history, install local GGUF →
  benchmark (generation 2 → 3, trend file), bad reference, activate →
  queue → approve → applied → rollback, policy-approved activation applies
  immediately, reject needs a reason, upgrade → uninstall guard/force →
  retire refusal, project add/list/remove, up/down compose chains +
  unknown profile, `models` shows the platform source, repo verbs stay
  hermetic without a platform.
- **Golden (PR-1) and PR-3 goldens untouched and passing** — the plan's
  tripwire for this PR.

## Behavior notes

- **Default routing path changes (planned, V1_PR_PLAN PR-6):** with no
  per-repo registry and no platform, agentd now asks LiteLLM for
  `role-<role>` instead of a hard-coded model name. The shipped profiles
  serve those aliases (above), so default installs keep working; a
  hand-maintained LiteLLM config elsewhere must add the aliases or set
  `llm.roles` explicitly.
- **One pre-existing test modified:** `tests/unit/test_governance.py::
  test_apply_model_registry` asserted the old hard-coded default
  (`qwen2.5-7b`) for untouched roles; it now asserts the alias. This is the
  assertion the plan's Behavior note anticipated; the test's subject
  (repo registry application) is unchanged.
- `evaluate-models`, `local-ezai models`, and the evolution pipeline's own
  client additionally apply the platform layer when a platform is
  attached; without one they are byte-identical to before.
- `agentd/README.md` config example and `docs/CLI_REFERENCE.md` updated.

## Rollback note

`git revert <commit>`: new modules and tests are additive; the LiteLLM
config changes are additive entries; `config.py` defaults revert to the
previous names. No migrations; `config/projects.yaml` (if created) is a
plain file nothing else reads yet.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane —
  unavailable here; to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry (additions justified above: LiteLLM
      alias entries, `platform` config section, `--reload`/`--by` flags,
      `governance approve` applying)
- [x] New tests cover the scope; full suite green (459); ruff clean
- [x] Pre-existing tests unmodified except the one justified assertion;
      PR-1 golden and PR-3 goldens passing
- [x] Architecture docs updated to as-built (MODEL_ROUTING_DESIGN §5,
      MODEL_GOVERNANCE_V2 §1, CLI_REFERENCE, agentd/README); ADR-027 gains
      the PR-6 slice (Proposed)
- [x] Rollback note present
- [x] No model/vendor names in code: `config.py` carries none (tested);
      the CLI derives compose override files from the profile name
      instead of naming SKUs; model names live only in LiteLLM/catalog
      data and test fixtures
