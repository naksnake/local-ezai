# PR-11 — CLI connected mode (control plane, phase P2)

**Phase:** P2 · **ADR:** ADR-028 (Proposed; PR-11 slice recorded) · **Size:** M ·
**Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-11 ·
**Depends on:** PR-9 (operations + error vocabulary + endpoints), PR-8
(liveness, token, identity headers), PR-6 verbs ·
**Status:** implemented on `claude/next-ready-pr-bnq7r3` as the ninth
stacked commit (PR-3..10 unmerged; each commit reverts independently)

## Scope (as delivered)

- **Transport auto-detect** (`platform_cli.select_transport`): for the
  management verbs (`model`, `governance`, `project`, `status`) the CLI
  probes the control plane's liveness once (`GET /health`, 1 s budget) at
  `control.url` / `EZAI_CONTROL_URL` (default `http://localhost:<port>`).
  Answer → **connected mode**: the verb runs through the API with the
  service token, the forwarded identity (`X-EZAI-User` = the CLI's actor,
  `X-EZAI-Client: cli`) and a fresh `Idempotency-Key` per mutation — the
  audit trail, the queue and the idempotency store are shared with every
  other surface. No answer → **direct mode**: in-process, as P1 built it
  (offline-first). `--transport auto|connected|direct` (then
  `$EZAI_TRANSPORT`) overrides.
- **Fail fast, never a silent split:** `--transport connected` with no
  daemon → exit 2 "control plane unreachable at URL — start it (make
  control-up), point EZAI_CONTROL_URL at it, or use --transport direct"; a
  reachable daemon with no configured token → exit 2 naming
  `EZAI_CONTROL_TOKEN` (bypassing the daemon silently would fork the audit).
- **Identical UX and outputs.** `agentd/control/client.py::ConnectedOps`
  mirrors the direct operations method for method (the PR-9 verb ↔
  endpoint mapping) and returns the response body — the operation's own
  JSON. The verb formatters now call `ctx.ops.<operation>()` on either a
  `PlatformContext` (`DirectOps`, in-process) or a `ConnectedContext`
  (`ConnectedOps`); text output is byte-identical, `--json` output is
  identical, and daemon errors arrive as the shared envelope
  (`ControlPlaneError`) printed exactly like direct-mode errors, with the
  same exit codes (`503`/`platform_unavailable` → 2, other refusals → 1).
  `status` gains one `transport:` line in both modes (the only visible
  difference) and, in connected mode, takes the stack health from the
  daemon's aggregated report.
- **Host-only verbs stay direct:** `bootstrap`, `up`, `down` act on this
  host's compose files and `.env` (CLI_AND_WEBUI §4 asymmetry 2) and never
  contact a daemon.
- **Parity smoke** (`tests/integration/test_cli_connected.py`): seven verbs
  run in both transports against one platform must produce the same JSON
  and the same text (transport line excepted); mutations through the daemon
  land in the same registry/queue/audit that direct mode reads; plus one
  **real-socket** run through uvicorn on a loopback port.
- **Config:** `control.url` (seeded by `EZAI_CONTROL_URL`);
  `.env.example` documents `EZAI_CONTROL_URL` / `EZAI_TRANSPORT`.
- **Excluded (later PRs):** spec freeze + kill-the-daemon +
  two-concurrent-runs tests (PR-12); the full three-surface parity harness
  as a release gate (PR-24); connected mode for repo-work verbs (they stay
  in-process by the offline-first rule); connection retries, streaming
  progress, new endpoints.

## Design decisions made in this PR

1. **Direct mode keeps P1's in-process management.** CLI_AND_WEBUI §2 says
   management verbs "fail fast with control plane unreachable" in direct
   mode; as built, that fail-fast applies when connected mode is
   *requested* (or a daemon answers but no token is configured) and when
   no platform exists at all (PlatformError, exit 2). With a platform on
   disk and no daemon, the verbs work in-process — P1's exit criteria,
   `make bootstrap`, and every existing test depend on it, and both modes
   write the same declarative store, so nothing forks.
2. **One operations object, two implementations.** The PR-9 refactor made
   the verbs formatters over dict-returning operations; connected mode
   swaps the source (`ctx.ops`) and nothing else — the parity test is the
   proof, byte for byte.
3. **The client uses `httpx` only.** A CLI on another machine needs no
   web framework; the daemon side keeps FastAPI.
4. **Probe once, bounded.** One liveness request with a 1 s budget per
   invocation; an unreachable daemon costs at most that second and falls
   back to direct in `auto`.
5. **`status` shows its transport.** The one deliberate output difference:
   an operator must be able to see which surface answered.

## Tests (17 new; suite 537 → 554)

- `tests/integration/test_cli_connected.py`: auto-detect uses the daemon
  (probe then verb, `transport: connected <url>`, health from the daemon);
  **parity** — seven verbs (`status`, `model explain|history|catalog`,
  `catalog --group`, `governance list`, `project list`) identical in JSON
  and text across transports; connected mutations (activate → approve)
  through the daemon with `requested_by`/decision as the forwarded identity,
  visible to direct-mode `model history`, audited as `api.*`, every request
  carrying token + identity, every mutation a distinct UUID key; connected
  errors equal the direct-mode object (404, 409, unknown request); requested
  `connected` unreachable → exit 2 with the fix (auto falls back to direct);
  reachable daemon without token → exit 2; `--transport` beats
  `$EZAI_TRANSPORT`, unknown value → exit 2; host-only verbs (`up`,
  `bootstrap`) never touch the daemon; a wrong token is reported verbatim
  and audited without the secret; `ControlPlaneError` /
  `ControlUnreachable` objects; **real socket** through uvicorn (status,
  parity of `model explain`, an activation applied as generation 2).
- All PR-6/8/9/10 CLI and control-plane tests pass unmodified; goldens
  intact.

## Behavior notes

- `local-ezai status` prints a new `transport:` line in both modes (JSON
  gains `transport`, `system_memory_gb`, `cpu_cores`; the aggregated
  `/v1/health` snapshot gains the latter two — untyped dict, contract
  unchanged at `1.0.0-draft.10`).
- Every platform verb accepts `--transport`. With a control plane running
  on the host (`make control-up`), management verbs now go through it by
  default; without one, nothing changes.
- `_apply` (dead since PR-9) removed from `platform_cli`.

## Rollback note

`git revert <commit>` removes the client, the transport selection and the
`transport:` line; direct mode is exactly what remains. No state format
changes; audit records written through the daemon stay readable.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane —
  unavailable here; to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry (additions justified above: the token
      fail-fast, the `transport:` status line, host-only routing, the
      real-socket smoke)
- [x] New tests cover the scope; full suite green (554); ruff clean
- [x] Pre-existing tests unmodified; goldens passing
- [x] Architecture docs updated to as-built (CLI_AND_WEBUI_STRATEGY §2,
      CLI_REFERENCE, OPERATION_MANUAL, `.env.example`); ADR-028 PR-11 slice
      recorded
- [x] Rollback note present
- [x] No model/vendor names in code (control package H1 grep covers
      `client.py`)
