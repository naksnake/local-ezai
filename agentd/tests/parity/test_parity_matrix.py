"""The parity matrix as a release gate (PR-24, P6; CLI_AND_WEBUI_STRATEGY §3
rows × §7 "execute via CLI-direct, CLI-connected, and control-plane API,
and assert identical state transitions and audit records").

Every row of the §3 table has a test here (a tripwire parses the table);
each executes its scenario on three identical worlds, one per surface, and
asserts equal response bodies, equal declarative state and equal operation
audit — daemon-side annotations normalised explicitly (harness.py). Cells
the matrix leaves deliberately empty are asserted as absent, and the chat
client's ceiling is checked per row against the daemon's policy table.

Offline. `make swe-parity` runs this package alone; it is part of
`make release-gate` and of the full suite."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agentd.config import ControlConfig
from agentd.control.app import create_app
from agentd.control.policy import CHAT_OPS_CLIENT, client_may
from tests.conftest import git, happy_path_script, planner_response
from tests.parity.harness import (
    API,
    CONNECTED,
    DIRECT,
    HUMAN,
    REPO_ROOT,
    SURFACES,
    Arena,
    Step,
    all_equal,
    audit,
    bodies,
    journal_types,
    parse_json_output,
    scrub,
    state,
    wait_terminal,
)

STRATEGY_DOC = REPO_ROOT / "docs" / "CLI_AND_WEBUI_STRATEGY.md"
MEMORY_TEXT = "Always run the project's own tests before committing"
TASK = "fix the add bug"


@pytest.fixture
def arena(tmp_path: Path, monkeypatch):
    arena = Arena(tmp_path, monkeypatch)
    yield arena
    arena.close()


def compare(arena: Arena, steps: list[Step], surfaces: tuple[str, ...] = SURFACES):
    """Run ``steps`` on every surface and assert the three equivalences."""
    worlds = arena.build(*surfaces)
    outcomes = arena.run_scenario(steps, surfaces)
    all_equal(bodies(outcomes, worlds), "response bodies")
    all_equal({s: state(w) for s, w in worlds.items()}, "declarative state")
    views = {s: audit(w) for s, w in worlds.items()}
    all_equal({s: v.operations for s, v in views.items()}, "operation audit")
    if DIRECT in views:
        assert views[DIRECT].daemon == []           # in-process: nothing acts on its behalf
    if CONNECTED in views and API in views:          # both daemon surfaces: same wrapper events
        all_equal({s: views[s].daemon for s in (CONNECTED, API)}, "daemon-side audit")
    return worlds, outcomes, views


# ── the matrix rows as data ──────────────────────────────────────────────────

#: §3 row label → the control-plane operations it maps to and whether the
#: chat-ops client (OPENWEBUI_INTEGRATION §5 ceiling; PR-15 policy) may
#: perform each. Reads are always allowed (🔍); ❌ cells are refused.
CHAT_CEILING: dict[str, dict[str, bool]] = {
    "Start run / sprint / fix / evolve": {"run_start": True, "run_cancel": False},
    "Plan preview": {"run_start": True},
    "Run reports / journals / exec audit": {"runs_list": True, "run_get": True,
                                            "run_report": True, "run_journal": True,
                                            "audit_tail": True},
    "Model install / benchmark / retire": {"model_install": False, "model_benchmark": False,
                                           "model_retire": False, "model_uninstall": False},
    "Model activate / upgrade (approval flow)": {"model_activate": False, "model_upgrade": False,
                                                 "governance_approve": False},
    "Rollback (generation)": {"generation_rollback": False},
    "Explain routing / models / explain-run": {"role_explain": True, "catalog_list": True,
                                               "catalog_recommend": True},
    "Governance queue (evolution PRs, activations)": {"governance_list": True,
                                                      "governance_show": True,
                                                      "governance_approve": False,
                                                      "governance_reject": False},
    # the matrix's "✅ via existing memory tool" is the run pipeline's own
    # memory tool; the control plane's curated add stays a human act
    "Memory browse / add rule": {"project_memory": True, "project_memory_add": False},
    "Health / bench / prune": {"aggregated_health": True, "models_list": True,
                               "model_benchmark": False},
    "Project registration (chat-ops allowlist)": {"projects_list": True, "project_add": False,
                                                  "project_remove": False},
    "Stack install / start / stop": {},   # no operation exists — for anyone
}

#: §3 row label → the test that proves it (the docs tripwire checks both ways).
ROW_TESTS: dict[str, str] = {
    "Start run / sprint / fix / evolve": "test_row_start_run_and_its_report",
    "Plan preview": "test_row_plan_preview",
    "Run reports / journals / exec audit": "test_row_start_run_and_its_report",
    "Model install / benchmark / retire": "test_row_model_lifecycle",
    "Model activate / upgrade (approval flow)": "test_row_approval_flow",
    "Rollback (generation)": "test_row_rollback",
    "Explain routing / models / explain-run": "test_row_explain",
    "Governance queue (evolution PRs, activations)": "test_row_governance_queue",
    "Memory browse / add rule": "test_row_memory",
    "Health / bench / prune": "test_row_health_and_bench",
    "Project registration (chat-ops allowlist)": "test_row_project_registration",
    "Stack install / start / stop": "test_row_stack_lifecycle_is_cli_only",
}


# ── rows with all three surfaces ─────────────────────────────────────────────


LIFECYCLE = [
    Step("install", lambda w: ("model", "install", f"gguf:{w.weights}", "--name", "delta-q4"),
         lambda w: ("POST", "/models", {"ref": f"gguf:{w.weights}", "name": "delta-q4"}, None)),
    Step("benchmark", ("model", "benchmark", "delta-q4"),
         ("POST", "/models/delta-q4/benchmark", None, None)),
    Step("activate in an empty group → policy-approved, applied",
         ("model", "activate", "delta-q4", "--group", "spare", "--by", HUMAN),
         ("POST", "/models/delta-q4/activate", {"group": "spare"}, None)),
    Step("retire", ("model", "retire", "delta-q4"),
         ("POST", "/models/delta-q4/retire", None, None)),
    Step("retire a serving model → refused", ("model", "retire", "alpha"),
         ("POST", "/models/alpha/retire", None, None), error=True),
    Step("uninstall", ("model", "uninstall", "delta-q4", "--force"),
         ("DELETE", "/models/delta-q4", None, {"force": "true"})),
    Step("status", ("status",), ("GET", "/models", None, None)),
]


def test_row_model_lifecycle(arena):
    worlds, outcomes, views = compare(arena, LIFECYCLE[:-1])
    for world in worlds.values():
        registry = world.inspector().registry()
        # install → 2, benchmark → 3, activation → 4, retire → 5, uninstall → 6
        assert "delta-q4" not in registry.models and registry.generation == 6
    # install/benchmark/retire/uninstall keep their evidence in the
    # generations; the one governed step (activation) is audited by the queue
    # (and re-renders the artifacts install/benchmark left behind: reconciled)
    assert views[DIRECT].operation_events == ["request.submitted", "request.approved",
                                              "generation.reconciled", "request.applied"]
    assert [e for e in views[API].daemon_events] == [
        "api.model_install", "api.model_benchmark", "api.model_activate", "api.model_retire",
        "api.model_retire", "api.model_uninstall"]
    refused = [o for o in outcomes[API] if o.label.startswith("retire a serving")][0]
    assert refused.status == 409 and refused.body["error"]["code"] == "lifecycle_refused"


APPROVAL = [
    Step("upgrade a serving model → pending", ("model", "upgrade", "alpha", "beta", "--by", HUMAN),
         ("POST", "/models/upgrade", {"old": "alpha", "new": "beta"}, None)),
    Step("show the request", ("governance", "show", "cr-0001"),
         ("GET", "/governance/cr-0001", None, None)),
    Step("approve → applied", ("governance", "approve", "cr-0001", "--reason", "faster", "--by",
                               HUMAN),
         ("POST", "/governance/cr-0001/approve", {"reason": "faster"}, None)),
    Step("explain the coder role", ("model", "explain", "coder"),
         ("GET", "/roles/coder", None, None)),
    Step("explain the pinned reviewer", ("model", "explain", "reviewer"),
         ("GET", "/roles/reviewer", None, None)),
]


def test_row_approval_flow(arena):
    worlds, outcomes, views = compare(arena, APPROVAL)
    for world in worlds.values():
        registry = world.inspector().registry()
        assert registry.generation == 2 and registry.resolve("coder").primary == "beta"
        assert registry.resolve("reviewer").primary == "beta"      # the pin followed
    assert views[DIRECT].operation_events == ["request.submitted", "request.approved",
                                              "request.applied"]
    shown = outcomes[API][1].body["request"]
    assert shown["requested_by"] == f"{HUMAN} via admin-center"   # the console's identity
    assert outcomes[CONNECTED][1].body["request"]["requested_by"] == f"{HUMAN} via cli"
    assert outcomes[DIRECT][1].body["request"]["requested_by"] == HUMAN


ROLLBACK = [
    Step("activate in an empty group → applied", ("model", "activate", "beta", "--group", "spare",
                                                  "--by", HUMAN),
         ("POST", "/models/beta/activate", {"group": "spare"}, None)),
    Step("roll back", ("model", "rollback", "--reason", "too slow", "--by", HUMAN),
         ("POST", "/generations/rollback", {"reason": "too slow"}, None)),
    Step("history", ("model", "history"), ("GET", "/generations", None, None)),
    Step("roll back to an unknown generation → refused",
         ("model", "rollback", "--to-generation", "9", "--by", HUMAN),
         ("POST", "/generations/rollback", {"to_generation": 9}, None), error=True),
]


def test_row_rollback(arena):
    worlds, outcomes, views = compare(arena, ROLLBACK)
    for world in worlds.values():
        registry = world.inspector().registry()
        assert registry.generation == 3 and registry.models["beta"].state == "benchmarked"
    assert views[DIRECT].operation_events[-1] == "generation.rolled_back"
    history = outcomes[API][2].body["generations"]
    assert [g["generation"] for g in history] == [1, 2, 3]


EXPLAIN = [
    Step("explain coder", ("model", "explain", "coder"), ("GET", "/roles/coder", None, None)),
    Step("explain reviewer (pinned)", ("model", "explain", "reviewer"),
         ("GET", "/roles/reviewer", None, None)),
    Step("catalog", ("model", "catalog"), ("GET", "/catalog", None, None)),
    Step("recommendations for coding", ("model", "catalog", "--group", "coding"),
         ("GET", "/catalog/recommendations", None, {"group": "coding"})),
    Step("explain an unknown role → not_found", ("model", "explain", "nobody"),
         ("GET", "/roles/nobody", None, None), error=True),
]


def test_row_explain(arena):
    worlds, outcomes, views = compare(arena, EXPLAIN)
    for view in views.values():                 # reads change nothing and audit nothing
        assert view.operations == [] and view.daemon == []
    for world in worlds.values():
        assert world.inspector().registry().generation == 1
    assert outcomes[API][0].body["primary"] == "alpha"
    assert outcomes[API][-1].body["error"]["code"] == "not_found"


QUEUE = [
    Step("activate into a serving group → pending", ("model", "activate", "beta", "--group",
                                                     "coding", "--by", HUMAN),
         ("POST", "/models/beta/activate", {"group": "coding"}, None)),
    Step("list", ("governance", "list"), ("GET", "/governance", None, None)),
    Step("reject with a reason", ("governance", "reject", "cr-0001", "--reason", "regression",
                                  "--by", HUMAN),
         ("POST", "/governance/cr-0001/reject", {"reason": "regression"}, None)),
    Step("list the rejected", ("governance", "list", "--status", "rejected"),
         ("GET", "/governance", None, {"status": "rejected"})),
    Step("decide twice → refused", ("governance", "approve", "cr-0001", "--by", HUMAN),
         ("POST", "/governance/cr-0001/approve", {}, None), error=True),
    Step("show an unknown request → not_found", ("governance", "show", "cr-9"),
         ("GET", "/governance/cr-9", None, None), error=True),
]


def test_row_governance_queue(arena):
    worlds, outcomes, views = compare(arena, QUEUE)
    for world in worlds.values():
        registry = world.inspector().registry()
        assert registry.generation == 1 and registry.resolve("coder").primary == "alpha"
        [request] = world.inspector().queue.list()
        assert request.status == "rejected" and request.decision.reason == "regression"
    assert views[DIRECT].operation_events == ["request.submitted", "request.rejected"]
    assert outcomes[API][4].body["error"]["code"] == "governance_rule"
    assert outcomes[API][5].body["error"]["code"] == "not_found"


PROJECTS = [
    Step("add", lambda w: ("project", "add", str(w.repo), "--name", "sample"),
         lambda w: ("POST", "/projects", {"path": str(w.repo), "name": "sample"}, None)),
    Step("list", ("project", "list"), ("GET", "/projects", None, None)),
    Step("add twice → refused", lambda w: ("project", "add", str(w.repo), "--name", "sample"),
         lambda w: ("POST", "/projects", {"path": str(w.repo), "name": "sample"}, None),
         error=True),
    Step("remove", ("project", "remove", "sample"),
         ("DELETE", "/projects", None, {"target": "sample"})),
    Step("list (empty)", ("project", "list"), ("GET", "/projects", None, None)),
    Step("remove again → not_found", ("project", "remove", "sample"),
         ("DELETE", "/projects", None, {"target": "sample"}), error=True),
]


def test_row_project_registration(arena):
    worlds, outcomes, views = compare(arena, PROJECTS)
    assert views[DIRECT].operation_events == ["project.added", "project.removed"]
    assert all(r["actor"] == HUMAN for r in views[API].operations)   # `via admin-center` scrubbed
    listed = outcomes[CONNECTED][1].body["projects"]
    assert [p["name"] for p in listed] == ["sample"] and listed[0]["added_by"] == f"{HUMAN} via cli"
    assert outcomes[API][1].body["projects"][0]["added_by"] == f"{HUMAN} via admin-center"
    assert outcomes[DIRECT][1].body["projects"][0]["added_by"] == HUMAN


def test_row_health_and_bench(arena):
    """`status` is the one verb whose JSON names its transport; the API's
    aggregated report wraps the same snapshot. Bench = `model benchmark`."""
    worlds = arena.build(*SURFACES)
    direct = parse_json_output(arena.cli(worlds[DIRECT], ("status",), transport="direct")[1])
    connected = parse_json_output(arena.cli(worlds[CONNECTED], ("status",),
                                            transport="connected")[1])
    report = arena.api(worlds[API], "GET", "/health").json()
    assert direct.pop("transport") == "direct (in-process)"
    assert connected.pop("transport") == f"connected {worlds[CONNECTED].url}"
    assert direct.pop("health") == connected.pop("health") == {
        "engine": True, "router": True, "control": True}
    snapshots = {DIRECT: scrub(direct, worlds[DIRECT].roots),
                 CONNECTED: scrub(connected, worlds[CONNECTED].roots),
                 API: scrub(report["platform"], worlds[API].roots)}
    all_equal(snapshots, "the status snapshot")
    assert report["control"]["contract"] == "1.2.0"
    probed = {s["id"]: s["ok"] for s in report["services"]}
    assert probed["engine"] and probed["router"]     # what the CLI's health line shows
    bench = [Step("benchmark the serving model", ("model", "benchmark", "alpha"),
                  ("POST", "/models/alpha/benchmark", None, None))]
    _, outcomes, _ = compare(arena, bench)
    assert outcomes[API][0].body["tokens_per_s"] == 15.5


# ── rows whose CLI cell is repository work (never through a daemon) ──────────


def test_row_start_run_and_its_report(arena):
    """Row 1 + row 3: the same scripted run in-process (`local-ezai <repo>
    run`) and through the daemon (`POST /v1/runs`) produce the same report
    and the same journal; the CLI's repo work never probes the daemon (the
    offline-first rule, CLI_AND_WEBUI §2), so the connected cell is the API's."""
    worlds = arena.build(DIRECT, API, script=happy_path_script())
    register = Step("register", lambda w: ("project", "add", str(w.repo), "--name", "sample"),
                    lambda w: ("POST", "/projects", {"path": str(w.repo), "name": "sample"}, None))
    arena.run_scenario([register], (DIRECT, API))

    code, out = arena.cli(worlds[DIRECT], ("run", TASK), transport=None, repo=True)
    assert code == 0, out
    direct_report = parse_json_output(out)
    assert worlds[DIRECT].factory_calls == []          # never asked a daemon

    started = arena.api(worlds[API], "POST", "/runs",
                        {"kind": "run", "project": "sample", "task": TASK})
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    final = wait_terminal(arena, worlds[API], run_id)
    assert final["status"] == "completed", final
    api_report = arena.api(worlds[API], "GET", f"/runs/{run_id}/report").json()["report"]
    api_journal = arena.api(worlds[API], "GET", f"/runs/{run_id}/journal",
                            params={"tail": 1000}).json()

    assert direct_report["status"] == api_report["status"] == "completed"
    all_equal({DIRECT: scrub(direct_report, worlds[DIRECT].roots),
               API: scrub(api_report, worlds[API].roots)}, "the run report")
    all_equal({DIRECT: journal_types(Path(direct_report["journal_path"])),
               API: [e["type"] for e in api_journal["events"]]}, "the journal")
    assert api_journal["total"] == len(api_journal["events"])
    for world in worlds.values():                       # the fix landed on a run branch, unpushed
        # the working tree is untouched; the run's memory lives in the
        # repository's own .agent/ (ADR-017) on both surfaces
        assert git(world.repo, "status", "--porcelain").splitlines() == ["?? .agent/"]
        branches = git(world.repo, "branch", "--list", "swe/*")
        assert branches.strip(), branches
    all_equal({s: state(w) for s, w in worlds.items()}, "declarative state")
    # run supervision is the daemon's annotation, not the operation's audit
    views = {s: audit(w) for s, w in worlds.items()}
    assert views[DIRECT].operations == views[API].operations == [
        {"event": "project.added", "actor": HUMAN, "request_id": "", "generation": None,
         "details": {"name": "sample", "path": "<world>/sample"}}]
    assert views[DIRECT].daemon == []
    assert views[API].daemon_events == ["api.project_add", "run.submitted", "api.run_start",
                                        "run.finished"]


