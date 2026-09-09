# Local-EZAI — V1 Release Report (`v1.0.0`)

**Version:** agentd 1.0.0 · contract 1.2.0 · **Date:** 2026-09-09 ·
**Scope:** the V1 product of [TARGET_PRODUCT_V1.md](TARGET_PRODUCT_V1.md),
delivered as PR-1..26 of [V1_PR_PLAN.md](V1_PR_PLAN.md) under ADR-025..031 ·
**Change log:** [RELEASE_NOTES.md](RELEASE_NOTES.md)

## Verdict

**READY FOR HUMAN SIGN-OFF.** Every automated gate is green on the release
branch: the full offline suite, lint, the chat-stack baseline, the boundary
drill, the F1–F11 acceptance suite, the parity harness and the agnosticism
gates (`make release-gate`). Two steps remain that only a human on real
hardware can take, and they are listed in §7: the 72-hour soak on the two
host classes ([SOAK_RUNBOOK.md](SOAK_RUNBOOK.md)) and the release sign-off
that tags `v1.0.0`. Agents propose; humans approve
([GOVERNANCE.md](GOVERNANCE.md)).

## 1. What V1 delivered

| Phase | ADR | PRs | Delivered |
|---|---|---|---|
| P1 Registry v2 · PAL · lifecycle | ADR-027 | 1–7 | generations, capability classes + `fit()`, runtime descriptors + renderer + `engine` alias, install/validate/benchmark + catalog recommender, activate/upgrade/rollback/retire + governance queue with atomic apply, CLI namespaces + role aliases, the `.env` bootstrap |
| P2 control plane | ADR-028 | 8–12 | `ezaid` (token + identity, one audit log, one error vocabulary, idempotency), lifecycle/governance/project endpoints over shared operations, async runs, CLI connected mode, contract frozen at 1.0.0 with kill-the-daemon and concurrency tests |
| P3 OpenWebUI | ADR-029 | 13–15 | SWE tool server behind mcpo, the Orchestrator persona, the chat-ops ceiling enforced in the daemon and drilled |
| P4 Admin Center | ADR-030 | 16–20 | the console on the monitor (ten pages, approvals with evidence), identity handoff, five zero-CLI journeys in real Chromium; contract 1.1.0 |
| P5 first run | ADR-031 | 21–23 | `install.sh`, `make setup`, `init`, the Platform-ready card, the offline bundle, F1–F11 scripted; contract 1.2.0 |
| P6 gates + release | ADR-026 | 24–26 | the parity harness, the agnosticism gates (third-runtime drill, H1–H4), the soak runbook, this report, the refreshed guides |

Each PR's artifact (`docs/prs/PR-<n>-*.md`) records scope, decisions,
tests, behavior notes, verification boundary, rollback and self-review.

## 2. Final validation (release branch)

| Gate | Command | Result |
|---|---|---|
| Full offline suite | `make swe-test` | 740+ passed, 0 failed (offline: scripted LLM, real git, real Chromium for Browser QA) |
| Lint | `make swe-lint` | ruff clean |
| Chat stack byte-identical | `python3 scripts/chat-stack-baseline.py --check` | matches |
| Chat-ops boundary drill | `make swe-drill` | green (governance unreachable from chat, prompt injection, chat/RAG baseline) |
| First-run acceptance F1–F11 | `make swe-accept` | green |
| Parity harness | `make swe-parity` | green (12 matrix rows × CLI-direct · CLI-connected · API) |
| Agnosticism gates | `make swe-gates` | green (third-runtime drill, H1 word audit, H2–H4) |
| One command | `make release-gate` | all of the above |

Golden renders per legacy profile (`agentd/tests/fixtures/rendered/`) and
the ADR-020 role-map golden are unchanged since PR-3.

## 3. Definition of Done (TARGET_PRODUCT_V1 §8), item by item

