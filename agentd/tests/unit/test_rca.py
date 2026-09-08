"""Root Cause Analysis Engine — categorization, signatures, stall detection."""

from agentd.rca import RcaEngine
from agentd.schemas import CheckResult, ValidationReport

PYTEST_ASSERTION = """\
============================= test session starts ==============================
FAILED test_calc.py::test_add - AssertionError: assert 6 == 5
1 failed in 0.02s
"""

TRACEBACK_KEYERROR = """\
Traceback (most recent call last):
  File "app.py", line 10, in <module>
    main()
  File "lib/util.py", line 7, in main
    return data["x"]
KeyError: 'x'
"""

SYNTAX_ERROR = """\
  File "broken.py", line 3
    def add(a, b:
                 ^
SyntaxError: '(' was never closed
"""

IMPORT_ERROR = """\
Traceback (most recent call last):
  File "main.py", line 1, in <module>
    import requestz
ModuleNotFoundError: No module named 'requestz'
"""

RUFF_OUTPUT = "app.py:3:1: F401 'os' imported but unused\nFound 1 error.\n"

MAKE_OUTPUT = "make: *** [all] Error 2\n"


def _report(*checks: CheckResult) -> ValidationReport:
    return ValidationReport(passed=all(c.ok for c in checks), checks=list(checks))


def _failing(name: str, output: str, exit_code: int | None = 1) -> CheckResult:
    return CheckResult(name=name, command="cmd", ok=False, exit_code=exit_code,
                       output_tail=output)


def analyze_one(name: str, output: str, exit_code: int | None = 1):
    engine = RcaEngine()
    analyses = engine.analyze(_report(_failing(name, output, exit_code)))
    assert len(analyses) == 1
    return analyses[0]


# ── categorization ────────────────────────────────────────────────────────────


def test_assertion_categorized():
    analysis = analyze_one("test[0]", PYTEST_ASSERTION)
    assert analysis.category == "assertion"
    assert analysis.exception == "AssertionError"
    assert "assert 6 == 5" in analysis.message
    assert "test_calc.py" in analysis.locations


def test_runtime_exception_categorized_with_locations():
    analysis = analyze_one("test[0]", TRACEBACK_KEYERROR)
    assert analysis.category == "exception"
    assert analysis.exception == "KeyError"
    assert analysis.locations == ["app.py:10", "lib/util.py:7"]


def test_syntax_error_categorized():
    analysis = analyze_one("test[0]", SYNTAX_ERROR)
    assert analysis.category == "syntax"
    assert "broken.py:3" in analysis.locations


def test_import_error_categorized():
    analysis = analyze_one("test[0]", IMPORT_ERROR)
    assert analysis.category == "import"
    assert analysis.exception == "ModuleNotFoundError"


def test_timeout_categorized_by_exit_code_none():
    analysis = analyze_one("test[0]", "partial output...", exit_code=None)
    assert analysis.category == "timeout"


def test_environment_categorized_by_exit_127():
    analysis = analyze_one("test[0]", "sh: 1: pytst: not found", exit_code=127)
    assert analysis.category == "environment"


def test_environment_categorized_by_content():
    analysis = analyze_one("test[0]", "bash: cargo: command not found", exit_code=1)
    assert analysis.category == "environment"


def test_lint_fallback_by_check_name():
    analysis = analyze_one("lint[0]", RUFF_OUTPUT)
    assert analysis.category == "lint"
    assert "app.py:3" in analysis.locations


def test_build_fallback_by_check_name():
    analysis = analyze_one("build[0]", MAKE_OUTPUT)
    assert analysis.category == "build"


def test_unknown_category():
    analysis = analyze_one("test[0]", "something odd happened", exit_code=2)
    assert analysis.category == "unknown"


def test_every_category_has_a_strategy_seed():
    for output, exit_code in [
        (PYTEST_ASSERTION, 1), (TRACEBACK_KEYERROR, 1), (SYNTAX_ERROR, 1),
        (IMPORT_ERROR, 1), ("x", None), ("nope", 127), ("odd", 2),
    ]:
        analysis = analyze_one("test[0]", output, exit_code)
        assert analysis.suggested_strategy


def test_passing_checks_produce_no_analysis():
    engine = RcaEngine()
    ok = CheckResult(name="test[0]", command="cmd", ok=True, exit_code=0)
    assert engine.analyze(_report(ok)) == []


def test_multiple_failures_analyzed_independently():
    engine = RcaEngine()
    analyses = engine.analyze(
        _report(
            _failing("lint[0]", RUFF_OUTPUT),
            _failing("test[0]", PYTEST_ASSERTION),
        )
    )
    assert {a.category for a in analyses} == {"lint", "assertion"}


# ── signatures & stall detection ─────────────────────────────────────────────


def test_signature_stability():
    a1 = analyze_one("test[0]", PYTEST_ASSERTION)
    a2 = analyze_one("test[0]", PYTEST_ASSERTION)
    assert a1.signature == a2.signature


def test_combined_signature_is_order_independent():
    engine = RcaEngine()
    checks = [_failing("lint[0]", RUFF_OUTPUT), _failing("test[0]", PYTEST_ASSERTION)]
    sig_a = engine.combined_signature(engine.analyze(_report(*checks)))
    sig_b = engine.combined_signature(engine.analyze(_report(*reversed(checks))))
    assert sig_a == sig_b


def test_stall_detection():
    engine = RcaEngine(stall_threshold=3)
    assert not engine.is_stalled([])
    assert not engine.is_stalled(["s"])
    assert not engine.is_stalled(["s", "s"])
    assert engine.is_stalled(["s", "s", "s"])
    assert engine.is_stalled(["other", "s", "s", "s"])
    assert not engine.is_stalled(["s", "other", "s"])
    assert not engine.is_stalled(["a", "s", "s"])


def test_stall_threshold_floor_is_two():
    engine = RcaEngine(stall_threshold=0)
    assert not engine.is_stalled(["s"])
    assert engine.is_stalled(["s", "s"])


# ── prompt rendering ──────────────────────────────────────────────────────────


def test_render_for_prompt():
    engine = RcaEngine()
    analyses = engine.analyze(_report(_failing("test[0]", TRACEBACK_KEYERROR)))
    text = RcaEngine.render_for_prompt(analyses)
    assert "category: exception" in text
    assert "KeyError" in text
    assert "app.py:10" in text
    assert "suggested strategy:" in text
    assert RcaEngine.render_for_prompt([]) == "(no failing checks)"
