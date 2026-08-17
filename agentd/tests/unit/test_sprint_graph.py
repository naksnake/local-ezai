"""Dependency graph (deterministic) + Sprint Agent (requirement analysis)."""

import json

import pytest

from agentd.agents import SprintAgent
from agentd.agents.base import AgentError
from agentd.journal import Journal, NullJournal
from agentd.llm import ScriptedLLM
from agentd.runner import build_registry
from agentd.schemas import SprintPlan, SprintTaskSpec
from agentd.sprint import topological_waves, validate_dependencies


def spec(task_id, deps=()):
    return SprintTaskSpec(id=task_id, title=task_id, description="d",
                          depends_on=list(deps))


# ── DAG validation ────────────────────────────────────────────────────────────


def test_valid_dag():
    tasks = [spec("T1"), spec("T2", ["T1"]), spec("T3", ["T1"])]
    assert validate_dependencies(tasks) == []


def test_duplicate_ids():
    errors = validate_dependencies([spec("T1"), spec("T1")])
    assert any("duplicate" in e for e in errors)


def test_unknown_dependency():
    errors = validate_dependencies([spec("T1", ["T9"])])
    assert any("unknown task 'T9'" in e for e in errors)


def test_self_dependency():
    errors = validate_dependencies([spec("T1", ["T1"])])
    assert any("depends on itself" in e for e in errors)


def test_cycle_detected():
    tasks = [spec("T1", ["T3"]), spec("T2", ["T1"]), spec("T3", ["T2"])]
    errors = validate_dependencies(tasks)
    assert any("cycle" in e for e in errors)
    assert "T1" in errors[0] and "T2" in errors[0] and "T3" in errors[0]


# ── topological waves ─────────────────────────────────────────────────────────


def test_waves_parallel_grouping():
    tasks = [spec("A"), spec("B"), spec("C", ["A", "B"]), spec("D", ["C"]),
             spec("E")]
    waves = topological_waves(tasks)
    assert [[t.id for t in wave] for wave in waves] == [
        ["A", "B", "E"],  # independent → one parallel wave
        ["C"],
        ["D"],
    ]


def test_waves_chain_is_sequential():
    tasks = [spec("T1"), spec("T2", ["T1"]), spec("T3", ["T2"])]
    waves = topological_waves(tasks)
    assert [[t.id for t in w] for w in waves] == [["T1"], ["T2"], ["T3"]]


def test_waves_preserve_plan_order_within_wave():
    tasks = [spec("Z"), spec("A")]
    assert [t.id for t in topological_waves(tasks)[0]] == ["Z", "A"]


# ── Sprint Agent ──────────────────────────────────────────────────────────────

GOOD_PLAN = json.dumps({
    "goal": "customer management sprint",
    "requirements": ["JWT auth", "customer CRUD"],
    "tasks": [
        {"id": "T1", "title": "auth", "description": "JWT auth incl. tests",
         "depends_on": []},
        {"id": "T2", "title": "crud", "description": "customer CRUD incl. tests",
         "depends_on": []},
        {"id": "T3", "title": "wire-up", "description": "protect CRUD with auth",
         "depends_on": ["T1", "T2"]},
    ],
    "notes": "",
})

CYCLIC_PLAN = json.dumps({
    "goal": "g", "requirements": [],
    "tasks": [
        {"id": "T1", "title": "a", "description": "d", "depends_on": ["T2"]},
        {"id": "T2", "title": "b", "description": "d", "depends_on": ["T1"]},
    ],
})

SPEC = "# Sprint\n- build auth\n- build crud\n- wire them together\n"


def make_agent(config, script, journal=None):
    journal = journal or NullJournal()
    llm = ScriptedLLM(script)
    return SprintAgent(config, llm, build_registry(config, journal),
                       journal, memory=None), llm


def test_sprint_agent_returns_validated_plan(config, inplace_ws):
    agent, llm = make_agent(config, [{"content": GOOD_PLAN}])
    plan = agent.run(SPEC, inplace_ws)
    assert isinstance(plan, SprintPlan)
    assert [t.id for t in plan.tasks] == ["T1", "T2", "T3"]
    assert plan.tasks[2].depends_on == ["T1", "T2"]
    # the spec text reached the model
    assert "wire them together" in llm.calls[0]["messages"][1]["content"]


def test_sprint_agent_retries_cyclic_graph(config, inplace_ws):
    agent, llm = make_agent(config,
                            [{"content": CYCLIC_PLAN}, {"content": GOOD_PLAN}])
    plan = agent.run(SPEC, inplace_ws)
    assert len(plan.tasks) == 3
    # the retry prompt named the structural problem
    retry_msg = llm.calls[1]["messages"][1]["content"]
    assert "cycle" in retry_msg


def test_sprint_agent_gives_up_after_retries(config, inplace_ws):
    config.llm.retries = 1
    agent, _ = make_agent(config,
                          [{"content": CYCLIC_PLAN}, {"content": CYCLIC_PLAN}])
    with pytest.raises(AgentError):
        agent.run(SPEC, inplace_ws)


def test_sprint_agent_can_explore_and_journals(config, inplace_ws, tmp_path):
    journal = Journal(tmp_path / "runs" / "sp")
    llm = ScriptedLLM([
        {"tool_calls": [{"name": "fs_ls", "arguments": {}}]},
        {"content": GOOD_PLAN},
    ])
    agent = SprintAgent(config, llm, build_registry(config, journal), journal)
    agent.run(SPEC, inplace_ws)
    events = [e for e in journal.read() if e["type"] == "SPRINT_PLAN"]
    assert events
    assert events[0]["payload"]["tasks"] == ["T1", "T2", "T3"]
    assert "T1->T3" in events[0]["payload"]["edges"]


def test_sprint_agent_cannot_write(config, inplace_ws):
    agent, _ = make_agent(config, [
        {"tool_calls": [{"name": "fs_write",
                         "arguments": {"path": "evil.py", "content": "x"}}]},
        {"content": GOOD_PLAN},
    ])
    agent.run(SPEC, inplace_ws)
    assert not (inplace_ws.root / "evil.py").exists()
