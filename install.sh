#!/usr/bin/env bash
# install.sh — Local-EZAI first run, steps 1–3 (V1 P5 · PR-21 · ADR-031;
# docs/FINAL_FIRST_RUN_EXPERIENCE.md §3, docs/FIRST_RUN_EXPERIENCE.md §1)
#
#   1 detect    hardware → capability class (or --profile / --class asserts one)
#   2 .env      generate it from .env.example — or REPAIR an existing .env:
#               user values kept byte for byte, missing keys added, a backup
#               first, nothing else on disk touched (F5). Then the model seeds
#               are validated: every problem printed WITH its fix, before any
#               download (F8).
#   3 secrets   the placeholder keys / tokens / passwords are minted
#
# then the one review-edit stop: a fresh .env opens once in $EDITOR (F2);
# without a terminal the script says what to edit and exits 3; --yes accepts
# the file as generated (F7). Steps 4–8 (fetch · render · up · verify · report)
# follow with `make bootstrap` and `make up-*` (PR-22 folds them into
# `make setup`); this script starts nothing and downloads nothing.
#
#   ./install.sh [--yes] [--check] [--profile gpu|cpu|n97|n97-igpu | --class <class>]
#                [--runtime <id>] [--no-editor] [--json]
#
# Exit codes: 0 ready · 1 problems printed (fix .env, re-run) · 2 usage / preflight
#             · 3 review stop (edit .env once, re-run)
#
# EZAI_PYTHON=<interpreter with agentd importable> skips the venv step (tests, CI).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
say()  { echo -e "${CYAN}[install]${NC} $*" >&2; }
warn() { echo -e "${YELLOW}[install]${NC} $*" >&2; }
fail() { echo -e "${RED}[install]${NC} $*" >&2; exit 2; }

CHECK=0; JSON=0
for arg in "$@"; do
    case "$arg" in
        -h|--help)
            sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        --check) CHECK=1 ;;
        --json)  JSON=1 ;;
    esac
done

# ── 0 preflight ──────────────────────────────────────────────────────────────
[[ -f .env.example && -d config/providers ]] \
    || fail "run this script from the local-ezai checkout (needs .env.example and config/providers/)"

PYTHON="${EZAI_PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
    if [[ -x .venv-agentd/bin/python ]]; then
        PYTHON=.venv-agentd/bin/python
    else
        command -v python3 >/dev/null 2>&1 \
            || fail "python3 (3.10+) is required — Ubuntu: sudo apt-get install -y python3 python3-venv"
        python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
            || fail "python3 >= 3.10 is required (found: $(python3 --version 2>&1))"
        say "installing the agentd runtime into .venv-agentd (once; the CLI, bootstrap and control plane live there)"
        if command -v make >/dev/null 2>&1; then
            make --no-print-directory swe-install >/dev/null \
                || fail "could not install the agentd runtime — see: make swe-install"
        else
            python3 -m venv .venv-agentd \
                && .venv-agentd/bin/pip install -q --upgrade pip \
                && .venv-agentd/bin/pip install -q -e './agentd[dev,browser,control]' \
                || fail "could not install the agentd runtime (python3 -m venv .venv-agentd && .venv-agentd/bin/pip install -e './agentd[dev,browser,control]')"
        fi
        PYTHON=.venv-agentd/bin/python
    fi
fi

DOCKER_OK=0
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    DOCKER_OK=1
else
    warn "Docker with the compose plugin is not available yet — it is needed from 'make bootstrap' on, not by this script. Ubuntu: bash scripts/setup.sh"
fi

# ── 1–3 detect · .env (generate / repair / validate) · secrets ───────────────
code=0
"$PYTHON" -m agentd.installer --root "$ROOT" "$@" || code=$?

# ── ports: the existing relocation of occupied host ports (make up* runs it too)
if [[ $code -eq 0 && $CHECK -eq 0 && $JSON -eq 0 && $DOCKER_OK -eq 1 && -f .env ]]; then
    bash scripts/check-ports.sh || warn "port check skipped: scripts/check-ports.sh failed"
fi

exit $code
