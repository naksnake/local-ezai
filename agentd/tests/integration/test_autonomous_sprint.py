"""Autonomous sprint execution end to end: requirement analysis → dependency
waves → PARALLEL task runs in separate worktrees → ordered merge-back →
documentation commit. Real git, real subprocess validation; per-task
ScriptedLLMs via the llm_factory seam (parallel waves interleave calls, so
each task needs its own deterministic client)."""

import json
import subprocess

import pytest
import yaml

from agentd.llm import ScriptedLLM
from agentd.schemas import SprintTaskSpec
from agentd.sprint_exec import run_sprint_autonomous
from tests.conftest import git, git_commit_all, review_approve_response


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


def sprint_plan_json(tasks):
    return json.dumps({
        "goal": "build the greeting feature set",
        "requirements": ["greeting module", "farewell module", "combined API"],
        "tasks": tasks,
        "notes": "",
    })


def task_script(goal, path, content):
    """Planner → coder writes one file → done → review approves
    (one full pipeline incl. the H2 reviewer gate)."""
    return [
        {"content": json.dumps({
            "goal": goal,
            "tasks": [{"id": "T1", "intent": goal, "files_hint": [path],
                       "check": "py_compile", "kind": "feature"}],
        })},
        {"tool_calls": [{"name": "fs_write",
                         "arguments": {"path": path, "content": content}}]},
        {"content": f"done: {goal}"},
        review_approve_response(),
    ]


def factory_for(scripts: dict):
    """Per-task ScriptedLLM factory keyed by SprintTaskSpec id."""
    made = {}

    def factory(task: SprintTaskSpec):
        made[task.id] = ScriptedLLM(scripts[task.id])
        return made[task.id]

    factory.made = made
    return factory


PARALLEL_PLAN = [
    {"id": "A", "title": "greeting module",
     "description": "create greeting.py with HELLO", "depends_on": []},
    {"id": "B", "title": "farewell module",
     "description": "create farewell.py with BYE", "depends_on": []},
    {"id": "C", "title": "combined api",
     "description": "create combined.py importing both", "depends_on": ["A", "B"]},
]


def test_parallel_waves_merge_and_document(config, green_repo):
    factory = factory_for({
        "_analysis_": [{"content": sprint_plan_json(PARALLEL_PLAN)}],
        "A": task_script("create greeting module", "greeting.py",
                         "HELLO = 'hi'\n"),
        "B": task_script("create farewell module", "farewell.py",
                         "BYE = 'bye'\n"),
        "C": task_script(
            "create combined api", "combined.py",
            "from greeting import HELLO\nfrom farewell import BYE\n"),
    })
    report = run_sprint_autonomous(config, green_repo, "# spec\nbuild it all\n",
                                   llm_factory=factory, sprint_id="asp1")

    assert report.status == "completed"
    assert report.waves == 2
    by_id = {t.task_id: t for t in report.tasks}
    assert by_id["A"].wave == 1 and by_id["B"].wave == 1
    assert by_id["C"].wave == 2 and by_id["C"].depends_on == ["A", "B"]
    assert all(t.status == "completed" and t.merged for t in report.tasks)

    # A and B ran in PARALLEL worktrees and merged back; C saw both files
    branch = report.branch
    assert "HELLO" in git(green_repo, "show", f"{branch}:greeting.py")
    assert "BYE" in git(green_repo, "show", f"{branch}:farewell.py")
    assert "from greeting import" in git(green_repo, "show", f"{branch}:combined.py")
    log = git(green_repo, "log", "--format=%s", branch)
    assert "merge sprint task A" in log
    assert "merge sprint task B" in log

    # documentation: generated, committed as the final commit, with the graph
    assert report.report_doc == "docs/sprints/sprint-asp1.md"
    doc = git(green_repo, "show", f"{branch}:docs/sprints/sprint-asp1.md")
    assert "```mermaid" in doc
    assert "A --> C" in doc and "B --> C" in doc
    assert "**Status:** COMPLETED" in doc
    assert git(green_repo, "log", "-1", "--format=%s", branch).startswith(
        "docs: sprint asp1 report")

    # parallel task worktrees and branches were cleaned up
    assert git(green_repo, "branch", "--list", f"{branch}-*") == ""
    # C genuinely ran AFTER the merge: its worktree is the sprint worktree
    assert by_id["C"].run_id.endswith("-c")