def test_row_plan_preview(arena):
    worlds = arena.build(DIRECT, API, script=[planner_response()])
    arena.api(worlds[API], "POST", "/projects", {"path": str(worlds[API].repo), "name": "sample"})
    code, out = arena.cli(worlds[DIRECT], ("plan", TASK), transport=None, as_json=False, repo=True)
    assert code == 0, out
    direct_plan = parse_json_output(out)
    assert worlds[DIRECT].factory_calls == []
    run_id = arena.api(worlds[API], "POST", "/runs",
                       {"kind": "plan", "project": "sample", "task": TASK}).json()["run_id"]
    assert wait_terminal(arena, worlds[API], run_id)["status"] == "completed"
    api_plan = arena.api(worlds[API], "GET", f"/runs/{run_id}/report").json()["report"]
    assert direct_plan == api_plan and direct_plan["tasks"][0]["id"] == "T1"
    [direct_journal] = (worlds[DIRECT].home / "runs").glob("*/journal.jsonl")
    api_journal = arena.api(worlds[API], "GET", f"/runs/{run_id}/journal",
                            params={"tail": 1000}).json()["events"]
    assert journal_types(direct_journal) == [e["type"] for e in api_journal]
    for world in worlds.values():                       # a plan leaves zero traces
        assert git(world.repo, "status", "--porcelain") == ""
        assert git(world.repo, "branch", "--list", "swe/*").strip() == ""


