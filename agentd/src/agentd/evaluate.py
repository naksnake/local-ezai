"""Model evaluation harness — ``local-ezai evaluate-models`` (ADR-020).

Governance needs evidence that the routed models actually work. For every
routed role this harness sends a small role-appropriate probe through the
normal client (so fallback chains are exercised too), verifies the answer
shape (structured-output roles must return a parseable JSON object),
measures latency, and records everything to
``.agent/model_benchmarks.json`` in the target repository.

This is a smoke-evaluation of availability, protocol compliance, and
latency — not a quality leaderboard. Quality evolution is the Evolution
workflow's benchmark step.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from agentd.config import AgentdConfig
from agentd.llm import LLMClient, build_llm
from agentd.logging_setup import get_logger
from agentd.schemas import (
    ModelEvalReport,
    ModelProbeResult,
    RunMetrics,
    extract_json_object,
)

log = get_logger("evaluate")

#: role → (probe prompt, expects_json)
PROBES: dict[str, tuple[str, bool]] = {
    "planner": (
        'Reply with ONLY this JSON object: {"goal": "probe", "tasks": '
        '[{"id": "T1", "intent": "probe task"}]}', True),
    "coder": ("Reply with a one-line Python function that returns 42.", False),
    "debugger": (
        'Reply with ONLY this JSON object: {"root_cause": "probe", '
        '"confidence": "high"}', True),
    "reviewer": (
        'Reply with ONLY this JSON object: {"verdict": "approve", '
        '"findings": []}', True),
    "documentation": ("Reply with one sentence describing a README file.",
                      False),
    "memory": ("Reply with one short sentence naming a coding best practice.",
               False),
    "evolution": (
        'Reply with ONLY this JSON object: {"improvements": '
        '[{"id": "I1", "title": "probe"}]}', True),
    "sprint": (
        'Reply with ONLY this JSON object: {"goal": "probe", "tasks": '
        '[{"id": "T1", "title": "t", "description": "d"}]}', True),
    "chat": ("Reply with the single word: ready", False),
}

BENCHMARKS_FILENAME = "model_benchmarks.json"


def evaluate_models(
    config: AgentdConfig,
    repo: Path,
    llm: LLMClient | None = None,
) -> ModelEvalReport:
    """Probe every routed role; persist results into <repo>/.agent/."""
    from agentd.model_registry import apply_model_registry
    from agentd.runner import resolve_origin_root

    repo = Path(repo).resolve()
    config = apply_model_registry(config, resolve_origin_root(repo))
    client = llm or build_llm(config.llm)

    results: list[ModelProbeResult] = []
    for role, (prompt, expects_json) in PROBES.items():
        model = config.llm.model_for_role(role)
        fallbacks = config.llm.role_fallbacks.get(role, [])
        started = time.monotonic()
        ok, error = True, None
        try:
            response = client.chat(role, [{"role": "user", "content": prompt}])
            if not response.content.strip():
                ok, error = False, "empty response"
            elif expects_json:
                extract_json_object(response.content)
        except Exception as exc:  # noqa: BLE001 — a failed probe is a result
            ok, error = False, f"{type(exc).__name__}: {str(exc)[:200]}"
        latency_ms = int((time.monotonic() - started) * 1000)
        results.append(ModelProbeResult(
            role=role, model=model, fallbacks=fallbacks, ok=ok,
            latency_ms=latency_ms, expects_json=expects_json, error=error,
        ))
        log.info("probe %-13s %-18s %s (%d ms)", role, model,
                 "ok" if ok else f"FAILED: {error}", latency_ms)

    report = ModelEvalReport(
        evaluated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        base_url=config.llm.base_url,
        passed=all(r.ok for r in results),
        results=results,
        metrics=aggregate_run_metrics(Path(config.runs_dir), repo=repo),
    )

    agent_dir = repo / config.memory.dir
    agent_dir.mkdir(parents=True, exist_ok=True)
    out = agent_dir / BENCHMARKS_FILENAME
    # Trend data (Phase H6): fold the previous evaluation into a bounded
    # history so successive runs expose model-performance drift.
    report.history = _rolled_history(out)
    out.write_text(json.dumps(report.model_dump(), indent=2) + "\n",
                   encoding="utf-8")
    log.info("benchmarks written to %s", out)
    return report


_HISTORY_CAP = 20


def _rolled_history(benchmarks_path: Path) -> list[dict]:
    """Previous file's history + a compact summary of its own evaluation."""
    try:
        previous = json.loads(benchmarks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    summary = {
        "evaluated_at": previous.get("evaluated_at", ""),
        "passed": previous.get("passed"),
        "roles": {
            r.get("role", "?"): {
                "model": r.get("model", ""),
                "ok": r.get("ok"),
                "latency_ms": r.get("latency_ms", 0),
            }
            for r in previous.get("results", [])
        },
    }
    history = list(previous.get("history", []))
    history.append(summary)
    return history[-_HISTORY_CAP:]


# ── run-history quality metrics (Phase H6) ───────────────────────────────────


def aggregate_run_metrics(runs_dir: Path, repo: Path | None = None) -> RunMetrics:
    """Scan run reports (optionally scoped to one repository) and derive the
    dashboard rates: planning accuracy, coding success, validation pass,
    debugging success, review approval, execution speed."""

    def rate(hits: int, total: int) -> float | None:
        return round(hits / total, 3) if total else None

    total = completed = 0
    plans = plans_done = 0
    validations = validations_green = 0
    healed_runs = healed_completed = 0
    reviews = reviews_approved = 0
    heal_iterations: list[int] = []
    durations: list[float] = []

    for run_dir in sorted(runs_dir.iterdir()) if runs_dir.is_dir() else []:
        report_file = run_dir / "report.json"
        if not report_file.is_file():
            continue
        try:
            data = json.loads(report_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if repo is not None and data.get("repo_path") not in (
            str(repo), str(Path(repo).resolve())
        ):
            continue
        total += 1
        if data.get("status") == "completed":
            completed += 1
        if data.get("plan"):
            plans += 1
            tasks = {t["id"] for t in data["plan"].get("tasks", [])}
            done = {r["task_id"] for r in data.get("task_results", [])
                    if r.get("status") == "done"}
            if tasks and tasks <= done:
                plans_done += 1
        if data.get("validation") is not None:
            validations += 1
            if data["validation"].get("passed"):
                validations_green += 1
        iterations = int(data.get("iterations_used", 0) or 0)
        if iterations > 0:
            healed_runs += 1
            heal_iterations.append(iterations)
            if data.get("status") == "completed":
                healed_completed += 1
        review = data.get("review")
        if review is not None:
            reviews += 1
            if review.get("verdict") == "approve":
                reviews_approved += 1
        duration = _run_duration_seconds(run_dir)
        if duration is not None:
            durations.append(duration)

    return RunMetrics(
        runs_total=total,
        runs_completed=completed,
        planning_accuracy=rate(plans_done, plans),
        coding_success_rate=rate(completed, total),
        validation_pass_rate=rate(validations_green, validations),
        debugging_success_rate=rate(healed_completed, healed_runs),
        review_approval_rate=rate(reviews_approved, reviews),
        avg_heal_iterations=(round(sum(heal_iterations) / len(heal_iterations), 2)
                             if heal_iterations else None),
        avg_run_seconds=(round(sum(durations) / len(durations), 1)
                         if durations else None),
    )


def _run_duration_seconds(run_dir: Path) -> float | None:
    """Wall clock of a run from its journal's first and last event."""
    journal = run_dir / "journal.jsonl"
    try:
        lines = journal.read_text(encoding="utf-8").strip().splitlines()
        first = json.loads(lines[0])["ts"]
        last = json.loads(lines[-1])["ts"]
        span = (datetime.fromisoformat(last)
                - datetime.fromisoformat(first)).total_seconds()
        return max(span, 0.0)
    except (OSError, IndexError, KeyError, ValueError):
        return None


# ── governance report (Phase H6: visualization & reporting) ─────────────────


def render_governance_report(report: ModelEvalReport, repo: Path) -> str:
    """docs/MODEL_GOVERNANCE_REPORT.md — the human-readable dashboard."""

    def pct(value: float | None) -> str:
        return f"{value * 100:.0f}%" if value is not None else "—"

    lines = [
        "# Model Governance Report",
        "",
        f"Repository: `{Path(repo).name}` · generated by "
        f"`local-ezai evaluate-models --report` on {report.evaluated_at} · "
        f"endpoint `{report.base_url}` · "
        f"overall: {'**PASS**' if report.passed else '**FAIL**'}",
        "",
        "Routing source: `.agent/model_registry.yaml` "
        "([SELF_EVOLUTION_GUIDE.md](SELF_EVOLUTION_GUIDE.md) §3); raw data: "
        "`.agent/model_benchmarks.json`.",
        "",
        "## Routing & availability probes",
        "",
        "| Role | Primary model | Fallbacks | Probe | Latency | JSON-validated |",
        "|---|---|---|---|---|---|",
    ]
    for r in report.results:
        lines.append(
            f"| {r.role} | `{r.model}` | "
            f"{', '.join(f'`{f}`' for f in r.fallbacks) or '—'} | "
            f"{'✅ ok' if r.ok else '❌ ' + (r.error or 'failed')[:60]} | "
            f"{r.latency_ms} ms | {'yes' if r.expects_json else '—'} |"
        )

    m = report.metrics or RunMetrics()
    heal = m.avg_heal_iterations if m.avg_heal_iterations is not None else "—"
    speed = f"{m.avg_run_seconds} s" if m.avg_run_seconds is not None else "—"
    metric_rows = [
        ("Planning accuracy", pct(m.planning_accuracy),
         "plans fully executed / plans produced"),
        ("Coding success rate", pct(m.coding_success_rate),
         f"completed runs / all runs ({m.runs_completed}/{m.runs_total})"),
        ("Validation pass rate", pct(m.validation_pass_rate),
         "green validations / runs validated"),
        ("Debugging success rate", pct(m.debugging_success_rate),
         "healed-and-delivered / runs that self-healed"),
        ("Review approval rate", pct(m.review_approval_rate),
         "approvals / reviewer-gate runs"),
        ("Avg heal iterations", str(heal), "debug→fix cycles per healing run"),
        ("Avg execution speed", speed, "journal wall clock per run"),
    ]
    lines += [
        "",
        "## Quality metrics (from run history)",
        "",
        "| Metric | Value | Basis |",
        "|---|---|---|",
        *(f"| {name} | {value} | {basis} |"
          for name, value, basis in metric_rows),
    ]

    if report.history:
        lines += [
            "",
            "## Trend (previous evaluations)",
            "",
            "| Evaluated at | Overall | Role failures | Avg latency |",
            "|---|---|---|---|",
        ]
        for entry in report.history[-10:]:
            roles = entry.get("roles", {})
            failures = [r for r, v in roles.items() if not v.get("ok")]
            latencies = [v.get("latency_ms", 0) for v in roles.values()]
            avg_latency = (f"{sum(latencies) // len(latencies)} ms"
                           if latencies else "—")
            lines.append(
                f"| {entry.get('evaluated_at', '?')} | "
                f"{'pass' if entry.get('passed') else 'FAIL'} | "
                f"{', '.join(failures) or '—'} | {avg_latency} |"
            )

    lines += [
        "",
        "## Governance",
        "",
        "- Routing changes are **model replacement** — a human-approved PR "
        "editing `.agent/model_registry.yaml`, evidenced by this report "
        "([GOVERNANCE.md](GOVERNANCE.md)).",
        "- The evolution workflow reads these trends before proposing "
        "improvements ([SELF_EVOLUTION_GUIDE.md](SELF_EVOLUTION_GUIDE.md)).",
        "- Regenerate anytime: `local-ezai evaluate-models --report`.",
        "",
    ]
    return "\n".join(lines)


def write_governance_report(report: ModelEvalReport, repo: Path) -> Path:
    docs_dir = Path(repo) / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    out = docs_dir / "MODEL_GOVERNANCE_REPORT.md"
    out.write_text(render_governance_report(report, Path(repo)),
                   encoding="utf-8")
    log.info("governance report written to %s", out)
    return out
