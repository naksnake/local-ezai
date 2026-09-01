"""Model transparency & benchmark dashboard (Phases H4–H6): models_used
attribution, `models` / `explain-run` commands, run-history metrics,
benchmark trend history, and the governance report."""

import argparse
import json

from agentd.config import AgentdConfig, LLMConfig
from agentd.evaluate import (
    PROBES,
    aggregate_run_metrics,
    evaluate_models,
    render_governance_report,
    write_governance_report,
)
from agentd.journal import Journal
from agentd.llm import LLMError, LLMResponse, OpenAICompatLLM, ScriptedLLM
from agentd.main_cli import cmd_explain_run, cmd_models
from agentd.schemas import ModelEvalReport, ModelProbeResult, RunMetrics

REGISTRY_YAML = """\
agent_model_map:
  planner:
    primary: hermes3
    fallback: deepseek-r1
  coder:
    primary: qwen3-coder
    fallback: deepseek-r1
  reviewer:
    primary: llama3
"""


def _probe_responses(bad_roles=frozenset()):
    return [
        {"content": "not json at all" if role in bad_roles
         else PROBES[role][0].split("Reply with ")[-1].replace(
             "ONLY this JSON object: ", "")}
        for role in PROBES
    ]


# ── models_used attribution (H5) ─────────────────────────────────────────────


def _client(monkeypatch, fail_models=frozenset()):
    config = LLMConfig(roles={"planner": "hermes3", "default": "base"},
                       role_fallbacks={"planner": ["deepseek-r1"]})
    client = OpenAICompatLLM(config)

    def fake_chat_model(model, messages, tools=None):
        if model in fail_models:
            raise LLMError(f"{model} down")
        return LLMResponse(content=f"answer from {model}")

    monkeypatch.setattr(client, "_chat_model", fake_chat_model)
    return client


def test_models_used_records_primary(monkeypatch):
    client = _client(monkeypatch)
    client.chat("planner", [{"role": "user", "content": "x"}])
    assert client.models_used == {"planner": "hermes3"}


def test_models_used_is_fallback_aware(monkeypatch):
    client = _client(monkeypatch, fail_models={"hermes3"})
    client.chat("planner", [{"role": "user", "content": "x"}])
    assert client.models_used["planner"] == "deepseek-r1"


def test_scripted_llm_attributes_roles():
    llm = ScriptedLLM([{"content": "a"}, {"content": "b"}])
    llm.chat("planner", [])
    llm.chat("coder", [])
    assert llm.models_used == {"planner": "scripted", "coder": "scripted"}


# ── local-ezai models (H4) ───────────────────────────────────────────────────


def test_cmd_models_shows_registry_routing(tmp_repo, capsys):
    (tmp_repo / ".agent").mkdir()
    (tmp_repo / ".agent" / "model_registry.yaml").write_text(
        REGISTRY_YAML, encoding="utf-8")
    args = argparse.Namespace(as_json=False)
    assert cmd_models(AgentdConfig(), tmp_repo, args) == 0
    out = capsys.readouterr().out
    assert "model_registry.yaml" in out
    assert "Planner:\n  primary:  hermes3\n  fallback: deepseek-r1" in out
    assert "Coder:\n  primary:  qwen3-coder" in out
    assert "Reviewer:\n  primary:  llama3\n  fallback: (none)" in out


def test_cmd_models_without_registry(tmp_repo, capsys):
    assert cmd_models(AgentdConfig(), tmp_repo, argparse.Namespace(as_json=False)) == 0
    out = capsys.readouterr().out
    assert "no registry" in out
    assert "Planner:" in out  # config defaults still shown


def test_cmd_models_json(tmp_repo, capsys):
    (tmp_repo / ".agent").mkdir()
    (tmp_repo / ".agent" / "model_registry.yaml").write_text(
        REGISTRY_YAML, encoding="utf-8")
    assert cmd_models(AgentdConfig(), tmp_repo, argparse.Namespace(as_json=True)) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["roles"]["planner"] == {"primary": "hermes3",
                                        "fallback": ["deepseek-r1"]}


# ── local-ezai explain-run (H5) ──────────────────────────────────────────────


def _write_run(config, project, run_id, *, status="completed", models=None,
               with_review=True):
    run_dir = config.runs_dir / run_id
    run_dir.mkdir(parents=True)
    report = {
        "run_id": run_id, "status": status, "request": "add OAuth login",
        "repo_path": str(project), "workspace_path": str(project),
        "branch": f"swe/{run_id}",
        "models_used": models if models is not None else {
            "planner": "hermes3", "coder": "qwen3-coder",
            "debugger": "deepseek-r1", "reviewer": "llama3",
        },
        "validation": {"passed": status == "completed", "checks": [],
                       "browser": {"enabled": True, "passed": True}},
        "review": ({"verdict": "approve", "findings": []}
                   if with_review else None),
        "plan": {"goal": "g", "tasks": [{"id": "T1", "intent": "i"}]},
        "task_results": [{"task_id": "T1", "status": "done", "summary": "s"}],
        "iterations_used": 1,
        "healing": [],
    }
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return run_dir


