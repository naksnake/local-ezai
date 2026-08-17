"""Root Cause Analysis Engine.

Deterministic core of the self-healing workflow (Phase 2). Given a failing
:class:`~agentd.schemas.ValidationReport`, the engine:

1. **Categorizes** every failing check into a stable error category
   (syntax / import / assertion / exception / timeout / environment /
   lint / build / unknown) using ordered regex rules — never an LLM.
2. Extracts an **error signature** (category + exception + message head)
   and **source locations** (file:line) from tracebacks, pytest output and
   compiler-style diagnostics.
3. Suggests a **fix strategy seed** per category, which the Debug Agent
   refines into a concrete, root-cause-directed plan.
4. Detects **stalls**: when the identical combined signature persists for
   ``stall_threshold`` consecutive validations, the loop is patching
   symptoms, not the cause — the workflow aborts instead of burning its
   iteration budget.

Everything the engine produces is journaled (``RCA_REPORT``) and embedded
in the structured debugging report, satisfying the observability and
error-categorization requirements deterministically.
"""

from __future__ import annotations

import re

from agentd.schemas import ErrorAnalysis, ValidationReport

# Ordered: first match wins. Content rules run before name-based fallbacks.
_CONTENT_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("syntax", re.compile(r"\b(?:SyntaxError|IndentationError|TabError)\b")),
    ("import", re.compile(r"\b(?:ModuleNotFoundError|ImportError)\b")),
    ("assertion", re.compile(
        r"\bAssertionError\b|^E?\s*assert\b|\bFAILED\b.*::", re.MULTILINE)),
    ("environment", re.compile(
        r"command not found|No such file or directory: '?\w+"
        r"|not recognized as an internal", re.IGNORECASE)),
    ("exception", re.compile(
        r"Traceback \(most recent call last\)|\b[A-Z]\w*(?:Error|Exception)\b")),
]

_EXCEPTION_RE = re.compile(r"\b([A-Z]\w*(?:Error|Exception))\b(?::\s*(.*))?")
_TRACEBACK_LOC_RE = re.compile(r'File "([^"]+)", line (\d+)')
_DIAGNOSTIC_LOC_RE = re.compile(r"^([\w./\\-]+\.\w{1,4}):(\d+)", re.MULTILINE)
_PYTEST_NODE_RE = re.compile(r"([\w./\\-]+\.py)::")

_STRATEGY_SEEDS: dict[str, str] = {
    "syntax": (
        "Fix the syntax error exactly at the reported location; "
        "do not modify surrounding logic."
    ),
    "import": (
        "Verify the imported module/name actually exists in the workspace or "
        "environment; correct the import path or the module itself. Never "
        "delete the usage to silence the error."
    ),
    "assertion": (
        "Read both the failing test and the code under test. Decide which one "
        "contradicts the stated goal, and fix the code unless the test "
        "provably encodes an outdated expectation. Never weaken or delete the "
        "test to make it pass."
    ),
    "exception": (
        "Reproduce with the smallest command, read the traceback bottom-up, "
        "and fix where the bad value originates — not where it crashes."
    ),
    "timeout": (
        "Find what blocks (infinite loop, unbounded wait, external call). Fix "
        "the cause of the hang; do not simply raise the timeout."
    ),
    "environment": (
        "A required command or file is missing from the environment. Adjust "
        "the validation command or project configuration; code changes will "
        "not fix a missing tool."
    ),
    "lint": "Apply the specific fix the linter names at each reported location.",
    "build": (
        "Fix the FIRST build/compile error; subsequent errors are usually "
        "cascades of the first."
    ),
    "browser": (
        "A real-browser workflow failed. Read the failed step, the console/"
        "page errors, and the app log tail; fix the application code (route, "
        "handler, template, script) so the workflow's expectations hold. "
        "Never change the workflow spec to match broken behavior, and never "
        "silence console errors — remove their cause."
    ),
    "unknown": (
        "Reproduce the failing command, capture its full output, and identify "
        "the first point where behavior diverges from the expectation."
    ),
}


