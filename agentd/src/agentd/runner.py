"""Run assembly — wires config, workspace, journal, tools, agents, and the
graph together for one run. The CLI and the tests both enter here."""

from __future__ import annotations

import secrets
import time
from pathlib import Path

from agentd.agents import (
    BrowserQAAgent,
    CoderAgent,
    DebuggerAgent,
    GitAgent,
    PlannerAgent,
    ValidationAgent,
)
from agentd.config import AgentdConfig, load_repo_overrides, merge_repo_overrides
from agentd.graph import Orchestrator
from agentd.journal import Journal
from agentd.llm import LLMClient, build_llm
from agentd.logging_setup import get_logger
from agentd.permissions import PermissionPolicy
from agentd.rca import RcaEngine
from agentd.schemas import Plan, RunReport
from agentd.tools import ALL_TOOL_CLASSES
from agentd.tools.base import ToolRegistry
from agentd.workspace import Workspace, create_workspace

log = get_logger("runner")


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def build_registry(config: AgentdConfig, journal: Journal) -> ToolRegistry:
    return ToolRegistry(
        tools=[cls() for cls in ALL_TOOL_CLASSES],
        policy=PermissionPolicy(config),
        journal=journal,
        max_output_chars=config.limits.tool_output_max_chars,
    )


def prepare_run(
    config: AgentdConfig, repo: Path, run_id: str
) -> tuple[AgentdConfig, Workspace, Journal]:
    """Apply repo overrides, create the workspace and the journal."""
    config = merge_repo_overrides(config, load_repo_overrides(repo))
    workspace = create_workspace(config, repo, run_id)
    # Re-read overrides from the workspace itself (worktree == repo content
    # at HEAD; a repo may carry .agentd.yaml only on the checked-out branch).
    config = merge_repo_overrides(config, load_repo_overrides(workspace.root))
    journal = Journal(config.runs_dir / run_id)
    return config, workspace, journal


def execute_run(
    config: AgentdConfig,
    repo: Path,
    request: str,
    llm: LLMClient | None = None,
    run_id: str | None = None,
) -> RunReport:
    """End-to-end run: workspace → graph → report."""
    run_id = run_id or new_run_id()
    config, workspace, journal = prepare_run(config, repo, run_id)
    llm = llm or build_llm(config.llm)
    registry = build_registry(config, journal)

    orchestrator = Orchestrator(
        config=config,
        workspace=workspace,
        journal=journal,
        planner=PlannerAgent(config, llm, registry, journal),
        coder=CoderAgent(config, llm, registry, journal),
        validator=ValidationAgent(config, llm, registry, journal),
        git_agent=GitAgent(config, llm, registry, journal),
        debugger=DebuggerAgent(config, llm, registry, journal),
        browser_qa=BrowserQAAgent(config, llm, registry, journal),
        rca_engine=RcaEngine(config.limits.stall_threshold),
    )
    log.info("run %s starting in %s (branch %s)", run_id, workspace.root, workspace.branch)
    report = orchestrator.run(run_id, request)
    (journal.run_dir / "report.json").write_text(
        report.model_dump_json(indent=2), encoding="utf-8"
    )
    return report


def plan_only(
    config: AgentdConfig,
    repo: Path,
    request: str,
    llm: LLMClient | None = None,
    run_id: str | None = None,
) -> Plan:
    """A0 dry-run: produce and return the plan without executing anything.

    Runs in-place (the Planner is read-only), so no branch or worktree is
    created — a plan leaves zero traces in the repository.
    """
    run_id = run_id or new_run_id()
    config = config.model_copy(deep=True)
    config.workspace.mode = "in-place"
    config, workspace, journal = prepare_run(config, repo, run_id)
    llm = llm or build_llm(config.llm)
    registry = build_registry(config, journal)
    planner = PlannerAgent(config, llm, registry, journal)
    journal.append("RUN_SUBMITTED", run_id=run_id, request=request, mode="plan-only")
    plan = planner.run(request, workspace)
    journal.append("RUN_TERMINAL", status="completed", mode="plan-only")
    return plan
