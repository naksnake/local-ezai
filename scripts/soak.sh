#!/usr/bin/env bash
# scripts/soak.sh — the 72 h soak driver (docs/SOAK_RUNBOOK.md; V1 P6 exit
# criterion 2: "zero unexplained failures, rollback exercised under load").
#
# Runs a fixed schedule against THIS host's platform and records every step
# as one JSON line, then writes a summary the release report quotes:
#
#   every tick (15 min)   health   make health              stack health
#                         status   local-ezai status --json  generation · rendered · engine/router/control
#   hourly                bench    scripts/bench.sh          tokens/s on the live engine
#   every 2 h             run      local-ezai <project> run  a scripted SWE task on the sample project
#   every 6 h             churn    model benchmark <chat primary> → model rollback   (rollback under load)
#   every 24 h            evolve   local-ezai <project> evolve                       (ends at a proposal)
#                         stats    docker stats --no-stream                          (memory / CPU trend)
#
# Usage: bash scripts/soak.sh [--hours N] [--tick SECONDS] [--out DIR] [--project PATH] [--dry-run]
#        make soak HOURS=72 [SOAK_ARGS="--dry-run"]
#   --dry-run  prints the schedule for the window and exits — runs nothing, needs no stack.
#   --tick 60 --hours 0.5  rehearses the whole schedule in 30 minutes on a dev box.
set -euo pipefail

HOURS=72
TICK=900
OUT=""
PROJECT=""
DRY=0
TASK="add a GET /health endpoint that returns {\"status\": \"ok\"} and a test for it"

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --hours) HOURS="$2"; shift 2 ;;
        --tick) TICK="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        --project) PROJECT="$2"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "soak: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
    esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLI="${EZAI_CLI:-$ROOT/.venv-agentd/bin/local-ezai}"
PROJECT="${PROJECT:-$ROOT/sample-project}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${OUT:-$ROOT/config/soak/$STAMP}"
LOG="$OUT/log.jsonl"

# The schedule, in ticks (15 min at the default tick).
EVERY_BENCH=4      # 1 h
EVERY_RUN=8        # 2 h
EVERY_CHURN=24     # 6 h
EVERY_EVOLVE=96    # 24 h
EVERY_STATS=96     # 24 h

TOTAL_TICKS=$(python3 -c "import math,sys; print(max(1, math.ceil(float(sys.argv[1]) * 3600 / float(sys.argv[2]))))" "$HOURS" "$TICK")

due() { # due <every> <tick>: does an action run at this tick? (tick 0 = the baseline sample)
    [[ $(( $2 % $1 )) -eq 0 ]]
}

actions_at() { # actions_at <tick> → space-separated action names
    local t="$1" list="health status"
    due "$EVERY_BENCH" "$t" && list="$list bench"
    due "$EVERY_RUN" "$t" && list="$list run"
    due "$EVERY_CHURN" "$t" && list="$list churn"
    due "$EVERY_EVOLVE" "$t" && list="$list evolve"
    due "$EVERY_STATS" "$t" && list="$list stats"
    echo "$list"
}

if [[ "$DRY" -eq 1 ]]; then
    echo "soak schedule — ${HOURS} h window, tick ${TICK}s → ${TOTAL_TICKS} ticks (dry run: nothing executed)"
    echo "  platform: $ROOT"
    echo "  project:  $PROJECT"
    echo "  results:  $OUT/{log.jsonl,results.md}"
    echo
    for t in $(seq 0 $(( TOTAL_TICKS < 8 ? TOTAL_TICKS - 1 : 7 ))); do
        printf '  tick %3d  +%6.1f h  %s\n' "$t" "$(python3 -c "print($t * $TICK / 3600)")" "$(actions_at "$t")"
    done
    [[ "$TOTAL_TICKS" -gt 8 ]] && echo "  …"
    echo
    for name in health status bench run churn evolve stats; do
        count=0
        for t in $(seq 0 $(( TOTAL_TICKS - 1 ))); do
            case " $(actions_at "$t") " in *" $name "*) count=$((count + 1)) ;; esac
        done
        printf '  %-7s × %d\n' "$name" "$count"
    done
    exit 0
fi

command -v python3 >/dev/null || { echo "soak: python3 is required" >&2; exit 2; }
[[ -x "$CLI" ]] || { echo "soak: local-ezai not found at $CLI — run make swe-install (or set EZAI_CLI)" >&2; exit 2; }
[[ -d "$PROJECT/.git" ]] || { echo "soak: $PROJECT is not a git repository — run make setup first (it registers the sample project)" >&2; exit 2; }
mkdir -p "$OUT"
: > "$LOG"
TICK_NO=0

