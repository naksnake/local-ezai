"""The Admin Center's Models / Routing / Runtime pages (PR-17, ADR-030;
WEBUI_ADMIN_CENTER §2, MODEL_ROUTING_DESIGN §7, RUNTIME_ABSTRACTION §5,
HARDWARE_AGNOSTIC §3): role-first model cards with the recommender's fit
verdicts, the lifecycle mutations the parity matrix commits to (through the
daemon, admin only, same-origin guarded), the explain view, and the runtime
switch pre-check.

Offline: the monitor as shipped, wired to the in-process daemon over the
lifecycle seams the CLI tests fake (fetch, side-load, validator). One
real-Chromium smoke renders the three pages and benchmarks a model from
the page."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from agentd import platform_cli
from agentd.activation import Platform
from agentd.capability import CapabilityVector
from agentd.config import ControlConfig, load_config
from agentd.control import api as control_api
from agentd.control.app import create_app
from agentd.platform_cli import build_context
from agentd.registry_v2 import load_registry, save_generation
from agentd.render import write_rendered
from agentd.runtime_descriptor import load_descriptors
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
from tests.unit.test_activation import seed_registry
from tests.unit.test_lifecycle import FakeEngineHTTP, FakeRunner

REPO_ROOT = Path(__file__).resolve().parents[3]
PAGE = {"X-Requested-With": "admin-center"}
#: The groups the contract lets a client see: those a model belongs to or a
#: role resolves through. The seed's empty, role-less `spare` group is
#: invisible to the 1.0.0 contract — and to any thin client.
GROUPS = ["chat", "coding", "reasoning"]


@pytest.fixture
def center(tmp_path: Path, monkeypatch):
    """A bootstrapped platform under the daemon (lifecycle seams faked) and
    the monitor wired to it."""
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
    app = create_app(ControlConfig(token=TOKEN), ctx,
                     prober=lambda url, headers: (200, "ok healthy passed openapi"))
    with TestClient(app) as control:
        monitor = load_monitor(monkeypatch)
        web, wire = attach(monitor, control)
        yield SimpleNamespace(root=root, ctx=ctx, control=control, monitor=monitor, web=web,
                              wire=wire, weights=tmp_path)


def registry(center):
    return load_registry(center.root / "config")


def audited(center) -> list[tuple[str, str]]:
    return [(r.event, r.actor) for r in center.ctx.queue.audit()]


# ── models page ──────────────────────────────────────────────────────────────


def test_models_page_is_role_first_with_the_platforms_fit_evidence(center):
    response = center.web.get("/api/ezai/models", auth=VIEWER)
    assert response.status_code == 200, response.text
    d = response.json()
    assert d["generation"] == 1 and d["runtime"] == "llamacpp" and d["role"] == "viewer"
    assert d["class"] == "cpu-low" and set(d["runtimes"]) == {"llamacpp", "vllm"}
    groups = {g["group"]: g for g in d["groups"]}
    assert list(groups) == GROUPS  # every group a model or a role names, sorted
    coding = groups["coding"]
    assert coding["roles"] == ["coder"]
    assert [m["name"] for m in coding["serving"]] == ["alpha"]  # the resolution order
    assert [m["name"] for m in coding["others"]] == ["beta", "gamma"]
    alpha = coding["serving"][0]
    assert alpha["state"] == "active" and alpha["tokens_per_s"] == 10.0 and alpha["serving"]
    # the snapshot enrichment: membership, context, format, license per model
    assert alpha["groups"] == ["reasoning", "coding", "chat"] and alpha["context"] == 8192
    assert alpha["format"] == "gguf" and "license" in alpha
    assert alpha["fit"] is None  # not a catalog model → no verdict, honestly
    # the recommender's candidates for this group on the active runtime, verdicts included
    ids = [c["id"] for c in coding["candidates"]]
    assert "qwen2.5-coder-1.5b-instruct" in ids and all(c["provider"] == "llamacpp"
                                                        for c in coding["candidates"])
    small = next(c for c in coding["candidates"] if c["id"] == "qwen2.5-coder-1.5b-instruct")
    assert small["eligible"] and small["verdict"]["fits"]
    assert small["verdict"]["placement"] == "system"
    assert groups["reasoning"]["roles"] == ["reviewer"]  # pinned role: chain = active members
    assert [m["name"] for m in groups["reasoning"]["serving"]] == ["alpha"]
    # catalog + per-variant verdicts for the active runtime only
    assert d["catalog"]["count"] == 7
    assert set(d["verdicts"]["qwen2.5-coder-7b-instruct"]) == {"gguf"}  # hf → vllm, filtered out
    assert {r["role"] for r in d["roles"]} == {"coder", "reviewer", "chat"}
    assert [h["generation"] for h in d["history"]] == [1] and d["pending"] == []
    assert d["orphans"] == []


def test_lifecycle_through_the_monitor_as_admin(center):
    weights = center.weights / "delta.gguf"
    weights.write_bytes(b"d" * 4096)
    installed = center.web.post("/api/ezai/models", auth=ADMIN, headers=PAGE,
                                json={"ref": f"gguf:{weights}", "name": "delta",
                                      "group": "coding"})
    assert installed.status_code == 200, installed.text
    assert installed.json()["ok"] and installed.json()["state"] == "installed"
    bench = center.web.post("/api/ezai/models/delta/benchmark", auth=ADMIN, headers=PAGE)
    assert bench.status_code == 200, bench.text
    assert bench.json()["tokens_per_s"] == 15.5 and bench.json()["state"] == "benchmarked"
    page = center.web.get("/api/ezai/models", auth=ADMIN).json()
    # installing never touches groups (MODEL_LIFECYCLE §2): the model is listed
    # under "not in any group" until an activation places it
    delta = next(m for m in page["orphans"] if m["name"] == "delta")
    assert delta["state"] == "benchmarked" and delta["tokens_per_s"] == 15.5
    assert delta["fit"] is None  # a user-supplied source: measured evidence, no catalog verdict
    assert delta["groups"] == [] and delta["format"] == "gguf"
    before = registry(center).generation  # install and benchmark each persisted a generation
    assert before == 3 and page["generation"] == before

    # a raw source declares no tool-call format, so it cannot serve the coder:
    # the render-time negotiation refuses and the page gets the platform's words
    refused = center.web.post("/api/ezai/models/delta/activate", auth=ADMIN, headers=PAGE,
                              json={"group": "coding"})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "lifecycle_refused"
    assert "tool_calling" in refused.json()["error"]["message"]
    assert registry(center).generation == before

    # activation of a capable, benchmarked model as coding primary changes what
    # serves the coder → a change request awaiting approval
    proposal = center.web.post("/api/ezai/models/beta/activate", auth=ADMIN, headers=PAGE,
                               json={"group": "coding"})
    assert proposal.status_code == 200, proposal.text
    request = proposal.json()["request"]
    assert proposal.json()["applied"] is None and request["status"] == "pending"
    assert request["requires_approval"] is True and "coder" in request["affected_roles"]
    page = center.web.get("/api/ezai/models", auth=ADMIN).json()
    assert [q["id"] for q in page["pending"]] == [request["id"]]
    assert page["generation"] == before  # nothing applied yet
    # the decision is a separate human act (Governance page, PR-18, or the CLI):
    # here the CLI approves, so the audit shows two different humans
    approved = center.control.post(f"{V1}/governance/{request['id']}/approve", headers=AUTH,
                                   json={"reason": "reviewed"})
    assert approved.status_code == 200 and approved.json()["applied"]["ok"]
    page = center.web.get("/api/ezai/models", auth=VIEWER).json()
    coding = next(g for g in page["groups"] if g["group"] == "coding")
    assert page["generation"] == before + 1
    assert [m["name"] for m in coding["serving"]] == ["beta", "alpha"]  # new primary + fallback
    assert [h["generation"] for h in page["history"]] == list(range(1, before + 2))
    assert page["history"][-1]["diff"]  # the activation's diff lines

    # rollback restores the approved state at once, audited, no queue
    rolled = center.web.post("/api/ezai/generations/rollback", auth=ADMIN, headers=PAGE,
                             json={"to_generation": before, "reason": "drill"})
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["ok"] and not rolled.json()["rolled_back"]  # no self-rollback needed
    assert f"content of generation {before}" in rolled.json()["message"]
    page = center.web.get("/api/ezai/models", auth=VIEWER).json()
    coding = next(g for g in page["groups"] if g["group"] == "coding")
    assert page["generation"] == before + 2
    assert [m["name"] for m in coding["serving"]] == ["alpha"]
    assert registry(center).models["beta"].state == "benchmarked"
    assert "delta" in page["models"]  # the rollback target still knew delta
    events = audited(center)
    for event in ("api.model_install", "api.model_benchmark", "api.model_activate",
                  "api.generation_rollback"):
        assert (event, "admin via admin-center") in events, event
    assert ("api.governance_approve", "nita via cli") in events
    mutations = [r for r in center.wire.requests if r.method != "GET"]
    assert mutations and all(r.headers["Idempotency-Key"] for r in mutations)
    assert all(r.headers["X-EZAI-User"] == "admin" for r in mutations)


def test_retire_upgrade_and_uninstall_paths(center):
    # retiring the serving primary is refused by the daemon — the refusal passes through
    refused = center.web.post("/api/ezai/models/alpha/retire", auth=ADMIN, headers=PAGE)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "lifecycle_refused"
    assert "activate" in refused.json()["error"]["message"] + refused.json()["error"]["fix"]
    # upgrade alpha → beta: a change request; approval swaps and retires alpha
    upgrade = center.web.post("/api/ezai/models/upgrade", auth=ADMIN, headers=PAGE,
                              json={"old": "alpha", "new": "beta"})
    assert upgrade.status_code == 200, upgrade.text
    request = upgrade.json()["request"]
    assert request["kind"] == "upgrade" and upgrade.json()["applied"] is None
    approved = center.control.post(f"{V1}/governance/{request['id']}/approve", headers=AUTH)
    assert approved.status_code == 200 and approved.json()["applied"]["ok"]
    page = center.web.get("/api/ezai/models", auth=ADMIN).json()
    coding = next(g for g in page["groups"] if g["group"] == "coding")
    assert [m["name"] for m in coding["serving"]] == ["beta"]
    alpha = next(m for m in coding["others"] if m["name"] == "alpha")
    assert alpha["state"] == "retired"
    # uninstall: a rollback target needs force — both answers come from the daemon
    blocked = center.web.delete("/api/ezai/models/alpha", auth=ADMIN, headers=PAGE)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "lifecycle_refused"
    removed = center.web.delete("/api/ezai/models/alpha?force=true", auth=ADMIN, headers=PAGE)
    assert removed.status_code == 200 and removed.json()["removed"] is True
    page = center.web.get("/api/ezai/models", auth=ADMIN).json()
    assert "alpha" not in page["models"]
    forced = [r for r in center.wire.requests if r.method == "DELETE"]
    assert [r.url.params.get("force") for r in forced] == [None, "true"]


@pytest.mark.parametrize("method, path, body", [
    ("POST", "/api/ezai/models", {"ref": "gguf:/nowhere/x.gguf"}),
    ("POST", "/api/ezai/models/upgrade", {"old": "alpha", "new": "beta"}),
    ("POST", "/api/ezai/models/gamma/benchmark", None),
    ("POST", "/api/ezai/models/beta/activate", {"group": "coding"}),
    ("POST", "/api/ezai/models/beta/retire", None),
    ("DELETE", "/api/ezai/models/gamma", None),
    ("POST", "/api/ezai/generations/rollback", {"reason": "x"}),
])
def test_mutations_need_the_admin_role_and_the_pages_header(center, method, path, body):
    as_viewer = center.web.request(method, path, auth=VIEWER, headers=PAGE, json=body)
    assert as_viewer.status_code == 403
    anonymous = center.web.request(method, path, json=body)
    assert anonymous.status_code == 401
    forged = center.web.request(method, path, auth=ADMIN, json=body)  # ambient login, no header
    assert forged.status_code == 400
    assert forged.json()["error"]["code"] == "same_origin_required"
    assert center.wire.requests == []  # the daemon never heard of any of it
    assert not any(event.startswith("api.") for event, _ in audited(center))
    assert registry(center).generation == 1


def test_daemon_errors_and_states_pass_through(center):
    missing = center.web.post("/api/ezai/models/nope/benchmark", auth=ADMIN, headers=PAGE)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    # activating without evidence is the daemon's refusal, in its words
    no_evidence = center.web.post("/api/ezai/models/gamma/activate", auth=ADMIN, headers=PAGE,
                                  json={"group": "coding"})
    assert no_evidence.status_code == 409
    assert no_evidence.json()["error"]["code"] == "lifecycle_refused"
    assert "benchmark" in (no_evidence.json()["error"]["message"]
                           + no_evidence.json()["error"]["fix"]).lower()
    bad_ref = center.web.post("/api/ezai/models", auth=ADMIN, headers=PAGE,
                              json={"ref": "s3:nowhere"})
    assert bad_ref.status_code in (409, 422)
    assert bad_ref.json()["error"]["code"] in ("lifecycle_refused", "invalid_request",
                                               "catalog_refused")


# ── routing page ─────────────────────────────────────────────────────────────


def test_routing_page_explains_every_defined_role(center):
    d = center.web.get("/api/ezai/routing", auth=VIEWER).json()
    assert d["generation"] == 1 and d["known_roles"][0] == "orchestrator"
    roles = {r["role"]: r for r in d["roles"]}
    assert set(roles) == {"coder", "reviewer", "chat"}  # the rest: "not defined in this registry"
    reviewer = roles["reviewer"]
    assert reviewer["source"] == {"pin": ["alpha"], "group": "reasoning"}
    assert reviewer["primary"] == "alpha" and reviewer["fallbacks"] == []
    assert reviewer["checks"]["alpha"]["ok"]
    assert reviewer["checks"]["alpha"]["checks"]["json_output"] is True
    assert reviewer["contract"]["tool_calling"] and reviewer["contract"]["json_output"]
    assert reviewer["reason"] and reviewer["generation"] == 1
    coder = roles["coder"]
    assert coder["checks"]["alpha"]["checks"]["tool_calling"] is True
    assert coder["contract"]["min_context"] == 8192
    assert [h["generation"] for h in d["history"]] == [1]
    assert set(d["models"]) == {"alpha", "beta", "gamma"}


# ── runtime page ─────────────────────────────────────────────────────────────


def test_runtime_page_prechecks_a_switch_honestly(center):
    d = center.web.get("/api/ezai/runtime", auth=VIEWER).json()
    assert d["active"] == "llamacpp" and d["runtimes"] == ["llamacpp", "vllm"]
    assert d["ok"] is True and {s["id"] for s in d["services"]} == {"engine", "router"}
    assert d["platform"]["capability_class"] == "cpu-low"
    assert d["platform"]["system_memory_gb"] > 0 and d["generation"] == 1  # this host's vector
    assert [m["name"] for m in d["active_models"]] == ["alpha"]
    assert len(d["prechecks"]) == 1
    vllm = d["prechecks"][0]
    assert vllm["runtime"] == "vllm" and vllm["ready"] is False
    # the blocker is named with its format and runtime; alpha has no catalog variant
    assert vllm["active_models"] == [{"name": "alpha", "runtime": "llamacpp", "format": "gguf",
                                      "variant_in_catalog": False}]
    per_group = {g["group"]: g for g in vllm["groups"]}
    assert set(per_group) == set(GROUPS)
    coding = per_group["coding"]
    assert all(c["provider"] == "vllm" and c["format"] == "hf" for c in coding["candidates"])
    assert coding["eligible"] == 0  # 14 GB hf weights do not fit 15.5 GB of RAM with overhead
    assert all(c["verdict"]["fits"] is False and c["verdict"]["placement"] == "none"
               for c in coding["candidates"])
    # the small chat model FITS in memory but is still not eligible: the vllm
    # descriptor's context budget on this class is below the chat role's
    # contract — the pre-check carries the platform's own words for it
    chat = per_group["chat"]
    small = next(c for c in chat["candidates"] if c["id"] == "qwen2.5-1.5b-instruct")
    assert small["verdict"]["fits"] and small["verdict"]["placement"] == "system"
    assert small["eligible"] is False and chat["eligible"] == 0
    assert "min_context" in small["contract_failures"][0]
    assert "class budget" in small["contract_failures"][0]


# ── pages · navigation ───────────────────────────────────────────────────────


def test_pages_and_navigation(center):
    for path in ("/models", "/routing", "/runtime"):
        assert center.web.get(path).status_code == 401
        page = center.web.get(path, auth=VIEWER)
        assert page.status_code == 200 and "Local-EZAI Admin Center" in page.text
    for path in ("/", "/overview", "/runs"):
        text = center.web.get(path, auth=VIEWER).text
        for link in ('href="/models"', 'href="/routing"', 'href="/runtime"'):
            assert link in text, (path, link)
    assert center.web.get("/api/status", auth=VIEWER).status_code == 200  # dashboard intact


# ── real browser smoke (Browser QA is part of validation) ────────────────────


def test_browser_smoke_models_routing_runtime_and_one_admin_action(center):
    sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    server, thread, base = serve(center.monitor.app)
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
            viewer.goto(f"{base}/models")
            viewer.wait_for_selector("#footer:not(:empty)")
            text = viewer.inner_text("body")
            assert "generation 1" in text and "alpha" in text
            assert "qwen2.5-coder-1.5b-instruct" in text and "fits" in text
            assert viewer.title() == "Models · Local-EZAI Admin Center"
            assert viewer.locator("button[data-act]").count() == 0  # a viewer sees no mutation
            viewer.goto(f"{base}/routing")
            viewer.wait_for_selector("#footer:not(:empty)")
            text = viewer.inner_text("body")
            assert "reviewer" in text and "pin alpha" in text
            assert "not defined in this registry" in text
            viewer.goto(f"{base}/runtime")
            viewer.wait_for_selector("#footer:not(:empty)")
            text = viewer.inner_text("body")  # section titles render upper-cased (CSS)
            assert "llamacpp" in text and "switch to vllm" in text.lower()
            assert "no vllm variant in the catalog" in text

            admin = browser.new_context(
                http_credentials={"username": ADMIN[0], "password": ADMIN[1]}).new_page()
            watch(admin)
            admin.on("dialog", lambda dialog: dialog.accept())
            admin.goto(f"{base}/models")
            admin.wait_for_selector("#footer:not(:empty)")
            buttons = admin.locator("button[data-act=benchmark][data-name=gamma]")
            assert buttons.count() == 3  # gamma sits in three groups: one row per group
            buttons.first.click()
            admin.wait_for_selector("#notice.ok:has-text('15.5 tok/s')")
            admin.wait_for_selector("tr:has(strong:text-is('gamma')) .badge.benchmarked")
            assert admin.locator("tr:has(strong:text-is('gamma')) .badge.benchmarked").count() == 3
            assert registry(center).models["gamma"].state == "benchmarked"
            assert ("api.model_benchmark", "admin via admin-center") in audited(center)
            assert problems == [], problems
            browser.close()
    finally:
        server.should_exit = True
        thread.join(10)
