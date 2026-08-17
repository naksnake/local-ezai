# Installing local-ezai

The `local-ezai` CLI is the production interface of the autonomous
software-engineering runtime (`agentd` Python package). It is
cross-platform: **Linux**, **macOS**, and **Windows**.

## Prerequisites

| Requirement | Notes |
|---|---|
| Python ≥ 3.10 | Linux: `python3`; Windows: [python.org](https://python.org) installer or `winget install Python.Python.3.12` |
| git ≥ 2.20 | must be on PATH (`git --version`) |
| A local model endpoint | the [local-ezai stack](../README.md) (LiteLLM at `http://localhost:4000/v1`), or any OpenAI-compatible server |
| (optional) Chromium via Playwright | only for Browser QA (`local-ezai test` on repos with `browser_qa:` configured) |

## Option 1 — pipx (recommended for users)

Isolated install with the `local-ezai` and `ezai` commands on your PATH:

```bash
# Linux / macOS
pipx install "git+https://github.com/naksnake/local-ezai.git#subdirectory=agentd"

# with Browser QA support
pipx install "git+https://github.com/naksnake/local-ezai.git#subdirectory=agentd[browser]"
pipx runpip agentd install playwright && playwright install chromium
```

```powershell
# Windows (PowerShell)
py -m pip install --user pipx
py -m pipx ensurepath          # restart the terminal afterwards
pipx install "git+https://github.com/naksnake/local-ezai.git#subdirectory=agentd"
```

## Option 2 — pip in a virtual environment

```bash
# Linux / macOS
git clone https://github.com/naksnake/local-ezai.git
cd local-ezai
python3 -m venv .venv
. .venv/bin/activate
pip install './agentd[browser]'
playwright install chromium        # only needed for Browser QA
```

```powershell
# Windows (PowerShell)
git clone https://github.com/naksnake/local-ezai.git
cd local-ezai
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install .\agentd[browser]
playwright install chromium
```

## Option 3 — from source, for development (Linux)

```bash
make swe-install     # venv + editable install with dev + browser extras
make swe-browsers    # Playwright Chromium
make swe-test        # 200+ offline tests
```

## Configure the model endpoint

By default the CLI talks to the local-ezai stack's LiteLLM proxy. Point it
elsewhere with environment variables or a config file:

```bash
# environment (Linux/macOS: export, Windows: $env: or setx)
export AGENTD_LLM__BASE_URL="http://localhost:4000/v1"
export LITELLM_MASTER_KEY="sk-..."           # the stack's master key
```

```yaml
# or ~/.config/agentd.yaml + AGENTD_CONFIG=~/.config/agentd.yaml
llm:
  base_url: http://localhost:4000/v1
  api_key: sk-...
  roles:
    coder: qwen2.5-coder-1.5b    # per-role model aliases
```

Full configuration reference: [README.md](README.md#configuration).

## Verify

```bash
local-ezai --version
local-ezai .                       # open chat for the current project
local-ezai plan "add a --version flag"
local-ezai run  "Create JWT Authentication"
local-ezai sprint sprint28.md
```

Exit codes: `0` success · `1` run/validation/review failed · `2` usage or
workspace error (not a git repo, missing spec) · `3` model unreachable ·
`130` interrupted.

## Platform notes

- **Windows**: paths are handled via `pathlib` throughout; validation
  autodetection uses the running interpreter (no `python3` binary
  assumption); Browser QA app processes are managed with Windows process
  groups (`taskkill /T`). Validation/browser commands configured in a
  repo's `.agentd.yaml` run through the platform shell (`cmd.exe`), so
  repos targeting both platforms should prefer `python -m ...`-style,
  shell-neutral commands.
- **Linux/macOS**: commands run through `/bin/sh`; app processes get their
  own process group (`SIGTERM`/`SIGKILL` teardown).
- State lives under `~/.agentd/` (runs, journals, worktrees) and each
  project's `.agent/` (memory) — both are plain files you can inspect.

## Building distributable packages

```bash
python -m pip install build
python -m build agentd/            # produces agentd/dist/*.whl and *.tar.gz
pip install agentd/dist/agentd-*.whl
```