class RcaEngine:
    """Deterministic error categorization, signatures, and stall detection."""

    def __init__(self, stall_threshold: int = 3) -> None:
        self.stall_threshold = max(2, int(stall_threshold))

    # ── categorization ───────────────────────────────────────────────────────

    def analyze(self, report: ValidationReport) -> list[ErrorAnalysis]:
        """One ErrorAnalysis per failing check (empty list if none failed)."""
        analyses: list[ErrorAnalysis] = []
        for check in report.checks:
            if check.ok:
                continue
            category = self._categorize(check.name, check.output_tail,
                                         check.exit_code)
            exception, message = self._exception_and_message(check.output_tail)
            locations = self._locations(check.output_tail)
            analyses.append(
                ErrorAnalysis(
                    check_name=check.name,
                    category=category,
                    exception=exception,
                    message=message,
                    locations=locations,
                    signature=self._signature(check.name, category, exception,
                                              message),
                    suggested_strategy=_STRATEGY_SEEDS[category],
                    evidence=check.output_tail[-1500:],
                )
            )
        return analyses

    @staticmethod
    def _categorize(check_name: str, output: str, exit_code: int | None) -> str:
        # Browser checks are categorized by pipeline stage, not by content —
        # their output may quote arbitrary app tracebacks/console text.
        if check_name.startswith("browser"):
            return "browser"
        if exit_code is None:
            return "timeout"
        if exit_code == 127:
            return "environment"
        for category, pattern in _CONTENT_RULES:
            if pattern.search(output):
                return category
        if check_name.startswith("lint"):
            return "lint"
        if check_name.startswith("build"):
            return "build"
        return "unknown"

    @staticmethod
    def _exception_and_message(output: str) -> tuple[str, str]:
        exception, message = "", ""
        for match in _EXCEPTION_RE.finditer(output):
            exception = match.group(1)
            message = (match.group(2) or "").strip()
        return exception, message[:200]

    @staticmethod
    def _locations(output: str, limit: int = 10) -> list[str]:
        seen: list[str] = []

        def _add(loc: str) -> None:
            if loc not in seen and len(seen) < limit:
                seen.append(loc)

        for path, line in _TRACEBACK_LOC_RE.findall(output):
            _add(f"{path}:{line}")
        for path, line in _DIAGNOSTIC_LOC_RE.findall(output):
            _add(f"{path}:{line}")
        for path in _PYTEST_NODE_RE.findall(output):
            _add(path)
        return seen

    @staticmethod
    def _signature(check_name: str, category: str, exception: str,
                   message: str) -> str:
        return f"{check_name}|{category}|{exception}|{message[:80]}"

    # ── signatures & stall detection ────────────────────────────────────────

    @staticmethod
    def combined_signature(analyses: list[ErrorAnalysis]) -> str:
        """Order-independent signature of one whole failed validation."""
        return " ;; ".join(sorted(a.signature for a in analyses))

    def is_stalled(self, signature_history: list[str]) -> bool:
        """True when the last ``stall_threshold`` validations failed with the
        identical combined signature — fixes are not touching the cause."""
        n = self.stall_threshold
        if len(signature_history) < n:
            return False
        tail = signature_history[-n:]
        return len(set(tail)) == 1

    # ── prompt rendering ─────────────────────────────────────────────────────

    @staticmethod
    def render_for_prompt(analyses: list[ErrorAnalysis]) -> str:
        """Compact, model-facing rendering of the analysis."""
        if not analyses:
            return "(no failing checks)"
        blocks = []
        for a in analyses:
            lines = [f"- check: {a.check_name}",
                     f"  category: {a.category}"]
            if a.exception:
                lines.append(f"  exception: {a.exception}: {a.message}")
            if a.locations:
                lines.append(f"  locations: {', '.join(a.locations)}")
            lines.append(f"  suggested strategy: {a.suggested_strategy}")
            blocks.append("\n".join(lines))
        return "\n".join(blocks)
