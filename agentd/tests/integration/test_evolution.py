"""Evolution workflow end to end: evidence → proposal → implement →
validate → benchmark → release notes → PR bundle → human approval."""

import json
import subprocess

import pytest
import yaml

from agentd.evolution import gather_evidence, run_evolution
from agentd.llm import ScriptedLLM
from agentd.main_cli import main
from agentd.memory import KIND_FAILED_FIX, KIND_IMPLEMENTATION, MemoryStore
from tests.conftest import git, git_commit_all


@pytest.fixture
def green_repo(tmp_path):
    repo = tmp_path / "app"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    (repo / "app.py").write_text("VERSION = 1\n", encoding="utf-8")
    (repo / ".agentd.yaml").write_text(yaml.safe_dump({
        "validation": {"commands": {"test": ["python3 -m py_compile app.py"]}},
    }), encoding="utf-8")
    git_commit_all(repo, "initial commit")
    return repo


@pytest.fixture
def config(tmp_path):
    from agentd.config import AgentdConfig

    cfg = AgentdConfig()
    cfg.workspace.root = tmp_path / "ws"
    cfg.runs_dir = tmp_path / "runs"
    cfg.validation.autodetect = False
    cfg.llm.provider = "scripted"
    return cfg


PROPOSAL = {"content": json.dumps({
    "title": "harden input validation",
    "history_summary": "recent runs repeatedly fixed input handling",
    "failure_patterns": ["assertion failures around empty input"],
    "bottlenecks": ["no guard clauses in app.py"],
    "improvements": [
        {"id": "I1", "title": "guard empty input",
         "description": "add an is_valid() helper to app.py returning bool; "
                        "acceptance: py_compile passes",
         "rationale": "addresses the repeated assertion failures"},
    ],
    "notes": "small, single-file change",
})}

IMPROVEMENT_RUN = [
    {"content": json.dumps({"goal": "guard empty input", "tasks": [
        {"id": "T1", "intent": "add is_valid helper", "check": "py_compile"}]})},
    {"tool_calls": [{"name": "fs_write", "arguments": {
        "path": "guards.py", "content": "def is_valid(x):\n    return bool(x)\n"}}]},
    {"content": "added the helper"},
]


def seed_history(repo, config):
    store = MemoryStore(repo / config.memory.dir)
    store.record(KIND_IMPLEMENTATION, "fixed empty-input crash",
                 "status: completed", run_id="old1")
    store.record(KIND_FAILED_FIX, "bad fix", "tried catching everything",
                 run_id="old2", error_signature="sig-A", category="exception",
                 data={"approach": "wrap in try/except"})
    store.record(KIND_FAILED_FIX, "bad fix again", "tried catching everything",
                 run_id="old3", error_signature="sig-A", category="exception",
                 data={"approach": "wrap in try/except harder"})
    store.close()


def test_gather_evidence_contains_history_failures_and_runs(config, green_repo):
    seed_history(green_repo, config)
    (config.runs_dir / "r1").mkdir(parents=True)
    (config.runs_dir / "r1" / "report.json").write_text(json.dumps(
        {"status": "failed", "iterations_used": 3, "error": "stall"}))
    evidence = gather_evidence(config, green_repo)
    assert "Implementation history" in evidence
    assert "fixed empty-input crash" in evidence
    assert "REPEATED failure signatures" in evidence
    assert "x2: sig-A" in evidence
    assert "Recent runs" in evidence and "stall" in evidence


