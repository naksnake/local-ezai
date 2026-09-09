"""The Admin Center's Governance page (PR-18, ADR-030; WEBUI_ADMIN_CENTER §3,
WEBUI_PRODUCT_STRATEGY §3.7, MODEL_GOVERNANCE_V2 §2): one queue, evidence
next to every decision, approve / reject through the daemon as the monitor
login, deep links the SWE tools already print.

Offline: the monitor as shipped over the in-process daemon (the PR-17
fixture); a real-Chromium smoke approves one request and rejects another
from the approval view."""

from __future__ import annotations

import pytest

from tests.integration import test_admin_center_models as models_tests
from tests.integration.test_admin_center import (
    ADMIN,
    AUTH,
    V1,
    VIEWER,
    launch_chromium,
    serve,
)
from tests.integration.test_admin_center_models import PAGE, audited, registry

#: The PR-17 fixture: bootstrapped platform, daemon with faked lifecycle seams, monitor.
center = models_tests.center


def propose(center, model: str = "beta", group: str = "coding") -> dict:
    """An activation request through the monitor (admin), as a human would."""
    out = center.web.post(f"/api/ezai/models/{model}/activate", auth=ADMIN, headers=PAGE,
                          json={"group": group})
    assert out.status_code == 200, out.text
    return out.json()["request"]


# ── queue · detail ───────────────────────────────────────────────────────────


def test_an_empty_queue_is_a_page_state(center):
    d = center.web.get("/api/ezai/governance", auth=VIEWER).json()
    assert d["generation"] == 1 and d["role"] == "viewer" and d["status"] is None
    assert d["requests"] == [] and d["pending"] == 0
    assert d["counts"] == {s: 0 for s in ("pending", "approved", "applied", "rejected", "failed",
                                          "superseded")}
    assert center.web.get("/governance").status_code == 401
    assert center.web.get("/governance", auth=VIEWER).status_code == 200


def test_a_pending_request_shows_its_evidence(center):
    request = propose(center)
    assert request["id"] == "cr-0001" and request["status"] == "pending"
    queue = center.web.get("/api/ezai/governance", auth=VIEWER).json()
    assert queue["pending"] == 1 and queue["counts"]["pending"] == 1
    [row] = queue["requests"]
    assert row["id"] == "cr-0001" and row["kind"] == "activation"
    assert row["requested_by"] == "admin via admin-center" and row["proposed_by"] == "human"
    assert row["affected_roles"] == ["coder"] and row["runtime_switch"] is False
    assert row["requires_approval"] is True and row["decision"] is None
    assert "evidence" not in row and "diff" not in row  # the listing stays light

    detail = center.web.get("/api/ezai/governance/cr-0001", auth=VIEWER).json()
    assert detail["generation"] == 1 and detail["reversible_to"] == 1
    r = detail["request"]
    assert "proposed" not in r  # the registry dump is not human evidence — the diff is
    assert r["diff"] == ["model beta: state benchmarked → active",
                         "group coding: [alpha] → [beta, alpha]"]
    assert r["affected_roles"]["coder"] == {"before": {"primary": "alpha", "fallbacks": []},
                                            "after": {"primary": "beta", "fallbacks": ["alpha"]}}
    evidence = r["evidence"]
    assert evidence["benchmarks"]["beta"]["tokens_per_s"] == 12.0
    assert evidence["benchmarks"]["alpha"]["tokens_per_s"] == 10.0
    assert evidence["fit"]["beta"]["fits"] is True and evidence["fit"]["beta"]["warnings"]
    report = {(c["role"], c["model"], c["position"]): c for c in evidence["capability_report"]}
    assert report[("coder", "beta", "primary")]["ok"] is True
    assert report[("coder", "alpha", "fallback")]["checks"]["tool_calling"] is True
    assert evidence["runtime"] == {"before": "llamacpp", "after": "llamacpp", "switch": False}
    assert evidence["capability_class"] == "cpu-low"
    # the deep link the SWE tools and the CLI print is a page here
    page = center.web.get("/governance/cr-0001", auth=VIEWER)
    assert page.status_code == 200 and "Local-EZAI Admin Center" in page.text
    # the overview and the models page point at it
    overview = center.web.get("/api/ezai/overview", auth=VIEWER).json()
    assert [q["id"] for q in overview["pending"]] == ["cr-0001"]
    assert "/governance" in page.text and "Open Governance" in page.text


