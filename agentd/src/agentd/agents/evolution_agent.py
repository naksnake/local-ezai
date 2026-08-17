"""Evolution Agent (CLAUDE.md roster, Phase 7).

Purpose: **improve Local-EZAI (or any target repo) — never replace it.**

The agent receives deterministic evidence gathered from the platform's own
records — implementation history, failed fixes and their signatures, recent
run outcomes, the roadmap — analyzes history, analyzes failures, identifies
bottlenecks, and proposes a small set of concrete improvements as a
schema-validated :class:`~agentd.schemas.EvolutionProposal`.

The agent only proposes. The evolution pipeline (evolution.py) implements,
validates, benchmarks, and ends at a pull request / proposal bundle —
**human approval is the terminal state** (CLAUDE.md Human Governance).
"""

from __future__ import annotations

from agentd.agents.base import BaseAgent
from agentd.agents.prompts import load_prompt
from agentd.schemas import EvolutionProposal
from agentd.workspace import Workspace


class EvolutionAgent(BaseAgent):
    agent_name = "evolution"
    role = "evolution"
    tool_names = ["fs_ls", "fs_read", "fs_glob", "code_grep"]  # read-only

    def run(self, evidence: str, workspace: Workspace,
            focus: str = "") -> EvolutionProposal:
        user = (
            f"Repository: {workspace.root.name} (branch {workspace.branch})\n\n"
            f"Evidence gathered from the platform's own records:\n"
            f"---\n{evidence[:12000]}\n---\n"
        )
        if focus:
            user += f"\nHuman focus for this evolution cycle: {focus}\n"
        user += ("\nAnalyze the history and failures, identify bottlenecks, "
                 "explore the repository where needed, then reply with the "
                 "JSON evolution proposal.")
        proposal: EvolutionProposal = self.ask_for_json(
            load_prompt("evolution"), user, workspace,
            EvolutionProposal.model_validate,
        )
        self.journal.append(
            "EVOLUTION_PROPOSAL",
            title=proposal.title,
            failure_patterns=proposal.failure_patterns[:5],
            bottlenecks=proposal.bottlenecks[:5],
            improvements=[i.id for i in proposal.improvements],
        )
        return proposal
