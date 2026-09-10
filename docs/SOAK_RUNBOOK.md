# 72-hour Soak Runbook (V1 release gate, P6 exit criterion 2)

**Purpose:** prove on real hardware what the offline suite cannot — that the
platform runs for three days under a realistic mix of chat serving, SWE
runs, lifecycle churn and evolution cycles with **zero unexplained
failures**, and that **rollback works under load**
([V1_IMPLEMENTATION_PLAN.md](V1_IMPLEMENTATION_PLAN.md) §P6). Results go into
[V1_RELEASE_REPORT.md](V1_RELEASE_REPORT.md) §4 before the human sign-off.

The driver is `scripts/soak.sh` (`make soak`); this runbook is what a human
does around it.

## 1. Hosts

Run the soak on **two hosts**, one per capability family
([HARDWARE_AGNOSTIC_ARCHITECTURE.md](HARDWARE_AGNOSTIC_ARCHITECTURE.md) §1),
matching the profiles TARGET_PRODUCT_V1 §5 commits to:

| Host | Class | Profile | Runtime the installer proposes |
|---|---|---|---|
| A — accelerator workstation | `accel-large` (or `accel-small`) | `gpu` | the HF runtime |
| B — low-power mini-PC | `cpu-low` | `n97` | the GGUF runtime |

Both hosts start from a **fresh clone**. Do not reuse a bootstrapped
checkout: the soak's first hour doubles as the F1 wall-clock measurement
(DoD item 1, "≤ 30 minutes, having edited only `.env`").

## 2. Day 0 — first run, timed

```bash
git clone <repo> local-ezai && cd local-ezai
date -u +%FT%TZ                          # T0 — write it down
make setup                               # edit .env once when asked (AI_RUNTIME + the three seeds)
date -u +%FT%TZ                          # T1 — the F1 number is T1 − T0
open http://<host>:3000                  # first account = admin; the "Platform ready" card shows
make orchestrator                        # the Orchestrator persona (after the first login)
make control-up                          # the control plane, so lifecycle churn is audited through it
```

Record T1 − T0 per host in the results template (§6). Anything you edited
other than `.env` is a finding.

## 3. Start the soak

```bash
make soak HOURS=72                       # foreground; run it under tmux/screen
# rehearsal on a dev box:  make soak HOURS=0.5 SOAK_ARGS="--tick 60"
# see the schedule only:   make soak SOAK_ARGS="--dry-run"
```

The driver writes `config/soak/<stamp>/log.jsonl` (one JSON line per step:
`ts`, `tick`, `action`, `ok`, `seconds`, the output tail) and, at the end,
`config/soak/<stamp>/results.md`. It never aborts on a failed step — a
failure is a data point to explain.

| Every | Action | What it exercises |
|---|---|---|
| 15 min | `health`, `status` | the eight services, engine/router/control, generation vs rendered |
| 1 h | `bench` | tokens/s on the live engine (drift over the window) |
| 2 h | `run` | a scripted SWE task on the bundled sample project (plan → code → validate → review → commit on `swe/<id>`) |
| 6 h | `churn` | `model benchmark <chat primary>` (a side-loaded second engine + a new generation) then `model rollback` — **rollback under load**, through the control plane when it answers |
| 24 h | `evolve` | an evolution cycle on the sample project (ends at a proposal bundle, never a merge) |
| 24 h | `stats` | `docker stats --no-stream`: memory and CPU per container |

Meanwhile, **use the platform** as a user would: chat in OpenWebUI, ask the
Orchestrator to plan and run a task on the sample project, open the Admin
Center pages, approve or reject one activation from `/governance`. Note
what you did and when in the template — human actions during the soak are
part of the load.

## 4. Pass criteria

1. **Zero unexplained failures.** Every `ok: false` line in `log.jsonl` has
   an explanation in the results template: an expected refusal (a second
   `evolve` while one is open, a `run` whose advisory check failed on the
   model's answer), an environmental cause (host suspended, disk full), or
   a defect that was fixed and re-soaked. "Flaky" is not an explanation.
2. **Rollback exercised under load:** every `churn` step's rollback
   succeeded (`results.md` counts them; 12 over 72 h) while chat kept
   answering — confirm with the `status` samples on either side of each
   churn tick (engine up, generation advanced by 2).
3. **No engine restart outside the churn steps** (`docker ps` uptime, the
   `status` samples' generation, `make logs-vllm`).
4. **Memory bounded:** the `stats` samples at 0 / 24 / 48 / 72 h show no
   monotonic growth in any container beyond the model's working set.
5. **Chat unchanged:** `python3 scripts/chat-stack-baseline.py --check`
   still matches at the end.

## 5. Collect

```bash
ls config/soak/<stamp>/                  # log.jsonl · results.md
make health | tee config/soak/<stamp>/health-final.txt
local-ezai model history --json > config/soak/<stamp>/generations.json
local-ezai governance list --json > config/soak/<stamp>/governance.json
python3 scripts/chat-stack-baseline.py --check | tee config/soak/<stamp>/baseline.txt
```

Attach the directory (or its `results.md`) to the release report; the
`config/soak/` directory is ignored by git — copy what the report needs.

## 6. Results template (one per host)

```
Host: ______________________  class: __________  profile: ______  runtime: __________
Clone → chatting (F1): T0 ________ T1 ________  = ____ min   files edited other than .env: none / ______
Soak window: ________ → ________  (72 h)         driver: config/soak/<stamp>/results.md
Steps: health __/__ ok · status __/__ · bench __/__ · run __/__ · churn __/__ · evolve __/__ · stats __/__
Rollbacks under load: ____ (expected 12)          engine restarts outside churn: ____
Failures to explain: ____   → each: tick, action, cause, fix/decision
Memory 0h/24h/48h/72h (engine · router · openwebui · qdrant): ______________________________
Human actions during the soak (what, when, outcome): __________________________________
Baseline --check at the end: matches / differs (why): ______
Verdict for this host: PASS / FAIL — signed: ______________  date: ________
```

## 7. Where it feeds

- `V1_RELEASE_REPORT.md` §4 (the two host rows) and §7 (sign-off).
- A failure that is a defect: fix it on a branch, run `make release-gate`,
  and re-soak the affected schedule (`make soak HOURS=24` is acceptable for
  a re-soak of a fix that touches only one action).
