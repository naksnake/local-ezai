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
    """Planner output — `Plan v1` envelope.

    Empty task lists are structurally allowed for synthetic plans (the
    ``fix``/``commit`` pipelines enter the workflow at VALIDATE with no
    implementation tasks); the Planner Agent itself rejects empty plans.
    """

    goal: str
    assumptions: list[str] = Field(default_factory=list)
    tasks: list[PlanTask] = Field(default_factory=list)
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


# ── Reviewer (Phase 5) ───────────────────────────────────────────────────────


#: Concern dimensions the Reviewer classifies findings into (Phase H2):
#: security, architecture, and maintainability are explicitly mandated.
ReviewCategory = Literal[
    "security", "architecture", "maintainability",
    "correctness", "performance", "testing", "style", "other",
]


class ReviewFinding(BaseModel):
    severity: Literal["high", "medium", "low"]
    category: ReviewCategory = "other"
    file: str = ""
    line: int | None = None
    issue: str
    suggestion: str = ""


class ReviewReport(BaseModel):
    """Reviewer Agent output — `Review v1` (adversarial pass over a diff)."""

    verdict: Literal["approve", "request_changes"]
    summary: str = ""
    findings: list[ReviewFinding] = Field(default_factory=list)


# ── Sprint (Phase 5/6) ───────────────────────────────────────────────────────


class SprintTaskSpec(BaseModel):
    """One task of an analyzed sprint plan (Sprint Agent output)."""

    id: str
    title: str
    description: str = Field(
        description="Self-contained implementation brief incl. acceptance "
                    "criteria, tests, and documentation expectations"
    )
    depends_on: list[str] = Field(default_factory=list)


class SprintPlan(BaseModel):
    """Sprint Agent output — requirement analysis + task breakdown +
    dependency graph (`SprintPlan v1`). DAG validity (unique ids, known
    dependencies, no cycles) is enforced by code, not by trust."""

    goal: str
    requirements: list[str] = Field(default_factory=list)
    tasks: list[SprintTaskSpec] = Field(min_length=1)
    notes: str = ""


class SprintTaskResult(BaseModel):
    index: int
    task: str
    run_id: str
    status: Literal["completed", "failed", "skipped"]
    task_id: str = ""
    wave: int = 0
    depends_on: list[str] = Field(default_factory=list)
    commit_sha: str = ""
    merged: bool = True
    error: str | None = None
    iterations_used: int = 0


class SprintReport(BaseModel):
    sprint_id: str
    status: Literal["completed", "failed"]
    branch: str
    workspace_path: str
    spec_file: str = ""
    plan: SprintPlan | None = None
    waves: int = 0
    report_doc: str = ""
    tasks: list[SprintTaskResult] = Field(default_factory=list)

    @property
    def completed_count(self) -> int:
        return sum(1 for t in self.tasks if t.status == "completed")


# ── Documentation Agent (Phase 7) ────────────────────────────────────────────


class DocsResult(BaseModel):
    """Documentation Agent output."""

    status: Literal["done", "failed"]
    summary: str = ""
    files_written: list[str] = Field(default_factory=list)


# ── Evolution workflow (Phase 7) ─────────────────────────────────────────────


class Improvement(BaseModel):
    id: str
    title: str
    description: str = Field(
        description="Self-contained implementation brief incl. acceptance "
                    "criteria and tests")
    rationale: str = ""


class EvolutionProposal(BaseModel):
    """Evolution Agent output — `EvolutionProposal v1`.

    Analyze history → analyze failures → identify bottlenecks → propose."""

    title: str
    history_summary: str = ""
    failure_patterns: list[str] = Field(default_factory=list)
    bottlenecks: list[str] = Field(default_factory=list)
    improvements: list[Improvement] = Field(min_length=1)
    notes: str = ""


class BenchmarkResult(BaseModel):
    """Timed validation snapshot (before/after an evolution)."""

    passed: bool = False
    checks: int = 0
    duration_seconds: float = 0.0


class PullRequestResult(BaseModel):
    created: bool = False
    url: str = ""
    bundle_path: str = ""
    note: str = ""


class EvolutionReport(BaseModel):
    evolution_id: str
    status: Literal["completed", "failed"]
    branch: str
    workspace_path: str
    proposal: EvolutionProposal | None = None
    tasks: list[SprintTaskResult] = Field(default_factory=list)
    benchmark_before: BenchmarkResult | None = None
    benchmark_after: BenchmarkResult | None = None
    release_notes_updated: bool = False
    pull_request: PullRequestResult | None = None
    error: str | None = None


# ── Model governance (Phase 7, ADR-020) ─────────────────────────────────────


class ModelProbeResult(BaseModel):
    role: str
    model: str
    fallbacks: list[str] = Field(default_factory=list)
    ok: bool
    latency_ms: int = 0
    expects_json: bool = False
    error: str | None = None


class RunMetrics(BaseModel):
    """Aggregated quality metrics from run history (Phase H6 dashboard).

    Rates are None when no run exercised that dimension yet."""

    runs_total: int = 0
    runs_completed: int = 0
    #: plans whose every task finished "done" / runs that produced a plan
    planning_accuracy: float | None = None
    #: completed runs / all runs (the pipeline's end-to-end success)
    coding_success_rate: float | None = None
    #: green validations / runs that reached validation
    validation_pass_rate: float | None = None
    #: runs that entered self-healing AND completed / runs that entered it
    debugging_success_rate: float | None = None
    #: approve verdicts / runs that reached the reviewer gate
    review_approval_rate: float | None = None
    avg_heal_iterations: float | None = None
    #: wall-clock per run, from the journal's first/last event
    avg_run_seconds: float | None = None


class ModelEvalReport(BaseModel):
    evaluated_at: str
    base_url: str = ""
    passed: bool
    results: list[ModelProbeResult] = Field(default_factory=list)
    #: Run-history quality metrics (Phase H6).
    metrics: RunMetrics | None = None
    #: Compact summaries of previous evaluations (trend data, capped).
    history: list[dict] = Field(default_factory=list)


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
    #: Reviewer-gate outcome (Phase H2, ADR-022) — None when the gate is
    #: disabled or was never reached.
    review: ReviewReport | None = None
    commit: CommitInfo | None = None
    journal_path: str = ""
    healing: list[HealingIteration] = Field(default_factory=list)
    iterations_used: int = 0
    #: Which model actually served each LLM role in this run, fallback-aware
    #: (Phase H5 model explainability; empty for deterministic-only runs).
    models_used: dict[str, str] = Field(default_factory=dict)


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