def test_explain_run_shows_stage_models(config, tmp_repo, capsys):
    _write_run(config, tmp_repo, "exp1")
    args = argparse.Namespace(run_id="exp1", as_json=False)
    assert cmd_explain_run(config, tmp_repo, args) == 0
    out = capsys.readouterr().out
    assert "task:   add OAuth login" in out
    assert "Planner:      hermes3" in out
    assert "Coder:        qwen3-coder" in out
    assert "Debugger:     deepseek-r1" in out
    assert "Reviewer:     llama3" in out
    assert "Browser QA:   Playwright (deterministic)" in out
    assert "review:  approve" in out
    assert "healing: 1 debug/fix iteration(s)" in out


def test_explain_run_defaults_to_latest_run_of_project(config, tmp_repo, capsys):
    import os
    import time

    first = _write_run(config, tmp_repo, "old", models={"planner": "m1"})
    stale = time.time() - 100
    os.utime(first / "report.json", (stale, stale))
    _write_run(config, tmp_repo, "new", models={"planner": "m2"})
    # a run of a DIFFERENT project is never picked
    other = _write_run(config, tmp_repo, "foreign", models={"planner": "m3"})
    data = json.loads((other / "report.json").read_text())
    data["repo_path"] = "/somewhere/else"
    (other / "report.json").write_text(json.dumps(data))

    args = argparse.Namespace(run_id=None, as_json=False)
    assert cmd_explain_run(config, tmp_repo, args) == 0
    out = capsys.readouterr().out
    assert "run:    new" in out and "m2" in out


def test_explain_run_missing_report_exits_2(config, tmp_repo, capsys):
    args = argparse.Namespace(run_id="nope", as_json=False)
    assert cmd_explain_run(config, tmp_repo, args) == 2


def test_explain_run_json(config, tmp_repo, capsys):
    _write_run(config, tmp_repo, "expj")
    args = argparse.Namespace(run_id="expj", as_json=True)
    assert cmd_explain_run(config, tmp_repo, args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["stages"]["Planner"] == "hermes3"
    assert data["stages"]["Browser QA"] == "Playwright (deterministic)"


# ── run-history metrics (H6) ─────────────────────────────────────────────────


def test_aggregate_run_metrics(config, tmp_repo):
    _write_run(config, tmp_repo, "m1")                       # green, healed
    _write_run(config, tmp_repo, "m2", status="failed")      # red
    run3 = _write_run(config, tmp_repo, "m3")
    data = json.loads((run3 / "report.json").read_text())
    data["review"] = {"verdict": "request_changes", "findings": []}
    (run3 / "report.json").write_text(json.dumps(data))
    # a journal for wall-clock measurement
    journal = Journal(config.runs_dir / "m1")
    journal.append("A")
    journal.append("B")

    metrics = aggregate_run_metrics(config.runs_dir, repo=tmp_repo)
    assert metrics.runs_total == 3 and metrics.runs_completed == 2
    assert metrics.coding_success_rate == round(2 / 3, 3)
    assert metrics.planning_accuracy == 1.0        # every plan fully executed
    assert metrics.validation_pass_rate == round(2 / 3, 3)
    assert metrics.debugging_success_rate == round(2 / 3, 3)
    assert metrics.review_approval_rate == round(2 / 3, 3)
    assert metrics.avg_heal_iterations == 1.0
    assert metrics.avg_run_seconds is not None

    foreign = aggregate_run_metrics(config.runs_dir, repo="/not/here")
    assert foreign.runs_total == 0
    assert foreign.coding_success_rate is None


# ── benchmark history + governance report (H6) ───────────────────────────────


def test_evaluate_models_rolls_history(config, tmp_repo):
    first = evaluate_models(config, tmp_repo, llm=ScriptedLLM(_probe_responses()))
    assert first.history == []
    second = evaluate_models(config, tmp_repo, llm=ScriptedLLM(_probe_responses()))
    assert len(second.history) == 1
    assert second.history[0]["evaluated_at"] == first.evaluated_at
    assert second.history[0]["roles"]["planner"]["ok"] is True
    benchmarks = json.loads(
        (tmp_repo / ".agent" / "model_benchmarks.json").read_text())
    assert len(benchmarks["history"]) == 1
    assert benchmarks["metrics"]["runs_total"] == 0


def test_governance_report_rendering(tmp_repo):
    report = ModelEvalReport(
        evaluated_at="2026-09-01T00:00:00+00:00", base_url="http://x/v1",
        passed=False,
        results=[
            ModelProbeResult(role="planner", model="hermes3",
                             fallbacks=["deepseek-r1"], ok=True,
                             latency_ms=210, expects_json=True),
            ModelProbeResult(role="coder", model="qwen3-coder", ok=False,
                             latency_ms=50, error="LLMError: down"),
        ],
        metrics=RunMetrics(runs_total=4, runs_completed=3,
                           coding_success_rate=0.75,
                           validation_pass_rate=1.0),
        history=[{"evaluated_at": "2026-08-31T00:00:00+00:00", "passed": True,
                  "roles": {"planner": {"model": "hermes3", "ok": True,
                                        "latency_ms": 190}}}],
    )
    text = render_governance_report(report, tmp_repo)
    assert "| planner | `hermes3` | `deepseek-r1` | ✅ ok | 210 ms | yes |" in text
    assert "❌ LLMError: down" in text
    assert "| Coding success rate | 75% | completed runs / all runs (3/4) |" in text
    assert "## Trend (previous evaluations)" in text
    assert "2026-08-31" in text

    out = write_governance_report(report, tmp_repo)
    assert out == tmp_repo / "docs" / "MODEL_GOVERNANCE_REPORT.md"
    assert out.read_text().startswith("# Model Governance Report")