# ── decisions ────────────────────────────────────────────────────────────────


def test_approve_from_the_page_applies_the_generation_and_is_audited(center):
    request = propose(center)
    approved = center.web.post(f"/api/ezai/governance/{request['id']}/approve", auth=ADMIN,
                               headers=PAGE, json={"reason": "benchmarks look good"})
    assert approved.status_code == 200, approved.text
    out = approved.json()
    # the outcome carries the request as decided (approved) plus the apply result;
    # the stored request is then marked applied (the detail below shows it)
    assert out["request"]["status"] == "approved" and out["applied"]["ok"] is True
    assert out["applied"]["generation"] == 2 and out["applied"]["rolled_back"] is False
    assert out["request"]["decision"]["by"] == "admin via admin-center"
    assert out["request"]["decision"]["reason"] == "benchmarks look good"
    assert registry(center).generation == 2 and registry(center).models["beta"].state == "active"

    detail = center.web.get(f"/api/ezai/governance/{request['id']}", auth=VIEWER).json()
    assert detail["request"]["status"] == "applied" and detail["generation"] == 2
    assert detail["request"]["result"]["generation"] == 2
    assert detail["reversible_to"] == 1
    queue = center.web.get("/api/ezai/governance", auth=VIEWER).json()
    assert queue["pending"] == 0 and queue["counts"]["applied"] == 1
    assert queue["requests"][0]["result"]["generation"] == 2
    models = center.web.get("/api/ezai/models", auth=VIEWER).json()
    coding = next(g for g in models["groups"] if g["group"] == "coding")
    assert [m["name"] for m in coding["serving"]] == ["beta", "alpha"]
    events = audited(center)
    assert ("api.governance_approve", "admin via admin-center") in events
    assert ("request.approved", "admin via admin-center") in events
    assert ("request.applied", "admin via admin-center") in events
    decision_calls = [r for r in center.wire.requests if r.url.path.endswith("/approve")]
    assert len(decision_calls) == 1 and decision_calls[0].headers["Idempotency-Key"]


