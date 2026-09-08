"""Tool abstraction layer.

A :class:`Tool` is a named, JSON-schema-described capability with a risk
tier. Agents never call tools directly — every invocation goes through the
:class:`ToolRegistry`, which enforces the permission policy, applies output
caps, journals the call, and converts any exception into a structured
:class:`ToolResult` (the model sees errors as data, never as crashes).

Tool names use ``snake_case`` (``fs_read``, ``git_push``) because OpenAI
function names may not contain dots; the architecture docs' ``fs.read``
notation maps 1:1.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from agentd.journal import Journal
from agentd.logging_setup import get_logger
from agentd.permissions import PermissionPolicy, ToolTier
from agentd.workspace import PathEscapeError, Workspace

log = get_logger("tools")


@dataclass
class ToolResult:
    """Uniform result envelope for every tool call."""

    ok: bool
    output: str = ""
    error: str | None = None
    exit_code: int | None = None
    duration_ms: int = 0
    truncated: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_model_text(self) -> str:
        """Compact representation handed back to the model."""
        if self.ok:
            body = self.output if self.output.strip() else "(no output)"
        else:
            body = f"ERROR: {self.error or 'unknown error'}"
            if self.output.strip():
                body += f"\n{self.output}"
        if self.truncated:
            body += "\n[output truncated]"
        return body


class Tool(ABC):
    """Base class for all tools."""

    name: str
    description: str
    tier: ToolTier
    parameters: dict[str, Any]  # JSON schema for the arguments object

    @abstractmethod
    def run(self, workspace: Workspace, **kwargs: Any) -> ToolResult:
        """Execute. Implementations raise freely; the registry catches."""

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Holds tool instances; the single gate every invocation passes through."""

    def __init__(
        self,
        tools: list[Tool],
        policy: PermissionPolicy,
        journal: Journal,
        max_output_chars: int = 8_000,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool
        self._policy = policy
        self._journal = journal
        self._max_output_chars = max_output_chars

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def schemas(self, names: list[str]) -> list[dict[str, Any]]:
        """OpenAI tool schemas for an agent's allowlist (unknown names fail loudly)."""
        missing = [n for n in names if n not in self._tools]
        if missing:
            raise KeyError(f"unknown tools requested: {missing}")
        return [self._tools[n].openai_schema() for n in names]

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        workspace: Workspace,
        agent: str = "?",
        allowlist: list[str] | None = None,
    ) -> ToolResult:
        """Permission-check, run, cap, and journal one tool call."""
        tool = self._tools.get(name)
        tier = tool.tier if tool else None

        if allowlist is not None and name not in allowlist:
            decision_reason = f"tool '{name}' is not in agent '{agent}' allowlist"
            allowed = False
        else:
            decision = self._policy.check(name, tier)
            allowed, decision_reason = decision.allowed, decision.reason

        self._journal.append(
            "TOOL_CALLED",
            agent=agent,
            tool=name,
            tier=int(tier) if tier is not None else None,
            arguments=_safe_args(arguments),
            allowed=allowed,
            reason=decision_reason,
        )
        if not allowed or tool is None:
            result = ToolResult(ok=False, error=f"permission denied: {decision_reason}")
            self._journal.append("TOOL_RESULT", agent=agent, tool=name, ok=False,
                                 error=result.error)
            return result

        start = time.monotonic()
        try:
            result = tool.run(workspace, **arguments)
        except PathEscapeError as exc:
            result = ToolResult(ok=False, error=str(exc))
        except TypeError as exc:  # wrong/missing arguments from the model
            result = ToolResult(ok=False, error=f"invalid arguments for {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 — models must see failures as data
            log.exception("tool %s crashed", name)
            result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        result.duration_ms = int((time.monotonic() - start) * 1000)

        if len(result.output) > self._max_output_chars:
            result.output = (
                result.output[: self._max_output_chars // 2]
                + "\n...\n"
                + result.output[-self._max_output_chars // 2 :]
            )
            result.truncated = True

        self._journal.append(
            "TOOL_RESULT",
            agent=agent,
            tool=name,
            ok=result.ok,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            truncated=result.truncated,
            error=result.error,
            output_chars=len(result.output),
        )
        return result


def _safe_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Journal-friendly copy of tool arguments (long values elided)."""
    out: dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str) and len(value) > 400:
            out[key] = value[:400] + f"... [{len(value)} chars]"
        else:
            out[key] = value
    return out
