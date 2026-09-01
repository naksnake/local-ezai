# Model Governance Report

Repository: `local-ezai` · maintained by `local-ezai evaluate-models
--report` (this checked-in copy documents the standing routing; probe and
metric columns are populated live wherever the command runs against a
serving model plane).

Routing source: `.agent/model_registry.yaml`
([SELF_EVOLUTION_GUIDE.md](SELF_EVOLUTION_GUIDE.md) §3); raw data:
`.agent/model_benchmarks.json`.

## Routing & availability probes

| Role | Primary model | Fallbacks | Probe | Latency | JSON-validated |
|---|---|---|---|---|---|
| planner | `hermes3` | `deepseek-r1` | * | * | yes |
| coder | `qwen3-coder` | `deepseek-r1` | * | * | — |
| debugger | `deepseek-r1` | `hermes3` | * | * | yes |
| reviewer | `llama3` | — | * | * | yes |
| documentation | `llama3` | — | * | * | — |
| memory | `hermes3` | — | * | * | — |
| evolution | `deepseek-r1` | — | * | * | yes |
| sprint | (config default) | — | * | * | yes |
| chat | (config default) | — | * | * | — |

`*` populated per environment by `local-ezai evaluate-models --report` —
each probe exercises the real client, including the fallback chain, and
structured-output roles must return parseable JSON.

## Quality metrics (from run history)

Aggregated from `~/.agentd/runs/*/report.json` on every evaluation:

| Metric | Basis |
|---|---|
| Planning accuracy | plans fully executed / plans produced |
| Coding success rate | completed runs / all runs |
| Validation pass rate | green validations / runs validated |
| Debugging success rate | healed-and-delivered / runs that self-healed |
| Review approval rate | approvals / reviewer-gate runs |
| Avg heal iterations | debug→fix cycles per healing run |
| Avg execution speed | journal wall clock per run |

## Trend (previous evaluations)

`.agent/model_benchmarks.json` keeps a rolling history (last 20
evaluations: per-role ok/latency and the overall verdict). The report's
trend table shows drift — a role that regresses, a latency that doubles —
and the **evolution workflow reads these trends as first-class evidence**
before proposing improvements ([SELF_EVOLUTION_GUIDE.md](SELF_EVOLUTION_GUIDE.md)).

## Governance

- Routing changes are **model replacement** — a human-approved PR editing
  `.agent/model_registry.yaml`, evidenced by this report
  ([GOVERNANCE.md](GOVERNANCE.md)).
- Which model actually served each stage of a specific run:
  `local-ezai explain-run <run-id>`; the standing routing:
  `local-ezai models`.
- Regenerate this report anytime: `local-ezai evaluate-models --report`.