def test_row_memory(arena):
    """The CLI's memory verb is repository work on the origin repo's own
    store (ADR-017); the daemon serves the same store for a registered
    project (contract 1.1.0, PR-19: "no connected twin needed — the store is
    the same"). State is identical; the platform audit records the daemon's
    curated add because a forwarded human acted through it — the accepted
    asymmetry, pinned here."""
    worlds = arena.build(DIRECT, API)
    direct, api = worlds[DIRECT], worlds[API]
    code, out = arena.cli(direct, ("memory", "--add", MEMORY_TEXT, "--kind", "project_rule"),
                          transport=None, as_json=False, repo=True)
    assert code == 0 and "remembered #1 [project_rule]" in out
    code, out = arena.cli(direct, ("memory",), transport=None, as_json=False, repo=True)
    assert code == 0 and MEMORY_TEXT[:60] in out and "1 total memories" in out
    assert direct.factory_calls == []                   # repo work never probes the daemon

    arena.api(api, "POST", "/projects", {"path": str(api.repo), "name": "sample"})
    added = arena.api(api, "POST", "/projects/sample/memory",
                      {"kind": "project_rule", "text": MEMORY_TEXT})
    assert added.status_code == 200 and added.json()["id"] == 1, added.text
    browsed = arena.api(api, "GET", "/projects/sample/memory").json()
    assert browsed["total"] == 1 and browsed["records"][0]["content"] == MEMORY_TEXT
    assert browsed["records"][0]["run_id"] == "manual"

    states = {s: state(w) for s, w in worlds.items()}
    assert states[DIRECT]["memory"] == states[API]["memory"] == [
        {"kind": "project_rule", "title": MEMORY_TEXT[:60], "content": MEMORY_TEXT,
         "run_id": "manual"}]
    assert states[DIRECT]["registry"] == states[API]["registry"]
    assert audit(direct).operations == []                # the store is the record
    assert audit(api).operation_events == ["project.added", "memory.added"]
    assert audit(api).operations[1]["actor"] == HUMAN


