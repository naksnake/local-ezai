# Local-EZAI — Sandbox Guide (sandboxd)

Every agent-driven shell execution — the `exec_run` tool, validation /
build / test checks, debugging reproductions, evolution benchmarks — goes
through **one executor**: the per-run sandbox (ADR-021, closing ADR-014's
interim posture). It applies three layers in every mode:

1. **Command allowlist** — refused before anything runs (when configured)
2. **Execution** — host subprocess or a disposable Docker container
3. **Audit log** — every execution, including refused ones, recorded

## 1. Modes

```yaml
sandbox:
  mode: auto          # auto | docker | host
  image: ""           # container image with the project's toolchain
```

| Mode | Behavior |
|---|---|
| `auto` (default) | **Docker whenever possible**: used when the Docker daemon answers AND an `image` is configured. Otherwise the host executor. |
| `docker` | Strict: the run fails with a clear error when the daemon or image is missing. |
| `host` | The pre-H1 behavior: subprocess in the workspace with timeouts and output caps. |

Out of the box (`image` unset) nothing changes — commands run on the host
exactly as before. The image gate is deliberate: project checks need the
project's toolchain, so the sandbox image is an operator decision, never a
guess. The resolved mode is journaled per run (`SANDBOX_MODE`).

## 2. Building a sandbox image

The image must contain the toolchain your validation commands use:

```dockerfile
# myproj-ci.Dockerfile
FROM python:3.11-slim
COPY requirements-dev.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements-dev.txt
```

```bash
docker build -t myproj-ci -f myproj-ci.Dockerfile .
```

```yaml
# agentd.yaml (global config — a repo cannot weaken its own sandbox)
sandbox:
  mode: auto
  image: myproj-ci
```

## 3. What container execution looks like

For each command the sandbox runs:

```
docker run --rm --name agentd-<id>
  -v <workspace>:<workspace> -w <workspace>   # host-identical mount path
  --network none                              # default-deny egress
  --memory 2g --cpus 2.0 --pids-limit 512     # resource limits
  -e PYTHONDONTWRITEBYTECODE=1
  <image> sh -c "<command>"
```

- **Restricted filesystem**: only the workspace (and, for git worktrees,
  the origin repo's `.git` directory) is mounted — the host filesystem is
  invisible.
- **Host-identical mount path**: error messages, configured commands, and
  git worktree pointers resolve the same inside and outside.
- **Timeouts**: the host enforces the wall clock; a wedged container is
  killed (`docker kill`).
- **Env passthrough** is explicit: `sandbox.env_passthrough: [VAR, ...]` —
  nothing else crosses the boundary.

Configurable resource limits: `memory` (e.g. `"4g"`), `cpus`, `pids_limit`,
`network` (`"none"` default; `"bridge"` for checks that genuinely need the
network).

## 4. Command allowlist

```yaml
sandbox:
  command_allowlist:
    - '^python3? -m (pytest|ruff|mypy|build)'
    - '^make (test|lint)$'
```

Empty (default) → every command is allowed and the workspace remains the
blast radius. Non-empty → any command not matching one of the regexes
(`re.search`) is refused with `command blocked by sandbox allowlist`,
audited, and surfaced to the agent as an ordinary tool error. The
allowlist applies in **all** modes, host included.

## 5. Execution audit log

Every run directory carries `~/.agentd/runs/<id>/exec_audit.jsonl`:

```json
{"ts": "...", "mode": "docker", "workspace": "...", "command": "python -m pytest -q",
 "allowed": true, "ok": false, "exit_code": 1, "duration_ms": 8123}
```

One line per execution — including refused ones (`"allowed": false`).
Disable with `sandbox.audit: false` (not recommended).

## 6. Scope and limits

- The sandbox governs **shell executions**. Git operations (status / add /
  commit / push) run through the dedicated, tier-gated git tools on the
  host — they are the delivery mechanism, not agent code execution.
  Browser QA launches the app under test on the host as well (it needs a
  local port for Playwright); its process handling is documented in
  ADR-016.
- A repo's `.agentd.yaml` **cannot** configure the sandbox — only the
  global config can (same rule as `git:` — a repo must not weaken its own
  isolation).
- Filesystem tools (`fs_*`) are path-contained to the workspace
  independently of the sandbox (`Workspace.resolve`).

Troubleshooting: [TROUBLESHOOTING.md](TROUBLESHOOTING.md); decision record:
ADR-021 in [.agent/decisions.md](../.agent/decisions.md).
