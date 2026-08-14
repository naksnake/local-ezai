"""LLM access layer.

Two providers behind one interface:

- :class:`OpenAICompatLLM` — talks to any OpenAI-compatible endpoint
  (the stack's LiteLLM proxy by default, or the engine directly), including
  native function/tool calling (the vLLM/llama.cpp profiles both enable the
  hermes tool-call parser).
- :class:`ScriptedLLM` — replays canned responses from a list or JSON file.
  Used by the test suite and by ``llm.provider: scripted`` for offline demos.

Agents address models by *role* (planner/coder/validator/git); roles map to
model aliases in the config (ADR-007). Agents never see model names.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from agentd.config import LLMConfig
from agentd.logging_setup import get_logger

log = get_logger("llm")


@dataclass
class ToolCallRequest:
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    parse_error: str | None = None
    raw_arguments: str = ""

    def as_message_entry(self) -> dict[str, Any]:
        """Re-encode for the assistant message we append to history."""
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.raw_arguments or json.dumps(self.arguments),
            },
        }


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMClient(Protocol):
    def chat(
        self,
        role: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse: ...


def parse_chat_response(data: dict[str, Any]) -> LLMResponse:
    """Parse an OpenAI-format /chat/completions response body."""
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"malformed completion response: {exc}") from exc

    tool_calls: list[ToolCallRequest] = []
    for tc in message.get("tool_calls") or []:
        function = tc.get("function") or {}
        raw_args = function.get("arguments") or "{}"
        args: dict[str, Any] = {}
        parse_error = None
        try:
            parsed = json.loads(raw_args)
            if isinstance(parsed, dict):
                args = parsed
            else:
                parse_error = f"arguments must be a JSON object, got {type(parsed).__name__}"
        except json.JSONDecodeError as exc:
            parse_error = f"invalid JSON in tool arguments: {exc}"
        tool_calls.append(
            ToolCallRequest(
                id=tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                name=function.get("name") or "",
                arguments=args,
                parse_error=parse_error,
                raw_arguments=raw_args,
            )
        )
    return LLMResponse(content=message.get("content") or "", tool_calls=tool_calls)


class LLMError(RuntimeError):
    """Transport or protocol failure talking to the model."""


class OpenAICompatLLM:
    """Minimal, dependency-light client for OpenAI-compatible endpoints."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self._client = httpx.Client(
            base_url=config.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=config.timeout,
        )

    def chat(
        self,
        role: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.config.model_for_role(role),
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        last_error: Exception | None = None
        for attempt in range(self.config.retries + 1):
            try:
                response = self._client.post("/chat/completions", json=payload)
                if response.status_code >= 500:
                    raise LLMError(f"server error {response.status_code}: {response.text[:200]}")
                if response.status_code >= 400:
                    # Client errors will not improve on retry.
                    raise LLMError(
                        f"request rejected ({response.status_code}): {response.text[:500]}"
                    ) from None
                return parse_chat_response(response.json())
            except (httpx.HTTPError, LLMError) as exc:
                if isinstance(exc, LLMError) and "request rejected" in str(exc):
                    raise
                last_error = exc
                if attempt < self.config.retries:
                    delay = 2.0**attempt
                    log.warning("LLM call failed (%s); retrying in %.0fs", exc, delay)
                    time.sleep(delay)
        raise LLMError(f"LLM unreachable after {self.config.retries + 1} attempts: {last_error}")

    def close(self) -> None:
        self._client.close()


class ScriptedLLM:
    """Deterministic replay client.

    ``script`` is a list of response dicts:

        {"content": "final text"}                          # plain reply
        {"tool_calls": [{"name": "fs_read",
                         "arguments": {"path": "a.py"}}]}  # tool request

    Calls are consumed in order regardless of role; ``calls`` records every
    request for assertions in tests.
    """

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self._script = list(script)
        self._index = 0
        self.calls: list[dict[str, Any]] = []

    @classmethod
    def from_file(cls, path: Path) -> ScriptedLLM:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"scripted LLM file {path} must contain a JSON list")
        return cls(data)

    def chat(
        self,
        role: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.calls.append({"role": role, "messages": messages, "tools": tools})
        if self._index >= len(self._script):
            raise LLMError(
                f"scripted LLM exhausted after {len(self._script)} responses "
                f"(role '{role}' asked for one more)"
            )
        entry = self._script[self._index]
        self._index += 1
        tool_calls = [
            ToolCallRequest(
                id=tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                name=tc["name"],
                arguments=tc.get("arguments") or {},
                raw_arguments=json.dumps(tc.get("arguments") or {}),
            )
            for tc in entry.get("tool_calls") or []
        ]
        return LLMResponse(content=entry.get("content") or "", tool_calls=tool_calls)


def build_llm(config: LLMConfig) -> LLMClient:
    if config.provider == "scripted":
        if not config.script_path:
            raise ValueError("llm.provider is 'scripted' but llm.script_path is not set")
        return ScriptedLLM.from_file(config.script_path)
    return OpenAICompatLLM(config)