def test_dependency_failure_skips_dependents(config, green_repo):
    factory = factory_for({
        "_analysis_": [{"content": sprint_plan_json(PARALLEL_PLAN)}],
        "A": [  # planner ok, coder gives up → task A fails
            {"content": json.dumps({"goal": "g", "tasks": [
                {"id": "T1", "intent": "x", "check": "c"}]})},
            {"content": "FAILED: cannot build greeting"},
        ],
        "B": task_script("create farewell module", "farewell.py", "BYE = 1\n"),
        # C must never execute — no script entries would remain
    })
    report = run_sprint_autonomous(config, green_repo, "# spec\n",
                                   llm_factory=factory, sprint_id="asp2")

    assert report.status == "failed"
    by_id = {t.task_id: t for t in report.tasks}
    assert by_id["A"].status == "failed"
    assert by_id["B"].status == "completed"  # same wave, already running
    assert by_id["C"].status == "skipped"
    assert "dependency failed" in by_id["C"].error
    # B's completed work still merged into the sprint branch
    assert "BYE" in git(green_repo, "show", f"{report.branch}:farewell.py")
    # the report doc exists in the workspace but is NOT committed (sprint red)
    from pathlib import Path

    assert (Path(report.workspace_path) / report.report_doc).is_file()
    assert "docs/sprints" not in git(green_repo, "log", "--format=%s",
                                     report.branch)


def test_merge_conflict_marks_task_failed(config, green_repo):
    conflict_plan = [
        {"id": "A", "title": "edit app A",
         "description": "set VERSION to 2", "depends_on": []},
        {"id": "B", "title": "edit app B",
         "description": "set VERSION to 3", "depends_on": []},
    ]

    def edit_script(goal, new_line):
        return [
            {"content": json.dumps({"goal": goal, "tasks": [
                {"id": "T1", "intent": goal, "check": "c"}]})},
            {"tool_calls": [{"name": "fs_edit",
                             "arguments": {"path": "app.py",
                                           "old_string": "VERSION = 1",
                                           "new_string": new_line}}]},
            {"content": "done"},
            review_approve_response(),
        ]

    factory = factory_for({
        "_analysis_": [{"content": sprint_plan_json(conflict_plan)}],
        "A": edit_script("set VERSION 2", "VERSION = 2"),
        "B": edit_script("set VERSION 3", "VERSION = 3"),
    })
    report = run_sprint_autonomous(config, green_repo, "# spec\n",
                                   llm_factory=factory, sprint_id="asp3")

    assert report.status == "failed"
    by_id = {t.task_id: t for t in report.tasks}
    # first merge wins; the second conflicts and is marked failed
    assert by_id["A"].status == "completed" and by_id["A"].merged
    assert by_id["B"].status == "failed" and not by_id["B"].merged
    assert "merge conflict" in by_id["B"].error
    # the sprint branch holds A's version, and is not left mid-merge
    assert "VERSION = 2" in git(green_repo, "show", f"{report.branch}:app.py")
    from pathlib import Path

    assert git(Path(report.workspace_path), "status", "--porcelain") \
        .count("UU") == 0


def test_sequential_chain_shares_one_llm(config, green_repo):
    """Waves of size 1 run on the sprint worktree with a single shared
    client — the CLI path for scripted providers."""
    chain_plan = [
        {"id": "T1", "title": "one", "description": "create one.py",
         "depends_on": []},
        {"id": "T2", "title": "two", "description": "create two.py, needs one",
         "depends_on": ["T1"]},
    ]
    shared = ScriptedLLM([
        {"content": sprint_plan_json(chain_plan)},
        *task_script("create one", "one.py", "ONE = 1\n"),
        *task_script("create two", "two.py", "TWO = 2\n"),
    ])
    report = run_sprint_autonomous(config, green_repo, "# spec\n",
                                   llm=shared, sprint_id="asp4")
    assert report.status == "completed"
    assert report.waves == 2
    assert [t.status for t in report.tasks] == ["completed", "completed"]
    assert "TWO" in git(green_repo, "show", f"{report.branch}:two.py")
    # no merge commits: sequential tasks commit directly on the sprint branch
    assert "merge sprint task" not in git(green_repo, "log", "--format=%s",
                                          report.branch)


def test_sprint_journal_events(config, green_repo):
    shared = ScriptedLLM([
        {"content": sprint_plan_json([
            {"id": "T1", "title": "one", "description": "create one.py",
             "depends_on": []}])},
        *task_script("create one", "one.py", "ONE = 1\n"),
    ])
    run_sprint_autonomous(config, green_repo, "# spec\n", llm=shared,
                          sprint_id="asp5")
    journal = config.runs_dir / "asp5" / "journal.jsonl"
    events = [json.loads(line) for line in journal.read_text().splitlines()
              if line]
    types = [e["type"] for e in events]
    for expected in ("SPRINT_PLAN", "SPRINT_WAVES", "SPRINT_WAVE_STARTED",
                     "SPRINT_TASK", "SPRINT_DOC", "RUN_TERMINAL"):
        assert expected in types, f"missing {expected}"
    doc_events = [e for e in events if e["type"] == "SPRINT_DOC"]
    assert doc_events[-1]["payload"]["committed"] is True