# ── the row that is CLI-only by design ───────────────────────────────────────


def test_row_stack_lifecycle_is_cli_only(arena):
    """`up`/`down` act on this host's compose files: host-only verbs, always
    direct even when connected is requested; the contract has no such
    operation (CLI_AND_WEBUI §4 asymmetry 2 — a WebUI that stops its own
    containers strands the user)."""
    worlds = arena.build(DIRECT, CONNECTED, API)
    for surface, transport in ((DIRECT, "direct"), (CONNECTED, "connected")):
        before = len(arena.runner.calls)
        code, _ = arena.cli(worlds[surface], ("up", "--profile", "n97"), transport=transport,
                            as_json=False)
        assert code == 0
        code, _ = arena.cli(worlds[surface], ("down", "--profile", "n97"), transport=transport,
                            as_json=False)
        assert code == 0
        compose = [c for c in arena.runner.calls[before:] if c[:2] == ["docker", "compose"]]
        assert len(compose) == 2 and "up" in compose[0] and "down" in compose[1], compose
        assert worlds[surface].factory_calls == []      # never a daemon, even if requested
    spec = create_app(ControlConfig(token="spec")).openapi()
    operations = {op["operationId"] for methods in spec["paths"].values()
                  for op in methods.values()}
    assert not operations & {"up", "down", "stack_up", "stack_down", "stack_start", "stack_stop",
                             "compose_up", "compose_down"}
    segments = {segment for path in spec["paths"] for segment in path.split("/")}
    assert not segments & {"up", "down", "stack", "compose", "start", "stop"}
    refused = arena.api(worlds[API], "POST", "/up", {})
    assert refused.status_code == 404 and refused.json()["error"]["code"] == "not_found"