| ✔ | Item | Evidence |
|---|---|---|
| ✅ (host timing in §4) | 1. Fresh machine → chatting with a governed model set in ≤ 30 minutes on the golden path, having edited only `.env` | F1 (`test_f1_fresh_accelerator_host_reaches_smoke_green`: install → setup → smoke green, the platform's own work timed), F2 (exactly one file hand-edited, once), F7 (zero prompts with a full `.env`) in `agentd/tests/acceptance/`; `install.sh` + `local-ezai setup` (PR-21/22); the wall-clock number is the soak's Day-0 measurement (SOAK_RUNBOOK §2) |
| ✅ | 2. Every operation in the parity matrix works from both CLI and WebUI with identical results | the parity harness (PR-24): every CLI_AND_WEBUI §3 row on three identical worlds — equal bodies, state and operation audit; the WebUI side: the Admin Center journeys j0–j5 in real Chromium (PR-20/23) and the SWE tool server tests (PR-13) |
| ✅ | 3. Install, benchmark, activate for a role, upgrade, roll back — no file edit, full audit trail, human approval on activation | PR-4/5/6 lifecycle + governance; parity rows "Model install / benchmark / retire", "activate / upgrade (approval flow)", "Rollback"; the Admin Center Models/Governance pages (PR-17/18); `config/governance/log.jsonl` as the single audit log (PR-8) |
| ✅ | 4. An SWE task started from an OpenWebUI conversation, its branch/report inspected from the same conversation | the SWE tool server (`plan`, `run`, `status`, `report`, `journal`; PR-13), the Orchestrator persona (PR-14), the boundary drill through the real MCP protocol (PR-15) |
| ✅ | 5. An evolution PR reviewed and (dis)approved from the Admin Center | `/governance/<id>` approve/reject with evidence (PR-18), `/evolution` cycles with proposal, benchmarks and bundle (PR-19), journeys j3/j4 (PR-20) |
| ✅ | 6. All 311+ existing tests keep passing; the SWE runtime works fully offline (no control plane for repo work) | 740+ tests green; every pre-existing test unmodified except the cases each PR artifact lists under "Behavior notes"; kill-the-daemon test (PR-12) and the parity harness's repo-work rows (`run`, `plan`, `memory` never probe a daemon) |

Product invariants (TARGET_PRODUCT_V1 §6): chat stack byte-identical (the
baseline check), `vllm` service name kept with the `engine` alias (PR-3
tripwire), MCP as the only tool protocol (PR-13), fail-closed action with
human approval on activation/push/merge (PR-5/15, the governance queue),
sandboxed execution behind the reviewer gate (ADR-021/022, unchanged),
every state change journaled and every user-edited config a rendered
artifact (PR-3/5, drift detection).

## 4. P6 exit criteria and the soak

| # | Criterion | Status |
|---|---|---|
| 1 | Parity harness green across the matrix | ✅ PR-24 (`make swe-parity`) |
| 2 | Soak: zero unexplained failures, rollback exercised under load | ⏳ **pending the hardware run** — runbook and driver delivered (SOAK_RUNBOOK.md, `make soak`); results to be pasted below |
| 3 | Product DoD checked item by item | ✅ §3 |
| 4 | Human release sign-off → tag `v1.0.0` | ⏳ §7 |

### Soak results (filled in by the release manager)

| Host | Class / profile / runtime | F1 clone → chatting | Steps ok / failed | Rollbacks under load | Failures explained | Memory bounded | Verdict |
|---|---|---|---|---|---|---|---|
| A (accelerator) | ______ | ____ min | ____ / ____ | ____ / 12 | ____ / ____ | yes / no | PASS / FAIL |
| B (low-power) | ______ | ____ min | ____ / ____ | ____ / 12 | ____ / ____ | yes / no | PASS / FAIL |

Attach `config/soak/<stamp>/results.md` per host (SOAK_RUNBOOK §5).

## 5. Known limitations (carried into the next train)

- The control plane's health table probes the engine slot at `/health`,
  not the active descriptor's readiness path (PR-25 residual; both shipped
  runtimes answer `/health`).
- A day-2 `model install` of an undeclared user source has no tool-call
  format, so tool-calling roles refuse it at render time (PR-25 residual;
  declare it through a catalog entry).
- One active engine slot per host; no runtime switch verb (a switch is an
  approval-gated activation; the Runtime page pre-checks it).
- The 30-minute first-run target and the soak are host measurements; the
  suite measures the platform's own work with the downloads excluded.

## 6. Guides refreshed for V1

[USER_GUIDE.md](USER_GUIDE.md) (the five-step first run, the Orchestrator,
the Admin Center, models and approvals as a user) ·
[OPERATION_MANUAL.md](OPERATION_MANUAL.md) (the gates, the soak, the counts) ·
[CLI_REFERENCE.md](CLI_REFERENCE.md) (contract 1.1.0/1.2.0 rows, the resolver
rule) · [TROUBLESHOOTING.md](TROUBLESHOOTING.md) (first run, control plane,
Admin Center, governance, descriptors) ·
[MAINTENANCE_GUIDE.md](MAINTENANCE_GUIDE.md) (the V1 layout, generation and
rollback operations, the release procedure) · [RELEASE_NOTES.md](RELEASE_NOTES.md).

## 7. Release checklist and sign-off (human)

1. `make release-gate` on the release branch — green.
2. The soak on both hosts per [SOAK_RUNBOOK.md](SOAK_RUNBOOK.md); §4 filled
   in; every failure explained or fixed and re-soaked.
3. Review this report and [RELEASE_NOTES.md](RELEASE_NOTES.md); the version
   is `1.0.0` in `agentd/src/agentd/__init__.py` and `agentd/pyproject.toml`.
4. Sign below; merge the release PR (human merge authority, V1_PR_PLAN §1).
5. Tag after merge: `git tag v1.0.0 && git push --tags`; build the wheel
   (`python -m build agentd/`).

| Role | Decision | Name | Date |
|---|---|---|---|
| Chief Architect — architecture additive, invariant-preserving, ADR-complete (ADR-025..031 Accepted) | approve / hold | ________ | ______ |
| Principal Engineer — gates green, pre-existing tests preserved, residuals recorded | approve / hold | ________ | ______ |
| Release Manager — soak PASS on both hosts, guides refreshed, rollback = revert the release PR | approve / hold | ________ | ______ |
| Product Owner — the six DoD items met, the five-step contract holds | approve / hold | ________ | ______ |

**The tag `v1.0.0` is applied only after every row reads "approve".**