def test_evolution_cycle_end_to_end(config, green_repo):
    seed_history(green_repo, config)
    llm = ScriptedLLM([PROPOSAL, *IMPROVEMENT_RUN])
    report = run_evolution(config, green_repo, llm=llm, evolution_id="ev1")

    assert report.status == "completed"
    assert report.branch == "evolve/ev1"
    assert report.proposal.title == "harden input validation"
    assert [t.status for t in report.tasks] == ["completed"]

    # benchmark ran before and after, against the repo's own commands
    assert report.benchmark_before.passed and report.benchmark_after.passed
    assert report.benchmark_before.checks == 1
    assert report.benchmark_after.duration_seconds > 0

    # implementation + release notes are commits on the evolve branch
    log = git(green_repo, "log", "--format=%s", "evolve/ev1")
    assert "feat: guard empty input" in log
    assert log.splitlines()[0].startswith("docs: release notes for evolution")
    notes = git(green_repo, "show", "evolve/ev1:docs/RELEASE_NOTES.md")
    assert "evolution ev1: harden input validation" in notes
    assert "I1: guard empty input" in notes
    assert report.release_notes_updated

    # PR: no forge configured → human-reviewable bundle, nothing pushed
    pr = report.pull_request
    assert pr is not None and not pr.created
    bundle = (config.runs_dir / "ev1" / "PR_PROPOSAL.md").read_text()
    assert "evolve: harden input validation" in bundle
    assert "benchmark" in bundle.lower() or "Benchmark" in bundle
    assert "Human review and approval required" in bundle

    # journal trajectory
    events = [json.loads(line) for line in
              (config.runs_dir / "ev1" / "journal.jsonl")
              .read_text().splitlines() if line]
    types = [e["type"] for e in events]
    for expected in ("EVOLUTION_EVIDENCE", "EVOLUTION_PROPOSAL",
                     "EVOLUTION_BENCHMARK", "EVOLUTION_TASK",
                     "EVOLUTION_RELEASE_NOTES", "EVOLUTION_PR",
                     "RUN_TERMINAL"):
        assert expected in types, f"missing {expected}"
    # the user's checkout is untouched
    assert git(green_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_evolution_failure_skips_notes_and_pr(config, green_repo):
    llm = ScriptedLLM([
        PROPOSAL,
        {"content": json.dumps({"goal": "g", "tasks": [
            {"id": "T1", "intent": "x", "check": "c"}]})},
        {"content": "FAILED: cannot implement safely"},
    ])
    report = run_evolution(config, green_repo, llm=llm, evolution_id="ev2")
    assert report.status == "failed"
    assert report.tasks[0].status == "failed"
    assert not report.release_notes_updated
    assert report.pull_request is None
    assert "cannot implement safely" in (report.error or "")
    log = git(green_repo, "log", "--format=%s", "evolve/ev2")
    assert "release notes" not in log


# ── CLI: evolve / roadmap / evaluate-models / docs ───────────────────────────


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)

    def invoke(script, *argv, capsys):
        script_file = tmp_path / "script.json"
        script_file.write_text(json.dumps(script), encoding="utf-8")
        cfg_file = tmp_path / "cfg.yaml"
        cfg_file.write_text(yaml.safe_dump({
            "llm": {"provider": "scripted", "script_path": str(script_file)},
            "workspace": {"root": str(tmp_path / "ws")},
            "runs_dir": str(tmp_path / "runs"),
            "validation": {"autodetect": False},
        }), encoding="utf-8")
        code = main([*argv, "--config", str(cfg_file)])
        return code, capsys.readouterr().out

    return invoke


def test_cli_evolve(green_repo, cli, capsys):
    code, out = cli([PROPOSAL, *IMPROVEMENT_RUN], str(green_repo), "evolve",
                    capsys=capsys)
    assert code == 0
    assert "proposal: harden input validation" in out
    assert "[DONE] I1" in out
    assert "benchmark: before PASS" in out
    assert "PR_PROPOSAL.md" in out
    assert "awaiting human review" in out


def test_cli_roadmap(green_repo, cli, capsys):
    agent_dir = green_repo / ".agent"
    agent_dir.mkdir(exist_ok=True)
    (agent_dir / "roadmap.md").write_text(
        "# Roadmap\n\n| ID | Milestone |\n|--|--|\n| M1 | do things |\n")
    code, out = cli([], str(green_repo), "roadmap", capsys=capsys)
    assert code == 0
    assert "# Roadmap" in out and "| M1 | do things |" in out
    code, out = cli([], str(green_repo), "roadmap", "--full", capsys=capsys)
    assert "| M1 | do things |" in out


def test_cli_roadmap_missing(green_repo, cli, capsys):
    code, out = cli([], str(green_repo), "roadmap", capsys=capsys)
    assert code == 0
    assert "no roadmap" in out


def test_cli_evaluate_models(green_repo, cli, capsys):
    from agentd.evaluate import PROBES

    responses = []
    for _, (_, expects_json) in PROBES.items():
        responses.append({"content": '{"ok": true}' if expects_json else "ready"})
    code, out = cli(responses, str(green_repo), "evaluate-models", capsys=capsys)
    assert code == 0
    assert "all roles passed" in out
    assert (green_repo / ".agent" / "model_benchmarks.json").is_file()


def test_cli_docs(green_repo, cli, capsys):
    script = [
        {"tool_calls": [{"name": "fs_write", "arguments": {
            "path": "docs/USER_GUIDE.md", "content": "# User Guide\n"}}]},
        {"content": "wrote the user guide"},
    ]
    code, out = cli(script, str(green_repo), "docs", capsys=capsys)
    assert code == 0
    assert "wrote: docs/USER_GUIDE.md" in out
    assert (green_repo / "docs" / "USER_GUIDE.md").is_file()
    assert "UNCOMMITTED" in out
