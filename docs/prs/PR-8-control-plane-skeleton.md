# PR-8 — `ezaid` service skeleton (control plane, phase P2 opens)

**Phase:** P2 (opens) · **ADR:** ADR-028 → **Proposed** · **Size:** M ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-8 ·
**Depends on:** PR-7 (P1 closed: platform state, `status` snapshot,
governance queue + audit log, rendered manifest) · **Status:** implemented
on `claude/next-ready-pr-bnq7r3` as the sixth stacked commit (PR-3..7
unmerged; each commit reverts independently)

## Scope (as delivered)

- **`agentd/src/agentd/control/` — the service.** `app.py` builds the
  FastAPI application: `GET /health` (liveness, unauthenticated — docker
  healthcheck and `local-ezai status`), `GET /v1/health` (aggregated:
  control-plane info + the platform snapshot `status` prints + one probe per
  stack service), `GET /v1/whoami` (the identity the audit log records),
  `GET /v1/audit?limit=N` (tail of the single audit log), `/openapi.json` +
  `/docs`. `auth.py`: bearer **service token** (`EZAI_CONTROL_TOKEN`,
  constant-time compare, never defaulted in code — no token, no start) +
  **forwarded identity** (`X-EZAI-User`, `X-EZAI-Client`) → actor
  `<user> via <client>`; rejections are audited without the presented
  secret. `health.py`: the probe table as **data** (compose service names on
  `ai-net`, the engine slot through its neutral `engine` alias, credentials
  from env for the two services that need one; `control.health_targets`
  adds/replaces/removes entries); concurrent probes that never raise.
  `main.py`: the `ezaid` entry point (`--print-spec`, `--write-spec`,
  `--version`, serve). One **error envelope** everywhere
  (`{"error": {"code", "message", "fix"}}` for 401/404/422/503).
- **`agentd/src/agentd/audit.py` — the single audit log.** `AuditLog`
  (append-only JSONL, lock-serialized writers, `tail`/`count`) extracted from
  the governance queue; `GovernanceQueue.record/audit` delegate to it — same
  file (`config/governance/log.jsonl`), same `AuditRecord` fields, existing
  logs read back unchanged. The control plane records `control.started` /
  `control.stopped` / `auth.rejected` into it.
- **Contract artifact:** `docs/api/ezaid-openapi.json`, `info.version =
  CONTRACT_VERSION` (`1.0.0-draft.8`; PR-12 freezes `1.0.0`), security
  scheme `serviceToken`, the identity headers documented on every `/v1`
  operation. A tripwire test compares the committed document with the live
  app on the **contract surface** (operations, parameters, response codes,
  security, schema names) so framework rendering details cannot fail it.
  `make control-spec` regenerates.
- **Deployment:** `agentd/Dockerfile` (pinned FastAPI/uvicorn, `CMD ezaid`,
  liveness healthcheck), the opt-in overlay `docker-compose.control.yml`
  (`ezaid` on `${EZAI_CONTROL_PORT:-8010}`, `./config` mounted as the
  platform, `ai-net`), `make control-up|down|logs|spec`, `.env.example`
  gains `EZAI_CONTROL_TOKEN` + `EZAI_CONTROL_PORT`.
- **Config:** `control:` section (`host`, `port`, `token`,
  `health_targets`) + `AGENTD_CONTROL__*`; the product variables
  `EZAI_CONTROL_TOKEN` / `EZAI_CONTROL_PORT` seed it (stack convention, like
  `LITELLM_MASTER_KEY`). Optional extra `agentd[control]` (fastapi, uvicorn)
  — CI and `make swe-install` install it; direct mode never imports it.
- **CLI:** `local-ezai status` shows `control up/down` (probe of
  `/health` on `EZAI_CONTROL_PORT`); `platform_snapshot()` extracted from
  `cmd_status` and shared with `/v1/health` (output unchanged).
- **ADR-028 → Proposed** (`.agent/decisions.md`).
- **Excluded by design (later P2 PRs):** lifecycle/governance endpoints,
  idempotency keys and the full shared error catalogue (PR-9); run
  endpoints and concurrency limits (PR-10); CLI connected mode and transport
  auto-detect (PR-11); spec freeze, kill-the-daemon and two-concurrent-runs
  tests, starting the overlay from `make up` (PR-12); Admin Center / tool
  server clients (P3/P4); docker-socket access from the container.

## Design decisions made in this PR

1. **FastAPI, as an optional extra.** The house pattern (monitor,
   embed-server) and the contract-first strategy (§6 of
   CLI_AND_WEBUI_STRATEGY) call for a framework that generates the OpenAPI
   document; agentd's dependency set stays lean for the CLI because the
   framework lives behind `agentd[control]` and only `control/app.py`
   imports it (the package root exports constants the CLI uses).
