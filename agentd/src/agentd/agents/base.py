"""BaseAgent — the shared tool-calling loop.

Each agent turn: model call → (tool calls? execute through the registry and
feed results back : final text). The loop is bounded by
``limits.max_agent_turns``; budget exhaustion is a normal, journaled outcome
(ADR-010: loop integrity is code, not model judgment).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentd.config import AgentdConfig
from agentd.journal import Journal
from agentd.llm import LLMClient
from agentd.logging_setup import get_logger
from agentd.tools.base import ToolRegistry
from agentd.workspace import Workspace

log = get_logger("agents")


@dataclass
class LoopOutcome:
    """What a tool loop produced."""

    final_text: str
    turns_used: int
    budget_exhausted: bool = False


class BaseAgent:
    #: journal / logging identity, e.g. "planner"
    agent_name: str = "agent"
    #: LLM role for model selection (ADR-007)
    role: str = "default"
    #: tool allowlist (names); empty list = no tools offered
    tool_names: list[str] = []

    def __init__(
        self,
        config: AgentdConfig,
        llm: LLMClient,
        registry: ToolRegistry,
        journal: Journal,
    ) -> None:
        self.config = config
        self.llm = llm
        self.registry = registry
        self.journal = journal
        self.log = get_logger(f"agents.{self.agent_name}")

    # ── The loop ────────────────────────────────────────────────────────────

    def run_loop(
        self,
        system_prompt: str,
        user_prompt: str,
        workspace: Workspace,
        max_turns: int | None = None,
    ) -> LoopOutcome:
        max_turns = max_turns or self.config.limits.max_agent_turns
        schemas = self.registry.schemas(self.tool_names) if self.tool_names else None
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        self.journal.append("AGENT_SPAWNED", agent=self.agent_name, role=self.role,
                            tools=self.tool_names, max_turns=max_turns)

        for turn in range(1, max_turns + 1):
            response = self.llm.chat(self.role, messages, tools=schemas)
            self.journal.append(
                "LLM_CALL",
                agent=self.agent_name,
                turn=turn,
                tool_calls=[tc.name for tc in response.tool_calls],
                content_chars=len(response.content),
            )
            if not response.wants_tools:
                return LoopOutcome(final_text=response.content, turns_used=turn)

            messages.append(
                {
                    "role": "assistant",
                    "content": response.content or None,
                    "tool_calls": [tc.as_message_entry() for tc in response.tool_calls],
                }
            )
            for tc in response.tool_calls:
                if tc.parse_error:
                    result_text = f"ERROR: {tc.parse_error}"
                else:
                    result = self.registry.execute(
                        tc.name,
                        tc.arguments,
                        workspace,
                        agent=self.agent_name,
                        allowlist=self.tool_names,
                    )
                    result_text = result.to_model_text()
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": result_text}
                )

        self.log.warning("%s exhausted %d turns", self.agent_name, max_turns)
        return LoopOutcome(final_text="", turns_used=max_turns, budget_exhausted=True)

    # ── Structured-output helper ────────────────────────────────────────────

    def ask_for_json(
        self,
        system_prompt: str,
        user_prompt: str,
        workspace: Workspace,
        validate: Any,
        retries: int | None = None,
    ) -> Any:
        """Run the loop, then parse+validate the final text as JSON.

        ``validate`` is a callable (e.g. ``Plan.model_validate``) applied to
        the extracted object. On parse/validation failure the error is fed
        back to the model, bounded by ``retries``.
        """
        from agentd.schemas import extract_json_object

        retries = self.config.llm.retries if retries is None else retries
        prompt = user_prompt
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            outcome = self.run_loop(system_prompt, prompt, workspace)
            if outcome.budget_exhausted:
                raise AgentError(
                    f"{self.agent_name} exhausted its turn budget before answering"
                )
            try:
                data = extract_json_object(outcome.final_text)
                return validate(data)
            except Exception as exc:  # noqa: BLE001 — ValueError or pydantic ValidationError
                last_error = exc
                self.journal.append(
                    "STRUCTURED_OUTPUT_RETRY",
                    agent=self.agent_name,
                    attempt=attempt + 1,
                    error=str(exc)[:500],
                )
                prompt = (
                    f"{user_prompt}\n\n"
                    f"Your previous reply could not be used ({exc}). "
                    "Reply again with ONLY the corrected JSON object."
                )
        raise AgentError(
            f"{self.agent_name} produced no valid structured output "
            f"after {retries + 1} attempts: {last_error}"
        )


class AgentError(RuntimeError):
    """An agent could not fulfill its contract (budget, structure, etc.)."""