def test_reject_needs_a_reason_and_decisions_are_made_once(center):
    request = propose(center)
    # the daemon's rule, in its words
    unreasoned = center.web.post(f"/api/ezai/governance/{request['id']}/reject", auth=ADMIN,
                                 headers=PAGE, json={"reason": ""})
    assert unreasoned.status_code == 409, unreasoned.text
    assert unreasoned.json()["error"]["code"] == "governance_rule"
    assert "needs a reason" in unreasoned.json()["error"]["message"]
    assert registry(center).generation == 1
    rejected = center.web.post(f"/api/ezai/governance/{request['id']}/reject", auth=ADMIN,
                               headers=PAGE, json={"reason": "not on this host"})
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["request"]["status"] == "rejected"
    assert rejected.json()["applied"] is None
    assert rejected.json()["request"]["decision"]["reason"] == "not on this host"
    # decisions are made once, on pending requests
    again = center.web.post(f"/api/ezai/governance/{request['id']}/approve", auth=ADMIN,
                            headers=PAGE, json={"reason": "changed my mind"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "governance_rule"
    assert "made once" in again.json()["error"]["message"]
    assert registry(center).generation == 1 and registry(center).models["beta"].state != "active"
    # the history filter
    everything = center.web.get("/api/ezai/governance", auth=VIEWER).json()
    assert everything["pending"] == 0 and everything["counts"]["rejected"] == 1
    only_rejected = center.web.get("/api/ezai/governance?status=rejected", auth=VIEWER).json()
    assert [r["id"] for r in only_rejected["requests"]] == [request["id"]]
    assert only_rejected["status"] == "rejected"
    assert center.web.get("/api/ezai/governance?status=pending",
                          auth=VIEWER).json()["requests"] == []
    assert ("api.governance_reject", "admin via admin-center") in audited(center)
    missing = center.web.get("/api/ezai/governance/cr-0099", auth=VIEWER)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_decisions_need_the_admin_role_and_the_pages_header(center, decision):
    request = propose(center)
    path = f"/api/ezai/governance/{request['id']}/{decision}"
    body = {"reason": "because"}
    before = len(center.wire.requests)
    assert center.web.post(path, auth=VIEWER, headers=PAGE, json=body).status_code == 403
    assert center.web.post(path, json=body).status_code == 401
    forged = center.web.post(path, auth=ADMIN, json=body)  # ambient login, no page header
    assert forged.status_code == 400
    assert forged.json()["error"]["code"] == "same_origin_required"
    assert len(center.wire.requests) == before  # the daemon never heard of it
    assert center.ctx.queue.get(request["id"]).status == "pending"
    # a viewer may read the queue and the approval view
    assert center.web.get("/api/ezai/governance", auth=VIEWER).json()["pending"] == 1
    assert center.web.get(f"/api/ezai/governance/{request['id']}",
                          auth=VIEWER).json()["request"]["status"] == "pending"


# ── pages · navigation ───────────────────────────────────────────────────────


def test_navigation_reaches_governance_from_every_page(center):
    for path in ("/", "/overview", "/models", "/routing", "/runtime", "/runs", "/governance"):
        text = center.web.get(path, auth=VIEWER).text
        assert 'href="/governance"' in text, path
    assert center.web.get("/governance/cr-0001").status_code == 401  # login first
    assert center.web.get("/api/status", auth=VIEWER).status_code == 200  # dashboard intact


# ── real browser smoke (Browser QA is part of validation) ────────────────────


def test_browser_smoke_approve_and_reject_from_the_approval_view(center):
    sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    # gamma needs evidence before it can be proposed — the benchmark persists generation 2,
    # so both requests below share that base generation
    bench = center.control.post(f"{V1}/models/gamma/benchmark", headers=AUTH)
    assert bench.status_code == 200, bench.text
    base = registry(center).generation
    assert base == 2
    first = propose(center)  # cr-0001: activate beta as coding primary
    second = center.control.post(f"{V1}/models/gamma/activate", headers=AUTH,
                                 json={"group": "chat"}).json()["request"]
    assert second["id"] == "cr-0002" and second["status"] == "pending"
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
            viewer.goto(f"{base_url}/governance")
            viewer.wait_for_selector("#footer:not(:empty)")
            text = viewer.inner_text("body")
            assert "cr-0001" in text and "cr-0002" in text and "2 pending" in text
            viewer.goto(f"{base_url}/governance/{first['id']}")
            viewer.wait_for_selector("#footer:not(:empty)")
            text = viewer.inner_text("body")
            assert "beta" in text and "alpha" in text and "12 tok/s" in text  # evidence
            assert "coder" in text and "no switch" in text
            assert viewer.locator("button[data-decide]").count() == 0  # viewer is read-only
            assert "viewer is read-only" in text

            admin = browser.new_context(
                http_credentials={"username": ADMIN[0], "password": ADMIN[1]}).new_page()
            watch(admin)
            admin.goto(f"{base_url}/governance/{first['id']}")
            admin.wait_for_selector("button[data-decide=approve]")
            admin.click("button[data-decide=approve]")  # inline confirmation, no native dialog
            admin.fill("form.inline input[name=reason]", "looks right")
            admin.click("form.inline button[data-confirm]")
            admin.wait_for_selector(".card-header .badge.applied")
            text = admin.inner_text("body")
            assert "looks right" in text and f"now generation {base + 1}" in text
            assert "approved" in admin.inner_text("#notice")
            assert registry(center).generation == base + 1

            admin.goto(f"{base_url}/governance/{second['id']}")
            admin.wait_for_selector("button[data-decide=reject]")
            admin.click("button[data-decide=reject]")
            admin.fill("form.inline input[name=reason]", "not on this host")
            admin.click("form.inline button[data-confirm]")
            admin.wait_for_selector(".card-header .badge.rejected")
            assert "not on this host" in admin.inner_text("body")  # the recorded reason
            assert center.ctx.queue.get(second["id"]).status == "rejected"
            assert registry(center).generation == base + 1  # a rejection changes nothing

            admin.goto(f"{base_url}/governance")
            admin.wait_for_selector("#footer:not(:empty)")
            text = admin.inner_text("body")
            assert "0 pending" in text and "nothing awaits approval" in text
            assert f"→ gen {base + 1}" in text
            assert problems == [], problems
            browser.close()
    finally:
        server.should_exit = True
        thread.join(10)
