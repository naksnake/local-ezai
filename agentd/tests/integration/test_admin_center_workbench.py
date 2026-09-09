"""The Admin Center's Sprints / Evolution / Memory / Projects pages (PR-19,
ADR-030; WEBUI_ADMIN_CENTER §2, WEBUI_PRODUCT_STRATEGY §3.5–§3.6 and
journeys 3–4, ADR-017) and the contract's additive 1.1.0 memory operations.

Offline: the monitor as shipped over the in-process daemon; fake sprint and
evolution pipelines return realistic reports, the fixture repository carries
a real memory store. A real-Chromium smoke renders the four pages and adds a
memory rule from the page."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from agentd import platform_cli
from agentd.activation import Platform
from agentd.capability import CapabilityVector
from agentd.config import ControlConfig, load_config
from agentd.control import CLIENT_HEADER, CONTRACT_VERSION
from agentd.control import api as control_api
from agentd.control.app import create_app
from agentd.control.runs import RunRegistry
from agentd.memory import (
    KIND_ARCHITECTURE,
    KIND_FAILED_FIX,
    KIND_IMPLEMENTATION,
    KIND_RULE,
    KIND_STYLE,
    KIND_SUCCESSFUL_FIX,
    MemoryStore,
)
from agentd.platform_cli import build_context
from agentd.registry_v2 import save_generation
from agentd.render import write_rendered
from agentd.runtime_descriptor import load_descriptors
from agentd.schemas import (
    BenchmarkResult,
    EvolutionProposal,
    EvolutionReport,
    Improvement,
    PullRequestResult,
    SprintPlan,
    SprintReport,
    SprintTaskResult,
    SprintTaskSpec,
)
from tests.integration.test_admin_center import (
    ADMIN,
    AUTH,
    TOKEN,
    V1,
    VIEWER,
    attach,
    launch_chromium,
    load_monitor,
    serve,
)
from tests.integration.test_admin_center_models import PAGE, audited
from tests.unit.test_activation import seed_registry
from tests.unit.test_control_runs import DummyLLM, wait_terminal
from tests.unit.test_lifecycle import FakeEngineHTTP, FakeRunner

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC = "# Sprint: calculator hardening\n\n- add the schema\n- expose the api\n- document it\n"


def fake_sprint(config, repo, record, llm) -> SprintReport:
    plan = SprintPlan(goal="ship the sprint", requirements=["r1"], tasks=[
        SprintTaskSpec(id="T1", title="schema", description="add the schema"),
        SprintTaskSpec(id="T2", title="api", description="expose it", depends_on=["T1"]),
        SprintTaskSpec(id="T3", title="docs", description="document it", depends_on=["T1"])])
    tasks = [
        SprintTaskResult(index=0, task="schema", run_id="t1", status="completed", task_id="T1",
                         wave=1, commit_sha="a" * 40),
        SprintTaskResult(index=1, task="api", run_id="t2", status="completed", task_id="T2",
                         wave=2, depends_on=["T1"], commit_sha="b" * 40),
        SprintTaskResult(index=2, task="docs", run_id="t3", status="failed", task_id="T3",
                         wave=2, depends_on=["T1"], error="lint failed"),
    ]
    return SprintReport(sprint_id=record.run_id, status="completed",
                        branch=f"sprint/{record.run_id}", workspace_path=str(repo),
                        spec_file="SPRINT.md", plan=plan, waves=2,
                        report_doc="docs/sprints/calculator-hardening.md", tasks=tasks)


def fake_evolve(config, repo, record, llm) -> EvolutionReport:
    proposal = EvolutionProposal(
        title="Cache the validator", history_summary="12 runs, 2 repeated failures",
        failure_patterns=["flaky lint on generated files"], bottlenecks=["reviewer latency"],
        improvements=[Improvement(id="I1", title="cache lint results per file hash",
                                  description="…", rationale="lint dominates validation time")])
    return EvolutionReport(evolution_id=record.run_id, status="completed",
                           branch=f"evolve/{record.run_id}", workspace_path=str(repo),
                           proposal=proposal,
                           tasks=[SprintTaskResult(index=0, task="cache lint results",
                                                   run_id="e1", status="completed",
                                                   task_id="I1", wave=1, commit_sha="c" * 40)],
                           benchmark_before=BenchmarkResult(passed=True, checks=3,
                                                            duration_seconds=58.0),
                           benchmark_after=BenchmarkResult(passed=True, checks=3,
                                                           duration_seconds=41.0),
                           release_notes_updated=True,
                           pull_request=PullRequestResult(
                               created=False, bundle_path=".agent/evolution/PR_PROPOSAL.md",
                               note="no forge configured"))


def seed_memory(repo: Path) -> None:
    store = MemoryStore(repo / ".agent")
    store.record(KIND_RULE, "tests live next to modules",
                 "tests live next to the module they cover", run_id="manual")
    store.record(KIND_STYLE, "ruff line length 100", "keep lines under 100 characters",
                 run_id="manual")
    store.record(KIND_ARCHITECTURE, "one control plane", "everything goes through ezaid",
                 run_id="manual")
    store.record(KIND_FAILED_FIX, "retry without cause", "patching the symptom failed",
                 run_id="r-1", error_signature="AssertionError: add", category="test")
    store.record(KIND_SUCCESSFUL_FIX, "fix add operator", "return a + b instead of a - b",
                 run_id="r-1", error_signature="AssertionError: add", category="test",
                 files=["calculator.py"])
    store.record(KIND_IMPLEMENTATION, "added calculator tests", "test_calculator.py",
                 run_id="r-1")
    store.export_lessons()
    store.close()


@pytest.fixture
def center(tmp_path: Path, tmp_repo: Path, monkeypatch):
    root = tmp_path / "platform"
    (root / "config").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "config" / "providers", root / "config" / "providers")
    (root / "docker-compose.yml").write_text("services: {}\n")
    saved = save_generation(seed_registry(), root / "config", note="bootstrap")
    platform = Platform(config_dir=root / "config", platform_root=root,
                        descriptors=load_descriptors(root / "config"),
                        vector=CapabilityVector(system_memory_gb=15.5, cpu_cores=4),
                        capability_class="cpu-low", accelerator="none")
    write_rendered(platform.render(saved), platform.rendered)
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    monkeypatch.setattr(platform_cli, "default_runner", FakeRunner())
    monkeypatch.setattr(platform_cli, "http_probe", lambda url: 200)
    monkeypatch.setattr(platform_cli, "engine_http", lambda: FakeEngineHTTP(
        timings={"predicted_per_second": 15.5, "prompt_per_second": 80.0, "predicted_n": 100}))
    monkeypatch.setattr(platform_cli, "build_validator",
                        lambda ctx: lambda descriptor, name, entry: platform_cli.lifecycle
                        .ProbeResult(True, "", 0.2, name))
    monkeypatch.setattr(control_api, "docker_available", lambda: False)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({
        "llm": {"provider": "scripted", "script_path": str(tmp_path / "script.json")},
        "platform": {"config_dir": str(root / "config")},
        "runs_dir": str(tmp_path / "runs")}), encoding="utf-8")
    (tmp_path / "script.json").write_text("[]")
    ctx = build_context(load_config(cfg), root, actor="ezaid")
    platform_cli.add_project(ctx, str(tmp_repo))
    seed_memory(tmp_repo)
    registry = RunRegistry(ctx.config, root / "config" / "control" / "runs",
                           audit=ctx.queue.audit_log, max_concurrent=2, max_queued=2,
                           pipelines={"sprint": fake_sprint, "evolve": fake_evolve},
                           llm_factory=lambda config: DummyLLM())
    app = create_app(ControlConfig(token=TOKEN), ctx,
                     prober=lambda url, headers: (200, "ok healthy passed openapi"),
                     runs=registry)
    with TestClient(app) as control:
        monitor = load_monitor(monkeypatch)
        web, wire = attach(monitor, control)
        yield SimpleNamespace(root=root, ctx=ctx, control=control, monitor=monitor, web=web,
                              wire=wire, repo=tmp_repo, tmp=tmp_path)


def make_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)
    return path


def start(center, body: dict) -> dict:
    """A console start: the monitor answers 200 with the daemon's run record
    (the daemon itself answered 202 — the proxy keeps its own status)."""
    out = center.web.post("/api/ezai/runs", auth=ADMIN, headers=PAGE, json=body)
    assert out.status_code == 200, out.text
    return out.json()


# ── the contract's additive 1.1.0 operations ─────────────────────────────────


def test_contract_1_1_0_serves_a_projects_memory(center):
    assert CONTRACT_VERSION == "1.1.0"
    spec = center.control.get("/openapi.json").json()
    assert spec["info"]["version"] == "1.1.0"
    ops = {op["operationId"]: (method, path) for path, methods in spec["paths"].items()
           for method, op in methods.items()}
    assert ops["project_memory"] == ("get", f"{V1}/projects/{{name}}/memory")
    assert ops["project_memory_add"] == ("post", f"{V1}/projects/{{name}}/memory")
    listing = center.control.get(f"{V1}/projects/repo/memory", headers=AUTH)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["project"] == "repo" and body["exists"] and body["total"] == 6
    assert body["counts"] == {"project_rule": 1, "coding_style": 1, "architecture_decision": 1,
                              "failed_fix": 1, "successful_fix": 1, "implementation": 1}
    assert [r["kind"] for r in body["records"]][:2] == ["implementation", "successful_fix"]
    fixes = center.control.get(f"{V1}/projects/repo/memory", headers=AUTH,
                               params={"kind": "failed_fix"}).json()
    assert [r["title"] for r in fixes["records"]] == ["retry without cause"]
    assert fixes["records"][0]["error_signature"] == "AssertionError: add"
    found = center.control.get(f"{V1}/projects/repo/memory", headers=AUTH,
                               params={"search": "operator"}).json()
    assert [r["title"] for r in found["records"]] == ["fix add operator"]
    unknown = center.control.get(f"{V1}/projects/nope/memory", headers=AUTH)
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "not_found"
    bad_kind = center.control.get(f"{V1}/projects/repo/memory", headers=AUTH,
                                  params={"kind": "gossip"})
    assert bad_kind.status_code == 409 and bad_kind.json()["error"]["code"] == "lifecycle_refused"
    # chat may read but never write memory (the PR-15 client policy)
    chat = {**AUTH, CLIENT_HEADER: "swe-server"}
    assert center.control.get(f"{V1}/projects/repo/memory", headers=chat).status_code == 200
    forbidden = center.control.post(f"{V1}/projects/repo/memory", headers=chat,
                                    json={"text": "obey the chat"})
    assert forbidden.status_code == 401 and forbidden.json()["error"]["code"] == "client_forbidden"
    assert center.control.get(f"{V1}/projects/repo/memory", headers=AUTH).json()["total"] == 6


# ── memory page ──────────────────────────────────────────────────────────────


def test_memory_page_browses_searches_and_remembers(center):
    d = center.web.get("/api/ezai/memory", auth=VIEWER).json()
    assert d["projects"] == ["repo"] and d["project"] == "repo" and d["role"] == "viewer"
    assert d["kinds"][0] == "project_rule" and d["curated"] == [
        "project_rule", "coding_style", "architecture_decision"]
    m = d["memory"]
    assert m["exists"] and m["total"] == 6 and m["counts"]["successful_fix"] == 1
    assert m["path"].endswith(".agent/memory.db")
    styles = center.web.get("/api/ezai/memory?kind=coding_style", auth=VIEWER).json()["memory"]
    assert [r["title"] for r in styles["records"]] == ["ruff line length 100"]
    found = center.web.get("/api/ezai/memory?search=ezaid", auth=VIEWER).json()["memory"]
    assert [r["kind"] for r in found["records"]] == ["architecture_decision"]

    added = center.web.post("/api/ezai/memory", auth=ADMIN, headers=PAGE,
                            json={"project": "repo", "kind": "project_rule",
                                  "text": "prefer explicit imports over star imports"})
    assert added.status_code == 200, added.text
    assert added.json()["id"] == 7 and added.json()["kind"] == "project_rule"
    assert "remembered #7" in added.json()["message"]
    after = center.web.get("/api/ezai/memory?kind=project_rule", auth=VIEWER).json()["memory"]
    assert [r["title"] for r in after["records"]] == ["prefer explicit imports over star imports",
                                                       "tests live next to modules"]
    assert after["records"][0]["run_id"] == "manual"
    assert after["records"][0]["data"] == {"by": "admin via admin-center"}
    lessons = json.loads((center.repo / ".agent" / "lessons_learned.json").read_text())
    assert any(r["content"] == "prefer explicit imports over star imports"
               for r in lessons["project_rules"])
    events = audited(center)
    assert ("memory.added", "admin via admin-center") in events
    assert ("api.project_memory_add", "admin via admin-center") in events
    # the daemon's rules, in its words
    fixes_by_hand = center.web.post("/api/ezai/memory", auth=ADMIN, headers=PAGE,
                                    json={"project": "repo", "kind": "failed_fix", "text": "x"})
    assert fixes_by_hand.status_code == 422
    assert fixes_by_hand.json()["error"]["code"] == "invalid_request"
    empty = center.web.post("/api/ezai/memory", auth=ADMIN, headers=PAGE,
                            json={"project": "repo", "kind": "project_rule", "text": ""})
    assert empty.status_code == 422
    unknown = center.web.post("/api/ezai/memory", auth=ADMIN, headers=PAGE,
                              json={"project": "nope", "kind": "project_rule", "text": "x"})
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "not_found"
    assert center.web.get("/api/ezai/memory", auth=VIEWER).json()["memory"]["total"] == 7


# ── projects page ────────────────────────────────────────────────────────────


def test_projects_page_lists_registers_and_removes(center):
    d = center.web.get("/api/ezai/projects", auth=VIEWER).json()
    [repo] = d["projects"]
    assert repo["name"] == "repo" and repo["path"] == str(center.repo)
    assert repo["runs"] == 0 and repo["active"] == 0 and repo["last"] is None
    second = make_repo(center.tmp / "second")
    added = center.web.post("/api/ezai/projects", auth=ADMIN, headers=PAGE,
                            json={"path": str(second), "name": "second"})
    assert added.status_code == 200, added.text
    assert added.json()["project"]["added_by"] == "admin via admin-center"
    listed = center.web.get("/api/ezai/projects", auth=VIEWER).json()["projects"]
    assert [p["name"] for p in listed] == ["repo", "second"]
    # the daemon's refusals pass through
    not_git = center.web.post("/api/ezai/projects", auth=ADMIN, headers=PAGE,
                              json={"path": str(center.tmp)})
    assert not_git.status_code == 409 and not_git.json()["error"]["code"] == "lifecycle_refused"
    assert "not a git repository" in not_git.json()["error"]["message"]
    duplicate = center.web.post("/api/ezai/projects", auth=ADMIN, headers=PAGE,
                                json={"path": str(second)})
    assert duplicate.status_code == 409
    removed = center.web.delete("/api/ezai/projects?target=second", auth=ADMIN, headers=PAGE)
    assert removed.status_code == 200 and removed.json()["removed"] == "second"
    gone = center.web.delete("/api/ezai/projects?target=second", auth=ADMIN, headers=PAGE)
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "not_found"
    assert [p["name"] for p in center.web.get("/api/ezai/projects",
                                             auth=VIEWER).json()["projects"]] == ["repo"]
    events = audited(center)
    assert ("api.project_add", "admin via admin-center") in events
    assert ("api.project_remove", "admin via admin-center") in events


# ── sprints · evolution (console starts) ─────────────────────────────────────


def test_console_starts_a_sprint_and_shows_its_plan_and_graph(center):
    started = start(center, {"kind": "sprint", "project": "repo", "spec": SPEC,
                             "keep_going": True})
    assert started["kind"] == "sprint" and started["actor"] == "admin via admin-center"
    assert started["request"] == SPEC and started["options"] == {"keep_going": True}
    assert wait_terminal(center.control, started["run_id"])["status"] == "completed"
    d = center.web.get("/api/ezai/sprints", auth=VIEWER).json()
    assert d["projects"] == ["repo"] and d["active"] == 0
    [item] = d["sprints"]
    assert item["run"]["run_id"] == started["run_id"]
    report = item["report"]
    assert report["plan"]["goal"] == "ship the sprint" and report["waves"] == 2
    assert report["report_doc"] == "docs/sprints/calculator-hardening.md"
    assert [(t["task_id"], t["wave"], t["status"]) for t in report["tasks"]] == [
        ("T1", 1, "completed"), ("T2", 2, "completed"), ("T3", 2, "failed")]
    assert item["mermaid"].splitlines()[0] == "graph LR"
    assert "  T1 --> T2" in item["mermaid"] and "  T1 --> T3" in item["mermaid"]
    assert '  T1["T1: schema"]' in item["mermaid"]
    # the project page now counts the work
    [repo] = center.web.get("/api/ezai/projects", auth=VIEWER).json()["projects"]
    assert repo["runs"] == 1 and repo["last"]["kind"] == "sprint"
    # the runs page filter by project
    filtered = center.web.get("/api/ezai/runs?project=repo", auth=VIEWER).json()
    assert [r["run_id"] for r in filtered["runs"]] == [started["run_id"]]
    assert center.web.get("/api/ezai/runs?project=other", auth=VIEWER).json()["runs"] == []


def test_console_runs_an_evolution_cycle_and_shows_the_proposal(center):
    started = start(center, {"kind": "evolve", "project": "repo", "focus": "reviewer latency"})
    assert started["request"] == "reviewer latency"
    assert wait_terminal(center.control, started["run_id"])["status"] == "completed"
    d = center.web.get("/api/ezai/evolution", auth=VIEWER).json()
    assert d["queue"] == [] and d["projects"] == ["repo"]
    [cycle] = d["cycles"]
    report = cycle["report"]
    assert report["proposal"]["title"] == "Cache the validator"
    assert [i["title"] for i in report["proposal"]["improvements"]] == [
        "cache lint results per file hash"]
    assert report["benchmark_before"]["duration_seconds"] == 58.0
    assert report["benchmark_after"]["duration_seconds"] == 41.0
    assert report["pull_request"]["bundle_path"] == ".agent/evolution/PR_PROPOSAL.md"
    assert report["release_notes_updated"] is True
    assert cycle["run"]["actor"] == "admin via admin-center"
    assert ("api.run_start", "admin via admin-center") in audited(center)


def test_the_console_starts_sprints_and_evolution_only(center):
    for kind in ("run", "fix", "plan", None):
        refused = center.web.post("/api/ezai/runs", auth=ADMIN, headers=PAGE,
                                  json={"kind": kind, "project": "repo", "task": "x"})
        assert refused.status_code == 400, kind
        assert refused.json()["error"]["code"] == "invalid_request"
        assert "console starts sprint and evolve" in refused.json()["error"]["message"]
    assert not any(r.url.path.endswith("/runs") and r.method == "POST"
                   for r in center.wire.requests)
    # a sprint without a specification: the daemon's own validation
    no_spec = center.web.post("/api/ezai/runs", auth=ADMIN, headers=PAGE,
                              json={"kind": "sprint", "project": "repo"})
    assert no_spec.status_code == 422 and "spec" in no_spec.json()["error"]["message"]
    unregistered = center.web.post("/api/ezai/runs", auth=ADMIN, headers=PAGE,
                                   json={"kind": "sprint", "project": "nope", "spec": SPEC})
    assert unregistered.status_code == 404
    assert center.web.get("/api/ezai/sprints", auth=VIEWER).json()["sprints"] == []


@pytest.mark.parametrize("method, path, body", [
    ("POST", "/api/ezai/projects", {"path": "/tmp"}),
    ("DELETE", "/api/ezai/projects?target=repo", None),
    ("POST", "/api/ezai/runs", {"kind": "sprint", "project": "repo", "spec": SPEC}),
    ("POST", "/api/ezai/memory", {"project": "repo", "kind": "project_rule", "text": "x"}),
])
def test_mutations_need_the_admin_role_and_the_pages_header(center, method, path, body):
    assert center.web.request(method, path, auth=VIEWER, headers=PAGE, json=body).status_code == 403
    assert center.web.request(method, path, json=body).status_code == 401
    forged = center.web.request(method, path, auth=ADMIN, json=body)
    assert forged.status_code == 400
    assert forged.json()["error"]["code"] == "same_origin_required"
    assert center.wire.requests == []
    assert not any(event.startswith("api.") for event, _ in audited(center))
    # viewers read every page's data
    for page in ("projects", "sprints", "evolution", "memory"):
        assert center.web.get(f"/api/ezai/{page}", auth=VIEWER).status_code == 200


# ── pages · navigation ───────────────────────────────────────────────────────


def test_pages_and_navigation(center):
    for path in ("/projects", "/sprints", "/evolution", "/memory"):
        assert center.web.get(path).status_code == 401
        page = center.web.get(path, auth=VIEWER)
        assert page.status_code == 200 and "Local-EZAI Admin Center" in page.text
    for path in ("/", "/overview", "/governance"):
        text = center.web.get(path, auth=VIEWER).text
        for link in ('href="/projects"', 'href="/sprints"', 'href="/evolution"', 'href="/memory"'):
            assert link in text, (path, link)
    assert center.web.get("/api/status", auth=VIEWER).status_code == 200  # dashboard intact


# ── real browser smoke (Browser QA is part of validation) ────────────────────


def test_browser_smoke_workbench_pages_and_remembering_from_the_page(center):
    sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    sprint = start(center, {"kind": "sprint", "project": "repo", "spec": SPEC})
    cycle = start(center, {"kind": "evolve", "project": "repo"})
    assert wait_terminal(center.control, sprint["run_id"])["status"] == "completed"
    assert wait_terminal(center.control, cycle["run_id"])["status"] == "completed"
    server, thread, base_url = serve(center.monitor.app)
    try:
        with sync_api.sync_playwright() as playwright:
            browser = launch_chromium(playwright)
            problems: list[str] = []

            def watch(page) -> None:
                page.on("console", lambda m: problems.append(
                    f"{m.text} @ {(m.location or {}).get('url', '')}")
                    if m.type == "error" and "favicon" not in (m.location or {}).get("url", "")
                    else None)
                page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))

            viewer = browser.new_context(
                http_credentials={"username": VIEWER[0], "password": VIEWER[1]}).new_page()
            watch(viewer)
            viewer.goto(f"{base_url}/sprints")
            viewer.wait_for_selector("#footer:not(:empty)")
            text = viewer.inner_text("body")
            assert "ship the sprint" in text and "graph LR" in text and "T1 --> T2" in text
            assert "lint failed" in text and viewer.locator("#sprint-form").count() == 0
            viewer.goto(f"{base_url}/evolution")
            viewer.wait_for_selector("#footer:not(:empty)")
            text = viewer.inner_text("body")
            assert "Cache the validator" in text and "awaiting human review" in text
            assert "58s" in text and "41s" in text and "PR_PROPOSAL.md" in text
            viewer.goto(f"{base_url}/projects")
            viewer.wait_for_selector("#footer:not(:empty)")
            text = viewer.inner_text("body")
            assert "repo" in text and "2 runs" in text
            assert viewer.locator("button[data-remove]").count() == 0
            viewer.goto(f"{base_url}/memory")
            viewer.wait_for_selector("#footer:not(:empty)")
            text = viewer.inner_text("body")
            assert "tests live next to modules" in text and "AssertionError: add" in text
            assert "6 entries" in text and viewer.locator("#memory-form").count() == 0

            admin = browser.new_context(
                http_credentials={"username": ADMIN[0], "password": ADMIN[1]}).new_page()
            watch(admin)
            admin.goto(f"{base_url}/memory")
            admin.wait_for_selector("#memory-form")
            admin.select_option("#memory-form select[name=kind]", "coding_style")
            admin.fill("#memory-form input[name=text]", "docstrings state the why, not the what")
            admin.click("#memory-form button[type=submit]")
            admin.wait_for_selector("#notice.ok:has-text('remembered #7')")
            admin.wait_for_selector("text=docstrings state the why, not the what")
            assert "7 entries" in admin.inner_text("#subtitle")
            assert ("memory.added", "admin via admin-center") in audited(center)
            admin.goto(f"{base_url}/projects")
            admin.wait_for_selector("#project-form")
            assert admin.locator("button[data-remove]").count() == 1
            assert problems == [], problems
            browser.close()
    finally:
        server.should_exit = True
        thread.join(10)