# ── the chat column, per row ─────────────────────────────────────────────────


def test_chat_ceiling_matches_the_matrix_row_by_row():
    spec = create_app(ControlConfig(token="spec")).openapi()
    methods = {op["operationId"]: method.upper()
               for path in spec["paths"].values() for method, op in path.items()}
    for row, cells in CHAT_CEILING.items():
        for operation, allowed in cells.items():
            assert operation in methods, (row, operation)
            assert client_may(CHAT_OPS_CLIENT, operation, methods[operation]) is allowed, (
                row, operation)
    # every mutation the contract offers is classified by some row
    mutating = {op for op, method in methods.items() if method != "GET"}
    classified = {op for cells in CHAT_CEILING.values() for op in cells}
    assert mutating <= classified, mutating - classified


# ── tripwires: the doc table, the gate wiring, the normaliser ────────────────


def matrix_rows_in_the_doc() -> list[str]:
    text = STRATEGY_DOC.read_text(encoding="utf-8")
    section = text.split("## 3. Parity matrix", 1)[1].split("Legend:", 1)[0]
    rows = []
    for line in section.splitlines():
        header_or_rule = line.startswith("| Operation") or set(line) <= {"|", "-", " "}
        if not line.startswith("| ") or header_or_rule:
            continue
        rows.append(line.split("|")[1].strip())
    return rows


