"""Structured envelopes exchanged between agents (AGENT_DESIGN.md §3).

Agents communicate through these validated models, never raw transcripts.
All of them serialize cleanly into the LangGraph state and the journal.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# ── Planner ──────────────────────────────────────────────────────────────────


class PlanTask(BaseModel):
    """One unit of work for the Coding Agent."""

    id: str
    intent: str = Field(description="What to change and why, self-contained")
    files_hint: list[str] = Field(default_factory=list)
    check: str = Field(default="", description="How this task is verified")
    kind: Literal["feature", "fix", "test", "docs", "refactor", "chore"] = "feature"


class Plan(BaseModel):
    """Planner output — `Plan v1` envelope."""

    goal: str
    assumptions: list[str] = Field(default_factory=list)
    tasks: list[PlanTask] = Field(min_length=1)
    risks: list[str] = Field(default_factory=list)

    @field_validator("tasks")
    @classmethod
    def _unique_ids(cls, tasks: list[PlanTask]) -> list[PlanTask]:
        seen: set[str] = set()
        for task in tasks:
            if task.id in seen:
                raise ValueError(f"duplicate task id: {task.id}")
            seen.add(task.id)
        return tasks


# ── Coder ────────────────────────────────────────────────────────────────────


class TaskResult(BaseModel):
    """Coding Agent output for one plan task — `TaskResult v1`."""

    task_id: str
    status: Literal["done", "failed"]
    summary: str
    files_changed: list[str] = Field(default_factory=list)
    turns_used: int = 0


# ── Validator ────────────────────────────────────────────────────────────────


class CheckResult(BaseModel):
    name: str
    command: str
    ok: bool
    exit_code: int | None = None
    duration_ms: int = 0
    output_tail: str = ""


class ValidationReport(BaseModel):
    """Validation Agent output — `Verification v1`."""

    passed: bool
    checks: list[CheckResult] = Field(default_factory=list)
    summary: str = ""

    def failure_evidence(self, max_chars: int = 4000) -> str:
        """Concise failing-check evidence for a fix task / the report."""
        parts = []
        for check in self.checks:
            if not check.ok:
                parts.append(
                    f"check '{check.name}' failed"
                    f" (command: {check.command}, exit code: {check.exit_code}):\n"
                    f"{check.output_tail}"
                )
        text = "\n\n".join(parts)
        return text[-max_chars:] if len(text) > max_chars else text


# ── Git ──────────────────────────────────────────────────────────────────────


class CommitInfo(BaseModel):
    """Git Agent output — `Delivery v1` (local-first)."""

    sha: str = ""
    message: str = ""
    branch: str = ""
    files_committed: int = 0
    pushed: bool = False
    push_error: str | None = None


# ── Run report ───────────────────────────────────────────────────────────────


class RunReport(BaseModel):
    run_id: str
    status: Literal["completed", "failed"]
    request: str
    repo_path: str
    workspace_path: str
    branch: str
    error: str | None = None
    plan: Plan | None = None
    task_results: list[TaskResult] = Field(default_factory=list)
    validation: ValidationReport | None = None
    commit: CommitInfo | None = None
    journal_path: str = ""
    fix_attempts: int = 0


# ── JSON extraction (LLM structured output) ─────────────────────────────────

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json_object(text: str) -> dict:
    """Pull the first JSON object out of an LLM reply.

    Handles plain JSON, fenced ```json blocks, and prose-wrapped objects.
    Raises ``ValueError`` when no parseable object is found.
    """
    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
    candidates.extend(m.group(1).strip() for m in _FENCE_RE.finditer(text))
    # Last resort: outermost braces span.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("no JSON object found in model output")