2. **One audit log, one primitive.** Rather than a second log for API
   events, the governance log became the platform log through
   `agentd/audit.py`; `/v1/audit` therefore already shows approvals and
   bootstraps next to control-plane events.
3. **Fail closed on the token.** No default token in code: the service does
   not start without one (compose supplies `${EZAI_CONTROL_TOKEN:-…}` like
   every other stack credential; the installer mints it in P5). Rejected
   calls are audited with path, reason, peer and the *claimed* client — never
   the secret.
4. **Health targets are data; the engine is `engine`.** The table mirrors
   the monitor's checks but addresses the slot by its neutral alias
   (ADR-026) and lets `control.health_targets` change it without code.
5. **Contract-surface tripwire.** Comparing the whole generated document
   would break CI on a FastAPI/pydantic release; comparing the surface keeps
   the artifact honest about the contract while tolerating rendering.
6. **Opt-in overlay.** ADR-002: the daemon is a new plane; nothing in the
   base stack depends on it, `make up` does not start it, rollback is not
   starting it. PR-11/12 decide when connected mode makes it default.

## Tests (22 new; suite 482 → 504)

- `tests/unit/test_audit.py` (4): append + tail + count; records never
  rewritten (one JSON record per line); 8 concurrent writers × 25 records
  intact; governance queue and control plane share one log.
- `tests/unit/test_control_plane.py` (17): liveness open and versioned;
  `/v1` refuses missing / wrong / non-bearer credentials with the envelope,
  `WWW-Authenticate`, and audited rejections **without the secret**;
  forwarded identity → `nita via admin-center`, control characters
  stripped; `authenticate` fails closed without a configured token;
  aggregated health combines the platform snapshot (generation, rendered,
  slot runtime, models, approvals) with per-service probes, credentials
  reach only declared targets, the engine is probed as `engine`; down /
  erroring / pattern-missing services reported without failing; targets as
  data (replace, remove, add; through config); no platform → 503 envelope;
  audit endpoint tails the log incl. `control.started`/`stopped`, `limit`
  validation → 422 envelope; 404 envelope; the OpenAPI document (contract
  version, `serviceToken` scheme, every `/v1` op secured + 401 + identity
  headers, liveness open); **committed artifact matches the app**;
  `ezaid --print-spec/--write-spec/--version` need no platform or token;
  `ezaid` refuses to serve without a token; config seeded by product env
  with correct precedence; compose overlay additive (single service, port
  variable, network, mount, build) and the base stack untouched; H1 grep
  over the control package.
- `tests/integration/test_platform_cli.py` (1): `status` reports control
  health in JSON and text, probing `EZAI_CONTROL_PORT`.
- Pre-existing tests unmodified; PR-1/PR-3 goldens intact.

## Behavior notes

- `local-ezai status` prints `health: engine … · router … · control up|down`
  (one more item; JSON gains `health.control`). All other CLI output is
  byte-identical.
- `make swe-install` and CI install `agentd[dev,browser,control]`.
- No compose service, Make target, or `.env` key that existed before this
  PR changes; `docker-compose.control.yml` is new and never started
  implicitly.
- Inside the container the platform root is `/platform` (only `config/` is
  mounted). Operations that touch weights or `.agent/` on the host (install,
  benchmark trend) arrive with PR-9 and will extend the mounts then.
- **Verification boundary:** `docker compose -f docker-compose.yml -f
  docker-compose.control.yml config` validates the merged overlay (nine
  services, `ezaid` resolved with its port/env/mount); the image build
  itself was not exercised in this sandbox (no docker daemon) — first
  `make control-up` on a stack host builds it.

## Rollback note

`git revert <commit>` removes the control package, the overlay, the
Dockerfile, the Make targets and the audit module; the governance queue
regains its inline log methods (same file, same format — logs written by
the control plane remain readable, their `control.*`/`auth.*` events simply
stop). A running `ezaid` container is stopped with `make control-down`
(ADR-002: rollback = don't start the overlay). No data migration either way.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane —
  unavailable here; to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry (additions justified above: the audit
      extraction, the `status` control item, the `control:` config section,
      the contract-surface tripwire, the optional extra)
- [x] New tests cover the scope; full suite green (504); ruff clean
- [x] Pre-existing tests unmodified; goldens passing
- [x] Architecture docs updated to as-built (CLI_AND_WEBUI_STRATEGY §1/§6,
      CURRENT_ARCHITECTURE, OPERATION_MANUAL, CLI_REFERENCE, README,
      agentd/README); ADR-028 entered as Proposed
- [x] Rollback note present
- [x] No model/vendor names in code: the control package names services
      (`litellm`, `qdrant`, …) as topology data and no engine, image, model
      family, or GPU vendor (tested)
