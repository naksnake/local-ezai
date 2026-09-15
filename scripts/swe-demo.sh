#!/usr/bin/env bash
# scripts/swe-demo.sh — the Autonomous SWE pipeline, offline, in about a minute.
#
# No GPU, no model download, no running stack: a SCRIPTED model (agentd's
# `scripted` provider) plays the Planner, the Coder and the Reviewer with the
# canned answers in examples/swe-demo/responses.json. Everything else is the
# real platform — the git worktree and branch, the file edits, the project's
# own unittest suite as the validation gate, the review gate, the commit, the
# journal and the report, the project memory.
#
#   1/5  copy examples/sample-project into a scratch git repository
#   2/5  local-ezai <repo> plan "add a GET /health endpoint …"   (dry run — nothing written)
#   3/5  local-ezai <repo> run  "…the same task…"                (plan → code → validate → review → commit)
#   4/5  show the delivered branch, its diff, the project's tests on it
#   5/5  show the journal and `explain-run` (which model served which stage)
#
# Usage: bash scripts/swe-demo.sh [--dir DIR] [--keep]
#        make swe-demo [DEMO_DIR=/path]
#   --dir DIR   where the demo lives (default: a fresh directory under $TMPDIR); implies --keep
#   --keep      leave the demo directory in place (a failed run always keeps it)
# The CLI: $EZAI_CLI, else .venv-agentd/bin/local-ezai, else `local-ezai` on PATH (make swe-install).
set -euo pipefail

DIR=""
KEEP=0
TASK='add a GET /health endpoint that returns {"status": "ok"} and a test for it'

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir) DIR="$2"; KEEP=1; shift 2 ;;
        --keep) KEEP=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "swe-demo: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
    esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RESPONSES="$ROOT/examples/swe-demo/responses.json"
SAMPLE="$ROOT/examples/sample-project"

say() { printf '\n==> %s\n' "$*"; }
show() { printf '  $ %s\n' "$*"; "$@"; }

# ── preflight: python3, git, the CLI ─────────────────────────────────────────
command -v python3 >/dev/null || { echo "swe-demo: python3 is required" >&2; exit 2; }
command -v git >/dev/null || { echo "swe-demo: git is required" >&2; exit 2; }
if [[ -n "${EZAI_CLI:-}" ]]; then
    CLI="$EZAI_CLI"
elif [[ -x "$ROOT/.venv-agentd/bin/local-ezai" ]]; then
    CLI="$ROOT/.venv-agentd/bin/local-ezai"
elif command -v local-ezai >/dev/null; then
    CLI="$(command -v local-ezai)"
else
    echo "swe-demo: local-ezai not found — run 'make swe-install' first (or set EZAI_CLI)" >&2
    exit 2
fi
[[ -f "$RESPONSES" ]] || { echo "swe-demo: $RESPONSES is missing" >&2; exit 2; }
[[ -d "$SAMPLE" ]] || { echo "swe-demo: $SAMPLE is missing" >&2; exit 2; }

DIR="${DIR:-$(mktemp -d "${TMPDIR:-/tmp}/ezai-swe-demo.XXXXXX")}"
PROJECT="$DIR/project"
STATE="$DIR/state"
CONFIG="$STATE/config.yaml"
if [[ -e "$PROJECT" ]]; then
    echo "swe-demo: $PROJECT already exists — pick another --dir or remove it" >&2
    exit 2
fi
mkdir -p "$STATE"

echo "swe-demo: the Autonomous SWE pipeline, offline — scripted model, real everything else"
echo "  platform: $ROOT"
echo "  cli:      $CLI"
echo "  demo:     $DIR"

# ── 1/5 the project ──────────────────────────────────────────────────────────
say "1/5  A scratch copy of examples/sample-project becomes a git repository (branch main)"
cp -R "$SAMPLE/." "$PROJECT"
find "$PROJECT" -name __pycache__ -type d -prune -exec rm -rf {} +
git -C "$PROJECT" init -q
git -C "$PROJECT" symbolic-ref HEAD refs/heads/main
git -C "$PROJECT" add -A
git -C "$PROJECT" -c user.name="Local-EZAI demo" -c user.email="demo@local-ezai" \
    commit -qm "sample project, before the autonomous run"
echo "  $(git -C "$PROJECT" rev-parse --short HEAD) on main — a tiny HTTP service with tests but no /health endpoint"
echo "  its check, from .agentd.yaml: python3 -m unittest discover -s tests -q →" \
     "$(cd "$PROJECT" && python3 -m unittest discover -s tests -q 2>&1 | tail -1)"

