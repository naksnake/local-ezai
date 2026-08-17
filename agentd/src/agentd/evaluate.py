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
from agentd.schemas import ModelEvalReport, ModelProbeResult, extract_json_object

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
    )

    agent_dir = repo / config.memory.dir
    agent_dir.mkdir(parents=True, exist_ok=True)
    out = agent_dir / BENCHMARKS_FILENAME
    out.write_text(json.dumps(report.model_dump(), indent=2) + "\n",
                   encoding="utf-8")
    log.info("benchmarks written to %s", out)
    return report
