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


# ── Browser QA (Phase 3) ─────────────────────────────────────────────────────


class BrowserStepResult(BaseModel):
    """One executed workflow step."""

    index: int
    action: str
    detail: str = ""
    ok: bool
    error: str | None = None
    duration_ms: int = 0


class BrowserWorkflowResult(BaseModel):
    """One executed user workflow (login, create-customer, ...).

    ``passed`` requires BOTH: every step succeeded (including expect_*
    verifications) AND zero console/page errors — per the Phase 3 rule that
    validation fails on browser test failure, console errors, or workflow
    verification failure.
    """

    name: str
    passed: bool
    steps_passed: bool
    steps: list[BrowserStepResult] = Field(default_factory=list)
    failed_step: str | None = None
    console_errors: list[str] = Field(default_factory=list)
    page_errors: list[str] = Field(default_factory=list)
    screenshots: list[str] = Field(default_factory=list)
    duration_ms: int = 0


class BrowserQAReport(BaseModel):
    """Browser QA Agent output — the validation report for the UI layer."""

    enabled: bool
    passed: bool
    skipped: bool = False
    workflows: list[BrowserWorkflowResult] = Field(default_factory=list)
    app_url: str = ""
    app_log_tail: str = ""
    error: str | None = None  # setup/launch failure (browser missing, app dead)
    summary: str = ""


class ValidationReport(BaseModel):
    """Validation Agent output — `Verification v1`."""

    passed: bool
    checks: list[CheckResult] = Field(default_factory=list)
    summary: str = ""
    #: Browser QA stage outcome (None when the stage never ran/not configured).
    browser: BrowserQAReport | None = None

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


# ── Self-healing: RCA + Debug Agent envelopes (Phase 2) ─────────────────────

ErrorCategory = Literal[
    "syntax", "import", "assertion", "exception", "timeout",
    "environment", "lint", "build", "browser", "unknown",
]


class ErrorAnalysis(BaseModel):
    """One failing check, categorized by the deterministic RCA engine."""

    check_name: str
    category: ErrorCategory
    exception: str = ""
    message: str = ""
    locations: list[str] = Field(default_factory=list)
    signature: str
    suggested_strategy: str = ""
    evidence: str = ""


class FixStrategy(BaseModel):
    """How the diagnosed root cause will be repaired."""

    approach: str
    steps: list[str] = Field(min_length=1)
    files_to_change: list[str] = Field(default_factory=list)
    risk: Literal["low", "medium", "high"] = "low"


class DebugReport(BaseModel):
    """Debug Agent output — `DebugReport v1` (structured debugging report).

    The contract enforces root-cause discipline: the report must name the
    cause, justify why it is the cause rather than the symptom, and carry a
    concrete fix strategy. The Debug Agent cannot edit files — repair is the
    Coding Agent's job, driven by this report.
    """

    root_cause: str
    category: ErrorCategory
    confidence: Literal["high", "medium", "low"]
    why_root_cause: str = Field(
        default="",
        description="Why this is the origin of the failure, not a symptom",
    )
    evidence: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    fix_strategy: FixStrategy


class HealingIteration(BaseModel):
    """Observability record for one DEBUG → FIX → REVALIDATE cycle."""

    iteration: int
    error_signature: str = ""
    categories: list[str] = Field(default_factory=list)
    root_cause: str = ""
    confidence: str = ""
    approach: str = ""
    fix_task_id: str = ""
    fix_status: str = ""
    revalidation_passed: bool = False


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
    healing: list[HealingIteration] = Field(default_factory=list)
    iterations_used: int = 0


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