cat > "$CONFIG" <<YAML
# swe-demo: a scripted model plays planner, coder and reviewer — everything else is real.
llm:
  provider: scripted
  script_path: "$RESPONSES"
workspace:
  root: "$STATE/workspaces"   # the run's git worktree lands here, not under ~/.agentd
runs_dir: "$STATE/runs"       # journal.jsonl + report.json per run
YAML
echo "  config: $CONFIG (llm.provider: scripted — the only difference from a real run)"

# ── 2/5 plan ─────────────────────────────────────────────────────────────────
say "2/5  Plan only: the Planner reads the repository and proposes tasks — nothing is written"
show "$CLI" "$PROJECT" plan "$TASK" --config "$CONFIG"

# ── 3/5 run ──────────────────────────────────────────────────────────────────
say "3/5  The full pipeline: plan → code → validate → review → commit, on a swe/<run-id> branch"
RUN_EXIT=0
show "$CLI" "$PROJECT" run "$TASK" --config "$CONFIG" || RUN_EXIT=$?

RUN_DIR="$(ls -td "$STATE"/runs/*/ 2>/dev/null | head -1 || true)"
RUN_DIR="${RUN_DIR%/}"
REPORT="$RUN_DIR/report.json"
if [[ -z "$RUN_DIR" || ! -f "$REPORT" ]]; then
    echo "swe-demo: no report was written (exit $RUN_EXIT) — see $STATE/runs; the demo directory is kept at $DIR" >&2
    exit 1
fi
read -r STATUS BRANCH WORKTREE < <(python3 - "$REPORT" <<'PY'
import json, sys
report = json.load(open(sys.argv[1], encoding="utf-8"))
print(report["status"], report.get("branch") or "-", report.get("workspace_path") or "-")
PY
)
if [[ "$STATUS" != "completed" ]]; then
    echo >&2
    echo "swe-demo: the run ended '$STATUS' (exit $RUN_EXIT)" >&2
    echo "  report:  $REPORT" >&2
    echo "  journal: $RUN_DIR/journal.jsonl" >&2
    echo "  the demo directory is kept at $DIR" >&2
    exit 1
fi

# ── 4/5 the delivery ─────────────────────────────────────────────────────────
say "4/5  What was delivered: a reviewed commit on $BRANCH — main is untouched"
show git -C "$PROJECT" --no-pager log --oneline "main..$BRANCH"
show git -C "$PROJECT" --no-pager diff --stat "main...$BRANCH"
echo
echo "  the new route in app.py on the branch:"
git -C "$PROJECT" --no-pager show "$BRANCH:app.py" | grep -n '"/health"' | sed 's/^/    /'
echo
echo "  the project's own check on the branch (in the worktree the run used):"
(cd "$WORKTREE" && python3 -m unittest discover -s tests -v 2>&1 | tail -4 | sed 's/^/    /')

# ── 5/5 the evidence ─────────────────────────────────────────────────────────
say "5/5  The evidence: every step is journaled; explain-run names the model behind each stage"
python3 - "$RUN_DIR" <<'PY'
import json, pathlib, sys
events = [json.loads(line) for line in (pathlib.Path(sys.argv[1]) / "journal.jsonl")
          .read_text(encoding="utf-8").splitlines() if line.strip()]
kinds: list[str] = []
for event in events:
    if not kinds or kinds[-1] != event["type"]:
        kinds.append(event["type"])
print(f"  {len(events)} journal events: " + " → ".join(kinds[:14]) + (" …" if len(kinds) > 14 else ""))
PY
show "$CLI" "$PROJECT" explain-run --config "$CONFIG"

# ── wrap-up ──────────────────────────────────────────────────────────────────
say "Done. What was real, what was scripted"
cat <<TXT
  real:      the worktree and the $BRANCH branch, the edits, the project's unittest suite as
             the validation gate, the review gate before the commit, the commit itself, the
             journal and report, the project memory written to $PROJECT/.agent/
  scripted:  only the model's answers — examples/swe-demo/responses.json played the planner,
             the coder and the reviewer. Point the same pipeline at a real model and your own
             repository: docs/SWE_DEMO.md §3.
TXT
if [[ "$KEEP" -eq 1 ]]; then
    cat <<TXT
  kept:      $DIR
             report   $REPORT
             journal  $RUN_DIR/journal.jsonl
             branch   git -C $PROJECT log -p main..$BRANCH
TXT
else
    rm -rf "$DIR"
    echo "  (scratch directory removed — pass --keep or --dir to inspect the report, journal and branch)"
fi