json_string() { python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$1"; }

log() { # log <action> <ok> <seconds> <detail>
    printf '{"ts":"%s","tick":%d,"action":"%s","ok":%s,"seconds":%s,"detail":%s}\n' \
        "$(date -u +%FT%TZ)" "$TICK_NO" "$1" "$2" "$3" "$(json_string "$4")" >> "$LOG"
}

step() { # step <action> <command…>: run, time, record (never abort the soak)
    local name="$1"; shift
    local start end ok detail
    start=$(date +%s)
    if detail=$("$@" 2>&1); then ok=true; else ok=false; fi
    end=$(date +%s)
    log "$name" "$ok" "$((end - start))" "${detail:0:2000}"
    echo "$(date -u +%FT%TZ) tick $TICK_NO $name: $([[ $ok == true ]] && echo ok || echo FAILED) ($((end - start))s)"
}

chat_primary() { "$CLI" model explain chat --json 2>/dev/null | python3 -c 'import json, sys; print(json.load(sys.stdin)["primary"])'; }

do_health() { ( cd "$ROOT" && make -s health ); }
do_status() { "$CLI" status --json; }
do_bench() { ( cd "$ROOT" && bash scripts/bench.sh ); }
do_run() { "$CLI" "$PROJECT" run "$TASK" --json; }
do_churn() { # rollback under load: a new generation (benchmark), then rollback — no approval needed
    local primary
    primary="$(chat_primary)" || return 1
    "$CLI" model benchmark "$primary" --json && "$CLI" model rollback --reason "soak churn tick $TICK_NO" --json
}
do_evolve() { "$CLI" "$PROJECT" evolve --json; }
do_stats() { docker stats --no-stream --format '{{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}}'; }

echo "soak: ${HOURS} h, ${TOTAL_TICKS} ticks of ${TICK}s → $OUT"
for TICK_NO in $(seq 0 $(( TOTAL_TICKS - 1 ))); do
    for action in $(actions_at "$TICK_NO"); do
        step "$action" "do_$action"
    done
    [[ "$TICK_NO" -lt $(( TOTAL_TICKS - 1 )) ]] && sleep "$TICK"
done

# ── summary → results.md (the release report quotes this) ───────────────────
LOG="$LOG" OUT="$OUT" HOURS="$HOURS" TICK="$TICK" python3 - <<'PY'
import collections, json, os

log = [json.loads(line) for line in open(os.environ["LOG"], encoding="utf-8") if line.strip()]
by_action = collections.defaultdict(lambda: {"ok": 0, "failed": 0, "seconds": []})
for record in log:
    bucket = by_action[record["action"]]
    bucket["ok" if record["ok"] else "failed"] += 1
    bucket["seconds"].append(record["seconds"])
failures = [r for r in log if not r["ok"]]
rollbacks = sum(1 for r in log if r["action"] == "churn" and r["ok"])
lines = [
    "# Soak results", "",
    f"window: {os.environ['HOURS']} h · tick {os.environ['TICK']} s · {len(log)} steps · "
    f"started {log[0]['ts'] if log else '-'} · ended {log[-1]['ts'] if log else '-'}", "",
    "| action | ok | failed | max s | mean s |", "|---|---|---|---|---|",
]
for action, bucket in sorted(by_action.items()):
    secs = bucket["seconds"]
    lines.append(f"| {action} | {bucket['ok']} | {bucket['failed']} | {max(secs)} | "
                 f"{sum(secs) / len(secs):.1f} |")
lines += ["", f"rollbacks exercised under load: **{rollbacks}**", "",
          f"failures to explain: **{len(failures)}**", ""]
lines += ["| ts | tick | action | detail (tail) | explained? |", "|---|---|---|---|---|"]
lines += [f"| {r['ts']} | {r['tick']} | {r['action']} | {r['detail'][-160:].replace('|', '/')} |  |"
          for r in failures]
lines += ["", "Pass criteria (docs/SOAK_RUNBOOK.md): every failure above explained or fixed, "
          "rollbacks ≥ 1 per 24 h, no engine restart outside the churn steps, memory in "
          "`stats` samples bounded."]
open(os.path.join(os.environ["OUT"], "results.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
print(f"soak: summary written to {os.environ['OUT']}/results.md ({len(failures)} failure(s) to explain)")
PY