def test_every_matrix_row_of_the_strategy_doc_has_a_harness_row():
    rows = matrix_rows_in_the_doc()
    assert len(rows) == 12 and len(set(rows)) == 12
    assert set(rows) == set(ROW_TESTS) == set(CHAT_CEILING)
    here = Path(__file__).read_text(encoding="utf-8")
    for label, test_name in ROW_TESTS.items():
        assert re.search(rf"^def {test_name}\(", here, re.MULTILINE), (label, test_name)


def test_wiring_the_gate_is_reachable_from_make_and_ci():
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "swe-parity:" in makefile and "tests/parity" in makefile
    assert "release-gate:" in makefile
    for target in ("swe-parity", "release-gate"):
        assert re.search(rf"\.PHONY:.*\b{target}\b", makefile.replace("\\\n", " ")), target
    workflow = (REPO_ROOT / ".github" / "workflows" / "agentd-ci.yml").read_text(encoding="utf-8")
    assert "tests/parity" in workflow and "tests/acceptance" in workflow
    assert "workflow_dispatch" in workflow          # manual trigger only, by request
    assert (Path(__file__).parent / "__init__.py").is_file()


def test_the_normaliser_is_explicit_and_narrow():
    raw = {
        "ts": "2026-09-09T12:00:00", "actor": "nita via admin-center", "transport": "connected x",
        "run": "swe/20260909-120000-abc123", "path": "/w/api/platform/models/gguf/a.gguf",
        "sha": "a" * 40, "short": "committed 0123456789ab on main",
        "message": "benchmark alpha: 15.5 tok/s (side-load, 100 tokens in 0.2s)",
        "nested": [{"created_at": "2026-09-09T12:00:00Z", "idempotency_key": "k",
                    "duration_ms": 12, "tokens_per_s": 15.5}],
    }
    assert scrub(raw, ("/w/api",)) == {
        "ts": "<ts>", "actor": "nita", "run": "swe/<run>",
        "path": "<world>/platform/models/gguf/a.gguf",
        "sha": "<sha>", "short": "committed <sha> on main",
        "message": "benchmark alpha: 15.5 tok/s (side-load, 100 tokens in <s>s)",
        "nested": [{"created_at": "<ts>", "tokens_per_s": 15.5}],
    }
    # what is NOT normalised stays a difference — the gate is not a sieve
    assert scrub({"generation": 2}) != scrub({"generation": 3})
    assert scrub("nita via cli") == scrub("nita via admin-center") == "nita"
    assert json.dumps(scrub({"a": ["x"]})) == '{"a": ["x"]}'
