"""ezaid lifecycle + governance endpoints (PR-9, ADR-028; CLI_AND_WEBUI
§3/§6): the API and the CLI share one operation per verb (parity), errors
are the shared object, mutating calls honor Idempotency-Key and are audited
with the forwarded identity.

Offline: FastAPI's in-process client over the same faked docker/HTTP seams
the CLI tests use; a bootstrapped platform under tmp_path."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from agentd import platform_cli
from agentd.activation import Platform
from agentd.capability import CapabilityVector
from agentd.config import ControlConfig, load_config
from agentd.control import API_PREFIX, CLIENT_HEADER, CONTRACT_VERSION, USER_HEADER
from agentd.control import api as control_api
from agentd.control.app import create_app
from agentd.control.idempotency import HEADER as IDEMPOTENCY_HEADER
from agentd.control.idempotency import REPLAYED_HEADER
from agentd.main_cli import main
from agentd.platform_cli import build_context
from agentd.registry_v2 import load_registry, save_generation
from agentd.render import write_rendered
from agentd.runtime_descriptor import load_descriptors
from tests.unit.test_activation import seed_registry
from tests.unit.test_lifecycle import FakeEngineHTTP, FakeRunner

REPO_ROOT = Path(__file__).resolve().parents[3]
TOKEN = "test-service-token-9c1"
AUTH = {"Authorization": f"Bearer {TOKEN}", USER_HEADER: "nita", CLIENT_HEADER: "cli"}
V1 = API_PREFIX


@pytest.fixture
def platform_root(tmp_path: Path) -> Path:
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
    return root


@pytest.fixture
def api(platform_root: Path, tmp_path: Path, monkeypatch):
    """An authenticated client over the same seams the CLI tests fake; the
    client carries the config file so tests can run the CLI for parity."""
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    monkeypatch.setattr(platform_cli, "default_runner", FakeRunner())
    monkeypatch.setattr(platform_cli, "http_probe", lambda url: 200)
    monkeypatch.setattr(platform_cli, "engine_http", lambda: FakeEngineHTTP(
        timings={"predicted_per_second": 15.5, "prompt_per_second": 80.0, "predicted_n": 100}))
    monkeypatch.setattr(platform_cli, "build_validator",
                        lambda ctx: lambda descriptor, name, entry: platform_cli.lifecycle
                        .ProbeResult(True, "", 0.2, name))
    monkeypatch.setattr(control_api, "docker_available", lambda: False)
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "llm": {"provider": "scripted", "script_path": str(tmp_path / "script.json")},
        "platform": {"config_dir": str(platform_root / "config")},
        "runs_dir": str(tmp_path / "runs")}), encoding="utf-8")
    (tmp_path / "script.json").write_text("[]")
    ctx = build_context(load_config(cfg_file), platform_root, actor="ezaid")
    app = create_app(ControlConfig(token=TOKEN), ctx, prober=lambda url, headers: (200, "ok"))
    with TestClient(app) as client:
        client.cfg_file = cfg_file  # type: ignore[attr-defined]
        client.ctx = ctx  # type: ignore[attr-defined]
        client.root = platform_root  # type: ignore[attr-defined]
        yield client


def cli_json(client: TestClient, *argv: str, capsys) -> dict:
    code = main([*argv, "--json", "--config", str(client.cfg_file)])
    out = capsys.readouterr().out
    assert code == 0, out
    return json.loads(out)


def registry(client: TestClient):
    return load_registry(client.root / "config")


# ── parity: the API body IS the CLI's --json output ──────────────────────────


@pytest.mark.parametrize("argv, path", [
    (("model", "explain", "coder"), f"{V1}/roles/coder"),
    (("model", "history"), f"{V1}/generations"),
    (("model", "catalog"), f"{V1}/catalog"),
    (("model", "catalog", "--group", "coding"), f"{V1}/catalog/recommendations?group=coding"),
    (("governance", "list"), f"{V1}/governance"),
    (("project", "list"), f"{V1}/projects"),
])
def test_read_verbs_are_the_same_object_in_both_surfaces(api, argv, path, capsys):
    response = api.get(path, headers=AUTH)
    assert response.status_code == 200, response.text
    assert response.json() == cli_json(api, *argv, capsys=capsys)


def test_models_list_is_the_status_snapshot(api, capsys):
    body = api.get(f"{V1}/models", headers=AUTH).json()
    status = cli_json(api, "status", capsys=capsys)
    assert body == {"generation": status["generation"], "models": status["models"]}
    assert body["models"]["alpha"]["state"] == "active"


# ── activation → governance → apply → rollback ──────────────────────────────


def test_activation_flow_through_the_api(api):
    proposal = api.post(f"{V1}/models/beta/activate", headers=AUTH, json={"group": "coding"})
    assert proposal.status_code == 200, proposal.text
    body = proposal.json()
    assert body["request"]["id"] == "cr-0001" and body["request"]["status"] == "pending"
    assert body["request"]["requested_by"] == "nita via cli" and body["applied"] is None
    assert body["request"]["affected_roles"]["coder"]["after"]["primary"] == "beta"
    assert registry(api).generation == 1  # nothing applied yet

    queue = api.get(f"{V1}/governance", headers=AUTH, params={"status": "pending"}).json()
    assert [r["id"] for r in queue["requests"]] == ["cr-0001"]
    shown = api.get(f"{V1}/governance/cr-0001", headers=AUTH).json()["request"]
    assert shown["evidence"]["benchmarks"]["beta"]["tokens_per_s"] == 12.0

    approved = api.post(f"{V1}/governance/cr-0001/approve", headers=AUTH,
                        json={"reason": "looks good"})
    assert approved.status_code == 200, approved.text
    applied = approved.json()["applied"]
    assert applied["ok"] and applied["generation"] == 2 and "rendered only" in applied["message"]
    assert approved.json()["request"]["decision"]["by"] == "nita via cli"
    after = registry(api)
    assert after.generation == 2 and after.resolve("coder").primary == "beta"

    again = api.post(f"{V1}/governance/cr-0001/approve", headers=AUTH, json={})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "governance_rule"
    assert "decisions are made once" in again.json()["error"]["message"]

    rolled = api.post(f"{V1}/generations/rollback", headers=AUTH, json={"reason": "too slow"})
    assert rolled.status_code == 200 and rolled.json()["ok"]
    assert rolled.json()["generation"] == 3
    assert registry(api).resolve("coder").primary == "alpha"
    history = api.get(f"{V1}/generations", headers=AUTH).json()
    assert [g["generation"] for g in history["generations"]] == [1, 2, 3]

    events = [(r.event, r.actor) for r in api.ctx.queue.audit()]
    assert ("api.model_activate", "nita via cli") in events
    assert ("api.governance_approve", "nita via cli") in events
    assert ("api.generation_rollback", "nita via cli") in events
    assert ("request.approved", "nita via cli") in events  # the operation's own audit


def test_policy_approved_activation_applies_at_once(api):
    response = api.post(f"{V1}/models/beta/activate", headers=AUTH, json={"group": "spare"})
    body = response.json()
    assert response.status_code == 200 and body["request"]["status"] == "approved"
    assert body["request"]["decision"]["by"] == "policy"
    assert body["applied"]["ok"] and body["applied"]["generation"] == 2
    assert registry(api).models["beta"].state == "active"


def test_reject_requires_a_reason_and_records_it(api):
    api.post(f"{V1}/models/beta/activate", headers=AUTH, json={"group": "coding"})
    missing = api.post(f"{V1}/governance/cr-0001/reject", headers=AUTH, json={})
    assert missing.status_code == 422 and missing.json()["error"]["code"] == "invalid_request"
    assert "reason" in missing.json()["error"]["message"]
    empty = api.post(f"{V1}/governance/cr-0001/reject", headers=AUTH, json={"reason": "  "})
    assert empty.status_code == 409 and empty.json()["error"]["code"] == "governance_rule"
    rejected = api.post(f"{V1}/governance/cr-0001/reject", headers=AUTH,
                        json={"reason": "regression"})
    assert rejected.status_code == 200
    assert rejected.json()["request"]["status"] == "rejected"
    assert rejected.json()["request"]["decision"]["reason"] == "regression"
    assert rejected.json()["applied"] is None
    listed = api.get(f"{V1}/governance", headers=AUTH, params={"status": "rejected"}).json()
    assert [r["id"] for r in listed["requests"]] == ["cr-0001"]


# ── install · benchmark · upgrade · retire · uninstall ───────────────────────


def test_install_then_benchmark_via_api(api, tmp_path):
    weights = tmp_path / "gamma-q4.gguf"
    weights.write_bytes(b"g" * 4096)
    installed = api.post(f"{V1}/models", headers=AUTH,
                         json={"ref": f"gguf:{weights}", "name": "gamma-q4"})
    assert installed.status_code == 200, installed.text
    body = installed.json()
    assert body["ok"] and body["state"] == "installed" and body["name"] == "gamma-q4"
    assert registry(api).models["gamma-q4"].state == "installed"

    bench = api.post(f"{V1}/models/gamma-q4/benchmark", headers=AUTH)
    assert bench.status_code == 200, bench.text
    assert bench.json()["tokens_per_s"] == 15.5 and bench.json()["state"] == "benchmarked"
    assert bench.json()["via"] == "side-load"
    assert registry(api).models["gamma-q4"].benchmarks["tokens_per_s"] == 15.5

    bad = api.post(f"{V1}/models", headers=AUTH, json={"ref": "s3:nowhere"})
    assert bad.status_code in (409, 422)
    assert bad.json()["error"]["code"] in ("lifecycle_refused", "invalid_request",
                                           "catalog_refused")
    unknown = api.post(f"{V1}/models/nope/benchmark", headers=AUTH)
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "not_found"


def test_upgrade_retire_uninstall_via_api(api):
    upgrade = api.post(f"{V1}/models/upgrade", headers=AUTH, json={"old": "alpha", "new": "beta"})
    assert upgrade.status_code == 200 and upgrade.json()["request"]["kind"] == "upgrade"
    approved = api.post(f"{V1}/governance/cr-0001/approve", headers=AUTH)
    assert approved.status_code == 200 and approved.json()["applied"]["ok"]
    assert registry(api).models["alpha"].state == "retired"

    blocked = api.delete(f"{V1}/models/alpha", headers=AUTH)  # rollback target
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "lifecycle_refused"
    removed = api.delete(f"{V1}/models/alpha", headers=AUTH, params={"force": "true"})
    assert removed.status_code == 200 and removed.json()["removed"] is True
    assert "alpha" not in registry(api).models

    serving = api.post(f"{V1}/models/beta/retire", headers=AUTH)
    assert serving.status_code == 409 and serving.json()["error"]["code"] == "lifecycle_refused"
    missing = api.delete(f"{V1}/models/nope", headers=AUTH)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"


def test_role_explain_and_unknown_role(api):
    coder = api.get(f"{V1}/roles/coder", headers=AUTH).json()
    assert coder["primary"] == "alpha" and coder["ok"] is True
    assert coder["source"] == {"pin": [], "group": "coding"}
    assert coder["checks"]["alpha"]["checks"]["tool_calling"] is True
    unknown = api.get(f"{V1}/roles/nope", headers=AUTH)
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "not_found"


# ── projects ─────────────────────────────────────────────────────────────────


def test_projects_via_api(api, tmp_repo):
    added = api.post(f"{V1}/projects", headers=AUTH, json={"path": str(tmp_repo)})
    assert added.status_code == 200, added.text
    assert added.json()["project"]["name"] == "repo"
    assert added.json()["project"]["added_by"] == "nita via cli"
    listed = api.get(f"{V1}/projects", headers=AUTH).json()
    assert [p["path"] for p in listed["projects"]] == [str(tmp_repo)]
    duplicate = api.post(f"{V1}/projects", headers=AUTH, json={"path": str(tmp_repo)})
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "lifecycle_refused"
    removed = api.delete(f"{V1}/projects", headers=AUTH, params={"target": "repo"})
    assert removed.status_code == 200 and removed.json()["removed"] == "repo"
    gone = api.delete(f"{V1}/projects", headers=AUTH, params={"target": "repo"})
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "not_found"


# ── idempotency · reload guard · audit ───────────────────────────────────────


def test_idempotency_key_replays_the_stored_response_and_detects_conflicts(api):
    headers = {**AUTH, IDEMPOTENCY_HEADER: "k-1"}
    first = api.post(f"{V1}/models/beta/activate", headers=headers, json={"group": "spare"})
    assert first.status_code == 200 and first.json()["applied"]["generation"] == 2
    assert first.headers[IDEMPOTENCY_HEADER] == "k-1" and REPLAYED_HEADER not in first.headers

    replay = api.post(f"{V1}/models/beta/activate", headers=headers, json={"group": "spare"})
    assert replay.status_code == 200 and replay.json() == first.json()
    assert replay.headers[REPLAYED_HEADER] == "true"
    assert registry(api).generation == 2  # the mutation did not run twice

    conflict = api.post(f"{V1}/models/beta/activate", headers=headers, json={"group": "coding"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert api.app.state.idempotency.count() == 1

    too_long = api.post(f"{V1}/models/beta/activate",
                        headers={**AUTH, IDEMPOTENCY_HEADER: "x" * 201}, json={"group": "spare"})
    assert too_long.status_code == 400 and too_long.json()["error"]["code"] == "invalid_request"

    # a refused call is stored too: retrying it replays the refusal (alpha serves)
    refused = api.post(f"{V1}/models/alpha/retire", headers={**AUTH, IDEMPOTENCY_HEADER: "k-2"})
    again = api.post(f"{V1}/models/alpha/retire", headers={**AUTH, IDEMPOTENCY_HEADER: "k-2"})
    assert refused.status_code == again.status_code == 409
    assert again.headers[REPLAYED_HEADER] == "true" and again.json() == refused.json()


def test_reload_is_refused_where_consumers_cannot_be_reloaded(api):
    response = api.post(f"{V1}/models/beta/activate", headers=AUTH,
                        json={"group": "spare", "reload": True})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "reload_unavailable"
    assert "make up" in response.json()["error"]["fix"]
    assert registry(api).generation == 1  # refused before anything happened
    assert api.get(f"{V1}/governance", headers=AUTH).json()["requests"] == []


def test_mutating_calls_are_audited_with_the_forwarded_identity(api):
    api.get(f"{V1}/models", headers=AUTH)
    api.post(f"{V1}/models/beta/activate", headers={**AUTH, IDEMPOTENCY_HEADER: "k-9"},
             json={"group": "spare"})
    api.post(f"{V1}/models/nope/retire", headers={"Authorization": f"Bearer {TOKEN}"})
    records = api.ctx.queue.audit()
    api_events = [r for r in records if r.event.startswith("api.")]
    assert [r.event for r in api_events] == ["api.model_activate", "api.model_retire"]
    assert api_events[0].actor == "nita via cli"
    assert api_events[0].details["idempotency_key"] == "k-9"
    assert api_events[0].details["status"] == 200 and api_events[0].details["client"] == "cli"
    assert api_events[1].actor == "api" and api_events[1].details["status"] == 404
    assert not any(r.event == "api.models_list" for r in records)  # reads are not audited
    # rejected authentication is audited once, as auth.rejected, not as an api.* call
    api.post(f"{V1}/models/beta/retire")
    tail = api.ctx.queue.audit()
    assert tail[-1].event == "auth.rejected"
    assert len([r for r in tail if r.event.startswith("api.")]) == 2


# ── the contract ─────────────────────────────────────────────────────────────


def test_openapi_documents_every_verb_with_its_cli_mapping(api):
    spec = api.get("/openapi.json").json()
    assert spec["info"]["version"] == CONTRACT_VERSION
    operations = {op["operationId"]: (method, path, op)
                  for path, methods in spec["paths"].items() for method, op in methods.items()}
    expected = {"liveness", "aggregated_health", "whoami", "audit_tail", "models_list",
                "model_install", "model_benchmark", "model_activate", "model_upgrade",
                "generation_rollback", "model_retire", "model_uninstall", "role_explain",
                "generation_history", "catalog_list", "catalog_recommend", "governance_list",
                "governance_show", "governance_approve", "governance_reject", "projects_list",
                "project_add", "project_remove",
                # PR-10: the async run registry
                "run_start", "runs_list", "run_get", "run_report", "run_journal", "run_cancel",
                # PR-19 (contract 1.1.0, additive): a registered project's memory
                "project_memory", "project_memory_add",
                # PR-23 (contract 1.2.0, additive): the first-run report
                "first_run_report"}
    assert set(operations) == expected
    for op_id, (method, path, op) in operations.items():
        if not path.startswith(V1):
            continue
        assert op["security"] == [{"serviceToken": []}], op_id
        if method in ("post", "delete"):
            assert "409" in op["responses"] and "404" in op["responses"], op_id
            if not path.startswith(f"{V1}/runs") or op_id == "run_start":
                # run_cancel is the Admin Center's asymmetry (CLI_AND_WEBUI §3)
                assert "CLI: local-ezai" in op["summary"], op_id
    assert "ChangeRequest" in spec["components"]["schemas"]
    assert spec["components"]["schemas"]["Recommendations"]["properties"]["class"]
